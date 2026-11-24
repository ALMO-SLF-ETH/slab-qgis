"""Read a sweep's on-disk records into a summary the Results page can show.

A sweep leaves two JSON files at its project root: ``study.json`` (the plan -- scheme, seed and
the swept parameters' distributions) and ``runs.json`` (the run index -- each run's outcome,
parameters and weight, written as runs complete). This module reads both into a
:class:`StudySummary` of plain data, plus the provenance of the code that produced the runs. It is
deliberately free of GDAL/Qt so it is cheap to import and simple to test; the raster aggregation
lives in :mod:`slab_qgis.results.aggregate`.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RUN_INDEX_FILENAME = "runs.json"
STUDY_FILENAME = "study.json"
RESULT_FILENAME = "result.json"
PROVENANCE_FILENAME = "provenance.json"


@dataclass(frozen=True)
class SweptParam:
    """A parameter the study varied, with its chosen distribution."""

    name: str
    label: str
    spec: dict[str, Any]
    unit: str = ""

    @property
    def header(self) -> str:
        """Column header: the label with its unit appended, e.g. ``dt_max (s)``."""
        return f"{self.label} ({self.unit})" if self.unit else self.label

    def summary(self) -> str:
        """A one-line description, e.g. ``Normal(mean=25, std=3) in [20, 30]``."""
        dist = self.spec.get("dist")
        if dist == "normal":
            text = f"Normal(mean={self.spec.get('mean')}, std={self.spec.get('std')})"
            lo, hi = self.spec.get("min"), self.spec.get("max")
            if lo is not None or hi is not None:
                text += f" in [{lo}, {hi}]"
            return text
        if dist == "uniform":
            return f"Uniform({self.spec.get('low')}, {self.spec.get('high')})"
        return str(dist or "varied")


@dataclass
class RunRecord:
    """One run as the index recorded it."""

    run_id: str
    status: str
    weight: float
    params: dict[str, Any]
    output_dir: str
    duration_s: float | None = None
    error: str | None = None
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def outputs(self) -> dict[str, str]:
        """All of the run's outputs (name -> path), read from its ``result.json``.

        This is the same ``outputs`` mapping a single deterministic run records, so the Results page
        can load one sweep run exactly as the Run page loads a single run. Empty if the file is
        missing or unreadable (e.g. a failed run)."""
        raw = _read_json(Path(self.output_dir) / RESULT_FILENAME).get("outputs", {})
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}

    def max_values_path(self) -> str | None:
        """The run's ``max_values`` raster, read from its ``result.json`` (``None`` if absent)."""
        path = self.outputs().get("max_values")
        return path if isinstance(path, str) else None


@dataclass
class StudySummary:
    """Everything the Results page needs about one sweep."""

    project_root: str
    project: str = ""
    scheme: str = ""
    seed: int | None = None
    count: int = 0
    manifest: dict[str, Any] = field(default_factory=dict)
    swept: list[SweptParam] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def n_total(self) -> int:
        return len(self.runs)

    @property
    def n_success(self) -> int:
        return sum(1 for r in self.runs if r.ok)

    @property
    def n_failed(self) -> int:
        return sum(1 for r in self.runs if not r.ok)

    @property
    def total_duration_s(self) -> float:
        return float(sum(r.duration_s or 0.0 for r in self.runs))

    def failures(self) -> dict[str, int]:
        """How many runs failed for each distinct error message."""
        counts: dict[str, int] = {}
        for r in self.runs:
            if not r.ok:
                counts[r.error or "unknown error"] = counts.get(r.error or "unknown error", 0) + 1
        return counts

    def successful_runs(self) -> list[RunRecord]:
        return [r for r in self.runs if r.ok]

    def dirty_components(self) -> list[str]:
        """Names of code components recorded as git-dirty (results then are not reproducible)."""
        dirty: list[str] = []
        for label, rec in _provenance_components(self.provenance):
            if isinstance(rec, dict) and rec.get("dirty"):
                dirty.append(label)
        return dirty


def load_study(project_root: str | Path, manifest: Any | None = None) -> StudySummary:
    """Read ``runs.json`` + ``study.json`` under ``project_root`` into a :class:`StudySummary`.

    ``manifest`` (optional) supplies human labels for the swept parameters. Missing or partial
    files yield a partial summary rather than an error, so a sweep still running can be inspected.
    """
    root = Path(project_root)
    study = _read_json(root / STUDY_FILENAME)
    index = _read_json(root / RUN_INDEX_FILENAME)

    runs = [_run_record(r, root) for r in index.get("runs", []) if isinstance(r, dict)]
    labels = _param_labels(manifest)
    units = _param_units(manifest)
    swept = [
        SweptParam(name=name, label=labels.get(name, name), spec=spec, unit=units.get(name, ""))
        for name, spec in (study.get("swept") or {}).items()
        if isinstance(spec, dict)
    ]
    summary = StudySummary(
        project_root=str(root),
        project=str(study.get("project") or root.name),
        scheme=_scheme_name(study, index),
        seed=study.get("seed")
        if study.get("seed") is not None
        else index.get("study", {}).get("seed"),
        count=int(study.get("count") or index.get("study", {}).get("count") or len(runs)),
        manifest=index.get("manifest") or {},
        swept=swept,
        runs=runs,
        provenance=_first_provenance(runs),
    )
    return summary


# --- helpers --------------------------------------------------------------------------
def _run_record(raw: dict[str, Any], root: Path) -> RunRecord:
    out = raw.get("dir") or str(root / str(raw.get("run_id", "")))
    return RunRecord(
        run_id=str(raw.get("run_id", "")),
        status=str(raw.get("status", "")),
        weight=float(raw.get("weight", 1.0)),
        params=dict(raw.get("params") or {}),
        output_dir=out,
        duration_s=raw.get("duration_s"),
        error=raw.get("error"),
        attempts=int(raw.get("attempts", 1)),
    )


def _scheme_name(study: dict[str, Any], index: dict[str, Any]) -> str:
    if study.get("scheme_key"):
        return str(study["scheme_key"])
    for describe in (study.get("scheme"), index.get("study", {}).get("scheme")):
        if isinstance(describe, dict) and describe.get("kind"):
            return str(describe["kind"])
    return ""


def _param_labels(manifest: Any | None) -> dict[str, str]:
    if manifest is None:
        return {}
    labels: dict[str, str] = {}
    for name, spec in getattr(manifest, "params", {}).items():
        labels[name] = getattr(spec, "display_label", None) or getattr(spec, "label", None) or name
    return labels


def _param_units(manifest: Any | None) -> dict[str, str]:
    """Each manifest parameter's unit (e.g. ``s`` for ``dt_max``); empty when not declared."""
    if manifest is None:
        return {}
    units: dict[str, str] = {}
    for name, spec in getattr(manifest, "params", {}).items():
        units[name] = getattr(spec, "unit", None) or ""
    return units


def _first_provenance(runs: list[RunRecord]) -> dict[str, Any]:
    for r in runs:
        prov = _read_json(Path(r.output_dir) / PROVENANCE_FILENAME)
        if prov:
            return prov
    return {}


def _provenance_components(prov: dict[str, Any]) -> list[tuple[str, Any]]:
    """Flatten a provenance.json into (label, record) pairs for the code components it names."""
    items: list[tuple[str, Any]] = []
    if isinstance(prov.get("engine"), dict):
        items.append(("engine", prov["engine"]))
    for label, rec in (prov.get("tools") or {}).items():
        items.append((str(label), rec))
    for label, rec in (prov.get("model") or {}).items():
        items.append((str(label), rec))
    return items


def _read_json(path: Path) -> dict[str, Any]:
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    return {}


__all__ = [
    "RunRecord",
    "StudySummary",
    "SweptParam",
    "load_study",
]
