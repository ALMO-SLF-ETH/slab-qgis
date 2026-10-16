"""Download and merge DEM tiles from online providers for a region of interest.

This subpackage is model-agnostic and has no hard dependency on ``qgis``:

* :mod:`almo_sweep_qgis.dem.providers` — a provider abstraction and a registry. A provider knows
  how to list the DEM tiles covering a geographic bounding box (e.g. via a STAC API). Adding a new
  data provider means adding one :class:`~almo_sweep_qgis.dem.providers.DemProvider` subclass and
  registering it; nothing else changes. Pure standard library, so it is unit-testable on its own.
* :mod:`almo_sweep_qgis.dem.fetch` — turns the listed tiles into one merged, clipped GeoTIFF using
  GDAL (``gdal.BuildVRT`` + ``gdal.Warp`` over ``/vsicurl/``). Imports ``osgeo`` lazily, so it runs
  under QGIS's Python without forcing GDAL on callers that only need the provider layer.

The GUI glue (region from a polygon layer, a background task, loading the result) lives in
``almo_sweep_qgis.panels.dem_download_dialog``.
"""

from almo_sweep_qgis.dem.providers import (
    DemProvider,
    TileRef,
    get_provider,
    list_providers,
)

__all__ = ["DemProvider", "TileRef", "get_provider", "list_providers"]
