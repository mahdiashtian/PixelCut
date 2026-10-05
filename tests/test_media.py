import json

import pytest
from PIL import Image

from movie_editor.domain import Asset, Color, Draft, Quality, Transition
from movie_editor.media.graphics import find_font, text_overlay
from movie_editor.media.probe import inspect
from movie_editor.media.process import capture
from movie_editor.media.renderer import Renderer

pytestmark = pytest.mark.integration


async def clip(config, path, *, fps="30000/1001", color=None, audio=True, vfr=False, duration=2):
    source = f"color=c={color}:s=320x180:r={fps}" if color else f"testsrc2=s=320x180:r={fps}"
    args = [
        config.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        source,
    ]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    if vfr:
        args += ["-vf", "select='not(eq(mod(n,3),1))'", "-fps_mode", "passthrough"]
    args += [
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-threads",
        "2",
        str(path),
    ]
    await capture(args)
    return path


async def timestamps(config, path):
    data = json.loads(
        await capture(
            [
                config.ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=pts_time",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    return [float(frame["pts_time"]) for frame in data["frames"]]


async def audio_hash(config, path):
    return await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            "-f",
            "hash",
            "-",
        ]
    )


async def pcm(config, path):
    return await capture(
        [config.ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "f32le", "-"]
    )


@pytest.mark.parametrize("vfr", [False, True])
@pytest.mark.parametrize("quality", [Quality.HIGH, Quality.LOSSLESS])
async def test_watermark_preserves_each_frame_timestamp_and_audio(
    config, store, tmp_path, vfr, quality
):
    source = await clip(config, tmp_path / "source.mp4", vfr=vfr)
    draft = Draft("watermark", str(source), "source.mp4", text="سلام @demo")
    draft.settings.quality = quality
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    before, after = await timestamps(config, source), await timestamps(config, result.path)
    assert len(before) == len(after)
    # Matroska stores timestamps in milliseconds; MP4 uses the microsecond encoder timebase.
    tolerance = 0.000501 if quality == Quality.LOSSLESS else 0.000003
    assert after == pytest.approx(before, abs=tolerance)
    assert (result.info.width, result.info.height) == (320, 180)
    if quality == Quality.HIGH:
        assert await audio_hash(config, source) == await audio_hash(config, result.path)
    else:
        assert await pcm(config, source) == await pcm(config, result.path)


@pytest.mark.parametrize("quality", [Quality.LOSSLESS, Quality.SOURCE])
async def test_dynamic_text_changes_on_light_and_dark_frames(config, store, tmp_path, quality):
    dark = await clip(
        config, tmp_path / "dark.mp4", color="black", audio=False, fps="30", duration=1
    )
    light = await clip(
        config, tmp_path / "light.mp4", color="white", audio=False, fps="30", duration=1
    )
    source = tmp_path / "contrast.mp4"
    await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(dark),
            "-i",
            str(light),
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            str(source),
        ]
    )
    draft = Draft("contrast", str(source), "contrast.mp4", text="TEST")
    draft.settings.quality = quality
    draft.settings.text_style.width_percent = 50
    draft.settings.text_style.color = Color.DYNAMIC
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    overlay = text_overlay(
        "TEST", find_font(None), 320, 180, draft.settings.text_style, tmp_path / "mask.png"
    )
    with Image.open(overlay.path) as mask:
        opaque = [
            (x, y)
            for y in range(mask.height)
            for x in range(mask.width)
            if mask.getpixel((x, y))[3] > 240
        ]
    means = []
    for t in (0.5, 1.5):
        pixels = await capture(
            [
                config.ffmpeg,
                "-v",
                "error",
                "-ss",
                str(t),
                "-i",
                str(result.path),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ]
        )
        values = [pixels[((overlay.y + y) * 320 + overlay.x + x) * 3] for x, y in opaque]
        means.append(sum(values) / len(values))
    assert means[0] > 220  # white text on black
    assert means[1] < 35  # black text on white


@pytest.mark.parametrize("transition", list(Transition))
@pytest.mark.parametrize("quality", [Quality.HIGH, Quality.LOSSLESS])
async def test_every_transition_with_intro_outro_and_missing_audio(
    config, store, tmp_path, transition, quality
):
    source = await clip(config, tmp_path / "main.mp4", fps="30")
    intro = await clip(config, tmp_path / "intro.mp4", fps="24", color="red", audio=False)
    outro = await clip(config, tmp_path / "outro.mp4", fps="15", color="blue")
    store.add_asset(Asset("intro", "intro", "intro.mp4", str(intro)))
    store.add_asset(Asset("outro", "outro", "outro.mp4", str(outro)))
    draft = Draft(
        "joined", str(source), "main.mp4", intro_id="intro", outro_id="outro", text="demo"
    )
    draft.settings.quality = quality
    draft.settings.intro_join.kind = draft.settings.outro_join.kind = transition
    draft.settings.intro_join.seconds = draft.settings.outro_join.seconds = 0.5
    logo_path = tmp_path / "logo.png"
    Image.new("RGBA", (80, 40), (0, 255, 0, 160)).save(logo_path)
    store.add_asset(Asset("logo", "logo", "logo.png", str(logo_path)))
    draft.logo_id = "logo"
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    assert result.info.duration == pytest.approx(6 if transition == Transition.CUT else 5, abs=0.1)
    assert result.info.audio_codec == "pcm_f64le"
    assert result.path.suffix == ".mkv"
    if transition == Transition.CUT:
        assert len(await timestamps(config, result.path)) == 48 + 60 + 30
    else:
        assert float(result.info.fps) == pytest.approx(30, abs=0.3)


async def test_trim_mute_lossless_and_preview(config, store, tmp_path):
    source = await clip(config, tmp_path / "source.mp4", fps="30", duration=10)
    draft = Draft(
        "trim", str(source), "source.mp4", text="demo", trim_start=1, trim_end=2, mute=True
    )
    draft.settings.quality = Quality.LOSSLESS
    result = await Renderer(config, store).render(draft, tmp_path / "lossless")
    assert result.path.suffix == ".mkv" and result.info.audio_codec is None
    assert result.info.duration == pytest.approx(1, abs=0.1)
    draft.trim_start, draft.trim_end, draft.mute = 0, None, False
    draft.settings.quality = Quality.FAST
    preview = await Renderer(config, store).render(draft, tmp_path / "preview", preview=True)
    assert preview.info.duration == pytest.approx(8, abs=0.1)


async def test_bad_input_and_invalid_trim_are_rejected(config, store, tmp_path):
    fake = tmp_path / "fake.mp4"
    fake.write_text("not video")
    with pytest.raises(ValueError):
        await inspect(fake, config)
    source = await clip(config, tmp_path / "source.mp4")
    with pytest.raises(ValueError, match="برش"):
        await Renderer(config, store).render(
            Draft("trim", str(source), "source.mp4", trim_end=99), tmp_path / "render"
        )
