from __future__ import annotations

from pathlib import Path

import pytest

from schriftlotse.config import AppPaths


@pytest.fixture
def app_paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        data=tmp_path / "data",
        cache=tmp_path / "cache",
        models=tmp_path / "cache" / "models",
        output=tmp_path / "output",
        database=tmp_path / "data" / "test.sqlite3",
        settings=tmp_path / "data" / "settings.json",
    )
