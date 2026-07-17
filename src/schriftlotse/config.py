from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import PlatformDirs


@dataclass(frozen=True, slots=True)
class AppPaths:
    data: Path
    cache: Path
    models: Path
    output: Path
    database: Path
    settings: Path

    @classmethod
    def default(cls) -> AppPaths:
        dirs = PlatformDirs("SchriftLotse", "SchriftLotse")
        data = Path(dirs.user_data_dir)
        cache = Path(dirs.user_cache_dir)
        output = Path.home() / "Documents" / "SchriftLotse"
        return cls(
            data=data,
            cache=cache,
            models=cache / "models",
            output=output,
            database=data / "schriftlotse.sqlite3",
            settings=data / "settings.json",
        )

    def ensure(self) -> None:
        for path in (self.data, self.cache, self.models, self.output):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class Settings:
    advanced_models: bool = True
    semantic_search: bool = True
    cloud_budget_usd: float = 1.0
    output_dir: str | None = None
    tesseract_command: str = "tesseract"

    @classmethod
    def load(cls, paths: AppPaths) -> Settings:
        if not paths.settings.exists():
            return cls()
        raw: dict[str, Any] = json.loads(paths.settings.read_text(encoding="utf-8"))
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in raw.items() if key in allowed})

    def save(self, paths: AppPaths) -> None:
        paths.ensure()
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__}
        paths.settings.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
