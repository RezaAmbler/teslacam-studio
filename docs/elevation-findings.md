# Elevation: what's available, and the options

Ideation only — nothing here is measured against a real run the way
`map-zoom-findings.md` is. This is a few minutes' worth of scoping so a
future session can pick a direction without re-deriving it.

**Summary:** Tesla's SEI telemetry has no altitude field at all — not a
parsing gap, the data is never transmitted. Getting elevation into `--map`/
`--gauge` means either an external elevation lookup by lat/lon, or deriving
a coarse *relative* climb/descend signal from the IMU we already decode.
Neither is free; this is a real research spike, not a quick add.

## Confirmed: no altitude in the source data

`tesla_gps.py`'s `SEI_FIELDS` protobuf schema (the full reverse-engineered
field map) has no altitude entry — the fields are `version`, `gear_state`,
`frame_seq_no`, `vehicle_speed_mps`, `accelerator_pedal_position`,
`steering_wheel_angle`, `blinker_on_left/right`, `brake_applied`,
`autopilot_state`, `latitude_deg`, `longitude_deg`, `heading_deg`,
`linear_acceleration_mps2_x/y/z`. Lat/lon alone can't substitute — 2D GPS
coordinates carry no vertical component.

## Option A: external elevation lookup by lat/lon

Same shape as `--map`'s existing OSM tile fetch (a network call keyed by
the route's bounding box, done once when the GPX is built) — the natural
place to slot this in.

- **A free public API** (open-elevation.com, USGS Elevation Point Query
  Service). USGS EPQS is US-only; open-elevation is global but rate-limited
  and its uptime/reliability as a small free service is unproven for
  anything beyond casual use. Either way: this is a *per-point* API, and a
  drive's GPX can carry thousands of points — exactly the kind of naive
  per-request cost that bit `--map-zoom`'s tile fetching. Needs the same
  treatment: batch requests, downsample the query points (elevation doesn't
  need per-frame resolution — one query per ~50-100m of travel is almost
  certainly enough, then interpolate), and cache by coordinate so a
  re-render doesn't re-fetch.
- **A paid API** (Google Maps Elevation, Mapbox Terrain). Accurate and
  fast, but a billing dependency this otherwise-free/local tool doesn't
  have anywhere else — a real philosophical departure, not just an
  implementation detail.
- **A local/offline dataset** (a bundled or one-time-downloaded DEM, e.g.
  SRTM or a coarser global raster). No per-run network dependency once
  fetched, closer to this project's existing OSM-tiles-are-the-only-network-
  dependency posture — but a real dataset (even a low-res global one) is a
  non-trivial download/storage/lookup-code undertaking, more work than the
  API options for a first spike.

Recommendation if this gets picked up: start with the free-API option,
because it reuses --map's existing "build the GPX, then enrich it" shape
most directly and needs no bundled data — treat batching/caching as
required from the start, not a follow-up, given the `--map-zoom` lesson
that naive per-point cost is where this kind of feature actually goes
wrong.

## Option B: derive a relative climb/descend cue from the IMU (no network)

We already decode `linear_acceleration_mps2_z` (vertical accel) at up to
frame rate. In principle, integrating vertical accel over time gives
vertical *velocity*, and integrating that gives relative elevation change
— entirely offline, no API, no dataset.

In practice this is the classic inertial-navigation trap: double-
integrating noisy accelerometer data drifts without bound over any real
duration (seconds to low minutes before the error swamps the signal),
so this could plausibly drive a short-window "currently climbing / level /
descending" indicator, but not a trustworthy elevation *value* or a chart
spanning a whole drive. Needs real filtering/detrending work to be usable
even for the coarse version, and needs the IMU axis convention confirmed
first (see the `--gauge` G-force work — same open question, see
`docs/gauge-fsd-showcase.md` if that landed).

Worth a short, bounded spike (confirm whether a simple high-pass + integrate
even produces a *qualitatively* sane climbing/descending signal on a known
hill, cheap to try), but going in expecting it to only ever be a coarse cue,
not a replacement for Option A's real elevation values.

## Recommendation

Backlog as its own research spike, own branch, not blocking anything else.
Option A (free API, batched + cached, same shape as the OSM tile fetch) is
the more promising primary path; Option B is a cheap, interesting, strictly
lower-fidelity side experiment worth an hour if someone's curious, not a
substitute.
