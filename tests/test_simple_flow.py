from types import SimpleNamespace
from unittest.mock import AsyncMock

from movie_editor.assets import AssetService
from movie_editor.domain import Draft, Quality, Transition, number
from movie_editor.telegram.controller import Controller
from movie_editor.telegram.views import advanced_buttons, draft_buttons


def controller(config, store):
    client = SimpleNamespace(send_message=AsyncMock())
    bot = Controller(client, config, store, AssetService(config, store), None)
    bot.draft = Draft("active", "source.mp4", "source.mp4")
    return bot, client


def test_default_is_lossless_and_cut_preserves_fps():
    draft = Draft("id", "source", "source")
    assert draft.settings.quality == Quality.LOSSLESS
    assert draft.settings.intro_join.kind == Transition.CUT
    assert draft.settings.outro_join.kind == Transition.CUT
    assert len(draft_buttons(draft)) == 5
    assert advanced_buttons(draft)
    assert number("۵٫۵٪", 1, 95, "value") == 5.5


async def test_intro_button_accepts_video_immediately(config, store):
    bot, client = controller(config, store)
    await bot.action("d:active", ["library", "intro", "0"])
    assert bot.pending.kind == "intro" and bot.pending.scope == "d:active"
    assert "بفرستید" in client.send_message.call_args.args[1]


async def test_size_can_be_selected_without_typing(config, store):
    bot, client = controller(config, store)
    await bot.action("d:active", ["preset", "text", "width_percent", "30"])
    assert bot.draft.settings.text_style.width_percent == 30
    assert store.draft().settings.text_style.width_percent == 30


async def test_cancel_and_stale_stop_do_not_delete_project(config, store):
    bot, client = controller(config, store)
    await bot.cancel()
    assert bot.draft.id == "active"
    event = SimpleNamespace(
        sender_id=123, chat_id=123, is_private=True, data=b"g:stop", answer=AsyncMock()
    )
    await bot.on_callback(event)
    assert bot.draft.id == "active"
