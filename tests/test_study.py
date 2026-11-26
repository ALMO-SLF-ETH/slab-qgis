"""Tests for reading a sweep's on-disk records into a StudySummary."""

from __future__ import annotations

import json
from pathlib import Path

from slab_qgis.results.study import SweptParam, load_study


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_study(root: Path) -> None:
    _write(
        root / "study.json",
        {
            "project": "brienz",
            "scheme_key": "grid",
            "seed": 7,
            "count": 2,
            "swept": {
                "friction": {"dist": "normal", "mean": 25, "std": 3, "min": 20, "max": 30},
                "density": {"dist": "uniform", "low": 2000, "high": 2500},
            },
        },
    )
    _write(
        root / "runs.json",
        {
            "manifest": {"name": "slab-pro", "version": "1.0", "sha256": "abc"},
            "study": {"scheme": {"kind": "grid"}, "seed": 7, "count": 2},
            "runs": [
                {
                    "run_id": "brienz_000001",
                    "status": "success",
                    "dir": str(root / "brienz_000001"),
                    "params": {"friction": 20.0, "density": 2000.0},
                    "weight": 0.25,
                    "duration_s": 12.0,
                },
                {
                    "run_id": "brienz_000002",
                    "status": "failed",
                    "dir": str(root / "brienz_000002"),
                    "params": {"friction": 30.0, "density": 2500.0},
                    "weight": 0.75,
                    "duration_s": 3.0,
                    "error": "model exited with code 1",
                },
            ],
        },
    )
    # one run's provenance, with a dirty component
    _write(
        root / "brienz_000001" / "provenance.json",
        {
            "engine": {"name": "slab-sweep", "commit": "deadbee", "dirty": False},
            "tools": {"gui": {"name": "slab_qgis", "commit": "cafe", "dirty": True}},
            "model": {"slab_pro_binary": {"commit": "f00", "dirty": False}},
        },
    )
    _write(
        root / "brienz_000001" / "result.json",
        {"outputs": {"max_values": str(root / "brienz_000001" / "raw" / "max_values.tif")}},
    )


def test_load_study_reads_plan_and_runs(tmp_path: Path) -> None:
    _make_study(tmp_path)
    s = load_study(tmp_path)

    assert s.project == "brienz" and s.scheme == "grid" and s.seed == 7 and s.count == 2
    assert s.manifest["name"] == "slab-pro"
    assert s.n_total == 2 and s.n_success == 1 and s.n_failed == 1
    assert s.total_duration_s == 15.0
    assert [p.name for p in s.swept] == ["friction", "density"]


def test_swept_param_summaries() -> None:
    normal = SweptParam(
        "friction", "Basal friction", {"dist": "normal", "mean": 25, "std": 3, "min": 20, "max": 30}
    )
    uniform = SweptParam("density", "Density", {"dist": "uniform", "low": 2000, "high": 2500})
    assert normal.summary() == "Normal(mean=25, std=3) in [20, 30]"
    assert uniform.summary() == "Uniform(2000, 2500)"


def test_failures_and_dirty_and_outputs(tmp_path: Path) -> None:
    _make_study(tmp_path)
    s = load_study(tmp_path)

    assert s.failures() == {"model exited with code 1": 1}
    assert "gui" in s.dirty_components()  # the dirty component is surfaced
    ok = s.successful_runs()
    assert len(ok) == 1
    assert ok[0].max_values_path().endswith("max_values.tif")


def test_missing_files_give_partial_summary(tmp_path: Path) -> None:
    # Nothing written yet: a partial summary, not an error.
    s = load_study(tmp_path)
    assert s.n_total == 0 and s.swept == [] and s.scheme == ""
