"""Tests for the pure layout/filter/timing helpers in tesla_combine.py.

No ffmpeg, no footage -- these exercise the arithmetic and graph-building only.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tesla_combine as tc


SESSION = datetime(2026, 7, 14, 18, 52, 35)
ALL6 = ["front", "back", "left_repeater", "right_repeater",
        "left_pillar", "right_pillar"]


# --- build_rows ------------------------------------------------------------

def test_build_rows_six_cameras_front_hero():
    rows, hero = tc.build_rows(ALL6, "front")
    assert hero == ["front"]
    # hero row is slotted second (after the first pair row)
    assert rows[1] == ["front"]
    assert ["left_pillar", "right_pillar"] in rows
    assert ["left_repeater", "right_repeater"] in rows
    assert ["back"] in rows
    # every present angle appears exactly once
    flat = [a for r in rows for a in r]
    assert sorted(flat) == sorted(ALL6)


def test_build_rows_solo_feature_orphans_pair_partner():
    rows, hero = tc.build_rows(ALL6, "left_pillar")
    assert hero == ["left_pillar"]
    flat = [a for r in rows for a in r]
    # right_pillar's partner was pulled out; it survives as its own solo row
    assert ["right_pillar"] in rows
    assert sorted(flat) == sorted(ALL6)


def test_build_rows_pair_feature():
    rows, hero = tc.build_rows(ALL6, "repeaters")
    assert hero == ["left_repeater", "right_repeater"]
    assert rows[1] == ["left_repeater", "right_repeater"]


def test_build_rows_missing_angles_single_camera():
    rows, hero = tc.build_rows(["front"], "front")
    assert rows == [["front"]]
    assert hero == ["front"]


# --- inject_map_row --------------------------------------------------------

def test_inject_map_pairs_with_solo_back():
    rows = [["front"], ["back"], ["left_pillar", "right_pillar"]]
    out = tc.inject_map_row(rows, hero_angles=["front"])
    assert ["back", tc.MAP_TILE_KEY] in out


def test_inject_map_back_is_hero_gets_own_row():
    rows = [["left_pillar", "right_pillar"], ["back"]]
    out = tc.inject_map_row(rows, hero_angles=["back"])
    # back is the hero -> don't attach; map gets its own bottom row
    assert out[-1] == [tc.MAP_TILE_KEY]
    assert ["back", tc.MAP_TILE_KEY] not in out


def test_inject_map_back_absent_gets_own_row():
    rows = [["front"], ["left_pillar", "right_pillar"]]
    out = tc.inject_map_row(rows, hero_angles=["front"])
    assert out[-1] == [tc.MAP_TILE_KEY]


def test_inject_map_back_in_a_pair_row_gets_own_row():
    # back not present as a plain solo row -> map goes to its own row
    rows = [["front"], ["left_repeater", "right_repeater"]]
    out = tc.inject_map_row(rows, hero_angles=["front"])
    assert out[-1] == [tc.MAP_TILE_KEY]


# --- build_filter ----------------------------------------------------------

def _dims_paths(angles):
    dims = {a: (1280, 960) for a in angles}
    paths = {a: Path(f"/x/{a}.mp4") for a in angles}
    return dims, paths


def _realistic_dims(angles=ALL6, with_map=False):
    """The real confirmed Tesla dims (HW4 car): front is genuinely higher-res
    than the other 5 cameras, all sharing one aspect ratio (~1.5437:1)."""
    dims = {a: ((2896, 1876) if a == "front" else (1448, 938)) for a in angles}
    paths = {a: Path(f"/x/{a}.mp4") for a in angles}
    if with_map:
        dims[tc.MAP_TILE_KEY] = (1448, 938)
        paths[tc.MAP_TILE_KEY] = Path("/x/map.mp4")
    return dims, paths


def test_build_filter_invariants_speed_1():
    dims, paths = _dims_paths(ALL6)
    text, order, w, h = tc.build_filter(
        dims, paths, has_text=False, font=None, epoch=0, max_dim=4096,
        native=False, speed=1.0, feature="front")
    # fps normalization on every input, exactly one per camera
    assert text.count(f"fps={tc.OUTPUT_FPS}") == len(ALL6)
    assert "hstack" in text
    assert "vstack" in text
    # no time-remap at 1x
    assert "setpts" not in text
    assert len(order) == len(ALL6)


def test_build_filter_speed_adds_setpts_and_extra_fps():
    dims, paths = _dims_paths(ALL6)
    text, order, w, h = tc.build_filter(
        dims, paths, has_text=False, font=None, epoch=0, max_dim=4096,
        native=False, speed=2.0, feature="front")
    assert "setpts=0.5*PTS" in text
    # one fps per input plus the trailing fps after setpts
    assert text.count(f"fps={tc.OUTPUT_FPS}") == len(ALL6) + 1


def test_build_filter_hero_upscales_to_canvas_width():
    dims, paths = _dims_paths(ALL6)
    text, order, w, h = tc.build_filter(
        dims, paths, has_text=False, font=None, epoch=0, max_dim=4096,
        native=False, speed=1.0, feature="front")
    # pair rows are 2560 wide; the 1280-wide front hero scales up to match
    assert "scale=2560:" in text


# --- landscape_layout --------------------------------------------------------

def test_landscape_layout_worked_example():
    # front hero + 5 other cams + map -> sidebar ~483px, canvas ~3378x1876
    dims, _ = _realistic_dims(with_map=True)
    hero, sidebar, hero_w, H, w_side, canvas_w, canvas_h = tc.landscape_layout(dims, "front")
    assert hero == ["front"]
    assert len(sidebar) == 6  # 5 other cams + map
    assert sidebar[-1] == tc.MAP_TILE_KEY  # map always last
    assert hero_w == 2896
    assert H == 1876
    assert 470 <= w_side <= 495
    assert 3350 <= canvas_w <= 3400
    assert canvas_h == 1876
    aspect = canvas_w / canvas_h
    assert 1.7 <= aspect <= 1.95


def test_landscape_layout_pair_hero():
    dims, _ = _realistic_dims()
    hero, sidebar, hero_w, H, w_side, canvas_w, canvas_h = tc.landscape_layout(dims, "repeaters")
    assert hero == ["left_repeater", "right_repeater"]
    assert set(sidebar) == {"front", "back", "left_pillar", "right_pillar"}
    assert hero_w == 1448 * 2
    assert H == 938
    assert w_side > 0
    assert canvas_w == hero_w + w_side


def test_landscape_layout_fewer_cameras_present():
    dims, _ = _realistic_dims(angles=["front", "back", "left_pillar"])
    hero, sidebar, hero_w, H, w_side, canvas_w, canvas_h = tc.landscape_layout(dims, "front")
    assert hero == ["front"]
    assert sidebar == ["back", "left_pillar"]
    assert canvas_w == hero_w + w_side


def test_landscape_layout_hero_only_no_sidebar():
    dims, _ = _realistic_dims(angles=["left_repeater", "right_repeater"])
    hero, sidebar, hero_w, H, w_side, canvas_w, canvas_h = tc.landscape_layout(dims, "repeaters")
    assert sidebar == []
    assert w_side == 0
    assert canvas_w == hero_w


# --- build_filter_landscape --------------------------------------------------

def test_build_filter_landscape_hero_not_upscaled():
    # Contrast with test_build_filter_hero_upscales_to_canvas_width: the
    # landscape hero gets no scale filter at all -- native resolution.
    dims, paths = _realistic_dims(with_map=True)
    text, order, w, h = tc.build_filter_landscape(
        dims, paths, has_text=False, font=None, epoch=0, max_dim=4096,
        native=False, speed=1.0, feature="front")
    # v0 is the hero (front, first present angle) -- it flows straight into
    # the hstack with no intervening scale= on that tag.
    assert "[v0]scale=" not in text


def test_build_filter_landscape_sidebar_shares_one_scale_width():
    dims, paths = _realistic_dims(with_map=True)
    _, _, _, _, w_side, _, _ = tc.landscape_layout(dims, "front")
    text, order, w, h = tc.build_filter_landscape(
        dims, paths, has_text=False, font=None, epoch=0, max_dim=4096,
        native=False, speed=1.0, feature="front")
    assert text.count(f"scale={w_side}:") == 6  # 5 cams + map, all one width


def test_build_filter_landscape_pair_hero_hstacks():
    dims, paths = _realistic_dims()
    text, order, w, h = tc.build_filter_landscape(
        dims, paths, has_text=False, font=None, epoch=0, max_dim=4096,
        native=False, speed=1.0, feature="repeaters")
    assert "hstack=inputs=2:shortest=1[hero];" in text


def test_build_filter_landscape_map_present_and_label_less():
    dims, paths = _realistic_dims(with_map=True)
    text, order, w, h = tc.build_filter_landscape(
        dims, paths, has_text=True, font="/font.ttf", epoch=0, max_dim=4096,
        native=False, speed=1.0, feature="front")
    assert paths[tc.MAP_TILE_KEY] in order
    # map tile's own chain has no drawtext label (it's the only input with no
    # LABEL_TEXT entry, so no fontsize=20 label chain references it)
    map_idx = order.index(paths[tc.MAP_TILE_KEY])
    map_chunk = text.split(f"[{map_idx}:v]")[1].split(";")[0]
    assert "drawtext" not in map_chunk


def test_build_filter_landscape_speed_and_fps_invariants():
    dims, paths = _realistic_dims(with_map=True)
    text, order, w, h = tc.build_filter_landscape(
        dims, paths, has_text=False, font=None, epoch=0, max_dim=4096,
        native=False, speed=2.0, feature="front")
    assert "setpts=0.5*PTS" in text
    assert text.count(f"fps={tc.OUTPUT_FPS}") == len(order) + 1


def test_build_filter_landscape_native_skips_cap():
    dims, paths = _realistic_dims(with_map=True)
    text, order, w, h = tc.build_filter_landscape(
        dims, paths, has_text=False, font=None, epoch=0, max_dim=100,
        native=True, speed=1.0, feature="front")
    assert "scale=100" not in text
    assert w > 100 and h > 100


def test_build_filter_landscape_sidebar_heights_sum_to_hero_height():
    dims, paths = _realistic_dims(with_map=True)
    _, _, _, H, w_side, _, _ = tc.landscape_layout(dims, "front")
    text, order, w, h = tc.build_filter_landscape(
        dims, paths, has_text=False, font=None, epoch=0, max_dim=4096,
        native=False, speed=1.0, feature="front")
    heights = [int(m.split("[")[0]) for m in text.split(f"scale={w_side}:")[1:]]
    assert sum(heights) == H


# --- hw_fit_scale / fit_dims ----------------------------------------------

def test_hw_fit_scale_within_limit_returns_none():
    assert tc.hw_fit_scale(4096, 2000, 4096) is None
    assert tc.hw_fit_scale(4096, 4096, 4096) is None


def test_hw_fit_scale_width_over_limit():
    assert tc.hw_fit_scale(4097, 2000, 4096) == "4096:-2"


def test_hw_fit_scale_height_over_limit():
    assert tc.hw_fit_scale(2000, 4097, 4096) == "-2:4096"


def test_fit_dims_even_rounding():
    w, h = tc.fit_dims(5000, 2500, 4096)
    assert (w, h) == (4096, 2048)
    assert w % 2 == 0 and h % 2 == 0


def test_fit_dims_tall():
    w, h = tc.fit_dims(2500, 5000, 4096)
    assert h == 4096
    assert w % 2 == 0


# --- parse_trim ------------------------------------------------------------

def test_parse_trim_seconds():
    assert tc.parse_trim("90", SESSION) == 90.0


def test_parse_trim_hhmmss():
    # 18:59:00 - 18:52:35 = 385s
    assert tc.parse_trim("18:59:00", SESSION) == 385.0


def test_parse_trim_none():
    assert tc.parse_trim(None, SESSION) is None


def test_parse_trim_before_session_start_dies():
    with pytest.raises(SystemExit):
        tc.parse_trim("10:00:00", SESSION)


def test_parse_trim_out_of_range_wallclock_dies_friendly():
    # minute 75 would raise a raw ValueError from .replace(); must die friendly.
    with pytest.raises(SystemExit):
        tc.parse_trim("18:75:00", SESSION)


def test_parse_trim_garbage_dies():
    with pytest.raises(SystemExit):
        tc.parse_trim("not-a-time", SESSION)


# --- retime_samples --------------------------------------------------------

BASE = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _secs(sample):
    return (sample["time"] - BASE).total_seconds()


def _s(frame_index, lat=40.0, lon=-74.0):
    return {"frame_index": frame_index, "lat": lat, "lon": lon,
            "speed_mps": 10.0, "heading": 90.0}


def test_retime_single_clip_basic():
    # fps 10, sample at frame 0 and 10 -> t=0.0 and 1.0
    out = tc.retime_samples([[_s(0), _s(10)]], [10.0], [60.0], 0.0, 60.0)
    times = [round(_secs(s), 3) for s in out]
    # first at 0.0 (no head pad), plus tail pad at grid_dur+0.5
    assert times[0] == 0.0
    assert times[1] == 1.0
    assert times[-1] == pytest.approx(60.5)  # tail edge-hold


def test_retime_multi_clip_with_gap():
    # clip0 dur 60 but only 20s of content, clip1 dur 60; concat squeezes the gap
    c0 = [_s(0), _s(100)]     # fps 10 -> 0.0, 10.0
    c1 = [_s(0), _s(50)]      # -> concat_start 60 -> 60.0, 65.0
    out = tc.retime_samples([c0, c1], [10.0, 10.0], [60.0, 60.0], 0.0, 130.0)
    times = [round(_secs(s), 3) for s in out]
    assert 60.0 in times   # first sample of clip1 lands at sum of prior durs
    assert 65.0 in times


def test_retime_trim_offset_shifts_and_drops_pre_window():
    # offset 5s: frame 0 -> ct = -5 (dropped); frame 100 -> ct = 5
    c0 = [_s(0), _s(100)]  # fps 10 -> -5.0 (out), 5.0
    out = tc.retime_samples([c0], [10.0], [60.0], 5.0, 55.0)
    times = [round(_secs(s), 3) for s in out]
    assert -5.0 not in times
    # the in-window sample survives; a head pad is inserted at 0.0
    assert 0.0 in times     # edge-hold head pad (first real sample at 5.0)
    assert 5.0 in times


def test_retime_out_of_window_tail_dropped():
    # sample beyond grid_dur is dropped
    c0 = [_s(0), _s(1000)]   # fps 10 -> 0.0, 100.0 (grid_dur 50 -> dropped)
    out = tc.retime_samples([c0], [10.0], [60.0], 0.0, 50.0)
    times = [round(_secs(s), 3) for s in out]
    assert 100.0 not in times
    assert 0.0 in times


def test_retime_empty_when_no_samples():
    assert tc.retime_samples([[]], [10.0], [60.0], 0.0, 60.0) == []


def test_retime_head_and_tail_edge_hold():
    # single sample mid-window -> both a head pad (t=0) and tail pad appear
    out = tc.retime_samples([[_s(50)]], [10.0], [60.0], 0.0, 60.0)  # frame 50 -> 5.0
    times = [round(_secs(s), 3) for s in out]
    assert times[0] == 0.0            # head edge-hold
    assert 5.0 in times               # the real sample
    assert times[-1] == pytest.approx(60.5)  # tail edge-hold
    # head/tail padding copies the nearest known position
    assert out[0]["lat"] == out[1]["lat"]


def test_retime_passes_through_fsd_overlay_fields():
    # linear_acceleration_mps2_x/y and autopilot_state must survive retiming --
    # write_gpx() repurposes them into <cad>/<power>/<hr> for tesla_fsd_overlay.py,
    # and it can only do that if retime_samples doesn't drop them first.
    s = _s(0)
    s["linear_acceleration_mps2_x"] = 0.42
    s["linear_acceleration_mps2_y"] = -1.5
    s["autopilot_state"] = 1
    out = tc.retime_samples([[s]], [10.0], [60.0], 0.0, 60.0)
    real = out[0]  # frame 0 -> t=0.0, no head pad needed
    assert real["linear_acceleration_mps2_x"] == 0.42
    assert real["linear_acceleration_mps2_y"] == -1.5
    assert real["autopilot_state"] == 1


def test_retime_fsd_overlay_fields_default_none_when_absent():
    # samples without these fields (e.g. older extraction, or a camera angle
    # where they weren't decoded) shouldn't raise -- just carry through as None.
    out = tc.retime_samples([[_s(0)]], [10.0], [60.0], 0.0, 60.0)
    assert out[0]["linear_acceleration_mps2_x"] is None


def test_retime_edge_pads_null_fsd_fields_but_keep_position():
    # A held first/last position is correct (the car sat still) -- but holding
    # the FSD-overlay fields across the same pad would fabricate autopilot/G
    # data for a stretch with zero real telemetry (e.g. an eventual hands-free
    # scoreboard counting time it shouldn't). Pads must null these three
    # fields specifically while still copying lat/lon from the nearest sample.
    s = _s(50)  # mid-window -> both a head pad (t=0) and tail pad appear
    s["linear_acceleration_mps2_x"] = 0.9
    s["linear_acceleration_mps2_y"] = -0.3
    s["autopilot_state"] = 1
    out = tc.retime_samples([[s]], [10.0], [60.0], 0.0, 60.0)
    head_pad, real, tail_pad = out[0], out[1], out[-1]

    for pad in (head_pad, tail_pad):
        assert pad["linear_acceleration_mps2_x"] is None
        assert pad["linear_acceleration_mps2_y"] is None
        assert pad["autopilot_state"] is None
        assert pad["lat"] == real["lat"]  # position still held, unlike FSD fields


def test_retime_bridges_fsd_fields_across_mid_drive_gap():
    # A real mid-drive SEI dropout (e.g. one clip in a multi-clip event has
    # no SEI while its neighbors do) must NOT let gopro-overlay's own
    # Timeseries.get() linearly interpolate lateral_g/autopilot_engaged
    # straight across the gap -- confirmed as a real hole (an independent
    # review traced gopro-overlay's own interpolation code) in the "a gap
    # must show as a gap" principle every other FSD field/widget follows.
    # Two consecutive real samples 50s apart (clip0 has content only through
    # frame 100 = t10.0; clip1's first sample lands at concat_start=60.0)
    # must get two synthetic None-FSD bridge points inserted just inside
    # each side of the gap.
    s0 = _s(100)
    s0["linear_acceleration_mps2_x"] = -2.0
    s0["autopilot_state"] = 1
    s1 = _s(0)
    s1["linear_acceleration_mps2_x"] = 1.5
    s1["autopilot_state"] = 1
    c0 = [_s(0), s0]
    c1 = [s1, _s(50)]
    out = tc.retime_samples([c0, c1], [10.0, 10.0], [60.0, 60.0], 0.0, 130.0)
    by_time = {round(_secs(s), 3): s for s in out}

    assert 10.0 in by_time and by_time[10.0]["linear_acceleration_mps2_x"] == -2.0
    assert 60.0 in by_time and by_time[60.0]["linear_acceleration_mps2_x"] == 1.5
    # bridge points just inside the gap, both FSD-field-null
    bridge_start = by_time[10.01]
    bridge_end = by_time[59.99]
    for bridge in (bridge_start, bridge_end):
        assert bridge["linear_acceleration_mps2_x"] is None
        assert bridge["autopilot_state"] is None
    # position is left alone across the gap (unlike the FSD fields)
    assert bridge_start["lat"] == s0["lat"]
    assert bridge_end["lat"] == s1["lat"]


def test_retime_no_bridge_for_normal_sample_spacing():
    # Ordinary consecutive-sample spacing (well under GAP_BREAK_SECONDS)
    # must not spuriously insert bridge points -- exact count/timing check.
    out = tc.retime_samples([[_s(0), _s(10)]], [10.0], [60.0], 0.0, 60.0)
    times = [round(_secs(s), 3) for s in out]
    # exactly: real@0.0, real@1.0, tail pad@60.5 -- no bridge inserted for
    # a 1.0s gap (GAP_BREAK_SECONDS requires a STRICTLY greater gap)
    assert times == [0.0, 1.0, 60.5]


# --- looks_tesla_encrypted ---------------------------------------------------

def test_encrypted_sniff_real_mp4_header(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00\x00\x00\x1cftypisom" + b"\x00" * 16)
    assert tc.looks_tesla_encrypted(p) is False


def test_encrypted_sniff_ciphertext(tmp_path):
    p = tmp_path / "clip.mp4"
    # real header captured from an encrypted TeslaCam clip: no ftyp box
    p.write_bytes(bytes.fromhex("0000000002f67870da22124fe6a3a5ba") + b"\x00" * 16)
    assert tc.looks_tesla_encrypted(p) is True


def test_encrypted_sniff_short_or_missing_file(tmp_path):
    short = tmp_path / "short.mp4"
    short.write_bytes(b"\x00\x00")
    assert tc.looks_tesla_encrypted(short) is False   # too short to judge
    assert tc.looks_tesla_encrypted(tmp_path / "nope.mp4") is False


# --- CLI parsing: --landscape / --quality -----------------------------------

def test_cli_landscape_default_off():
    args = tc.build_parser().parse_args(["/some/folder"])
    assert args.landscape is False


def test_cli_landscape_flag():
    args = tc.build_parser().parse_args(["/some/folder", "--landscape"])
    assert args.landscape is True


def test_cli_quality_default_fast():
    args = tc.build_parser().parse_args(["/some/folder"])
    assert args.quality == "fast"


def test_cli_quality_high():
    args = tc.build_parser().parse_args(["/some/folder", "--quality", "high"])
    assert args.quality == "high"


def test_cli_quality_bad_value_dies():
    with pytest.raises(SystemExit):
        tc.build_parser().parse_args(["/some/folder", "--quality", "ultra"])


# --- encoder_args ------------------------------------------------------------

def test_encoder_args_fast_default_is_hardware():
    args, warning = tc.encoder_args(native=False, quality="fast", max_dim=4096,
                                     final_w=1920, final_h=1080)
    assert "h264_videotoolbox" in args
    assert warning is None


def test_encoder_args_quality_high_is_crf18_software():
    args, warning = tc.encoder_args(native=False, quality="high", max_dim=4096,
                                     final_w=1920, final_h=1080)
    assert "libx264" in args
    assert "18" in args
    assert warning is None


def test_encoder_args_oversized_native_falls_back_crf20():
    args, warning = tc.encoder_args(native=True, quality="fast", max_dim=4096,
                                     final_w=5000, final_h=3000)
    assert "libx264" in args
    assert "20" in args
    assert warning is not None


def test_encoder_args_quality_high_overrides_oversized_native_no_warning():
    # --native alone would have fallen back to CRF 20 with a warning; explicit
    # --quality high wins instead, at CRF 18, with no "falling back" warning.
    args, warning = tc.encoder_args(native=True, quality="high", max_dim=4096,
                                     final_w=5000, final_h=3000)
    assert "libx264" in args
    assert "18" in args
    assert "20" not in args
    assert warning is None


# --- --fsd-scoreboard: CLI parsing/defaults ----------------------------------

def test_cli_fsd_scoreboard_default_off():
    args = tc.build_parser().parse_args(["/some/folder"])
    assert args.fsd_scoreboard is False


def test_cli_fsd_scoreboard_flag():
    args = tc.build_parser().parse_args(["/some/folder", "--fsd-scoreboard"])
    assert args.fsd_scoreboard is True


# --- --fsd-scoreboard + a paired --feature: the die() path in setup_tools ---

def _scoreboard_tools_args(**kw):
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
    a.fsd_scoreboard = False
    a.fsd_friction_circle = False
    a.fsd_note_highway = False
    a.feature = "front"
    a.__dict__.update(kw)
    return a


def test_fsd_scoreboard_pair_feature_is_rejected(monkeypatch, tmp_path):
    # Hermetic: stub out real tool discovery (ffmpeg/font/gopro-overlay) so
    # this exercises only the --fsd-scoreboard + paired --feature validation
    # itself, not this machine's actual environment. Mirrors
    # test_gauge.py::test_gauge_pair_feature_is_rejected exactly -- same
    # restriction, same reasoning (composites onto one hero tile).
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    with pytest.raises(SystemExit):
        tc.setup_tools(_scoreboard_tools_args(fsd_scoreboard=True, feature="repeaters"))


def test_fsd_scoreboard_solo_feature_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    tools = tc.setup_tools(_scoreboard_tools_args(fsd_scoreboard=True, feature="left_pillar"))
    assert tools.map_venv_py == tmp_path / "py"
    assert tools.map_gopro == tmp_path / "gopro"


def test_fsd_scoreboard_missing_tooling_message_names_the_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (None, None, ["gopro-overlay"]))

    with pytest.raises(SystemExit):
        tc.setup_tools(_scoreboard_tools_args(fsd_scoreboard=True))


# --- --fsd-scoreboard: filename suffix (build_grid, --dry-run) ---------------

def _scoreboard_grid_plan(tmp_path):
    """A minimal two-camera Plan good enough to drive build_grid end to end
    under --dry-run -- no real ffmpeg/footage needed (see this file's own
    docstring): probe_dims/probe_duration/the actual gopro-overlay subprocess
    are all skipped on the --dry-run path, and build_filter's composition is
    pure string-building from dims/paths, so fabricated dims and non-existent
    video paths are enough to exercise the real code."""
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


def test_fsd_scoreboard_filename_suffix(tmp_path):
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--fsd-scoreboard", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"))
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = {"gps_s": 0.0, "map_s": 0.0, "gauge_s": 0.0, "gauge_built": False,
             "fsd_overlay_s": 0.0, "scoreboard_built": False, "grid_s": 0.0}
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    out_grid, final_w, final_h = tc.build_grid(args, tools, plan, angle_paths, stats,
                                               tmp_path, progress)

    assert stats["scoreboard_built"] is True
    assert "_scoreboard" in out_grid.name
    assert out_grid.name == "session_grid_scoreboard.mp4"


def test_fsd_scoreboard_and_gauge_suffix_order(tmp_path):
    # --gauge --fsd-scoreboard together chain sequentially through
    # angle_paths[hero_angle] (gauge composites first); the filename records
    # both, gauge before scoreboard, matching the chain order.
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--gauge", "--fsd-scoreboard", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = {"gps_s": 0.0, "map_s": 0.0, "gauge_s": 0.0, "gauge_built": False,
             "fsd_overlay_s": 0.0, "scoreboard_built": False, "grid_s": 0.0}
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    out_grid, final_w, final_h = tc.build_grid(args, tools, plan, angle_paths, stats,
                                               tmp_path, progress)

    assert stats["gauge_built"] is True
    assert stats["scoreboard_built"] is True
    assert out_grid.name == "session_grid_gauge_scoreboard.mp4"


# --- --fsd-friction-circle: CLI parsing/defaults ------------------------------

def test_cli_fsd_friction_circle_default_off():
    args = tc.build_parser().parse_args(["/some/folder"])
    assert args.fsd_friction_circle is False


def test_cli_fsd_friction_circle_flag():
    args = tc.build_parser().parse_args(["/some/folder", "--fsd-friction-circle"])
    assert args.fsd_friction_circle is True


# --- --fsd-friction-circle + a paired --feature: the die() path in setup_tools

def test_fsd_friction_circle_pair_feature_is_rejected(monkeypatch, tmp_path):
    # Mirrors test_fsd_scoreboard_pair_feature_is_rejected exactly -- same
    # restriction, same reasoning (composites onto one hero tile).
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    with pytest.raises(SystemExit):
        tc.setup_tools(_scoreboard_tools_args(fsd_friction_circle=True, feature="repeaters"))


def test_fsd_friction_circle_solo_feature_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    tools = tc.setup_tools(_scoreboard_tools_args(fsd_friction_circle=True, feature="left_pillar"))
    assert tools.map_venv_py == tmp_path / "py"
    assert tools.map_gopro == tmp_path / "gopro"


def test_fsd_friction_circle_missing_tooling_message_names_the_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (None, None, ["gopro-overlay"]))

    with pytest.raises(SystemExit):
        tc.setup_tools(_scoreboard_tools_args(fsd_friction_circle=True))


# --- --fsd-friction-circle: filename suffix (build_grid, --dry-run) ----------

def _friction_circle_stats(**overrides):
    """A stats dict with every key build_grid/print_stats might touch across
    --gauge/--fsd-scoreboard/--fsd-friction-circle, all defaulted off -- the
    same shape _scoreboard_grid_plan's callers already build by hand, just
    extended with the friction-circle keys the refactor added. All three
    --fsd-* widgets now share ONE timing counter (stats["fsd_overlay_s"] --
    see build_fsd_overlay's consolidation), but each still gets its own
    "*_built" flag, so those stay separate."""
    stats = {"gps_s": 0.0, "map_s": 0.0, "gauge_s": 0.0, "gauge_built": False,
             "fsd_overlay_s": 0.0,
             "scoreboard_built": False, "friction_circle_built": False, "grid_s": 0.0}
    stats.update(overrides)
    return stats


def test_fsd_friction_circle_filename_suffix(tmp_path):
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--fsd-friction-circle", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"))
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _friction_circle_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    out_grid, final_w, final_h = tc.build_grid(args, tools, plan, angle_paths, stats,
                                               tmp_path, progress)

    assert stats["friction_circle_built"] is True
    assert "_friction-circle" in out_grid.name
    assert out_grid.name == "session_grid_friction-circle.mp4"


def test_fsd_friction_circle_gauge_scoreboard_suffix_order(tmp_path):
    # All three overlay flags together chain sequentially through
    # angle_paths[hero_angle] (gauge, then scoreboard, then friction circle
    # -- the same order build_grid composites them in); the filename records
    # all three in that order.
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--gauge", "--fsd-scoreboard", "--fsd-friction-circle", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _friction_circle_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    out_grid, final_w, final_h = tc.build_grid(args, tools, plan, angle_paths, stats,
                                               tmp_path, progress)

    assert stats["gauge_built"] is True
    assert stats["scoreboard_built"] is True
    assert stats["friction_circle_built"] is True
    assert out_grid.name == "session_grid_gauge_scoreboard_friction-circle.mp4"


def test_fsd_friction_circle_dry_run_passes_widget_flag(tmp_path, capsys):
    # build_fsd_overlay's consolidation passes --widget through to
    # tesla_fsd_overlay.py explicitly -- confirm the printed --dry-run
    # command actually carries the right widget name. log() prints straight
    # to stdout (see tesla_combine.py's log()), not through Progress's own
    # `stream`, so capsys -- not the Progress stream -- is what catches it.
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--fsd-friction-circle", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"))
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _friction_circle_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    tc.build_grid(args, tools, plan, angle_paths, stats, tmp_path, progress)

    printed = capsys.readouterr().out
    assert "--widget friction-circle" in printed


# --- --fsd-note-highway: CLI parsing/defaults ---------------------------------

def test_cli_fsd_note_highway_default_off():
    args = tc.build_parser().parse_args(["/some/folder"])
    assert args.fsd_note_highway is False


def test_cli_fsd_note_highway_flag():
    args = tc.build_parser().parse_args(["/some/folder", "--fsd-note-highway"])
    assert args.fsd_note_highway is True


# --- --fsd-note-highway + a paired --feature: the die() path in setup_tools --

def test_fsd_note_highway_pair_feature_is_rejected(monkeypatch, tmp_path):
    # Mirrors test_fsd_friction_circle_pair_feature_is_rejected exactly --
    # same restriction, same reasoning (composites onto one hero tile).
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    with pytest.raises(SystemExit):
        tc.setup_tools(_scoreboard_tools_args(fsd_note_highway=True, feature="repeaters"))


def test_fsd_note_highway_solo_feature_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (tmp_path / "py", tmp_path / "gopro", []))
    monkeypatch.setattr(tc, "find_map_font", lambda: "/System/Library/Fonts/Menlo.ttc")

    tools = tc.setup_tools(_scoreboard_tools_args(fsd_note_highway=True, feature="left_pillar"))
    assert tools.map_venv_py == tmp_path / "py"
    assert tools.map_gopro == tmp_path / "gopro"


def test_fsd_note_highway_missing_tooling_message_names_the_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "find_ffmpeg", lambda: ("/bin/ffmpeg", True))
    monkeypatch.setattr(tc, "find_font", lambda: "/System/Library/Fonts/Menlo.ttc")
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(tc, "find_map_tooling",
                        lambda script_dir: (None, None, ["gopro-overlay"]))

    with pytest.raises(SystemExit):
        tc.setup_tools(_scoreboard_tools_args(fsd_note_highway=True))


# --- --fsd-note-highway: filename suffix (build_grid, --dry-run) -------------

def _note_highway_stats(**overrides):
    """A stats dict with every key build_grid/print_stats might touch across
    --gauge/--fsd-scoreboard/--fsd-friction-circle/--fsd-note-highway, all
    defaulted off -- extends _friction_circle_stats' shape with the
    note-highway keys this branch added (just another "*_built" flag --
    "fsd_overlay_s" is already shared by all three, see
    _friction_circle_stats)."""
    stats = _friction_circle_stats()
    stats.update({"note_highway_built": False})
    stats.update(overrides)
    return stats


def test_fsd_note_highway_filename_suffix(tmp_path):
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--fsd-note-highway", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"))
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _note_highway_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    out_grid, final_w, final_h = tc.build_grid(args, tools, plan, angle_paths, stats,
                                               tmp_path, progress)

    assert stats["note_highway_built"] is True
    assert "_note-highway" in out_grid.name
    assert out_grid.name == "session_grid_note-highway.mp4"


def test_fsd_note_highway_full_chain_suffix_order(tmp_path):
    # All four overlay flags together (--gauge --fsd-scoreboard
    # --fsd-friction-circle --fsd-note-highway) chain sequentially through
    # angle_paths[hero_angle] -- gauge, then scoreboard, then friction
    # circle, then note highway, the same order build_grid composites them
    # in; the filename records all four in that order. Confirms the full
    # chain now that all four FSD/gauge overlay flags exist.
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--gauge", "--fsd-scoreboard", "--fsd-friction-circle",
         "--fsd-note-highway", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _note_highway_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    out_grid, final_w, final_h = tc.build_grid(args, tools, plan, angle_paths, stats,
                                               tmp_path, progress)

    assert stats["gauge_built"] is True
    assert stats["scoreboard_built"] is True
    assert stats["friction_circle_built"] is True
    assert stats["note_highway_built"] is True
    assert out_grid.name == "session_grid_gauge_scoreboard_friction-circle_note-highway.mp4"


def test_fsd_note_highway_dry_run_passes_widget_flag(tmp_path, capsys):
    # build_fsd_overlay's consolidation passes --widget through to
    # tesla_fsd_overlay.py explicitly -- confirm the printed --dry-run
    # command actually carries the right widget name.
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--fsd-note-highway", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"))
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _note_highway_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    tc.build_grid(args, tools, plan, angle_paths, stats, tmp_path, progress)

    printed = capsys.readouterr().out
    assert "--widget note-highway" in printed


# --- FSD overlay consolidation: one combined pass, not one pass per flag -----
# The actual behavioral change this branch makes: --gauge stays its own
# subprocess (a genuinely different tool), but the three --fsd-* flags now
# share ONE tesla_fsd_overlay.py invocation instead of three sequential
# ones. These tests are deliberately distinct from the pre-existing
# filename-suffix/dry-run-command tests above (which only ever exercised
# one --fsd-* flag at a time, or checked the filename shape, not the call
# count) -- this is the real regression test for the consolidation itself.

def test_fsd_overlay_dry_run_single_combined_invocation(tmp_path, capsys):
    # All three --fsd-* flags together must print exactly ONE
    # tesla_fsd_overlay.py command line, carrying all three widget names in
    # one --widget invocation -- not three separate command lines.
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--gauge", "--fsd-scoreboard", "--fsd-friction-circle",
         "--fsd-note-highway", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _note_highway_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    tc.build_grid(args, tools, plan, angle_paths, stats, tmp_path, progress)

    printed = capsys.readouterr().out
    # Exactly one tesla_fsd_overlay.py command line (--gauge's own
    # gopro-dashboard.py command is a separate, expected invocation).
    assert printed.count("tesla_fsd_overlay.py") == 1
    assert "--widget scoreboard friction-circle note-highway" in printed


def test_fsd_overlay_consolidation_single_build_call_all_widgets(monkeypatch, tmp_path):
    # Monkeypatch build_fsd_overlay itself and count/inspect calls -- confirms
    # build_grid really does call it exactly once with all three widget names,
    # not three times with one widget each (the old per-flag behavior).
    import io
    calls = []

    def fake_build_fsd_overlay(hero_video_path, gpx_path, tile_dims, widgets,
                               ffmpeg, venv_py, out_path, tmpdir, dry_run, progress):
        calls.append(list(widgets))
        return out_path

    monkeypatch.setattr(tc, "build_fsd_overlay", fake_build_fsd_overlay)

    args = tc.build_parser().parse_args(
        ["/some/folder", "--gauge", "--fsd-scoreboard", "--fsd-friction-circle",
         "--fsd-note-highway", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"), map_gopro=Path("fake-gopro"),
                     map_font="/System/Library/Fonts/Menlo.ttc")
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _note_highway_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    tc.build_grid(args, tools, plan, angle_paths, stats, tmp_path, progress)

    assert len(calls) == 1
    assert calls[0] == ["scoreboard", "friction-circle", "note-highway"]


def test_fsd_overlay_consolidation_single_call_with_two_of_three(monkeypatch, tmp_path):
    # Also confirm a PARTIAL combination (two of the three flags, no --gauge)
    # still collapses to one call carrying just the two active widget names,
    # not a full three or a per-flag call.
    import io
    calls = []

    def fake_build_fsd_overlay(hero_video_path, gpx_path, tile_dims, widgets,
                               ffmpeg, venv_py, out_path, tmpdir, dry_run, progress):
        calls.append(list(widgets))
        return out_path

    monkeypatch.setattr(tc, "build_fsd_overlay", fake_build_fsd_overlay)

    args = tc.build_parser().parse_args(
        ["/some/folder", "--fsd-scoreboard", "--fsd-note-highway", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"))
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _note_highway_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    tc.build_grid(args, tools, plan, angle_paths, stats, tmp_path, progress)

    assert len(calls) == 1
    assert calls[0] == ["scoreboard", "note-highway"]


def test_fsd_overlay_dry_run_two_widget_subset_command(tmp_path, capsys):
    # The single-widget dry-run command tests above (friction-circle alone,
    # note-highway alone) don't exercise the multi-value --widget case at
    # all -- confirm a two-of-three combination's printed command carries
    # BOTH names, in the fixed (scoreboard, friction-circle, note-highway)
    # order build_grid always uses, not CLI argument order.
    import io
    args = tc.build_parser().parse_args(
        ["/some/folder", "--fsd-friction-circle", "--fsd-scoreboard", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"))
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _note_highway_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    tc.build_grid(args, tools, plan, angle_paths, stats, tmp_path, progress)

    printed = capsys.readouterr().out
    assert printed.count("tesla_fsd_overlay.py") == 1
    assert "--widget scoreboard friction-circle" in printed


def test_fsd_overlay_step_abandoned_when_no_gps(monkeypatch, tmp_path):
    # If GPS extraction finds no SEI telemetry at all (build_route_gpx
    # returns None), the still-pending fsd_overlay_render step must be
    # dropped by build_grid's progress.abandon(...) call -- this pins the
    # actual kind STRING passed there. abandon() silently ignores any kind
    # name it doesn't recognize (see Progress.abandon's own docstring), so a
    # typo in that call's argument list (e.g. a leftover
    # "scoreboard_render" from before the consolidation) would leave this
    # step stuck "pending" forever with nothing left in the plan to ever
    # finish it -- corrupting the job's ETA -- without any test failing.
    # Real GPS extraction never runs here: build_route_gpx is monkeypatched
    # directly, bypassing --dry-run's own "always succeeds" GPS stub (see
    # that function's own dry_run branch), so this is the one way to
    # exercise the no-GPS path without real footage.
    import io
    monkeypatch.setattr(tc, "build_route_gpx", lambda *a, **k: None)

    args = tc.build_parser().parse_args(
        ["/some/folder", "--fsd-scoreboard", "--fsd-friction-circle", "--dry-run"])
    tools = tc.Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", has_text=False, font=None,
                     map_venv_py=Path("fake-venv-python"))
    plan = _scoreboard_grid_plan(tmp_path)
    angle_paths = {"front": tmp_path / "front_combined.mp4",
                   "back": tmp_path / "back_combined.mp4"}
    stats = _note_highway_stats()
    steps = tc.plan_steps(args, plan.selections, plan.footage)
    progress = tc.Progress(steps, stream=io.StringIO(), ansi=False, verbose=True)

    tc.build_grid(args, tools, plan, angle_paths, stats, tmp_path, progress)

    fsd_step_idx = next(i for i, s in enumerate(progress.steps)
                        if s.kind == "fsd_overlay_render")
    assert progress.state[fsd_step_idx] == "skipped"
    assert stats["scoreboard_built"] is False
    assert stats["friction_circle_built"] is False


# --- FILENAME_RE / discover_clips -------------------------------------------

def test_filename_re_matches_plain_clip():
    m = tc.FILENAME_RE.match("2026-08-22_17-21-05-front.mp4")
    assert m is not None
    assert m.group(1) == "2026-08-22_17-21-05"
    assert m.group(2) == "front"


def test_filename_re_rejects_unrelated_suffix():
    # Any renamed/stray file with a suffix before .mp4 doesn't match -- this
    # tool doesn't try to guess intent from filename variants; see
    # test_discover_clips_warns_on_skipped_files for how a mismatch like
    # this is surfaced instead (a warning, not a silent drop).
    assert tc.FILENAME_RE.match("2026-08-22_17-21-05-front-copy.mp4") is None
    assert tc.FILENAME_RE.match("2026-08-22_17-21-05-front-START.mp4") is None


def test_discover_clips_warns_on_skipped_files(tmp_path, capsys):
    # A renamed/stray .mp4 that doesn't match FILENAME_RE is excluded from
    # the render (correct -- this tool doesn't know how to place it on the
    # timeline) but must warn, by name, rather than silently shrinking the
    # render with nothing to explain why (confirmed real risk: a folder
    # trimmed/renamed by hand can end up missing exactly the clip a
    # --trim-start window needed).
    (tmp_path / "2026-08-22_17-21-05-front.mp4").touch()
    (tmp_path / "2026-08-22_17-21-05-front-START.mp4").touch()
    by_angle, session_start = tc.discover_clips(tmp_path)
    front_names = [p.name for _, p in by_angle["front"]]
    assert front_names == ["2026-08-22_17-21-05-front.mp4"]  # the renamed one is excluded
    printed = capsys.readouterr().out
    assert "1 .mp4 file(s)" in printed
    assert "2026-08-22_17-21-05-front-START.mp4" in printed


def test_discover_clips_no_warning_when_everything_matches(tmp_path, capsys):
    (tmp_path / "2026-08-22_17-21-05-front.mp4").touch()
    (tmp_path / "2026-08-22_17-21-05-back.mp4").touch()
    tc.discover_clips(tmp_path)
    printed = capsys.readouterr().out
    assert "WARNING" not in printed
