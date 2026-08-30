"""Continuous, plot-driven streaming loop.

No vote windows, no pauses: danmaku is continuously summarized, the Story engine
turns that signal into the next *continuation* prompt, clips are generated AHEAD
into a ready-buffer, and the player streams them back-to-back so the room never
sees a gap. The audience steers the plot, not just the next clip.

Offline (max_clips set): generate N clips, then the caller renders one continuous
mp4. Live (max_clips=None): run forever, feeding the Streamer for RTMP push.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from .config import Config
from .events import EventHub, summarize
from .journal import Journal
from .models import Clip, RoomEvent
from .story import Story
from .streamer import Streamer
from .video_client import VideoClient

DEFAULT_SIGNAL = {
    "top_keywords": [], "superchats": [], "danmaku_count": 0, "votes": [],
    "total_superchat_amt": 0, "total_gift_amt": 0, "lines": [],
}


async def run_continuous(
    cfg: Config,
    client: VideoClient,
    hub: EventHub,
    streamer: Streamer,
    source=None,                    # optional DanmakuSource
    max_clips: int | None = None,   # None = run forever
    prompt_refresh_s: float = 6.0,
    buffer_target: int | None = None,  # live: keep N clips ready (default cfg.buffer_target)
    signal_window_s: float | None = None,  # audience signal decay (default cfg)
) -> dict:
    cfg.ensure_dirs()
    if buffer_target is None:
        buffer_target = cfg.buffer_target
    if signal_window_s is None:
        signal_window_s = cfg.signal_window_s
    src_task: asyncio.Task | None = asyncio.create_task(source.run()) if source else None
    if src_task is not None:
        def _src_died(t: asyncio.Task) -> None:
            # fire-and-forget tasks swallow their exception until GC, so a dead danmaku
            # reader looked EXACTLY like a quiet room. Say so the moment it happens.
            if t.cancelled():
                return
            e = t.exception()
            print(f"[danmaku] *** READER TASK ENDED *** "
                  + (f"{type(e).__name__}: {e}" if e else "returned with no error"))
        src_task.add_done_callback(_src_died)

    story = Story(cfg.work_dir)
    recent: list[RoomEvent] = []
    signal: dict = dict(DEFAULT_SIGNAL)
    # ONE prompt per clip. The director fills this; the producer drains it. Bounded at 1
    # so `put` blocks -- that back-pressure paces the director to the producer instead of
    # a wall-clock timer, which is what stops beats being computed and thrown away.
    # Carries (prompt, beat_info): the story state is captured AT DIRECTOR TIME because
    # the pipeline runs 1-2 beats ahead, so by the time a clip airs `story.plot` has
    # already moved on -- reading it at push time would mislabel every frame.
    prompts: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=1)
    ready: asyncio.Queue[Clip] = asyncio.Queue()
    report: dict = {"prompts": [], "latencies": [], "cost": 0.0, "clips": 0}
    journal = Journal(cfg.journal_path)

    def _fresh(evs: list[RoomEvent]) -> list[RoomEvent]:
        """Keep only events from the last `signal_window_s`.

        The old code trimmed `recent` by COUNT (at 400 events) and never by time, so in a
        quiet room a single danmaku never aged out: one "check" stayed in `top_keywords`
        and rode along in 50 consecutive beats' context. A live room needs the audience
        signal to DECAY, or the story keeps reacting to a comment from ten minutes ago."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=signal_window_s)
        out = []
        for e in evs:
            try:
                if datetime.fromisoformat(e.ts) >= cutoff:
                    out.append(e)
            except Exception:
                out.append(e)   # unparseable ts -> keep rather than silently drop
        return out

    async def collect() -> None:
        while True:
            ev = await hub.queue.get()
            recent.append(ev)
            if len(recent) > 400:
                del recent[:200]

    async def summarize_loop() -> None:
        nonlocal signal, recent
        while True:
            await asyncio.sleep(prompt_refresh_s)
            recent[:] = _fresh(recent)
            signal = summarize(recent)

    async def director_loop() -> None:
        """Produce EXACTLY ONE beat per clip, pipelined one ahead.

        The old design free-ran on a timer and the producer sampled whatever was latest.
        Two things went wrong with that: consecutive clips could re-read the same prompt
        (identical beats on air), and -- worse -- the director's ~3.3s cycle outran the
        ~5s clip, so about one beat in three advanced `plot`, was saved, and was
        never filmed. The story silently ran ahead of the picture.

        Here `put` blocks on a maxsize-1 queue, so the director computes the next beat
        only when the producer has taken the previous one: every beat computed is a beat
        aired, in order, and the producer never waits on the LLM (it refills in ~1.3s
        against a ~4.5s generation). The establishing shot needs no special case -- it is
        simply the first thing `continuation_prompt` returns."""
        while True:
            try:
                prompt = await story.continuation_prompt(cfg, signal, use_llm=True)
            except Exception as e:
                # continuation_prompt already falls back to a rule beat on LLM failure;
                # this catches anything harder so the queue never starves.
                print(f"[director] beat failed ({type(e).__name__}); using rule beat")
                try:
                    prompt = await story.continuation_prompt(cfg, signal, use_llm=False)
                except Exception:
                    await asyncio.sleep(1.0)
                    continue
            info = {
                "action": getattr(story, "last_action", ""),
                "plot": getattr(story, "plot", ""),
                "chars": sorted(getattr(story, "last_chars", [])),
                "danmaku": list(getattr(story, "last_texts", [])),
                "mood": getattr(story, "mood", ""),
                "setting": getattr(story, "setting", ""),
                "display_text": getattr(story, "display_text", ""),
                "subtitle": getattr(story, "subtitle", ""),
            }
            await prompts.put((prompt, info))

    async def producer() -> None:
        i = 0
        while max_clips is None or i < max_clips:
            # live: only generate when the buffer has room (keeps it topped up)
            if max_clips is None:
                while ready.qsize() >= buffer_target:
                    await asyncio.sleep(0.2)
            # one beat, consumed exactly once -- see director_loop
            prompt, info = await prompts.get()
            dest = cfg.work_dir / "ready" / f"clip_{i:04d}.mp4"
            t0 = time.monotonic()
            # TITLE-CARD BEAT: the director judged this beat better told than shown (a
            # time skip, the gist of a conversation we cannot hear). No video is
            # generated -- which costs nothing and takes ~0.3s instead of ~4.8s, so text
            # beats actively REFILL the buffer rather than draining it.
            text = (info.get("display_text") or "").strip()
            # A title card is needed whenever there is NO audio to carry the beat --
            # a silent film MUST explain environmental sound / narrator / time / place
            # in text. This is independent of the SILENT_FILM B&W grade flag (the card
            # is about SILENCE, not about monochrome), so never gate it on that flag.
            if text:
                try:
                    card = await asyncio.to_thread(
                        streamer.intertitle, text, streamer.card_seconds(text),
                        dest.with_name(f"card_{i:04d}.mp4"))
                    clip = Clip(path=card, prompt=text, provider="intertitle",
                                duration_seconds=streamer.card_seconds(text),
                                metadata={"cost": 0.0, "title_card": True})
                    lat = time.monotonic() - t0
                    clip.metadata["beat"] = i
                    clip.metadata["info"] = info
                    print(f"[producer] clip {i}: TITLE CARD ({lat:.1f}s, $0) -> {text[:60]}")
                    await ready.put(clip)
                    i += 1
                    continue
                except Exception as e:
                    print(f"[producer] title card {i} failed ({type(e).__name__}); "
                          f"falling back to video")
            try:
                clip = await client.generate(prompt, cfg.clip_seconds, dest)
            except Exception as e:
                # ALWAYS include the type: httpx transport errors (ReadTimeout,
                # RemoteProtocolError) stringify to "", so `{e}` alone printed
                # "clip 8 failed: ;" and told us nothing about the cause.
                print(f"[producer] clip {i} failed: {type(e).__name__}: {e}; skipping to next")
                i += 1
                continue
            lat = time.monotonic() - t0
            report["prompts"].append(prompt)
            report["latencies"].append(round(lat, 2))
            report["cost"] += clip.metadata.get("cost") or 0.0
            report["clips"] = i + 1
            # journal: the text conversation behind this clip (audience -> director -> prompt)
            # use `info` (captured at director time), NOT story.* -- the pipeline runs
            # 1-2 beats ahead, so reading story.* here labelled each clip with a LATER
            # beat's action/danmaku than the one actually filmed.
            journal.record(
                beat=i,
                action=info["action"],
                characters=info["chars"],
                danmaku=info["danmaku"],
                mood=info["mood"],
                setting=info["setting"],
                prompt=prompt,
                clip=clip.path.name,
                provider=getattr(clip, "provider", cfg.video_provider),
                cost_usd=round(clip.metadata.get("cost") or 0.0, 6),
                runtime_ms=clip.metadata.get("runtime_ms"),
                latency_s=round(lat, 2),
            )
            # carry the beat with the clip so the pusher can burn the debug overlay
            clip.metadata["beat"] = i
            clip.metadata["info"] = info
            print(f"[producer] clip {i}: {lat:.1f}s -> {clip.path.name} (buf {ready.qsize()})")
            await ready.put(clip)
            i += 1

    async def player() -> None:
        n = 0
        while True:
            clip = await ready.get()
            streamer.add_clip(clip)
            n += 1
            if max_clips is not None and n >= max_clips:
                break

    async def live_pusher() -> None:
        """LIVE path: ONE long-lived RTMP publish for the whole broadcast.

        Clips are fed in as raw H.264 Annex-B, so the receiver sees a single
        uninterrupted stream instead of one publish per clip -- which is what used to
        make bilibili show its own "loading" at every boundary. `feed_segment` blocks
        at realtime pace, so this loop is self-clocking: when a clip isn't ready yet we
        feed a 1s filler and check again, keeping the pipe fed without running ahead.

        The publisher opens IMMEDIATELY, before any clip exists. We tried deferring it
        until clip 0 was ready ("open on real video, never on filler") and it backfired:
        livehime is typically already broadcasting, so a deferred open means it receives
        NO stream during generation -- and the room falls back to bilibili's own loading
        screen, the exact failure this design exists to prevent. Holding the connection
        open and feeding our standby card keeps the gap ours."""
        streamer.open_publisher()
        while True:
            try:
                clip = ready.get_nowait()
            except asyncio.QueueEmpty:
                # Silent-film answer to a dry buffer: an INTERTITLE, which is native to
                # the form -- so a generation gap reads as the show rather than a fault,
                # and the director's narrator line carries the story while we wait.
                try:
                    card = getattr(story, "card", "")
                    # Title cards are ALWAYS needed (there is no audio), so the card
                    # fires whenever the director has a narrator line -- regardless of
                    # whether the silent-film B&W grade is on.
                    if card:
                        seg = await asyncio.to_thread(streamer.intertitle, card)
                        await asyncio.to_thread(streamer.feed_segment, seg)
                    else:
                        await asyncio.to_thread(streamer.feed_idle)
                except Exception as e:
                    print(f"[push] gap filler failed: {type(e).__name__}: {e}")
                    await asyncio.sleep(1.0)
                continue
            try:
                info = clip.metadata.get("info") or {}
                overlay = streamer.debug_overlay(clip.metadata.get("beat"), info)
                # A character's spoken line, burned as a subtitle over the shot. May be
                # absent for a card or a pure-action beat. Only the LIVE push carries
                # it (the offline render path concatenates pre-encoded clips).
                subtitle = (info.get("subtitle") or "").strip() or None
                await asyncio.to_thread(
                    streamer.feed_segment, clip.path, overlay, subtitle)
                print(f"[push] on air -> {clip.path.name} (buf {ready.qsize()})")
            except Exception as e:
                # reconnect already failed inside feed_segment; back off so a dead
                # target (livehime not listening) doesn't spin this loop hot
                print(f"[push] clip feed failed: {type(e).__name__}: {e}")
                await asyncio.sleep(2.0)

    async def per_clip_pusher() -> None:
        """Fallback (CONTINUOUS_PUSH=0): one direct publish per clip. Reliable, but the
        receiver sees the stream end between clips and shows its loading screen."""
        gap_s = max(6.0, cfg.clip_seconds + 3.0)
        try:
            await asyncio.to_thread(streamer.push_idle_live)
            print("[push] lead-in error screen (instant video)")
        except Exception as e:
            print(f"[push] lead-in failed: {type(e).__name__}: {e}")
        while True:
            try:
                clip = await asyncio.wait_for(ready.get(), timeout=gap_s)
            except asyncio.TimeoutError:
                try:
                    await asyncio.to_thread(streamer.push_idle_live)
                    print("[push] no clip -> error screen (feed held)")
                except Exception as e:
                    print(f"[push] idle push failed: {type(e).__name__}: {e}")
                continue
            try:
                await asyncio.to_thread(streamer.push_clip_live, clip)
                print(f"[push] live -> {clip.path.name}")
            except Exception as e:
                print(f"[push] clip push failed: {type(e).__name__}: {e}")

    collect_task = asyncio.create_task(collect())
    summarize_task = asyncio.create_task(summarize_loop())
    director_task = asyncio.create_task(director_loop())
    producer_task = asyncio.create_task(producer())
    # offline: player accumulates clips for render; live: pusher streams to RTMP
    if max_clips is not None:
        consumer = player()
    else:
        consumer = live_pusher() if cfg.continuous_push else per_clip_pusher()
    consumer_task = asyncio.create_task(consumer)

    try:
        # offline: producer+consumer finish at max_clips; live: run until cancelled
        await asyncio.gather(producer_task, consumer_task, return_exceptions=True)
    finally:
        for t in (collect_task, summarize_task, director_task):
            t.cancel()
        if src_task is not None:
            src_task.cancel()
        streamer.close_publisher()

    report["total_cost_usd"] = round(report["cost"], 4)
    return report
