"""Build the intro GIFs: inputs, Slab-Pro run, rendering, for each scene.

    python make_all.py [--slab-pro PATH] [--palette qgis] [rise floor fall plan ...]

"rise" (the default) is the plugin's intro: it is written to slab_qgis/resources/intro.gif.
Other scenes are written to work/slab-qgis-intro-<scene>.gif.

The Slab-Pro binary is --slab-pro, else $SLAB_PRO_BIN, else ``slab-pro`` on PATH. Use a
self-contained release package's binary (bin/slab-pro[.exe]) or a build whose runtime libraries
are found. "rise" is rendered backwards (grains rain down into the word), the others forwards.
For other options, run make_inputs.py / render_gif.py yourself.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTRO_GIF = HERE.parents[1] / "slab_qgis" / "resources" / "intro.gif"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenes", nargs="*", default=["rise"])
    parser.add_argument("--palette", default="qgis", help="see render_gif.py --help")
    parser.add_argument(
        "--slab-pro", default=os.environ.get("SLAB_PRO_BIN") or shutil.which("slab-pro")
    )
    args = parser.parse_args()
    if not args.slab_pro:
        parser.error("no Slab-Pro binary: pass --slab-pro or set SLAB_PRO_BIN")

    for scene in args.scenes:
        work = HERE / "work" / scene
        subprocess.run(
            [sys.executable, HERE / "make_inputs.py", "--scene", scene, "--out", work], check=True
        )
        shutil.rmtree(work / "output", ignore_errors=True)
        with (work / "run.log").open("w", encoding="utf-8") as log:
            proc = subprocess.run(
                [args.slab_pro, "simConfig.json"], cwd=work, stdout=log, stderr=subprocess.STDOUT
            )
        if proc.returncode != 0:
            print(f"error: Slab-Pro failed for {scene}; see {work / 'run.log'}", file=sys.stderr)
            return 1
        render = [sys.executable, HERE / "render_gif.py", "--run", work, "--palette", args.palette]
        if scene == "rise":
            render += ["--reverse", "--hold-start", "0.2", "--hold-end", "1.5"]
        gif = INTRO_GIF if scene == "rise" else work.parent / f"slab-qgis-intro-{scene}.gif"
        subprocess.run([*render, "--out", gif], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
