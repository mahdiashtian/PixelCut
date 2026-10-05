"""Native-size GIF conversion with a separate adaptive palette for each frame."""

from pathlib import Path

from ..config import Config
from ..domain import GifRange
from .limits import check_output, check_resources, check_space, guarded_progress
from .probe import inspect
from .process import Progress, encode
from .result import RenderResult


class GifExporter:
    def __init__(self, config: Config):
        self.config = config

    async def convert(
        self,
        source: Path,
        selection: GifRange,
        folder: Path,
        progress: Progress | None = None,
    ) -> RenderResult:
        info = await inspect(source, self.config)
        start, end = selection.resolve(info.duration)
        if info.fps > 100:
            raise ValueError(
                "زمان‌بندی GIF دقت یک‌صدم ثانیه دارد؛ ویدیوی بالای ۱۰۰ FPS "
                "بدون تغییر نرخ فریم به GIF تبدیل نمی‌شود."
            )
        folder.mkdir(parents=True, exist_ok=True)
        check_space(self.config, folder)
        output = folder / "edited.gif"
        # Per-frame palettes bound memory usage even for a full-length source video.
        graph = (
            f"[0:{info.video_index}]setpts=PTS-STARTPTS,trim=start={start:.9f}:end={end:.9f},"
            "setpts=PTS-STARTPTS,split[frames][colors];"
            "[colors]palettegen=stats_mode=single:max_colors=256:reserve_transparent=0[palette];"
            "[frames][palette]paletteuse=new=1:dither=sierra2_4a[gif]"
        )
        (folder / "filters.txt").write_text(graph, encoding="utf-8")
        args = [
            self.config.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-loglevel",
            "warning",
            "-filter_complex_threads",
            str(self.config.threads),
            "-threads",
            str(min(self.config.threads, 2)),
            "-i",
            str(source),
            "-filter_complex",
            graph,
            "-map",
            "[gif]",
            "-an",
            "-c:v",
            "gif",
            "-gifflags",
            "0",
            "-loop",
            "0",
            "-fps_mode",
            "passthrough",
            "-enc_time_base:v",
            "1/1000000",
            "-threads",
            str(self.config.threads),
            "-map_metadata",
            "-1",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output),
        ]
        await encode(
            args,
            folder / "ffmpeg.log",
            end - start,
            self.config.max_render_seconds,
            guarded_progress(self.config, output, progress),
            memory_mb=self.config.max_ffmpeg_memory_mb,
            watchdog=lambda: check_resources(self.config, output),
        )
        check_output(self.config, output)
        result = await inspect(output, self.config, count_frames=True)
        if (result.width, result.height) != (info.width, info.height):
            raise ValueError("ابعاد GIF با ویدیوی اصلی تطابق ندارد؛ خروجی ارسال نشد.")
        if abs(result.duration - (end - start)) > max(0.1, 2 / float(info.fps)):
            raise ValueError("مدت GIF با بازهٔ انتخاب‌شده تطابق ندارد؛ خروجی ارسال نشد.")
        if start == 0 and end == info.duration:
            original = (
                info
                if info.frames is not None
                else await inspect(source, self.config, count_frames=True)
            )
            if original.frames is not None and result.frames != original.frames:
                raise ValueError("تعداد فریم GIF با ویدیو متفاوت است؛ خروجی ارسال نشد.")
        return RenderResult(
            output,
            result,
            f"GIF از ثانیهٔ {start:g} تا {end:g}، با ابعاد اصلی و پالت مستقل هر فریم. "
            "GIF بی‌صدا و محدود به ۲۵۶ رنگ در هر فریم است.",
        )
