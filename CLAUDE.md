# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Guidance for Claude Code working in teslacam-studio. See `README.md` for the user-facing docs.

## What this is
A macOS tool that combines a Tesla Sentry/Dashcam event folder (6 per-camera
`YYYY-MM-DD_HH-MM-SS-<angle>.mp4` clips) into one labeled grid video with a
burned-in clock, optional face blur, and an optional live GPS route-map overlay.

- `tesla_combine.py` — the main compositor (concat → grid via ffmpeg filter_complex).
- `tesla_gps.py` — standalone SEI→GPX/telemetry extractor (stdlib only, no deps).
- `playcheck.sh` — headless playback sanity checks for an output mp4.

## Environment & setup
- **macOS**, uses Apple VideoToolbox (`h264_videotoolbox`) for hardware encode.
- Needs **ffmpeg with `drawtext`** → `brew install ffmpeg-full` (Homebrew's plain
  `ffmpeg` lacks libfreetype). The script auto-detects
  `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` and falls back to PATH.
- `--map` needs **gopro-overlay in `./.venv`** (Python 3.10+), not committed:
  ```bash
  python3.12 -m venv .venv && ./.venv/bin/python -m pip install gopro-overlay
  ```
  Gotcha: if the repo path contains spaces the venv console-script shebang breaks,
  so the code invokes gopro as `./.venv/bin/python ./.venv/bin/gopro-dashboard.py`.
- `--blur-faces` needs `deface` (`pip install deface`). Optional.
- **Test footage lives OUTSIDE the repo** (Tesla clips on an external drive). Ask
  the user for a path; don't expect sample clips in the repo. `.gitignore` blocks
  all `*.mp4/*.gpx/*.csv` so footage/outputs never get committed.

## Run / verify
- `python3 tesla_combine.py <event-folder>` (add `--map`, `--trim-start/-end`,
  `--feature`, `--speed`, `--blur-faces`, `--dry-run`).
- `python3 tesla_gps.py <folder> --probe` — which clips carry GPS telemetry.
- `--dry-run` prints every ffmpeg command without running — use it to sanity-check
  layout/filter changes cheaply.
- `./playcheck.sh <out.mp4>` — decode integrity, faststart, hw-decodable dims, CFR.

## Architecture & key decisions
- **GPS from SEI:** Tesla embeds per-frame telemetry (lat/lon/speed/heading/
  steering/gear/Autopilot/accel) as SEI NAL units (type 6, payloadType 5, a
  4-byte `42 42 42 69` prefix then a protobuf `SeiMetadata`) *inside* the H.264
  bitstream — not a separate stream, so ffprobe won't show it. `tesla_gps.py`
  hand-decodes the protobuf wire format (no protobuf lib). **Requires firmware
  2025.44.25+ / HW3+ and clips recorded while DRIVING** (parked Sentry clips have
  none). SEI can start/stop mid-clip, so each sample's time is anchored to its
  true video `frame_index`, not the SEI ordinal.
- **Grid:** per-camera lossless concat (cached in `.tesla_combine_cache.json`,
  keyed by `CONCAT_CACHE_VERSION` — kept separate from `SCRIPT_VERSION` so feature
  bumps don't invalidate concats), then `hstack`/`vstack` with per-input
  `fps=OUTPUT_FPS` normalization, a burned-in clock, and a hw-fit scale to stay
  ≤4096px (VideoToolbox limit).
- **Map tile (`--map`):** extract GPS from the front (or first available) source
  clips → **re-time onto the CONCATENATED grid timeline** (`sum of prior clip
  durations + frame_index/fps`) so it stays synced when the grid squeezes out
  recording gaps → GPX → gopro-overlay `moving_journey_map` (follows the car AND
  draws the route) → composited as a virtual, label-less tile keyed
  `MAP_TILE_KEY`, paired with the back camera via `inject_map_row`. The grid's own
  `setpts`/`fps` filter scales every tile (map included) together, so `--speed`
  stays locked. GPS comes from ORIGINAL source clips (SEI isn't in the concat/
  deface outputs).
- **`--map-zoom` (OSM 1–19, default 19) + `--map-mag` (1.0–4.0, default 2.0):**
  OSM tiles cap at z19 (~358m across the small grid cell); `--map-mag` renders the
  map smaller then upscales to fill the tile — tighter/navigation-style but softer.
- **Design constraint:** cameras are NOT frame-locked on `frame_seq_no` — extract
  GPS from the specific camera being overlaid; don't join cameras on it.
- **Progress (`Progress`, stdlib only):** the run is planned up front as a list of
  `Step(kind, label, work)` (`plan_steps`), `work` in footage-seconds. Fractions
  are never timer guesses — ffmpeg is run with `-progress pipe:1 -loglevel error`
  and its `out_time` parsed, `deface`'s tqdm counter is regex'd, the GPS pass uses
  its own loop counter. Job ETA = sum of `work/rate[kind]`, where each kind's rate
  is replaced by the measured one when the first step of that kind ends
  (`end(work=...)` also corrects the footage estimate from the probed concat).
  Quiet-by-default means **children are captured**: `fail_child` must keep printing
  the tail of that capture, or a failure becomes invisible. `--verbose` restores
  the old raw-output path; `--dry-run` takes it too.
- **Interrupts:** Ctrl-C removes the half-written file the running step was
  producing (`Progress.out`) and reports resumability; the concat cache is saved
  after *each* camera so that resume is real. Ctrl-T (SIGINFO) prints a status
  line, which is the only progress you get under `--verbose`.
- **Conventions:** opt-in feature flags keep default runs lean; `die()`/`log()`/
  `run()` helpers; every path works under `--dry-run`.

## Verification status
- `tesla_gps.py` decoder: independently verified frame-accurate (byte-for-byte
  vs an independent reimplementation).
- `--map` re-timing sync: verified frame-accurate across multi-clip/gap/trim/speed.
- **Open:** the `--map-mag` magnification was functionally tested (dry-run,
  validation, real render, frame inspection) but did NOT go through the adversarial
  review loop the rest of the code did.

## Gotchas
- Never commit footage or rendered outputs.
- The encode path is macOS/VideoToolbox-specific.
- `report_gap` reports clock drift from squeezed recording gaps; fixed to work
  under `--trim-end` too.
- Anything printed mid-run must go through `log()` — it clears and redraws the
  progress display around the print. A bare `print()` will be scribbled over.
