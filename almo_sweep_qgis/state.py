"""Shared, observable study state passed between the plugin's pages.

One :class:`StudyState` instance is owned by the panel and handed to each page. Pages read
the current selection from it and write their edits back; they react to changes by connecting
to its signals. Keeping this in one place (rather than threading data through constructors)
lets pages stay decoupled and independently testable.
"""

from __future__ import annotations

from typing import Any

from qgis.PyQt.QtCore import QObject, pyqtSignal

from almo_sweep_qgis.sweep_plan import default_sweep


class StudyState(QObject):
    """The mutable state of the study the user is assembling."""

    #: Emitted with the new manifest (an ``almo_sweep`` ModelManifest) or ``None`` when it is
    #: cleared. ``object`` payload keeps this Qt5/Qt6-safe and avoids importing the engine here.
    manifestChanged = pyqtSignal(object)

    #: Emitted as ``(input_name, new_layer_id)`` when a page swaps an input's layer for another
    #: (e.g. the Run page resamples a raster at launch), so the Parameters page re-syncs its combo.
    inputLayerReplaced = pyqtSignal(str, object)

    #: Emitted whenever the sweep config (:attr:`sweep`) changes. The Parameters page authors the
    #: whole sweep (enable toggle, per-parameter selection, and the scheme/sampling method) and
    #: emits this so other pages (e.g. a future results view) can react.
    sweepChanged = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._manifest: Any | None = None
        self._manifest_path: str | None = None
        #: input name -> selected map-layer id (or ``None`` if unset).
        self.input_layers: dict[str, str | None] = {}
        #: parameter name -> current value (seeded with manifest defaults).
        self.param_values: dict[str, Any] = {}
        #: input name -> a pending resample request {"resolution", "alg", "out_path"} armed via the
        #: "Resample to" control but not yet applied; the Run page applies these at launch.
        self.resample_requests: dict[str, dict[str, Any]] = {}
        #: The study (sweep) config authored on the Parameters page: a plain-data dict (see
        #: :func:`~almo_sweep_qgis.sweep_plan.default_sweep`). The Run page reads it at launch.
        self.sweep: dict[str, Any] = default_sweep()

    @property
    def manifest(self) -> Any | None:
        return self._manifest

    @property
    def manifest_path(self) -> str | None:
        return self._manifest_path

    def set_manifest(self, manifest: Any | None, path: str | None = None) -> None:
        """Set (or clear) the active model manifest and notify listeners."""
        self._manifest = manifest
        self._manifest_path = path
        self.input_layers = {}
        self.param_values = {}
        self.resample_requests = {}
        # A new model has different parameters, so any previous sweep selection is meaningless.
        self.sweep = default_sweep()
        self.manifestChanged.emit(manifest)
