# Elevation feature — implementation plan

Builds on the earlier research spike (`docs/elevation-findings.md` on the
`backlog-additions` branch). This plan is deliberately **not accompanied by
any code** — the network-dependency decision in Section 1 needs the user's
explicit sign-off before anything gets built, not a unilateral pick made
while they were away. Nothing here has been implemented; no branch exists
for it yet.

---

## 1. The network-dependency decision — go/no-go, for the user to make

**What would actually be sent to a third party:** every lat/lon point (or a
downsampled subset of them) from the GPX built for that render — i.e., the
real geographic trace of a real drive, including wherever it starts and
ends (frequently home). Worth naming plainly: this project currently has
exactly one network dependency (OSM map tiles for `--map`, fetched by
*tile* — a bounding box, not a specific point trace), and both of the free
per-point options below have no stated privacy policy on retention/logging
of query traffic. Adding this is a step beyond that existing precedent,
not a repeat of it.

**Sanity-checking the research doc's recommendation, not just restating
it:** `docs/elevation-findings.md` recommends "start with the free-API
option" without picking between open-elevation.com and USGS EPQS, and
dismisses a bundled offline dataset mainly on "more work for a first
spike" grounds. That doesn't fully weigh the actual tradeoffs:

| Option | Cost | Reliability | Coverage | Privacy | Batching |
|---|---|---|---|---|---|
| **open-elevation.com** | Free | Unproven — small community-run instance, no SLA, known history of extended downtime | Global | Sends real per-point route data to a third party with no stated retention policy | Yes — real batch POST endpoint |
| **USGS EPQS** | Free | Government service, generally solid | **US-only** | Same GPS-trace privacy concern, but a US federal service is a more predictable actor than a volunteer-run free tier | **No** — single-point GET only; must be many individual (cacheable) requests |
| **Google/Mapbox Elevation** | Paid, real $/call | High | Global | Same privacy concern, plus the drive data goes to Google | Yes |
| **Bundled/offline dataset (e.g. clipped SRTM)** | Free after one-time download | No per-render network dependency at all | Depends what's bundled | **Materially different**: a one-time regional download doesn't leak a specific drive's route as a live query; it's a blind area fetch, not a route fetch | N/A — local lookup |

For this user's footage (US Tesla dashcam driving), **USGS EPQS is the
more defensible default of the two free live-API options** — not
open-elevation as the research doc leaned toward — precisely because it
doesn't have open-elevation's uptime/trust profile, even though it costs
batching (mitigated by required caching anyway) and is US-only (a real
limitation only if footage is ever driven outside the US/its territories).

The **offline/bundled dataset** option should also be weighed higher than
the research doc gave it. Its "more work" downside is real, but it's the
only option that meaningfully changes the privacy story — no live
per-drive query to any third party at render time, "closer to this
project's existing OSM-tiles-are-the-only-network-dependency posture" as
the research doc itself puts it, just for a data *pull* rather than a live
*query stream* keyed to the user's actual trips. It's a real third option,
not a footnote.

**What this costs if the answer is yes to a live API:**
- **Latency**: adds real wall-clock time to an already-slow batch
  pipeline, the same lesson `--map-zoom` learned the hard way (a
  92-minute drive needed ~170,000 tiles before `--map-zoom` got tuned
  down). Elevation is structurally cheaper than tiles — cost grows
  *linearly* with route distance if downsampled by distance-traveled, not
  quadratically with the bounding box the way tile count does — but it's
  still a new per-render network round-trip that didn't exist for
  `--gauge`/`--fsd-*` before.
- **Reliability**: a render can now fail, or degrade, because of a third
  party's uptime — something no other feature in this tool depends on
  except `--map`.
- **Money**: $0 for the free options; real, ongoing $/call for
  Google/Mapbox-grade accuracy or a real SLA.

**This is a decision point, not a foregone conclusion.** The
recommendation in Section 4 is for the user to approve, not something
already acted on.

---

## 2. Option A implementation plan (assuming sign-off on a specific service)

### Where it slots into the existing pipeline

`build_route_gpx` (`tesla_combine.py`) already has the right shape: it
calls `tesla_gps.extract_samples` per clip → `retime_samples` (pure
re-timing) → `tesla_gps.write_gpx`. Elevation enrichment is a **new step
inserted between `retime_samples` and `write_gpx`**:

```
clip_samples → retime_samples() → enrich_elevation() → write_gpx()
```

This mirrors exactly how `--map`/`--gauge` already share GPS extraction —
`enrich_elevation` only runs when a new `--elevation` flag is set, so it
costs nothing for users who don't opt in, same pattern as
`--map`/`--gauge`/`--fsd-*`.

### New module: `tesla_elevation.py`

Following `tesla_fsd_metrics.py`'s precedent (pure stdlib, zero
`gopro_overlay` dependency, so `tests/` under system Python can exercise
it directly):

- **Downsampling**: walk the retimed sample list and select query points
  spaced at least `ELEVATION_SAMPLE_DISTANCE_M` (~75–100m, tunable, the
  research doc's own suggestion) apart, using a small stdlib haversine
  helper — elevation genuinely doesn't need per-frame resolution the way
  position does.
- **Caching**: keyed by lat/lon rounded to ~4-5 decimal places (~1–11m
  precision), persisted as JSON. Unlike the existing per-run `CACHE_FILE`/
  `load_cache`/`save_cache` (keyed to one output directory), this cache
  should be **global/persistent across events** (e.g. a dotfile in the
  user's home or a repo-level cache dir) — repeated drives on the same
  commute/regular routes should get real cache hits across entirely
  different renders, a materially better hit rate than the existing
  per-run concat cache gets. This is the single most important piece to
  get right per the `--map-zoom` lesson, and should be non-optional in
  v1, not a follow-up.
- **Batching**: chunk downsampled points into the provider's batch
  endpoint if it has one (open-elevation.com does; USGS EPQS doesn't, so
  USGS means many small cacheable single-point requests instead — a real
  practical cost of picking USGS, on top of the reliability upside).
- **Interpolation**: after fetching elevation only at the downsampled
  points, linearly interpolate for every retimed sample between its two
  bracketing queried points — cheap, and honest about not claiming more
  precision than a coarse dataset provides.
- **Graceful degradation**: if the API is unreachable mid-run, this
  should warn and skip elevation for the run (matching `retime_samples`'s
  established "never silently fabricate — a gap must show as a gap"
  principle already used for the FSD fields), not crash the whole render.

### GPX field — a genuinely simpler mechanism than the FSD `<cad>`/`<power>`/`<hr>` trick

GPX already has a first-class `<ele>` element, and `gopro_overlay/gpx.py`'s
`fudge()` already reads `point.elevation` straight into a `GPX.alt` field
via `gpxpy` — no extension-tag repurposing hack needed at all (unlike
speed/accel/autopilot state, which needed the
`<speed>`/`<cad>`/`<power>`/`<hr>` trick because `fudge()` only recognizes
a fixed handful of extension tag names). `write_gpx` just needs
`<ele>{elevation_m:.2f}</ele>` added per-point when known. And critically,
`layout_xml.py`'s `metric_accessor_from` **already has `"alt"`/
`"altitude"` wired to an `altitude_unit` converter** — so this data, once
in the GPX, is immediately usable by any existing `metric`/`chart`/`msi`
XML component with zero new `gopro_overlay` plumbing.

### The actual visual feature — two concrete tiers, v1 recommended

- **v1 (small, recommend shipping this with Option A)**: extend
  `write_gauge_layout`'s existing panel with an "ALT" numeric readout plus
  a sparkline chart, exactly mirroring the existing `SPEED, LAST {N}s`
  chart (same `SimpleChart` widget, `metric="alt"`, same layout math
  already in `write_gauge_layout`). Nearly free given the `alt` accessor
  already exists — no new widget code, no new compositing pass, just more
  XML and a few plumbing lines.
- **v2 (bigger, a possible follow-up, not required)**: a dedicated
  whole-drive elevation-profile ribbon with a "now" playhead,
  architecturally similar to `NoteHighway` — needs the same
  full-timeline-lookahead precomputation (`elevation_timeline` built once
  before `Overlay(...)` construction) and its own widget file. A
  materially bigger lift than v1 and shouldn't gate shipping Option A.

---

## 3. Option B implementation plan (no-network relative climb/descend cue)

Reuses `linear_acceleration_mps2_z`, already decoded but not yet plumbed
through `retime_samples`/`write_gpx` (only `_x`/`_y`/`autopilot_state` are
today) — that plumbing itself is a small, precedented addition.

**Filtering/detrending needed to make it even qualitatively usable:**
- Raw double integration of `accel_z` drifts unboundedly — the FSD IMU
  work's own axis-mapping only identified `_z` as "vertical" *by
  elimination* (lowest variance, no correlation with steering or
  speed-derivative), not independently validated against real elevation
  ground truth, so the input signal itself is on shakier empirical footing
  than Option A's already-fine GPS data.
- Minimum viable approach: a short sliding-window high-pass (subtract
  rolling mean) before the first integration to velocity, detrend again
  before the second integration to relative elevation, and **periodically
  re-zero** (e.g. every 10–20s) rather than let error accumulate over a
  whole drive — bounding error to within one window instead of the whole
  trip.

**Honest limits, stated plainly:** qualitative only (climbing/level/
descending), not a value in meters; only trustworthy over short windows
(seconds, maybe up to a minute); cannot support a whole-drive elevation
chart or profile; will show visible discontinuities at each re-zero. The
underlying axis-identity assumption is itself unconfirmed against real
ground truth, unlike lateral/longitudinal G which the FSD overlay work
did confirm.

**Visual**: given the signal can't honestly support a chart, a small
trending-icon (up-arrow/level/down-arrow) in the `--gauge` panel is
proposed rather than a sparkline — a chart would imply more precision
than the data can support.

**Scope**: a filter/detrend function in a small new pure-Python module
(or folded into `tesla_fsd_metrics.py`-style code) plus the small
`retime_samples`/`write_gpx` plumbing for `linear_acceleration_mps2_z`,
plus a tiny icon widget. No network code, no caching/batching layer, no
privacy question at all.

---

## 4. Recommendation (for the user's approval, not acted on)

- **If comfortable with a real per-drive GPS trace going to a third
  party**: go with Option A, v1 visual (gauge panel readout + chart).
  Within Option A, default to **USGS EPQS** rather than the research
  doc's uncritical lean toward open-elevation.com — a US federal service
  is a more predictable long-term dependency than a volunteer-run free
  API, at the cost of losing batching (mitigated by caching anyway) and
  US-only coverage (a non-issue for footage that's actually US driving).
- **If the privacy question gives pause but real elevation values are
  still wanted**: seriously consider the **bundled/offline dataset** path
  instead of a live API — more work up front, but the only option that
  avoids sending real per-drive route data to anyone at render time, and
  the option most consistent with this project's existing "OSM tiles are
  the only network dependency" posture. Worth weighing as a genuine
  contender, not a throwaway third option.
- **If zero new dependencies and zero privacy exposure is preferred at
  all**: Option B, framed honestly to users as a coarse, qualitative
  "trending" cue, not a real elevation chart.

Leaning toward Option A/USGS EPQS if forced to pick, but this genuinely is
the kind of call — new external dependency, real data leaving the machine
— that should be the user's, not something built toward unilaterally.

---

## 5. Verification plan (matching this project's existing rigor standard)

Following the bar CLAUDE.md's "Verification status" section sets for
every other feature here (nothing ships on "looks right in a synthetic
test" alone):

- **Ground-truth sanity check**: cross-check fetched/interpolated
  elevation values for a real drive with a known, obvious elevation
  change (the 17.5km real mountain drive already used and characterized
  for the FSD friction-circle/note-highway work is a strong candidate)
  against an independent source (e.g. a topo map or Google Earth
  elevation profile for that road) at several spot-checked points.
- **Caching correctness**: verify a second render of the same event makes
  zero new network calls (log/count them), and that a *different* event
  whose route overlaps a previous one gets real cache hits.
- **Downsampling/interpolation honesty**: spot-check a few interpolated
  mid-segment values against explicitly-queried points to confirm
  interpolation isn't fabricating false precision.
- **Degraded-network behavior**: simulate the API being unreachable
  mid-run and confirm the render warns and skips elevation cleanly rather
  than crashing or hanging (same "gap must show as a gap" principle
  already established for the FSD fields' handling of telemetry gaps).
- **End-to-end GPX→gopro-overlay wiring**: confirm `<ele>` written by
  `write_gpx` really does reach `entry.alt` and drives the new chart/
  readout correctly — needs a real venv render, not just `--dry-run`.
- **Real-footage integrated render**: `--gauge --elevation` (flag name
  TBD) against real Tesla footage with genuine elevation change,
  `playcheck.sh` clean, and combined with `--fsd-*`/`--landscape --map`
  to confirm no panel collisions, the same combination-testing pattern
  every other overlay here has gone through.
- **For Option B specifically**: an explicit, candid check of whether the
  derived qualitative up/down cue actually agrees with reality at a few
  spot-checked timestamps on that same known-elevation-change drive — and
  document plainly if it doesn't hold up, rather than shipping an
  unconfirmed claim the way `TakeoverCounter` is currently honestly
  flagged as untested against a real disengagement.

---

## 6. Scope/complexity estimate vs. the FSD branches already shipped this session

- **Option A, v1 (gauge panel readout + chart)**: **smaller** than any
  single FSD branch (scoreboard/friction-circle/note-highway/pace-notes).
  No new compositing subprocess pass, no new sign-convention math, no PIL
  drawing/widget code — just a new ~150–250 line pure-Python module
  (comparable to `tesla_fsd_metrics.py`) for fetch/cache/downsample/
  interpolate, small plumbing additions to `retime_samples`/`write_gpx`/
  `write_gauge_layout`, and a new CLI flag. The genuinely *new*
  complexity this repo hasn't dealt with before is the network/caching/
  batching/degraded-network-handling layer — none of the FSD branches
  needed that, since they're pure local math on data already in the GPX.
- **Option A, v2 (dedicated elevation-profile ribbon)**: comparable to or
  bigger than `--fsd-note-highway` (needs the same full-timeline-
  lookahead architecture and its own widget file), plus the network layer
  underneath it. Not recommended as v1 scope.
- **Option B**: **smaller** than any FSD branch shipped this session — a
  filter/detrend function, a small plumbing addition for `accel_z`, and a
  tiny icon widget, no new subprocess pass if folded into the existing
  `--gauge` panel.

---

### Critical files for implementation

- `tesla_gps.py` — `SEI_FIELDS`, `write_gpx` (add `<ele>` emission)
- `tesla_combine.py` — `build_route_gpx`, `retime_samples`,
  `write_gauge_layout`, CLI arg wiring, `load_cache`/`save_cache` pattern
  to mirror for the elevation cache
- `.venv/lib/python3.12/site-packages/gopro_overlay/gpx.py` — confirms
  `<ele>`/`alt` is native, no extension-tag hack needed
- `.venv/lib/python3.12/site-packages/gopro_overlay/layout_xml.py` —
  confirms `metric_accessor_from` already wires `"alt"`/`"altitude"`
- `tesla_fsd_metrics.py` — the precedent to follow for a new
  dependency-free, testable module (`tesla_elevation.py`)
- `CLAUDE.md` — the architecture/verification-standard doc that would
  need updating once this lands
- `docs/elevation-findings.md` (on `backlog-additions`) — the research
  spike this plan builds on and sanity-checks
