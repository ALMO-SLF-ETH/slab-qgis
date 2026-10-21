"""Sequencing of a run's background steps (resample(s) then simulation(s)) with batch progress.

A run may need to resample one or more rasters before the simulation(s) execute. Each step runs
off the main thread as a :class:`QgsTask`; :class:`RunController` runs them in order, maps each
step's progress into one overall :class:`~almo_sweep_qgis.progress.BatchProgress`, and runs each
step's main-thread finalisation between tasks. The design is batch-first: today the sequence is
"resample… then one run", but a parameter sweep is simply more run steps -- the progress bar and
ETA already describe the whole batch.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from qgis.core import QgsApplication, QgsTask
from qgis.PyQt.QtCore import QObject, pyqtSignal

from almo_sweep_qgis.dem.resample import resample_raster
from almo_sweep_qgis.progress import BatchProgress, format_progress


@runtime_checkable
class Job(Protocol):
    """One step in a run: a label, a relative weight, a background task and a main-thread finish."""

    label: str
    weight: float

    def make_task(self) -> QgsTask:
        """Build (on the main thread) the task that does this step's background work."""

    def finalize(self) -> None:
        """Main-thread work after the task succeeds; raise to fail the whole run."""


class ResampleTask(QgsTask):
    """Background raster resample (GDAL warp), so the UI stays responsive on large DEMs."""

    def __init__(self, src: str, dst: str, resolution: float, alg: str) -> None:
        super().__init__("Resample raster", QgsTask.Flag.CanCancel)
        self._src = src
        self._dst = dst
        self._res = resolution
        self._alg = alg
        self.out_path: str | None = None
        self.error: str | None = None

    def run(self) -> bool:
        try:
            self.out_path = resample_raster(
                self._src,
                self._dst,
                self._res,
                self._alg,
                progress=lambda frac, _msg: self.setProgress(frac * 100.0),
                should_cancel=self.isCanceled,
            )
        except Exception as exc:  # ResampleError or any unexpected GDAL error
            self.error = str(exc)
            return False
        return not self.isCanceled()


class RunController(QObject):
    """Runs a list of :class:`Job` steps in order, reporting one batch progress + ETA.

    Signals: ``progressChanged(fraction, info_text)`` for the bar/label; ``finished(results)`` with
    the collected per-job results; ``failed(message)`` if a step could not execute; ``stopped()`` if
    the active step was cancelled.
    """

    progressChanged = pyqtSignal(float, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(
        self,
        jobs: list[Job],
        *,
        clock: Any = datetime.now,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._jobs = list(jobs)
        self._progress = BatchProgress([getattr(j, "weight", 1.0) for j in self._jobs])
        self._clock = clock
        self._index = -1
        self._task: QgsTask | None = None
        self._results: list[Any] = []

    def start(self) -> None:
        if not self._jobs:
            self.finished.emit([])
            return
        self._progress.start()
        self._advance()

    def cancel(self) -> None:
        if self._task is not None:
            with suppress(Exception):
                self._task.cancel()

    # -- sequencing --------------------------------------------------------------------
    def _advance(self) -> None:
        self._index += 1
        if self._index >= len(self._jobs):
            self.finished.emit(self._results)
            return
        job = self._jobs[self._index]
        try:
            task = job.make_task()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self._task = task
        task.progressChanged.connect(self._on_task_progress)
        task.taskCompleted.connect(self._on_task_completed)
        task.taskTerminated.connect(self._on_task_terminated)
        self._emit(0.0)
        QgsApplication.taskManager().addTask(task)

    def _on_task_progress(self, pct: float) -> None:
        self._emit(pct / 100.0)

    def _emit(self, step_fraction: float) -> None:
        snapshot = self._progress.snapshot(self._index, step_fraction)
        label = getattr(self._jobs[self._index], "label", "")
        info = format_progress(snapshot, label, clock=self._clock)
        self.progressChanged.emit(snapshot.fraction, info)

    def _on_task_completed(self) -> None:
        job = self._jobs[self._index]
        try:
            job.finalize()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        result = getattr(job, "result", None)
        if result is not None:
            self._results.append(result)
        self._emit(1.0)
        self._advance()

    def _on_task_terminated(self) -> None:
        task = self._task
        if task is not None and task.isCanceled():
            self.stopped.emit()
            return
        message = (
            getattr(task, "error_message", None)
            or getattr(task, "error", None)
            or "the step was terminated"
        )
        self.failed.emit(message)
