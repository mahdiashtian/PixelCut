import pytest

from movie_editor.domain import Draft, Position, Settings, number


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "abc", 0, 96])
def test_reject_invalid_width(value):
    with pytest.raises(ValueError):
        number(value, 1, 95, "width")


def test_defaults_do_not_store_content_or_trim(store):
    draft = Draft(
        "id",
        "source.mp4",
        "clip.mp4",
        text="سلام",
        logo_id="logo",
        intro_id="intro",
        trim_start=1,
        trim_end=3,
        mute=True,
    )
    draft.settings.text_style.position = Position.BOTTOM_RIGHT
    store.save_draft(draft)
    store.save_defaults(draft.settings)
    restored = store.draft()
    assert restored.text == "سلام" and restored.trim_end == 3
    new = Draft("other", "other.mp4", "other.mp4", store.defaults())
    assert new.text == "" and new.logo_id is None and new.intro_id is None
    assert new.trim_start == 0 and new.trim_end is None and not new.mute
    assert new.settings.text_style.position == Position.BOTTOM_RIGHT


def test_invalid_position_and_trim_are_rejected():
    settings = Settings()
    settings.text_style.position = "bad"
    with pytest.raises(ValueError):
        settings.validate()
    with pytest.raises(ValueError):
        Draft("id", "source", "source", trim_start=10, trim_end=5).validate()
