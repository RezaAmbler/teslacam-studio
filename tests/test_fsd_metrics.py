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


# --- grade_corner (--fsd-pace-notes) -------------------------------------------

def test_grade_corner_floor_is_grade_6():
    # The floor equals CornerCounter.ENTER_G exactly -- see
    # PACE_NOTE_GRADE_THRESHOLDS' own comment for why that matters.
    assert m.grade_corner(0.15) == 6
    assert m.grade_corner(0.16) == 6


def test_grade_corner_ceiling_is_grade_1():
    assert m.grade_corner(0.45) == 1
    assert m.grade_corner(0.9) == 1


def test_grade_corner_middle_bands():
    assert m.grade_corner(0.20) == 5
    assert m.grade_corner(0.25) == 4
    assert m.grade_corner(0.32) == 3
    assert m.grade_corner(0.40) == 2


def test_grade_corner_uses_magnitude():
    assert m.grade_corner(-0.45) == 1
    assert m.grade_corner(-0.15) == 6


def test_grade_corner_below_every_threshold_clamps_to_6():
    # Defensive only -- segment_corners never creates a Corner this weak,
    # but grade_corner is independently callable/testable.
    assert m.grade_corner(0.0) == 6
    assert m.grade_corner(0.05) == 6


# --- segment_corners (--fsd-pace-notes) -----------------------------------------

def test_segment_corners_finds_a_single_clean_corner():
    values = [0.0, 0.05, 0.16, 0.30, 0.40, 0.20, 0.05, 0.0]
    corners = m.segment_corners(values)
    assert len(corners) == 1
    c = corners[0]
    assert c.start_index == 2  # first sample >= enter_g
    assert c.end_index == 6    # first sample after that < exit_g
    assert c.peak_g == pytest.approx(0.40)
    assert c.direction == 1
    assert c.observed_samples == 5  # indices 2..6 inclusive, no gaps
    assert c.gap_before is False


def test_segment_corners_negative_lateral_g_is_left_direction():
    values = [0.0, -0.16, -0.30, -0.05, 0.0]
    corners = m.segment_corners(values)
    assert len(corners) == 1
    assert corners[0].direction == -1


def test_segment_corners_direction_uses_sign_at_the_peak_sample():
    # The peak sample's own sign decides direction, not the entry sample's.
    values = [0.0, 0.16, -0.40, 0.10, 0.0]
    corners = m.segment_corners(values)
    assert len(corners) == 1
    assert corners[0].peak_g == pytest.approx(0.40)
    assert corners[0].direction == -1


def test_segment_corners_never_crossing_enter_g_finds_nothing():
    values = [0.0, 0.05, 0.10, 0.08, 0.05, 0.0]
    assert m.segment_corners(values) == []


def test_segment_corners_two_separate_corners_in_start_order():
    values = [0.0, 0.16, 0.20, 0.0, 0.0, 0.0, -0.16, -0.25, 0.0]
    corners = m.segment_corners(values)
    assert len(corners) == 2
    assert corners[0].start_index < corners[1].start_index
    assert corners[0].direction == 1
    assert corners[1].direction == -1


def test_segment_corners_none_mid_corner_pauses_without_ending_it():
    # A None mid-corner is a no-op (same as CornerCounter.update's own
    # None-safety) -- it must not end the corner or move the peak, and the
    # corner should still resolve correctly once real samples resume.
    values = [0.0, 0.16, 0.30, None, None, 0.25, 0.05, 0.0]
    corners = m.segment_corners(values)
    assert len(corners) == 1
    assert corners[0].peak_g == pytest.approx(0.30)
    assert corners[0].end_index == 6
    # observed_samples counts only the 4 REAL samples seen while in-corner
    # (indices 1,2,5,6) -- NOT the raw index span (end - start = 5).
    assert corners[0].observed_samples == 4


def test_segment_corners_open_at_end_of_array_is_dropped():
    # The video/timeline ends mid-corner -- never confirmed how it resolves,
    # so it must not be reported as a real, complete corner.
    values = [0.0, 0.16, 0.30, 0.25]
    assert m.segment_corners(values) == []


def test_segment_corners_open_at_end_of_array_is_dropped_even_with_a_trailing_gap():
    # Same as above, but the corner's final samples are unobserved (a
    # telemetry gap running right up to the end of the timeline) rather than
    # simply absent -- must still be dropped, not synthesized an end from
    # nothing.
    values = [0.0, 0.16, 0.30, None, None, None]
    assert m.segment_corners(values) == []


def test_segment_corners_min_samples_drops_short_blips():
    # A corner lasting fewer than min_samples REAL samples is dropped
    # entirely -- too short to be worth a rally call.
    values = [0.0, 0.16, 0.30, 0.05, 0.0]  # observed = 3 (indices 1,2,3)
    assert m.segment_corners(values, min_samples=5) == []
    assert len(m.segment_corners(values, min_samples=2)) == 1


def test_segment_corners_respects_custom_thresholds():
    values = [0.0, 0.25, 0.35, 0.20, 0.0]
    # Peak (0.35) never reaches a stricter enter_g -- no corner at all.
    assert m.segment_corners(values, enter_g=0.5, exit_g=0.15) == []
    # A looser enter_g/exit_g pair finds one.
    corners = m.segment_corners(values, enter_g=0.2, exit_g=0.1)
    assert len(corners) == 1


def test_segment_corners_empty_input_is_empty_output():
    assert m.segment_corners([]) == []


def test_segment_corners_all_none_is_empty_output():
    assert m.segment_corners([None, None, None]) == []


# --- segment_corners: gap-inflated duration must NOT fabricate a real corner
# (a real bug found by an independent review, with an executable repro: a
# single real sample either side of a long telemetry blackout was being
# reported as one long, confirmed corner because min_samples/observed
# duration were computed from raw index span, which keeps ticking through
# a gap the same as through real data).

def test_segment_corners_huge_gap_does_not_inflate_observed_duration():
    # ONE real sample, then a 10-second (100-sample) blackout, then ONE real
    # return-to-baseline sample. Raw index span (101) would clear even a
    # generous min_samples; observed_samples (2 real samples) must not.
    values = [0.2] + [None] * 100 + [0.05]
    assert m.segment_corners(values, min_samples=7) == []


def test_segment_corners_gap_does_not_defeat_min_samples_filter():
    # A single real sample either side of an 8-sample gap: raw index span is
    # 9 (>= min_samples=7, the real PACE_NOTES_MIN_CORNER_N), but only 2
    # samples were actually observed -- must still be dropped.
    values = [0.2] + [None] * 8 + [0.05]
    assert m.segment_corners(values, min_samples=7) == []


def test_segment_corners_observed_samples_excludes_internal_gap_samples():
    values = [0.2, 0.3] + [None] * 5 + [0.25, 0.05]
    corners = m.segment_corners(values, min_samples=1)
    assert len(corners) == 1
    assert corners[0].observed_samples == 4  # the 4 real samples, not 9


# --- segment_corners: gap_before (chain-suppression tracking) -----------------

def test_segment_corners_gap_before_false_with_no_preceding_gap():
    values = [0.0, 0.0, 0.16, 0.30, 0.05, 0.0]
    corners = m.segment_corners(values)
    assert corners[0].gap_before is False


def test_segment_corners_gap_before_true_when_a_gap_precedes_the_corner():
    values = [0.0, None, None, 0.16, 0.30, 0.05, 0.0]
    corners = m.segment_corners(values)
    assert len(corners) == 1
    assert corners[0].gap_before is True


def test_segment_corners_gap_before_only_reflects_the_IMMEDIATELY_preceding_stretch():
    # A gap before corner 1 does not "leak" into corner 2's own gap_before --
    # tracking resets once a corner (accepted or not) is exited.
    values = ([0.0, None, None, 0.16, 0.30, 0.05, 0.0]  # corner 1, gap before it
             + [0.0, 0.16, 0.30, 0.05, 0.0])            # corner 2, clean approach
    corners = m.segment_corners(values)
    assert len(corners) == 2
    assert corners[0].gap_before is True
    assert corners[1].gap_before is False


# --- build_pace_notes (--fsd-pace-notes) ----------------------------------------

def _corner(start=5, end=10, peak_g=0.30, direction=1, observed_samples=None,
           gap_before=False):
    """A Corner for hand-constructed build_pace_notes tests -- observed_samples
    defaults to the raw index span (end - start + 1) when not given, matching
    what segment_corners would produce for a gap-free corner."""
    if observed_samples is None:
        observed_samples = end - start + 1
    return m.Corner(start_index=start, end_index=end, peak_g=peak_g,
                    direction=direction, observed_samples=observed_samples,
                    gap_before=gap_before)


def test_build_pace_notes_one_corner_one_note():
    corners = [_corner(start=5, end=10, peak_g=0.30)]
    notes = m.build_pace_notes(corners, long_samples=100, chain_gap_samples=5)
    assert len(notes) == 1
    note = notes[0]
    assert note.corner == corners[0]
    assert note.grade == m.grade_corner(0.30)
    assert note.long is False
    assert note.chain_grade is None
    assert note.chain_direction is None


def test_build_pace_notes_long_corner_flagged():
    corners = [_corner(start=0, end=50, peak_g=0.30, observed_samples=45)]
    notes = m.build_pace_notes(corners, long_samples=40, chain_gap_samples=5)
    assert notes[0].long is True


def test_build_pace_notes_short_corner_not_flagged_long():
    corners = [_corner(start=0, end=30, peak_g=0.30, observed_samples=25)]
    notes = m.build_pace_notes(corners, long_samples=40, chain_gap_samples=5)
    assert notes[0].long is False


def test_build_pace_notes_long_uses_observed_samples_not_raw_span():
    # A real bug found by review: raw index span (100) would have cleared
    # long_samples=40, but only 5 samples were actually observed -- must NOT
    # be flagged long. This is the same gap-inflation issue as
    # test_segment_corners_huge_gap_does_not_inflate_observed_duration, one
    # layer up in build_pace_notes.
    corners = [_corner(start=0, end=100, peak_g=0.30, observed_samples=5)]
    notes = m.build_pace_notes(corners, long_samples=40, chain_gap_samples=5)
    assert notes[0].long is False


def test_build_pace_notes_chains_close_corners_and_suppresses_the_second():
    corners = [
        _corner(start=5, end=10, peak_g=0.40, direction=1),
        _corner(start=12, end=18, peak_g=0.20, direction=-1),  # gap=2
    ]
    notes = m.build_pace_notes(corners, long_samples=100, chain_gap_samples=5)
    assert len(notes) == 1  # the second corner did NOT get its own note
    note = notes[0]
    assert note.corner == corners[0]
    assert note.chain_grade == m.grade_corner(0.20)
    assert note.chain_direction == -1


def test_build_pace_notes_gap_too_wide_does_not_chain():
    corners = [
        _corner(start=5, end=10, peak_g=0.40, direction=1),
        _corner(start=30, end=36, peak_g=0.20, direction=-1),  # gap=20
    ]
    notes = m.build_pace_notes(corners, long_samples=100, chain_gap_samples=5)
    assert len(notes) == 2  # both stand alone
    assert notes[0].chain_grade is None
    assert notes[1].chain_grade is None


def test_build_pace_notes_does_not_chain_across_an_unobserved_gap():
    # A real bug found by review: two corners close enough in index terms to
    # chain, but with a genuine telemetry gap (gap_before=True) sitting in
    # the straight stretch between them -- we never actually saw that
    # stretch was straight, so chaining a confident "into" callout across it
    # would be fabricated linkage, not an observed one.
    corners = [
        _corner(start=5, end=10, peak_g=0.40, direction=1, gap_before=False),
        _corner(start=12, end=18, peak_g=0.20, direction=-1, gap_before=True),  # gap=2, but unobserved
    ]
    notes = m.build_pace_notes(corners, long_samples=100, chain_gap_samples=5)
    assert len(notes) == 2  # NOT chained -- both stand alone
    assert notes[0].chain_grade is None
    assert notes[1].corner == corners[1]


def test_build_pace_notes_chains_at_most_one_link():
    # Never "into X into Y" -- a chained corner's own potential chain into a
    # THIRD corner is NOT inherited by the first note: #2 is absorbed into
    # #1's chain (suppressing #2's own standalone note), but #3 -- despite
    # being close enough to #2 to have chained FROM it -- gets its own
    # separate, unchained standalone note, since #2 was already consumed.
    corners = [
        _corner(start=5, end=10, peak_g=0.40, direction=1),
        _corner(start=12, end=16, peak_g=0.20, direction=-1),  # chains to #1
        _corner(start=18, end=24, peak_g=0.25, direction=1),  # would chain to #2
    ]
    notes = m.build_pace_notes(corners, long_samples=100, chain_gap_samples=5)
    assert len(notes) == 2
    assert notes[0].corner == corners[0]
    assert notes[0].chain_grade == m.grade_corner(0.20)
    assert notes[0].chain_direction == -1
    assert notes[1].corner == corners[2]
    assert notes[1].chain_grade is None  # not "into X into Y"


def test_build_pace_notes_empty_input_is_empty_output():
    assert m.build_pace_notes([], long_samples=10, chain_gap_samples=5) == []


# --- pace_note_window / visible_window / active_pace_note / pace_note_alpha
# (--fsd-pace-notes) --------------------------------------------------------

def _note(start=20, end=25, peak_g=0.30, direction=1, **kw):
    corner = _corner(start=start, end=end, peak_g=peak_g, direction=direction)
    return m.PaceNote(corner=corner, grade=m.grade_corner(peak_g), long=False, **kw)


def test_pace_note_window_basic():
    note = _note(start=20)
    assert m.pace_note_window(note, lead_samples=5, hold_samples=3) == (15, 23)


def test_pace_note_window_clamps_lead_to_zero_near_drive_start():
    note = _note(start=3)
    start, end = m.pace_note_window(note, lead_samples=10, hold_samples=2)
    assert start == 0
    assert end == 5


# --- visible_window: truncation against a following note ----------------------

def test_visible_window_matches_natural_window_when_alone():
    notes = [_note(start=20)]
    assert m.visible_window(notes, 0, lead_samples=5, hold_samples=3) == (15, 23)


def test_visible_window_matches_natural_window_when_no_overlap_with_next():
    notes = [_note(start=20), _note(start=100)]
    # Natural window for notes[0] is (15, 23); notes[1]'s natural start (95)
    # is far past that, so no truncation should occur.
    assert m.visible_window(notes, 0, lead_samples=5, hold_samples=3) == (15, 23)


def test_visible_window_truncates_when_next_note_overlaps():
    notes = [_note(start=20), _note(start=22)]
    # notes[0]'s natural window is (15,23); notes[1]'s natural start is 17
    # (22-5) -- notes[0] must be truncated to end at 16 (17-1).
    assert m.visible_window(notes, 0, lead_samples=5, hold_samples=3) == (15, 16)
    # notes[1] (the last in the list) is never truncated.
    assert m.visible_window(notes, 1, lead_samples=5, hold_samples=3) == (17, 25)


def test_visible_window_truncation_never_goes_below_start():
    # A pathological near-zero gap: truncation clamps to a 1-sample window
    # rather than producing an invalid (end < start) range.
    notes = [_note(start=20), _note(start=20)]
    start, end = m.visible_window(notes, 0, lead_samples=5, hold_samples=3)
    assert end >= start


# --- active_pace_note --------------------------------------------------------

def test_active_pace_note_none_outside_any_window():
    notes = [_note(start=20)]
    assert m.active_pace_note(notes, index=5, lead_samples=5, hold_samples=3) is None
    assert m.active_pace_note(notes, index=30, lead_samples=5, hold_samples=3) is None


def test_active_pace_note_matches_within_window():
    notes = [_note(start=20)]
    assert m.active_pace_note(notes, index=15, lead_samples=5, hold_samples=3) is notes[0]
    assert m.active_pace_note(notes, index=20, lead_samples=5, hold_samples=3) is notes[0]
    assert m.active_pace_note(notes, index=23, lead_samples=5, hold_samples=3) is notes[0]


def test_active_pace_note_overlapping_windows_newer_note_wins():
    early = _note(start=20)
    later = _note(start=22)  # its lead window overlaps `early`'s hold window
    notes = [early, later]
    # notes[0] is truncated to (15,16) by visible_window (see above), so at
    # index 21 only `later` (window (17,25)) can possibly match.
    assert m.active_pace_note(notes, index=21, lead_samples=5, hold_samples=3) is later
    # And `early` is genuinely gone (not just outranked) once its truncated
    # window has passed -- confirms this isn't merely "later always wins
    # when both match", but that the earlier note's own window really ends.
    assert m.active_pace_note(notes, index=17, lead_samples=5, hold_samples=3) is later


def test_active_pace_note_earlier_note_still_shows_before_truncation_point():
    early = _note(start=20)
    later = _note(start=22)
    notes = [early, later]
    # Before `later`'s own window opens (17), `early` is still active.
    assert m.active_pace_note(notes, index=16, lead_samples=5, hold_samples=3) is early


# --- pace_note_alpha ----------------------------------------------------------

def test_pace_note_alpha_zero_outside_window():
    notes = [_note(start=20)]
    assert m.pace_note_alpha(notes, 0, index=5, lead_samples=5, hold_samples=3,
                             fade_in_samples=3, fade_out_samples=3) == 0.0


def test_pace_note_alpha_ramps_up_during_fade_in():
    notes = [_note(start=20)]
    start, _end = m.pace_note_window(notes[0], lead_samples=6, hold_samples=3)
    assert m.pace_note_alpha(notes, 0, index=start, lead_samples=6, hold_samples=3,
                             fade_in_samples=3, fade_out_samples=3) == pytest.approx(0.0)
    assert m.pace_note_alpha(notes, 0, index=start + 1, lead_samples=6, hold_samples=3,
                             fade_in_samples=3, fade_out_samples=3) == pytest.approx(1 / 3)
    assert m.pace_note_alpha(notes, 0, index=start + 3, lead_samples=6, hold_samples=3,
                             fade_in_samples=3, fade_out_samples=3) == pytest.approx(1.0)


def test_pace_note_alpha_holds_at_full_opacity_mid_window():
    notes = [_note(start=20)]
    assert m.pace_note_alpha(notes, 0, index=20, lead_samples=6, hold_samples=3,
                             fade_in_samples=3, fade_out_samples=3) == pytest.approx(1.0)


def test_pace_note_alpha_ramps_down_during_fade_out():
    notes = [_note(start=20)]
    _start, end = m.pace_note_window(notes[0], lead_samples=6, hold_samples=6)
    assert m.pace_note_alpha(notes, 0, index=end, lead_samples=6, hold_samples=6,
                             fade_in_samples=3, fade_out_samples=3) == pytest.approx(0.0)
    assert m.pace_note_alpha(notes, 0, index=end - 1, lead_samples=6, hold_samples=6,
                             fade_in_samples=3, fade_out_samples=3) == pytest.approx(1 / 3)
    assert m.pace_note_alpha(notes, 0, index=end - 3, lead_samples=6, hold_samples=6,
                             fade_in_samples=3, fade_out_samples=3) == pytest.approx(1.0)


def test_pace_note_alpha_short_window_caps_at_the_smaller_ramp():
    # A window shorter than fade_in + fade_out (a corner right at the start
    # of the drive, clamped -- see pace_note_window's own test) must never
    # exceed 1.0 or go negative -- the min() of both ramps handles this.
    notes = [_note(start=2)]  # lead clamps to 0, window is short
    start, end = m.pace_note_window(notes[0], lead_samples=6, hold_samples=1)
    for index in range(start, end + 1):
        alpha = m.pace_note_alpha(notes, 0, index, lead_samples=6, hold_samples=1,
                                  fade_in_samples=3, fade_out_samples=3)
        assert 0.0 <= alpha <= 1.0


def test_pace_note_alpha_earlier_note_fades_out_smoothly_before_a_later_note_opens():
    # A real bug found by review: without truncation, `early` would hold at
    # full alpha=1.0 right up to the instant `later`'s window opens, then
    # vanish in one frame while `later` pops in already fading from zero --
    # a hard cut, not "a pure alpha fade" as this branch's own docs claim.
    # With visible_window's truncation, `early`'s alpha must ramp smoothly
    # DOWN to 0 by the time `later` can possibly become active.
    early = _note(start=20)
    later = _note(start=22)
    notes = [early, later]
    lead, hold, fade_in, fade_out = 5, 3, 3, 3
    # early's truncated window is (15, 16) (see test_visible_window_
    # truncates_when_next_note_overlaps above).
    alpha_at_16 = m.pace_note_alpha(notes, 0, 16, lead, hold, fade_in, fade_out)
    alpha_at_17 = m.pace_note_alpha(notes, 0, 17, lead, hold, fade_in, fade_out)
    assert alpha_at_16 == pytest.approx(0.0)  # smoothly reaches 0, not cut off high
    assert alpha_at_17 == 0.0  # and stays gone (outside its truncated window)
    # `later` (index 1) begins fading in right where `early` leaves off.
    later_start, _ = m.pace_note_window(later, lead, hold)
    assert later_start == 17
    alpha_later_at_17 = m.pace_note_alpha(notes, 1, 17, lead, hold, fade_in, fade_out)
    assert alpha_later_at_17 == pytest.approx(0.0)  # later's own fade-in just starting


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
