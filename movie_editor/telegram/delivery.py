"""Original-file delivery, with bounded-memory byte parts for large exports."""

import asyncio
import hashlib
import io
import json
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..config import Config
from . import reassemble

UploadProgress = Callable[[int, int], Awaitable[None]]


class FileSlice(io.RawIOBase):
    """Seekable view over one part; Telethon never loads the whole export into RAM."""

    def __init__(self, path: Path, offset: int, size: int, name: str):
        super().__init__()
        self._file = path.open("rb")
        self.offset, self.size, self.name = offset, size, name
        self.position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        self._checkClosed()
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._checkClosed()
        origin = {io.SEEK_SET: 0, io.SEEK_CUR: self.position, io.SEEK_END: self.size}[whence]
        position = origin + offset
        if position < 0:
            raise ValueError("Negative seek")
        self.position = position
        return position

    def read(self, size: int = -1) -> bytes:
        self._checkClosed()
        remaining = max(0, self.size - self.position)
        size = remaining if size < 0 else min(size, remaining)
        self._file.seek(self.offset + self.position)
        block = self._file.read(size)
        self.position += len(block)
        return block

    def close(self) -> None:
        self._file.close()
        super().close()


async def manifest_for(path: Path, limit: int) -> dict:
    name = f"pixelcut-{secrets.token_hex(6)}{path.suffix.lower()}"
    size = path.stat().st_size
    digest = hashlib.sha256()
    parts = []
    with path.open("rb") as source:
        for index, offset in enumerate(range(0, size, limit), 1):
            part_size = min(limit, size - offset)
            part_hash = hashlib.sha256()
            remaining = part_size
            while remaining:
                block = source.read(min(4 * 1024**2, remaining))
                if not block:
                    raise ValueError("فایل خروجی هنگام آماده‌سازی ارسال ناقص شد.")
                digest.update(block)
                part_hash.update(block)
                remaining -= len(block)
                await asyncio.sleep(0)
            parts.append(
                {
                    "name": f"{name}.part{index:04d}",
                    "size": part_size,
                    "sha256": part_hash.hexdigest(),
                }
            )
    return {"name": name, "size": size, "sha256": digest.hexdigest(), "parts": parts}


class FileDelivery:
    def __init__(self, client, config: Config):
        self.client, self.config = client, config

    @property
    def limit(self) -> int:
        # Keep each upload conservative even if MAX_UPLOAD_MB is configured above 2 GiB.
        return max(1, min(1900 * 1024**2, int(self.config.max_upload_mb * 1024**2)))

    async def send(self, path: Path, *, progress: UploadProgress, caption: str, **options) -> bool:
        """Return True for multipart delivery. Never compress or alter the export."""
        if path.stat().st_size <= self.limit:
            await self.client.send_file(
                self.config.admin_id,
                str(path),
                caption=caption,
                parse_mode=None,
                progress_callback=progress,
                **options,
            )
            return False
        await self.client.send_message(
            self.config.admin_id,
            f"حجم خروجی {path.stat().st_size / 1024**2:.1f} MiB است؛ "
            "کیفیت منبع افزایش پیدا نکرده، ذخیرهٔ بدون اتلاف حجم بیشتری دارد.\n"
            "فایل بدون تغییر در چند قسمت ارسال می‌شود. قسمت‌ها جداگانه قابل پخش نیستند؛ "
            "بعد از دریافت همهٔ قسمت‌ها، راهنمای اتصال را اجرا کنید. "
            "برای یک فایل کوچک‌تر، گزینهٔ «خروجی فشرده» را انتخاب کنید.",
            parse_mode=None,
        )
        manifest = await manifest_for(path, self.limit)
        manifest_path = path.parent / "export.manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        manifest_name = f"{Path(manifest['name']).stem}.manifest.json"
        offset = 0
        for index, part in enumerate(manifest["parts"], 1):

            async def part_progress(current: int, total: int, base: int = offset) -> None:
                await progress(base + current, manifest["size"])

            with FileSlice(path, offset, part["size"], part["name"]) as stream:
                await self.client.send_file(
                    self.config.admin_id,
                    stream,
                    file_size=part["size"],
                    force_document=True,
                    mime_type="application/octet-stream",
                    caption=f"قسمت {index} از {len(manifest['parts'])}. " + caption,
                    parse_mode=None,
                    progress_callback=part_progress,
                )
            offset += part["size"]
        with manifest_path.open("rb") as source:
            manifest_file = io.BytesIO(source.read())
        manifest_file.name = manifest_name
        await self.client.send_file(
            self.config.admin_id,
            manifest_file,
            force_document=True,
            caption="فهرست قسمت‌ها و checksum خروجی؛ کنار قسمت‌ها ذخیره کنید.",
            parse_mode=None,
        )
        script = io.BytesIO(Path(reassemble.__file__).read_bytes())
        script.name = "restore.py"
        await self.client.send_file(
            self.config.admin_id,
            script,
            force_document=True,
            caption=(
                "همهٔ قسمت‌ها، فایل manifest و این فایل را در یک پوشه ذخیره کنید. "
                f"سپس اجرا کنید:\npython3 restore.py {manifest_name}\n"
                "خروجی دقیقاً برابر فایل کامل است؛ SHA-256 کنترل می‌شود. "
                "روی ویندوز به‌جای python3 از python استفاده کنید."
            ),
            parse_mode=None,
        )
        return True
