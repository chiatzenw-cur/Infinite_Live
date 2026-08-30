"""infinity-live CLI.

mock   -- offline demo: mock danmaku + mock video -> a real local mp4
run    -- full loop with a real/configured provider and danmaku source
stream -- CONTINUOUS, plot-driven live/interactive loop (--clips for offline render)
probe  -- validate a real video API key with a single small generation
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path


def _cfg(**overrides):
    from .config import load_config
    cfg = load_config()
    for k, v in overrides.items():
        if v is not None:
            setattr(cfg, k, v)
    cfg.ensure_dirs()
    return cfg


async def _mock(args) -> None:
    os.environ.setdefault("VIDEO_MOCK_LATENCY", str(args.latency))
    cfg = _cfg(video_provider="mock", bili_danmaku_mode="mock")
    from .video_client import MockVideoClient
    from .events import EventHub
    from .danmaku.mock_source import MockDanmakuSource
    from .streamer import Streamer

    client = MockVideoClient(cfg)
    hub = EventHub()
    source = MockDanmakuSource(cfg, hub)
    streamer = Streamer(cfg)

    from .loop import run_loop
    report = await run_loop(cfg, client, hub, streamer, n_beats=args.beats,
                            source=source, collect_seconds=args.window)
    out = __import__("pathlib").Path(args.out)
    await streamer.render(out)
    print(f"\n[demo] rendered {args.beats} beats -> {out} ({out.stat().st_size/1e6:.1f} MB)")
    print(f"[demo] generation latencies: {report['latencies']}s")


async def _stream(args) -> None:
    """Continuous, plot-driven loop. --clips N renders an offline mp4; without it, push live."""
    cfg = _cfg(
        video_provider=args.provider,
        bili_danmaku_mode=args.danmaku,
        bili_room_id=args.room,
        output_path=args.out,
    )
    from .video_client import VideoClient
    from .events import EventHub
    from .streamer import Streamer

    client = VideoClient.build(cfg)
    hub = EventHub()
    streamer = Streamer(cfg)

    source = None
    if cfg.bili_danmaku_mode == "mock":
        from .danmaku.mock_source import MockDanmakuSource
        source = MockDanmakuSource(cfg, hub)
    elif cfg.bili_danmaku_mode == "selfhosted" and cfg.bili_room_id:
        from .danmaku.bilibili_websocket import BilibiliWebsocketSource
        source = BilibiliWebsocketSource(cfg, hub)

    from .continuous import run_continuous
    print(f"[stream] provider={cfg.video_provider} danmaku={cfg.bili_danmaku_mode} "
          f"clips={args.clips or '∞'}")
    report = await run_continuous(cfg, client, hub, streamer, source=source,
                                  max_clips=args.clips, prompt_refresh_s=args.refresh)

    if args.out:
        out = Path(args.out)
        # between=False -> clips play back-to-back (continuous show, no interstitial pauses)
        await streamer.render(out, between=False)
        print(f"[stream] continuous -> {out} ({out.stat().st_size/1e6:.1f} MB, "
              f"{report['clips']} clips, ${report['total_cost_usd']})")
        print(f"[stream] prompt chain:")
        for p in report["prompts"]:
            print(f"  - {p[:90]}")
        print(f"[stream] per-clip latencies: {report['latencies']}s")
    elif cfg.pushing:
        print("[stream] pushing continuous to RTMP (Ctrl-C to stop)...")
        await streamer.push_loop(between=False)
    else:
        raise SystemExit("set --out <file> (offline) or BILI_PUSH_URL/KEY (live)")


async def _run(args) -> None:
    cfg = _cfg(
        video_provider=args.provider,
        bili_danmaku_mode=args.danmaku,
        bili_room_id=args.room,
        output_path=args.out,
    )
    from .video_client import VideoClient
    from .events import EventHub
    from .streamer import Streamer

    client = VideoClient.build(cfg)
    hub = EventHub()
    streamer = Streamer(cfg)

    source = None
    if cfg.bili_danmaku_mode == "mock":
        from .danmaku.mock_source import MockDanmakuSource
        source = MockDanmakuSource(cfg, hub)
    elif cfg.bili_danmaku_mode == "selfhosted" and cfg.bili_room_id:
        from .danmaku.bilibili_websocket import BilibiliWebsocketSource
        source = BilibiliWebsocketSource(cfg, hub)

    from .loop import run_loop
    if args.beats:
        report = await run_loop(cfg, client, hub, streamer, n_beats=args.beats,
                                source=source, collect_seconds=args.window)
        if args.out:
            out = __import__("pathlib").Path(args.out)
            await streamer.render(out)
            print(f"[run] rendered -> {out}")
        else:
            print("[run] set --out or BILI_PUSH_URL/KEY to see output")
    else:
        print("[run] live loop (Ctrl-C to stop). If pushing, ensure BILI_PUSH_URL/KEY set.")
        if cfg.pushing:
            await streamer.push_loop()
        else:
            raise SystemExit("nothing to do: set --beats/--out (file) or BILI_PUSH_URL/KEY (live)")


async def _probe(args) -> None:
    cfg = _cfg(video_provider=args.provider)
    from .video_client import VideoClient
    import time as _t
    client = VideoClient.build(cfg)
    dest = cfg.work_dir / "raw" / f"probe_{args.provider}.mp4"
    print(f"[probe] {cfg.video_provider}: generating {args.duration}s clip...")
    t0 = _t.monotonic()
    clip = await client.generate("a golden retriever in a meadow, cinematic, no text",
                                 args.duration, dest, timeout=180)
    print(f"[probe] OK in {_t.monotonic()-t0:.1f}s -> {clip.path} "
          f"({clip.duration_seconds:.1f}s actual)")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="infinity-live")
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("mock", help="offline demo -> a real local mp4")
    pm.add_argument("--beats", type=int, default=4)
    pm.add_argument("--latency", type=float, default=4.0)
    pm.add_argument("--window", type=float, default=None)
    pm.add_argument("--out", default="assets/demo/output.mp4")
    pm.set_defaults(func=_mock)

    pr = sub.add_parser("run", help="full loop (configured provider + danmaku)")
    pr.add_argument("--provider", default=None)
    pr.add_argument("--danmaku", default=None)
    pr.add_argument("--room", default=None)
    pr.add_argument("--beats", type=int, default=None)
    pr.add_argument("--out", default=None)
    pr.add_argument("--window", type=float, default=None)
    pr.set_defaults(func=_run)

    ps = sub.add_parser("stream", help="continuous plot-driven loop (no vote/pause)")
    ps.add_argument("--provider", default=None)
    ps.add_argument("--danmaku", default=None)
    ps.add_argument("--room", default=None)
    ps.add_argument("--clips", type=int, default=None, help="N clips -> offline mp4; omit -> live push")
    ps.add_argument("--out", default=None)
    ps.add_argument("--refresh", type=float, default=6.0, help="danmaku->prompt refresh seconds")
    ps.set_defaults(func=_stream)

    pp = sub.add_parser("probe", help="validate a real video API key")
    pp.add_argument("--provider", default=None)
    pp.add_argument("--duration", type=int, default=5)
    pp.set_defaults(func=_probe)

    args = p.parse_args(argv)
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
