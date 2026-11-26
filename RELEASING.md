# Releasing Slab-QGIS

A release is a tag `vX.Y.Z` on `main`. The [release workflow](.github/workflows/release.yml) then
builds the plugin zip with the bundled engine, tests it, publishes a GitHub release, and updates
the plugin repository on GitHub Pages, from which QGIS installs and updates the plugin:

```
slab-sweep tag ──> bump PR (engine.toml) ──> dev ──> PR into main ──> tag vX.Y.Z ──> GitHub release
                                                                                  └─> plugins.xml + models.json on Pages
```

Models (Slab-Pro) are released from their own repositories; the plugin picks their releases up by
itself (**Get models…**), so they need no plugin release.

## One-time setup

- **Pages**: *Settings ▸ Pages ▸ Source* is **GitHub Actions**.
- **`SLAB_QGIS_BOT_TOKEN`** (secret in this repository): a token with *Contents* and *Pull
  requests* write access to slab-qgis. The bump workflow opens its pull request with it, so CI runs
  on that pull request (pull requests opened with the default token start no workflows), and the
  ALMO-SLF-ETH organisation does not let the default token open pull requests at all.
- **`SLAB_QGIS_DISPATCH_TOKEN`** (secret in slab-sweep): a token with *Contents* write access to
  slab-qgis. slab-sweep's release workflow uses it to start the bump right away; without it, the
  daily bump run picks the release up.

Tokens expire: when one does, the bump fails with a 401 or 403; replace the secret.

## 1. Update the bundled engine

[`engine.toml`](engine.toml) pins the slab-sweep release a plugin zip bundles (version, wheel
name and SHA-256). It is updated by pull request, not by hand:

- When slab-sweep publishes a release, the [bump workflow](.github/workflows/bump-engine.yml)
  opens a pull request **"Bundle slab-sweep X.Y.Z"** from `bump/slab-sweep` into `dev`; CI tests
  the plugin with that engine. Merge it (merge commit) when green.
- The workflow also runs daily and can be started by hand (*Actions ▸ Bump bundled engine ▸ Run
  workflow*, optionally with a tag); it does nothing when the pin is current.
- Locally: `python scripts/bump_engine.py [--tag vX.Y.Z]` updates `engine.toml` the same way.

## 2. Prepare the release on `dev`

1. Set `version=X.Y.Z` in [`slab_qgis/metadata.txt`](slab_qgis/metadata.txt). While the plugin is
   experimental, keep `experimental=True`.
2. Move the *Unreleased* entries in [`CHANGELOG.md`](CHANGELOG.md) under `## [X.Y.Z] - YYYY-MM-DD`.
3. Check that `metadata.txt` has the real contact `email` (plugins.qgis.org requires it) and that
   `engine.toml` pins a release.
4. Open a pull request from `dev` into `main` and merge it with **Create a merge commit** when CI
   is green.

## 3. Tag

```bash
git fetch origin
git tag -a vX.Y.Z -m "Slab-QGIS X.Y.Z" origin/main
git push origin vX.Y.Z
```

The release workflow checks that the tag matches `metadata.txt` and that an engine is pinned,
builds `slab_qgis-X.Y.Z.zip`, smoke-tests it in QGIS LTR and stable, creates the GitHub release
with the zip, and publishes `plugins.xml` and `models.json` to
`https://almo-slf-eth.github.io/slab-qgis/`. If a job fails, fix the cause on `dev`, merge into
`main` again, delete the tag (`git push origin :vX.Y.Z`) and tag again.

## 4. Check

In a fresh QGIS profile, add the repository `https://almo-slf-eth.github.io/slab-qgis/plugins.xml`
(see the [README](README.md#install)), install Slab-QGIS, open the panel and install Slab-Pro with
**Get models…**. QGIS installations that already have the plugin offer the update in the plugin
manager.

## 5. The official QGIS plugin repository

Once the plugin is listed on [plugins.qgis.org](https://plugins.qgis.org/), upload each release's
zip there too (*My plugins ▸ Slab-QGIS ▸ Add version*), with an OSGeo account. The first upload is
reviewed by hand, which can take a few days.

## Offering a new model

Add the model to [`slab_qgis/resources/models.json`](slab_qgis/resources/models.json) (see
[docs/development.md](docs/development.md#the-model-registry)) and release the plugin: existing
installations read the updated list from Pages.
