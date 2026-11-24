"""Tests for per-model runtime settings (persistence, env injection, dialog)."""

from __future__ import annotations

import pytest
from qgis.core import QgsApplication
from qgis.gui import QgsFileWidget
from slab_qgis import engine, settings
from slab_qgis.panels.settings_dialog import RuntimeSettingsDialog


@pytest.fixture
def manifest():
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    from slab_sweep.contracts import parse_manifest

    return parse_manifest(
        {
            "model": {"name": "toy_settings", "version": "0"},
            "invocation": {"command": ["x"]},
            "runtime": {
                "TOY_BIN": {"kind": "file", "required": True, "description": "binary"},
                "TOY_DIR": {"kind": "directory", "required": False, "description": "dir"},
            },
        }
    )


def test_persist_and_env(qgis_app: QgsApplication, manifest) -> None:
    settings.set_runtime_value("toy_settings", "TOY_BIN", "/opt/toy/bin")
    settings.set_runtime_value("toy_settings", "TOY_DIR", "")
    assert settings.runtime_value("toy_settings", "TOY_BIN") == "/opt/toy/bin"
    # Only non-empty settings become env vars.
    assert settings.runtime_env(manifest) == {"TOY_BIN": "/opt/toy/bin"}


def test_missing_required(qgis_app: QgsApplication, manifest) -> None:
    settings.set_runtime_value("toy_settings", "TOY_BIN", "")
    assert settings.missing_required(manifest) == ["TOY_BIN"]
    settings.set_runtime_value("toy_settings", "TOY_BIN", "/opt/toy/bin")
    assert settings.missing_required(manifest) == []  # TOY_DIR is optional


def test_model_registry_persists_add_remove_and_last(qgis_app: QgsApplication) -> None:
    # start clean
    for p in settings.registered_models():
        settings.remove_model(p)
    assert settings.registered_models() == []

    settings.add_model(r"C:\models\a\model.toml")
    settings.add_model(r"C:\models\b\model.toml")
    settings.add_model(r"C:\models\a\model.toml")  # duplicate ignored
    assert settings.registered_models() == [r"C:\models\a\model.toml", r"C:\models\b\model.toml"]

    # a fresh read sees the same list (persisted via QgsSettings)
    assert settings.registered_models() == [r"C:\models\a\model.toml", r"C:\models\b\model.toml"]

    settings.set_last_model(r"C:\models\b\model.toml")
    assert settings.last_model() == r"C:\models\b\model.toml"

    # removing the last model also clears it as 'last'
    settings.remove_model(r"C:\models\b\model.toml")
    assert settings.registered_models() == [r"C:\models\a\model.toml"]
    assert settings.last_model() == ""


def test_dialog_builds_prefilled_fields(qgis_app: QgsApplication, manifest) -> None:
    settings.set_runtime_value("toy_settings", "TOY_BIN", "/opt/toy/bin")
    dlg = RuntimeSettingsDialog(manifest)
    pickers = dlg.findChildren(QgsFileWidget)
    assert len(pickers) == 2  # one file, one directory
    assert any(p.filePath() == "/opt/toy/bin" for p in pickers)


@pytest.fixture
def packaged_manifest(tmp_path):
    from slab_sweep.contracts import load_manifest

    toml = tmp_path / "model.toml"
    toml.write_text(
        """
[model]
name = "toy_packaged"
version = "0"
schema = "1.2"
[invocation]
command = ["x"]
[runtime.TOY_BIN]
kind = "file"
default = "{model_dir}/bin/toy{exe}"
""",
        encoding="utf-8",
    )
    return load_manifest(toml)


def test_default_satisfies_required(qgis_app: QgsApplication, packaged_manifest) -> None:
    settings.set_runtime_value("toy_packaged", "TOY_BIN", "")
    assert settings.missing_required(packaged_manifest) == []
    default = settings.runtime_defaults(packaged_manifest)["TOY_BIN"]
    assert default.startswith(packaged_manifest.model_dir.as_posix())
    # the default is not stored: the engine applies it, so a moved package still works
    assert settings.runtime_env(packaged_manifest) == {}


def test_dialog_shows_default_as_placeholder(qgis_app: QgsApplication, packaged_manifest) -> None:
    settings.set_runtime_value("toy_packaged", "TOY_BIN", "")
    dlg = RuntimeSettingsDialog(packaged_manifest)
    (picker,) = dlg.findChildren(QgsFileWidget)
    assert picker.filePath() == ""
    assert (
        picker.lineEdit().placeholderText()
        == settings.runtime_defaults(packaged_manifest)["TOY_BIN"]
    )
