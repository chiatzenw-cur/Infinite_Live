"""Continuous narrative + a fixed 'show bible' (playwright state) + content safety.

The model is T2V with NO reference-conditioning, so a character can't be locked
frame-to-frame. Instead we carry a FIXED world bible -- a hard-coded art style, a
small set of fixed settings, and a small fixed cast with descriptors -- injected into
EVERY prompt. The DeepSeek director writes narrative beats *within* that bible.

THE SHOW: a light D&D-style storybook fantasy adventure in a small village and its woods
-- a young adventurer, a glowing wisp companion, a village elder, a mysterious stranger.
Drawn in a SIMPLE 3D ANIME / STORYBOOK style (deliberately: simple shapes, big eyes, flat
cel shading, few textures, bright colours) so characters stay recognisable between
independently-generated clips and mild blur reads as soft render rather than a defect.
Presented WITHOUT AUDIO (a silent film) -- dialogue becomes SUBTITLES over the shot, every
non-voice sound / narrator line / time & place jump becomes a TITLE CARD.

Bible constitution (the director cannot violate it):
  * settings: a fixed short list. No new locations are ever introduced.
  * characters: a fixed cast of MAX_CHARACTERS, descriptors baked in. The director may
    name 1..MAX_CAST_IN_SHOT of them. If it names too few / none, we FILL the shot with
    cast members (with their descriptors). If it names a non-cast name, we REJECT it.
  * a Censor filters audience danmaku before the director, and moderates the final
    prompt before it reaches the video model.
  * HARD BAN (kept even though the show is political): no Nazi iconography or swastikas,
    no gore, no real named politicians. A generated swastika is a permanent platform ban,
    not a warning -- this line is what keeps a takedown recoverable.

STORY MEMORY -- three tiers, because a 1-2 sentence rolling summary forgets everything:
  * `chronicle`: an APPEND-ONLY list of one-line beat records. Never compressed at rest,
    so nothing is ever permanently lost. Persisted in full.
  * `digest`   : a derived summary of the OLDER chronicle entries, recomputed from the
    RAW entries (never from a previous digest), so it cannot decay recursively the way
    the old `plot` field did.
  * `arc`      : the LONG-TERM goal -- where the story is being pushed, held stable for
    many beats and only replaced when the director reports it achieved (or it times
    out). `action` is the shot that carries the next beat toward the arc.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx

from .config import Config
from .safety import Censor

# hard-coded art direction. NOTE: we ask for COLOUR anime here and convert to B&W in the
# Streamer -- T2V models are markedly better at colour, and a post grade is consistent
# across clips in a way prompt-requested monochrome never is.
STYLE = ("moe bishoujo anime key visual, light-novel illustration style, strictly 2D "
         "hand-drawn, LARGE glossy eyes with detailed irises and multiple bright "
         "catchlights, long delicate eyelashes, small refined nose and mouth, slender "
         "graceful features, smooth porcelain skin with soft blush, fine clean line art, "
         "soft gradient cel shading, beautiful youthful character design, "
         "painted backgrounds, no 3D, no photorealism, no chibi, no gag manga, "
         "no caricature, consistent character design, muted "
         "1920s palette of soot grey, ochre and oxblood, heavy dramatic side-lighting "
         "and long shadows, german expressionist staging, no text or captions")

# CHIBI preset (the new active look). Select via env ANIME_STYLE=chibi; the muted
# 1920s drama style above (`weimar`) is the backup when ANIME_STYLE is unset/weimar.
STYLE_CHIBI = ("adorable chibi Q-version anime style, big sparkling eyes, small rounded "
               "bodies with tiny limbs, soft pastel colors, clean simple outlines, flat "
               "cel shading, kawaii proportions, expressive cute faces, soft gentle "
               "lighting, painted backgrounds, no 3D, no photorealism, consistent "
               "character design, no text or captions")

# KYOANI preset (spare, Haruhi/KyoAni). Kyoto Animation: crisp clean 2D anime, polished,
# expressive. Kept as a preset, no longer the active look.
STYLE_KYOANI = ("kyoto animation (kyoani) anime style, crisp clean 2D hand-drawn, large "
                "expressive eyes with detailed irises and bright catchlights, soft refined "
                "lineart, smooth cel shading with soft gradients and gentle highlights, "
                "warm vibrant natural colors, youthful lively character design, polished "
                "high-production look, painted backgrounds, no 3D, no photorealism, "
                "consistent character design, no text or captions")

# STORYBOOK preset (the ACTIVE look): simplified 3D anime-TV / storybook animation.
# Chosen deliberately to tolerate a SMALL, reference-less model with no last-frame or
# character conditioning: simple well-defined shapes, big readable eyes, flat cel shading,
# few textures, clear silhouettes, bright flat colours, low complexity. So characters stay
# recognisable between independently-generated clips and mild blur reads as a soft render
# instead of a defect. kyoani / chibi / weimar remain as spare presets.
STYLE_STORYBOOK = (
    "simple stylized 3D animated TV series style, anime-inspired character design, "
    "clean flat cel shading, large expressive eyes, simple facial features, bright "
    "readable colors, limited texture detail, soft even lighting, clean simple "
    "composition, family-friendly animated storybook look, simple background shapes, "
    "easy-to-recognize character silhouettes, low complexity, smooth broad shapes, "
    "soft rounded forms, consistent character design, no text or captions"
)

# map env ANIME_STYLE -> style string (storybook now active; kyoani / chibi / weimar spare)
_STYLE_BY_KEY = {
    "storybook": STYLE_STORYBOOK,
    "tv": STYLE_STORYBOOK,
    "3d": STYLE_STORYBOOK,
    "default": STYLE_STORYBOOK,
    "kyoani": STYLE_KYOANI,
    "chibi": STYLE_CHIBI,
    "weimar": STYLE,
    "drama": STYLE,
}

# A SHORT fixed list of SIMPLE, REPEATABLE places -- small models hold a low-element,
# high-recognisability location far better than a busy one.
SETTINGS: dict[str, str] = {
    "village": ("a small storybook village square: cobblestone ground, a round stone "
                "well, a big old oak tree, a few cottages with thatched roofs, soft "
                "morning light"),
    "forest": ("a gentle forest path: tall simple trees on both sides, a worn dirt path, "
               "dappled sunlight, a few glowing mushrooms"),
    "lake": ("a still lakeside: calm water, a short wooden jetty, tall reeds, a green "
             "meadow shore, quiet light"),
    "ruins": ("an old stone ruin: a mossy arched gateway, a broken column, a faint "
              "mysterious glow inside"),
}
DEFAULT_SETTING = "village"

# The stream's OPENING shot (first clip): a storybook establishing shot of the village.
OPENING_PROMPT = (
    "simple stylized 3D animated storybook style, clean flat cel shading, large "
    "expressive eyes, simple facial features, bright readable colors, soft even lighting, "
    "simple background shapes, family-friendly fantasy look, no text or captions. "
    "A tiny storybook village square at dawn: a round stone well, a big old oak, "
    "thatched-roof cottages, soft golden light. A young adventurer in a green hooded "
    "cloak with auburn bob hair and a leather satchel stands by the well, looking toward "
    "the forest, while a small glowing blue wisp with tiny wings hovers by her shoulder. "
    "Peaceful, full of wonder, the start of a small adventure. "
    "One smooth slow push-in, no cuts, no text."
)

# fixed cast: name -> descriptor. EACH has >=3 STRONG symbolic identifiers (hair
# colour+style, a signature accessory, a consistent outfit colour) so an audience can
# recognise them between clips even though the model has no reference-conditioning.
CAST: dict[str, str] = {
    "Mira": ("a young adventurer hero, auburn bob hair, a green hooded cloak, a leather "
             "satchel with a brass compass, brave and curious"),
    "Pip": ("a tiny magical wisp companion, a glowing soft-blue body, tiny translucent "
            "wings, a little silver bell, floats in the air"),
    "Rowan": ("a village elder keeper, long white beard, a tall wooden staff, an ochre "
              "robe, kind and watchful"),
    "Kael": ("a mysterious stranger, pale silver hair, a black coat, a red scarf, "
             "guarded and quiet"),
}
MAX_CHARACTERS = len(CAST)
MAX_CAST_IN_SHOT = 2                # keep shots readable; <=2 per shot helps continuity
# The audience may introduce new cast. Capped; every extra face is one more the small
# model must hold consistent, so new cast must also carry >=3 strong identifiers.
MAX_TOTAL_CAST = 8

MOODS = {
    "default": "calm, storybook, gentle wonder",
    "excited": "quickening, adventurous, on the move",
    "celebration": "bright, triumphant, warm",
    "offbeat": "hushed, mysterious, a held breath",
}

# story-memory tuning
CHRONICLE_VERBATIM = 10     # most recent beats sent to the director in full
DIGEST_EVERY = 8            # recompute the digest every N beats
ARC_MAX_BEATS = 18          # force a new arc if one overstays (prevents a stuck goal)

_STATE_FILE = "story_state.json"


class Story:
    def __init__(self, work_dir: str | Path = "assets") -> None:
        self.censor = Censor()
        # art style preset: storybook (active) / kyoani / chibi / weimar via env ANIME_STYLE
        self.style = _STYLE_BY_KEY.get(os.getenv("ANIME_STYLE", "storybook").lower(), STYLE_STORYBOOK)
        self.settings = dict(SETTINGS)
        self.setting_key = DEFAULT_SETTING
        self.setting = SETTINGS[DEFAULT_SETTING]
        self.bg = ""                             # persistent background descriptor
        self.cast = dict(CAST)
        self.cast_names = list(CAST)
        self.extra_cast: dict[str, str] = {}   # audience-introduced cast
        self.mood = MOODS["default"]
        self.beats: list[str] = []
        self.opening = True                      # next call emits the establishing shot
        # -- three-tier memory (see module docstring) --
        self.chronicle: list[str] = []           # append-only, never lossy at rest
        self.digest = ""                         # derived from RAW chronicle entries
        self.digest_at = 0                       # chronicle length at last digest
        self.arc = ""                            # long-term goal, held across beats
        self.arc_beats = 0                       # how long the current arc has run
        self.card = ""                           # intertitle text (narrator voice)
        self._recent_cards: list[str] = []       # shown to the director so it
                                                 # does not reword an old card
        self._rule_i = 0                         # rotates the fallback beats
        self._since_card = 0                     # shots in a row; TOLD to the
                                                 # director so it can judge cadence
        self.changed = ""                        # director's own answer to
                                                 # "what is different now?"
        self.display_text = ""                   # when set, THIS beat is a title
                                                 # card and no video is generated
        self.subtitle = ""                       # a character's spoken line, drawn as a
                                                 # SUBTITLE over the shot (silent film)
        self.state_path = Path(work_dir) / _STATE_FILE
        self._load_state()
        # bg is always derived from setting_key (the place) -- deterministic.
        self._refresh_bg()

    # ---- back-compat: `plot` used to be the whole memory ---------------------
    @property
    def plot(self) -> str:
        """What has happened so far. Now assembled from the chronicle rather than being
        a single rewritten-every-beat sentence (which forgot everything past ~3 beats)."""
        tail = " ".join(self.chronicle[-3:])
        return (f"{self.digest} {tail}".strip() if self.digest else tail)

    # ---- persistence (stateful between runs) --------------------------------
    def _load_state(self) -> None:
        try:
            if not self.state_path.exists():
                return
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.chronicle = [str(x) for x in d.get("chronicle", [])]
            self.digest = str(d.get("digest", ""))
            self.digest_at = int(d.get("digest_at", 0) or 0)
            for n, desc in (d.get("extra_cast") or {}).items():
                self.extra_cast[str(n)] = str(desc)
                self.cast[str(n)] = str(desc)
            self.cast_names = list(self.cast)
            self.arc = str(d.get("arc", ""))
            self.arc_beats = int(d.get("arc_beats", 0) or 0)
            self.setting_key = str(d.get("setting_key", DEFAULT_SETTING))
            self.setting = self.settings.get(self.setting_key, SETTINGS[DEFAULT_SETTING])
            # migrate a pre-chronicle state file: its `plot` becomes the first digest
            if not self.chronicle and d.get("plot"):
                self.digest = str(d["plot"])
            if d.get("opening_started"):
                self.opening = False
        except Exception:
            pass

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"chronicle": self.chronicle, "digest": self.digest,
                            "extra_cast": self.extra_cast,
                            "digest_at": self.digest_at,
                            "arc": self.arc, "arc_beats": self.arc_beats,
                            "setting_key": self.setting_key,
                            "opening_started": True, "saved_at": time.time()},
                           ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ---- audience signal handling ------------------------------------------
    def _censor_signal(self, signal: dict) -> dict:
        sig = dict(signal)
        sig["superchats"] = [s for s in signal.get("superchats", [])
                             if self.censor.filter_danmaku(str(s.get("text", "")))]
        sig["top_keywords"] = [(k, v) for k, v in signal.get("top_keywords", [])
                               if self.censor.filter_danmaku(str(k))]
        sig["votes"] = [v for v in signal.get("votes", [])
                        if self.censor.filter_danmaku(str(v))]
        # raw sentences, not just word tokens: splitting "a storm rolls in" into
        # ['a','storm','rolls','in'] destroys the meaning before the director sees it.
        sig["lines"] = [t for t in signal.get("lines", [])
                        if self.censor.filter_danmaku(str(t))]
        return sig

    def _mood_from_signal(self, signal: dict) -> str:
        total = signal.get("total_superchat_amt", 0) + signal.get("total_gift_amt", 0)
        if total >= 100:
            return MOODS["celebration"]
        if total > 0 or signal.get("danmaku_count", 0) > 40:
            return MOODS["excited"]
        return MOODS["default"]

    # ---- bible constitution ------------------------------------------------
    def _resolve_chars(self, requested: list[str]) -> list[tuple[str, str]]:
        valid = [n for n in requested if isinstance(n, str) and n in self.cast]
        if not valid:
            valid = self.cast_names[:min(MAX_CAST_IN_SHOT, len(self.cast_names))]
        valid = valid[:MAX_CAST_IN_SHOT]
        return [(n, self.cast[n]) for n in valid]

    def _admit_character(self, spec: dict) -> None:
        """Admit an audience-requested character into the PERMANENT cast.

        The bible used to reject every unknown name, which meant "add von Macklenburg"
        was captured, understood, and then silently ignored -- indistinguishable from
        broken steering. Now the director supplies a visual descriptor and the character
        joins the cast, so later prompts can carry them consistently. Capped at
        MAX_TOTAL_CAST: with no reference-conditioning the descriptor is the ONLY thing
        holding a face together between clips, and every extra one dilutes the others."""
        if not isinstance(spec, dict):
            return
        name = str(spec.get("name", "")).strip()[:32]
        desc = " ".join(str(spec.get("descriptor", "")).split())[:220]
        if not name or not desc or name in self.cast:
            return
        if len(self.cast) >= MAX_TOTAL_CAST:
            print(f"[story] cast full ({MAX_TOTAL_CAST}); refusing new character {name!r}")
            return
        # the censor runs on audience-sourced names/descriptors like any other input
        if not self.censor.filter_danmaku(f"{name} {desc}"):
            print(f"[story] new character {name!r} rejected by censor")
            return
        self.cast[name] = desc
        self.extra_cast[name] = desc
        self.cast_names = list(self.cast)
        print(f"[story] AUDIENCE ADDED CAST: {name} -- {desc[:70]}")

    def _resolve_setting(self, requested: str) -> None:
        """The director may MOVE between bible settings, but never invent one."""
        key = (requested or "").strip().lower()
        if key in self.settings:
            self.setting_key = key
            self.setting = self.settings[key]
            self._refresh_bg()

    def _refresh_bg(self) -> None:
        """The CANONICAL, FIXED background descriptor for the CURRENT place.

        Two rules:
          * same place -> SAME background, always. `bg` is a pure deterministic function
            of setting_key (the place), never of run history -- so a chosen place
            reads identically every clip and across restarts.
          * a different place -> a different (still fixed) background.
        T2V has no reference-conditioning, so a repeated, identical, explicit environment
        sentence is the only lever to stop a room drifting clip-to-clip."""
        place = self.settings.get(self.setting_key, SETTINGS[DEFAULT_SETTING])
        self.bg = (f"{place}. The location is FIXED and IDENTICAL in every clip: the same "
                   f"room, the same furniture and props, the same lighting, the same "
                   f"camera position and angle. Never change the environment -- only the "
                   f"people and their action may change.")

    def _render_prompt(self, chars: list[tuple[str, str]], action: str) -> str:
        names = " and ".join(n for n, _ in chars)
        descs = "; ".join(d for _, d in chars)
        return (f"{self.style}. {self.bg} {descs}. {names}: {action}. "
                f"Mood: {self.mood}. One smooth continuous camera move, no cuts, no text.")

    # Fallback beats used when the director times out. It MUST vary: with a quiet room
    # the old single-string version aired the identical line four times in a row during a
    # run of DeepSeek ReadTimeouts, which reads as the stream being broken.
    _RULE_BEATS = (
        "the wind stirs the leaves, and Mira looks up from the path",
        "Pip drifts ahead and hovers, waiting to be followed",
        "Mira stops, turns a slow circle, and gets her bearings",
        "a bird breaks from the branches, and Mira follows it with her eyes",
        "Mira crouches, brushes moss from a stone, and reads what is under it",
        "Pip's glow dims for a moment, then steadies",
        "Mira shifts the satchel on her shoulder and walks on",
    )

    def _rule_action(self, signal: dict) -> str:
        lines = signal.get("lines") or []
        if lines:
            return f"a villager murmurs {lines[0][:60]}, and Mira looks up"
        keywords = [k for k, _ in signal.get("top_keywords", [])]
        if keywords:
            return f"{keywords[0]} catches everyone's eye by the well"
        # rotate, so consecutive fallbacks are never the same line
        beat = self._RULE_BEATS[self._rule_i % len(self._RULE_BEATS)]
        self._rule_i += 1
        return beat

    # ---- public API --------------------------------------------------------
    async def continuation_prompt(self, cfg: Config, signal: dict,
                                  use_llm: bool = True) -> str:
        # FIRST beat: play the establishing shot.
        if self.opening:
            self.opening = False
            self.card = "清晨，林间的小村庄刚刚醒来。井边的树叶轻轻响了一下，还没有人出发。"
            self.last_action = "opening establishing shot"
            self.last_chars = []
            self.last_texts = []
            self._save_state()
            # non-weimar styles lead with their own art direction; weimar keeps its
            # bespoke silent-film opening verbatim.
            return (f"{self.style}. " + OPENING_PROMPT) if self.style is not STYLE else OPENING_PROMPT

        signal = self._censor_signal(signal)
        self.mood = self._mood_from_signal(signal)
        chars: list[tuple[str, str]] = []
        action = ""
        if use_llm and cfg.llm_api_key:
            try:
                d = await self._llm_continue(cfg, signal)
                self._admit_character(d.get("new_character") or {})
                chars = self._resolve_chars(d.get("characters", []))
                self._resolve_setting(d.get("setting", ""))
                action = d.get("action", "")
                # ARC: hold it across beats; only replace when achieved or timed out.
                if d.get("arc_done") or self.arc_beats >= ARC_MAX_BEATS or not self.arc:
                    new_arc = (d.get("arc") or "").strip()
                    if new_arc:
                        self.arc = new_arc
                        self.arc_beats = 0
                if d.get("card"):
                    self.card = d["card"]
                # a title-card beat, but never two in a row -- back-to-back cards read
                # as the stream having died rather than as a deliberate device
                # trust the director's explicit choice, but a 'card' with no
                # text is not a card, and two in a row reads as a dead stream.
                gap = (d.get("needs_card") or "none").strip().lower()
                # SILENT FILM, two text channels:
                #   a CHARACTER'S OWN WORDS  -> SUBTITLE over a filmed shot.
                #   everything else heard or inferred -- off-screen/environmental
                #   sound, a narrator line, a time/place jump -> a TITLE CARD
                #   (display_text) and NO video is generated.
                # A card must be EARNED by sound/time/place; a "card" with "none"
                # was the mood-card failure that turned the broadcast into a
                # slideshow. Speech is NEVER a card -- it rides on a shot.
                is_card = d.get("beat_type") == "card" and gap in (
                    "sound", "time", "place")
                is_speech = gap == "speech"
                want = (d.get("display_text", "") or "").strip() if is_card else ""
                # Speech is ALWAYS a shot with a subtitle, never a card. If the model
                # kept the old habit (the line in display_text, no subtitle), reuse it
                # as the subtitle so the dialogue is not silently dropped.
                if is_speech:
                    sub = ((d.get("subtitle") or "").strip()
                           or (d.get("display_text") or "").strip())
                    want = ""
                else:
                    sub = "" if is_card else (d.get("subtitle") or "").strip()
                # Airing the SAME card text twice is a defect, not a directorial
                # choice; drop an exact repeat and film the beat instead. Cadence
                # stays the director's call; only literal duplication is refused.
                if want and want.strip().lower() in {
                        c.strip().lower() for c in self._recent_cards}:
                    print(f"[story] duplicate card refused: {want[:60]!r}")
                    want = ""
                self.display_text = want
                self.subtitle = sub[:120]
                self.changed = d.get("changed", "")
            except Exception as e:  # noqa: BLE001
                print(f"[story] LLM director failed ({type(e).__name__}); using rule beat")
        # telemetry only -- this NUMBER is shown to the director so it can judge its own
        # cadence; it never forces a card. The director stays in charge of the choice.
        if self.display_text:
            self._recent_cards.append(self.display_text)
            self._recent_cards = self._recent_cards[-8:]
        self._since_card = 0 if self.display_text else self._since_card + 1
        if not chars:
            chars = self._resolve_chars([])
        if not action:
            action = self._rule_action(signal)

        prompt = self._render_prompt(chars, action)
        ok, _ = self.censor.sanitize_prompt(prompt)
        if not ok:
            prompt = self._render_prompt(chars, self.censor.safe_action())

        # append-only chronicle: the record is never rewritten, only added to
        self.chronicle.append(action[:160])
        self.arc_beats += 1
        self.beats.append(action[:80])
        if len(self.beats) > 12:
            self.beats = self.beats[-12:]
        self.last_action = action
        self.last_chars = [name for name, _ in chars]
        self.last_texts = list(signal.get("lines", [])) \
            or [k for k, _ in signal.get("top_keywords", [])]
        # refresh the digest from the RAW older entries (never from a prior digest).
        # Watermark, NOT `len % DIGEST_EVERY == 0`: an exact-multiple test is skipped
        # entirely whenever a beat fails and the count steps past the multiple.
        if len(self.chronicle) > CHRONICLE_VERBATIM and \
                len(self.chronicle) - self.digest_at >= DIGEST_EVERY:
            self.digest_at = len(self.chronicle)
            try:
                self.digest = await self._llm_digest(cfg)
            except Exception as e:  # noqa: BLE001
                print(f"[story] digest skipped ({type(e).__name__})")
        self._save_state()
        return prompt

    # ---- DeepSeek director --------------------------------------------------
    def _system_prompt(self) -> str:
        return (
            "You are the director of a light D&D-style STORYBOOK FANTASY ADVENTURE set in "
            "a small village and its woods, drawn in a simple 3D anime / storybook style "
            "and presented WITH NO AUDIO (the audience hears nothing -- it is a silent "
            "film). The mood is warm, whimsical, family-friendly: a young adventurer, a "
            "glowing wisp companion, a village elder, and a quiet stranger. A mysterious "
            "event is stirring, and the audience steers where the adventure goes.\n"
            f"The world is FIXED. Settings, choose exactly one simple, recognizable place: "
            + "; ".join(f"{k} = {v}" for k, v in self.settings.items())
            + ". The cast is ALWAYS exactly these: "
            + "; ".join(f"{n} = {d}" for n, d in self.cast.items())
            + ". Never invent a new location or a new character on your own -- "
            "introduce them ONLY when the audience explicitly asks for one. Use only 1 "
            "or 2 of the cast per shot, naming them.\n"
            "NEW CAST: the audience may ask for a new character ('add a bard', 'new "
            "character: the cat'). When they do, and only then, return `new_character` "
            "with a name and a descriptor -- they become a PERMANENT cast member. Each "
            "character must carry at least THREE STRONG symbolic identifiers so the "
            "audience can recognise them between clips: a hair colour+style, a signature "
            "accessory, and a consistent outfit colour. Give every one of them. Honour "
            "the audience's spelling of the name; invent nothing unasked.\n"
            "HARD BANS, absolute: no graphic violence or gore; no explicit content; no "
            "real public figures; no scary or disturbing imagery. Keep it warm, cute and "
            "wholesome -- cozy rooms, small wonders, gentle adventure.\n"
            "THIS IS A SILENT FILM. There is NO AUDIO AT ALL -- the audience hears "
            "nothing. Every meaning they would get from SOUND must be carried by TEXT, "
            "in exactly one of two forms:\n"
            "  SUBTITLE   = a character's OWN spoken words, drawn over a filmed shot. "
            "Return `subtitle` (their exact line) and beat_type='shot'. Only a "
            "character's own words ever go here.\n"
            "  TITLE CARD = everything else heard or inferred: an off-screen/environmental "
            "sound (a distant chime, a gust of wind, footsteps, a creature's cry), a "
            "narrator line, a TIME or PLACE jump. Return `display_text` and "
            "beat_type='card'; NO video is generated -- the card IS the beat.\n"
            "LANGUAGE: everything the AUDIENCE READS must be written in SIMPLIFIED "
            "CHINESE (简体中文) -- `subtitle`, `display_text`, and `card` are all "
            "audience-facing. Write them in plain, warm, natural Chinese. The content "
            "that feeds the VIDEO MODEL -- `setting`, `characters`, `action` -- stays in "
            "ENGLISH so the image model renders cleanly.\n"
            "Rule: if it leaves a character's mouth it is a SUBTITLE (in Chinese); "
            "anything else the audience would hear or must be told is a TITLE CARD (in "
            "Chinese). Never rely on the image alone to convey something that would be "
            "heard -- if you would need audio for the point to land, put it in a subtitle "
            "or a card.\n"
            "\nYOU PLAN ONLY ONE 5-SECOND SHOT PER BEAT. There is NO multi-beat scene, "
            "NO scene goal, NO 'ends_when' -- never plan beyond the next 5 seconds. Each "
            "beat is a COMPLETE, self-contained moment that RESOLVES within those 5 "
            "seconds; when it ends the story has moved somewhere new. If the situation is "
            "not different after this beat, you have failed.\n"
            "EVERY SHOT IS EXACTLY 5 SECONDS. This is the hardest constraint and most "
            "beats get it wrong in one of two ways:\n"
            "  TOO MUCH: 'Mira draws her sword, then checks the map, then climbs the wall' "
            "-- that is three actions and needs 20s.\n"
            "  TOO LITTLE: 'Mira looks at the well' then 'Mira still looks at the well' -- "
            "nothing changed, the film stalls.\n"
            "  RIGHT: ONE decisive, everyday action, completed within 5 seconds, that "
            "CHANGES THE SITUATION. 'Mira lifts the old well's lid and a blue light spills "
            "out.' 'Kael steps out of the shadow and says nothing.' 'Pip darts through the "
            "broken archway first.' One gesture; afterwards something is different.\n"
            "Ask yourself before answering: after this beat, what has CHANGED? If the "
            "honest answer is 'nothing, it is the same moment from another angle', you "
            "have failed -- pick the next real moment instead.\n"
            "SKIP THE BORING PARTS. Do not film walking, waiting, or small handling of "
            "objects -- cut to a title card. If the next thing that matters is an hour "
            "later or across the wood, do NOT film the journey: emit a card and jump.\n"
            "THE AUDIENCE STEERS THIS SHOW. When audience messages are present and do not "
            "break the bible, the very NEXT beat must visibly answer them -- put the thing "
            "they asked for ON SCREEN, and bend the arc toward it rather than finishing "
            "your current thought first. Only ignore a message if it breaks the bible "
            "(new character, new location, banned content). This is the point of the "
            "show: a viewer must be able to SEE that what they typed changed the film.\n"
            "Return ONLY a JSON object with EXACTLY these fields (output the JSON only, "
            "no prose, no markdown):\n"
            '  "setting": one settings key (string).\n'
            '  "characters": array of cast names, 1 or 2 only.\n'
            '  "needs_card": REQUIRED, answer first. This film has NO SOUND. The next '
            "beat cannot rely on something the audience would hear, so state how it is "
            "carried -- exactly one of:\n"
            "    speech -- a character speaks something that matters. Return `subtitle` "
            "(in Chinese) with their exact words and film the shot (beat_type=shot); the "
            "line is drawn as a SUBTITLE. Most common.\n"
            "    sound -- an off-screen/environmental sound turns the beat (a distant "
            "chime, wind, footsteps, a creature's cry). TITLE CARD (beat_type=card, "
            "display_text).\n"
            "    time -- a jump forward we will not film. TITLE CARD.\n"
            "    place -- a move across the wood we will not film. TITLE CARD.\n"
            "    none -- pure visible action. Shot, no subtitle.\n"
            "  TITLE CARD beats cost nothing and take a third of a second; prefer them "
            "for environmental sound, transitions and atmosphere. Film a SHOT only when "
            "there is real visible, everyday action.\n"
            '  "beat_type": "card" when needs_card is sound/time/place, "shot" for '
            'speech and none. Speech is ALWAYS a shot with a subtitle, never a card. '
            "Never two cards in a row.\n"
            '  "action": the shot when beat_type=shot. ONE action, ONE clause, AT MOST '
            "16 words, fits in five seconds, and CHANGES something. Write it in ENGLISH. "
            "When this is a speech beat, describe the character's visible gesture as they "
            "say the line (e.g. Mira kneels by the well and says it softly). Never use "
            "';' or 'then' -- if you need them you have written two beats, so film the "
            "first and save the second for next time. CUT AHEAD to the next interesting "
            "moment; never linger on a small handling of objects across several beats.\n"
            '  "changed": under 10 words -- what is DIFFERENT after this beat that was '
            "not true before. If you cannot name a real change, your action is a "
            "re-frame of the same moment; discard it and pick the next real moment.\n"
            '  "card": one short narrator card in CHINESE, max 12 Chinese characters, '
            "used only as a standby card (e.g. 远处的钟声敲了三下，村里没人说话).\n"
            '  "subtitle": REQUIRED when a character speaks (needs_card=speech), empty '
            "otherwise. The character's EXACT spoken line in SIMPLIFIED CHINESE -- the "
            "words themselves, not a paraphrase, not narration. Max 14 Chinese characters, "
            "drawn as a SUBTITLE over the shot (e.g. 我们黎明就走，别告诉别人). Only a "
            "character's own words; narration and environmental sound go in display_text.\n"
            '  "display_text": REQUIRED when beat_type=card, empty otherwise. Written in '
            "SIMPLIFIED CHINESE, max 20 characters. This text IS the beat -- no video is "
            "generated -- so it must carry the story alone. For sound, name the sound and "
            "its effect (e.g. 远处的钟声敲了三下，村子一下子安静下来). For time/place, a "
            "narrator line crossing the gap (e.g. 三天过去，井水却越来越蓝). Never put a "
            "character's spoken words here -- that belongs in subtitle.\n"
            '  "new_character": only when the audience asks for a new character -- '
            "{name, descriptor}. Descriptor 15-25 words, concrete and visual, drawn in "
            "storybook 3D anime style, and MUST name at least THREE strong identifiers "
            "(hair colour+style, a signature accessory, an outfit colour) so the audience "
            "can recognise them.\n"
            '  "arc_done": true only if the long-term arc below is now resolved.\n'
            '  "arc": only when arc_done is true or no arc exists -- the next long-term '
            "goal spanning many beats.\n"
        )
    async def _llm_continue(self, cfg: Config, signal: dict) -> dict:
        keywords = [k for k, _ in signal.get("top_keywords", [])]
        lines = signal.get("lines", [])
        sc_text = " ".join(s.get("text", "") for s in signal.get("superchats", []))
        recent = self.chronicle[-CHRONICLE_VERBATIM:]
        user = (
            f"THE STORY SO FAR (earlier): {self.digest or 'the film has just opened'}\n"
            f"RECENT BEATS, oldest first:\n"
            + ("\n".join(f"  {i+1}. {b}" for i, b in enumerate(recent)) or "  (none yet)")
            + f"\n\nLONG-TERM ARC (hold this; it spans many beats): "
            f"{self.arc or '(none set -- invent one and return it in `arc`)'}\n"
            f"Beats spent on this arc: {self.arc_beats} (soft limit {ARC_MAX_BEATS}).\n"
            f"Mood: {self.mood}. Current setting: {self.setting_key}.\n"
            # The director always has a good next shot in mind and cannot see its own
            # cadence, so it chose "shot" 16 times out of 16. This is not a nudge toward
            # a quota -- it is the fact a real director would know, so it can judge.
            f"Title cards already used -- NEVER repeat one of these, and never restate "
            f"the same idea in new words: {self._recent_cards[-6:] or '(none yet)'}\n"
            f"Shots since the last card: {self._since_card}.\n"
            f"THE LAST BEAT WAS: {self.beats[-1] if self.beats else '(the opening)'}.\n"
            f"  The next beat MUST be a DIFFERENT moment -- do not re-film that, do not "
            f"reword it, do not stay with the same prop or the same two people doing "
            f"another small gesture. Advance to the next real event. You plan ONLY this "
            f"one 5-second beat; it must resolve inside 5 seconds.\n"
            f"AUDIENCE (they steer the story; honour it when it fits the world, "
            f"ignore it when it breaks the bible): messages={lines[:6]}; "
            f"keywords={keywords[:5]}; superchat={sc_text[:160]!r}\n\n"
            "Write the NEXT beat. It must be a NEW event that carries the chronicle "
            "toward the arc -- not a repeat, not a restatement. Output only the JSON."
        )
        # 420 was too small once the schema grew (beat_type/changed/display_text/
        # new_character): replies truncated mid-JSON, parsing failed, and the beat fell
        # back to the formulaic rule action -- which is what put repeats back on screen.
        # grew again with scene + needs_card; truncation shows up as rule-beat
        # fallbacks ("a tram bell sounds somewhere unseen") on random beats
        content = await self._chat(cfg, self._system_prompt(), user, max_tokens=700)
        # runtime logging: print the prompt the director got and its output, every beat.
        if not self.beats:
            print(f"[director] ===== SYSTEM PROMPT (once) =====\n{self._system_prompt()}")
        b = len(self.beats) + 1
        print(f"[director] ===== BEAT {b}: PROMPT >>> =====\n{user}")
        print(f"[director] ===== BEAT {b}: OUTPUT <<< =====\n{content.strip()}")
        return self._parse_director(content)

    async def _llm_digest(self, cfg: Config) -> str:
        """Summarise the OLDER chronicle entries from the RAW records.

        Deliberately re-derived from source every time rather than summarising the
        previous digest -- that recursion is exactly what made the old `plot` field
        forget everything within a few beats."""
        old = self.chronicle[:-CHRONICLE_VERBATIM]
        if not old:
            return self.digest
        body = "\n".join(f"{i+1}. {b}" for i, b in enumerate(old))
        content = await self._chat(
            cfg,
            "You compress a silent film's story record. Return 3-5 sentences of plain "
            "prose: what has happened, who changed, what is unresolved. Keep concrete "
            "details (objects, places, decisions). No preamble, no JSON.",
            f"Beats 1..{len(old)} of the film:\n{body}",
            max_tokens=320)
        return content.strip()

    async def _chat(self, cfg: Config, system: str, user: str, max_tokens: int,
                    timeout: float = 20.0, attempts: int = 2) -> str:
        url = cfg.llm_base_url
        if not url.endswith("/chat/completions"):
            url = url.rstrip("/") + "/chat/completions"
        last: Exception | None = None
        for i in range(attempts):
            try:
                return await self._chat_once(url, cfg, system, user, max_tokens, timeout)
            except Exception as e:  # noqa: BLE001
                last = e
                if i + 1 < attempts:
                    print(f"[story] director {type(e).__name__}; retrying once")
        raise last  # type: ignore[misc]

    async def _chat_once(self, url: str, cfg: Config, system: str, user: str,
                         max_tokens: int, timeout: float) -> str:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
                             json={"model": cfg.llm_model,
                                   "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": user}],
                                   "temperature": 0.9, "max_tokens": max_tokens})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_director(content: str) -> dict:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                chars = obj.get("characters", [])
                return {
                    "setting": str(obj.get("setting", "")).strip(),
                    "characters": chars if isinstance(chars, list) else [],
                    "action": str(obj.get("action", "")).strip(),
                    "card": str(obj.get("card", "")).strip(),
                    "needs_card": str(obj.get("needs_card", "none")).strip().lower(),
                    "beat_type": str(obj.get("beat_type", "shot")).strip().lower(),
                    "changed": str(obj.get("changed", "")).strip(),
                    "display_text": str(obj.get("display_text", "")).strip(),
                    "subtitle": str(obj.get("subtitle", "")).strip(),
                    "new_character": obj.get("new_character") or {},
                    "arc": str(obj.get("arc", "")).strip(),
                    "arc_done": bool(obj.get("arc_done", False)),
                }
            except json.JSONDecodeError:
                pass
        # fallback: treat the whole content as the action
        return {"setting": "", "characters": [], "action": content.strip().strip('"'),
                "card": "", "display_text": "", "subtitle": "", "new_character": {},
                "beat_type": "shot", "changed": "", "needs_card": "none",
                "arc": "",
                "arc_done": False}
