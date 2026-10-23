from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from schriftlotse.config import AppPaths, Settings


@dataclass(frozen=True, slots=True)
class ManagedFile:
    original_path: Path
    managed_path: Path
    original_name: str
    sha256: str
    size: int
    media_type: str


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class LibraryManager:
    """Owns stable originals and reproducible document assets.

    The object store makes repeat imports cheap. Human-readable document folders
    contain hard links where the file system supports them and ordinary copies
    otherwise. Originals are never modified by the OCR pipeline.
    """

    def __init__(self, paths: AppPaths, settings: Settings | None = None) -> None:
        self.paths = paths
        self.settings = settings or Settings.load(paths)
        self.root = self.settings.resolved_library(paths)
        self.objects = self.root / ".objekte"
        self.documents = self.root / "Dokumente"
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects.mkdir(parents=True, exist_ok=True)
        self.documents.mkdir(parents=True, exist_ok=True)

    def document_root(self, document_id: str) -> Path:
        return self.documents / document_id

    def originals_dir(self, document_id: str) -> Path:
        path = self.document_root(document_id) / "Originale"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def prepared_dir(self, document_id: str) -> Path:
        path = self.document_root(document_id) / "Arbeitsseiten"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def derived_dir(self, document_id: str) -> Path:
        path = self.document_root(document_id) / "Ergebnisse"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def thumbnail_path(self, document_id: str) -> Path:
        path = self.document_root(document_id) / "Vorschau.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def make_page_preview(self, document_id: str, page_index: int, image: Image.Image) -> Path:
        directory = self.document_root(document_id) / "Vorschauen"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{page_index:04d}.jpg"
        preview = ImageOps.exif_transpose(image.convert("RGB"))
        preview.thumbnail((900, 900), Image.Resampling.LANCZOS)
        preview.save(destination, "JPEG", quality=76, optimize=True)
        return destination

    def adopt_sources(self, document_id: str, sources: list[Path]) -> list[ManagedFile]:
        adopted: list[ManagedFile] = []
        destination_dir = self.originals_dir(document_id)
        used_names: set[str] = set()
        for source in sources:
            source = source.expanduser().resolve()
            if not source.is_file():
                raise FileNotFoundError(f"Quelldatei fehlt: {source}")
            digest = sha256_file(source)
            size = source.stat().st_size
            object_path = self.objects / digest[:2] / digest
            object_path.parent.mkdir(parents=True, exist_ok=True)
            if not object_path.is_file():
                self._atomic_copy(source, object_path)
                if sha256_file(object_path) != digest:
                    object_path.unlink(missing_ok=True)
                    raise OSError(f"Prüfsumme nach dem Kopieren abweichend: {source.name}")
            preferred = destination_dir / source.name
            if (
                preferred.is_file()
                and preferred.name.casefold() not in used_names
                and sha256_file(preferred) == digest
            ):
                name = preferred.name
                used_names.add(name.casefold())
            else:
                name = self._unique_name(source.name, used_names, destination_dir)
            managed = destination_dir / name
            if not managed.exists():
                self._clone_or_copy(object_path, managed)
            media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            adopted.append(
                ManagedFile(source, managed.resolve(), source.name, digest, size, media_type)
            )
        return adopted

    @staticmethod
    def existing_managed_files(database: Any, document_id: str) -> list[ManagedFile]:
        return [
            ManagedFile(
                original_path=Path(row["original_path"]),
                managed_path=Path(row["managed_path"]),
                original_name=row["original_name"],
                sha256=row["sha256"],
                size=row["size"],
                media_type=row["media_type"],
            )
            for row in database.document_files(document_id)
        ]

    @staticmethod
    def _unique_name(name: str, used: set[str], directory: Path) -> str:
        clean = Path(name).name or "Scan"
        candidate = clean
        index = 2
        while candidate.casefold() in used or (directory / candidate).exists():
            path = Path(clean)
            candidate = f"{path.stem}-{index}{path.suffix}"
            index += 1
        used.add(candidate.casefold())
        return candidate

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".schriftlotse-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as target, source.open("rb") as origin:
                shutil.copyfileobj(origin, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
            shutil.copystat(source, destination, follow_symlinks=True)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def _clone_or_copy(cls, source: Path, destination: Path) -> None:
        """Use an independent APFS clone on macOS, never a mutable hard link."""
        if sys.platform == "darwin":
            result = subprocess.run(
                ["/bin/cp", "-c", str(source), str(destination)],
                capture_output=True,
                check=False,
            )
            if result.returncode == 0 and destination.is_file():
                return
            destination.unlink(missing_ok=True)
        cls._atomic_copy(source, destination)

    def make_thumbnail(self, document_id: str, source: Path, page_index: int = 0) -> Path:
        destination = self.thumbnail_path(document_id)
        try:
            from schriftlotse.ingest import load_page

            image = load_page(source, page_index).convert("RGB")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((900, 900), Image.Resampling.LANCZOS)
            image.save(destination, "JPEG", quality=78, optimize=True)
        except Exception:
            Image.new("RGB", (640, 420), "#ece9e1").save(destination, "JPEG", quality=75)
        return destination

    def adopt_existing_document(self, database: Any, document_id: str) -> dict[str, Any]:
        row = database.document(document_id)
        if row is None:
            raise KeyError(document_id)
        source_paths = [Path(value) for value in json.loads(row["source_paths"] or "[]")]
        available = [path for path in source_paths if path.is_file()]
        if not available:
            raise FileNotFoundError("Keine Quelldatei dieses Dokuments ist mehr erreichbar")
        managed = self.adopt_sources(document_id, available)
        mapping = {str(item.original_path): str(item.managed_path) for item in managed}
        thumbnail = self.make_thumbnail(document_id, managed[0].managed_path)
        database.mark_document_managed(document_id, managed, mapping, thumbnail)
        for page in database.rows(
            "SELECT page_index,prepared_path FROM pages WHERE document_id=? ORDER BY page_index",
            (document_id,),
        ):
            prepared = Path(page["prepared_path"]) if page["prepared_path"] else None
            if prepared is None or not prepared.is_file():
                continue
            destination = self.prepared_dir(document_id) / f"{int(page['page_index']):04d}.png"
            if prepared.resolve() != destination.resolve():
                self._atomic_copy(prepared, destination)
            database.update_page_prepared_path(document_id, int(page["page_index"]), destination)
        return {
            "document_id": document_id,
            "files": len(managed),
            "bytes": sum(item.size for item in managed),
            "thumbnail": str(thumbnail),
        }

    def migration_preview(self, database: Any) -> dict[str, Any]:
        documents: list[dict[str, Any]] = []
        total_bytes = 0
        for row in database.rows(
            "SELECT * FROM documents WHERE library_managed=0 AND deleted_at IS NULL "
            "ORDER BY created_at DESC,title"
        ):
            paths = [Path(value) for value in json.loads(row["source_paths"] or "[]")]
            reachable = [path for path in paths if path.is_file()]
            size = sum(path.stat().st_size for path in reachable)
            total_bytes += size
            output = Path(row["output_dir"]) if row["output_dir"] else None
            documents.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "sources": len(paths),
                    "reachable": len(reachable),
                    "bytes": size,
                    "managed": bool(row["library_managed"]),
                    "output_available": bool(output and output.is_dir()),
                    "grouping_review": len(paths) > 1 and row["title"].casefold() == "testdocs",
                }
            )
        return {
            "library": str(self.root),
            "documents": documents,
            "pending": len(documents),
            "bytes": total_bytes,
            "requires_review": sum(bool(item["grouping_review"]) for item in documents),
        }

    def verify_document(self, database: Any, document_id: str) -> dict[str, Any]:
        files = database.document_files(document_id)
        checked = 0
        problems: list[dict[str, str]] = []
        for row in files:
            checked += 1
            path = Path(row["managed_path"])
            status = "ok"
            message = "Datei und Prüfsumme sind in Ordnung"
            if not path.is_file():
                status, message = "fehlt", "Verwaltete Datei fehlt"
            elif sha256_file(path) != row["sha256"]:
                status, message = "verändert", "Prüfsumme stimmt nicht mehr überein"
            database.record_integrity_check(document_id, row["id"], status, message)
            if status != "ok":
                problems.append(
                    {"file": row["original_name"], "status": status, "message": message}
                )
        return {"document_id": document_id, "checked": checked, "problems": problems}

    def repair_document(self, database: Any, document_id: str) -> dict[str, Any]:
        repaired: list[str] = []
        unresolved: list[str] = []
        for row in database.document_files(document_id):
            managed = Path(row["managed_path"])
            digest = row["sha256"]
            object_path = self.objects / digest[:2] / digest
            if managed.is_file() and sha256_file(managed) == digest:
                continue
            if not object_path.is_file() or sha256_file(object_path) != digest:
                unresolved.append(row["original_name"])
                continue
            managed.unlink(missing_ok=True)
            self._clone_or_copy(object_path, managed)
            database.record_integrity_check(
                document_id, row["id"], "ok", "Aus dem geprüften Bibliotheksobjekt repariert"
            )
            repaired.append(row["original_name"])
        return {"document_id": document_id, "repaired": repaired, "unresolved": unresolved}

    def purge_document(self, database: Any, document_id: str) -> None:
        row = database.document(document_id)
        if row is None or not row["deleted_at"]:
            raise ValueError("Nur Dokumente im Papierkorb können endgültig gelöscht werden")
        target = self.document_root(document_id).resolve()
        parent = self.documents.resolve()
        if target.parent != parent or target.name != document_id:
            raise ValueError("Ungültiger Bibliothekspfad")
        digests = {row["sha256"] for row in database.document_files(document_id)}
        quarantine = parent / f".{document_id}.wird-geloescht"
        if quarantine.exists():
            raise ValueError("Für dieses Dokument läuft bereits ein Löschvorgang")
        if target.is_dir():
            target.rename(quarantine)
        try:
            database.purge_document(document_id)
        except Exception:
            if quarantine.is_dir() and not target.exists():
                quarantine.rename(target)
            raise
        if quarantine.is_dir():
            shutil.rmtree(quarantine)
        for digest in digests:
            still_used = database.rows(
                "SELECT 1 FROM document_files WHERE sha256=? LIMIT 1", (digest,)
            )
            object_path = self.objects / digest[:2] / digest
            if not still_used and object_path.is_file():
                object_path.unlink()
