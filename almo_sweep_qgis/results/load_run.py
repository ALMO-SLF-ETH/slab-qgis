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
from pathlib import Path
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
    run_group = project.layerTreeRoot().insertGroup(0, str(run_id))
    loaded = 0
    frame_series = False
    for name, path in outputs.items():
        if os.path.isdir(path):
            # A directory of raster frames -> temporal subgroup (e.g. dampm's grid_series).
            n = _load_frame_series(name, path, run_group, output_dt)
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


def discover_outputs(output_dir: str) -> dict[str, str]:
    """Recover a run's outputs by scanning ``output_dir`` for the model's artefacts on disk.

    A successful run lists its outputs in ``result.json``; a *failed* run does not (it writes
    ``outputs: {}``), even when the model had already produced some or all of its rasters before
    failing. This scans the run folder for those artefacts and returns the same ``name -> path``
    shape a successful run would, so the outputs can still be loaded. Mirrors the dampm adapter's
    own output collection (``max_values``, ``final_grid``, ``grid_series``, ``particles``); empty
    if nothing is found.
    """
    root = Path(output_dir)
    found: dict[str, str] = {}
    if not root.is_dir():
        return found
    rasters = sorted(root.rglob("rasters_frame_*.tif"))
    particles = sorted(root.rglob("particles_frame_*.ply"))
    max_values = next(iter(root.rglob("max_values.tif")), None)
    if max_values is not None:
        found["max_values"] = str(max_values)
    if rasters:
        found["final_grid"] = str(rasters[-1])  # last frame = final state
        found["grid_series"] = str(rasters[-1].parent)  # the folder of frames (temporal series)
    if particles:
        found["particles"] = str(particles[-1].parent)
    return found


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
    layer: QgsRasterLayer,
    band: int,
    ramp: Any,
    lo: float | None = None,
    hi: float | None = None,
) -> None:
    """Render ``band`` as single-band pseudocolour (Turbo) with bilinear display resampling.

    ``lo``/``hi`` set the colour classification range; either left ``None`` is filled from this
    layer's own band statistics (so a standalone raster still gets a sensible range, while a frame
    of a series is coloured on its group's shared ensemble range passed in by the caller).
    """
    dp = layer.dataProvider()
    dp.setNoDataValue(band, _NODATA)
    renderer = QgsSingleBandPseudoColorRenderer(dp, band)
    if lo is None or hi is None:
        with contextlib.suppress(Exception):
            stats = dp.bandStatistics(band)
            lo = stats.minimumValue if lo is None else lo
            hi = stats.maximumValue if hi is None else hi
    lo = 0.0 if lo is None else float(lo)
    renderer.setClassificationMin(lo)
    if hi is not None and float(hi) > lo:
        renderer.setClassificationMax(float(hi))
    renderer.createShader(ramp.clone())
    layer.setRenderer(renderer)
    resampler = layer.resampleFilter()
    if resampler is not None:
        resampler.setZoomedInResampler(QgsBilinearRasterResampler())
        resampler.setZoomedOutResampler(QgsBilinearRasterResampler())


def _load_frame_series(name: str, folder: str, run_group: Any, output_dt: float) -> int:
    """Load a folder of raster frames as temporal subgroups (inside ``run_group``).

    A ``name`` subgroup (e.g. ``grid_series``) holds one subgroup per band in :data:`FRAME_BANDS``
    (flow height h, velocity v_2D), each frame a timestamp (spaced by the model's output interval).
    Every frame is single-band pseudocolour (**Turbo**) and, within a band's subgroup, every frame
    is coloured on the **same** range — the ensemble min/max of that band across *all* frames in the
    group — so colours are directly comparable from one frame to the next. Times map onto a dummy
    datetime axis (QGIS has no 'elapsed seconds' axis); spacing uses ``output_dt``.
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
    # One shared colour range per band, from the ensemble of every frame in this group.
    ranges = _frames_band_ranges(frames, [b for b, _ in FRAME_BANDS])

    parent = run_group.addGroup(name)
    total = 0
    for band, blabel in FRAME_BANDS:
        lo, hi = ranges.get(band, (None, None))
        subgroup = parent.addGroup(blabel)
        for i, path in enumerate(frames):
            layer = QgsRasterLayer(path, f"t = {i * spacing_s:g} s")
            if not layer.isValid() or layer.bandCount() < band:
                continue
            _style_singleband(layer, band, ramp, lo=lo, hi=hi)

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


def _frames_band_ranges(
    frames: list[str], bands: list[int]
) -> dict[int, tuple[float | None, float | None]]:
    """Ensemble (min, max) of each band across all ``frames`` — the group's shared colour range.

    Opens each frame once and folds its per-band statistics into a running min/max. The frames'
    nodata sentinel is set before reading statistics so it is excluded (otherwise it would drag the
    minimum down to the sentinel value). A band with no readable statistics comes back ``(None,
    None)``, which leaves the colouring to each frame's own range.
    """
    acc: dict[int, list[float | None]] = {b: [None, None] for b in bands}
    for path in frames:
        layer = QgsRasterLayer(path, "f")
        if not layer.isValid():
            continue
        dp = layer.dataProvider()
        band_count = layer.bandCount()
        for band in bands:
            if band > band_count:
                continue
            dp.setNoDataValue(band, _NODATA)  # exclude the sentinel from the statistics
            with contextlib.suppress(Exception):
                stats = dp.bandStatistics(band)
                lo, hi = acc[band]
                acc[band][0] = stats.minimumValue if lo is None else min(lo, stats.minimumValue)
                acc[band][1] = stats.maximumValue if hi is None else max(hi, stats.maximumValue)
    return {band: (lo, hi) for band, (lo, hi) in acc.items()}


def turbo_ramp() -> Any:
    """The Turbo colour ramp (falling back to other perceptual ramps, then a gradient)."""
    style = QgsStyle.defaultStyle()
    for n in ("Turbo", "Viridis", "Spectral"):
        ramp = style.colorRamp(n)
        if ramp is not None:
            return ramp
    return QgsGradientColorRamp(QColor(0, 0, 255), QColor(255, 0, 0))


__all__ = ["FRAME_BANDS", "LoadedRun", "discover_outputs", "load_run_outputs", "turbo_ramp"]
