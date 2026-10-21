"""Tests for the pure batch-progress / ETA model (no Qt, injectable clock)."""

from __future__ import annotations

from datetime import datetime

from almo_sweep_qgis.progress import BatchProgress, format_duration, format_progress


class _Clock:
    """A fake monotonic clock driven by the test."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_overall_fraction_spans_weighted_steps() -> None:
    clock = _Clock()
    # Two steps: a light resample (0.2) then a heavy run (1.0); total weight 1.2.
    bp = BatchProgress([0.2, 1.0], now=clock)
    bp.start()

    # Halfway through the resample step -> 0.1 / 1.2 of the batch.
    assert abs(bp.snapshot(0, 0.5).fraction - (0.1 / 1.2)) < 1e-9
    # Resample done, run half done -> (0.2 + 0.5) / 1.2.
    assert abs(bp.snapshot(1, 0.5).fraction - (0.7 / 1.2)) < 1e-9
    # Everything done.
    assert abs(bp.snapshot(1, 1.0).fraction - 1.0) < 1e-9


def test_eta_is_extrapolated_from_elapsed_and_fraction() -> None:
    clock = _Clock()
    bp = BatchProgress([1.0], now=clock)
    bp.start()
    clock.t = 10.0  # 10 s elapsed at 25% -> total ~40 s, ~30 s left
    snap = bp.snapshot(0, 0.25)
    assert snap.elapsed_s == 10.0
    assert snap.total_s is not None and abs(snap.total_s - 40.0) < 1e-6
    assert snap.eta_s is not None and abs(snap.eta_s - 30.0) < 1e-6


def test_eta_withheld_until_enough_signal() -> None:
    clock = _Clock()
    bp = BatchProgress([1.0], now=clock)
    bp.start()
    clock.t = 0.2  # too little elapsed
    assert bp.snapshot(0, 0.5).eta_s is None


def test_format_duration() -> None:
    assert format_duration(None) == "—"
    assert format_duration(5) == "0:05"
    assert format_duration(125) == "2:05"
    assert format_duration(3700) == "1:01:40"


def test_format_progress_includes_phase_eta_and_finish() -> None:
    clock = _Clock()
    bp = BatchProgress([1.0], now=clock)
    bp.start()
    clock.t = 30.0
    snap = bp.snapshot(0, 0.5)  # 30 s at 50% -> ~30 s left
    text = format_progress(snap, "Running 'r1'", clock=lambda: datetime(2026, 1, 1, 14, 0, 0))
    assert text.startswith("Running 'r1' — ")
    assert "elapsed" in text and "left" in text
    assert "finish ~14:00" in text  # now 14:00 + ~30 s rounds to 14:00
