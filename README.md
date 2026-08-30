# Infinity-Live

**TL;DR:** [alex-remade/infinte-tv](https://github.com/alex-remade/infinte-tv), but cheap
— running on FastWan, trading quality for cost.

Realtime AI video generation for bilibili live, driven by danmaku.

An LLM director writes the next beat, a text-to-video model draws it, and the result is
pushed as one unbroken RTMP stream. Viewers steer the story from chat: they can introduce
characters, cause events, and see the film answer within a beat.

Chinese README: [README-ZH.md](README-ZH.md)

Inspired by [alex-remade/infinte-tv](https://github.com/alex-remade/infinte-tv), which
does chat-driven realtime AI TV with LTX Video (locally, or LTX 2.3 Fast on fal.ai). This is an independent
implementation, not a fork — different models, different platform, different approach to
the latency problem. See [Acknowledgments](#acknowledgments).

## Samples

Frames from a live run. The show is a storybook fantasy adventure; the cast, world and art
style are all configuration.

![A shot with a burned-in subtitle](docs/img/sample-shot.jpg)

*A shot with a dialogue subtitle. There is no audio, so spoken lines are burned over the
picture.*

![A wider scene](docs/img/sample-scene.jpg)

*Characters hold together across independent clips only because their visual descriptors
are injected into every prompt.*

![A title card](docs/img/sample-card.jpg)

*A title card. The director emits one when the next beat is a time skip, a journey or a
sound it cannot film. Cards render locally in about half a second and cost nothing.*

## Approach

[alex-remade/infinte-tv](https://github.com/alex-remade/infinte-tv) runs LTX Video — either
a local LTX v1 pipeline, or fal.ai's hosted **LTX 2.3 Fast, a 22B model**. That is the
quality-first bet, and a big model billed per generation is expensive to leave running.

This project takes the opposite bet: **FastWan 1.3B QAD** — about seventeen times smaller —
at roughly **$7 per hour** of airtime ($0.0125 per 5-second clip, less when the director
uses title cards).

The problem is that FastWan has no audio and its quality is low. Four things make it
enough, and the fourth is the one people miss:

- **Title cards.** When the next beat is a time skip, a journey, or a sound, the director
  emits an intertitle instead of a shot. A card renders locally in about 0.5 seconds and
  costs nothing, so it doubles as a latency buffer when generation falls behind. This is
  not mandatory — the director decides per beat.
- **Subtitles.** There is no audio, so spoken lines are burned over the shot rather than
  voiced. Dialogue costs a font, not a TTS call.
- **Black and white grade.** Grain and contrast hide the clip-to-clip character drift that
  colour makes obvious, which is the main visible weakness of stitching independent clips.
- **A setting chosen to hide the model's weaknesses.** The storybook fantasy is not a
  taste decision, it is a mitigation. A small model is bad at fine detail, text, hands,
  crowds, hard straight edges and anything with a real-world referent a viewer can check.
  So the world is a village, a forest, a lake and some ruins: organic shapes with no
  signage, no vehicles, no architecture that has to be correct. The art style is simple
  and flat, with large eyes and few details per character, which is exactly what the model
  renders reliably. Pick a setting the model is already good at and the same weights look
  far better. Choosing a period city with uniforms, printed posters and street signage would
  expose every weakness at once.

A fifth difference is structural rather than cosmetic. Upstream has a director too —
its system prompt opens "You are the writer and director of an ongoing animated story" —
but it is tuned for *novelty*: it is told to "introduce NEW: location, character, object,
or event" and to use "dramatic transitions (explosions, portals, sudden changes)", working
from a rolling window of the last ~100 seconds of prompts.

This project does the opposite, because it has to. A 22B model can render a brand-new
location convincingly; a 1.3B model cannot. So instead of a prompt generator that invents,
there is a **show bible** the director works inside:

- **A fixed cast.** Every character has a visual descriptor injected verbatim into every
  prompt they appear in. The director may not invent people; the audience can add them,
  capped, and then they are permanent too.
- **A fixed set of locations.** Four of them. The director chooses between them and can
  never improvise a fifth.
- **Persistent story memory** instead of a rolling window: an append-only chronicle, a
  digest re-derived from the raw entries, and a long-term arc that survives restarts.
- **A director that plans scenes**, not just the next shot — each scene has a goal and a
  stated ending it drives toward.

It is less a prompt generator and more a small show runner: same world, same faces, every
beat, for as long as the stream is up. That is what makes a weak model watchable for hours
rather than seconds.

The rest is what it takes to run continuously on bilibili:

- **Continuous single-connection RTMP.** Independent clips are concatenated as one raw
  H.264 elementary stream, so the receiver never sees the stream end.
- **Danmaku instead of Twitch chat**, read over bilibili's live WebSocket and pushed
  through 直播姬 (livehime).
- **Story memory.** A three-tier chronicle, digest and arc structure so the plot holds
  together over hundreds of beats.

## Features

- Continuous RTMP output from discrete clips, with no reconnect between them
- LLM director that chooses per beat whether to film a shot or cut to a title card
- Audience steering: add characters, cause events; out-of-world requests are refused
- Show bible: fixed cast, fixed settings, per-character visual descriptors
- Swappable art style, cast and world without touching any code outside one module
- Optional debug overlay burning the director's plan onto the picture

## Architecture

```
bilibili danmaku (WebSocket)
        |
        v
  audience signal  ---->  LLM director  ---->  beat: shot, or title card
   (decays, 90s)          chronicle / arc /            |
                          scene memory                 v
                                              text-to-video (5s clip)
                                                       |
                                                       v
                                      grade + subtitle + optional overlay
                                                       |
                                                       v
                     one long-lived ffmpeg ----> RTMP ----> livehime ----> bilibili
```

## Core Components

- `src/infinity_live/story.py` — the show bible and the LLM director. Art style, cast,
  settings, premise, and the three-tier story memory all live here.
- `src/infinity_live/continuous.py` — the run loop. Director queue, generation buffer,
  and the live pusher.
- `src/infinity_live/streamer.py` — ffmpeg. Elementary-stream publisher, silent-film
  grade, title-card and subtitle rendering.
- `src/infinity_live/video_client.py` — text-to-video providers (DeepInfra, Seedance,
  Wan, mock).
- `src/infinity_live/danmaku/` — bilibili live WebSocket reader and a mock source.
- `src/infinity_live/safety.py` — danmaku filter and prompt moderation.
- `src/infinity_live/journal.py` — append-only record of audience text to prompt to clip.

## Quick Start

### Prerequisites

- Python 3.11
- FFmpeg on PATH, built with `libx264`, `drawtext` (freetype), and the `setts` bitstream
  filter. FFmpeg 7.0 or newer is required for `setts`.
- 直播姬 (livehime), the bilibili broadcaster app, running on a reachable host. It is the
  RTMP receiver that forwards to bilibili.
- A CJK-capable font for subtitles and title cards. On Windows the code looks for
  `msyh.ttc`, `simhei.ttf` and `palab.ttf`, falling back through them.
- API keys for a text-to-video provider and an LLM.

### 1. Clone and Setup

```bash
git clone <this-repo>
cd Infinite_Live
uv pip install --python .venv/Scripts/python.exe -e .
```

### 2. Environment Configuration

```bash
cp .env.example .env
```

Minimum working configuration:

```ini
DEEP_INFRA_API=...          # text-to-video provider
DEEPSEEK_API_KEY=...        # director LLM
BILI_ROOM_ID=...            # your live room id
BILI_DANMAKU_MODE=selfhosted
BILI_PUSH_URL=rtmp://<livehime-host>:1935/live
BILI_STREAM_KEY=livehime
```

### 3. Start livehime First

livehime only goes live once it *receives* a stream, so the order matters:

1. Start livehime and leave it waiting for a stream
2. Start this application
3. livehime receives the push and the room goes live

### 4. Run

```bash
.venv/Scripts/python.exe -m infinity_live.cli stream \
    --provider deepinfra --danmaku selfhosted --room <ROOM_ID>
```

Other subcommands:

```bash
# one real generation, to verify credentials
.venv/Scripts/python.exe -m infinity_live.cli probe

# offline render of N clips to a file, no streaming
.venv/Scripts/python.exe -m infinity_live.cli stream --clips 5 --out demo.mp4
```

Delete `assets/story_state.json` to begin a new story from the opening shot. Leave it in
place to continue an existing one.

## Making It Your Own Show

The shipped show is a storybook fantasy adventure. It is only a configuration. Everything
that defines it lives in `src/infinity_live/story.py`.

### The cast

`CAST` maps a name to a visual descriptor that is injected verbatim into every prompt the
character appears in. The video model has no reference conditioning, so this descriptor is
the only thing keeping a character recognisable between clips. Make it concrete and
repeatable: silhouette, hair, one distinctive prop.

```python
CAST: dict[str, str] = {
    "Mira": ("a young adventurer hero, auburn bob hair, a green hooded cloak, a leather "
             "satchel with a brass compass, brave and curious"),
    "Pip":  ("a tiny magical wisp companion, a glowing soft-blue body, tiny translucent "
             "wings, a little silver bell, floats in the air"),
}
```

Descriptors override the global art style. With a moe style set, a character described as
`"a gaunt man in his fifties"` still renders realistically while everyone else is moe. When
you change the art style, re-read every descriptor for words that fight it.

- `MAX_CAST_IN_SHOT` (default 2) caps characters per shot. More than two hurts consistency.
- `MAX_TOTAL_CAST` (default 8) caps how many the audience may add.

### The world

`SETTINGS` is a fixed, short list of places. The director may move between them but can
never invent one, because a known location renders far better than an improvised one.

```python
SETTINGS: dict[str, str] = {
    "village": "a small storybook village square: cobblestone ground, a round stone well...",
    "forest":  "a gentle forest path: tall simple trees, a worn dirt path, dappled sunlight...",
}
DEFAULT_SETTING = "village"
```

### The art style

Presets live at the top of `story.py` and are selected with the `ANIME_STYLE` variable:

- `storybook` (default) — soft 3D-anime storybook. Aliases: `tv`, `3d`, `default`
- `kyoani` — crisp clean 2D
- `chibi` — Q-version, big sparkling eyes
- `weimar` — muted 1920s drama, expressionist lighting. Alias: `drama`

An unrecognised value falls back to `storybook`.

```bash
ANIME_STYLE=kyoani .venv/Scripts/python.exe -m infinity_live.cli stream ...
```

To add your own, write the string and register it in `_STYLE_BY_KEY`. Two things that
matter in practice:

- Be specific and use negatives. `"moe anime style"` alone is too weak an anchor, and
  `"soft rounded faces"` drags the model toward chibi gag-manga. Naming a register such as
  `light-novel illustration` or `key visual`, plus explicit `no chibi, no caricature`,
  works far better.
- Ask for colour. These models handle colour better than monochrome, so generate in colour
  and grade to black and white downstream with `SILENT_FILM=1` if you want a period look.

### The premise

`Story._system_prompt()` opens with a paragraph describing the show to the director: genre,
tone, who the characters are, what is at stake. Rewrite that paragraph and the story
changes. It also carries the hard content bans; keep them and adapt them to your setting.

### The opening shot

`OPENING_PROMPT` is the first clip of a fresh story.

## Configuration

Art and presentation:

- `ANIME_STYLE` (default `storybook`) — art style preset
- `SILENT_FILM` (default `1`) — black and white grade, grain, vignette. `0` for colour
- `SILENT_GRAIN` (default `12`) — grain amount. Higher hides clip-to-clip character drift
  at the cost of bitrate
- `SILENT_FLICKER` (default `0`) — projector flicker, off by design. An early version
  pulsed at 6 Hz and 17 Hz, inside the photosensitive band
- `DEBUG_OVERLAY` (default `1`) — burns the director's plan onto the picture. Viewers see
  this; set `0` for a clean broadcast

Generation and streaming:

- `DEEPINFRA_MODEL` (default `FastVideo/FastWan-QAD-FP8-1.3B`) — the video model.
  `FastVideo/FastWan2.2-TI2V-5B-FullAttn-Diffusers` benchmarked at the same latency,
  slightly cheaper, and noticeably better at holding the art direction
- `BUFFER_TARGET` (default `3`) — clips generated ahead
- `SIGNAL_WINDOW_S` (default `90`) — how long an audience comment stays in the signal
- `STREAM_WIDTH` / `STREAM_HEIGHT` / `STREAM_FPS` (default `832` / `480` / `16`) — the
  canonical encode. Every segment must match, or the decoder re-initialises mid-stream

## Project Structure

```
src/infinity_live/
    cli.py                    entry point and subcommands
    config.py                 environment-driven configuration
    continuous.py             run loop: director queue, buffer, live pusher
    story.py                  show bible, LLM director, story memory
    streamer.py               ffmpeg: publisher, grade, cards, subtitles
    video_client.py           text-to-video providers
    events.py                 audience signal aggregation
    journal.py                append-only record of prompts and clips
    safety.py                 danmaku filter and prompt moderation
    danmaku/
        bilibili_websocket.py bilibili live WebSocket reader
        mock_source.py        offline fake audience
```

Runtime output is written to `assets/` and is not tracked.

## Troubleshooting

**No danmaku is received, but the connection looks healthy.** The bilibili reader must send
the guest `buvid` device fingerprint inside the WebSocket auth body, not only in the HTTP
headers. Without it the platform accepts the socket, delivers every room event, and
silently omits the comments. A `LOG_IN_NOTICE` message is informational, not a refusal; no
login cookie is required.

**No danmaku while the room is offline.** bilibili does not broadcast danmaku for an
offline room. The reader connects anyway and reconnects when the room goes live.

**Characters shake or blink.** Clips with B-frames have decode order different from display
order. Regenerating timestamps from the packet index then presents frames at the wrong
instant. Segments are re-encoded with `-bf 0` so decode order is display order.

**The receiver shows a loading spinner between clips.** Something is publishing each clip
as its own RTMP connection. Set `CONTINUOUS_PUSH=1` so a single long-lived publisher spans
every clip.

**Connection refused pushing to livehime.** livehime must be running and waiting for a
stream before the application starts.

## Known Limits

- The default video model is fixed at 5-second clips. Longer clips exist only on models
  roughly ten times the price.
- There is no reference conditioning, so faces drift between clips. Descriptors and the
  grade are what make this tolerable.
- The bilibili danmaku reader is unofficial and may break if the platform changes.
- Cost scales with airtime: roughly $0.01 per 5 seconds of video, less when the director
  chooses title cards.

## License

Vibe coded this shi. All glory to my buddies Liang Wenfeng and Amodei.

## Acknowledgments

- [alex-remade/infinte-tv](https://github.com/alex-remade/infinte-tv) — the project that
  inspired this one. It demonstrates chat-driven realtime AI TV with LTX Video on FAL, and
  its README states the work is licensed under the MIT License with no copyright notice as of writing. No code from it is used
  here; the debt is to the idea. Thank you to @alex-remade for publishing it.
- [FastVideo](https://github.com/hao-ai-lab/FastVideo) — the FastWan models used here
- [DeepInfra](https://deepinfra.com) — text-to-video inference
- [DeepSeek](https://www.deepseek.com) — the director LLM
- [FFmpeg](https://ffmpeg.org) — everything to do with video
