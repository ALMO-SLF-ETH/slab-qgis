"""Batch progress + time estimate, shared by the run pipeline's UI.

A run is a sequence of weighted steps -- e.g. resample a DEM, then run the simulation; later, a
parameter sweep will be many simulation steps. :class:`BatchProgress` turns each step's own
0..1 progress into one overall fraction across the whole batch and an elapsed/ETA estimate, so
the progress bar and the "finish ~HH:MM" always describe the *batch* (or the single run when
there is only one). The maths is pure and clock-injectable, so it is unit-testable without Qt.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

#: Below this much elapsed time (or overall fraction) an ETA is too noisy to show.
_ETA_MIN_ELAPSED_S = 1.0
_ETA_MIN_FRACTION = 0.02

#: The remaining time is estimated from the pace over the *recent* part of the run, not the
#: whole-run average. A simulation reports progress as simulated time, which advances fast early
#: and slowly later (shrinking time steps, more active cells), so a whole-run average stays
#: anchored to the fast early pace and the ETA keeps climbing. The trailing window is the more
#: recent of a small fixed minimum and a fraction of the elapsed time, and the measured rate is
#: lightly smoothed so the estimate tracks a slowdown without jumping around.
_ETA_WINDOW_FRACTION = 0.5
_ETA_MIN_WINDOW_S = 3.0
_ETA_RATE_SMOOTHING = 0.4

#: While the simulation's per-frame cost is still ramping up (the flow is spreading, so each unit
#: of simulated time takes longer in wall time), *no* causal estimate can see the expensive middle
#: coming -- so a precise countdown there is false precision that only ever ticks upward. We detect
#: when the estimate has become trustworthy by watching the total-time estimate itself: while it is
#: still moving a lot between updates it has not converged, so the remaining time is shown as a
#: coarse, deliberately-long *range* with no finish clock; once successive estimates agree to within
#: ``_SETTLE_TOLERANCE`` (and we are past ``_STABLE_MIN_FRACTION``) we latch "settled" and switch to
#: the sharp countdown + finish time. ``_UNSETTLED_RATE_FACTOR`` widens the range's upper bound (a
#: lower assumed pace => a longer time), so the shown range leans long rather than short.
_STABLE_MIN_FRACTION = 0.15
_SETTLE_TOLERANCE = 0.06
_UNSETTLED_RATE_FACTOR = 0.7


@dataclass(frozen=True)
class ProgressSnapshot:
    """A point-in-time view of batch progress."""

    fraction: float  # overall completion in [0, 1]
    elapsed_s: float
    eta_s: float | None  # estimated seconds remaining, or None when too early to tell
    total_s: float | None  # estimated total duration, or None
    step_index: int  # 0-based index of the active step
    step_count: int
    settled: bool = True  # False while the pace is still ramping (show a coarse range, not a clock)
    eta_hi_s: float | None = None  # conservative upper bound for the range while unsettled


class BatchProgress:
    """Aggregates per-step progress into an overall fraction + ETA over weighted steps."""

    def __init__(self, weights: list[float], *, now: Callable[[], float] = time.monotonic) -> None:
        cleaned = [max(0.0, float(w)) for w in weights]
        self._weights = cleaned or [1.0]
        self._total = sum(self._weights) or 1.0
        self._now = now
        self._t0: float | None = None
        self._samples: list[tuple[float, float]] = []  # (elapsed_s, overall_fraction), ascending
        self._rate: float | None = None  # smoothed fraction/second over the trailing window
        self._total_prev: float | None = None  # previous total-time estimate (convergence check)
        self._settled = False  # latched once the total-time estimate has converged

    def start(self) -> None:
        self._t0 = self._now()
        self._samples = [(0.0, 0.0)]
        self._rate = None
        self._total_prev = None
        self._settled = False

    def snapshot(self, step_index: int, step_fraction: float) -> ProgressSnapshot:
        """Overall progress with the active step ``step_index`` at ``step_fraction`` (0..1)."""
        step_index = max(0, min(step_index, len(self._weights) - 1))
        step_fraction = max(0.0, min(1.0, step_fraction))
        done = sum(self._weights[:step_index]) + self._weights[step_index] * step_fraction
        fraction = max(0.0, min(1.0, done / self._total))
        elapsed = (self._now() - self._t0) if self._t0 is not None else 0.0

        if self._t0 is not None:
            self._record(elapsed, fraction)
        eta, total, settled, eta_hi = self._estimate(elapsed, fraction)
        return ProgressSnapshot(
            fraction, elapsed, eta, total, step_index, len(self._weights), settled, eta_hi
        )

    def _record(self, elapsed: float, fraction: float) -> None:
        """Append (or, at the same instant, replace) the latest (elapsed, fraction) sample."""
        if self._samples and elapsed <= self._samples[-1][0]:
            self._samples[-1] = (elapsed, fraction)
        else:
            self._samples.append((elapsed, fraction))

    def _estimate(
        self, elapsed: float, fraction: float
    ) -> tuple[float | None, float | None, bool, float | None]:
        """Estimate (eta_s, total_s, settled, eta_hi_s) from the pace over the recent window."""
        if fraction < _ETA_MIN_FRACTION or elapsed < _ETA_MIN_ELAPSED_S:
            return None, None, True, None

        window = max(_ETA_MIN_WINDOW_S, elapsed * _ETA_WINDOW_FRACTION)
        cutoff = elapsed - window
        base = self._samples[0]  # most recent sample at/before the window's start
        for sample in self._samples:
            if sample[0] <= cutoff:
                base = sample
            else:
                break

        dt = elapsed - base[0]
        df = fraction - base[1]
        if dt <= 0.0 or df <= 0.0:
            # No measurable recent progress; reuse the last good rate rather than guess.
            if self._rate is None:
                return None, None, True, None
            rate = self._rate
        else:
            recent = df / dt
            rate = (
                recent
                if self._rate is None
                else _ETA_RATE_SMOOTHING * recent + (1.0 - _ETA_RATE_SMOOTHING) * self._rate
            )
            self._rate = rate

        if rate <= 0.0:
            return None, None, True, None
        remaining = 1.0 - fraction
        eta = max(0.0, remaining / rate)
        total = elapsed + eta

        # "Settled" = the total-time estimate has converged (successive updates agree). Latch it so
        # the countdown does not flip back to a coarse range once it has become trustworthy.
        if not self._settled:
            converged = (
                self._total_prev is not None
                and fraction >= _STABLE_MIN_FRACTION
                and abs(total - self._total_prev) <= _SETTLE_TOLERANCE * total
            )
            self._settled = bool(converged)
            self._total_prev = total
        # While unsettled, lean the range long: assume the pace could still drop (frames keep
        # getting more expensive), which lengthens the remaining time.
        eta_hi = None if self._settled else max(eta, remaining / (rate * _UNSETTLED_RATE_FACTOR))
        return eta, total, self._settled, eta_hi


def format_duration(seconds: float | None) -> str:
    """``125`` -> ``2:05``; ``3700`` -> ``1:01:40``; ``None`` -> ``—``."""
    if seconds is None:
        return "—"
    total = round(max(0.0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_coarse_range(low_s: float, high_s: float) -> str:
    """A deliberately-rough remaining-time range, e.g. ``2 to 3 min`` or ``under a minute``.

    Used while the pace is still ramping, so the estimate reads as approximate (a range, no
    seconds, no finish clock) rather than as a precise countdown that would only tick upward.
    """
    if high_s < 60.0:
        return "under a minute"
    step = 1.0 if high_s <= 600.0 else (5.0 if high_s <= 1800.0 else 10.0)
    low_m = max(step, round(low_s / 60.0 / step) * step)
    high_m = max(low_m + step, math.ceil(high_s / 60.0 / step) * step)
    en_dash = chr(0x2013)
    return f"{low_m:g}{en_dash}{high_m:g} min"


def format_progress(
    snapshot: ProgressSnapshot,
    phase: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """One-line status: the phase, elapsed, ETA, finish time and (for a batch) step count.

    ``clock`` supplies "now" for the finish-time estimate (``datetime.now`` in the app; a fixed
    value in tests); omit it to leave the finish time out. While the pace is still ramping
    (``snapshot.settled`` is False) the remaining time is shown as a coarse, long-leaning range
    with no finish clock, because a precise countdown there would be false precision.
    """
    detail = f"{format_duration(snapshot.elapsed_s)} elapsed"
    if snapshot.eta_s is not None:
        if not snapshot.settled:
            high = snapshot.eta_hi_s if snapshot.eta_hi_s is not None else snapshot.eta_s
            detail += f" · roughly {format_coarse_range(snapshot.eta_s, high)} left"
        else:
            detail += f" · ~{format_duration(snapshot.eta_s)} left"
            if clock is not None:
                finish = clock() + timedelta(seconds=snapshot.eta_s)
                detail += f" · finish ~{finish:%H:%M}"
    if snapshot.step_count > 1:
        detail += f" · step {snapshot.step_index + 1}/{snapshot.step_count}"
    return f"{phase} — {detail}" if phase else detail
