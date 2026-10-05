"""Large exports retain every byte, with bounded upload reads and restartable delivery."""

import asyncio
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.helpers import _FileStream

from movie_editor.domain import Draft, Quality
from movie_editor.jobs import JobService
from movie_editor.media.limits import check_output
from movie_editor.media.renderer import Renderer
from movie_editor.telegram.delivery import FileDelivery, FileSlice
from movie_editor.telegram.reassemble import restore

from .test_media import clip


class ReceivingClient:
    """Read upload streams as Telethon does, saving received files for recovery tests."""

    def __init__(self, folder):
        self.folder = folder
        self.files = []
        self.send_message = AsyncMock()

    async def send_file(self, chat, file, **kwargs):
        async with _FileStream(file, file_size=kwargs.get("file_size")) as stream:
            path = self.folder / Path(stream.name).name
            with path.open("wb") as target:
                while block := stream.read(64 * 1024):
                    target.write(block)
                    if callback := kwargs.get("progress_callback"):
                        await callback(target.tell(), stream.file_size)
        self.files.append((path, kwargs))


async def test_large_export_recovers_identical_bytes_with_monotonic_progress(config, tmp_path):
    folder = tmp_path / "received"
    folder.mkdir()
    output = tmp_path / "edited.mkv"
    data = bytes(range(256)) * 3000 + b"end"
    output.write_bytes(data)
    client = ReceivingClient(folder)
    config = replace(config, max_upload_mb=0.25)
    progress = AsyncMock()
    multipart = await FileDelivery(client, config).send(
        output, progress=progress, caption="lossless", force_document=True
    )
    assert multipart
    parts = [path for path, _ in client.files if ".part" in path.name]
    assert len(parts) == 3 and all(path.stat().st_size <= 256 * 1024 for path in parts)
    assert not any(tmp_path.glob("*.part*"))  # No additional large files on the server.
    positions = [call.args[0] for call in progress.call_args_list]
    assert positions == sorted(positions) and positions[-1] == len(data)
    manifest = next(folder.glob("*.manifest.json"))
    recovered = restore(manifest)
    assert recovered.read_bytes() == data
    assert (
        hashlib.sha256(recovered.read_bytes()).hexdigest()
        == json.loads(manifest.read_text())["sha256"]
    )
    assert (folder / "restore.py").exists()


async def test_file_at_size_limit_is_sent_once_without_parts(config, tmp_path):
    output = tmp_path / "edited.mp4"
    output.write_bytes(b"x" * 1024)
    client = SimpleNamespace(send_file=AsyncMock(), send_message=AsyncMock())
    config = replace(config, max_upload_mb=1024 / 1024**2)
    assert not await FileDelivery(client, config).send(
        output, progress=AsyncMock(), caption="original", force_document=True
    )
    client.send_file.assert_awaited_once()
    assert client.send_file.call_args.args == (123, str(output))
    client.send_message.assert_not_awaited()


async def test_slice_reports_only_its_bytes_to_telethon(tmp_path):
    path = tmp_path / "data"
    path.write_bytes(b"0123456789")
    with FileSlice(path, 3, 4, "export.part0001") as part:
        async with _FileStream(part) as stream:
            assert stream.file_size == 4
            assert stream.read(512 * 1024) == b"3456"
            assert stream.read(512 * 1024) == b""
        assert part.seek(-2, io.SEEK_END) == 2
        assert part.read() == b"56"
        with pytest.raises(ValueError):
            part.seek(-1)
    assert part.closed


@pytest.mark.parametrize("failure", ["missing", "corrupt", "checksum"])
async def test_recovery_rejects_bad_or_missing_parts(config, tmp_path, failure):
    folder = tmp_path / "received"
    folder.mkdir()
    output = tmp_path / "edited.mkv"
    output.write_bytes(b"original-media" * 2000)
    client = ReceivingClient(folder)
    await FileDelivery(client, replace(config, max_upload_mb=0.01)).send(
        output, progress=AsyncMock(), caption="original"
    )
    manifest_path = next(folder.glob("*.manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    part = folder / manifest["parts"][0]["name"]
    if failure == "missing":
        part.unlink()
    elif failure == "corrupt":
        part.write_bytes(b"x" * part.stat().st_size)
    else:
        manifest["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        restore(manifest_path)
    assert not (folder / manifest["name"]).exists()


def test_render_size_limit_is_independent_of_single_upload(config, tmp_path):
    output = tmp_path / "large.mkv"
    output.write_bytes(b"x" * 2048)
    config = replace(config, max_upload_mb=0.001, max_render_mb=1)
    with pytest.raises(ValueError, match="حجم"):
        check_output(config, output)
    check_output(config, output, multipart=True)
    with pytest.raises(ValueError, match="رندر"):
        check_output(replace(config, max_render_mb=0.001), output, multipart=True)


@pytest.mark.integration
async def test_failed_upload_retries_after_restart_without_rendering_again(config, store, tmp_path):
    source = await clip(config, tmp_path / "source.mp4", duration=0.5)
    draft = Draft("retry", str(source), source.name, text="FIRST")
    store.save_draft(draft)
    status = SimpleNamespace(edit=AsyncMock())
    client = SimpleNamespace(
        send_message=AsyncMock(return_value=status),
        send_file=AsyncMock(side_effect=ConnectionError("offline")),
    )
    renderer = Renderer(config, store)
    renderer.render = AsyncMock(wraps=renderer.render)
    service = JobService(client, config, store, renderer)
    await service.run(draft, False, AsyncMock())
    folder = config.data_dir / "jobs" / draft.id / "render"
    output = folder / "edited.mkv"
    before = output.read_bytes()
    assert service.pending(draft)
    assert "ConnectionError" in status.edit.call_args.args[0]
    assert status.edit.call_args.kwargs["buttons"]
    draft.text = "LATER"
    draft.settings.quality = Quality.HIGH
    store.save_draft(draft)
    new_renderer = SimpleNamespace(render=AsyncMock(), thumbnail=renderer.thumbnail)
    sent = []

    async def receive(chat, path, **kwargs):
        sent.append((Path(path).read_bytes(), kwargs))

    client.send_file = receive
    restarted = JobService(client, config, store, new_renderer)
    await restarted.run(draft, False, AsyncMock(), retry=True)
    assert sent[0][0] == before and sent[0][1]["force_document"]
    assert "CRF 0" in sent[0][1]["caption"]
    new_renderer.render.assert_not_awaited()
    renderer.render.assert_awaited_once()
    assert not output.exists() and not restarted.pending(draft)
    assert store.draft().text == "LATER" and store.draft().settings.quality == Quality.HIGH


async def test_cancelled_upload_preserves_ready_video(config, store, tmp_path):
    source = await clip(config, tmp_path / "source.mp4", duration=0.5)
    draft = Draft("cancel-send", str(source), source.name)
    client = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        send_file=AsyncMock(side_effect=asyncio.CancelledError),
    )
    service = JobService(client, config, store, Renderer(config, store))
    with pytest.raises(asyncio.CancelledError):
        await service.run(draft, False, AsyncMock())
    assert service.pending(draft)


async def test_thumbnail_failure_does_not_block_original_video(config, store, tmp_path):
    source = await clip(config, tmp_path / "source.mp4", duration=0.5)
    draft = Draft("thumbnail", str(source), source.name)
    renderer = Renderer(config, store)
    renderer.thumbnail = AsyncMock(side_effect=ValueError("thumbnail unavailable"))
    client = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        send_file=AsyncMock(),
    )
    await JobService(client, config, store, renderer).run(draft, False, AsyncMock())
    assert client.send_file.call_args.kwargs["thumb"] is None
    assert store.history()[0]["status"] == "done"


@pytest.mark.integration
async def test_video_larger_than_upload_limit_is_rendered_then_delivered_exactly(
    config, store, tmp_path
):
    source = await clip(config, tmp_path / "source.mp4", duration=1)
    draft = Draft("large", str(source), source.name, text="WATERMARK")
    config = replace(config, max_upload_mb=0.01)
    result = await Renderer(config, store).render(draft, tmp_path / "render")
    before = result.path.read_bytes()
    assert len(before) > config.max_upload_mb * 1024**2
    received = tmp_path / "received"
    received.mkdir()
    await FileDelivery(ReceivingClient(received), config).send(
        result.path, progress=AsyncMock(), caption=result.notice
    )
    recovered = restore(next(received.glob("*.manifest.json")))
    assert recovered.read_bytes() == before
