"""Shared output size and disk checks for video and GIF exports."""

import shutil
from pathlib import Path

from ..config import Config
from .process import Progress


def check_space(config: Config, folder: Path) -> None:
    if shutil.disk_usage(folder).free < config.min_free_disk_mb * 1024**2:
        raise ValueError("فضای آزاد دیسک برای تبدیل کافی نیست.")


def check_output(config: Config, output: Path) -> None:
    if output.exists() and output.stat().st_size > config.max_upload_mb * 1024**2:
        raise ValueError("حجم خروجی از حد ارسال گذشت؛ بازهٔ کوتاه‌تری انتخاب کنید.")


def guarded_progress(config: Config, output: Path, progress: Progress | None) -> Progress:
    async def guard(percent: float) -> None:
        check_output(config, output)
        check_space(config, output.parent)
        if progress:
            await progress(percent)

    return guard
