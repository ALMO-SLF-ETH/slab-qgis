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
#: Registry of model manifests the user has added, and the last one they had open.
_MODELS_KEY = "almo_sweep/models"
_LAST_MODEL_KEY = "almo_sweep/last_model"


def _key(model_name: str, var: str) -> str:
    return f"{_PREFIX}/{model_name}/{var}"


# ---------------------------------------------------------------------------
# Model registry: a persistent list of manifest paths the user has added
# ---------------------------------------------------------------------------
def _as_str_list(value: Any) -> list[str]:
    """QgsSettings may hand back a list, a single string, or None for a stored list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    try:
        return [str(v) for v in value if str(v)]
    except TypeError:
        return []


def registered_models() -> list[str]:
    """The manifest paths the user has added, in insertion order (deduplicated)."""
    seen: set[str] = set()
    out: list[str] = []
    for path in _as_str_list(QgsSettings().value(_MODELS_KEY)):
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def add_model(path: str) -> None:
    """Add ``path`` to the registry (no-op if already present)."""
    models = registered_models()
    if path and path not in models:
        models.append(path)
        QgsSettings().setValue(_MODELS_KEY, models)


def remove_model(path: str) -> None:
    """Drop ``path`` from the registry (and clear it as the last model if it was)."""
    QgsSettings().setValue(_MODELS_KEY, [p for p in registered_models() if p != path])
    if last_model() == path:
        set_last_model("")


def last_model() -> str:
    """The manifest path last selected (empty string if none / not set)."""
    return str(QgsSettings().value(_LAST_MODEL_KEY, "") or "")


def set_last_model(path: str) -> None:
    QgsSettings().setValue(_LAST_MODEL_KEY, path)


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
