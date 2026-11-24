"""Network access through QGIS's network manager, so the user's proxy settings apply.

Blocking calls: run them from a :class:`~qgis.core.QgsTask` (or another worker thread), never on
the GUI thread for anything large.
"""

from __future__ import annotations

from pathlib import Path

from qgis.core import QgsBlockingNetworkRequest
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from slab_qgis.catalog.core import CatalogError

USER_AGENT = b"slab-qgis"


def _request(url: str) -> QNetworkRequest:
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"User-Agent", USER_AGENT)
    if url.startswith("https://api.github.com/"):
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
    return request


def fetch(url: str) -> bytes:
    """GET ``url`` and return the body; raises :class:`CatalogError` on any failure."""
    blocking = QgsBlockingNetworkRequest()
    error = blocking.get(_request(url), forceRefresh=True)
    if error != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise CatalogError(f"could not download {url}: {blocking.errorMessage()}")
    return bytes(blocking.reply().content())


def download(url: str, target: Path) -> Path:
    """Download ``url`` to ``target`` (parent folders created) and return ``target``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(fetch(url))
    return target
