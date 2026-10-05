from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from movie_editor.domain import Delivery, Draft, Quality
from movie_editor.jobs import JobService
from movie_editor.media.renderer import RenderResult


async def test_default_delivery_is_document_and_temporary_output_is_cleaned(config, store):
    draft = Draft("job", "source.mp4", "source.mp4")
    folder = config.data_dir / "jobs" / draft.id / "render"
    folder.mkdir(parents=True)
    output = folder / "edited.mp4"
    output.write_bytes(b"video")
    info = SimpleNamespace(duration=1, width=320, height=180)
    renderer = SimpleNamespace(
        render=AsyncMock(return_value=RenderResult(output, info, "notice")),
        thumbnail=AsyncMock(return_value=folder / "thumb.jpg"),
    )
    status = SimpleNamespace(edit=AsyncMock())
    client = SimpleNamespace(send_message=AsyncMock(return_value=status), send_file=AsyncMock())
    refresh = AsyncMock()
    await JobService(client, config, store, renderer).run(draft, False, refresh)
    assert client.send_file.call_args.kwargs["force_document"] is True
    assert not output.exists()
    assert store.history()[0]["status"] == "done"
    refresh.assert_awaited_once()


async def test_delivery_failure_keeps_validated_output_for_retry(config, store):
    draft = Draft("job", "source.mp4", "source.mp4")
    store.save_draft(draft)
    folder = config.data_dir / "jobs" / draft.id / "render"
    folder.mkdir(parents=True)
    output = folder / "edited.mp4"
    output.write_bytes(b"video")
    info = SimpleNamespace(duration=1, width=320, height=180)
    renderer = SimpleNamespace(
        render=AsyncMock(return_value=RenderResult(output, info, "notice")),
        thumbnail=AsyncMock(return_value=folder / "thumb.jpg"),
    )
    client = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        send_file=AsyncMock(side_effect=RuntimeError("delivery failed")),
    )
    await JobService(client, config, store, renderer).run(draft, False, AsyncMock())
    assert store.draft().id == "job"
    assert store.history()[0]["status"] == "failed"
    assert output.exists()
    assert (folder / "pending.json").exists()


@pytest.mark.parametrize("preview", [False, True])
async def test_mkv_audio_fallback_is_sent_as_original_file_even_in_preview(config, store, preview):
    draft = Draft("sound", "source.mp4", "source.mp4")
    draft.settings.quality = Quality.HIGH
    draft.settings.delivery = Delivery.VIDEO
    folder = config.data_dir / "jobs" / draft.id / ("preview" if preview else "render")
    folder.mkdir(parents=True)
    output = folder / "edited.mkv"
    output.write_bytes(b"mkv")
    info = SimpleNamespace(duration=1, width=160, height=96)
    notice = "صدا بدون فشرده‌سازی با اتلاف ذخیره شد."
    renderer = SimpleNamespace(
        render=AsyncMock(return_value=RenderResult(output, info, notice)),
        thumbnail=AsyncMock(return_value=folder / "thumb.jpg"),
    )
    client = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        send_file=AsyncMock(),
    )
    await JobService(client, config, store, renderer).run(draft, preview, AsyncMock())
    kwargs = client.send_file.call_args.kwargs
    assert kwargs["force_document"] is True
    assert kwargs["supports_streaming"] is False
    assert kwargs["attributes"] == []
    assert notice in kwargs["caption"]
    assert not output.exists()
