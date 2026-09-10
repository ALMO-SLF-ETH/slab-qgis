# ALMO Sweep — QGIS plugin

A QGIS 4 / Qt 6 front-end for the [almo-sweep](../almo-sweep) simulation engine. Load a DEM
and release-zone layers, build a model's parameters from its manifest, run deterministic or
probabilistic mass-movement studies, and inspect the results — all from the map canvas.

> Status: **Phase 0 shell.** The plugin loads, adds a toolbar button and a dockable panel
> with the five workflow sections (Inputs · Parameters · Study · Run · Results). The pages
> are placeholders, filled in step by step.

## Layout

```
almo_sweep_qgis/            the plugin package (this folder is what QGIS loads)
  __init__.py               classFactory entry point
  metadata.txt              QGIS plugin metadata
  plugin.py                 AlmoSweepPlugin: menu/toolbar + dock lifecycle
  panels/main_panel.py      the dockable panel and its section navigator
  resources/icon.svg        toolbar icon
tests/                      headless smoke tests (run under QGIS's Python)
```

The plugin is the only part of the stack that imports `qgis`. All model-agnostic logic lives
in the `almo-sweep` engine, which the plugin imports to read manifests and drive runs.

## Requirements

- **QGIS 4.x / Qt 6** (developed against QGIS 4.2.2). Also runs on the QGIS 3.44 Qt 6 build.
- The **almo-sweep** engine installed into QGIS's Python (see below).

## Install for development

1. Install the engine into QGIS's own interpreter (one time; it is stdlib + numpy only):

   ```powershell
   & "C:\Program Files\QGIS 4.2.2\bin\python-qgis.bat" -m pip install -e "..\almo-sweep"
   ```

2. Make QGIS see the plugin by linking the `almo_sweep_qgis/` folder into your active
   profile's plugin directory. QGIS 4 uses the `QGIS4` profile root (QGIS 3 uses `QGIS3`);
   confirm via **QGIS ▸ Settings ▸ User Profiles ▸ Open Active Profile Folder**, then
   `python\plugins\`. For QGIS 4.2.2's default profile (run in an **elevated** PowerShell, as
   symlinks need admin):

   ```powershell
   $plugins = "$env:APPDATA\QGIS\QGIS4\profiles\default\python\plugins"
   New-Item -ItemType Directory -Force -Path $plugins | Out-Null
   New-Item -ItemType SymbolicLink -Path "$plugins\almo_sweep_qgis" `
            -Target "$PWD\almo_sweep_qgis"
   ```

3. Restart QGIS (or use *Plugin Reloader*), then enable **ALMO Sweep** in
   *Plugins ▸ Manage and Install Plugins* (tick *Show also experimental plugins* under
   *Settings* if it does not appear).

## Tests

The tests import `qgis`, so they run under QGIS's Python, which needs `pytest` once:

```powershell
$py = "C:\Program Files\QGIS 4.2.2\bin\python-qgis.bat"
& $py -m pip install pytest
& $py -m pytest
```

## Static checks

Linting and formatting need no QGIS and run via pre-commit:

```bash
pre-commit run --all-files
```

Type-checking runs under QGIS's Python (`qgis` ships only partial stubs), so it is not wired
into pre-commit; the engine core, by contrast, is fully `mypy --strict`.
