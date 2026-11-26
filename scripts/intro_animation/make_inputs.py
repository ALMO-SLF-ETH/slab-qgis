"""Inputs for the intro animation: the word "Slab-QGIS" as a granular release for Slab-Pro.

    python make_inputs.py --scene rise|floor|fall|plan [--out work/<scene>]

Slab-Pro is depth-averaged: it computes the flow over a terrain surface, seen from above. To make
the letters collapse *down the screen*, as if they stood upright, the terrain is a steep slope
falling towards the bottom of the image, and the word is released on it:

  floor  the slope curves into a flat floor below the word: the letters slide down and pile up
  fall   one long slope: the letters slide down and out of the (fixed) view at the bottom
  rise   "fall" upside down (gravity pulls up): the letters slide up and out at the top; played
         backwards (render_gif.py --reverse), grains rain down from above into the word
  plan   a flat plane: the letters slump outwards in place, as seen from above

Writes into ``--out``:
  terrain.asc     the DEM (ESRI ASCII grid)
  release.asc     release depth: ``--depth`` metres inside the letters, 0 elsewhere (same grid)
  simConfig.json  the Slab-Pro configuration (RASTER release, Coulomb friction, frequent output)
  view.json       the part of the domain the animation shows (render_gif.py reads it)

The text is drawn with DejaVu Sans Bold, which ships with matplotlib, so the result does not
depend on the fonts installed on the machine.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from matplotlib import font_manager
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
#: A Slab-Pro checkout, for the complete default configuration in interface/ (default: the
#: Slab-Pro-dev checkout next to this repository).
SLAB_PRO = HERE.parents[2] / "Slab-Pro-dev"

#: Per scene: slope (deg; negative = falling towards the top), Coulomb mu, simulated time (s),
#: free space below and above the word (m), and how much of it the view shows (m).
_TALL = 220.0  # room for the material to run out of the view without leaving the terrain
SCENES = {
    "floor": {"slope": 40.0, "mu": 0.55, "total_time": 7.0, "below": 60.0, "view_below": 32.0},
    "fall": {"slope": 45.0, "mu": 0.3, "total_time": 6.0, "below": _TALL, "view_below": 4.0},
    "rise": {"slope": -45.0, "mu": 0.3, "total_time": 6.0, "above": _TALL, "view_below": 4.0},
    "plan": {"slope": 0.0, "mu": 0.7, "total_time": 1.8, "view_below": 6.0},
}


def text_mask(text: str, cap_height_px: int) -> np.ndarray:
    """Boolean mask of ``text`` (True inside the glyphs), cropped to the ink."""
    font_path = font_manager.findfont(
        font_manager.FontProperties(family="DejaVu Sans", weight="bold"), fallback_to_default=False
    )
    # DejaVu Sans' cap height is ~0.73 em
    font = ImageFont.truetype(font_path, size=round(cap_height_px / 0.73))
    left, top, right, bottom = font.getbbox(text)
    img = Image.new("L", (right - left + 4, bottom - top + 4), 0)
    ImageDraw.Draw(img).text((2 - left, 2 - top), text, font=font, fill=255)
    mask = np.asarray(img) >= 128
    rows, cols = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    return mask[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]


def write_asc(path: Path, data: np.ndarray, dx: float) -> None:
    nrows, ncols = data.shape
    header = (
        f"ncols {ncols}\nnrows {nrows}\nxllcorner 0.0\nyllcorner 0.0\n"
        f"cellsize {dx}\nNODATA_value -9999\n"
    )
    body = "\n".join(" ".join(f"{v:.4g}" for v in row) for row in data)
    path.write_text(header + body + "\n", encoding="ascii")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scene", choices=sorted(SCENES), default="rise")
    parser.add_argument("--out", type=Path, help="output folder (default: work/<scene>)")
    parser.add_argument("--text", default="Slab-QGIS")
    parser.add_argument("--dx", type=float, default=0.5, help="cell size (m)")
    parser.add_argument("--cap-height", type=float, default=30.0, help="letter height (m)")
    parser.add_argument("--depth", type=float, default=3.0, help="release depth in the letters (m)")
    parser.add_argument("--side", type=float, default=25.0, help="free space left/right/above (m)")
    parser.add_argument("--slope", type=float, help="slope angle (deg); default per scene")
    parser.add_argument("--mu", type=float, help="Coulomb friction coefficient; default per scene")
    parser.add_argument("--total-time", type=float, help="simulated time (s); default per scene")
    parser.add_argument("--output-dt", type=float, default=0.05, help="frame interval (s)")
    parser.add_argument(
        "--bend", type=float, default=6.0, help="radius of the slope-floor bend (m)"
    )
    parser.add_argument("--slab-pro-src", type=Path, default=SLAB_PRO, help="Slab-Pro checkout")
    args = parser.parse_args()
    scene = dict(SCENES[args.scene])
    for key in ("slope", "mu", "total_time"):
        if getattr(args, key) is not None:
            scene[key] = getattr(args, key)
    out = (args.out or HERE / "work" / args.scene).resolve()

    dx = args.dx
    word = text_mask(args.text, round(args.cap_height / dx))
    side = round(args.side / dx)
    above = round(scene.get("above", args.side) / dx)
    below = round(scene.get("below", args.side) / dx)
    mask = np.pad(word, ((above, below), (side, side)))
    nrows, ncols = mask.shape
    # ESRI grids list the northernmost row first, like the image, so the text reads upright and
    # "down the screen" is south, decreasing y. Cell-centre coordinates:
    y = (nrows - 0.5 - np.arange(nrows)) * dx
    word_top, word_bottom = y[above], y[above + word.shape[0] - 1]

    tan = math.tan(math.radians(scene["slope"]))
    if args.scene == "floor":
        # the slope bends smoothly (softplus) into a flat floor a few metres below the word
        floor_y = word_bottom - 6.0
        z_row = tan * args.bend * np.logaddexp(0.0, (y - floor_y) / args.bend)
    else:
        z_row = tan * (y - y[-1])  # one plane (flat for "plan"); slope < 0 falls to the top
        z_row -= z_row.min()
    terrain = np.repeat(z_row[:, None], ncols, axis=1)
    release = np.where(mask, args.depth, 0.0)

    out.mkdir(parents=True, exist_ok=True)
    write_asc(out / "terrain.asc", terrain, dx)
    write_asc(out / "release.asc", release, dx)

    cols = np.flatnonzero(mask.any(axis=0))
    margin = 4.0
    view = {
        "x0": cols[0] * dx - margin,
        "x1": (cols[-1] + 1) * dx + margin,
        "y0": word_bottom - scene["view_below"],
        "y1": word_top + margin,
    }
    (out / "view.json").write_text(json.dumps(view, indent=2), encoding="utf-8")

    # Slab-Pro reads every key, so start from the interface's complete default configuration.
    sys.path.insert(0, str(args.slab_pro_src / "interface"))
    from slab_pro_interface.schema import default_simconfig

    config = default_simconfig()
    config["project_dir"] = str(out / "output")
    config["sim_name"] = "intro"
    config["physical_params"]["rho"] = 1600.0
    config["time_params"].update(output_dt=args.output_dt, total_time=scene["total_time"])
    config["numerical_params"].update(dt_max=0.01, sampling_method="POISSON", seed=3)
    config["friction"]["type"] = "COULOMB"
    config["friction"]["coulomb"]["mu"] = scene["mu"]
    config["terrain"]["type"] = "DEM"
    config["terrain"]["dem"]["filepath"] = str(out / "terrain.asc")
    config["release"]["type"] = "RASTER"
    config["release"]["raster"].update(filepath=str(out / "release.asc"), h_min=args.depth / 2)
    (out / "simConfig.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(
        f"{out}: {args.scene}, {ncols} x {nrows} cells of {dx} m, slope {scene['slope']} deg, "
        f"mu {scene['mu']}, {scene['total_time']} s"
    )


if __name__ == "__main__":
    main()
