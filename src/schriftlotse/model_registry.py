from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
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
    processor_source: str | None = None
    processor_revision: str | None = None
    optional: bool = True
    estimated_size_mb: int = 0
    requires_acceptance: bool = False
    quantization_bits: int | None = None


MODELS: dict[str, ModelSpec] = {
    "party-v4": ModelSpec(
        key="party-v4",
        name="Party v4",
        purpose="Allgemeine Ganzseiten-Zweitlesung auf stabilem CPU-Pfad",
        license="Apache-2.0",
        source="https://zenodo.org/records/20642057",
        kind="file",
        url="https://zenodo.org/api/records/20642057/files/model.safetensors/content",
        checksum="md5:cf165e67061d492b72f600a6a72b7c61",
        filename="model.safetensors",
        optional=True,
        estimated_size_mb=497,
    ),
    "orli": ModelSpec(
        key="orli",
        name="Orli",
        purpose="Nicht empfohlen: auf Apple Silicon ohne belegten Zusatznutzen",
        license="Apache-2.0",
        source="https://zenodo.org/records/20558179",
        kind="file",
        url="https://zenodo.org/api/records/20558179/files/orli_base.safetensors/content",
        checksum="md5:a9a6b0caf497203e758dbd4fc624af10",
        filename="orli_base.safetensors",
        optional=True,
        estimated_size_mb=567,
    ),
    "trocr-kurrent-19": ModelSpec(
        key="trocr-kurrent-19",
        name="TrOCR Kurrent 19. Jh.",
        purpose="Kurrent des 19. Jahrhunderts",
        license="MIT",
        source="https://huggingface.co/dh-unibe/trocr-kurrent",
        kind="huggingface",
        revision="dd026dc68fd784f214bccd932081b8048e5bfba0",
        processor_source="https://huggingface.co/microsoft/trocr-base-handwritten",
        processor_revision="eaacaf452b06415df8f10bb6fad3a4c11e609406",
        optional=False,
        estimated_size_mb=1200,
    ),
    "trocr-kurrent-early": ModelSpec(
        key="trocr-kurrent-early",
        name="TrOCR Kurrent 16.–18. Jh.",
        purpose="Frühe deutsche Kurrentschrift",
        license="MIT",
        source="https://huggingface.co/dh-unibe/trocr-kurrent-XVI-XVII",
        kind="huggingface",
        revision="eaedace4a032ef319db19351342f60019e3daca6",
        processor_source="https://huggingface.co/microsoft/trocr-base-handwritten",
        processor_revision="eaacaf452b06415df8f10bb6fad3a4c11e609406",
        estimated_size_mb=1200,
    ),
    "trocr-modern": ModelSpec(
        key="trocr-modern",
        name="TrOCR deutsche Handschrift",
        purpose="Neuere lateinische Handschrift",
        license="AFL-3.0",
        source="https://huggingface.co/fhswf/TrOCR_german_handwritten",
        kind="huggingface",
        revision="f43d8831af99105e9dbb718fcfc8373c0010174d",
        processor_source="https://huggingface.co/microsoft/trocr-base-handwritten",
        processor_revision="eaacaf452b06415df8f10bb6fad3a4c11e609406",
    ),
    "trocr-medieval": ModelSpec(
        key="trocr-medieval",
        name="TrOCR mittelalterliche Schrift",
        purpose="Experimentelle Erkennung vor 1500",
        license="MIT",
        source="https://huggingface.co/dh-unibe/trocr-medieval-escriptmask",
        kind="huggingface",
        revision="bd7124a363ca38b868fdeb4b712f02bef29e6c6e",
        processor_source="https://huggingface.co/microsoft/trocr-base-handwritten",
        processor_revision="eaacaf452b06415df8f10bb6fad3a4c11e609406",
    ),
    "qwen-embed": ModelSpec(
        key="qwen-embed",
        name="Qwen3 Embedding 0.6B",
        purpose="Lokale semantische Archivsuche",
        license="Apache-2.0",
        source="https://huggingface.co/Qwen/Qwen3-Embedding-0.6B",
        kind="huggingface",
        revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        estimated_size_mb=1200,
    ),
    "ub-german-handwriting": ModelSpec(
        key="ub-german-handwriting",
        name="UB Mannheim deutsche Handschrift",
        purpose="Kleiner allgemeiner Kraken-Zweitleser",
        license="CC-BY-SA-4.0",
        source="https://zenodo.org/records/7933463",
        kind="file",
        url=("https://zenodo.org/api/records/7933463/files/german_handwriting.mlmodel/content"),
        checksum="md5:6c41ae2cc0a990f018bf549490041a06",
        filename="german_handwriting.mlmodel",
        estimated_size_mb=16,
    ),
    "churro-mlx-8bit": ModelSpec(
        key="churro-mlx-8bit",
        name="CHURRO 3B MLX 8-Bit",
        purpose="Standard-Ganzseitenleser für historische Dokumente auf Apple Silicon",
        license="Qwen Research License",
        source="https://huggingface.co/stanford-oval/churro-3B",
        kind="mlx",
        revision="ca2150ea465d5a3d67818c50e234b9422619c75d",
        estimated_size_mb=4400,
        requires_acceptance=True,
        quantization_bits=8,
    ),
}


class ModelManager:
    _PROCESSOR_FILES = (
        "merges.txt",
        "preprocessor_config.json",
        "special_tokens_map.json",
        "tokenizer_config.json",
        "vocab.json",
    )

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.paths.ensure()

    def path_for(self, key: str) -> Path:
        spec = MODELS[key]
        directory = self.paths.models / key
        return directory / spec.filename if spec.filename else directory

    def processor_path_for(self, key: str) -> Path:
        return self.path_for(key) / "processor"

    def is_installed(self, key: str) -> bool:
        path = self.path_for(key)
        if MODELS[key].kind in {"huggingface", "mlx"}:
            if not (path / ".schriftlotse-model.json").exists():
                return False
            if MODELS[key].processor_source:
                processor = self.processor_path_for(key)
                return all((processor / filename).exists() for filename in self._PROCESSOR_FILES)
            return True
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
                "estimated_size_mb": spec.estimated_size_mb,
                "requires_acceptance": spec.requires_acceptance,
            }
            for spec in MODELS.values()
        ]

    def install(self, key: str, *, accept_license: bool = False) -> Path:
        spec = MODELS[key]
        if spec.requires_acceptance and not accept_license:
            raise ValueError(f"Die Lizenz für {spec.name} muss zuerst bestätigt werden")
        destination = self.path_for(key)
        if self.is_installed(key):
            return destination
        required_mb = spec.estimated_size_mb * (3 if spec.kind == "mlx" else 2)
        free_mb = shutil.disk_usage(self.paths.models).free // (1024 * 1024)
        if required_mb and free_mb < required_mb:
            raise RuntimeError(
                f"Zu wenig freier Speicher: etwa {required_mb} MB erforderlich, "
                f"{free_mb} MB verfügbar"
            )
        if spec.kind == "file":
            return self._download_file(spec, destination)
        if spec.kind == "huggingface":
            return self._download_huggingface(spec, destination)
        if spec.kind == "mlx":
            return self._install_mlx(spec, destination)
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

    def _install_mlx(self, spec: ModelSpec, destination: Path) -> Path:
        try:
            import mlx_vlm  # noqa: F401
            from huggingface_hub import snapshot_download
        except ImportError as error:
            raise RuntimeError(
                "MLX-Modellpaket fehlt: uv sync --extra mlx --extra models"
            ) from error
        source = self.paths.cache / "conversion" / f"{spec.key}-source"
        source.mkdir(parents=True, exist_ok=True)
        repo_id = spec.source.removeprefix("https://huggingface.co/")
        snapshot_download(repo_id=repo_id, revision=spec.revision, local_dir=source)
        destination.mkdir(parents=True, exist_ok=True)
        quantization_bits = spec.quantization_bits or 8
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "mlx_vlm.convert",
                "--hf-path",
                str(source),
                "--mlx-path",
                str(destination),
                "-q",
                "--q-bits",
                str(quantization_bits),
            ],
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(process.stderr.strip() or "CHURRO-Konvertierung fehlgeschlagen")
        (destination / ".schriftlotse-model.json").write_text(
            json.dumps(
                {
                    "repo_id": repo_id,
                    "revision": spec.revision,
                    "license": spec.license,
                    "quantization": f"mlx-{quantization_bits}bit",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        shutil.rmtree(source, ignore_errors=True)
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
        if spec.processor_source:
            if not spec.processor_revision:
                raise ValueError(f"Keine feste Prozessor-Revision für {spec.name}")
            processor_repo = spec.processor_source.removeprefix("https://huggingface.co/")
            snapshot_download(
                repo_id=processor_repo,
                revision=spec.processor_revision,
                local_dir=destination / "processor",
                allow_patterns=list(self._PROCESSOR_FILES),
            )
        marker = destination / ".schriftlotse-model.json"
        marker.write_text(
            json.dumps(
                {
                    "repo_id": repo_id,
                    "revision": spec.revision,
                    "license": spec.license,
                    "processor_repo_id": (
                        spec.processor_source.removeprefix("https://huggingface.co/")
                        if spec.processor_source
                        else None
                    ),
                    "processor_revision": spec.processor_revision,
                },
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
