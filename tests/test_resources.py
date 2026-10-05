"""A resource failure stops the child, reports its cause, and leaves the bot alive."""

import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from movie_editor.media import process
from movie_editor.media.limits import check_resources
from movie_editor.media.probe import inspect
from movie_editor.media.resources import resident_bytes


@pytest.mark.parametrize(
    "code,stderr,phrase",
    [
        (-9, "", "SIGKILL"),
        (137, "", "SIGKILL"),
        (1, "Cannot allocate memory", "RAM"),
        (1, "No space left on device", "دیسک"),
        (1, "Invalid data found", "کد خروج"),
    ],
)
def test_process_error_identifies_failure(config, tmp_path, code, stderr, phrase):
    log = tmp_path / "ffmpeg.log"
    log.write_text(stderr)
    assert phrase in process.failure_message(log, code)


async def test_memory_guard_stops_the_child_and_records_metrics(tmp_path, monkeypatch):
    children = []
    create = process.asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        child = await create(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(process.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(process, "resident_bytes", lambda pid: 2 * 1024**2)
    log = tmp_path / "ffmpeg.log"
    with pytest.raises(ValueError, match="RAM"):
        await process.encode(
            [sys.executable, "-c", "import time; time.sleep(60)"], log, 1800, 5, None, memory_mb=1
        )
    assert children[0].returncode is not None
    metrics = json.loads(log.with_suffix(".metrics.json").read_text())
    assert metrics["peak_rss_bytes"] == 2 * 1024**2
    assert metrics["memory_limit_mb"] == 1


async def test_output_guard_runs_even_without_progress(config, tmp_path):
    output = tmp_path / "large.mkv"
    script = (
        "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_bytes(b'x'*4096); time.sleep(60)"
    )
    config = replace(config, max_render_mb=0.001)
    with pytest.raises(ValueError, match="حجم"):
        await process.encode(
            [sys.executable, "-c", script, str(output)],
            tmp_path / "ffmpeg.log",
            1800,
            5,
            None,
            watchdog=lambda: check_resources(config, output, multipart=True),
        )


def test_resident_memory_can_be_read_on_linux_and_windows():
    if sys.platform.startswith("linux") or sys.platform == "win32":
        assert resident_bytes(os.getpid()) > 0


async def test_frame_count_uses_configured_budget_instead_of_fixed_five_minutes(
    config, monkeypatch
):
    metadata = {
        "streams": [
            {
                "codec_type": "video",
                "index": 0,
                "width": 320,
                "height": 180,
                "duration": "1800",
                "pix_fmt": "yuv420p",
                "nb_read_frames": "54000",
                "avg_frame_rate": "30/1",
                "time_base": "1/90000",
            }
        ]
    }
    capture = AsyncMock(return_value=json.dumps(metadata).encode())
    monkeypatch.setattr("movie_editor.media.probe.capture", capture)
    config = replace(config, max_render_seconds=3600)
    info = await inspect(Path("long.mp4"), config, count_frames=True)
    assert info.frames == 54000
    assert capture.call_args.kwargs["timeout"] == 3600
    args = capture.call_args.args[0]
    assert args[args.index("-threads") + 1] == "2"
