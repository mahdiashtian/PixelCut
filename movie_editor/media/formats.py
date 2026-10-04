"""Keep the source pixel layout for lossless compositing whenever supported."""

from dataclasses import dataclass

from .probe import MediaInfo


@dataclass(frozen=True)
class PixelLayout:
    working: str
    encoded: str
    overlay: str
    horizontal_grid: int = 1
    vertical_grid: int = 1


def layout_for(info: MediaInfo) -> PixelLayout:
    # No round trip through RGB for the common planar YUV inputs.
    layouts = {
        "yuv420p": PixelLayout("yuv420p", "yuv420p", "yuv420", 2, 2),
        "yuv422p": PixelLayout("yuv422p", "yuv422p", "yuv422", 2, 1),
        "yuv444p": PixelLayout("yuv444p", "yuv444p", "yuv444"),
        "rgb24": PixelLayout("bgr0", "bgr0", "rgb"),
        "bgr24": PixelLayout("bgr0", "bgr0", "rgb"),
        "bgr0": PixelLayout("bgr0", "bgr0", "rgb"),
        "rgb0": PixelLayout("bgr0", "bgr0", "rgb"),
        "gbrp": PixelLayout("bgr0", "bgr0", "rgb"),
    }
    if info.pixel_format not in layouts:
        raise ValueError(
            "فرمت رنگ این ویدیو در مسیر بدون افت پشتیبانی نمی‌شود. "
            "برای جلوگیری از تبدیل ناخواسته، خروجی ساخته نشد."
        )
    return layouts[info.pixel_format]
