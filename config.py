from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv

load_dotenv()


@dataclass(slots=True)
class Settings:
    bot_token: str
    download_root: Path = Path("data/downloads")
    max_workers: int = 2


SETTINGS = Settings(
    bot_token=os.getenv("BOT_TOKEN", "").strip(),
    download_root=Path(os.getenv("DOWNLOAD_ROOT", "data/downloads")).expanduser(),
    max_workers=int(os.getenv("MAX_WORKERS", "2")),
)

if not SETTINGS.bot_token:
    raise RuntimeError("BOT_TOKEN is missing in environment")
