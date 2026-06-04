import asyncio
import tempfile
from pathlib import Path
from typing import Any, Optional

from yt_dlp import YoutubeDL

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav"}
VALID_EXT = VIDEO_EXT | AUDIO_EXT


def download_with_ytdlp_sync(
    url: str,
    temp_dir: Path,
    progress_state: dict,
) -> tuple[dict[str, Any], Path]:
    cookies_path = Path("cookies.txt")

    def progress_hook(d):
        if d.get("status") != "downloading":
            return

        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes")

        if not total or not downloaded:
            return

        percent = int(downloaded / total * 100)

        if percent - progress_state.get("last_percent", 0) < 1:
            return

        progress_state["last_percent"] = percent

    ydl_opts = {
        "outtmpl": str(temp_dir / "%(title).120s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "socket_timeout": 15,
        "retries": 3,
        "fragment_retries": 3,
        "ignoreerrors": False,
        "no_check_certificate": True,
        "progress_hooks": [progress_hook],
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
    }

    # Мягкая проверка: активируем маскировку под Chrome только если curl-cffi доступен
    try:
        import curl_cffi

        # Импортируем целевой класс для маскировки сетевых запросов
        from yt_dlp.networking.impersonate import ImpersonateTarget

        # Для API библиотеки yt-dlp требует объект ImpersonateTarget, а не просто строку
        if hasattr(ImpersonateTarget, "from_str"):
            ydl_opts["impersonate"] = ImpersonateTarget.from_str("chrome")
        else:
            ydl_opts["impersonate"] = ImpersonateTarget(browser="chrome")

    except Exception:
        # Если curl_cffi нет, либо изменился внутренний API yt-dlp,
        # просто убираем ключ, чтобы бот продолжил работу на стандартных запросах
        ydl_opts.pop("impersonate", None)

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
    url: str, download_root: Path, progress_state: Optional[dict] = None
):
    download_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="media_bot_", dir=download_root))

    if progress_state is None:
        progress_state = {"last_percent": 0}

    info, file_path = await asyncio.to_thread(
        download_with_ytdlp_sync, url, temp_dir, progress_state
    )
    return info, file_path, temp_dir, progress_state
