"""Tests for the sweep hazard-map aggregator (reads/writes real GeoTIFFs via GDAL)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from slab_qgis.results.aggregate import (
    AggregateConfig,
    AggregateError,
    AggregateInput,
    aggregate_max_values,
)

GT = (0.0, 10.0, 0.0, 0.0, 0.0, -10.0)  # 10 m pixels -> pixel area 100


def _write_maxvals(path: Path, h: list[list[float]], v: list[list[float]]) -> str:
    from osgeo import gdal

    gdal.UseExceptions()
    ha, va = np.array(h, dtype=np.float32), np.array(v, dtype=np.float32)
    ds = gdal.GetDriverByName("GTiff").Create(
        str(path), ha.shape[1], ha.shape[0], 2, gdal.GDT_Float32
    )
    ds.SetGeoTransform(GT)
    for band, arr in ((1, ha), (2, va)):
        b = ds.GetRasterBand(band)
        b.SetNoDataValue(-9999.0)
        b.WriteArray(arr)
    ds.FlushCache()
    ds = None
    return str(path)


def _read(path: str) -> np.ndarray:
    from osgeo import gdal

    gdal.UseExceptions()
    ds = gdal.Open(path)  # keep the dataset alive until the read completes
    arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    ds = None
    return arr


@pytest.fixture
def two_runs(tmp_path: Path) -> list[AggregateInput]:
    # One row, three cells. Run A (w=1) and Run B (w=3); nodata (-9999) means dry.
    a = _write_maxvals(tmp_path / "a.tif", [[-9999.0, 1.0, 5.0]], [[-9999.0, 2.0, 4.0]])
    b = _write_maxvals(tmp_path / "b.tif", [[-9999.0, 0.0, 10.0]], [[-9999.0, 0.0, 8.0]])
    return [AggregateInput("A", a, 1.0), AggregateInput("B", b, 3.0)]


def test_weighted_maps(tmp_path: Path, two_runs: list[AggregateInput]) -> None:
    res = aggregate_max_values(two_runs, tmp_path / "agg", AggregateConfig(percentile=50.0))
    assert res.n_runs == 2 and res.total_weight == 4.0

    prob = _read(res.layers["impact_probability"])
    np.testing.assert_allclose(prob, [[0.0, 0.25, 1.0]])  # Σw[h>τ]/Σw

    mean_h = _read(res.layers["mean_max_height"])
    np.testing.assert_allclose(mean_h, [[0.0, 0.25, 8.75]])  # (1·5+3·10)/4 = 8.75 at cell 2

    mean_v = _read(res.layers["mean_max_velocity"])
    np.testing.assert_allclose(mean_v, [[0.0, 0.5, 7.0]])  # (1·4+3·8)/4 = 7.0 at cell 2

    env = _read(res.layers["max_height_envelope"])
    np.testing.assert_allclose(env, [[0.0, 1.0, 10.0]])

    p50 = _read(res.layers["percentile_max_height"])
    np.testing.assert_allclose(p50, [[0.0, 0.0, 10.0]])  # weight-median favours Run B (w=3)


def test_per_run_metrics(tmp_path: Path, two_runs: list[AggregateInput]) -> None:
    res = aggregate_max_values(two_runs, tmp_path / "agg", AggregateConfig(percentile=None))
    a, b = res.per_run["A"], res.per_run["B"]
    assert a.max_height == 5.0 and a.max_velocity == 4.0
    assert a.impacted_cells == 2 and a.impacted_area == 200.0  # 2 cells * 100 m2
    assert b.max_height == 10.0 and b.impacted_cells == 1 and b.impacted_area == 100.0


def test_stripes_give_the_same_result_as_one_pass(
    tmp_path: Path, two_runs: list[AggregateInput]
) -> None:
    # A tiny memory budget forces several stripes; the output must be identical to one pass.
    full = aggregate_max_values(two_runs, tmp_path / "full", AggregateConfig(percentile=50.0))
    tiny = aggregate_max_values(
        two_runs,
        tmp_path / "tiny",
        AggregateConfig(percentile=50.0, memory_budget_bytes=1),
    )
    for name in full.layers:
        np.testing.assert_allclose(_read(full.layers[name]), _read(tiny.layers[name]))


def test_skipping_percentile_omits_that_layer(
    tmp_path: Path, two_runs: list[AggregateInput]
) -> None:
    res = aggregate_max_values(two_runs, tmp_path / "agg", AggregateConfig(percentile=None))
    assert "percentile_max_height" not in res.layers
    assert "impact_probability" in res.layers


def test_no_inputs_raises(tmp_path: Path) -> None:
    with pytest.raises(AggregateError, match="no successful runs"):
        aggregate_max_values([], tmp_path / "agg")


def test_mismatched_grid_raises(tmp_path: Path, two_runs: list[AggregateInput]) -> None:
    odd = _write_maxvals(tmp_path / "odd.tif", [[1.0, 2.0]], [[1.0, 2.0]])  # 1x2, not 1x3
    with pytest.raises(AggregateError, match="does not match the grid"):
        aggregate_max_values([*two_runs, AggregateInput("C", odd, 1.0)], tmp_path / "agg")
