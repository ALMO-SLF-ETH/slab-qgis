"""Shared raster styling for DEMs loaded into the project."""

from __future__ import annotations

from qgis.core import QgsBilinearRasterResampler, QgsHillshadeRenderer, QgsRasterLayer


def apply_hillshade(layer: QgsRasterLayer) -> None:
    """Render a DEM raster as a shaded relief, with bilinear (2x2) display resampling.

    Bilinear resampling when zoomed in *and* out smooths the shaded relief instead of showing
    blocky nearest-neighbour pixels (the QGIS default).
    """
    renderer = QgsHillshadeRenderer(layer.dataProvider(), 1, 315.0, 45.0)
    layer.setRenderer(renderer)
    resampler = layer.resampleFilter()
    if resampler is not None:
        resampler.setZoomedInResampler(QgsBilinearRasterResampler())
        resampler.setZoomedOutResampler(QgsBilinearRasterResampler())
    layer.triggerRepaint()
