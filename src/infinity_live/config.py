"""Environment-driven configuration. Values come from .env or the shell."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    # --- beat / loop -----------------------------------------------------
    beat_seconds: int = _int("BEAT_SECONDS", 15)
    lookahead: int = _int("LOOKAHEAD", 1)  # how many beats ahead to prefetch

    # --- video generation ------------------------------------------------
    # provider: mock | wan | seedance | kling | fastwan | deepinfra
    # accept the common key-name aliases so a hand-written .env just works
    video_provider: str = os.getenv(
        "VIDEO_PROVIDER",
        "deepinfra" if (os.getenv("DEEP_INFRA_API") or os.getenv("DEEPINFRA_API_KEY")) else
        "seedance" if (os.getenv("VOLCANO_API") or os.getenv("ARK_API_KEY")) else
        "wan" if os.getenv("DASHSCOPE_API_KEY") else "mock",
    )
    video_resolution: str = os.getenv("VIDEO_RESOLUTION", "720P")
    video_ratio: str = os.getenv("VIDEO_RATIO", "16:9")
    video_api_key: str = os.getenv("VIDEO_API_KEY") or os.getenv(
        "VOLCANO_API") or os.getenv("ARK_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or ""
    video_audio: bool = os.getenv("VIDEO_AUDIO", "1") == "1"
    # Seedance model ID is version-suffixed; override via env, else the confirmed 2.0 fast
    seedance_model: str = os.getenv("SEEDANCE_MODEL", "doubao-seedance-2-0-fast-260128")
    # self-hosted FastWan server (scripts/fastwan_server.py) URL
    fastwan_url: str = os.getenv("FASTWAN_URL", "http://127.0.0.1:8000")

    # --- DeepInfra (FastWan-QAD-FP8-1.3B) --- synchronous, 5s/480p, T2V only
    deepinfra_api_key: str = os.getenv("DEEP_INFRA_API") or os.getenv("DEEPINFRA_API_KEY") or ""
    # Active model = the FastWan-QAD-FP8-1.3B that the stream was built on: 16fps,
    # ~$0.0125/clip, art direction now carried by the story style prompt (kyoani/chibi)
    # rather than by a heavier model. The 5B review model remains available as a spare
    # via DEEPINFRA_MODEL=FastVideo/FastWan2.2-TI2V-5B-FullAttn-Diffusers. The 1.3B is
    # fixed at 5s clips; a 10s request returns 422.
    deepinfra_model: str = os.getenv(
        "DEEPINFRA_MODEL", "FastVideo/FastWan-QAD-FP8-1.3B")
    deepinfra_resolution: str = os.getenv("DEEPINFRA_RESOLUTION", "480p")
    deepinfra_orientation: str = os.getenv("DEEPINFRA_ORIENTATION", "landscape")
    clip_seconds: int = _int("CLIP_SECONDS", 5)   # deepinfra FastWan is fixed 5s

    # --- continuous push: the canonical elementary-stream encode ----------
    # Every segment fed to the single long-lived RTMP publisher MUST share these
    # H.264 parameters (one SPS for the whole stream), so anything that differs
    # is re-encoded to match before it is fed in. Defaults = the FastWan raw clip
    # encode, so real clips are `-c copy` passthrough with no added latency.
    stream_width: int = _int("STREAM_WIDTH", 832)
    stream_height: int = _int("STREAM_HEIGHT", 480)
    stream_fps: int = _int("STREAM_FPS", 16)
    # 0 = per-clip direct publish (old behaviour, loading between clips)
    continuous_push: bool = os.getenv("CONTINUOUS_PUSH", "1") == "1"
    # Burn the director's plan (beat / cast / danmaku / action / plot) onto the
    # picture. This is VISIBLE TO VIEWERS -- set DEBUG_OVERLAY=0 for a clean broadcast.
    debug_overlay: bool = os.getenv("DEBUG_OVERLAY", "1") == "1"
    # 1920s silent-film grade (B&W, grain, vignette, gate flicker) applied in the same
    # re-encode pass as the -bf 0 fix. Also masks the generator's clip-to-clip character
    # drift, which colour makes obvious. SILENT_GRAIN trades bitrate for that masking.
    silent_film: bool = os.getenv("SILENT_FILM", "1") == "1"
    silent_grain: int = _int("SILENT_GRAIN", 12)
    # Projector gate flicker amplitude. DEFAULT 0 = OFF, deliberately: the first version
    # pulsed at 6Hz and 17Hz, which is inside the 3-30Hz photosensitive band, and the
    # user reported eye strain within one run. Grain + vignette + contrast carry the
    # period look on their own. If re-enabled, keep it small (<=0.02) and slow (<3Hz).
    silent_flicker: float = float(os.getenv("SILENT_FLICKER", "0"))
    # clips kept generated-ahead; deep enough to ride out a slow generation (one clip
    # has taken 42s vs the usual ~5s) without the feed falling back to filler.
    buffer_target: int = _int("BUFFER_TARGET", 3)

    # --- local assets ----------------------------------------------------
    work_dir: Path = Path(os.getenv("WORK_DIR", "assets"))
    journal_path: Path = Path(os.getenv("JOURNAL_PATH", "assets/journal.jsonl"))
    # The director is no longer timer-paced -- it is back-pressured by a maxsize-1 queue
    # so it emits exactly one beat per clip (see continuous.director_loop). Kept only for
    # callers that still pass it.
    director_interval: float = float(os.getenv("DIRECTOR_INTERVAL", "2.0"))
    # How long an audience event stays in the signal. Without this the `recent` list was
    # trimmed by count only, so in a quiet room one danmaku rode along in every prompt
    # forever (observed: a single "check" in 50 consecutive beats).
    signal_window_s: float = float(os.getenv("SIGNAL_WINDOW_S", "90"))

    # --- bilibili: reading the room --------------------------------------
    # danmaku_mode: mock | selfhosted | openlive
    bili_danmaku_mode: str = os.getenv("BILI_DANMAKU_MODE", "mock")
    bili_room_id: str = os.getenv("BILI_ROOM_ID", "")
    # Open Live platform credentials (production path, needs 入驻审核)
    bili_open_app_id: str = os.getenv("BILI_OPEN_APP_ID", "")
    bili_open_secret: str = os.getenv("BILI_OPEN_SECRET", "")

    # --- bilibili: pushing the stream ------------------------------------
    bili_push_url: str = os.getenv("BILI_PUSH_URL", "")  # rtmp://live-push.bilivideo.com/live
    bili_stream_key: str = os.getenv("BILI_STREAM_KEY", "")
    # OBS-bridge: when set, the continuous concat stream is pushed to THIS local
    # RTMP source (what OBS's Media Source reads) instead of livehime; OBS then
    # re-broadcasts to livehime with a connection it never drops.
    push_url_override: str = os.getenv("OBS_SOURCE_RTMP", "")
    # when OUTPUT_PATH is set, streamer writes a local file instead of pushing
    output_path: str = os.getenv("OUTPUT_PATH", "")

    # --- llm (vote -> prompt) ---------------------------------------------
    llm_api_key: str = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
    llm_base_url: str = os.getenv("LLM_BASE_URL") or os.getenv("DEEPSEEK_API_ENDPOINT") or "https://api.deepseek.com"
    # DEEPSEEK_API_ENDPOINT is allowed to be the full /chat/completions URL; normalise to base
    if "/chat/completions" in llm_base_url:
        llm_base_url = llm_base_url.split("/chat/completions")[0]
    llm_model: str = os.getenv("LLM_MODEL") or "deepseek-chat"

    @property
    def rtmp_full(self) -> str:
        return f"{self.bili_push_url.rstrip('/')}/{self.bili_stream_key.lstrip('/')}"

    @property
    def pushing(self) -> bool:
        return bool((self.bili_push_url or self.push_url_override) and self.bili_stream_key)

    def ensure_dirs(self) -> None:
        for sub in ("raw", "ready", "interstitials", "demo"):
            (self.work_dir / sub).mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    return Config()
