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

import collections
import math
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
    autopilot_engaged: Optional[bool]


def decode_fsd_fields(cad: Optional[float], power: Optional[float],
                       hr: Optional[float]) -> FsdFields:
    """Decode the repurposed GPX tag values (already unwrapped to plain
    floats/None by the caller) back into named, documented FSD fields.

    `cad` -> linear_acceleration_mps2_x -> lateral_g (divided by standard
             gravity to convert m/s^2 -> g)
    `power` -> linear_acceleration_mps2_y -> longitudinal_g (same conversion)
    `hr` -> autopilot_state -> autopilot_engaged (True only for the one
            confirmed "engaged" state code; see AUTOPILOT_ENGAGED_STATE)

    Missing (None) inputs pass through as None -- a GPX point that predates
    the repurposed-tag change, or a frame tesla_gps never observed a full SEI
    sample for, should degrade gracefully rather than crash the whole render.

    autopilot_engaged is deliberately Optional[bool], NOT a plain bool that
    collapses "unknown" into "not engaged": retime_samples nulls this field
    across its edge-hold pads (a real telemetry gap, e.g. SEI ending before
    the video does), and a caller that can't tell "we don't know" from "we
    know it's disengaged" will misread a data gap as a real disengagement --
    confirmed as a real bug (a phantom takeover_count) when the caller was a
    plain bool here. TakeoverCounter/CornerCounter must treat this None as
    "skip this sample," not as False.
    """
    lateral_g = cad / STANDARD_GRAVITY_MPS2 if cad is not None else None
    longitudinal_g = power / STANDARD_GRAVITY_MPS2 if power is not None else None
    autopilot_engaged = (None if hr is None
                         else int(round(hr)) == AUTOPILOT_ENGAGED_STATE)
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
        # The just-completed corner's peak |lateral_g|, latched the instant a
        # corner exits (see update()) -- feeds the friction-circle widget's
        # "peak this corner" readout (--fsd-friction-circle). Stays 0.0 until
        # the first corner actually completes (a corner still in progress
        # hasn't "completed" yet, so it doesn't move this).
        self.last_corner_peak_g = 0.0
        self._in_corner = False
        # The IN-PROGRESS corner's running peak -- reset to the entry
        # magnitude when a corner starts, updated while _in_corner stays
        # True, and copied into last_corner_peak_g the instant it exits.
        # Kept here (not a second class) so there's exactly one place the
        # "am I in a corner right now" hysteresis lives.
        self._current_corner_peak = 0.0

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
            self._current_corner_peak = magnitude
            self.corner_count += 1
            return True
        if self._in_corner:
            if magnitude > self._current_corner_peak:
                self._current_corner_peak = magnitude
            if magnitude < self.exit_g:
                self._in_corner = False
                self.last_corner_peak_g = self._current_corner_peak
        return False

    @property
    def display_peak_g(self) -> float:
        """What a "peak this corner" readout should actually show right now
        -- NOT the same as last_corner_peak_g alone. That field only latches
        on corner EXIT, so a widget reading it directly shows the PREVIOUS
        corner's peak for the entire duration of the current one -- exactly
        the moment a viewer is most likely to be looking at a G-meter.
        Flagged by an independent review as a real, if minor, correctness
        gap (the label says "this corner", the data said "the last one").

        While a corner is in progress, this returns the live, still-growing
        _current_corner_peak; once it exits, last_corner_peak_g (now frozen
        at that corner's final peak) takes over until the next corner starts
        raising _current_corner_peak again. Continuous and always describes
        "the most relevant corner peak right now" -- growing during a
        corner, holding steady between corners."""
        return self._current_corner_peak if self._in_corner else self.last_corner_peak_g


class GTrailBuffer:
    """Fixed-length trail of recent (lateral_g, longitudinal_g) samples, for
    the friction-circle G-meter (--fsd-friction-circle) to render as a fading
    polyline/dot-chain. A plain `collections.deque(maxlen=N)` under the hood
    -- oldest points fall off the end automatically as new ones are appended,
    no manual eviction needed.

    `N` (the "3-second" trail Fable's brainstorm asked for) is a decision for
    the WIDGET to make, not this class -- it depends on the render loop's
    sample cadence (tesla_fsd_overlay.py steps at ~0.1s, so N=30 there), so
    this class just takes whatever `maxlen` its caller hands it.

    `.append` is a no-op when either axis is None -- mirrors the takeover-
    counter lesson (see TakeoverCounter's docstring): a real telemetry gap
    must leave a visible pause/break in the trail, never fabricate a point at
    the origin (which would read as "the car was at rest, dead center") or
    silently carry a stale value forward (which would read as "the car held
    that exact G" through a gap it didn't actually measure).
    """

    def __init__(self, maxlen: int):
        self._points = collections.deque(maxlen=maxlen)

    def append(self, lateral_g: Optional[float], longitudinal_g: Optional[float]) -> None:
        if lateral_g is None or longitudinal_g is None:
            return
        self._points.append((lateral_g, longitudinal_g))

    @property
    def points(self):
        """Oldest-to-newest list of (lateral_g, longitudinal_g) pairs -- a
        plain list snapshot (not the live deque) so a caller can't mutate
        this buffer's internal state through it."""
        return list(self._points)


def g_to_offset(lateral_g: float, longitudinal_g: float, max_g: float) -> tuple:
    """Map a (lateral_g, longitudinal_g) sample to a normalized (dx, dy)
    offset for the friction-circle G-meter (--fsd-friction-circle), where
    +dx = right and +dy = accelerating (forward). Both are clamped to
    magnitude 1.0 -- scaled down together along the same direction, not
    just cropped -- when the input magnitude exceeds max_g, so an unusually
    large G spike still lands ON the rim rather than outside it entirely.

    Moved here (not left inline in tesla_fsd_overlay.py's widget code) after
    an independent review flagged it: this is pure math with zero
    gopro_overlay dependency, but it had been living in the venv-only driver
    script anyway, where tests/ (system Python, no gopro-overlay installed)
    can't reach it to guard against a regression. That matters beyond just
    this widget -- the two still-deferred FSD-overlay ideas (a note-highway
    ribbon, rally pace-notes) will need this EXACT SAME sign convention
    (e.g. pace-notes calling a corner "Right 3" vs "Left 3"), so if either
    one re-derives the sign independently instead of calling this function,
    a silent contradiction between the two overlays is exactly the bug class
    this project has now hit more than once. One tested function, not two
    (or three, or four) independently-guessed ones.

    BOTH signs are confirmed against real telemetry, not eyeballed off a
    video frame (a single frame turned out to be an unreliable check on a
    continuously winding road -- see CLAUDE.md's "Axis sign convention"
    design note for the full method and the numbers):
      - lateral: corr(d(heading_deg)/dt [+ = turning right], lateral_g)
        = +0.68 over 28,000 real points -- turning right already gives
        positive lateral_g, so dx is lateral_g directly, no sign flip.
      - longitudinal: corr(d(speed)/dt [+ = accelerating], raw
        linear_acceleration_mps2_y) = -0.36 over the same data --
        accelerating gives NEGATIVE longitudinal_g. Since +dy is defined
        here as "accelerating", dy is the NEGATION of longitudinal_g (a
        real, confirmed sign flip -- this is not the same relationship as
        the lateral axis, and must not be "simplified" to match it).

    Returns a normalized offset, not pixel coordinates -- screen-space
    conventions (e.g. that +y is DOWN on screen, the opposite of this
    function's +dy = accelerating = "up" in the physical/intuitive sense)
    are a UI-framework detail that belongs in the widget, not here.
    """
    magnitude = math.hypot(lateral_g, longitudinal_g)
    if magnitude > max_g and magnitude > 0:
        scale = max_g / magnitude
        lateral_g *= scale
        longitudinal_g *= scale
    dx = lateral_g / max_g
    dy = -longitudinal_g / max_g
    return dx, dy


class TakeoverCounter:
    """Counts autopilot disengagements: a CONFIRMED engaged -> CONFIRMED
    not-engaged falling edge, once per edge.

    Unlike CornerCounter, no hysteresis is needed here -- `autopilot_engaged`
    (tesla_fsd_metrics.decode_fsd_fields) is a clean boolean derived from an
    SEI state code, not a noisy continuous sensor value that could chatter
    across a single threshold. It IS, however, Optional -- None means "we
    don't know," not "disengaged" (see decode_fsd_fields' docstring). This
    must be None-safe the same way CornerCounter.update already is: a real
    bug (a phantom takeover_count) was confirmed end-to-end when this method
    treated a None (e.g. from retime_samples' edge-hold pad, nulled because
    a telemetry gap is NOT a confirmed disengagement) the same as False --
    every drive whose SEI coverage ends before the video does would then
    report a fabricated takeover the instant the pad's None samples begin.

    KNOWN LIMITATION (see CLAUDE.md): every real drive probed so far
    (17.5km) stayed engaged (autopilot_state == 1) the entire time -- zero
    real disengagements observed. This class is implemented and
    unit-testable against synthetic data, but has NOT been verified against
    a real disengagement event. "Looks right in a synthetic test" is not the
    same claim as "confirmed against reality."
    """

    def __init__(self):
        self.takeover_count = 0
        self._engaged = False

    def update(self, engaged: Optional[bool]) -> bool:
        """Feed one autopilot_engaged sample (None-safe -- an unknown sample
        neither counts as a takeover nor changes the tracked state, so a
        stretch of missing telemetry can never itself look like a
        disengagement).

        Returns True on the exact sample that incremented takeover_count (a
        confirmed engaged -> confirmed not-engaged falling edge), False
        otherwise -- staying in either state, a rising (re-engage) edge, or
        an unknown (None) sample, doesn't count."""
        if engaged is None:
            return False
        counted = self._engaged and not engaged
        if counted:
            self.takeover_count += 1
        self._engaged = engaged
        return counted


def format_hms(seconds: float) -> str:
    """Format a non-negative seconds count as "M:SS" (e.g. "14:32") or,
    once it reaches an hour, "H:MM:SS" (e.g. "1:04:32") -- the legible-on-
    screen counterpart to HandsFreeAccumulator's raw hands_free_seconds,
    used by the scoreboard widget's readout."""
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


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
