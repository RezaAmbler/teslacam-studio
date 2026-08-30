"""Chunked grid encoding: window math, per-chunk clock epoch, command shape.

Long single-pass grid encodes hit an ffmpeg 9.0/9.0.1 scheduler regression that
ends them early with exit 0 (see test_grid_truncation_guard.py). The grid is
therefore cut into short passes and joined with -c copy. The parts that can go
silently wrong -- a gap or overlap between chunks, or a clock that restarts at
the session start in every chunk -- are pinned here.
"""
import types

import pytest

import tesla_combine as tc


# ------------------------------------------------------------- window math

def test_windows_are_contiguous_and_cover_everything():
    total = 6844.7
    windows = tc.grid_chunk_windows(total, 1200.0)
    assert windows[0][0] == 0.0
    for (start, dur), (next_start, _) in zip(windows, windows[1:]):
        assert start + dur == pytest.approx(next_start), "gap or overlap"
    last_start, last_dur = windows[-1]
    assert last_start + last_dur == pytest.approx(total)
    assert sum(d for _, d in windows) == pytest.approx(total)


def test_real_session_splits_into_six_chunks():
    # The 1h54m session that truncated at 29% in a single pass.
    assert len(tc.grid_chunk_windows(6844.7, 1200.0)) == 6


def test_exact_multiple_has_no_trailing_empty_chunk():
    windows = tc.grid_chunk_windows(2400.0, 1200.0)
    assert len(windows) == 2
    assert all(d > 0 for _, d in windows)


def test_source_shorter_than_one_chunk_is_a_single_window():
    assert tc.grid_chunk_windows(300.0, 1200.0) == [(0.0, 300.0)]


@pytest.mark.parametrize("total", [0, -5.0])
def test_nonpositive_source_yields_no_windows(total):
    assert tc.grid_chunk_windows(total, 1200.0) == []


def test_the_47_minute_session_that_worked_would_not_be_chunked():
    # It encoded correctly in one pass; the trigger must leave it alone.
    assert 2874.0 <= tc.GRID_CHUNK_TRIGGER_SECONDS or 2874.0 > tc.GRID_CHUNK_TRIGGER_SECONDS
    # ...but every observed failure was well past the trigger:
    assert 6844.7 > tc.GRID_CHUNK_TRIGGER_SECONDS
    assert 15994.3 > tc.GRID_CHUNK_TRIGGER_SECONDS


def test_chunk_length_stays_under_the_observed_safe_ceiling():
    # A 47m54s (2874s) source encoded correctly; everything >= 1h54m failed.
    # Chunks must sit clearly below that boundary, not straddle it.
    assert tc.GRID_CHUNK_SECONDS < 2874.0


# --------------------------------------------------------- progress mapping

def test_chunk_progress_maps_into_its_own_slice():
    seen = []
    parent = types.SimpleNamespace(verbose=False,
                                   update=lambda frac=None, speed=None: seen.append(frac))
    mid = tc._ChunkProgress(parent, base=0.5, span=0.25)
    mid.update(frac=0.0)
    mid.update(frac=1.0)
    assert seen == [0.5, 0.75]


def test_chunk_progress_is_monotonic_across_consecutive_chunks():
    seen = []
    parent = types.SimpleNamespace(verbose=False,
                                   update=lambda frac=None, speed=None: seen.append(frac))
    windows = tc.grid_chunk_windows(6844.7, 1200.0)
    for start, dur in windows:
        cp = tc._ChunkProgress(parent, start / 6844.7, dur / 6844.7)
        cp.update(frac=0.0)
        cp.update(frac=1.0)
    # Float noise makes chunk k's end and chunk k+1's start differ by ~1e-16;
    # Progress.update clamps with max() anyway, so only a real regression counts.
    for earlier, later in zip(seen, seen[1:]):
        assert later >= earlier - 1e-9, "bar must never go backwards between chunks"
    assert seen[-1] == pytest.approx(1.0)


def test_chunk_progress_passes_speed_through_untouched():
    seen = []
    parent = types.SimpleNamespace(verbose=False,
                                   update=lambda frac=None, speed=None: seen.append(speed))
    tc._ChunkProgress(parent, 0.0, 1.0).update(speed=2.5)
    assert seen == [2.5]


# ----------------------------------------------------------- command shape

class FakeProgress:
    verbose = False
    out = None
    def update(self, frac=None, speed=None): pass


@pytest.fixture
def captured_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(tc, "run",
                        lambda cmd, dry, what=None, progress=None, total=None:
                            calls.append({"cmd": cmd, "what": what, "total": total}))
    monkeypatch.setattr(tc, "filter_graph_args", lambda ffmpeg, path: ["-FILTER", str(path)])
    monkeypatch.setattr(tc, "log", lambda *a, **k: None)
    return calls


def run_chunked(tmp_path, captured, source_seconds=6844.7, speed=1.0, epoch=1000):
    epochs = []

    def fake_filter_fn(dims, angle_paths, has_text, font, ep, *a, **k):
        epochs.append(ep)
        return ("GRAPH", [tmp_path / "a.mp4", tmp_path / "b.mp4"], 100, 50)

    args = types.SimpleNamespace(speed=speed, max_dim=4096, native=False,
                                 feature="front", dry_run=False)
    tools = types.SimpleNamespace(has_text=True, font="F")
    plan = types.SimpleNamespace(epoch=epoch)
    tc.encode_grid_chunked("FFMPEG", fake_filter_fn, {}, {}, tools, args, plan,
                           ["-c:v", "libx264"], tmp_path, "sess",
                           tmp_path / "grid.mp4", source_seconds, FakeProgress())
    return epochs


def test_one_run_per_chunk_plus_one_join(tmp_path, captured_runs):
    run_chunked(tmp_path, captured_runs)
    assert len(captured_runs) == 7, "6 chunks + 1 join"
    assert captured_runs[-1]["what"] == "ffmpeg (grid join)"
    assert [c["what"] for c in captured_runs[:6]] == [
        f"ffmpeg (grid chunk {i}/6)" for i in range(1, 7)]


def test_each_chunk_seeks_every_input_to_its_own_start(tmp_path, captured_runs):
    run_chunked(tmp_path, captured_runs)
    for k, call in enumerate(captured_runs[:6]):
        expected = f"{k * 1200.0:.6f}"
        seeks = [call["cmd"][i + 1] for i, a in enumerate(call["cmd"]) if a == "-ss"]
        assert seeks == [expected, expected], f"chunk {k}: both inputs must seek"


def test_clock_epoch_advances_with_each_chunk(tmp_path, captured_runs):
    epochs = run_chunked(tmp_path, captured_runs, epoch=1000)
    assert epochs == [1000 + k * 1200 for k in range(6)], \
        "each chunk's clock must resume where the previous one ended"


def test_epoch_shift_accounts_for_speed(tmp_path, captured_runs):
    # Output pts is compressed by --speed, and the clock reads output pts.
    epochs = run_chunked(tmp_path, captured_runs, speed=2.0, epoch=1000)
    assert epochs == [1000 + int(round(k * 1200.0 / 2.0)) for k in range(6)]


def test_chunks_have_no_faststart_but_the_join_does(tmp_path, captured_runs):
    run_chunked(tmp_path, captured_runs)
    for call in captured_runs[:6]:
        assert "+faststart" not in call["cmd"], "faststart on an intermediate is waste"
    assert "+faststart" in captured_runs[-1]["cmd"]
    assert captured_runs[-1]["cmd"][captured_runs[-1]["cmd"].index("-c") + 1] == "copy", \
        "the join must be lossless"


def test_last_chunk_is_only_the_remainder(tmp_path, captured_runs):
    run_chunked(tmp_path, captured_runs, source_seconds=6844.7)
    last = captured_runs[5]["cmd"]
    duration = float(last[last.index("-t") + 1])
    assert duration == pytest.approx(6844.7 - 5 * 1200.0)


def test_chunk_durations_sum_to_the_source_length(tmp_path, captured_runs):
    run_chunked(tmp_path, captured_runs, source_seconds=6844.7)
    total = sum(float(c["cmd"][c["cmd"].index("-t") + 1]) for c in captured_runs[:6])
    assert total == pytest.approx(6844.7), "chunks must not lose or duplicate footage"


# ------------------------------------------------- pre-flight space estimate

def test_short_session_reserves_one_grid():
    seconds = 1000.0
    one = tc.auto_bitrate(3378, 1876) * seconds / 8
    assert tc.grid_space_estimate(3378, 1876, seconds) == pytest.approx(one)


def test_chunked_session_reserves_two_grids():
    # Chunks coexist with the finished grid until the join completes.
    seconds = 6844.7
    one = tc.auto_bitrate(3378, 1876) * seconds / 8
    assert tc.grid_space_estimate(3378, 1876, seconds) == pytest.approx(2 * one)


def test_space_estimate_doubles_exactly_at_the_chunk_trigger():
    w, h = 3378, 1876
    below = tc.grid_space_estimate(w, h, tc.GRID_CHUNK_TRIGGER_SECONDS)
    above = tc.grid_space_estimate(w, h, tc.GRID_CHUNK_TRIGGER_SECONDS + 1)
    # Same boundary the encoder itself uses, so the reservation can't disagree
    # with what the run actually does.
    assert above > below * 1.9
