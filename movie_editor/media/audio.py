"""Preserve decoded samples, channel levels and the video's audio timeline."""

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from .probe import MediaInfo
from .process import capture, command, stop


@dataclass(frozen=True)
class AudioLayout:
    rate: int
    channels: int
    name: str


def layout_name(info: MediaInfo) -> str:
    # FFprobe's names are data from a media file, never arbitrary filter syntax.
    names = {
        "mono",
        "stereo",
        "2.1",
        "3.0",
        "3.0(back)",
        "quad",
        "quad(side)",
        "4.0",
        "4.1",
        "5.0",
        "5.0(side)",
        "5.1",
        "5.1(side)",
        "6.0",
        "6.0(front)",
        "hexagonal",
        "6.1",
        "6.1(back)",
        "6.1(front)",
        "7.0",
        "7.0(front)",
        "7.1",
        "7.1(wide)",
        "7.1(wide-side)",
        "octagonal",
    }
    if info.audio_layout in names:
        return info.audio_layout
    if 1 <= info.audio_channels <= 8:
        return {1: "mono", 2: "stereo"}.get(info.audio_channels, f"{info.audio_channels}c")
    raise ValueError("چیدمان کانال‌های صدا پشتیبانی نمی‌شود؛ صدا بدون اطلاع تبدیل نشد.")


def common_layout(infos: list[MediaInfo], *, strict: bool) -> AudioLayout:
    sounds = [info for info in infos if info.audio_codec]
    if any(not 8000 <= info.audio_rate <= 384000 for info in sounds):
        raise ValueError("نرخ نمونه‌برداری صدا معتبر یا پشتیبانی‌شده نیست.")
    if any(info.audio_format.rstrip("p") == "s64" for info in sounds):
        raise ValueError("پردازش PCM صحیح ۶۴ بیت فعلاً پشتیبانی نمی‌شود؛ دقت صدا کاهش داده نشد.")
    if strict and len({info.audio_rate for info in sounds}) > 1:
        raise ValueError(
            "برای حفظ نمونه‌های صدا در Lossless، نرخ نمونه‌برداری کلیپ‌ها باید یکسان باشد."
        )
    names = {layout_name(info) for info in sounds}
    channels = max(info.audio_channels for info in sounds)
    if len(names) == 1:
        name = names.pop()
    elif names <= {"mono", "stereo"}:
        name = "stereo"
    else:
        raise ValueError("چیدمان کانال‌های کلیپ‌ها باید یکسان باشد؛ downmix خودکار انجام نشد.")
    # Never lower a source's sample rate. Same-rate inputs bypass rate conversion.
    return AudioLayout(max(info.audio_rate for info in sounds), channels, name)


def segment_audio(
    index: int, info: MediaInfo, start: float, duration: float, layout: AudioLayout
) -> str:
    begin = round(start * layout.rate)
    end = begin + round(duration * layout.rate)
    if info.audio_codec:
        chain = f"[{index}:{info.audio_index}]asetpts=PTS-{info.video_start:.9f}/TB"
        if info.audio_channels == 1 and layout.channels == 2:
            # Default rematrixing attenuates mono. Duplicate it at exactly unity gain.
            chain += ",pan=stereo|c0=c0|c1=c0"
        chain += (
            f",aformat=sample_fmts=dblp:channel_layouts={layout.name}"
            f",aresample={layout.rate}:osf=dblp:tsf=dblp:async=1:first_pts=0:"
            "min_hard_comp=0.002:filter_size=128:cutoff=0.97:kaiser_beta=16"
        )
    else:
        chain = f"anullsrc=r={layout.rate}:cl={layout.name},aformat=sample_fmts=dblp"
    return (
        chain
        + f",apad=whole_len={end},atrim=start_sample={begin}:end_sample={end}"
        + f",asetpts=N/SR/TB[a{index}]"
    )


@dataclass(frozen=True)
class AudioEncoding:
    codec: str | None
    matroska: bool
    copied: bool = False


def encoding(
    info: MediaInfo, *, filtered: bool, lossless: bool, muted: bool, normalize: bool = False
) -> AudioEncoding:
    if muted:
        return AudioEncoding(None, lossless)
    if filtered:
        # 64-bit float stores the filter's double-precision samples without quantization.
        # MP4/AAC would add another lossy encode; use MKV even with H.264 video.
        return AudioEncoding("pcm_f64le", True)
    if not info.audio_codec:
        return AudioEncoding(None, lossless)
    if normalize and info.audio_format.rstrip("p") == "s64":
        raise ValueError("تغییر زمان‌بندی PCM صحیح ۶۴ بیت بدون کاهش دقت فعلاً پشتیبانی نمی‌شود.")
    if not normalize and not lossless and info.audio_codec in {"aac", "mp3", "alac"}:
        return AudioEncoding("copy", False, True)
    if not normalize and (
        info.audio_codec in {"flac", "alac"} or info.audio_codec.startswith("pcm_")
    ):
        return AudioEncoding("copy", True, True)
    precision = "64" if info.audio_format.rstrip("p") in {"dbl", "s32", "s64"} else "32"
    return AudioEncoding(f"pcm_f{precision}le", True)


def validate_output(
    result: MediaInfo,
    source: MediaInfo,
    plan: AudioEncoding,
    duration: float,
    layout: AudioLayout | None,
) -> None:
    if plan.codec is None:
        if result.audio_codec:
            raise ValueError("خروجی بی‌صدا نباید ترک صوتی داشته باشد.")
        return
    if not result.audio_codec:
        raise ValueError("صدای خروجی مفقود است؛ خروجی ارسال نشد.")
    rate = layout.rate if layout else source.audio_rate
    channels = layout.channels if layout else source.audio_channels
    if (result.audio_rate, result.audio_channels) != (rate, channels):
        raise ValueError(
            "نرخ نمونه‌برداری یا تعداد کانال‌های صدا تغییر ناخواسته دارد؛ خروجی ارسال نشد."
        )
    # Encoded duration can include codec priming/discard padding. Decoded timelines
    # and sample hashes below are authoritative for an untouched audio stream.
    expected = duration if layout else None
    if expected is not None and (
        result.audio_duration is None or abs(result.audio_duration - expected) > 0.002
    ):
        raise ValueError("مدت صدای خروجی با بازهٔ مورد انتظار تطابق ندارد؛ خروجی ارسال نشد.")
    if layout and abs(result.audio_start) > 0.002:
        raise ValueError("شروع صدا با تصویر هماهنگ نیست؛ خروجی ارسال نشد.")


@dataclass(frozen=True)
class AudioTimeline:
    samples: int
    start: float
    end: float
    gaps: tuple[tuple[float, float], ...]
    last_samples: int = 0


async def timeline(path: Path, config: Config, rate: int) -> AudioTimeline:
    """Stream decoded frame metadata with bounded memory, honoring codec pre-skip."""
    args = [
        config.ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_frames",
        "-show_entries",
        "frame=best_effort_timestamp_time,nb_samples",
        "-of",
        "compact=p=0:nk=0",
        str(path),
    ]
    count, first, end, gaps, last = 0, None, 0.0, [], 0
    with tempfile.TemporaryFile() as errors:
        process = await asyncio.create_subprocess_exec(
            *command(args), stdout=asyncio.subprocess.PIPE, stderr=errors
        )
        try:
            async with asyncio.timeout(config.max_render_seconds):
                while line := await process.stdout.readline():
                    fields = dict(
                        part.split("=", 1)
                        for part in line.decode().strip().split("|")
                        if "=" in part
                    )
                    if "nb_samples" not in fields:
                        continue
                    pts = float(fields["best_effort_timestamp_time"])
                    size = int(fields["nb_samples"])
                    if first is None:
                        first = pts
                    elif abs(pts - end) > 0.00201:
                        # Keep only real discontinuities, not Matroska's ms rounding.
                        if len(gaps) >= 10000:
                            raise ValueError("زمان‌بندی صدای ورودی بیش از حد ناپیوسته است.")
                        gaps.append((end, pts - end))
                    end = pts + size / rate
                    count += size
                    last = size
                await process.wait()
            if process.returncode or first is None:
                raise ValueError("بررسی زمان‌بندی صدای خروجی موفق نبود؛ خروجی ارسال نشد.")
        except BaseException:
            await stop(process)
            raise
    return AudioTimeline(count, first, end, tuple(gaps), last)


async def verify_samples(
    source_path: Path,
    output_path: Path,
    config: Config,
    source: MediaInfo,
    result: MediaInfo,
    expected_samples: int | None,
) -> None:
    after = await timeline(output_path, config, result.audio_rate)
    if expected_samples is not None:
        if after.samples != expected_samples or after.gaps or abs(after.start) > 0.002:
            raise ValueError("تعداد نمونه‌ها یا پیوستگی صدای خروجی تغییر کرده؛ خروجی ارسال نشد.")
        return
    before = await timeline(source_path, config, source.audio_rate)
    if before.samples != after.samples:
        raise ValueError("نمونه‌ای از صدای اصلی حذف یا اضافه شده؛ خروجی ارسال نشد.")
    origin = source.video_start
    output_origin = result.video_start
    if (
        abs((after.start - output_origin) - (before.start - origin)) > 0.00201
        or abs((after.end - output_origin) - (before.end - origin)) > 0.00201
        or len(after.gaps) != len(before.gaps)
    ):
        raise ValueError("زمان‌بندی صدای اصلی تغییر کرده؛ خروجی ارسال نشد.")
    for (old_time, old_gap), (new_time, new_gap) in zip(before.gaps, after.gaps, strict=True):
        if (
            abs((new_time - output_origin) - (old_time - origin)) > 0.00201
            or abs(new_gap - old_gap) > 0.00201
        ):
            raise ValueError("وقفهٔ ناخواسته در صدای خروجی ایجاد شده؛ خروجی ارسال نشد.")

    async def digest(path: Path) -> bytes:
        codec = "pcm_s64le" if source.audio_format.rstrip("p") == "s64" else "pcm_f64le"
        return await capture(
            [
                config.ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-c:a",
                codec,
                "-f",
                "hash",
                "-hash",
                "sha256",
                "-",
            ],
            timeout=config.max_render_seconds,
        )

    original_hash, output_hash = await asyncio.gather(digest(source_path), digest(output_path))
    if original_hash != output_hash:
        raise ValueError("نمونه‌های صدای اصلی با خروجی یکسان نیست؛ خروجی ارسال نشد.")


async def validate_join_span(path: Path, info: MediaInfo, config: Config) -> None:
    if not info.audio_codec:
        return
    sound = await timeline(path, config, info.audio_rate)
    end = sound.end
    if info.audio_codec in {"aac", "mp3", "opus", "vorbis", "ac3", "eac3", "dts"}:
        # Older decoders expose a whole final codec frame even when the container
        # marks part of it as padding. Honor its declared playable end, but only
        # within that final frame: a real voice tail must still block the join.
        declared_end = (
            info.audio_start + info.audio_duration if info.audio_duration is not None else end
        )
        if 0 <= end - declared_end <= sound.last_samples / info.audio_rate + 0.00201:
            end = declared_end
    if sound.start < info.video_start - 0.00201 or end > info.video_start + info.duration + 0.00201:
        raise ValueError(
            "صدای یک کلیپ از بازهٔ تصویر آن بیرون است؛ برای جلوگیری از قطع صدا، اتصال انجام نشد. "
            "کلیپی با بازهٔ تصویر و صدای هماهنگ انتخاب کنید یا برش زمانی را صریح مشخص کنید."
        )
