# ALMO Sweep — QGIS plugin

A QGIS 4 / Qt 6 front-end for the [almo-sweep](../almo-sweep) simulation engine. Load a DEM
and release-zone layers, build a model's parameters from its manifest, run mass-movement
simulations, and load the results back onto the map — all from the QGIS canvas.

The plugin is **model-agnostic**: it drives any model that ships an ALMO *manifest*
(`model.toml`). This guide uses the [dampm](../dampm) model as the example.

## How the stack fits together

![ALMO stack — how the GUI, the engine and dampm fit together](docs/stack_architecture.svg)

Three repos, decoupled by a small **file contract** rather than code dependencies: this
**plugin** (the only part that imports `qgis`) drives the **[almo-sweep](../almo-sweep)** engine
(pure Python, model-agnostic), which runs any **model** that ships a `model.toml` manifest plus an
executable entry point. [dampm](../dampm) provides both via its `dampm_interface/` adapter, so the
GUI and the engine contain no dampm-specific code.

---

## Requirements

- **QGIS 4.x / Qt 6** (developed against QGIS 4.2.2; also runs on the QGIS 3.44 Qt 6 build).
- The **almo-sweep** engine (pure Python, stdlib + numpy).
- A **model** to run: its ALMO adapter (e.g. `dampm_interface`) plus its built binary. See
  the model's own README (e.g. [dampm](../dampm/README.md)) for building the binary.

The engine and the model adapter are Python packages that must be installed **into QGIS's own
Python interpreter** (not a system/conda Python). The steps below do this in order.

---

## Install

Follow these steps top to bottom.

### 1. Locate QGIS's Python interpreter

Open **QGIS ▸ Plugins ▸ Python Console** and run:

```python
import sys; print(sys.executable)
```

You'll use this interpreter for the `pip` steps below. Typical locations (yours may differ):

| OS | QGIS Python for `pip` |
|----|-----------------------|
| **Windows** | `C:\Program Files\QGIS 4.2.2\bin\python-qgis.bat` |
| **macOS**   | `/Applications/QGIS-<version>.app/Contents/MacOS/python` |
| **Linux**   | the system `python3` QGIS was built against (often just `python3`) |

Below, `<qgis-python>` means that interpreter.

### 2. Install the engine into QGIS's Python

```bash
<qgis-python> -m pip install -e /path/to/ALMO/Simulations/almo-sweep
```

### 3. Install the model adapter into QGIS's Python

For dampm:

```bash
<qgis-python> -m pip install -e /path/to/ALMO/Simulations/dampm/interface
```

You also need dampm's compiled binary — build it per [dampm/README.md](../dampm/README.md).
You will point the plugin at that binary in step 7 (no environment variables needed).

> **macOS note:** if pip can't write into the app bundle, prefix the command with `sudo`.

### 4. Link the plugin into your QGIS profile

Find your profile's plugin folder via **QGIS ▸ Settings ▸ User Profiles ▸ Open Active Profile
Folder**, then go into `python/plugins/`. Typical defaults:

| OS | Plugins folder |
|----|----------------|
| **Windows** | `%APPDATA%\QGIS\QGIS4\profiles\default\python\plugins` |
| **macOS**   | `~/Library/Application Support/QGIS/QGIS4/profiles/default/python/plugins` |
| **Linux**   | `~/.local/share/QGIS/QGIS4/profiles/default/python/plugins` |

Link the `almo_sweep_qgis/` folder into it (a link means code changes are picked up on
reload — no re-copying):

**Windows** (PowerShell; junctions need no admin rights):
```powershell
$plugins = "$env:APPDATA\QGIS\QGIS4\profiles\default\python\plugins"
New-Item -ItemType Directory -Force -Path $plugins | Out-Null
New-Item -ItemType Junction -Path "$plugins\almo_sweep_qgis" `
         -Target "C:\path\to\ALMO\Simulations\QGIS_Simulation_GUI\almo_sweep_qgis"
```

**macOS / Linux**:
```bash
plugins="$HOME/Library/Application Support/QGIS/QGIS4/profiles/default/python/plugins"  # macOS
# plugins="$HOME/.local/share/QGIS/QGIS4/profiles/default/python/plugins"               # Linux
mkdir -p "$plugins"
ln -s /path/to/ALMO/Simulations/QGIS_Simulation_GUI/almo_sweep_qgis "$plugins/almo_sweep_qgis"
```

*(If you can't create a link, copying the `almo_sweep_qgis/` folder in also works; you'll
re-copy after each code change.)*

### 5. Enable the plugin

Restart QGIS. Open **Plugins ▸ Manage and Install Plugins**, tick **Show also experimental
plugins** under *Settings*, then enable **ALMO Sweep** under *Installed*. A green-slope
toolbar button appears; click it to show the **ALMO Sweep** dock panel.

---

## Use

### Download a DEM for a region (optional)

If you don't already have a DEM, the **Pre-processing** tab has **Download DEM for region…**. Pick a
provider (currently **swissALTI3D** from swisstopo, 0.5 m or 2 m source tiles), a **polygon layer**
outlining your region of interest, and an output file; the covering tiles are fetched and **merged
into one clipped GeoTIFF** (in the background), then added to the map **as a hillshade** ready to
use as the DEM input. You can optionally **resample** the output to a different resolution with a
chosen method (bilinear — a 2×2 kernel — by default; "average" is better for downsampling). Tiles
are listed live from the provider's STAC API, so the current data is always used. Adding another
provider is a single class in `almo_sweep_qgis/dem/providers.py` — it then appears in the dropdown
automatically. (The region polygon layer must be in a CRS with an EPSG code, e.g. EPSG:2056.)

### 6. Register and select a model

The panel header holds a **persistent list of models**. The first time, click **Add…** and
select the model's manifest, e.g. `dampm/interface/model.toml` — it's added to the list and
selected. On later launches it's already there: just pick it from the dropdown. **Remove**
drops the selected entry. The list (and the last model you had open) survive QGIS restarts, and
each model remembers its own runtime settings (below), so a model you've used before is ready
to run with its binary/DLL paths already set. The status line confirms the model, its inputs
and parameters.

### 7. Configure the model's runtime settings (once per machine)

Click **Model settings…** (next to the model selector) and fill in the paths the model needs.
These are stored per model and reused every time you select it. For dampm:

- **`DAMPM_BIN`** — the compiled dampm executable (required).
- **`DAMPM_DLL_DIR`** — **Windows only**: the folder with dampm's runtime DLLs
  (e.g. `C:\msys64\ucrt64\bin` for an MSYS2 build). **Leave empty on macOS/Linux.**

These are stored in QGIS settings and remembered across sessions — no environment variables,
no launching QGIS from a terminal.

### 8. Parameters (inputs + settings) and run

1. **Parameters** — one page holds everything about the run: the spatial inputs *and* the
   model settings, laid out under their manifest **group** headings and labelled with human
   names. Controls that only apply in certain cases appear automatically when relevant — e.g.
   the Voellmy coefficients show only when the friction law is Voellmy, and (for dampm) the
   **release-zone layer appears only when the release shape is `POLYGON`**; `RECTANGLE` and
   `ELLIPSE` releases are defined numerically instead. For dampm the DEM must be an ESRI ASCII
   `.asc` and the release a polygon shapefile; load them into QGIS first.

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
2. **Run** — set a **project name** and output folder, choose which outputs to keep, and click
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

   **Output layout** — inside the chosen output folder a **project folder** (named by the project
   name) holds everything:

   ```
   <output folder>/<project>/
     inputs/                     the DEM and release layers the runs use (see "Copy inputs")
     study.json                  (sweeps only) the scheme, seed and every run's params + weight
     <project>_000001/           one folder per run (6-digit, so > 100 000 runs are fine)
       runspec.json  simConfig.json  dampm_log.txt  result.json  provenance.json
       raw/                      the model's rasters / particles
     <project>_000002/  …
   ```

   - **Copy inputs** — when *Copy input layers into the project* is ticked (default), each input is
     copied into `inputs/` (with its sidecar files) so the project is self-contained; an input that
     already lives inside the project is left in place. Untick it to read inputs from where they are.
   - A **deterministic** study produces one run folder (`<project>_000001`); a **sweep** produces
     one per sample.
   - **Provenance** — each run's `provenance.json` records the versions (release + git
     commit/dirty/date) of the plugin, the almo-sweep engine and the dampm binary that produced it,
     so a result is always traceable to the code that made it.

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

### Study — the sweep method

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

### Results — inspect a sweep

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

---

## Development

### Layout

```
almo_sweep_qgis/            the plugin package (this folder is what QGIS loads)
  __init__.py               classFactory entry point
  metadata.txt              QGIS plugin metadata
  plugin.py                 AlmoSweepPlugin: menu/toolbar + dock lifecycle
  settings.py               per-model runtime settings (QgsSettings)
  state.py                  shared StudyState (manifest, inputs, params, sweep config)
  sweep_plan.py             plain-data sweep config -> engine distributions + strategy; run count
  panels/                   main_panel (tabbed), parameters_page (inputs + params + sweep),
                            run_page, run_pipeline, run_task, results_page, preprocessing_page,
                            settings_dialog
  results/                  study.py (read runs.json/study.json), aggregate.py (weighted hazard maps)
  resources/icon.svg        toolbar icon
tests/                      headless tests (run under QGIS's Python)
```

The plugin is the only part of the stack that imports `qgis`; all model-agnostic logic lives
in the `almo-sweep` engine.

### Tests

The tests import `qgis`, so they run under QGIS's Python (install `pytest` there once):

```bash
<qgis-python> -m pip install pytest
<qgis-python> -m pytest
```

### Static checks

Linting/formatting need no QGIS and run via pre-commit:

```bash
pre-commit run --all-files
```

Type-checking runs under QGIS's Python (`qgis` ships only partial stubs), so it is not wired
into pre-commit; the engine core, by contrast, is fully `mypy --strict`.

## License

Copyright © 2026 ALMO. This plugin is free software, licensed under the **GNU General Public
License v3.0** (GPL-3.0-only) — see [LICENSE](LICENSE).
