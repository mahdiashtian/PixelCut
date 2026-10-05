"""Compare decoded samples, including boundaries and independent channels."""

import asyncio
from array import array
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from movie_editor.domain import Asset, Draft, Look, Quality, Transition
from movie_editor.media.probe import inspect
from movie_editor.media.process import capture
from movie_editor.media.renderer import Renderer

pytestmark = pytest.mark.integration


async def sound_clip(
    config,
    path,
    *,
    rate=44100,
    channels=2,
    codec="pcm_s24le",
    duration=3,
    constant=None,
    audio_offset=0,
    video_offset=0,
    audio=True,
):
    args = [
        config.ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=s=160x96:r=25:d={duration}",
    ]
    if audio:
        expressions = [
            str(constant)
            if constant is not None
            else f"0.15*sin(2*PI*{997 + i * 123}*t)+0.04*cos(2*PI*{5303 + i * 97}*t)"
            for i in range(channels)
        ]
        layout = {1: "mono", 2: "stereo", 6: "5.1"}[channels]
        args += [
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={'|'.join(expressions)}:s={rate}:d={duration}:c={layout}",
        ]
    if audio_offset or video_offset:
        args += ["-filter_complex", f"[0:v]setpts=PTS+{video_offset}/TB[v]", "-map", "[v]"]
        if audio:
            args += ["-map", "1:a", "-af", f"asetpts=PTS+{audio_offset}/TB"]
    else:
        args += ["-map", "0:v"]
        if audio:
            args += ["-map", "1:a"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", codec]
    args += ["-threads", "2", "-avoid_negative_ts", "disabled", str(path)]
    await capture(args)
    return path


async def samples(config, path):
    return await capture(
        [config.ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "f64le", "-"]
    )


def doubles(data):
    values = array("d")
    values.frombytes(data)
    return values


@pytest.mark.parametrize("quality", list(Quality))
@pytest.mark.parametrize(
    "codec", ["pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le", "flac"]
)
async def test_trim_preserves_all_samples_and_channels(config, store, tmp_path, quality, codec):
    source = await sound_clip(config, tmp_path / "source.mkv", codec=codec)
    draft = Draft("trim-sound", str(source), source.name, trim_start=0.4, trim_end=2.6, text="TEST")
    draft.settings.quality = quality
    draft.settings.color_grade.look = Look.COOL
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    before = await samples(config, source)
    after = await samples(config, result.path)
    stride = 2 * 8
    assert after == before[round(0.4 * 44100) * stride : round(2.6 * 44100) * stride]
    assert result.info.audio_rate == 44100
    assert result.info.audio_channels == 2
    assert result.info.audio_duration == pytest.approx(2.2, abs=0.001)
    assert result.path.suffix == ".mkv"


@pytest.mark.parametrize("channels,rate", [(1, 44100), (2, 96000), (6, 48000)])
async def test_cut_joins_preserve_every_sample_at_both_boundaries(
    config,
    store,
    tmp_path,
    channels,
    rate,
):
    paths = []
    for index in range(3):
        paths.append(
            await sound_clip(
                config,
                tmp_path / f"{index}.mkv",
                channels=channels,
                rate=rate,
                duration=1.2,
                constant=0.1 + index * 0.05,
            )
        )
    store.add_asset(Asset("intro", "intro", paths[0].name, str(paths[0])))
    store.add_asset(Asset("outro", "outro", paths[2].name, str(paths[2])))
    draft = Draft("cut-sound", str(paths[1]), paths[1].name, intro_id="intro", outro_id="outro")
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    expected = b"".join([await samples(config, path) for path in paths])
    assert await samples(config, result.path) == expected
    assert result.info.audio_channels == channels
    assert result.info.audio_rate == rate


@pytest.mark.parametrize("transition", [value for value in Transition if value != Transition.CUT])
@pytest.mark.parametrize("quality", list(Quality))
async def test_every_transition_has_continuous_sound_and_exact_unmixed_regions(
    config,
    store,
    tmp_path,
    transition,
    quality,
):
    paths = []
    for index in range(3):
        paths.append(
            await sound_clip(
                config,
                tmp_path / f"{index}.mkv",
                channels=1,
                duration=1.2,
                constant=0.1 + index * 0.05,
            )
        )
    store.add_asset(Asset("intro", "intro", paths[0].name, str(paths[0])))
    store.add_asset(Asset("outro", "outro", paths[2].name, str(paths[2])))
    draft = Draft("fade-sound", str(paths[1]), paths[1].name, intro_id="intro", outro_id="outro")
    draft.settings.quality = quality
    draft.settings.intro_join.kind = draft.settings.outro_join.kind = transition
    draft.settings.intro_join.seconds = draft.settings.outro_join.seconds = 0.4
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    before = [doubles(await samples(config, path)) for path in paths]
    after = doubles(await samples(config, result.path))
    rate = 44100
    assert len(after) == round(2.8 * rate)
    assert after[: round(0.8 * rate)] == before[0][: round(0.8 * rate)]
    assert (
        after[round(1.2 * rate) : round(1.6 * rate)]
        == before[1][round(0.4 * rate) : round(0.8 * rate)]
    )
    assert after[round(2.0 * rate) :] == before[2][round(0.4 * rate) :]
    for left, right in [(0.8, 1.2), (1.6, 2.0)]:
        section = after[round(left * rate) : round(right * rate)]
        assert min(section) >= 0.09998
        assert max(section) <= 0.20001
        assert max(abs(b - a) for a, b in zip(section, section[1:], strict=False)) < 0.00001


async def test_preview_keeps_every_sample_up_to_its_selected_end(config, store, tmp_path):
    source = await sound_clip(config, tmp_path / "long.mkv", channels=1, duration=9)
    result = await Renderer(config, store).render(
        Draft("preview-sound", str(source), source.name), tmp_path / "render", preview=True
    )
    assert await samples(config, result.path) == (await samples(config, source))[: 8 * 44100 * 8]
    assert result.info.audio_channels == 1


async def test_mono_join_does_not_attenuate_original_channel(config, store, tmp_path):
    mono = await sound_clip(config, tmp_path / "mono.mkv", channels=1, duration=1, constant=0.2)
    stereo = await sound_clip(config, tmp_path / "stereo.mkv", duration=1, constant=0.1)
    store.add_asset(Asset("intro", "intro", mono.name, str(mono)))
    result = await Renderer(config, store).render(
        Draft("mono-join", str(stereo), stereo.name, intro_id="intro"), tmp_path / "render"
    )
    source = doubles(await samples(config, mono))
    expected = array("d", (value for sample in source for value in (sample, sample))).tobytes()
    assert await samples(config, result.path) == expected + await samples(config, stereo)


@pytest.mark.parametrize("audio_offset,video_offset", [(0.24, 0), (0, 0.24), (5, 5)])
async def test_audio_start_offset_aligns_without_losing_in_range_samples(
    config,
    store,
    tmp_path,
    audio_offset,
    video_offset,
):
    source = await sound_clip(
        config,
        tmp_path / "offset.mkv",
        channels=1,
        audio_offset=audio_offset,
        video_offset=video_offset,
    )
    result = await Renderer(config, store).render(
        Draft("offset-sound", str(source), source.name, trim_end=3), tmp_path / "render"
    )
    original = await samples(config, source)
    delta = round((audio_offset - video_offset) * 44100)
    count = 3 * 44100
    if delta >= 0:
        expected = b"\0" * (delta * 8) + original[: (count - delta) * 8]
    else:
        expected = original[-delta * 8 :] + b"\0" * (-delta * 8)
    assert await samples(config, result.path) == expected
    assert (await inspect(result.path, config)).duration == pytest.approx(3, abs=0.001)


@pytest.mark.parametrize("audio_offset,video_offset", [(0.24, 0), (0, 0.24), (5, 5)])
async def test_whole_clip_retains_audio_even_before_or_after_video(
    config,
    store,
    tmp_path,
    audio_offset,
    video_offset,
):
    source = await sound_clip(
        config,
        tmp_path / "offset-full.mkv",
        channels=1,
        audio_offset=audio_offset,
        video_offset=video_offset,
    )
    result = await Renderer(config, store).render(
        Draft("whole-offset-sound", str(source), source.name), tmp_path / "render"
    )
    assert await samples(config, result.path) == await samples(config, source)
    before = await inspect(source, config)
    assert (result.info.audio_start - result.info.video_start) == pytest.approx(
        before.audio_start - before.video_start, abs=0.002
    )


@pytest.mark.parametrize("quality", list(Quality))
@pytest.mark.parametrize(
    "codec,extension",
    [
        ("aac", ".mp4"),
        ("mp3", ".mp4"),
        ("libopus", ".mkv"),
        ("alac", ".mp4"),
        ("flac", ".mkv"),
        ("pcm_s32le", ".mkv"),
        ("pcm_f64le", ".mkv"),
    ],
)
async def test_whole_clip_keeps_all_decoded_samples_for_common_codecs(
    config,
    store,
    tmp_path,
    quality,
    codec,
    extension,
):
    source = await sound_clip(
        config, tmp_path / ("source" + extension), rate=48000, codec=codec, duration=3
    )
    draft = Draft("full-sound", str(source), source.name, text="TEST")
    draft.settings.quality = quality
    draft.settings.color_grade.look = Look.BW
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    assert await samples(config, result.path) == await samples(config, source)
    assert result.info.audio_channels == 2
    assert result.info.audio_rate == 48000


@pytest.mark.parametrize("codec", ["aac", "mp3", "alac"])
async def test_native_lossless_watermark_copies_compressed_audio(config, store, tmp_path, codec):
    source = await sound_clip(config, tmp_path / "source.mp4", codec=codec, duration=3)
    draft = Draft("native-copy", str(source), source.name, text="PixelCut")
    draft.settings.quality = Quality.LOSSLESS
    assert draft.settings.quality == Quality.LOSSLESS
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    assert result.path.suffix == ".mp4"
    assert result.info.audio_codec == codec
    assert await samples(config, source) == await samples(config, result.path)

    async def packets(path):
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
                "-hash",
                "sha256",
                "-",
            ]
        )

    assert await packets(source) == await packets(result.path)


async def test_mixed_rates_keep_highest_rate_and_do_not_change_its_samples(config, store, tmp_path):
    intro = await sound_clip(
        config, tmp_path / "intro.mkv", rate=44100, channels=1, duration=1, constant=0.125
    )
    source = await sound_clip(
        config, tmp_path / "main.mkv", rate=96000, channels=1, duration=1, constant=0.25
    )
    store.add_asset(Asset("intro", "intro", intro.name, str(intro)))
    draft = Draft("rates-sound", str(source), source.name, intro_id="intro")
    draft.settings.quality = Quality.LOSSLESS
    with pytest.raises(ValueError, match="نرخ نمونه"):
        await Renderer(config, store).render(draft, tmp_path / "strict")
    draft.settings.quality = Quality.HIGH
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    output = await samples(config, result.path)
    assert result.info.audio_rate == 96000
    assert len(output) == 2 * 96000 * 8
    assert output[96000 * 8 :] == await samples(config, source)
    intro_values = doubles(output[: 96000 * 8])
    assert max(abs(value - 0.125) for value in intro_values[200:-200]) < 1e-9


@pytest.mark.parametrize("silent", ["intro", "main", "outro"])
async def test_only_silent_segments_are_silent_and_boundaries_are_exact(
    config, store, tmp_path, silent
):
    paths = {}
    for kind in ("intro", "main", "outro"):
        paths[kind] = await sound_clip(
            config, tmp_path / f"{kind}.mkv", channels=1, duration=1, audio=kind != silent
        )
    for kind in ("intro", "outro"):
        store.add_asset(Asset(kind, kind, paths[kind].name, str(paths[kind])))
    draft = Draft(
        "silent-sound", str(paths["main"]), paths["main"].name, intro_id="intro", outro_id="outro"
    )
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    expected = b""
    for kind in ("intro", "main", "outro"):
        expected += b"\0" * (44100 * 8) if kind == silent else await samples(config, paths[kind])
    assert await samples(config, result.path) == expected


async def test_incompatible_surround_join_is_rejected_instead_of_downmixed(config, store, tmp_path):
    source = await sound_clip(config, tmp_path / "surround.mkv", channels=6, duration=1)
    intro = await sound_clip(config, tmp_path / "stereo.mkv", channels=2, duration=1)
    store.add_asset(Asset("intro", "intro", intro.name, str(intro)))
    with pytest.raises(ValueError, match="چیدمان"):
        await Renderer(config, store).render(
            Draft("layout-sound", str(source), source.name, intro_id="intro"), tmp_path / "render"
        )


async def video_pixels(config, path):
    return await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "rawvideo",
            "-",
        ]
    )


async def test_three_concurrent_renders_keep_every_pixel_sample_and_frame(
    config,
    store,
    tmp_path,
    monkeypatch,
):
    from movie_editor.media import renderer as renderer_module

    from .test_media import timestamps

    sources = [
        await sound_clip(config, tmp_path / "one.mkv", rate=44100, channels=2, duration=3),
        await sound_clip(
            config, tmp_path / "two.mkv", rate=96000, channels=1, codec="pcm_s32le", duration=3
        ),
        await sound_clip(config, tmp_path / "three.mkv", rate=48000, channels=6, duration=1.2),
    ]
    store.add_asset(Asset("three-intro", "intro", sources[2].name, str(sources[2])))
    store.add_asset(Asset("three-outro", "outro", sources[2].name, str(sources[2])))
    drafts = [
        Draft("one", str(sources[0]), sources[0].name),
        Draft("two", str(sources[1]), sources[1].name, trim_start=0.4, trim_end=2.6),
        Draft(
            "three",
            str(sources[2]),
            sources[2].name,
            intro_id="three-intro",
            outro_id="three-outro",
        ),
    ]
    for draft in drafts:
        draft.settings.quality = Quality.LOSSLESS
    active, peak, arrived = 0, 0, 0
    barrier = asyncio.Event()
    original_encode = renderer_module.encode

    async def observed_encode(*args, **kwargs):
        nonlocal active, peak, arrived
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
        timeout=60,
    )
    assert peak == 3
    assert len({result.path for result in results}) == 3
    pixels = [await video_pixels(config, source) for source in sources]
    sounds = [await samples(config, source) for source in sources]
    expected_pixels = [
        pixels[0],
        pixels[1][10 * 160 * 96 * 3 // 2 : 65 * 160 * 96 * 3 // 2],
        pixels[2] * 3,
    ]
    expected_sounds = [
        sounds[0],
        sounds[1][round(0.4 * 96000) * 8 : round(2.6 * 96000) * 8],
        sounds[2] * 3,
    ]
    for index, result in enumerate(results):
        assert await video_pixels(config, result.path) == expected_pixels[index]
        assert await samples(config, result.path) == expected_sounds[index]
        expected_frames = [75, 55, 90][index]
        times = await timestamps(config, result.path)
        assert len(times) == expected_frames
        assert times == pytest.approx(
            [frame / 25 for frame in range(expected_frames)], abs=0.000501
        )


async def test_join_refuses_to_cut_a_voice_tail_outside_the_video(config, store, tmp_path):
    source = await sound_clip(config, tmp_path / "source.mkv", duration=1)
    intro = await sound_clip(config, tmp_path / "late-intro.mkv", duration=1, audio_offset=0.24)
    store.add_asset(Asset("intro", "intro", intro.name, str(intro)))
    with pytest.raises(ValueError, match="قطع صدا"):
        await Renderer(config, store).render(
            Draft("tail-sound", str(source), source.name, intro_id="intro"), tmp_path / "render"
        )


async def test_multiple_audio_tracks_are_not_silently_discarded(config, tmp_path):
    source = await sound_clip(config, tmp_path / "source.mkv", duration=1)
    multiple = tmp_path / "multiple.mkv"
    await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            str(multiple),
        ]
    )
    with pytest.raises(ValueError, match="چندترک"):
        await inspect(multiple, config)


async def test_runtime_audio_guard_rejects_a_cut_missing_or_changed_sound(config, store, tmp_path):
    from movie_editor.media.audio import encoding, validate_output, verify_samples

    source = await sound_clip(config, tmp_path / "source.mkv", duration=1)
    truncated = await sound_clip(config, tmp_path / "short.mkv", duration=0.8)
    before, after = await inspect(source, config), await inspect(truncated, config)
    with pytest.raises(ValueError, match="نمونه"):
        await verify_samples(source, truncated, config, before, after, None)
    changed = await sound_clip(config, tmp_path / "changed.mkv", duration=1, constant=0.125)
    with pytest.raises(ValueError, match="یکسان"):
        await verify_samples(source, changed, config, before, await inspect(changed, config), None)
    silent = await sound_clip(config, tmp_path / "silent.mkv", duration=1, audio=False)
    plan = encoding(before, filtered=False, lossless=True, muted=False)
    with pytest.raises(ValueError, match="مفقود"):
        validate_output(await inspect(silent, config), before, plan, 1, None)


@pytest.mark.parametrize("extra,allowed", [(256 / 48000, True), (0.05, False)])
async def test_old_decoder_codec_padding_does_not_mask_a_real_audio_tail(
    config,
    tmp_path,
    monkeypatch,
    extra,
    allowed,
):
    from movie_editor.media import audio as audio_module
    from movie_editor.media.audio import AudioTimeline, validate_join_span

    source = await sound_clip(config, tmp_path / "aac.mp4", codec="aac", rate=48000, duration=2)
    info = replace(await inspect(source, config), audio_duration=2, audio_start=0)
    decoded = AudioTimeline(round((2 + extra) * 48000), 0, 2 + extra, (), 1024)
    monkeypatch.setattr(audio_module, "timeline", AsyncMock(return_value=decoded))
    if allowed:
        await validate_join_span(source, info, config)
    else:
        with pytest.raises(ValueError, match="قطع صدا"):
            await validate_join_span(source, info, config)
