"""Tests for run-folder naming: template expansion, formats, and the incremental counter."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from slab_qgis import run_naming

_NOW = datetime(2026, 11, 6, 14, 30, 5)


def test_to_strftime_translates_friendly_tokens() -> None:
    assert run_naming.to_strftime("YYYY_MM_DD") == "%Y_%m_%d"
    assert run_naming.to_strftime("hh_mm_ss") == "%H_%M_%S"
    assert run_naming.to_strftime("YY-MM") == "%y-%m"  # MM is month, not minute


def test_expand_run_name_fills_all_placeholders() -> None:
    name = run_naming.expand_run_name(
        "blatten_slush_DATE_TIME_RUNCOUNTER",
        project="blatten",
        counter=7,
        now=_NOW,
        date_format="YYYY_MM_DD",
        time_format="hh_mm_ss",
    )
    assert name == "blatten_slush_2026_11_06_14_30_05_000007"


def test_expand_project_and_datetime_tokens() -> None:
    assert (
        run_naming.expand_run_name(
            "PROJECT_DATETIME",
            project="demo",
            counter=1,
            now=_NOW,
            date_format="YYYY_MM_DD",
            time_format="hh_mm_ss",
        )
        == "demo_2026_11_06_14_30_05"
    )


def test_sanitize_strips_unsafe_characters() -> None:
    assert run_naming.sanitize_run_id("a/b c:d*e") == "a_b_c_d_e"
    assert run_naming.sanitize_run_id("***") == "run"  # never empty


def test_next_counter_starts_at_one_then_follows_runs_json(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    assert run_naming.next_counter(root) == 1
    (root / "runs.json").write_text(
        json.dumps({"runs": [{"run_id": "proj_000003", "counter": 3}]}), encoding="utf-8"
    )
    assert run_naming.next_counter(root) == 4


def test_next_counter_also_scans_existing_folders(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "proj_000009").mkdir(parents=True)  # a leftover folder, no runs.json
    assert run_naming.next_counter(root) == 10


def test_resolve_run_id_increments_and_never_collides(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    first, c1 = run_naming.resolve_run_id(
        root,
        template="PROJECT_RUNCOUNTER",
        project="proj",
        date_format="YYYY_MM_DD",
        time_format="hh_mm_ss",
        now=_NOW,
    )
    assert (first, c1) == ("proj_000001", 1)
    (root / first).mkdir(parents=True)  # the first run created its folder

    second, c2 = run_naming.resolve_run_id(
        root,
        template="PROJECT_RUNCOUNTER",
        project="proj",
        date_format="YYYY_MM_DD",
        time_format="hh_mm_ss",
        now=_NOW,
    )
    assert (second, c2) == ("proj_000002", 2)


def test_resolve_run_id_appends_counter_when_template_has_none(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "fixed_run").mkdir(parents=True)  # a static-named run already exists
    run_id, counter = run_naming.resolve_run_id(
        root,
        template="fixed_run",  # no RUNCOUNTER placeholder
        project="proj",
        date_format="YYYY_MM_DD",
        time_format="hh_mm_ss",
        now=_NOW,
    )
    # A counter suffix is appended so the folder is still unique and the id still advances.
    assert run_id == "fixed_run_000001"
    assert counter == 1
    assert not (root / run_id).exists()
