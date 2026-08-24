#!/usr/bin/env python3
"""tesla_fsd_overlay.py — driver for FSD showcase overlays.

Wired into tesla_combine.py's CLI as `--fsd-scoreboard` (first of the four
FSD-showcase visuals to actually ship): proves, end to end, that Tesla's
per-frame G-force/autopilot telemetry (SEI -> tesla_gps.write_gpx()'s
repurposed GPX tags -> gopro-overlay's GPX-based pipeline -> tesla_fsd_metrics'
pure decode/derivation -> a widget) can drive a real composited video, and
draws the real "streak scoreboard" visual (StreakScoreboard, --widget
scoreboard, the default) -- an accumulating hands-free/corner/peak-G/takeover
stat line. The three still-deferred showcase ideas (friction circle,
note-highway ribbon, pace-notes) are each their own follow-up branch's job;
FsdDiagnosticText (--widget diagnostic) is kept around, not deleted, for
debugging those. See CLAUDE.md for the full design rationale (axis mapping,
GPX tag repurposing, why gopro-dashboard.py's CLI can't carry any of this on
its own).

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
    showcase visual; "diagnostic" keeps FsdDiagnosticText available (not
    deleted) for future debugging of the three still-deferred FSD-overlay
    ideas, which will want the same kind of raw-value proof-of-plumbing check
    this branch's own verification relied on.
    """
    text_font = font.font_variant(size=22)

    def create(entry):
        if widget == "diagnostic":
            return [FsdDiagnosticText(Coordinate(24, 24), entry, text_font)]
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
    ap.add_argument("--widget", default="scoreboard", choices=["scoreboard", "diagnostic"],
                    help="which overlay to draw (default: scoreboard). scoreboard: the real "
                        "streak-scoreboard showcase visual. diagnostic: the original "
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
    stepper = frame_meta.stepper(timeunits(seconds=0.1 * timelapse_correction))
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
