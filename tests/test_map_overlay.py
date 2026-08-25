"""Tests for the --map-overlay HUD-style translucent map inset: the pure
corner-selection logic (pick_map_overlay_corner), write_map_overlay_layout's
geometry, and CLI parsing/validation.

Mirrors the boundary test_gauge.py/test_combine_layout.py already draw: unit
-test the geometry and string-building, not the gopro-dashboard.py subprocess
orchestration (no test here calls build_map_overlay/build_route_gpx end to
end either -- that needs real footage, per CLAUDE.md).
"""
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tesla_combine as tc


# --- pick_map_overlay_corner --------------------------------------------------

def test_pick_map_overlay_corner_default_bottom_right():
    # Nothing else active -- the research spike's own recommendation.
    assert tc.pick_map_overlay_corner(
        gauge=False, scoreboard=False, friction_circle=False, note_highway=False
    ) == "bottom-right"


def test_pick_map_overlay_corner_avoids_gauge_bottom_left_by_staying_put():
    # --gauge alone doesn't touch bottom-right -- no fallback needed.
    assert tc.pick_map_overlay_corner(
        gauge=True, scoreboard=False, friction_circle=False, note_highway=False
    ) == "bottom-right"


def test_pick_map_overlay_corner_falls_back_when_friction_circle_present():
    # The real collision risk this flag needs to design around: both
    # --fsd-friction-circle and --map-overlay default to bottom-right.
    assert tc.pick_map_overlay_corner(
        gauge=False, scoreboard=False, friction_circle=True, note_highway=False
    ) == "bottom-left"


def test_pick_map_overlay_corner_falls_back_further_when_gauge_also_present():
    # bottom-right claimed by friction circle, bottom-left claimed by gauge --
    # falls to top-right.
    assert tc.pick_map_overlay_corner(
        gauge=True, scoreboard=False, friction_circle=True, note_highway=False
    ) == "top-right"


def test_pick_map_overlay_corner_scoreboard_occupies_top_right():
    assert tc.pick_map_overlay_corner(
        gauge=True, scoreboard=True, friction_circle=True, note_highway=False
    ) == "bottom-right"  # every real corner claimed -- see docstring point 4


def test_pick_map_overlay_corner_note_highway_also_occupies_top_right():
    # NoteHighway's ribbon isn't corner-anchored, but it sits close enough to
    # the top that a top-right panel would collide with it too -- treated the
    # same as --fsd-scoreboard for this purpose.
    assert tc.pick_map_overlay_corner(
        gauge=True, scoreboard=False, friction_circle=True, note_highway=True
    ) == "bottom-right"  # every real corner claimed -- see docstring point 4


def test_pick_map_overlay_corner_note_highway_alone_does_not_claim_bottom_right():
    # Only gauge/friction-circle affect bottom-right/bottom-left; note-highway
    # alone shouldn't push --map-overlay off its preferred corner.
    assert tc.pick_map_overlay_corner(
        gauge=False, scoreboard=False, friction_circle=False, note_highway=True
    ) == "bottom-right"


def test_pick_map_overlay_corner_all_occupied_falls_back_to_bottom_right():
    # --gauge + --fsd-scoreboard + --fsd-friction-circle + --map-overlay: no
    # free corner remains. Documented degradation (shares bottom-right with
    # the friction circle) rather than an invented fifth position.
    assert tc.pick_map_overlay_corner(
        gauge=True, scoreboard=True, friction_circle=True, note_highway=True
    ) == "bottom-right"


def test_pick_map_overlay_corner_untested_fc_plus_scoreboard_no_gauge():
    # A combination not covered by the other cases above: friction-circle
    # (claims bottom-right) + scoreboard (claims top-right) but NO --gauge --
    # bottom-left is genuinely free, so this should land there, not fall
    # all the way to the shared-bottom-right last resort.
    assert tc.pick_map_overlay_corner(
        gauge=False, scoreboard=True, friction_circle=True, note_highway=False
    ) == "bottom-left"


# --- pick_map_overlay_corner: clock_bottom_left (--landscape collision) ------

def test_pick_map_overlay_corner_clock_bottom_left_default_off():
    # Default (clock_bottom_left unset) behaves exactly as before -- backward
    # compatible, and matches the non-landscape/--no-labels case where the
    # clock never lands in the hero tile's bottom-left corner at all.
    assert tc.pick_map_overlay_corner(
        gauge=False, scoreboard=False, friction_circle=True, note_highway=False
    ) == "bottom-left"


def test_pick_map_overlay_corner_clock_bottom_left_claims_it_even_without_gauge():
    # The real bug this parameter fixes: under --landscape (with labels on),
    # the burned-in clock lands in the hero tile's bottom-left corner
    # regardless of --gauge -- confirmed against a real
    # --landscape --fsd-friction-circle --map-overlay render (no --gauge)
    # showing the clock drawn directly over the map inset before this fix.
    assert tc.pick_map_overlay_corner(
        gauge=False, scoreboard=False, friction_circle=True, note_highway=False,
        clock_bottom_left=True,
    ) == "top-right"


def test_pick_map_overlay_corner_clock_bottom_left_reaches_shared_fallback_without_gauge():
    # With clock_bottom_left, the degenerate "share bottom-right with the
    # friction circle" case is reachable with only THREE overlay flags
    # (--fsd-friction-circle + --map-overlay + --fsd-scoreboard), not four --
    # no --gauge needed, since the clock claims bottom-left on its own.
    assert tc.pick_map_overlay_corner(
        gauge=False, scoreboard=True, friction_circle=True, note_highway=False,
        clock_bottom_left=True,
    ) == "bottom-right"


def test_pick_map_overlay_corner_clock_bottom_left_no_effect_when_bottom_right_free():
    # clock_bottom_left only matters once something has already pushed the
    # panel toward bottom-left -- with bottom-right free, it's irrelevant.
    assert tc.pick_map_overlay_corner(
        gauge=False, scoreboard=False, friction_circle=False, note_highway=False,
        clock_bottom_left=True,
    ) == "bottom-right"


# --- write_map_overlay_layout -------------------------------------------------

def _parse_map_overlay_layout(path):
    root = ET.fromstring(path.read_text())
    frame = root.find("frame")
    assert frame is not None
    return frame


@pytest.mark.parametrize("tile_w,tile_h", [(2896, 1876), (1280, 960), (500, 400)])
@pytest.mark.parametrize("corner", list(tc.MAP_OVERLAY_CORNERS))
def test_write_map_overlay_layout_panel_within_tile_bounds(tmp_path, tile_w, tile_h, corner):
    path = tmp_path / "map_overlay.xml"
    tc.write_map_overlay_layout(path, tile_w, tile_h, corner=corner, zoom=19)
    frame = _parse_map_overlay_layout(path)
    x, y = int(frame.get("x")), int(frame.get("y"))
    w, h = int(frame.get("width")), int(frame.get("height"))
    assert x >= 0 and y >= 0
    assert x + w <= tile_w
    assert y + h <= tile_h


@pytest.mark.parametrize("tile_w,tile_h", [(2896, 1876), (1280, 960), (500, 1200)])
def test_write_map_overlay_layout_panel_side_scales_with_shorter_dimension(tmp_path, tile_w, tile_h):
    path = tmp_path / "map_overlay.xml"
    tc.write_map_overlay_layout(path, tile_w, tile_h, corner="bottom-right", zoom=19)
    frame = _parse_map_overlay_layout(path)
    w, h = int(frame.get("width")), int(frame.get("height"))
    assert w == h  # deliberately square -- see MAP_OVERLAY_SIZE_FRAC's own comment
    assert w == pytest.approx(min(tile_w, tile_h) * tc.MAP_OVERLAY_SIZE_FRAC, abs=2)


def test_write_map_overlay_layout_bottom_right_margin(tmp_path):
    path = tmp_path / "map_overlay.xml"
    tc.write_map_overlay_layout(path, 2896, 1876, corner="bottom-right", zoom=19)
    frame = _parse_map_overlay_layout(path)
    x, y = int(frame.get("x")), int(frame.get("y"))
    w, h = int(frame.get("width")), int(frame.get("height"))
    assert x == 2896 - w - tc.MAP_OVERLAY_MARGIN
    assert y == 1876 - h - tc.MAP_OVERLAY_MARGIN


def test_write_map_overlay_layout_bottom_left_margin(tmp_path):
    path = tmp_path / "map_overlay.xml"
    tc.write_map_overlay_layout(path, 2896, 1876, corner="bottom-left", zoom=19)
    frame = _parse_map_overlay_layout(path)
    x, y = int(frame.get("x")), int(frame.get("y"))
    h = int(frame.get("height"))
    assert x == tc.MAP_OVERLAY_MARGIN
    assert y == 1876 - h - tc.MAP_OVERLAY_MARGIN


def test_write_map_overlay_layout_top_right_margin(tmp_path):
    path = tmp_path / "map_overlay.xml"
    tc.write_map_overlay_layout(path, 2896, 1876, corner="top-right", zoom=19)
    frame = _parse_map_overlay_layout(path)
    x, y = int(frame.get("x")), int(frame.get("y"))
    w = int(frame.get("width"))
    assert x == 2896 - w - tc.MAP_OVERLAY_MARGIN
    assert y == tc.MAP_OVERLAY_MARGIN


def test_write_map_overlay_layout_unknown_corner_raises(tmp_path):
    path = tmp_path / "map_overlay.xml"
    with pytest.raises(ValueError):
        tc.write_map_overlay_layout(path, 2896, 1876, corner="top-left", zoom=19)


def test_write_map_overlay_layout_translucent_bg_matches_gauge_starting_value(tmp_path):
    # Starts from --gauge's own panel alpha value.
    path = tmp_path / "map_overlay.xml"
    tc.write_map_overlay_layout(path, 2896, 1876, corner="bottom-right", zoom=19)
    frame = _parse_map_overlay_layout(path)
    bg = frame.get("bg")
    assert bg == f"0,0,0,{tc.MAP_OVERLAY_BG_ALPHA}"


def test_write_map_overlay_layout_sets_opacity_not_just_bg_alpha(tmp_path):
    # The actual translucency control: gopro-overlay's Frame widget
    # (gopro_overlay/widgets/widgets.py) builds its alpha mask from a
    # SEPARATE `opacity` attribute (0.0-1.0), not from `bg`'s own alpha
    # component -- `bg=`'s alpha is cosmetic only. Confirmed against a real
    # render: without `opacity=` the panel came out fully opaque, not
    # translucent (see CLAUDE.md's Verification status). Regression-pins
    # that fix.
    path = tmp_path / "map_overlay.xml"
    tc.write_map_overlay_layout(path, 2896, 1876, corner="bottom-right", zoom=19)
    frame = _parse_map_overlay_layout(path)
    opacity = float(frame.get("opacity"))
    assert opacity == pytest.approx(tc.MAP_OVERLAY_BG_ALPHA / 255, abs=0.001)


def test_write_map_overlay_layout_contains_moving_journey_map(tmp_path):
    path = tmp_path / "map_overlay.xml"
    tc.write_map_overlay_layout(path, 2896, 1876, corner="bottom-right", zoom=17)
    frame = _parse_map_overlay_layout(path)
    components = list(frame.iter("component"))
    assert len(components) == 1
    comp = components[0]
    assert comp.get("type") == "moving_journey_map"
    assert comp.get("zoom") == "17"


def test_write_map_overlay_layout_map_widget_fits_inside_pad(tmp_path):
    path = tmp_path / "map_overlay.xml"
    tc.write_map_overlay_layout(path, 2896, 1876, corner="bottom-right", zoom=19)
    root = ET.fromstring(path.read_text())
    frame = root.find("frame")
    panel = int(frame.get("width"))
    comp = frame.find("translate/component")
    size = int(comp.get("size"))
    pad = max(2, round(panel * tc.MAP_OVERLAY_PAD_FRAC))
    assert size == panel - 2 * pad
    assert size > 0


def test_write_map_overlay_layout_is_valid_xml_for_every_size_and_corner():
    import tempfile
    for tile_w, tile_h in [(2896, 1876), (1280, 960), (500, 400), (3840, 2160)]:
        for corner in tc.MAP_OVERLAY_CORNERS:
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "map_overlay.xml"
                tc.write_map_overlay_layout(path, tile_w, tile_h, corner=corner, zoom=19)
                ET.fromstring(path.read_text())  # raises on malformed XML


# --- CLI parsing/defaults -----------------------------------------------------

def test_cli_map_overlay_default_off():
    args = tc.build_parser().parse_args(["/some/folder"])
    assert args.map_overlay is False


def test_cli_map_overlay_flag():
    args = tc.build_parser().parse_args(["/some/folder", "--map-overlay"])
    assert args.map_overlay is True


def test_cli_map_overlay_combines_with_map():
    args = tc.build_parser().parse_args(["/some/folder", "--map", "--map-overlay"])
    assert args.map is True
    assert args.map_overlay is True


# --- --map-overlay + a paired --feature: the die() path in setup_tools -------

def _map_overlay_tools_args(**kw):
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
    a.map_overlay = False
    a.map_overlay_alpha = tc.MAP_OVERLAY_BG_ALPHA
    a.fsd_scoreboard = False
    a.fsd_friction_circle = False
    a.fsd_note_highway = False
    a.feature = "front"
    a.__dict__.update(kw)
    return a


def test_map_overlay_pair_feature_is_rejected(monkeypatch, tmp_path):
    # Mirrors test_fsd_friction_circle_pair_feature_is_rejected exactly --
    # same restriction, same reasoning (composites onto one hero tile).
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    with pytest.raises(SystemExit):
        tc.setup_tools(_map_overlay_tools_args(map_overlay=True, feature="repeaters"))


def test_map_overlay_solo_feature_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    tools = tc.setup_tools(_map_overlay_tools_args(map_overlay=True, feature="left_pillar"))
    assert tools.map_venv_py == tmp_path / "py"
    assert tools.map_gopro == tmp_path / "gopro"


def test_map_overlay_missing_tooling_message_names_the_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (None, None, ["gopro-overlay"]))

    with pytest.raises(SystemExit):
        tc.setup_tools(_map_overlay_tools_args(map_overlay=True))


def test_map_overlay_alone_validates_map_zoom_range(monkeypatch, tmp_path):
    # --map-overlay reuses --map-zoom (no separate flag) -- confirm the range
    # check fires even when --map itself is off.
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    with pytest.raises(SystemExit):
        tc.setup_tools(_map_overlay_tools_args(map_overlay=True, map_zoom=25))


def test_map_overlay_alone_ignores_map_mag_range(monkeypatch, tmp_path):
    # --map-mag is --map-only -- an out-of-range value shouldn't die when
    # only --map-overlay (not --map) is active.
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    tools = tc.setup_tools(_map_overlay_tools_args(map_overlay=True, map_mag=99.0))
    assert tools.map_venv_py == tmp_path / "py"


def test_map_overlay_alpha_out_of_range_dies(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    with pytest.raises(SystemExit):
        tc.setup_tools(_map_overlay_tools_args(map_overlay=True, map_overlay_alpha=256))
    with pytest.raises(SystemExit):
        tc.setup_tools(_map_overlay_tools_args(map_overlay=True, map_overlay_alpha=-1))


def test_map_overlay_alpha_endpoints_are_accepted(monkeypatch, tmp_path):
    # 0 and 255 are the inclusive ends of the documented range -- neither
    # should die.
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    for endpoint in (0, 255):
        tools = tc.setup_tools(_map_overlay_tools_args(map_overlay=True,
                                                        map_overlay_alpha=endpoint))
        assert tools.map_venv_py == tmp_path / "py"


def test_map_overlay_alpha_ignored_when_map_overlay_off(monkeypatch, tmp_path):
    # Out-of-range alpha shouldn't die if --map-overlay itself isn't set --
    # matches --map-zoom/--map-mag's own "only validated when relevant" style.
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")

    tools = tc.setup_tools(_map_overlay_tools_args(map_overlay=False, map_overlay_alpha=999))
    assert tools.map_venv_py is None


def test_write_map_overlay_layout_default_alpha(tmp_path):
    path = tmp_path / "layout.xml"
    tc.write_map_overlay_layout(path, 1000, 800, corner="bottom-right", zoom=19)
    text = path.read_text()
    assert f'bg="0,0,0,{tc.MAP_OVERLAY_BG_ALPHA}"' in text
    assert f'opacity="{tc.MAP_OVERLAY_BG_ALPHA / 255:.3f}"' in text


def test_write_map_overlay_layout_custom_alpha(tmp_path):
    # The override actually reaches the XML -- both bg='s alpha component
    # AND the separate opacity= attribute (see MAP_OVERLAY_BG_ALPHA's own
    # comment on why gopro-overlay's Frame widget needs both).
    path = tmp_path / "layout.xml"
    tc.write_map_overlay_layout(path, 1000, 800, corner="bottom-right", zoom=19, bg_alpha=40)
    text = path.read_text()
    assert 'bg="0,0,0,40"' in text
    assert 'opacity="0.157"' in text


# --- --map-overlay: filename suffix / chaining (build_grid, --dry-run) -------

def _map_overlay_grid_plan(tmp_path):
    """Mirrors _scoreboard_grid_plan in test_combine_layout.py."""
    selections = {
        "front": (["front1.mp4"], 0.0, 600.0),
        "back": (["back1.mp4"], 0.0, 600.0),
    }
    dims = {"front": (1280, 960), "back": (1280, 960)}
    footage = {"front": 600.0, "back": 600.0}
    plan = tc.Plan(
        folder=tmp_path, out_dir=tmp_path, session_name="session",
        by_angle={"front": [], "back": []},
        session_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        n_clips=2, in_bytes=0, selections=selections, dims=dims, epoch=0,
        footage=footage, steps=[],
    )
    return plan


def _map_overlay_stats(**overrides):
    # All three --fsd-* widgets share ONE timing counter (fsd_overlay_s --
    # see build_fsd_overlay's consolidation), but each still gets its own
    # "*_built" flag.
    stats = {"gps_s": 0.0, "map_s": 0.0, "gauge_s": 0.0, "gauge_built": False,
             "fsd_overlay_s": 0.0,
             "scoreboard_built": False, "friction_circle_built": False,
             "note_highway_built": False,
             "map_overlay_s": 0.0, "map_overlay_built": False, "grid_s": 0.0}
    stats.update(overrides)
    return stats


def test_map_overlay_filename_suffix(tmp_path):
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--map-overlay", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _map_overlay_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _map_overlay_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    out_grid, final_w, final_h = tc.build_grid(args, tools, plan, angle_paths, stats,
                                               tmp_path, progress)

    assert stats["map_overlay_built"] is True
    assert out_grid.name == "session_grid_map-overlay.mp4"


def test_map_overlay_and_sidebar_map_both_present(tmp_path):
    # --map --map-overlay together: both a sidebar tile AND a HUD inset --
    # additive, not either/or (see CLAUDE.md's design decision).
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--map", "--map-overlay", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _map_overlay_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _map_overlay_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    out_grid, final_w, final_h = tc.build_grid(args, tools, plan, angle_paths, stats,
                                               tmp_path, progress)

    assert stats["map_overlay_built"] is True
    assert tc.MAP_TILE_KEY in angle_paths
    assert out_grid.name == "session_grid_map_map-overlay.mp4"


def test_map_overlay_chains_after_other_hero_overlays(tmp_path):
    # --gauge --fsd-scoreboard --fsd-friction-circle --fsd-note-highway
    # --map-overlay together: map-overlay is the newest, so it goes last in
    # the filename (matching chain order through angle_paths[hero_angle]).
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--gauge", "--fsd-scoreboard", "--fsd-friction-circle",
         "--fsd-note-highway", "--map-overlay", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _map_overlay_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _map_overlay_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    out_grid, final_w, final_h = tc.build_grid(args, tools, plan, angle_paths, stats,
                                               tmp_path, progress)

    assert stats["gauge_built"] is True
    assert stats["scoreboard_built"] is True
    assert stats["friction_circle_built"] is True
    assert stats["note_highway_built"] is True
    assert stats["map_overlay_built"] is True
    assert out_grid.name == (
        "session_grid_gauge_scoreboard_friction-circle_note-highway_map-overlay.mp4")

    # And the collision-avoidance logic actually fired for real inside
    # build_grid, not just in isolation: with gauge (bottom-left) and
    # friction-circle (bottom-right) both active, and scoreboard/note-highway
    # both claiming top-right, the written layout XML should record the
    # documented "no free corner" fallback (shares bottom-right).
    layout_path = tmp_path / "map_overlay_layout.xml"
    assert layout_path.exists()
    root = ET.fromstring(layout_path.read_text())
    frame = root.find("frame")
    x, w = int(frame.get("x")), int(frame.get("width"))
    tile_w = plan.dims["front"][0]
    assert x == tile_w - w - tc.MAP_OVERLAY_MARGIN  # bottom-right x position


def test_map_overlay_corner_reflects_friction_circle_collision(tmp_path):
    # --map-overlay --fsd-friction-circle together (no --gauge): the one
    # real collision this flag needs to design around. Confirm the actual
    # written layout XML lands at bottom-LEFT, not sharing bottom-right with
    # the friction circle.
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--fsd-friction-circle", "--map-overlay", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _map_overlay_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _map_overlay_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    tc.build_grid(args, tools, plan, angle_paths, stats, tmp_path, progress)

    layout_path = tmp_path / "map_overlay_layout.xml"
    root = ET.fromstring(layout_path.read_text())
    frame = root.find("frame")
    x = int(frame.get("x"))
    assert x == tc.MAP_OVERLAY_MARGIN  # bottom-left x position, not bottom-right


def test_map_overlay_corner_avoids_landscape_clock_collision(tmp_path):
    # The real bug an independent review caught: under --landscape (labels
    # on), the burned-in clock lands in the hero tile's own bottom-left
    # corner (canvas bottom-left IS hero-tile bottom-left in that layout),
    # so --map-overlay --fsd-friction-circle together must NOT fall back to
    # bottom-left here the way the non-landscape case above does -- it
    # should skip past it to top-right instead. has_text=True is essential:
    # this is exactly the has_text-gated clock_bottom_left condition wired
    # in build_grid.
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--landscape", "--fsd-friction-circle", "--map-overlay",
         "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=True,
                     font="/System/Library/Fonts/Menlo.ttc",
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _map_overlay_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _map_overlay_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    tc.build_grid(args, tools, plan, angle_paths, stats, tmp_path, progress)

    layout_path = tmp_path / "map_overlay_layout.xml"
    root = ET.fromstring(layout_path.read_text())
    frame = root.find("frame")
    x, y = int(frame.get("x")), int(frame.get("y"))
    tile_w = plan.dims["front"][0]
    w = int(frame.get("width"))
    assert x == tile_w - w - tc.MAP_OVERLAY_MARGIN  # top-right x position
    assert y == tc.MAP_OVERLAY_MARGIN                # top-right y position


def test_map_overlay_corner_landscape_without_labels_keeps_bottom_left(tmp_path):
    # The clock only exists when labels are on (_apply_tail's has_text
    # guard) -- --landscape alone, with --no-labels, must NOT trigger the
    # clock_bottom_left fallback.
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--landscape", "--no-labels", "--fsd-friction-circle",
         "--map-overlay", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _map_overlay_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _map_overlay_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    tc.build_grid(args, tools, plan, angle_paths, stats, tmp_path, progress)

    layout_path = tmp_path / "map_overlay_layout.xml"
    root = ET.fromstring(layout_path.read_text())
    frame = root.find("frame")
    x = int(frame.get("x"))
    assert x == tc.MAP_OVERLAY_MARGIN  # bottom-left x position, not top-right


def test_map_overlay_dry_run_uses_map_zoom(tmp_path, capsys):
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--map-overlay", "--map-zoom", "14", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _map_overlay_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _map_overlay_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    tc.build_grid(args, tools, plan, angle_paths, stats, tmp_path, progress)

    layout_path = tmp_path / "map_overlay_layout.xml"
    root = ET.fromstring(layout_path.read_text())
    comp = root.find("frame/translate/component")
    assert comp.get("zoom") == "14"
