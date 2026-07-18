from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import PlatformDirs

MACOS_EXECUTABLE_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/usr/bin",
    "/bin",
)


def resolve_executable(command: str) -> str | None:
    """Resolve CLI tools reliably, including Finder-launched macOS apps."""
    value = command.strip()
    if not value:
        return None
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        return None
    resolved = shutil.which(value)
    if resolved:
        return resolved
    for directory in MACOS_EXECUTABLE_DIRS:
        candidate = Path(directory) / value
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


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
    default_quality: str = "beste_lokale_qualitaet"
    default_script: str = "auto"
    openrouter_profile: str = "quality"
    show_preprocessing: bool = True

    @classmethod
    def load(cls, paths: AppPaths) -> Settings:
        if not paths.settings.exists():
            return cls()
        try:
            raw: dict[str, Any] = json.loads(paths.settings.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            # A damaged preferences file must never prevent access to the local
            # archive. The next explicit save replaces it atomically.
            return cls()
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in raw.items() if key in allowed})

    def save(self, paths: AppPaths) -> None:
        paths.ensure()
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__}
        encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            prefix="settings-", suffix=".json", dir=paths.settings.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, paths.settings)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
