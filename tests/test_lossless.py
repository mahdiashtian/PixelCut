import pytest

from movie_editor.domain import Color, Draft, Quality
from movie_editor.media.graphics import find_font, text_overlay
from movie_editor.media.process import capture
from movie_editor.media.renderer import Renderer

from .test_media import clip, pcm

pytestmark = pytest.mark.integration


async def decoded(config, path, pixel_format):
    return await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-fps_mode",
            "passthrough",
            "-pix_fmt",
            pixel_format,
            "-f",
            "rawvideo",
            "-",
        ]
    )


@pytest.mark.parametrize("pixel_format", ["yuv420p", "yuv422p", "yuv444p", "bgr0"])
async def test_lossless_no_edit_has_identical_decoded_pixels(config, store, tmp_path, pixel_format):
    original = await clip(config, tmp_path / "original.mp4", fps="30", duration=0.5)
    source = tmp_path / "source.mkv"
    await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(original),
            "-c:v",
            "ffv1",
            "-pix_fmt",
            pixel_format,
            "-c:a",
            "copy",
            str(source),
        ]
    )
    draft = Draft("native", str(source), "source.mkv")
    assert draft.settings.quality == Quality.LOSSLESS
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    assert result.info.pixel_format == pixel_format
    assert await decoded(config, source, pixel_format) == await decoded(
        config, result.path, pixel_format
    )


@pytest.mark.parametrize("color", [Color.WHITE, Color.BLACK, Color.DYNAMIC])
async def test_lossless_watermark_only_changes_its_region(config, store, tmp_path, color):
    source = await clip(config, tmp_path / "source.mp4", fps="30", duration=0.5)
    draft = Draft("native", str(source), "source.mp4", text="TEST")
    draft.settings.text_style.color = color
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    assert result.info.pixel_format == "yuv420p"
    assert await pcm(config, source) == await pcm(config, result.path)
    before = await decoded(config, source, "yuv420p")
    after = await decoded(config, result.path, "yuv420p")
    assert before != after and len(before) == len(after)
    overlay = text_overlay(
        "TEST", find_font(None), 320, 180, draft.settings.text_style, tmp_path / "stamp.png"
    )
    frame_size = 320 * 180 * 3 // 2
    for frame_start in range(0, len(before), frame_size):
        offset = frame_start
        for width, height, scale in ((320, 180, 1), (160, 90, 2), (160, 90, 2)):
            # Include the chroma footprint and edge filtering around the stamp.
            left = max(0, (overlay.x - 8) // scale)
            right = min(width, (overlay.x + overlay.width + 8 + scale - 1) // scale)
            top = max(0, (overlay.y - 8) // scale)
            bottom = min(height, (overlay.y + overlay.height + 8 + scale - 1) // scale)
            for row in range(height):
                start = offset + row * width
                if row < top or row >= bottom:
                    assert before[start : start + width] == after[start : start + width]
                else:
                    assert before[start : start + left] == after[start : start + left]
                    assert (
                        before[start + right : start + width]
                        == after[start + right : start + width]
                    )
            offset += width * height
