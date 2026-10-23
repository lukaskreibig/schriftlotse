"""SchriftLotse: lokale Erkennung und Suche für historische Dokumente."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("schriftlotse")
except PackageNotFoundError:  # pragma: no cover - source checkout
    __version__ = "0.2.0"

__all__ = ["__version__"]
