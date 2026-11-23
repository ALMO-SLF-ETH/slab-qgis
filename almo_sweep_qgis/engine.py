"""Guarded access to the ``almo-sweep`` engine from inside QGIS.

The plugin depends on the ``almo_sweep`` package being importable in QGIS's Python. Rather
than let an ImportError break plugin loading, this module captures availability so the UI can
show a helpful message and stay usable until the engine is installed. When available, it
re-exports the engine entry points the plugin uses.
"""

from __future__ import annotations

from typing import Any

INSTALL_HINT = (
    "Install it into QGIS's Python, e.g.:  python-qgis.bat -m pip install -e <path>/almo-sweep"
)

try:
    from almo_sweep.contracts import build_sweep, load_manifest
    from almo_sweep.run import (
        RunIndex,
        SweepRun,
        default_index,
        default_max_workers,
        resolve_runspec,
        run_entry,
        run_one,
        run_sweep,
    )

    AVAILABLE = True
    IMPORT_ERROR: Exception | None = None
except Exception as exc:  # engine not installed in this interpreter
    AVAILABLE = False
    IMPORT_ERROR = exc

    def _unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"almo-sweep engine is not installed. {INSTALL_HINT}")

    load_manifest = build_sweep = resolve_runspec = run_one = _unavailable
    run_sweep = default_index = default_max_workers = SweepRun = _unavailable
    RunIndex = run_entry = _unavailable
