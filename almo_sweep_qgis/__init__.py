"""ALMO Sweep -- a QGIS 4 / Qt 6 front-end for the almo-sweep simulation engine.

QGIS calls :func:`classFactory` to instantiate the plugin. All Qt imports go through
``qgis.PyQt`` so the same code runs on QGIS 3 (Qt 5) and QGIS 4 (Qt 6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.gui import QgisInterface

    from almo_sweep_qgis.plugin import AlmoSweepPlugin


def classFactory(iface: QgisInterface) -> AlmoSweepPlugin:
    """QGIS plugin entry point."""
    from almo_sweep_qgis.plugin import AlmoSweepPlugin

    return AlmoSweepPlugin(iface)
