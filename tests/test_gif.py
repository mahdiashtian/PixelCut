from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from movie_editor.domain import Draft, GifRange
from movie_editor.jobs import JobService
from movie_editor.media.gif import GifExporter
from movie_editor.media.process import capture
from movie_editor.media.result import RenderResult
from movie_editor.telegram.controller import Pending
from movie_editor.telegram.views import draft_buttons, gif_buttons

from .test_media import clip, timestamps
from .test_simple_flow import controller


@pytest.mark.parametrize("text", ["12 20", "۱۲ تا ۲۰", "۱۲، ۲۰"])
def test_gif_range_accepts_seconds_in_both_languages(text):
    assert GifRange.from_text(text).resolve(30) == (12, 20)


@pytest.mark.parametrize("text", ["20 12", "12 12", "-1 20", "nan 20", "12", "abc xyz"])
def test_gif_range_rejects_invalid_input(text):
    with pytest.raises(ValueError):
        GifRange.from_text(text)


def test_gif_range_supports_whole_clip_and_fractional_seconds():
    assert GifRange.from_text("کل").resolve(120) == (0, 120)
    assert GifRange.from_text("۱٫۵ تا ۲٫۲۵").resolve(30) == (1.5, 2.25)
    with pytest.raises(ValueError):
        GifRange(10, 40).resolve(30)


def test_gif_is_accessible_from_the_simple_menu():
    draft = Draft("current", "source.mp4", "source.mp4")
    rows = draft_buttons(draft)
    assert len(rows) == 6
    assert any(b.type.data == b"d:current:gif" for row in rows for b in row)
    assert [b.type.data for b in gif_buttons(draft)[0]] == [
        b"d:current:gif_all",
        b"d:current:gif_range",
    ]


async def test_gif_range_flow_keeps_existing_edits(config, store, monkeypatch):
    bot, client = controller(config, store)
    bot.jobs = SimpleNamespace(run=AsyncMock())
    bot.draft.text, bot.draft.trim_start, bot.draft.trim_end = "سلام", 2, 10
    store.save_draft(bot.draft)
    before = bot.draft.to_dict()
    monkeypatch.setattr(
        "movie_editor.telegram.controller.inspect",
        AsyncMock(return_value=SimpleNamespace(duration=30)),
    )
    await bot.action("d:active", ["gif_range"])
    assert bot.pending == Pending("gif", "d:active")
    await bot.accept_text(bot.pending, "۱۲ تا ۲۰")
    await bot.activity.task
    assert bot.pending is None
    assert bot.jobs.run.call_args.kwargs["gif"] == GifRange(12, 20)
    assert bot.draft.to_dict() == before == store.draft().to_dict()


async def test_invalid_gif_range_can_be_corrected(config, store, monkeypatch):
    bot, client = controller(config, store)
    bot.jobs = SimpleNamespace(run=AsyncMock())
    monkeypatch.setattr(
        "movie_editor.telegram.controller.inspect",
        AsyncMock(return_value=SimpleNamespace(duration=30)),
    )
    await bot.action("d:active", ["gif_range"])
    with pytest.raises(ValueError, match="مدت"):
        await bot.accept_text(bot.pending, "20 40")
    assert bot.pending == Pending("gif", "d:active")
    bot.jobs.run.assert_not_awaited()


async def test_whole_gif_flow_ignores_video_trim(config, store, monkeypatch):
    bot, client = controller(config, store)
    bot.jobs = SimpleNamespace(run=AsyncMock())
    bot.draft.trim_start, bot.draft.trim_end = 2, 10
    monkeypatch.setattr(
        "movie_editor.telegram.controller.inspect",
        AsyncMock(return_value=SimpleNamespace(duration=30)),
    )
    await bot.action("d:active", ["gif_all"])
    await bot.activity.task
    assert bot.jobs.run.call_args.kwargs["gif"] == GifRange()
    assert bot.draft.trim_start == 2 and bot.draft.trim_end == 10


@pytest.mark.integration
@pytest.mark.parametrize("fps,vfr", [("30000/1001", False), ("30", True), ("60", False)])
async def test_whole_gif_preserves_size_frames_and_centisecond_timing(config, tmp_path, fps, vfr):
    source = await clip(config, tmp_path / "source.mp4", fps=fps, vfr=vfr, duration=1)
    result = await GifExporter(config).convert(source, GifRange(), tmp_path / "gif")
    assert result.path.read_bytes().startswith((b"GIF87a", b"GIF89a"))
    assert (result.info.width, result.info.height) == (320, 180)
    assert result.info.audio_codec is None
    original_pts = await timestamps(config, source)
    with Image.open(result.path) as image:
        assert image.n_frames == len(original_pts)
        assert image.info["loop"] == 0
        time = 0.0
        for index, original in enumerate(original_pts):
            image.seek(index)
            assert time == pytest.approx(original, abs=0.0101)
            time += image.info["duration"] / 1000
    assert time == pytest.approx(1, abs=0.05)


@pytest.mark.integration
async def test_gif_selects_only_requested_scene_and_retains_flat_colors(config, tmp_path):
    source = tmp_path / "scenes.mkv"
    await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=10:d=1",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "ffv1",
            "-pix_fmt",
            "bgr0",
            "-threads",
            "2",
            str(source),
        ]
    )
    result = await GifExporter(config).convert(source, GifRange(1, 1.5), tmp_path / "gif")
    reference = await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-ss",
            "1",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
    )
    with Image.open(result.path) as image:
        assert image.n_frames == 5
        for index in range(image.n_frames):
            image.seek(index)
            assert image.convert("RGB").tobytes() == reference
    assert result.info.duration == pytest.approx(0.5, abs=0.01)


@pytest.mark.integration
async def test_gif_large_output_stops_without_changing_resolution(config, tmp_path):
    source = await clip(config, tmp_path / "source.mp4", fps="30", duration=2)
    small_limit = replace(config, max_upload_mb=0.001)
    with pytest.raises(ValueError, match="حجم"):
        await GifExporter(small_limit).convert(source, GifRange(), tmp_path / "gif")


@pytest.mark.integration
async def test_gif_rejects_out_of_bounds_range_and_high_fps(config, tmp_path):
    source = await clip(config, tmp_path / "source.mp4", fps="120", duration=0.5)
    exporter = GifExporter(config)
    with pytest.raises(ValueError, match="مدت"):
        await exporter.convert(source, GifRange(0, 2), tmp_path / "bad-range")
    with pytest.raises(ValueError, match="۱۰۰"):
        await exporter.convert(source, GifRange(), tmp_path / "fast")
    assert not (tmp_path / "fast" / "edited.gif").exists()


@pytest.mark.parametrize("failed", [False, True])
async def test_gif_delivery_is_original_file_and_always_cleans_up(config, store, failed):
    draft = Draft("gif-job", "source.mp4", "source.mp4", trim_start=2, trim_end=10)
    store.save_draft(draft)
    folder = config.data_dir / "jobs" / draft.id / "gif"
    folder.mkdir(parents=True)
    output = folder / "edited.gif"
    output.write_bytes(b"GIF89a")
    exporter = SimpleNamespace(
        convert=AsyncMock(
            return_value=RenderResult(
                output, SimpleNamespace(duration=1, width=320, height=180), "GIF notice"
            )
        )
    )
    renderer = SimpleNamespace(
        render=AsyncMock(), thumbnail=AsyncMock(return_value=folder / "thumb.jpg")
    )
    client = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        send_file=AsyncMock(side_effect=RuntimeError("send failed") if failed else None),
    )
    await JobService(client, config, store, renderer, exporter).run(
        draft, False, AsyncMock(), gif=GifRange(3, 4)
    )
    renderer.render.assert_not_awaited()
    assert exporter.convert.call_args.args[:2] == (Path(draft.source), GifRange(3, 4))
    assert client.send_file.call_args.kwargs["force_document"] is True
    assert client.send_file.call_args.kwargs["mime_type"] == "image/gif"
    assert not output.exists()
    assert store.draft().to_dict() == draft.to_dict()
    assert store.history()[0]["status"] == ("failed" if failed else "done")
