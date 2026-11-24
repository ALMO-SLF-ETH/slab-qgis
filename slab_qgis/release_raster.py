"""Turn a polygon layer into a release-depth raster for a Slab-Pro RASTER release.

A release raster tells the model *where* material starts and *how deep* it is: every cell holds a
release height in metres, and cells with no material are ``0``. This module builds one from a set
of polygons (the release zones) and a user-chosen depth:

* **Extrude** — each polygon is filled with the chosen height ``height_m``.
* **Round corners** — an optional morphological opening+closing with a disk of radius
  ``corner_radius_m`` rounds both convex and concave corners, so the release outline is smooth
  rather than polygonal.
* **Taper the depth to the edges** — an optional border taper ramps the height smoothly from the
  full depth in the interior down to ~0 at the polygon border over ``taper_width_m`` metres, so
  the released slab has feathered edges instead of a vertical wall.

The result is a single-band Float32 GeoTIFF covering the polygons' extent (plus a small zero
margin), in the polygons' CRS, with ``0`` wherever no material sits — ready to feed Slab-Pro as the
``release_raster`` input (a RASTER release). ``osgeo``/``scipy`` are imported lazily so importing
this module never requires them.

All lengths (``height_m``, ``corner_radius_m``, ``taper_width_m``, ``resolution_m``) are in the
CRS's units, which must be metres — the caller validates that before building.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

#: Slab-Pro's nodata sentinel. The raster never actually contains it (no-material cells are 0, which
#: Slab-Pro reads as "below any h_min, so no release"), but it is tagged for consistency with the rest.
NODATA = -9999.0


class ReleaseRasterError(RuntimeError):
    """A release raster could not be built."""


@dataclass(frozen=True)
class ReleaseRasterResult:
    """Summary of a built release raster, for user feedback."""

    path: str
    ncols: int
    nrows: int
    resolution_m: float
    #: Bounding box actually written, (xmin, ymin, xmax, ymax) in the CRS.
    extent: tuple[float, float, float, float]
    #: Number of cells that carry material (height > 0).
    material_cells: int
    #: Largest cell height in the raster (metres); <= ``height_m``.
    max_height_m: float
    #: Rough released volume, sum(height) * cell area (cubic metres).
    volume_m3: float


def _gdal():
    try:
        from osgeo import gdal, ogr, osr
    except ImportError as exc:  # pragma: no cover - environment dependent
        msg = "GDAL (osgeo) is required to build a release raster but is not available."
        raise ReleaseRasterError(msg) from exc
    gdal.UseExceptions()
    ogr.UseExceptions()
    osr.UseExceptions()
    return gdal, ogr, osr


def _edt():
    try:
        from scipy.ndimage import distance_transform_edt
    except ImportError as exc:  # pragma: no cover - environment dependent
        msg = (
            "SciPy is required for corner rounding and depth tapering but is not available. "
            "Install scipy, or build with both radii set to 0 to skip smoothing."
        )
        raise ReleaseRasterError(msg) from exc
    return distance_transform_edt


def build_release_raster(
    polygons_wkt: list[str],
    crs_wkt: str,
    out_path: str,
    *,
    height_m: float,
    resolution_m: float,
    corner_radius_m: float = 0.0,
    taper_width_m: float = 0.0,
) -> ReleaseRasterResult:
    """Build a release-depth GeoTIFF from ``polygons_wkt``; return a :class:`ReleaseRasterResult`.

    ``polygons_wkt`` are polygon geometries (WKT) in the CRS given by ``crs_wkt``; ``height_m`` is
    the release depth to extrude; ``resolution_m`` is the output cell size. ``corner_radius_m`` > 0
    rounds the polygon corners, and ``taper_width_m`` > 0 feathers the depth to zero over that
    distance from the border. Raises :class:`ReleaseRasterError` on bad input or if smoothing
    removes all material (e.g. a corner radius wider than the polygon).
    """
    if not polygons_wkt:
        raise ReleaseRasterError("no polygons were given")
    if height_m <= 0:
        raise ReleaseRasterError("the release height must be positive")
    if resolution_m <= 0:
        raise ReleaseRasterError("the resolution must be positive")
    if corner_radius_m < 0 or taper_width_m < 0:
        raise ReleaseRasterError("the corner radius and taper width cannot be negative")

    import numpy as np

    gdal, ogr, osr = _gdal()
    res = float(resolution_m)

    srs = osr.SpatialReference()
    if srs.ImportFromWkt(crs_wkt) != 0:
        raise ReleaseRasterError("the layer CRS could not be interpreted")

    # Build an in-memory OGR layer from the WKTs, so the rasterizer can burn them.
    vec = ogr.GetDriverByName("MEM").CreateDataSource("rel")
    layer = vec.CreateLayer("zones", srs, ogr.wkbPolygon)
    envelope: list[float] | None = None
    for wkt in polygons_wkt:
        geom = ogr.CreateGeometryFromWkt(wkt)
        if geom is None or geom.IsEmpty():
            continue
        feat = ogr.Feature(layer.GetLayerDefn())
        feat.SetGeometry(geom)
        layer.CreateFeature(feat)
        xmin, xmax, ymin, ymax = geom.GetEnvelope()
        envelope = (
            [xmin, ymin, xmax, ymax]
            if envelope is None
            else [
                min(envelope[0], xmin),
                min(envelope[1], ymin),
                max(envelope[2], xmax),
                max(envelope[3], ymax),
            ]
        )
    if envelope is None:
        raise ReleaseRasterError("the polygons are all empty")

    # Pad so rounded corners (which can bulge outward by up to the radius) and a zero ring fit,
    # then snap the origin to a clean multiple of the resolution.
    pad = corner_radius_m + 2.0 * res
    xmin = math.floor((envelope[0] - pad) / res) * res
    ymin = math.floor((envelope[1] - pad) / res) * res
    xmax = math.ceil((envelope[2] + pad) / res) * res
    ymax = math.ceil((envelope[3] + pad) / res) * res
    ncols = max(1, round((xmax - xmin) / res))
    nrows = max(1, round((ymax - ymin) / res))

    # Rasterize the polygons to a 0/1 mask (row 0 = top, i.e. ymax).
    mem = gdal.GetDriverByName("MEM").Create("", ncols, nrows, 1, gdal.GDT_Byte)
    mem.SetGeoTransform((xmin, res, 0.0, ymax, 0.0, -res))
    mem.SetProjection(crs_wkt)
    if gdal.RasterizeLayer(mem, [1], layer, burn_values=[1]) != 0:
        raise ReleaseRasterError("the polygons could not be rasterized")
    mask = mem.ReadAsArray().astype(bool)
    if not mask.any():
        raise ReleaseRasterError(
            "no cells fall inside the polygons at this resolution — use a finer resolution"
        )

    if corner_radius_m > 0:
        mask = _round_corners(mask, corner_radius_m / res)
        if not mask.any():
            raise ReleaseRasterError(
                "corner rounding removed all material — the radius is larger than the polygons; "
                "reduce the corner radius"
            )

    height = _taper_height(mask, float(height_m), taper_width_m / res, np)

    _write_geotiff(gdal, out_path, height, (xmin, res, ymax, -res), crs_wkt, np)

    cell_area = res * res
    material_cells = int((height > 0).sum())
    return ReleaseRasterResult(
        path=out_path,
        ncols=ncols,
        nrows=nrows,
        resolution_m=res,
        extent=(xmin, ymin, xmax, ymax),
        material_cells=material_cells,
        max_height_m=float(height.max()),
        volume_m3=float(height.sum()) * cell_area,
    )


def _round_corners(mask, radius_cells: float):
    """Round a binary mask's corners by a morphological opening then closing with a disk.

    Opening (erode then dilate) rounds convex corners and drops protrusions thinner than the disk;
    closing (dilate then erode) rounds concave corners and fills notches thinner than the disk. Both
    use an exact Euclidean radius via the distance transform, so the rounding matches the requested
    metric radius rather than a blocky structuring element.
    """
    if radius_cells < 0.5:
        return mask  # sub-cell radius: nothing to round
    edt = _edt()

    def erode(m):
        # Erosion of an empty set is empty; guard it so we never call the EDT on an all-False array
        # (whose "distance to background" is degenerate).
        return m if not m.any() else edt(m) > radius_cells

    def dilate(m):
        # Dilation of an empty set is empty; the EDT of an all-True array has no background and is
        # degenerate, so short-circuit here too.
        return m if not m.any() else edt(~m) <= radius_cells

    opened = dilate(erode(mask))  # rounds convex corners, drops thin protrusions
    return erode(dilate(opened))  # rounds concave corners, fills thin notches


def _taper_height(mask, height_m: float, taper_cells: float, np):
    """Height field: ``height_m`` inside, feathered to ~0 at the border over ``taper_cells`` cells.

    Without a taper the depth is a flat slab (full height inside, 0 outside). With one, the height
    follows a smoothstep of the distance to the border, so the slab has smooth, feathered edges
    instead of a vertical wall.
    """
    out = np.zeros(mask.shape, dtype="float32")
    if taper_cells <= 0:
        out[mask] = height_m
        return out
    edt = _edt()
    dist = edt(mask)  # cells from each inside cell to the nearest outside cell (0 outside)
    t = np.clip(dist / taper_cells, 0.0, 1.0)
    ramp = t * t * (3.0 - 2.0 * t)  # smoothstep: flat at both ends, smooth in between
    out[mask] = (height_m * ramp[mask]).astype("float32")
    return out


def _write_geotiff(gdal, out_path, array, geo, crs_wkt, np) -> None:
    """Write ``array`` as a single-band Float32 GeoTIFF. ``geo`` is (xmin, res, ymax, -res)."""
    xmin, res, ymax, neg_res = geo
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir or ".", exist_ok=True)
    nrows, ncols = array.shape
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(
        out_path,
        ncols,
        nrows,
        1,
        gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES"],
    )
    if ds is None:
        raise ReleaseRasterError(f"could not create the output raster: {out_path}")
    ds.SetGeoTransform((xmin, res, 0.0, ymax, 0.0, neg_res))
    ds.SetProjection(crs_wkt)
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(np.ascontiguousarray(array, dtype="float32"))
    band.FlushCache()
    ds = None


__all__ = ["NODATA", "ReleaseRasterError", "ReleaseRasterResult", "build_release_raster"]
