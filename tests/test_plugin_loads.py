"""Headless smoke tests: the plugin loads, registers, and cleans up.

Run under a QGIS Python interpreter. They assert both the real QGIS API surface we depend on
and the plugin's own load/unload behaviour against a mocked ``iface``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from almo_sweep_qgis import classFactory, settings
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


def _dampm_model_toml() -> Path:
    return Path(__file__).resolve().parents[2] / "dampm" / "interface" / "model.toml"


@pytest.mark.skipif(not _dampm_model_toml().exists(), reason="dampm model.toml not generated")
def test_registered_model_populates_combo_and_loads_on_startup(qgis_app: QgsApplication) -> None:
    model = str(_dampm_model_toml())
    for p in settings.registered_models():  # start clean
        settings.remove_model(p)
    settings.add_model(model)

    panel = MainPanel()  # reads the registry at construction
    combo = panel._model_combo
    assert combo.count() == 1
    assert combo.currentData() == model
    # the registered model is loaded into state without any manual browsing
    assert panel.state.manifest is not None
    assert panel.state.manifest.name == "dampm"
    # and it is remembered as the last model for next launch
    assert settings.last_model() == model

    for p in settings.registered_models():  # leave the registry clean for other tests
        settings.remove_model(p)
