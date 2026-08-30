"""Core data types: audience events and generated clips."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RoomEvent:
    """Base for anything that happens in the live room."""
    kind: Literal["danmaku", "superchat", "gift", "guard", "vote"]
    user: str = ""
    text: str = ""
    amount: float = 0.0          # 元 (RMB) for superchat / gift value
    ts: str = field(default_factory=_now)


@dataclass
class Danmaku(RoomEvent):
    kind: Literal["danmaku", "superchat", "gift", "guard", "vote"] = "danmaku"


@dataclass
class Superchat(RoomEvent):
    kind: Literal["danmaku", "superchat", "gift", "guard", "vote"] = "superchat"


@dataclass
class Gift(RoomEvent):
    kind: Literal["danmaku", "superchat", "gift", "guard", "vote"] = "gift"


@dataclass
class Guard(RoomEvent):
    kind: Literal["danmaku", "superchat", "gift", "guard", "vote"] = "guard"


@dataclass
class Clip:
    """A generated video segment for one beat."""
    path: Path
    prompt: str
    provider: str
    created_ts: str = field(default_factory=_now)
    duration_seconds: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class Beat:
    """One minute of the stream: an audience-driven beat."""
    index: int
    clip: Clip | None = None
    events: list[RoomEvent] = field(default_factory=list)
    prompt: str = ""
