"""Tests for the --gauge dashboard overlay's pure Python: hero_angles_for,
write_gauge_layout's panel/section geometry, and CLI parsing/validation.

Mirrors the boundary the --map tests already draw: unit-test the geometry and
string-building, not the gopro-dashboard.py subprocess orchestration (no test
here calls build_gauge_overlay/build_route_gpx end-to-end either -- that needs
real footage, per CLAUDE.md).
"""
import re
import xml.etree.ElementTree as ET

import pytest

import tesla_combine as tc


# --- hero_angles_for ---------------------------------------------------------

def test_hero_angles_for_solo():
    assert tc.hero_angles_for("front") == ["front"]
    assert tc.hero_angles_for("back") == ["back"]
    assert tc.hero_angles_for("left_pillar") == ["left_pillar"]


def test_hero_angles_for_pair():
    assert tc.hero_angles_for("repeaters") == ["left_repeater", "right_repeater"]
    assert tc.hero_angles_for("pillars") == ["left_pillar", "right_pillar"]


def test_hero_angles_for_matches_build_rows_and_landscape_layout():
    # build_rows/landscape_layout used to each compute this inline; confirm
    # the extracted helper still agrees with what they do with it.
    rows, hero = tc.build_rows(["front", "back"], "front")
    assert hero == tc.hero_angles_for("front")

    dims = {a: (1280, 960) for a in ["left_repeater", "right_repeater", "front"]}
    hero2, *_ = tc.landscape_layout(dims, "repeaters")
    assert hero2 == tc.hero_angles_for("repeaters")


# --- write_gauge_layout -------------------------------------------------------

def _parse_gauge_layout(path):
    root = ET.fromstring(path.read_text())
    frame = root.find("frame")
    assert frame is not None
    return frame


@pytest.mark.parametrize("tile_w,tile_h", [(2896, 1876), (1280, 960), (500, 400)])
def test_write_gauge_layout_panel_within_tile_bounds(tmp_path, tile_w, tile_h):
    path = tmp_path / "gauge.xml"
    tc.write_gauge_layout(path, tile_w, tile_h, "mph")
    frame = _parse_gauge_layout(path)
    x, y = int(frame.get("x")), int(frame.get("y"))
    w, h = int(frame.get("width")), int(frame.get("height"))
    assert x >= 0 and y >= 0
    assert x + w <= tile_w
    assert y + h <= tile_h


@pytest.mark.parametrize("tile_w,tile_h", [(2896, 1876), (1280, 960), (500, 400)])
def test_write_gauge_layout_scales_proportionally(tmp_path, tile_w, tile_h):
    path = tmp_path / "gauge.xml"
    tc.write_gauge_layout(path, tile_w, tile_h, "mph")
    frame = _parse_gauge_layout(path)
    w, h = int(frame.get("width")), int(frame.get("height"))
    assert w == pytest.approx(tile_w * tc.GAUGE_PANEL_W_FRAC, abs=2)
    assert h == pytest.approx(tile_h * tc.GAUGE_PANEL_H_FRAC, abs=2)


def test_write_gauge_layout_bottom_left_margin(tmp_path):
    path = tmp_path / "gauge.xml"
    tc.write_gauge_layout(path, 2896, 1876, "mph")
    frame = _parse_gauge_layout(path)
    x, y = int(frame.get("x")), int(frame.get("y"))
    w, h = int(frame.get("width")), int(frame.get("height"))
    assert x == tc.GAUGE_MARGIN
    assert y == 1876 - h - tc.GAUGE_MARGIN


def test_write_gauge_layout_contains_all_four_widgets(tmp_path):
    path = tmp_path / "gauge.xml"
    tc.write_gauge_layout(path, 2896, 1876, "mph")
    frame = _parse_gauge_layout(path)
    types = [c.get("type") for c in frame.iter("component")]
    # Two <text> components: the "MPH" unit label under the speed readout,
    # and the "SPEED, LAST 30s" label above the chart (the chart's own
    # min/max axis numbers otherwise don't say what they're measuring).
    # compass-arrow (not compass): fixed N/S/E/W ring, red rotating heading
    # arrow -- gopro-overlay's plain "compass" has no color attribute at all
    # to pick a heading marker out in a different color.
    assert types == ["msi", "compass-arrow", "metric", "text", "text", "chart"]


def test_write_gauge_layout_sections_dont_overlap_and_fit_panel(tmp_path):
    path = tmp_path / "gauge.xml"
    tc.write_gauge_layout(path, 2896, 1876, "mph")
    frame = _parse_gauge_layout(path)
    panel_w = int(frame.get("width"))

    # msi/compass are wrapped in <translate x=.. y=..> (they don't take x/y
    # themselves); metric/text/chart carry x/y directly.
    msi_x = int(frame.find("./translate[1]").get("x"))
    compass_x = int(frame.find("./translate[2]").get("x"))
    metric_el = frame.find("./component[@type='metric']")
    chart_el = frame.find("./component[@type='chart']")
    speed_x = int(metric_el.get("x"))
    chart_x = int(chart_el.get("x"))
    chart_w = int(chart_el.get("width"))

    assert 0 <= msi_x < compass_x < speed_x < chart_x
    assert chart_x + chart_w <= panel_w


def test_write_gauge_layout_chart_has_a_label_above_it(tmp_path):
    # The chart's own min/max axis numbers (gopro-overlay's default chart
    # decoration) don't say what they're measuring on their own -- this label
    # names the metric and the rolling window, kept in sync with the chart's
    # own `seconds=` via GAUGE_CHART_SECONDS so they can't drift apart.
    path = tmp_path / "gauge.xml"
    tc.write_gauge_layout(path, 2896, 1876, "mph")
    frame = _parse_gauge_layout(path)
    chart_el = frame.find("./component[@type='chart']")
    assert chart_el.get("seconds") == str(tc.GAUGE_CHART_SECONDS)

    texts = frame.findall("./component[@type='text']")
    chart_label = texts[-1]  # unit label ("MPH") comes first, chart label second
    assert chart_label.text == f"SPEED, LAST {tc.GAUGE_CHART_SECONDS}s"
    # Sits directly above the chart, same x, and doesn't overlap it vertically.
    assert int(chart_label.get("x")) == int(chart_el.get("x"))
    label_y = int(chart_label.get("y"))
    chart_y = int(chart_el.get("y"))
    assert label_y < chart_y


@pytest.mark.parametrize("units", ["mph", "kph"])
def test_write_gauge_layout_units_threaded_through_and_labelled(tmp_path, units):
    path = tmp_path / "gauge.xml"
    tc.write_gauge_layout(path, 2896, 1876, units)
    text = path.read_text()
    frame = _parse_gauge_layout(path)
    # Every speed-metric component gets the same units= attribute.
    for tag in ("msi", "metric", "chart"):
        el = frame.find(f"./component[@type='{tag}']") or frame.find(f".//component[@type='{tag}']")
        assert el.get("units") == units
    # The static unit label is the uppercase form (matches the sample's "MPH").
    label = frame.find("./component[@type='text']")
    assert label.text == units.upper()


def test_write_gauge_layout_msi_and_compass_need_translate_wrapper(tmp_path):
    # gopro-overlay's msi/compass-arrow components don't accept x/y
    # attributes of their own (unlike metric/text/chart) -- confirmed by
    # reading gopro_overlay/layout_xml.py's @allow_attributes lists. If this
    # ever regressed to emitting x/y directly on <component
    # type="msi"|"compass-arrow">, gopro-dashboard.py would reject the
    # layout outright.
    path = tmp_path / "gauge.xml"
    tc.write_gauge_layout(path, 2896, 1876, "mph")
    frame = _parse_gauge_layout(path)
    for tag in ("msi", "compass-arrow"):
        el = frame.find(f".//component[@type='{tag}']")
        assert "x" not in el.attrib and "y" not in el.attrib
        parent_tag = None
        for parent in frame.iter():
            if el in list(parent):
                parent_tag = parent.tag
        assert parent_tag == "translate"


def test_write_gauge_layout_compass_arrow_is_red(tmp_path):
    # The heading arrow is colored (compass-arrow's `arrow=` attribute) so
    # it reads distinctly from the fixed white N/S/E/W ring -- the standard
    # red-arrow compass convention the plain "compass" widget can't do at
    # all (single shared `fg` color for everything).
    path = tmp_path / "gauge.xml"
    tc.write_gauge_layout(path, 2896, 1876, "mph")
    frame = _parse_gauge_layout(path)
    el = frame.find(".//component[@type='compass-arrow']")
    assert el.get("arrow") == tc.GAUGE_COMPASS_ARROW_RGB


def test_write_gauge_layout_is_valid_xml_for_every_size():
    # No exception, and re-parseable, across a spread of tile sizes (tall
    # grid's full-width hero row vs. landscape's native-res hero block).
    import tempfile
    from pathlib import Path
    for tile_w, tile_h in [(2896, 1876), (1448, 938), (3840, 2160), (100, 80)]:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "gauge.xml"
            tc.write_gauge_layout(path, tile_w, tile_h, "kph")
            ET.fromstring(path.read_text())  # raises on malformed XML


# --- CLI parsing: --gauge / --gauge-units ------------------------------------

def test_cli_gauge_default_off():
    args = tc.build_parser().parse_args(["/some/folder"])
    assert args.gauge is False
    assert args.gauge_units == "mph"


def test_cli_gauge_flag():
    args = tc.build_parser().parse_args(["/some/folder", "--gauge"])
    assert args.gauge is True


def test_cli_gauge_units_kph():
    args = tc.build_parser().parse_args(["/some/folder", "--gauge", "--gauge-units", "kph"])
    assert args.gauge_units == "kph"


def test_cli_gauge_units_bad_value_dies():
    with pytest.raises(SystemExit):
        tc.build_parser().parse_args(["/some/folder", "--gauge-units", "furlongs-per-fortnight"])


# --- --gauge + a paired --feature: the die() path in setup_tools -------------

def _tools_args(**kw):
    class A:
        pass
    a = A()
    a.no_labels = False
    a.blur_faces = False
    a.map = False
    a.map_zoom = 19
    a.map_mag = 2.0
    a.gauge = False
    a.gauge_units = "mph"
    a.fsd_scoreboard = False
    a.fsd_friction_circle = False
    a.feature = "front"
    a.__dict__.update(kw)
    return a


def test_gauge_pair_feature_is_rejected(monkeypatch, tmp_path):
    # Hermetic: stub out real tool discovery (ffmpeg/font/gopro-overlay) so
    # this exercises only the --gauge + paired --feature validation itself,
    # not this machine's actual environment.
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    with pytest.raises(SystemExit):
        tc.setup_tools(_tools_args(gauge=True, feature="repeaters"))


def test_gauge_solo_feature_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    tools = tc.setup_tools(_tools_args(gauge=True, feature="left_pillar"))
    assert tools.map_venv_py == tmp_path / "py"
    assert tools.map_gopro == tmp_path / "gopro"
