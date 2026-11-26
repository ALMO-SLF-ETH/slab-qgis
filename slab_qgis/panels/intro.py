"""The intro animation shown over QGIS when the user opens the Slab-QGIS panel.

``resources/intro.gif`` (made with ``scripts/intro_animation``): grains rain down and settle into
the word "Slab-QGIS" -- a Slab-Pro simulation played backwards. It plays once on a borderless
card centred on the QGIS window, holds the last frame, then fades out. It never takes the
keyboard focus, and a click dismisses it. It can be turned off with :func:`set_enabled`.
"""

from __future__ import annotations

from pathlib import Path

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from qgis.PyQt.QtGui import QMouseEvent, QMovie
from qgis.PyQt.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

INTRO_GIF = Path(__file__).resolve().parents[1] / "resources" / "intro.gif"
_ENABLED_KEY = "slab_qgis/intro/show"
FADE_MS = 450
#: Background of the card: the GIF's own background colour, so the animation has no visible edge.
CARD_COLOUR = "#0e161e"


def enabled() -> bool:
    return str(QgsSettings().value(_ENABLED_KEY, "true")).lower() in ("true", "1")


def set_enabled(on: bool) -> None:
    QgsSettings().setValue(_ENABLED_KEY, "true" if on else "false")


class IntroSplash(QWidget):
    """A borderless card that plays the intro GIF once over ``parent``, then fades out."""

    def __init__(self, parent: QWidget | None = None, gif: Path = INTRO_GIF) -> None:
        flags = (
            Qt.WindowType.Tool  # stays above the QGIS window, no taskbar entry
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)  # rounded card corners
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to close")

        card = QFrame(self)
        card.setObjectName("introCard")
        card.setStyleSheet(
            f"#introCard {{ background: {CARD_COLOUR}; border-radius: 14px;"
            " border: 1px solid rgba(255, 255, 255, 30); }"
        )
        inner = QVBoxLayout(card)
        inner.setContentsMargins(22, 18, 22, 18)
        self.label = QLabel(card)
        inner.addWidget(self.label)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.movie = QMovie(str(gif), parent=self)
        self.label.setMovie(self.movie)
        self.movie.frameChanged.connect(self._on_frame)
        self.movie.finished.connect(self._hold_then_fade)
        self._fading = False
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.setEasingCurve(QEasingCurve.Type.InQuad)
        self._fade.finished.connect(self.close)

    def play(self) -> None:
        """Centre over the parent window, show, and start the animation."""
        self.movie.jumpToFrame(0)
        self.adjustSize()
        host = self.parentWidget()
        if host is not None:
            centre = host.mapToGlobal(host.rect().center())
            self.move(centre.x() - self.width() // 2, centre.y() - self.height() // 2)
        self.show()
        self.raise_()
        self.movie.start()

    def _on_frame(self, number: int) -> None:
        # The GIF may loop; stop it on its last frame and keep that frame up for its own delay.
        if number == self.movie.frameCount() - 1:
            delay = max(self.movie.nextFrameDelay(), 0)
            self.movie.setPaused(True)
            QTimer.singleShot(delay, self._hold_then_fade)

    def _hold_then_fade(self) -> None:
        if not self._fading:
            self._fading = True
            self._fade.start()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.close()


def show_intro(parent: QWidget | None) -> IntroSplash | None:
    """Play the intro over ``parent``, unless it is turned off or the GIF is missing."""
    if not enabled() or not INTRO_GIF.is_file():
        return None
    if parent is not None:  # one at a time: a quick re-open replaces a running intro
        for running in parent.findChildren(IntroSplash):
            running.close()
    splash = IntroSplash(parent)
    if not splash.movie.isValid():
        splash.deleteLater()
        return None
    splash.play()
    return splash
