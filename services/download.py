import asyncio
import tempfile
from pathlib import Path
from typing import Any, Optional

from yt_dlp import YoutubeDL

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav"}
VALID_EXT = VIDEO_EXT | AUDIO_EXT


def download_with_ytdlp_sync(url: str, temp_dir: Path) -> tuple[dict[str, Any], Path]:
    cookies_path = Path("cookies.txt")

    ydl_opts = {
        "outtmpl": str(temp_dir / "%(title).120s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "socket_timeout": 15,
        "retries": 3,
        "fragment_retries": 3,
        "ignoreerrors": True,
        "no_check_certificate": True,
    }

    # безопасно подключаем cookies только если файл существует
    if cookies_path.exists():
        ydl_opts["cookiefile"] = str(cookies_path.resolve())

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        if not isinstance(info, dict):
            raise RuntimeError("yt-dlp did not return a metadata dict")

    files = [
        p
        for p in temp_dir.rglob("*")
        if p.is_file()
        and not p.name.endswith(".part")
        and p.suffix.lower() in VALID_EXT
    ]

    if not files:
        raise RuntimeError("Downloaded media file was not found")

    file_path = max(files, key=lambda p: p.stat().st_size)

    return info, file_path


async def download_with_ytdlp(
    url: str, download_root: Path
) -> tuple[dict[str, Any], Path, Path]:

    download_root.mkdir(parents=True, exist_ok=True)

    temp_dir = Path(tempfile.mkdtemp(prefix="media_bot_", dir=download_root))

    info, file_path = await asyncio.to_thread(download_with_ytdlp_sync, url, temp_dir)

    return info, file_path, temp_dir
