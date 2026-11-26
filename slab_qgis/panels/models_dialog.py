"""Dialog to download, update and remove the models the plugin offers (``models.json``).

Each offered model is a row with its installed and latest version. Network work (reading
releases, downloading a package) runs in a background :class:`QgsTask`, so QGIS stays
responsive; the dialog only updates its rows when the task reports back.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from qgis.core import QgsApplication, QgsTask
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from slab_qgis.catalog import core, net, service
from slab_qgis.catalog.core import CatalogModel, Fetch

#: Runs ``work`` off the GUI thread and calls ``done(result, error)`` on the GUI thread.
Runner = Callable[[str, Callable[[], Any], Callable[[Any, Exception | None], None]], None]

COLUMNS = ("Model", "Installed", "Latest", "Status")


class _CallableTask(QgsTask):
    def __init__(self, title: str, work: Callable[[], Any], done: Callable[..., None]) -> None:
        super().__init__(title, QgsTask.Flag.CanCancel)
        self._work, self._done = work, done
        self._result: Any = None
        self._error: Exception | None = None

    def run(self) -> bool:
        try:
            self._result = self._work()
        except Exception as exc:  # reported in finished(), on the GUI thread
            self._error = exc
            return False
        return True

    def finished(self, _ok: bool) -> None:
        self._done(self._result, self._error)


_running: set[QgsTask] = set()  # keep Python references until QGIS has finished each task


def task_runner(title: str, work: Callable[[], Any], done: Callable[..., None]) -> None:
    def finish(result: Any, error: Exception | None) -> None:
        _running.discard(task)
        done(result, error)

    task = _CallableTask(title, work, finish)
    _running.add(task)
    QgsApplication.taskManager().addTask(task)


class ModelsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        fetch: Fetch = net.fetch,
        download: Callable[..., Any] = net.download,
        runner: Runner = task_runner,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Slab-QGIS — models")
        self.resize(640, 300)
        self._fetch, self._download, self._run = fetch, download, runner
        self._models: list[CatalogModel] = service.registry()  # bundled/cached; refreshed below
        self._latest: dict[str, str] = {}
        self._status: dict[str, str] = {}
        #: Called with the new manifest path after an install, so the panel can select it.
        self.installed_callback: Callable[[str], None] | None = None

        root = QVBoxLayout(self)
        intro = QLabel(
            "Models published by ALMO-SLF-ETH. Installing downloads this platform's package of "
            "the latest release and adds it to the model list, ready to run."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        root.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.install_button = QPushButton("Install")
        self.install_button.clicked.connect(self._on_install)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._on_remove)
        self.refresh_button = QPushButton("Check for updates")
        self.refresh_button.clicked.connect(self.refresh)
        for b in (self.install_button, self.remove_button, self.refresh_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self.daily_check = QCheckBox("Check for model updates once a day")
        self.daily_check.setChecked(service.updates_enabled())
        self.daily_check.toggled.connect(service.set_updates_enabled)
        root.addWidget(self.daily_check)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        root.addWidget(close)

        self._fill()
        self.refresh()

    # -- table -------------------------------------------------------------------------------
    def _fill(self) -> None:
        have = service.installed()
        selected = self._selected_id()
        self.table.setRowCount(len(self._models))
        for row, model in enumerate(self._models):
            current = have[model.id].version if model.id in have else ""
            latest = self._latest.get(model.id, "")
            status = self._status.get(model.id) or self._default_status(current, latest)
            for col, text in enumerate((model.name, current or "—", latest or "…", status)):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, model.id)
                if col == 0 and model.description:
                    item.setToolTip(model.description)
                self.table.setItem(row, col, item)
            if model.id == selected:
                self.table.selectRow(row)
        if selected is None and self._models:
            self.table.selectRow(0)
        self._update_buttons()

    @staticmethod
    def _default_status(current: str, latest: str) -> str:
        if not latest:
            return "not installed" if not current else "installed"
        if not current:
            return "available"
        return "update available" if core.is_newer(latest, current) else "up to date"

    def _selected_id(self) -> str | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _selected(self) -> CatalogModel | None:
        mid = self._selected_id()
        return next((m for m in self._models if m.id == mid), None)

    def _update_buttons(self) -> None:
        model = self._selected()
        have = service.installed()
        current = have[model.id].version if model and model.id in have else ""
        latest = self._latest.get(model.id, "") if model else ""
        self.install_button.setText(
            "Update"
            if current and latest and core.is_newer(latest, current)
            else "Reinstall"
            if current
            else "Install"
        )
        self.install_button.setEnabled(model is not None and bool(latest))
        self.remove_button.setEnabled(bool(current))

    # -- actions -----------------------------------------------------------------------------
    def refresh(self) -> None:
        """Re-read the registry and each model's latest release in the background."""
        self.refresh_button.setEnabled(False)

        def work() -> tuple[list[CatalogModel], dict[str, str], dict[str, str]]:
            models = service.registry(self._fetch)
            latest: dict[str, str] = {}
            errors: dict[str, str] = {}
            for m in models:
                try:
                    release = core.latest_release(m.repository, self._fetch)
                    core.pick_asset(m, release, core.platform_key())  # offered for this machine?
                    latest[m.id] = release.version
                except core.CatalogError as exc:
                    errors[m.id] = str(exc)
            return models, latest, errors

        def done(result: Any, error: Exception | None) -> None:
            self.refresh_button.setEnabled(True)
            if error is not None:
                self._status = {m.id: f"could not check: {error}" for m in self._models}
            else:
                self._models, self._latest, errors = result
                self._status = {mid: f"not available: {msg}" for mid, msg in errors.items()}
            self._fill()

        self._run("Checking for Slab-QGIS models", work, done)

    def _on_install(self) -> None:
        model = self._selected()
        if model is None:
            return
        self._status[model.id] = "downloading…"
        self._fill()
        self.install_button.setEnabled(False)

        def done(result: Any, error: Exception | None) -> None:
            if error is not None:
                self._status[model.id] = f"install failed: {error}"
            else:
                note = f"installed {result.version}"
                if result.replaced:
                    note += f" (replaced {result.replaced})"
                if result.cleared_settings:
                    note += "; reset " + ", ".join(result.cleared_settings) + " to the package's"
                self._status[model.id] = note
                if self.installed_callback is not None:
                    self.installed_callback(str(result.manifest))
            self._fill()

        self._run(
            f"Installing {model.name}",
            lambda: service.install(model, self._fetch, self._download),
            done,
        )

    def _on_remove(self) -> None:
        model = self._selected()
        if model is None:
            return
        service.uninstall(model.id)
        self._status[model.id] = "removed"
        if self.installed_callback is not None:
            self.installed_callback("")
        self._fill()
