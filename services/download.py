import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from yt_dlp import YoutubeDL

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
GIF_EXT = {".gif"}
VALID_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT | GIF_EXT


def collect_downloaded_files(temp_dir: Path) -> list[Path]:
    files = [
        p for p in temp_dir.rglob("*") if p.is_file() and p.suffix.lower() in VALID_EXT
    ]
    files.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return files


def download_with_ytdlp_sync(
    url: str,
    temp_dir: Path,
    progress_state: dict,
) -> tuple[dict[str, Any], list[Path]]:
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
        "outtmpl": str(temp_dir / "%(playlist_index)s_%(title).120s [%(id)s].%(ext)s"),
        "noplaylist": False,
        "playlistend": 10,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "socket_timeout": 10,
        "retries": 2,
        "extractor_retries": 2,
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

    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget

        if hasattr(ImpersonateTarget, "from_str"):
            ydl_opts["impersonate"] = ImpersonateTarget.from_str("chrome")
        else:
            ydl_opts["impersonate"] = ImpersonateTarget(browser="chrome")
    except Exception:
        ydl_opts.pop("impersonate", None)

    if cookies_path.exists():
        ydl_opts["cookiefile"] = str(cookies_path.resolve())

    ytdlp_error: Exception | None = None

    try:
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True) or {}
        except Exception as e:
            if "Impersonate target" in str(e) and "impersonate" in ydl_opts:
                ydl_opts.pop("impersonate", None)
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True) or {}
            else:
                raise e

        files = collect_downloaded_files(temp_dir)
        if files:
            return info, files
        ytdlp_error = RuntimeError("yt-dlp завершился, но файлы не были найдены")
    except Exception as exc:
        ytdlp_error = exc

    if not shutil.which("gallery-dl"):
        raise ytdlp_error or RuntimeError(
            "yt-dlp failed and gallery-dl is not installed"
        )

    cmd = ["gallery-dl", "-d", str(temp_dir), url]
    if cookies_path.exists():
        cmd.extend(["--cookies", str(cookies_path.resolve())])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"gallery-dl failed (code {result.returncode}): {result.stderr.strip()}"
        )

    files = collect_downloaded_files(temp_dir)
    if not files:
        raise RuntimeError("gallery-dl не нашёл медиа-файлы")

    info = {
        "title": url,
    }
    return info, files


async def download_with_ytdlp(
    url: str,
    download_root: Path,
    progress_state: Optional[dict] = None,
):
    download_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="media_bot_", dir=download_root))

    if progress_state is None:
        progress_state = {"last_percent": 0}

    try:
        info, files = await asyncio.to_thread(
            download_with_ytdlp_sync, url, temp_dir, progress_state
        )
        return info, files, temp_dir, progress_state
    except Exception:
        await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)
        raise
