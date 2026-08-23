#!/usr/bin/env python3
"""
Combine a Tesla Sentry/Dashcam event folder into per-camera videos plus a
labeled multi-camera grid with a burned-in clock.

Expects clips named YYYY-MM-DD_HH-MM-SS-<angle>.mp4 (Tesla's own naming),
angle in: front, back, left_repeater, right_repeater, left_pillar, right_pillar.
Whichever angles are actually present get used -- you don't need all 6.

Usage:
    python3 tesla_combine.py /path/to/event/folder
    python3 tesla_combine.py /path/to/event/folder --trim-start 18:59:00
    python3 tesla_combine.py /path/to/event/folder --trim-start 18:59:00 --trim-end 19:02:00
    python3 tesla_combine.py /path/to/event/folder --speed 2
    python3 tesla_combine.py /path/to/event/folder --feature back      # feature back solo
    python3 tesla_combine.py /path/to/event/folder --feature repeaters # feature both repeaters
    python3 tesla_combine.py /path/to/event/folder --native   # true native res, slow software encode
    python3 tesla_combine.py /path/to/event/folder --blur-faces # auto-blur people's faces (needs `deface`)
    python3 tesla_combine.py /path/to/event/folder --map      # add a live GPS route-map tile (needs gopro-overlay in ./.venv)
    python3 tesla_combine.py /path/to/event/folder --map --map-mag 3   # tighter, navigation-style map view
    python3 tesla_combine.py /path/to/event/folder --map --map-mag 1 --map-zoom 16  # wider, sharper (e.g. highway)
    python3 tesla_combine.py /path/to/event/folder --gauge     # composite a speed/compass dashboard onto the hero tile (needs gopro-overlay in ./.venv)
    python3 tesla_combine.py /path/to/event/folder --gauge --gauge-units kph
    python3 tesla_combine.py /path/to/event/folder --verbose  # raw ffmpeg/deface output instead of the progress display
    python3 tesla_combine.py /path/to/event/folder --dry-run  # print commands, do nothing

While it runs, a two-line display shows which step of how many is going, how far
into it, and an ETA for the whole job. Ctrl-T prints that status on demand;
Ctrl-C stops cleanly (the half-written output is removed and the cameras already
finished are cached, so re-running resumes).

Output (written next to the input folder unless --output-dir is given):
    <session>_<angle>_combined.mp4   -- one lossless concat per camera angle
    <session>_<angle>_blurred.mp4    -- (with --blur-faces) that concat, faces anonymized
    <session>_maptile.mp4            -- (with --map) the standalone live route-map tile
    <session>_<hero-angle>_gauge.mp4 -- (with --gauge) that hero tile, dashboard overlay composited on
    <session>_grid[_feature-X][_blurred][_gauge][_map].mp4 -- labeled multi-camera composite w/ clock
"""

import argparse
import collections
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

SCRIPT_VERSION = "2.4"
# Separate from SCRIPT_VERSION so feature releases can bump the script version
# without invalidating every cached per-camera concat (concat semantics are
# unchanged). Bump this ONLY when the concat output itself would change.
CONCAT_CACHE_VERSION = "2.0"

CAMERA_ANGLES = ["front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar"]
FILENAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(front|back|left_repeater|right_repeater|left_pillar|right_pillar)\.mp4$"
)
LABEL_TEXT = {
    "front": "FRONT", "back": "BACK",
    "left_pillar": "LEFT PILLAR", "right_pillar": "RIGHT PILLAR",
    "left_repeater": "LEFT REPEATER", "right_repeater": "RIGHT REPEATER",
}
HERO_FONT_SIZE = 64
NORMAL_FONT_SIZE = 40
# --landscape's sidebar tiles are ~480-500px wide, much narrower than tall
# mode's ~1265px-wide non-hero rows -- a starting guess, must be checked
# against a real rendered frame and adjusted if labels look clipped,
# oversized, or illegible.
SIDEBAR_FONT_SIZE = 20
# Cameras that are normally paired side-by-side. --feature can name either a
# single angle (solo hero, its ex-partner gets its own row instead of being
# dropped) or one of these pair keywords (both cameras large together as the
# hero row).
PAIR_DEFS = {
    "pillars": ("left_pillar", "right_pillar"),
    "repeaters": ("left_repeater", "right_repeater"),
}
FEATURE_CHOICES = CAMERA_ANGLES + list(PAIR_DEFS.keys())
FONT_CANDIDATES = ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Courier.ttc"]
CACHE_FILE = ".tesla_combine_cache.json"
OUTPUT_FPS = 24

# --- live route-map tile (--map) ---------------------------------------------
# The map is treated as a virtual, label-less tile keyed like a camera angle, so
# it flows through the same grid layout/stacking code as the real cameras. Its
# GPS comes from the SEI telemetry tesla_gps.py decodes out of the front clips.
MAP_TILE_KEY = "map"
# gopro-overlay must load a TTF at startup even though our map-only layout draws
# no text; Arial ships with stock macOS and is known to load cleanly.
MAP_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

# --- gauge dashboard overlay (--gauge) ----------------------------------------
# A dark, semi-transparent rounded panel composited bottom-left onto the hero
# camera tile, holding (left to right) a speedometer dial, a compass, a big
# speed readout, and a recent-speed sparkline chart. Built the same way the
# map tile is: gopro-overlay renders a gopro-overlay XML layout, but for the
# gauge it composites straight onto our own video (see build_gauge_overlay)
# rather than onto a synthetic-size widget layer, so no --overlay-size/scale
# pass is needed here.
#
# Every fraction below is pixel-estimated off the one surviving sample render
# (see the module docstring / CLAUDE.md) -- a starting point that must be
# tuned against a real rendered frame, exactly like SIDEBAR_FONT_SIZE was for
# --landscape.
GAUGE_PANEL_W_FRAC = 0.47   # panel width, as a fraction of the tile width
GAUGE_PANEL_H_FRAC = 0.17   # panel height, as a fraction of the tile height
GAUGE_MARGIN = 24           # px from the tile's bottom-left corner
GAUGE_PAD_FRAC = 0.08       # inner padding, as a fraction of panel height
# Left-to-right width shares of the panel's four sections: dial, compass,
# speed readout, chart.
GAUGE_SECTION_WEIGHTS = (3, 2, 2, 3)


# The live progress display, if one is running. log()/die() consult it so an
# ordinary log line never lands on top of a half-drawn bar.
_PROGRESS = None


def log(msg=""):
    # flush: when stdout is a pipe or a file rather than a terminal, Python
    # block-buffers it, so a long run prints nothing until it finishes --
    # exactly when progress no longer helps.
    if _PROGRESS is not None:
        _PROGRESS.clear()
    print(msg, flush=True)
    if _PROGRESS is not None:
        _PROGRESS.restore()


def die(msg):
    if _PROGRESS is not None:
        _PROGRESS.close()
    print(f"\nERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def human_bytes(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def human_time(sec):
    sec = int(round(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# --- progress reporting -------------------------------------------------------
# Stdlib only, deliberately: this script has no third-party dependencies and a
# progress bar isn't worth adding tqdm/rich for. Nothing here is timer-guesswork --
# every fraction comes from output the child tools already emit (ffmpeg's
# `-progress pipe:1` key=value stream, deface's tqdm frame counter, or, for the
# GPS pass, our own loop counter).

@dataclass
class Step:
    """One unit of work in the job plan. `work` is in seconds of footage (output
    seconds for the grid), the unit every rate below is relative to."""
    kind: str        # concat | blur | map_gps | map_render | map_scale | gauge_render | grid
    label: str
    work: float


# Throughput priors in footage-seconds per wall-clock second, used only until a
# step of that kind finishes and reports its real rate. Ballpark figures from a
# 6-camera, ~32-clips-per-camera run on Apple silicon with the footage on an
# external SSD; being wrong here only makes the first ETA rough.
RATE_PRIORS = {
    "concat": 7.0,       # stream copy, I/O bound
    "blur": 0.6,         # deface CPU face detection -- the slow one
    "map_gps": 5.0,      # demux + SEI decode, per clip
    "map_render": 1.0,   # gopro-overlay route render
    "map_scale": 8.0,    # small libx264 upscale pass
    "gauge_render": 1.0, # gopro-overlay dashboard-panel render + ffmpeg overlay
    "grid": 2.0,         # VideoToolbox hardware encode
}
KIND_LABELS = {"concat": "concat", "blur": "blur", "map_gps": "GPS extract",
               "map_render": "map render", "map_scale": "map upscale",
               "gauge_render": "gauge render", "grid": "grid"}
BAR_FULL, BAR_EMPTY = "█", "░"
BAR_FULL_ASCII, BAR_EMPTY_ASCII = "#", "-"
LABEL_W = 26


class Progress:
    """A job-wide progress display: one line for the running step, one for the
    whole job.

    The job ETA is adaptive. Each step is costed as work/rate[kind], and a kind's
    rate is replaced by the measured one as soon as the first step of that kind
    finishes -- the six per-camera blurs are near-identical, so the estimate
    settles after the first of them rather than staying on a guess for an hour.

    Rendering degrades in two steps: a redrawn two-line display on a terminal, a
    plain appended line every 5%/15s when stdout is a pipe or a log file, and
    nothing at all under --verbose (where the child tools' own output is doing
    the talking).
    """

    def __init__(self, steps, stream=None, ansi=None, verbose=False,
                 now=time.monotonic):
        self.steps = list(steps)
        self.state = ["pending"] * len(self.steps)
        self.stream = stream if stream is not None else sys.stdout
        self.verbose = verbose
        if ansi is None:
            ansi = bool(getattr(self.stream, "isatty", lambda: False)())
        self.ansi = bool(ansi) and not verbose
        self.now = now
        self.rates = dict(RATE_PRIORS)
        self.i = -1
        self.frac = 0.0
        self.speed = None
        self.determinate = True
        self.out = None           # file the running step is writing, if any
        self.closed = False
        self.t_job = now()
        self.t_step = self.t_job
        self.drawn = 0            # lines currently on screen
        self.last_draw = 0.0
        self.last_plain = 0.0
        self.plain_frac = -1.0
        uni = self._supports_unicode()
        self.full = BAR_FULL if uni else BAR_FULL_ASCII
        self.empty = BAR_EMPTY if uni else BAR_EMPTY_ASCII
        self.sep = "·" if uni else "|"
        self.cut = "…" if uni else "~"

    # -- plan/cost math --------------------------------------------------------

    def _supports_unicode(self):
        """Block-drawing characters unless the stream can't encode them (writing
        one that it can't would raise mid-render and kill the run)."""
        enc = getattr(self.stream, "encoding", None) or "ascii"
        try:
            (BAR_FULL + BAR_EMPTY + "·…").encode(enc)
        except (LookupError, UnicodeEncodeError):
            return False
        return True

    def _cost(self, step):
        return step.work / max(1e-6, self.rates.get(step.kind, 1.0))

    def remaining(self):
        """Estimated wall-clock seconds left in the whole job."""
        rem = sum(self._cost(s) for j, s in enumerate(self.steps)
                  if self.state[j] == "pending")
        if 0 <= self.i < len(self.steps) and self.state[self.i] == "running":
            cost = self._cost(self.steps[self.i])
            spent = self.now() - self.t_step
            rem += max(0.0, cost * (1.0 - self.frac) if self.determinate
                       else cost - spent)
        return rem

    def job_fraction(self):
        elapsed = self.now() - self.t_job
        rem = self.remaining()
        return elapsed / (elapsed + rem) if elapsed + rem > 0 else 0.0

    def _advance(self, kind, label, work=0.0):
        """Move to the next pending step of `kind`. Falls back to appending one so
        an unplanned step (or a plan/execution mismatch) still displays."""
        for j in range(self.i + 1, len(self.steps)):
            if self.steps[j].kind == kind and self.state[j] == "pending":
                self.i = j
                if label:
                    self.steps[j].label = label
                return
        self.steps.append(Step(kind, label or kind, work))
        self.state.append("pending")
        self.i = len(self.steps) - 1

    # -- step lifecycle --------------------------------------------------------

    def begin(self, kind, label, work=0.0, determinate=True, out=None):
        """Start a step. `out` is the file it is writing, remembered so an
        interrupted run can clean up the half-written thing it leaves behind."""
        self._advance(kind, label, work)
        self.state[self.i] = "running"
        self.out = out
        self.frac, self.speed, self.determinate = 0.0, None, determinate
        self.t_step = self.now()
        self.plain_frac = -1.0
        if self.verbose:
            log(f"\n== [{self.i + 1}/{len(self.steps)}] {label} ==")
        else:
            self.redraw(force=True)

    def update(self, frac=None, speed=None):
        if frac is not None:
            # A real number retracts an earlier give-up: a slow starter (deface
            # spends minutes opening a multi-hour input before its first tqdm
            # tick) must not be stuck on elapsed-time for the rest of the step.
            self.determinate = True
            # Totals can be estimates; never let a step sit at a smug 100% while
            # it's still running, and never go backwards.
            self.frac = max(self.frac, min(0.999, max(0.0, frac)))
        if speed is not None:
            self.speed = speed
        self.redraw()

    def indeterminate(self):
        """Give up on a percentage for this step and show elapsed time instead --
        for a tool that turned out not to report anything we can parse."""
        self.determinate = False

    def end(self, work=None):
        """Finish the running step. `work` corrects the amount of work it turned
        out to be (a probed duration beats an estimate), so the rate learned from
        it -- and every remaining ETA -- is based on what really happened."""
        if self.i < 0:
            return
        step = self.steps[self.i]
        if work:
            step.work = work
        spent = self.now() - self.t_step
        # Recalibrate this kind's rate from what actually happened. Anything
        # shorter than a blink is too noisy to learn from, and a rate that high
        # rounds the rest of that kind down to free anyway.
        if spent > 0.25 and step.work > 0:
            self.rates[step.kind] = min(10_000.0, step.work / spent)
        self.state[self.i] = "done"
        self.frac = 1.0
        self.out = None
        if not self.verbose:
            # The finished step scrolls away as a one-liner, so the run leaves a
            # readable history of what took how long.
            rate = (f" ({step.work / spent:.1f}x realtime)"
                    if self.determinate and step.work > 0 and spent > 0.25 else "")
            log(f"[{self.i + 1}/{len(self.steps)}] {step.label} -- done in "
                f"{human_time(spent)}{rate}")

    def rescale_pending(self, ratio):
        """Apply a correction factor to every step not yet started. The footage
        estimate is shared by every camera, so one measured concat fixes the lot.
        Absurd ratios are ignored -- they'd mean the estimate wasn't comparable."""
        if not 0.1 < ratio < 10.0:
            return
        for j, s in enumerate(self.steps):
            if self.state[j] == "pending":
                s.work *= ratio

    def abandon(self, *kinds):
        """Silently drop every pending step of these kinds -- work the run has
        just determined will never happen (e.g. no GPS, so no map to render)."""
        for j, s in enumerate(self.steps):
            if s.kind in kinds and self.state[j] == "pending":
                self.state[j] = "skipped"

    def skip(self, kind, label, message):
        """Mark a planned step as not needed (a cache hit), dropping its weight
        from the job estimate so the ETA reflects the work actually left."""
        self._advance(kind, label)
        self.state[self.i] = "skipped"
        log(f"[{self.i + 1}/{len(self.steps)}] {message}")

    # -- rendering -------------------------------------------------------------

    def _bar(self, frac, width):
        filled = int(round(max(0.0, min(1.0, frac)) * width))
        return self.full * filled + self.empty * (width - filled)

    def _lines(self, width):
        """The display as a list of strings -- kept separate from the writing so
        it can be tested without a terminal."""
        step = self.steps[self.i]
        head = f"[{self.i + 1:>2}/{len(self.steps)}] "
        label = (step.label if len(step.label) <= LABEL_W
                 else step.label[:LABEL_W - 1] + self.cut)
        spent = self.now() - self.t_step
        if self.determinate:
            speed = f"  {self.speed:.1f}x" if self.speed else ""
            eta = self._step_eta(spent)
            tail = f" {int(self.frac * 100):3d}%{speed}  ETA {eta}"
        else:
            tail = f"  {human_time(spent)} elapsed"
        job_tail = (f" {int(self.job_fraction() * 100):3d}%  elapsed "
                    f"{human_time(self.now() - self.t_job)} {self.sep} "
                    f"ETA ~{human_eta(self.remaining())}")

        # Fit to the terminal by giving up the least useful thing first: the
        # label's padding, then the bar. The numbers on the right are the point of
        # the display, so they never get truncated away on a narrow window.
        avail = max(0, width - len(head) - max(len(tail), len(job_tail)))
        if avail >= LABEL_W + 9:
            label_w, bar_w = LABEL_W, min(22, avail - LABEL_W - 1)
        elif avail >= 23:
            label_w, bar_w = avail - 11, 10
        else:
            label_w, bar_w = avail, 0
        gap = " " if bar_w else ""
        label = label[:label_w]
        step_bar = self._bar(self.frac, bar_w) if self.determinate else " " * bar_w
        return [
            f"{head}{label:<{label_w}}{gap}{step_bar}{tail}",
            f"{' ' * len(head)}{'job':<{label_w}}{gap}"
            f"{self._bar(self.job_fraction(), bar_w)}{job_tail}",
        ]

    def _step_eta(self, spent):
        if self.frac <= 0.01:
            return "--"
        return human_eta(spent * (1.0 - self.frac) / self.frac)

    def _plain_line(self):
        step = self.steps[self.i]
        # Nothing measured yet (a step that just started, or --verbose, where the
        # child isn't reporting to us at all): elapsed time beats a fake 0%.
        pct = (f"{int(self.frac * 100)}%" if self.determinate and self.frac > 0
               else f"{human_time(self.now() - self.t_step)} elapsed")
        return (f"[{self.i + 1}/{len(self.steps)}] {step.label}  {pct}"
                f"  {self.sep}  job {int(self.job_fraction() * 100)}%"
                f" {self.sep} ETA ~{human_eta(self.remaining())}")

    def clear(self):
        if not self.ansi or not self.drawn:
            return
        # Up N lines, then erase everything from here down.
        self.stream.write(f"\r\x1b[{self.drawn}A\x1b[J")
        self.stream.flush()
        self.drawn = 0

    def restore(self):
        """Redraw after something else printed a line. Only the in-place display
        needs restoring -- in plain mode, re-printing the same status after every
        log line is just noise."""
        if self.ansi:
            self.redraw(force=True)

    def redraw(self, force=False):
        if self.closed or self.verbose or self.i < 0 or self.state[self.i] != "running":
            return
        t = self.now()
        if self.ansi:
            if not force and t - self.last_draw < 0.2:
                return
            self.last_draw = t
            width = shutil.get_terminal_size((100, 24)).columns
            lines = self._lines(width - 1)
            self.clear()
            self.stream.write("".join(line[:width - 1] + "\n" for line in lines))
            self.stream.flush()
            self.drawn = len(lines)
            return
        # No terminal to redraw in: append a line at most every 15s, or whenever
        # the step advances another 5%, so piped logs stay readable.
        if not force and t - self.last_plain < 15.0 and self.frac - self.plain_frac < 0.05:
            return
        self.last_plain, self.plain_frac = t, self.frac
        self.stream.write(self._plain_line() + "\n")
        self.stream.flush()

    def running_step(self):
        if 0 <= self.i < len(self.steps) and self.state[self.i] == "running":
            return self.steps[self.i]
        return None

    def status_line(self):
        """One line saying where the run is, for printing on demand (Ctrl-T) --
        including under --verbose, where there's no bar to look at."""
        if self.running_step() is None:
            done = sum(1 for s in self.state if s in ("done", "skipped"))
            return f"[{done}/{len(self.steps)}] between steps"
        return self._plain_line()

    def close(self):
        """The display is done for good -- anything printed from here on is plain
        output (the STATS block, an interrupt report, an error)."""
        self.clear()
        self.closed = True


def parse_ffmpeg_progress(line):
    """One `key=value` line of ffmpeg's `-progress` stream -> (key, value), with
    the values we care about coerced: out_time_us to seconds, speed to a float.
    Returns (None, None) for anything unusable (ffmpeg emits 'N/A' early on)."""
    key, sep, value = line.strip().partition("=")
    if not sep:
        return None, None
    value = value.strip()
    if key in ("out_time_us", "out_time_ms"):
        # out_time_ms is misnamed in ffmpeg -- it holds microseconds too.
        try:
            return "out_time", int(value) / 1_000_000.0
        except ValueError:
            return None, None
    if key == "out_time":
        # The same instant again as HH:MM:SS.ffffff. Normalize it to seconds so
        # every "out_time" this returns is a number, whichever line it came from.
        try:
            h, m, s = value.split(":")
            return "out_time", int(h) * 3600 + int(m) * 60 + float(s)
        except ValueError:
            return None, None
    if key == "speed":
        try:
            return "speed", float(value.rstrip("x"))
        except ValueError:
            return None, None
    return key, value


TQDM_COUNT_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*\[")


def parse_tqdm_fraction(text):
    """Pull a completed fraction out of a tqdm bar such as
    ` 12%|###  | 5500/45800 [01:23<10:11, 66.0it/s]` (deface's progress).
    Returns None when the chunk holds no counter."""
    m = None
    for m in TQDM_COUNT_RE.finditer(text):
        pass  # the last match in the chunk is the most recent update
    if not m:
        return None
    done, total = int(m.group(1)), int(m.group(2))
    if total <= 0:
        return None
    return min(1.0, done / total)


def human_eta(sec):
    """Coarser than human_time, for estimates: an ETA of "1h 09m 47s" is false
    precision, and the seconds field churns distractingly while you watch it."""
    if sec < 600:
        return human_time(sec)
    minutes = int(round(sec / 60.0))
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def hero_angles_for(feature):
    """The angle(s) making up the hero row/tile for a --feature choice: both
    cameras of a PAIR_DEFS pair (e.g. 'repeaters'), or a single-item list for
    a solo angle. Shared by build_rows, landscape_layout, and the --gauge
    hero-tile lookup in build_grid -- one place for an expression all three
    would otherwise duplicate."""
    return list(PAIR_DEFS[feature]) if feature in PAIR_DEFS else [feature]


def build_rows(present_angles, feature):
    """
    Lay out rows top to bottom given which camera angles are actually present
    and which one (or pair) is featured as the large hero row.

    feature: a single angle name (solo hero) or a PAIR_DEFS key (both cameras
    of that pair large side-by-side as the hero row).

    Any camera whose pair-partner got pulled out for a solo feature (e.g.
    --feature left_pillar orphans right_pillar) still gets its own row rather
    than being dropped from the grid.

    Returns (rows, hero_angles) -- hero_angles is the list of angles making up
    the hero row, used elsewhere to decide label font size and whether a row
    needs upscaling instead of padding.
    """
    hero_angles = hero_angles_for(feature)
    remaining = [a for a in present_angles if a not in hero_angles]

    rows = []
    used = set()
    for l, r in PAIR_DEFS.values():
        if l in remaining and r in remaining:
            rows.append([l, r])
            used.update([l, r])
    for a in remaining:
        if a not in used:
            rows.append([a])
            used.add(a)

    if rows:
        rows = [rows[0], hero_angles] + rows[1:]
    else:
        rows = [hero_angles]
    return rows, hero_angles


def inject_map_row(rows, hero_angles=()):
    """Slot the live route-map tile into the grid layout.

    Prefer pairing it with a solo BACK row -- the map fills space that row would
    otherwise pad with black bars, and no camera is occluded. If back isn't a
    plain solo row (absent, or itself the featured hero -- where pairing would
    suppress the hero upscale and leave back small), the map gets its own row at
    the bottom. Mutates and returns `rows`.
    """
    for row in rows:
        if row == ["back"] and "back" not in hero_angles:
            row.append(MAP_TILE_KEY)
            return rows
    rows.append([MAP_TILE_KEY])
    return rows


def printable_cmd(cmd):
    return " ".join(f"'{c}'" if " " in c else c for c in cmd)


def run(cmd, dry_run=False, what="ffmpeg", progress=None, total=None):
    """Run a child tool, dying with its own error text if it fails.

    Without an active `progress` (so: --verbose, --dry-run, or any caller that
    doesn't track progress) this is the original path -- echo the command, let the
    child write straight to the terminal.

    With one, the child goes quiet so it can't scribble over the progress display,
    and drives it instead:
      * `total` given -> an ffmpeg command; ask it for a `-progress` stream and
        track the output time against `total` seconds;
      * `total` None  -> some other tool; track elapsed time only.
    Quiet never means silent on failure: the tail of the child's captured output
    is printed before dying.
    """
    if progress is None or progress.verbose or dry_run:
        log(f"$ {printable_cmd(cmd)}")
        if dry_run:
            return
        r = subprocess.run(cmd)
        if r.returncode != 0:
            die(f"{what} failed with exit code {r.returncode}. Its own output is above -- "
                f"that message usually says exactly what went wrong.")
        return

    if total is not None:
        cmd = [cmd[0], "-nostdin", "-loglevel", "error", "-nostats",
               "-progress", "pipe:1"] + list(cmd[1:])
        _run_ffmpeg_tracked(cmd, what, progress, total)
    else:
        _run_tracked(cmd, what, progress)


def reap(proc, grace=5.0):
    """Make sure a child is gone before we are. On Ctrl-C the terminal already
    sent SIGINT to the whole foreground group, so the child is usually on its way
    out -- this just waits for it, and insists if it isn't."""
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


def fail_child(cmd, what, code, tail):
    """Report a child that failed while its output was being captured: the command,
    the tail of what it printed, then the usual die()."""
    log(f"$ {printable_cmd(cmd)}")
    if tail:
        log("--- last output from " + what + " ---")
        for line in tail:
            log(line)
        log("--- end of output ---")
    die(f"{what} failed with exit code {code}. Its own output is above -- "
        f"that message usually says exactly what went wrong. "
        f"(Re-run with --verbose to watch it live.)")


def _run_ffmpeg_tracked(cmd, what, progress, total):
    """ffmpeg with `-progress pipe:1`: parse the key=value stream off stdout,
    park stderr in a temp file so a chatty encoder can't fill a pipe and deadlock."""
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                text=True, bufsize=1)
        try:
            for line in proc.stdout:
                key, value = parse_ffmpeg_progress(line)
                if key == "out_time" and total > 0:
                    progress.update(frac=value / total)
                elif key == "speed":
                    progress.update(speed=value)
            code = proc.wait()
        except KeyboardInterrupt:
            reap(proc)
            raise
        if code != 0:
            errf.seek(0)
            fail_child(cmd, what, code, errf.read().splitlines()[-30:])


def _run_tracked(cmd, what, progress, parse=None, drop=()):
    """Run a non-ffmpeg tool with its output captured, optionally deriving a
    completed fraction from it (deface's tqdm counter). Output arrives in
    \\r-separated bursts, so read raw chunks rather than lines.

    `drop` lists substrings of lines not worth keeping for the failure tail
    (progress redraws, known-harmless warnings)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tail, residual, saw_frac = collections.deque(maxlen=30), "", False
    t0 = progress.now()
    while True:
        try:
            chunk = proc.stdout.read1(4096)
        except KeyboardInterrupt:
            reap(proc)
            raise
        if not chunk:
            break
        text = residual + chunk.decode("utf-8", "replace")
        pieces = re.split(r"[\r\n]", text)
        residual = pieces.pop()
        if parse is not None:
            frac = parse(text)
            if frac is not None:
                saw_frac = True
                progress.update(frac=frac)
        for piece in pieces:
            piece = piece.strip()
            if piece and not any(d in piece for d in drop):
                tail.append(piece)
        # Some tools report nothing parseable at all; after a grace period stop
        # pretending we know how far along it is and just show elapsed time.
        if parse is not None and not saw_frac and progress.now() - t0 > 20:
            progress.indeterminate()
        progress.redraw()
    if residual.strip():
        tail.append(residual.strip())
    code = proc.wait()
    if code != 0:
        fail_child(cmd, what, code, list(tail))


def find_ffmpeg():
    """
    Homebrew's plain `ffmpeg` formula ships WITHOUT libfreetype, so it has no
    `drawtext` filter -- labels/timestamp need `ffmpeg-full` (a separate,
    unlinked formula) instead. Prefer whichever binary actually has drawtext.
    """
    candidates = [
        "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
        "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
        shutil.which("ffmpeg-full"),
        shutil.which("ffmpeg"),
    ]
    fallback = None
    for c in candidates:
        if not c or not Path(c).exists():
            continue
        try:
            r = subprocess.run([c, "-filters"], capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        if fallback is None:
            fallback = c
        if "drawtext" in r.stdout:
            return c, True
    if fallback:
        log(f"WARNING: no ffmpeg with drawtext found; using {fallback} without labels/timestamp.")
        return fallback, False
    die("No ffmpeg binary found on this machine. Install it with: brew install ffmpeg-full")


def filter_graph_args(ffmpeg, filter_path):
    """
    ffmpeg args that read a filter_complex graph in from a file.

    Reading the graph from a file sidesteps shell quoting entirely. ffmpeg 9
    dropped -filter_complex_script in favour of the generic "read this option's
    value from a file" spelling, -/filter_complex, so use whichever this binary
    actually understands.
    """
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-h", "full"],
                           capture_output=True, text=True, timeout=30)
        legacy = "filter_complex_script" in r.stdout
    except Exception:
        legacy = False
    return ["-filter_complex_script" if legacy else "-/filter_complex", str(filter_path)]


def find_font():
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return f
    die("No usable font found; edit FONT_CANDIDATES in this script.")


def find_deface():
    """The `deface` face-anonymizer (https://github.com/ORB-HD/deface) ships as a
    console script. It's an optional dependency -- only needed for --blur-faces --
    so we look it up lazily rather than requiring it for a normal run.

    `pip install --user deface` drops the launcher in a per-user scripts dir
    (e.g. ~/Library/Python/3.x/bin on macOS) that often ISN'T on PATH, so
    shutil.which misses it. Fall back to the known user-base bin before giving up."""
    found = shutil.which("deface")
    if found:
        return found
    import site
    for base in filter(None, [site.getuserbase(), sys.prefix]):
        cand = Path(base) / "bin" / "deface"
        if cand.exists():
            return str(cand)
    return None


def deface_video(deface_bin, in_path, out_path, mode, thresh, scale, dry_run,
                 progress=None):
    """
    Run deface over one video, writing an anonymized copy. deface detects faces
    per frame (CenterFace net) and replaces each with a blur/solid box/mosaic.

    Runs on CPU by default (no GPU needed) and re-encodes the whole file, so it's
    the slow part of a --blur-faces run. --scale downsamples ONLY the detection
    pass (output stays full-res), which is the main speed lever on Tesla's
    1280x960 cameras. deface drops audio, which is moot here -- Tesla clips are
    silent and the grid is built with -an anyway.
    """
    cmd = [deface_bin, str(in_path), "-o", str(out_path),
           "--thresh", str(thresh), "--replacewith", mode]
    if scale:
        cmd += ["--scale", scale]
    if progress is None or progress.verbose or dry_run:
        run(cmd, dry_run, what="deface")
        return
    # deface drives a tqdm bar over the frame count, which is an exact progress
    # source -- better than anything we could estimate. Its own redraws and
    # imageio's macro-block warning are noise once we're rendering the bar.
    _run_tracked(cmd, "deface", progress, parse=parse_tqdm_fraction,
                 drop=("it/s]", "IMAGEIO", "?it/s"))


def find_map_tooling(script_dir):
    """Locate the gopro-overlay CLI installed in the sibling .venv (created by
    `python3.12 -m venv .venv && ./.venv/bin/python -m pip install gopro-overlay`).
    Returns (venv_python, gopro_script, missing_paths)."""
    venv_py = script_dir / ".venv" / "bin" / "python"
    gopro = script_dir / ".venv" / "bin" / "gopro-dashboard.py"
    missing = [str(p) for p in (venv_py, gopro) if not p.exists()]
    return venv_py, gopro, missing


def find_map_font():
    for f in MAP_FONT_CANDIDATES:
        if Path(f).exists():
            return f
    # gopro only needs *a* loadable TTF -- the map-only layout has no text.
    return find_font()


def write_map_layout(path, tile_w, tile_h, zoom=18, line_width=6):
    """Write a full-bleed gopro-overlay layout holding a single
    moving_journey_map -- a map that follows the car AND draws the whole route
    trace. The widget is square (size x size), so we render it at the tile's long
    edge and translate it up/left so its centre band fills the cell edge-to-edge
    with the car marker centred."""
    size = max(tile_w, tile_h)
    xoff = -((size - tile_w) // 2)
    yoff = -((size - tile_h) // 2)
    Path(path).write_text(
        "<layout>\n"
        f'    <translate x="{xoff}" y="{yoff}">\n'
        f'        <component type="moving_journey_map" name="route" '
        f'size="{size}" zoom="{zoom}" line-width="{line_width}"/>\n'
        "    </translate>\n"
        "</layout>\n"
    )


def write_gauge_layout(path, tile_w, tile_h, units):
    """Write a gopro-overlay layout holding the --gauge dashboard panel: a
    dark rounded box positioned bottom-left over the tile, with (left to
    right) a speedometer dial (msi), a compass, a big speed readout + unit
    label, and a recent-speed sparkline chart.

    `units` is "mph" or "kph" -- passed straight through as each gopro-overlay
    component's own `units=` attribute (Converters recognizes both directly),
    so no --units-speed flag on the gopro-dashboard.py invocation is needed.

    Panel/section sizing is entirely in terms of tile_w/tile_h (see the
    GAUGE_* fractions above), so this works unchanged for both the tall
    grid's full-width hero row and landscape's native-res hero block -- no
    orientation-specific logic needed here either.

    gopro-overlay's msi/compass components don't accept x/y attributes of
    their own (unlike metric/text/chart, which do) -- they're positioned by
    wrapping each in a <translate>, the same mechanism write_map_layout
    already uses for moving_journey_map.
    """
    panel_w = max(2, round(tile_w * GAUGE_PANEL_W_FRAC))
    panel_h = max(2, round(tile_h * GAUGE_PANEL_H_FRAC))
    panel_x = GAUGE_MARGIN
    panel_y = tile_h - panel_h - GAUGE_MARGIN

    pad = max(4, round(panel_h * GAUGE_PAD_FRAC))
    inner_h = max(2, panel_h - 2 * pad)
    dial = inner_h  # msi/compass are square, sized to the panel's inner height

    inner_w = max(2, panel_w - 2 * pad)
    total_weight = sum(GAUGE_SECTION_WEIGHTS)
    dial_w, compass_w, speed_w, _ = (
        max(2, round(inner_w * w / total_weight)) for w in GAUGE_SECTION_WEIGHTS
    )

    msi_x = pad
    compass_x = msi_x + dial_w
    speed_x = compass_x + compass_w
    chart_x = speed_x + speed_w
    chart_w = max(2, panel_w - pad - chart_x)

    speed_size = max(8, round(inner_h * 0.55))
    unit_size = max(6, round(inner_h * 0.22))

    Path(path).write_text(
        "<layout>\n"
        f'    <frame x="{panel_x}" y="{panel_y}" width="{panel_w}" height="{panel_h}" '
        f'cr="{pad}" bg="0,0,0,180">\n'
        f'        <translate x="{msi_x}" y="{pad}">\n'
        f'            <component type="msi" size="{dial}" metric="speed" '
        f'units="{units}" needle="1"/>\n'
        "        </translate>\n"
        f'        <translate x="{compass_x}" y="{pad}">\n'
        f'            <component type="compass" size="{dial}"/>\n'
        "        </translate>\n"
        f'        <component type="metric" x="{speed_x}" y="{pad}" metric="speed" '
        f'units="{units}" dp="0" size="{speed_size}"/>\n'
        f'        <component type="text" x="{speed_x}" y="{pad + speed_size + 4}" '
        f'size="{unit_size}">{units.upper()}</component>\n'
        f'        <component type="chart" x="{chart_x}" y="{pad}" metric="speed" '
        f'units="{units}" width="{chart_w}" height="{inner_h}" seconds="30"/>\n'
        "    </frame>\n"
        "</layout>\n"
    )


def retime_samples(clip_samples, clip_fps, clip_durations, offset, grid_dur):
    """Re-time per-clip GPS samples onto the CONCATENATED grid timeline.

    Pure arithmetic/filtering extracted from build_map_tile so it can be tested
    in isolation. Given each clip's already-extracted samples (dicts carrying
    frame_index/lat/lon and optional speed_mps/heading), that clip's fps, and its
    already-resolved duration, place every sample at
    time = (sum of prior clip durations) + frame_index/fps -- minus the trim
    `offset` pre-applied to clip 0's concat -- and drop anything outside the
    [0, grid_dur] window. The first/last known positions are then held out to the
    window edges (the tail pad runs slightly long so vstack's shortest=1 trims
    the map to the cameras, never the reverse).

    `clip_samples`, `clip_fps` and `clip_durations` are parallel lists (one entry
    per clip). Returns the re-timed sample list, or [] if nothing lands in-window.
    Times are datetimes anchored to a fixed base epoch (their spacing is what
    matters downstream, not the absolute value).
    """
    base = datetime(2000, 1, 1, tzinfo=timezone.utc)
    retimed = []
    concat_start = -offset  # clip 0 was pre-trimmed by `offset` in the concat
    for samples, fps, dur in zip(clip_samples, clip_fps, clip_durations):
        for s in samples:
            ct = concat_start + s["frame_index"] / fps
            if ct < -0.001 or ct > grid_dur + 0.001:
                continue  # outside the trim window
            retimed.append({
                "lat": s["lat"], "lon": s["lon"],
                "speed_mps": s.get("speed_mps"), "heading": s.get("heading"),
                "time": base + timedelta(seconds=max(0.0, ct)),
            })
        concat_start += dur

    if not retimed:
        return []

    # Hold the first/last known position out to the window edges so the rendered
    # map spans the whole grid (the car sat still where SEI is absent). The tail
    # pad runs slightly long so vstack's shortest=1 trims the map to the cameras,
    # never the other way round.
    if (retimed[0]["time"] - base).total_seconds() > 0.05:
        retimed.insert(0, {**retimed[0], "time": base})
    if grid_dur - (retimed[-1]["time"] - base).total_seconds() > 0.05:
        retimed.append({**retimed[-1],
                        "time": base + timedelta(seconds=grid_dur + 0.5)})
    return retimed


def build_route_gpx(source_clips, offset, grid_dur, ffmpeg, ffprobe, out_gpx_path,
                    dry_run, progress):
    """Extract GPS from the ORIGINAL source clips (SEI lives in the source
    bitstream; concat/blurred outputs don't carry it) and write it as a GPX
    re-timed onto the CONCATENATED grid timeline. Returns out_gpx_path, or
    None if the selected clips carry no GPS/SEI telemetry.

    Shared by build_map_tile and build_gauge_overlay's caller in build_grid,
    so requesting --map --gauge together extracts and retimes GPS ONCE, not
    twice -- a real cost (minutes on a long drive per CLAUDE.md).

    (Two params beyond the ones named in the original design sketch --
    `ffmpeg`/`ffprobe` -- are needed here: tesla_gps.extract_samples and
    probe_duration can't run without them.)

    Re-timing is the crux of keeping either overlay in sync with the grid. The
    grid concatenates clips and squeezes out recording gaps, so a wall-clock
    placement would drift apart from the footage. Instead each GPS sample is
    placed at time = (sum of prior clip durations) + frame_index/fps -- using
    the per-frame provenance tesla_gps emits (see retime_samples).
    """
    import tesla_gps  # local import: only needed for --map/--gauge, keeps base runs lean

    if dry_run:
        log("== would extract GPS from the source clips, re-time onto the grid "
            "timeline, and write a GPX ==")
        return out_gpx_path

    progress.begin("map_gps", "GPS extract", grid_dur)
    # Gather each clip's samples/fps/duration (the ffmpeg/tesla_gps side effects),
    # then hand the plain data to retime_samples for the pure re-timing math.
    clip_samples, clip_fps, clip_durs = [], [], []
    for idx, clip in enumerate(source_clips):
        progress.update(frac=idx / max(1, len(source_clips)))
        clip_samples.append(tesla_gps.extract_samples(str(clip), ffmpeg, ffprobe))
        clip_fps.append(tesla_gps.probe_fps(str(clip), ffprobe))
        dur = probe_duration(ffprobe, clip)
        if dur is None:
            # Never silently add 0 -- that would shift every later clip's GPS
            # earlier on the concat timeline while its frames still play, quietly
            # desyncing the overlay. Estimate from the next clip's filename start
            # (else a nominal 60s) and say so.
            nxt = source_clips[idx + 1] if idx + 1 < len(source_clips) else None
            here = tesla_gps.parse_clip_time(str(clip))
            there = tesla_gps.parse_clip_time(str(nxt)) if nxt else None
            dur = (there - here).total_seconds() if (here and there) else 60.0
            log(f"WARNING: could not probe {Path(clip).name}; estimating "
                f"{dur:.1f}s to keep the overlay in sync.")
        clip_durs.append(dur)

    retimed = retime_samples(clip_samples, clip_fps, clip_durs, offset, grid_dur)
    progress.end()

    if not retimed:
        log("== the selected clips carry no GPS/SEI telemetry -- skipping "
            "--map/--gauge; the grid is built without them ==")
        return None

    tesla_gps.write_gpx(retimed, str(out_gpx_path), track_name="Tesla route",
                        tz=timezone.utc)
    log(f"== wrote {len(retimed)}-point route GPX ==")
    return out_gpx_path


def build_map_tile(gpx_path, grid_dur, tile_dims, ffmpeg, venv_py, gopro_script,
                   font, zoom, mag, out_path, tmpdir, dry_run, progress):
    """Render the live route-map tile from an already-built GPX (see
    build_route_gpx). Returns out_path.

    The map is rendered at 1x; the grid's own speed/fps filter then scales
    every tile (map included) together, so they stay locked.
    """
    tile_w, tile_h = tile_dims
    layout_path = tmpdir / "map_layout.xml"
    # To zoom tighter than OSM's max tile zoom (19), render fewer map pixels -- a
    # smaller ground area at the same zoom -- and upscale to fill the tile: a
    # `mag`x magnification. mag == 1 renders straight to the tile at native
    # sharpness; higher mag is a tighter, navigation-style view but softer.
    magnifying = bool(mag) and mag != 1.0
    render_w = max(2, round(tile_w / mag / 2) * 2) if magnifying else tile_w
    render_h = max(2, round(tile_h / mag / 2) * 2) if magnifying else tile_h
    write_map_layout(layout_path, render_w, render_h, zoom=zoom)

    gopro_out = (tmpdir / "map_pre.mp4") if magnifying else out_path
    gopro_cmd = [
        str(venv_py), str(gopro_script), "--use-gpx-only", "--gpx", str(gpx_path),
        "--overlay-size", f"{render_w}x{render_h}", "--generate", "default",
        "--map-style", "osm", "--layout", "xml", "--layout-xml", str(layout_path),
        "--font", font, "--bg", "0,0,0,255",
        "--ffmpeg-dir", str(Path(ffmpeg).parent), str(gopro_out),
    ]
    # Upscale the magnified render back to the full tile so it still hstacks with
    # the back camera. libx264 (software) is fine -- the tile is small and this
    # is a one-off scale pass.
    scale_cmd = [ffmpeg, "-y", "-i", str(gopro_out), "-vf",
                 f"scale={tile_w}:{tile_h}:flags=bicubic", "-c:v", "libx264",
                 "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                 str(out_path)]

    if dry_run:
        log("== [--map] would render the route-map tile from the extracted GPX"
            f"{f' ({mag}x magnified)' if magnifying else ''} ==")
        for c in ([gopro_cmd, scale_cmd] if magnifying else [gopro_cmd]):
            printable = " ".join(f"'{a}'" if " " in a else a for a in c)
            log(f"$ {printable}")
        return out_path

    log(f"== [--map] rendering route-map tile ({render_w}x{render_h}"
        f"{f' ->{tile_w}x{tile_h} ({mag}x magnified)' if magnifying else ''}) ==")
    # gopro-overlay's runtime tracks the route, not the footage length, and it
    # reports no parseable progress -- show elapsed time rather than a fake bar.
    progress.begin("map_render", "route map render", grid_dur, determinate=False,
                   out=gopro_out)
    run(gopro_cmd, dry_run=False, what="gopro-dashboard (map tile)", progress=progress)
    progress.end()
    if magnifying:
        progress.begin("map_scale", f"map upscale ({mag}x)", grid_dur, out=out_path)
        run(scale_cmd, dry_run=False, what="ffmpeg (map magnify)",
            progress=progress, total=grid_dur)
        progress.end()
    return out_path


def build_gauge_overlay(hero_video_path, gpx_path, tile_dims, units, ffmpeg, venv_py,
                        gopro_script, font, out_path, tmpdir, dry_run, progress):
    """Composite the speed/compass dashboard panel onto the hero camera tile.
    Returns out_path.

    Unlike build_map_tile (which renders a synthetic-size widget layer and
    needs --overlay-size), this hands gopro-dashboard.py the hero video
    itself as its positional `input` argument (immediately followed by
    `output` -- gopro-dashboard.py's argparser has both `input` and `output`
    as bare positionals, and with a run of optional flags in between the two
    tokens it mis-assigns them, e.g. treating a lone leading `input` token as
    satisfying the required `output` positional instead and erroring on the
    trailing path as "unrecognized arguments"; confirmed by running it for
    real. Keeping them adjacent, before any --flags, parses correctly): with
    --use-gpx-only AND a video input, gopro-dashboard reads that video's real
    dimensions/duration itself (find_recording(), a plain ffprobe of the
    video stream -- no GoPro-specific metadata track needed) and runs its own
    internal ffmpeg `[0:v][1:v]overlay` compositing pass (FFMPEGOverlayVideo,
    in gopro_overlay/ffmpeg_overlay.py) -- producing a fully-composited output
    video in one subprocess call. No filter-graph code of our own is
    involved; build_filter/build_filter_landscape never learn a gauge was
    composited -- they just see a different source file at the same
    resolution (angle_paths[hero] is swapped in build_grid before either
    runs).
    """
    tile_w, tile_h = tile_dims
    layout_path = tmpdir / "gauge_layout.xml"
    write_gauge_layout(layout_path, tile_w, tile_h, units=units)

    gopro_cmd = [
        str(venv_py), str(gopro_script), str(hero_video_path), str(out_path),
        "--use-gpx-only", "--gpx", str(gpx_path),
        "--layout", "xml", "--layout-xml", str(layout_path),
        "--font", font, "--ffmpeg-dir", str(Path(ffmpeg).parent),
    ]

    if dry_run:
        log("== [--gauge] would composite the speed/compass dashboard panel "
            "onto the hero camera tile ==")
        printable = " ".join(f"'{a}'" if " " in a else a for a in gopro_cmd)
        log(f"$ {printable}")
        return out_path

    log(f"== [--gauge] compositing dashboard overlay onto {Path(hero_video_path).name} ==")
    # Same as the map render: gopro-overlay reports no parseable progress here.
    progress.begin("gauge_render", "gauge overlay render", determinate=False,
                   out=out_path)
    run(gopro_cmd, dry_run=False, what="gopro-dashboard (gauge overlay)", progress=progress)
    progress.end()
    return out_path


def ffprobe_json(ffprobe, args):
    r = subprocess.run([ffprobe, "-v", "error"] + args + ["-of", "json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def looks_tesla_encrypted(path):
    """True if the clip lacks an ISO-BMFF ftyp box -- the signature of Tesla's
    'Encrypt Dashcam Recordings' output, which is opaque ciphertext."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return len(head) == 12 and head[4:8] != b"ftyp"


TESLA_ENCRYPTED_HINT = (
    'This clip appears to be Tesla-encrypted ("Encrypt Dashcam Recordings" is ON '
    "in the car). Decrypt clips at https://dashcam.tesla.com or in the car's "
    "Dashcam app (select clips, then the padlock button), or turn encryption off: "
    "Controls > Safety > Encrypt Dashcam Recordings. For batch decryption see "
    "https://github.com/XGxF3/tesla-dashcam-decrypt. See -README_en.txt in the "
    "clip folder."
)


def probe_dims(ffprobe, path):
    d = ffprobe_json(ffprobe, ["-select_streams", "v:0",
                               "-show_entries", "stream=width,height", str(path)])
    if not d or not d.get("streams"):
        if looks_tesla_encrypted(path):
            die(f"Could not read {path}\n{TESLA_ENCRYPTED_HINT}")
        die(f"Could not read video dimensions from {path} -- is it a valid mp4?")
    s = d["streams"][0]
    return s["width"], s["height"]


def probe_duration(ffprobe, path):
    """Returns duration in seconds, or None if the file is unreadable/corrupt."""
    d = ffprobe_json(ffprobe, ["-show_entries", "format=duration", str(path)])
    if not d or "format" not in d or "duration" not in d["format"]:
        return None
    try:
        return float(d["format"]["duration"])
    except (TypeError, ValueError):
        return None


def discover_clips(folder):
    """Returns {angle: [(start_datetime, path), ...]} sorted by time, plus session start."""
    by_angle = {a: [] for a in CAMERA_ANGLES}
    all_ts = []
    for p in sorted(folder.glob("*.mp4")):
        m = FILENAME_RE.match(p.name)
        if not m:
            continue
        ts_str, angle = m.groups()
        dt = datetime.strptime(ts_str, "%Y-%m-%d_%H-%M-%S")
        by_angle[angle].append((dt, p))
        all_ts.append(dt)
    by_angle = {a: sorted(v) for a, v in by_angle.items() if v}
    if not by_angle:
        die(f"No Tesla dashcam clips found in {folder}\n"
            f"Expected names like 2026-07-14_18-52-35-front.mp4")
    return by_angle, min(all_ts)


def select_clips(ffprobe, clips, session_start, trim_start, trim_end):
    """
    Pick only the clips overlapping the requested wall-clock window, and work
    out how far into the first of them the window actually begins.

    Tesla names each clip by its wall-clock START, so clip boundaries come
    from filenames for free. Durations only get probed near the window edges,
    which keeps this cheap even on a 3-hour folder.

    Returns (selected_clip_paths, offset_into_first_clip, duration_or_None).
    """
    if trim_start is None and trim_end is None:
        return [p for _, p in clips], 0.0, None

    t0 = session_start + timedelta(seconds=trim_start or 0.0)
    t1 = session_start + timedelta(seconds=trim_end) if trim_end is not None else None

    starts = [dt for dt, _ in clips]
    # A clip's window runs until the next clip starts (its real content may end
    # sooner -- see the gap check below -- but for *selection* erring long is safe).
    selected = []
    for i, (dt, p) in enumerate(clips):
        nxt = starts[i + 1] if i + 1 < len(starts) else None
        if nxt is None:
            dur = probe_duration(ffprobe, p)
            end = dt + timedelta(seconds=dur if dur else 60.0)
        else:
            end = nxt
        if end <= t0:
            continue
        if t1 is not None and dt >= t1:
            break
        selected.append((dt, p))

    if not selected:
        die(f"No clips overlap the requested window "
            f"({t0.strftime('%H:%M:%S')}"
            f"{' to ' + t1.strftime('%H:%M:%S') if t1 else ' onward'}).")

    first_dt, first_path = selected[0]
    offset = (t0 - first_dt).total_seconds()
    if offset < 0:
        offset = 0.0

    # If the requested moment lands in a gap between clips (Tesla recordings do
    # have them), the offset can exceed the first clip's real length. Skip to
    # the next clip and start at its beginning rather than seeking off the end.
    first_dur = probe_duration(ffprobe, first_path)
    if first_dur is not None and offset >= first_dur:
        if len(selected) > 1:
            log(f"NOTE: {t0.strftime('%H:%M:%S')} falls in a gap in the recording; "
                f"starting at the next available clip instead.")
            selected = selected[1:]
            offset = 0.0
        else:
            die(f"{t0.strftime('%H:%M:%S')} is past the end of the recording.")

    duration = None
    if t1 is not None:
        duration = (t1 - (t0 if offset else selected[0][0])).total_seconds()
        if duration <= 0:
            die("--trim-end must be after --trim-start.")

    return [p for _, p in selected], offset, duration


def estimate_concat_seconds(ffprobe, clips, offset, duration):
    """How many seconds of footage one camera's concat will hold -- the total the
    progress display measures ffmpeg's output time against.

    With --trim-end the answer is exact. Otherwise, rather than probing every clip
    (188 of them on a long session, on an external drive), lean on Tesla naming
    each clip by its wall-clock start: clip i's length is at most the gap to clip
    i+1's start, capped at the nominal 60s. Only the last clip has no successor,
    so it costs one probe. Gaps in the recording make this an over-estimate, which
    a completed concat then corrects (see refine_footage_estimate)."""
    if duration:
        return duration
    if not clips:
        return 0.0
    starts = []
    for c in clips:
        m = FILENAME_RE.match(Path(c).name)
        starts.append(datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S") if m else None)
    total = 0.0
    for i in range(len(clips) - 1):
        if starts[i] and starts[i + 1]:
            total += min(60.0, max(0.0, (starts[i + 1] - starts[i]).total_seconds()))
        else:
            total += 60.0
    total += probe_duration(ffprobe, clips[-1]) or 60.0
    return max(1.0, total - (offset or 0.0))


def load_cache(out_dir):
    p = out_dir / CACHE_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(out_dir, cache):
    try:
        (out_dir / CACHE_FILE).write_text(json.dumps(cache, indent=2))
    except OSError:
        pass  # cache is an optimization; never fail the run over it


def concat_key(clips, offset, duration):
    return {
        "v": CONCAT_CACHE_VERSION,
        "clips": [c.name for c in clips],
        "ss": round(offset, 3),
        "t": round(duration, 3) if duration is not None else None,
    }


def blur_key(concat_k, mode, thresh, scale):
    """Cache identity for a blurred file: which concat it came from plus the
    deface settings, so changing --blur-mode/--blur-thresh forces a rebuild."""
    return {"concat": concat_k, "mode": mode, "thresh": thresh, "scale": scale}


def concat_angle(ffmpeg, ffprobe, clips, offset, duration, out_path, tmpdir, dry_run,
                 progress=None, total=None):
    """
    Losslessly stitch one camera's ~1-minute clips into a single file, applying
    the trim window during the concat rather than after it -- so pulling 10
    seconds out of a 3-hour folder doesn't write the whole 3 hours first.

    Seeking here is fiddlier than it looks. Passing -ss to the *concat demuxer*
    makes it copy everything from the seek point to the end of that clip into
    mdat while only indexing the -t window -- a correct-playing but ~4x bloated
    file (measured: 38.9 MB holding 10.6 MB of indexed data). Input -ss on a
    plain file has no such problem, so:
      - a fine offset is applied by pre-trimming ONLY the first clip as a plain
        input, which is both exact and tight;
      - the concat demuxer then runs with no -ss at all.

    The concat list needs ABSOLUTE paths -- relative paths in it resolve
    relative to the list file's own directory, not the shell's cwd.
    """
    # Single clip with a fine offset: plain input seek straight to the output.
    if offset and len(clips) == 1:
        cmd = [ffmpeg, "-y", "-ss", str(offset)]
        if duration:
            cmd += ["-t", str(duration)]
        cmd += ["-i", str(clips[0].resolve()), "-c", "copy",
                "-movflags", "+faststart", str(out_path)]
        run(cmd, dry_run, what="ffmpeg (trim)", progress=progress, total=total)
        return

    if offset:
        pre = tmpdir / f"{out_path.stem}_pre.mp4"
        run([ffmpeg, "-y", "-ss", str(offset), "-i", str(clips[0].resolve()),
             "-c", "copy", str(pre)], dry_run, what="ffmpeg (trim first clip)",
            progress=progress)
        clips = [pre] + list(clips[1:])

    list_path = tmpdir / f"{out_path.stem}_concat.txt"
    with open(list_path, "w") as f:
        for c in clips:
            f.write(f"file '{c.resolve()}'\n")

    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0"]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-i", str(list_path), "-c", "copy", "-movflags", "+faststart", str(out_path)]
    run(cmd, dry_run, what="ffmpeg (concat)", progress=progress, total=total)


def hw_fit_scale(w, h, max_dim):
    """Apple's VideoToolbox H.264 hardware encoder/decoder caps out around
    max_dim px in any single dimension (empirically bisected: 4096 works,
    4352 doesn't). Scale the whole composite down to fit if it's oversized,
    so playback gets hardware decode instead of a slow/CPU-bound software
    fallback. Returns an ffmpeg scale= expression, or None if no scaling needed."""
    if w <= max_dim and h <= max_dim:
        return None
    return f"{max_dim}:-2" if w >= h else f"-2:{max_dim}"


def fit_dims(w, h, max_dim):
    """Numeric counterpart to hw_fit_scale, for bitrate math -- mirrors what
    ffmpeg's scale=-2:H / scale=W:-2 actually computes (even width/height)."""
    if w >= h:
        new_w, new_h = max_dim, int(round(h * max_dim / w / 2) * 2)
    else:
        new_h, new_w = max_dim, int(round(w * max_dim / h / 2) * 2)
    return new_w, new_h


def _tile_chain(idx, angle, dims, angle_paths, has_text, font, font_size):
    """One tile's `[N:v]fps=...[+drawtext][vN]` filter chain -- the per-camera
    line every composition (tall-mode row, landscape hero/sidebar tile) emits
    before stacking. `dims`/`angle_paths` are the same dicts every layout
    function is handed; `font_size` is the caller's choice (hero vs. normal
    vs. sidebar) rather than an inline pick, so this one chain-builder serves
    every composition. Returns (filter_line, tag, input_path) -- `filter_line`
    already carries its trailing ';'.
    """
    tag = f"v{idx}"
    chain = f"[{idx}:v]fps={OUTPUT_FPS}"
    if has_text and angle in LABEL_TEXT:  # map tile is deliberately label-less
        text = LABEL_TEXT[angle]
        chain += (f",drawtext=fontfile={font}:text='{text}':fontcolor=white:"
                  f"fontsize={font_size}:box=1:boxcolor=black@0.6:boxborderw=10:x=20:y=20")
    chain += f"[{tag}];"
    return chain, tag, angle_paths[angle]


def _apply_tail(lines, stage, has_text, font, epoch, speed):
    """Append the shared closing stages -- burned-in timestamp (if labels are
    on), then the speed/fps remap (or a passthrough `null`) -- to `lines`, and
    join it all into the final filter_complex text. Every build_filter*
    function's composition-specific stacking is done by the time it calls
    this; only this tail is common to all of them.
    """
    if has_text:
        lines.append(
            f"[{stage}]drawtext=fontfile={font}:text='%{{pts\\:localtime\\:{epoch}}}':"
            f"fontcolor=white:fontsize=56:box=1:boxcolor=black@0.5:boxborderw=12:"
            f"x=20:y=h-th-20[timestamped];"
        )
        stage = "timestamped"

    if speed != 1.0:
        lines.append(f"[{stage}]setpts={1/speed}*PTS,fps={OUTPUT_FPS}[out]")
    else:
        lines.append(f"[{stage}]null[out]")

    return "\n".join(lines)


def build_filter(dims, angle_paths, has_text, font, epoch, max_dim, native, speed, feature):
    """
    Build the filter_complex graph as a text block (fed to ffmpeg via
    a file (see filter_graph_args), which sidesteps shell-quoting entirely -- the
    drawtext pts:localtime escaping in particular is finicky enough that
    building it as a string and passing through the shell is asking for
    trouble). Returns (filter_text, input_order, final_width, final_height).

    This is the TALL-mode layout: rows stacked top to bottom. See
    build_filter_landscape for the sibling hero+sidebar layout (--landscape).

    dims comes from the ORIGINAL source clips, not the concat outputs, so this
    works under --dry-run when the concat outputs don't exist yet.

    Each row of cameras is: per-tile [fps + optional label] -> hstack (if >1
    tile) -> pad or upscale to canvas width. Rows are then vstack'd. xstack
    with hand-computed coordinates would also work but is easy to get subtly
    wrong; hstack/vstack/pad needs no coordinate math.

    fps=OUTPUT_FPS on every input is load-bearing: the cameras are each
    variable-frame-rate on their own independent clock, and stacking them
    without normalizing first emits a new output frame every time ANY input
    ticks -- which bloats the frame count several-fold and plays back choppy.
    """
    present = [a for a in CAMERA_ANGLES if a in dims]
    rows, hero_angles = build_rows(present, feature)
    if not rows:
        die("Not enough matching camera angles to build a grid.")
    # The map tile (if present) rides in dims/angle_paths under MAP_TILE_KEY but
    # isn't a CAMERA_ANGLE, so build_rows ignores it; slot it in explicitly.
    if MAP_TILE_KEY in dims:
        rows = inject_map_row(rows, hero_angles)

    canvas_w = max(sum(dims[a][0] for a in row) for row in rows)

    lines = []
    input_order = []
    idx = 0
    row_tags = []
    row_heights = []
    for row in rows:
        tile_tags = []
        for angle in row:
            size = HERO_FONT_SIZE if angle in hero_angles else NORMAL_FONT_SIZE
            line, tag, path = _tile_chain(idx, angle, dims, angle_paths, has_text, font, size)
            lines.append(line)
            tile_tags.append(tag)
            input_order.append(path)
            idx += 1

        if len(tile_tags) > 1:
            row_raw = f"row{len(lines)}"
            lines.append("".join(f"[{t}]" for t in tile_tags)
                         + f"hstack=inputs={len(tile_tags)}:shortest=1[{row_raw}];")
        else:
            row_raw = tile_tags[0]

        row_w = sum(dims[a][0] for a in row)
        row_h = max(dims[a][1] for a in row)
        if row_w < canvas_w:
            if row == hero_angles:
                # The featured camera(s) are lower native resolution than the
                # canvas width (true for anything but front solo) -- scale up to
                # fill the hero row instead of padding with black bars, so it
                # actually reads as prominent rather than small-and-centered.
                # Upscales via interpolation; adds no real detail.
                scaled_h = round(row_h * canvas_w / row_w / 2) * 2
                tag = f"row{len(lines)}s"
                lines.append(f"[{row_raw}]scale={canvas_w}:{scaled_h}[{tag}];")
                row_tags.append(tag)
                row_heights.append(scaled_h)
            else:
                padded = f"row{len(lines)}p"
                xoff = (canvas_w - row_w) // 2
                lines.append(f"[{row_raw}]pad={canvas_w}:{row_h}:{xoff}:0:black[{padded}];")
                row_tags.append(padded)
                row_heights.append(row_h)
        else:
            row_tags.append(row_raw)
            row_heights.append(row_h)

    canvas_h = sum(row_heights)

    if len(row_tags) > 1:
        lines.append("".join(f"[{t}]" for t in row_tags)
                     + f"vstack=inputs={len(row_tags)}:shortest=1[grid];")
        stage = "grid"
    else:
        stage = row_tags[0]

    final_w, final_h = canvas_w, canvas_h
    if not native:
        scale_expr = hw_fit_scale(canvas_w, canvas_h, max_dim)
        if scale_expr:
            lines.append(f"[{stage}]scale={scale_expr}[gridscaled];")
            stage = "gridscaled"
            final_w, final_h = fit_dims(canvas_w, canvas_h, max_dim)

    filter_text = _apply_tail(lines, stage, has_text, font, epoch, speed)
    return filter_text, input_order, final_w, final_h


def landscape_layout(dims, feature):
    """
    Pure geometry for the --landscape layout: the featured camera(s) at full
    native resolution on the left, every other present tile (cameras plus the
    map, if MAP_TILE_KEY is in `dims`) stacked single-file into a thin
    sidebar column on the right, sized so the sidebar's stacked height
    matches the hero's height exactly. Structurally avoids the tall grid's
    4096px hardware-encoder height cap for the common case (a chunkier
    multi-column sidebar would just re-trigger it).

    dims: {angle: (w, h)}, same shape as everywhere else in this file --
    may include MAP_TILE_KEY. feature: the --feature choice (a single angle
    for a solo hero, or a PAIR_DEFS key for a two-camera hero).

    Each sidebar tile is scaled to a common width W_side, preserving its own
    aspect ratio, so its height is W_side/aspect_i. For the stacked column to
    sum to exactly H: sum_i(W_side/aspect_i) = H, i.e.
    W_side = H / sum(1/aspect_i) -- which reduces to H*aspect/N when every
    sidebar tile shares one aspect (the common case: Tesla's 6 camera angles
    are all the same ~1.5437:1, whatever their native resolution).

    Returns (hero_angles, sidebar_angles, hero_w, H, w_side, canvas_w,
    canvas_h). sidebar_angles is in CAMERA_ANGLES order with the map (if
    present) last -- mirroring the tall grid's own back+map pairing, and
    guaranteeing the map (which has no fixed native aspect of its own) is
    always the sidebar's remainder-clamped last tile in
    build_filter_landscape, never a middle one.
    """
    hero_angles = hero_angles_for(feature)
    sidebar_angles = [a for a in CAMERA_ANGLES if a in dims and a not in hero_angles]
    if MAP_TILE_KEY in dims and MAP_TILE_KEY not in hero_angles:
        sidebar_angles.append(MAP_TILE_KEY)

    hero_w = sum(dims[a][0] for a in hero_angles)
    # A pair hero is hstacked with shortest=1, which crops to the SHORTER
    # input's height -- min, not max, so this matches what ffmpeg actually
    # produces. In practice PAIR_DEFS pairs always share identical native
    # dims (both pillars, both repeaters), so this never bites on real
    # footage, but a mismatched pair would otherwise make canvas_h a lie.
    H = min(dims[a][1] for a in hero_angles)

    if sidebar_angles:
        inv_aspect_sum = sum(dims[a][1] / dims[a][0] for a in sidebar_angles)
        w_side = round(H / inv_aspect_sum / 2) * 2 if inv_aspect_sum > 0 else 0
    else:
        w_side = 0

    canvas_w = hero_w + w_side
    canvas_h = H
    return hero_angles, sidebar_angles, hero_w, H, w_side, canvas_w, canvas_h


def build_filter_landscape(dims, angle_paths, has_text, font, epoch, max_dim, native, speed, feature):
    """
    Sibling to build_filter for the --landscape layout: the hero tile(s) at
    full native resolution (NO scale filter -- that's the whole point, unlike
    the tall grid's hero-row upscale-to-canvas-width) hstacked on the left,
    every other present tile scaled to a common sidebar width and vstacked
    single-file on the right, then hero+sidebar hstacked into the final grid.
    Kept as a separate function (not a branch inside build_filter) so the
    default tall-mode path can't regress, and each composition algorithm
    stays readable on its own. Same return shape as build_filter:
    (filter_text, input_order, final_width, final_height).

    The map tile needs no special-casing here: once dims[MAP_TILE_KEY] exists
    it's just another sidebar entry (landscape_layout always places it last),
    and _tile_chain's existing `angle in LABEL_TEXT` check already keeps it
    label-less, same as tall mode.
    """
    hero_angles, sidebar_angles, hero_w, H, w_side, canvas_w, canvas_h = (
        landscape_layout(dims, feature))

    lines = []
    input_order = []
    idx = 0

    hero_tags = []
    for angle in hero_angles:
        line, tag, path = _tile_chain(idx, angle, dims, angle_paths, has_text,
                                      font, HERO_FONT_SIZE)
        lines.append(line)
        hero_tags.append(tag)
        input_order.append(path)
        idx += 1

    if len(hero_tags) > 1:
        hero_stage = "hero"
        lines.append("".join(f"[{t}]" for t in hero_tags)
                     + f"hstack=inputs={len(hero_tags)}:shortest=1[{hero_stage}];")
    else:
        hero_stage = hero_tags[0]

    if sidebar_angles:
        side_tags = []
        heights = []
        for i, angle in enumerate(sidebar_angles):
            line, tag, path = _tile_chain(idx, angle, dims, angle_paths, has_text,
                                          font, SIDEBAR_FONT_SIZE)
            lines.append(line)
            input_order.append(path)
            idx += 1

            if i < len(sidebar_angles) - 1:
                w, h = dims[angle]
                tile_h = round(w_side * h / w / 2) * 2
            else:
                # Last tile absorbs whatever rounding drift the others left,
                # so the stacked column sums to exactly H -- no black gap,
                # no overflow. The map (always last, see landscape_layout)
                # lands here, which is also right for it: it has no native
                # aspect of its own to preserve.
                tile_h = H - sum(heights)
                tile_h = max(2, tile_h - (tile_h % 2))
            heights.append(tile_h)

            scaled_tag = f"{tag}s"
            lines.append(f"[{tag}]scale={w_side}:{tile_h}[{scaled_tag}];")
            side_tags.append(scaled_tag)

        if len(side_tags) > 1:
            side_stage = "sidebar"
            lines.append("".join(f"[{t}]" for t in side_tags)
                         + f"vstack=inputs={len(side_tags)}:shortest=1[{side_stage}];")
        else:
            side_stage = side_tags[0]

        stage = "grid"
        lines.append(f"[{hero_stage}][{side_stage}]hstack=inputs=2:shortest=1[{stage}];")
    else:
        stage = hero_stage

    final_w, final_h = canvas_w, canvas_h
    if not native:
        scale_expr = hw_fit_scale(canvas_w, canvas_h, max_dim)
        if scale_expr:
            lines.append(f"[{stage}]scale={scale_expr}[gridscaled];")
            stage = "gridscaled"
            final_w, final_h = fit_dims(canvas_w, canvas_h, max_dim)

    filter_text = _apply_tail(lines, stage, has_text, font, epoch, speed)
    return filter_text, input_order, final_w, final_h


def auto_bitrate(w, h, fps=OUTPUT_FPS, bits_per_pixel=0.07):
    return max(4_000_000, int(w * h * fps * bits_per_pixel))


def parse_trim(value, session_start):
    """HH:MM:SS wall-clock (same day as the recording) or a plain seconds offset."""
    if value is None:
        return None
    if ":" in value:
        try:
            h, m, s = (int(x) for x in value.split(":"))
            trim_dt = session_start.replace(hour=h, minute=m, second=s, microsecond=0)
        except ValueError:
            die(f"Could not read '{value}' as a time. Use HH:MM:SS or a number of seconds.")
        offset = (trim_dt - session_start).total_seconds()
        if offset < 0:
            die(f"--trim value {value} is before the session start "
                f"({session_start.strftime('%H:%M:%S')}).")
        return offset
    try:
        return float(value)
    except ValueError:
        die(f"Could not read '{value}' as a time. Use HH:MM:SS or a number of seconds.")


def check_space(out_dir, estimate, skip):
    free = shutil.disk_usage(out_dir).free
    log(f"Estimated output: ~{human_bytes(estimate)} | free on target volume: {human_bytes(free)}")
    if estimate > free and not skip:
        die(f"Not enough free space on {out_dir}: needs roughly {human_bytes(estimate)}, "
            f"{human_bytes(free)} free.\nFree up space, pick another --output-dir, trim to a "
            f"shorter window, or pass --skip-space-check if you think the estimate is wrong.")


def report_gap(ffprobe, clips, out_path, offset, duration, session_start):
    """
    The burned-in clock is computed as epoch + elapsed video time, which assumes
    the recording is continuous. Tesla recordings can have gaps between clips
    (Sentry drops segments), and concatenating squeezes those gaps out -- so the
    clock can drift behind true wall-clock by however much is missing. Measure it
    and say so rather than silently showing a slightly wrong time.

    Costs one probe of the last clip; the rest is arithmetic on the output.
    """
    actual = probe_duration(ffprobe, out_path)
    if actual is None or not clips:
        return None
    starts = []
    for c in clips:
        mm = FILENAME_RE.match(c.name)
        if not mm:
            return None
        starts.append(datetime.strptime(mm.group(1), "%Y-%m-%d_%H-%M-%S"))

    if duration is None:
        # Untrimmed: one probe of the last clip is enough. Total wall span from
        # the window start to the last clip's end, minus the concatenated length,
        # is exactly the gap time squeezed out.
        last_dur = probe_duration(ffprobe, clips[-1])
        if last_dur is None:
            return None
        wall_start = starts[0] + timedelta(seconds=offset)
        wall_end = starts[-1] + timedelta(seconds=last_dur)
        return (wall_end - wall_start).total_seconds() - actual

    # Trimmed (--trim-end): capping wall_span at `duration` makes it ~= actual and
    # hides gaps INSIDE the window, so sum the inter-clip gaps that fall within the
    # actual output length directly. The trimmed clip list is small, so probing
    # each is cheap.
    drift = 0.0
    pos = -offset  # concat position of each clip's own start (clip 0 pre-trimmed)
    for i in range(len(clips) - 1):
        d = probe_duration(ffprobe, clips[i])
        if d is None:
            return None
        pos += d
        if pos >= actual:
            break
        gap = (starts[i + 1] - (starts[i] + timedelta(seconds=d))).total_seconds()
        drift += max(0.0, gap)
    return drift


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, help="Tesla event folder containing the *-<angle>.mp4 clips")
    ap.add_argument("--output-dir", type=Path, default=None, help="default: same as input folder")
    ap.add_argument("--trim-start", default=None, help="HH:MM:SS wall-clock or seconds offset")
    ap.add_argument("--trim-end", default=None, help="HH:MM:SS wall-clock or seconds offset")
    ap.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier, e.g. 2 for 2x")
    ap.add_argument("--no-labels", action="store_true", help="skip per-tile labels and the burned-in clock")
    ap.add_argument("--feature", default="front", choices=FEATURE_CHOICES,
                    help="what gets the large hero row (default: front). Solo: front, back, "
                         "left_pillar, right_pillar, left_repeater, right_repeater -- that one "
                         "camera large, its old pair-partner (if any) gets its own row instead of "
                         "being dropped. Pair: pillars, repeaters -- both L/R cameras large "
                         "together. e.g. --feature back if you got rear-ended.")
    ap.add_argument("--landscape", action="store_true",
                    help="landscape layout instead of the tall stacked-row grid: the featured "
                         "camera at full native resolution on the left, every other present "
                         "camera (and the map, if any) in a thin single-file sidebar column on "
                         "the right, sized to match the hero's height. Produces a real landscape "
                         "aspect ratio (good for YouTube/social feed video) and, for the common "
                         "case, avoids the tall grid's height-inflation softness by construction.")
    ap.add_argument("--native", action="store_true",
                    help="skip the hardware-fit scale-down; true native resolution. Only forces a "
                         "slow software encode if the native size ALSO exceeds --max-dim -- use "
                         "--quality high if you want software encoding for its own sake.")
    ap.add_argument("--max-dim", type=int, default=4096, help="hardware encode/decode ceiling to fit under")
    ap.add_argument("--quality", default="fast", choices=["fast", "high"],
                    help="fast (default): today's hardware encode (h264_videotoolbox). high: force "
                         "software encoding (libx264, veryfast, CRF 18) regardless of canvas size "
                         "or --native -- sharper than the hardware encoder at an equivalent "
                         "bitrate, but much slower.")
    ap.add_argument("--blur-faces", action="store_true",
                    help="auto-detect and anonymize people's faces in every camera (and thus the "
                         "grid). Needs the `deface` tool: python3 -m pip install deface. Re-encodes "
                         "each camera through a face detector, so it's slow -- expect roughly "
                         "real-time per camera on CPU.")
    ap.add_argument("--blur-mode", default="blur", choices=["blur", "solid", "mosaic"],
                    help="how faces are hidden with --blur-faces (default: blur). solid = black box, "
                         "mosaic = pixelate.")
    ap.add_argument("--blur-thresh", type=float, default=0.2,
                    help="face-detection confidence 0-1 for --blur-faces (default: 0.2). Lower catches "
                         "more faces but risks false positives; raise it if too much gets blurred.")
    ap.add_argument("--blur-scale", default=None,
                    help="downscale WxH (e.g. 640x480) for the face-DETECTION pass only; output stays "
                         "full-res. Speeds up --blur-faces at some cost to small/distant faces.")
    ap.add_argument("--map", action="store_true",
                    help="add a live route-map tile (paired with the back camera) built from the "
                         "car's GPS. Needs SEI telemetry in the clips (firmware 2025.44.25+/HW3+, "
                         "recorded while driving) plus the gopro-overlay tool in ./.venv "
                         "(python3.12 -m venv .venv && ./.venv/bin/python -m pip install gopro-overlay). "
                         "Downloads OpenStreetMap tiles on first use, so it needs network access.")
    ap.add_argument("--map-zoom", type=int, default=19,
                    help="OpenStreetMap tile zoom for the --map tile (default: 19, the OSM max). "
                         "Higher = more street detail; lower = wider area (better for highway). "
                         "The whole route is rendered into one image, so high zoom on a long "
                         "multi-km drive gets slow. To go TIGHTER than zoom 19, use --map-mag.")
    ap.add_argument("--map-mag", type=float, default=2.0,
                    help="magnify the map tile beyond OSM's zoom limit for a tighter, "
                         "navigation-style view (default: 2.0; 1.0 = off/sharpest). The map is "
                         "rendered smaller then upscaled, so higher = tighter but softer. "
                         "e.g. 2 shows ~half the area, 3 ~a third. Try 3 for a very close view.")
    ap.add_argument("--gauge", action="store_true",
                    help="composite a speed/compass dashboard panel (dial, compass, big speed "
                         "readout, sparkline chart) onto the hero camera tile. Same prerequisites "
                         "as --map (SEI telemetry, gopro-overlay in ./.venv -- see --map's help). "
                         "v1 only supports a solo hero: --feature must be a single camera, not a "
                         "pair like 'repeaters'.")
    ap.add_argument("--gauge-units", default="mph", choices=["mph", "kph"],
                    help="speed units for --gauge (default: mph, matching US driving footage).")
    ap.add_argument("--force-concat", action="store_true",
                    help="rebuild the per-camera concats even if matching ones already exist")
    ap.add_argument("--skip-space-check", action="store_true", help="don't pre-flight free disk space")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show every command and let ffmpeg/deface print in full, instead of "
                         "the progress display. Use this when something fails and you want to "
                         "watch it happen.")
    ap.add_argument("--no-progress", action="store_true",
                    help="don't redraw a live progress bar; print a plain progress line "
                         "every so often instead (this is automatic when the output isn't a "
                         "terminal, e.g. piped to a log file)")
    ap.add_argument("--dry-run", action="store_true", help="print the ffmpeg commands without running them")
    return ap


@dataclass
class Tools:
    """Resolved external tool paths + label/blur/map/gauge capability flags for
    one run. map_venv_py/map_gopro/map_font are shared by --map and --gauge --
    both use the same gopro-overlay installation."""
    ffmpeg: str
    ffprobe: str
    has_text: bool
    font: Optional[str]
    deface_bin: Optional[str] = None
    map_venv_py: Optional[Path] = None
    map_gopro: Optional[Path] = None
    map_font: Optional[str] = None


@dataclass
class Plan:
    """The job plan: discovered clips, per-camera selections, source dims and the
    burned-in-clock epoch -- everything the build phases need after setup."""
    folder: Path
    out_dir: Path
    session_name: str
    by_angle: Dict[str, list]
    session_start: datetime
    n_clips: int
    in_bytes: int
    selections: Dict[str, tuple]              # angle -> (sel_paths, offset, duration)
    dims: Dict[str, Tuple[int, int]]          # angle -> (w, h) from SOURCE clips
    epoch: int
    footage: Dict[str, float]                 # angle -> seconds of footage selected
    steps: list                               # Step list, in execution order


def setup_tools(args) -> Tools:
    """Discover ffmpeg/ffprobe and the optional deface/gopro tooling, and validate
    the flags that gate them. Dies (via die()) on anything missing or out of range."""
    ffmpeg, has_drawtext = find_ffmpeg()
    sibling_ffprobe = Path(ffmpeg).parent / "ffprobe"
    ffprobe = str(sibling_ffprobe) if sibling_ffprobe.exists() else shutil.which("ffprobe")
    if not ffprobe:
        die("Found ffmpeg but no ffprobe alongside it.")
    has_text = has_drawtext and not args.no_labels
    font = find_font() if has_text else None

    tools = Tools(ffmpeg=ffmpeg, ffprobe=ffprobe, has_text=has_text, font=font)

    if args.blur_faces:
        tools.deface_bin = find_deface()
        if not tools.deface_bin:
            die("--blur-faces needs the `deface` tool, which isn't installed.\n"
                "Install it with:  python3 -m pip install deface\n"
                "(CPU-only works -- no GPU required; the first run downloads a ~36MB "
                "face-detection model.)")

    if args.map or args.gauge:
        # --map and --gauge share the same gopro-overlay tooling and GPS
        # extraction (see build_route_gpx) -- one discovery/validation block
        # for both.
        script_dir = Path(__file__).resolve().parent
        map_venv_py, map_gopro, map_missing = find_map_tooling(script_dir)
        if map_missing:
            flag = "--map/--gauge" if (args.map and args.gauge) else ("--map" if args.map else "--gauge")
            die(f"{flag} needs the gopro-overlay tool in a sibling .venv next to this script.\n"
                "Set it up with:\n"
                "  python3.12 -m venv .venv && ./.venv/bin/python -m pip install gopro-overlay\n"
                f"Missing: {', '.join(map_missing)}")
        if args.map:
            if not 1 <= args.map_zoom <= 19:
                die(f"--map-zoom {args.map_zoom} is out of range; OSM tiles support 1-19 "
                    f"(default 19; use --map-mag to go tighter).")
            if not 1.0 <= args.map_mag <= 4.0:
                die(f"--map-mag {args.map_mag} is out of range; use 1.0 (off) to 4.0. "
                    f"Higher magnifies more (tighter) but softer.")
        tools.map_venv_py, tools.map_gopro = map_venv_py, map_gopro
        tools.map_font = find_map_font()

    if args.gauge and len(hero_angles_for(args.feature)) > 1:
        die("--gauge needs a solo --feature (a pair like 'repeaters' has two hero "
            "tiles) -- pick a single camera.")

    return tools


def plan_job(args, tools: Tools, folder: Path, out_dir: Path,
             session_name: str) -> Plan:
    """Discover clips, apply the feature/trim validation, probe source dims, and
    pre-flight the disk-space estimate. Returns everything the build phases need."""
    ffprobe = tools.ffprobe
    by_angle, session_start = discover_clips(folder)
    n_clips = sum(len(v) for v in by_angle.values())
    in_bytes = sum(p.stat().st_size for v in by_angle.values() for _, p in v)
    log(f"tesla_combine {SCRIPT_VERSION}")
    log(f"Found {n_clips} clips ({human_bytes(in_bytes)}) across: {', '.join(by_angle)}")
    log(f"Session starts {session_start:%Y-%m-%d %H:%M:%S}")

    if args.feature in PAIR_DEFS:
        missing = [a for a in PAIR_DEFS[args.feature] if a not in by_angle]
        if missing:
            die(f"--feature {args.feature} needs both {PAIR_DEFS[args.feature]}, "
                f"missing: {', '.join(missing)}")
    elif args.feature not in by_angle:
        die(f"--feature {args.feature} has no clips in this folder (found: {', '.join(by_angle)})")

    trim_start = parse_trim(args.trim_start, session_start)
    trim_end = parse_trim(args.trim_end, session_start)
    if trim_start is not None and trim_end is not None and trim_end <= trim_start:
        die("--trim-end must be after --trim-start.")
    epoch = int((session_start + timedelta(seconds=trim_start or 0.0)).timestamp())

    # Dimensions come from the SOURCE clips so the filter graph can be built
    # (and printed) without the concat outputs existing yet -- which is what
    # makes --dry-run work.
    dims = {a: probe_dims(ffprobe, v[0][1]) for a, v in by_angle.items()}

    # Work out the clip selection per angle up front, so the space estimate and
    # the plan reflect the trim rather than the whole session.
    selections = {}
    for angle, clips in by_angle.items():
        sel, off, dur = select_clips(ffprobe, clips, session_start, trim_start, trim_end)
        selections[angle] = (sel, off, dur)
    if trim_start is not None or trim_end is not None:
        any_sel = next(iter(selections.values()))[0]
        log(f"Trim window selects {len(any_sel)} of {len(next(iter(by_angle.values())))} "
            f"clips per camera")

    _, _, sample_dur = next(iter(selections.values()))
    est_dims = dims
    if args.map:
        # Mirror the tile-sizing fallback used at render time (back, else the map
        # source camera, else a default) so the estimate matches when back is absent.
        est_dims = {**dims, MAP_TILE_KEY: dims.get("back") or dims.get("front") or (1280, 960)}
    if args.landscape:
        # landscape_layout is the single source of truth for canvas size in
        # this layout -- same function the filter builder and the map-tile
        # sizing use, so the pre-flight estimate matches what actually renders.
        _, _, _, _, _, probe_w, probe_h = landscape_layout(est_dims, args.feature)
    else:
        grid_rows, hero = build_rows([a for a in CAMERA_ANGLES if a in dims], args.feature)
        if args.map:
            # Same layout the grid will use, so the space estimate reflects the
            # extra map row.
            grid_rows = inject_map_row([list(r) for r in grid_rows], hero)
        probe_w = max(sum(est_dims[a][0] for a in row) for row in grid_rows)
        probe_h = sum(max(est_dims[a][1] for a in row) for row in grid_rows)
    est_w, est_h = (probe_w, probe_h) if args.native else (
        fit_dims(probe_w, probe_h, args.max_dim)
        if hw_fit_scale(probe_w, probe_h, args.max_dim) else (probe_w, probe_h))

    sel_bytes = sum(p.stat().st_size for sel, _, _ in selections.values() for p in sel)
    est_seconds = sample_dur if sample_dur else 60.0 * max(len(s) for s, _, _ in selections.values())
    est = sel_bytes * (min(1.0, est_seconds / (60.0 * max(len(s) for s, _, _ in selections.values())))
                       if sample_dur else 1.0)
    est += auto_bitrate(est_w, est_h) * est_seconds / 8
    if args.blur_faces:
        # Each blurred per-camera copy lands alongside its concat; libx264 output
        # is usually a bit smaller than the source, so the concats' size is a
        # safe upper bound to reserve for them.
        est += sel_bytes
    if not args.dry_run:
        check_space(out_dir, est, args.skip_space_check)

    footage = {angle: estimate_concat_seconds(ffprobe, sel, off, dur)
               for angle, (sel, off, dur) in selections.items()}
    steps = plan_steps(args, selections, footage)
    kinds = collections.Counter(KIND_LABELS.get(s.kind, s.kind) for s in steps)
    log(f"Plan: {len(steps)} steps ("
        + ", ".join(f"{n} {k}" if n > 1 else k for k, n in kinds.items())
        + f") | ~{human_time(max(footage.values()))} of footage per camera")

    return Plan(folder=folder, out_dir=out_dir, session_name=session_name,
                by_angle=by_angle, session_start=session_start, n_clips=n_clips,
                in_bytes=in_bytes, selections=selections, dims=dims, epoch=epoch,
                footage=footage, steps=steps)


def plan_steps(args, selections, footage):
    """The ordered list of steps the run will work through, in the order the build
    phases actually execute them: per camera concat (then its blur), then the map
    tile, then the grid. Cache hits still appear here -- they're marked skipped
    when the build finds them, which is also when their weight leaves the ETA."""
    steps = []
    for angle in selections:
        steps.append(Step("concat", f"concat {angle}", footage[angle]))
        if args.blur_faces:
            steps.append(Step("blur", f"blur faces {angle}", footage[angle]))
    if args.map or args.gauge:
        # GPS extraction is shared -- one map_gps step regardless of whether
        # --map, --gauge, or both were requested (see build_route_gpx).
        map_source = "front" if "front" in selections else next(iter(selections))
        map_work = footage[map_source]
        steps.append(Step("map_gps", "GPS extract", map_work))
        if args.map:
            steps.append(Step("map_render", "route map render", map_work))
            if args.map_mag and args.map_mag != 1.0:
                steps.append(Step("map_scale", f"map upscale ({args.map_mag}x)", map_work))
        if args.gauge:
            steps.append(Step("gauge_render", "gauge overlay render", map_work))
    if len(selections) > 1 or args.map:
        # The grid encodes the OUTPUT timeline, which --speed has already scaled.
        steps.append(Step("grid", "grid encode",
                          max(footage.values()) / max(0.01, args.speed)))
    return steps


def build_per_camera(args, tools: Tools, plan: Plan, cache: dict,
                     stats: dict, tmpdir: Path, progress: Progress):
    """Concat each camera's clips (reusing cached concats), optionally blur faces,
    and record per-camera clock drift. Mutates `cache`/`stats`, saves the cache,
    and returns (angle_paths, drifts)."""
    ffmpeg, ffprobe = tools.ffmpeg, tools.ffprobe
    out_dir, session_name = plan.out_dir, plan.session_name
    angle_paths = {}
    drifts = {}
    refined = False   # has a finished concat corrected the footage estimate yet?
    for angle, (sel, off, dur) in plan.selections.items():
        out_path = out_dir / f"{session_name}_{angle}_combined.mp4"
        key = concat_key(sel, off, dur)
        cached = cache.get(out_path.name)
        fresh = (not args.force_concat and cached == key and out_path.exists()
                 and probe_duration(ffprobe, out_path) is not None)
        footage = plan.footage.get(angle, 0.0)
        if fresh:
            progress.skip("concat", f"concat {angle}",
                          f"{angle}: reusing existing concat ({len(sel)} clips)")
            stats["reused"] += 1
        else:
            progress.begin("concat",
                           f"concat {angle} ({len(sel)} clip{'' if len(sel) == 1 else 's'})",
                           footage, out=out_path)
            t0 = time.monotonic()
            concat_angle(ffmpeg, ffprobe, sel, off, dur, out_path, tmpdir, args.dry_run,
                         progress=progress, total=footage)
            stats["concat_s"] += time.monotonic() - t0
            stats["built"] += 1
            # The concat's real length settles what every later step is working
            # on: the footage estimate ignores gaps in the recording, and every
            # camera shares the same gaps.
            actual = None if args.dry_run else probe_duration(ffprobe, out_path)
            progress.end(work=actual)
            if actual and footage > 0 and not refined:
                refined = True
                progress.rescale_pending(actual / footage)
            if not args.dry_run:
                # Flush as soon as the concat is real, not at the end of the
                # camera: the blur that follows takes hours, so an interrupt
                # almost always lands there and would otherwise throw this away.
                cache[out_path.name] = key
                save_cache(out_dir, cache)

        # Anonymize faces on the FULL-RES per-camera concat, then feed the
        # blurred copy into the grid so the composite inherits the blurring
        # too. Detecting on the full-res source (rather than the downscaled
        # grid, where each tile is small) is what makes the detection work.
        source_path = out_path
        if args.blur_faces:
            blur_path = out_dir / f"{session_name}_{angle}_blurred.mp4"
            bkey = blur_key(key, args.blur_mode, args.blur_thresh, args.blur_scale)
            bcached = cache.get(blur_path.name)
            bfresh = (not args.force_concat and bcached == bkey and blur_path.exists()
                      and probe_duration(ffprobe, blur_path) is not None)
            if bfresh:
                progress.skip("blur", f"blur faces {angle}",
                              f"{angle}: reusing existing blurred video")
                stats["blur_reused"] += 1
            else:
                progress.begin("blur", f"blur faces {angle}", footage, out=blur_path)
                t0 = time.monotonic()
                deface_video(tools.deface_bin, out_path, blur_path, args.blur_mode,
                             args.blur_thresh, args.blur_scale, args.dry_run,
                             progress=progress)
                stats["blur_s"] += time.monotonic() - t0
                stats["blurred"] += 1
                progress.end()
                if not args.dry_run:
                    cache[blur_path.name] = bkey
                    save_cache(out_dir, cache)
            source_path = blur_path
        angle_paths[angle] = source_path

        if not args.dry_run:
            d = report_gap(ffprobe, sel, out_path, off, dur, plan.session_start)
            if d is not None:
                drifts[angle] = d
            # Save after every camera rather than once at the end: a run that gets
            # interrupted three cameras in should resume from three cameras in.
            save_cache(out_dir, cache)

    return angle_paths, drifts


def encoder_args(native, quality, max_dim, final_w, final_h):
    """
    Pick the grid encode's `-c:v ...` args. Returns (cmd_args, warning_or_None)
    -- this never prints; the caller logs the warning (if any).

    `quality == "high"` always wins: CRF 18 software (libx264, veryfast) --
    the same CRF already used for the map-tile upscale pass (this repo's
    existing precedent for "quality is the actual goal, not an unavoidable
    fallback"), so no warning -- the user asked for this.

    Otherwise, preserves the original --native-exceeds-the-cap fallback
    verbatim (CRF 20, with its warning); anything else is the normal
    hardware path.
    """
    if quality == "high":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"], None

    if native and (final_w > max_dim or final_h > max_dim):
        warning = ("--native exceeds the hardware encoder's limit -- falling back to slow "
                   "software encoding (libx264). Expect this to take a while.")
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"], warning

    return ["-c:v", "h264_videotoolbox", "-b:v", str(auto_bitrate(final_w, final_h))], None


def build_grid(args, tools: Tools, plan: Plan, angle_paths: dict,
               stats: dict, tmpdir: Path, progress: Progress):
    """Re-probe real input dims, build the optional map tile, assemble the filter
    graph and encode the grid. Returns (out_grid, final_w, final_h), or None if
    there's only one angle (no grid built)."""
    ffmpeg, ffprobe = tools.ffmpeg, tools.ffprobe
    out_dir, session_name = plan.out_dir, plan.session_name

    # The grid's layout math must match the ACTUAL files it stacks. Concat is
    # a stream copy so dims are unchanged, but deface re-encodes and its
    # encoder rounds each dimension UP to a macroblock multiple (e.g.
    # 1876 -> 1888), which would break the pad/scale math built from the
    # source-clip dims. Re-probe the real inputs here. (Skipped under
    # --dry-run, where these files don't exist yet and the source dims are
    # the best available estimate for printing the plan.)
    dims = dict(plan.dims)
    if not args.dry_run:
        dims = {a: probe_dims(ffprobe, p) for a, p in angle_paths.items()}

    # Build the optional live route-map tile and/or --gauge dashboard overlay.
    # Both need the same GPS: extracted from the ORIGINAL front source clips
    # (SEI lives in the source bitstream, not the concat/blurred outputs) and
    # re-timed onto the grid timeline -- build_route_gpx does this ONCE and is
    # shared by both, so --map --gauge together don't pay for it twice.
    if args.map or args.gauge:
        t0 = time.monotonic()
        map_source = "front" if "front" in plan.selections else next(iter(plan.selections))
        src_sel, src_off, _ = plan.selections[map_source]
        src_concat = out_dir / f"{session_name}_{map_source}_combined.mp4"
        grid_dur = (probe_duration(ffprobe, src_concat)
                    if not args.dry_run else plan.selections[map_source][2]) or 60.0
        gpx_path = build_route_gpx(src_sel, src_off, grid_dur, ffmpeg, ffprobe,
                                   tmpdir / "route.gpx", args.dry_run, progress)
        stats["gps_s"] += time.monotonic() - t0

        if gpx_path is None:
            # No GPS/SEI telemetry at all -- neither overlay can be built.
            # Drop their still-pending steps so the job ETA stops counting
            # work that isn't coming.
            progress.abandon("map_render", "map_scale", "gauge_render")
        else:
            if args.map:
                t0 = time.monotonic()
                if args.landscape:
                    # No fixed native aspect of its own -- landscape_layout
                    # always places the map last in the sidebar, where it
                    # gets the remainder-clamped height. Assume a real
                    # camera's aspect (same as every Tesla angle) just to
                    # solve for w_side/H here; the actual per-tile heights
                    # (and any rounding drift) are settled for real in
                    # build_filter_landscape once this dims entry exists.
                    placeholder = dims.get("back") or dims.get(map_source) or (1280, 960)
                    _, _, _, _, w_side, _, _ = landscape_layout(
                        {**dims, MAP_TILE_KEY: placeholder}, args.feature)
                    map_dims = (w_side, round(w_side * placeholder[1] / placeholder[0] / 2) * 2)
                else:
                    map_dims = dims.get("back") or dims.get(map_source) or (1280, 960)
                # Persist the tile (not tmpdir): it's a slow step, it's a
                # useful standalone artifact, and it survives a later
                # grid-encode failure.
                map_out = out_dir / f"{session_name}_maptile.mp4"
                built = build_map_tile(gpx_path, grid_dur, map_dims, ffmpeg,
                                       tools.map_venv_py, tools.map_gopro, tools.map_font,
                                       args.map_zoom, args.map_mag, map_out, tmpdir,
                                       args.dry_run, progress)
                stats["map_s"] += time.monotonic() - t0
                angle_paths[MAP_TILE_KEY] = built
                dims[MAP_TILE_KEY] = map_dims

            if args.gauge:
                t0 = time.monotonic()
                # A solo hero is guaranteed by setup_tools (dies early on a
                # paired --feature), so there's exactly one hero angle here.
                hero_angle = hero_angles_for(args.feature)[0]
                # Persist the composited hero (not tmpdir): same rationale as
                # the map tile -- a real, potentially slow, standalone
                # artifact, and it replaces angle_paths[hero_angle] below so
                # both build_filter and build_filter_landscape pick it up
                # without either needing to know a gauge was composited.
                gauge_out = out_dir / f"{session_name}_{hero_angle}_gauge.mp4"
                built = build_gauge_overlay(
                    angle_paths[hero_angle], gpx_path, dims[hero_angle],
                    args.gauge_units, ffmpeg, tools.map_venv_py, tools.map_gopro,
                    tools.map_font, gauge_out, tmpdir, args.dry_run, progress)
                stats["gauge_s"] += time.monotonic() - t0
                angle_paths[hero_angle] = built
                stats["gauge_built"] = True

    if len(angle_paths) < 2:
        log("\nOnly one camera angle found -- skipping grid, per-angle concat above is the "
            "final output.")
        progress.abandon("grid")
        return None

    filter_fn = build_filter_landscape if args.landscape else build_filter
    filter_text, input_order, final_w, final_h = filter_fn(
        dims, angle_paths, tools.has_text, tools.font, plan.epoch, args.max_dim,
        args.native, args.speed, args.feature
    )
    filter_path = tmpdir / "grid.filter"
    filter_path.write_text(filter_text)
    # The graph is debugging detail -- worth printing when someone asked to see
    # the commands, just noise above a progress bar.
    if progress.verbose:
        log(f"\n== filter graph ==\n{filter_text}\n")
    log(f"final canvas: {final_w}x{final_h}")

    cmd = [ffmpeg, "-y"]
    for p in input_order:
        cmd += ["-i", str(p)]
    cmd += filter_graph_args(ffmpeg, filter_path) + ["-map", "[out]"]

    enc_args, warning = encoder_args(args.native, args.quality, args.max_dim, final_w, final_h)
    if warning:
        log(f"WARNING: {warning}")
    if args.quality == "high" and not warning:
        log("NOTE: --quality high requested -- using software encode (libx264, CRF 18) "
            "instead of hardware.")
    cmd += enc_args

    suffix = "_landscape" if args.landscape else ""
    suffix += "" if args.feature == "front" else f"_feature-{args.feature}"
    suffix += "_blurred" if args.blur_faces else ""
    suffix += "_gauge" if stats.get("gauge_built") else ""
    suffix += "_map" if MAP_TILE_KEY in angle_paths else ""
    out_grid = out_dir / f"{session_name}_grid{suffix}.mp4"
    cmd += ["-an", "-movflags", "+faststart", str(out_grid)]

    log(f"\n== building grid -> {out_grid} ==")
    # ffmpeg reports the OUTPUT time, which --speed has already scaled, so the
    # total to measure it against is the source length divided by --speed.
    grid_seconds = None
    if not args.dry_run:
        grid_seconds = probe_duration(ffprobe, next(iter(angle_paths.values())))
    grid_seconds = (grid_seconds or max(plan.footage.values())) / max(0.01, args.speed)
    progress.begin("grid", "grid encode", grid_seconds, out=out_grid)
    t0 = time.monotonic()
    run(cmd, args.dry_run, what="ffmpeg (grid)", progress=progress, total=grid_seconds)
    stats["grid_s"] = time.monotonic() - t0
    progress.end()
    return out_grid, final_w, final_h


def print_stats(args, tools: Tools, plan: Plan, stats: dict, drifts: dict,
                angle_paths: dict, out_grid: Path, final_w: int, final_h: int,
                t_job: float) -> None:
    """Print the final STATS block (sizes, timings, and any clock-drift note)."""
    ffprobe = tools.ffprobe
    elapsed = time.monotonic() - t_job
    out_bytes = sum(p.stat().st_size for p in [out_grid] + list(angle_paths.values())
                    if p.exists())
    grid_dur = probe_duration(ffprobe, out_grid) or 0.0
    log("\n" + "=" * 60)
    log("STATS")
    log("=" * 60)
    log(f"  input            {plan.n_clips} clips, {human_bytes(plan.in_bytes)}")
    log(f"  output           {human_bytes(out_bytes)} "
        f"(grid {human_bytes(out_grid.stat().st_size)} @ {final_w}x{final_h})")
    log(f"  footage          {human_time(grid_dur)} of combined video")
    log(f"  concat           {human_time(stats['concat_s'])} "
        f"({stats['built']} built, {stats['reused']} reused)")
    if (args.map or args.gauge) and stats["gps_s"] > 0:
        log(f"  GPS extract      {human_time(stats['gps_s'])}")
    if args.map:
        map_built = MAP_TILE_KEY in angle_paths
        log(f"  route map        {human_time(stats['map_s'])}"
            f"{'' if map_built else '  (no GPS -- tile skipped)'}")
    if args.gauge:
        log(f"  gauge overlay    {human_time(stats['gauge_s'])}"
            f"{'' if stats['gauge_built'] else '  (no GPS -- overlay skipped)'}")
    if args.blur_faces:
        log(f"  face blur        {human_time(stats['blur_s'])} "
            f"({stats['blurred']} blurred, {stats['blur_reused']} reused)")
    log(f"  grid encode      {human_time(stats['grid_s'])}"
        + (f"  ({grid_dur / stats['grid_s']:.2f}x realtime)" if stats["grid_s"] > 0 else ""))
    log(f"  total            {human_time(elapsed)}"
        + (f"  ({grid_dur / elapsed:.2f}x realtime)" if elapsed > 0 else ""))
    worst = max(drifts.values()) if drifts else 0.0
    if worst > 1.0:
        log("")
        log(f"  NOTE: the recording is missing ~{worst:.1f}s of wall-clock time "
            f"(gaps between clips).")
        log(f"        Concatenating squeezes gaps out, so the burned-in clock ends up to "
            f"~{worst:.0f}s")
        log(f"        behind true wall-clock by the end. Timestamps early in the video are "
            f"unaffected.")
    log("=" * 60)


def report_interrupt(args, progress: Progress, out_dir: Path) -> int:
    """Ctrl-C: say what was in flight, bin the half-written file it was producing,
    and point out that the finished work is cached. A partial mp4 left lying about
    is worse than useless -- it looks like a real output, and (for a concat) the
    next run's cache check would have to notice it isn't."""
    progress.close()
    step = progress.running_step()
    log("")
    log(f"Interrupted{f' during: {step.label}' if step else ''}.")
    partial = progress.out
    if partial is not None and not args.dry_run and Path(partial).exists():
        try:
            Path(partial).unlink()
            log(f"Removed the incomplete {Path(partial).name}.")
        except OSError as e:
            log(f"NOTE: could not remove the incomplete {partial} ({e}) -- "
                f"delete it before re-running.")
    done = sum(1 for s in progress.state if s in ("done", "skipped"))
    log(f"{done} of {len(progress.steps)} steps finished. Completed per-camera "
        f"videos are cached in {out_dir / CACHE_FILE},")
    log("so re-running the same command resumes rather than starting over.")
    return 130


def main(argv=None) -> int:
    global _PROGRESS
    args = build_parser().parse_args(argv)

    if args.speed <= 0:
        die(f"--speed {args.speed} is out of range; use a positive multiplier "
            f"(e.g. 1 for normal, 2 for 2x, 0.5 for half speed).")

    t_job = time.monotonic()
    stats = {"concat_s": 0.0, "grid_s": 0.0, "reused": 0, "built": 0,
             "blur_s": 0.0, "blurred": 0, "blur_reused": 0, "gps_s": 0.0,
             "map_s": 0.0, "gauge_s": 0.0, "gauge_built": False}

    folder = args.folder.resolve()
    if not folder.is_dir():
        die(f"Not a folder: {folder}")
    out_dir = (args.output_dir or folder).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    session_name = folder.name

    tools = setup_tools(args)
    plan = plan_job(args, tools, folder, out_dir, session_name)

    # --dry-run prints commands rather than running them, so there's nothing to
    # measure -- it takes the same path as --verbose.
    progress = Progress(plan.steps, verbose=args.verbose or args.dry_run,
                        ansi=False if args.no_progress else None)
    _PROGRESS = progress
    if hasattr(signal, "SIGINFO"):
        # macOS/BSD Ctrl-T asks a running program where it's at. Answer it -- it
        # costs nothing and it's the only status you get under --verbose or when
        # the output is a log file rather than a terminal.
        signal.signal(signal.SIGINFO, lambda *_: log(progress.status_line()))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cache = load_cache(out_dir)
            angle_paths, drifts = build_per_camera(args, tools, plan, cache, stats,
                                                   tmpdir, progress)
            result = build_grid(args, tools, plan, angle_paths, stats, tmpdir, progress)
            if result is None:
                return 0
            out_grid, final_w, final_h = result
    except KeyboardInterrupt:
        return report_interrupt(args, progress, out_dir)
    finally:
        # Leave the terminal in a sane state on Ctrl-C or a crash, not halfway
        # through a redrawn bar.
        progress.close()
        _PROGRESS = None

    if args.dry_run:
        log("\nDry run -- nothing was written.")
        return 0

    print_stats(args, tools, plan, stats, drifts, angle_paths, out_grid,
                final_w, final_h, t_job)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Ctrl-C outside a build step (during discovery/probing) -- nothing to
        # clean up, and a Python traceback would only obscure that.
        print("\nInterrupted.", file=sys.stderr, flush=True)
        sys.exit(130)
