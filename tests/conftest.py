import shutil
from pathlib import Path

import pytest

from movie_editor.config import Config
from movie_editor.storage import Store


@pytest.fixture
def config(tmp_path):
    root = Path(__file__).resolve().parents[1]
    binaries = list((root / ".tools").glob("*/bin/ffmpeg.exe"))
    ffmpeg = shutil.which("ffmpeg") or (str(binaries[0]) if binaries else None)
    ffprobe = shutil.which("ffprobe") or (
        str(binaries[0].with_name("ffprobe.exe")) if binaries else None
    )
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and FFprobe are required for media tests")
    config = Config(
        1,
        "test",
        "test",
        123,
        tmp_path / "data",
        ffmpeg,
        ffprobe,
        min_free_disk_mb=1,
        max_render_seconds=30,
        threads=2,
    )
    config.prepare()
    return config


@pytest.fixture
def store(tmp_path):
    repository = Store(tmp_path / "test.sqlite3")
    yield repository
    repository.close()
