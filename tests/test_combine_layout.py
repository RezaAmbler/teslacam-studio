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

    assert real["linear_acceleration_mps2_x"] == 0.9
    assert real["autopilot_state"] == 1
    assert out[0]["linear_acceleration_mps2_y"] is None
    assert out[0]["autopilot_state"] is None


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
