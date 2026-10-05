"""Validated environment configuration; credentials never live in source code."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    admin_id: int
    data_dir: Path
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    default_font: Path | None = None
    max_upload_mb: int = 1900
    max_font_mb: int = 20
    max_video_seconds: int = 7200
    max_render_seconds: int = 14400
    max_pixels: int = 33177600
    min_free_disk_mb: int = 2048
    threads: int = 4
    max_lut_mb: int = 20
    max_render_mb: int = 20000
    max_ffmpeg_memory_mb: int = 1536

    @classmethod
    def from_env(cls, require_telegram: bool = True) -> "Config":
        load_dotenv()
        required = [
            "TELEGRAM_API_ID",
            "TELEGRAM_API_HASH",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ADMIN_ID",
        ]
        missing = [key for key in required if not os.getenv(key, "").strip()]
        if missing and require_telegram:
            raise ValueError("متغیرهای .env تنظیم نشده‌اند: " + ", ".join(missing))

        def positive(key: str, default: int | None = None) -> int:
            try:
                raw = os.getenv(key, "").strip() or (str(default) if default is not None else "")
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{key} باید عدد صحیح مثبت باشد.") from exc
            if value <= 0:
                raise ValueError(f"{key} باید مثبت باشد.")
            return value

        font = os.getenv("DEFAULT_FONT_PATH", "").strip()
        return cls(
            api_id=positive("TELEGRAM_API_ID", None if require_telegram else 1),
            api_hash=os.getenv("TELEGRAM_API_HASH", "").strip(),
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            admin_id=positive("TELEGRAM_ADMIN_ID", None if require_telegram else 1),
            data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
            ffmpeg=os.getenv("FFMPEG_BIN", "ffmpeg"),
            ffprobe=os.getenv("FFPROBE_BIN", "ffprobe"),
            default_font=Path(font).resolve() if font else None,
            max_upload_mb=positive("MAX_UPLOAD_MB", 1900),
            max_font_mb=positive("MAX_FONT_MB", 20),
            max_video_seconds=positive("MAX_VIDEO_SECONDS", 7200),
            max_render_seconds=positive("MAX_RENDER_SECONDS", 14400),
            max_pixels=positive("MAX_PIXELS", 33177600),
            min_free_disk_mb=positive("MIN_FREE_DISK_MB", 2048),
            threads=positive("FFMPEG_THREADS", 4),
            max_lut_mb=positive("MAX_LUT_MB", 20),
            max_render_mb=positive("MAX_RENDER_MB", 20000),
            max_ffmpeg_memory_mb=positive("MAX_FFMPEG_MEMORY_MB", 1536),
        )

    def prepare(self) -> None:
        for binary in (self.ffmpeg, self.ffprobe):
            if shutil.which(binary) is None:
                raise ValueError(f"برنامه پیدا نشد: {binary}. مسیر آن را در .env تنظیم کنید.")
        if self.default_font and not self.default_font.is_file():
            raise ValueError("DEFAULT_FONT_PATH به فایل موجود اشاره نمی‌کند.")
        for folder in (self.data_dir, self.data_dir / "assets", self.data_dir / "jobs"):
            folder.mkdir(parents=True, exist_ok=True)
