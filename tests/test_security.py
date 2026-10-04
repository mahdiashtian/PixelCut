from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from movie_editor.assets import AssetService
from movie_editor.domain import Draft
from movie_editor.jobs import Activity, JobService
from movie_editor.media.renderer import Renderer
from movie_editor.telegram.controller import Controller


@pytest.mark.parametrize(
    "sender,chat,private", [(999, 999, True), (123, -100, False), (999, 123, True)]
)
async def test_unauthorized_messages_and_callbacks_are_silent(config, store, sender, chat, private):
    client = SimpleNamespace(send_message=AsyncMock())
    renderer = Renderer(config, store)
    controller = Controller(
        client,
        config,
        store,
        AssetService(config, store),
        JobService(client, config, store, renderer),
    )
    event = SimpleNamespace(
        sender_id=sender,
        chat_id=chat,
        is_private=private,
        raw_text="/start",
        data=b"g:home",
        answer=AsyncMock(),
    )
    await controller.on_message(event)
    await controller.on_callback(event)
    client.send_message.assert_not_awaited()
    event.answer.assert_not_awaited()


async def test_stale_callback_cannot_mutate_new_draft(config, store):
    client = SimpleNamespace(send_message=AsyncMock())
    controller = Controller(client, config, store, AssetService(config, store), None)
    controller.draft = Draft("new", "source", "source")
    event = SimpleNamespace(
        sender_id=123, chat_id=123, is_private=True, data=b"d:old:mute", answer=AsyncMock()
    )
    await controller.on_callback(event)
    assert not controller.draft.mute
    assert "قبلی" in client.send_message.call_args.args[1]


async def test_activity_cancels_and_can_restart():
    import asyncio

    activity = Activity()
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def operation():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    activity.start("render", operation)
    await entered.wait()
    assert activity.busy
    await activity.cancel()
    assert finished.is_set() and not activity.busy
    activity.start("second", operation)
    await asyncio.sleep(0)
    await activity.cancel()
