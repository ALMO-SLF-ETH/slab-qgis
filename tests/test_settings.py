"""Tests for per-model runtime settings (persistence, env injection, dialog)."""

from __future__ import annotations

import pytest
from almo_sweep_qgis import engine, settings
from almo_sweep_qgis.panels.settings_dialog import RuntimeSettingsDialog
from qgis.core import QgsApplication
from qgis.gui import QgsFileWidget


@pytest.fixture
def manifest():
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    from almo_sweep.contracts import parse_manifest

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
