# Changelog

All notable changes to Slab-QGIS are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The first public release.

### Added

- A dockable panel with Pre-processing, Parameters, Run and Results tabs, for QGIS 3.44 and QGIS 4.
- A persistent model list: models are registered from their slab-sweep manifest (`model.toml`),
  each with its own runtime settings (e.g. `SLAB_PRO_BIN`), defaults from the manifest shown as
  placeholders.
- **Get models…**: install, update and remove the models the plugin offers (Slab-Pro) from their
  GitHub releases, with checksum verification; a daily background check reports newer releases in
  the message bar.
- Parameters built from the manifest: groups, labels, units, bounds and controls that appear only
  when they apply; DEM and release inputs from QGIS layers, with live release area and volume and
  optional DEM resampling.
- DEM download for a region from swissALTI3D, merged and clipped into one GeoTIFF.
- Single runs and parameter sweeps (grid, Latin hypercube, Monte Carlo), run in parallel in the
  background with retries, progress and a time-remaining estimate; Stop ends the model process.
- Results loaded onto the map (per-band layers, frame series in the temporal controller) and, for
  sweeps, weighted hazard maps: impact probability, mean, envelope and percentile.
- Provenance for every run: versions of the plugin, engine and model, and input digests.
- The slab-sweep engine is bundled in the plugin zip (pinned in `engine.toml`, updated by pull
  request when slab-sweep releases); a separately installed slab-sweep takes precedence.
- Releases from a tag: tested zip, GitHub release, and a plugin repository on GitHub Pages
  (`https://almo-slf-eth.github.io/slab-qgis/plugins.xml`).
- An intro animation when the panel is opened: grains rain down into the word Slab-QGIS (a
  Slab-Pro simulation played backwards); `scripts/intro_animation` rebuilds it.
