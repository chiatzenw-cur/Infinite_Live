# GPU / self-host decision record

**Context:** the cloud video APIs (Seedance 1.0/2.0, Wan) were all too slow or
too expensive for a per-minute live loop. FastVideo's open-weight models are the
candidate path. The key truth: **cheap-4090 realtime ≠ FastH3.**

## Model → hardware reality

| Model | Params | Needs | 5s clip | 15s clip | Real-time? |
|---|---|---|---|---|---|
| **FastWan-QAD-FP8-1.3B** | 1.3B | **1× RTX 4090** | ~3.4s | n/a (5s cap) | ✅ but 480p/5s/T2V |
| FastWan-QAD-1.3B (NVFP4) | 1.3B | 1× RTX 5090 (Blackwell) | 1.78s | n/a | ✅ but 480p/5s/T2V |
| FastWan-QAD-1.3B-SA2 | 1.3B | 1× RTX 5090 | ~2.0s | n/a | ✅ 480p/5s/T2V |
| **FastH3 v1 (Preview, 4-step)** | 35B BF16 | **2/4/7/8× GPU** (FSDP, 56 heads), Blackwell-tuned | 16.2s (1×B200) | 47.2s (1×B200) | 4×B200≈0.97×, 8×B200≈1.16× |
| FastH3 on DGX Spark/GB10 | 35B | 1× GB10 | ~902s | — | ❌ useless |

| FastH3 can't fit a 4090 (~70GB transformer weights); it needs multi-GPU sharding
and the Blackwell VSA/FA4 kernels. Renting a 4090 gives real-time **only** for
the 1.3B model.

## Measured on 1× RTX 4090 (AutoDL, FastWan-QAD-FP8-1.3B, 5s/480p, 3 steps)

Ran via the single-process executor (`scripts/single_executor.py`) — the stock `mp`
executor dies on AutoDL's seccomp (`pidfd_getfd: Operation not permitted`), so we
bypass the subprocess entirely. `text_encoder_cpu_offload=True` (UMT5-xxl ~23GB on
CPU only at prompt-encode; not on the per-clip critical path).

| Run | Denoise (3 steps) | Generation | End-to-end | Decoder | Notes |
|---|---|---|---|---|---|
| 1st call (warmup) | 3.07s/it (~9.2s) | 51.40s | 51.49s | full Wan VAE | pays torch.compile(DiT+VAE) + model load — NOT steady state |
| **2nd call (steady)** | **1.74s/it (~5.2s)** | **17.98s** | **~19.7s** | full Wan VAE | `video_samples/raccoon_fp8_tensor_compile.mp4` saved ✅ |
| 3rd run — warmup | 6.42s/it (~19s) | 25.77s | 25.77s | **TAEHV** | re-load + taehv init |
| **4th run (steady)** | **1.95s/it (~5.9s)** | **11.25s** | **11.25s** | **TAEHV** | `Saved TAEHV-decoded video …` ✅ |

**With TAEHV the VAE decode bottleneck (~13s) disappears:** steady e2e fell from
**19.7s → 11.25s** per 5s clip, and the `[FP8_TENSOR_COMPILE] 3 steps in 14.43s` FP8
timer confirms it. **The remaining gap to the official ~3.4s/5s is the attention
backend, not the VAE** — this box builds with `TORCH_SDPA` (no `fastvideo-kernels` /
flash-attn / sage-attn / nvcc). To reach ~3.4s you must build `fastvideo-kernels`
(`fastvideo-kernels/./build.sh`), which needs a CUDA toolchain (`nvcc`) the AutoDL
fp image lacks. This is a **quality-of-life optimization**, not a blocker.

**Pacing for the live loop:** 11.25s/clip × 3 = ~34s for a 15s beat. That does NOT
fit a 30s window (15s play + 15s vote). Options: (a) build fastvideo-kernels → ~18s
per beat (fits), (b) lengthen the beat to ~60s (play 15s + ~45s vote/generate, using
a pre-rolled interstitial to bridge), or (c) batch 3 clips in one forward.

## Final 4090 verdict (after the source-build + kernel hunt)

| Setup (steady, 5s/480p) | Latency | Realtime | Note |
|---|---|---|---|
| PyPI fastvideo + `TORCH_SDPA` + taehv | **11.25s** | 0.44× | **best 4090 result** |
| Source fastvideo + compiled kernels + `VIDEO_SPARSE_ATTN` | **15.7s** | 0.32× | **slower** — VSA hurts at 5s clips |

- **flash-attn has no wheel for torch 2.12** (menu caps at 2.9); source build failed
  (it tried to fetch a nonexistent torch2.12 wheel). `FLASH_ATTENTION_FORCE_BUILD=TRUE`
  was the untried lever.
- **`ATTN_QAT_INFER` is Blackwell-only** (`sm_120a/sm_121a` via build.sh; `sm_100a/sm_103a`
  via flash-attention-fp4). 4090 is `sm_89` → `kernel=none`, unusable.
- **PyPI `fastvideo` ships `kernel=none`**; the kernels only appear after a from-source
  install (`uv pip install -e .`).
- **Verdict: the 4090 cannot hit the official 3.4s on this stack** (it needs FlashAttention,
  unbuildable for torch 2.12). It tops out ~11.25s/5s = 0.44×, **not no-pause**.
  → **Decision: move to RTX 5090 + FastWan-QAD-1.3B (NVFP4) + `ATTN_QAT_INFER`** for
  1.78s/5s = 2.8× realtime. See `docs/MIGRATION_TO_5090.md`.

## Rental economics (approx, ~2026)

| Setup | $/hr | What it buys |
|---|---|---|
| 1× RTX 4090 (AutoDL / 恒源云, ¥) | ~¥1.5–2.5 | FastWan-QAD-FP8 baseline |
| 1× B200 (RunPod) | $6.79 | FastH3 15s in ~47s → fits a 1-min beat if vote closes late |
| 4× B200 | $27.16 | FastH3 ~0.97× realtime |
| 8× B200 | $54.32 | FastH3 >1× realtime |
| 4× H100 SXM | $13.16 | untested for FastH3 (no published latency) |

## The critical blocker (for both models)

Neither FastH3 v1 nor FastWan-QAD supports **reference-conditioned generation**
(FastH3 v1 is T2AV-only; Ref2VA not distilled). Persistent characters
(Mara/Elias across 20 beats) will **drift**. This, not speed, is the current
hard problem. Options: (a) a reference-capable renderer for character lock, or
(b) embrace the drift as a feature ("the host morphs with every vote").

## Licensing

FastH3 inherits the **MiniMax H3 community license**: commercial use allowed
below $20M revenue in the applicable territory, but **excludes US / EU / UK / SK**;
commercial interfaces must display "MiniMax H3". **China is not excluded** →
fine for the bilibili product. FastWan-QAD derives from Wan2.1 (Apache-2.0), so
the weights are permissively licensed (verify the model card before shipping).

## Plan

1. Rent a cheap 4090 → `bash scripts/bench_4090.sh` → validate loop timing (near-zero cost).
2. One run on 1× / 4× B200 (RunPod) → FastH3 quality/audio/15s decision.
3. Solve identity persistence separately before committing to a single renderer.
