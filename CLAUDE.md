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
- **HUD-style translucent map inset (`--map-overlay`):** a small, semi-
  transparent `moving_journey_map` (the SAME widget the sidebar `--map` tile
  uses) composited directly onto a corner of the hero camera tile — additive
  alongside `--map`, not a replacement for it: both a sidebar tile AND a HUD
  inset is a valid, if unusual, combination (verified together, see
  Verification status). Scoped first as a research spike
  (`docs/translucent-map-overlay-findings.md`, `backlog-additions` branch);
  implemented by mirroring `write_gauge_layout`/`build_gauge_overlay`'s
  established pattern almost exactly (`write_map_overlay_layout`/
  `build_map_overlay`) — a `<frame bg=... opacity=...>` around a square
  `moving_journey_map`, composited via the same `gopro-dashboard.py
  --use-gpx-only <hero-video>` positional-input compositing mode `--gauge`
  uses. `build_gopro_layout_overlay` is the shared tail both `build_gauge_
  overlay` and `build_map_overlay` now delegate to (extracted during this
  branch) — the gopro-dashboard.py invocation, `--dry-run` printing, and
  progress bookkeeping were byte-for-byte identical between the two, only
  the layout-XML-writing and file-naming differed, so a second near-
  identical function would've been the same copy-paste `build_fsd_overlay`'s
  own consolidation (`FSD_OVERLAY_META`) already avoided for the FSD
  showcase overlays. GPS extraction (`build_route_gpx`) is shared with
  `--map`/`--gauge`/the FSD showcase flags the same way — one extraction
  regardless of how many are requested together. Reuses `--map-zoom` (no
  separate `--map-overlay-zoom` flag); does NOT support `--map-mag` --
  unlike `build_map_tile`, `build_map_overlay`'s compositing path has no
  synthetic-size-render-then-upscale pass to hang a magnification step off
  of (it composites straight onto the real hero video, like `--gauge`).
  Deliberately **square** (`MAP_OVERLAY_SIZE_FRAC` of the hero tile's
  *shorter* side): `moving_journey_map` is inherently square, and a square
  panel sidesteps `write_map_layout`'s crop/centre-offset trick entirely
  (that trick exists only because the sidebar tile itself isn't square).
  v1 requires a **solo hero** (same restriction as `--gauge`/every FSD
  showcase flag) — validated in `setup_tools`.
  - **Corner/collision design (the real open question a research spike this
    thin leaves unanswered):** `--gauge` owns bottom-left, `StreakScoreboard`
    owns top-right, `FrictionCircle` owns bottom-right, the note-highway
    ribbon owns a full-width band near the top — `--map-overlay` prefers
    **bottom-right** (the spike's own recommendation: avoids `--gauge` by
    construction, mirroring how `--map`'s sidebar tile and `--gauge` already
    coexist today), but that's the SAME corner `--fsd-friction-circle`
    already claims — the one real collision risk, not a hypothetical one.
    `pick_map_overlay_corner(gauge, scoreboard, friction_circle,
    note_highway)` (pure, tested) resolves it with a fixed preference order
    computed fresh in `build_grid` from that run's own flags: bottom-right →
    bottom-left (if friction-circle took bottom-right) → top-right (if
    gauge ALSO took bottom-left; treated as claimed by EITHER scoreboard OR
    the note-highway ribbon, since the ribbon's own near-top band would
    collide with a top-right panel too even though it isn't corner-anchored
    itself) → if even that's claimed, settle back on bottom-right and accept
    sharing it with the friction circle, a documented degenerate case
    (`--gauge --fsd-scoreboard --fsd-friction-circle --fsd-note-highway
    --map-overlay` all at once) rather than an invented fifth position.
    Verified against real combined renders, not just the isolated pure
    function: `--map-overlay --fsd-friction-circle` (no collision — HUD
    inset lands bottom-left instead), `--gauge --map-overlay` (both
    corners as expected, no fallback needed), and the fullest combination
    (all five hero-tile overlays) — which DOES show the documented overlap
    in the rendered frame, confirming the fallback chain reaches its
    documented last resort for real, not just in the pure-function tests.
  - **The translucency mechanism itself was NOT actually proven going in,
    despite the research spike's own claim** ("`--gauge`'s own panel
    `bg="0,0,0,180"` already renders correctly through this exact
    pipeline" — asserted from "a few minutes' scoping, not a measured
    spike", the spike's own disclaimer). Verifying `--map-overlay` against
    a REAL rendered frame (not just re-reading that claim) found it false
    as stated: gopro-overlay's `Frame` widget (`gopro_overlay/widgets/
    widgets.py`) builds its alpha mask from a SEPARATE `opacity` XML
    attribute (0.0–1.0, default 1.0), and `Frame.draw()` calls
    `rect.putalpha(self.mask)` — which OVERWRITES the entire panel's alpha
    with that mask, regardless of whatever alpha `bg=`'s own RGBA string
    embedded. Without `opacity=` also set, `bg="0,0,0,180"` renders fully
    **opaque**, not translucent — confirmed by sampling real output pixels
    (a "translucent" panel read back as pure `(0,0,0)`, not a blend with
    the video underneath) and cross-checked against gopro-overlay's own
    bundled example layouts (e.g. `layouts/default-1920x1080.xml`'s
    "gps-lock" frame), which always pair `bg=` with a separate `opacity=`,
    never rely on `bg=`'s alpha alone. Fixed in BOTH `write_map_overlay_
    layout` (this branch) and `write_gauge_layout` (`GAUGE_BG_ALPHA`, same
    root cause — `--gauge`'s panel had been silently rendering fully
    opaque since it was added, contradicting its own "semi-transparent"
    design description) by adding the matching `opacity=` attribute derived
    from the same alpha constant. One side effect worth knowing: `opacity=`
    makes the whole `<frame>` translucent, background AND children alike
    (not just a translucent box behind opaque content) — confirmed this is
    the intended look for a HUD-style overlay (some of the live video now
    genuinely shows through the map/dial/text, not just the panel's own
    padding) by looking at the real fixed render, not assumed.
- **FSD showcase overlays (`tesla_fsd_overlay.py` / `tesla_fsd_metrics.py`):**
  groundwork for four planned visual ideas (a hands-free/corner-count streak
  scoreboard, a friction-circle G-force meter, a scrolling "note highway"
  anticipation ribbon, rally pace-notes). The first three of the four — the
  streak scoreboard, the friction circle, and the note highway — are now real
  and wired into `tesla_combine.py`'s CLI as `--fsd-scoreboard`
  (`fsd-streak-scoreboard` branch, off the merged `fsd-overlay-foundation`),
  `--fsd-friction-circle` (`fsd-friction-circle` branch), and
  `--fsd-note-highway` (`fsd-note-highway` branch); pace-notes remains its own
  follow-up branch's job. `StreakScoreboard` (`tesla_fsd_overlay.py`) replaces
  the branch's original `FsdDiagnosticText` throwaway plain-text overlay as
  the default draw target (`--widget scoreboard`, vs. `--widget diagnostic`
  -- the old diagnostic is kept, not deleted, for debugging the remaining
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
  - **`build_fsd_overlay` consolidation**: the original `--fsd-scoreboard`-
    only `build_fsd_scoreboard_overlay` (`tesla_combine.py`) was generalized
    into `build_fsd_overlay(hero_video_path, gpx_path, tile_dims, widget,
    ffmpeg, venv_py, out_path, tmpdir, dry_run, progress)`, taking an
    explicit `widget` ("scoreboard", "friction-circle", or now
    "note-highway") passed straight through to the subprocess as
    `tesla_fsd_overlay.py --widget <widget>` -- a second (or third)
    near-identical function would just have been copy-paste with one word
    changed. `FSD_OVERLAY_META` holds the small per-widget bits that
    actually differ (the CLI flag name for log lines, the `Step`/`Progress`
    kind, etc) — the note-highway branch just added a third entry, no new
    `build_*` function needed. The one existing `--fsd-scoreboard` call site
    in `build_grid` now passes `widget="scoreboard"` explicitly (previously
    implicit via the driver's own `--widget` default) — confirmed, by
    rerunning every existing `--fsd-scoreboard` test immediately after this
    refactor and before adding anything friction-circle-specific, that the
    consolidation alone changes nothing about `--fsd-scoreboard`'s behavior.

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
    widget: **the two still-deferred ideas (note-highway ribbon, pace-notes)
    need this exact same convention** (e.g. pace-notes calling a corner
    "Right 3" vs "Left 3"), so they should call `g_to_offset` too, not
    re-derive the sign independently. (The note-highway ribbon has since
    landed and does follow this convention for its one axis — see "The note
    highway" bullet above for why it doesn't literally call `g_to_offset`
    itself, which is a two-axis function this single-axis ribbon doesn't
    need.)
  - **Design note, now revisited — and actually done, not just planned**: at
    the time this was originally written, `--gauge` → `--fsd-scoreboard` →
    `--fsd-friction-circle` already chained as three sequential subprocess
    renders of the hero tile (each a full decode + draw + re-encode
    generation), and note-highway then landed the same sequential-subprocess
    way too (a fourth `build_fsd_overlay` call chained through
    `angle_paths[hero_angle]`), so `--gauge --fsd-scoreboard
    --fsd-friction-circle --fsd-note-highway` together meant FOUR sequential
    hero-tile re-encode generations — measured for real on a real ~47-minute
    session, one such pass alone projected to ~3h09m, so four sequentially
    ran well over half a day. The `fsd-consolidated-overlay-pass` branch did
    the consolidation this note anticipated: `tesla_fsd_overlay.py`'s
    `--widget` flag is now `nargs="+"` (accepts multiple values),
    `create_widgets_for` builds and returns ALL requested widgets in one
    list (it always returned a list, just of length 1 before), and
    `tesla_combine.py`'s `build_fsd_overlay`/`build_grid` now collect every
    active `--fsd-*` flag and make exactly ONE `tesla_fsd_overlay.py` call
    carrying all of them — cutting those three-or-fewer sequential FSD
    passes down to ONE regardless of how many of the three flags are
    combined. `--gauge` deliberately stays its OWN separate pass (a
    genuinely different tool/subprocess, `gopro-dashboard.py`, not
    `tesla_fsd_overlay.py`) — so the ceiling drops from 4 sequential
    hero-tile passes to 2 (gauge, then one combined FSD pass), not to 1.
    `FSD_OVERLAY_META` was correspondingly trimmed to hold only per-widget
    *display* info (flag name, log label) — `kind`/`step_label`/`run_what`
    became fixed values (`FSD_OVERLAY_KIND` etc.) since there's only ever
    one combined kind now, not a per-widget lookup. The pre-existing
    per-flag filename suffix convention and `stats["*_built"]` flags were
    deliberately preserved byte-for-byte (only the *timing* accumulator
    consolidated, from three `stats["*_s"]` counters to one
    `stats["fsd_overlay_s"]`) — confirmed by rerunning every pre-existing
    filename/suffix-order test with only its stats-dict *fixture* updated
    to the new key shape (the tests' own assertions on filename/suffix/
    `*_built` shape needed no change at all), the same "confirm the
    consolidation alone changes nothing about the existing flags' behavior"
    discipline the original `build_fsd_overlay` consolidation (scoreboard →
    friction-circle → note-highway, described above) already established.
    See "Verification status" below for what a real render and an
    independent Fable review confirmed about this.
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
  verification methodology): `discover_clips()` silently dropped any `.mp4`
  that didn't match `FILENAME_RE`.** The user had renamed a clip with a
  `-START` suffix as their own personal marker (not a naming convention this
  tool needs to understand — later clarified it was never meant to be
  recognized), and `FILENAME_RE` requiring the angle to be immediately
  followed by `.mp4` meant `discover_clips()` dropped it with **no
  warning**. In the render used for the verification above, this made
  `--trim-start 17:22:00` actually start at 17:22:05 — a silent 5-second
  truncation, no error printed, output just quietly 55s instead of the
  requested 60s. First fix special-cased `-START` in the regex; reverted
  after the user clarified `-START` wasn't meant to be recognized, and it
  turned out to be too narrow anyway — the same folder also has `-SKIP`
  (×5) and `-END` suffixes from the same personal-curation habit, none of
  which a `-START`-only regex would have caught. **Real fix**: don't special-
  case any suffix at all — `discover_clips()` now warns, by name, on every
  `.mp4` that doesn't match `FILENAME_RE`, still excluding it from the
  render (correct — this tool doesn't know how to place an arbitrarily-named
  file on the timeline) but making the exclusion visible instead of silent.
  Confirmed against the real affected folder: the warning lists all 7
  renamed files by name (`-START`, 5× `-SKIP`, `-END`) in one line.
- `--map-overlay`/`pick_map_overlay_corner`/`write_map_overlay_layout`: went
  through the same real-render verification loop as every prior overlay,
  and it changed the design mid-branch rather than confirming a guess.
  - **The translucency mechanism the research spike claimed was "already
    proven" turned out NOT to work as claimed, caught by looking at real
    output pixels, not by re-reading the spike's own assertion.** A first
    real render's HUD panel read back as solid opaque black at the pixel
    level, not the intended ~70% translucent blend with the video
    underneath — traced to a real bug in how `bg="R,G,B,A"` interacts with
    gopro-overlay's `Frame` widget (see the design-notes bullet above for
    the mechanism). Fixed in both `write_map_overlay_layout` (this branch)
    and, since it's the exact same root cause, `write_gauge_layout`
    (`--gauge` had been silently rendering its "semi-transparent" panel
    fully opaque since it was added). Re-verified after the fix by sampling
    real pixels again: the panel border now blends with the video
    background as expected (measured values matched the predicted
    alpha-180-over-background blend to within rounding), and the map
    content itself is now also genuinely translucent (video visibly shows
    through), which — after looking at the actual result, not just fixing
    the number — is the correct look for a "HUD-style" map, not an
    unwanted side effect of the fix.
  - **Placement/sizing** (`MAP_OVERLAY_SIZE_FRAC=0.32`, `_MARGIN=24`,
    `_PAD_FRAC=0.05`, `_LINE_WIDTH=4`) reached the confirmed-against-a-
    real-frame stage on the first pass (after the alpha fix above) — no
    further iteration needed. `_BG_ALPHA` started at 180 (matching
    `--gauge`'s own panel) but was lowered to **110** (~43% opaque, down
    from ~71%) after a user review of a real render asked for the HUD to
    read as more see-through — re-verified against a real render at 110:
    the panel border/edges now visibly blend with the live video
    underneath at a glance, not just measurably in a pixel sample. This is
    now the CLI *default*, not the only option: `--map-overlay-alpha
    0-255` (validated, same pattern as `--map-zoom`'s range check) threads
    a user-chosen value through `build_map_overlay`/`write_map_overlay_
    layout`'s `bg_alpha` parameter — added after the same user review
    asked whether translucency was overridable at all. Verified end-to-end
    at a deliberately extreme value (40, far below the default) against a
    real render: the map genuinely reads as far more see-through, not just
    a number that changed in the XML.
    Verified against real Tesla footage (a real mountain drive,
    `--landscape --quality high`, the same footage/window every other
    overlay in this file was verified against): route line, position dot,
    and street/river labels are all legible at this size/opacity over live
    moving video, including where the road surface shows through the
    translucent border — the exact thing the research spike flagged as
    genuinely unproven (a sidebar tile sits on a plain background; this
    competes with live video). No collision with the hero label (top-left)
    or the sidebar column in any
    layout tested.
  - **Corner/collision design verified against real combined renders**, not
    just `pick_map_overlay_corner`'s own unit tests: `--map-overlay`
    `--fsd-friction-circle` together (the one real collision risk) shows
    the HUD inset falling back to bottom-left with no overlap; `--gauge
    --map-overlay` shows both in their preferred corners; `--map
    --map-overlay` together (sidebar tile AND HUD inset at once) shows
    both legibly, confirming that additive combination is genuinely usable
    and not just technically non-crashing; and the fullest combination
    (`--gauge --fsd-scoreboard --fsd-friction-circle --fsd-note-highway
    --map-overlay`) shows the documented last-resort overlap (HUD inset
    sharing bottom-right with the friction circle) actually happening in a
    real rendered frame, not just asserted in the docstring/tests -- a
    known, accepted limitation of that specific five-overlay combination,
    not a silent bug.
  - `playcheck.sh` clean (decode integrity, faststart, hw-decodable dims,
    CFR) on every one of the real renders above.
  - **Independent Fable review found one real, high-value bug beyond the
    alpha fix above, plus two documentation-accuracy issues** (the review
    independently re-derived the `bg=`/`opacity=` mechanism from
    gopro-overlay's own source before trusting this file's account of it,
    and separately enumerated all 16 `pick_map_overlay_corner` input
    combinations against its docstring). All three fixed and re-verified:
    - **Real bug: the burned-in clock collides with the bottom-left
      fallback under `--landscape`.** `pick_map_overlay_corner` modeled
      `--gauge`/`--fsd-scoreboard`/`--fsd-friction-circle`/
      `--fsd-note-highway` as the only things that can occupy a corner --
      it never accounted for `_apply_tail`'s own burned-in clock
      (`x=20:y=h-th-20` on the FINAL CANVAS, whenever labels are on). Under
      `--landscape` the hero tile spans the full canvas height at the left
      edge, so canvas bottom-left IS hero-tile bottom-left, unconditionally
      -- meaning `--landscape --fsd-friction-circle --map-overlay` (no
      `--gauge` needed) fell back to bottom-left as designed, straight into
      the clock's own territory. Confirmed with a real render *before* the
      fix: the clock's text box was drawn directly over the map inset's
      bottom edge. Fixed: `pick_map_overlay_corner` gained a
      `clock_bottom_left` parameter (treats bottom-left as claimed when
      set, same as `gauge`), and `build_grid` passes
      `args.landscape and tools.has_text` -- exactly the condition under
      which the clock actually lands there. Re-verified against the same
      real render: the panel now falls all the way to top-right, clear of
      the clock, the friction circle, AND the hero label. The equivalent
      risk in the DEFAULT (non-landscape) grid -- the hero row can also end
      up as the canvas's bottom row on a low-camera-count session (e.g.
      only front+back present, since `build_rows` puts the hero row last
      when there's just one other row group) -- is deliberately NOT modeled:
      it depends on which OTHER camera angles are present, not just which
      overlay flags are active, doesn't fit this function's pure
      flags-in-corner-out signature, and is a PRE-EXISTING exposure
      `--gauge`'s own bottom-left panel has always had, not something this
      branch creates or worsens -- confirmed for the `--landscape` case
      specifically (a real `--gauge --landscape` render shows the exact
      same clock-over-panel overlap this branch's fix addresses for
      `--map-overlay`), though the tall-grid low-camera-count variant of
      the same underlying exposure was reasoned about from `build_rows`'s
      own row-ordering logic, not separately rendered. Left as a known,
      documented, deferred gap either way.
    - **Doc fix: `MAP_OVERLAY_PAD_FRAC`'s comment (and `write_map_overlay_
      layout`'s docstring) had described the pad as separating a
      translucent border from a "fully opaque map raster" -- wrong, given
      the alpha-mechanism fix above: `Frame`'s `opacity=` mask applies to
      the WHOLE rect uniformly, map raster included (`Frame`'s own
      docstring: "makes a child controllably transparent"), which this
      file's own Verification-status entry above already established for
      real ("the map content itself is now also genuinely translucent").
      The pad comment just hadn't been updated to match. Fixed to describe
      the pad as a border-width control only, not a translucency boundary.
    - **Doc fix: `pick_map_overlay_corner`'s docstring point 4 mis-stated
      its own trigger condition** ("--gauge, --fsd-scoreboard (or
      --fsd-friction-circle... but see below)" -- friction-circle is
      actually a REQUIRED conjunct to reach point 4 at all, not an
      alternative to scoreboard, and "but see below" dangled with nothing
      following it). The code itself was already correct for all 16 input
      combinations (confirmed by the reviewer enumerating them); only the
      prose was garbled. Fixed to state the real condition plainly:
      `friction_circle AND (gauge OR clock_bottom_left) AND (scoreboard OR
      note_highway)`.
    - Two further review notes were investigated and are recorded above
      rather than requiring a code change: whether `--gauge`'s own dial/
      compass/text stayed legible now that the opacity fix also makes
      those children translucent (re-rendered `--gauge` alone after the
      fix and visually confirmed: dial, needle, compass letters, big speed
      readout, and sparkline chart are all clearly legible at ~70% alpha
      over live video -- the translucency reads as intentional HUD styling,
      not a regression); and a couple of minor test-coverage gaps (an
      untested corner combination, the no-GPS degradation path) below the
      "real bug" bar this project's reviews use to gate a fix, some of
      which were still added anyway since they were cheap.
- **FSD overlay consolidation (`fsd-consolidated-overlay-pass` branch):**
  the four-sequential-hero-tile-passes problem the friction-circle branch's
  own "Design note, now revisited" flagged is fixed for real, not just
  replanned again -- see that note (above, under the friction-circle
  bullet) for the mechanism. Confirmed multiple independent ways, not just
  by reading the diff:
  - **`--dry-run --verbose`** with all three `--fsd-*` flags plus `--gauge`
    together prints exactly ONE `tesla_fsd_overlay.py` command line (`grep
    -c tesla_fsd_overlay.py` on the output == 1), carrying
    `--widget scoreboard friction-circle note-highway` as one invocation --
    not three separate command lines. The job plan line also collapsed from
    what would have been three separate step labels to one: `Plan: 12 steps
    (6 concat, GPS extract, map render, map upscale, gauge render, FSD
    overlay render, grid)` -- one `FSD overlay render` step, not three.
  - **A real render** (`--landscape --quality high --map --map-zoom 16
    --gauge --fsd-scoreboard --fsd-friction-circle --fsd-note-highway`, the
    same real footage/trim-window every overlay branch in this file has
    been verified against, 60s of real drive) completed in 2m57s wall clock
    total. The STATS section shows exactly the structural change this
    branch claims: `gauge overlay 54s` and ONE consolidated
    `FSD overlay (scoreboard+friction-circle+note-highway) 59s` line -- not
    three separate FSD timing lines -- i.e. 2 hero-tile re-encode passes for
    this run (gauge, then one combined FSD pass), down from what would have
    been 4 before this branch (gauge + 3 separate FSD passes). The
    intermediate hero-tile filename also reflects one combined FSD pass:
    `..._front_scoreboard_friction-circle_note-highway.mp4`, a single file,
    not three. The FINAL grid filename kept its pre-existing per-flag
    suffix shape unchanged, confirming the "preserve the naming convention,
    consolidate only the timing/pass count" design held for real, not just
    in the unit tests: `..._grid_landscape_gauge_scoreboard_friction-circle_
    note-highway_map.mp4`.
  - `playcheck.sh` clean on the real render's output (decode integrity,
    faststart, hw-decodable dims at 3378x1876, CFR at 1321 frames).
  - **Visual regression check**: a frame extracted from partway through the
    real render's output and cropped to the hero tile shows all four
    hero-tile overlays rendering exactly as designed, with no collisions
    and no visible change from their pre-consolidation appearance -- the
    whole point being identical visual output via a cheaper pipeline, not a
    different one: hero label top-left ("FRONT"), `StreakScoreboard`
    top-right ("FSD ENGAGED", hands-free/corner/peak-G/takeover stats),
    `NoteHighway` ribbon directly below both top-anchored elements (with its
    full PAST/NOW/AHEAD/CORNERING SEVERITY legend intact), `--gauge`'s
    dashboard bottom-left (dial/compass/speed/chart, plus the burned-in
    clock in the same corner), and `FrictionCircle` bottom-right (ringed
    target, current dot, "peak this corner" readout) -- all five hero-tile
    overlays (four FSD/gauge + the burned-in clock) coexisting exactly as
    the pre-existing corner/collision design intended.
  - Regression coverage: all pre-existing FSD/gauge/map-overlay tests
    (filename suffix, `_built` flags, dry-run `--widget` command
    assertions) pass unmodified after updating only their stats-dict
    fixture shape (three `_s` timing keys -> one `fsd_overlay_s`, the three
    `_built` flags kept as-is) -- confirming the consolidation really is
    behavior-preserving for every pre-existing flag combination, the same
    way the original `build_fsd_overlay` generalization (scoreboard ->
    friction-circle -> note-highway) was confirmed. New tests were added
    specifically for the consolidation itself (not covered by any
    pre-existing test, which only ever exercised one `--fsd-*` flag at a
    time or checked filename shape): monkeypatching `build_fsd_overlay` to
    count/inspect calls confirms `--gauge --fsd-scoreboard
    --fsd-friction-circle --fsd-note-highway` together produces exactly ONE
    call carrying all three widget names in order, and a second test
    confirms a partial combination (two of three flags) also collapses to
    one call carrying just those two names, not a spurious third.
  - **Independent Fable review**: read the full diff plus the surrounding
    unchanged code it touches (`Progress`, `build_grid`, `plan_steps`,
    `print_stats`, the suffix-construction block, `tesla_fsd_overlay.py`'s
    whole `main()`), reran the test suite independently, and exercised the
    new `nargs="+"` argparse behavior for real under `./.venv` rather than
    just reading it. Found no real bugs in the consolidation logic itself —
    the widget build order (fixed by build_grid's own
    `(scoreboard, friction-circle, note-highway)` tuple, not CLI argument
    order, and confirmed to reproduce the old sequential passes' painter's-
    algorithm layering), the `lateral_g_timeline` membership-check plumbing,
    the `diagnostic`-combination rejection, the ETA/rate-tracking math (one
    `fsd_overlay_render` step's `work` correctly covers what used to be up
    to three steps' work, since `Progress` costs/recalibrates purely by
    `kind`, not step count), and the `stats["*_built"]`/filename-suffix
    preservation across every flag combination (including zero-flags/
    no-GPS) were all independently confirmed correct — including confirming
    for real that `--widget`'s new `nargs="+"` doesn't reintroduce the
    positional-argument mis-assignment class of bug this project's own
    `gopro-dashboard.py` history warns about (build_fsd_overlay's command
    shape keeps positionals first, `--widget` last with its values
    trailing; `choices=` also rejects a malformed invocation loudly instead
    of silently mis-parsing).

    It did catch three real, pre-commit issues, all fixed and re-verified:
    - A dangling `[see below]` placeholder in this very file (this bullet
      you're reading now is the fill-in).
    - `tesla_combine.py`'s own module docstring (its `Output:` block) still
      listed the three old separate per-widget intermediate filenames
      (`..._scoreboard.mp4`, `..._friction-circle.mp4`,
      `..._note-highway.mp4`) even though `README.md`'s equivalent table
      had already been updated to the new combined `<widgets>`-joined form
      — fixed to match.
    - **A real dead-code bug**: `plan_steps` built a widget-detailed step
      label (`f"FSD overlay render ({'+'.join(active_fsd)})"`), but
      `build_fsd_overlay` called `progress.begin()` with the fixed
      `FSD_OVERLAY_STEP_LABEL` constant instead — and `Progress._advance`
      always overwrites the planned label with whatever `begin()` passes,
      so the widget detail silently never reached the running/done log
      lines (a real information loss vs. the old three-separate-steps
      design, which showed which widget was rendering). Fixed by building
      the same `f"{FSD_OVERLAY_STEP_LABEL} ({'+'.join(widgets)})"` label
      inside `build_fsd_overlay` itself and passing that to `begin()` —
      confirmed with a direct call against a stubbed `Progress`/`run` that
      the label reaching `begin()` now really does carry the widget names
      (`"FSD overlay render (scoreboard+friction-circle+note-highway)"`),
      not just that the string is built somewhere unused.

    Also flagged, and judged worth fixing since they were cheap (below the
    "real bug" bar, same convention every prior review's cheap-fixes have
    followed): a garbled sentence in `create_widgets_for`'s own docstring
    (rewritten); a CLAUDE.md sentence claiming pre-existing tests were
    rerun "unmodified" when their stats-dict *fixtures* (not their
    assertions) had in fact been updated to the new key shape (reworded to
    say precisely that); and two test-coverage gaps — no test pinned the
    exact `kind` string `build_grid`'s no-GPS `progress.abandon(...)` call
    passes (a typo there would leave a step stuck "pending" forever with
    nothing to fail), and no dry-run command-string test covered a
    two-of-three widget subset (only the all-three and single-widget cases
    had one) — both added as new tests (`test_fsd_overlay_step_abandoned_
    when_no_gps`, `test_fsd_overlay_dry_run_two_widget_subset_command`,
    `tests/test_combine_layout.py`). Not flagged as bugs, only recorded:
    the `else` fallback in `create()` maps any unrecognized widget name to
    `StreakScoreboard` (pre-existing shape from before this branch, and
    unreachable from `tesla_combine.py` since its own three flags are the
    only source of widget names — a direct caller bypassing `main()`'s
    `choices=` validation is the only way to hit it) and duplicate widget
    names are silently accepted by the same unreachable path. Full test
    suite: 318 passing after these fixes (up from 313 at the start of this
    branch, up from 316 before this review's two added tests).

## Gotchas
- Never commit footage or rendered outputs.
- The user personally renames clips they've curated for a final video with
  suffixes like `-START`/`-SKIP`/`-END` (their own convention, not
  something this tool interprets) — `discover_clips()` doesn't try to
  understand these, it just warns by name on any `.mp4` that doesn't match
  `FILENAME_RE` and excludes it from the render, rather than silently
  shrinking the output.
- The encode path is macOS/VideoToolbox-specific.
- `report_gap` reports clock drift from squeezed recording gaps; fixed to work
  under `--trim-end` too.
- Anything printed mid-run must go through `log()` — it clears and redraws the
  progress display around the print. A bare `print()` will be scribbled over.
