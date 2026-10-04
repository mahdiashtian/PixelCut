"""Serializable editing models, independent of Telegram and FFmpeg."""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any


class Position(StrEnum):
    TOP_LEFT = "tl"
    TOP_CENTER = "tc"
    TOP_RIGHT = "tr"
    CENTER_LEFT = "cl"
    CENTER = "cc"
    CENTER_RIGHT = "cr"
    BOTTOM_LEFT = "bl"
    BOTTOM_CENTER = "bc"
    BOTTOM_RIGHT = "br"


class Color(StrEnum):
    WHITE = "white"
    BLACK = "black"
    DYNAMIC = "dynamic"


class Transition(StrEnum):
    CUT = "cut"
    FADE = "fade"
    SHADOW = "fadeblack"
    DISSOLVE = "dissolve"
    SLIDE = "smoothleft"
    CIRCLE = "circleopen"
    PIXEL = "pixelize"


class Quality(StrEnum):
    HIGH = "high"
    LOSSLESS = "lossless"
    FAST = "fast"


class Delivery(StrEnum):
    FILE = "file"
    VIDEO = "video"


def number(value: Any, low: float, high: float, label: str) -> float:
    if isinstance(value, str):
        value = value.strip().removesuffix("٪").removesuffix("%").replace("٫", ".")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: عدد معتبر وارد کنید.") from exc
    if not isfinite(result) or not low <= result <= high:
        raise ValueError(f"{label}: عدد باید بین {low:g} و {high:g} باشد.")
    return result


@dataclass
class Style:
    # Width of the entire text/logo as a percentage of the displayed video width.
    width_percent: float = 20
    position: Position = Position.CENTER
    color: Color = Color.DYNAMIC
    opacity: float = 100
    margin_percent: float = 2
    font_id: str | None = None

    def validate(self) -> None:
        self.width_percent = number(self.width_percent, 1, 95, "اندازه")
        self.opacity = number(self.opacity, 1, 100, "شفافیت")
        self.margin_percent = number(self.margin_percent, 0, 20, "حاشیه")
        self.position = Position(self.position)
        self.color = Color(self.color)

    @classmethod
    def from_dict(cls, data: dict) -> "Style":
        obj = cls(**data)
        obj.validate()
        return obj


@dataclass
class Join:
    kind: Transition = Transition.CUT
    seconds: float = 0.6

    def validate(self) -> None:
        self.kind = Transition(self.kind)
        self.seconds = number(self.seconds, 0.1, 3, "زمان ترنزیشن")

    @classmethod
    def from_dict(cls, data: dict) -> "Join":
        obj = cls(**data)
        obj.validate()
        return obj


@dataclass
class Settings:
    text_style: Style = field(default_factory=Style)
    logo_style: Style = field(
        default_factory=lambda: Style(width_percent=12, position=Position.TOP_RIGHT)
    )
    intro_join: Join = field(default_factory=Join)
    outro_join: Join = field(default_factory=Join)
    quality: Quality = Quality.LOSSLESS
    delivery: Delivery = Delivery.FILE

    def validate(self) -> None:
        self.text_style.validate()
        self.logo_style.validate()
        self.intro_join.validate()
        self.outro_join.validate()
        self.quality = Quality(self.quality)
        self.delivery = Delivery(self.delivery)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        obj = cls(
            text_style=Style.from_dict(data["text_style"]),
            logo_style=Style.from_dict(data["logo_style"]),
            intro_join=Join.from_dict(data["intro_join"]),
            outro_join=Join.from_dict(data["outro_join"]),
            quality=Quality(data["quality"]),
            delivery=Delivery(data["delivery"]),
        )
        obj.validate()
        return obj


@dataclass
class Draft:
    id: str
    source: str
    original_name: str
    settings: Settings = field(default_factory=Settings)
    text: str = ""
    logo_id: str | None = None
    intro_id: str | None = None
    outro_id: str | None = None
    trim_start: float = 0
    trim_end: float | None = None
    mute: bool = False

    def validate(self) -> None:
        self.settings.validate()
        if len(self.text) > 200 or "\n" in self.text or "\r" in self.text:
            raise ValueError("واترمارک باید یک خط و حداکثر ۲۰۰ کاراکتر باشد.")
        self.trim_start = number(self.trim_start, 0, 86400, "شروع برش")
        if self.trim_end is not None:
            self.trim_end = number(self.trim_end, 0, 86400, "پایان برش")
            if self.trim_end <= self.trim_start:
                raise ValueError("پایان برش باید بعد از شروع باشد.")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Draft":
        data = dict(data)
        data["settings"] = Settings.from_dict(data["settings"])
        obj = cls(**data)
        obj.validate()
        return obj


@dataclass(frozen=True)
class Asset:
    id: str
    kind: str
    name: str
    path: str
