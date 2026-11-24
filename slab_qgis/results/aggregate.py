"""Reduce a sweep's per-run ``max_values`` rasters into weighted hazard maps.

Every run writes a ``max_values`` raster (band 1 ``h_max`` = per-cell max flow height over the run,
band 2 ``v_2D_max`` = max velocity). Because the DEM/release inputs are never swept, all runs share
one grid, so the rasters combine cell-by-cell with no resampling.

:func:`aggregate_max_values` streams them in horizontal stripes -- one stripe of rows is read from
every run, reduced, and written out before the next -- so peak memory is bounded by the stripe
size, not the run count or the grid size. In that single pass it produces:

* **impact probability** -- weighted fraction of runs whose ``h_max`` exceeds a depth threshold,
  ``Σ wᵢ·[hᵢ > τ] / Σ wᵢ``. The headline hazard product: P(reached).
* **weighted mean** ``h_max`` and ``v_2D_max`` -- ``Σ wᵢ·xᵢ / Σ wᵢ`` (expected intensity).
* **max-of-max envelope** -- cell-wise maximum ``h_max`` across runs (credible worst case).
* **weighted pᵗʰ percentile** of ``h_max`` -- the tail (default p90); optional, and the one output
  that must hold all runs' values for a stripe in memory, so the stripe is sized to a memory budget.
* **per-run scalar metrics** -- each run's overall max depth, max velocity and impacted-cell area,
  derived here (Slab-Pro reports no scalars) so the Results table has something to show.

The sample ``weight`` is used exactly as recorded, decoupled from how the sample was selected: for
Monte Carlo / Latin hypercube the weights are equal (so this reduces to a plain mean / frequency);
for a grid they are the quadrature weights, so weighting is essential. One code path, every scheme.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

#: Output file names for each aggregate layer (written into the aggregates directory).
LAYER_FILENAMES = {
    "impact_probability": "impact_probability.tif",
    "mean_max_height": "mean_max_height.tif",
    "mean_max_velocity": "mean_max_velocity.tif",
    "max_height_envelope": "max_height_envelope.tif",
    "percentile_max_height": "p{p:g}_max_height.tif",
}


class AggregateError(Exception):
    """Aggregation could not proceed (no inputs, mismatched grids, unreadable rasters)."""


@dataclass(frozen=True)
class AggregateInput:
    """One run contributing to the aggregate: its ``max_values`` raster and the sample weight."""

    run_id: str
    path: str
    weight: float = 1.0


@dataclass(frozen=True)
class AggregateConfig:
    """Knobs for the reduction."""

    threshold: float = 0.1  # impact depth τ (metres): a cell counts as reached when h_max > τ
    percentile: float | None = 90.0  # weighted percentile of h_max to map; None skips it
    h_band: int = 1  # 1-based band index of h_max in max_values
    v_band: int = 2  # 1-based band index of v_2D_max
    memory_budget_bytes: int = 512 * 1024 * 1024  # caps the stripe size for the percentile pass


@dataclass
class RunMetrics:
    """Scalar metrics derived for one run from its ``max_values`` raster."""

    run_id: str
    max_height: float = 0.0
    max_velocity: float = 0.0
    impacted_cells: int = 0
    impacted_area: float = 0.0  # map units squared, = impacted_cells * pixel area


@dataclass
class AggregateResult:
    """What :func:`aggregate_max_values` produced."""

    layers: dict[str, str] = field(default_factory=dict)  # layer name -> written raster path
    per_run: dict[str, RunMetrics] = field(default_factory=dict)
    n_runs: int = 0
    total_weight: float = 0.0


ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


def aggregate_max_values(
    inputs: list[AggregateInput],
    out_dir: str | Path,
    config: AggregateConfig | None = None,
    *,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> AggregateResult:
    """Reduce every run's ``max_values`` into the weighted hazard maps (see the module docstring).

    ``inputs`` are the successful runs; ``out_dir`` receives the aggregate GeoTIFFs. Raises
    :class:`AggregateError` if there is nothing to aggregate or the grids do not match. Honours a
    ``cancel`` callback between stripes and reports ``progress(fraction, message)``.
    """
    from osgeo import gdal

    gdal.UseExceptions()
    cfg = config or AggregateConfig()
    if not inputs:
        raise AggregateError("no successful runs to aggregate")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    grid = _read_grid(inputs[0].path, cfg)
    total_weight = float(sum(i.weight for i in inputs))
    if total_weight <= 0:
        raise AggregateError("the total sample weight is zero")

    want_pct = cfg.percentile is not None
    stripe_rows = _stripe_rows(grid, len(inputs), cfg) if want_pct else grid.ysize
    writers = _open_writers(out, grid, cfg)
    per_run = {i.run_id: RunMetrics(run_id=i.run_id) for i in inputs}
    px_area = abs(grid.gt[1] * grid.gt[5])

    for y0 in range(0, grid.ysize, stripe_rows):
        if cancel is not None and cancel():
            _close_writers(writers)
            raise AggregateError("cancelled")
        rows = min(stripe_rows, grid.ysize - y0)
        _reduce_stripe(inputs, grid, cfg, y0, rows, total_weight, writers, per_run, px_area)
        if progress is not None:
            progress(
                min(1.0, (y0 + rows) / grid.ysize), f"Aggregating rows {y0 + rows}/{grid.ysize}"
            )

    layers = _close_writers(writers)
    return AggregateResult(
        layers=layers, per_run=per_run, n_runs=len(inputs), total_weight=total_weight
    )


# --- grid & I/O helpers ---------------------------------------------------------------
@dataclass(frozen=True)
class _Grid:
    xsize: int
    ysize: int
    gt: tuple[float, ...]
    proj: str
    h_nodata: float | None
    v_nodata: float | None


def _read_grid(path: str, cfg: AggregateConfig) -> _Grid:
    from osgeo import gdal

    ds = gdal.Open(path)
    if ds is None:
        raise AggregateError(f"cannot open {path}")
    if ds.RasterCount < max(cfg.h_band, cfg.v_band):
        raise AggregateError(f"{path} has {ds.RasterCount} band(s); expected h and v")
    return _Grid(
        xsize=ds.RasterXSize,
        ysize=ds.RasterYSize,
        gt=tuple(ds.GetGeoTransform()),
        proj=ds.GetProjection(),
        h_nodata=ds.GetRasterBand(cfg.h_band).GetNoDataValue(),
        v_nodata=ds.GetRasterBand(cfg.v_band).GetNoDataValue(),
    )


def _stripe_rows(grid: _Grid, n_runs: int, cfg: AggregateConfig) -> int:
    """Rows per stripe so the percentile buffer (~4 copies of n_runs by stripe) fits the budget."""
    per_row = max(1, n_runs * grid.xsize * 8 * 4)  # float64, ~4 working copies
    return max(1, min(grid.ysize, cfg.memory_budget_bytes // per_row))


def _reduce_stripe(
    inputs: list[AggregateInput],
    grid: _Grid,
    cfg: AggregateConfig,
    y0: int,
    rows: int,
    total_weight: float,
    writers: dict[str, Any],
    per_run: dict[str, RunMetrics],
    px_area: float,
) -> None:
    from osgeo import gdal

    shape = (rows, grid.xsize)
    num_impact = np.zeros(shape, dtype=np.float64)
    sum_h = np.zeros(shape, dtype=np.float64)
    sum_v = np.zeros(shape, dtype=np.float64)
    env_h = np.zeros(shape, dtype=np.float64)
    want_pct = cfg.percentile is not None
    h_stack = np.empty((len(inputs), *shape), dtype=np.float64) if want_pct else None
    weights = np.array([i.weight for i in inputs], dtype=np.float64)

    for idx, inp in enumerate(inputs):
        ds = gdal.Open(inp.path)
        if ds is None or ds.RasterXSize != grid.xsize or ds.RasterYSize != grid.ysize:
            raise AggregateError(f"{inp.path} does not match the grid of {inputs[0].path}")
        h = ds.GetRasterBand(cfg.h_band).ReadAsArray(0, y0, grid.xsize, rows).astype(np.float64)
        v = ds.GetRasterBand(cfg.v_band).ReadAsArray(0, y0, grid.xsize, rows).astype(np.float64)
        h = _clean(h, grid.h_nodata)
        v = _clean(v, grid.v_nodata)
        w = inp.weight

        impacted = h > cfg.threshold
        num_impact += w * impacted
        sum_h += w * h
        sum_v += w * v
        np.maximum(env_h, h, out=env_h)
        if h_stack is not None:
            h_stack[idx] = h

        m = per_run[inp.run_id]
        m.max_height = max(m.max_height, float(h.max(initial=0.0)))
        m.max_velocity = max(m.max_velocity, float(v.max(initial=0.0)))
        cells = int(impacted.sum())
        m.impacted_cells += cells
        m.impacted_area += cells * px_area

    writers["impact_probability"].write(y0, num_impact / total_weight)
    writers["mean_max_height"].write(y0, sum_h / total_weight)
    writers["mean_max_velocity"].write(y0, sum_v / total_weight)
    writers["max_height_envelope"].write(y0, env_h)
    if h_stack is not None and "percentile_max_height" in writers:
        writers["percentile_max_height"].write(
            y0, _weighted_percentile(h_stack, weights, float(cfg.percentile), total_weight)
        )


def _clean(a: np.ndarray, nodata: float | None) -> np.ndarray:
    """Nodata and negatives mean 'dry' -> 0, so a cell never reached does not skew the maths."""
    if nodata is not None:
        a = np.where(a == nodata, 0.0, a)
    return np.where(a < 0.0, 0.0, a)


def _weighted_percentile(
    stack: np.ndarray, weights: np.ndarray, pct: float, total_weight: float
) -> np.ndarray:
    """Per-cell weighted ``pct`` percentile of ``stack`` (runs along axis 0)."""
    order = np.argsort(stack, axis=0)
    values = np.take_along_axis(stack, order, axis=0)
    w_sorted = weights[order]
    cum = np.cumsum(w_sorted, axis=0)
    target = (pct / 100.0) * total_weight
    reached = cum >= target
    idx = np.argmax(reached, axis=0)  # first run index whose cumulative weight meets the target
    return np.take_along_axis(values, idx[np.newaxis], axis=0)[0]


class _BandWriter:
    """Writes successive row-stripes into a single-band Float32 GeoTIFF."""

    def __init__(self, path: Path, grid: _Grid) -> None:
        from osgeo import gdal

        driver = gdal.GetDriverByName("GTiff")
        self._ds = driver.Create(
            str(path), grid.xsize, grid.ysize, 1, gdal.GDT_Float32, ["COMPRESS=DEFLATE"]
        )
        self._ds.SetGeoTransform(grid.gt)
        if grid.proj:
            self._ds.SetProjection(grid.proj)
        self.path = str(path)

    def write(self, y0: int, data: np.ndarray) -> None:
        self._ds.GetRasterBand(1).WriteArray(data.astype(np.float32), 0, y0)

    def close(self) -> None:
        self._ds.FlushCache()
        self._ds = None


def _open_writers(out: Path, grid: _Grid, cfg: AggregateConfig) -> dict[str, Any]:
    writers: dict[str, Any] = {}
    for name, template in LAYER_FILENAMES.items():
        if name == "percentile_max_height":
            if cfg.percentile is None:
                continue
            fname = template.format(p=cfg.percentile)
        else:
            fname = template
        writers[name] = _BandWriter(out / fname, grid)
    return writers


def _close_writers(writers: dict[str, Any]) -> dict[str, str]:
    paths = {}
    for name, writer in writers.items():
        paths[name] = writer.path
        writer.close()
    return paths


__all__ = [
    "AggregateConfig",
    "AggregateError",
    "AggregateInput",
    "AggregateResult",
    "RunMetrics",
    "aggregate_max_values",
]
