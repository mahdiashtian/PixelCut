"""FFprobe metadata and explicit limits for supported SDR video inputs."""

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..config import Config
from .process import capture


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    duration: float
    fps: Fraction
    time_base: Fraction
    frames: int | None
    audio_codec: str | None
    audio_start: float
    video_start: float
    color_space: str
    color_transfer: str
    color_primaries: str
    color_range: str
    pixel_format: str
    rotation: int
    video_index: int
    audio_index: int | None
    audio_duration: float | None


def fraction(value: str | None, fallback: str) -> Fraction:
    try:
        result = Fraction(value or fallback)
        return result if result > 0 else Fraction(fallback)
    except (ValueError, ZeroDivisionError):
        return Fraction(fallback)


async def inspect(path: Path, config: Config, count_frames: bool = False) -> MediaInfo:
    args = [config.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json"]
    if count_frames:
        args.append("-count_frames")
    args.append(str(path))
    data = json.loads(await capture(args, timeout=300 if count_frames else 60))
    streams = data.get("streams", [])
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise ValueError("فایل باید ویدیو داشته باشد.")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    rotation = int(float(video.get("tags", {}).get("rotate", 0)))
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = int(float(side["rotation"]))
    width, height = int(video["width"]), int(video["height"])
    sar = fraction(video.get("sample_aspect_ratio", "1:1").replace(":", "/"), "1")
    if sar != 1:
        raise ValueError("ویدیوی با پیکسل غیرمربع فعلاً پشتیبانی نمی‌شود؛ ابتدا SAR را اصلاح کنید.")
    if rotation % 90:
        raise ValueError("چرخش ویدیو باید مضرب ۹۰ درجه باشد.")
    if rotation % 180:
        width, height = height, width
    raw_frames = video.get("nb_read_frames") if count_frames else video.get("nb_frames")
    duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0)
    info = MediaInfo(
        width,
        height,
        duration,
        fraction(video.get("avg_frame_rate"), "30"),
        fraction(video.get("time_base"), "1/90000"),
        int(raw_frames) if str(raw_frames).isdigit() else None,
        audio.get("codec_name"),
        float(audio.get("start_time") or 0),
        float(video.get("start_time") or 0),
        video.get("color_space", "unknown"),
        video.get("color_transfer", "unknown"),
        video.get("color_primaries", "unknown"),
        video.get("color_range", "unknown"),
        video.get("pix_fmt", "unknown"),
        rotation,
        int(video["index"]),
        int(audio["index"]) if audio else None,
        float(audio["duration"]) if audio.get("duration") else None,
    )
    if not 0 < info.duration <= config.max_video_seconds:
        raise ValueError(f"مدت ویدیو باید بین صفر و {config.max_video_seconds} ثانیه باشد.")
    if width < 16 or height < 16 or width * height > config.max_pixels:
        raise ValueError("ابعاد ویدیو خارج از محدودهٔ مجاز است.")
    if (
        info.color_transfer in {"smpte2084", "arib-std-b67"}
        or "10" in info.pixel_format
        or "12" in info.pixel_format
    ):
        raise ValueError(
            "برای جلوگیری از تغییر ناخواستهٔ کیفیت، HDR و ویدیوی ۱۰/۱۲ بیت فعلاً پذیرفته نمی‌شود."
        )
    if not 1 <= info.fps <= 240:
        raise ValueError("نرخ فریم ویدیو باید بین ۱ و ۲۴۰ باشد.")
    return info
