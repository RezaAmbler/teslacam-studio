# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Guidance for Claude Code working in teslacam-studio. See `README.md` for the user-facing docs.

## What this is
A macOS tool that combines a Tesla Sentry/Dashcam event folder (6 per-camera
`YYYY-MM-DD_HH-MM-SS-<angle>.mp4` clips) into one labeled grid video with a
burned-in clock, optional face blur, an optional live GPS route-map overlay,
an optional speed/compass dashboard overlay, and an optional FSD streak
scoreboard overlay.

- `tesla_combine.py` — the main compositor (concat → grid via ffmpeg filter_complex).
- `tesla_gps.py` — standalone SEI→GPX/telemetry extractor (stdlib only, no deps).
- `tesla_fsd_overlay.py` / `tesla_fsd_metrics.py` — FSD showcase overlay driver
  (must run under `./.venv`) and its pure-Python metric derivation.
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
  bumps don't invalidate concats), then two composable layouts (`build_filter`
  default / `build_filter_landscape` for `--landscape`, chosen by `build_grid`)
  with per-input `fps=OUTPUT_FPS` normalization, a burned-in clock, and a hw-fit
  scale to stay ≤4096px (VideoToolbox limit). Default is a tall `hstack`/`vstack`
  of rows top to bottom, which can push canvas *height* past the cap on a full
  6-camera+map session — forcing a whole-canvas downscale that also softens the
  featured camera. `--landscape` avoids this structurally: the hero tile(s) stay
  at native resolution with no scale filter at all, and every other present
  camera (+ map) is stacked single-file into a thin sidebar column
  (`landscape_layout` — the shared geometry used by the filter builder, the
  map-tile pre-sizing, and the pre-flight space estimate) sized so the sidebar's
  stacked height matches the hero's height exactly, keeping total canvas height
  bounded by the hero alone.
- **`--quality high`:** forces the software libx264/veryfast path (`encoder_args`)
  at CRF 18 for its own sake, reusing the same CRF already used for the map-tile
  upscale pass — distinct from the CRF 20 fallback that only fires when
  `--native` alone pushes the canvas over the hardware cap.
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
- **Gauge dashboard overlay (`--gauge`):** a speedometer dial, compass, big speed
  readout, and a recent-speed sparkline chart, composited onto the hero camera
  tile. Unlike the map tile, this does **not** go through this repo's own
  `hstack`/`vstack`/`pad` filter-graph code at all — `gopro-dashboard.py` has a
  built-in compositing mode (`--use-gpx-only --gpx <gpx> --input <video>`) that
  renders the widget layer as raw RGBA and runs its own internal
  `ffmpeg ... -filter_complex "[0:v][1:v]overlay"` (`FFMPEGOverlayVideo` in
  `gopro_overlay/ffmpeg_overlay.py`), producing a fully-composited output video
  in one subprocess call (`build_gauge_overlay`). The gauge step runs in
  `build_grid`, *before* `build_filter`/`build_filter_landscape`, and simply
  replaces the hero's entry in `angle_paths` with the composited file (same
  pattern `--blur-faces` already uses) — neither filter-building function ever
  learns a gauge was composited; it just sees a different source file at the
  same resolution. `gopro-dashboard.py`'s `input`/`output` are bare *positional*
  args (not `--input`/`--output` flags); they must be passed adjacent to each
  other, before any `--flag`s, or argparse's positional-matching mis-assigns
  them (confirmed by running it for real). v1 requires a **solo hero**
  (`--feature` can't be a pair like `repeaters`) — validated in `setup_tools`.
  GPS extraction (`build_route_gpx`, factored out of the old `build_map_tile`)
  is **shared** with `--map`: `--map --gauge` together extract/retime GPS once,
  not twice. `tesla_gps.write_gpx()` emits the speed extension tag as plain
  `<speed>` (m/s) rather than `<tesla:speed_mps>` — gopro-overlay's GPX parser
  (`gopro_overlay/gpx.py`) only recognizes extension tags whose *local name* is
  exactly `speed` (namespace-agnostic) among a fixed small set, expects the
  value in m/s (`units.Quantity(gpx.speed, units.mps)` — exactly what Tesla's
  own `speed_mps` already is), and feeds it straight into `Entry.speed`
  (`metric_accessor_from("speed")`), used by both the `msi` needle and the big
  `metric` readout. This avoids GPS-position-noise jitter in the needle vs.
  deriving speed from consecutive lat/lon (`cspeed`) as gopro-overlay does by
  default. `course_deg` stays unused — the `compass` widget reads `cog`, which
  gopro-overlay computes itself from consecutive lat/lon (geodesic bearing), so
  the compass needs zero data changes, same as the existing map tile.
- **FSD showcase overlays (`tesla_fsd_overlay.py` / `tesla_fsd_metrics.py`):**
  groundwork for four planned visual ideas (a hands-free/corner-count streak
  scoreboard, a friction-circle G-force meter, a scrolling "note highway"
  anticipation ribbon, rally pace-notes). The first of the four — the streak
  scoreboard — is now real and wired into `tesla_combine.py`'s CLI as
  `--fsd-scoreboard` (`fsd-streak-scoreboard` branch, off the merged
  `fsd-overlay-foundation`); the other three remain each their own follow-up
  branch's job. `StreakScoreboard` (`tesla_fsd_overlay.py`) replaces the
  branch's original `FsdDiagnosticText` throwaway plain-text overlay as the
  default draw target (`--widget scoreboard`, vs. `--widget diagnostic` --
  the old diagnostic is kept, not deleted, for debugging the next three
  ideas the same way it helped prove this one's plumbing). It draws a single
  dark translucent panel, **top-right** of the hero tile: a color-coded "FSD
  ENGAGED"/"FSD OFF" badge (green while engaged, gray/red while not) followed
  by hands-free time, corner count, peak cornering G, and takeover count.
  Top-right is deliberate, not arbitrary: `tesla_combine.py`'s grid filter
  graph (`_tile_chain`) draws the hero tile's own `"FRONT"`-style label at
  `x=20:y=20` (`HERO_FONT_SIZE=64`) *after* this compositing step runs (this
  script only ever sees the bare hero video, not the eventual grid label), so
  a top-left scoreboard would collide with that later-drawn label; top-right
  also avoids `--gauge`'s own bottom-left panel, so `--gauge --fsd-scoreboard`
  together (which `tesla_combine.py` chains sequentially through
  `angle_paths[hero_angle]` — gauge composites first, scoreboard onto that
  output next, same pattern `--blur-faces` → `--gauge` already uses) don't
  overlap either. `TakeoverCounter` (`tesla_fsd_metrics.py`) counts
  `autopilot_engaged` engaged→not-engaged falling edges, no hysteresis needed
  (unlike `CornerCounter`) since the input is already a clean boolean, not a
  noisy continuous value. **Known limitation:** every real drive probed so
  far (17.5km) stayed engaged the entire time — zero real disengagements
  observed — so `TakeoverCounter` is implemented and unit-tested against
  synthetic data only, and has NOT been verified against a real disengagement
  event. Don't mistake "looks right in a synthetic test" for "confirmed
  against reality" here. Two findings drove the original foundation's design:
  - **IMU axis mapping resolved with real data**, since Tesla documents none
    of this: correlated 28,000 telemetry points (17.5km of real mountain
    driving) against two independent signals — `linear_acceleration_mps2_x`
    correlates with steering angle (r=0.31) → **lateral**;
    `linear_acceleration_mps2_y` correlates with speed-derivative (r=-0.36) →
    **longitudinal**; `_z` correlates with neither and has the smallest
    variance → **vertical**, by elimination, unused by any of the four ideas'
    v1. `brake_applied` never fired once across that whole stretch (FSD's
    regen braking apparently never touches the physical brake), so "braking"
    for these features is read off measured deceleration, not that flag.
    `autopilot_state` was `1` (engaged) for the entire 17.5km — no
    disengagement captured yet, so no other value's meaning is confirmed;
    `tesla_fsd_metrics.AUTOPILOT_ENGAGED_STATE` treats anything else as
    disengaged, the conservative reading.
  - **gopro-overlay's normal CLI/XML pipeline can't carry any of this.** No
    G-force/scatter/ribbon widget exists among its ~25 built-in types, and
    even the "just text" ideas need per-frame *state* (a running timer, a
    corner counter) that its `metric="speed"`-style "read one live value"
    model doesn't support — `metric_accessor_from` (`gopro_overlay/
    layout_xml.py`) is a hardcoded ~33-name dict with no fallback, so a new
    derived field can't be read via `metric=` without patching the library.
    Fix: `gopro-dashboard.py`'s CLI is a thin, non-importable
    `__main__`-only wrapper around plain library classes that *are* usable
    directly — `Overlay(framemeta, create_widgets)`'s `create_widgets` is
    just any `entry -> list[Widget]` callable (proven by the library's own
    non-XML `speed_awareness_layout`), and `FrameMeta.process()` (`gopro_
    overlay/framemeta.py`) is the library's own mechanism for adding
    arbitrary new computed per-entry fields (`Entry.__getattr__` has no
    fixed schema at all — any key a processor returns becomes a live
    attribute). `tesla_fsd_overlay.py` is a new driver script (own file,
    must run under `./.venv` same as `--map`/`--gauge`'s subprocess
    boundary) built by mirroring `gopro-dashboard.py`'s own `--use-gpx-only
    --input <video>` render loop, with our own `process()` steps and a
    directly-drawn `Widget` (`StreakScoreboard`, or `FsdDiagnosticText` under
    `--widget diagnostic`) instead of XML.
  - **Data plumbing:** only `linear_acceleration_mps2_x/y` and
    `autopilot_state` needed to newly reach the GPX (speed/lat/lon/`cog`
    are already there) — `tesla_gps.write_gpx()` repurposes three more of
    gopro-overlay's fixed extension-tag names (`<cad>`, `<power>`, `<hr>` —
    same precedented trick as the `--gauge` `speed_mps` → `<speed>` fix
    above) and `retime_samples` (`tesla_combine.py`) carries the raw values
    through to it. `tesla_fsd_metrics.py` (zero `gopro_overlay` dependency,
    stdlib only) decodes those back into `lateral_g`/`longitudinal_g`/
    `autopilot_engaged` and derives `corner_count` (a hysteresis
    threshold-crossing detector on `|lateral_g|`), `hands_free_seconds`, and
    (as of `--fsd-scoreboard`) `takeover_count` (`TakeoverCounter`, a plain
    engaged→not-engaged falling-edge count) — kept dependency-free so `tests/`
    (system Python, no gopro-overlay installed) can test the derivation math
    directly; `tesla_fsd_overlay.py` unwraps gopro-overlay's `pint` `Quantity`
    values to plain floats before calling into it.
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
- FSD showcase overlays foundation: `tesla_fsd_overlay.py` verified end-to-end
  against real footage (a real FSD-engaged mountain drive) — diagnostic overlay
  composited cleanly onto native-res video, `hands_free_seconds` tracked
  elapsed time exactly, `corner_count` climbed plausibly with real curves.
  Also verified a partial-GPX-coverage case (a GPX covering less than the
  video's real duration) both warns and freezes FSD values at their last
  known state past the uncovered tail rather than drifting out of sync with
  the video — this caught a real bug (see git history: an earlier timelapse-
  factor divisor was reused from a different upstream branch than the one
  this driver actually mirrors, so it stretched telemetry timing whenever
  GPX coverage fell short of the video; full-coverage footage happened to
  mask it, since the ratio was ~1.0 either way). The IMU axis-mapping
  correlation itself (not just the plumbing) is a real empirical finding,
  not a spec, and hasn't been cross-checked against a second, independent
  drive yet.
- `--fsd-scoreboard`/`StreakScoreboard`: verified end-to-end against real
  footage through the *full integrated* `tesla_combine.py` pipeline (not just
  the standalone driver) — `--fsd-scoreboard` alone, `--gauge --fsd-scoreboard`
  together (confirms the chaining order and that neither panel collides with
  the other or the hero tile's own label), and both again under `--landscape
  --map` (confirms the widget's frame-size-derived geometry, not a fixed
  tile-size assumption, really does work unchanged across layouts).
  `playcheck.sh` clean on every render, including `+faststart` (which the
  foundation branch's standalone diagnostic script doesn't set, but the
  integrated pipeline does). `SCOREBOARD_PANEL_W_FRAC`/`_H_FRAC`/
  `SCOREBOARD_MARGIN` are accordingly tuned, not a starting guess.
  A second independent review (same process as the foundation branch's) then
  caught a real bug before commit: `decode_fsd_fields` collapsed a *missing*
  `autopilot_state` sample (`hr is None` — e.g. `retime_samples`' edge-hold
  pad, nulled deliberately because a telemetry gap isn't a confirmed
  disengagement) into a plain `False`, and `TakeoverCounter.update` wasn't
  None-aware the way `CornerCounter.update` already was — so any drive whose
  SEI coverage ends before the video does would report a phantom takeover
  the instant the pad's unknown samples began. Confirmed with an executable
  repro before the fix (`autopilot_engaged` is now `Optional[bool]`,
  `TakeoverCounter.update` now short-circuits on `None` exactly like
  `CornerCounter.update` does). `TakeoverCounter` still can't be verified
  against a real *disengagement* with currently-available footage (see the
  bullet above) — that gap is expected to persist past this branch, not just
  until the next one; the bug just fixed was about *missing data* being
  misread as a disengagement, a different and now-closed hole from "we've
  never seen a real one to check against."

## Gotchas
- Never commit footage or rendered outputs.
- The encode path is macOS/VideoToolbox-specific.
- `report_gap` reports clock drift from squeezed recording gaps; fixed to work
  under `--trim-end` too.
- Anything printed mid-run must go through `log()` — it clears and redraws the
  progress display around the print. A bare `print()` will be scribbled over.
