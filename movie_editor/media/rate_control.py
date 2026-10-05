"""Source-sized two-pass video encoding; preserve audio precision independently."""

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from .audio import AudioEncoding
from .graph import Graph, Segment
from .process import command, stop


async def packet_sizes(segment: Segment, config: Config) -> tuple[int, int]:
    # Packet sizes are reliable even for MKV/VFR files without stream bit_rate.
    # Stream metadata, never retain every packet or decode raw frames in RAM.
    args = [
        config.ffprobe,
        "-v",
        "error",
        "-show_packets",
        "-show_entries",
        "packet=stream_index,size",
        "-of",
        "compact=p=0:nk=0",
        str(segment.path),
    ]
    video, audio = 0, 0
    with tempfile.TemporaryFile() as errors:
        process = await asyncio.create_subprocess_exec(
            *command(args), stdout=asyncio.subprocess.PIPE, stderr=errors
        )
        try:
            async with asyncio.timeout(config.max_render_seconds):
                async for line in process.stdout:
                    fields = dict(
                        part.split("=", 1)
                        for part in line.decode().strip().split("|")
                        if "=" in part
                    )
                    if "stream_index" not in fields or "size" not in fields:
                        continue
                    index, size = int(fields["stream_index"]), int(fields["size"])
                    if index == segment.info.video_index:
                        video += size
                    elif index == segment.info.audio_index:
                        audio += size
                await process.wait()
            if process.returncode or video <= 0:
                raise ValueError("نرخ فشرده‌سازی ویدیوی ورودی قابل محاسبه نیست.")
        finally:
            await stop(process)
    return video, audio


@dataclass(frozen=True)
class RatePlan:
    encoder: str
    bitrate: int
    ceiling_bytes: int
    pixel_format: str

    def options(self, config: Config, pass_number: int) -> list[str]:
        args = [
            "-c:v",
            self.encoder,
            "-b:v",
            str(self.bitrate),
            "-preset",
            "slow",
            "-pix_fmt",
            self.pixel_format,
        ]
        if self.encoder == "libx265":
            args += [
                "-x265-params",
                f"pass={pass_number}:stats=source-rate.stats:"
                f"pools={config.threads}:frame-threads=1:rc-lookahead=8:"
                "lookahead-slices=1:log-level=error",
            ]
        else:
            args += [
                "-pass",
                str(pass_number),
                "-passlogfile",
                "source-rate",
                "-rc-lookahead",
                "8",
                "-x264-params",
                "sync-lookahead=0:lookahead-threads=1",
            ]
        return args

    def check_size(self, output: Path) -> None:
        if output.exists() and output.stat().st_size > self.ceiling_bytes:
            raise ValueError(
                "حجم خروجی از سقف متناسب با ورودی گذشت؛ فایل حجیم ارسال نشد. "
                "ابعاد و فریم‌ها کاهش داده نشدند. جزئیات در ffmpeg.log است."
            )


async def plan_rate(
    segments: list[Segment], graph: Graph, audio: AudioEncoding, config: Config, main_index: int
) -> RatePlan:
    sizes = [await packet_sizes(segment, config) for segment in segments]
    span = sum(s.duration for s in segments)
    video_bytes = (
        sum(size[0] * s.duration / s.info.duration for s, size in zip(segments, sizes, strict=True))
        * graph.duration
        / span
    )
    # Allow 5% extra video bits for the new watermark; never force CRF 0 or a fixed CRF.
    # Tiny static clips need a few extra kilobytes for newly drawn glyphs/keyframes.
    # This fixed allowance is negligible for a 200 MB upload and avoids unreadable text.
    bitrate = max(15000, round((video_bytes * 1.05 + 8192) * 8 / graph.duration))
    main = segments[main_index].info
    if audio.codec is None:
        audio_bytes = 0
    elif audio.copied:
        audio_bytes = sizes[main_index][1]
    else:
        rate = graph.audio_layout.rate if graph.audio_layout else main.audio_rate
        channels = graph.audio_layout.channels if graph.audio_layout else main.audio_channels
        precision = 8 if audio.codec == "pcm_f64le" else 4
        # Processed audio remains lossless. It has a separate, explicit space budget;
        # never hide an AAC re-encode or reduce sample precision to meet video size.
        audio_bytes = rate * channels * precision * graph.duration * 1.05
    rgb = main.pixel_format in {"rgb24", "bgr24", "rgb0", "bgr0", "gbrp"}
    if main.video_codec == "hevc":
        encoder, pixel_format = "libx265", "gbrp" if rgb else main.pixel_format
    elif rgb:
        # RGB input must retain RGB coding and its full-range/matrix metadata.
        # Converting it to YUV while retaining RGB tags causes a major color shift.
        encoder, pixel_format = "libx264rgb", "bgr0"
    else:
        encoder, pixel_format = "libx264", main.pixel_format
    return RatePlan(
        encoder,
        bitrate,
        round(max(video_bytes * 1.25, 15000 * graph.duration / 8 * 1.25) + audio_bytes + 65536),
        pixel_format,
    )


def clean_stats(folder: Path) -> None:
    for path in folder.glob("source-rate*"):
        if path.is_file():
            path.unlink(missing_ok=True)
