# Slab-QGIS user guide

How to set up and run mass-movement simulation studies with the Slab-QGIS plugin. For installing
the plugin, see the [README](../README.md#install).

1. [Open the panel](#open-the-panel)
2. [Get a model](#get-a-model)
3. [Model settings](#model-settings)
4. [Download a DEM (optional)](#download-a-dem-optional)
5. [Parameters](#parameters)
6. [Run](#run)
7. [Studies: the sweep method](#studies-the-sweep-method)
8. [Results: inspect a sweep](#results-inspect-a-sweep)
9. [Updates and preferences](#updates-and-preferences)

## Open the panel

Click the **Slab-QGIS** toolbar button (or **Plugins ▸ Slab-QGIS**) to show the dock panel. Each
time you open it, a short intro plays over the map: grains rain down into the word Slab-QGIS, a
Slab-Pro simulation played backwards. A click dismisses it; it can be turned off (see
[Updates and preferences](#updates-and-preferences)).

The panel has one tab per step: **Pre-processing**, **Parameters**, **Run** and **Results**. Its
header holds the model selector.

## Get a model

The plugin runs any model that ships a slab-sweep manifest (`model.toml`); this guide uses
[Slab-Pro](https://github.com/ALMO-SLF-ETH/Slab-Pro). There are two ways to add one to the
**model list** in the panel header:

- **Get models…** lists the models published for the plugin. **Install** downloads this platform's
  package of the latest release into your QGIS profile (`<profile>/slab_qgis/models/`), checks its
  checksum, adds it to the model list and selects it. A package carries everything the model needs
  (Slab-Pro's binary, its libraries and its adapter), so it runs without any further settings.
  **Update** replaces the installed version with a newer release; **Remove** deletes it.
- **Add…** registers a model from its manifest on disk, e.g. a source checkout's
  `Slab-Pro/interface/model.toml`. Such a model usually needs its paths set in
  [Model settings](#model-settings).

The list, and the model you had open last, survive QGIS restarts; **Remove** (next to **Add…**)
drops the selected entry from the list. The status line confirms the model, its inputs and
parameters.

## Model settings

**Model settings…** (next to the model selector) holds the host paths a model needs, stored per
model in QGIS's settings, so a model you used before is ready to run. A model installed with
**Get models…** fills them in itself; the fields then show the package's own values as greyed-out
placeholders, and you only enter something to override them.

For Slab-Pro registered from a source checkout:

- **`SLAB_PRO_BIN`** — the compiled Slab-Pro executable (required).
- **`SLAB_PRO_DLL_DIR`** — **Windows only**: the folder with Slab-Pro's runtime DLLs (e.g.
  `C:\msys64\ucrt64\bin` for an MSYS2 build). **Leave empty on macOS/Linux.**

No environment variables or terminal launch are needed.

## Download a DEM (optional)


If you don't already have a DEM, the **Pre-processing** tab has **Download DEM for region…**. Pick a
provider (currently **swissALTI3D** from swisstopo, 0.5 m or 2 m source tiles), a **polygon layer**
outlining your region of interest, and an output file; the covering tiles are fetched and **merged
into one clipped GeoTIFF** (in the background), then added to the map **as a hillshade** ready to
use as the DEM input. You can optionally **resample** the output to a different resolution with a
chosen method (bilinear — a 2×2 kernel — by default; "average" is better for downsampling). Tiles
are listed live from the provider's STAC API, so the current data is always used. Adding another
provider is a single class in `slab_qgis/dem/providers.py` — it then appears in the dropdown
automatically. (The region polygon layer must be in a CRS with an EPSG code, e.g. EPSG:2056.)


## Parameters

One page holds everything about the run: the spatial inputs *and* the
model settings, laid out under their manifest **group** headings and labelled with human
names. Controls that only apply in certain cases appear automatically when relevant — e.g.
the Voellmy coefficients show only when the friction law is Voellmy, and (for Slab-Pro) the
**release-zone layer appears only when the release shape is `POLYGON`**; `RECTANGLE` and
`ELLIPSE` releases are defined numerically instead. For Slab-Pro the DEM can be any GDAL raster (GeoTIFF,
ESRI ASCII `.asc`, …) and the release a polygon layer; load them into QGIS first.

- **Parameter sweep** — ticking **Enable parameter sweep** at the top reveals a **Sweep this
  parameter** option on each *currently-visible* sweepable numeric parameter. Tick one to give
  it a **range** and a **distribution** (Uniform or Normal) instead of a fixed value; the others
  keep their set value. Because the option lives on the control itself, only parameters that
  actually apply (per `visible_when`) are ever offered for sweeping. The sweep **method** (grid /
  Latin hypercube / Monte Carlo, and its steps/samples/seed) appears just above, on the same
  page, once the sweep is enabled.

- **DEM resolution** — under each raster picker the selected raster's native **resolution** is
  shown. Tick **Resample to** to coarsen/refine it to a chosen cell size with a chosen method
  (bilinear — a 2×2 kernel — by default; "average" is better for downsampling) and click
  **Apply**. The result is written to a new GeoTIFF (defaulting to the source name plus
  `_<res>m`, e.g. `dem_05m.tif` for 0.5 m, in the source's folder — you can change the path),
  added as a hillshade, and selected as the input. Resampling is skipped if the target equals
  the current resolution. If **Resample to** is ticked but you never press **Apply**, the
  resample is performed automatically when the simulation starts.
- **Release estimate** — in the release section, below the release selection, a live approximate
  **area** and **volume** for the current release is shown as you edit: the polygon's area for a
  `POLYGON` release, `Lx·Ly` / `π·rx·ry` for a `RECTANGLE` / `ELLIPSE`, and (for a `RASTER`
  release) the cell size × the released cells, with volume = area × depth (`h0`) or, for a
  raster, the summed per-cell depths × cell area. It refreshes automatically as you edit the
  release polygon — live while drawing/moving vertices and again on commit/save.

## Run

Set a **project name** and output folder, choose which outputs to keep, and click
**Run**. Everything for the project is written under one folder (see **Output layout** below).
The run executes as a background **pipeline** of steps — any armed DEM resamples first, then
(optionally) the inputs are copied into the project, then the simulation(s). The progress bar
and the line beneath it describe the **whole batch**: the
current phase (e.g. *Resampling 'dem' to 2 m* or *Running 'myproj_000001'*), elapsed time, an
estimated time remaining and finish clock time, and — once sweeps run many simulations — a
`step N/M` counter and a batch-wide finish estimate. The time remaining is estimated from the
**recent** pace, not the whole-run average: a simulation reports progress as simulated time,
which races ahead early and crawls later, so a plain average would start far too optimistic
and keep climbing — the recent-pace estimate tracks the slowdown instead. Early on, while that
pace is still ramping and no honest estimate exists yet, the remaining time is shown as a
coarse, deliberately-long **range** (e.g. *roughly 2 to 3 min left*) with no finish clock; once
the estimate has converged it switches to a sharp countdown and a stable *finish ~HH:MM*. If any chosen input layer isn't saved to
disk yet (a scratch/temporary or project-embedded layer), you're asked where to save it first,
since the model reads inputs from files. **Stop** cancels the running
step — it terminates the model process itself (not just the task wrapper), so a run stops
immediately rather than finishing in the background.


### Output layout

Inside the chosen output folder a **project folder** (named by the project
name) holds everything:

```
<output folder>/<project>/
  inputs/                     the DEM and release layers the runs use (see "Copy inputs")
  study.json                  (sweeps only) the scheme, seed and every run's params + weight
  <project>_000001/           one folder per run (6-digit, so > 100 000 runs are fine)
    runspec.json  simConfig.json  slab_pro_log.txt  result.json  provenance.json
    raw/                      the model's rasters / particles
  <project>_000002/  …
```

- **Copy inputs** — when *Copy input layers into the project* is ticked (default), each input is
  copied into `inputs/` (with its sidecar files) so the project is self-contained; an input that
  already lives inside the project is left in place. Untick it to read inputs from where they are.
- A **deterministic** study produces one run folder (`<project>_000001`); a **sweep** produces
  one per sample.
- **Provenance** — each run's `provenance.json` records the versions (release + git
  commit/dirty/date) of the plugin, the slab-sweep engine and the Slab-Pro binary that produced it,
  so a result is always traceable to the code that made it.


### Results on the map

On success of a **single** run, **all of its outputs are added under one layer group named after
the run**:
- **Multi-band rasters** (`final_grid`, `max_values`) are **unpacked into one single-band layer
  per band**, each in its own subgroup, rendered **single-band pseudocolor (Turbo)**.
- A **series of raster frames** (`grid_series`) is loaded as **temporal groups** — one per band
  (**flow height**, **velocity**). Each frame is a timestamp (spaced by the model's output
  interval); the colour upper bound per band comes from `max_values.tif` (the run maxima), so
  colours are consistent across frames. Open **View ▸ Panels ▸ Temporal Controller** in
  animation mode to step/play through them.

A **sweep** is not auto-loaded onto the map (it can be hundreds of runs): the status line
reports how many succeeded and where the outputs are, and inspection/aggregation is left to the
Results page.


## Studies: the sweep method


*Which* parameters vary (and over what range/distribution) is chosen on the **Parameters** page,
beside the values; the sweep **method** lives there too, revealed once the sweep is enabled:

- **Scheme** — how the combinations to simulate are chosen:
  - **Grid (full factorial)** — each varied parameter is cut into equal **steps** (linear or
    logarithmic) and *every* combination is run. The run count is the product of the steps, so it
    grows fast; a live summary shows the total and warns when it is large.
  - **Latin hypercube** — stratified, space-filling sampling; the run count is the **sample count**
    you set, independent of how many parameters vary.
  - **Monte Carlo** — random draws; again a fixed **sample count**. A **fixed seed** makes the draw
    reproducible.
- The per-parameter **distribution** (set on Parameters) is used only to **weight the results**
  afterwards (the hazard maps on the Results page — each run's weight is recorded in `study.json`
  and `runs.json`); it never changes *which* combinations are run. Parameters you don't vary stay at
  their Parameters-page values.
- **Study execution** (on the Run page, shown while a sweep is enabled) sets how many runs execute
  at once (**Parallel runs**, default the processor count) and the **Retries** per failed run.


## Results: inspect a sweep


The **Results** page reads a project's `runs.json` / `study.json` and shows:

- a **header** — scheme, seed, success/failure counts, the swept parameters and their
  distributions, total compute time, a ⚠ warning if any code component was git-dirty at run time
  (results then are not reproducible), and a failure summary;
- a **per-run table** — weight, the swept-parameter values, duration, error, and (after computing
  hazard maps) each run's derived max depth, max velocity and impacted area.

A sweep's temporal outputs are never bulk-loaded. Instead **Compute hazard maps** reduces every
successful run's `max_values` — **weighted by each run's recorded sample weight**, so one path is
correct for every scheme (equal weights for LHS/Monte Carlo; quadrature weights for a grid) — into
a few layers written to `<project>/aggregates/` and loaded styled:

- **impact probability** — weighted fraction of runs whose max depth exceeds the threshold `τ`
  (configurable, default 0.1 m): the headline P(reached) map;
- **weighted mean** max depth and max velocity (expected intensity);
- **max-of-max envelope** (credible worst case);
- an optional **weighted percentile** of max depth (default p90, the tail).

The reduction streams the rasters in row-stripes (bounded memory regardless of run count) off the
main thread.

## Updates and preferences

- **The plugin** is updated by QGIS's plugin manager (**Plugins ▸ Manage and Install Plugins**),
  which also tells you about new versions. Each plugin release bundles a tested slab-sweep engine.
- **Models** installed with **Get models…**: once a day the plugin checks for newer releases and
  shows a notice in QGIS's message bar with an **Open models…** button. Turn the check off with
  *Check for model updates once a day* in the **Get models…** dialog.
- **The intro animation** can be turned off in **Settings ▸ Options ▸ Advanced**: set
  `slab_qgis/intro/show` to `false`.
