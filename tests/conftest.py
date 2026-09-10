"""Shared fixtures for the ALMO Sweep plugin tests.

These tests must run under a QGIS Python interpreter (they import ``qgis``). See the README
for how to invoke pytest through QGIS's interpreter.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from qgis.core import QgsApplication
from qgis.testing import start_app


@pytest.fixture(scope="session")
def qgis_app() -> Iterator[QgsApplication]:
    """Start a headless QGIS application once for the whole test session."""
    app = start_app()
    yield app
