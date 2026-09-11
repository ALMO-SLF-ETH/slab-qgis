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


def test_dialog_builds_prefilled_fields(qgis_app: QgsApplication, manifest) -> None:
    settings.set_runtime_value("toy_settings", "TOY_BIN", "/opt/toy/bin")
    dlg = RuntimeSettingsDialog(manifest)
    pickers = dlg.findChildren(QgsFileWidget)
    assert len(pickers) == 2  # one file, one directory
    assert any(p.filePath() == "/opt/toy/bin" for p in pickers)
