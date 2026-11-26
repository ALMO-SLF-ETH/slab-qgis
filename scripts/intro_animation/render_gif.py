"""Render Slab-Pro's particle snapshots of the collapse into an animated GIF.

    python render_gif.py --run work/<scene> [--reverse] [--palette qgis] [--out FILE.gif]

Each particle is drawn as a grain on a supersampled canvas that is then downscaled, so the grains
read as a granular surface. A grain's colour comes from a palette gradient across the view (so
each letter gets its own hue); thin grains are lighter and the thinnest fade out, so the film
left behind by the flow does not blur the shapes.

The animation holds the first frame, plays the flow, and holds the last frame. With ``--reverse``
it plays backwards: for the ``rise`` scene, grains rain down from above and settle into the word.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

#: Palettes: background and the colour stops of the gradient across the view (left to right).
PALETTES = {
    # the QGIS greens running into warm amber and orange
    "qgis": ((14, 22, 30), [(88, 150, 50), (147, 176, 35), (240, 180, 41), (238, 121, 19)]),
    # teal, sky blue, violet, magenta
    "aurora": ((11, 16, 33), [(46, 196, 182), (76, 201, 240), (123, 97, 255), (241, 91, 181)]),
    # the first version's sand (thick = dark)
    "sand": ((28, 35, 48), [(226, 180, 110), (190, 128, 62), (190, 128, 62), (226, 180, 110)]),
}


def read_ply(path: Path) -> dict[str, np.ndarray]:
    """The vertex properties of a binary little-endian PLY file written by Slab-Pro."""
    with path.open("rb") as f:
        names: list[str] = []
        types: list[str] = []
        count = 0
        while (line := f.readline().decode("ascii").strip()) != "end_header":
            parts = line.split()
            if parts[:2] == ["element", "vertex"]:
                count = int(parts[2])
            elif parts[0] == "property":
                types.append(
                    {"float": "<f4", "double": "<f8", "int": "<i4", "uint": "<u4"}[parts[1]]
                )
                names.append(parts[2])
            elif parts[0] == "format":
                assert parts[1] == "binary_little_endian", parts
        data = np.fromfile(f, dtype=np.dtype(list(zip(names, types, strict=True))), count=count)
    return {n: data[n].astype(float) for n in names}


def gradient(stops: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Colours along ``stops`` for t in [0, 1]."""
    t = np.clip(t, 0.0, 1.0) * (len(stops) - 1)
    i = np.minimum(t.astype(int), len(stops) - 2)
    f = (t - i)[:, None]
    return stops[i] * (1 - f) + stops[i + 1] * f


def render(
    frame: dict[str, np.ndarray],
    bounds: tuple[float, float, float, float],
    width: int,
    palette: str,
    h_ref: float,
    fade: float,
    ss: int = 3,
) -> Image.Image:
    background, stops = PALETTES[palette]
    bg = np.array(background, dtype=float)
    stops_arr = np.array(stops, dtype=float)
    x0, x1, y0, y1 = bounds
    w = width * ss
    h = round(w * (y1 - y0) / (x1 - x0))
    canvas = np.empty((h, w, 3), dtype=float)
    canvas[:] = bg
    px = ((frame["x"] - x0) / (x1 - x0) * (w - 1)).round().astype(int)
    py = ((y1 - frame["y"]) / (y1 - y0) * (h - 1)).round().astype(int)
    inside = (px >= 0) & (px < w) & (py >= 0) & (py < h)  # grains outside the view are not drawn
    order = np.flatnonzero(inside)[np.argsort(frame["h"][inside])]  # thick grains on top
    px, py, hh = px[order], py[order], frame["h"][order]

    colours = gradient(stops_arr, px / (w - 1))
    thickness = np.clip(hh / h_ref, 0.0, 1.0)[:, None]
    colours = colours + (255.0 - colours) * 0.35 * (1 - thickness)  # thin grains lighter
    alpha = np.clip(hh / (fade * h_ref), 0.0, 1.0)[:, None]  # the thinnest fade out
    colours = colours * alpha + bg * (1 - alpha)
    for dx in range(-1, 2):  # 3x3 grains on the supersampled canvas
        for dy in range(-1, 2):
            qx, qy = np.clip(px + dx, 0, w - 1), np.clip(py + dy, 0, h - 1)
            canvas[qy, qx] = colours
    img = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8))
    return img.resize((width, round(h / ss)), Image.Resampling.LANCZOS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--run",
        type=Path,
        default=Path(__file__).resolve().parent / "work" / "rise",
        help="make_inputs --out",
    )
    parser.add_argument("--out", type=Path, help="GIF path (default: slab-qgis-intro-<run>.gif)")
    parser.add_argument("--reverse", action="store_true", help="play the flow backwards")
    parser.add_argument("--palette", choices=sorted(PALETTES), default="qgis")
    parser.add_argument("--width", type=int, default=480, help="GIF width (px)")
    parser.add_argument("--fps", type=float, default=12.0)
    parser.add_argument("--speed", type=float, default=2.0, help="playback speed vs. real time")
    parser.add_argument("--hold-start", type=float, default=0.5, help="seconds on the first frame")
    parser.add_argument("--hold-end", type=float, default=1.0, help="seconds on the last frame")
    parser.add_argument("--sim-dt", type=float, default=0.05, help="output_dt of the run (s)")
    parser.add_argument("--colors", type=int, default=32, help="GIF palette size")
    parser.add_argument("--forever", action="store_true", help="loop the GIF (default: play once)")
    parser.add_argument(
        "--fade",
        type=float,
        default=0.4,
        help="grains thinner than this fraction of the initial depth fade out",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=6.0,
        help="crop margin around the flow, without view.json (m)",
    )
    args = parser.parse_args()

    out = args.out or Path(
        f"slab-qgis-intro-{args.run.name}{'-reversed' if args.reverse else ''}.gif"
    )
    files = sorted((args.run / "output" / "intro" / "particles").glob("particles_frame_*.ply"))
    frames = [read_ply(p) for p in files]
    view_file = args.run / "view.json"
    if view_file.exists():  # the fixed view make_inputs.py chose for the scene
        v = json.loads(view_file.read_text(encoding="utf-8"))
        bounds = (v["x0"], v["x1"], v["y0"], v["y1"])
    else:  # everything the flow reaches
        xs = np.concatenate([f["x"] for f in frames])
        ys = np.concatenate([f["y"] for f in frames])
        bounds = (
            xs.min() - args.margin,
            xs.max() + args.margin,
            ys.min() - args.margin,
            ys.max() + args.margin,
        )
    h_ref = float(np.percentile(frames[0]["h"], 95))

    # sample simulation frames at the playback rate
    step = args.speed / args.fps / args.sim_dt
    picks = np.arange(0, len(frames) - 1e-9, step).round().astype(int)
    picks = np.minimum(picks, len(frames) - 1)
    images = [render(frames[i], bounds, args.width, args.palette, h_ref, args.fade) for i in picks]
    # drop the tail once nothing changes visibly any more (settled, or everything left the view)
    last = len(images)
    for k in range(len(images) - 1, 0, -1):
        diff = np.abs(np.asarray(images[k], float) - np.asarray(images[k - 1], float)).mean()
        if diff > 0.15:
            last = k + 1
            break
    images = images[:last]
    if args.reverse:
        images.reverse()
    frame_ms = round(1000 / args.fps)
    durations = [frame_ms] * len(images)
    durations[0] = round(args.hold_start * 1000)
    durations[-1] = round(args.hold_end * 1000)

    # one palette for all frames, from a strip of the first, middle and last frame
    samples = [images[0], images[len(images) // 2], images[-1]]
    strip = Image.new("RGB", (samples[0].width, samples[0].height * 3))
    for k, im in enumerate(samples):
        strip.paste(im, (0, k * im.height))
    palette_src = strip.quantize(colors=args.colors, method=Image.Quantize.MEDIANCUT)
    gif = [im.quantize(palette=palette_src, dither=Image.Dither.NONE) for im in images]
    gif[0].save(
        out,
        save_all=True,
        append_images=gif[1:],
        duration=durations,
        **({"loop": 0} if args.forever else {}),  # without a loop count, it plays once
        optimize=True,
        disposal=1,
    )
    size_kb = out.stat().st_size / 1024
    total = sum(durations) / 1000
    print(
        f"{out}: {len(gif)} frames, {gif[0].size[0]}x{gif[0].size[1]} px, "
        f"{total:.1f} s, {size_kb:.0f} KB"
    )


if __name__ == "__main__":
    main()
