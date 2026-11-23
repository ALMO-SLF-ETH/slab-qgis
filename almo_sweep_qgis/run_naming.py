"""Name a single run's output folder from a user template, like a sweep names its runs.

A sweep gives every run its own folder (``<project>_000001``, ``_000002``, …) and catalogues them
in ``runs.json``. A single run used to always write ``<project>_000001``, so repeated manual runs
into one project overwrote each other and were invisible to the Results page. This module lets a
manual run be named the same way a sweep run is -- a unique, incrementing folder every time -- from
a template the user controls:

* **Placeholders** (bare upper-case words) expand per run:
  ``PROJECT`` (the project name), ``DATE``, ``TIME``, ``DATETIME`` and ``RUNCOUNTER`` (an
  incremental number scoped to the project). Everything else in the template is kept literally, so
  ``blatten_slush_DATE_TIME_RUNCOUNTER`` becomes e.g. ``blatten_slush_2026_11_06_14_30_00_000007``.
* **Date/time formats** use friendly tokens (``YYYY``/``YY``/``MM``/``DD`` and ``HH``/``hh``/``mm``/
  ``ss``), translated to :func:`datetime.datetime.strftime` codes. Defaults: ``YYYY_MM_DD`` and
  ``hh_mm_ss``.

:func:`resolve_run_id` is the entry point: it reads the project's existing ``runs.json`` and run
folders to pick the next free counter, expands the template, and -- as a backstop -- bumps the
counter until the folder does not already exist, so a new run can never land on an old one. The
module is free of Qt/GDAL so it is cheap to import and simple to test.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

#: Zero-padding width for ``RUNCOUNTER`` (matches a sweep's default run-id width).
COUNTER_WIDTH = 6

#: Friendly date/time tokens -> ``strftime`` codes. Case-sensitive: ``MM`` is the month, ``mm`` the
#: minute; ``HH``/``hh`` are both the 24-hour clock (the user wrote ``hh_mm_ss`` for 24-hour time).
_DT_TOKENS = {
    "YYYY": "%Y",
    "YY": "%y",
    "MM": "%m",
    "DD": "%d",
    "HH": "%H",
    "hh": "%H",
    "mm": "%M",
    "ss": "%S",
    "SS": "%S",
}
# Longest-first alternation so e.g. ``YYYY`` wins over ``YY``.
_DT_RE = re.compile("|".join(sorted(_DT_TOKENS, key=len, reverse=True)))

#: Characters allowed in a folder name; anything else becomes ``_``.
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
#: Trailing run number in a folder/run-id name, e.g. the ``7`` in ``blatten_000007``.
_TRAILING_INT_RE = re.compile(r"(\d+)\s*$")


def to_strftime(fmt: str) -> str:
    """Translate a friendly date/time format (``YYYY_MM_DD``) to a ``strftime`` pattern."""
    return _DT_RE.sub(lambda m: _DT_TOKENS[m.group(0)], fmt or "")


def sanitize_run_id(name: str) -> str:
    """Make ``name`` safe as a folder name: keep ``A-Z a-z 0-9 . _ -`` and collapse the rest."""
    cleaned = _SAFE_RE.sub("_", name).strip("_. ")
    return cleaned or "run"


def expand_run_name(
    template: str,
    *,
    project: str,
    counter: int,
    now: datetime,
    date_format: str,
    time_format: str,
    width: int = COUNTER_WIDTH,
) -> str:
    """Expand a name ``template``'s placeholders into a sanitised folder name (no uniqueness check).

    See the module docstring for the placeholders. Unknown words are left as-is; the result is run
    through :func:`sanitize_run_id`."""
    date_str = now.strftime(to_strftime(date_format))
    time_str = now.strftime(to_strftime(time_format))
    values = {
        "RUNCOUNTER": f"{counter:0{width}d}",
        "DATETIME": f"{date_str}_{time_str}",
        "PROJECT": project,
        "DATE": date_str,
        "TIME": time_str,
    }
    # Longest-first so ``DATETIME`` matches before ``DATE``/``TIME``.
    rx = re.compile("|".join(sorted(values, key=len, reverse=True)))
    expanded = rx.sub(lambda m: values[m.group(0)], template or "")
    return sanitize_run_id(expanded)


def next_counter(project_root: str | Path, _project_name: str = "") -> int:
    """The next free incremental run number for the project at ``project_root``.

    One past the largest number seen in the project's ``runs.json`` (each entry's ``counter``, or
    the trailing number of its ``run_id``) and in the existing ``<something>_NNNN`` run folders, so
    it keeps rising across separate launches and whatever the template looks like. 1 for an empty or
    missing project."""
    seen = {0}
    for entry in _read_run_entries(project_root):
        counter = entry.get("counter")
        if isinstance(counter, int) and not isinstance(counter, bool):
            seen.add(counter)
        else:
            trailing = _trailing_int(str(entry.get("run_id", "")))
            if trailing is not None:
                seen.add(trailing)
    with contextlib.suppress(OSError):
        for child in os.scandir(project_root):
            if child.is_dir():
                trailing = _trailing_int(child.name)
                if trailing is not None:
                    seen.add(trailing)
    return max(seen) + 1


def resolve_run_id(
    project_root: str | Path,
    *,
    template: str,
    project: str,
    date_format: str,
    time_format: str,
    now: datetime | None = None,
    width: int = COUNTER_WIDTH,
) -> tuple[str, int]:
    """Pick a unique run id (folder name) and its counter for the next manual run.

    Chooses the next counter (:func:`next_counter`), expands ``template``, and guarantees the folder
    does not already exist under ``project_root`` -- bumping the counter until it is free. When the
    template has no ``RUNCOUNTER`` placeholder, the counter is appended as a suffix so the folder is
    still unique and the id still advances. Returns ``(run_id, counter)``; nothing is created."""
    now = now or datetime.now()
    counter = next_counter(project_root, project)
    has_counter = "RUNCOUNTER" in (template or "")
    root = Path(project_root)
    while True:
        run_id = expand_run_name(
            template or "PROJECT_RUNCOUNTER",
            project=project,
            counter=counter,
            now=now,
            date_format=date_format,
            time_format=time_format,
            width=width,
        )
        if not has_counter:
            run_id = sanitize_run_id(f"{run_id}_{counter:0{width}d}")
        if not (root / run_id).is_dir():
            return run_id, counter
        counter += 1


# -- helpers --------------------------------------------------------------------------------
def _trailing_int(name: str) -> int | None:
    match = _TRAILING_INT_RE.search(name)
    return int(match.group(1)) if match else None


def _read_run_entries(project_root: str | Path) -> list[dict[str, object]]:
    """The ``runs`` list from ``<project_root>/runs.json`` (empty if missing or unreadable)."""
    path = Path(project_root) / "runs.json"
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("runs"), list):
            return [r for r in data["runs"] if isinstance(r, dict)]
    return []


__all__ = [
    "COUNTER_WIDTH",
    "expand_run_name",
    "next_counter",
    "resolve_run_id",
    "sanitize_run_id",
    "to_strftime",
]
