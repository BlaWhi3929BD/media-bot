import asyncio
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
}


def make_caption(info: dict) -> str:
    title = info.get("title") or "Без названия"
    uploader = info.get("uploader") or info.get("channel") or ""
    duration = info.get("duration")
    dur_text = f"\nДлительность: {duration} сек." if duration else ""
    extra = f"\nАвтор: {uploader}" if uploader else ""
    return f"<b>{title}</b>{extra}{dur_text}"


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
        "Отправь мне ссылку одним сообщением, и я попробую скачать медиа и вернуть файл."
    )


@router.message(F.text)
async def handle_text(message: Message) -> None:
    is_private = message.chat.type == "private"

    url = extract_url(message.text or "")

    if not url:
        if is_private:
            await message.answer(
                "Принимаются только ссылки TikTok / X / YouTube / Reddit / Instagram / SoundCloud"
            )
        return

    service = detect_service(url)

    if service == "unknown":
        if is_private:
            await message.answer("Сайт не распознан.")
        return

    if service not in ALLOWED_SERVICES:
        if is_private:
            await message.answer(
                "Этот сервис пока не поддерживается.\nРазрешены: TikTok, X, YouTube, Reddit, Instagram, SoundCloud"
            )
        return

    status = await message.answer(f"Ссылка принята. Источник: {service}. Ожидайте...")

    temp_dir: Optional[Path] = None

    try:
        info, file_path, temp_dir = await download_with_ytdlp(
            url, SETTINGS.download_root
        )

        if not file_path.exists():
            raise FileNotFoundError("Downloaded file not found")

        await status.edit_text(f"Файл готов. Отправляю: {service}.")
        await send_downloaded_file(message, file_path, info)

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as exc:
        log.exception("Failed to process url %s", url)

        if is_private:
            await status.edit_text(f"Ошибка: {type(exc).__name__}: {exc}")
        else:
            await status.delete()

    finally:
        if temp_dir and temp_dir.exists():
            await cleanup_path(temp_dir)
