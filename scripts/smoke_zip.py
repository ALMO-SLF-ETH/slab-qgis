"""Smoke-test a built plugin zip: its bundled engine loads and can run a model.

Run it with the Python the plugin will run under (QGIS's), in an environment where slab-sweep is
*not* installed, so the bundled copy is what gets tested::

    python scripts/smoke_zip.py dist/slab_qgis-<version>.zip

It extracts the zip, then in a fresh interpreter without PYTHONPATH or the user site-packages
imports ``slab_qgis.engine``, checks it picked the bundled engine, and runs a one-step model whose
own process imports ``slab_sweep`` -- the same path a model launched from the plugin takes. Needs
no QGIS modules, only the standard library and numpy.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

_CHILD = r"""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])

from slab_qgis import engine

assert engine.AVAILABLE, f"engine not importable: {engine.IMPORT_ERROR}"
assert engine.SOURCE == "bundled", f"expected the bundled engine, got {engine.SOURCE!r}"
import slab_sweep
assert Path(slab_sweep.__file__).is_relative_to(engine.VENDOR_DIR), slab_sweep.__file__

work = Path(tempfile.mkdtemp())
(work / "model.py").write_text(
    "import os, sys\n"
    "from slab_sweep.contracts import RESULT_FILENAME, RunResult, RunSpec, emit_progress\n"
    "spec = RunSpec.read(sys.argv[2])\n"
    "emit_progress(1.0, 'done')\n"
    "RunResult.success(spec.run_id, {}).write(os.path.join(spec.output_dir, RESULT_FILENAME))\n",
    encoding="utf-8",
)
(work / "model.toml").write_text(
    '[model]\nname = "smoke"\nversion = "1"\nschema = "1.2"\n'
    '[invocation]\ncommand = ["{python}", "{model_dir}/model.py"]\n'
    'args_template = ["--runspec", "{runspec}"]\n',
    encoding="utf-8",
)
from slab_sweep.contracts import RunSpec

manifest = engine.load_manifest(work / "model.toml")
spec = RunSpec(
    run_id="smoke", model_name="smoke", params={}, inputs={}, output_dir=str(work / "run")
)
result = engine.run_one(manifest, spec, env=engine.model_env(dict(os.environ)))
assert result.ok, f"model run failed: {result.error}\n{result.message}"
print(f"ok: bundled slab-sweep {engine.VERSION} loaded from {engine.VENDOR_DIR} and ran a model")
"""


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(argv[0]) as zf:
            zf.extractall(tmp)
        # Not -I: QGIS's own Python on Windows needs PYTHONHOME from its launcher.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        cmd = [sys.executable, "-s", "-c", _CHILD, tmp]
        return subprocess.run(cmd, env=env, check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
