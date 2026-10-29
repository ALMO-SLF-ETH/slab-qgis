"""Tests for the plain-data sweep config helpers and the engine-backed plan builder."""

from __future__ import annotations

from pathlib import Path

import pytest
from almo_sweep_qgis import engine, sweep_plan


@pytest.fixture
def manifest(dampm_manifest_path: Path):
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    return engine.load_manifest(str(dampm_manifest_path))


# --- pure helpers (no engine) ---------------------------------------------------------
def test_default_is_a_single_deterministic_run() -> None:
    sweep = sweep_plan.default_sweep()
    assert sweep["enabled"] is False
    assert sweep_plan.projected_count(sweep) == 1
    assert "Single deterministic run" in sweep_plan.summary_text(sweep)


def test_grid_count_is_the_product_of_steps() -> None:
    sweep = sweep_plan.default_sweep()
    sweep["enabled"] = True
    sweep["scheme"] = "grid"
    sweep["steps"] = 5
    sweep["params"] = {
        "friction_angle": {"dist": "uniform", "low": 0.0, "high": 60.0, "steps": 3},
        "density": {"dist": "uniform", "low": 500.0, "high": 3000.0},  # uses default 5
    }
    assert sweep_plan.projected_count(sweep) == 15  # 3 x 5
    assert "15 simulation" in sweep_plan.summary_text(sweep)


def test_sampler_count_is_n_samples() -> None:
    sweep = sweep_plan.default_sweep()
    sweep.update(enabled=True, scheme="monte_carlo", n_samples=40)
    sweep["params"] = {"friction_angle": {"dist": "normal", "mean": 30.0, "std": 5.0}}
    assert sweep_plan.projected_count(sweep) == 40
    assert "Monte Carlo" in sweep_plan.summary_text(sweep)


def test_enabled_but_nothing_varied_is_one_run() -> None:
    sweep = sweep_plan.default_sweep()
    sweep["enabled"] = True
    assert sweep_plan.projected_count(sweep) == 1
    assert "single simulation" in sweep_plan.summary_text(sweep)


# --- build_plan (engine-backed) -------------------------------------------------------
def test_build_plan_grid_pins_unvaried_params(manifest) -> None:
    sweep = sweep_plan.default_sweep()
    sweep.update(enabled=True, scheme="grid", steps=3)
    sweep["params"] = {"friction_angle": {"dist": "uniform", "low": 0.0, "high": 60.0}}
    base = {"friction_angle": 30.0, "density": 1234.0, "integrator": "euler", "rk4_stages": 4}

    plan = sweep_plan.build_plan(manifest, sweep, base)
    assert plan.count == 3
    frictions = sorted(s.values["friction_angle"] for s in plan.samples)
    assert frictions == [0.0, 30.0, 60.0]  # the grid axis
    # unvaried parameters keep their current Parameters-page values
    assert all(s.values["density"] == 1234.0 for s in plan.samples)
    assert all(s.values["integrator"] == "euler" for s in plan.samples)
    assert abs(sum(s.weight for s in plan.samples) - 1.0) < 1e-9
    assert plan.describe["kind"] == "grid"


def test_build_plan_monte_carlo_is_seeded(manifest) -> None:
    sweep = sweep_plan.default_sweep()
    sweep.update(enabled=True, scheme="monte_carlo", n_samples=16, seed=42)
    sweep["params"] = {"friction_angle": {"dist": "normal", "mean": 30.0, "std": 5.0}}
    base = {"density": 2000.0, "integrator": "rk4"}

    a = sweep_plan.build_plan(manifest, sweep, base)
    b = sweep_plan.build_plan(manifest, sweep, base)
    assert a.count == 16
    assert [s.values["friction_angle"] for s in a.samples] == [
        s.values["friction_angle"] for s in b.samples
    ]  # reproducible from the seed


def test_build_plan_reports_a_friendly_error_on_bad_range(manifest) -> None:
    sweep = sweep_plan.default_sweep()
    sweep.update(enabled=True, scheme="grid")
    # low > manifest max -> the engine rejects it; build_plan surfaces a SweepPlanError.
    sweep["params"] = {"friction_angle": {"dist": "uniform", "low": 0.0, "high": 90.0}}
    with pytest.raises(sweep_plan.SweepPlanError):
        sweep_plan.build_plan(manifest, sweep, {})
