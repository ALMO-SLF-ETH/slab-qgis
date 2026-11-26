"""Model catalogue: which models exist, which release fits this machine, and installing one.

Pure standard library (no ``qgis`` import) so it is easy to test; network access is passed in as
a ``fetch(url) -> bytes`` callable (see :mod:`slab_qgis.catalog.net` for the QGIS one).

* The **registry** (``models.json``) lists the models the plugin offers: an id, a display name,
  the GitHub repository that publishes its releases, and the release-asset file name for each
  platform (``{version}`` is replaced by the release version).
* A **release** is read from the GitHub Releases API; :func:`pick_asset` chooses this platform's
  package.
* :func:`install_archive` unpacks a package into ``<models_root>/<id>/<version>/`` and returns the
  path of the ``model.toml`` it contains, which the plugin then registers like any other model.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import sys
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

Fetch = Callable[[str], bytes]

REGISTRY_SCHEMA = 1
GITHUB_API = "https://api.github.com"
MANIFEST_NAME = "model.toml"


class CatalogError(Exception):
    """A registry, release or package problem worth showing to the user."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CatalogModel:
    id: str
    name: str
    repository: str  # "owner/repo" publishing the releases
    assets: dict[str, str]  # platform key -> asset file name with {version}
    description: str = ""
    homepage: str = ""

    def asset_name(self, platform_key: str, version: str) -> str | None:
        pattern = self.assets.get(platform_key)
        return pattern.replace("{version}", version) if pattern else None


def parse_registry(data: bytes | str) -> list[CatalogModel]:
    try:
        raw = json.loads(data)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"models registry is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != REGISTRY_SCHEMA:
        raise CatalogError(f"models registry must be an object with schema {REGISTRY_SCHEMA}")
    models: list[CatalogModel] = []
    for i, item in enumerate(raw.get("models", [])):
        try:
            models.append(
                CatalogModel(
                    id=str(item["id"]),
                    name=str(item.get("name", item["id"])),
                    repository=str(item["repository"]),
                    assets={str(k): str(v) for k, v in dict(item["assets"]).items()},
                    description=str(item.get("description", "")),
                    homepage=str(item.get("homepage", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"models registry entry {i} is incomplete: {exc}") from exc
    return models


# ---------------------------------------------------------------------------
# Platform and versions
# ---------------------------------------------------------------------------
def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """This machine as a registry key: ``windows-x86_64``, ``linux-x86_64``, ``macos-arm64``."""
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine()).lower()
    os_name = (
        "windows"
        if system.startswith(("win", "cygwin"))
        else "macos"
        if system.startswith("darwin")
        else "linux"
    )
    arch = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return f"{os_name}-{arch}"


_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-.+]?(.*))?$")


def version_key(version: str) -> tuple[tuple[int, ...], int, str]:
    """Sort key for release versions: numeric parts, then a final release above a pre-release."""
    m = _VERSION.match(version.strip())
    if not m:
        return ((), 0, version)
    numbers = tuple(int(p) for p in m.group(1).split("."))
    numbers = numbers + (0,) * (3 - len(numbers)) if len(numbers) < 3 else numbers
    suffix = m.group(2) or ""
    return (numbers, 0 if suffix else 1, suffix)


def is_newer(candidate: str, current: str) -> bool:
    return bool(candidate) and (not current or version_key(candidate) > version_key(current))


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int = 0
    sha256: str = ""  # from GitHub's asset digest, when it provides one


@dataclass(frozen=True)
class Release:
    tag: str
    version: str
    url: str  # human page
    assets: tuple[ReleaseAsset, ...] = field(default_factory=tuple)

    def asset(self, name: str) -> ReleaseAsset | None:
        return next((a for a in self.assets if a.name == name), None)


def parse_release(data: bytes | str | dict[str, Any]) -> Release:
    raw = json.loads(data) if isinstance(data, (bytes, str)) else data
    try:
        tag = str(raw["tag_name"])
        assets = []
        for a in raw.get("assets", []):
            digest = str(a.get("digest") or "")
            assets.append(
                ReleaseAsset(
                    name=str(a["name"]),
                    url=str(a["browser_download_url"]),
                    size=int(a.get("size") or 0),
                    sha256=digest.removeprefix("sha256:") if digest.startswith("sha256:") else "",
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogError(f"unexpected release data: {exc}") from exc
    return Release(
        tag=tag,
        version=tag.removeprefix("v"),
        url=str(raw.get("html_url", "")),
        assets=tuple(assets),
    )


def latest_release(repository: str, fetch: Fetch) -> Release:
    """The repository's latest (non-draft, non-pre-release) release."""
    return parse_release(fetch(f"{GITHUB_API}/repos/{repository}/releases/latest"))


def pick_asset(model: CatalogModel, release: Release, platform_key: str) -> ReleaseAsset:
    name = model.asset_name(platform_key, release.version)
    if name is None:
        raise CatalogError(f"{model.name} has no package for this platform ({platform_key})")
    asset = release.asset(name)
    if asset is None:
        raise CatalogError(f"{model.name} {release.version} has no file {name}")
    return asset


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InstalledModel:
    id: str
    version: str
    manifest: Path


def verify_sha256(path: Path, expected: str) -> None:
    if not expected:
        return
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected.lower():
        raise CatalogError(f"{path.name}: checksum mismatch (got {digest}, expected {expected})")


def _safe_members(names: list[str]) -> None:
    for name in names:
        parts = PurePosixPath(name.replace("\\", "/")).parts
        if name.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]):
            raise CatalogError(f"package contains an unsafe path: {name}")


def _extract(archive: Path, dest: Path) -> None:
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            _safe_members(zf.namelist())
            zf.extractall(dest)
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()
            _safe_members([m.name for m in members])
            for m in members:
                if m.issym() or m.islnk():
                    target = PurePosixPath(m.name).parent / m.linkname
                    if m.linkname.startswith("/") or ".." in target.parts:
                        raise CatalogError(f"package contains an unsafe link: {m.name}")
            if hasattr(tarfile, "data_filter"):  # Python 3.12+ (and 3.11.4+)
                tf.extractall(dest, filter="tar")  # keeps exec bits, rejects escaping links
            else:  # pragma: no cover - older interpreters; members were checked above
                tf.extractall(dest)
    else:
        raise CatalogError(f"unsupported package format: {archive.name}")


def find_manifest(folder: Path) -> Path:
    """The package's ``model.toml``: at the top or inside the single top-level folder."""
    candidates = [
        folder / MANIFEST_NAME,
        *(p / MANIFEST_NAME for p in folder.iterdir() if p.is_dir()),
    ]
    found = [p for p in candidates if p.is_file()]
    if not found:
        raise CatalogError(
            f"the package contains no {MANIFEST_NAME}, so it cannot be registered as a model"
        )
    return found[0]


def install_archive(archive: Path, models_root: Path, model_id: str, version: str) -> Path:
    """Unpack ``archive`` as ``<models_root>/<model_id>/<version>/`` and return its manifest path.

    Unpacks into a staging folder first, so a failed install leaves any existing version intact.
    """
    target = models_root / model_id / version
    staging = models_root / model_id / f".{version}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        _extract(archive, staging)
        relative = find_manifest(staging).relative_to(staging)
        _restore_exec_bits(staging)
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target / relative


def _restore_exec_bits(folder: Path) -> None:
    """zip loses Unix permissions; make files in ``bin/`` folders executable (no-op on Windows)."""
    if sys.platform == "win32":
        return
    for path in folder.rglob("*"):
        if path.is_file() and path.parent.name == "bin":
            path.chmod(path.stat().st_mode | 0o755)


def installed_models(models_root: Path) -> list[InstalledModel]:
    """Every installed version under ``models_root`` that has a manifest, newest first per model."""
    found: list[InstalledModel] = []
    if not models_root.is_dir():
        return found
    for model_dir in sorted(p for p in models_root.iterdir() if p.is_dir()):
        versions = [p for p in model_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
        for vdir in sorted(versions, key=lambda p: version_key(p.name), reverse=True):
            try:
                found.append(InstalledModel(model_dir.name, vdir.name, find_manifest(vdir)))
            except CatalogError:
                continue
    return found


def remove_version(models_root: Path, model_id: str, version: str) -> None:
    shutil.rmtree(models_root / model_id / version, ignore_errors=True)
