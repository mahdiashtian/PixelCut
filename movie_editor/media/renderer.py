"""Inspect inputs, prepare overlays, then encode once and verify the result."""

import asyncio
from pathlib import Path

from ..config import Config
from ..domain import Draft, Quality
from ..storage import Store
from .formats import PixelLayout, layout_for
from .graph import Segment, build_graph
from .graphics import find_font, logo_overlay, text_overlay
from .limits import check_output, check_space, guarded_progress
from .probe import inspect
from .process import Progress, capture, encode
from .result import RenderResult


class Renderer:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    async def render(
        self, draft: Draft, folder: Path, progress: Progress | None = None, preview: bool = False
    ) -> RenderResult:
        if preview:
            draft = Draft.from_dict(draft.to_dict())
            draft.settings.quality = Quality.FAST
        draft.validate()
        folder.mkdir(parents=True, exist_ok=True)
        check_space(self.config, folder)
        paths: list[Path] = []
        if draft.intro_id:
            paths.append(Path(self.store.asset(draft.intro_id, "intro").path))
        paths.append(Path(draft.source))
        if draft.outro_id:
            paths.append(Path(self.store.asset(draft.outro_id, "outro").path))
        infos = [await inspect(path, self.config) for path in paths]
        main_index = 1 if draft.intro_id else 0
        main = infos[main_index]
        if draft.trim_start >= main.duration or (draft.trim_end and draft.trim_end > main.duration):
            raise ValueError("زمان برش خارج از مدت ویدیوی اصلی است.")
        segments = [
            Segment(
                path,
                info,
                draft.trim_start if i == main_index else 0,
                draft.trim_end if i == main_index else None,
            )
            for i, (path, info) in enumerate(zip(paths, infos, strict=True))
        ]
        if preview:
            segments = [
                Segment(
                    s.path,
                    s.info,
                    s.start,
                    min(
                        s.end if s.end is not None else s.info.duration,
                        s.start + (8 if i == main_index else 3),
                    ),
                )
                for i, s in enumerate(segments)
            ]
        lossless = draft.settings.quality == Quality.LOSSLESS
        layout = layout_for(main) if lossless else PixelLayout("yuv444p", "yuv420p", "yuv444", 2, 2)
        if lossless:
            for info in infos:
                if layout_for(info).encoded != layout.encoded:
                    raise ValueError(
                        "برای اتصال بدون افت، فرمت رنگ کلیپ‌ها باید یکسان باشد؛ "
                        "کلیپ ابتدا/انتهای سازگار انتخاب کنید."
                    )
        width = main.width + (-main.width % layout.horizontal_grid)
        height = main.height + (-main.height % layout.vertical_grid)
        overlays = []
        if draft.text:
            font_asset = self.store.asset(draft.settings.text_style.font_id, "font")
            font_path = Path(font_asset.path) if font_asset else find_font(self.config.default_font)
            overlays.append(
                await asyncio.to_thread(
                    text_overlay,
                    draft.text,
                    font_path,
                    width,
                    height,
                    draft.settings.text_style,
                    folder / "text.png",
                )
            )
        if draft.logo_id:
            logo = self.store.asset(draft.logo_id, "logo")
            overlays.append(
                await asyncio.to_thread(
                    logo_overlay,
                    Path(logo.path),
                    width,
                    height,
                    draft.settings.logo_style,
                    folder / "logo.png",
                )
            )
        graph = build_graph(draft, segments, overlays)
        if graph.duration > self.config.max_video_seconds:
            raise ValueError("مدت خروجی از محدودیت تنظیم‌شده بیشتر می‌شود.")
        output = folder / ("edited.mkv" if lossless else "edited.mp4")
        graph_file = folder / "filters.txt"
        graph_file.write_text(graph.filters, encoding="utf-8")
        args = [
            self.config.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-loglevel",
            "warning",
            "-filter_complex_threads",
            str(self.config.threads),
        ]
        for path in paths:
            args.extend(["-i", str(path)])
        for overlay in overlays:
            args.extend(["-i", str(overlay.path)])
        args.extend(["-filter_complex", graph.filters, "-map", f"[{graph.video}]"])
        native_audio = main.audio_codec == "flac" or (main.audio_codec or "").startswith("pcm_")
        copy_audio = graph.copy_audio and (
            native_audio if lossless else main.audio_codec in {"aac", "mp3", "alac"}
        )
        if graph.audio:
            args.extend(["-map", f"[{graph.audio}]"])
        elif graph.copy_audio:
            args.extend(["-map", f"0:{main.audio_index}"])
        if draft.mute or not any(info.audio_codec for info in infos):
            args.append("-an")
        elif copy_audio:
            args.extend(["-c:a", "copy"])
        else:
            # AAC priming metadata is not preserved by every Matroska muxer version.
            # Float PCM preserves the decoder's samples without shifting video timestamps.
            args.extend(["-c:a", "pcm_f32le" if lossless else "aac"])
            if not lossless:
                args.extend(["-b:a", "192k"])
        if lossless:
            args.extend(
                ["-c:v", "ffv1", "-level", "3", "-pix_fmt", graph.pixel_format, "-slicecrc", "1"]
            )
        else:
            args.extend(
                [
                    "-c:v",
                    "libx264",
                    "-crf",
                    "17" if draft.settings.quality == Quality.HIGH else "20",
                    "-preset",
                    "slow" if draft.settings.quality == Quality.HIGH else "veryfast",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                ]
            )
        # Use the filter's microsecond timebase to avoid quantizing VFR frames to a nominal FPS.
        args.extend(
            [
                "-fps_mode",
                "passthrough",
                "-enc_time_base:v",
                "1/1000000",
                "-threads",
                str(self.config.threads),
                "-map_metadata",
                "-1",
            ]
        )
        for flag, value in (
            ("-colorspace", main.color_space),
            ("-color_trc", main.color_transfer),
            ("-color_primaries", main.color_primaries),
            ("-color_range", main.color_range),
        ):
            if flag == "-colorspace" and value == "gbr":
                value = "rgb"
            if value not in {"unknown", "reserved", "unspecified"}:
                args.extend([flag, value])
        args.extend(["-progress", "pipe:1", "-nostats", str(output)])

        await encode(
            args,
            folder / "ffmpeg.log",
            graph.duration,
            self.config.max_render_seconds,
            guarded_progress(self.config, output, progress),
        )
        result = await inspect(output, self.config)
        check_output(self.config, output)
        if (result.width, result.height) != (width, height):
            raise ValueError("ابعاد خروجی با ابعاد مورد انتظار تطابق ندارد.")
        if abs(result.duration - graph.duration) > max(0.25, 3 / float(main.fps)):
            raise ValueError("مدت خروجی با پروژه تطابق ندارد؛ فایل ورودی را بررسی کنید.")
        if result.audio_duration and result.audio_duration > result.duration + 0.25:
            raise ValueError("صدای خروجی از تصویر طولانی‌تر است؛ فایل ورودی را بررسی کنید.")
        if not preview and len(segments) == 1 and draft.trim_start == 0 and draft.trim_end is None:
            # Count actual decoded frames if the container does not expose a frame count.
            original_count = main.frames
            if original_count is None:
                original_count = (await inspect(paths[0], self.config, count_frames=True)).frames
            output_count = result.frames
            if output_count is None:
                output_count = (await inspect(output, self.config, count_frames=True)).frames
            if original_count is not None and output_count != original_count:
                raise ValueError("تعداد فریم خروجی با ورودی متفاوت است؛ خروجی ارسال نشد.")
        notice = (
            "برای ترنزیشن، همهٔ کلیپ‌ها به FPS ویدیوی اصلی تبدیل شدند."
            if graph.normalize_fps
            else "زمان‌بندی فریم‌ها بدون تبدیل اجباری FPS پردازش شد."
        )
        if copy_audio:
            notice += " صدا بدون encode مجدد کپی شد."
        return RenderResult(output, result, notice)

    async def thumbnail(self, video: Path, dst: Path) -> Path:
        await capture(
            [
                self.config.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                "scale=320:320:force_original_aspect_ratio=decrease",
                str(dst),
            ]
        )
        return dst
