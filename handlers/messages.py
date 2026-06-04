import asyncio
import contextlib
import logging
import re
import shutil
from collections import deque
from html import escape
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, Message

from config import SETTINGS
from services.download import download_with_ytdlp

log = logging.getLogger(__name__)
router = Router()

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(SETTINGS.max_workers)
QUEUE = deque()

URL_RE = re.compile(
    r"(https?://[^\s<>()]+|www\.[^\s<>()]+|(?:vt\.tiktok\.com|tiktok\.com|youtu\.be|youtube\.com|x\.com|twitter\.com|instagram\.com|reddit\.com|soundcloud\.com|open\.spotify\.com)/[^\s<>()]+)",
    re.IGNORECASE,
)

ALLOWED_SERVICES = {
    "tiktok",
    "x",
    "youtube",
    "reddit",
    "instagram",
    "soundcloud",
    "spotify",
}

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".gif"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav"}


def extract_url(text: str) -> Optional[str]:
    if not text:
        return None

    match = URL_RE.search(text)
    if not match:
        return None

    url = match.group(0).strip().rstrip(").,]!?:;\"'")

    if not url.startswith("http"):
        url = "https://" + url

    parsed = urlparse(url)
    if not parsed.netloc:
        return None

    return url


def detect_service(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    host = host.removeprefix("www.")

    if "youtube.com" in host or host == "youtu.be":
        return "youtube"
    if "tiktok.com" in host:
        return "tiktok"
    if "x.com" in host or "twitter.com" in host:
        return "x"
    if "reddit.com" in host:
        return "reddit"
    if "instagram.com" in host:
        return "instagram"
    if "soundcloud.com" in host:
        return "soundcloud"
    if "spotify.com" in host:
        return "spotify"

    return "unknown"


def make_caption(info: dict) -> str:
    title = escape(str(info.get("title") or "Без названия"))
    uploader = escape(str(info.get("uploader") or info.get("channel") or ""))
    duration = info.get("duration")

    extra = f"\nАвтор: {uploader}" if uploader else ""
    dur_text = f"\nДлительность: {duration} сек." if duration else ""

    return f"<b>{title}</b>{extra}{dur_text}"


def make_bar(percent: int, size: int = 12) -> str:
    percent = max(0, min(100, percent))
    filled = int(size * percent / 100)
    return "█" * filled + "░" * (size - filled)


async def cleanup_path(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


async def send_downloaded_files(
    message: Message, files: list[Path], info: dict
) -> None:
    caption = make_caption(info)

    visual_paths = [f for f in files if f.suffix.lower() in PHOTO_EXT | VIDEO_EXT]
    audio_files = [f for f in files if f.suffix.lower() in AUDIO_EXT]
    other_files = [
        f for f in files if f.suffix.lower() not in PHOTO_EXT | VIDEO_EXT | AUDIO_EXT
    ]

    async def send_visual_batch(batch: list[Path], with_caption: bool) -> None:
        media = []
        for idx, f in enumerate(batch):
            ext = f.suffix.lower()
            media_caption = caption if with_caption and idx == 0 else None

            if ext in PHOTO_EXT:
                media.append(
                    InputMediaPhoto(
                        media=FSInputFile(f),
                        caption=media_caption,
                        parse_mode=ParseMode.HTML if media_caption else None,
                    )
                )
            else:
                media.append(
                    InputMediaVideo(
                        media=FSInputFile(f),
                        caption=media_caption,
                        parse_mode=ParseMode.HTML if media_caption else None,
                    )
                )

        if len(media) == 1:
            single = media[0]
            if isinstance(single, InputMediaPhoto):
                await message.answer_photo(
                    photo=single.media,
                    caption=single.caption,
                    parse_mode=single.parse_mode,
                )
            else:
                await message.answer_video(
                    video=single.media,
                    caption=single.caption,
                    parse_mode=single.parse_mode,
                )
        else:
            await message.answer_media_group(media=media)

    for start in range(0, len(visual_paths), 10):
        batch = visual_paths[start : start + 10]
        await send_visual_batch(
            batch, with_caption=(start == 0 and not audio_files and not other_files)
        )

    for audio_path in audio_files:
        await message.answer_audio(
            audio=FSInputFile(audio_path),
            caption=caption
            if not visual_paths and audio_path == audio_files[0]
            else None,
            parse_mode=ParseMode.HTML
            if (not visual_paths and audio_path == audio_files[0])
            else None,
        )

    for file_path in other_files:
        await message.answer_document(
            document=FSInputFile(file_path),
            caption=caption
            if not visual_paths and not audio_files and file_path == other_files[0]
            else None,
            parse_mode=ParseMode.HTML
            if (not visual_paths and not audio_files and file_path == other_files[0])
            else None,
        )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Отправьте ссылку одним сообщением, и я попробую скачать медиа и вернуть файл."
    )


@router.message(F.text)
async def handle_text(message: Message) -> None:
    is_private = message.chat.type == "private"
    url = extract_url(message.text or "")

    if not url:
        if is_private:
            await message.answer(
                "Принимаются только ссылки TikTok / X / YouTube / Reddit / Instagram / SoundCloud / Spotify"
            )
        return

    service = detect_service(url)

    if service == "unknown" or service not in ALLOWED_SERVICES:
        if is_private:
            await message.answer(
                "Этот сервис пока не поддерживается или сайт не распознан."
            )
        return

    queue_id = object()
    QUEUE.append(queue_id)
    position = len(QUEUE)

    progress_msg = await message.answer(
        f"⌛ Ссылка принята ({service})\nПозиция в очереди: #{position}"
    )

    temp_dir: Optional[Path] = None
    download_task: Optional[asyncio.Task] = None
    queue_task: Optional[asyncio.Task] = None
    progress_done = asyncio.Event()

    try:

        async def queue_updater() -> None:
            last_pos = None
            while queue_id in QUEUE:
                try:
                    pos = QUEUE.index(queue_id) + 1
                except ValueError:
                    break

                if pos != last_pos:
                    last_pos = pos
                    with contextlib.suppress(Exception):
                        await progress_msg.edit_text(
                            f"⌛ Ссылка принята ({service})\nПозиция в очереди: #{pos}"
                        )

                await asyncio.sleep(3)

        queue_task = asyncio.create_task(queue_updater())

        async with asyncio.timeout(180):
            async with DOWNLOAD_SEMAPHORE:
                with contextlib.suppress(ValueError):
                    QUEUE.remove(queue_id)

                if queue_task:
                    queue_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await queue_task

                with contextlib.suppress(Exception):
                    await progress_msg.edit_text(
                        f"⬇️ Скачиваю медиа из {service}...\n[░░░░░░░░░░░░]"
                    )

                progress_state = {"last_percent": 0}
                last_percent = 0

                async def progress_updater() -> None:
                    nonlocal last_percent
                    while not progress_done.is_set():
                        percent = int(progress_state.get("last_percent", 0))
                        if percent != last_percent and (
                            percent - last_percent >= 10 or percent == 100
                        ):
                            last_percent = percent
                            bar = make_bar(percent)
                            with contextlib.suppress(Exception):
                                await progress_msg.edit_text(
                                    f"⬇️ Downloading: {percent}%\n[{bar}]"
                                )
                        await asyncio.sleep(1)

                download_task = asyncio.create_task(progress_updater())

                info, files, temp_dir, _ = await download_with_ytdlp(
                    url, SETTINGS.download_root, progress_state=progress_state
                )

                if not files:
                    raise FileNotFoundError("Скачанные файлы не найдены.")

                progress_done.set()
                if download_task:
                    download_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await download_task

                with contextlib.suppress(Exception):
                    await progress_msg.edit_text("Загрузка завершена. Отправляю...")

                await send_downloaded_files(message, files, info)

                with contextlib.suppress(Exception):
                    await progress_msg.delete()

    except Exception as exc:
        log.exception("Failed to process url %s", url)
        if is_private:
            error_text = (
                f"Ошибка: {exc}"
                if isinstance(exc, ValueError)
                else f"Ошибка: {type(exc).__name__}"
            )
            with contextlib.suppress(Exception):
                await progress_msg.edit_text(error_text)
        else:
            with contextlib.suppress(Exception):
                await progress_msg.delete()

    finally:
        progress_done.set()

        if download_task and not download_task.done():
            download_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await download_task

        if temp_dir and temp_dir.exists():
            await cleanup_path(temp_dir)

        with contextlib.suppress(ValueError):
            QUEUE.remove(queue_id)

        if queue_task and not queue_task.done():
            queue_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await queue_task
