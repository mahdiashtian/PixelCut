"""Local media preflight, independent of logging into Telegram."""

from .config import Config
from .media.graphics import find_font
from .media.process import capture


async def check(config: Config) -> None:
    config.prepare()
    version = (await capture([config.ffmpeg, "-version"])).decode(errors="replace").splitlines()[0]
    filters = (await capture([config.ffmpeg, "-hide_banner", "-filters"])).decode()
    encoders = (await capture([config.ffmpeg, "-hide_banner", "-encoders"])).decode()
    missing = [
        name
        for name in (
            "xfade",
            "acrossfade",
            "overlay",
            "alphamerge",
            "lut",
            "concat",
            "palettegen",
            "paletteuse",
        )
        if f" {name} " not in filters
    ]
    missing.extend(
        name
        for name in ("libx264", "ffv1", "aac", "pcm_f32le", "gif")
        if f" {name} " not in encoders
    )
    if missing:
        raise ValueError("FFmpeg فاقد قابلیت‌های لازم است: " + ", ".join(missing))
    await capture([config.ffprobe, "-version"])
    print(version)
    print("Media tools: OK")
    try:
        print("Default font:", find_font(config.default_font))
    except ValueError:
        print("No default font: upload a TTF/OTF in the bot before using text.")
