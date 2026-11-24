"""Merge a provider's DEM tiles into one clipped GeoTIFF with GDAL.

The tiles are read in place over ``/vsicurl/`` (no manual download), mosaicked with
``gdal.BuildVRT`` and written with ``gdal.Warp`` -- optionally clipped to the region polygon. This
is the reliable replacement for dem_to_stl, which only loaded the tiles as layers. ``osgeo`` is
imported lazily so the provider layer stays GDAL-free.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from slab_qgis.dem.providers import DemFetchError, DemProvider

#: Called with (fraction in [0,1], message) as the fetch/merge progresses.
ProgressCb = Callable[[float, str], None]


def _osgeo():
    try:
        from osgeo import gdal, ogr, osr
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DemFetchError(
            "GDAL (osgeo) is required to merge DEM tiles but is not available."
        ) from exc
    gdal.UseExceptions()
    ogr.UseExceptions()
    osr.UseExceptions()
    return gdal, ogr, osr


def fetch_dem(
    provider: DemProvider,
    polygon_wkt: str,
    polygon_crs_epsg: int,
    source_resolution: float,
    out_path: str,
    *,
    output_resolution: float | None = None,
    resample_alg: str = "bilinear",
    clip: bool = True,
    progress: ProgressCb | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """Build a merged GeoTIFF of ``provider``'s tiles covering ``polygon_wkt``.

    ``source_resolution`` selects which tiles to fetch (the provider's native assets, e.g. 0.5 m or
    2 m). ``output_resolution`` is the grid the result is written at: when given (and different from
    the source), the output is resampled to it with ``resample_alg`` (a GDAL algorithm such as
    ``"bilinear"`` -- a 2x2 kernel -- ``"average"`` or ``"cubic"``); when omitted, the source
    resolution is kept as-is. ``polygon_wkt`` is the region of interest in
    EPSG:``polygon_crs_epsg``; with ``clip`` the output is cut to the polygon, otherwise to its
    bounding box. The output is in
    the provider's native CRS. Returns ``out_path``. Raises :class:`DemFetchError`.
    """
    gdal, ogr, osr = _osgeo()

    def report(frac: float, msg: str) -> None:
        if progress is not None:
            progress(frac, msg)

    # Make remote reads resilient.
    gdal.SetConfigOption("GDAL_HTTP_TIMEOUT", "60")
    gdal.SetConfigOption("GDAL_HTTP_MAX_RETRY", "3")
    gdal.SetConfigOption("GDAL_HTTP_RETRY_DELAY", "2")
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")

    report(0.02, "Querying provider for tiles…")
    out_res = (
        output_resolution if output_resolution and output_resolution > 0 else source_resolution
    )
    min_lon, min_lat, max_lon, max_lat = _bbox_wgs84(polygon_wkt, polygon_crs_epsg, ogr, osr)
    tiles = provider.tiles_for_bbox(min_lon, min_lat, max_lon, max_lat, source_resolution)
    if not tiles:
        raise DemFetchError(
            "No DEM tiles from this provider cover the selected region at this resolution."
        )
    report(0.10, f"{len(tiles)} tile(s) found; preparing mosaic…")

    vrt_path = "/vsimem/almo_dem_mosaic.vrt"
    sources = ["/vsicurl/" + t.url for t in tiles]
    vrt = gdal.BuildVRT(vrt_path, sources)
    if vrt is None:
        raise DemFetchError("Failed to build the tile mosaic (VRT).")
    vrt.FlushCache()
    vrt = None

    def _gdal_cb(complete: float, _msg: str, _data) -> int:
        if should_cancel is not None and should_cancel():
            return 0  # abort gdal.Warp
        report(0.2 + 0.78 * complete, "Downloading and merging tiles…")
        return 1

    warp_opts = dict(
        format="GTiff",
        dstSRS=f"EPSG:{provider.crs_epsg}",
        xRes=out_res,
        yRes=out_res,
        resampleAlg=resample_alg,
        creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES", "BIGTIFF=IF_SAFER"],
        multithread=True,
        callback=_gdal_cb,
    )
    cutline_path: str | None = None
    if clip:
        cutline_path = _write_cutline(polygon_wkt, polygon_crs_epsg, ogr, osr)
        warp_opts.update(cutlineDSName=cutline_path, cropToCutline=True)
    else:
        warp_opts.update(
            outputBounds=_bbox_in_crs(polygon_wkt, polygon_crs_epsg, provider.crs_epsg, ogr, osr)
        )

    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir or ".", exist_ok=True)

    report(0.20, "Downloading and merging tiles…")
    try:
        ds = gdal.Warp(out_path, vrt_path, **warp_opts)
    except RuntimeError as exc:
        raise DemFetchError(f"GDAL could not merge the tiles: {exc}") from exc
    finally:
        gdal.Unlink(vrt_path)
        if cutline_path is not None:
            gdal.Unlink(cutline_path)
    if ds is None:
        raise DemFetchError("GDAL warp/merge produced no output.")
    ds = None

    report(1.0, "Done.")
    return out_path


# --- geometry helpers -----------------------------------------------------------------
def _srs(epsg: int, osr):
    s = osr.SpatialReference()
    s.ImportFromEPSG(int(epsg))
    s.SetAxisMappingStrategy(
        osr.OAMS_TRADITIONAL_GIS_ORDER
    )  # always (x=lon/easting, y=lat/northing)
    return s


def _geom_in_crs(polygon_wkt: str, src_epsg: int, dst_epsg: int, ogr, osr):
    geom = ogr.CreateGeometryFromWkt(polygon_wkt)
    if geom is None:
        raise DemFetchError("The region polygon could not be parsed.")
    if int(src_epsg) != int(dst_epsg):
        tr = osr.CoordinateTransformation(_srs(src_epsg, osr), _srs(dst_epsg, osr))
        geom.Transform(tr)
    return geom


def _bbox_wgs84(polygon_wkt: str, src_epsg: int, ogr, osr) -> tuple[float, float, float, float]:
    geom = _geom_in_crs(polygon_wkt, src_epsg, 4326, ogr, osr)
    min_x, max_x, min_y, max_y = geom.GetEnvelope()  # (minX, maxX, minY, maxY)
    return min_x, min_y, max_x, max_y


def _bbox_in_crs(polygon_wkt: str, src_epsg: int, dst_epsg: int, ogr, osr) -> list[float]:
    geom = _geom_in_crs(polygon_wkt, src_epsg, dst_epsg, ogr, osr)
    min_x, max_x, min_y, max_y = geom.GetEnvelope()
    return [min_x, min_y, max_x, max_y]  # gdal.Warp outputBounds order


def _write_cutline(polygon_wkt: str, src_epsg: int, ogr, osr) -> str:
    """Write the region polygon (in its own CRS) to an in-memory GeoJSON for gdal.Warp's cutline.

    GDAL reprojects the cutline to the warp's CRS itself, as long as the layer carries an SRS.
    """
    path = "/vsimem/almo_dem_cutline.geojson"
    drv = ogr.GetDriverByName("GeoJSON")
    ds = drv.CreateDataSource(path)
    layer = ds.CreateLayer("cutline", _srs(src_epsg, osr), ogr.wkbPolygon)
    feat = ogr.Feature(layer.GetLayerDefn())
    geom = ogr.CreateGeometryFromWkt(polygon_wkt)
    if geom is None:
        raise DemFetchError("The region polygon could not be parsed.")
    feat.SetGeometry(geom)
    layer.CreateFeature(feat)
    feat = None
    ds = None  # flush to /vsimem
    return path
