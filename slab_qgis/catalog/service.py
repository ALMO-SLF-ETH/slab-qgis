"""The plugin's model catalogue: registry, installs in the QGIS profile, and update checks.

Installed models live in ``<QGIS profile>/slab_qgis/models/<id>/<version>/``. Installing one
registers its ``model.toml`` in the model list (replacing an older installed version), so it is
selectable at once. The registry is the bundled ``resources/models.json``, refreshed from
:data:`REGISTRY_URL` when the network allows, so new models can be offered without a plugin
release.

The update check compares, at most once a day, the installed models (and a separately installed
slab-sweep engine) with their latest releases. The plugin itself is updated by QGIS's plugin
manager from the plugin repository, so it is not checked here.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from qgis.core import QgsApplication, QgsSettings

from slab_qgis import engine, settings
from slab_qgis.catalog import core
from slab_qgis.catalog.core import CatalogError, CatalogModel, Fetch, InstalledModel

REGISTRY_URL = "https://almo-slf-eth.github.io/slab-qgis/models.json"
ENGINE_REPOSITORY = "ALMO-SLF-ETH/slab-sweep"
_BUNDLED_REGISTRY = Path(__file__).resolve().parents[1] / "resources" / "models.json"
_REGISTRY_CACHE_KEY = "slab_qgis/catalog/registry"
_LAST_CHECK_KEY = "slab_qgis/catalog/last_update_check"
_CHECK_ENABLED_KEY = "slab_qgis/catalog/check_updates"
CHECK_INTERVAL_S = 24 * 3600


def models_root() -> Path:
    return Path(QgsApplication.qgisSettingsDirPath()) / "slab_qgis" / "models"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def registry(fetch: Fetch | None = None) -> list[CatalogModel]:
    """The offered models: refreshed from :data:`REGISTRY_URL` if ``fetch`` is given and works,
    else the last refreshed copy, else the one bundled with the plugin."""
    if fetch is not None:
        try:
            data = fetch(REGISTRY_URL)
            models = core.parse_registry(data)
            QgsSettings().setValue(_REGISTRY_CACHE_KEY, data.decode("utf-8"))
            return models
        except (CatalogError, UnicodeDecodeError):
            pass
    cached = str(QgsSettings().value(_REGISTRY_CACHE_KEY, "") or "")
    if cached:
        try:
            return core.parse_registry(cached)
        except CatalogError:
            pass
    return core.parse_registry(_BUNDLED_REGISTRY.read_bytes())


def installed() -> dict[str, InstalledModel]:
    """The newest installed version of each model, by model id."""
    newest: dict[str, InstalledModel] = {}
    for item in core.installed_models(models_root()):
        newest.setdefault(item.id, item)  # installed_models lists newest first
    return newest


# ---------------------------------------------------------------------------
# Install / remove
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InstallResult:
    model: CatalogModel
    version: str
    manifest: Path
    replaced: str = ""  # the version this install replaced, if any
    cleared_settings: tuple[str, ...] = ()  # runtime settings reset to the package's defaults


def install(model: CatalogModel, fetch: Fetch, download: Callable[[str, Path], Path]) -> InstallResult:
    """Download this platform's package of ``model``'s latest release, install and register it.

    An older installed version is unregistered and deleted once the new one is in place. Stored
    runtime settings that the package provides a default for (e.g. the binary's path) are reset,
    so the installed package's own files are used.
    """
    release = core.latest_release(model.repository, fetch)
    asset = core.pick_asset(model, release, core.platform_key())
    previous = installed().get(model.id)
    with tempfile.TemporaryDirectory() as tmp:
        archive = download(asset.url, Path(tmp) / asset.name)
        core.verify_sha256(archive, asset.sha256)
        manifest_path = core.install_archive(archive, models_root(), model.id, release.version)

    cleared: list[str] = []
    if engine.AVAILABLE:
        manifest = engine.load_manifest(manifest_path)  # fails loudly on an invalid manifest
        for var in settings.runtime_defaults(manifest):
            if settings.runtime_value(manifest.name, var):
                settings.set_runtime_value(manifest.name, var, "")
                cleared.append(var)

    settings.add_model(str(manifest_path))
    replaced = ""
    if previous is not None and previous.version != release.version:
        settings.remove_model(str(previous.manifest))
        core.remove_version(models_root(), model.id, previous.version)
        replaced = previous.version
    settings.set_last_model(str(manifest_path))
    return InstallResult(model, release.version, manifest_path, replaced, tuple(cleared))


def uninstall(model_id: str) -> None:
    """Unregister and delete every installed version of ``model_id``."""
    for item in core.installed_models(models_root()):
        if item.id == model_id:
            settings.remove_model(str(item.manifest))
            core.remove_version(models_root(), item.id, item.version)


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Update:
    kind: str  # "model" or "engine"
    id: str
    name: str
    current: str
    latest: str
    url: str


def updates_enabled() -> bool:
    return str(QgsSettings().value(_CHECK_ENABLED_KEY, "true")).lower() in ("true", "1")


def set_updates_enabled(enabled: bool) -> None:
    QgsSettings().setValue(_CHECK_ENABLED_KEY, "true" if enabled else "false")


def check_due(now: float | None = None) -> bool:
    """True if update checks are on and the last one is at least a day old."""
    if not updates_enabled():
        return False
    try:
        last = float(QgsSettings().value(_LAST_CHECK_KEY, 0) or 0)
    except (TypeError, ValueError):
        last = 0.0
    return (now if now is not None else time.time()) - last >= CHECK_INTERVAL_S


def check_updates(fetch: Fetch, now: float | None = None) -> list[Update]:
    """Installed models and a separately installed engine with a newer release.

    Records the check time; a model whose release cannot be read is skipped, not reported.
    """
    found: list[Update] = []
    have = installed()
    for model in registry(fetch):
        current = have.get(model.id)
        if current is None:
            continue
        try:
            release = core.latest_release(model.repository, fetch)
        except CatalogError:
            continue
        if core.is_newer(release.version, current.version):
            found.append(
                Update("model", model.id, model.name, current.version, release.version, release.url)
            )
    if engine.SOURCE == "installed" and engine.VERSION:
        # A bundled engine is updated with the plugin; a separately installed one is not.
        try:
            release = core.latest_release(ENGINE_REPOSITORY, fetch)
            if core.is_newer(release.version, engine.VERSION):
                found.append(
                    Update(
                        "engine",
                        "slab-sweep",
                        "slab-sweep",
                        engine.VERSION,
                        release.version,
                        release.url,
                    )
                )
        except CatalogError:
            pass
    QgsSettings().setValue(_LAST_CHECK_KEY, now if now is not None else time.time())
    return found
