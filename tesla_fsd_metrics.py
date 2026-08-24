#!/usr/bin/env python3
"""tesla_fsd_metrics.py — pure-Python derivation of FSD showcase metrics.

Foundation for the four FSD-overlay ideas (friction circle, hands-free/
corner-count scoreboard, note-highway ribbon, rally-style pace notes) -- see
CLAUDE.md and each branch's design notes for the full context. All four have
now shipped.

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


def ribbon_window(values: list, index: int, past_n: int, future_n: int) -> list:
    """Slice a `past_n + 1 + future_n`-length window out of `values`, centered
    on `index` -- the pure math behind the note-highway ribbon
    (--fsd-note-highway): a horizontal scrolling strip of cornering severity
    where "now" sits fixed at the center, what's LEFT of center is what the
    car already did, and what's RIGHT of center is what the road is about to
    demand -- the one FSD showcase visual that needs the whole drive's
    timeline, not just the current sample (or a short trailing window, like
    the friction circle's trail).

    Continues the g_to_offset precedent: this is plain list slicing with zero
    gopro_overlay dependency, so it belongs in this dependency-free module,
    not in the venv-only tesla_fsd_overlay.py driver where tests/ (system
    Python, no gopro-overlay installed) couldn't reach it.

    Positions before the start or past the end of `values` are None-padded,
    NOT wrapped, clamped to the nearest real index, or fabricated -- the same
    "a gap must show as a gap" principle GTrailBuffer/decode_fsd_fields/
    TakeoverCounter all already follow for a real telemetry gap. Here the gap
    is structural (the start or end of the drive itself) rather than a
    missing sample mid-drive, but the right on-screen answer is identical: a
    visible gap, not a flat line pretending the road was straight before the
    drive began or after it ended.

    A `None` already present inside `values` (a genuine mid-drive telemetry
    gap) passes through completely untouched -- this function only ever adds
    padding at the two ends, never interpolates or drops an existing value.

    The returned list is always exactly `past_n + 1 + future_n` items long,
    regardless of how much of it ended up padded -- callers (the widget) can
    rely on that fixed length unconditionally.
    """
    return [values[i] if 0 <= i < len(values) else None
            for i in range(index - past_n, index + future_n + 1)]


# --- pace notes (--fsd-pace-notes) -------------------------------------------
# Rally-style pace-notes callouts, e.g. "RIGHT 3": a text callout that appears
# a few seconds BEFORE a corner and clears shortly after, mirroring a real
# rally co-driver reading pre-recorded notes just ahead of the driver. Like
# the note-highway ribbon (see ribbon_window above), this needs the drive's
# WHOLE timeline up front, not just the current sample -- a callout has to
# appear before its corner starts, which is only possible because
# compositing happens after the fact. Designed with a Fable-model creative
# consult (same process the other three FSD widgets went through) -- see
# CLAUDE.md's "The pace notes" design-notes bullet for the full brief and
# rationale behind the specific numbers below.

# Rally severity numbers are non-linear: 1 is a near-hairpin/dramatic corner,
# 6 is barely worth a call. Scaled to FSD's own real cornering envelope
# (comfortable mountain-road FSD driving measured so far tops out well under
# 0.6g -- see FRICTION_CIRCLE_MAX_G's own comment in tesla_fsd_overlay.py),
# not real rally G-loads (1.5g+) -- a straight port of real pace-note g
# breakpoints would put every FSD corner at "6" and never call anything
# else. The floor (0.15) is deliberately CornerCounter.ENTER_G exactly: every
# corner graded here is a corner CornerCounter would also count (both use
# the same 0.15g floor). That's a WEAKER guarantee than "the counts always
# match" -- they don't: build_pace_notes drops sub-min-duration blips and
# merges chained pairs into one callout, and segment_corners drops a corner
# still open when the timeline ends, none of which CornerCounter does -- so
# the scoreboard's corner_count and the number of pace-notes callouts a
# viewer actually hears can legitimately differ. What's guaranteed is that
# every CALLOUT corresponds to a counted corner, never the reverse claim.
PACE_NOTE_GRADE_THRESHOLDS = (
    (0.45, 1),
    (0.37, 2),
    (0.29, 3),
    (0.22, 4),
    (0.17, 5),
    (0.15, 6),
)


def grade_corner(peak_g: float) -> int:
    """Map a corner's peak |lateral_g| to a rally-style severity number, 1
    (tightest/most dramatic) to 6 (loosest/barely worth a call) -- see
    PACE_NOTE_GRADE_THRESHOLDS above for the breakpoints and rationale.

    A magnitude below every threshold clamps to 6 (the loosest grade) rather
    than raising or returning an out-of-range number -- defensive only:
    segment_corners (below) only ever creates a Corner whose peak already
    cleared PACE_NOTE_GRADE_THRESHOLDS' own floor, so this path shouldn't be
    reachable from that caller, but grade_corner is a public, independently
    testable function and must degrade gracefully for any caller.
    """
    magnitude = abs(peak_g)
    for min_g, grade in PACE_NOTE_GRADE_THRESHOLDS:
        if magnitude >= min_g:
            return grade
    return 6


class Corner(NamedTuple):
    """One detected corner, found by scanning a FULL lateral_g timeline in a
    single pass (see segment_corners) -- unlike CornerCounter (a streaming,
    one-sample-at-a-time detector feeding the scoreboard's corner_count and
    the friction-circle's peak-G readout), pace-notes needs each corner's
    START index known before the render loop gets anywhere near it, so the
    callout can appear ahead of time. `direction`: +1 = right, -1 = left,
    the sign of lateral_g at the corner's peak sample -- see segment_corners
    for why this reuses g_to_offset rather than re-deriving the sign.

    `start_index`/`end_index` are raw TIMELINE positions (end_index is the
    first sample that confirmed exit, i.e. the sample whose magnitude first
    dropped below exit_g) -- used for display timing (when the corner really
    happened), so they intentionally still span any telemetry gap the corner
    happened to bracket. `observed_samples` is different: it counts only the
    REAL (non-None) samples actually seen while this corner was open, and is
    what duration-based decisions (build_pace_notes' min_samples filter and
    "LONG" flag) key off -- see segment_corners' own docstring for why
    end_index - start_index would be the wrong number to use for those.

    `gap_before` is True if the stretch immediately preceding this corner's
    own start (back to the previous corner's end, or the start of the
    timeline) contained ANY missing sample -- used to refuse chaining two
    corners across an unobserved gap (see build_pace_notes): confidently
    saying "into" implies the road between them was actually seen to be
    straight, not merely assumed to be from missing data."""
    start_index: int
    end_index: int
    peak_g: float
    direction: int
    observed_samples: int
    gap_before: bool


def segment_corners(values: list, enter_g: float = CornerCounter.ENTER_G,
                    exit_g: float = CornerCounter.EXIT_G, min_samples: int = 1) -> list:
    """Scan a full lateral_g timeline (e.g. NoteHighway's own
    lateral_g_timeline) for corners in one pass, returned in START order as
    a list of Corner.

    This is a genuinely new array-scanning pass, NOT a call into
    CornerCounter itself, for the same reason ribbon_window is its own
    function rather than a call into CornerCounter: a full-timeline
    lookahead is a fundamentally different mode from CornerCounter's
    one-sample-at-a-time streaming API, which has no notion of "this
    corner's start index" to hand back once it's already several samples in.
    It DOES reuse CornerCounter's ENTER_G/EXIT_G thresholds by default --
    the same tuned hysteresis the scoreboard's corner_count and the
    friction-circle's peak-G readout already use, so pace-notes calls out
    exactly the corners the rest of the app already agrees are corners, not
    a independently-tuned subset.

    None values (a telemetry gap) are a no-op for corner STATE exactly like
    CornerCounter.update()'s own None-safety: they neither start nor end a
    corner, and don't move the peak -- so a gap mid-corner simply pauses the
    corner (holding its in-progress state) rather than ending or fabricating
    one. But a gap must ALSO not fabricate DURATION: a real 0.2g sample
    followed by a 10-second telemetry blackout and then a single return-to-
    baseline sample must not be reported as a ten-second corner just because
    the two real samples are ten seconds apart on the timeline -- confirmed
    as a real bug (an executable repro produced exactly that "RIGHT 5 LONG"
    callout from one real sample either side of a manufactured gap) by an
    independent review before this fix. So `observed_samples` -- a count of
    only the REAL samples seen while in this corner -- is what min_samples
    filters against and what build_pace_notes' "LONG" flag reads, NOT
    end_index - start_index (which stays a plain timeline span, still useful
    for display timing, but is exactly the wrong number for "how much of
    this corner did we actually measure").

    A corner still open when the array runs out (the video ends mid-corner)
    is DROPPED, not synthesized an end -- we never confirmed how it
    resolves, and per this codebase's "a gap must show as a gap" principle,
    an unconfirmed corner must not be called out as if it were a real,
    complete one. `min_samples` (now measured in REAL observed samples, see
    above) drops any corner shorter than that many samples too (the widget's
    decision what "too short to call out" means in real seconds, mirroring
    how ribbon_window/GTrailBuffer take counts, not seconds, and leave the
    seconds->samples conversion to the caller).
    """
    corners = []
    in_corner = False
    start = peak_idx = None
    peak = 0.0
    observed = 0
    gap_since_last_corner = False
    for i, v in enumerate(values):
        if v is None:
            if not in_corner:
                gap_since_last_corner = True
            continue
        magnitude = abs(v)
        if not in_corner:
            if magnitude >= enter_g:
                in_corner = True
                start = i
                peak = magnitude
                peak_idx = i
                observed = 1
            continue
        observed += 1
        if magnitude > peak:
            peak = magnitude
            peak_idx = i
        if magnitude < exit_g:
            if observed >= min_samples:
                peak_signed = values[peak_idx]
                # Reuses g_to_offset for the sign -- not just its
                # convention, the actual function -- per CLAUDE.md's
                # explicit warning that pace-notes must not re-derive
                # the lateral sign independently. max_g is set to the
                # peak's own magnitude so g_to_offset's clamp-to-rim
                # never engages here (nothing to clamp against): only
                # dx's SIGN is used, so its scaled magnitude is
                # irrelevant, just its direction.
                dx, _dy = g_to_offset(peak_signed, 0.0,
                                      max_g=max(abs(peak_signed), 1e-9))
                direction = 1 if dx >= 0 else -1
                corners.append(Corner(start, i, peak, direction, observed,
                                      gap_since_last_corner))
            in_corner = False
            gap_since_last_corner = False
    return corners


class PaceNote(NamedTuple):
    """One rally-style callout, built from a Corner (see build_pace_notes).
    `chain_grade`/`chain_direction` are set (not None) when this note
    CHAINS into the next corner (e.g. "RIGHT 3" with a smaller "into LEFT 4"
    line beneath it) -- real pace notes chain closely-linked corners like
    this, and it's cheap once corners are already segmented. When a note
    chains into the next corner, that next corner does NOT also get its own
    standalone PaceNote (see build_pace_notes) -- re-announcing it a second
    later would read as a stutter, not authenticity."""
    corner: Corner
    grade: int
    long: bool
    chain_grade: Optional[int] = None
    chain_direction: Optional[int] = None


def build_pace_notes(corners: list, long_samples: int, chain_gap_samples: int) -> list:
    """Turn a list of Corner (segment_corners' output, already in start
    order) into a list of PaceNote, one per corner EXCEPT a corner that gets
    absorbed into the previous note's chain (see PaceNote's docstring).

    `long_samples`: a corner whose `observed_samples` (REAL samples actually
    seen while it was open -- NOT its raw start_index/end_index span, which
    can span an unobserved telemetry gap; see Corner's and segment_corners'
    own docstrings for why that distinction is load-bearing) is at least
    this many gets PaceNote.long = True (the widget renders a "LONG"
    suffix) -- the widget's decision what "long" means in real seconds, same
    seconds->samples-at-the-caller convention segment_corners' min_samples
    already uses.

    `chain_gap_samples`: two corners chain when the gap between the first's
    END and the second's START is no more than this many samples (and not
    negative -- a negative gap would mean the "next" corner's Corner tuple
    somehow starts before the current one ends, which segment_corners' own
    single left-to-right pass can never produce, but the check costs
    nothing) AND that gap was actually fully OBSERVED -- `nxt.gap_before`
    being True means some part of the straight stretch between the two
    corners was missing telemetry, so this function can't confidently claim
    "into": maybe there was a third corner hiding in the gap. Confirmed as a
    real bug (two corners either side of a total telemetry blackout chaining
    into a confident-looking "into" callout) by an independent review before
    this fix. Chaining looks only one corner ahead -- a note chains into AT
    MOST one further corner, never "into X into Y", matching how a real
    rally note only ever calls the next linked corner, not a whole string of
    them in one breath.
    """
    notes = []
    skip_next = False
    for i, corner in enumerate(corners):
        if skip_next:
            skip_next = False
            continue
        grade = grade_corner(corner.peak_g)
        long_ = corner.observed_samples >= long_samples
        chain_grade = chain_direction = None
        if i + 1 < len(corners):
            nxt = corners[i + 1]
            gap = nxt.start_index - corner.end_index
            if 0 <= gap <= chain_gap_samples and not nxt.gap_before:
                chain_grade = grade_corner(nxt.peak_g)
                chain_direction = nxt.direction
                skip_next = True
        notes.append(PaceNote(corner=corner, grade=grade, long=long_,
                              chain_grade=chain_grade, chain_direction=chain_direction))
    return notes


def pace_note_window(note: "PaceNote", lead_samples: int, hold_samples: int) -> tuple:
    """The NATURAL (start_index, end_index) INCLUSIVE range of draw-call
    indices during which `note` should be considered showable, ignoring any
    other note that might overlap it (see visible_window for the version
    that accounts for a following note): from `lead_samples` before its
    corner's start (clamped to 0 -- a corner near the very start of the
    drive just gets a shorter lead-in, not a negative index) through
    `hold_samples` AFTER the corner's start -- deliberately anchored to the
    corner's START, not its end, matching a real co-driver: the call is read
    on approach and the driver is already executing the corner by the time
    it clears, not still waiting for it to finish. Full opacity itself ends
    even earlier than this window's own end -- pace_note_alpha starts
    ramping the fade-out `fade_out_samples` before this window closes, so
    "clears" in the ordinary sense happens at `hold_samples - fade_out_
    samples` after the corner's start, not at `hold_samples` (see
    pace_note_alpha's own docstring, which is the accurate description of
    when the callout visually disappears)."""
    start = max(0, note.corner.start_index - lead_samples)
    end = note.corner.start_index + hold_samples
    return start, end


def visible_window(notes: list, i: int, lead_samples: int, hold_samples: int) -> tuple:
    """The (start_index, end_index) INCLUSIVE range of draw-call indices
    during which notes[i] should actually be shown -- `pace_note_window`'s
    natural window, TRUNCATED so it never remains visible into the following
    note's (notes[i+1], if any) own natural window.

    Without this truncation, an overlapping earlier note stays at full
    hold-opacity right up to the exact index a later note's window begins,
    then vanishes in a single frame while the later note pops in already
    fading up from zero -- a real, reachable glitch (confirmed by an
    independent review with an executable repro: two corners ~2-3 real
    seconds apart, closer than LEAD_SAMPLES + HOLD_SAMPLES but too far apart
    to chain, produce exactly this one-frame hard cut) that contradicts this
    branch's own "pure alpha fade, never two panels at once" design claim.

    Truncating the end here means pace_note_alpha's fade-out ramp (which
    counts backward from whatever end THIS function hands it) completes
    EARLIER too -- the earlier note fades out smoothly and reaches zero by
    the time the later note's own window opens, instead of being cut off
    mid-hold. This never EXTENDS a window, only shortens it, and is clamped
    so the result is never shorter than one sample even in a pathological
    near-zero-gap case (chaining, not this truncation, is the mechanism for
    genuinely tight corners -- see build_pace_notes).

    `notes` is assumed sorted by corner.start_index ascending -- exactly the
    order build_pace_notes/segment_corners already produce it in.
    """
    start, end = pace_note_window(notes[i], lead_samples, hold_samples)
    if i + 1 < len(notes):
        next_start, _next_end = pace_note_window(notes[i + 1], lead_samples, hold_samples)
        end = min(end, next_start - 1)
    return start, max(start, end)


def active_pace_note(notes: list, index: int, lead_samples: int,
                     hold_samples: int) -> Optional["PaceNote"]:
    """The PaceNote that should be visible at draw-call `index`, or None.

    Uses visible_window (not the bare pace_note_window) for the containment
    check, so a note already truncated against its successor (see
    visible_window's own docstring) is never selected past that truncated
    end -- scanning in REVERSE and returning the first match means the
    LATER-starting note wins whenever both would otherwise be showable at
    this index, so a fresh call always takes priority over a stale one:
    never two panels at once, and -- since visible_window already truncated
    the earlier note's own window so it fades out before this point is ever
    reached -- never an abrupt cut either.
    """
    for i in range(len(notes) - 1, -1, -1):
        start, end = visible_window(notes, i, lead_samples, hold_samples)
        if start <= index <= end:
            return notes[i]
    return None


def pace_note_alpha(notes: list, i: int, index: int, lead_samples: int, hold_samples: int,
                    fade_in_samples: int, fade_out_samples: int) -> float:
    """0.0-1.0 opacity for notes[i] at draw-call `index`: ramps up linearly
    over `fade_in_samples` from visible_window(notes, i, ...)'s (possibly
    truncated-against-the-next-note, see that function's own docstring)
    start, holds at 1.0, then ramps down linearly over `fade_out_samples` to
    that same window's end. This is the ENTIRE "animation" -- alpha only, no
    position slide or size change (a slide would fight the note-highway
    ribbon's own leftward flow this widget sits below; PIL font sizes step
    discretely so a scale "animation" would judder rather than read as
    smooth) -- the same direct-RGBA-on-transparent-canvas technique the
    friction-circle's trail already proved composites correctly (see
    FrictionCircle's own docstring). Returns 0.0 outside the window entirely
    -- a defensive fallback; callers should already have confirmed
    `notes[i] is active_pace_note(...)` before calling this.

    Takes `(notes, i)` rather than a bare note (a signature change from an
    earlier version of this function) specifically so it can consult the
    SAME truncated window active_pace_note used to select this note --
    keeping the two independently re-derive the window risked exactly the
    inconsistency (a note fading based on its natural, untruncated window
    while a DIFFERENT note is actually being drawn over top of it) this
    function exists to prevent.
    """
    start, end = visible_window(notes, i, lead_samples, hold_samples)
    if index < start or index > end:
        return 0.0
    fade_in = max(1, fade_in_samples)
    fade_out = max(1, fade_out_samples)
    alpha_in = min(1.0, (index - start) / fade_in)
    alpha_out = min(1.0, (end - index) / fade_out)
    return max(0.0, min(alpha_in, alpha_out))


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
