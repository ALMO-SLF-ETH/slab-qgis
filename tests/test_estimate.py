"""Tests for the release area/volume estimator (pure maths + a GDAL raster read)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from slab_qgis import estimate


def test_rectangle_area_and_volume() -> None:
    e = estimate.compute_release_estimate(
        {"release_type": "RECTANGLE", "rect_Lx": 4.0, "rect_Ly": 3.0, "h0": 2.0}
    )
    assert e.area_m2 == 12.0
    assert e.volume_m3 == 24.0


def test_ellipse_area_and_volume() -> None:
    e = estimate.compute_release_estimate(
        {"release_type": "ELLIPSE", "ell_rx": 2.0, "ell_ry": 1.0, "h0": 5.0}
    )
    assert math.isclose(e.area_m2, math.pi * 2.0)
    assert math.isclose(e.volume_m3, math.pi * 2.0 * 5.0)


def test_polygon_uses_caller_supplied_area() -> None:
    e = estimate.compute_release_estimate(
        {"release_type": "POLYGON", "h0": 3.0}, polygon_area_m2=10.0
    )
    assert e.area_m2 == 10.0
    assert e.volume_m3 == 30.0


def test_polygon_without_layer_is_unknown() -> None:
    e = estimate.compute_release_estimate({"release_type": "POLYGON", "h0": 3.0})
    assert e.area_m2 is None and e.volume_m3 is None


def test_missing_release_type_yields_nothing() -> None:
    e = estimate.compute_release_estimate({})
    assert e.area_m2 is None and e.volume_m3 is None


def test_raster_release_area_and_volume(tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    import numpy as np
    from osgeo import gdal

    # 4x4 raster, 2 m cells (cell area 4 m²). Three cells hold depth 5 (>= h_min=2), rest 0.
    path = str(tmp_path / "release.tif")
    arr = np.zeros((4, 4), dtype="float32")
    arr[0, 0] = arr[0, 1] = arr[1, 0] = 5.0
    ds = gdal.GetDriverByName("GTiff").Create(path, 4, 4, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([0, 2, 0, 0, 0, -2])
    ds.GetRasterBand(1).WriteArray(arr)
    ds = None

    e = estimate.compute_release_estimate(
        {"release_type": "RASTER", "raster_h_min": 2.0}, release_raster_path=path
    )
    assert e.area_m2 == 3 * 4.0  # three released cells x 4 m²
    assert e.volume_m3 == (5.0 * 3) * 4.0  # sum of depths x cell area


def test_raster_respects_minimum_depth(tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    import numpy as np
    from osgeo import gdal

    # Values 1 and 3 with h_min=2: only the 3-cell is released.
    path = str(tmp_path / "release.tif")
    arr = np.array([[1.0, 3.0], [0.0, 0.0]], dtype="float32")
    ds = gdal.GetDriverByName("GTiff").Create(path, 2, 2, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([0, 1, 0, 0, 0, -1])  # 1 m cells
    ds.GetRasterBand(1).WriteArray(arr)
    ds = None

    e = estimate.compute_release_estimate(
        {"release_type": "RASTER", "raster_h_min": 2.0}, release_raster_path=path
    )
    assert e.area_m2 == 1.0  # one cell of 1 m²
    assert e.volume_m3 == 3.0
