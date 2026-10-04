"""Compare local looks and installed reference profiles on the same source frame."""

import asyncio
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import Config
from ..domain import ColorGrade, Draft, Look
from ..storage import Store
from .grading import RECIPES, grade_graph, stage_lut
from .graphics import find_font
from .limits import check_output, check_space
from .probe import inspect
from .process import Progress, capture
from .result import RenderResult


class FilterGallery:
    def __init__(self, config: Config, store: Store):
        self.config, self.store = config, store

    async def render(
        self, draft: Draft, folder: Path, progress: Progress | None = None
    ) -> RenderResult:
        draft.validate()
        folder = folder.resolve()
        folder.mkdir(parents=True, exist_ok=True)
        check_space(self.config, folder)
        source = Path(draft.source).resolve()
        info = await inspect(source, self.config)
        end = draft.trim_end if draft.trim_end is not None else info.duration
        if not 0 <= draft.trim_start < end <= info.duration:
            raise ValueError("بازهٔ انتخاب‌شده خارج از مدت ویدیو است.")
        instant = draft.trim_start + min(0.5, (end - draft.trim_start) / 2)
        frame = folder / "sample-source.png"
        await capture(
            [
                self.config.ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-ss",
                str(instant),
                "-i",
                str(source),
                "-map",
                f"0:{info.video_index}",
                "-frames:v",
                "1",
                "-vf",
                "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2",
                "-threads",
                str(self.config.threads),
                str(frame),
            ]
        )
        profiles = [(look, None) for look in RECIPES]
        for look in (Look.DNT1, Look.DNT2, Look.DNT3, Look.DNT4, Look.DNT5):
            asset_id = self.store.look_lut(look)
            if asset_id:
                self.store.asset(asset_id, "lut")
                profiles.append((look, asset_id))
        if draft.settings.color_grade.look == Look.CUSTOM:
            profiles.append((Look.CUSTOM, draft.settings.color_grade.lut_id))
        tiles = []
        for index, (look, asset_id) in enumerate(profiles):
            grade = (
                ColorGrade()
                if look == Look.NONE
                else replace(draft.settings.color_grade, look=look, lut_id=asset_id)
            )
            stage_lut(grade, self.store, folder)
            target = folder / f"sample-{index}.png"
            await capture(
                [
                    self.config.ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-filter_complex_threads",
                    str(self.config.threads),
                    "-i",
                    str(frame),
                    "-filter_complex",
                    grade_graph(grade, "0:v", "colored"),
                    "-map",
                    "[colored]",
                    "-frames:v",
                    "1",
                    "-threads",
                    str(self.config.threads),
                    str(target),
                ],
                timeout=self.config.max_render_seconds,
                cwd=folder,
            )
            tiles.append(("Original" if look == Look.NONE else look.value.upper(), target))
            check_space(self.config, folder)
            if progress:
                await progress((index + 1) / len(profiles) * 99)
        output = folder / "comparison.png"

        def assemble():
            width, height = 320, 208
            columns = 3
            image = Image.new(
                "RGB",
                (width * columns, height * ((len(tiles) + columns - 1) // columns)),
                "#17191f",
            )
            draw = ImageDraw.Draw(image)
            try:
                font = ImageFont.truetype(str(find_font(self.config.default_font)), 18)
            except ValueError:
                font = ImageFont.load_default()
            for index, (label, path) in enumerate(tiles):
                x, y = (index % columns) * width, (index // columns) * height
                with Image.open(path) as tile:
                    image.paste(tile.convert("RGB"), (x, y))
                draw.text((x + 8, y + 184), label, font=font, fill="white")
            image.save(output)

        await asyncio.to_thread(assemble)
        check_output(self.config, output)
        return RenderResult(
            output,
            None,
            f"مقایسهٔ فیلترها روی یک فریم از ثانیهٔ {instant:g}. "
            "DNT فقط با LUT نصب‌شده نمایش داده می‌شود. "
            "این تصویر نمونه است؛ خروجی ویدیو ابعاد اصلی دارد.",
        )
