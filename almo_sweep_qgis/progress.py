"""Batch progress + time estimate, shared by the run pipeline's UI.

A run is a sequence of weighted steps -- e.g. resample a DEM, then run the simulation; later, a
parameter sweep will be many simulation steps. :class:`BatchProgress` turns each step's own
0..1 progress into one overall fraction across the whole batch and an elapsed/ETA estimate, so
the progress bar and the "finish ~HH:MM" always describe the *batch* (or the single run when
there is only one). The maths is pure and clock-injectable, so it is unit-testable without Qt.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

#: Below this much elapsed time (or overall fraction) an ETA is too noisy to show.
_ETA_MIN_ELAPSED_S = 1.0
_ETA_MIN_FRACTION = 0.02


@dataclass(frozen=True)
class ProgressSnapshot:
    """A point-in-time view of batch progress."""

    fraction: float  # overall completion in [0, 1]
    elapsed_s: float
    eta_s: float | None  # estimated seconds remaining, or None when too early to tell
    total_s: float | None  # estimated total duration, or None
    step_index: int  # 0-based index of the active step
    step_count: int


class BatchProgress:
    """Aggregates per-step progress into an overall fraction + ETA over weighted steps."""

    def __init__(self, weights: list[float], *, now: Callable[[], float] = time.monotonic) -> None:
        cleaned = [max(0.0, float(w)) for w in weights]
        self._weights = cleaned or [1.0]
        self._total = sum(self._weights) or 1.0
        self._now = now
        self._t0: float | None = None

    def start(self) -> None:
        self._t0 = self._now()

    def snapshot(self, step_index: int, step_fraction: float) -> ProgressSnapshot:
        """Overall progress with the active step ``step_index`` at ``step_fraction`` (0..1)."""
        step_index = max(0, min(step_index, len(self._weights) - 1))
        step_fraction = max(0.0, min(1.0, step_fraction))
        done = sum(self._weights[:step_index]) + self._weights[step_index] * step_fraction
        fraction = max(0.0, min(1.0, done / self._total))
        elapsed = (self._now() - self._t0) if self._t0 is not None else 0.0

        eta: float | None = None
        total: float | None = None
        if fraction >= _ETA_MIN_FRACTION and elapsed >= _ETA_MIN_ELAPSED_S:
            total = elapsed / fraction
            eta = max(0.0, total - elapsed)
        return ProgressSnapshot(fraction, elapsed, eta, total, step_index, len(self._weights))


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


def format_progress(
    snapshot: ProgressSnapshot,
    phase: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """One-line status: the phase, elapsed, ETA, finish time and (for a batch) step count.

    ``clock`` supplies "now" for the finish-time estimate (``datetime.now`` in the app; a fixed
    value in tests); omit it to leave the finish time out.
    """
    detail = f"{format_duration(snapshot.elapsed_s)} elapsed"
    if snapshot.eta_s is not None:
        detail += f" · ~{format_duration(snapshot.eta_s)} left"
        if clock is not None:
            finish = clock() + timedelta(seconds=snapshot.eta_s)
            detail += f" · finish ~{finish:%H:%M}"
    if snapshot.step_count > 1:
        detail += f" · step {snapshot.step_index + 1}/{snapshot.step_count}"
    return f"{phase} — {detail}" if phase else detail
