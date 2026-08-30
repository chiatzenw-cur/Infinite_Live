"""The heartbeat: generate beat N+1 from beat N's audience signal.

Generation runs CONCURRENTLY with the beat's own playback/vote window, so a clip
that finishes well under the 30s budget (2 x beat) is ready at the boundary; an
interstitial loops to cover any overrun. Latency is measured and reported.
"""
from __future__ import annotations

import asyncio
import time

from .config import Config
from .events import EventHub
from .models import Clip, RoomEvent
from .prompt_builder import build_prompt
from .streamer import Streamer
from .video_client import VideoClient

DEFAULT_PROMPT = ("a serene animated scene, cinematic, vibrant colors, "
                  "one long slow dolly move, no cuts, no text on screen")


async def _collect_events(hub: EventHub, seconds: float) -> list[RoomEvent]:
    await asyncio.sleep(seconds)
    return await hub.drain()


async def run_loop(
    cfg: Config,
    client: VideoClient,
    hub: EventHub,
    streamer: Streamer,
    n_beats: int | None = None,
    source=None,                       # optional DanmakuSource to start
    collect_seconds: float | None = None,
) -> dict:
    cfg.ensure_dirs()
    window_s = collect_seconds if collect_seconds is not None else max(1.0, cfg.beat_seconds * 0.7)

    src_task: asyncio.Task | None = None
    if source is not None:
        src_task = asyncio.create_task(source.run())

    report: dict = {"beats": [], "latencies": [], "prompts": []}
    clips: dict[int, Clip] = {}

    async def gen(i: int, prompt: str) -> Clip:
        dest = cfg.work_dir / "ready" / f"beat_{i:04d}.mp4"
        clip = await client.generate(prompt, cfg.beat_seconds, dest)
        clips[i] = clip
        streamer.add_clip(clip)
        return clip

    # seed beat 0
    await gen(0, DEFAULT_PROMPT)
    prev_window: list[RoomEvent] = []
    total = n_beats if n_beats is not None else -1  # -1 = run forever

    i = 1
    while total < 0 or i < total:
        prompt = await build_prompt(cfg, prev_window) if prev_window else DEFAULT_PROMPT
        t0 = time.monotonic()
        gen_task = asyncio.create_task(gen(i, prompt))

        # this beat's interaction window, collected while beat i generates
        prev_window = await _collect_events(hub, window_s)

        await gen_task
        latency = time.monotonic() - t0
        budget = 2.0 * cfg.beat_seconds
        report["beats"].append(i)
        report["prompts"].append(prompt)
        report["latencies"].append(round(latency, 2))
        print(f"[beat {i}] prompt: {prompt[:70]!r}  gen={latency:.1f}s "
              f"(budget {budget:.0f}s {'OK' if latency <= budget else 'OVER'})")
        i += 1

    if src_task is not None:
        src_task.cancel()
    return report
