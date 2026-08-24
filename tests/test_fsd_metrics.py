"""Tests for tesla_fsd_metrics.py: the pure-Python axis-mapping decode,
corner-count hysteresis detector, and hands-free-seconds accumulator behind
the FSD-overlay foundation branch (tesla_fsd_overlay.py). Synthetic data
only, no gopro_overlay import -- this module is deliberately importable and
testable under the system Python (see its own docstring)."""
import math

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
    # (CLAUDE.md) -- any other OBSERVED value is a confirmed False, but a
    # MISSING value (hr=None) must decode to None, not False -- collapsing
    # "we don't know" into "confirmed disengaged" was a real bug (a phantom
    # TakeoverCounter takeover across a telemetry gap), see CLAUDE.md and
    # TakeoverCounter's docstring.
    assert m.decode_fsd_fields(cad=0.0, power=0.0, hr=0).autopilot_engaged is False
    assert m.decode_fsd_fields(cad=0.0, power=0.0, hr=2).autopilot_engaged is False
    assert m.decode_fsd_fields(cad=0.0, power=0.0, hr=None).autopilot_engaged is None
    assert m.decode_fsd_fields(cad=0.0, power=0.0, hr=1).autopilot_engaged is True


def test_decode_fsd_fields_missing_inputs_pass_through_as_none():
    fields = m.decode_fsd_fields(cad=None, power=None, hr=None)
    assert fields.lateral_g is None
    assert fields.longitudinal_g is None
    assert fields.autopilot_engaged is None


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


# --- CornerCounter.last_corner_peak_g (--fsd-friction-circle) -----------------

def test_corner_counter_last_corner_peak_g_starts_at_zero():
    counter = m.CornerCounter()
    assert counter.last_corner_peak_g == 0.0


def test_corner_counter_last_corner_peak_g_stays_zero_mid_corner():
    counter = m.CornerCounter()
    # Still IN the corner (never dropped below exit_g) -- nothing has
    # "completed" yet, so last_corner_peak_g must not move.
    for v in [0.0, 0.16, 0.30, 0.20]:
        counter.update(v)
    assert counter.last_corner_peak_g == 0.0


def test_corner_counter_last_corner_peak_g_latches_on_exit():
    counter = m.CornerCounter()
    # Peaks at 0.30 mid-corner, then exits -- last_corner_peak_g should latch
    # to 0.30 (the corner's own peak), NOT the overall peak_lateral_g and NOT
    # the exit sample's own (much smaller) magnitude.
    sequence = [0.0, 0.16, 0.30, 0.20, 0.05]  # last value drops below EXIT_G
    for v in sequence:
        counter.update(v)
    assert counter.last_corner_peak_g == pytest.approx(0.30)
    assert counter.peak_lateral_g == pytest.approx(0.30)


def test_corner_counter_last_corner_peak_g_resets_for_next_corner():
    counter = m.CornerCounter()
    # First corner peaks at 0.20 and exits.
    for v in [0.0, 0.16, 0.20, 0.05]:
        counter.update(v)
    assert counter.last_corner_peak_g == pytest.approx(0.20)
    # Second corner, weaker (peaks at 0.18) -- must overwrite, not keep the
    # first corner's higher peak, and must not move until IT exits either.
    for v in [0.16, 0.18]:
        counter.update(v)
    assert counter.last_corner_peak_g == pytest.approx(0.20)  # unchanged mid-corner
    counter.update(0.05)  # now it exits
    assert counter.last_corner_peak_g == pytest.approx(0.18)


def test_corner_counter_last_corner_peak_g_uses_magnitude():
    counter = m.CornerCounter()
    for v in [0.0, -0.16, -0.30, -0.20, -0.05]:
        counter.update(v)
    assert counter.last_corner_peak_g == pytest.approx(0.30)


def test_corner_counter_display_peak_g_starts_at_zero():
    counter = m.CornerCounter()
    assert counter.display_peak_g == 0.0


def test_corner_counter_display_peak_g_grows_live_mid_corner():
    # Unlike last_corner_peak_g (which stays 0.0 mid-corner -- see the test
    # above), display_peak_g is what a "peak this corner" readout should
    # actually show: the live, still-growing peak of the IN-PROGRESS corner.
    counter = m.CornerCounter()
    counter.update(0.16)  # corner starts
    assert counter.display_peak_g == pytest.approx(0.16)
    counter.update(0.25)  # grows
    assert counter.display_peak_g == pytest.approx(0.25)
    counter.update(0.20)  # a dip that's still above exit_g -- must NOT shrink
    assert counter.display_peak_g == pytest.approx(0.25)


def test_corner_counter_display_peak_g_holds_after_exit_until_next_corner():
    counter = m.CornerCounter()
    for v in [0.16, 0.30, 0.05]:  # a corner that peaks at 0.30, then exits
        counter.update(v)
    assert counter.display_peak_g == pytest.approx(0.30)
    counter.update(0.05)  # still between corners -- holds the finished peak
    assert counter.display_peak_g == pytest.approx(0.30)
    counter.update(0.16)  # next corner starts, live tracking resumes
    assert counter.display_peak_g == pytest.approx(0.16)
    counter.update(0.10)  # a dip mid-corner that's still above exit_g
    assert counter.display_peak_g == pytest.approx(0.16)  # not shrunk


def test_corner_counter_existing_behavior_unaffected_by_extension():
    # A rerun of the pre-existing corner_count/peak_lateral_g/edge-return
    # contract, to confirm the last_corner_peak_g extension didn't disturb it.
    counter = m.CornerCounter()
    sequence = [0.0, 0.05, 0.10, 0.16, 0.20, 0.16, 0.10, 0.05, 0.0]
    edges = [counter.update(v) for v in sequence]
    assert counter.corner_count == 1
    assert sum(edges) == 1
    assert edges.index(True) == sequence.index(0.16)
    assert counter.peak_lateral_g == pytest.approx(0.20)


# --- GTrailBuffer (--fsd-friction-circle) --------------------------------------

def test_gtrail_buffer_starts_empty():
    trail = m.GTrailBuffer(maxlen=3)
    assert trail.points == []


def test_gtrail_buffer_appends_in_order_oldest_to_newest():
    trail = m.GTrailBuffer(maxlen=5)
    trail.append(0.1, 0.2)
    trail.append(0.3, -0.1)
    assert trail.points == [(0.1, 0.2), (0.3, -0.1)]


def test_gtrail_buffer_evicts_oldest_first_at_maxlen():
    trail = m.GTrailBuffer(maxlen=3)
    for i in range(5):
        trail.append(float(i), 0.0)
    # Only the last 3 appended survive, oldest-to-newest.
    assert trail.points == [(2.0, 0.0), (3.0, 0.0), (4.0, 0.0)]


def test_gtrail_buffer_none_lateral_is_a_noop():
    trail = m.GTrailBuffer(maxlen=3)
    trail.append(0.1, 0.2)
    trail.append(None, 0.5)  # missing lateral -- must not grow or corrupt
    assert trail.points == [(0.1, 0.2)]


def test_gtrail_buffer_none_longitudinal_is_a_noop():
    trail = m.GTrailBuffer(maxlen=3)
    trail.append(0.1, 0.2)
    trail.append(0.4, None)  # missing longitudinal -- must not grow or corrupt
    assert trail.points == [(0.1, 0.2)]


def test_gtrail_buffer_both_none_is_a_noop():
    trail = m.GTrailBuffer(maxlen=3)
    trail.append(None, None)
    assert trail.points == []


def test_gtrail_buffer_points_is_a_snapshot_not_the_live_buffer():
    trail = m.GTrailBuffer(maxlen=3)
    trail.append(0.1, 0.2)
    snapshot = trail.points
    trail.append(0.3, 0.4)
    assert snapshot == [(0.1, 0.2)]  # unaffected by the later append


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


# --- g_to_offset (--fsd-friction-circle) ---------------------------------------
# The friction circle's sign convention, confirmed against real telemetry (see
# g_to_offset's own docstring and CLAUDE.md's "Axis sign convention" design
# note for the correlation values) -- pinned here so a regression on either
# axis is caught, and so the two still-deferred FSD-overlay ideas that will
# need this SAME convention have one tested function to call rather than
# re-deriving it (an independent review flagged this exact risk).

def test_g_to_offset_positive_lateral_g_is_positive_dx():
    # Turning right correlates with positive lateral_g (r=+0.68 vs real
    # heading-rate data) -- dx is lateral_g directly, no sign flip.
    dx, dy = m.g_to_offset(lateral_g=0.3, longitudinal_g=0.0, max_g=0.6)
    assert dx == pytest.approx(0.5)
    assert dy == pytest.approx(0.0)


def test_g_to_offset_negative_lateral_g_is_negative_dx():
    dx, dy = m.g_to_offset(lateral_g=-0.3, longitudinal_g=0.0, max_g=0.6)
    assert dx == pytest.approx(-0.5)


def test_g_to_offset_accelerating_is_positive_dy():
    # Accelerating correlates with NEGATIVE raw longitudinal_g (r=-0.36 vs
    # real speed-derivative data) -- dy is defined as "+dy = accelerating",
    # so this is a genuine sign FLIP relative to lateral, not a passthrough.
    dx, dy = m.g_to_offset(lateral_g=0.0, longitudinal_g=-0.3, max_g=0.6)
    assert dy == pytest.approx(0.5)


def test_g_to_offset_braking_is_negative_dy():
    dx, dy = m.g_to_offset(lateral_g=0.0, longitudinal_g=0.3, max_g=0.6)
    assert dy == pytest.approx(-0.5)


def test_g_to_offset_zero_input_is_origin():
    dx, dy = m.g_to_offset(lateral_g=0.0, longitudinal_g=0.0, max_g=0.6)
    assert dx == pytest.approx(0.0)
    assert dy == pytest.approx(0.0)


def test_g_to_offset_within_max_g_is_unclamped():
    dx, dy = m.g_to_offset(lateral_g=0.3, longitudinal_g=-0.3, max_g=0.6)
    assert dx == pytest.approx(0.5)
    assert dy == pytest.approx(0.5)


def test_g_to_offset_exceeding_max_g_clamps_to_the_rim_preserving_direction():
    # A spike well past max_g must land AT magnitude 1.0 (the rim), scaled
    # down along the same direction -- not cropped to some arbitrary point.
    dx, dy = m.g_to_offset(lateral_g=1.2, longitudinal_g=0.0, max_g=0.6)
    assert dx == pytest.approx(1.0)
    assert dy == pytest.approx(0.0)
    assert math.hypot(dx, dy) == pytest.approx(1.0)


def test_g_to_offset_diagonal_clamp_preserves_ratio_between_axes():
    # lateral_g=0.6 (right) and longitudinal_g=-0.6 (accelerating) are equal
    # magnitude, and dy is accelerating's NEGATION -- so both dx and dy come
    # out equal (not opposite): right-and-accelerating clamps to the
    # upper-right of the rim, not a mixed sign.
    dx, dy = m.g_to_offset(lateral_g=0.6, longitudinal_g=-0.6, max_g=0.3)
    assert math.hypot(dx, dy) == pytest.approx(1.0)
    assert dx == pytest.approx(dy)
    assert dx > 0 and dy > 0


# --- ribbon_window (--fsd-note-highway) ---------------------------------------
# The pure windowing math behind the note-highway ribbon: a fixed-length
# slice of `values` centered on `index`, None-padded (never wrapped, clamped,
# or fabricated) wherever the window runs off either end of the array.

def test_ribbon_window_mid_array_returns_the_expected_slice():
    values = list(range(20))
    window = m.ribbon_window(values, index=10, past_n=3, future_n=2)
    assert window == [7, 8, 9, 10, 11, 12]


def test_ribbon_window_length_is_always_past_plus_one_plus_future():
    values = list(range(20))
    for index in (0, 1, 5, 10, 19, 25, -5):
        window = m.ribbon_window(values, index=index, past_n=4, future_n=6)
        assert len(window) == 4 + 1 + 6


def test_ribbon_window_start_of_array_left_pads_with_none():
    values = list(range(10))
    window = m.ribbon_window(values, index=1, past_n=3, future_n=2)
    # index=1: positions -2, -1 are before the array start -> None; 0..3 real.
    assert window == [None, None, 0, 1, 2, 3]


def test_ribbon_window_end_of_array_right_pads_with_none():
    values = list(range(10))
    window = m.ribbon_window(values, index=8, past_n=2, future_n=3)
    # index=8: positions 6,7,8,9 real; 10,11 past the end -> None.
    assert window == [6, 7, 8, 9, None, None]


def test_ribbon_window_entirely_before_the_array_is_all_none():
    values = list(range(5))
    window = m.ribbon_window(values, index=-10, past_n=1, future_n=1)
    assert window == [None, None, None]


def test_ribbon_window_entirely_after_the_array_is_all_none():
    values = list(range(5))
    window = m.ribbon_window(values, index=100, past_n=1, future_n=1)
    assert window == [None, None, None]


def test_ribbon_window_zero_past_and_future_is_a_single_point_window():
    values = list(range(20))
    window = m.ribbon_window(values, index=10, past_n=0, future_n=0)
    assert window == [10]


def test_ribbon_window_zero_past_and_future_out_of_range_is_none():
    values = list(range(5))
    window = m.ribbon_window(values, index=99, past_n=0, future_n=0)
    assert window == [None]


def test_ribbon_window_existing_none_values_pass_through_untouched():
    # A genuine mid-drive telemetry gap already in `values` must survive
    # untouched -- ribbon_window only ever ADDS padding at the two ends, it
    # never interpolates or drops a real (even if None) entry.
    values = [0.1, 0.2, None, 0.4, 0.5]
    window = m.ribbon_window(values, index=2, past_n=2, future_n=2)
    assert window == [0.1, 0.2, None, 0.4, 0.5]


def test_ribbon_window_none_value_combined_with_edge_padding():
    values = [None, 0.2, 0.3]
    window = m.ribbon_window(values, index=0, past_n=2, future_n=1)
    # positions -2, -1 are before the array start -> None (padding);
    # position 0 is a REAL None (a genuine gap); 1 is real data.
    assert window == [None, None, None, 0.2]


# --- TakeoverCounter ----------------------------------------------------------

def test_takeover_counter_starts_at_zero():
    counter = m.TakeoverCounter()
    assert counter.takeover_count == 0


def test_takeover_counter_engaged_to_disengaged_counts_once():
    counter = m.TakeoverCounter()
    edges = [counter.update(v) for v in [True, False]]
    assert counter.takeover_count == 1
    assert edges == [False, True]  # fires exactly on the falling edge


def test_takeover_counter_disengaged_to_engaged_does_not_count():
    counter = m.TakeoverCounter()
    edges = [counter.update(v) for v in [False, True]]
    assert counter.takeover_count == 0
    assert edges == [False, False]


def test_takeover_counter_staying_engaged_does_not_count():
    counter = m.TakeoverCounter()
    for v in [True, True, True, True]:
        counter.update(v)
    assert counter.takeover_count == 0


def test_takeover_counter_staying_disengaged_does_not_count():
    counter = m.TakeoverCounter()
    for v in [False, False, False]:
        counter.update(v)
    assert counter.takeover_count == 0


def test_takeover_counter_multiple_disengagements_count_each():
    counter = m.TakeoverCounter()
    # engaged -> takeover -> re-engage -> takeover again
    sequence = [True, False, False, True, True, False]
    for v in sequence:
        counter.update(v)
    assert counter.takeover_count == 2


def test_takeover_counter_first_sample_disengaged_does_not_count():
    # No prior state to fall from -- starting disengaged isn't a takeover.
    counter = m.TakeoverCounter()
    counter.update(False)
    assert counter.takeover_count == 0


def test_takeover_counter_none_samples_do_not_count_or_change_state():
    # A confirmed regression test: an unknown (None) sample -- e.g.
    # retime_samples' edge-hold pad, nulled because a telemetry gap is not a
    # confirmed disengagement -- must never itself look like a takeover, and
    # must not disturb the tracked engaged/disengaged state either. Before
    # the fix, engaged->None was treated as engaged->False and counted.
    counter = m.TakeoverCounter()
    edges = [counter.update(v) for v in [True, None, None, True]]
    assert counter.takeover_count == 0
    assert edges == [False, False, False, False]


def test_takeover_counter_none_then_disengaged_still_counts_once():
    # A gap followed by a REAL, confirmed disengagement must still count --
    # None-safety must not suppress genuine takeovers, only fabricated ones.
    counter = m.TakeoverCounter()
    edges = [counter.update(v) for v in [True, None, False]]
    assert counter.takeover_count == 1
    assert edges == [False, False, True]


# --- format_hms -----------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0, "0:00"),
    (5, "0:05"),
    (59, "0:59"),
    (60, "1:00"),
    (872, "14:32"),
    (3599, "59:59"),
    (3600, "1:00:00"),
    (3872, "1:04:32"),
    (4200, "1:10:00"),
])
def test_format_hms(seconds, expected):
    assert m.format_hms(seconds) == expected


def test_format_hms_rounds_fractional_seconds():
    assert m.format_hms(89.6) == "1:30"


def test_format_hms_negative_clamps_to_zero():
    assert m.format_hms(-5) == "0:00"
