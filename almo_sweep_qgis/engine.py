"""Guarded access to the ``almo-sweep`` engine from inside QGIS.

The plugin depends on the ``almo_sweep`` package being importable in QGIS's Python. Rather
than let an ImportError break plugin loading, this module captures availability so the UI can
show a helpful message and stay usable until the engine is installed.
"""

from __future__ import annotations

from typing import Any

INSTALL_HINT = (
    "Install it into QGIS's Python, e.g.:  python-qgis.bat -m pip install -e <path>/almo-sweep"
)

try:
    from almo_sweep.contracts import load_manifest as _load_manifest

    AVAILABLE = True
    IMPORT_ERROR: Exception | None = None
except Exception as exc:  # engine not installed in this interpreter
    _load_manifest = None
    AVAILABLE = False
    IMPORT_ERROR = exc


def load_manifest(path: str) -> Any:
    """Load and validate a manifest, or raise if the engine is unavailable.

    Returns an ``almo_sweep.contracts.ModelManifest``. Raises ``ManifestError`` (from the
    engine) on an invalid manifest, or ``RuntimeError`` if the engine is not installed.
    """
    if not AVAILABLE or _load_manifest is None:
        raise RuntimeError(f"almo-sweep engine is not installed. {INSTALL_HINT}")
    return _load_manifest(path)
