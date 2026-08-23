"""Tests for tesla_gps.py output helpers: clip-time/rate parsing, timezone/ISO
formatting, distance, and the GPX/CSV writers. Synthetic data only."""
import json
from datetime import datetime, timezone

import pytest

import tesla_gps as g


# --- parse_clip_time -------------------------------------------------------

def test_parse_clip_time_valid():
    dt = g.parse_clip_time("/some/dir/2026-07-14_18-57-37-front.mp4")
    assert dt == datetime(2026, 7, 14, 18, 57, 37)


def test_parse_clip_time_rejects_derived_names():
    # combined/derived files must not match the strict raw-clip pattern
    assert g.parse_clip_time("2026-07-14_18-57-37-front_combined.mp4") is None
    assert g.parse_clip_time("random.mp4") is None


# --- _parse_rate + probe_fps rejection -------------------------------------

def test_parse_rate_fraction_and_float():
    assert g._parse_rate("30/1") == 30.0
    assert g._parse_rate("24000/1001") == pytest.approx(23.976, abs=1e-3)
    assert g._parse_rate("36") == 36.0


def test_parse_rate_invalid():
    assert g._parse_rate("0/0") is None
    assert g._parse_rate("abc") is None
    assert g._parse_rate("-5") is None


def test_probe_fps_rejects_bogus_r_frame_rate(monkeypatch):
    # avg_frame_rate bogus (10000/1, > plausible ceiling), nb_frames/duration is
    # the true measured average (1800/60 = 30) and must win over r_frame_rate.
    fake_json = json.dumps({"streams": [{
        "avg_frame_rate": "10000/1",
        "r_frame_rate": "10000/1",
        "nb_frames": "1800",
        "duration": "60",
    }]})

    class FakeProc:
        stdout = fake_json
        stderr = ""
        returncode = 0

    monkeypatch.setattr(g.subprocess, "run", lambda *a, **k: FakeProc())
    fps = g.probe_fps("clip.mp4", "ffprobe")
    assert fps == pytest.approx(30.0)


# --- parse_utc_offset ------------------------------------------------------

def test_parse_utc_offset_negative():
    tz = g.parse_utc_offset("-07:00")
    assert tz.utcoffset(None).total_seconds() == -7 * 3600


def test_parse_utc_offset_positive_half_hour():
    tz = g.parse_utc_offset("+05:30")
    assert tz.utcoffset(None).total_seconds() == (5 * 3600 + 30 * 60)


def test_parse_utc_offset_invalid():
    with pytest.raises(ValueError):
        g.parse_utc_offset("not-an-offset")


# --- _iso_time -------------------------------------------------------------

def test_iso_time_naive_has_no_zone():
    t = datetime(2026, 7, 14, 18, 57, 37, 250000)
    s = g._iso_time(t)
    assert s == "2026-07-14T18:57:37.250"
    assert "+" not in s and not s.endswith("Z")


def test_iso_time_with_negative_offset():
    t = datetime(2026, 7, 14, 18, 57, 37, 0)
    s = g._iso_time(t, g.parse_utc_offset("-07:00"))
    assert s == "2026-07-14T18:57:37.000-07:00"


# --- haversine_m -----------------------------------------------------------

def test_haversine_one_degree_lon_at_equator():
    # 1 degree of longitude at the equator ~= 111.19 km
    d = g.haversine_m(0.0, 0.0, 0.0, 1.0)
    assert d == pytest.approx(111195, rel=1e-3)


def test_haversine_zero_distance():
    assert g.haversine_m(40.0, -74.0, 40.0, -74.0) == pytest.approx(0.0)


# --- write_gpx -------------------------------------------------------------

def _sample(frame_index, lat, lon, t):
    return {
        "lat": lat, "lon": lon, "time": t,
        "speed_mps": 12.5, "heading": 90.0,
        "frame_seq": 100 + frame_index, "frame_index": frame_index,
        "source_clip": "2026-07-14_18-57-37-front.mp4",
    }


def test_write_gpx_golden(tmp_path):
    t0 = datetime(2026, 7, 14, 18, 57, 37)
    t1 = datetime(2026, 7, 14, 18, 57, 38)
    samples = [_sample(0, 40.7128000, -74.0060000, t0),
               _sample(36, 40.7130000, -74.0058000, t1)]
    out = tmp_path / "route.gpx"
    g.write_gpx(samples, str(out), track_name="Test route")
    text = out.read_text()
    assert '<gpx version="1.1"' in text
    assert "<name>Test route</name>" in text
    assert text.count("<trkpt") == 2
    assert 'lat="40.7128000" lon="-74.0060000"' in text
    assert "<time>2026-07-14T18:57:37.000</time>" in text
    # Plain "speed" (no tesla: prefix): gopro-overlay's GPX parser only
    # recognizes extension tags whose local name is exactly "speed" -- see
    # write_gpx's comment and CLAUDE.md.
    assert "<speed>12.500</speed>" in text
    assert "<tesla:course_deg>90.00</tesla:course_deg>" in text
    assert "<tesla:frame_index>0</tesla:frame_index>" in text


def test_write_gpx_with_utc_offset(tmp_path):
    t0 = datetime(2026, 7, 14, 18, 57, 37)
    out = tmp_path / "route.gpx"
    g.write_gpx([_sample(0, 40.0, -74.0, t0)], str(out),
                tz=g.parse_utc_offset("-07:00"))
    assert "<time>2026-07-14T18:57:37.000-07:00</time>" in out.read_text()


# --- write_telemetry_csv ---------------------------------------------------

def test_write_telemetry_csv_golden(tmp_path):
    t0 = datetime(2026, 7, 14, 18, 57, 37)
    # a fuller row exercising booleans (written as 1/0) and empty numerics
    sample = {
        "time": t0, "source_clip": "2026-07-14_18-57-37-front.mp4",
        "frame_index": 0, "frame_seq_no": 100,
        "latitude_deg": 40.7128, "longitude_deg": -74.0060, "heading_deg": 90.0,
        "vehicle_speed_mps": 12.5, "gear_state": 4,
        "accelerator_pedal_position": 0.3, "steering_wheel_angle": -1.2,
        "blinker_on_left": True, "blinker_on_right": False,
        "brake_applied": False, "autopilot_state": 2,
        "linear_acceleration_mps2_x": 0.1,
        "linear_acceleration_mps2_y": None,   # absent numeric -> empty cell
        "linear_acceleration_mps2_z": 9.8, "version": 2,
        # convenience aliases that write_telemetry_csv ignores
        "lat": 40.7128, "lon": -74.0060,
    }
    out = tmp_path / "telemetry.csv"
    g.write_telemetry_csv([sample], str(out))
    lines = out.read_text().splitlines()
    header = lines[0].split(",")
    assert header == g.TELEMETRY_COLUMNS
    row = dict(zip(header, lines[1].split(",")))
    assert row["time"] == "2026-07-14T18:57:37.000"
    assert row["blinker_on_left"] == "1"
    assert row["blinker_on_right"] == "0"
    assert row["latitude_deg"] == "40.7128"
    assert row["linear_acceleration_mps2_y"] == ""  # None -> empty
