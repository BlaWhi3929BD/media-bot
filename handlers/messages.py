import asyncio
import contextlib
import logging
import re
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message

from config import SETTINGS
from services.download import download_with_ytdlp

log = logging.getLogger(__name__)
router = Router()

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(SETTINGS.max_workers)

URL_RE = re.compile(
    r"(https?://[^\s<>()]+|www\.[^\s<>()]+|(?:vt\.tiktok\.com|tiktok\.com|youtu\.be|youtube\.com|x\.com|twitter\.com|instagram\.com|reddit\.com|soundcloud\.com|open\.spotify\.com)/[^\s<>()]+)",
    re.IGNORECASE,
)


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


ALLOWED_SERVICES = {
    "tiktok",
    "x",
    "youtube",
    "reddit",
    "instagram",
    "soundcloud",
    "spotify",
}


def make_caption(info: dict) -> str:
    title = info.get("title") or "Без названия"
    uploader = info.get("uploader") or info.get("channel") or ""
    duration = info.get("duration")
    dur_text = f"\nДлительность: {duration} сек." if duration else ""
    extra = f"\nАвтор: {uploader}" if uploader else ""
    return f"<b>{title}</b>{extra}{dur_text}"


def make_bar(percent: int, size: int = 12) -> str:
    filled = int(size * percent / 100)
    return "█" * filled + "░" * (size - filled)


async def cleanup_path(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path)


async def send_downloaded_file(message: Message, file_path: Path, info: dict) -> None:
    caption = make_caption(info)
    suffix = file_path.suffix.lower()
    file = FSInputFile(str(file_path))

    if suffix in {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav"}:
        await message.answer_audio(
            audio=file, caption=caption, parse_mode=ParseMode.HTML
        )
        return

    if suffix in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
        await message.answer_video(
            video=file, caption=caption, parse_mode=ParseMode.HTML
        )
        return

    await message.answer_document(
        document=file, caption=caption, parse_mode=ParseMode.HTML
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Отправь ссылку одним сообщением, и я попробую скачать медиа и вернуть файл."
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

    progress_msg = await message.answer(
        f"⬇️ Ссылка принята ({service}). Ожидание очереди...\n[░░░░░░░░░░░░]"
    )
    temp_dir: Optional[Path] = None
    task: Optional[asyncio.Task] = None

    try:
        async with DOWNLOAD_SEMAPHORE:
            await progress_msg.edit_text(
                f"⬇️ Скачиваю медиа из {service}...\n[░░░░░░░░░░░░]"
            )

            progress_state = {"last_percent": 0}
            last_percent = 0
            stop_flag = False

            async def progress_updater():
                nonlocal last_percent
                while not stop_flag:
                    percent = progress_state.get("last_percent", 0)
                    if percent - last_percent >= 10:
                        last_percent = percent
                        bar = make_bar(percent)
                        with contextlib.suppress(Exception):
                            await progress_msg.edit_text(
                                f"⬇️ Downloading: {percent}%\n[{bar}]"
                            )
                    await asyncio.sleep(1)

            task = asyncio.create_task(progress_updater())

            info, file_path, temp_dir, _ = await download_with_ytdlp(
                url, SETTINGS.download_root, progress_state=progress_state
            )

            stop_flag = True
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            if not file_path.exists():
                raise FileNotFoundError("Скачанный файл исчез или не был найден.")

            with contextlib.suppress(Exception):
                await progress_msg.edit_text("Загрузка завершена. Отправляю файл...")

            await send_downloaded_file(message, file_path, info)

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
        if task and not task.done():
            task.cancel()

        if temp_dir and temp_dir.exists():
            await cleanup_path(temp_dir)
