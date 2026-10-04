"""Validate uploaded fonts, logos and reusable video clips before retaining them."""

import asyncio
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageFont

from .config import Config
from .domain import Asset
from .media.probe import inspect
from .storage import Store

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


class AssetService:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    def destination(self, kind: str, filename: str) -> Path:
        extension = Path(filename).suffix.lower()
        allowed = {
            "font": {".ttf", ".otf"},
            "logo": {".png", ".webp", ".jpg", ".jpeg"},
            "intro": VIDEO_EXTENSIONS,
            "outro": VIDEO_EXTENSIONS,
        }
        if extension not in allowed[kind]:
            raise ValueError("پسوند مناسب نیست. مجاز: " + ", ".join(sorted(allowed[kind])))
        return self.config.data_dir / "assets" / f"{uuid4().hex}{extension}"

    async def retain(self, path: Path, kind: str, filename: str) -> Asset:
        if kind == "font":

            def check_font():
                font = ImageFont.truetype(str(path), 32)
                font.getmask("سلام ABC 123")

            await asyncio.to_thread(check_font)
        elif kind == "logo":

            def check_logo():
                with Image.open(path) as image:
                    if image.width * image.height > 16_000_000 or getattr(image, "n_frames", 1) > 1:
                        raise ValueError("لوگو باید تصویر ثابت و حداکثر ۱۶ مگاپیکسل باشد.")
                    image.verify()

            await asyncio.to_thread(check_logo)
        else:
            await inspect(path, self.config)
        asset = Asset(uuid4().hex[:12], kind, Path(filename).name[:80], str(path))
        self.store.add_asset(asset)
        return asset

    def delete(self, asset_id: str, kind: str, used: set[str]) -> None:
        asset = self.store.asset(asset_id, kind)
        if asset is None:
            return
        if asset_id in used:
            raise ValueError(
                "این فایل در ویرایش فعلی یا پیش‌فرض استفاده می‌شود؛ ابتدا انتخاب را بردارید."
            )
        path = Path(asset.path).resolve()
        if not path.is_relative_to((self.config.data_dir / "assets").resolve()):
            raise ValueError("مسیر فایل کتابخانه معتبر نیست.")
        path.unlink(missing_ok=True)
        self.store.delete_asset(asset_id)
