"""Continuous delivery engine.

A live room needs a *non-stop* RTMP push; you can't push a 15s file once a
minute or bilibili closes the room. So this maintains an ordered list of
committed clips and streams them as one concatenated program, padding gaps with
a looping interstitial "(awaiting audience)" b-roll.

Two modes:
  * render(out_path)  -- concat committed clips into one local mp4 (offline demo)
  * push_loop()       -- concat + push to the bilibili RTMP URL (live)

All segments are normalized to the same encode (h264 1280x720 24fps yuv420p,
re-encoded here if a provider returns something else) so `-c copy` concatenation
is frame-accurate.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import Config
from .models import Clip

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def _run(cmd: list[str], timeout: int = 600) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd)}\n{proc.stderr[-800:]}")
    return proc.stdout


class Streamer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.clips: list[Clip] = []
        self._standard_size = (1280, 720)
        self._inter = self._make_interstitial()
        # match the FastWan raw clip encode (h264 832x480 @16 yuv420p) so the
        # single publisher can `-c copy` everything without re-encoding clips.
        self.err_screen = self._make_error_screen()
        # Short filler fed between clips, kept to 1s so a freshly-ready clip goes on air
        # within ~1s instead of waiting out a long interstitial. This is the user's own
        # error_screen.png ("lagging") ON PURPOSE: when the buffer is dry the stream IS
        # lagging, so naming it plainly beats a euphemistic "please stand by" card.
        self.idle_screen = self._make_error_screen(seconds=1, name="idle_short.mp4")
        self._pub: subprocess.Popen | None = None  # single long-lived RTMP publisher
        self._push_err = None  # open file handle for the publisher's stderr log
        self._annexb_cache: dict[Path, bytes] = {}
        self._fed = 0

    # -- idle / error screen (used when no clip is ready) --------------------
    def _make_error_screen(self, seconds: int = 8, name: str = "error_screen.mp4") -> Path:
        src = Path("error_screen.png")
        dest = self.cfg.work_dir / "interstitials" / name
        if not src.exists():
            print("[streamer] error_screen.png not found; using gradient interstitial")
            return self._inter
        w, h, fps = self.cfg.stream_width, self.cfg.stream_height, self.cfg.stream_fps
        # match the raw clip encode exactly so it feeds the continuous publisher as
        # copy-passthrough; loop 1 still, scaled+letterboxed
        cmd = [
            FFMPEG, "-y", "-loop", "1", "-i", str(src),
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                   f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p",
            "-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps), "-an", str(dest),
        ]
        _run(cmd)
        print(f"[streamer] error screen: {dest} ({dest.stat().st_size//1024} KiB)")
        return dest

    # -- live RTMP push: ONE direct publish per segment (livehime's accepted form)
    # livehime accepts a DIRECT `-re -i <file> -c copy -f flv rtmp` publish but
    # rejects a concat-demuxer stream, so each segment is its own direct ffmpeg,
    # chained back-to-back. `-flvflags no_duration_filesize` suppresses the
    # end-of-stream header write that triggers the -10054 reset on teardown, so
    # chaining stays clean (no disconnect between clips).
    def _push_direct(self, path: Path, retry: int = 1) -> None:
        target = (self.cfg.push_url_override or self.cfg.bili_push_url).rstrip('/')
        key = self.cfg.bili_stream_key
        if not target or not key:
            raise RuntimeError("set OBS_SOURCE_RTMP/BILI_PUSH_URL + BILI_STREAM_KEY to push live")
        rtmp = f"{target}/{key.lstrip('/')}"
        cmd = [FFMPEG, "-y", "-re", "-i", str(path), "-c", "copy",
               "-flvflags", "no_duration_filesize", "-f", "flv", rtmp]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 and retry > 0:
            return self._push_direct(path, retry=retry - 1)
        if proc.returncode != 0:
            raise RuntimeError(
                f"rtmp push failed for {path.name}: "
                f"{' '.join((proc.stderr or proc.stdout).splitlines()[-4:])}")

    def push_clip_live(self, clip: Clip) -> None:
        """Push one RAW provider clip (h264 832x480@16) directly -- livehime accepts
        it as-is, no re-encode, no added latency."""
        self._push_direct(clip.path)

    def push_idle_live(self) -> None:
        """Push the error / idle screen (same encode) so the feed never drops when
        no clip is ready."""
        self._push_direct(self.err_screen)

    # -- ONE continuous RTMP publisher (the fix for the clip-boundary loading) --
    #
    # Why the earlier attempts failed: every clip is its own *container*, and two
    # containers can't be appended into one stream -- the second FLV/MP4 header is
    # read as "Invalid data found" and the demuxer freezes (see progress_so_far §2).
    #
    # The fix is to drop the container entirely. We feed the publisher a raw H.264
    # **Annex-B elementary stream**: no header, no per-file metadata, just NAL units.
    # Concatenated elementary streams are legal by construction -- each clip simply
    # carries its own SPS/PPS, which decoders accept mid-stream -- so ONE ffmpeg and
    # ONE RTMP publish span every clip and livehime never sees the stream end.
    #
    # The remaining catch: the raw-h264 demuxer emits packets with no timestamps, so
    # `-c copy` dies with "Packet is missing PTS". `setts=ts=N/<fps>/TB` regenerates
    # them from the packet index, which keeps counting ACROSS clip boundaries -- one
    # strictly-monotonic CFR timeline for the whole broadcast.
    def _publisher_target(self) -> str:
        target = (self.cfg.push_url_override or self.cfg.bili_push_url).rstrip('/')
        if not target:
            raise RuntimeError("set BILI_PUSH_URL (or OBS_SOURCE_RTMP) to push the stream")
        return f"{target}/{self.cfg.bili_stream_key.lstrip('/')}"

    def open_publisher(self) -> None:
        rtmp = self._publisher_target()
        fps = self.cfg.stream_fps
        cmd = [
            FFMPEG, "-y", "-re",
            "-f", "h264", "-framerate", str(fps), "-i", "pipe:0",
            "-c", "copy", "-bsf:v", f"setts=ts=N/{fps}/TB",
            "-f", "flv", "-flvflags", "no_duration_filesize", rtmp,
        ]
        log = self.cfg.work_dir / "bridge.log"
        self._push_err = open(log, "w", encoding="utf-8")
        self._pub = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._push_err)
        self._fed = 0
        print(f"[streamer] continuous publisher up -> {rtmp} (log: {log.name})")

    # -- 1920s silent-film grade ---------------------------------------------
    # Applied in the SAME pass as the `-bf 0` re-encode, so it costs one filter chain
    # rather than another encode. Two payoffs beyond the look: 16fps is already the
    # native silent-film rate (our clips are 16fps, so nothing is faked), and the grain
    # + contrast crush hide the generator's clip-to-clip character drift, which is the
    # single most visible weakness of stitching independent 5s T2V clips.
    def _film_filter(self) -> str:
        g = max(0, self.cfg.silent_grain)
        parts = [
            "format=gray",
            # orthochromatic stock: lifted blacks, rolled-off highlights
            "curves=all='0/0.06 0.25/0.20 0.5/0.52 0.75/0.86 1/0.97'",
            "eq=contrast=1.18:brightness=0.02:eval=frame",
        ]
        if g:
            parts.append(f"noise=alls={g}:allf=t+u")
        parts.append("vignette=PI/4.2")
        # Projector gate flicker, OFF by default. The original ran at 6Hz + 17Hz, well
        # inside the 3-30Hz photosensitive band, and caused eye strain within one run.
        # Kept slow (2.3Hz) and shallow when enabled at all.
        f = max(0.0, min(self.cfg.silent_flicker, 0.02))
        if f > 0:
            parts.append(f"eq=brightness='{f}*sin(2*PI*t*2.3)':eval=frame")
        return ",".join(parts)

    # -- intertitle card: the silent-film answer to a dry buffer --------------
    # An intertitle is NATIVE to the form, so a generation gap stops reading as a fault
    # and becomes part of the show -- and because the director already writes narrator
    # prose, the card carries the story forward instead of just filling time.
    @staticmethod
    def _has_cjk(text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", text or ""))

    @staticmethod
    def _card_font(text: str) -> Path | None:
        """Pick a font that can actually render the card text. Chinese intertitles need
        a CJK face (the period Latin serifs have no CJK glyphs); Latin cards keep the
        old serif chain."""
        if Streamer._has_cjk(text):
            return next((p for p in (Path("C:/Windows/Fonts/msyh.ttc"),
                                     Path("C:/Windows/Fonts/simhei.ttf"),
                                     Path("C:/Windows/Fonts/simsun.ttc"),
                                     Path("C:/Windows/Fonts/palab.ttf"),
                                     Path("C:/Windows/Fonts/timesbd.ttf")) if p.exists()), None)
        return next((p for p in (Path("C:/Windows/Fonts/palab.ttf"),
                                 Path("C:/Windows/Fonts/constanb.ttf"),
                                 Path("C:/Windows/Fonts/georgiab.ttf"),
                                 Path("C:/Windows/Fonts/timesbd.ttf")) if p.exists()), None)

    @staticmethod
    def card_seconds(text: str, wpm: int = 130) -> float:
        """Hold a card for actual READING time. Chinese is read by character (~5/sec),
        English by word; a silent film holds a card for the slowest reader, and a fixed
        2.5s cuts long cards off mid-sentence."""
        if Streamer._has_cjk(text):
            chars = len(re.sub(r"\s", "", text or ""))
            return max(2.5, min(9.0, 1.2 + chars / 5.5))
        words = len((text or "").split())
        return max(2.5, min(9.0, 1.6 + words * 60.0 / wpm))

    def intertitle(self, text: str, seconds: float | None = None,
                   dest: Path | None = None) -> Path:
        d = self.cfg.work_dir / "interstitials"
        d.mkdir(parents=True, exist_ok=True)
        dest = dest or (d / "card.mp4")
        seconds = seconds if seconds is not None else self.card_seconds(text)
        w, h, fps = self.cfg.stream_width, self.cfg.stream_height, self.cfg.stream_fps
        # Chinese cards need a CJK face; Latin keeps the period serif chain.
        font = self._card_font(text)
        if font is None:
            return self.idle_screen
        txt = self._wrap_card(text)
        f = self.cfg.work_dir / "debug" / f"{dest.stem}.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" is required: Path.write_text on Windows
        # maps \n to \r\n, and drawtext renders the stray \r as an EXTRA
        # break -- which is what made every card and overlay come out double-spaced.
        f.write_text(txt, encoding="utf-8", newline="\n")
        esc = lambda p: str(p).replace("\\", "/").replace(":", "\\:")
        bw, bh = int(w * 0.93), int(h * 0.90)
        bx, by = (w - bw) // 2, (h - bh) // 2
        grade = self._film_filter() if self.cfg.silent_film else ""
        vf = (f"drawbox=x={bx}:y={by}:w={bw}:h={bh}:color=0xd8d0be@0.9:t=3,"
              f"drawbox=x={bx+10}:y={by+10}:w={bw-20}:h={bh-20}:color=0xd8d0be@0.55:t=1,"
              f"drawtext=fontfile='{esc(font)}':textfile='{esc(f)}':fontsize=34"
              f":fontcolor=0xe8e0cc:line_spacing=6:x=(w-tw)/2:y=(h-th)/2,"
              + (grade + "," if grade else "") + "format=yuv420p")
        try:
            _run([FFMPEG, "-y", "-f", "lavfi",
                  "-i", f"color=c=0x0a0a0a:s={w}x{h}:d={seconds}:r={fps}",
                  "-vf", vf, "-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast",
                  "-crf", "23", "-profile:v", "high", "-pix_fmt", "yuv420p",
                  "-r", str(fps), "-an", str(dest)])
        except RuntimeError as e:
            print(f"[streamer] intertitle failed ({e}); using idle screen")
            return self.idle_screen
        return dest

    @staticmethod
    def _wrap_card(text: str, width: int = 34) -> str:
        import textwrap
        t = " ".join((text or "").split()) or "..."
        # CJK glyphs are square, so a 34-char Latin wrap overflows a 832px card.
        if Streamer._has_cjk(t):
            width = 16
        lines = textwrap.wrap(t, width)[:4]
        # don't leave a lone CJK punctuation mark (。，！？；、…) on its own line
        if len(lines) >= 2 and len(lines[-1]) <= 1 and lines[-1][-1:] in "。，！？；、…":
            lines[-2] = lines[-2] + lines[-1]
            lines.pop()
        return "\n".join(lines)

    # -- debug overlay: the director's planning, burned onto the picture ------
    # Free to add: `_annexb` already re-encodes every segment for the `-bf 0` fix, so
    # this is one extra filter in an existing pass, not a second encode.
    _FONT = next((p for p in (Path("C:/Windows/Fonts/msyh.ttc"),
                              Path("C:/Windows/Fonts/simhei.ttf"),
                              Path("C:/Windows/Fonts/arial.ttf")) if p.exists()), None)

    def debug_overlay(self, beat, info: dict) -> str | None:
        """Render the director's plan for THIS beat as overlay text (None when off).

        `info` must be the state captured at director time -- the pipeline runs 1-2
        beats ahead, so story.* at push time describes a beat that hasn't aired yet."""
        if not self.cfg.debug_overlay or self._FONT is None:
            return None
        import textwrap

        def wrap(label: str, s: str, width: int = 92, lines: int = 2) -> list[str]:
            s = " ".join((s or "").split())
            if not s:
                return []
            out = textwrap.wrap(s, width)[:lines]
            pad = " " * 8   # must match the label column below, or continuations misalign
            return [f"{label:<6}| {out[0]}"] + [f"{pad}{l}" for l in out[1:]]

        head = f"#{beat}"
        if info.get("chars"):
            head += f"  cast: {', '.join(info['chars'])}"
        if info.get("mood"):
            head += f"  mood: {info['mood']}"
        # The line viewers actually respond to: the audience text RECEIVED for this
        # beat, made explicit so a viewer can SEE the input that steered the film.
        if info.get("danmaku"):
            head += f"  RECEIVED DANMU: {', '.join(info['danmaku'][:4])}"
        rows = [head]
        sub = (info.get("subtitle") or "").strip()
        if sub:
            rows += [f"SUBTITLE: {sub}"]
        rows += wrap("ACT", info.get("action", ""))
        rows += wrap("PLOT", info.get("plot", ""))
        return "\n".join(rows)

    def _overlay_filter(self, text: str) -> str:
        """drawtext reading from a FILE -- filtergraph escaping of arbitrary LLM prose
        (colons, quotes, commas, backslashes) is a losing game, and `textfile` sidesteps
        it entirely. Only the two PATHS need escaping."""
        d = self.cfg.work_dir / "debug"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "overlay.txt"
        f.write_text(text, encoding="utf-8", newline="\n")  # see card note
        esc = lambda p: str(p).replace("\\", "/").replace(":", "\\:")
        # anchored TOP so the debug plan never collides with the silent-film subtitle
        # caption that lives at the bottom of the frame.
        return (f"drawtext=fontfile='{esc(self._FONT)}':textfile='{esc(f)}'"
                f":fontsize=11:fontcolor=white:line_spacing=1"
                f":box=1:boxcolor=black@0.72:boxborderw=6:x=8:y=8")

    def _subtitle_font(self) -> Path | None:
        """CJK-capable face for the silent-film dialogue subtitle. Prefer msyh (renders
        both Chinese danmaku and Latin cast names); fall back to the intertitle serif."""
        return next((p for p in (Path("C:/Windows/Fonts/msyh.ttc"),
                                 Path("C:/Windows/Fonts/simsun.ttc"),
                                 Path("C:/Windows/Fonts/palab.ttf"),
                                 Path("C:/Windows/Fonts/timesbd.ttf")) if p.exists()), None)

    def subtitle_filter(self, text: str) -> str:
        """A character's spoken line as a SUBTITLE over the shot -- the silent-film
        dialogue title. Drawn bottom-centre on a black band, CJK-capable so it reads
        whether the cast speaks English or the danmaku is Chinese. Read from a FILE
        (see _overlay_filter). ALWAYS applied on a speech beat, regardless of
        DEBUG_OVERLAY: the film is silent, so the line must be visible."""
        import textwrap
        if not (text or "").strip():
            return ""
        font = self._FONT or self._subtitle_font()
        if font is None:
            return ""
        d = self.cfg.work_dir / "debug"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "subtitle.txt"
        lines = textwrap.wrap(" ".join((text or "").split()), 40)[:2]
        if not lines:
            return ""
        f.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        esc = lambda p: str(p).replace("\\", "/").replace(":", "\\:")
        return (f"drawtext=fontfile='{esc(font)}':textfile='{esc(f)}':fontsize=22"
                f":fontcolor=white:line_spacing=4:box=1:boxcolor=black@0.6"
                f":boxborderw=10:x=(w-text_w)/2:y=h-78")

    def _annexb(self, path: Path, overlay: str | None = None,
                subtitle: str | None = None) -> bytes:
        """Segment -> raw Annex-B bytes, re-encoded WITHOUT B-frames.

        `-bf 0` is not an optimisation, it is a correctness requirement. The provider
        clips carry B-frames (`has_b_frames=2`, pattern `I B B B P ...`), so decode
        order != display order. The raw-h264 demuxer discards timestamps and `setts`
        can only regenerate them from the packet index -- i.e. DECODE order -- which
        presented every B-frame at the wrong instant and made characters visibly shake
        and blink on air, even though the source .mp4 files were perfectly fine.
        With no B-frames, decode order IS display order and `ts=N/fps` is exact.

        The re-encode also guarantees every segment shares one SPS (same size/fps/
        profile), which the single continuous publisher requires -- so it subsumes the
        old probe-and-conform step. Costs ~0.2s per clip against ~5s of airtime."""
        # cache only un-overlaid segments: the overlay text differs per beat, so a
        # path-keyed cache would serve a stale beat's caption.
        if overlay is None and subtitle is None:
            cached = self._annexb_cache.get(path)
            if cached is not None:
                return cached
        w, h, fps = self.cfg.stream_width, self.cfg.stream_height, self.cfg.stream_fps
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}")
        # grade BEFORE the debug overlay, so the overlay stays crisp and readable
        # rather than being ground up by the film grain.
        if self.cfg.silent_film and not path.name.startswith("card"):
            vf += "," + self._film_filter()
        subf = self.subtitle_filter(subtitle) if subtitle else ""
        if subf:
            vf += "," + subf
        if overlay:
            vf += "," + self._overlay_filter(overlay)
        r = subprocess.run(
            [FFMPEG, "-loglevel", "error", "-i", str(path),
             "-vf", vf,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-bf", "0", "-g", str(fps * 2), "-profile:v", "high",
             "-pix_fmt", "yuv420p", "-an",
             "-bsf:v", "h264_mp4toannexb", "-f", "h264", "-"],
            capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"annex-b encode failed for {path.name}: {r.stderr[-200:]}")
        # only the interstitials repeat, so only they are worth holding in memory
        if overlay is None and subtitle is None and path.parent.name == "interstitials":
            self._annexb_cache[path] = r.stdout
        return r.stdout

    def feed_segment(self, path: Path, overlay: str | None = None,
                     subtitle: str | None = None) -> None:
        """Write one segment's Annex-B bytes into the live publisher. BLOCKS while
        ffmpeg drains the pipe at `-re` (realtime) pace -- that back-pressure is what
        keeps the broadcast at 1x without any sleep bookkeeping. Runs in a thread."""
        if self._pub is None or self._pub.stdin is None:
            raise RuntimeError("publisher not open")
        try:
            data = self._annexb(path, overlay, subtitle)
        except RuntimeError as e:
            if overlay is None and subtitle is None:
                raise
            # a bad glyph or filter hiccup must never take the show off air
            print(f"[streamer] overlay/subtitle failed ({e}); feeding clean segment")
            data = self._annexb(path)
        try:
            self._pub.stdin.write(data)
            self._pub.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            # publisher died (livehime dropped :1935, network blip) -> reconnect and
            # resume with this segment so the show continues.
            print(f"[streamer] publisher died ({type(e).__name__}); reconnecting")
            self.close_publisher()
            self.open_publisher()
            self._pub.stdin.write(data)
            self._pub.stdin.flush()
        self._fed += 1

    def feed_idle(self) -> None:
        """Short filler so the pipe never runs dry while a clip is still generating."""
        self.feed_segment(self.idle_screen)

    @property
    def publisher_alive(self) -> bool:
        return self._pub is not None and self._pub.poll() is None

    def close_publisher(self) -> None:
        if self._pub is not None:
            try:
                if self._pub.stdin:
                    self._pub.stdin.close()
            except Exception:
                pass
            try:
                self._pub.wait(timeout=3)
            except Exception:
                self._pub.kill()
            try:
                if self._push_err:
                    self._push_err.flush()
                    self._push_err.close()
            except Exception:
                pass
            self._pub = None
            print("[streamer] bridge publisher closed")

    # -- normalize a provider clip to the standard encode --------------------
    def normalize(self, clip: Clip, add: bool = True) -> Clip:
        path = self.cfg.work_dir / "ready" / f"{clip.path.stem}-norm.mp4"
        w, h = self._standard_size
        cmd = [
            FFMPEG, "-y", "-i", str(clip.path),
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", "24",
            "-an", "-movflags", "+faststart", str(path),
        ]
        _run(cmd)
        normalized = Clip(path=path, prompt=clip.prompt, provider=clip.provider,
                          duration_seconds=clip.duration_seconds or self.cfg.beat_seconds,
                          metadata={**clip.metadata, "normalized": True})
        if add:
            self.add_clip(normalized)
        return normalized

    def add_clip(self, clip: Clip) -> None:
        self.clips.append(clip)

    # -- looping interstitial for gap padding --------------------------------
    def _make_interstitial(self) -> Path:
        dest = self.cfg.work_dir / "interstitials" / "awaiting.mp4"
        w, h = self._standard_size
        dur = max(6, self.cfg.beat_seconds)
        cmd = [
            FFMPEG, "-y",
            "-f", "lavfi", "-i", f"gradients=s={w}x{h}:r=24:d={dur}:c0=0x14142b:c1=0x2b2b4a",
            "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "26", "-r", "24", str(dest),
        ]
        _run(cmd)
        return dest

    # -- build a concat playlist: interstitial then each clip -----------------
    def _playlist(self, path: Path, between: bool = True) -> Path:
        lines = ["ffconcat version 1.0"]
        for i, c in enumerate(self.clips):
            if between and i > 0:
                # wrap a short interstitial so the handoff never reads as a hard cut
                lines.append(f"file '{self._inter.resolve().as_posix()}'")
            lines.append(f"file '{c.path.resolve().as_posix()}'")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    async def render(self, out_path: Path, between: bool = True) -> Path:
        plist = self._playlist(self.cfg.work_dir / "ready" / "playlist.txt", between)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(plist),
              "-c", "copy", "-movflags", "+faststart", str(out_path)])
        return out_path

    async def push_loop(self, between: bool = True) -> None:
        """Live path: push committed clips as one continuous stream to RTMP.
        Requires a real-name bilibili account and push URL/key (see README)."""
        if not self.cfg.pushing:
            raise RuntimeError("set BILI_PUSH_URL + BILI_STREAM_KEY to push live")
        rtmp = self.cfg.rtmp_full
        plist = self._playlist(self.cfg.work_dir / "ready" / "playlist.txt", between=between)
        _run([FFMPEG, "-re", "-f", "concat", "-safe", "0", "-i", str(plist),
              "-c", "copy", "-f", "flv", rtmp])
        # NOTE: `-re` realtime paces output to clip duration so bilibili sees a
        # steady live feed. On new clips, restart (OBS-style) at the boundary.
