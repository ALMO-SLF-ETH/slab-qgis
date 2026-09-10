"""Background task that runs one simulation off the main thread.

QGIS UI work must stay on the main thread, so the (blocking) model subprocess runs inside a
:class:`QgsTask`. Progress is forwarded via ``setProgress`` (safe to call from ``run``); the
terminal ``RunResult`` is stored on the task and read back in the main-thread completion
handler. ``run`` returns True when the run *executed* (even if the model reported failure) and
False only when execution itself could not proceed (e.g. a bad invocation).
"""

from __future__ import annotations

from typing import Any

from qgis.core import QgsTask

from almo_sweep_qgis import engine


class RunTask(QgsTask):
    """Runs ``engine.run_one`` for one resolved run-spec."""

    def __init__(self, manifest: Any, runspec: Any, description: str = "ALMO Sweep run") -> None:
        super().__init__(description, QgsTask.Flag.CanCancel)
        self._manifest = manifest
        self._runspec = runspec
        self.result: Any | None = None
        self.error_message: str | None = None

    def run(self) -> bool:
        try:
            self.result = engine.run_one(
                self._manifest, self._runspec, on_progress=self._on_progress
            )
        except Exception as exc:
            self.error_message = str(exc)
            return False
        return True

    def _on_progress(self, event: Any) -> None:
        self.setProgress(float(event.progress) * 100.0)
