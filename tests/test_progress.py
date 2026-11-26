"""Tests for the pure batch-progress / ETA model (no Qt, injectable clock)."""

from __future__ import annotations

from datetime import datetime

from slab_qgis.progress import (
    BatchProgress,
    ProgressSnapshot,
    format_coarse_range,
    format_duration,
    format_progress,
)


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


def test_eta_tracks_a_slowing_run_instead_of_the_fast_early_pace() -> None:
    # A simulation reports progress as simulated time, which races ahead early then crawls. A
    # whole-run average (elapsed/fraction) stays anchored to that fast early pace and keeps
    # under-estimating; the recent-pace estimate must instead grow to reflect the slowdown.
    clock = _Clock()
    bp = BatchProgress([1.0], now=clock)
    bp.start()

    # Fast first 20 s to 60%, then a long crawl adding ~2.5%/10 s.
    schedule = [(10, 0.40), (20, 0.60)]
    schedule += [(t, 0.60 + 0.025 * ((t - 20) / 10)) for t in range(30, 101, 10)]
    snap = None
    for t, f in schedule:
        clock.t = float(t)
        snap = bp.snapshot(0, f)

    assert snap is not None and snap.eta_s is not None
    # The naive whole-run average at the final point would be (1-f)*elapsed/f.
    f_final = schedule[-1][1]
    naive_eta = (1.0 - f_final) * 100.0 / f_final
    assert snap.eta_s > naive_eta * 1.5  # tracks the crawl, not the early sprint


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


def test_format_coarse_range() -> None:
    dash = chr(0x2013)  # en dash, as used between the range endpoints
    assert format_coarse_range(20, 50) == "under a minute"
    assert format_coarse_range(100, 140) == f"2{dash}3 min"  # 1.67 min -> 2, 2.33 min -> 3
    assert format_coarse_range(60, 60) == f"1{dash}2 min"  # low == high still spans a step


def test_while_ramping_shows_a_coarse_range_then_a_clock_once_settled() -> None:
    # A simulation whose per-frame cost ramps up: progress (simulated time) advances fast early
    # and slower as it goes. While that ramp is underway the estimate must read as a rough range
    # with no finish clock; once the pace settles it becomes a sharp countdown + finish time.
    clock = _Clock()
    bp = BatchProgress([1.0], now=clock)
    bp.start()

    # Decelerating pace (fraction/second dropping) -> still ramping.
    ramp = None
    for t, f in [(10, 0.20), (30, 0.30), (60, 0.37)]:
        clock.t = float(t)
        ramp = bp.snapshot(0, f)
    assert ramp is not None and not ramp.settled
    text = format_progress(ramp, "Running 'r1'", clock=lambda: datetime(2026, 1, 1, 14, 0, 0))
    assert "roughly" in text and "min left" in text
    assert "finish" not in text  # no false-precise clock while the pace is still climbing

    # Now a steady pace: the total-time estimate converges, so the estimate latches "settled".
    settled = None
    f = 0.37
    for t in range(80, 220, 20):
        f = round(f + 0.03, 4)  # constant pace -> total-time estimate stops moving
        clock.t = float(t)
        settled = bp.snapshot(0, f)
    assert settled is not None and settled.settled
    text = format_progress(settled, "Running 'r1'", clock=lambda: datetime(2026, 1, 1, 14, 0, 0))
    assert "finish ~" in text and "roughly" not in text


def test_format_progress_includes_phase_eta_and_finish() -> None:
    # A settled snapshot (30 s elapsed, 50%, ~30 s left) formats as a sharp countdown + finish.
    snap = ProgressSnapshot(
        fraction=0.5, elapsed_s=30.0, eta_s=30.0, total_s=60.0, step_index=0, step_count=1
    )
    text = format_progress(snap, "Running 'r1'", clock=lambda: datetime(2026, 1, 1, 14, 0, 0))
    assert text.startswith("Running 'r1' — ")
    assert "elapsed" in text and "left" in text
    assert "finish ~14:00" in text  # now 14:00 + ~30 s rounds to 14:00
