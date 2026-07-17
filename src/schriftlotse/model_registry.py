from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from schriftlotse.config import AppPaths


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    name: str
    purpose: str
    license: str
    source: str
    kind: str
    revision: str | None = None
    url: str | None = None
    checksum: str | None = None
    filename: str | None = None
    optional: bool = True


MODELS: dict[str, ModelSpec] = {
    "party-v4": ModelSpec(
        key="party-v4",
        name="Party v4",
        purpose="Experimentelle Hochspeicher-Zweitlesung (nicht für 18-GB-Macs)",
        license="Apache-2.0",
        source="https://zenodo.org/records/20642057",
        kind="file",
        url="https://zenodo.org/api/records/20642057/files/model.safetensors/content",
        checksum="md5:cf165e67061d492b72f600a6a72b7c61",
        filename="model.safetensors",
        optional=True,
    ),
    "orli": ModelSpec(
        key="orli",
        name="Orli",
        purpose="Grundlinien und Lesereihenfolge",
        license="Apache-2.0",
        source="https://zenodo.org/records/20558179",
        kind="file",
        url="https://zenodo.org/api/records/20558179/files/orli_base.safetensors/content",
        checksum="md5:a9a6b0caf497203e758dbd4fc624af10",
        filename="orli_base.safetensors",
        optional=False,
    ),
    "trocr-kurrent-19": ModelSpec(
        key="trocr-kurrent-19",
        name="TrOCR Kurrent 19. Jh.",
        purpose="Kurrent des 19. Jahrhunderts",
        license="MIT",
        source="https://huggingface.co/dh-unibe/trocr-kurrent",
        kind="huggingface",
        revision="dd026dc68fd784f214bccd932081b8048e5bfba0",
        optional=False,
    ),
    "trocr-kurrent-early": ModelSpec(
        key="trocr-kurrent-early",
        name="TrOCR Kurrent 16.–18. Jh.",
        purpose="Frühe deutsche Kurrentschrift",
        license="MIT",
        source="https://huggingface.co/dh-unibe/trocr-kurrent-XVI-XVII",
        kind="huggingface",
        revision="eaedace4a032ef319db19351342f60019e3daca6",
    ),
    "trocr-modern": ModelSpec(
        key="trocr-modern",
        name="TrOCR deutsche Handschrift",
        purpose="Neuere lateinische Handschrift",
        license="AFL-3.0",
        source="https://huggingface.co/fhswf/TrOCR_german_handwritten",
        kind="huggingface",
        revision="f43d8831af99105e9dbb718fcfc8373c0010174d",
    ),
    "trocr-medieval": ModelSpec(
        key="trocr-medieval",
        name="TrOCR mittelalterliche Schrift",
        purpose="Experimentelle Erkennung vor 1500",
        license="MIT",
        source="https://huggingface.co/dh-unibe/trocr-medieval-escriptmask",
        kind="huggingface",
        revision="bd7124a363ca38b868fdeb4b712f02bef29e6c6e",
    ),
    "qwen-embed": ModelSpec(
        key="qwen-embed",
        name="Qwen3 Embedding 0.6B",
        purpose="Lokale semantische Archivsuche",
        license="Apache-2.0",
        source="https://huggingface.co/Qwen/Qwen3-Embedding-0.6B",
        kind="huggingface",
        revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
    ),
}


class ModelManager:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.paths.ensure()

    def path_for(self, key: str) -> Path:
        spec = MODELS[key]
        directory = self.paths.models / key
        return directory / spec.filename if spec.filename else directory

    def is_installed(self, key: str) -> bool:
        path = self.path_for(key)
        if MODELS[key].kind == "huggingface":
            return (path / ".schriftlotse-model.json").exists()
        return path.exists() and path.stat().st_size > 0

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "key": spec.key,
                "name": spec.name,
                "purpose": spec.purpose,
                "license": spec.license,
                "installed": self.is_installed(spec.key),
                "path": str(self.path_for(spec.key)),
                "source": spec.source,
            }
            for spec in MODELS.values()
        ]

    def install(self, key: str) -> Path:
        spec = MODELS[key]
        destination = self.path_for(key)
        if self.is_installed(key):
            return destination
        if spec.kind == "file":
            return self._download_file(spec, destination)
        if spec.kind == "huggingface":
            return self._download_huggingface(spec, destination)
        raise ValueError(spec.kind)

    def _download_file(self, spec: ModelSpec, destination: Path) -> Path:
        if spec.url is None:
            raise ValueError(f"Keine Download-URL für {spec.key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.md5() if (spec.checksum or "").startswith("md5:") else hashlib.sha256()
        with httpx.stream("GET", spec.url, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    handle.write(chunk)
                    digest.update(chunk)
        expected = (spec.checksum or "").split(":", 1)[-1]
        if expected and digest.hexdigest() != expected:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"Prüfsumme für {spec.name} stimmt nicht")
        temporary.replace(destination)
        return destination

    def _download_huggingface(self, spec: ModelSpec, destination: Path) -> Path:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as error:
            raise RuntimeError("Modellpaket fehlt: uv sync --extra models") from error
        destination.mkdir(parents=True, exist_ok=True)
        repo_id = spec.source.removeprefix("https://huggingface.co/")
        snapshot_download(
            repo_id=repo_id,
            revision=spec.revision,
            local_dir=destination,
            ignore_patterns=[
                "*.bin",
                "optimizer.pt",
                "scheduler.pt",
                "trainer_state.json",
                "rng_state.pth",
                "scaler.pt",
            ],
        )
        marker = destination / ".schriftlotse-model.json"
        marker.write_text(
            json.dumps(
                {"repo_id": repo_id, "revision": spec.revision, "license": spec.license},
                indent=2,
            ),
            encoding="utf-8",
        )
        return destination

    def remove(self, key: str) -> None:
        target = self.path_for(key)
        directory = target if target.is_dir() else target.parent
        if directory.resolve().parent != self.paths.models.resolve():
            raise ValueError("Ungültiges Modellverzeichnis")
        shutil.rmtree(directory, ignore_errors=True)
