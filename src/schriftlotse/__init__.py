"""SchriftLotse: lokale Erkennung und Suche für historische Dokumente."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("schriftlotse")
except PackageNotFoundError:  # pragma: no cover - source checkout
    __version__ = "0.1.2"

__all__ = ["__version__"]
