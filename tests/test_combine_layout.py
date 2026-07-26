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
