"""Simulated audience for offline demos: emits danmaku, a superchat and votes."""
from __future__ import annotations

import asyncio
import random

from ..config import Config
from ..events import EventHub
from ..models import Danmaku, Gift, Superchat, RoomEvent
from .base import DanmakuSource


class MockDanmakuSource(DanmakuSource):
    name = "mock"

    TOPICS = ["kitten", "cyberpunk", "space", "island", "samurai", "garden"]
    START_DANMAKU = [
        "hello\n", "what is this\n", "so cool\n", "more\n", "cat\n", "privacy\n",
    ]

    async def run(self) -> None:
        rng = random.Random()
        while True:
            # a short burst of chat, then a vote prompt every beat
            for _ in range(rng.randint(2, 5)):
                topic = rng.choice(self.TOPICS)
                await self.hub.emit(Danmaku(user=f"u{rng.randint(1, 500)}",
                                            text=f"make it {topic}!"))
                await asyncio.sleep(0.15)

            # present a vote
            choices = rng.sample(self.TOPICS, 2)
            await self.hub.emit(Danmaku(user="VOTER", text=f"1:{choices[0]} 2:{choices[1]}"))
            await asyncio.sleep(0.4)
            await self.hub.emit(RoomEvent(kind="vote",
                                          user=f"u{rng.randint(1, 500)}",
                                          text=choices[rng.randint(0, 1)]))

            # occasionally a gift / superchat to steer the next beat
            roll = rng.random()
            if roll < 0.35:
                await self.hub.emit(Gift(user=f"u{rng.randint(1, 999)}",
                                         amount=rng.choice([1, 5, 30]), text="礼物"))
            if roll < 0.18:
                await self.hub.emit(Superchat(user="老板", amount=rng.choice([30, 100, 500]),
                                              text="给猫猫加个披风！"))

            await asyncio.sleep(self.cfg.beat_seconds * 0.4)
