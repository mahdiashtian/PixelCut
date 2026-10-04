import asyncio
import sys

import pytest

from movie_editor.domain import Asset, Draft, Transition
from movie_editor.media.probe import inspect
from movie_editor.media.process import capture
from movie_editor.media.renderer import Renderer

from .test_media import clip, timestamps

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "first,second", [(Transition.CUT, Transition.SHADOW), (Transition.SHADOW, Transition.CUT)]
)
async def test_mixed_transitions_and_fractional_fps(config, store, tmp_path, first, second):
    source = await clip(config, tmp_path / "main.mp4")
    intro = await clip(config, tmp_path / "intro.mp4", fps="24", audio=False)
    outro = await clip(config, tmp_path / "outro.mp4", fps="15")
    store.add_asset(Asset("intro", "intro", "intro.mp4", str(intro)))
    store.add_asset(Asset("outro", "outro", "outro.mp4", str(outro)))
    draft = Draft("mixed", str(source), "main.mp4", intro_id="intro", outro_id="outro")
    draft.settings.intro_join.kind, draft.settings.outro_join.kind = first, second
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    assert result.info.duration == pytest.approx(5.402, abs=0.15)
    assert float(result.info.fps) == pytest.approx(30000 / 1001, abs=0.4)


async def test_trimmed_preview_trims_audio_too(config, store, tmp_path):
    source = await clip(config, tmp_path / "main.mp4", fps="30", duration=10)
    draft = Draft("preview", str(source), "main.mp4")
    result = await Renderer(config, store).render(draft, tmp_path / "preview", preview=True)
    assert result.info.duration == pytest.approx(8, abs=0.05)
    assert result.info.audio_duration == pytest.approx(8, abs=0.05)


async def test_rotated_phone_video_keeps_display_orientation(config, store, tmp_path):
    source = await clip(config, tmp_path / "main.mp4", fps="30")
    rotated = tmp_path / "rotated.mp4"
    await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-y",
            "-display_rotation:v:0",
            "90",
            "-i",
            str(source),
            "-c",
            "copy",
            str(rotated),
        ]
    )
    assert (await inspect(rotated, config)).width == 180
    draft = Draft("rotated", str(rotated), "rotated.mp4", text="demo")
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    assert (result.info.width, result.info.height) == (180, 320)
    assert result.info.rotation == 0
    assert len(await timestamps(config, result.path)) == 60


async def test_capture_cancellation_terminates_subprocess():
    task = asyncio.create_task(capture([sys.executable, "-c", "import time; time.sleep(30)"]))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=4)
