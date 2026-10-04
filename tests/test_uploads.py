import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from movie_editor.assets import AssetService
from movie_editor.domain import Draft
from movie_editor.media.graphics import find_font
from movie_editor.telegram.controller import Controller, Pending
from movie_editor.telegram.uploads import UploadService


def event(name, size=100):
    return SimpleNamespace(
        file=SimpleNamespace(name=name, size=size), photo=None, media=True, message=object()
    )


def client_for(source):
    async def download(message, file, progress_callback):
        Path(file).write_bytes(source)
        await progress_callback(len(source), len(source))
        return file

    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        download_media=AsyncMock(side_effect=download),
    )


async def test_font_upload_is_retained_selected_and_restored(config, store):
    client = client_for(find_font(None).read_bytes())
    controller = Controller(client, config, store, AssetService(config, store), None)
    await controller.upload(event("persian.ttf"), Pending("font", "s"))
    selected = store.defaults().text_style.font_id
    assert selected
    font = store.asset(selected, "font")
    assert Path(font.path).is_file()
    source = config.data_dir / "source.mp4"
    source.write_bytes(b"source")
    controller.draft = Draft("active", str(source), "source.mp4", store.defaults())
    store.save_draft(controller.draft)
    restored = Controller(client, config, store, AssetService(config, store), None)
    assert restored.draft.settings.text_style.font_id == selected
    with pytest.raises(ValueError, match="استفاده"):
        restored.assets.delete(selected, "font", restored.used_assets())


async def test_invalid_font_is_removed_and_not_retained(config, store):
    service = UploadService(client_for(b"not a font"), config, AssetService(config, store))
    with pytest.raises(OSError):
        await service.receive(event("bad.ttf"), "font")
    assert store.assets("font") == []
    assert list((config.data_dir / "assets").iterdir()) == []


async def test_cancelled_download_removes_partial_file(config, store):
    started = asyncio.Event()

    async def download(message, file, progress_callback):
        Path(file).write_bytes(b"partial")
        started.set()
        await asyncio.Event().wait()

    client = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        download_media=AsyncMock(side_effect=download),
    )
    service = UploadService(client, config, AssetService(config, store))
    task = asyncio.create_task(service.receive(event("input.mp4"), "source"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((config.data_dir / "jobs").iterdir()) == []


async def test_upload_size_limit_checked_before_download(config, store):
    client = client_for(b"data")
    service = UploadService(client, config, AssetService(config, store))
    with pytest.raises(ValueError, match="حجم"):
        await service.receive(event("font.ttf", 21 * 1024**2), "font")
    client.download_media.assert_not_awaited()
