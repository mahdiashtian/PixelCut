"""Real-duration regression: 30 minutes, 54,000 frames and a complete audio audit."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from movie_editor.domain import Draft, Position
from movie_editor.media.probe import inspect
from movie_editor.media.process import capture
from movie_editor.media.renderer import Renderer

pytestmark = pytest.mark.integration


async def test_thirty_minute_watermark_has_exact_pixels_frames_and_audio(config, store, tmp_path):
    config = replace(config, max_render_seconds=600)
    source = tmp_path / "thirty-minutes.mp4"
    # Use real CFR frames and uninterrupted audio. Looping an AAC-containing MP4
    # repeats its priming/padding and does not make a true 54,000-frame fixture.
    # Small dimensions keep this duration regression affordable on CI; separate
    # tests cover pixel formats, variable frame rates, and concurrent renders.
    await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=192x108:r=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "1800",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-threads",
            "2",
            str(source),
        ],
        timeout=600,
    )
    before = await inspect(source, config)
    assert before.frames == 54000
    assert before.duration == pytest.approx(1800, abs=0.001)
    draft = Draft("long", str(source), source.name, text="PixelCut")
    draft.settings.text_style.position = Position.BOTTOM_RIGHT
    folder = tmp_path / "render"
    stage = AsyncMock()
    result = await Renderer(config, store).render(draft, folder, stage=stage)
    assert result.info.duration == pytest.approx(1800, abs=0.001)
    assert (result.info.width, result.info.height) == (192, 108)
    assert result.path.suffix == ".mp4"
    assert result.info.audio_codec == "aac"  # Original compressed audio, including padding.
    metrics = json.loads((folder / "ffmpeg.metrics.json").read_text())
    assert metrics["returncode"] == 0
    assert 0 < metrics["peak_rss_bytes"] < config.max_ffmpeg_memory_mb * 1024**2
    assert any("شمارش" in call.args[0] for call in stage.call_args_list)

    async def frames(path):
        # Hash every pixel of a crop outside the watermark; never retain raw frames in RAM.
        return await capture(
            [
                config.ffmpeg,
                "-v",
                "error",
                "-threads",
                "2",
                "-i",
                str(path),
                "-an",
                "-vf",
                "crop=192:40:0:0",
                "-pix_fmt",
                "yuv420p",
                "-fps_mode",
                "passthrough",
                "-f",
                "hash",
                "-hash",
                "sha256",
                "-",
            ],
            timeout=600,
        )

    assert await frames(source) == await frames(result.path)
    # Renderer has already compared the frame count and all decoded audio samples/timestamps.
