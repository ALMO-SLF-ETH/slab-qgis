# Intro animation

The plugin's intro, `slab_qgis/resources/intro.gif`: grains rain down from above and settle into
the word **Slab-QGIS**. It is a Slab-Pro simulation of the word collapsing as a granular flow,
under gravity pulling *up*, played backwards. The plugin plays it over QGIS whenever the user
opens the panel (`slab_qgis/panels/intro.py`).

| Scene | What happens | File |
|---|---|---|
| `rise` (default) | the letters slide up a 45° slope and out of the view at the top; rendered backwards | `slab_qgis/resources/intro.gif` |
| `floor` | the letters slide down a 40° slope and pile up on a flat floor below | `work/slab-qgis-intro-floor.gif` |
| `fall` | the letters slide down a 45° slope and out of the view at the bottom | `work/slab-qgis-intro-fall.gif` |
| `plan` | the letters slump outwards on a flat plane, seen from above | `work/slab-qgis-intro-plan.gif` |

## How it works

Slab-Pro is depth-averaged: it computes a flow over a terrain surface, seen from above. To make
the letters move *along the screen*, as if they stood upright, the terrain is a slope falling
towards the bottom of the image (towards the top for `rise`), and the word is the release:

1. `make_inputs.py` draws the text (DejaVu Sans Bold, shipped with matplotlib) into a release
   raster (3 m deep inside the letters, 0.5 m cells, letters 30 m tall), builds the terrain for
   the scene, and writes a Slab-Pro configuration (RASTER release, Coulomb friction, Poisson
   particle sampling, a frame every 0.05 s) plus the view the animation shows (`view.json`).
2. Slab-Pro runs it (a few seconds) and writes particle snapshots (`particles/*.ply`).
3. `render_gif.py` draws every particle as a grain, coloured by a palette gradient across the
   view (each letter its own hue; `qgis`: QGIS greens into amber and orange, `aurora`: teal to
   magenta, `sand`), lighter where the flow is thin, the thinnest fading out. It downsamples and
   writes the GIF at 2x speed and 12 fps with 32 colours, holding the first and last frame, to
   play once (`--forever` loops it); `--reverse` plays it backwards, so `rise` ends on the word.

## Build

Needs Python with numpy, Pillow and matplotlib, a Slab-Pro checkout for the complete default
configuration in its `interface/` (by default the `Slab-Pro-dev` checkout next to this
repository; `make_inputs.py --slab-pro-src`), and a Slab-Pro binary, e.g. from a release package:

```bash
python make_all.py --slab-pro <package>/bin/slab-pro.exe   # rebuilds slab_qgis/resources/intro.gif
python make_all.py --slab-pro ... floor          # another scene, into work/
python make_inputs.py --scene floor --mu 0.5     # or tune a step by hand (see --help)
python render_gif.py --run work/rise --reverse --palette aurora --out work/aurora.gif
```

`work/` holds the inputs and Slab-Pro's output; it is ignored by git and can be deleted.

## Tuning notes

- Friction and slope set the character: `floor` uses mu 0.55 on 40° (slides, then stops on the
  floor), `fall` and `rise` mu 0.3 on 45° (keep accelerating).
- File size grows with the number of frames (`--fps`, `--speed`) and the width; the grains change
  every frame, so GIF compression helps little. At 480 px wide: rise ~410 KB, floor ~840 KB.
- `--fade` (default 0.4) hides grains thinner than 40 % of the initial depth: the thin film left
  behind on the slope would otherwise blur the shapes.
