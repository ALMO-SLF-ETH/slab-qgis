"""Tests for building a release-depth raster from polygons (extrude, round corners, taper edges)."""

from __future__ import annotations

from pathlib import Path

import pytest
from slab_qgis.release_raster import (
    ReleaseRasterError,
    build_release_raster,
)


def _crs_wkt(epsg: int = 2056) -> str:
    from osgeo import osr

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    return srs.ExportToWkt()


def _square_wkt(x0: float, y0: float, x1: float, y1: float) -> str:
    return f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"


def _read(path: str):
    from osgeo import gdal

    gdal.UseExceptions()
    ds = gdal.Open(path)
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray()
    gt = ds.GetGeoTransform()
    nodata = band.GetNoDataValue()
    ds = None
    return arr, gt, nodata


def _value_at(arr, gt, x: float, y: float) -> float:
    """Sample the raster value at world coordinate (x, y)."""
    col = int((x - gt[0]) / gt[1])
    row = int((y - gt[3]) / gt[5])  # gt[5] is negative
    return float(arr[row, col])


def test_flat_slab_fills_polygon_and_zeros_outside(tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    out = str(tmp_path / "release.tif")
    poly = _square_wkt(100.0, 100.0, 140.0, 140.0)  # 40 x 40 m square

    res = build_release_raster([poly], _crs_wkt(), out, height_m=5.0, resolution_m=1.0)

    assert res.max_height_m == pytest.approx(5.0)
    assert res.material_cells == pytest.approx(40 * 40, rel=0.05)  # ~1600 cells
    assert res.volume_m3 == pytest.approx(40 * 40 * 1.0 * 5.0, rel=0.05)

    arr, gt, nodata = _read(out)
    assert nodata == pytest.approx(-9999.0)
    assert _value_at(arr, gt, 120.0, 120.0) == pytest.approx(5.0)  # interior: full depth
    # A point in the margin ring (inside the raster, outside the polygon) is zero.
    assert _value_at(arr, gt, gt[0] + 0.5, 120.0) == pytest.approx(0.0)
    # The raster covers the polygon's extent with a zero margin around it.
    assert gt[0] <= 100.0 and (gt[0] + arr.shape[1] * gt[1]) >= 140.0


def test_taper_feathers_depth_to_the_border(tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    pytest.importorskip("scipy")  # corner rounding and tapering use scipy.ndimage
    out = str(tmp_path / "taper.tif")
    poly = _square_wkt(0.0, 0.0, 60.0, 60.0)

    res = build_release_raster(
        [poly], _crs_wkt(), out, height_m=10.0, resolution_m=1.0, taper_width_m=10.0
    )
    arr, gt, _ = _read(out)

    centre = _value_at(arr, gt, 30.0, 30.0)  # deep interior (> taper from any border)
    near_edge = _value_at(arr, gt, 2.0, 30.0)  # ~2 m from the left border
    assert centre == pytest.approx(10.0)  # interior keeps full depth
    assert 0.0 < near_edge < 10.0  # feathered down near the border
    assert res.max_height_m == pytest.approx(10.0)
    # A tapered slab holds less material than a flat one of the same footprint.
    assert res.volume_m3 < 60 * 60 * 10.0


def test_corner_rounding_removes_the_sharp_corner(tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    pytest.importorskip("scipy")  # corner rounding and tapering use scipy.ndimage
    out_sharp = str(tmp_path / "sharp.tif")
    out_round = str(tmp_path / "round.tif")
    poly = _square_wkt(0.0, 0.0, 50.0, 50.0)

    build_release_raster([poly], _crs_wkt(), out_sharp, height_m=3.0, resolution_m=1.0)
    build_release_raster(
        [poly], _crs_wkt(), out_round, height_m=3.0, resolution_m=1.0, corner_radius_m=10.0
    )
    sharp, gt_s, _ = _read(out_sharp)
    rnd, gt_r, _ = _read(out_round)

    # A cell just inside the bottom-left corner is material when sharp, cut away when rounded.
    assert _value_at(sharp, gt_s, 1.5, 1.5) == pytest.approx(3.0)
    assert _value_at(rnd, gt_r, 1.5, 1.5) == pytest.approx(0.0)
    # The interior is untouched by rounding.
    assert _value_at(rnd, gt_r, 25.0, 25.0) == pytest.approx(3.0)


def test_multiple_polygons_are_all_rasterized(tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    out = str(tmp_path / "multi.tif")
    a = _square_wkt(0.0, 0.0, 20.0, 20.0)
    b = _square_wkt(100.0, 100.0, 120.0, 120.0)

    build_release_raster([a, b], _crs_wkt(), out, height_m=2.0, resolution_m=1.0)
    arr, gt, _ = _read(out)

    assert _value_at(arr, gt, 10.0, 10.0) == pytest.approx(2.0)  # inside A
    assert _value_at(arr, gt, 110.0, 110.0) == pytest.approx(2.0)  # inside B
    assert _value_at(arr, gt, 60.0, 60.0) == pytest.approx(0.0)  # the gap between them


def test_invalid_inputs_raise() -> None:
    pytest.importorskip("osgeo")
    poly = _square_wkt(0.0, 0.0, 10.0, 10.0)
    with pytest.raises(ReleaseRasterError, match="no polygons"):
        build_release_raster([], _crs_wkt(), "x.tif", height_m=1.0, resolution_m=1.0)
    with pytest.raises(ReleaseRasterError, match="height must be positive"):
        build_release_raster([poly], _crs_wkt(), "x.tif", height_m=0.0, resolution_m=1.0)
    with pytest.raises(ReleaseRasterError, match="resolution must be positive"):
        build_release_raster([poly], _crs_wkt(), "x.tif", height_m=1.0, resolution_m=0.0)


def test_corner_radius_larger_than_polygon_is_reported(tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    pytest.importorskip("scipy")  # corner rounding and tapering use scipy.ndimage
    out = str(tmp_path / "gone.tif")
    poly = _square_wkt(0.0, 0.0, 10.0, 10.0)  # 10 m square
    with pytest.raises(ReleaseRasterError, match="removed all material"):
        build_release_raster(
            [poly], _crs_wkt(), out, height_m=1.0, resolution_m=1.0, corner_radius_m=20.0
        )


def test_dialog_and_page_wiring_construct(qgis_app) -> None:
    # Smoke test: the dialog and the preprocessing page import and build headlessly (catches wiring
    # and import regressions). No build is launched.
    from slab_qgis.panels.preprocessing_page import PreprocessingPage
    from slab_qgis.panels.release_raster_dialog import ReleaseRasterDialog

    dialog = ReleaseRasterDialog()
    assert dialog._height_spin.value() > 0
    assert dialog._res_spin.value() > 0
    assert dialog._corner_spin.value() == 0.0  # smoothing off by default
    assert dialog._taper_spin.value() == 0.0

    page = PreprocessingPage()
    assert page._release_button is not None
