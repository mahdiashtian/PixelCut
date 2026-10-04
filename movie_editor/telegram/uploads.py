"""Telegram downloads with limits, validation and cleanup of incomplete files."""

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from telethon.errors import MessageNotModifiedError

from ..assets import VIDEO_EXTENSIONS, AssetService
from ..config import Config
from ..domain import Asset
from ..media.probe import inspect
from .views import button


@dataclass(frozen=True)
class SourceVideo:
    id: str
    path: Path
    name: str


class UploadService:
    def __init__(self, client, config: Config, assets: AssetService):
        self.client, self.config, self.assets = client, config, assets

    async def receive(self, event, kind: str) -> SourceVideo | Asset:
        file = event.file
        if not file:
            raise ValueError("فایل قابل دریافت نیست.")
        name = file.name or ("logo.jpg" if event.photo else "video.mp4")
        max_mb = self.config.max_font_mb if kind in {"font", "logo"} else self.config.max_upload_mb
        if (file.size or 0) > max_mb * 1024**2:
            raise ValueError(f"حجم فایل از {max_mb} مگابایت بیشتر است.")
        if shutil.disk_usage(self.config.data_dir).free < (
            (file.size or 0) * 3 + self.config.min_free_disk_mb * 1024**2
        ):
            raise ValueError("فضای آزاد سرور برای دریافت و پردازش این فایل کافی نیست.")
        if kind == "source":
            ext = Path(name).suffix.lower()
            if ext not in VIDEO_EXTENSIONS:
                raise ValueError("ویدیوی MP4/MOV/MKV/WebM/AVI/M4V ارسال کنید.")
            source_id = uuid4().hex[:12]
            folder = self.config.data_dir / "jobs" / source_id
            folder.mkdir(parents=True)
            destination = folder / f"source{ext}"
        else:
            destination = self.assets.destination(kind, name)
        retained = False
        status = None
        try:
            stop_buttons = [[button("توقف دریافت", "g:stop")]]
            status = await self.client.send_message(
                self.config.admin_id, "در حال دریافت فایل…", buttons=stop_buttons
            )
            last_update = 0.0

            async def progress(current, total):
                nonlocal last_update
                now = asyncio.get_running_loop().time()
                if now - last_update >= 5 and total:
                    last_update = now
                    try:
                        await status.edit(
                            f"در حال دریافت: {current / total * 100:.0f}٪", buttons=stop_buttons
                        )
                    except MessageNotModifiedError:
                        pass

            async with asyncio.timeout(1800):
                downloaded = await self.client.download_media(
                    event.message, file=str(destination), progress_callback=progress
                )
            if not downloaded or not destination.is_file():
                raise ValueError("دریافت فایل کامل نشد؛ دوباره ارسال کنید.")
            if destination.stat().st_size > max_mb * 1024**2:
                raise ValueError("فایل دریافتی از محدودیت حجم بزرگ‌تر است.")
            if kind == "source":
                await inspect(destination, self.config)
                result = SourceVideo(source_id, destination, Path(name).name[:150])
            else:
                result = await self.assets.retain(destination, kind, name)
            retained = True
            await status.edit("فایل دریافت و اعتبارسنجی شد.", buttons=None)
            return result
        finally:
            if not retained:
                destination.unlink(missing_ok=True)
                if kind == "source" and destination.parent.exists():
                    destination.parent.rmdir()
