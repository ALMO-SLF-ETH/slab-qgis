# Contributing to slab-qgis

## Branches and pull requests

- `main` is the released plugin. It only changes through reviewed pull requests.
- `dev` collects the work in progress. Work on a topic branch off `dev` named `feature/…`,
  `bugfix/…`, `chore/…`, `test/…`, `ci/…` or `docs/…`, and open the pull request against `main`.
  Large changes can be split into **stacked** pull requests; say in each description which PR it
  depends on.
- Merge with **"Create a merge commit"**, not squash or rebase, so commit hashes stay the same on
  `dev`, the topic branches and `main` (the same rule as Slab-Pro and slab-sweep).
- Keep commits focused, with an imperative subject line ("Load failed runs", not "loaded…").
- The plugin never contains model-specific code: anything a model needs comes from its manifest.
  Engine logic belongs in [slab-sweep](https://github.com/ALMO-SLF-ETH/slab-sweep), not here.

## Development setup, tests and linting

See the [README](README.md#development): the tests run under QGIS's own Python, with a sibling
`slab-sweep` checkout; `pre-commit run --all-files` runs the linters as CI does.
