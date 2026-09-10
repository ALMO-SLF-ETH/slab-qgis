"""Shared fixtures for the ALMO Sweep plugin tests.

These tests must run under a QGIS Python interpreter (they import ``qgis``). See the README
for how to invoke pytest through QGIS's interpreter.

If the sibling ``almo-sweep`` engine is not installed into this interpreter, its ``src`` is
put on ``sys.path`` so the tests can exercise manifest-driven UI without a prior install.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from qgis.core import QgsApplication
from qgis.testing import start_app

_SIM_ROOT = Path(__file__).resolve().parents[2]  # .../ALMO/Simulations
_ENGINE_SRC = _SIM_ROOT / "almo-sweep" / "src"
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
    yield app


@pytest.fixture
def dampm_manifest_path() -> Path:
    """Path to the engine's reference dampm manifest fixture."""
    return _SIM_ROOT / "almo-sweep" / "tests" / "fixtures" / "dampm.model.toml"
