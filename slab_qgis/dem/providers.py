"""DEM data providers: list the tiles covering a bounding box, independent of GDAL and QGIS.

A :class:`DemProvider` is the single extension point for a new source of DEM data. It declares its
native CRS and the resolutions it offers, and returns the download URLs of the tiles that cover a
WGS84 bounding box. The merge step (:mod:`slab_qgis.dem.fetch`) is provider-agnostic and works
off that list, so supporting another provider is just another subclass plus a ``register()`` call.

Only the standard library is used here, so providers can be unit-tested without GDAL or QGIS.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

#: Identifies this client to the data portals (some reject requests without a User-Agent).
_USER_AGENT = "slab-qgis-dem/0.1"


@dataclass(frozen=True)
class TileRef:
    """One DEM tile to fetch. ``url`` is a plain http(s) URL (the fetcher adds ``/vsicurl/``)."""

    url: str
    id: str = ""


class DemFetchError(Exception):
    """A provider could not list tiles, or the DEM could not be built (network/GDAL error)."""


class DemProvider(ABC):
    """A source of DEM tiles.

    Subclasses set :attr:`id`, :attr:`label`, :attr:`crs_epsg` (the CRS the tiles are stored in) and
    :attr:`resolutions` (metres per pixel, best/finest first), and implement :meth:`tiles_for_bbox`.
    """

    id: str = ""
    label: str = ""
    crs_epsg: int = 4326
    resolutions: tuple[float, ...] = ()

    @abstractmethod
    def tiles_for_bbox(
        self, min_lon: float, min_lat: float, max_lon: float, max_lat: float, resolution: float
    ) -> list[TileRef]:
        """Return the tiles intersecting the WGS84 (EPSG:4326) bbox at the given resolution."""
        raise NotImplementedError

    def default_resolution(self) -> float:
        return self.resolutions[0] if self.resolutions else 0.0

    # -- helpers for subclasses --------------------------------------------------------
    @staticmethod
    def _get_json(url: str, *, timeout: float = 60.0, retries: int = 3) -> dict:
        """GET a URL and parse JSON, retrying a few times on transient network errors."""
        last: Exception | None = None
        for _attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.load(resp)
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                last = exc
        raise DemFetchError(f"request failed after {retries} attempts: {url} ({last})")


class SwisstopoAlti3D(DemProvider):
    """swissALTI3D (swisstopo) via the data.geo.admin.ch STAC API.

    Replaces dem_to_stl's static CSV of tile URLs with a live STAC query, so the current tiles (and
    acquisition years) covering the region are always used. Each 1 km tile exposes a 0.5 m and a 2 m
    GeoTIFF; the resolution selects which asset to take.
    """

    id = "swisstopo_alti3d"
    label = "swissALTI3D (swisstopo, Switzerland)"
    crs_epsg = 2056  # CH1903+ / LV95
    resolutions = (0.5, 2.0)

    _STAC_ITEMS = (
        "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swissalti3d/items"
    )

    def tiles_for_bbox(
        self, min_lon: float, min_lat: float, max_lon: float, max_lat: float, resolution: float
    ) -> list[TileRef]:
        token = self._resolution_token(resolution)
        url: str | None = (
            f"{self._STAC_ITEMS}?bbox={min_lon},{min_lat},{max_lon},{max_lat}&limit=100"
        )
        tiles: dict[str, TileRef] = {}  # keyed by url to dedupe across pages
        pages = 0
        while url and pages < 1000:
            data = self._get_json(url)
            for feature in data.get("features", []):
                for key, asset in feature.get("assets", {}).items():
                    href = asset.get("href", "")
                    if key.endswith(".tif") and f"_{token}_{self.crs_epsg}_" in key and href:
                        tiles[href] = TileRef(url=href, id=str(feature.get("id", "")))
            url = self._next_link(data)
            pages += 1
        return list(tiles.values())

    @staticmethod
    def _resolution_token(resolution: float) -> str:
        # swisstopo encodes the resolution in the asset name as "0.5" or "2".
        if abs(resolution - 0.5) < 1e-9:
            return "0.5"
        if abs(resolution - 2.0) < 1e-9:
            return "2"
        raise DemFetchError(f"swissALTI3D offers 0.5 m or 2 m, not {resolution} m.")

    @staticmethod
    def _next_link(data: dict) -> str | None:
        for link in data.get("links", []):
            if link.get("rel") == "next" and link.get("href"):
                return str(link["href"])
        return None


# --- registry -------------------------------------------------------------------------
_REGISTRY: dict[str, DemProvider] = {}


def register(provider: DemProvider) -> None:
    _REGISTRY[provider.id] = provider


def get_provider(provider_id: str) -> DemProvider:
    try:
        return _REGISTRY[provider_id]
    except KeyError:
        raise DemFetchError(f"unknown DEM provider: {provider_id!r}") from None


def list_providers() -> list[DemProvider]:
    """Registered providers, in registration order (what the GUI offers in its dropdown)."""
    return list(_REGISTRY.values())


register(SwisstopoAlti3D())
