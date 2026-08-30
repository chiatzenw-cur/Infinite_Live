"""Danmaku source contract: any thing that emits RoomEvents into an EventHub."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Config
from ..events import EventHub


class DanmakuSource(ABC):
    name = "base"

    def __init__(self, cfg: Config, hub: EventHub):
        self.cfg = cfg
        self.hub = hub

    @abstractmethod
    async def run(self) -> None:
        """Block forever, emitting events into self.hub."""
        ...
