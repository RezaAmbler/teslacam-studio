# teslacam-studio

Turn a Tesla **Sentry / Dashcam** event folder into a single, labeled multi-camera
video — with a burned-in wall clock, optional face blurring, and a stack of
optional overlays driven by the telemetry Tesla embeds in the footage: a live GPS
route map, a speed/compass dashboard, and three FSD driving-showcase panels.

![teslacam-studio — landscape layout: the front camera at native resolution with every overlay active (FSD streak scoreboard top-right, note-highway cornering ribbon below it, translucent speed/compass dashboard bottom-left, friction-circle G-meter bottom-right, burned-in clock), the other five cameras and the live GPS route map in the sidebar column](docs/example.jpg)

*`--landscape --quality high --map --gauge --fsd-scoreboard --fsd-friction-circle --fsd-note-highway`*

Tesla saves each camera as its own ~1-minute clip. teslacam-studio stitches them
per-camera, then composites them into one video you can actually watch.

## Two layouts

- **`--landscape`** — the featured camera at **full native resolution** on the
  left, every other present camera (and the route map, if any) stacked
  single-file in a thin sidebar column on the right, sized so the sidebar's
  height matches the hero's exactly. A real landscape aspect ratio, and the
  layout the hero-tile overlays are designed around. This is what you want for
  YouTube/social feed video, and what the screenshot above shows.
- **default (tall grid)** — cameras stacked in rows: the featured camera large,
  repeaters and pillars paired, back paired with the moving map. Fine for a quick
  look at an incident, but on a full six-camera session the stacked height can
  push the canvas past the hardware encoder's 4096px cap, forcing a whole-canvas
  downscale that softens the featured camera along with everything else.
  `--landscape` avoids that structurally — the hero tile is never scaled at all.

---

## Features

- **Multi-camera grid** from whichever of the 6 angles are present (front, back,
  left/right repeater, left/right pillar) — you don't need all six.
- **Burned-in wall clock** computed from the clips' real timestamps (`--no-labels`
  turns it and the per-tile labels off).
- **Trim** to a wall-clock window (`--trim-start 18:59:00 --trim-end 19:02:00`).
- **Feature/hero** any camera or pair large (`--feature back`, `--feature repeaters`).
- **Speed up** playback (`--speed 2`).
- **Face blurring** for privacy (`--blur-faces`, via [`deface`](https://github.com/ORB-HD/deface)),
  with tunable mode, detection threshold, and a downscaled detection pass.
- **Six overlays** driven by the car's own telemetry — a route map, a HUD map
  inset, a speed/compass dashboard, and three FSD showcase panels. See
  [Overlays](#overlays) below.
- **Hardware-accelerated** H.264 encode (Apple VideoToolbox), with automatic
  scale-to-fit so the output stays on the hardware decode path — or force
  software encoding for its own sake with `--quality high` (libx264, CRF 18).
- **Caching** — per-camera concats are cached; re-running with different grid
  options doesn't re-stitch. Ctrl-C is safe: the half-written file is removed and
  the finished cameras are reused on the next run.
- **Live progress** — one display for the whole job: which step of how many is
  running, how far into it, and an ETA for the run as a whole (below).
- **Pre-flight checks** — free disk space on the target volume is estimated and
  compared before anything renders (`--skip-space-check` to bypass);
  Tesla-encrypted clips are detected and reported with a fix rather than failing
  as an opaque decode error; and any `.mp4` that doesn't match Tesla's clip
  naming is named in a warning instead of being silently dropped.

## Overlays

Every overlay reads the same SEI telemetry (see
[How the GPS map works](#how-the-gps-map-works)) and needs `gopro-overlay` in
`./.venv`. Requesting several together extracts the GPS **once**, not once per
overlay.

| Flag | What it draws | Where |
|------|---------------|-------|
| `--map` | A moving route map that follows the car and traces where you drove. Tunable zoom (`--map-zoom`) and magnification beyond OSM's limit (`--map-mag`). | Its own tile — paired with the back camera in the grid, or in the sidebar under `--landscape` |
| `--map-overlay` | The *same* moving map widget, small and translucent, as a HUD panel. Reuses `--map-zoom`; translucency via `--map-overlay-alpha 0-255` (default 110, ~43% opaque). Additive with `--map` — a sidebar tile **and** a HUD inset together is valid. | A free corner of the hero tile (see corner allocation below) |
| `--gauge` | Speedometer dial, compass, big speed readout, and a recent-speed sparkline chart. `--gauge-units mph\|kph`. | Bottom-left of the hero tile |
| `--fsd-scoreboard` | A color-coded "FSD ENGAGED"/"FSD OFF" badge, hands-free time, corner count, peak cornering G, and takeover count. | Top-right of the hero tile |
| `--fsd-friction-circle` | The classic motorsport G-G diagram: lateral G vs. longitudinal G on a ringed target (rings at 0.2g/0.4g), with a fading 3-second trail and a "peak this corner" readout. Smooth FSD driving traces clean arcs; jerky driving scatters. | Bottom-right of the hero tile |
| `--fsd-note-highway` | A horizontal scrolling ribbon of signed cornering severity, "now" fixed at a vertical playhead in the center — left of it is what FSD already did, right of it is what the road is about to demand, flowing toward the playhead like a rhythm-game note chart. Carries its own legend (PAST/NOW/AHEAD, "CORNERING SEVERITY", ±g scale, R/L ticks). | Full-width band across the hero tile, below the hero label and the scoreboard |

**Corner allocation.** The hero tile's own label is drawn top-left, so the
overlays claim the other three corners by design: `--gauge` bottom-left,
`--fsd-scoreboard` top-right, `--fsd-friction-circle` bottom-right, with the
note-highway ribbon in the band below the two top-anchored elements. All four
can be combined with no collisions. `--map-overlay` picks whichever corner is
still free at render time, preferring bottom-right, then bottom-left, then
top-right — accounting for the burned-in clock, which lands in the hero tile's
bottom-left corner under `--landscape`. Ask for *everything* at once and it runs
out of corners; see [Known limitations](#known-limitations).

**One compositing pass.** `--fsd-scoreboard`, `--fsd-friction-circle` and
`--fsd-note-highway` composite together in a **single** pass over the hero tile
no matter how many of the three you request. `--gauge` and `--map-overlay` are
separate passes (different underlying tools), so the worst case is three
hero-tile passes rather than six.

**Solo hero only.** `--gauge`, `--map-overlay` and all three `--fsd-*` flags
require `--feature` to name a single camera, not a pair like `repeaters`.

## How the GPS map works

Newer Tesla firmware embeds per-frame telemetry — GPS, speed, heading, steering,
Autopilot state, and IMU acceleration — as **SEI metadata inside the H.264
bitstream** (the same data surfaced by
[teslamotors/dashcam](https://github.com/teslamotors/dashcam)).
`tesla_gps.py` decodes it (no external libraries — a hand-rolled protobuf reader),
turns it into a GPX track re-timed to the concatenated grid timeline, and
[gopro-overlay](https://github.com/time4tea/gopro-dashboard-overlay) renders the
map tile and dashboard; `tesla_fsd_overlay.py` derives the FSD metrics and draws
the three showcase panels.

**GPS is only available when:**
- the car is on **firmware 2025.44.25 or later** and **HW3 or newer**, and
- the clips were recorded **while driving** (parked Sentry clips carry no telemetry).

Check whether a folder has GPS before rendering:

```bash
python3 tesla_gps.py /path/to/clips --probe
```

If a folder has no telemetry, the overlay flags print a notice and the grid is
built without them — nothing breaks.

## Watching a long run

A six-camera session with `--blur-faces --map` can run for hours, so the tool
plans the work up front and reports against that plan:

```
Plan: 16 steps (6 concat, 6 blur, GPS extract, map render, map upscale, grid) | ~31m 50s of footage per camera

[ 3/16] blur faces front         ████████░░░░░░░░  38%  ETA 22m 40s
        job                      ███░░░░░░░░░░░░░  11%  elapsed 6m 12s · ETA ~1h 48m
```

Nothing there is a guess from a timer — the percentages come from ffmpeg's own
progress stream and `deface`'s frame counter. The job ETA starts from rough
per-step estimates and **re-calibrates from measured throughput** as steps
finish, so it settles quickly (after the first camera's blur, the other five are
predicted from that measurement).

- Press **Ctrl-T** (macOS/BSD) at any time to print where the run is.
- Press **Ctrl-C** to stop: the incomplete output file is deleted, and re-running
  the same command picks up from the cameras already finished.
- Piping to a file switches to a plain progress line every ~15s (no escape codes).
- `--verbose` shows every command and the raw ffmpeg/deface output instead —
  what you want when something fails and you need to watch it happen.

---

## Requirements

- **macOS** (uses Apple VideoToolbox for hardware encode; the grid logic is
  otherwise portable).
- **ffmpeg with `drawtext`** — Homebrew's plain `ffmpeg` ships *without*
  libfreetype, so install `ffmpeg-full`:
  ```bash
  brew install ffmpeg-full
  ```
  The script auto-detects `ffmpeg-full` and falls back gracefully.
- **Python 3.9+** for the core tool (standard library only).
- For any overlay (`--map`, `--map-overlay`, `--gauge`, `--fsd-scoreboard`,
  `--fsd-friction-circle`, `--fsd-note-highway`): **Python 3.10+** and
  `gopro-overlay` in a local venv (below).
- For `--blur-faces`: `pip install deface` (optional).
- **Dashcam encryption must be OFF**, or the clips must be exported decrypted —
  see [Encrypted clips](#encrypted-clips).

## Setup

```bash
git clone https://github.com/RezaAmbler/teslacam-studio.git
cd teslacam-studio

# Optional — only needed for the overlays (--map, --map-overlay, --gauge,
# --fsd-scoreboard, --fsd-friction-circle, --fsd-note-highway):
python3.12 -m venv .venv
./.venv/bin/python -m pip install gopro-overlay
```

`--map`/`--map-overlay`/`--gauge` look for `gopro-dashboard.py` inside `./.venv`;
the `--fsd-*` flags look for their own driver script (`tesla_fsd_overlay.py`) and
the `gopro-overlay` library installed there. `--map`/`--map-overlay` download
OpenStreetMap tiles on first use (so they need network access); `--gauge` and the
`--fsd-*` overlays composite onto your own footage and need no network access.

### Running the tests

The tools themselves are stdlib-only; only the test suite needs a dependency:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

CI runs the suite on every push (Python 3.9 and 3.12).

---

## Usage

```bash
# Basic: combine an event folder into a labeled grid
python3 tesla_combine.py /path/to/event/folder

# Landscape layout (real 16:9-ish aspect ratio) instead of the tall grid
python3 tesla_combine.py /path/to/event/folder --landscape --map

# Trim to a wall-clock window
python3 tesla_combine.py /path/to/event/folder --trim-start 18:59:00 --trim-end 19:02:00

# Feature the rear camera (e.g. you got rear-ended), at 2x speed
python3 tesla_combine.py /path/to/event/folder --feature back --speed 2

# Blur faces (needs `deface`)
python3 tesla_combine.py /path/to/event/folder --blur-faces

# Add a live GPS route-map tile (needs gopro-overlay in ./.venv)
python3 tesla_combine.py /path/to/event/folder --map

# Tighter, navigation-style map; or wider & sharper for highway
python3 tesla_combine.py /path/to/event/folder --map --map-mag 3
python3 tesla_combine.py /path/to/event/folder --map --map-mag 1 --map-zoom 16

# Composite a speed/compass dashboard onto the hero tile
python3 tesla_combine.py /path/to/event/folder --gauge
python3 tesla_combine.py /path/to/event/folder --gauge --gauge-units kph

# The FSD showcase overlays, alone or in any combination (one compositing pass)
python3 tesla_combine.py /path/to/event/folder --fsd-scoreboard
python3 tesla_combine.py /path/to/event/folder --fsd-friction-circle
python3 tesla_combine.py /path/to/event/folder --fsd-note-highway

# A small translucent HUD map inset on the hero tile's corner, instead of
# (or alongside) the sidebar tile
python3 tesla_combine.py /path/to/event/folder --map-overlay
python3 tesla_combine.py /path/to/event/folder --map --map-overlay
python3 tesla_combine.py /path/to/event/folder --map-overlay --map-overlay-alpha 40

# All four hero-tile overlays together (each occupies its own region)
python3 tesla_combine.py /path/to/event/folder --gauge --fsd-scoreboard --fsd-friction-circle --fsd-note-highway

# Force software encoding for sharper output (slower)
python3 tesla_combine.py /path/to/event/folder --quality high

# See the ffmpeg commands without running anything
python3 tesla_combine.py /path/to/event/folder --dry-run
```

Run `python3 tesla_combine.py --help` for the full flag list.

### Key options

| Flag | Purpose |
|------|---------|
| `--trim-start` / `--trim-end` | `HH:MM:SS` wall-clock or seconds offset |
| `--speed` | Playback speed multiplier |
| `--feature` | Which camera/pair gets the large hero row |
| `--landscape` | Hero camera at native res + thin sidebar column, instead of the tall grid |
| `--no-labels` | Skip the per-tile labels and the burned-in clock |
| `--blur-faces` | Anonymize faces (`--blur-mode blur\|solid\|mosaic`) |
| `--blur-thresh` | Face-detection confidence 0–1 (default `0.2`) — raise it if too much gets blurred |
| `--blur-scale` | Downscale `WxH` for the detection pass only; output stays full-res (faster) |
| `--map` | Add the live GPS route-map tile |
| `--map-zoom` | OSM tile zoom 1–19 (default 19) |
| `--map-mag` | Magnify beyond OSM's limit, 1.0–4.0 (default 2.0; 1 = off) |
| `--map-overlay` | Composite a small translucent HUD-style route map onto a corner of the hero tile — additive with `--map` (solo `--feature` only) |
| `--map-overlay-alpha` | Translucency of the `--map-overlay` panel, `0` (invisible) to `255` (fully opaque) (default `110`) |
| `--gauge` | Composite a speed/compass dashboard panel onto the hero tile (solo `--feature` only) |
| `--gauge-units` | `mph` (default) or `kph` |
| `--fsd-scoreboard` | FSD streak scoreboard: hands-free time, corner count, peak G, takeovers (solo `--feature` only) |
| `--fsd-friction-circle` | FSD friction-circle G-meter: lateral vs. longitudinal G, fading trail, peak-this-corner (solo `--feature` only) |
| `--fsd-note-highway` | FSD note-highway ribbon: scrolling signed lateral-G severity, "now" fixed at center (solo `--feature` only) |
| `--native` | True native resolution (skips the hardware-fit scale-down) |
| `--max-dim` | Hardware encode/decode ceiling to fit under (default `4096`) |
| `--quality` | `fast` (default, hardware) or `high` (software libx264, CRF 18) |
| `--output-dir` | Where outputs go (default: next to the input folder) |
| `--force-concat` | Rebuild the per-camera concats even if cached ones exist |
| `--skip-space-check` | Don't pre-flight free disk space |
| `-v` / `--verbose` | Full ffmpeg/deface output instead of the progress display |
| `--no-progress` | Plain progress lines, no live redraw (automatic when piped) |
| `--dry-run` | Print ffmpeg commands, do nothing |

## Input

Tesla's own clip naming, in one folder:

```
2026-07-14_18-57-37-front.mp4
2026-07-14_18-57-37-back.mp4
2026-07-14_18-57-37-left_repeater.mp4
...
```

Any `.mp4` in the folder that doesn't match that pattern — a clip you renamed by
hand, a stray export, an unrelated video — **is excluded from the render**, since
there's no way to place it on the timeline. That exclusion is announced: the run
prints a warning naming every skipped file, so a hand-curated folder can't
quietly produce a shorter output than you asked for.

### Encrypted clips

If **Controls > Safety > Encrypt Dashcam Recordings** is on, the `.mp4` files on
the USB drive are opaque ciphertext and nothing can decode them directly.
teslacam-studio detects this and says so, rather than failing with a generic
decode error. Either export the clips through the Tesla Dashcam app (select
clips, then the padlock button) or turn encryption off before recording.

## Output

Written next to the input folder unless `--output-dir` is given:

| File | What |
|------|------|
| `<session>_<angle>_combined.mp4` | one lossless concat per camera angle |
| `<session>_<angle>_blurred.mp4` | (with `--blur-faces`) that concat, faces anonymized |
| `<session>_maptile.mp4` | (with `--map`) the standalone live route-map tile |
| `<session>_<hero-angle>_gauge.mp4` | (with `--gauge`) that hero tile, dashboard overlay composited on |
| `<session>_<hero-angle>_<widgets>.mp4` | (with any of `--fsd-scoreboard`/`--fsd-friction-circle`/`--fsd-note-highway`) that hero tile, with every requested FSD widget composited on in a single pass — `<widgets>` is the active widget names joined by `_`, e.g. `_scoreboard`, or `_scoreboard_friction-circle_note-highway` if all three are requested together |
| `<session>_<hero-angle>_map-overlay.mp4` | (with `--map-overlay`) that hero tile, translucent HUD map inset composited on |
| `<session>_grid[_landscape][_feature-X][_blurred][_gauge][_scoreboard][_friction-circle][_note-highway][_map][_map-overlay].mp4` | the labeled multi-camera composite |

`playcheck.sh <file.mp4>` runs headless playback sanity checks (decode integrity,
faststart index, hardware-decodable dimensions, constant frame rate).

---

## Known limitations

- **Takeover counting is unproven against a real disengagement.** Every drive
  tested so far stayed FSD-engaged throughout, so `--fsd-scoreboard`'s takeover
  count is verified against synthetic data only. (A related bug — a telemetry
  gap being *misread* as a disengagement — was found and fixed; this is the
  separate "we've never seen a real one" gap.)
- **Overlays need a solo hero.** `--gauge`, `--map-overlay` and the three
  `--fsd-*` flags reject a paired `--feature` (like `repeaters`).
- **Ask for too many overlays at once and two will share a corner.** The hero
  tile has three usable corners (the fourth holds its label), so once
  `--map-overlay` is combined with `--gauge` + `--fsd-friction-circle` + either
  `--fsd-scoreboard` or `--fsd-note-highway`, nothing is left and the HUD map
  inset falls back to sharing bottom-right with the friction circle. Under
  `--landscape` it takes one flag fewer, since the burned-in clock already
  claims bottom-left. Known and documented, not a silent bug — drop a flag, or
  use `--no-labels`, to avoid it.
- **The burned-in clock shares bottom-left with `--gauge`'s panel.** The clock is
  drawn on the *final canvas*, so under `--landscape` — where the hero tile spans
  the full canvas height at the left edge — it lands on the gauge panel's bottom
  edge. The same can happen in the tall grid when the hero row ends up as the
  bottom row (a low-camera-count session). `--map-overlay` accounts for the clock
  when picking its corner; `--gauge` itself doesn't move. `--no-labels` sidesteps
  it.
- **`--map-mag` is functionally tested but not hardened** to the same degree as
  the rest of the map path.
- **The clock can drift behind true wall-clock** when Tesla dropped recording
  segments, since concatenating squeezes those gaps out. The run measures the
  drift and reports it rather than silently showing a wrong time.
- **The encode path is macOS-specific** (VideoToolbox).

---

## Credits

- **SEI telemetry format** — [teslamotors/dashcam](https://github.com/teslamotors/dashcam) (Tesla's own tools + `dashcam.proto`).
- **Map rendering** — [gopro-overlay](https://github.com/time4tea/gopro-dashboard-overlay).
- **Face anonymization** — [deface](https://github.com/ORB-HD/deface).
- Map data © OpenStreetMap contributors.

## License

MIT — see [LICENSE](LICENSE).
