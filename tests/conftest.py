"""Shared fixtures for the Slab-QGIS plugin tests.

These tests must run under a QGIS Python interpreter (they import ``qgis``). See
docs/development.md for how to invoke pytest through QGIS's interpreter.

If the sibling ``slab-sweep`` engine is not installed into this interpreter, its ``src`` is
put on ``sys.path`` so the tests can exercise manifest-driven UI without a prior install.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from qgis.core import QgsApplication
from qgis.testing import start_app

_SIM_ROOT = Path(__file__).resolve().parents[2]  # .../ALMO/Simulations
_ENGINE_SRC = _SIM_ROOT / "slab-sweep" / "src"
if _ENGINE_SRC.is_dir():
    # In-process import (this interpreter) ...
    if str(_ENGINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ENGINE_SRC))
    # ... and for model subprocesses the executor spawns, which must import the engine too.
    _pp = os.environ.get("PYTHONPATH", "")
    if str(_ENGINE_SRC) not in _pp.split(os.pathsep):
        os.environ["PYTHONPATH"] = (
            os.pathsep.join([str(_ENGINE_SRC), _pp]) if _pp else str(_ENGINE_SRC)
        )


@pytest.fixture(scope="session")
def qgis_app() -> Iterator[QgsApplication]:
    """Start a headless QGIS application once for the whole test session."""
    app = start_app()
    # The plugin checks GitHub for model updates on load; tests must not touch the network.
    from slab_qgis.catalog import service

    service.set_updates_enabled(False)
    yield app


@pytest.fixture(autouse=True)
def _clean_project() -> Iterator[None]:
    """Reset the shared ``QgsProject`` between tests so map layers don't leak across them.

    QGIS keeps one global project for the process; without this, layers (and layer-tree groups) a
    test adds would be visible to later tests that scan ``mapLayers()`` (so they'd be order-bound).
    """
    from qgis.core import QgsProject

    yield
    with contextlib.suppress(Exception):
        QgsProject.instance().clear()


@pytest.fixture
def slab_pro_manifest_path() -> Path:
    """Path to the engine's reference Slab-Pro manifest fixture."""
    return _SIM_ROOT / "slab-sweep" / "tests" / "fixtures" / "slab_pro.model.toml"
