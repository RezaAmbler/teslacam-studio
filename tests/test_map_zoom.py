"""--map-zoom auto-derivation: tile math, zoom choice, GPX bounds, resolver.

The tile-count math is pinned against the REAL drives measured in
docs/map-zoom-findings.md rather than against numbers this code produced --
that doc's figures came from actual gopro-overlay runs (one of which fetched
for 116 minutes at z19 and never started drawing), so they're the ground truth
this function has to reproduce.
"""
import math
from pathlib import Path

import pytest

import tesla_combine as tc


LAT = 35.0


def square_bbox(km, lat=LAT):
    """A roughly-square bbox `km` across, at a realistic driving latitude."""
    dlat = km / 110.57
    dlon = km / (111.32 * math.cos(math.radians(lat)))
    return (lat, -120.0, lat + dlat, -120.0 + dlon)


# --- _tile_xy: the boundary values its comments claim -------------------------

def test_tile_xy_longitude_endpoints():
    # x is "tiles east of the antimeridian": -180 is the origin, Greenwich the
    # middle, +180 the far edge. Pins the (lon + 180) / 360 * n rescale.
    n = 2.0 ** 10
    assert tc._tile_xy(0.0, -180.0, 10)[0] == pytest.approx(0.0)
    assert tc._tile_xy(0.0, 0.0, 10)[0] == pytest.approx(n / 2)
    assert tc._tile_xy(0.0, 180.0, 10)[0] == pytest.approx(n)


def test_tile_xy_latitude_endpoints_and_flip():
    # y is "tiles SOUTH of the north edge" -- the axis is flipped relative to
    # latitude, which is what the (1 - ...) does. The cutoff latitude landing
    # exactly on 0 and n is what makes the map square.
    n = 2.0 ** 10
    L = tc.WEB_MERCATOR_MAX_LAT
    assert tc._tile_xy(L, 0.0, 10)[1] == pytest.approx(0.0, abs=1e-6)
    assert tc._tile_xy(0.0, 0.0, 10)[1] == pytest.approx(n / 2)
    assert tc._tile_xy(-L, 0.0, 10)[1] == pytest.approx(n, abs=1e-6)
    # Northern latitudes sit above southern ones on screen, i.e. smaller y.
    assert tc._tile_xy(40.0, 0.0, 10)[1] < tc._tile_xy(30.0, 0.0, 10)[1]


def test_tile_xy_clamps_at_the_poles_instead_of_dividing_by_zero():
    # cos(90 degrees) is 0, so without the clamp this raises ZeroDivisionError
    # (and tan runs to infinity). Nothing in a car's GPS gets near this, but a
    # crash here would be a silly way to lose a render.
    n = 2.0 ** 10
    assert tc._tile_xy(90.0, 0.0, 10)[1] == pytest.approx(0.0, abs=1e-6)
    assert tc._tile_xy(-90.0, 0.0, 10)[1] == pytest.approx(n, abs=1e-6)


def test_tile_xy_grid_is_square_at_every_zoom():
    # The whole point of the WEB_MERCATOR_MAX_LAT cutoff: one 2**zoom grid
    # indexes both axes, so x and y share a scale.
    # Tolerance is RELATIVE: the y term goes through log/tan, so its float
    # error scales with n, and an absolute epsilon that's generous at z8
    # (n=256) is far too tight at z19 (n=524288).
    for z in (0, 8, 16, 19):
        n = 2.0 ** z
        assert tc._tile_xy(0.0, 180.0, z)[0] == pytest.approx(n, rel=1e-9)
        assert tc._tile_xy(-tc.WEB_MERCATOR_MAX_LAT, 0.0, z)[1] == pytest.approx(n, rel=1e-9)


def test_tile_xy_returns_fractional_positions():
    # Callers int() this to get a tile index; the fraction is what lets
    # tile_count_for_bounds see that a bbox straddles two tiles.
    x, _ = tc._tile_xy(0.0, -179.9, 10)
    assert 0.0 < x < 1.0


# --- tile_count_for_bounds ---------------------------------------------------

@pytest.mark.parametrize("km,zoom,measured", [
    (0.3, 19, 25),          # driveway departure, 2 min
    (8.0, 19, 15_625),      # short trip, 12 min
    (40.6, 19, 401_956),    # long drive, 92 min
    (8.0, 16, 256),
    (40.6, 16, 6_400),
])
def test_tile_count_matches_measured_drives(km, zoom, measured):
    got = tc.tile_count_for_bounds(square_bbox(km), zoom)
    # Tolerance is per-AXIS, not per-area, because that's how the two sources of
    # difference behave -- squaring makes either look alarming on a small route
    # and invisible on a big one:
    #   +1.5 tiles: a bbox rarely lands on tile boundaries, so each axis can
    #     need one extra tile (a 5x5 driveway clip becomes 6x6 = 36 vs 25).
    #   +/-5%: the doc records each drive's extent as ONE number, so square_bbox
    #     above only RECONSTRUCTS the bbox that produced its counts. The
    #     residual few percent is this fixture's approximation, not the tile
    #     math's -- consistent across every row, in the same direction.
    # Over-counting is the safe direction regardless: it can only make the
    # derived zoom more conservative, never more expensive.
    side, got_side = math.sqrt(measured), math.sqrt(got)
    assert side * 0.95 - 0.5 <= got_side <= side * 1.05 + 1.5, (
        f"{got} tiles ({got_side:.1f}/side) vs measured {measured} ({side:.1f}/side)")


def test_tile_count_never_below_one():
    # A stationary car: min == max on both axes. Must not return 0 (which would
    # make every zoom "fit" and hand back z19 for a route with no extent).
    assert tc.tile_count_for_bounds((35.0, -120.0, 35.0, -120.0), 19) == 1


def test_tile_count_grows_with_the_square_of_extent():
    # The whole reason a fixed default is wrong: cost is quadratic in area, so
    # doubling how far the car roamed roughly quadruples the tiles.
    small = tc.tile_count_for_bounds(square_bbox(5.0), 16)
    big = tc.tile_count_for_bounds(square_bbox(10.0), 16)
    assert 3.4 <= big / small <= 4.6, f"ratio {big / small:.2f} is not ~4x"


def test_tile_count_falls_by_four_per_zoom_level():
    b = square_bbox(20.0)
    for z in (19, 18, 17, 16):
        assert 3.4 <= tc.tile_count_for_bounds(b, z) / tc.tile_count_for_bounds(b, z - 1) <= 4.6


# --- derive_map_zoom ---------------------------------------------------------

def test_derive_keeps_z19_for_a_driveway_clip():
    # The case z19 is actually right for -- the car barely moves, 25 tiles.
    assert tc.derive_map_zoom(square_bbox(0.3)) == tc.OSM_MAX_ZOOM


def test_derive_backs_off_for_a_long_drive():
    # The 92-minute drive that wanted 401,956 tiles at z19.
    zoom = tc.derive_map_zoom(square_bbox(40.6))
    assert zoom <= 16
    assert tc.tile_count_for_bounds(square_bbox(40.6), zoom) <= tc.MAP_TILE_BUDGET


@pytest.mark.parametrize("km", [0.3, 1.0, 8.0, 40.6, 150.0, 500.0])
def test_derived_zoom_always_fits_the_budget(km):
    b = square_bbox(km)
    assert tc.tile_count_for_bounds(b, tc.derive_map_zoom(b)) <= tc.MAP_TILE_BUDGET


@pytest.mark.parametrize("km", [0.3, 8.0, 40.6, 150.0])
def test_derived_zoom_is_the_highest_that_fits(km):
    # Not merely "a zoom that fits" -- the BEST one. One level up must bust the
    # budget, otherwise we're throwing away map detail for free.
    b = square_bbox(km)
    z = tc.derive_map_zoom(b)
    if z < tc.OSM_MAX_ZOOM:
        assert tc.tile_count_for_bounds(b, z + 1) > tc.MAP_TILE_BUDGET


def test_derive_never_returns_out_of_range():
    # A continent-spanning bbox still has to land on a zoom OSM serves.
    z = tc.derive_map_zoom((-40.0, -170.0, 60.0, 170.0))
    assert tc.OSM_MIN_ZOOM <= z <= tc.OSM_MAX_ZOOM


def test_derive_respects_a_custom_budget():
    b = square_bbox(40.6)
    tight = tc.derive_map_zoom(b, budget=100)
    loose = tc.derive_map_zoom(b, budget=100_000)
    assert tight < loose


# --- gpx_bounds --------------------------------------------------------------

GPX_HEAD = ('<?xml version="1.0"?><gpx version="1.1" creator="t" '
            'xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>')


def _write_gpx(path, points):
    body = "".join(f'<trkpt lat="{la}" lon="{lo}"></trkpt>' for la, lo in points)
    path.write_text(GPX_HEAD + body + "</trkseg></trk></gpx>")
    return path


def test_gpx_bounds_reads_the_extent(tmp_path):
    g = _write_gpx(tmp_path / "r.gpx",
                   [(35.0, -120.5), (35.2, -120.1), (34.9, -120.3)])
    assert tc.gpx_bounds(g) == pytest.approx((34.9, -120.5, 35.2, -120.1))


def test_gpx_bounds_handles_namespaced_tags(tmp_path):
    # write_gpx emits a default xmlns, so a plain find("trkpt") wouldn't match.
    g = _write_gpx(tmp_path / "r.gpx", [(1.0, 2.0), (3.0, 4.0)])
    assert tc.gpx_bounds(g) == (1.0, 2.0, 3.0, 4.0)


def test_gpx_bounds_none_when_no_points(tmp_path):
    g = tmp_path / "empty.gpx"
    g.write_text(GPX_HEAD + "</trkseg></trk></gpx>")
    assert tc.gpx_bounds(g) is None


def test_gpx_bounds_none_when_missing_or_malformed(tmp_path):
    assert tc.gpx_bounds(tmp_path / "nope.gpx") is None
    bad = tmp_path / "bad.gpx"
    bad.write_text("<gpx><trk><unclosed>")
    assert tc.gpx_bounds(bad) is None


# --- resolve_map_zoom --------------------------------------------------------

def test_resolve_derives_when_no_flag_given(tmp_path, capsys):
    g = _write_gpx(tmp_path / "r.gpx", [(35.0, -120.0), (35.4, -119.55)])
    zoom = tc.resolve_map_zoom(g, None)
    assert zoom == tc.derive_map_zoom(tc.gpx_bounds(g))
    out = capsys.readouterr().out
    assert "auto" in out and "OSM tiles" in out and "km" in out


def test_resolve_honours_an_explicit_zoom(tmp_path, capsys):
    g = _write_gpx(tmp_path / "r.gpx", [(35.0, -120.0), (35.4, -119.55)])
    assert tc.resolve_map_zoom(g, 12) == 12
    assert "explicit" in capsys.readouterr().out


def test_resolve_warns_when_an_explicit_zoom_is_ruinous(tmp_path, capsys):
    # The exact situation that burned 116 minutes with no output: a big route
    # at z19. An override is allowed, but it must not be silent.
    g = _write_gpx(tmp_path / "r.gpx", [(35.0, -120.0), (35.4, -119.55)])
    tc.resolve_map_zoom(g, 19)
    out = capsys.readouterr().out
    assert "WARNING" in out and "look" in out and "like a hang" in out


def test_resolve_does_not_warn_for_a_sane_explicit_zoom(tmp_path, capsys):
    g = _write_gpx(tmp_path / "r.gpx", [(35.0, -120.0), (35.4, -119.55)])
    tc.resolve_map_zoom(g, tc.derive_map_zoom(tc.gpx_bounds(g)))
    assert "WARNING" not in capsys.readouterr().out


def test_resolve_falls_back_to_osm_max_without_a_bbox(tmp_path, capsys):
    # No trackpoints -- can't derive. Must not crash or return None (either
    # would blow up write_map_layout's zoom= attribute downstream).
    g = tmp_path / "empty.gpx"
    g.write_text(GPX_HEAD + "</trkseg></trk></gpx>")
    assert tc.resolve_map_zoom(g, None) == tc.OSM_MAX_ZOOM
    assert "NOTE" in capsys.readouterr().out


def test_resolve_dry_run_does_not_touch_the_filesystem(capsys):
    # Under --dry-run no GPX has been written yet; resolving must still hand
    # back a usable zoom rather than reading a file that isn't there.
    assert tc.resolve_map_zoom(Path("/no/such/route.gpx"), None, dry_run=True) == tc.OSM_MAX_ZOOM
    assert "render time" in capsys.readouterr().out


def test_resolve_dry_run_keeps_an_explicit_zoom(capsys):
    assert tc.resolve_map_zoom(Path("/no/such/route.gpx"), 14, dry_run=True) == 14


# --- CLI ---------------------------------------------------------------------

def test_cli_map_zoom_defaults_to_auto():
    assert tc.build_parser().parse_args(["/f"]).map_zoom is None


def test_cli_map_zoom_explicit_still_parses():
    assert tc.build_parser().parse_args(["/f", "--map-zoom", "16"]).map_zoom == 16
