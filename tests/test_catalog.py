"""Tests for the model catalogue: registry, releases, safe installs, and the update check."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from qgis.core import QgsApplication
from slab_qgis import settings
from slab_qgis.catalog import core, service
from slab_qgis.catalog.core import CatalogError

_MANIFEST = """
[model]
name = "toy_pkg"
version = "{version}"
schema = "1.2"
[invocation]
command = ["{{python}}", "{{model_dir}}/run.py"]
[runtime.TOY_PKG_BIN]
kind = "file"
default = "{{model_dir}}/bin/toy{{exe}}"
"""

_REGISTRY = {
    "schema": 1,
    "models": [
        {
            "id": "toy",
            "name": "Toy",
            "repository": "example/toy",
            "assets": {"testos-x86_64": "toy-{version}-testos.zip"},
        }
    ],
}


def _package_zip(version: str, *, manifest: bool = True, extra: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        top = f"toy-{version}-testos"
        if manifest:
            zf.writestr(f"{top}/model.toml", _MANIFEST.format(version=version))
        zf.writestr(f"{top}/bin/toy", "binary")
        for name, text in (extra or {}).items():
            zf.writestr(name, text)
    return buf.getvalue()


class FakeGitHub:
    """Serves a registry, one latest release per repository, and the release files."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.latest: dict[str, dict] = {}
        self.registry: bytes | None = json.dumps(_REGISTRY).encode()

    def publish(self, repository: str, version: str, files: dict[str, bytes]) -> None:
        assets = []
        for name, data in files.items():
            url = f"https://example.invalid/{repository}/{version}/{name}"
            self.files[url] = data
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            assets.append({"name": name, "browser_download_url": url, "digest": digest})
        self.latest[repository] = {
            "tag_name": f"v{version}",
            "html_url": f"https://example.invalid/{repository}/v{version}",
            "assets": assets,
        }

    def fetch(self, url: str) -> bytes:
        if url == service.REGISTRY_URL:
            if self.registry is None:
                raise CatalogError("offline")
            return self.registry
        for repository, release in self.latest.items():
            if url == f"{core.GITHUB_API}/repos/{repository}/releases/latest":
                return json.dumps(release).encode()
        if url in self.files:
            return self.files[url]
        raise CatalogError(f"404 {url}")

    def download(self, url: str, target: Path) -> Path:
        target.write_bytes(self.fetch(url))
        return target


@pytest.fixture
def github(qgis_app: QgsApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    monkeypatch.setattr(service, "models_root", lambda: tmp_path / "models")
    monkeypatch.setattr(core, "platform_key", lambda *a: "testos-x86_64")
    for path in settings.registered_models():
        settings.remove_model(path)
    from qgis.core import QgsSettings

    QgsSettings().remove("slab_qgis/catalog")
    yield FakeGitHub()
    for path in settings.registered_models():
        settings.remove_model(path)
    QgsSettings().remove("slab_qgis/catalog")
    service.set_updates_enabled(False)  # as conftest set it: no real network checks in tests


# --- pure helpers ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("system", "machine", "key"),
    [
        ("win32", "AMD64", "windows-x86_64"),
        ("linux", "x86_64", "linux-x86_64"),
        ("darwin", "arm64", "macos-arm64"),
        ("linux", "aarch64", "linux-arm64"),
    ],
)
def test_platform_key(system: str, machine: str, key: str) -> None:
    assert core.platform_key(system, machine) == key


def test_version_ordering() -> None:
    assert core.is_newer("0.2.0", "0.1.9")
    assert core.is_newer("1.0", "0.9.9")
    assert core.is_newer("1.0.0", "1.0.0-rc1")  # a final release beats its pre-release
    assert not core.is_newer("0.1.0", "0.1.0")
    assert core.is_newer("0.1.0", "")  # anything beats "not installed"


def test_bundled_registry_parses() -> None:
    models = core.parse_registry(service._BUNDLED_REGISTRY.read_bytes())
    slab_pro = next(m for m in models if m.id == "slab-pro")
    assert slab_pro.repository == "ALMO-SLF-ETH/Slab-Pro"
    assert slab_pro.asset_name("windows-x86_64", "0.2.0") == "slab-pro-0.2.0-windows-x86_64.zip"


def test_unsafe_package_paths_are_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    archive.write_bytes(_package_zip("1.0.0", extra={"../escape.txt": "x"}))
    with pytest.raises(CatalogError, match="unsafe path"):
        core.install_archive(archive, tmp_path / "models", "toy", "1.0.0")
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "models" / "toy" / "1.0.0").exists()


def test_tar_gz_package_installs(tmp_path: Path) -> None:
    archive = tmp_path / "toy-1.0.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        data = _MANIFEST.format(version="1.0.0").encode()
        info = tarfile.TarInfo("toy-1.0.0/model.toml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    manifest = core.install_archive(archive, tmp_path / "models", "toy", "1.0.0")
    assert manifest == tmp_path / "models" / "toy" / "1.0.0" / "toy-1.0.0" / "model.toml"


def test_package_without_manifest_is_rejected_and_leaves_nothing(tmp_path: Path) -> None:
    archive = tmp_path / "toy.zip"
    archive.write_bytes(_package_zip("1.0.0", manifest=False))
    with pytest.raises(CatalogError, match="no model.toml"):
        core.install_archive(archive, tmp_path / "models", "toy", "1.0.0")
    assert list((tmp_path / "models" / "toy").iterdir()) == []


# --- service: install, update, check --------------------------------------------------------
def test_install_registers_the_model_and_uses_package_defaults(github: FakeGitHub) -> None:
    github.publish("example/toy", "1.0.0", {"toy-1.0.0-testos.zip": _package_zip("1.0.0")})
    settings.set_runtime_value("toy_pkg", "TOY_PKG_BIN", "/old/dev/build/toy")

    (toy,) = service.registry(github.fetch)
    result = service.install(toy, github.fetch, github.download)

    assert result.version == "1.0.0"
    assert result.manifest.is_file()
    assert settings.registered_models() == [str(result.manifest)]
    assert settings.last_model() == str(result.manifest)
    # the stale path would have overridden the packaged binary, so it is reset
    assert result.cleared_settings == ("TOY_PKG_BIN",)
    assert settings.runtime_value("toy_pkg", "TOY_PKG_BIN") == ""
    assert service.installed()["toy"].version == "1.0.0"


def test_update_replaces_the_old_version(github: FakeGitHub) -> None:
    github.publish("example/toy", "1.0.0", {"toy-1.0.0-testos.zip": _package_zip("1.0.0")})
    (toy,) = service.registry(github.fetch)
    old = service.install(toy, github.fetch, github.download)

    github.publish("example/toy", "1.1.0", {"toy-1.1.0-testos.zip": _package_zip("1.1.0")})
    (update,) = service.check_updates(github.fetch)
    assert (update.kind, update.id, update.current, update.latest) == ("model", "toy", "1.0.0", "1.1.0")

    new = service.install(toy, github.fetch, github.download)
    assert new.replaced == "1.0.0"
    assert settings.registered_models() == [str(new.manifest)]
    assert not old.manifest.exists()
    assert service.check_updates(github.fetch) == []


def test_checksum_mismatch_aborts_the_install(github: FakeGitHub) -> None:
    github.publish("example/toy", "1.0.0", {"toy-1.0.0-testos.zip": _package_zip("1.0.0")})
    url = next(iter(github.files))
    github.files[url] = _package_zip("1.0.0", extra={"tampered": "x"})
    (toy,) = service.registry(github.fetch)
    with pytest.raises(CatalogError, match="checksum mismatch"):
        service.install(toy, github.fetch, github.download)
    assert settings.registered_models() == []


def test_missing_platform_package_is_reported(github: FakeGitHub) -> None:
    github.publish("example/toy", "1.0.0", {"toy-1.0.0-otheros.zip": b""})
    (toy,) = service.registry(github.fetch)
    with pytest.raises(CatalogError, match="has no file toy-1.0.0-testos.zip"):
        service.install(toy, github.fetch, github.download)


def test_registry_falls_back_to_cache_then_bundled(github: FakeGitHub) -> None:
    assert [m.id for m in service.registry(github.fetch)] == ["toy"]  # fetched and cached
    github.registry = None  # offline
    assert [m.id for m in service.registry(github.fetch)] == ["toy"]  # from the cache
    from qgis.core import QgsSettings

    QgsSettings().remove("slab_qgis/catalog/registry")
    assert "slab-pro" in [m.id for m in service.registry(github.fetch)]  # bundled


def test_check_is_due_once_a_day(github: FakeGitHub) -> None:
    assert service.check_due(now=1_000_000.0)
    service.check_updates(github.fetch, now=1_000_000.0)
    assert not service.check_due(now=1_000_000.0 + 3600)
    assert service.check_due(now=1_000_000.0 + service.CHECK_INTERVAL_S)
    service.set_updates_enabled(False)
    assert not service.check_due(now=2_000_000.0)


def test_models_dialog_lists_installs_and_removes(github: FakeGitHub) -> None:
    from slab_qgis.panels.models_dialog import ModelsDialog

    def run_now(_title, work, done):  # synchronous stand-in for the QgsTask runner
        try:
            result, error = work(), None
        except Exception as exc:
            result, error = None, exc
        done(result, error)

    github.publish("example/toy", "1.0.0", {"toy-1.0.0-testos.zip": _package_zip("1.0.0")})
    dialog = ModelsDialog(fetch=github.fetch, download=github.download, runner=run_now)
    selected: list[str] = []
    dialog.installed_callback = selected.append

    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 2).text() == "1.0.0"
    assert dialog.table.item(0, 3).text() == "available"
    assert dialog.install_button.text() == "Install"

    dialog.install_button.click()
    assert dialog.table.item(0, 1).text() == "1.0.0"
    assert dialog.table.item(0, 3).text().startswith("installed 1.0.0")
    assert selected and selected[-1].endswith("model.toml")
    assert dialog.remove_button.isEnabled()

    github.publish("example/toy", "1.1.0", {"toy-1.1.0-testos.zip": _package_zip("1.1.0")})
    dialog.refresh()
    assert dialog.table.item(0, 3).text() == "update available"
    assert dialog.install_button.text() == "Update"

    dialog.remove_button.click()
    assert dialog.table.item(0, 1).text() == "—"
    assert settings.registered_models() == []
