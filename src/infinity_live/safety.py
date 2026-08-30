"""Content safety for a China-streaming (bilibili) AIGC show.

Three gates:
  1. filter_danmaku()  -- drop audience input that carries banned content BEFORE it
                          reaches the director, so viewers can't steer the show there.
  2. The director is hard-constrained by its system prompt (never emit banned content).
  3. sanitize_prompt() -- moderate the final prompt right before it hits the video model;
                          a banned prompt is rejected (caller falls back to a safe beat).

Categories follow China live-streaming/prov. regulations (no politics, explicitness,
violence/gore, gambling, drugs, superstition/cult, abuse/hate, illegal).
"""
from __future__ import annotations

import re

# Each category = a list of tokens; a match on ANY token in ANY category ⇒ banned.
_BANNED = {
    "politics": ["习近平", "政治", "敏感", "反革命", "暴恐", "颠覆", "邪教", "台独", "港独",
                 "疆独", "藏独", "反动", "游行", "示威", "暴动", "军警", "维稳",
                 "境外势力", "颜色革命", "六四", "唐山",
                 ],
    "explicit": ["色情", "情色", "裸", "性交", "口交", "自慰", "av片", "av女", "porn", "成人", "黄片", "妓",
                 "淫", "嫖娼", "卖淫", "互撸", "这很刺激"],
    "violence": ["暴力", "打架", "斗殴", "恐怖", "杀人", "砍", "肢解", "虐待", "自残",
                 "自杀", "凶杀", "尸体", "尸块", "酷刑", "血腥", "碎尸", "爆头", "枪击"],
    "gambling": ["赌博", "赌场", "博彩", "彩票", "赌球", "下注", "骰子", "老虎机",
                 "炸金花", "打鱼机", "六合彩"],
    "drugs": ["毒品", "冰毒", "海洛因", "摇头丸", "大麻", "k粉", "吸毒", "贩毒",
              "麻古", "笑气"],
    "superstition": ["迷信", "算命", "看相", "风水", "降头", "巫术", "开光", "符咒",
                     "做法事", "驱邪", "法师", "通灵"],
    "abuse": ["傻逼", "他妈", "你妈", "操", "滚", "垃圾", "废物", "去死", "贱",
              "死全家", "贱人", "王八蛋", "狗东西"],
}

_DEFAULT_SAFE_ACTION = "the scene carries on gently, the world settling into quiet detail."


class Censor:
    def __init__(self, extra: list[str] | None = None) -> None:
        self._case_sensitive = {"explicit": True}   # keep 'av'/'k粉' case meaningful
        self._rx: dict[str, re.Pattern] = {}
        for cat, terms in _BANNED.items():
            flags = 0 if self._case_sensitive.get(cat) else re.IGNORECASE
            self._rx[cat] = re.compile("|".join(re.escape(t) for t in terms), flags)
        if extra:
            self._rx["extra"] = re.compile("|".join(re.escape(t) for t in extra))

    def find_banned(self, text: str) -> str | None:
        """Return the category name if the text carries any banned content, else None."""
        for cat, rx in self._rx.items():
            if rx.search(text):
                return cat
        return None

    def is_banned(self, text: str) -> bool:
        return self.find_banned(text) is not None

    def filter_danmaku(self, text: str) -> str:
        """Return text unless it carries banned content, in which case drop the message."""
        return "" if self.is_banned(text) else text

    def sanitize_prompt(self, prompt: str) -> tuple[bool, str]:
        """(ok, cleaned). ok=False when banned ⇒ caller should use a safe fallback."""
        if self.is_banned(prompt):
            return False, ""
        return True, prompt

    def safe_action(self) -> str:
        return _DEFAULT_SAFE_ACTION
