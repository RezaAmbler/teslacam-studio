#!/usr/bin/env python3
"""tesla_fsd_metrics.py — pure-Python derivation of FSD showcase metrics.

Foundation for the four deferred FSD-overlay ideas (friction circle, hands-
free/corner-count scoreboard, note-highway ribbon, pace-notes) -- see
CLAUDE.md and the branch's design notes for the full context.

Deliberately has ZERO dependency on gopro_overlay (or anything else outside
the stdlib): tests/ runs under the system Python (no gopro-overlay installed
there), so the pure decode/derivation logic lives here as plain functions and
classes over simple values, importable and testable on their own. The driver
script `tesla_fsd_overlay.py` (which DOES import gopro_overlay, and therefore
only runs under ./.venv) calls into this module from inside its
FrameMeta.process() callbacks, unwrapping gopro-overlay's pint Quantity
objects to plain floats first.

Axis mapping (confirmed against real footage -- see CLAUDE.md "IMU axis
mapping resolved with real data"):
    linear_acceleration_mps2_x  -> lateral_g       (steering-angle correlated)
    linear_acceleration_mps2_y  -> longitudinal_g  (speed-derivative correlated)
    linear_acceleration_mps2_z  -> vertical (not decoded here -- not needed by
                                   any of the four ideas' v1)

These raw m/s^2 values reach this module already unwrapped from the GPX
repurposed tags <cad>/<power>/<hr> that tesla_gps.write_gpx() emits (see the
comment there) and gopro-overlay's gpx.py parses back out under gopro-overlay's
own field names cad/power/hr.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

# Standard gravity, m/s^2 -- the conversion factor from Tesla's raw
# linear_acceleration_mps2_* to "g" units used throughout the four showcase
# ideas (a friction circle, G thresholds, etc. are all far more legible in g
# than in m/s^2).
STANDARD_GRAVITY_MPS2 = 9.80665

# Tesla's SEI autopilot_state: only the value 1 (engaged) has been confirmed
# against real footage so far -- CLAUDE.md notes the whole 17.5km verification
# drive stayed engaged (state == 1) the entire time, so no other value's
# meaning is known. Treating anything other than exactly 1 as "not engaged" is
# the conservative choice: an unrecognized state code counting as hands-free
# would silently overstate the scoreboard's headline number.
AUTOPILOT_ENGAGED_STATE = 1


class FsdFields(NamedTuple):
    """Decoded per-frame FSD telemetry, ready for a widget to read."""
    lateral_g: Optional[float]
    longitudinal_g: Optional[float]
    autopilot_engaged: bool


def decode_fsd_fields(cad: Optional[float], power: Optional[float],
                       hr: Optional[float]) -> FsdFields:
    """Decode the repurposed GPX tag values (already unwrapped to plain
    floats/None by the caller) back into named, documented FSD fields.

    `cad` -> linear_acceleration_mps2_x -> lateral_g (divided by standard
             gravity to convert m/s^2 -> g)
    `power` -> linear_acceleration_mps2_y -> longitudinal_g (same conversion)
    `hr` -> autopilot_state -> autopilot_engaged (True only for the one
            confirmed "engaged" state code; see AUTOPILOT_ENGAGED_STATE)

    Missing (None) inputs pass through as None/False rather than raising --
    a GPX point that predates the repurposed-tag change, or a frame tesla_gps
    never observed a full SEI sample for, should degrade gracefully rather
    than crash the whole render.
    """
    lateral_g = cad / STANDARD_GRAVITY_MPS2 if cad is not None else None
    longitudinal_g = power / STANDARD_GRAVITY_MPS2 if power is not None else None
    autopilot_engaged = hr is not None and int(round(hr)) == AUTOPILOT_ENGAGED_STATE
    return FsdFields(lateral_g=lateral_g, longitudinal_g=longitudinal_g,
                     autopilot_engaged=autopilot_engaged)


class CornerCounter:
    """Hysteresis threshold-crossing detector on |lateral_g|, plus a running
    peak.

    Enter/exit thresholds are engineering judgment calls, not yet tuned
    against real footage (per CLAUDE.md, that tuning happens during
    real-footage verification):
      - ENTER_G = 0.15: comfortable FSD cornering on a mountain road sits
        roughly in the 0.1-0.4g lateral band; 0.15g is comfortably above GPS/
        IMU sensor noise (observed correlations were moderate-strength, i.e.
        noisy) while still catching a gentle real corner, not just road
        camber or a lane-keeping wiggle.
      - EXIT_G = 0.08, well below ENTER_G: without this gap, lateral_g
        oscillating narrowly around a single threshold during one real corner
        (sensor noise) would cross it multiple times and over-count; dropping
        back below a materially lower bar before re-arming means only a
        genuine return to (near-)straight driving ends a corner.
    A "corner" starts on a rising edge through ENTER_G (counted once) and can
    only end -- and re-arm for the next count -- once |lateral_g| falls below
    EXIT_G.
    """

    ENTER_G = 0.15
    EXIT_G = 0.08

    def __init__(self, enter_g: float = ENTER_G, exit_g: float = EXIT_G):
        if exit_g >= enter_g:
            raise ValueError("exit_g must be below enter_g for hysteresis to work")
        self.enter_g = enter_g
        self.exit_g = exit_g
        self.corner_count = 0
        self.peak_lateral_g = 0.0
        self._in_corner = False

    def update(self, lateral_g: Optional[float]) -> bool:
        """Feed one lateral_g sample (None-safe -- a missing sample neither
        starts nor ends a corner, and doesn't move the peak).

        Returns True on the exact sample that incremented corner_count (a
        rising edge), False otherwise -- callers that only want the running
        totals (corner_count / peak_lateral_g attributes) can ignore the
        return value.
        """
        if lateral_g is None:
            return False
        magnitude = abs(lateral_g)
        if magnitude > self.peak_lateral_g:
            self.peak_lateral_g = magnitude

        if not self._in_corner and magnitude >= self.enter_g:
            self._in_corner = True
            self.corner_count += 1
            return True
        if self._in_corner and magnitude < self.exit_g:
            self._in_corner = False
        return False


class HandsFreeAccumulator:
    """Running total of elapsed time spent with autopilot engaged.

    `update` is called once per processed frame (foundation branch: the ~0.1s
    cadence timeseries_to_framemeta() builds, one call per FrameMeta entry) --
    `dt_seconds` is the elapsed wall-clock time since the previous call, and
    `engaged` is this sample's autopilot_engaged state. Attributing the whole
    interval to the state observed at its end (rather than splitting it at an
    engagement transition) is a deliberate simplification: at a ~0.1s sample
    cadence the resulting error is bounded by a single tick, which is
    negligible against a hands-free timer meant to read in whole seconds.
    """

    def __init__(self):
        self.hands_free_seconds = 0.0

    def update(self, engaged: bool, dt_seconds: float) -> float:
        if engaged and dt_seconds > 0:
            self.hands_free_seconds += dt_seconds
        return self.hands_free_seconds
