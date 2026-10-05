"""Compressed uploads must not become lossless-sized exports by default."""

import asyncio
from dataclasses import replace

import pytest

from movie_editor.domain import Draft, Position, Quality
from movie_editor.media import renderer as renderer_module
from movie_editor.media.probe import inspect
from movie_editor.media.process import capture
from movie_editor.media.renderer import Renderer
from movie_editor.storage import Store

from .test_media import clip, pcm, timestamps


async def similarity(config, source, output, folder, filters="crop=320:80:0:0"):
    await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-threads",
            "2",
            "-i",
            str(source),
            "-threads",
            "2",
            "-i",
            str(output),
            "-filter_complex",
            f"[0:v]{filters}[a];[1:v]{filters}[b];[a][b]ssim=stats_file=quality.stats[v]",
            "-map",
            "[v]",
            "-an",
            "-f",
            "null",
            "-",
        ],
        cwd=folder,
    )
    return [
        float(line.split("All:")[1].split()[0])
        for line in (folder / "quality.stats").read_text().splitlines()
    ]


@pytest.mark.integration
@pytest.mark.parametrize("container,vfr", [("mp4", False), ("mp4", True), ("mkv", True)])
async def test_default_watermark_keeps_source_size_frames_timestamps_and_sound(
    config, store, tmp_path, container, vfr
):
    source = await clip(config, tmp_path / "source.mp4", fps="30", duration=8, vfr=vfr)
    if container == "mkv":
        converted = tmp_path / "source.mkv"
        await capture(
            [config.ffmpeg, "-v", "error", "-y", "-i", str(source), "-c", "copy", str(converted)]
        )
        source = converted
    draft = Draft("sized", str(source), source.name, text="PixelCut")
    draft.settings.text_style.position = Position.BOTTOM_RIGHT
    assert draft.settings.quality == Quality.SOURCE
    folder = tmp_path / "render"
    result = await Renderer(config, store).render(draft, folder)
    before, after = await timestamps(config, source), await timestamps(config, result.path)
    assert len(before) == len(after)
    assert after == pytest.approx(before, abs=0.001)
    assert (result.info.width, result.info.height) == (320, 180)
    assert result.path.stat().st_size <= source.stat().st_size * 1.25 + 8192
    assert result.info.audio_codec == "aac"
    assert await pcm(config, source) == await pcm(config, result.path)
    assert not list(folder.glob("source-rate*"))
    assert (folder / "ffmpeg.pass1.metrics.json").is_file()

    scores = await similarity(config, source, result.path, tmp_path)
    assert len(scores) == len(before)
    assert sum(scores) / len(scores) > 0.99


@pytest.mark.integration
@pytest.mark.parametrize("vfr", [False, True])
async def test_hevc_input_stays_hevc_and_keeps_source_size(config, store, tmp_path, vfr):
    config = replace(config, max_render_seconds=90)
    source = tmp_path / "hevc.mp4"
    timing = ["-vf", "select='not(eq(mod(n,3),1))'", "-fps_mode", "passthrough"] if vfr else []
    await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=30:d=6",
            *timing,
            "-c:v",
            "libx265",
            "-preset",
            "fast",
            "-x265-params",
            "pools=2:frame-threads=1:log-level=error",
            "-crf",
            "23",
            str(source),
        ],
        timeout=90,
    )
    draft = Draft("hevc", str(source), source.name, text="PixelCut")
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    assert result.info.video_codec == "hevc"
    assert (await inspect(result.path, config, count_frames=True)).frames == (120 if vfr else 180)
    assert await timestamps(config, result.path) == pytest.approx(
        await timestamps(config, source), abs=0.000003
    )
    assert result.path.stat().st_size <= source.stat().st_size * 1.25 + 8192


@pytest.mark.integration
@pytest.mark.parametrize(
    "encoder,pixel_format",
    [("ffv1", "yuv422p"), ("ffv1", "yuv444p"), ("ffv1", "bgr0"), ("libx265", "gbrp")],
)
async def test_source_size_preserves_native_color_layout_and_rgb_colors(
    config, store, tmp_path, encoder, pixel_format
):
    rgb = pixel_format in {"bgr0", "gbrp"}
    source = tmp_path / "source.mkv"
    options = ["-colorspace", "rgb", "-color_range", "pc"] if rgb else []
    if encoder == "libx265":
        options += [
            "-preset",
            "fast",
            "-crf",
            "18",
            "-x265-params",
            "pools=2:frame-threads=1:log-level=error",
        ]
    await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=25:d=3",
            "-c:v",
            encoder,
            "-pix_fmt",
            pixel_format,
            *options,
            str(source),
        ]
    )
    result = await Renderer(config, store).render(
        Draft("color-layout", str(source), source.name), tmp_path / "render"
    )
    assert result.info.pixel_format == ("gbrp" if rgb else pixel_format)
    assert result.path.stat().st_size <= source.stat().st_size * 1.25 + 8192
    scores = await similarity(config, source, result.path, tmp_path, "format=gbrp")
    assert len(scores) == 75
    # A second lossy RGB HEVC generation at the same rate adds quantization.
    # Check for the much larger color shift caused by converting RGB to YUV
    # while retaining identity-matrix/full-range RGB tags; pixel identity is not promised.
    assert sum(scores) / len(scores) > (0.95 if encoder == "libx265" else 0.99)


@pytest.mark.integration
async def test_three_source_sized_renders_isolate_both_passes_and_preserve_sound_and_frames(
    config, store, tmp_path, monkeypatch
):
    config = replace(config, max_render_seconds=90)
    sources = [
        await clip(config, tmp_path / "one.mp4", fps="25", duration=6),
        await clip(config, tmp_path / "two.mp4", fps="30", duration=6, vfr=True),
        await clip(config, tmp_path / "three.mp4", fps="60", duration=6),
    ]
    drafts = [
        Draft(str(i), str(path), path.name, text=f"PixelCut {i}") for i, path in enumerate(sources)
    ]
    barrier = asyncio.Event()
    arrived, active, peak = 0, 0, 0
    original_encode = renderer_module.encode

    async def observed_encode(*args, **kwargs):
        nonlocal arrived, active, peak
        arrived += 1
        if arrived == 3:
            barrier.set()
        await barrier.wait()
        active += 1
        peak = max(peak, active)
        try:
            return await original_encode(*args, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(renderer_module, "encode", observed_encode)
    renderer = Renderer(config, store)
    results = await asyncio.wait_for(
        asyncio.gather(*(renderer.render(draft, tmp_path / draft.id) for draft in drafts)),
        timeout=90,
    )
    assert peak == 3
    assert arrived == 6
    assert len({result.path.parent for result in results}) == 3
    for source, result in zip(sources, results, strict=True):
        assert await timestamps(config, result.path) == pytest.approx(
            await timestamps(config, source), abs=0.000003
        )
        assert await pcm(config, result.path) == await pcm(config, source)
        assert result.path.stat().st_size <= source.stat().st_size * 1.25 + 8192
        assert not list(result.path.parent.glob("source-rate*"))
        assert (result.path.parent / "ffmpeg.pass1.metrics.json").is_file()
        assert (result.path.parent / "ffmpeg.metrics.json").is_file()


@pytest.mark.integration
async def test_source_size_limit_stops_oversized_output_and_cleans_pass_stats(
    config, store, tmp_path, monkeypatch
):
    source = await clip(config, tmp_path / "source.mp4", duration=3)
    original_plan = renderer_module.plan_rate

    async def tiny_limit(*args, **kwargs):
        return replace(await original_plan(*args, **kwargs), ceiling_bytes=1024)

    monkeypatch.setattr(renderer_module, "plan_rate", tiny_limit)
    folder = tmp_path / "render"
    with pytest.raises(ValueError, match="حجم خروجی از سقف متناسب"):
        await Renderer(config, store).render(
            Draft("capped", str(source), source.name, text="PixelCut"), folder
        )
    assert not list(folder.glob("source-rate*"))
    assert source.is_file()


def test_existing_lossless_defaults_and_draft_are_migrated_once(store, tmp_path):
    draft = Draft("legacy", "movie.mp4", "movie.mp4", text="saved")
    draft.settings.quality = Quality.LOSSLESS
    draft.settings.text_style.width_percent = 37
    store.save_draft(draft)
    store.save_defaults(draft.settings)
    with store.db:
        store.db.execute("DELETE FROM preferences WHERE key='source_quality_migrated'")
    reopened = Store(tmp_path / "test.sqlite3")
    try:
        assert reopened.defaults().quality == Quality.SOURCE
        assert reopened.draft().settings.quality == Quality.SOURCE
        assert reopened.draft().text == "saved"
        assert reopened.defaults().text_style.width_percent == 37
        explicit = reopened.defaults()
        explicit.quality = Quality.LOSSLESS
        reopened.save_defaults(explicit)
    finally:
        reopened.close()
    again = Store(tmp_path / "test.sqlite3")
    try:
        assert again.defaults().quality == Quality.LOSSLESS
    finally:
        again.close()
