# Backlog

Known work, roughly highest-value first. Each item links to the detail it needs.

## High

- **Derive `--map-zoom` from the route instead of defaulting to 19.**
  The default is the most expensive value OSM offers, and tile cost grows with
  the *square* of how far the drive roamed. A 65 km drive wants ~170,000 tiles
  at z19 (~7.7 h of downloading before a single frame is drawn); the same route
  at z16 wants ~2,800 (~7 min). The GPX is already built before the map renders,
  so the bounding box is known for free — pick the smallest zoom that fits a
  tile budget, and let `--map-zoom` override.
  → [`docs/map-zoom-findings.md`](docs/map-zoom-findings.md)

- **Say something during the map step.**
  `gopro-dashboard` can sit for hours fetching tiles with no output at all. A
  run looks identical to a hang: ~1% CPU, no log line, no growing file. At
  minimum, log the computed bbox, the tile estimate, and the chosen zoom before
  starting the fetch, so a bad combination is obvious in seconds instead of
  hours. → [`docs/map-zoom-findings.md`](docs/map-zoom-findings.md#no-progress-signal)

## Medium

- **Document the tile-cost cliff and OSM's usage policy.**
  `--map-zoom`'s help text sells 19 as "the OSM max", which reads like a quality
  setting rather than a cost cliff. OSM's tile servers are volunteer-funded and
  their usage policy discourages bulk downloading; z19 on a long drive is
  squarely over that line.
  → [`docs/map-zoom-findings.md`](docs/map-zoom-findings.md#osm-tile-usage-policy)

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

- **A translucent map inset on the front camera, instead of a sidebar
  tile.** HUD-style: the route map as a semi-transparent overlay in the
  front tile's corner rather than a separate grid cell. Structurally very
  close to how `--gauge` already composites a panel onto the hero camera
  (same `--input`-driven gopro-dashboard.py overlay mechanism) — and the
  translucency itself is already proven working (`--gauge`'s own panel
  background is a translucent value rendering correctly today), so the
  open questions are UX/design (replace the tile or add a mode?, exact
  placement/opacity) rather than "can this even work".
  → [`docs/translucent-map-overlay-findings.md`](docs/translucent-map-overlay-findings.md)
