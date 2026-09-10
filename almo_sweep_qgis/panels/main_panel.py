"""The main dockable panel: a left-hand navigator over the workflow's five sections.

The sections mirror the study workflow end to end:

1. **Inputs**     -- pick the DEM raster and the release-zone layer(s).
2. **Parameters** -- the model's parameter form, built from its manifest.
3. **Study**      -- deterministic run or a probabilistic sweep (distributions, samples).
4. **Run**        -- name, output folder, output selection, launch and monitor.
5. **Results**    -- browse and inspect the runs produced.

This is the Phase-0 shell: the navigation and page frames exist; each page is filled in
its own step. Keeping the pages as separate widgets from the start means each can be built
and tested in isolation.
"""

from __future__ import annotations

from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

#: Ordered section titles. The order is the intended left-to-right workflow.
PAGE_TITLES = ("Inputs", "Parameters", "Study", "Run", "Results")


class MainPanel(QgsDockWidget):
    """A dock widget hosting a navigator (left) and a stack of section pages (right)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("ALMO Sweep", parent)
        # A stable object name lets QGIS save and restore the dock's geometry/state.
        self.setObjectName("AlmoSweepPanel")

        container = QWidget(self)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        self._nav = QListWidget(container)
        self._nav.setMaximumWidth(140)
        self._nav.addItems(PAGE_TITLES)

        self._stack = QStackedWidget(container)
        for title in PAGE_TITLES:
            self._stack.addWidget(_placeholder_page(title))

        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._nav.setCurrentRow(0)

        layout.addWidget(self._nav, 0)
        layout.addWidget(self._stack, 1)
        self.setWidget(container)


def _placeholder_page(title: str) -> QWidget:
    page = QWidget()
    box = QVBoxLayout(page)
    heading = QLabel(f"<b>{title}</b>")
    note = QLabel("Coming soon.")
    note.setEnabled(False)
    box.addWidget(heading)
    box.addWidget(note)
    box.addStretch(1)
    box.setAlignment(Qt.AlignmentFlag.AlignTop)
    return page
