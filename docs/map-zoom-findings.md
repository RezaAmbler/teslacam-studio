# `--map-zoom`: measured cost, and why the default is wrong

Findings from rendering a full day of dashcam footage (2026-08-22, ~4.2 h of
driving) with `--map`. Everything here is measured on that run, not estimated.

**Summary:** `--map-zoom` defaults to 19. Tile cost grows with the *square* of
how far the drive roamed, so 19 is fine for a driveway and catastrophic for a
commute. One 92-minute drive needed ~170,000 OSM tiles at z19 — about 7.7 hours
of downloading before a single frame was drawn. The same route at z16 needs
~2,800 tiles, roughly 7 minutes. The route's bounding box is already known
before the map renders, so the right zoom can be computed rather than guessed.

---

## The three multipliers

### 1. Tiles are a fixed 256×256 px, so each zoom level costs 4×

A tile is the same image size at every zoom. Going one level in halves the
ground distance a tile covers in *both* axes, so the same area takes 2×2 = 4
tiles. Three levels (19→16) is 4³ = **64×**.

Measured against the 17.1 × 40.6 km bounding box of one drive:

| zoom | tile covers | tiles for that bbox | ratio |
|-----:|------------:|--------------------:|------:|
| 19 | 64 m | 169,912 | — |
| 18 | 128 m | 42,478 | 4.0× fewer |
| 17 | 256 m | 10,653 | 4.0× fewer |
| 16 | 512 m | 2,720 | 3.9× fewer |
| 15 | 1025 m | 680 | 4.0× fewer |

The observed ratios land on 4.0× per level exactly as the geometry predicts.

### 2. It fetches the bounding box, not the road {#why-bbox-not-corridor}

This is what turns "expensive" into "absurd". The drive covered **64.9 km of
road**, but wandered across a box of **17.1 × 40.6 km = 695 km²**.
`moving_journey_map` draws the whole route *and* follows the car, so it needs
the entire extent available and cannot fetch only a ribbon along the path.

Most of those 170,000 tiles are empty hillside between roads the car never
touched. **Cost scales with the area roamed over, not the distance driven.** A
65 km round trip along one highway is cheap; 65 km scattered across a county is
not — same odometer reading, ~100× the tiles.

Fixing this properly means teaching gopro-overlay to fetch a corridor around the
route rather than the bbox. That is an upstream change, not one for this repo.

### 3. Fetch rate is fixed at ~6 tiles/sec

Tiles are individual HTTP requests (~1.1 s latency each, ~6 in flight). The rate
does not improve at lower zoom — it is network-bound. So tile count converts
directly to wall-clock:

- z19: 169,912 tiles ÷ ~6/s = **7.7 hours** before the first frame is drawn
- z16: 2,720 tiles = **~7 minutes**

Observed on the run: at z19 the 60-minute chunk fetched for **116 minutes** and
never started drawing. Restarted at z16, drawing began in **~2 minutes**.

---

## Zoom is also a quality decision, and 19 is the wrong end

The map renders at `tile_w / --map-mag` pixels wide. With the 2896 px native
grid the map tile is 1448 px, and the default `--map-mag 2.0` renders it at
**724 px**, then upscales. Visible ground width is therefore `724 × m/px`:

| zoom | visible width | time to cross at 100 km/h | max bbox side @2,000 tiles | suits |
|-----:|--------------:|--------------------------:|---------------------------:|-------|
| 19 | 181 m | 7 s | 2.9 km | parking manoeuvre, driveway |
| 18 | 362 m | 13 s | 5.7 km | a few city blocks |
| 17 | 724 m | 26 s | 11.5 km | town / short urban trip |
| 16 | 1449 m | 52 s | 22.9 km | a full drive, highway or cross-town |
| 15 | 2898 m | 104 s | 45.8 km | regional road trip |

At z19 a car at highway speed crosses the entire visible map in **7 seconds** —
no context, no sense of direction of travel. So the 64× saving from z16 buys a
map that is *more* useful for a driving video, not a compromise.

z19 is not wrong, it is **narrow**: it suits clips where the car barely moves.
Note its ideal case — parked Sentry events — is unreachable, because parked
clips carry no telemetry and `--map` needs GPS.

Measured across the same day's actual drives:

| clip | extent | z19 | z17 | z16 |
|------|-------:|----:|----:|----:|
| driveway departure, 2 min | 0.3 km | **25** | 4 | 1 |
| short trip, 12 min | 8 km | 15,625 | 1,024 | 256 |
| long drive, 92 min | 40.6 km | **401,956** | 25,281 | 6,400 |

The 2-minute driveway clip at z19 cost 25 tiles and looked exactly right. The
same flag on the 92-minute drive wants 400,000. Nothing changed but distance.

---

## Proposed fix: derive the default

`build_map_tile` already extracts the GPX *before* rendering, so the bounding box
is free at that point. Sketch:

1. Compute the bbox from the retimed samples.
2. For each zoom from 19 down, compute tiles needed for that bbox.
3. Pick the highest zoom whose count fits a budget (~2,000 tiles is a sane
   starting point — it kept every real drive under ~10 minutes of fetching).
4. Log the choice and the estimate.
5. An explicit `--map-zoom` still wins, but warn when the estimate is large.

That yields z19 on the driveway clip and z16 on the 92-minute drive with no flag
from the user.

If a derived default is too much, **z16 is a far better fixed default** than 19.
Its failure mode on a short clip is a slightly-too-wide map; z19's failure mode
is 170,000 requests to a volunteer-funded service.

## No progress signal {#no-progress-signal}

A long fetch is indistinguishable from a hang. During the 116-minute z19 fetch:

- `gopro-dashboard` sat at ~1% CPU (25 min elapsed, 44 s of CPU time)
- no log output at all
- no output file — `map_pre.mp4` is not created until drawing starts

The only way to confirm progress was querying the tile cache SQLite directly
(`~/.gopro-graphics/tilecache.sqlite`) and watching the row count climb. This
cost real time to diagnose twice, and led to one wrong conclusion (throttling)
before the bbox math explained it. Logging bbox + tile estimate + chosen zoom
before the fetch would have made it obvious immediately.

For reference, the three phases have very different signatures:

| phase | CPU | how to tell it's alive |
|-------|-----|------------------------|
| GPS extract + re-time | low, brief | `.gpx` appears in tmpdir |
| tile fetch + map draw | ~1%, network-bound | tile cache row count grows |
| grid encode | ~250% (VideoToolbox) | `_grid_map.mp4` grows |

## OSM tile usage policy {#osm-tile-usage-policy}

`tile.openstreetmap.org` is volunteer-funded and its usage policy discourages
bulk downloading. 170,000 tiles for one video is over that line, and the current
help text — "default: 19, the OSM max" — reads like a quality knob rather than a
cost cliff. Worth saying plainly in `--help` and the README that zoom drives
request volume quadratically.

Tiles cache locally in `~/.gopro-graphics/tilecache.sqlite`, so repeat renders of
the same area are nearly free. Over this session it grew from ~8,000 to ~54,000
rows (~158 MB).

---

## Telemetry as a driving filter {#telemetry-as-a-driving-filter}

RecentClips records whenever the car is awake, not only while moving. Of 720
front clips in one day (12 h of recording), only **252 carried SEI telemetry —
35%**, forming 8 driving segments totalling 4.2 h. The rest was the car parked
with cameras rolling.

`tesla_gps.extract_samples` returns quickly on a clip with no SEI (~0.2 s vs
~0.4 s), so probing a whole day costs about 4 minutes. Since `--map` already
requires telemetry, presence of GPS is both the driving/parked discriminator and
the map's precondition — one probe serves both.

Worth exposing as a `--driving-only` flag, or at minimum documenting the
`tesla_gps.py --probe` recipe, so batch users don't spend hours rendering a
stationary car.

## Batch rendering notes {#batch-rendering-notes}

A resumable driver was written outside the repo for this run. Points worth
keeping if it is ever folded in:

- **Chunk long drives.** ~60 min per chunk keeps the map step tractable, then
  concatenate losslessly per segment. Chunks share geometry so `-c copy` works.
- **Delete byproducts per chunk.** Per-camera concats and the map tile dwarf the
  grid output; dropping them as each chunk finishes keeps peak disk flat.
  Observed: free space did not move across a 3.8 GB deliverable.
- **Symlink chunk inputs.** Copying would have duplicated 142 GB.
- **Resume on the finished unit, not the intermediate.** The first version keyed
  resume on per-chunk files, but the concat step deletes those once a segment is
  finalised — so a restart happily re-rendered work already delivered. Key
  resume on whatever the pipeline does *not* delete.
- **The concat cache lives in the output dir**, so per-chunk output dirs keep
  caches isolated.

## Throughput reference

Measured on Apple silicon, 6 cameras, 2530×4096 output, hardware encode:

| stage | rate |
|-------|------|
| decryption | ~6.7 clips/s (4,320 clips ≈ 11 min) |
| GPS probe | ~0.3 s/clip |
| grid encode | ~2.0× realtime (~250% CPU) |
| full chunk, tiles cached | ~0.85× realtime end to end |
| full chunk, cold tiles at z19 | unbounded in practice — see above |
