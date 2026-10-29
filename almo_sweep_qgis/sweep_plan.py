"""Translate the Parameters page's plain-data sweep config into an engine sweep, and count it.

The Parameters page keeps the sweep configuration as a plain ``dict`` of primitives (JSON-friendly,
no engine import), so the page renders even when the engine is absent and the config can be saved
for provenance. This module is the bridge:

* :func:`default_sweep` -- the empty/deterministic config a new study starts from.
* :func:`projected_count` / :func:`summary_text` -- pure, engine-free helpers the page calls on
  every edit to show "Grid: 3 x 2 = 6 simulations" live (no sampling, so cheap).
* :func:`build_plan` -- the real expansion, called once at launch: it builds the engine's
  distributions + selection strategy and returns the concrete, weighted samples to run.

The engine is imported lazily inside :func:`build_plan` only, so importing this module (for the
pure helpers) never requires the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: A grid/sample count above which the UI warns that the study may run for a very long time.
LARGE_SWEEP = 200

#: Default grid points per swept parameter (mirrors the engine's ``GridSelection.DEFAULT_STEPS``).
_DEFAULT_STEPS = 5

#: Selection schemes the Parameters page offers: config key -> human label.
SCHEMES = (
    ("grid", "Grid (full factorial)"),
    ("latin_hypercube", "Latin hypercube"),
    ("monte_carlo", "Monte Carlo"),
)
_SCHEME_LABELS = dict(SCHEMES)


class SweepPlanError(Exception):
    """A study config could not be turned into a runnable set of samples."""


def default_sweep() -> dict[str, Any]:
    """The config a fresh study starts from: disabled (one deterministic run)."""
    return {
        "enabled": False,
        "scheme": "grid",
        "n_samples": 50,  # Monte Carlo / Latin hypercube
        "steps": _DEFAULT_STEPS,  # default grid points per parameter
        "spacing": "linear",  # grid spacing: "linear" | "log"
        "seed": None,  # Monte Carlo / Latin hypercube; None = nondeterministic
        # name -> {"dist": "uniform"|"normal", "low","high" | "mean","std","min","max",
        #          "steps": int|None (grid per-parameter override)}
        "params": {},
    }


def varied_params(sweep: dict[str, Any]) -> dict[str, Any]:
    """The per-parameter entries for the parameters the study actually varies."""
    return dict(sweep.get("params") or {})


def projected_count(sweep: dict[str, Any]) -> int:
    """How many simulations this config implies, without drawing any samples.

    Grid: the product of each varied parameter's step count. Monte Carlo / Latin hypercube:
    the requested sample count. A sweep with nothing varied (or disabled) is a single run.
    """
    if not sweep.get("enabled"):
        return 1
    varied = varied_params(sweep)
    if not varied:
        return 1
    if sweep.get("scheme", "grid") == "grid":
        default_steps = int(sweep.get("steps", _DEFAULT_STEPS) or _DEFAULT_STEPS)
        count = 1
        for entry in varied.values():
            count *= max(1, int(entry.get("steps") or default_steps))
        return count
    return max(1, int(sweep.get("n_samples", 1) or 1))


def summary_text(sweep: dict[str, Any]) -> str:
    """A one-line description of the study, e.g. ``Grid: 3 x 2 = 6 simulations``."""
    if not sweep.get("enabled"):
        return "Single deterministic run (parameters as set on the Parameters page)."
    varied = varied_params(sweep)
    if not varied:
        return "No parameters selected to vary — this runs a single simulation."
    count = projected_count(sweep)
    scheme = _SCHEME_LABELS.get(sweep.get("scheme", "grid"), "Sweep")
    if sweep.get("scheme", "grid") == "grid":
        default_steps = int(sweep.get("steps", _DEFAULT_STEPS) or _DEFAULT_STEPS)
        factors = " x ".join(
            str(max(1, int(e.get("steps") or default_steps))) for e in varied.values()
        )
        return f"{scheme}: {factors} = {count} simulation(s) over {len(varied)} parameter(s)."
    return f"{scheme}: {count} simulation(s) over {len(varied)} parameter(s)."


@dataclass(frozen=True)
class SweepPlan:
    """The concrete, weighted samples a study expands to, plus provenance."""

    samples: list[Any]  # engine ``Sample`` records (``.values`` dict + ``.weight`` float)
    describe: dict[str, Any]  # the selection strategy's JSON-serialisable summary
    seed: int | None

    @property
    def count(self) -> int:
        return len(self.samples)


def build_plan(manifest: Any, sweep: dict[str, Any], base_params: dict[str, Any]) -> SweepPlan:
    """Expand ``sweep`` into concrete weighted samples against ``manifest``.

    Varied parameters get an engine distribution (their range + weight profile); every other
    parameter is pinned to its current value from ``base_params`` (the Parameters page). The
    chosen scheme becomes the engine selection strategy. Raises :class:`SweepPlanError` with a
    human message on any inconsistency (bad range, out-of-bounds, non-sweepable parameter, ...).
    """
    from almo_sweep.contracts import (
        GridSelection,
        LatinHypercubeSelection,
        MonteCarloSelection,
        Normal,
        Uniform,
        build_sweep,
    )
    from almo_sweep.contracts.distributions import DistributionError
    from almo_sweep.contracts.selection import SelectionError
    from almo_sweep.contracts.sweep import SweepError

    varied = varied_params(sweep)
    try:
        distributions: dict[str, Any] = {}
        fixed: dict[str, Any] = {}
        for name in manifest.params:
            if name in varied:
                distributions[name] = _make_distribution(varied[name], Uniform, Normal)
            elif name in base_params:
                # Pin every non-varied parameter to its current Parameters-page value, so the
                # sweep varies exactly what the user chose and nothing else.
                fixed[name] = base_params[name]

        scheme = sweep.get("scheme", "grid")
        seed = sweep.get("seed")
        if scheme == "grid":
            strategy: Any = GridSelection(
                steps=_grid_steps(sweep, varied),
                spacing=str(sweep.get("spacing", "linear")),
            )
        elif scheme == "latin_hypercube":
            strategy = LatinHypercubeSelection(_n_samples(sweep))
        else:
            strategy = MonteCarloSelection(_n_samples(sweep))

        cfg = build_sweep(
            manifest, distributions=distributions, fixed=fixed, strategy=strategy, seed=seed
        )
        samples = cfg.samples_with_weights(manifest)
    except (SweepError, DistributionError, SelectionError, ValueError) as exc:
        raise SweepPlanError(str(exc)) from exc

    return SweepPlan(samples=samples, describe=strategy.describe(), seed=seed)


def _make_distribution(entry: dict[str, Any], uniform_cls: Any, normal_cls: Any) -> Any:
    if entry.get("dist") == "normal":
        return normal_cls(
            mean=float(entry["mean"]),
            std=float(entry["std"]),
            min=_opt_float(entry.get("min")),
            max=_opt_float(entry.get("max")),
        )
    return uniform_cls(low=float(entry["low"]), high=float(entry["high"]))


def _grid_steps(sweep: dict[str, Any], varied: dict[str, Any]) -> dict[str, int]:
    default_steps = int(sweep.get("steps", _DEFAULT_STEPS) or _DEFAULT_STEPS)
    return {name: int(entry.get("steps") or default_steps) for name, entry in varied.items()}


def _n_samples(sweep: dict[str, Any]) -> int:
    return max(1, int(sweep.get("n_samples", 1) or 1))


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)
