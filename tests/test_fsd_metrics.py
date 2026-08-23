"""Tests for tesla_fsd_metrics.py: the pure-Python axis-mapping decode,
corner-count hysteresis detector, and hands-free-seconds accumulator behind
the FSD-overlay foundation branch (tesla_fsd_overlay.py). Synthetic data
only, no gopro_overlay import -- this module is deliberately importable and
testable under the system Python (see its own docstring)."""
import pytest

import tesla_fsd_metrics as m


# --- decode_fsd_fields -------------------------------------------------------

def test_decode_fsd_fields_converts_mps2_to_g():
    # cad/power carry raw m/s^2 (the repurposed linear_acceleration_mps2_x/y);
    # STANDARD_GRAVITY_MPS2 is the exact conversion factor.
    fields = m.decode_fsd_fields(cad=m.STANDARD_GRAVITY_MPS2, power=-m.STANDARD_GRAVITY_MPS2 / 2, hr=1)
    assert fields.lateral_g == pytest.approx(1.0)
    assert fields.longitudinal_g == pytest.approx(-0.5)
    assert fields.autopilot_engaged is True


def test_decode_fsd_fields_autopilot_only_engaged_for_confirmed_state():
    # Only autopilot_state == 1 is confirmed as "engaged" against real footage
    # (CLAUDE.md) -- any other observed/absent value must NOT count as engaged.
    assert m.decode_fsd_fields(cad=0.0, power=0.0, hr=0).autopilot_engaged is False
    assert m.decode_fsd_fields(cad=0.0, power=0.0, hr=2).autopilot_engaged is False
    assert m.decode_fsd_fields(cad=0.0, power=0.0, hr=None).autopilot_engaged is False
    assert m.decode_fsd_fields(cad=0.0, power=0.0, hr=1).autopilot_engaged is True


def test_decode_fsd_fields_missing_inputs_pass_through_as_none():
    fields = m.decode_fsd_fields(cad=None, power=None, hr=None)
    assert fields.lateral_g is None
    assert fields.longitudinal_g is None
    assert fields.autopilot_engaged is False


def test_decode_fsd_fields_zero_is_not_missing():
    # 0.0 is a legitimate reading (straight-line driving at constant speed),
    # must not be conflated with "no data" (None).
    fields = m.decode_fsd_fields(cad=0.0, power=0.0, hr=1)
    assert fields.lateral_g == 0.0
    assert fields.longitudinal_g == 0.0


# --- CornerCounter -----------------------------------------------------------

def test_corner_counter_single_clean_corner_counts_once():
    counter = m.CornerCounter()
    # ramp up through ENTER_G, hold, ramp back down through EXIT_G
    sequence = [0.0, 0.05, 0.10, 0.16, 0.20, 0.16, 0.10, 0.05, 0.0]
    edges = [counter.update(v) for v in sequence]
    assert counter.corner_count == 1
    assert sum(edges) == 1
    assert edges.index(True) == sequence.index(0.16)  # fires exactly on entry crossing


def test_corner_counter_never_crossing_threshold_counts_zero():
    counter = m.CornerCounter()
    for v in [0.0, 0.02, 0.05, 0.08, 0.05, 0.0, -0.05]:
        counter.update(v)
    assert counter.corner_count == 0


def test_corner_counter_noise_around_enter_threshold_without_dropping_below_exit_does_not_doublecount():
    counter = m.CornerCounter()
    # Enters a corner, then jitters back and forth ABOVE exit_g (never
    # actually straightens out) before finally exiting -- must count once.
    sequence = [0.0, 0.16, 0.20, 0.14, 0.18, 0.13, 0.20, 0.10, 0.03, 0.0]
    for v in sequence:
        counter.update(v)
    assert counter.corner_count == 1


def test_corner_counter_two_separate_corners_count_twice():
    counter = m.CornerCounter()
    sequence = [0.0, 0.2, 0.0, 0.0, 0.2, 0.0]  # up, fully down, up again
    for v in sequence:
        counter.update(v)
    assert counter.corner_count == 2


def test_corner_counter_handles_negative_lateral_g_by_magnitude():
    counter = m.CornerCounter()
    for v in [0.0, -0.2, -0.25, 0.0]:
        counter.update(v)
    assert counter.corner_count == 1


def test_corner_counter_none_samples_are_ignored():
    counter = m.CornerCounter()
    for v in [0.0, None, 0.2, None, 0.0]:
        counter.update(v)
    assert counter.corner_count == 1


def test_corner_counter_tracks_peak_lateral_g():
    counter = m.CornerCounter()
    for v in [0.05, 0.3, 0.1, -0.5, 0.2]:
        counter.update(v)
    assert counter.peak_lateral_g == pytest.approx(0.5)


def test_corner_counter_rejects_invalid_threshold_ordering():
    with pytest.raises(ValueError):
        m.CornerCounter(enter_g=0.1, exit_g=0.2)


# --- HandsFreeAccumulator -----------------------------------------------------

def test_hands_free_accumulator_sums_only_engaged_intervals():
    acc = m.HandsFreeAccumulator()
    # engaged, engaged, disengaged, engaged -- each interval 1s
    total = None
    for engaged, dt in [(True, 1.0), (True, 1.0), (False, 1.0), (True, 1.0)]:
        total = acc.update(engaged, dt)
    assert total == pytest.approx(3.0)
    assert acc.hands_free_seconds == pytest.approx(3.0)


def test_hands_free_accumulator_never_engaged_is_zero():
    acc = m.HandsFreeAccumulator()
    for dt in [0.1, 0.1, 0.1]:
        acc.update(False, dt)
    assert acc.hands_free_seconds == 0.0


def test_hands_free_accumulator_matches_known_elapsed_time():
    acc = m.HandsFreeAccumulator()
    # A realistic ~0.1s-cadence sequence: 2s hands-free, 0.5s takeover, 1.5s
    # hands-free again -- total engaged time should equal 3.5s.
    ticks = [(True, 0.1)] * 20 + [(False, 0.1)] * 5 + [(True, 0.1)] * 15
    for engaged, dt in ticks:
        acc.update(engaged, dt)
    assert acc.hands_free_seconds == pytest.approx(3.5)


def test_hands_free_accumulator_ignores_nonpositive_dt():
    acc = m.HandsFreeAccumulator()
    acc.update(True, 0.0)
    acc.update(True, -1.0)
    assert acc.hands_free_seconds == 0.0
