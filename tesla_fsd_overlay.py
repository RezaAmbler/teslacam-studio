#!/usr/bin/env python3
"""tesla_fsd_overlay.py — driver for FSD showcase overlays.

Wired into tesla_combine.py's CLI as `--fsd-scoreboard` and
`--fsd-friction-circle` (first two of the four FSD-showcase visuals to
actually ship): proves, end to end, that Tesla's per-frame G-force/autopilot
telemetry (SEI -> tesla_gps.write_gpx()'s repurposed GPX tags -> gopro-overlay's
GPX-based pipeline -> tesla_fsd_metrics' pure decode/derivation -> a widget)
can drive a real composited video, and draws either the "streak scoreboard"
visual (StreakScoreboard, --widget scoreboard, the default) -- an accumulating
hands-free/corner/peak-G/takeover stat line -- or the "friction circle" G-G
diagram visual (FrictionCircle, --widget friction-circle) -- lateral G vs.
longitudinal G on a ringed target with a fading trail. The two still-deferred
showcase ideas (note-highway ribbon, pace-notes) are each their own follow-up
branch's job; FsdDiagnosticText (--widget diagnostic) is kept around, not
deleted, for debugging those. See CLAUDE.md for the full design rationale
(axis mapping, GPX tag repurposing, why gopro-dashboard.py's CLI can't carry
any of this on its own).

Must run under ./.venv (gopro-overlay installed there, not under the system
Python tesla_combine.py itself uses) -- same venv-boundary reason `--map`/
`--gauge` already subprocess out to gopro-dashboard.py:

    ./.venv/bin/python tesla_fsd_overlay.py <input-video> <output> \\
        --gpx <path> --font <path> --ffmpeg-dir <dir> [--widget scoreboard|diagnostic]

`<input-video>` is a hero-camera video (e.g. tesla_combine.py's own
`*-front-combined.mp4`); `--gpx` is a GPX written by tesla_gps.write_gpx()
carrying the repurposed <cad>/<power>/<hr> tags (see that function's comment).
Positional input/output are adjacent, before any --flags -- the same ordering
fix build_gauge_overlay (tesla_combine.py) already had to learn the hard way
about gopro-dashboard.py's own argparser mis-assigning a lone leading
positional otherwise.

Structure mirrors gopro-dashboard.py's own `--use-gpx-only --input <video>`
code path closely (loading, FFMPEGOverlayVideo, the stepper/SingleBuffer/
Overlay.draw render loop) -- each piece read verbatim from the installed
gopro-overlay 0.134.0 package this branch was built against, and ported
near-verbatim since it's already a proven-working loop body. What's new here
is the two process() passes decoding/deriving the FSD fields, and the
StreakScoreboard/FsdDiagnosticText widgets in place of an XML-driven layout.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# tesla_fsd_metrics has zero gopro_overlay dependency (see its own docstring),
# so it's safe -- and cheap -- to import before the gopro_overlay imports
# below, which only work under ./.venv (see the module docstring above).
import tesla_fsd_metrics as fsd_metrics

from gopro_overlay.assertion import assert_file_exists
from gopro_overlay.buffering import SingleBuffer
from gopro_overlay.entry import Entry
from gopro_overlay.execution import InProcessExecution
from gopro_overlay.ffmpeg import FFMPEG
from gopro_overlay.ffmpeg_gopro import FFMPEGGoPro
from gopro_overlay.ffmpeg_overlay import FFMPEGOverlayVideo
from gopro_overlay.font import load_font
from gopro_overlay.framemeta_gpx import timeseries_to_framemeta
from gopro_overlay.layout import Overlay
from gopro_overlay.loading import load_external
from gopro_overlay.log import log, fatal
from gopro_overlay.point import Coordinate
from gopro_overlay.progresstrack import ProgressBarProgress
from gopro_overlay.timeunits import timeunits
from gopro_overlay.units import units
from gopro_overlay.widgets.widgets import Widget

# The render loop's own step cadence -- main() passes this to frame_meta.
# stepper(), which is what "one widget draw() call" actually means in wall-
# clock terms. FRICTION_CIRCLE_TRAIL_HZ/_LEN below derive from this SAME
# constant rather than re-stating "0.1" independently -- an earlier version
# had that duplication (a real finding from an independent review: change
# one without the other and the "3-second" trail silently becomes a
# different duration, with nothing to catch the drift).
RENDER_STEP_SECONDS = 0.1

# A loadable TTF is required at startup even for this plain-text diagnostic
# widget. Mirrors tesla_combine.py's own FONT_CANDIDATES/find_font() fallback
# idea, kept as an independent list here rather than importing tesla_combine.py
# -- this script runs under a different Python (./.venv) than tesla_combine.py
# itself and has no other reason to depend on that ~2300-line module.
FONT_CANDIDATES = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Courier.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def find_font() -> str:
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return f
    fatal("No usable font found; pass --font explicitly.")


# --- streak scoreboard (--widget scoreboard, the default) -------------------
# A single-line dark rounded panel, top-right of the hero tile -- matching
# --gauge's established visual language (tesla_combine.py's GAUGE_* panel is
# bg=(0,0,0,180)-ish translucent), but purpose-built here as a directly-drawn
# widget rather than a gopro-overlay XML layout: --gauge's dial/compass/chart
# each need real widget geometry, this is one line of styled text, so a
# hand-rolled PIL draw is simpler than a layout XML round-trip for it.
#
# Position: TOP-RIGHT, deliberately not top-left. tesla_combine.py's grid
# filter graph (_tile_chain) draws the hero tile's own "FRONT"-style label at
# x=20:y=20 (HERO_FONT_SIZE=64) AFTER this compositing step runs (this script
# only ever sees the bare hero video -- the grid label doesn't exist yet at
# this point in the pipeline) -- so a top-left scoreboard here would collide
# with that later-drawn label. Top-right avoids that collision AND --gauge's
# own bottom-left panel, so `--gauge --fsd-scoreboard` together (which
# tesla_combine.py chains sequentially, gauge first) don't overlap either.
#
# Every fraction below started as a synthetic-render estimate (same status
# GAUGE_PANEL_W_FRAC etc started with in tesla_combine.py) and has since been
# confirmed against real rendered frames -- both the tall grid and
# --landscape, alone and combined with --gauge -- legible, no overflow, no
# collision with the hero label or --gauge's panel in any of those.
SCOREBOARD_PANEL_W_FRAC = 0.85   # panel width, as a fraction of the tile width -- wide,
                                 # because a single line carrying the badge plus four
                                 # stats needs the room; measured to comfortably fit the
                                 # worst-case text length (a >1hr hands-free readout) at
                                 # SCOREBOARD_FONT_SCALE across every tile size tested.
SCOREBOARD_PANEL_H_FRAC = 0.07   # panel height, as a fraction of the tile height --
                                 # a single line, so much shorter than --gauge's
                                 # 4-section GAUGE_PANEL_H_FRAC (0.17) box.
SCOREBOARD_MARGIN = 24          # px from the tile's top-right corner (matches GAUGE_MARGIN)
SCOREBOARD_FONT_SCALE = 0.5     # font size, as a fraction of the panel's inner (post-padding)
                                 # height -- deliberately well under 1.0 so the line's real
                                 # measured width (not just its height) fits inside
                                 # SCOREBOARD_PANEL_W_FRAC's panel at typical tile sizes.

SCOREBOARD_PANEL_BG = (0, 0, 0, 180)         # translucent dark panel, matching --gauge
SCOREBOARD_ENGAGED_BG = (20, 130, 40, 230)   # badge: green tint while FSD is engaged
SCOREBOARD_DISENGAGED_BG = (110, 45, 45, 230)  # badge: gray/red tint while not engaged


class StreakScoreboard(Widget):
    """The real "streak scoreboard" showcase visual: an accumulating stat
    line -- hands-free time, corner count, peak cornering G, takeover count --
    that builds a visible track record on screen as the drive progresses.
    Replaces FsdDiagnosticText as the default draw target (see --widget).

    One dark translucent panel (SCOREBOARD_PANEL_BG, rounded corners) holding
    a color-coded "FSD ENGAGED"/"FSD OFF" badge chip (green while engaged,
    gray/red while not) followed by plain white stats text. Geometry is
    computed from the actual frame size at draw time (not a fixed layout),
    so it works unchanged for both the tall grid's full-width hero row and
    landscape's native-res hero block -- same approach write_gauge_layout
    takes via tile_w/tile_h, just resolved here from `image.size` instead of
    a size passed in, since this widget composites directly rather than
    going through a gopro-overlay XML layout.

    KNOWN LIMITATION: the takeover count follows TakeoverCounter, which
    (per tesla_fsd_metrics.TakeoverCounter's own docstring and CLAUDE.md) has
    never been exercised against a real disengagement event -- every drive
    probed so far stayed engaged throughout. The rest of the line (hands-free
    time, corner count, peak G) IS verified against real footage.
    """

    PAD_FRAC = 0.18        # inner panel padding, as a fraction of panel height
    BADGE_PAD_FRAC = 0.35  # badge chip's horizontal text padding, as a fraction of font size
    GAP_FRAC = 0.5         # gap between the badge and the stats text, as a fraction of font size

    def __init__(self, entry, font):
        self.entry = entry
        self.font = font

    def draw(self, image, draw):
        e = self.entry()
        engaged = bool(e.autopilot_engaged)
        hands_free = fsd_metrics.format_hms(e.hands_free_seconds or 0.0)
        corners = e.corner_count or 0
        peak = e.peak_lateral_g or 0.0
        takeovers = e.takeover_count or 0

        img_w, img_h = image.size
        panel_w = max(2, round(img_w * SCOREBOARD_PANEL_W_FRAC))
        panel_h = max(2, round(img_h * SCOREBOARD_PANEL_H_FRAC))
        x2 = img_w - SCOREBOARD_MARGIN
        x1 = x2 - panel_w
        y1 = SCOREBOARD_MARGIN
        y2 = y1 + panel_h

        pad = max(4, round(panel_h * self.PAD_FRAC))
        radius = max(2, round(panel_h * 0.2))
        draw.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=SCOREBOARD_PANEL_BG)

        font_size = max(8, round((panel_h - 2 * pad) * SCOREBOARD_FONT_SCALE))

        badge_text = "FSD ENGAGED" if engaged else "FSD OFF"
        stats_text = (f"hands-free {hands_free} · corner {corners} · "
                      f"peak {peak:.2f}g · takeovers {takeovers}")

        # SCOREBOARD_PANEL_W_FRAC/FONT_SCALE were sized to fit this line
        # comfortably (see their comments), but that was checked against
        # Menlo -- a monospace font this project always finds first
        # (FONT_CANDIDATES). If a caller ever loads a wider, proportional
        # fallback font, or a tile size well outside what's been tested,
        # this shrinks the font a step at a time until the real measured
        # width (via textbbox, not assumed) fits inside the panel, rather
        # than silently letting text run past the right edge.
        gap = max(2, round(font_size * self.GAP_FRAC))
        badge_pad = max(2, round(font_size * self.BADGE_PAD_FRAC))
        available_w = (x2 - pad) - (x1 + pad) - 2 * badge_pad - gap
        font = self.font.font_variant(size=font_size)
        while font_size > 8:
            badge_bbox = draw.textbbox((0, 0), badge_text, font=font)
            stats_bbox = draw.textbbox((0, 0), stats_text, font=font)
            total_w = (badge_bbox[2] - badge_bbox[0]) + (stats_bbox[2] - stats_bbox[0])
            if total_w <= available_w:
                break
            font_size -= 1
            badge_pad = max(2, round(font_size * self.BADGE_PAD_FRAC))
            gap = max(2, round(font_size * self.GAP_FRAC))
            font = self.font.font_variant(size=font_size)

        badge_bbox = draw.textbbox((0, 0), badge_text, font=font)
        badge_text_w = badge_bbox[2] - badge_bbox[0]
        badge_x1, badge_y1 = x1 + pad, y1 + pad
        badge_x2, badge_y2 = badge_x1 + badge_text_w + 2 * badge_pad, y2 - pad
        badge_bg = SCOREBOARD_ENGAGED_BG if engaged else SCOREBOARD_DISENGAGED_BG
        draw.rounded_rectangle([badge_x1, badge_y1, badge_x2, badge_y2],
                               radius=max(2, round((badge_y2 - badge_y1) * 0.25)), fill=badge_bg)
        badge_text_y = (badge_y1 + (badge_y2 - badge_y1 - (badge_bbox[3] - badge_bbox[1])) / 2
                        - badge_bbox[1])
        draw.text((badge_x1 + badge_pad, badge_text_y), badge_text, font=font, fill=(255, 255, 255, 255))

        stats_x = badge_x2 + gap
        stats_bbox = draw.textbbox((0, 0), stats_text, font=font)
        stats_text_y = y1 + (panel_h - (stats_bbox[3] - stats_bbox[1])) / 2 - stats_bbox[1]
        draw.text((stats_x, stats_text_y), stats_text, font=font, fill=(255, 255, 255, 255))


# --- friction circle G-meter (--fsd-friction-circle) -------------------------
# The classic motorsport G-G diagram: lateral G vs. longitudinal G plotted as a
# dot on a ringed target, with a fading trail behind it. Smooth FSD driving
# (brake -> turn-in -> apex -> throttle blending into one continuous curve)
# traces clean arcs; jerky driving scatters.
#
# Position: BOTTOM-RIGHT -- the one corner the hero tile's other overlays
# leave free. tesla_combine.py's grid filter graph draws the hero label
# top-left (see StreakScoreboard's own comment above); StreakScoreboard itself
# takes top-right; --gauge's dashboard panel takes bottom-left (GAUGE_MARGIN,
# tesla_combine.py). Bottom-right is also naturally suited to a roughly-
# square ringed target, unlike the scoreboard's wide single-line panel. This
# means all three overlay flags together (--gauge --fsd-scoreboard
# --fsd-friction-circle) now use all four corners with no collisions by
# construction, not by luck.
#
# Every fraction below started as a synthetic-render estimate, same status
# every other panel constant in this codebase has had (SCOREBOARD_*, GAUGE_*)
# -- a starting point that must be checked against a real rendered frame.
FRICTION_CIRCLE_SIZE_FRAC = 0.30  # ringed-target circle's diameter, as a
                                  # fraction of the tile's SHORTER dimension
                                  # (not width/height separately, since the
                                  # target itself is round) -- a starting
                                  # guess-then-verify constant like every
                                  # other panel size in this codebase.
FRICTION_CIRCLE_MARGIN = 24      # px from the tile's bottom-right corner
                                  # (matches GAUGE_MARGIN/SCOREBOARD_MARGIN)
FRICTION_CIRCLE_PAD_FRAC = 0.16  # inner margin between the circle's outer
                                  # edge and the drawn rings, as a fraction of
                                  # the circle's diameter -- reserves room for
                                  # the axis tick labels without them
                                  # overhanging the panel's edge.
FRICTION_CIRCLE_TEXT_H_FRAC = 0.16  # height of the "peak this corner" text
                                  # strip below the circle, as a fraction of
                                  # the circle's diameter.
FRICTION_CIRCLE_GAP_FRAC = 0.04  # gap between the circle and the text strip
                                  # below it, as a fraction of the circle's
                                  # diameter.

FRICTION_CIRCLE_MAX_G = 0.6      # g value at the RING AREA's outer edge (the
                                  # full-scale radius a sample is plotted
                                  # against) -- comfortable FSD lateral
                                  # cornering measured so far tops out well
                                  # under this (CornerCounter.ENTER_G=0.15,
                                  # real drives peaking ~0.3-0.5g per
                                  # CLAUDE.md), so 0.6g gives headroom above
                                  # the 0.4g outer ring without a realistic
                                  # peak pinning to the rim.
FRICTION_CIRCLE_RING_STEP = 0.2  # ring interval in g -- rings drawn at 0.2g
                                  # and 0.4g, per Fable's brainstorm.

FRICTION_CIRCLE_TRAIL_SECONDS = 3.0  # Fable's "3-second" trail
# Derived from RENDER_STEP_SECONDS (module-level, above) rather than
# restating "10Hz"/"0.1s" independently -- one widget draw() call per render
# step, and FrictionCircle appends to its trail once per draw() call, so
# RENDER_STEP_SECONDS is what "3 seconds" actually means in buffer length.
FRICTION_CIRCLE_TRAIL_LEN = round(FRICTION_CIRCLE_TRAIL_SECONDS / RENDER_STEP_SECONDS)  # 30

FRICTION_CIRCLE_BG = (0, 0, 0, 160)           # translucent dark disc, matching
                                              # SCOREBOARD_PANEL_BG/--gauge's style
FRICTION_CIRCLE_RING_RGB = (255, 255, 255, 90)   # faint ring outlines
FRICTION_CIRCLE_AXIS_RGB = (255, 255, 255, 55)   # faint crosshair axes
FRICTION_CIRCLE_TICK_RGB = (220, 220, 220, 200)  # axis tick label text
FRICTION_CIRCLE_TRAIL_RGB = (90, 200, 255)       # trail dot base color (alpha
                                                  # varies per point by age --
                                                  # see draw())
FRICTION_CIRCLE_TRAIL_MIN_ALPHA = 35   # oldest trail point
FRICTION_CIRCLE_TRAIL_MAX_ALPHA = 200  # newest trail point (still a step
                                        # below the current dot's own alpha,
                                        # so the current sample reads as the
                                        # brightest thing on the panel)
FRICTION_CIRCLE_DOT_RGB = (255, 255, 255, 255)   # current sample: solid bright dot
FRICTION_CIRCLE_TEXT_RGB = (255, 255, 255, 255)


class FrictionCircle(Widget):
    """The friction-circle G-meter showcase visual (--widget friction-circle):
    a ringed target (concentric circles at 0.2g/0.4g) with the current
    (lateral_g, longitudinal_g) sample as a bright dot, a fading 3-second
    trail behind it (GTrailBuffer), and a small "peak this corner: X.XXg"
    readout fed by CornerCounter.display_peak_g (grows live while a corner
    is in progress, holds the finished corner's peak between corners --
    NOT last_corner_peak_g directly, which would show the PREVIOUS corner's
    value for the whole duration of the current one).

    Owns a GTrailBuffer instance as __init__ state -- safe because
    Overlay.__init__ (gopro_overlay/layout.py) calls create_widgets(entry)
    exactly ONCE and reuses the same widget instances for every subsequent
    draw() call across the whole render, so instance-level accumulation
    (append-once-per-frame, in draw() below) is the correct, simplest way to
    hold a trail -- no need to thread it through FrameMeta.process() the way
    the *shared* derived fields (lateral_g, last_corner_peak_g, etc) are.

    Axis convention (lateral_g -> x, longitudinal_g -> y) mirrors
    tesla_fsd_metrics' documented mapping. WHICH SIGN reads as left/right
    and accel/brake was initially a best-guess, then checked against real
    telemetry (not eyeballed off single video frames -- see _g_to_px's own
    comment for why that method gave contradictory answers on a winding
    road): lateral needed no change (turning right already gave positive
    lateral_g), longitudinal was backwards (accelerating gave negative
    longitudinal_g, which the original `cy - (...)` put toward BRAKE) and
    has been fixed to `cy + (...)`.

    RENDERING TECHNIQUE: draws every shape directly onto the shared overlay
    canvas via self.font/draw with per-shape RGBA fill (no separate
    Frame-style alpha-mask layer -- see gopro_overlay/widgets/widgets.py's
    Frame class for that alternate technique). This mirrors StreakScoreboard,
    whose translucent panel/badge fills are drawn the same direct way and
    were confirmed, against real rendered footage, to composite correctly:
    the render loop's SingleBuffer starts each frame from a FULLY
    TRANSPARENT (0,0,0,0) canvas (see tesla_fsd_overlay.py's main()), so a
    direct fill's alpha value is written as-is and ffmpeg's own overlay
    filter alpha-blends the finished frame onto the video correctly. The
    trail is drawn OLDEST POINT FIRST specifically so that where two points
    happen to overlap, the newer (more opaque) one paints over the older
    (fainter) one -- the correct painter's-algorithm order for a fade, and
    why direct-alpha (rather than Frame's separate-layer-then-composite
    technique) is sufficient here without a flat-opacity artifact.
    """

    def __init__(self, entry, font):
        self.entry = entry
        self.font = font
        self.trail = fsd_metrics.GTrailBuffer(maxlen=FRICTION_CIRCLE_TRAIL_LEN)

    @staticmethod
    def _g_to_px(lateral_g, longitudinal_g, cx, cy, radius_px, max_g):
        """Map a (lateral_g, longitudinal_g) sample to panel pixel
        coordinates. The sign/clamp math itself (which axis means which
        physical direction, confirmed against real telemetry -- not
        eyeballed off a single video frame, which turned out to be an
        unreliable check on a continuously winding road) lives in
        tesla_fsd_metrics.g_to_offset, not here -- moved there after an
        independent review flagged that this pure math, with zero
        gopro_overlay dependency, was stuck in a venv-only script tests/
        can't reach, and that the two still-deferred FSD-overlay ideas will
        need this exact same convention. See that function's own docstring
        for the confirmed correlation values.

        This method only does the SCREEN-SPACE part: g_to_offset returns
        dy where +dy = accelerating = "up" in the intuitive/physical sense,
        but screen pixel y increases DOWNWARD, so it's negated here -- a
        UI-framework detail that deliberately does NOT belong in the
        dependency-free metrics module.
        """
        dx, dy = fsd_metrics.g_to_offset(lateral_g, longitudinal_g, max_g)
        x = cx + dx * radius_px
        y = cy - dy * radius_px
        return x, y

    def draw(self, image, draw):
        e = self.entry()
        lateral_g = e.lateral_g
        longitudinal_g = e.longitudinal_g
        # display_peak_g, not last_corner_peak_g: the latter only latches on
        # corner EXIT, so reading it directly would show the PREVIOUS
        # corner's peak for the whole duration of the current one -- exactly
        # when a viewer is most likely to be looking at a G-meter. See
        # CornerCounter.display_peak_g's docstring.
        peak = e.display_peak_g or 0.0

        # Append-once-per-frame: a None sample (data gap) is a no-op inside
        # GTrailBuffer.append itself, so the trail simply pauses (holds its
        # existing points) rather than fabricating a point at the origin.
        self.trail.append(lateral_g, longitudinal_g)

        img_w, img_h = image.size
        size = max(2, round(min(img_w, img_h) * FRICTION_CIRCLE_SIZE_FRAC))
        text_h = max(1, round(size * FRICTION_CIRCLE_TEXT_H_FRAC))
        gap = max(1, round(size * FRICTION_CIRCLE_GAP_FRAC))

        x2 = img_w - FRICTION_CIRCLE_MARGIN
        x1 = x2 - size
        panel_y2 = img_h - FRICTION_CIRCLE_MARGIN
        circle_y2 = panel_y2 - text_h - gap
        circle_y1 = circle_y2 - size
        cx, cy = (x1 + x2) / 2, (circle_y1 + circle_y2) / 2

        pad = max(2, round(size * FRICTION_CIRCLE_PAD_FRAC))
        radius_px = size / 2 - pad

        # Ringed target background: a filled dark disc, then concentric ring
        # outlines at each FRICTION_CIRCLE_RING_STEP up to FRICTION_CIRCLE_MAX_G.
        draw.ellipse([x1, circle_y1, x2, circle_y2], fill=FRICTION_CIRCLE_BG)
        ring_g = FRICTION_CIRCLE_RING_STEP
        while ring_g < FRICTION_CIRCLE_MAX_G - 1e-9:
            r = radius_px * (ring_g / FRICTION_CIRCLE_MAX_G)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=FRICTION_CIRCLE_RING_RGB)
            ring_g += FRICTION_CIRCLE_RING_STEP
        draw.ellipse([cx - radius_px, cy - radius_px, cx + radius_px, cy + radius_px],
                     outline=FRICTION_CIRCLE_RING_RGB)

        # Faint crosshair axes.
        draw.line([(cx - radius_px, cy), (cx + radius_px, cy)], fill=FRICTION_CIRCLE_AXIS_RGB)
        draw.line([(cx, cy - radius_px), (cx, cy + radius_px)], fill=FRICTION_CIRCLE_AXIS_RGB)

        # Axis tick labels, in the pad ring between the rings and the panel's
        # outer edge -- see the class docstring for the left/right and
        # accel/brake sign caveat. LEFT/RIGHT are aligned INWARD (toward
        # center, not outward toward the panel's own rim) deliberately --
        # verified against a real rendered frame that outward alignment lets
        # a wide label like "RIGHT" overflow past the panel's own edge (and,
        # since this widget sits in the tile's bottom-right corner, come
        # uncomfortably close to the tile's real edge too). Aligning inward
        # instead always has the whole spacious center of the circle to grow
        # into, so it can never overflow the panel regardless of font/label
        # width.
        tick_font = self.font.font_variant(size=max(8, round(size * 0.045)))
        tick_r = size / 2 - pad / 2
        for text, tx, ty, align in (
            ("LEFT", cx - tick_r, cy, "left"),
            ("RIGHT", cx + tick_r, cy, "right"),
            ("ACCEL", cx, cy - tick_r, "center"),
            ("BRAKE", cx, cy + tick_r, "center"),
        ):
            bbox = draw.textbbox((0, 0), text, font=tick_font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if align == "right":
                ox, oy = tx - tw, ty - th / 2
            elif align == "left":
                ox, oy = tx, ty - th / 2
            else:
                ox, oy = tx - tw / 2, ty - th / 2
            draw.text((ox, oy - bbox[1]), text, font=tick_font, fill=FRICTION_CIRCLE_TICK_RGB)

        # Fading trail, oldest first (see the class docstring on draw order).
        points = self.trail.points
        trail_r = max(1, round(radius_px * 0.02))
        n = len(points)
        for i, (lg, tg) in enumerate(points):
            alpha = (FRICTION_CIRCLE_TRAIL_MAX_ALPHA if n == 1 else round(
                FRICTION_CIRCLE_TRAIL_MIN_ALPHA + (FRICTION_CIRCLE_TRAIL_MAX_ALPHA -
                FRICTION_CIRCLE_TRAIL_MIN_ALPHA) * i / (n - 1)))
            px, py = self._g_to_px(lg, tg, cx, cy, radius_px, FRICTION_CIRCLE_MAX_G)
            draw.ellipse([px - trail_r, py - trail_r, px + trail_r, py + trail_r],
                        fill=(*FRICTION_CIRCLE_TRAIL_RGB, alpha))

        # Current sample: a solid bright dot on top of the trail. Skipped
        # (not faked at the origin) when this frame has no reading.
        if lateral_g is not None and longitudinal_g is not None:
            dot_r = max(2, round(radius_px * 0.04))
            px, py = self._g_to_px(lateral_g, longitudinal_g, cx, cy, radius_px,
                                   FRICTION_CIRCLE_MAX_G)
            draw.ellipse([px - dot_r, py - dot_r, px + dot_r, py + dot_r],
                        fill=FRICTION_CIRCLE_DOT_RGB)

        # "peak this corner" readout, in the text strip below the circle.
        text = f"peak this corner: {peak:.2f}g"
        text_font_size = max(8, round(text_h * 0.7))
        text_font = self.font.font_variant(size=text_font_size)
        available_w = size
        while text_font_size > 8:
            bbox = draw.textbbox((0, 0), text, font=text_font)
            if (bbox[2] - bbox[0]) <= available_w:
                break
            text_font_size -= 1
            text_font = self.font.font_variant(size=text_font_size)
        bbox = draw.textbbox((0, 0), text, font=text_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        text_x = cx - tw / 2
        text_y = circle_y2 + gap + (text_h - th) / 2 - bbox[1]
        draw.text((text_x, text_y), text, font=text_font, fill=FRICTION_CIRCLE_TEXT_RGB)


class FsdDiagnosticText(Widget):
    """Throwaway diagnostic overlay: plain text proving lateral_g/
    longitudinal_g/hands_free_seconds/corner_count all reach a widget with
    real values. Mirrors the draw(image, draw)-with-PIL-directly pattern used
    by gopro-overlay's own Compass/CompassArrow widgets (gopro_overlay/
    widgets/compass.py, compass_arrow.py) rather than composing from Text/
    Composite -- this is one solid block of plain multi-line text, not
    several independently-positioned pieces.
    """

    LINE_HEIGHT = 24
    PAD = 10
    WIDTH = 260

    def __init__(self, at: Coordinate, entry, font):
        self.at = at
        self.entry = entry
        self.font = font

    def draw(self, image, draw):
        e = self.entry()
        lat_g = e.lateral_g
        lon_g = e.longitudinal_g
        hands_free = e.hands_free_seconds or 0.0
        corners = e.corner_count or 0

        lines = [
            f"LAT_G: {lat_g:+.2f}" if lat_g is not None else "LAT_G: --",
            f"LONG_G: {lon_g:+.2f}" if lon_g is not None else "LONG_G: --",
            f"HANDS-FREE: {hands_free:.0f}s",
            f"CORNERS: {corners:d}",
        ]

        draw.rectangle(
            [
                self.at.x - self.PAD, self.at.y - self.PAD,
                self.at.x + self.WIDTH, self.at.y + self.LINE_HEIGHT * len(lines) + self.PAD,
            ],
            fill=(0, 0, 0, 160),
        )
        for i, line in enumerate(lines):
            draw.text(
                (self.at.x, self.at.y + i * self.LINE_HEIGHT),
                line, font=self.font, fill=(255, 255, 255, 255),
            )


def create_widgets_for(font, widget="scoreboard"):
    """Returns a `create_widgets(entry)` callable of the shape Overlay()
    (gopro_overlay/layout.py) expects -- proven directly usable outside the
    XML layout system by the library's own non-XML speed_awareness_layout
    (same file). No XML, no metric_accessor_from lookup, no patching needed:
    entry() is just an Entry whose .lateral_g/.corner_count/etc. attributes
    exist because our own process() passes below put them there (see
    Entry.__getattr__ in gopro_overlay/entry.py -- no fixed schema at all).

    `widget`: "scoreboard" (default, --widget) builds the real StreakScoreboard
    showcase visual; "friction-circle" builds the FrictionCircle G-meter
    showcase visual; "diagnostic" keeps FsdDiagnosticText available (not
    deleted) for future debugging of the two still-deferred FSD-overlay
    ideas, which will want the same kind of raw-value proof-of-plumbing check
    this branch's own verification relied on.
    """
    text_font = font.font_variant(size=22)

    def create(entry):
        if widget == "diagnostic":
            return [FsdDiagnosticText(Coordinate(24, 24), entry, text_font)]
        if widget == "friction-circle":
            return [FrictionCircle(entry, font)]
        return [StreakScoreboard(entry, font)]

    return create


def make_axis_decode_processor():
    """First process() pass: the repurposed GPX tags (cad/power/hr -- still
    pint Quantities at this point, gopro-overlay's gpx.py wraps them in rpm/
    watt/bpm units matching their literal tag names) -> named lateral_g/
    longitudinal_g/autopilot_engaged fields, via the pure decode in
    tesla_fsd_metrics. Unwrapping `.magnitude` here (not in tesla_fsd_metrics)
    is what keeps that module free of any gopro_overlay/pint dependency."""

    def processor(entry: Entry):
        cad = entry.cad.magnitude if entry.cad is not None else None
        power = entry.power.magnitude if entry.power is not None else None
        hr = entry.hr.magnitude if entry.hr is not None else None
        fields = fsd_metrics.decode_fsd_fields(cad, power, hr)
        return {
            "lateral_g": fields.lateral_g,
            "longitudinal_g": fields.longitudinal_g,
            "autopilot_engaged": fields.autopilot_engaged,
        }

    return processor


def make_stateful_processor():
    """Second process() pass: the scoreboard's running/stateful fields
    (corner_count, peak_lateral_g, hands_free_seconds), closure-captured
    state per entry -- NOT gopro-overlay's process_deltas() mechanism.
    process_deltas' tail-correction loop (gopro_overlay/framemeta.py)
    deliberately revisits the final pair of entries a second time, which is
    harmless for the stateless per-pair calculations it was designed for
    (e.g. calculate_speeds) but would double-count the last interval for a
    running accumulator like this one -- so this uses plain process() with
    the previous entry's timestamp closure-captured instead.

    Must run AFTER make_axis_decode_processor()'s pass -- it reads
    entry.lateral_g/entry.autopilot_engaged, which that pass adds.
    """
    counter = fsd_metrics.CornerCounter()
    hands_free = fsd_metrics.HandsFreeAccumulator()
    takeovers = fsd_metrics.TakeoverCounter()
    last_dt = [None]  # single-element list: a mutable cell for the closure

    def processor(entry: Entry):
        if last_dt[0] is None:
            dt_seconds = 0.0
        else:
            dt_seconds = (entry.dt - last_dt[0]).total_seconds()
        last_dt[0] = entry.dt

        counter.update(entry.lateral_g)
        # hands_free treats an unknown (None) sample as conservative-display
        # "not engaged" (bool(None) is False) -- fine, since it just skips
        # accumulating time it can't confirm. takeovers must NOT do the same
        # coercion: TakeoverCounter.update needs the real None to tell "we
        # don't know" apart from "confirmed disengaged," or a telemetry gap
        # (e.g. retime_samples' edge-hold pad) reads as a fabricated
        # takeover -- see TakeoverCounter's docstring for the confirmed bug
        # this fixes.
        hands_free.update(bool(entry.autopilot_engaged), dt_seconds)
        takeovers.update(entry.autopilot_engaged)

        return {
            "corner_count": counter.corner_count,
            "peak_lateral_g": counter.peak_lateral_g,
            "last_corner_peak_g": counter.last_corner_peak_g,
            # display_peak_g (not last_corner_peak_g) is what the friction
            # circle's "peak this corner" readout actually shows -- see
            # CornerCounter.display_peak_g's docstring for why the raw
            # last_corner_peak_g field reads as the PREVIOUS corner's value
            # for the whole duration of the current one.
            "display_peak_g": counter.display_peak_g,
            "hands_free_seconds": hands_free.hands_free_seconds,
            "takeover_count": takeovers.takeover_count,
        }

    return processor


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Driver for FSD showcase overlays: decodes the GPX "
                    "repurposed-tag -> lateral_g/longitudinal_g/"
                    "autopilot_engaged -> derived-metric pipeline and "
                    "composites the real streak-scoreboard visual (or, with "
                    "--widget diagnostic, the original throwaway raw-value "
                    "text overlay). Invoked by tesla_combine.py's "
                    "--fsd-scoreboard flag -- see CLAUDE.md.")
    ap.add_argument("input", type=Path, help="hero camera video (e.g. the "
                    "*-front-combined.mp4 tesla_combine.py already produces)")
    ap.add_argument("output", type=Path, help="output video path")
    ap.add_argument("--gpx", type=Path, required=True,
                    help="GPX written by tesla_gps.write_gpx() with the "
                        "repurposed cad/power/hr tags")
    ap.add_argument("--font", default=None,
                    help="TTF font path (default: auto-detect a system font)")
    ap.add_argument("--ffmpeg-dir", type=Path, default=None,
                    help="directory containing the ffmpeg/ffprobe binaries "
                        "to use (default: PATH)")
    ap.add_argument("--widget", default="scoreboard",
                    choices=["scoreboard", "diagnostic", "friction-circle"],
                    help="which overlay to draw (default: scoreboard). scoreboard: the real "
                        "streak-scoreboard showcase visual. friction-circle: the G-G diagram "
                        "showcase visual. diagnostic: the original "
                        "throwaway raw-value text overlay, kept for debugging.")
    return ap


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    font_path = args.font or find_font()
    try:
        font = load_font(font_path)
    except OSError:
        fatal(f"Unable to load font '{font_path}' - use --font to choose a "
              f"font that is installed.")

    ffmpeg_exe = FFMPEG(location=args.ffmpeg_dir)
    if not ffmpeg_exe.is_installed():
        fatal("Can't start ffmpeg - is it installed?")

    ffmpeg_gopro = FFMPEGGoPro(ffmpeg_exe)

    inputpath = assert_file_exists(args.input)
    # Plain ffprobe under the hood (gopro_overlay/ffmpeg_gopro.py) -- no
    # GoPro-specific metadata track required, same as build_gauge_overlay's
    # use of gopro-dashboard.py's --use-gpx-only + video-input path
    # (tesla_combine.py), which this mirrors directly for identical timing.
    recording = ffmpeg_gopro.find_recording(inputpath)
    dimensions = recording.video.dimension
    duration = recording.video.duration

    external_file = assert_file_exists(args.gpx)
    gpx_timeseries = load_external(external_file, units)
    log(f"GPX file: {gpx_timeseries.min} -> {gpx_timeseries.max} "
        f"({len(gpx_timeseries)} points)")

    # GPX min == video frame 0 -- matches --gauge's existing implicit timing
    # convention exactly (tesla_combine.py's build_route_gpx already re-times
    # every GPS sample onto the grid/hero timeline before writing the GPX), so
    # there's no new timing risk here.
    frame_meta = timeseries_to_framemeta(gpx_timeseries, units, start_date=None,
                                         duration=duration)

    if len(frame_meta) < 1:
        fatal(f"No usable GPX points loaded from {external_file}")

    log(f"Timeseries has {len(frame_meta)} data points")
    log("Decoding FSD telemetry....")
    frame_meta.process(make_axis_decode_processor())
    frame_meta.process(make_stateful_processor())

    output: Path = args.output
    output.unlink(missing_ok=True)
    execution = InProcessExecution()
    ffmpeg = FFMPEGOverlayVideo(
        ffmpeg=ffmpeg_exe,
        input=inputpath,
        output=output,
        overlay_size=dimensions,
        execution=execution,
        creation_time=frame_meta.date_at(frame_meta.min),
    )

    # Draw an overlay frame every 0.1s of video. gopro-dashboard.py's own
    # --use-gpx-only render loop computes a "timelapse_correction" too, but
    # in that branch it's `frame_meta.duration() / video_duration` where
    # `video_duration := frame_meta.duration()` was *already* rebound a few
    # lines earlier (gopro-dashboard.py's args.use_gpx_only branch) -- so
    # upstream's ratio is 1.0 by construction, always, regardless of the
    # real probed video length. An earlier version of this driver divided by
    # the real video duration instead, which isn't the same thing: whenever
    # the GPX's own extent falls short of the video (a real SEI gap, or any
    # GPX not run through tesla_combine.py's edge-padding retime_samples),
    # that ratio drops below 1.0 and every FSD value after that point
    # renders at a fraction of real time -- correct-looking numbers, at the
    # wrong video timestamp, with nothing that errors. Caught in review, not
    # by the real-footage verification render (whose retimed+edge-padded GPX
    # happened to span ~the whole video, so the bug's effect was ~0). Fixed
    # by matching upstream's actual behavior -- hardcode 1.0, matching the
    # documented "GPX min == video frame 0" contract -- and warn loudly
    # instead of silently stretching when the GPX materially undershoots the
    # video's real duration.
    coverage_ratio = frame_meta.duration() / duration
    if coverage_ratio < 0.98:
        log(f"WARNING: the GPX covers only {coverage_ratio:.1%} of the video's "
            f"real duration ({frame_meta.duration().millis()/1000:.1f}s of "
            f"{duration.millis()/1000:.1f}s) -- FSD values will freeze at "
            f"their last known state for the uncovered tail rather than "
            f"silently drifting out of sync with it.")
    timelapse_correction = 1.0
    log(f"Timelapse Factor = {timelapse_correction:.3f}")
    stepper = frame_meta.stepper(timeunits(seconds=RENDER_STEP_SECONDS * timelapse_correction))
    progress = ProgressBarProgress("Render")

    overlay = Overlay(framemeta=frame_meta, create_widgets=create_widgets_for(font, args.widget))

    progress.start(len(stepper))
    with ffmpeg.generate() as writer:
        buffer = SingleBuffer(dimensions, (0, 0, 0, 0), writer)
        with buffer:
            for index, dt in enumerate(stepper.steps()):
                progress.update(index)
                buffer.draw(lambda frame: overlay.draw(dt, frame))

    log("Finished drawing frames. waiting for ffmpeg to catch up")
    progress.complete()
    log(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("User interrupted...")
        sys.exit(130)
