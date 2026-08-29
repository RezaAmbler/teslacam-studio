# Backlog

Known work, roughly highest-value first. Each item links to the detail it needs.

## Done

- ~~**Derive `--map-zoom` from the route instead of defaulting to 19.**~~
  Shipped on `map-auto-zoom`. `--map-zoom` now defaults to the highest zoom
  whose tile count fits a ~2,000-tile budget, computed from the GPX's own
  bounding box; an explicit `--map-zoom` still overrides. On the 54 km test
  drive this picks z16 (1,216 tiles, ~3m) instead of z19 (69,460 tiles, ~3h 13m).

- ~~**Say something during the map step.**~~
  Shipped on `map-auto-zoom`. Before fetching, the run now logs the route
  extent in km, the chosen zoom and whether it was derived or explicit, the
  tile count, and the estimated fetch time at ~6 tiles/sec. An explicit zoom
  needing more than 5x the budget also warns that it will look like a hang.
  (Measured: a 90s render predicted ~49s of tile fetching and took 50s.)

- ~~**A translucent map inset on the front camera, instead of a sidebar
  tile.**~~ Shipped as `--map-overlay`. Note the spike doc's central claim --
  that the translucency was "already proven working" via `--gauge`'s panel --
  turned out to be **false**: `--gauge` had been rendering fully opaque because
  gopro-overlay's `Frame` needs a separate `opacity=` attribute, and `bg=`'s
  own alpha is overwritten by it. Fixed in both. See CLAUDE.md.

## High

- **Document OSM's tile usage policy.**
  The cost cliff itself is now handled (derived zoom + the pre-fetch log line,
  above), but OSM's tile servers are volunteer-funded and their usage policy
  discourages bulk downloading — worth stating plainly in the README, since even
  a derived zoom fetches thousands of tiles.
  → [`docs/map-zoom-findings.md`](docs/map-zoom-findings.md#osm-tile-usage-policy)

## Medium

- **Telemetry presence is a driving/parked filter, and it's cheap.**
  RecentClips records whenever the car is awake, so most of it is parked. Only
  35% of one day's clips carried SEI telemetry, and probing costs ~0.3 s/clip.
  Worth exposing — a `--driving-only` flag, or just documenting the
  `tesla_gps.py --probe` recipe — so batch users don't render hours of a
  stationary car. → [`docs/map-zoom-findings.md`](docs/map-zoom-findings.md#telemetry-as-a-driving-filter)

## Low

- **Ship a batch driver.** Rendering a whole day means chunking long drives,
  concatenating, and cleaning byproducts as you go. A working, resumable driver
  was written outside the repo this session; it could be folded in.
  → [`docs/map-zoom-findings.md`](docs/map-zoom-findings.md#batch-rendering-notes)

- **Upstream: route-corridor tiles instead of bounding box.**
  `moving_journey_map` fetches the whole bbox, so a drive that wanders across a
  county pulls mostly-empty tiles it never displays. A corridor around the route
  would cut this hugely, but the change belongs in gopro-overlay, not here.
  → [`docs/map-zoom-findings.md`](docs/map-zoom-findings.md#why-bbox-not-corridor)

## Research spikes (ideation only, not measured)

- **Elevation on `--map`/`--gauge`.** Tesla's SEI telemetry has no altitude
  field at all — not a parsing gap, never transmitted. Getting it means an
  external elevation lookup by lat/lon (same shape as `--map`'s existing OSM
  tile fetch — needs the same batching/caching discipline the `--map-zoom`
  lesson taught, or it repeats that mistake with a different API), or a
  much lower-fidelity IMU-integration cue with no network dependency.
  → [`docs/elevation-findings.md`](docs/elevation-findings.md)

- ~~A translucent map inset on the front camera~~ — **shipped as
  `--map-overlay`**, see Done above. `docs/translucent-map-overlay-findings.md`
  is kept as the original spike, but read it with the correction in mind: its
  "translucency is already proven working" claim was wrong, and finding that
  out against a real rendered frame was most of the work.
