"""Results inspection for a sweep: aggregate per-run rasters into hazard maps.

A sweep can be thousands of runs, so the Results layer never loads every run's (temporal) output.
Instead it reads each run's cheap per-cell ``max_values`` raster once and reduces them, with the
sample weights the sweep recorded, into a handful of aggregate hazard maps plus per-run scalar
metrics. See :mod:`slab_qgis.results.aggregate`.
"""
