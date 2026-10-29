"""Pre-processing tab: model-independent data preparation before a study.

For now it hosts the DEM download tool (fetch and merge a DEM for a region from an online
provider). It is the home for further preparation tools (clipping, reprojection, …) as they land.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class PreprocessingPage(QWidget):
    """Model-independent data-preparation tools."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        dem = QGroupBox("Terrain")
        box = QVBoxLayout(dem)
        caption = QLabel(
            "Fetch and merge a DEM for a region polygon from an online provider (e.g. swissALTI3D)."
        )
        caption.setWordWrap(True)
        caption.setEnabled(False)
        box.addWidget(caption)
        self._dem_button = QPushButton("Download DEM for region…")
        self._dem_button.clicked.connect(self._open_dem_download)
        box.addWidget(self._dem_button)
        root.addWidget(dem)

        root.addStretch(1)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

    def _open_dem_download(self) -> None:
        from almo_sweep_qgis.panels.dem_download_dialog import DemDownloadDialog

        DemDownloadDialog(self).exec()
