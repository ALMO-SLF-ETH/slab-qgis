# Slab-QGIS

![Grains rain down and settle into the word Slab-QGIS](slab_qgis/resources/intro.gif)

A QGIS plugin to design, run and inspect **mass-movement simulation studies** from the map:
pick a DEM and release areas, set a model's parameters, run a single simulation or a parameter
sweep, and load the results back onto the map, including probability maps for sweeps.

The plugin is **model-agnostic**: it drives any model that ships a
[slab-sweep](https://github.com/ALMO-SLF-ETH/slab-sweep) manifest (`model.toml`), through the
slab-sweep engine it bundles. [Slab-Pro](https://github.com/ALMO-SLF-ETH/Slab-Pro), a
depth-averaged material point method for geophysical mass flows, is installed from within the
plugin with one click.

## Features

- **Models in one click**: *Get models…* downloads a model for your platform, ready to run, and
  tells you when a newer release is out.
- **Parameters from the model's manifest**: grouped, labelled controls with units and bounds;
  controls that only apply in some cases (e.g. a friction law's coefficients) appear when needed.
- **Inputs from QGIS layers**: DEM and release layers from the map, with live release area and
  volume, optional DEM resampling, and a DEM download for Switzerland (swissALTI3D).
- **Studies**: sweep parameters over ranges with grid, Latin hypercube or Monte Carlo sampling,
  run in parallel, with retries and a time-remaining estimate.
- **Results on the map**: a run's rasters as styled layers, frame series in the temporal
  controller, and weighted hazard maps (impact probability, mean, envelope, percentiles) for
  sweeps.
- **Reproducible**: every run records the versions of the plugin, the engine and the model, and
  digests of its inputs.

## Requirements

QGIS 3.44 or newer, including QGIS 4. Nothing else: the engine is bundled, and models come as
self-contained packages (Slab-Pro: Windows x86_64, Linux x86_64 with glibc 2.35 or newer, and
macOS on Apple Silicon).

## Install

Until the plugin is in the official QGIS plugin list, add its repository to QGIS once:

1. In QGIS, open **Plugins ▸ Manage and Install Plugins ▸ Settings**, tick **Show also
   experimental plugins**, and under *Plugin Repositories* click **Add…**:
   - Name: `Slab-QGIS`
   - URL: `https://almo-slf-eth.github.io/slab-qgis/plugins.xml`
2. Under **All**, find **Slab-QGIS** and click **Install Plugin**.

QGIS then also offers new Slab-QGIS versions as updates. A release zip can also be installed with
**Install from ZIP**, from the [releases](https://github.com/ALMO-SLF-ETH/slab-qgis/releases).

## Quick start

1. Click the **Slab-QGIS** toolbar button to open the panel.
2. Click **Get models…**, select **Slab-Pro** and click **Install**. It is added to the model list
   and selected.
3. Load a DEM (and, for a polygon release, a release-area polygon layer) into QGIS, or download a
   DEM on the **Pre-processing** tab.
4. On **Parameters**, pick the input layers and set the parameters.
5. On **Run**, choose a project name and output folder and click **Run**. The results are added to
   the map when the run finishes.

The [user guide](docs/user-guide.md) covers every step, parameter sweeps and the Results page.

## Documentation

- [User guide](docs/user-guide.md): models, parameters, runs, sweeps, results, updates.
- [Development](docs/development.md): setting up from source, layout, tests, the plugin zip.
- [Contributing](CONTRIBUTING.md) and [releasing](RELEASING.md).
- [Changelog](CHANGELOG.md).
- For model authors: the manifest format,
  [slab-sweep's docs/manifest-spec.md](https://github.com/ALMO-SLF-ETH/slab-sweep/blob/main/docs/manifest-spec.md).

## License

Copyright © 2026 ALMO. This plugin is free software, licensed under the **GNU General Public
License v3.0** (GPL-3.0-only) — see [LICENSE](LICENSE).
