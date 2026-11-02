"""Load one run's full outputs onto the QGIS map, grouped under a layer-tree group.

A single run (whether a deterministic run from the Run page, or one sample picked out of a finished
sweep on the Results page) is loaded the same way: all of its outputs go into one group named after
the run. Inside that group:

* a directory of raster frames (e.g. dampm's ``grid_series``) becomes temporal subgroups that step
  in time under the Temporal Controller;
* a multi-band raster (``final_grid``, ``max_values``) is unpacked into one single-band
  pseudocolour (Turbo) layer per band;
* a vector output is added as-is.

This is GDAL/Qt (QGIS) code by nature; it is kept out of the page widgets so both pages share one
implementation. The caller supplies the run's ``output_dt`` (temporal frame spacing) and the
model manifest (to tell vector outputs from rasters).
"""

from __future__ import annotations

import contextlib
import glob
import os
from dataclasses import dataclass
from typing import Any

from qgis.core import (
    QgsBilinearRasterResampler,
    QgsDateTimeRange,
    QgsGradientColorRamp,
    QgsProject,
    QgsRasterLayer,
    QgsRasterLayerTemporalProperties,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QTime
from qgis.PyQt.QtGui import QColor

#: Frame-raster bands to present as temporal groups: (band index, subgroup label). Matches dampm's
#: grid rasters (band 1 = flow height h, band 2 = 2D speed v).
FRAME_BANDS = ((1, "flow height h"), (2, "velocity v_2D"))

#: dampm's nodata sentinel for its output rasters.
_NODATA = -9999.0


@dataclass
class LoadedRun:
    """The outcome of loading one run: how many layers were added, and whether a time series was."""

    layers: int
    frame_series: bool


def load_run_outputs(
    outputs: dict[str, str],
    run_id: str,
    *,
    manifest: Any | None = None,
    output_dt: float = 1.0,
) -> LoadedRun:
    """Load ``outputs`` (output name -> path) into one layer-tree group named ``run_id``.

    Returns a :class:`LoadedRun` with the layer count and whether a stepable frame series loaded
    (so the caller can hint about the Temporal Controller). An empty group is removed.
    """
    project = QgsProject.instance()
    max_values_path = outputs.get("max_values")
    run_group = project.layerTreeRoot().insertGroup(0, str(run_id))
    loaded = 0
    frame_series = False
    for name, path in outputs.items():
        if os.path.isdir(path):
            # A directory of raster frames -> temporal subgroup (e.g. dampm's grid_series).
            n = _load_frame_series(name, path, max_values_path, run_group, output_dt)
            loaded += n
            frame_series = frame_series or n > 0
            continue
        kind = manifest.outputs[name].kind if (manifest and name in manifest.outputs) else None
        if kind == "vector":
            vlayer = QgsVectorLayer(path, name, "ogr")
            if vlayer.isValid():
                project.addMapLayer(vlayer, False)
                run_group.addLayer(vlayer)
                loaded += 1
            continue
        loaded += _load_banded_raster(name, path, run_group)

    if loaded == 0:
        project.layerTreeRoot().removeChildNode(run_group)  # nothing loaded -> no empty group
    return LoadedRun(loaded, frame_series)


def _load_banded_raster(name: str, path: str, run_group: Any) -> int:
    """Unpack a multi-band raster into one single-band pseudocolour layer per band.

    The per-band layers go in a subgroup named after the output (e.g. ``final_grid`` with its
    h / v_2D / mass / v_x / v_y / v_z bands, ``max_values`` with h_max / v_2D_max). Band names
    come from the file's band descriptions when present.
    """
    probe = QgsRasterLayer(path, name)
    if not probe.isValid():
        return 0
    project = QgsProject.instance()
    ramp = turbo_ramp()
    subgroup = run_group.addGroup(name)
    count = 0
    for band in range(1, probe.bandCount() + 1):
        layer = QgsRasterLayer(path, f"{name} — {probe.bandName(band)}")
        if not layer.isValid():
            continue
        _style_singleband(layer, band, ramp)
        project.addMapLayer(layer, False)
        subgroup.addLayer(layer)
        count += 1
    if count == 0:
        run_group.removeChildNode(subgroup)
    return count


def _style_singleband(
    layer: QgsRasterLayer, band: int, ramp: Any, upper: float | None = None
) -> None:
    """Render ``band`` as single-band pseudocolour (Turbo) with bilinear display resampling."""
    dp = layer.dataProvider()
    dp.setNoDataValue(band, _NODATA)
    renderer = QgsSingleBandPseudoColorRenderer(dp, band)
    lo, hi = 0.0, upper
    if hi is None:
        with contextlib.suppress(Exception):
            stats = dp.bandStatistics(band)
            lo, hi = stats.minimumValue, stats.maximumValue
    renderer.setClassificationMin(float(lo))
    if hi is not None and hi > lo:
        renderer.setClassificationMax(float(hi))
    renderer.createShader(ramp.clone())
    layer.setRenderer(renderer)
    resampler = layer.resampleFilter()
    if resampler is not None:
        resampler.setZoomedInResampler(QgsBilinearRasterResampler())
        resampler.setZoomedOutResampler(QgsBilinearRasterResampler())


def _load_frame_series(
    name: str, folder: str, max_values_path: str | None, run_group: Any, output_dt: float
) -> int:
    """Load a folder of raster frames as temporal subgroups (inside ``run_group``).

    A ``name`` subgroup (e.g. ``grid_series``) holds one subgroup per band in :data:`FRAME_BANDS`,
    each frame a timestamp (spaced by the model's output interval). Frames are single-band
    pseudocolour (**Turbo**); the per-band colour upper bound is taken from ``max_values.tif`` (the
    run maxima), so colours are consistent across frames and equal to the run's true maximum. Times
    map onto a dummy datetime axis (QGIS has no 'elapsed seconds' axis); spacing uses ``output_dt``.
    """
    frames = sorted(glob.glob(os.path.join(folder, "*.tif")))
    if len(frames) < 2:
        return 0  # not a stepable series

    project = QgsProject.instance()
    spacing_s = float(output_dt or 1.0)
    base = QDateTime(QDate(2000, 1, 1), QTime(0, 0, 0))
    mode = (
        getattr(QgsRasterLayerTemporalProperties, "ModeFixedTemporalRange", None)
        or QgsRasterLayerTemporalProperties.TemporalMode.ModeFixedTemporalRange
    )
    ramp = turbo_ramp()
    uppers = _band_upper_bounds(max_values_path, [b for b, _ in FRAME_BANDS])

    parent = run_group.addGroup(name)
    total = 0
    for band, blabel in FRAME_BANDS:
        # upper colour bound: from max_values.tif, else the frames' own max for this band
        upper = uppers.get(band)
        if upper is None:
            upper = _frames_band_max(frames, band)
        subgroup = parent.addGroup(blabel)
        for i, path in enumerate(frames):
            layer = QgsRasterLayer(path, f"t = {i * spacing_s:g} s")
            if not layer.isValid() or layer.bandCount() < band:
                continue
            _style_singleband(layer, band, ramp, upper if upper and upper > 0 else None)

            tp = layer.temporalProperties()
            tp.setIsActive(True)
            tp.setMode(mode)
            start = base.addMSecs(int(i * spacing_s * 1000))
            end = base.addMSecs(int((i + 1) * spacing_s * 1000))
            # Half-open [start, end): adjacent frames must not share the boundary instant,
            # otherwise both render at that timestamp. (includeBeginning=True, includeEnd=False)
            tp.setFixedTemporalRange(QgsDateTimeRange(start, end, True, False))

            project.addMapLayer(layer, False)
            subgroup.addLayer(layer)
            total += 1

    if not total:
        run_group.removeChildNode(parent)
    return total


def _band_upper_bounds(max_values_path: str | None, bands: list[int]) -> dict[int, float]:
    """Per-band colour upper bounds from ``max_values.tif`` (band i max = run maximum)."""
    bounds: dict[int, float] = {}
    if not max_values_path or not os.path.isfile(max_values_path):
        return bounds
    mv = QgsRasterLayer(max_values_path, "max_values")
    if not mv.isValid():
        return bounds
    dp = mv.dataProvider()
    for band in bands:
        if band <= dp.bandCount():
            with contextlib.suppress(Exception):
                bounds[band] = dp.bandStatistics(band).maximumValue
    return bounds


def _frames_band_max(frames: list[str], band: int) -> float | None:
    """Largest value of ``band`` across all frames (fallback when max_values is absent)."""
    result: float | None = None
    for path in frames:
        layer = QgsRasterLayer(path, "f")
        if not layer.isValid() or layer.bandCount() < band:
            continue
        with contextlib.suppress(Exception):
            m = layer.dataProvider().bandStatistics(band).maximumValue
            result = m if result is None else max(result, m)
    return result


def turbo_ramp() -> Any:
    """The Turbo colour ramp (falling back to other perceptual ramps, then a gradient)."""
    style = QgsStyle.defaultStyle()
    for n in ("Turbo", "Viridis", "Spectral"):
        ramp = style.colorRamp(n)
        if ramp is not None:
            return ramp
    return QgsGradientColorRamp(QColor(0, 0, 255), QColor(255, 0, 0))


__all__ = ["FRAME_BANDS", "LoadedRun", "load_run_outputs", "turbo_ramp"]
