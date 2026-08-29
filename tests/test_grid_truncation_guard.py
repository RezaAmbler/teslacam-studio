"""Guard against ffmpeg silently truncating a long grid encode.

The failure this protects against is not hypothetical: on a real 1h54m session
ffmpeg 9.0.1 wrote 32m57s of grid, exited 0, printed no warning at -loglevel
warning, and the run reported STATS as a success. Four separate runs truncated
at 29%, 53%, 53% and 57% of their inputs, so the guard has to catch a wide
range of losses without false-positiving on the second-or-two of legitimate
drift between per-camera concats.
"""
import pytest

import tesla_combine as tc


class FakeProbe:
    """Stands in for ffprobe; probe_duration() only needs the JSON shape."""
    def __init__(self, duration):
        self.duration = duration


@pytest.fixture
def probe_duration_returning(monkeypatch):
    def _set(value):
        monkeypatch.setattr(tc, "probe_duration", lambda ffprobe, path: value)
    return _set


# ---------------------------------------------------------------- pure predicate

def test_full_length_output_is_not_short():
    assert not tc.grid_output_is_short(6844.7, 6844.7)


def test_real_observed_truncation_is_caught():
    # The actual numbers from the 1h54m session: 32m57s written of 1h54m04s.
    assert tc.grid_output_is_short(1977.3, 6844.7)


@pytest.mark.parametrize("actual,expected", [
    (8500.6, 15994.3),   # run 1, 53%
    (9095.0, 15994.3),   # run 2, 57%
    (8424.1, 15994.3),   # run 3, 53%, different filesystem
])
def test_every_observed_truncation_is_caught(actual, expected):
    assert tc.grid_output_is_short(actual, expected)


def test_legitimate_inter_camera_drift_is_not_flagged():
    # Concats of the same session differ slightly; `shortest` trims to the
    # shortest input, so the output can be a beat under the probed expectation.
    assert not tc.grid_output_is_short(6844.7, 6845.8)
    assert not tc.grid_output_is_short(15994.3, 15996.3)


def test_a_longer_output_than_expected_is_not_short():
    # Without `shortest`, the output runs to the LONGEST input instead.
    assert not tc.grid_output_is_short(6845.8, 6844.7)


@pytest.mark.parametrize("expected", [None, 0, -1.0])
def test_unknown_expectation_never_fails(expected):
    assert not tc.grid_output_is_short(100.0, expected)


def test_unprobeable_actual_is_not_judged_here():
    # None means "couldn't probe"; verify_grid_output handles that separately
    # so the two failures get distinct error messages.
    assert not tc.grid_output_is_short(None, 6844.7)


def test_tolerance_boundary_is_proportional():
    assert not tc.grid_output_is_short(99.5, 100.0)   # 0.5% under, within tol
    assert tc.grid_output_is_short(98.0, 100.0)       # 2% under, flagged


# ------------------------------------------------------------------- the guard

def test_verify_passes_on_full_length(probe_duration_returning, tmp_path):
    probe_duration_returning(6844.7)
    tc.verify_grid_output(None, tmp_path / "grid.mp4", 6844.7, dry_run=False)


def test_verify_dies_on_truncated_output(probe_duration_returning, tmp_path):
    probe_duration_returning(1977.3)
    with pytest.raises(SystemExit):
        tc.verify_grid_output(None, tmp_path / "grid.mp4", 6844.7, dry_run=False)


def test_verify_dies_when_output_cannot_be_probed(probe_duration_returning, tmp_path):
    probe_duration_returning(None)
    with pytest.raises(SystemExit):
        tc.verify_grid_output(None, tmp_path / "grid.mp4", 6844.7, dry_run=False)


def test_verify_is_a_noop_under_dry_run(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(tc, "probe_duration",
                        lambda *a: called.append(a) or 1.0)
    tc.verify_grid_output(None, tmp_path / "grid.mp4", 6844.7, dry_run=True)
    assert called == [], "dry-run must not probe a file that was never written"


def test_truncation_message_names_the_intact_intermediates(
        probe_duration_returning, tmp_path, capsys):
    probe_duration_returning(1977.3)
    with pytest.raises(SystemExit):
        tc.verify_grid_output(None, tmp_path / "grid.mp4", 6844.7, dry_run=False)
    err = capsys.readouterr().err
    assert "32m" in err and "1h 54m" in err, err
    assert str(tmp_path) in err, "should point at the salvageable intermediates"
