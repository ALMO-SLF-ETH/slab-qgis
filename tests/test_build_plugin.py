"""Tests for scripts/build_plugin.py (the plugin zip with the bundled engine) and engine.model_env."""

from __future__ import annotations

import importlib.util
import os
import zipfile
from pathlib import Path
from types import ModuleType
from xml.etree import ElementTree

import pytest
from slab_qgis import engine

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_plugin.py"


@pytest.fixture(scope="module")
def build_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_plugin", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheel(path: Path, *, licence: bool = True, extra: dict[str, str] | None = None) -> Path:
    files = {
        "slab_sweep/__init__.py": '__version__ = "9.9.9"\n',
        "slab_sweep/contracts/__init__.py": "",
        "slab_sweep-9.9.9.dist-info/METADATA": "Name: slab-sweep\nVersion: 9.9.9\n",
        "other_pkg/__init__.py": "",  # not the engine: must not be bundled
    }
    if licence:
        files["slab_sweep-9.9.9.dist-info/licenses/LICENSE"] = "GPL-3.0"
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


def test_zip_bundles_engine_and_licences(build_plugin: ModuleType, tmp_path: Path) -> None:
    out = build_plugin.build(_wheel(tmp_path / "e.whl"), tmp_path / "dist")
    names = set(zipfile.ZipFile(out).namelist())
    assert "slab_qgis/LICENSE" in names
    assert "slab_qgis/_vendor/slab_sweep/__init__.py" in names
    assert "slab_qgis/_vendor/slab_sweep-9.9.9.dist-info/licenses/LICENSE" in names
    assert not any("other_pkg" in n for n in names)
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert all(n.startswith("slab_qgis/") for n in names)
    assert out.name.startswith("slab_qgis-") and out.suffix == ".zip"


def test_engine_without_licence_is_rejected(build_plugin: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(build_plugin.BuildError, match="engine licence"):
        build_plugin.build(_wheel(tmp_path / "e.whl", licence=False), tmp_path / "dist")


def test_unsafe_wheel_path_is_rejected(build_plugin: ModuleType, tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "e.whl", extra={"slab_sweep/../../evil.py": ""})
    with pytest.raises(build_plugin.BuildError, match="unsafe path"):
        build_plugin.build(wheel, tmp_path / "dist")


def test_empty_pin_needs_a_local_wheel(build_plugin: ModuleType) -> None:
    with pytest.raises(build_plugin.BuildError, match="pins no slab-sweep release"):
        build_plugin.fetch_pinned_wheel({"version": "", "wheel": "", "sha256": ""}, Path("."))


def test_plugins_xml_is_a_valid_index(build_plugin: ModuleType, tmp_path: Path) -> None:
    build_plugin.build(_wheel(tmp_path / "e.whl"), tmp_path / "dist", "https://example.org/dl/")
    root = ElementTree.parse(tmp_path / "dist" / "plugins.xml").getroot()
    (plugin,) = root.findall("pyqgis_plugin")
    assert plugin.get("name") == "Slab-QGIS"
    version = plugin.get("version")
    assert plugin.findtext("version") == version
    assert plugin.findtext("download_url") == f"https://example.org/dl/slab_qgis-{version}.zip"
    assert plugin.findtext("qgis_minimum_version") == "3.44"


def test_model_env_adds_the_bundled_engine_only_when_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {"PYTHONPATH": "first", "OTHER": "x"}
    monkeypatch.setattr(engine, "SOURCE", "installed")
    assert engine.model_env(base) == base

    monkeypatch.setattr(engine, "SOURCE", "bundled")
    env = engine.model_env(base)
    assert env["PYTHONPATH"].split(os.pathsep) == ["first", str(engine.VENDOR_DIR)]
    assert engine.model_env(env)["PYTHONPATH"] == env["PYTHONPATH"]  # not added twice
    assert base["PYTHONPATH"] == "first"  # input left untouched
