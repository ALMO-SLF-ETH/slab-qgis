"""Estimate a release's area and volume from the current parameter values.

A quick, approximate figure shown live in the UI as the user configures the release, so a
mistake (wrong depth, a huge polygon, a release raster in the wrong units) is obvious before a
run. It follows the model's release convention by well-known names (``release_type``, ``h0``,
the rectangle/ellipse geometry params, ``raster_h_min``); a model that does not use them yields
no estimate and the UI simply hides it.

The geometry maths is pure; reading a release raster needs GDAL, imported lazily, and returns
``None`` when it is unavailable or the raster cannot be read. Areas are in the layer's CRS units
squared (m² for a metric CRS such as EPSG:2056) and volumes in those units cubed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReleaseEstimate:
    """An approximate release size. ``None`` fields mean "not enough information yet"."""

    area_m2: float | None
    volume_m3: float | None
    detail: str  # short note on how it was derived, e.g. "rectangle Lx x Ly x h0"


def _num(params: dict[str, Any], name: str) -> float | None:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def rectangle_area(lx: float, ly: float) -> float:
    return abs(lx) * abs(ly)


def ellipse_area(rx: float, ry: float) -> float:
    return math.pi * abs(rx) * abs(ry)


def compute_release_estimate(
    params: dict[str, Any],
    *,
    polygon_area_m2: float | None = None,
    release_raster_path: str | None = None,
) -> ReleaseEstimate:
    """Estimate area and volume for the current ``release_type``.

    ``polygon_area_m2`` is the selected release-zone layer's area (the caller computes it from the
    layer, so this module stays QGIS-free). ``release_raster_path`` is the release-depth raster on
    disk for a RASTER release. ``h0`` is the uniform release depth for the non-raster shapes.
    """
    rtype = params.get("release_type")
    if rtype is None:
        return ReleaseEstimate(None, None, "")
    rtype = str(rtype).upper()
    h0 = _num(params, "h0")

    if rtype == "RECTANGLE":
        lx, ly = _num(params, "rect_Lx"), _num(params, "rect_Ly")
        if lx is None or ly is None:
            return ReleaseEstimate(None, None, "rectangle")
        area = rectangle_area(lx, ly)
        return ReleaseEstimate(area, _vol(area, h0), "rectangle Lx x Ly x h0")

    if rtype == "ELLIPSE":
        rx, ry = _num(params, "ell_rx"), _num(params, "ell_ry")
        if rx is None or ry is None:
            return ReleaseEstimate(None, None, "ellipse")
        area = ellipse_area(rx, ry)
        return ReleaseEstimate(area, _vol(area, h0), "pi x rx x ry x h0")

    if rtype == "POLYGON":
        if polygon_area_m2 is None:
            return ReleaseEstimate(None, None, "polygon (select a layer)")
        return ReleaseEstimate(polygon_area_m2, _vol(polygon_area_m2, h0), "polygon area x h0")

    if rtype == "RASTER":
        if not release_raster_path:
            return ReleaseEstimate(None, None, "raster (select a layer)")
        h_min = _num(params, "raster_h_min") or 0.0
        return raster_release(release_raster_path, h_min)

    return ReleaseEstimate(None, None, str(rtype))


def _vol(area: float | None, height: float | None) -> float | None:
    if area is None or height is None:
        return None
    return area * height


def raster_release(path: str, h_min: float) -> ReleaseEstimate:
    """Area and volume of a release-depth raster: released cells are those with depth >= h_min.

    Area = (cell size)^2 x (number of released cells); volume = (cell size)^2 x (sum of their
    depths). nodata cells are ignored. Returns empty fields if GDAL is missing or the read fails.
    """
    detail = "raster: cells x cell area / sum(depth) x cell area"
    try:
        import numpy as np
        from osgeo import gdal

        gdal.UseExceptions()
        ds = gdal.Open(path)
        if ds is None:
            return ReleaseEstimate(None, None, "raster (unreadable)")
        gt = ds.GetGeoTransform()
        cell_area = abs(gt[1]) * abs(gt[5])
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        arr = band.ReadAsArray().astype("float64")
        ds = None

        mask = np.isfinite(arr)
        if nodata is not None:
            mask &= arr != nodata
        # Released cells: a positive depth at or above the minimum release depth.
        threshold = max(h_min, 0.0)
        released = mask & (arr >= threshold) & (arr > 0.0)
        count = int(released.sum())
        depth_sum = float(arr[released].sum()) if count else 0.0
        return ReleaseEstimate(count * cell_area, depth_sum * cell_area, detail)
    except Exception:
        return ReleaseEstimate(None, None, "raster (unreadable)")
