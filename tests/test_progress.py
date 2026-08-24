"""Tests for the progress display in tesla_combine.py.

No subprocesses and no terminal -- these drive Progress with a fake clock and
feed the parsers the exact bytes ffmpeg and deface really emit.
"""
import io

import pytest

import tesla_combine as tc


class Clock:
    """A hand-cranked replacement for time.monotonic."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def tick(self, seconds):
        self.t += seconds
        return self.t


def make_progress(steps=None, **kw):
    clock = Clock()
    steps = steps if steps is not None else [
        tc.Step("concat", "concat front", 600.0),
        tc.Step("blur", "blur faces front", 600.0),
        tc.Step("grid", "grid encode", 600.0),
    ]
    kw.setdefault("stream", io.StringIO())
    kw.setdefault("ansi", False)
    return tc.Progress(steps, now=clock, **kw), clock


# --- ffmpeg -progress parsing ----------------------------------------------

def test_parse_progress_out_time_us_to_seconds():
    assert tc.parse_ffmpeg_progress("out_time_us=2916667\n") == ("out_time", pytest.approx(2.916667))


def test_parse_progress_out_time_ms_is_also_microseconds():
    # ffmpeg's out_time_ms is misnamed; it carries microseconds like out_time_us.
    assert tc.parse_ffmpeg_progress("out_time_ms=2916667") == ("out_time", pytest.approx(2.916667))


def test_parse_progress_out_time_clock_form_to_seconds():
    key, value = tc.parse_ffmpeg_progress("out_time=01:02:03.500000")
    assert key == "out_time"
    assert value == pytest.approx(3723.5)


def test_parse_progress_speed():
    assert tc.parse_ffmpeg_progress("speed=  6.82x") == ("speed", pytest.approx(6.82))


def test_parse_progress_handles_na_and_junk():
    # ffmpeg emits N/A before the first frame is out.
    assert tc.parse_ffmpeg_progress("out_time_us=N/A") == (None, None)
    assert tc.parse_ffmpeg_progress("speed=N/A") == (None, None)
    assert tc.parse_ffmpeg_progress("no equals sign here") == (None, None)


def test_parse_progress_passes_other_keys_through():
    assert tc.parse_ffmpeg_progress("progress=end") == ("progress", "end")


# --- deface/tqdm parsing ----------------------------------------------------

def test_parse_tqdm_fraction():
    line = "  8%|############    | 3595/45800 [16:35<3:22:49,  3.47it/s]"
    assert tc.parse_tqdm_fraction(line) == pytest.approx(3595 / 45800)


def test_parse_tqdm_takes_the_last_update_in_a_chunk():
    # Updates arrive \r-separated, several per read.
    chunk = ("  1%| | 591/45800 [02:47<3:35:51,  3.49it/s]\r"
             "  1%| | 592/45800 [02:48<3:40:31,  3.42it/s]\r"
             "  8%| | 3595/45800 [16:35<3:22:49,  3.47it/s]")
    assert tc.parse_tqdm_fraction(chunk) == pytest.approx(3595 / 45800)


def test_parse_tqdm_ignores_non_progress_output():
    assert tc.parse_tqdm_fraction("Input:  /tmp/x.mp4\nOutput: /tmp/y.mp4") is None
    assert tc.parse_tqdm_fraction("IMAGEIO FFMPEG_WRITER WARNING: resizing from "
                                  "(2896, 1876) to (2896, 1888)") is None


def test_parse_tqdm_zero_total_is_not_a_fraction():
    assert tc.parse_tqdm_fraction("0%| | 0/0 [00:00<?, ?it/s]") is None


# --- job weighting and ETA --------------------------------------------------

def test_eta_starts_from_the_priors():
    p, _ = make_progress()
    # 600s of footage through each of concat/blur/grid at the prior rates.
    expected = sum(600.0 / tc.RATE_PRIORS[k] for k in ("concat", "blur", "grid"))
    assert p.remaining() == pytest.approx(expected)


def test_finished_step_recalibrates_its_kind():
    p, clock = make_progress([tc.Step("blur", "blur front", 600.0),
                              tc.Step("blur", "blur back", 600.0)])
    before = p.remaining()
    p.begin("blur", "blur front", 600.0)
    clock.tick(1200.0)          # half the prior rate: 600s of footage took 1200s
    p.end()
    assert p.rates["blur"] == pytest.approx(0.5)
    # The remaining blur is now costed at the measured rate, not the prior.
    assert p.remaining() == pytest.approx(1200.0)
    assert p.remaining() > before / 2


def test_step_too_short_to_measure_leaves_the_rate_alone():
    p, clock = make_progress([tc.Step("concat", "concat front", 600.0)])
    p.begin("concat", "concat front", 600.0)
    clock.tick(0.05)
    p.end()
    assert p.rates["concat"] == tc.RATE_PRIORS["concat"]


def test_end_with_measured_work_overrides_the_estimate():
    p, clock = make_progress([tc.Step("concat", "concat front", 600.0)])
    p.begin("concat", "concat front", 600.0)
    clock.tick(60.0)
    p.end(work=300.0)           # the concat turned out to hold 300s, not 600s
    assert p.rates["concat"] == pytest.approx(5.0)


def test_skip_drops_the_step_weight_from_the_job():
    p, _ = make_progress()
    before = p.remaining()
    p.skip("blur", "blur faces front", "front: reusing existing blurred video")
    assert p.remaining() == pytest.approx(before - 600.0 / tc.RATE_PRIORS["blur"])
    assert p.state[1] == "skipped"


def test_abandon_drops_every_pending_step_of_a_kind():
    p, _ = make_progress([tc.Step("map_gps", "map GPS", 600.0),
                          tc.Step("map_render", "map render", 600.0),
                          tc.Step("map_scale", "map upscale", 600.0)])
    p.abandon("map_render", "map_scale")
    assert p.remaining() == pytest.approx(600.0 / tc.RATE_PRIORS["map_gps"])


def test_rescale_pending_scales_only_unstarted_steps():
    p, _ = make_progress()
    p.begin("concat", "concat front", 600.0)
    p.rescale_pending(0.5)
    assert [s.work for s in p.steps] == [600.0, 300.0, 300.0]


def test_rescale_pending_ignores_absurd_ratios():
    p, _ = make_progress()
    p.rescale_pending(0.0)
    p.rescale_pending(1000.0)
    assert [s.work for s in p.steps] == [600.0, 600.0, 600.0]


def test_job_fraction_grows_as_work_completes():
    p, clock = make_progress()
    assert p.job_fraction() == pytest.approx(0.0)
    p.begin("concat", "concat front", 600.0)
    clock.tick(60.0)
    p.end()
    mid = p.job_fraction()
    p.begin("blur", "blur faces front", 600.0)
    clock.tick(600.0)
    p.end()
    assert 0.0 < mid < p.job_fraction() < 1.0


def test_unplanned_step_is_appended_rather_than_lost():
    p, _ = make_progress([tc.Step("concat", "concat front", 600.0)])
    p.begin("grid", "grid encode", 120.0)
    assert p.steps[-1].kind == "grid"
    assert p.running_step().label == "grid encode"


# --- rendering --------------------------------------------------------------

def test_update_never_reaches_100_or_goes_backwards():
    p, _ = make_progress()
    p.begin("concat", "concat front", 600.0)
    p.update(frac=1.5)
    assert p.frac == pytest.approx(0.999)   # only end() means done
    p.update(frac=0.2)
    assert p.frac == pytest.approx(0.999)   # never rewinds


def test_lines_fit_the_terminal_width():
    p, clock = make_progress()
    p.ansi = True
    p.begin("concat", "concat front camera (32 clips)", 600.0)
    clock.tick(30.0)
    p.update(frac=0.5, speed=22.0)
    for width in (40, 60, 80, 100, 140):
        for line in p._lines(width):
            assert len(line) <= width, (width, line)


def test_lines_show_step_and_job_progress():
    p, clock = make_progress()
    p.ansi = True
    p.begin("concat", "concat front", 600.0)
    clock.tick(30.0)
    p.update(frac=0.5, speed=22.0)
    step_line, job_line = p._lines(100)
    assert step_line.startswith("[ 1/3] concat front")
    assert " 50%" in step_line and "22.0x" in step_line
    assert job_line.lstrip().startswith("job")
    assert "ETA" in job_line


def test_indeterminate_step_shows_elapsed_not_a_percentage():
    p, clock = make_progress()
    p.ansi = True
    p.begin("grid", "route map render", 600.0, determinate=False)
    clock.tick(134.0)
    step_line = p._lines(100)[0]
    assert "2m 14s elapsed" in step_line
    assert "%" not in step_line.split("elapsed")[0]


def test_late_fraction_revokes_an_earlier_give_up():
    """deface can spend minutes opening a multi-hour input before its first tqdm
    tick, long past the grace period that gives up on a percentage. When the real
    counter finally arrives the bar has to come back, or the longest steps in the
    job show elapsed-time only for hours."""
    p, clock = make_progress()
    p.ansi = True
    p.begin("blur", "blur faces front", 600.0)
    clock.tick(200.0)
    p.indeterminate()
    assert "elapsed" in p._lines(100)[0]
    p.update(frac=0.25)                        # the counter finally reports
    step_line = p._lines(100)[0]
    assert " 25%" in step_line
    assert "elapsed" not in step_line


def test_plain_mode_emits_no_ansi_escapes():
    stream = io.StringIO()
    p, clock = make_progress(stream=stream, ansi=False)
    p.begin("concat", "concat front", 600.0)
    clock.tick(60.0)
    p.update(frac=0.5)
    assert "\x1b" not in stream.getvalue()


def test_plain_mode_throttles_repeated_updates():
    stream = io.StringIO()
    p, clock = make_progress(stream=stream, ansi=False)
    p.begin("concat", "concat front", 600.0)   # the step's opening line
    stream.truncate(0), stream.seek(0)
    for _ in range(20):
        clock.tick(0.5)                        # 10s and 2%: inside the throttle
        p.update(frac=p.frac + 0.001)
    assert stream.getvalue() == ""


def test_plain_mode_prints_once_the_step_advances_enough():
    stream = io.StringIO()
    p, clock = make_progress(stream=stream, ansi=False)
    p.begin("concat", "concat front", 600.0)
    stream.truncate(0), stream.seek(0)
    clock.tick(1.0)
    p.update(frac=0.5)
    assert stream.getvalue().count("\n") == 1
    assert "concat front" in stream.getvalue()


def test_restore_does_not_reprint_in_plain_mode():
    stream = io.StringIO()
    p, _ = make_progress(stream=stream, ansi=False)
    p.begin("concat", "concat front", 600.0)
    stream.truncate(0), stream.seek(0)
    p.restore()
    assert stream.getvalue() == ""


def test_closed_display_stops_drawing():
    stream = io.StringIO()
    p, _ = make_progress(stream=stream, ansi=True)
    p.begin("concat", "concat front", 600.0)
    p.close()
    before = stream.getvalue()
    p.redraw(force=True)
    assert stream.getvalue() == before


def test_verbose_mode_draws_nothing():
    stream = io.StringIO()
    p, _ = make_progress(stream=stream, ansi=True, verbose=True)
    assert p.ansi is False
    p.begin("concat", "concat front", 600.0)
    p.update(frac=0.5)
    assert stream.getvalue() == ""


def test_ascii_fallback_when_the_stream_cannot_encode_blocks():
    class AsciiStream(io.StringIO):
        encoding = "ascii"

    p, _ = make_progress(stream=AsciiStream(), ansi=True)
    p.begin("concat", "concat front", 600.0)
    p.update(frac=0.5)
    line = p._lines(100)[0]
    assert tc.BAR_FULL not in line
    assert tc.BAR_FULL_ASCII in line


def test_status_line_between_steps():
    p, _ = make_progress()
    assert "between steps" in p.status_line()
    p.begin("concat", "concat front", 600.0)
    assert "concat front" in p.status_line()


# --- human_eta --------------------------------------------------------------

def test_human_eta_keeps_seconds_when_short():
    assert tc.human_eta(95) == "1m 35s"


def test_human_eta_rounds_to_minutes_when_long():
    assert tc.human_eta(3600 + 9 * 60 + 47) == "1h 10m"
    assert tc.human_eta(2000) == "33m"


# --- footage estimation -----------------------------------------------------

def test_estimate_concat_seconds_uses_the_trim_duration_when_known():
    assert tc.estimate_concat_seconds(None, [], 0.0, 42.5) == 42.5


def test_estimate_concat_seconds_from_filename_spacing(monkeypatch, tmp_path):
    # Three back-to-back minute clips; only the last one gets probed.
    names = ["2026-07-26_13-06-25-front.mp4",
             "2026-07-26_13-07-25-front.mp4",
             "2026-07-26_13-08-25-front.mp4"]
    clips = [tmp_path / n for n in names]
    probed = []
    monkeypatch.setattr(tc, "probe_duration", lambda ff, p: probed.append(p) or 50.0)
    assert tc.estimate_concat_seconds(None, clips, 0.0, None) == pytest.approx(170.0)
    assert probed == [clips[-1]]


def test_estimate_concat_seconds_caps_a_recording_gap_at_a_clip_length(monkeypatch, tmp_path):
    # A 10-minute gap between clips is dead air, not 10 minutes of footage.
    clips = [tmp_path / "2026-07-26_13-06-25-front.mp4",
             tmp_path / "2026-07-26_13-16-25-front.mp4"]
    monkeypatch.setattr(tc, "probe_duration", lambda ff, p: 60.0)
    assert tc.estimate_concat_seconds(None, clips, 0.0, None) == pytest.approx(120.0)


def test_estimate_concat_seconds_subtracts_the_trim_offset(monkeypatch, tmp_path):
    clips = [tmp_path / "2026-07-26_13-06-25-front.mp4"]
    monkeypatch.setattr(tc, "probe_duration", lambda ff, p: 60.0)
    assert tc.estimate_concat_seconds(None, clips, 15.0, None) == pytest.approx(45.0)


# --- step planning ----------------------------------------------------------

class Args:
    def __init__(self, **kw):
        self.blur_faces = False
        self.map = False
        self.map_mag = 2.0
        self.gauge = False
        self.fsd_scoreboard = False
        self.speed = 1.0
        self.__dict__.update(kw)


def test_plan_steps_bare_run_is_a_concat_per_camera_plus_the_grid():
    sel = {"front": (), "back": ()}
    steps = tc.plan_steps(Args(), sel, {"front": 600.0, "back": 600.0})
    assert [s.kind for s in steps] == ["concat", "concat", "grid"]


def test_plan_steps_interleaves_blur_with_its_camera():
    sel = {"front": (), "back": ()}
    steps = tc.plan_steps(Args(blur_faces=True), sel, {"front": 600.0, "back": 600.0})
    # Order matters: it must match what build_per_camera actually does.
    assert [s.kind for s in steps] == ["concat", "blur", "concat", "blur", "grid"]


def test_plan_steps_map_adds_the_upscale_only_when_magnifying():
    sel = {"front": (), "back": ()}
    footage = {"front": 600.0, "back": 600.0}
    with_mag = [s.kind for s in tc.plan_steps(Args(map=True, map_mag=2.0), sel, footage)]
    without = [s.kind for s in tc.plan_steps(Args(map=True, map_mag=1.0), sel, footage)]
    assert "map_scale" in with_mag
    assert "map_scale" not in without
    assert without[-3:] == ["map_gps", "map_render", "grid"]


def test_plan_steps_grid_work_accounts_for_speed():
    sel = {"front": (), "back": ()}
    steps = tc.plan_steps(Args(speed=2.0), sel, {"front": 600.0, "back": 600.0})
    assert steps[-1].kind == "grid"
    assert steps[-1].work == pytest.approx(300.0)


def test_plan_steps_single_camera_has_no_grid():
    steps = tc.plan_steps(Args(), {"front": ()}, {"front": 600.0})
    assert [s.kind for s in steps] == ["concat"]
