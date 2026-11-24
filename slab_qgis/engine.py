"""Guarded access to the ``slab-sweep`` engine from inside QGIS.

The released plugin zip bundles a pinned engine in ``slab_qgis/_vendor/`` (see
``scripts/build_plugin.py``). A ``slab_sweep`` that is already importable -- installed into QGIS's
Python, typically an editable development checkout -- takes precedence; the bundled copy is used
only when there is none. ``SOURCE`` records which one was loaded.

Rather than let an ImportError break plugin loading, this module captures availability so the UI
can show a helpful message and stay usable without an engine. When available, it re-exports the
engine entry points the plugin uses.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

INSTALL_HINT = (
    "The released plugin bundles it; for a development checkout, install it into QGIS's "
    "Python, e.g.:  python-qgis.bat -m pip install -e <path>/slab-sweep"
)

#: Where the plugin zip carries its pinned copy of the engine.
VENDOR_DIR = Path(__file__).resolve().parent / "_vendor"

#: ``"installed"`` (an importable slab_sweep), ``"bundled"`` (``_vendor``) or ``""`` (none).
SOURCE = ""
if importlib.util.find_spec("slab_sweep") is not None:
    SOURCE = "installed"
elif (VENDOR_DIR / "slab_sweep" / "__init__.py").is_file():
    # Appended, not prepended, so it can never shadow an installed engine.
    sys.path.append(str(VENDOR_DIR))
    SOURCE = "bundled"

try:
    import slab_sweep
    from slab_sweep.contracts import build_sweep, load_manifest
    from slab_sweep.run import (
        RunIndex,
        SweepRun,
        default_index,
        default_max_workers,
        resolve_runspec,
        run_entry,
        run_one,
        run_sweep,
    )

    VERSION: str = slab_sweep.__version__
    AVAILABLE = True
    IMPORT_ERROR: Exception | None = None
except Exception as exc:  # engine not installed in this interpreter
    AVAILABLE = False
    IMPORT_ERROR = exc
    VERSION = ""
    SOURCE = ""

    def _unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"slab-sweep engine is not installed. {INSTALL_HINT}")

    load_manifest = build_sweep = resolve_runspec = run_one = _unavailable
    run_sweep = default_index = default_max_workers = SweepRun = _unavailable
    RunIndex = run_entry = _unavailable


def model_env(base: dict[str, str]) -> dict[str, str]:
    """``base`` plus what a model process needs to import the engine the plugin loaded.

    A model's own Python code may import ``slab_sweep`` (e.g. to emit progress). When the plugin
    runs on its bundled copy, that copy is not on the child's path by default, so it is added to
    ``PYTHONPATH`` (after any entries already there).
    """
    env = dict(base)
    if SOURCE == "bundled":
        parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
        if str(VENDOR_DIR) not in parts:
            env["PYTHONPATH"] = os.pathsep.join([*parts, str(VENDOR_DIR)])
    return env
