"""Turn audience room signal into a video-generation prompt.

Rule-based by default (no API key, works offline); if LLM_* is configured it
uses DeepSeek to write a richer cinematic brief. The prompt is what drives the
next beat's clip, so a changed vote profile changes the next 15 seconds.
"""
from __future__ import annotations

import httpx

from .config import Config
from .events import summarize
from .models import RoomEvent

DEGRADED = {
    "top_keywords": [],
    "superchats": [],
    "votes": [],
    "danmaku_count": 0,
}

SCENE_TEMPLATES = [
    "a dreamy floating island at sunset with drifting clouds and glinting waterfalls",
    "a neon-soaked cyberpunk alley in the rain, holographic signs flickering",
    "a tiny cute kitten in a gold armor commanding a mouse army on a cliff",
    "a futuristic space station corridor with a window onto a swirling nebula",
    "a cozy rooftop garden at dawn, warm light, hummingbirds and blooming flowers",
    "a lone samurai standing in a snowfield as petals of sakura blur past",
]


async def build_prompt(cfg: Config, events: list[RoomEvent]) -> str:
    signal = summarize(events)
    topics = [k for k, _ in signal["top_keywords"]]
    sc_text = " ".join(s["text"] for s in signal["superchats"])

    if cfg.llm_api_key:
        try:
            return await _llm_prompt(cfg, signal)
        except Exception:
            pass  # fall through to the rule-based draft on any LLM failure

    # --- deterministic draft -------------------------------------------------
    scene = _pick_scene(topics, signal)
    subject = f", featuring a dynamic subject responding to {topics[0]}" if topics else ""
    sc = f", reacting to the top superchat: {sc_text[:80]}" if sc_text else ""
    prompt = (
        f"A continuous slow cinematic pan of {scene}{subject}{sc}, "
        f"vibrant colors, soft film lighting, high detail. "
        f"Camera: one long steady dolly, no cuts, no text on screen."
    )
    return prompt


def _pick_scene(topics: list[str], signal: dict) -> str:
    if topics:
        joined = " ".join(topics).lower()
        for i, t in enumerate(SCENE_TEMPLATES):
            if any(word in joined for word in ("island", "sunset", "sky", "dream")):
                return t
    idx = (len(topics) + len(signal["votes"])) % len(SCENE_TEMPLATES)
    return SCENE_TEMPLATES[idx]


async def _llm_prompt(cfg: Config, signal: dict) -> str:
    system = (
        "You write single-sentence prompts for a 15-second text-to-video model. "
        "One continuous shot, clear subject, strong mood, no cuts, no text/captions, "
        "no camera instructions. Output only the prompt."
    )
    user = (
        f"Audience signal: {signal}\n"
        "Write a vivid cinematic 15s video prompt that reacts to the top keyword and "
        "the top superchat."
    )
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{cfg.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
            json={
                "model": cfg.llm_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.9,
                "max_tokens": 120,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
