"""The plugin object: menu/toolbar wiring and the dockable panel lifecycle."""

from __future__ import annotations

import os
from typing import Any

from qgis.core import Qgis
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QPushButton

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
        self._check_for_updates()

    def _check_for_updates(self) -> None:
        """At most once a day, look for newer releases of installed models (in the background)."""
        from slab_qgis.catalog import net, service
        from slab_qgis.panels.models_dialog import task_runner

        if not service.check_due():
            return

        def done(updates: Any, error: Exception | None) -> None:
            if error is None and updates and self._panel is not None:
                self._notify(updates)

        task_runner(
            "Checking for Slab-QGIS model updates", lambda: service.check_updates(net.fetch), done
        )

    def _notify(self, updates: list[Any]) -> None:
        text = ", ".join(f"{u.name} {u.current} → {u.latest}" for u in updates)
        bar = self.iface.messageBar()
        item = bar.createMessage(PLUGIN_NAME, f"Updates available: {text}")
        if any(u.kind == "model" for u in updates) and self._panel is not None:
            button = QPushButton("Open models…")
            button.clicked.connect(self._panel.open_models_dialog)
            item.layout().addWidget(button)
        bar.pushWidget(item, Qgis.MessageLevel.Info, 0)

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
