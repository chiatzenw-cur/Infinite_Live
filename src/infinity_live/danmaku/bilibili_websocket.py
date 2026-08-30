"""Self-hosted bilibili live danmaku reader (the "no approval" MVP path).

Connects to a room's broadcast websocket and emits Danmaku / Superchat / Gift /
Guard events. This reads ANY public room with zero platform approval -- the same
approach used by blrec / bililive-go. It is unofficial, so pin a cookie from the
streamer's browser if bilibili starts requiring authentication for getDanmuInfo.

Packet protocol (all big-endian):
    header = u32 body_len | u16 header_len(=16) | u16 protover | u16 op | u32 seq
  op: 2 heartbeat, 3 heartbeat-reply(popularity), 5 normal(payload), 7 auth, 8 auth-reply
  protover: 0 plain, 1 (unused), 2 zlib, 3 brotli
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import struct
import time
import urllib.parse
import zlib

import httpx
import websockets

from ..config import Config
from ..events import EventHub
from ..models import Danmaku, Gift, Guard, Superchat
from .base import DanmakuSource

HEADER_FMT = ">IHHII"
HEADER_LEN = 16


def _encode(op: int, body: bytes, protover: int = 1) -> bytes:
    return struct.pack(HEADER_FMT, HEADER_LEN + len(body), HEADER_LEN,
                       protover, op, 1) + body


def _decode_packets(data: bytes):
    """Yield (op, body) tuples from a possibly-compressed buffer."""
    if not data:
        return
    if len(data) < HEADER_LEN:
        # compressed stream without an outer header; attempt a direct decode
        pass
    pos = 0
    while pos + HEADER_LEN <= len(data):
        body_len, _hlen, protover, op, _seq = struct.unpack_from(HEADER_FMT, data, pos)
        if body_len < HEADER_LEN or pos + body_len > len(data):
            break
        body = data[pos + HEADER_LEN: pos + body_len]
        pos += body_len
        if protover == 2:
            yield from _decode_packets(zlib.decompress(body))
        elif protover == 3:
            try:
                import brotli  # type: ignore
                yield from _decode_packets(brotli.decompress(body))
            except Exception:
                continue
        else:
            yield op, body


class BilibiliWebsocketSource(DanmakuSource):
    name = "selfhosted"
    _last_rx: float = 0.0   # monotonic ts of the last packet; drives _watchdog
    _rx_count: int = 0      # packets seen on the current connection
    _seen_errs: set = set() # dispatch error kinds already reported
    _was_live: bool = False # room live_status at connect; drives _live_watch
    _seen_cmds: set = set() # command kinds already logged (diagnostic)
    _buvid: str = ""        # device fingerprint; REQUIRED in the auth packet
    _uid: int = 0           # real uid when logged in; 0 = anonymous = no danmaku

    async def _guest_cookies(self) -> str:
        """Fetch a guest buvid3/buvid4 fingerprint (bilibili risk control for getDanmuInfo)."""
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(
                    "https://api.bilibili.com/x/frontend/finger/spi",
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                d = r.json().get("data", {})
            parts = []
            if d.get("b_3"):
                parts.append(f"buvid3={d['b_3']}")
            if d.get("b_4"):
                parts.append(f"buvid4={d['b_4']}")
            parts.append(f"b_nut={int(time.time())}")
            return "; ".join(parts)
        except Exception:
            return ""

    _MIXIN_ENC_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5,
                      49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55,
                      40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57,
                      62, 11, 36, 20, 34, 44, 52]

    @staticmethod
    def _mixin_key(img_url: str, sub_url: str) -> str:
        img = img_url.rsplit("/", 1)[1].split(".")[0]
        sub = sub_url.rsplit("/", 1)[1].split(".")[0]
        orig = img + sub
        return "".join(orig[i] for i in BilibiliWebsocketSource._MIXIN_ENC_TAB)[:32]

    @staticmethod
    def _wbi_sign(params: dict, key: str) -> dict:
        params = dict(params)
        params["wts"] = int(time.time())
        params = dict(sorted(params.items()))
        query = urllib.parse.urlencode(params)
        params["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
        return params

    async def _http(self) -> tuple[int, str, str, int, str]:
        room_id = self.cfg.bili_room_id
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://live.bilibili.com/",
        }
        cookie = os.getenv("BILI_COOKIE", "").strip()
        if not cookie:
            cookie = await self._guest_cookies()
        if cookie:
            headers["Cookie"] = cookie
        # Keep the device fingerprint: it must go in the WS AUTH packet too, not just
        # the HTTP headers. We were fetching buvid3 and then discarding it, so the
        # socket authenticated as a device-less client -- which bilibili accepts and
        # then declines to send danmaku to. Every working client (blrec, bililive-go,
        # the web player) sends buvid in the auth body.
        m = re.search(r"buvid3=([^;]+)", cookie or "")
        self._buvid = m.group(1) if m else ""
        # A logged-in session: DedeUserID rides in the cookie, or set BILI_UID.
        um = re.search(r"DedeUserID=(\d+)", cookie or "")
        self._uid = int(um.group(1)) if um else int(os.getenv("BILI_UID", "0") or 0)
        # NOTE: a login is NOT required. An anonymous socket with a guest buvid does
        # receive DANMU_MSG -- confirmed live. bilibili sends LOG_IN_NOTICE as an
        # informational notice, not as a refusal; do not mistake it for a gate.
        async with httpx.AsyncClient(headers=headers, timeout=30) as c:
            init = await c.get(
                "https://api.live.bilibili.com/room/v1/Room/room_init",
                params={"id": room_id})
            init.raise_for_status()
            init_data = init.json().get("data")
            if init_data is None:
                raise RuntimeError(f"room_init failed: {init.text[:200]}")
            real_room = init_data["room_id"]

            # Bilibili does not broadcast danmaku for an OFFLINE room, so a healthy
            # socket with zero messages is indistinguishable from a broken reader.
            # Two wrong theories were chased about this before anyone checked whether
            # the room was live -- say it out loud instead.
            self._was_live = init_data.get("live_status", 1) == 1
            if not self._was_live:
                print(f"[danmaku] room {real_room} is OFFLINE "
                      f"(live_status={init_data.get('live_status')}); connecting anyway "
                      f"and will RECONNECT the moment it goes live")

            # WBI sign getDanmuInfo (bilibili -352 risk control)
            wbi_key = ""
            try:
                nav = await c.get("https://api.bilibili.com/x/web-interface/nav")
                wbi = (nav.json().get("data") or {}).get("wbi_img") or {}
                if wbi.get("img_url") and wbi.get("sub_url"):
                    wbi_key = self._mixin_key(wbi["img_url"], wbi["sub_url"])
            except Exception:
                pass
            params = {"id": real_room}
            if wbi_key:
                params = self._wbi_sign(params, wbi_key)
            di = await c.get(
                "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
                params=params)
            di.raise_for_status()
            di_data = di.json().get("data")
            if di_data is None or not di_data.get("host_list"):
                raise RuntimeError(
                    f"getDanmuInfo failed (set BILI_COOKIE if -352 persists): {di.text[:200]}")
            host = di_data["host_list"][0]
            return real_room, host["host"], int(host["wss_port"]), host["ws_port"], di_data["token"]

    async def run(self) -> None:
        while True:
            try:
                real_room, host, wss_port, ws_port, token = await self._http()
                url = f"wss://{host}:{wss_port}/sub"
                # ping_interval=None disables the websockets library's OWN keepalive.
                # It defaults to sending a WebSocket-protocol PING every 20s and closing
                # with 1011 'keepalive ping timeout' if no PONG arrives -- but bilibili
                # never answers WS-level pings, it uses the application-level op=2
                # heartbeat below. So the library was tearing down perfectly healthy
                # connections every couple of minutes (observed live with zero traffic,
                # losing every danmaku sent during the 5s reconnect).
                async with websockets.connect(
                        url, max_size=2 ** 24, ping_interval=None) as ws:
                    # THE fix for "the reader never receives danmaku": `buvid` (the
                    # device fingerprint) must be in the AUTH BODY, not just the HTTP
                    # headers -- we fetched it and threw it away for the whole project.
                    # Without it bilibili accepts the socket, sends room status events
                    # (LIVE, LOG_IN_NOTICE, STOP_LIVE_ROOM_LIST...) and silently omits
                    # DANMU_MSG. uid stays 0: anonymous is fine, the fingerprint is what
                    # matters. Confirmed live -- a viewer comment arrived minutes after.
                    auth_body = {
                        "uid": self._uid, "roomid": real_room, "protover": 3,
                        "platform": "web", "type": 2, "key": token,
                    }
                    if self._buvid:
                        auth_body["buvid"] = self._buvid
                    auth = _encode(7, json.dumps(auth_body).encode())
                    await ws.send(auth)
                    self._last_rx = time.monotonic()
                    has_buvid = "yes" if self._buvid else "NO (bilibili may withhold danmaku)"
                    print(f"[danmaku] connected room={real_room} via {host} "
                          f"buvid={has_buvid} protover=3")
                    hb = asyncio.create_task(self._heartbeat(ws, real_room))
                    wd = asyncio.create_task(self._watchdog(ws))
                    lw = asyncio.create_task(self._live_watch(ws))
                    stt = asyncio.create_task(self._status())
                    try:
                        await self._read_loop(ws)
                    finally:
                        hb.cancel()
                        wd.cancel()
                        lw.cancel()
                        stt.cancel()
                    # A CLEAN close lands here with nothing logged -- which is exactly how
                    # a silent reconnect loop stayed invisible: no danmaku AND no errors
                    # look identical from outside. Never let a disconnect be silent.
                    print(f"[danmaku] connection closed cleanly after "
                          f"{self._rx_count} packets; reconnecting in 5s")
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[danmaku] got {e!r}; reconnecting in 5s")
                await asyncio.sleep(5)

    async def _heartbeat(self, ws, room_id: int) -> None:
        while True:
            try:
                body = json.dumps({"uid": 0, "roomid": room_id, "protover": 2,
                                   "platform": "web", "type": 2}).encode()
                await ws.send(_encode(2, body))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # a transient send error must NOT kill heartbeat forever (that would
                # let bilibili drop us with 'keepalive ping timeout'); retry next tick
                print(f"[danmaku] heartbeat err {type(e).__name__}; retrying")
            await asyncio.sleep(15)   # bilibili pings ~30s; 15s keeps us well inside

    async def _is_live(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    "https://api.live.bilibili.com/room/v1/Room/room_init",
                    params={"id": self.cfg.bili_room_id},
                    headers={"User-Agent": "Mozilla/5.0",
                             "Referer": "https://live.bilibili.com/"})
                return ((r.json().get("data") or {}).get("live_status") == 1)
        except Exception:
            return self._was_live      # transient API failure: assume unchanged

    async def _live_watch(self, ws, every: float = 15.0) -> None:
        """THE fix for "the app never receives danmaku".

        Bilibili only streams danmaku for a LIVE room, and it decides that at CONNECT
        time -- a socket opened while the room is dark stays deaf even after the room
        goes live. The app always connects before livehime is broadcasting (livehime
        only goes live once it RECEIVES our push), so the reader was structurally deaf
        on every single run: healthy socket, heartbeats flowing, zero danmaku, no error.

        Proven live: room confirmed `live_status: 1`, 10 messages typed, 0 captured --
        while a separate socket opened AFTER the room went live received them fine.

        So: watch for the 0 -> 1 transition and reconnect, which makes bilibili start
        sending. Also covers livehime dropping and coming back mid-run.
        """
        while True:
            await asyncio.sleep(every)
            live = await self._is_live()
            if live and not self._was_live:
                self._was_live = True
                print("[danmaku] room went LIVE -> reconnecting so bilibili starts "
                      "sending danmaku (a socket opened while dark stays deaf)")
                await ws.close()
                return
            self._was_live = live

    async def _status(self, every: float = 30.0) -> None:
        """Say out loud what the socket is doing. Across many runs "zero danmaku" was
        indistinguishable from "task died" and from "subscribed to nothing" -- print the
        packet count and the payload command kinds so the next run tells us which."""
        while True:
            await asyncio.sleep(every)
            kinds = sorted(k for k in self._seen_cmds if k)
            print(f"[danmaku] alive: {self._rx_count} packets, "
                  f"{len(kinds)} payload cmd kinds seen"
                  + (f" -> {kinds[:6]}" if kinds else " -> NONE (heartbeats only)"))

    async def _watchdog(self, ws, stale_after: float = 60.0) -> None:
        """Replaces the library keepalive we disabled, using a signal bilibili actually
        sends: it answers our op=2 with an op=3 popularity reply every ~30s, so silence
        for 60s means the connection is dead even though TCP hasn't noticed. Closing
        here ends `_read_loop` and `run()` reconnects."""
        while True:
            await asyncio.sleep(10)
            if time.monotonic() - self._last_rx > stale_after:
                print(f"[danmaku] no data for {stale_after:.0f}s; forcing reconnect")
                await ws.close()
                return

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            self._last_rx = time.monotonic()
            self._rx_count += 1
            for op, body in _decode_packets(raw if isinstance(raw, bytes) else raw.encode()):
                if op == 2:
                    # bilibili may ping (op=2); echo it back as a pong so keepalive passes
                    try:
                        await ws.send(_encode(2, body))
                    except Exception:
                        pass
                    continue
                if op != 5:
                    continue
                try:
                    self._dispatch(json.loads(body.decode("utf-8")))
                except Exception as e:
                    # was a bare `continue`: a malformed or unexpected message shape
                    # vanished with no trace, which is indistinguishable from "no one
                    # is typing". Log it (once per kind) instead.
                    k = type(e).__name__
                    if k not in self._seen_errs:
                        self._seen_errs.add(k)
                        print(f"[danmaku] dispatch error {k}: {e} (further {k} muted)")
                    continue

    def _dispatch(self, msg: dict) -> None:
        cmd = msg.get("cmd", "")
        # Log every command kind ONCE. The app captured 0 danmaku across several runs
        # while a probe on the same room received them, and every theory so far has been
        # wrong -- so record what actually arrives instead of guessing again.
        if cmd not in self._seen_cmds:
            self._seen_cmds.add(cmd)
            print(f"[danmaku] first seen cmd={cmd!r}")
        # startswith, NOT ==: bilibili sends suffixed variants like
        # 'DANMU_MSG:4:0:2:2:2:0'. The probe that DID receive messages used a substring
        # match; this exact match would drop every suffixed one silently.
        if cmd.startswith("DANMU_MSG"):
            # info lives at the TOP level of DANMU_MSG: info[1]=text, info[2][1]=username
            info = msg.get("info") or (msg.get("data") or {}).get("info", [])
            user = info[2][1] if len(info) > 2 and len(info[2]) > 1 else ""
            text = info[1] if len(info) > 1 else ""
            print(f"[danmaku] {user}: {text}")
            asyncio.ensure_future(self.hub.emit(Danmaku(user=user, text=text)))
        elif cmd == "SUPER_CHAT_MESSAGE":
            data = msg.get("data", {})
            u = data.get("user_info", {})
            asyncio.ensure_future(self.hub.emit(Superchat(
                user=u.get("uname", ""),
                amount=data.get("price", 0.0),
                text=data.get("message", ""),
            )))
        elif cmd in ("SEND_GIFT", "COMBO_SEND"):
            data = msg.get("data", {})
            asyncio.ensure_future(self.hub.emit(Gift(
                user=data.get("uname", ""),
                amount=data.get("price", 0.0) or data.get("total_coin", 0) / 1000,
                text=f"x{data.get('num', 1)} {data.get('giftName', '')}",
            )))
        elif cmd in ("GUARD_BUY", "GUARD_MSG"):
            data = msg.get("data", {})
            asyncio.ensure_future(self.hub.emit(Guard(
                user=data.get("username", ""), amount=data.get("price", 0.0),
            )))
