"""Headless smoke tests: the plugin loads, registers, and cleans up.

Run under a QGIS Python interpreter. They assert both the real QGIS API surface we depend on
and the plugin's own load/unload behaviour against a mocked ``iface``.
"""

from __future__ import annotations

from almo_sweep_qgis import classFactory
from almo_sweep_qgis.panels.main_panel import PAGE_TITLES, MainPanel
from almo_sweep_qgis.plugin import AlmoSweepPlugin
from qgis.core import QgsApplication
from qgis.gui import QgisInterface
from qgis.PyQt.QtWidgets import QStackedWidget
from qgis.testing.mocked import get_iface

# Every QgisInterface method the plugin calls. Verified against the *real* class, because the
# mocked iface would accept anything and hide a typo or an API change.
REQUIRED_IFACE_METHODS = (
    "addDockWidget",
    "removeDockWidget",
    "addToolBarIcon",
    "removeToolBarIcon",
    "addPluginToMenu",
    "removePluginMenu",
    "mainWindow",
)


def test_real_qgisinterface_exposes_methods_we_call() -> None:
    missing = [name for name in REQUIRED_IFACE_METHODS if not hasattr(QgisInterface, name)]
    assert not missing, f"QgisInterface is missing: {missing}"


def test_classfactory_returns_plugin(qgis_app: QgsApplication) -> None:
    assert isinstance(classFactory(get_iface()), AlmoSweepPlugin)


def test_initgui_registers_and_unload_cleans_up(qgis_app: QgsApplication) -> None:
    iface = get_iface()
    plugin = classFactory(iface)

    plugin.initGui()
    iface.addDockWidget.assert_called_once()
    iface.addToolBarIcon.assert_called_once()
    iface.addPluginToMenu.assert_called_once()

    plugin.unload()
    iface.removeDockWidget.assert_called_once()
    iface.removeToolBarIcon.assert_called_once()
    iface.removePluginMenu.assert_called_once()


def test_main_panel_builds_all_pages(qgis_app: QgsApplication) -> None:
    panel = MainPanel()
    stack = panel.findChild(QStackedWidget)
    assert stack is not None
    assert stack.count() == len(PAGE_TITLES)
