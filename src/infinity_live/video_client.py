"""Provider-agnostic async video generation.

Every provider exposes the same contract:
    create_task(prompt, duration) -> task_id
    get_status(task_id)           -> (state, url_or_none)
    download(url, dest)           -> Path

The loop calls one method, ``generate()``, which submits beat N+1 while beat N
plays, polls, and prefetches before the boundary. A Mock client renders a real
clip locally with ffmpeg so the whole pipeline runs offline with real video
files and no API spend.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from .config import Config
from .models import Clip

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


class VideoClient(ABC):
    name = "base"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @abstractmethod
    async def create_task(self, prompt: str, duration: int) -> str: ...

    @abstractmethod
    async def get_status(self, task_id: str) -> tuple[str, str | None]:
        """Return (state, url). state in {PENDING,RUNNING,SUCCEEDED,FAILED}."""
        ...

    async def download(self, url: str, dest: Path) -> Path:
        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as c:
            resp = await c.get(url)
            resp.raise_for_status()
        dest.write_bytes(resp.content)
        return dest

    async def generate(self, prompt: str, duration: int, dest: Path,
                       poll_interval: float = 3.0, timeout: float = 180.0) -> Clip:
        """Submit -> poll -> download -> Clip. Override entirely in Mock."""
        task_id = await self.create_task(prompt, duration)
        start = time.time()
        while time.time() - start < timeout:
            state, url = await self.get_status(task_id)
            if state == "SUCCEEDED" and url:
                await self.download(url, dest)
                return Clip(path=dest, prompt=prompt, provider=self.name,
                            duration_seconds=self.probe_duration(dest))
            if state == "FAILED":
                raise RuntimeError(f"[{self.name}] generation failed for {task_id}")
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f"[{self.name}] generation timed out after {timeout}s")

    def probe_duration(self, path: Path) -> float:
        try:
            out = subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            return float(out)
        except Exception:
            return 0.0

    @classmethod
    def build(cls, cfg: Config) -> "VideoClient":
        if cfg.video_provider == "mock":
            return MockVideoClient(cfg)
        if cfg.video_provider == "wan":
            return WanVideoClient(cfg)
        if cfg.video_provider == "seedance":
            return SeedanceVideoClient(cfg)
        if cfg.video_provider == "fastwan":
            return FastWanVideoClient(cfg)
        if cfg.video_provider == "deepinfra":
            return DeepInfraVideoClient(cfg)
        raise ValueError(f"unknown video provider: {cfg.video_provider}")


# ---------------------------------------------------------------------------
# Mock: renders a real mp4 locally via ffmpeg (testsrc2 + beat text)
# ---------------------------------------------------------------------------
class MockVideoClient(VideoClient):
    name = "mock"
    LATENCY = float(os.getenv("VIDEO_MOCK_LATENCY", "4"))

    async def create_task(self, prompt: str, duration: int) -> str:
        # simulate provider latency so the loop exercises its lookahead budget
        await asyncio.sleep(self.LATENCY)
        return f"mock-{int(time.time() * 1000)}-{abs(hash(prompt)) % 9999}"

    async def get_status(self, task_id: str):
        return "SUCCEEDED", None

    async def generate(self, prompt: str, duration: int, dest: Path,
                       poll_interval: float = 3.0, timeout: float = 180.0) -> Clip:
        await asyncio.sleep(self.LATENCY)
        await self._render(dest, prompt)
        return Clip(path=dest, prompt=prompt, provider="mock",
                    duration_seconds=self.probe_duration(dest))

    async def _render(self, dest: Path, prompt: str) -> None:
        cfg = self.cfg
        w, h = (1280, 720) if cfg.video_resolution in ("720P", "1080P") else (852, 480)
        dur = int(cfg.clip_seconds)
        text = self._sanitize(prompt)[:60]
        # no drawtext: avoids font-path/quoting hangs on an unknown host
        cmd = [
            FFMPEG, "-y",
            "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=24:d={dur}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            "-crf", "28", "-r", "24", str(dest),
        ]
        try:
            await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            # last-resort: even simpler, no re-encode requirement
            alt = [FFMPEG, "-y", "-f", "lavfi", "-i", f"color=c=0x1e1e2e:s={w}x{h}:d={dur}",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "24", str(dest)]
            await asyncio.to_thread(subprocess.run, alt, check=True, capture_output=True)

    @staticmethod
    def _sanitize(text: str) -> str:
        # keep CJK + latin alnum + simple punctuation; drop anything that would
        # break the ffmpeg drawtext filter's quoting
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9 ,.!?-]", "", text).replace("'", " ")

    @staticmethod
    def _fontfile() -> str:
        # find a Windows TrueType font; the drive colon must be both escaped and
        # the value must be quoted, or the filter parser splits at ':'
        for name in ("arialbd.ttf", "arial.ttf", "seguisb.ttf", "segoeuib.ttf"):
            cand = Path(f"C:/Windows/Fonts/{name}")
            if cand.exists():
                fp = str(cand).replace("\\", "/")
                return f"fontfile='{fp[0]}\\:{fp[2:]}'"
        return "font='Arial'"


# ---------------------------------------------------------------------------
# Alibaba Wan (万相) via DashScope / Model Studio
# ---------------------------------------------------------------------------
class WanVideoClient(VideoClient):
    name = "wan"
    BASE = "https://dashscope.aliyuncs.com/api/v1"
    MODEL_T2V = "wanx2.1-t2v-turbo"      # cheap/fast text-to-video tier
    MODEL_I2V = "wan2.6-i2v-flash"       # cheap/fast with audio

    async def create_task(self, prompt: str, duration: int) -> str:
        headers = {"Authorization": f"Bearer {self.cfg.video_api_key}",
                   "Content-Type": "application/json"}
        payload = {
            "model": self.MODEL_I2V if self.cfg.video_audio else self.MODEL_T2V,
            "input": {"prompt": prompt},
            "parameters": {
                "size": self.cfg.video_resolution,
                "ratio": self.cfg.video_ratio,
                "duration": duration,
                "prompt_extend": True,
                "watermark": False,
                "audio": self.cfg.video_audio,
            },
        }
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{self.BASE}/services/aigc/video-generation/video-synthesis",
                             headers=headers, json=payload)
            r.raise_for_status()
            return r.json()["output"]["task_id"]

    async def get_status(self, task_id: str) -> tuple[str, str | None]:
        headers = {"Authorization": f"Bearer {self.cfg.video_api_key}"}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(f"{self.BASE}/tasks/{task_id}", headers=headers)
            r.raise_for_status()
            data = r.json()["output"]
            if data["task_status"] == "SUCCEEDED":
                return "SUCCEEDED", data.get("video_url")
            if data["task_status"] in ("FAILED", "CANCELED"):
                return "FAILED", None
            return "RUNNING", None


# ---------------------------------------------------------------------------
# ByteDance Seedance 2.0 via Volcano Engine (火山方舟 / ARK)
# ---------------------------------------------------------------------------
class SeedanceVideoClient(VideoClient):
    name = "seedance"
    # Confirmed v3 contract: POST /api/v3/contents/generations/tasks -> {"id": ...}
    BASE = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
    # model ID is version-suffixed; see config.SEEDANCE_MODEL (default confirmed 2.0 fast)

    @staticmethod
    def _error(resp: httpx.Response) -> str:
        try:
            msg = resp.json().get("error", {}).get("message", "")
            return msg or f"HTTP {resp.status_code}"
        except Exception:
            return f"HTTP {resp.status_code}"

    async def create_task(self, prompt: str, duration: int) -> str:
        headers = {"Authorization": f"Bearer {self.cfg.video_api_key}",
                   "Content-Type": "application/json"}
        payload = {
            "model": self.cfg.seedance_model,
            "content": [{"type": "text", "text": prompt}],
            "resolution": self.cfg.video_resolution.lower(),   # 480p/720p/1080p
            "ratio": self.cfg.video_ratio,
            "duration": duration,
            "generate_audio": self.cfg.video_audio,
            "watermark": False,
        }
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(self.BASE, headers=headers, json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"[seedance] {self._error(r)}")
            return r.json()["id"]

    async def get_status(self, task_id: str) -> tuple[str, str | None]:
        headers = {"Authorization": f"Bearer {self.cfg.video_api_key}"}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(f"{self.BASE}/{task_id}", headers=headers)
            r.raise_for_status()
            d = r.json()
            # status enum (int -> string) per ARK: 10 init, 50 processing,
            # 90 failed, 99 success; result URLs live in videos[].
            videos = d.get("videos") or []
            if videos and videos[0].get("url"):
                return "SUCCEEDED", videos[0]["url"]
            code = d.get("task_status", d.get("status"))
            if isinstance(code, int) and code in (90,):
                return "FAILED", None
            if isinstance(code, str) and code in ("failed", "cancelled"):
                return "FAILED", None
            return "RUNNING", None


# ---------------------------------------------------------------------------
# Self-hosted FastVideo (FastWan-QAD-FP8-1.3B) on a rented GPU
# ---------------------------------------------------------------------------
class FastWanVideoClient(VideoClient):
    """Calls scripts/fastwan_server.py running on the GPU box.

    The server keeps the model warm and returns an mp4 per request (~3.4s per
    5s/480p clip on a 4090). T2V only, 5s fixed, 480p, no reference conditioning.
    """
    name = "fastwan"

    async def create_task(self, prompt: str, duration: int) -> str:
        raise NotImplementedError("fastwan is a synchronous one-shot provider")

    async def get_status(self, task_id: str) -> tuple[str, str | None]:
        raise NotImplementedError("fastwan is a synchronous one-shot provider")

    async def generate(self, prompt: str, duration: int, dest: Path,
                       poll_interval: float = 3.0, timeout: float = 300.0) -> Clip:
        url = self.cfg.fastwan_url.rstrip("/") + "/generate"
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, json={"prompt": prompt})
            r.raise_for_status()
        dest.write_bytes(r.content)
        return Clip(path=dest, prompt=prompt, provider="fastwan",
                    duration_seconds=self.probe_duration(dest) or 5.0)


# ---------------------------------------------------------------------------
# DeepInfra FastWan-QAD-FP8-1.3B (cloud, synchronous, 5s/480p, T2V only)
# ---------------------------------------------------------------------------
class DeepInfraVideoClient(VideoClient):
    """One synchronous POST -> base64 video URL in the body.

    FastWan-QAD-FP8-1.3B: ~2.4s runtime per 5s/480p clip (DeepInfra hardware),
    costs ~$0.0125/clip, T2V-only (silent), fixed 5s/480p.
    """
    name = "deepinfra"
    BASE = "https://api.deepinfra.com/v1/inference"

    @property
    def _url(self) -> str:
        return f"{self.BASE}/{self.cfg.deepinfra_model}"

    async def create_task(self, prompt: str, duration: int) -> str:
        raise NotImplementedError("deepinfra is a synchronous one-shot provider")

    async def get_status(self, task_id: str) -> tuple[str, str | None]:
        raise NotImplementedError("deepinfra is a synchronous one-shot provider")

    async def generate(self, prompt: str, duration: int, dest: Path,
                       poll_interval: float = 3.0, timeout: float = 300.0) -> Clip:
        payload = {
            "prompt": prompt,
            "seconds": int(duration or self.cfg.clip_seconds),
            "resolution": self.cfg.deepinfra_resolution,
            "orientation": self.cfg.deepinfra_orientation,
        }
        headers = {
            "Authorization": f"bearer {self.cfg.deepinfra_api_key}",
            "Content-Type": "application/json",
        }
        if not self.cfg.deepinfra_api_key:
            raise RuntimeError("deepinfra: set DEEP_INFRA_API in .env")
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            r = await c.post(self._url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()

        video = (data.get("video_url") or data.get("video")
                 or (data.get("output") or {}).get("video"))
        if not video:
            raise RuntimeError(f"deepinfra: no video in response: {str(data)[:200]}")

        content = self._to_bytes(video)
        dest.write_bytes(content)
        st = data.get("inference_status") or {}
        return Clip(
            path=dest, prompt=prompt, provider="deepinfra",
            duration_seconds=self.probe_duration(dest) or float(duration or self.cfg.clip_seconds),
            metadata={"cost": st.get("cost"), "runtime_ms": st.get("runtime_ms"),
                      "request_id": data.get("request_id")},
        )

    @staticmethod
    def _to_bytes(video: str) -> bytes:
        if video.startswith("data:"):
            # data:video/mp4;base64,xxxx
            b64 = video.split(",", 1)[1]
            return base64.b64decode(b64)
        # otherwise it's a URL -> quick sync fetch
        resp = httpx.get(video, follow_redirects=True, timeout=120)
        resp.raise_for_status()
        return resp.content
