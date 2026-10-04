"""Result shared by video renders and GIF exports."""

from dataclasses import dataclass
from pathlib import Path

from .probe import MediaInfo


@dataclass(frozen=True)
class RenderResult:
    path: Path
    info: MediaInfo
    notice: str
