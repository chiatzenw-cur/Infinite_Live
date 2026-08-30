"""In-memory room-event stream and a small aggregate viewer."""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field

from .models import RoomEvent, Superchat


@dataclass
class EventHub:
    """Fan-in for danmaku/superchat/gift from any source; beat producer consumes it."""
    queue: asyncio.Queue[RoomEvent] = field(default_factory=asyncio.Queue)

    async def emit(self, event: RoomEvent) -> None:
        await self.queue.put(event)

    async def drain(self, timeout: float | None = None) -> list[RoomEvent]:
        """Return everything currently queued (non-blocking-ish) up to one item."""
        items: list[RoomEvent] = []
        try:
            while True:
                items.append(self.queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        return items


def summarize(events: list[RoomEvent], top_superchats: int = 3) -> dict:
    """Turn raw room events into a compact signal for the prompt builder."""
    scs: list[Superchat] = [e for e in events if isinstance(e, Superchat)]
    scs.sort(key=lambda e: e.amount, reverse=True)
    top_sc = scs[:top_superchats]

    danmaku_text = [e.text for e in events if e.kind == "danmaku" and e.text.strip()]
    keyword_counts = Counter()
    for t in danmaku_text:
        for token in t.replace("，", " ").replace(",", " ").split():
            token = token.strip("！!？?。~～")
            if token:
                keyword_counts[token] += 1

    votes = [e.text.strip().lower() for e in events if e.kind == "vote"]
    return {
        # RAW sentences, newest last. `top_keywords` shreds "a storm rolls in" into
        # ['a','storm','rolls','in'], destroying the meaning before the director reads
        # it -- and for Chinese danmaku, which has no spaces, a whole comment collapses
        # into one useless token. The LLM director can read sentences; give it sentences.
        "lines": danmaku_text[-8:],
        "superchats": [{"user": s.user, "amount": s.amount, "text": s.text} for s in top_sc],
        "total_superchat_amt": round(sum(s.amount for s in scs), 2),
        "total_gift_amt": round(sum(e.amount for e in events if e.kind == "gift"), 2),
        "danmaku_count": len(danmaku_text),
        "top_keywords": keyword_counts.most_common(5),
        "votes": votes,
    }
