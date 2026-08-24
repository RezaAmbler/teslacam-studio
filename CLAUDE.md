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
  anticipation ribbon, rally pace-notes). All four now real and wired into
  `tesla_combine.py`'s CLI as `--fsd-scoreboard` (`fsd-streak-scoreboard`
  branch, off the merged `fsd-overlay-foundation`), `--fsd-friction-circle`
  (`fsd-friction-circle` branch), `--fsd-note-highway` (`fsd-note-highway`
  branch), and `--fsd-pace-notes` (`fsd-pace-notes` branch, the last of the
  four). `StreakScoreboard` (`tesla_fsd_overlay.py`) replaces
  the branch's original `FsdDiagnosticText` throwaway plain-text overlay as
  the default draw target (`--widget scoreboard`, vs. `--widget diagnostic`
  -- the old diagnostic is kept, not deleted, for debugging future work the
  same way it helped prove this one's plumbing). It draws a single
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
  against reality" here.
  - **The friction circle** (`FrictionCircle`, `--widget friction-circle`,
    `--fsd-friction-circle`): the classic motorsport G-G diagram — lateral G
    vs. longitudinal G plotted as a dot on a ringed target (concentric rings
    at 0.2g/0.4g, `FRICTION_CIRCLE_RING_STEP`, full-scale rim at
    `FRICTION_CIRCLE_MAX_G=0.6`), with a fading 3-second trail behind it and
    a "peak this corner: X.XXg" readout. Smooth FSD driving (brake → turn-in
    → apex → throttle blending into one continuous curve) traces clean arcs;
    jerky driving scatters. **Bottom-right** of the hero tile — the one
    corner the other two overlays leave free: the grid's own hero label is
    top-left (drawn later by `tesla_combine.py`'s filter graph, same reason
    `StreakScoreboard` avoids it), `StreakScoreboard` itself is top-right,
    `--gauge`'s dashboard panel is bottom-left. So all three overlay flags
    together (`--gauge --fsd-scoreboard --fsd-friction-circle`) now use all
    four corners with no collisions, by construction. `FRICTION_CIRCLE_
    SIZE_FRAC`/`_MARGIN` are the same starting-guess-then-verify status every
    panel constant in this codebase has had.
    - `CornerCounter` (`tesla_fsd_metrics.py`) was extended, not duplicated,
      with `last_corner_peak_g` (latches to the just-completed corner's peak
      `|lateral_g|` the instant a corner *exits* — the same
      `magnitude < exit_g` transition that already flips `_in_corner` False)
      and an internal `_current_corner_peak` (the in-progress corner's
      running peak, reset to the entry magnitude when a corner *starts*).
      Kept in `CornerCounter` rather than a second class so there's exactly
      one place the "am I in a corner right now" hysteresis lives — two
      independent trackers could disagree. The extension is backward
      compatible: `corner_count`/`peak_lateral_g` and `update()`'s
      return-value contract are unchanged, confirmed by rerunning the
      pre-existing `CornerCounter` tests unmodified after the extension.
    - `GTrailBuffer` (`tesla_fsd_metrics.py`) is a thin
      `collections.deque(maxlen=N)` of `(lateral_g, longitudinal_g)` pairs.
      `.append` is a no-op when either axis is `None` — mirrors the
      takeover-counter lesson: a real telemetry gap must leave a visible
      *pause* in the trail (the deque just stops growing until data
      resumes), never fabricate a point at the origin or silently carry a
      stale value forward. `N` (Fable's "3-second" trail) is the *widget's*
      decision, not this class's — `tesla_fsd_overlay.py`'s render loop
      steps at ~0.1s, so `FrictionCircle` picks `N=30`; `GTrailBuffer` itself
      just takes whatever `maxlen` it's given. `FrictionCircle` owns one
      `GTrailBuffer` instance as `__init__` state and appends to it once per
      `draw()` call — safe because `Overlay.__init__` (`gopro_overlay/
      layout.py`) calls `create_widgets(entry)` exactly once and reuses the
      same widget instances for every subsequent `draw()` across the whole
      render, confirmed by reading that file this session.
    - **Alpha-fade technique that actually worked, and why**: direct
      per-shape RGBA fill on the shared overlay canvas (the same technique
      `StreakScoreboard`'s translucent panel/badge already uses), NOT
      gopro-overlay's own `Frame` widget technique (`gopro_overlay/widgets/
      widgets.py`) of drawing children onto a separate opaque layer, applying
      an alpha mask via `putalpha`, then `image.alpha_composite`-ing that
      layer onto the canvas. The direct approach works here because
      `tesla_fsd_overlay.py`'s `SingleBuffer` starts each frame from a
      **fully transparent** `(0,0,0,0)` canvas (see its own `main()`) — so a
      direct fill's alpha is written to the frame as-is, and ffmpeg's own
      `overlay` filter alpha-blends the *finished* frame onto the video
      correctly. The trail is drawn **oldest point first**, so where two
      points overlap, painter's-algorithm order means the newer (more
      opaque) one naturally overwrites the older (fainter) one — the correct
      order for a fade, and why the simpler direct-alpha approach was
      sufficient without a flat-opacity artifact. **This was verified by
      rendering a real synthetic frame and reading back actual pixel alpha
      values** (not just a code-read): a 40-sample synthetic corner produced
      a trail whose per-pixel alpha rose monotonically from 35 (oldest) to
      200 (newest), with the current-sample dot overwriting as solid
      `(255,255,255,255)` on top — confirming the fade is real, not flat.
      That same real-frame check also caught a real layout bug: the
      "RIGHT"/"LEFT" axis tick labels, drawn with OUTWARD text alignment
      (extending away from center, toward the panel's own rim), overflowed
      past the panel's edge and came uncomfortably close to the tile's own
      right edge (bottom-right placement means that edge is close by
      construction). Fixed by aligning those two labels INWARD instead
      (toward the spacious center of the circle) — always fits regardless of
      font/label width, since the whole circle's interior is available to
      grow into.
    - **Axis sign convention — confirmed against real telemetry, not
      eyeballed**: the IMU axis MAPPING (`linear_acceleration_mps2_x/y` →
      lateral/longitudinal) was already confirmed against real data (see
      below); which physical direction each channel's *positive* sign
      corresponds to needed a *second*, separate check. A single eyeballed
      video frame turned out to be an unreliable way to check it — this is a
      continuously winding mountain road, so "which way does the FRONT
      camera curve right now" is phase-lagged against "what is the car's
      lateral accel at this exact instant" (two different real timestamps
      gave contradictory answers by eye). Settled with statistics instead,
      over the same 28,000-point real drive used for the axis mapping:
      `corr(d(heading_deg)/dt [+ = turning right], lateral_g) = +0.68` —
      turning right already gives positive `lateral_g`, so the `x = cx +
      (...)` mapping needed no change. `corr(d(speed)/dt [+ = accelerating],
      raw linear_acceleration_mps2_y) = -0.36` — accelerating gives
      **negative** `longitudinal_g`, which the original `y = cy - (...)`
      put toward the `BRAKE` label (backwards); fixed to `y = cy + (...)`.
      Re-verified after the fix, again against real telemetry rather than a
      guess: found the exact real timestamps of strongest acceleration and
      strongest braking within a test window from the actual per-frame CSV,
      rendered both, and measured the dot's pixel position programmatically
      — the acceleration-moment dot sits measurably higher (closer to
      `ACCEL`) than the braking-moment dot, confirming the fix in the
      correct direction. A full-frame render during real braking-into-a-
      right-turn shows the dot in the bottom-right quadrant with a trail
      curving in from upper-left — physically coherent with trail-braking
      into a right-hander, matching real motorsport G-G diagrams. This sign
      logic (`FrictionCircle._g_to_px`, `tesla_fsd_overlay.py`) is currently
      the only place it's expressed, and it's pure math with no
      `gopro_overlay` dependency but still lives in the venv-only driver
      script rather than `tesla_fsd_metrics.py` — **the two still-deferred
      ideas (note-highway ribbon, pace-notes) will need this exact same
      "positive lateral = right turn" convention** (e.g. pace-notes calling
      a corner "Right 3" vs "Left 3"), so re-deriving it independently
      inside either one risks a silent contradiction with the friction
      circle. Worth hoisting into a tested, shared function before either
      of those branches starts. (This did in fact get hoisted into
      `tesla_fsd_metrics.g_to_offset` before the note-highway branch
      started — see "Sign math moved to a tested, shared function" under
      Verification status. The note-highway ribbon below only plots
      *lateral* severity, a single axis, so it reuses `g_to_offset`'s
      lateral sign convention directly rather than calling the function
      itself, which combines both axes into a 2D offset the ribbon doesn't
      need.)
  - **The note highway** (`NoteHighway`, `--widget note-highway`,
    `--fsd-note-highway`): a horizontal scrolling ribbon of signed
    cornering severity, "now" fixed at horizontal-center — left of center is
    what FSD already did, right of center is what the road is about to
    demand, flowing toward "now" like a rhythm-game note chart, the car's
    own trace merging into the ribbon exactly on arrival. This is the
    architecturally novel one of the four ideas: every other FSD widget only
    ever reads the *current* `entry()` (plus, for the friction circle, a
    self-accumulated trailing buffer) — this one needs the drive's *whole*
    timeline up front, including samples that haven't "happened" yet
    relative to the frame being drawn, because compositing happens after
    the fact and can show the road *before* the car reaches it, something a
    live dashboard could never do.
    - **Metric: reused `lateral_g`, not a new curvature/yaw-rate signal.**
      The original brainstorm suggested steering angle, or road curvature
      derived from `heading_deg` rate. `lateral_g` was used instead: it's
      already derived, already sign-confirmed against real telemetry (see
      `g_to_offset` above), and is a physically direct proxy for cornering
      severity (centripetal accel = f(curvature, speed²)) without
      introducing a fourth derived signal — a yaw-rate signal would need its
      own new `gopro-overlay` plumbing (`cog` isn't automatically available;
      it needs an explicit `process_deltas` call nothing currently makes,
      confirmed during the foundation branch).
    - **Full-timeline lookahead**: `FrameMeta` (`gopro_overlay/framemeta.py`)
      already supports this directly — `frame_meta.framelist`/`.frames` are
      plain attributes and `frame_meta[i]` is valid (`__getitem__` returns
      `self.frames[self.framelist[i]]`) — so `main()` builds
      `lateral_g_timeline = [frame_meta[i].lateral_g for i in
      range(len(frame_meta))]` **once**, after both `frame_meta.process(...)`
      calls have populated `.lateral_g` on every entry and before
      `Overlay(...)` is constructed, and passes it to `NoteHighway` (via
      `create_widgets_for`'s new optional `lateral_g_timeline` parameter,
      only required for `--widget note-highway`). The widget then just needs
      "where am I" in that array on each `draw()` call — reused the exact
      `self._index`-incremented-once-per-`draw()` pattern
      `FrictionCircle`/`GTrailBuffer` already established (rather than a
      timestamp lookup), which works because `timelapse_correction` is
      hardcoded to `1.0` and `timeseries_to_framemeta` builds entries at the
      same `RENDER_STEP_SECONDS` cadence the render loop steps at — draw-call
      N and array-index N stay in lockstep by construction, the same fact
      the friction circle's trail buffer already relies on. **Verified
      empirically, not just by reading the two source files**: a synthetic
      `FrameMeta` pushed through the real `timeseries_to_framemeta` →
      `stepper()` → `frame_meta.get(pts)`-per-step code path (the same
      lookup `Overlay.draw()` performs) confirmed draw-call index N's
      `entry.lateral_g` equals `lateral_g_timeline[N]` for every N across a
      whole synthetic drive, zero mismatches — including with an original
      sample cadence (0.2s) different from the render step (0.1s), so the
      lockstep genuinely comes from `timeseries_to_framemeta`'s own
      resampling, not a coincidence of matching input cadence.
    - **`ribbon_window(values, index, past_n, future_n)`** (`tesla_fsd_
      metrics.py`): the pure windowing math, continuing the `g_to_offset`
      precedent — plain list slicing with zero `gopro_overlay` dependency,
      so it lives in the dependency-free module `tests/` (system Python) can
      reach, not the venv-only driver. Returns a `past_n + 1 + future_n`-
      length slice centered on `index`, `None`-padded at either end where
      the array doesn't reach (start/end of the drive) — never wrapped,
      clamped, or fabricated, the same "a gap must show as a gap" principle
      `GTrailBuffer`/`decode_fsd_fields`/`TakeoverCounter` already follow. A
      `None` already inside `values` (a genuine mid-drive telemetry gap)
      passes through completely untouched — only the two ends get padded.
    - **Placement: full-width, below both top-anchored elements.** All four
      hero-tile corners are now used (TL hero label, TR `StreakScoreboard`,
      BL `--gauge`, BR `FrictionCircle`), but every FSD overlay is its own
      independent compositing pass (chained through `angle_paths`, blind to
      what any other pass drew), so placement has to come from fixed,
      hand-designed regions, not measured live against whatever else happens
      to be present in a given run. The gap between the gauge and
      friction-circle panels varies with tile aspect ratio (fragile to
      depend on); the vertical space *below* both top-anchored elements
      doesn't — it's derived purely from their own known, fixed heights
      (`NOTE_HIGHWAY_HERO_LABEL_CLEARANCE_PX=110`, a fixed pixel value
      matching `HERO_FONT_SIZE`'s own fixed-pixel nature, vs.
      `SCOREBOARD_MARGIN + img_h * SCOREBOARD_PANEL_H_FRAC`, computed live
      from `StreakScoreboard`'s own constants at draw time), so it stays
      clear of TL/TR regardless of tile shape, and sits well above BL/BR
      since it's near the top. `NOTE_HIGHWAY_WIDTH_FRAC=0.92`/
      `_HEIGHT_FRAC=0.11` and the `NOTE_HIGHWAY_PAST_SECONDS`/
      `_FUTURE_SECONDS=4.0` window are the same starting-guess-then-verify
      status every other panel constant in this codebase has had.
    - Renders a centered zero-g baseline, a signed line/filled area
      (positive `lateral_g` = right turn drawn *above* the baseline,
      negative = left drawn *below* — signed, matching `g_to_offset`'s own
      lateral convention directly, not just magnitude, since showing *which
      way* the upcoming corner goes is the whole point), and a fixed
      vertical "now" playhead at horizontal-center. `None`-padded slots
      (start/end of drive, or a genuine mid-drive gap) break the line/fill
      into separate contiguous runs rather than drawing a fabricated flat
      line through them.
  - **The pace notes** (`PaceNotes`, `--widget pace-notes`,
    `--fsd-pace-notes`, the fourth and last of the four planned ideas): a
    momentary rally-style text callout — "RIGHT 3", with a direction chevron
    and a color-coded severity digit — that appears a few seconds BEFORE a
    corner and clears shortly after, the way a real rally co-driver reads a
    pre-recorded note just ahead of the driver. Designed with a Fable-model
    creative consult (same process the other three ideas' original
    brainstorm and this branch's own placement/review went through) — its
    recommendations (severity curve, callout wording, lead/hold timing,
    alpha-only "animation", placement below the note-highway ribbon) were
    followed close to verbatim; see the design rationale below for where the
    implementation matched vs. adapted them.
    - **Architecture: precompute, never grade live.** Unlike `CornerCounter`
      (a streaming, one-sample-at-a-time detector feeding the scoreboard's
      `corner_count` and the friction-circle's peak-G readout),
      `segment_corners` (`tesla_fsd_metrics.py`) scans the WHOLE
      `lateral_g_timeline` (the same full-drive array `NoteHighway` already
      needs, built the same way in `main()` -- each widget is its own
      subprocess/`--widget` invocation, so `--fsd-note-highway` and
      `--fsd-pace-notes` never share one process to reuse the array
      across, but the construction is identical) in a single pass and
      returns every corner's `start_index`/`end_index`/`peak_g`/`direction`
      up front. `build_pace_notes` then grades and chains them once, also up
      front, into a `PaceNote` list. `draw()` only ever does a cheap lookup
      (`active_pace_note`/`pace_note_alpha`) against the current frame index
      — there is no per-frame grading, so there is no boundary-oscillation
      flicker to debounce: a corner is graded exactly once, on its final,
      fully-observed peak. This was the single design decision Fable's
      brainstorm flagged as solving the "flicker trap" (a pace note's grade
      changing mid-callout as `lateral_g` jitters near a threshold) *by
      construction* rather than by tuning.
    - **Severity grading** (`grade_corner`, `PACE_NOTE_GRADE_THRESHOLDS`,
      `tesla_fsd_metrics.py`): peak `|lateral_g|` → a rally-style 1
      (tightest/most dramatic) to 6 (loosest/barely worth a call), non-
      linear like real rally severity numbers, but scaled to FSD's own real
      cornering envelope (comfortable mountain-road FSD driving measured so
      far tops out well under `FRICTION_CIRCLE_MAX_G=0.6`) rather than real
      rally G-loads (1.5g+) — a literal port of real pace-note breakpoints
      would put every FSD corner at "6" and never call anything else. The
      floor (0.15) is deliberately `CornerCounter.ENTER_G` exactly: every
      corner graded here is a corner `CornerCounter` would also count (same
      0.15g floor) — a WEAKER guarantee than "the counts always match",
      flagged as overstated by an independent review: they don't match in
      general, since `build_pace_notes` drops sub-min-duration blips and
      merges chained pairs into one callout, and `segment_corners` drops a
      corner still open at the timeline's end, none of which
      `CornerCounter` does. What's guaranteed is that every CALLOUT
      corresponds to a counted corner, never the reverse claim.
    - **Direction reuses `g_to_offset`, literally, not just its
      convention.** `segment_corners` calls
      `g_to_offset(peak_signed_lateral_g, 0.0, max_g=|peak_signed_lateral_g|)`
      and reads the sign of the returned `dx` — not a re-derivation of
      "positive lateral_g = right", the actual confirmed-against-real-
      telemetry function every other FSD widget's direction already goes
      through (see `g_to_offset`'s own docstring, which specifically warned
      pace-notes would need this). `max_g` is set to the peak's own
      magnitude so `g_to_offset`'s clamp-to-rim never engages — only `dx`'s
      SIGN is used, its scaled magnitude is irrelevant here.
    - **Callout text** (Fable's recommendation, adopted close to verbatim):
      `"RIGHT 3"` / `"LEFT 4"` — direction word, then grade number, all
      caps, the minimum that captures rally flavor. A corner lasting at
      least `PACE_NOTES_LONG_SECONDS=4.0` gets a `" LONG"` suffix. Two
      corners chain into one callout (`"RIGHT 3"` with a smaller `"into
      LEFT 4"` line beneath) when the gap between them is at most
      `PACE_NOTES_CHAIN_GAP_SECONDS=2.0` — `build_pace_notes` chains AT MOST
      one link (never "into X into Y": a chained corner's own potential
      chain into a third corner is not inherited), and the chained corner
      does NOT also get its own standalone callout (re-announcing it a
      second later would read as a stutter, not authenticity) — covered by
      `test_build_pace_notes_chains_at_most_one_link`.
    - **Timing** (Fable's recommendation): `PACE_NOTES_LEAD_SECONDS=2.5`
      (callout appears this far before the corner's real start — inside the
      note-highway ribbon's own 4.0s lookahead, so by the time the callout
      pops, the corner's bump is already visible sliding toward the ribbon's
      playhead — the callout literally names the shape the viewer can
      already see coming), `PACE_NOTES_HOLD_SECONDS=1.0` (the callout's
      OUTER window extends this far AFTER the corner's start — anchored to
      the corner's START, not its end, matching a real co-driver: the call
      is read on approach and the driver is already executing the corner by
      the time it clears — NOT how long it stays at full opacity: the
      `_FADE_OUT_SECONDS` ramp runs INSIDE this window's own last stretch,
      so the callout is actually fully gone AT `HOLD_SECONDS`, not
      `HOLD_SECONDS` after full opacity ends; an earlier version of this
      doc and the code's own comments got this wrong, flagged by an
      independent review — see Verification status),
      `PACE_NOTES_FADE_IN_SECONDS=0.3`/`_FADE_OUT_SECONDS=0.6`.
      Overlap rule: when two notes' windows overlap (a tight corner
      sequence), the LATER-starting note wins — `active_pace_note` scans in
      reverse, so a fresh call always preempts a lingering stale one; never
      two panels at once, never a crossfade between them. The earlier
      note's own window is also TRUNCATED (`tesla_fsd_metrics.
      visible_window`) so it fades out smoothly and reaches zero before the
      later note's window can open, rather than being cut off abruptly at
      full opacity — a real bug an independent review found and fixed (see
      Verification status for the full story).
    - **"Animation" is alpha only** (Fable's recommendation, matching the
      friction circle's own established rendering technique): no position
      slide, no size change — `pace_note_alpha` returns a single 0.0-1.0
      scalar per frame (linear ramp up over the fade-in, hold at 1.0, linear
      ramp down over the fade-out) that `PaceNotes.draw()`'s `_fade()`
      multiplies into every shape's own RGBA alpha channel before drawing —
      the same direct-RGBA-on-transparent-canvas technique the friction
      circle's fading trail already proved composites correctly (see that
      section above). A slide would fight the note-highway ribbon's own
      leftward flow this widget sits below; PIL font sizes step discretely
      so a scale "animation" would judder rather than read as smooth.
    - **Placement: full-width, centered, directly below the note-highway
      ribbon's own band** — computed from the SAME `NOTE_HIGHWAY_*`
      constants `NoteHighway`'s own `draw()` uses for its bottom edge,
      whether or not `--fsd-note-highway` is actually enabled in a given
      run (every FSD overlay pass is its own blind, independent compositing
      step, so placement has to come from fixed, hand-designed regions —
      same reasoning `NoteHighway`'s own placement already documents). This
      clears every other overlay's fixed region by construction: TL hero
      label, TR `StreakScoreboard`, BL `--gauge`, BR `FrictionCircle`, and
      the `NoteHighway` ribbon strip itself. Fable's brainstorm floated
      anchoring near the ribbon's own "now" playhead instead, but that risks
      colliding with the plotted line/playhead/"NOW" label in an 11%-tile-
      height strip — directly below it, still visually tied to the ribbon it
      annotates, was the safer, still-thematic choice, and was the one
      verified against a real combined render (see Verification status).
    - **"A gap must show as a gap"**: `segment_corners` treats a `None`
      sample (a telemetry gap) exactly like `CornerCounter.update`'s own
      None-safety — it neither starts nor ends a corner, and doesn't move
      the peak, so a gap mid-corner simply pauses it. A corner still open
      when the timeline runs out (the drive ends mid-corner) is DROPPED,
      not synthesized an end — never confirmed how it resolves, so it must
      not be called out as a real, complete corner. Pace notes are also
      deliberately NOT gated on `autopilot_engaged`: the note describes the
      *road*, not FSD's state, and a takeover moment is exactly when a
      viewer most wants to know what corner prompted it — a deliberate
      decision, not an oversight, written down here so it isn't "fixed"
      later.

      This principle originally had a real gap of its own, found by an
      independent review: pausing corner STATE across a gap (above) is not
      the same as pausing corner DURATION, and the first version of this
      code only did the former — `min_samples`/`LONG` were computed from
      raw `end_index - start_index`, which keeps counting through an
      unobserved gap exactly like through real data, so a single real
      sample either side of a long blackout could be reported as one long,
      confirmed corner. Fixed by adding `Corner.observed_samples` (a count
      of only the REAL samples seen while the corner was open) for those
      decisions to key off instead, and `Corner.gap_before` (whether the
      approach to this corner was actually fully observed) so
      `build_pace_notes` refuses to chain two corners across an unobserved
      stretch. See Verification status for the full repro-and-fix story.
    - `PACE_NOTES_MIN_CORNER_SECONDS=0.7` drops corner blips too short to
      call out. `PACE_NOTES_GRADE_COLORS` reuses the scoreboard's
      established red/amber/green severity language (only the grade
      NUMBER is colored; the direction word and chevron stay white, so
      color always answers "how sharp" and the chevron/word always answer
      "which way"). Every `PACE_NOTES_*` fraction/margin is the same
      starting-guess-then-verify status every other panel constant in this
      codebase has had — see Verification status for what's since been
      confirmed against real footage.
  - **`build_fsd_overlay` consolidation**: the original `--fsd-scoreboard`-
    only `build_fsd_scoreboard_overlay` (`tesla_combine.py`) was generalized
    into `build_fsd_overlay(hero_video_path, gpx_path, tile_dims, widget,
    ffmpeg, venv_py, out_path, tmpdir, dry_run, progress)`, taking an
    explicit `widget` ("scoreboard", "friction-circle", "note-highway", or
    now "pace-notes") passed straight through to the subprocess as
    `tesla_fsd_overlay.py --widget <widget>` -- a second (or third, or
    fourth) near-identical function would just have been copy-paste with
    one word changed. `FSD_OVERLAY_META` holds the small per-widget bits
    that actually differ (the CLI flag name for log lines, the
    `Step`/`Progress` kind, etc) — the pace-notes branch just added a
    fourth entry, no new `build_*` function needed. The one existing
    `--fsd-scoreboard` call site in `build_grid` now passes
    `widget="scoreboard"` explicitly (previously implicit via the driver's
    own `--widget` default) — confirmed, by rerunning every existing
    `--fsd-scoreboard` test immediately after this refactor and before
    adding anything friction-circle-specific, that the consolidation alone
    changes nothing about `--fsd-scoreboard`'s behavior.

  Two findings drove the original foundation's design:
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
- `--fsd-friction-circle`/`FrictionCircle`: the direct-alpha trail-fade
  technique and the panel's overall layout (rings, ticks, current dot, peak-G
  readout) were first verified by rendering real synthetic frames through the
  actual widget code under `./.venv` and reading back real pixel alpha
  values — this caught and fixed a real tick-label overflow bug. It has
  since also gone through a full integrated `tesla_combine.py` render
  against real Tesla footage, the same way `--fsd-scoreboard` was: alone,
  combined with `--gauge --fsd-scoreboard --fsd-friction-circle` (all four
  hero-tile corners populated at once, no collisions), and under
  `--landscape --map`. `FRICTION_CIRCLE_SIZE_FRAC`/`_MARGIN`/`_MAX_G` are
  accordingly tuned, not a starting guess. The axis sign convention is
  confirmed against real telemetry (see the design-notes bullet above for
  the full method) — not an open item.
  - **Fixed (flagged by an independent review, same day)**: the "peak this
    corner" readout used to read `last_corner_peak_g` directly, which only
    latches on corner *exit* — so while a corner was actually in progress
    (the moment a viewer is most likely to be looking at a G-meter), it
    showed the *previous* corner's peak, not the live one. `CornerCounter.
    display_peak_g` now feeds the widget instead: the live, still-growing
    `_current_corner_peak` while `_in_corner` is true, falling back to
    `last_corner_peak_g` (now correctly read as "the last *completed*
    corner's peak") between corners. `last_corner_peak_g` itself is
    unchanged and still exists for any caller that specifically wants that
    narrower meaning.
  - **Sign math moved to a tested, shared function (flagged by the same
    review)**: `FrictionCircle._g_to_px`'s sign/clamp logic (confirmed
    against real telemetry, see above) used to live inline in the widget --
    pure math with zero `gopro_overlay` dependency, but stuck in the
    venv-only driver script where `tests/` (system Python) couldn't reach
    it. Moved to `tesla_fsd_metrics.g_to_offset(lateral_g, longitudinal_g,
    max_g) -> (dx, dy)`, returning a normalized, UI-framework-agnostic
    offset (+dx = right, +dy = accelerating) that `_g_to_px` now just scales
    by `radius_px` and flips into screen space (+y is DOWN on screen, the
    opposite of `g_to_offset`'s "+dy = accelerating = up" convention -- that
    flip is the one piece that legitimately belongs in the widget, not the
    dependency-free module). Now covered by direct tests pinning both axes'
    signs and the clamp-to-rim behavior — this matters beyond just this
    widget: **the two ideas that were still deferred at the time (note-
    highway ribbon, pace-notes) need this exact same convention** (e.g.
    pace-notes calling a corner "Right 3" vs "Left 3"), so they should call
    `g_to_offset` too, not re-derive the sign independently. (Both have
    since landed — the note-highway ribbon follows this convention for its
    one axis, see "The note highway" bullet above for why it doesn't
    literally call `g_to_offset` itself, which is a two-axis function that
    single-axis ribbon doesn't need; pace-notes' `segment_corners` DOES call
    `g_to_offset` directly, see "The pace notes" bullet above.)
  - **Design note, now resolved**: at the time this was originally written,
    `--gauge` → `--fsd-scoreboard` → `--fsd-friction-circle` already chained
    as three sequential subprocess renders of the hero tile (each a full
    decode + draw + re-encode generation), and landing the note-highway
    ribbon and pace-notes the same way was flagged as something that would
    make five generations — worth combining `create_widgets_for`'s multiple
    FSD widgets into one compositing pass before a third FSD widget landed,
    not after. Both note-highway and pace-notes landed the same sequential-
    subprocess way anyway (each its own `build_fsd_overlay` call chained
    through `angle_paths[hero_angle]`, not a combined pass), so
    `--gauge --fsd-scoreboard --fsd-friction-circle --fsd-note-highway
    --fsd-pace-notes` together now means FIVE sequential hero-tile re-encode
    generations. All four FSD showcase visuals now exist and none more are
    planned, so this is no longer "worth revisiting later" — it's the
    concrete, ready-to-do next efficiency win for anyone touching this area
    next: `create_widgets_for` already returns a widget *list*, so one
    `tesla_fsd_overlay.py` invocation could draw multiple FSD widgets (not
    `--gauge`, a different tool, which is its own subprocess and layout
    system entirely) in a single pass if `--widget` took multiple values,
    cutting five re-encodes to one regardless of how many of the four flags
    are combined. Deliberately NOT done as part of the pace-notes branch
    itself — it's a cross-cutting refactor of every existing widget's own
    call site, a bigger, riskier change than adding the fourth widget on
    the established pattern, and this codebase's own precedent (every prior
    FSD branch) is to land the new widget first, refactor separately.
- `--fsd-note-highway`/`NoteHighway`: the central architectural claim — that
  draw-call index N and `lateral_g_timeline[N]` stay in lockstep — was
  checked empirically, not just by reading `framemeta.py`/`framemeta_gpx.py`:
  a synthetic `Timeseries` (irregular 0.2s sample spacing, deliberately
  different from the 0.1s render step) was pushed through the real
  `timeseries_to_framemeta` → `frame_meta.process(...)` → `frame_meta.
  stepper(...)` → `frame_meta.get(pts)`-per-step path, and every one of the
  99 draw-equivalent steps matched `lateral_g_timeline[index]` exactly, zero
  mismatches. A second smoke test drove the real `NoteHighway.draw()` across
  a 200-sample synthetic timeline containing both a start/end (edge-padded)
  boundary and a genuine mid-drive `None` gap, confirming no exception at
  any point (including one deliberate past-the-end draw call, defensively
  handled by `ribbon_window`'s own bounds check) and that `self._index`
  advances exactly once per `draw()` call as designed.

  **Placement/rendering since confirmed against real footage, not just
  synthetic data** — and by more than a single eyeballed frame, after a
  first spot-check frame looked too flat to trust on sight alone: the
  ground-truth GPX the real render actually used was regenerated
  independently (same trim window, same `build_route_gpx` call), the
  panel's pixel geometry was hand-recomputed from the `NOTE_HIGHWAY_*`
  constants, and the "now" dot's pixel position was measured in extracted
  frames at 10 timestamps spanning a real left-turn → right-turn sign
  change. It matched ground-truth `lateral_g` to within ~0.01–0.02g at
  every point, and the playhead's x-position matched the hand-computed
  value to within 1px. (The methodology also caught a real, unrelated
  discovery in the process — see the `FILENAME_RE` gotcha below.)
  `NOTE_HIGHWAY_WIDTH_FRAC`/`_HEIGHT_FRAC`/`_PAST_SECONDS`/`_FUTURE_SECONDS`
  are accordingly now tuned, same confirmed-against-a-real-frame status
  `SCOREBOARD_PANEL_W_FRAC`/`FRICTION_CIRCLE_SIZE_FRAC` reached.

  **Legend added after real-footage review** (a user watching a rendered
  frame had no way to tell what the ribbon plotted, which axis was which,
  or what the scale was — none of the other three overlays have this gap,
  since the gauge has MPH/compass letters and the friction circle has
  LEFT/RIGHT/ACCEL/BRAKE ticks + a numeric readout). All label text lives
  in the panel's own PAD strip (the gap between the rounded-rect edge and
  the plot area) on all four sides — the plotted line's y-range is exactly
  `y1+pad..y2-pad` and x-range exactly `plot_x1..plot_x2`, so the pad strip
  is geometrically guaranteed clear of the line/fill at any g value, at any
  point in the window, not just "usually clear in practice." Top strip:
  `"PAST {N}s"` / `"AHEAD {N}s"` at the two horizontal extremes (reads the
  actual `NOTE_HIGHWAY_PAST_SECONDS`/`_FUTURE_SECONDS` constants, not a
  hardcoded guess at the window width) and `"NOW"` centered on the
  playhead. Bottom strip: `"CORNERING SEVERITY"` (what's plotted) and
  `"±{MAX_G}g"` (the scale). Left strip: `"R"`/`"L"` tick labels at the
  upper/lower quarter, spelling out the sign convention (+g/above baseline
  = right turn, matching `g_to_offset`) the same way `FrictionCircle`'s own
  ticks do, without needing a full axis label. Verified against a real
  rendered frame (not just code-read): legible at both the cropped-panel
  and full-tile scale, no overlap with the plotted data or with the hero
  label/scoreboard above it.

  **Independent Fable review found one real, medium-severity issue** (the
  first FSD-branch review that found nothing wrong in the branch's own new
  code) — not in `NoteHighway`/`ribbon_window` themselves, but in the
  shared `retime_samples` (`tesla_combine.py`) they depend on: only the
  *edge* head/tail pads nulled the FSD fields (`linear_acceleration_mps2_x/
  y`, `autopilot_state`) across a gap; a genuine **mid-drive** SEI dropout
  (e.g. one clip in a multi-clip event has no SEI while its neighbors do)
  left real samples bracketing the gap, and gopro-overlay's own
  `Timeseries.get()` linearly interpolates `lateral_g`/`autopilot_engaged`
  straight across it — fabricating a smooth cornering/engagement ramp
  through a stretch with zero real telemetry, which the note-highway ribbon
  would then show as *upcoming road* before the car got there, the worst
  possible place for this to surface. This contradicted the "a gap must
  show as a gap" principle every FSD field/widget here otherwise claims to
  follow (and had quietly applied to `FrictionCircle`'s trail,
  `CornerCounter`, and interpolated `autopilot_engaged`/`TakeoverCounter`
  since the foundation branch — this branch's docs were just the first to
  assert the principle held generally, which is what made the gap visible).
  **Fixed**: `retime_samples` now also breaks the FSD fields — not
  position, which is left to interpolate/hold exactly as before, matching
  the map tile's existing "squeezed gap" behavior — across any *mid-drive*
  gap between two consecutive real samples wider than
  `GAP_BREAK_SECONDS = 1.0` (comfortably above normal SEI sample spacing,
  comfortably below a real dropout), by inserting two synthetic
  `NO_FSD_DATA` points a few ms inside each side of the gap. Covered by new
  tests: a mid-drive-gap case confirming the bridge points land and null
  the right fields while leaving position alone, and a normal-spacing case
  confirming no bridge is spuriously inserted at ordinary sample cadence.
  The review also reconfirmed (by tracing gopro-overlay's own
  `SingleBuffer`/`Scene`/`Overlay.draw()` and `Timeseries.Stepper` source,
  not just re-reading this repo's comments) that the draw-call/array-index
  lockstep is exact — `Stepper` increments by an exact `timedelta(seconds=
  0.1)`, so there's no float-drift risk — and that `--speed` is safe to
  combine with this widget (the ribbon composites onto the pre-speed hero
  concat, so the grid's later `setpts` scales the baked-in ribbon and video
  together; the ±4s window is footage time, so at `--speed 2` a viewer
  sees ±2s of lookahead, worth knowing when retuning the window constants).

  **Separately discovered (not a note-highway bug, found via the same
  verification methodology) and since fixed: `FILENAME_RE` silently dropped
  `-START`-suffixed clips.** `FILENAME_RE` (`tesla_combine.py`) required an
  angle to be immediately followed by `.mp4`, so a clip renamed with a
  `-START` suffix (the user's own marker for where they want a final video
  to begin, on the first clip of an event folder they've otherwise trimmed
  down to just the footage they want included — not a Tesla naming
  convention) failed to match and `discover_clips()` dropped it with **no
  warning**. In the render used for the verification above, this made
  `--trim-start 17:22:00` actually start at 17:22:05 — a silent 5-second
  truncation, no error printed, output just quietly 55s instead of the
  requested 60s. Fixed: `FILENAME_RE` now accepts an optional non-capturing
  `-START` suffix before `.mp4` (the angle capture group is unaffected —
  `front-START.mp4` still yields `front`, not `front-START`); verified
  against the real affected folder that the `-START` clip is now discovered
  and correctly selected/offset for a trim window starting inside it. See
  the Gotchas section below for the `-START` convention itself.
- `--fsd-pace-notes`/`PaceNotes`: verified `--dry-run --verbose` solo and
  combined with every other `--fsd-*`/`--gauge` flag (confirms the chain
  order in both the printed compositing steps and the resulting filename),
  then a full integrated real-footage render through `tesla_combine.py`
  itself — `--fsd-pace-notes` alone and again as part of the "everything"
  combination (`--gauge --fsd-scoreboard --fsd-friction-circle
  --fsd-note-highway --fsd-pace-notes --map`), both under `--landscape
  --quality high --map --map-zoom 16` on the same real trimmed window this
  project's other recent branches used. `playcheck.sh` clean on both
  outputs. The combined render's extracted frame shows all five hero-tile
  regions populated at once (TL hero label, TR `StreakScoreboard`, the
  note-highway ribbon strip, `PaceNotes`' own callout centered directly
  below it, BL `--gauge`, BR `FrictionCircle`) with no visual overlap
  between any of them, confirming the placement claim holds in practice, not
  just in the constants' arithmetic.

  **Cross-checked against independently regenerated ground truth, not just
  eyeballed** (the same methodology the note-highway branch's own
  verification used, and the same "a single eyeballed frame is unreliable"
  lesson the friction-circle sign-convention work learned first): the exact
  GPX the solo render actually used was regenerated in a SEPARATE process
  (a fresh `build_route_gpx` call, same trim window), then run through the
  real `tesla_fsd_overlay.py` decode -> `segment_corners` ->
  `build_pace_notes` pipeline to print ground-truth corner timestamps/
  grades/directions/chain state. Real frames were then extracted from the
  actual rendered grid video at three of those corners' computed
  full-opacity timestamps (one plain "RIGHT 5", one plain "LEFT 5", and one
  CHAINED "RIGHT 6" / "into RIGHT 6" pair) and confirmed to show exactly the
  predicted text, direction chevron, and grade color in every case; a frame
  extracted from a quiet mid-drive moment between two notes' windows
  confirmed nothing is drawn there (the ephemeral/most-frames-draw-nothing
  design claim). Direction itself is not a fresh finding here -- it inherits
  `g_to_offset`'s already-confirmed-against-real-telemetry sign convention
  (see "Sign math moved to a tested, shared function" above) rather than
  re-deriving it, per this file's own repeated warning that pace-notes
  specifically needed to reuse that convention, not re-derive it.

  **Independent Fable review** (same process as every prior FSD branch, the
  most thorough one yet -- it built and ran executable repros for every
  suspicion rather than reasoning abstractly) found two real bugs, both
  fixed before commit, plus two doc-accuracy issues:
  - **A telemetry gap could fabricate corner DURATION**, not just corner
    state. `segment_corners` already treated a `None` sample as a no-op for
    hysteresis state (correct, mirrors `CornerCounter`), but every
    duration-derived decision -- `min_samples` filtering and
    `build_pace_notes`' `LONG` flag -- was computed from raw
    `end_index - start_index`, which keeps ticking through an unobserved
    gap the same as through real data. Confirmed with an executable repro:
    one real 0.2g sample, a manufactured 10-second all-`None` blackout, one
    real return-to-baseline sample -- reported as a single ten-second
    corner ("RIGHT 5 LONG"), fabricated almost entirely from unobserved
    time. A second repro showed the same gap defeating the "too short to
    call" `min_samples` filter outright. **Fixed**: `Corner` gained
    `observed_samples` (a count of only the REAL samples seen while the
    corner was open) and `gap_before` (whether the stretch immediately
    preceding this corner's start contained any missing sample);
    `min_samples`/`LONG` now key off `observed_samples`, and
    `build_pace_notes` refuses to chain two corners when `gap_before` is
    True (a real gap in the "straight road between them" means the chain's
    implicit "into" claim was never actually observed -- a third repro
    showed two corners either side of a total blackout chaining into a
    confident-looking, fabricated "into" callout before this fix). Covered
    by new tests mirroring both repros plus the chain-suppression case.
  - **A later note preempting an earlier one was an instant cut, not a
    fade** -- contradicting this branch's own "pure alpha fade, never a
    crossfade, never two panels at once" claim (the "never a crossfade"
    half was true; "pure alpha fade" wasn't). With the shipped timing
    constants, two corners spaced roughly 2-3 real seconds apart (closer
    than `LEAD_SECONDS + HOLD_SECONDS`, but too far apart to chain) produce
    overlapping windows; the old `active_pace_note` just picked the later
    note once its window opened, so the earlier note held at alpha 1.0
    until the exact frame the later note's own window began, then vanished
    in one frame while the later note popped in already fading up from
    zero. Confirmed with an executable repro before the fix. **Fixed**:
    `visible_window(notes, i, lead_samples, hold_samples)` truncates a
    note's natural window to end one sample before the FOLLOWING note's own
    natural window opens (never extends, only shortens, clamped to at least
    one sample); `active_pace_note`/`pace_note_alpha` now consult this
    truncated window instead of each note's bare natural one --
    `pace_note_alpha`'s signature changed from `(note, index, ...)` to
    `(notes, i, index, ...)` specifically so it always agrees with
    `active_pace_note` about which (possibly truncated) window is in play,
    rather than each independently re-deriving it. Verified two ways: a
    synthetic repro showing the earlier note's alpha now ramps smoothly to
    0 by the truncation point instead of being cut off at full opacity, and
    two extracted real synthetic-frame renders (one mid-fade-out on the
    earlier note, one mid-fade-in on the later note) confirmed visually.
  - **Doc drift, both fixed**: the `PACE_NOTE_GRADE_THRESHOLDS` comment (and
    this file's own severity-grading bullet above) overstated the
    corner_count/callout-count guarantee as "can never contradict each
    other" -- false, since `build_pace_notes` drops short blips, merges
    chained pairs, and `segment_corners` drops a corner still open at the
    timeline's end, none of which `CornerCounter` does. Reworded to the
    TRUE, weaker guarantee: every callout corresponds to a corner
    `CornerCounter` also counts (shared 0.15g floor), not a 1:1 count
    match. Separately, `PACE_NOTES_HOLD_SECONDS`'s comment and `PaceNotes`'
    own class docstring claimed the callout "clears `HOLD_SECONDS +
    FADE_OUT_SECONDS` after" the corner's start -- wrong: the fade-out ramp
    runs INSIDE the `HOLD_SECONDS` window, not after it, so the callout is
    actually fully gone AT `HOLD_SECONDS`, and full opacity itself ends at
    `HOLD_SECONDS - FADE_OUT_SECONDS`. `pace_note_alpha`'s own docstring
    was already the accurate description; the two other comments now match
    it.
  - **Confirmed clean** (checked against the code, not just the docs, with
    executable repros where suspicious): `g_to_offset` reuse for direction
    is literal, not re-derived; an open corner at the timeline's end is
    dropped even in the adversarial case of a trailing gap sitting inside
    it; the precompute-once architecture really does mean `draw()` never
    re-grades a corner (no flicker path exists to find); chaining really is
    at most one link with the chained corner correctly suppressed, verified
    with a three-corner case; placement really is identical whether or not
    `--fsd-note-highway` is enabled, verified by comparing the two draw()
    functions' formulas directly; alpha is applied to every drawn element
    (panel, chevron, both text lines) with no PIL text-measurement
    trailing-space bug; `end_index` semantics (first CONFIRMED-exit sample,
    not the last in-corner one) are used consistently everywhere they're
    read, though the class docstring didn't say so until this fix (now
    documents it explicitly); `tesla_combine.py`'s new `--fsd-pace-notes`
    block is a faithful, non-divergent copy of the established
    `--fsd-note-highway` pattern.

## Gotchas
- Never commit footage or rendered outputs.
- A clip filename may carry a user-added `-START` suffix (e.g.
  `..._front-START.mp4`) marking where the user wants a final video to
  begin, on an event folder they've otherwise trimmed down to just the
  footage they want included — it's the user's own convention, not a Tesla
  one. `FILENAME_RE` matches it like any other clip of that angle.
- The encode path is macOS/VideoToolbox-specific.
- `report_gap` reports clock drift from squeezed recording gaps; fixed to work
  under `--trim-end` too.
- Anything printed mid-run must go through `log()` — it clears and redraws the
  progress display around the print. A bare `print()` will be scribbled over.
