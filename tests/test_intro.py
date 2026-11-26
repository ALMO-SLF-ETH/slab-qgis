"""The intro animation: it plays over QGIS when the user opens the panel, and goes away again."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QWidget
from qgis.testing.mocked import get_iface
from slab_qgis import classFactory
from slab_qgis.panels import intro


@pytest.fixture
def host(qgis_app: QgsApplication) -> Iterator[QWidget]:
    intro.set_enabled(True)
    window = QWidget()
    window.resize(800, 600)
    window.show()
    yield window
    window.close()
    window.deleteLater()


def _closed(widget: QWidget) -> list[bool]:
    flag: list[bool] = []
    widget.destroyed.connect(lambda *_: flag.append(True))
    return flag


def test_the_bundled_gif_is_a_valid_animation(host: QWidget) -> None:
    splash = intro.IntroSplash(host)
    assert intro.INTRO_GIF.is_file()
    assert splash.movie.isValid()
    assert splash.movie.frameCount() > 10
    splash.deleteLater()


def test_intro_plays_over_the_window_without_taking_focus(host: QWidget) -> None:
    splash = intro.show_intro(host)
    assert splash is not None and splash.isVisible()
    assert splash.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert splash.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    # centred over the host window
    centre = host.mapToGlobal(host.rect().center())
    assert abs(splash.geometry().center().x() - centre.x()) <= 1
    assert abs(splash.geometry().center().y() - centre.y()) <= 1
    closed = _closed(splash)
    QTest.mouseClick(splash, Qt.MouseButton.LeftButton)  # a click dismisses it
    QTest.qWait(50)
    assert closed


def test_intro_fades_out_after_the_last_frame(host: QWidget) -> None:
    splash = intro.show_intro(host)
    assert splash is not None
    closed = _closed(splash)
    splash.movie.jumpToFrame(splash.movie.frameCount() - 1)  # held for its delay, then fades
    QTest.qWait(3000)
    assert closed


def test_a_reopen_replaces_a_running_intro(host: QWidget) -> None:
    first = intro.show_intro(host)
    assert first is not None
    closed = _closed(first)
    second = intro.show_intro(host)
    QTest.qWait(50)
    assert closed and second is not None and second.isVisible()
    second.close()


def test_intro_can_be_turned_off(host: QWidget) -> None:
    intro.set_enabled(False)
    try:
        assert intro.show_intro(host) is None
    finally:
        intro.set_enabled(True)


def test_only_a_user_opening_the_panel_plays_the_intro(
    qgis_app: QgsApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(intro, "show_intro", calls.append)
    plugin = classFactory(get_iface())
    plugin.initGui()
    try:
        plugin._action.setChecked(True)  # e.g. QGIS restoring the panel at start-up
        plugin._action.setChecked(False)
        assert calls == []
        plugin._action.trigger()  # the user clicks the toolbar button: opens the panel
        assert len(calls) == 1
        plugin._action.trigger()  # ... and closes it again: no intro
        assert len(calls) == 1
    finally:
        plugin.unload()
