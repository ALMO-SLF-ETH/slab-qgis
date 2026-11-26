"""Resample an existing raster (e.g. a DEM) to a different resolution with GDAL.

The DEM-download path resamples while merging tiles (see :mod:`slab_qgis.dem.fetch`);
this does the same for a raster already on disk, so a DEM loaded at its native resolution can be
coarsened or refined to the grid a run needs. ``osgeo`` is imported lazily so importing this
module never requires GDAL.
"""

from __future__ import annotations

import os
from collections.abc import Callable

#: Called with (fraction in [0, 1], message) as the resample progresses.
ProgressCb = Callable[[float, str], None]

#: (label, GDAL algorithm) choices for resampling-method dropdowns; the first is the default.
RESAMPLE_METHODS = (
    ("Bilinear (2x2 kernel)", "bilinear"),
    ("Nearest neighbour", "near"),
    ("Cubic (4x4 kernel)", "cubic"),
    ("Cubic spline", "cubicspline"),
    ("Lanczos", "lanczos"),
    ("Average (mean of covered cells; for downsampling)", "average"),
)


class ResampleError(RuntimeError):
    """A raster could not be resampled."""


def _gdal():
    try:
        from osgeo import gdal
    except ImportError as exc:  # pragma: no cover - environment dependent
        msg = "GDAL (osgeo) is required to resample a raster but is not available."
        raise ResampleError(msg) from exc
    gdal.UseExceptions()
    return gdal


def res_suffix(resolution: float) -> str:
    """Filename token for a resolution in metres: 0.5 -> ``05m``, 1 -> ``1m``, 10 -> ``10m``."""
    return f"{resolution:g}".replace(".", "").replace("-", "") + "m"


def default_resampled_path(src_path: str, resolution: float) -> str:
    """Default output path: the source name with the resolution token, in the source's folder."""
    base, _ext = os.path.splitext(src_path)
    return f"{base}_{res_suffix(resolution)}.tif"


def raster_resolution(path: str) -> tuple[float, float] | None:
    """The (x, y) pixel size of ``path`` in its CRS units, or None if it cannot be read."""
    try:
        gdal = _gdal()
        ds = gdal.Open(path)
        if ds is None:
            return None
        gt = ds.GetGeoTransform()
        ds = None
        return abs(gt[1]), abs(gt[5])
    except Exception:
        return None


def resample_raster(
    src_path: str,
    dst_path: str,
    resolution: float,
    resample_alg: str = "bilinear",
    *,
    progress: ProgressCb | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """Write ``src_path`` to ``dst_path`` at ``resolution`` (CRS units) and return ``dst_path``.

    ``resample_alg`` is a GDAL algorithm name such as ``"bilinear"`` (a 2x2 kernel), ``"average"``
    (good for downsampling) or ``"cubic"``. The CRS is preserved (no reprojection). Raises
    :class:`ResampleError` on failure.
    """
    if resolution <= 0:
        raise ResampleError("the target resolution must be positive")
    gdal = _gdal()

    def _cb(complete: float, _msg: str, _data) -> int:
        if should_cancel is not None and should_cancel():
            return 0  # abort gdal.Warp
        if progress is not None:
            progress(max(0.0, min(1.0, complete)), "Resampling…")
        return 1

    out_dir = os.path.dirname(os.path.abspath(dst_path))
    os.makedirs(out_dir or ".", exist_ok=True)

    if progress is not None:
        progress(0.0, "Resampling…")
    try:
        ds = gdal.Warp(
            dst_path,
            src_path,
            format="GTiff",
            xRes=resolution,
            yRes=resolution,
            resampleAlg=resample_alg,
            creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES", "BIGTIFF=IF_SAFER"],
            multithread=True,
            callback=_cb,
        )
    except RuntimeError as exc:
        raise ResampleError(f"GDAL could not resample the raster: {exc}") from exc
    if ds is None:
        raise ResampleError("GDAL resample produced no output (it may have been cancelled).")
    ds = None
    if progress is not None:
        progress(1.0, "Done.")
    return dst_path
