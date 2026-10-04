"""Prepare compact text/logo PNGs once, rather than painting every frame in Python."""

from dataclasses import dataclass
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

from ..domain import Color, Style


@dataclass(frozen=True)
class Overlay:
    path: Path
    width: int
    height: int
    x: int
    y: int
    dynamic: bool = False


def placement(width: int, height: int, box: tuple[int, int], style: Style) -> tuple[int, int]:
    margin = round(min(width, height) * style.margin_percent / 100)
    bw, bh = box
    row, column = style.position.value
    x = {"l": margin, "c": (width - bw) // 2, "r": width - margin - bw}[column]
    y = {"t": margin, "c": (height - bh) // 2, "b": height - margin - bh}[row]
    return max(0, x), max(0, y)


def find_font(configured: Path | None) -> Path:
    candidates = (
        [configured]
        if configured
        else [
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
        ]
    )
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise ValueError("فونت پیش‌فرض پیدا نشد. یک فونت TTF/OTF آپلود و انتخاب کنید.")


def text_overlay(
    text: str, font_path: Path, width: int, height: int, style: Style, dst: Path
) -> Overlay:
    style.validate()
    rendered = get_display(arabic_reshaper.reshape(text))
    margin = round(min(width, height) * style.margin_percent / 100)
    target_w = max(1, min(round(width * style.width_percent / 100), width - 2 * margin))
    target_h = max(1, height - 2 * margin)
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    low, high = 1, 4096
    while low < high:
        size = (low + high + 1) // 2
        font = ImageFont.truetype(str(font_path), size)
        left, top, right, bottom = draw.textbbox((0, 0), rendered, font=font)
        if right - left <= target_w and bottom - top <= target_h:
            low = size
        else:
            high = size - 1
    font = ImageFont.truetype(str(font_path), low)
    left, top, right, bottom = draw.textbbox((0, 0), rendered, font=font)
    bw, bh = max(1, right - left), max(1, bottom - top)
    image = Image.new("RGBA", (bw, bh))
    color = 0 if style.color == Color.BLACK else 255
    ImageDraw.Draw(image).text(
        (-left, -top),
        rendered,
        font=font,
        fill=(color, color, color, round(style.opacity * 255 / 100)),
    )
    image.save(dst)
    x, y = placement(width, height, image.size, style)
    return Overlay(dst, bw, bh, x, y, style.color == Color.DYNAMIC)


def logo_overlay(src: Path, width: int, height: int, style: Style, dst: Path) -> Overlay:
    style.validate()
    with Image.open(src) as original:
        image = original.convert("RGBA")
    margin = round(min(width, height) * style.margin_percent / 100)
    target_w = min(round(width * style.width_percent / 100), width - 2 * margin)
    ratio = min(target_w / image.width, (height - 2 * margin) / image.height)
    image = image.resize(
        (max(1, round(image.width * ratio)), max(1, round(image.height * ratio))),
        Image.Resampling.LANCZOS,
    )
    alpha = image.getchannel("A").point(lambda value: round(value * style.opacity / 100))
    image.putalpha(alpha)
    image.save(dst)
    x, y = placement(width, height, image.size, style)
    return Overlay(dst, image.width, image.height, x, y)
