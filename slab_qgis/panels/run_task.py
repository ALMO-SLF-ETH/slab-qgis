"""Background task that runs one simulation off the main thread.

QGIS UI work must stay on the main thread, so the (blocking) model subprocess runs inside a
:class:`QgsTask`. Progress is forwarded via ``setProgress`` (safe to call from ``run``); the
terminal ``RunResult`` is stored on the task and read back in the main-thread completion
handler. ``run`` returns True when the run *executed* (even if the model reported failure) and
False only when execution itself could not proceed (e.g. a bad invocation).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from pathlib import Path
from typing import Any

from qgis.core import QgsTask

from slab_qgis import engine, settings


class SweepTask(QgsTask):
    """Runs a whole sweep via ``engine.run_sweep`` off the main thread.

    The run-specs are built on the main thread (they read layer sources) and handed in; this task
    drives them through the engine scheduler with ``max_workers`` concurrency and ``retries``,
    forwards batch progress as runs complete, writes the project run index, and collects the
    per-run results. A Stop (task cancel) is shared by every run so the whole batch halts.
    """

    def __init__(
        self,
        manifest: Any,
        sweep_runs: list[Any],
        index: Any,
        *,
        max_workers: int = 1,
        retries: int = 0,
        description: str = "Slab-QGIS study",
    ) -> None:
        super().__init__(description, QgsTask.Flag.CanCancel)
        self._manifest = manifest
        self._sweep_runs = sweep_runs
        self._index = index
        self._max_workers = max_workers
        self._retries = retries
        self.results: list[Any] = []
        self.error_message: str | None = None
        self._done = 0
        self._lock = threading.Lock()

    def run(self) -> bool:
        env = engine.model_env(dict(os.environ))
        env.update(settings.runtime_env(self._manifest))
        try:
            report = engine.run_sweep(
                self._manifest,
                self._sweep_runs,
                max_workers=self._max_workers,
                retries=self._retries,
                env=env,
                substitutions={"python": _qgis_python()},
                tool_versions=_gui_tool_versions(),
                cancel_check=self.isCanceled,
                on_finished=self._on_run_finished,
                index=self._index,
            )
        except Exception as exc:
            self.error_message = str(exc)
            return False
        self.results = list(report.results)
        return not self.isCanceled()

    def _on_run_finished(self, _i: int, _result: Any) -> None:
        total = len(self._sweep_runs) or 1
        with self._lock:
            self._done += 1
            done = self._done
        self.setProgress(min(100.0, done / total * 100.0))


class RunTask(QgsTask):
    """Runs ``engine.run_one`` for one resolved run-spec."""

    def __init__(self, manifest: Any, runspec: Any, description: str = "Slab-QGIS run") -> None:
        super().__init__(description, QgsTask.Flag.CanCancel)
        self._manifest = manifest
        self._runspec = runspec
        self.result: Any | None = None
        self.error_message: str | None = None

    def run(self) -> bool:
        # Supply the model's configured runtime settings (e.g. SLAB_PRO_BIN) as env vars, so no
        # shell environment is needed when QGIS is launched normally.
        env = engine.model_env(dict(os.environ))
        env.update(settings.runtime_env(self._manifest))
        try:
            self.result = engine.run_one(
                self._manifest,
                self._runspec,
                on_progress=self._on_progress,
                substitutions={"python": _qgis_python()},
                env=env,
                # Let a Stop button (which cancels this task) actually kill the model process.
                cancel_check=self.isCanceled,
                # Record this plugin's version/commit/dirty in each run's provenance.json.
                tool_versions=_gui_tool_versions(),
            )
        except Exception as exc:
            self.error_message = str(exc)
            return False
        # A cancelled run reports as terminated (taskTerminated), not completed, even though the
        # engine returned a result — so the UI treats it as "stopped", not "finished/failed".
        return not self.isCanceled()

    def _on_progress(self, event: Any) -> None:
        self.setProgress(float(event.progress) * 100.0)


def _gui_tool_versions() -> dict[str, dict[str, Any]]:
    """This plugin's provenance (version from ``metadata.txt`` + git commit/dirty/date).

    Returned under ``"gui"`` for the engine to write into each run's ``provenance.json``. Empty if
    the engine (which provides the git helper) is unavailable."""
    try:
        from slab_sweep.provenance import package_provenance
    except Exception:
        return {}
    import slab_qgis

    pkg_dir = Path(slab_qgis.__file__).resolve().parent
    version = _read_plugin_version(pkg_dir / "metadata.txt")
    return {"gui": package_provenance("slab_qgis", pkg_dir, version=version)}


def _read_plugin_version(metadata_path: Path) -> str | None:
    """The ``version=`` value from the plugin's ``metadata.txt``, if present."""
    with contextlib.suppress(OSError):
        for line in metadata_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version="):
                return line.split("=", 1)[1].strip()
    return None


def _qgis_python() -> str:
    """Path to QGIS's Python interpreter, for models whose command uses ``{python}``.

    In the GUI ``sys.executable`` may be ``qgis-bin.exe`` rather than the interpreter, but the
    interpreter sits in the same directory, so look for it there and fall back to
    ``sys.executable``.
    """
    bindir = os.path.dirname(sys.executable)
    for name in ("python3.exe", "python.exe", "python3", "python"):
        candidate = os.path.join(bindir, name)
        if os.path.exists(candidate):
            return candidate
    return sys.executable
