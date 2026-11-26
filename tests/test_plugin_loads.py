"""Headless smoke tests: the plugin loads, registers, and cleans up.

Run under a QGIS Python interpreter. They assert both the real QGIS API surface we depend on
and the plugin's own load/unload behaviour against a mocked ``iface``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from qgis.core import QgsApplication
from qgis.gui import QgisInterface
from qgis.PyQt.QtWidgets import QTabWidget
from qgis.testing.mocked import get_iface
from slab_qgis import classFactory, settings
from slab_qgis.panels.main_panel import PAGE_TITLES, MainPanel
from slab_qgis.plugin import SlabQgisPlugin

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
    assert isinstance(classFactory(get_iface()), SlabQgisPlugin)


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
    tabs = panel.findChild(QTabWidget)
    assert tabs is not None
    assert tabs.count() == len(PAGE_TITLES)
    assert [tabs.tabText(i) for i in range(tabs.count())] == list(PAGE_TITLES)


def _slab_pro_model_toml() -> Path:
    # Slab-Pro's generated manifest in a sibling checkout (Slab-Pro-dev or Slab-Pro), if any.
    root = Path(__file__).resolve().parents[2]  # tests/ -> slab-qgis -> Simulations
    for repo in ("Slab-Pro-dev", "Slab-Pro"):
        path = root / repo / "interface" / "model.toml"
        if path.exists():
            return path
    return root / "Slab-Pro" / "interface" / "model.toml"


@pytest.mark.skipif(
    not _slab_pro_model_toml().exists(), reason="no Slab-Pro checkout with interface/model.toml"
)
def test_registered_model_populates_combo_and_loads_on_startup(qgis_app: QgsApplication) -> None:
    model = str(_slab_pro_model_toml())
    for p in settings.registered_models():  # start clean
        settings.remove_model(p)
    settings.add_model(model)

    panel = MainPanel()  # reads the registry at construction
    combo = panel._model_combo
    assert combo.count() == 1
    assert combo.currentData() == model
    # the registered model is loaded into state without any manual browsing
    assert panel.state.manifest is not None
    assert panel.state.manifest.name == "slab-pro"
    # and it is remembered as the last model for next launch
    assert settings.last_model() == model

    for p in settings.registered_models():  # leave the registry clean for other tests
        settings.remove_model(p)
