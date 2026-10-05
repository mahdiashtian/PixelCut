from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from movie_editor.assets import AssetService
from movie_editor.domain import Asset, ColorGrade, Draft, Look, Quality, Settings
from movie_editor.jobs import JobService
from movie_editor.media.gallery import FilterGallery
from movie_editor.media.grading import RECIPES
from movie_editor.media.lut import validate_cube
from movie_editor.media.renderer import Renderer
from movie_editor.media.result import RenderResult

from .test_lossless import decoded
from .test_media import clip, pcm, timestamps
from .test_simple_flow import controller


def cube(path, swap=False):
    rows = ['TITLE "Reference LUT"', "LUT_3D_SIZE 2"]
    for b in (0, 1):
        for g in (0, 1):
            for r in (0, 1):
                channels = (b, g, r) if swap else (r, g, b)
                rows.append(" ".join(str(v) for v in channels))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_old_settings_and_drafts_migrate_without_losing_choices(store):
    legacy = asdict(Settings())
    legacy.pop("color_grade")
    store.set("defaults", legacy)
    assert store.defaults().color_grade == ColorGrade()
    draft = Draft("old", "source.mp4", "original.mp4", text="سلام").to_dict()
    draft["settings"] = legacy
    store.set("draft", draft)
    assert store.draft().text == "سلام" and not store.draft().settings.color_grade.active


@pytest.mark.parametrize(
    "field,value",
    [
        ("strength", 101),
        ("brightness", "nan"),
        ("contrast", -1),
        ("saturation", 201),
        ("gamma", 0),
        ("temperature", 101),
        ("vignette", 101),
    ],
)
def test_invalid_color_values_are_rejected(field, value):
    grade = ColorGrade(**{field: value})
    with pytest.raises(ValueError):
        grade.validate()


def test_dnt_has_no_guessed_recipe():
    for look in (Look.DNT1, Look.DNT2, Look.DNT3, Look.DNT4, Look.DNT5):
        assert look not in RECIPES
        with pytest.raises(ValueError, match="مرجع"):
            ColorGrade(look=look).validate()


@pytest.mark.parametrize(
    "data",
    [
        "LUT_1D_SIZE 2\n0 0 0\n1 1 1",
        "LUT_3D_SIZE 100",
        "LUT_3D_SIZE 2\n0 0 0",
        "LUT_3D_SIZE 2\nnan 0 0",
        "LUT_3D_SIZE 2\n2 0 0",
        "LUT_3D_SIZE 2\nLUT_3D_SIZE 2",
        "LUT_3D_SIZE 2\nimport os",
    ],
)
def test_invalid_cube_files_are_rejected(tmp_path, data):
    path = tmp_path / "bad.cube"
    path.write_text(data)
    with pytest.raises(ValueError, match="LUT"):
        validate_cube(path)


async def test_lut_is_retained_and_dnt_binding_survives_restart(config, store, tmp_path):
    path = cube(config.data_dir / "assets" / "reference.cube")
    service = AssetService(config, store)
    asset = await service.retain(path, "lut", "DNT3.cube")
    assert validate_cube(path) == 2
    bot, client = controller(config, store)
    bot.select_asset("d:active", "lut", asset.id, "dnt3")
    assert store.look_lut(Look.DNT3) == asset.id
    assert store.draft().settings.color_grade.look == Look.DNT3
    with pytest.raises(ValueError, match="استفاده"):
        service.delete(asset.id, "lut", bot.used_assets())
    await bot.action("d:active", ["grade_reset"])
    service.delete(asset.id, "lut", bot.used_assets())
    assert store.look_lut(Look.DNT3) is None


async def test_selecting_uninstalled_dnt_requests_reference_not_a_fake_effect(config, store):
    bot, client = controller(config, store)
    await bot.action("d:active", ["look", "dnt5"])
    assert bot.pending.kind == "lut" and bot.pending.field == "dnt5"
    assert bot.draft.settings.color_grade.look == Look.NONE
    assert "DNT5" in client.send_message.call_args.args[1]


async def test_color_presets_and_validation_keep_saved_state_consistent(config, store):
    bot, client = controller(config, store)
    await bot.action("d:active", ["look", "bw"])
    await bot.action("d:active", ["grade_set", "strength", "۵۰"])
    before = store.draft().to_dict()
    with pytest.raises(ValueError):
        await bot.action("d:active", ["grade_set", "brightness", "nan"])
    assert bot.draft.to_dict() == store.draft().to_dict() == before
    assert bot.draft.settings.color_grade.look == Look.BW
    assert bot.draft.settings.color_grade.strength == 50


@pytest.mark.integration
@pytest.mark.parametrize("look", [look for look in RECIPES if look != Look.NONE])
@pytest.mark.parametrize("quality", [Quality.LOSSLESS, Quality.HIGH])
async def test_every_local_look_preserves_frames_audio_and_dimensions(
    config, store, tmp_path, look, quality
):
    source = await clip(config, tmp_path / "source.mp4", fps="30", duration=0.5)
    draft = Draft("color", str(source), "source.mp4")
    draft.settings.quality = quality
    draft.settings.color_grade.look = look
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    assert (result.info.width, result.info.height) == (320, 180)
    before_pts, after_pts = await timestamps(config, source), await timestamps(config, result.path)
    tolerance = 0.000501 if quality == Quality.LOSSLESS else 0.000003
    assert after_pts == pytest.approx(before_pts, abs=tolerance)
    assert await pcm(config, source) == await pcm(config, result.path)
    before, after = (
        await decoded(config, source, "rgb24"),
        await decoded(config, result.path, "rgb24"),
    )
    assert before != after and len(before) == len(after)
    if look in {Look.BW, Look.NOIR} and quality == Quality.LOSSLESS:
        assert after[::3] == after[1::3] == after[2::3]


@pytest.mark.integration
async def test_zero_strength_is_pixel_exact_and_color_grade_preserves_vfr(config, store, tmp_path):
    source = await clip(config, tmp_path / "source.mp4", vfr=True, duration=0.5)
    draft = Draft("zero", str(source), "source.mp4")
    draft.settings.quality = Quality.LOSSLESS
    draft.settings.color_grade = ColorGrade(look=Look.BW, strength=0)
    neutral = await Renderer(config, store).render(draft, tmp_path / "neutral")
    assert await decoded(config, source, "yuv420p") == await decoded(
        config, neutral.path, "yuv420p"
    )
    draft.settings.color_grade = ColorGrade(
        look=Look.CINEMA, brightness=10, gamma=1.1, temperature=20, vignette=50
    )
    colored = await Renderer(config, store).render(draft, tmp_path / "colored")
    assert await timestamps(config, colored.path) == pytest.approx(
        await timestamps(config, source), abs=0.000501
    )


@pytest.mark.integration
@pytest.mark.parametrize("strength", [50, 100])
async def test_reference_lut_applies_known_channel_mapping_in_a_path_with_spaces(
    config, store, tmp_path, strength
):
    folder = tmp_path / "folder with spaces"
    folder.mkdir()
    source = await clip(config, folder / "source.mp4", fps="30", duration=0.5)
    path = cube(folder / "reference.cube", swap=True)
    store.add_asset(Asset("lut", "lut", "DNT1.cube", str(path)))
    draft = Draft("lut", str(source), "source.mp4")
    draft.settings.quality = Quality.LOSSLESS
    draft.settings.color_grade = ColorGrade(look=Look.DNT1, strength=strength, lut_id="lut")
    result = await Renderer(config, store).render(draft, folder / "output")
    before = await decoded(config, source, "rgb24")
    after = await decoded(config, result.path, "rgb24")
    assert before != after and len(before) == len(after)
    # This reference is a linear red/blue swap; interpolation should preserve the exact transform.
    expected = bytearray(before)
    alpha = strength / 100
    for index in range(0, len(before), 3):
        expected[index] = int(before[index] * (1 - alpha) + before[index + 2] * alpha)
        expected[index + 2] = int(before[index + 2] * (1 - alpha) + before[index] * alpha)
    assert max(abs(a - b) for a, b in zip(after, expected, strict=True)) <= 1


@pytest.mark.integration
async def test_filter_is_applied_before_logo(config, store, tmp_path):
    source = await clip(config, tmp_path / "source.mp4", fps="30", duration=0.5)
    path = tmp_path / "logo.png"
    Image.new("RGBA", (80, 40), (255, 0, 0, 255)).save(path)
    store.add_asset(Asset("logo", "logo", "logo.png", str(path)))
    draft = Draft("color", str(source), "source.mp4", logo_id="logo")
    draft.settings.color_grade.look = Look.BW
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    pixels = await decoded(config, result.path, "rgb24")
    assert any(
        pixels[i] > 240 and pixels[i + 1] < 10 and pixels[i + 2] < 10
        for i in range(0, 320 * 180 * 3, 3)
    )


@pytest.mark.integration
async def test_gallery_includes_only_installed_dnt_and_uses_same_frame(config, store, tmp_path):
    source = await clip(config, tmp_path / "source.mp4", duration=1)
    path = cube(tmp_path / "reference.cube", swap=True)
    store.add_asset(Asset("lut", "lut", "reference.cube", str(path)))
    store.bind_look(Look.DNT2, "lut")
    result = await FilterGallery(config, store).render(
        Draft("gallery", str(source), "source.mp4"), tmp_path / "gallery"
    )
    assert result.info is None
    with Image.open(result.path) as image:
        assert image.size == (960, 832)
        assert image.crop((0, 0, 320, 180)).tobytes() != image.crop((320, 0, 640, 180)).tobytes()
    assert (tmp_path / "gallery" / "sample-10.png").exists()
    assert not (tmp_path / "gallery" / "sample-11.png").exists()


async def test_comparison_delivery_preserves_draft_and_cleans_samples(config, store):
    draft = Draft("gallery", "source.mp4", "source.mp4")
    store.save_draft(draft)
    folder = config.data_dir / "jobs" / draft.id / "compare"
    folder.mkdir(parents=True)
    output = folder / "comparison.png"
    output.write_bytes(b"png")
    sample = folder / "sample-0.png"
    sample.write_bytes(b"png")
    gallery = SimpleNamespace(
        render=AsyncMock(return_value=RenderResult(output, None, "comparison"))
    )
    renderer = SimpleNamespace(
        render=AsyncMock(), thumbnail=AsyncMock(return_value=folder / "thumb.jpg")
    )
    client = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        send_file=AsyncMock(),
    )
    await JobService(client, config, store, renderer, gallery=gallery).run(
        draft, False, AsyncMock(), comparison=True
    )
    assert client.send_file.call_args.kwargs["force_document"] is True
    assert not output.exists() and not sample.exists()
    assert store.draft().to_dict() == draft.to_dict()
    assert store.history() == []
