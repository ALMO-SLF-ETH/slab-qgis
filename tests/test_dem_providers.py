"""Unit tests for the DEM provider layer (no network, no QGIS, no GDAL)."""

from __future__ import annotations

import pytest
from slab_qgis.dem import providers


def _page(tile_id: str, next_url: str | None) -> dict:
    """A fake STAC item page with one tile exposing 0.5 m and 2 m GeoTIFF assets."""
    base = f"https://example/{tile_id}/"
    feature = {
        "id": tile_id,
        "assets": {
            f"{tile_id}_0.5_2056_5728.tif": {"href": base + f"{tile_id}_0.5_2056_5728.tif"},
            f"{tile_id}_0.5_2056_5728.xyz.zip": {"href": base + "x.xyz.zip"},
            f"{tile_id}_2_2056_5728.tif": {"href": base + f"{tile_id}_2_2056_5728.tif"},
        },
    }
    links = [{"rel": "next", "href": next_url}] if next_url else []
    return {"features": [feature], "links": links}


def test_registry_lists_swisstopo() -> None:
    ids = [p.id for p in providers.list_providers()]
    assert "swisstopo_alti3d" in ids
    assert providers.get_provider("swisstopo_alti3d").crs_epsg == 2056


def test_unknown_provider_raises() -> None:
    with pytest.raises(providers.DemFetchError):
        providers.get_provider("nope")


def test_tiles_filter_by_resolution_and_follow_pagination(monkeypatch) -> None:
    pages = {
        "START": _page("a_2501-1120", "PAGE2"),
        "PAGE2": _page("a_2501-1121", None),
    }
    calls: list[str] = []

    def fake_get_json(url: str, **_: object) -> dict:
        calls.append(url)
        # first call is the constructed items URL; subsequent calls use the 'next' href
        return pages["PAGE2"] if url == "PAGE2" else pages["START"]

    monkeypatch.setattr(providers.SwisstopoAlti3D, "_get_json", staticmethod(fake_get_json))
    prov = providers.SwisstopoAlti3D()

    tiles_half = prov.tiles_for_bbox(7.8, 46.4, 7.9, 46.5, 0.5)
    assert len(tiles_half) == 2  # one tile per page, both pages followed
    assert all(t.url.endswith("_0.5_2056_5728.tif") for t in tiles_half)
    assert len(calls) == 2  # pagination actually followed

    tiles_2m = prov.tiles_for_bbox(7.8, 46.4, 7.9, 46.5, 2.0)
    assert all(t.url.endswith("_2_2056_5728.tif") for t in tiles_2m)


def test_bad_resolution_raises() -> None:
    with pytest.raises(providers.DemFetchError):
        providers.SwisstopoAlti3D().tiles_for_bbox(7.8, 46.4, 7.9, 46.5, 5.0)
