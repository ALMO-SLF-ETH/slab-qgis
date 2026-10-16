"""Dialog to download and merge a DEM for a region polygon, from an online provider.

The user picks a provider (e.g. swissALTI3D), the source tile resolution, a polygon layer that
outlines the region of interest, and an output file; optionally the output can be resampled to a
different resolution with a chosen method. The covering tiles are mosaicked and clipped into one
GeoTIFF (in a background task), which is then added to the project (rendered as a hillshade) ready
to use as the DEM input. New providers appear here automatically once registered in
:mod:`almo_sweep_qgis.dem.providers`.
"""

from __future__ import annotations

from typing import Any

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsHillshadeRenderer,
    QgsMapLayerProxyModel,
    QgsProject,
    QgsRasterLayer,
    QgsTask,
    QgsVectorLayer,
)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from almo_sweep_qgis.dem import fetch, providers

#: Resampling methods offered when resampling to a custom resolution: (label, GDAL algorithm).
#: Bilinear (a 2x2-pixel kernel) is the default; "average" is usually better for downsampling.
_RESAMPLE_METHODS = (
    ("Bilinear (2x2 kernel)", "bilinear"),
    ("Nearest neighbour", "near"),
    ("Cubic (4x4 kernel)", "cubic"),
    ("Cubic spline", "cubicspline"),
    ("Lanczos", "lanczos"),
    ("Average (mean of covered cells; for downsampling)", "average"),
)


class DemDownloadDialog(QDialog):
    """Pick a provider + region, then fetch, merge (and optionally resample) a DEM GeoTIFF."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Download DEM for a region")
        self.setMinimumWidth(480)
        self._task: QgsTask | None = None
        self._out_path: str = ""

        form = QFormLayout()

        self._provider_combo = QComboBox()
        for prov in providers.list_providers():
            self._provider_combo.addItem(prov.label, prov.id)
        self._provider_combo.currentIndexChanged.connect(self._refresh_resolutions)
        form.addRow("Provider:", self._provider_combo)

        self._resolution_combo = QComboBox()
        self._resolution_combo.setToolTip("Which native tiles to fetch")
        self._resolution_combo.currentIndexChanged.connect(self._sync_resample_default)
        form.addRow("Source (tile) resolution:", self._resolution_combo)

        self._region_combo = QgsMapLayerComboBox()
        self._region_combo.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        self._region_combo.setToolTip("A polygon layer outlining the region of interest")
        form.addRow("Region (polygon):", self._region_combo)

        # Optional resampling to a different output resolution.
        self._resample_check = QCheckBox("Resample output to")
        self._resample_check.toggled.connect(self._on_resample_toggled)
        self._resample_res = QDoubleSpinBox()
        self._resample_res.setDecimals(2)
        self._resample_res.setRange(0.01, 10000.0)
        self._resample_res.setSingleStep(0.5)
        self._resample_res.setSuffix(" m")
        self._resample_res.setEnabled(False)
        self._resample_method = QComboBox()
        for label, alg in _RESAMPLE_METHODS:
            self._resample_method.addItem(label, alg)
        self._resample_method.setEnabled(False)
        resample_row = QHBoxLayout()
        resample_row.addWidget(self._resample_check, 0)
        resample_row.addWidget(self._resample_res, 0)
        resample_row.addWidget(self._resample_method, 1)
        form.addRow("Resampling:", resample_row)

        self._clip_check = QCheckBox("Clip output to the polygon (otherwise its bounding box)")
        self._clip_check.setChecked(True)
        form.addRow("", self._clip_check)

        self._out_widget = QgsFileWidget()
        self._out_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self._out_widget.setFilter("GeoTIFF (*.tif)")
        self._out_widget.setDialogTitle("Save merged DEM as…")
        form.addRow("Output file:", self._out_widget)

        root = QVBoxLayout(self)
        root.addLayout(form)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        root.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setEnabled(False)
        root.addWidget(self._status)

        buttons = QDialogButtonBox()
        self._download_button = QPushButton("Download")
        self._download_button.clicked.connect(self._on_download)
        buttons.addButton(self._download_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_resolutions()

    # -- helpers -----------------------------------------------------------------------
    def _refresh_resolutions(self) -> None:
        self._resolution_combo.clear()
        prov = providers.get_provider(self._provider_combo.currentData())
        for res in prov.resolutions:
            self._resolution_combo.addItem(f"{res:g} m", res)  # label shown, float kept as data
        self._sync_resample_default()

    def _sync_resample_default(self) -> None:
        # Keep the resample box seeded with the source resolution until the user takes it over.
        if (
            not self._resample_check.isChecked()
            and self._resolution_combo.currentData() is not None
        ):
            self._resample_res.setValue(float(self._resolution_combo.currentData()))

    def _on_resample_toggled(self, on: bool) -> None:
        self._resample_res.setEnabled(on)
        self._resample_method.setEnabled(on)

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    def _region_geometry(self) -> tuple[str, int] | None:
        """Dissolved WKT of the selected polygon layer plus its EPSG code, or None with a status."""
        layer = self._region_combo.currentLayer()
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            self._set_status("Select a valid polygon layer for the region.")
            return None
        geoms = [f.geometry() for f in layer.getFeatures() if not f.geometry().isEmpty()]
        if not geoms:
            self._set_status("The selected region layer has no features.")
            return None
        dissolved = QgsGeometry.unaryUnion(geoms) if len(geoms) > 1 else geoms[0]
        if dissolved.isEmpty():
            self._set_status("The region geometry is empty.")
            return None
        epsg = _epsg_of(layer.crs())
        if epsg is None:
            self._set_status(
                "The region layer's CRS has no EPSG code. Re-project it to a standard CRS "
                "(e.g. EPSG:2056) and try again."
            )
            return None
        return dissolved.asWkt(), epsg

    # -- actions -----------------------------------------------------------------------
    def _on_download(self) -> None:
        if self._task is not None:
            return  # a download is already running
        region = self._region_geometry()
        if region is None:
            return
        out_path = self._out_widget.filePath().strip()
        if not out_path:
            self._set_status("Choose an output file.")
            return
        if not out_path.lower().endswith(".tif"):
            out_path += ".tif"

        wkt, epsg = region
        provider = providers.get_provider(self._provider_combo.currentData())
        source_res = float(self._resolution_combo.currentData())
        output_res = float(self._resample_res.value()) if self._resample_check.isChecked() else None
        resample_alg = str(self._resample_method.currentData())

        self._progress.setValue(0)
        self._download_button.setEnabled(False)
        detail = f" → resample to {output_res:g} m ({resample_alg})" if output_res else ""
        self._set_status(f"Fetching {provider.label} at {source_res:g} m{detail}…")

        task = _DemDownloadTask(
            provider,
            wkt,
            epsg,
            source_res,
            out_path,
            self._clip_check.isChecked(),
            output_res,
            resample_alg,
        )
        task.progressChanged.connect(lambda pct: self._progress.setValue(int(pct)))
        task.taskCompleted.connect(self._on_completed)
        task.taskTerminated.connect(self._on_terminated)
        self._task = task
        self._out_path = out_path
        QgsApplication.taskManager().addTask(task)

    def _on_completed(self) -> None:
        self._download_button.setEnabled(True)
        self._progress.setValue(100)
        self._task = None
        layer = QgsRasterLayer(self._out_path, _layer_name(self._out_path))
        if layer.isValid():
            _apply_hillshade(layer)
            QgsProject.instance().addMapLayer(layer)
            self._set_status(
                f"Done — merged DEM saved to {self._out_path}, added to the map as a hillshade."
            )
        else:
            self._set_status(f"Saved to {self._out_path}, but it could not be loaded as a layer.")

    def _on_terminated(self) -> None:
        self._download_button.setEnabled(True)
        message = self._task.error_message if isinstance(self._task, _DemDownloadTask) else None
        self._task = None
        self._set_status(f"Download failed: {message or 'cancelled or interrupted'}")


class _DemDownloadTask(QgsTask):
    """Runs :func:`fetch.fetch_dem` off the main thread (network + GDAL are blocking)."""

    def __init__(
        self,
        provider: Any,
        polygon_wkt: str,
        polygon_crs_epsg: int,
        source_resolution: float,
        out_path: str,
        clip: bool,
        output_resolution: float | None,
        resample_alg: str,
    ) -> None:
        super().__init__("Download DEM", QgsTask.Flag.CanCancel)
        self._provider = provider
        self._wkt = polygon_wkt
        self._epsg = polygon_crs_epsg
        self._source_resolution = source_resolution
        self._out_path = out_path
        self._clip = clip
        self._output_resolution = output_resolution
        self._resample_alg = resample_alg
        self.error_message: str | None = None

    def run(self) -> bool:
        try:
            fetch.fetch_dem(
                self._provider,
                self._wkt,
                self._epsg,
                self._source_resolution,
                self._out_path,
                output_resolution=self._output_resolution,
                resample_alg=self._resample_alg,
                clip=self._clip,
                progress=lambda frac, _msg: self.setProgress(max(0.0, min(1.0, frac)) * 100.0),
                should_cancel=self.isCanceled,
            )
        except Exception as exc:
            self.error_message = str(exc)
            return False
        return True


def _apply_hillshade(layer: QgsRasterLayer) -> None:
    """Render a DEM raster as a shaded relief (band 1, light from NW at 45°)."""
    renderer = QgsHillshadeRenderer(layer.dataProvider(), 1, 315.0, 45.0)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def _epsg_of(crs: QgsCoordinateReferenceSystem) -> int | None:
    authid = crs.authid()  # e.g. "EPSG:2056"
    if authid.startswith("EPSG:"):
        try:
            return int(authid.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _layer_name(path: str) -> str:
    import os

    return os.path.splitext(os.path.basename(path))[0] or "merged_dem"
