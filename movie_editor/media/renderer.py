"""Compose edits in one graph, control encoding size, and verify the result."""

import asyncio
from pathlib import Path

from ..config import Config
from ..domain import Draft, Quality
from ..storage import Store
from .audio import encoding, validate_join_span, validate_output, verify_samples
from .formats import layout_for, render_layout
from .grading import stage_lut
from .graph import Segment, build_graph
from .graphics import find_font, logo_overlay, text_overlay
from .limits import check_output, check_resources, check_space, guarded_progress
from .probe import inspect
from .process import Progress, Stage, capture, encode
from .rate_control import clean_stats, plan_rate
from .result import RenderResult


class Renderer:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    async def render(
        self,
        draft: Draft,
        folder: Path,
        progress: Progress | None = None,
        preview: bool = False,
        stage: Stage | None = None,
    ) -> RenderResult:
        if preview:
            draft = Draft.from_dict(draft.to_dict())
            draft.settings.quality = Quality.FAST
        draft.validate()
        folder = folder.resolve()
        folder.mkdir(parents=True, exist_ok=True)
        check_space(self.config, folder)
        paths: list[Path] = []
        if draft.intro_id:
            paths.append(Path(self.store.asset(draft.intro_id, "intro").path).resolve())
        paths.append(Path(draft.source).resolve())
        if draft.outro_id:
            paths.append(Path(self.store.asset(draft.outro_id, "outro").path).resolve())
        if stage:
            await stage("در حال خواندن مشخصات ویدیو…")
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
        if len(segments) > 1 and not draft.mute:
            for segment in segments:
                if segment.start == 0 and segment.end is None:
                    await validate_join_span(segment.path, segment.info, self.config)
        layout = render_layout(main, draft.settings.quality, draft.settings.color_grade)
        if lossless:
            for info in infos:
                if layout_for(info).encoded != layout_for(main).encoded:
                    raise ValueError(
                        "برای اتصال بدون افت، فرمت رنگ کلیپ‌ها باید یکسان باشد؛ "
                        "کلیپ ابتدا/انتهای سازگار انتخاب کنید."
                    )
        native_layout = draft.settings.quality in {Quality.LOSSLESS, Quality.SOURCE}
        grid_x = layout.horizontal_grid if native_layout else 2
        grid_y = layout.vertical_grid if native_layout else 2
        width = main.width + (-main.width % grid_x)
        height = main.height + (-main.height % grid_y)
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
        stage_lut(draft.settings.color_grade, self.store, folder)
        if graph.duration > self.config.max_video_seconds:
            raise ValueError("مدت خروجی از محدودیت تنظیم‌شده بیشتر می‌شود.")
        audio_plan = encoding(
            main,
            filtered=graph.audio is not None,
            lossless=lossless,
            muted=draft.mute,
            normalize=graph.copy_audio and main.video_start != 0,
            source_sized=draft.settings.quality == Quality.SOURCE,
            prefer_mp4=(
                lossless and graph.pixel_format != "bgr0" and "mp4" in main.container.split(",")
            ),
        )
        source_sized = draft.settings.quality == Quality.SOURCE
        if source_sized and stage:
            await stage("در حال محاسبهٔ حجم متناسب با ویدیوی ورودی…")
        rate_plan = (
            await plan_rate(segments, graph, audio_plan, self.config, main_index)
            if source_sized
            else None
        )
        output = folder / ("edited.mkv" if audio_plan.matroska else "edited.mp4")
        graph_file = folder / "filters.txt"
        graph_file.write_text(graph.filters, encoding="utf-8")
        args = [
            self.config.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-copyts",
            "-loglevel",
            "warning",
            "-filter_complex_threads",
            str(self.config.threads),
        ]
        for path in paths:
            args.extend(["-threads", str(min(self.config.threads, 2)), "-i", str(path)])
        for overlay in overlays:
            args.extend(["-threads", "1", "-i", str(overlay.path)])
        args.extend(["-filter_complex", graph.filters, "-map", f"[{graph.video}]"])
        if graph.audio:
            args.extend(["-map", f"[{graph.audio}]"])
        elif graph.copy_audio:
            args.extend(["-map", f"0:{main.audio_index}"])
            if main.video_start:
                args.extend(["-af", f"asetpts=PTS-{main.video_start:.9f}/TB"])
        if audio_plan.codec is None:
            args.append("-an")
        else:
            args.extend(["-c:a", audio_plan.codec])
            if audio_plan.codec == "wavpack":
                # A decoded MP3's initial frame can have fewer than 128 samples after
                # encoder-delay removal. Fix the coding block size without padding audio.
                args.extend(["-sample_fmt:a", "fltp", "-frame_size:a", "4096"])
        video_start = len(args)
        if rate_plan:
            args.extend(rate_plan.options(self.config, 2))
        elif lossless:
            if graph.pixel_format == "bgr0":
                args.extend(["-c:v", "ffv1", "-level", "3", "-pix_fmt", "bgr0", "-slicecrc", "1"])
            else:
                # Inter-frame lossless H.264 is usually smaller than intra-only FFV1.
                # CRF 0 preserves the native YUV samples; no FPS or chroma reduction.
                args.extend(
                    [
                        "-c:v",
                        "libx264",
                        "-crf",
                        "0",
                        "-preset",
                        "slow",
                        "-pix_fmt",
                        graph.pixel_format,
                    ]
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
                ]
            )
        if rate_plan is None and (not lossless or graph.pixel_format != "bgr0"):
            args.extend(
                ["-rc-lookahead", "8", "-x264-params", "sync-lookahead=0:lookahead-threads=1"]
            )
        video_end = len(args)
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
            (
                "-colorspace",
                "rgb" if lossless and graph.pixel_format == "bgr0" else main.color_space,
            ),
            ("-color_trc", main.color_transfer),
            ("-color_primaries", main.color_primaries),
            (
                "-color_range",
                "pc" if lossless and graph.pixel_format == "bgr0" else main.color_range,
            ),
        ):
            if flag == "-colorspace" and value == "gbr":
                value = "rgb"
            if value not in {"unknown", "reserved", "unspecified"}:
                args.extend([flag, value])
        args.extend(["-max_interleave_delta", "1000000", "-progress", "pipe:1", "-nostats"])

        def watch_output() -> None:
            check_resources(self.config, output, multipart=True)
            if rate_plan:
                rate_plan.check_size(output)

        async def first_progress(percent: float) -> None:
            if progress:
                await progress(percent * 0.45)

        async def final_progress(percent: float) -> None:
            if progress:
                await progress(45 + percent * 0.54 if rate_plan else percent)

        try:
            if rate_plan:
                clean_stats(folder)
                output.unlink(missing_ok=True)
                if stage:
                    await stage(
                        "مرحلهٔ ۱ از ۲: تحلیل فشرده‌سازی برای کنترل حجم؛ "
                        f"سقف خروجی حدود {rate_plan.ceiling_bytes / 1024**2:.1f} MiB است…"
                    )
                first_args = (
                    args[:video_start] + rate_plan.options(self.config, 1) + args[video_end:]
                )
                await encode(
                    [*first_args, "-f", "null", "-"],
                    folder / "ffmpeg.pass1.log",
                    graph.duration,
                    self.config.max_render_seconds,
                    first_progress,
                    cwd=folder,
                    memory_mb=self.config.max_ffmpeg_memory_mb,
                    watchdog=watch_output,
                )
            if not audio_plan.matroska:
                args.extend(["-movflags", "+faststart"])
            if stage:
                await stage(
                    "مرحلهٔ ۲ از ۲: ساخت خروجی با حجم متناسب؛ ابعاد و زمان‌بندی فریم حفظ می‌شوند…"
                    if rate_plan
                    else "در حال رندر؛ ابعاد و کیفیت انتخاب‌شده حفظ می‌شوند…"
                )
            await encode(
                [*args, str(output)],
                folder / "ffmpeg.log",
                graph.duration,
                self.config.max_render_seconds,
                guarded_progress(self.config, output, final_progress, multipart=True),
                cwd=folder,
                memory_mb=self.config.max_ffmpeg_memory_mb,
                watchdog=watch_output,
            )
        finally:
            if rate_plan:
                clean_stats(folder)
        if rate_plan:
            rate_plan.check_size(output)
        if stage:
            await stage("رندر تمام شد؛ در حال بررسی مشخصات و صدا…")
        result = await inspect(output, self.config)
        check_output(self.config, output, multipart=True)
        if (result.width, result.height) != (width, height):
            raise ValueError("ابعاد خروجی با ابعاد مورد انتظار تطابق ندارد.")
        if abs(result.duration - graph.duration) > max(0.25, 3 / float(main.fps)):
            raise ValueError("مدت خروجی با پروژه تطابق ندارد؛ فایل ورودی را بررسی کنید.")
        validate_output(result, main, audio_plan, graph.duration, graph.audio_layout)
        if audio_plan.codec:
            await verify_samples(
                paths[main_index], output, self.config, main, result, graph.audio_samples
            )
        if not preview and len(segments) == 1 and draft.trim_start == 0 and draft.trim_end is None:
            if stage:
                await stage("در حال شمارش فریم‌ها؛ این بررسی برای فیلم بلند زمان می‌برد…")
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
        if audio_plan.copied:
            notice += " صدا بدون encode مجدد کپی شد."
        elif audio_plan.codec:
            notice += (
                " صدا با WavPack بدون اتلاف و بدون تغییر نمونه‌ها فشرده شد."
                if audio_plan.codec == "wavpack"
                else " صدا بدون فشرده‌سازی با اتلاف ذخیره شد."
            )
        if audio_plan.matroska and not lossless:
            video_codec = "HEVC" if result.video_codec == "hevc" else "H.264"
            notice += f" برای حفظ صدا، ویدیوی {video_codec} در فایل MKV قرار گرفت."
        if lossless:
            notice += (
                " تصویر با FFV1 بدون اتلاف ذخیره شد."
                if graph.pixel_format == "bgr0"
                else " تصویر با H.264 lossless (CRF 0) بدون اتلاف ذخیره شد."
            )
        elif source_sized:
            notice += (
                " تصویر با فشرده‌سازی دو مرحله‌ای و حجم متناسب با ورودی ذخیره شد؛ "
                "این حالت Lossless نیست."
            )
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
