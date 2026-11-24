"""The plugin object: menu/toolbar wiring and the dockable panel lifecycle."""

from __future__ import annotations

import os

from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from slab_qgis.panels.main_panel import MainPanel

PLUGIN_NAME = "Slab-QGIS"
_ICON_PATH = os.path.join(os.path.dirname(__file__), "resources", "icon.svg")


class SlabQgisPlugin:
    """Owns the toolbar action and the dock widget, and cleans both up on unload."""

    def __init__(self, iface: QgisInterface) -> None:
        self.iface = iface
        self._action: QAction | None = None
        self._panel: MainPanel | None = None

    def initGui(self) -> None:
        """Create the dock and the toggle action. Called by QGIS when the plugin loads."""
        self._panel = MainPanel(self.iface.mainWindow())
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._panel)
        self._panel.hide()

        self._action = QAction(QIcon(_ICON_PATH), PLUGIN_NAME, self.iface.mainWindow())
        self._action.setCheckable(True)
        self._action.setToolTip("Show or hide the Slab-QGIS panel")
        self._action.toggled.connect(self._panel.setVisible)
        self._panel.visibilityChanged.connect(self._action.setChecked)

        self.iface.addToolBarIcon(self._action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self._action)

    def unload(self) -> None:
        """Remove everything this plugin added. Called by QGIS when the plugin unloads."""
        if self._action is not None:
            self.iface.removePluginMenu(PLUGIN_NAME, self._action)
            self.iface.removeToolBarIcon(self._action)
            self._action = None
        if self._panel is not None:
            self.iface.removeDockWidget(self._panel)
            self._panel.deleteLater()
            self._panel = None
