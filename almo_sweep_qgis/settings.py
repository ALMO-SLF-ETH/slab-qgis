"""Persistent, per-model runtime settings (e.g. the path to a model's binary).

A model declares the host-specific settings it needs via the manifest's ``runtime`` table.
The plugin stores the user's values in :class:`QgsSettings` (so they survive restarts and
need no shell environment) and supplies them to each run as environment variables of the
same name. Values are keyed by model name, so different models keep independent settings.
"""

from __future__ import annotations

from typing import Any

from qgis.core import QgsSettings

_PREFIX = "almo_sweep/runtime"


def _key(model_name: str, var: str) -> str:
    return f"{_PREFIX}/{model_name}/{var}"


def runtime_value(model_name: str, var: str) -> str:
    """The stored value for one runtime setting (empty string if unset)."""
    return str(QgsSettings().value(_key(model_name, var), "") or "")


def set_runtime_value(model_name: str, var: str, value: str) -> None:
    QgsSettings().setValue(_key(model_name, var), value)


def runtime_env(manifest: Any) -> dict[str, str]:
    """Stored runtime settings for ``manifest`` as an env-var mapping (unset ones omitted)."""
    env: dict[str, str] = {}
    for var in getattr(manifest, "runtime", {}):
        value = runtime_value(manifest.name, var)
        if value:
            env[var] = value
    return env


def missing_required(manifest: Any) -> list[str]:
    """Names of required runtime settings that have no stored value yet."""
    return [
        var
        for var, spec in getattr(manifest, "runtime", {}).items()
        if spec.required and not runtime_value(manifest.name, var)
    ]
