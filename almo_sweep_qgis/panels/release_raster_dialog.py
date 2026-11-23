"""Dialog to turn a polygon layer into a release-depth raster for a dampm RASTER release.

The user picks a polygon layer (the release zones), a release depth, an output cell size, and
optionally a corner-rounding radius and a border taper width. The polygons are extruded to the
chosen depth, their corners rounded, and the depth feathered to the edges; the result is written as
a single-band GeoTIFF (``0`` where no material) and added to the map, ready to select as the
``release_raster`` input. The heavy raster work runs in a background task so the UI stays
responsive. See :mod:`almo_sweep_qgis.release_raster` for the computation.
"""

from __future__ import annotations

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsMapLayerProxyModel,
    QgsProject,
    QgsRasterLayer,
    QgsSingleBandPseudoColorRenderer,
    QgsTask,
    QgsVectorLayer,
)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from almo_sweep_qgis import release_raster
from almo_sweep_qgis.results.load_run import turbo_ramp


class ReleaseRasterDialog(QDialog):
    """Build a release-depth raster from a polygon layer (extrude, round corners, taper edges)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Build release raster from polygons")
        self.setMinimumWidth(500)
        self._task: QgsTask | None = None
        self._out_path = ""
        self._add_to_map = True

        caption = QLabel(
            "Fill one or more polygons with a release depth to make a RASTER release for dampm. "
            "Corners can be rounded and the depth feathered to the edges. The raster is 0 where no "
            "material sits. At run time only cells at or above the model's 'Raster minimum depth' "
            "(h_min) release, so keep the depth above it."
        )
        caption.setWordWrap(True)
        caption.setEnabled(False)

        form = QFormLayout()

        self._zones_combo = QgsMapLayerComboBox()
        self._zones_combo.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        self._zones_combo.setToolTip("The polygon layer whose features define where material sits")
        form.addRow("Release zones (polygons):", self._zones_combo)

        self._height_spin = _metre_spin(1.0, 0.01, 1000.0, decimals=2)
        self._height_spin.setToolTip("Release depth to extrude inside the polygons")
        form.addRow("Release depth:", self._height_spin)

        self._res_spin = _metre_spin(1.0, 0.01, 1000.0, decimals=2)
        self._res_spin.setToolTip(
            "Output cell size. Match (or better) your DEM resolution for a clean release."
        )
        form.addRow("Resolution (cell size):", self._res_spin)

        self._corner_spin = _metre_spin(0.0, 0.0, 10000.0, decimals=2)
        self._corner_spin.setToolTip(
            "Round the polygon corners with this radius (0 = keep the polygon corners sharp)."
        )
        form.addRow("Corner rounding radius:", self._corner_spin)

        self._taper_spin = _metre_spin(0.0, 0.0, 10000.0, decimals=2)
        self._taper_spin.setToolTip(
            "Feather the depth from full value in the interior down to 0 at the border over this "
            "distance (0 = a flat slab with vertical edges)."
        )
        form.addRow("Border taper width:", self._taper_spin)

        self._out_widget = QgsFileWidget()
        self._out_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self._out_widget.setFilter("GeoTIFF (*.tif)")
        self._out_widget.setDialogTitle("Save release raster as…")
        form.addRow("Output file:", self._out_widget)

        self._add_check = QCheckBox("Add the result to the map")
        self._add_check.setChecked(True)
        form.addRow("", self._add_check)

        root = QVBoxLayout(self)
        root.addWidget(caption)
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
        self._build_button = QPushButton("Build")
        self._build_button.clicked.connect(self._on_build)
        buttons.addButton(self._build_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # -- helpers -----------------------------------------------------------------------
    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    def _collect_polygons(self) -> tuple[list[str], str] | None:
        """WKTs of the selected polygon layer's features and its CRS WKT, or None with a status.

        Uses the layer's selected features when there is a selection, otherwise all of them. The
        CRS must be metric, since every length in this tool is in metres.
        """
        layer = self._zones_combo.currentLayer()
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            self._set_status("Select a valid polygon layer.")
            return None
        if layer.crs().mapUnits() != Qgis.DistanceUnit.Meters:
            self._set_status(
                "The polygon layer's CRS is not in metres. Re-project it to a metric CRS "
                "(e.g. EPSG:2056) so the depths and radii are in metres, then try again."
            )
            return None
        source = (
            layer.getSelectedFeatures() if layer.selectedFeatureCount() > 0 else layer.getFeatures()
        )
        wkts = [f.geometry().asWkt() for f in source if not f.geometry().isEmpty()]
        if not wkts:
            self._set_status("The polygon layer has no (non-empty) features.")
            return None
        return wkts, layer.crs().toWkt()

    # -- actions -----------------------------------------------------------------------
    def _on_build(self) -> None:
        if self._task is not None:
            return  # a build is already running
        collected = self._collect_polygons()
        if collected is None:
            return
        out_path = self._out_widget.filePath().strip()
        if not out_path:
            self._set_status("Choose an output file.")
            return
        if not out_path.lower().endswith(".tif"):
            out_path += ".tif"

        wkts, crs_wkt = collected
        self._out_path = out_path
        self._add_to_map = self._add_check.isChecked()

        self._progress.setRange(0, 0)  # busy indicator — the build is a single, non-chunked step
        self._build_button.setEnabled(False)
        self._set_status(f"Building release raster from {len(wkts)} polygon(s)…")

        task = _BuildReleaseRasterTask(
            wkts,
            crs_wkt,
            out_path,
            height_m=float(self._height_spin.value()),
            resolution_m=float(self._res_spin.value()),
            corner_radius_m=float(self._corner_spin.value()),
            taper_width_m=float(self._taper_spin.value()),
        )
        task.taskCompleted.connect(self._on_completed)
        task.taskTerminated.connect(self._on_terminated)
        self._task = task
        QgsApplication.taskManager().addTask(task)

    def _on_completed(self) -> None:
        result = self._task.result if isinstance(self._task, _BuildReleaseRasterTask) else None
        self._task = None
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._build_button.setEnabled(True)
        summary = ""
        if result is not None:
            summary = (
                f" — {result.ncols}x{result.nrows} cells at {result.resolution_m:g} m, "
                f"max depth {result.max_height_m:g} m, ~{result.volume_m3:,.0f} m³"
            )
        if self._add_to_map:
            layer = QgsRasterLayer(self._out_path, _layer_name(self._out_path))
            if layer.isValid():
                _style_release(layer, result.max_height_m if result else 0.0)
                QgsProject.instance().addMapLayer(layer)
                self._set_status(f"Done{summary}. Saved to {self._out_path} and added to the map.")
            else:
                self._set_status(f"Saved to {self._out_path}{summary}, but it could not be loaded.")
        else:
            self._set_status(f"Done{summary}. Saved to {self._out_path}.")

    def _on_terminated(self) -> None:
        message = (
            self._task.error_message if isinstance(self._task, _BuildReleaseRasterTask) else None
        )
        self._task = None
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._build_button.setEnabled(True)
        self._set_status(f"Could not build the release raster: {message or 'cancelled'}")


class _BuildReleaseRasterTask(QgsTask):
    """Runs :func:`release_raster.build_release_raster` off the main thread (GDAL + SciPy block)."""

    def __init__(
        self,
        polygons_wkt: list[str],
        crs_wkt: str,
        out_path: str,
        *,
        height_m: float,
        resolution_m: float,
        corner_radius_m: float,
        taper_width_m: float,
    ) -> None:
        super().__init__("Build release raster", QgsTask.Flag.CanCancel)
        self._wkts = polygons_wkt
        self._crs_wkt = crs_wkt
        self._out_path = out_path
        self._height_m = height_m
        self._resolution_m = resolution_m
        self._corner_radius_m = corner_radius_m
        self._taper_width_m = taper_width_m
        self.result: release_raster.ReleaseRasterResult | None = None
        self.error_message: str | None = None

    def run(self) -> bool:
        try:
            self.result = release_raster.build_release_raster(
                self._wkts,
                self._crs_wkt,
                self._out_path,
                height_m=self._height_m,
                resolution_m=self._resolution_m,
                corner_radius_m=self._corner_radius_m,
                taper_width_m=self._taper_width_m,
            )
        except Exception as exc:
            self.error_message = str(exc)
            return False
        return True


def _metre_spin(value: float, lo: float, hi: float, *, decimals: int) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setDecimals(decimals)
    spin.setRange(lo, hi)
    spin.setSingleStep(0.5)
    spin.setSuffix(" m")
    spin.setValue(value)
    return spin


def _style_release(layer: QgsRasterLayer, upper: float) -> None:
    """Render the release raster as single-band pseudocolour (Turbo), 0..max depth."""
    dp = layer.dataProvider()
    renderer = QgsSingleBandPseudoColorRenderer(dp, 1)
    renderer.setClassificationMin(0.0)
    if upper > 0:
        renderer.setClassificationMax(float(upper))
    renderer.createShader(turbo_ramp().clone())
    layer.setRenderer(renderer)


def _layer_name(path: str) -> str:
    import os

    return os.path.splitext(os.path.basename(path))[0] or "release_raster"
