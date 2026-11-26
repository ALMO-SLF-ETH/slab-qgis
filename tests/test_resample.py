"""Tests for the raster resampling helpers (naming + a real GDAL warp)."""

from __future__ import annotations

from pathlib import Path

import pytest
from slab_qgis.dem import resample


def test_res_suffix_matches_convention() -> None:
    assert resample.res_suffix(0.5) == "05m"
    assert resample.res_suffix(1.0) == "1m"
    assert resample.res_suffix(2.0) == "2m"
    assert resample.res_suffix(10.0) == "10m"


def test_default_resampled_path_appends_suffix(tmp_path: Path) -> None:
    src = str(tmp_path / "dem_blatten.tif")
    out = resample.default_resampled_path(src, 0.5)
    assert out.endswith("dem_blatten_05m.tif")
    assert str(tmp_path) in out  # same folder as the source


def test_resample_changes_resolution(tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    import numpy as np
    from osgeo import gdal

    src = str(tmp_path / "src.tif")
    ds = gdal.GetDriverByName("GTiff").Create(src, 8, 8, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([0, 4, 0, 0, 0, -4])  # 4 m cells
    ds.GetRasterBand(1).WriteArray(np.ones((8, 8), dtype="float32"))
    ds = None
    assert resample.raster_resolution(src) == (4.0, 4.0)

    dst = resample.default_resampled_path(src, 8.0)  # -> src_8m.tif
    out = resample.resample_raster(src, dst, 8.0, "average")
    assert out == dst
    assert resample.raster_resolution(out) == (8.0, 8.0)


def test_resample_rejects_nonpositive_resolution(tmp_path: Path) -> None:
    with pytest.raises(resample.ResampleError):
        resample.resample_raster(str(tmp_path / "a.tif"), str(tmp_path / "b.tif"), 0.0)
