# A translucent map inset on the front camera, instead of a sidebar tile

Ideation only, same caveat as `elevation-findings.md` — a few minutes'
scoping, not a measured spike.

**The idea:** instead of (or as an alternative to) the `--map` route tile
living beside the cameras in the grid (a full sidebar tile in `--landscape`,
or paired with `back` in the tall grid), render it as a semi-transparent
inset directly on the FRONT tile's bottom-right corner, sized roughly like
one grid cell — a HUD-style map instead of a separate panel.

## This is very close to infrastructure that already exists

`--gauge` (see the session that added it) composites a panel onto the hero
camera tile by handing `gopro-dashboard.py` the hero's own video as its
`--input`, alongside `--use-gpx-only --gpx <path>` — it reads the video's
real dimensions itself and runs its own internal ffmpeg
`[0:v][1:v]overlay` compositing pass, producing a fully-composited file in
one subprocess call. A translucent map inset is structurally the *same*
operation with a different layout XML (a `moving_journey_map` instead of
the dial/compass/chart panel) and a different corner. Little to no new
plumbing — mostly a new `write_*_layout` function and wiring, following
the exact pattern `write_gauge_layout`/`build_gauge_overlay` already set.

## The translucency question already has a real, working answer

This was the open question worth flagging before assuming it's easy: does
gopro-dashboard's raw-RGBA-stream → ffmpeg-`overlay` pipeline actually
respect partial alpha, or would a "translucent" render just show up as a
solid rectangle?

**It already works** — not hypothetically. The `--gauge` panel's own
`<frame bg="0,0,0,180">` is a translucent value (180/255, ~70% opaque) and
it renders correctly through this exact pipeline today. So the underlying
mechanism is proven; what's unproven for a map specifically is just the
*tuning* (how translucent is legible without being distracting over live
video, whether the map's own drawn route/tiles need a different opacity
than a flat dark panel does) and needs a real render to judge, same as
every other panel-sizing/opacity choice in this codebase has.

## Open questions for a real design pass (not answered here)

- **Replace the sidebar tile, or offer both?** A new flag/mode (e.g.
  `--map-style {tile, overlay}`, or a separate `--map-overlay` alongside
  `--map`) vs. changing `--map`'s behavior outright — a real UX decision,
  not a technical one.
- **Corner and sizing.** "Roughly one grid cell" is a starting point, not
  a spec — needs the same pixel-math-then-verify-against-a-render process
  `--gauge`'s panel and `--landscape`'s sidebar both went through.
  Bottom-right avoids the `--gauge` panel's own bottom-left corner, so the
  two could coexist without collision, similar to how `--map --gauge`
  already coexist today (map in the sidebar/back-pairing, gauge on front).
- **Does the route-drawing still read well at map-tile size, translucent,
  over moving footage?** The sidebar tile sits on a plain background; an
  overlay competes with live video underneath it for visual attention.
  Untested.

## Recommendation

Backlog as its own research spike/branch — low technical risk (the
compositing mechanism is proven, not hypothetical), but real design
decisions (replace vs. add, exact placement/opacity) that deserve a
proper look-at-a-render pass before committing, not a guess baked in
up front.
