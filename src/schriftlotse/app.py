from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
from collections.abc import Iterator
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from schriftlotse.cloud import OpenRouterReviewer, cloud_model_status
from schriftlotse.config import AppPaths, Settings, resolve_executable
from schriftlotse.database import Database
from schriftlotse.domain import (
    CloudPolicy,
    DocumentMetadata,
    DocumentRequest,
    QualityProfile,
    Reading,
    ReadingKind,
    ReviewStatus,
    ScriptHint,
    SearchMode,
    SearchQuery,
)
from schriftlotse.exports import export_document
from schriftlotse.ingest import (
    IMAGE_SUFFIXES,
    PDF_SUFFIXES,
    TIFF_SUFFIXES,
    discover_documents,
    import_preview,
    load_page,
    title_needs_review,
)
from schriftlotse.library import LibraryManager, sha256_file
from schriftlotse.model_registry import MODELS, ModelManager
from schriftlotse.pipeline import ProcessingPipeline
from schriftlotse.preprocessing import generate_variants
from schriftlotse.search import ArchiveSearch

LOGGER = logging.getLogger(__name__)
SUPPORTED_SOURCE_SUFFIXES = IMAGE_SUFFIXES | TIFF_SUFFIXES | PDF_SUFFIXES


def _authorized_native_source(raw_path: str) -> Path | None:
    """Resolve a native picker result inside a deliberately allowed local root."""
    try:
        # `raw_path` comes from NSOpenPanel, requires the per-process instance
        # token, and is reconstructed below only after containment in a trusted
        # root. Accepting that selected path is the purpose of this endpoint.
        candidate = Path(raw_path).expanduser().resolve(strict=True)  # lgtm[py/path-injection]
    except (OSError, RuntimeError):
        return None
    roots = [Path.home(), Path(tempfile.gettempdir()), Path("/Volumes")]
    for untrusted_root in roots:
        try:
            root = untrusted_root.resolve(strict=True)
            relative = candidate.relative_to(root)
        except (OSError, ValueError):
            continue
        if not relative.parts:
            return None
        # Rebuild the path from a trusted root after the containment check so
        # request data can never become an unrestricted filesystem expression.
        authorized = (root / relative).resolve(strict=True)
        if authorized.is_file() and authorized.suffix.casefold() not in SUPPORTED_SOURCE_SUFFIXES:
            return None
        if authorized.is_file() or authorized.is_dir():
            return authorized
    return None


class JobPayload(BaseModel):
    sources: list[str]
    year: int | None = Field(default=None, ge=800, le=2100)
    script: ScriptHint = ScriptHint.AUTO
    quality: QualityProfile = QualityProfile.BEST_LOCAL
    cloud: bool = False
    cloud_budget_usd: float = Field(default=1.0, ge=0, le=100)
    group_images_by_folder: bool = False
    cloud_model_profile: str = "quality"
    document_metadata: dict[str, DocumentMetadata] = Field(default_factory=dict)
    preserve_folder_structure: bool = True


class SearchPayload(BaseModel):
    text: str
    mode: SearchMode = SearchMode.SMART
    fuzziness: float = Field(default=0.72, ge=0, le=1)
    year_from: int | None = None
    year_to: int | None = None
    limit: int = Field(default=50, ge=1, le=500)


class ImportPreviewPayload(BaseModel):
    sources: list[str]
    group_images_by_folder: bool = False


class CorrectionPayload(BaseModel):
    text: str


class ModelInstallPayload(BaseModel):
    accept_license: bool = False


class CloudLinePayload(BaseModel):
    budget_usd: float = Field(default=0.5, gt=0, le=10)
    profile: str = "quality"


class SettingsPayload(BaseModel):
    advanced_models: bool = True
    semantic_search: bool = True
    cloud_budget_usd: float = Field(default=1.0, ge=0, le=100)
    output_dir: str | None = None
    tesseract_command: str = "tesseract"
    default_quality: str = "beste_lokale_qualitaet"
    default_script: str = "auto"
    openrouter_profile: str = "quality"
    show_preprocessing: bool = True
    output_token: str | None = None
    library_dir: str | None = None


class ArchiveMetadataPayload(BaseModel):
    title: str | None = Field(default=None, max_length=240)
    year: int | None = Field(default=None, ge=800, le=2100)
    archive: str = Field(default="", max_length=240)
    fonds: str = Field(default="", max_length=240)
    series: str = Field(default="", max_length=240)
    shelfmark: str = Field(default="", max_length=240)
    external_id: str = Field(default="", max_length=240)
    source_url: str = Field(default="", max_length=1000)
    creator: str = Field(default="", max_length=240)
    place: str = Field(default="", max_length=240)
    date_from: int | None = Field(default=None, ge=800, le=2100)
    date_to: int | None = Field(default=None, ge=800, le=2100)
    description: str = Field(default="", max_length=4000)
    rights: str = Field(default="", max_length=1000)
    notes: str = Field(default="", max_length=10000)
    document_status: str = "automatisch"
    collection_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class CollectionPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    parent_id: str | None = None


class CollectionUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    parent_id: str | None = None
    update_parent: bool = False


class SourceSyncPayload(BaseModel):
    relative_paths: list[str] = Field(default_factory=list)


class NativeSourcesPayload(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)


class MigrationPayload(BaseModel):
    document_ids: list[str] = Field(default_factory=list)
    split_document_ids: list[str] = Field(default_factory=list)


class OpenRouterKeyPayload(BaseModel):
    key: str
    verify_key: bool = Field(default=True, alias="validate")


@dataclass(slots=True)
class JobRuntime:
    id: str
    pipeline: ProcessingPipeline
    events: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    status: str = "wartend"
    message: str = "Auftrag wird vorbereitet"
    progress: float = 0.0
    started: float = field(default_factory=time.monotonic)
    exports: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    history: list[str] = field(default_factory=list)
    live: dict[str, Any] = field(default_factory=dict)
    preview_path: Path | None = None
    document_ids: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    collection_mapping: dict[str, list[str]] = field(default_factory=dict)
    source_folder_mapping: dict[str, dict[str, Any]] = field(default_factory=dict)
    event_details: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, message: str, progress: float) -> None:
        self.status = "läuft"
        self.message = message
        # Page splitting can increase the denominator after work has started;
        # the visible bar must nevertheless never jump backwards.
        self.progress = max(self.progress, max(0.0, min(1.0, progress)))
        if not self.history or self.history[-1] != message:
            self.history.append(message)
            self.history = self.history[-80:]
            detail = {
                "type": "fortschritt",
                "stage": self.live.get("stage", ""),
                "message": message,
                "progress": self.progress,
                "model": self.live.get("model", ""),
                "timestamp": time.time(),
            }
            self.event_details.append(detail)
            self.event_details = self.event_details[-160:]
            database = getattr(self.pipeline, "database", None)
            if database is not None:
                database.record_job_event(self.id, "fortschritt", message, self.progress, detail)
        self.events.put(self.snapshot())

    def emit_live(self, payload: dict[str, Any]) -> None:
        preview = payload.get("preview_path")
        if preview:
            self.preview_path = Path(str(preview)).expanduser().resolve()
        self.live = {key: value for key, value in payload.items() if key != "preview_path"}
        if self.preview_path is not None:
            self.live["preview_url"] = f"/api/jobs/{self.id}/preview"
        detail = {
            "type": "live",
            "message": self.message,
            "progress": self.progress,
            "timestamp": time.time(),
            **self.live,
        }
        self.event_details.append(detail)
        self.event_details = self.event_details[-160:]
        database = getattr(self.pipeline, "database", None)
        if database is not None:
            database.record_job_event(
                self.id,
                "live",
                self.message,
                self.progress,
                detail,
            )
        self.events.put(self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        elapsed = max(0, int(time.monotonic() - self.started))
        remaining: int | None = None
        if 0.03 <= self.progress < 1.0:
            remaining = max(0, round(elapsed * (1.0 - self.progress) / self.progress))
        return {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "progress": self.progress,
            "percent": round(self.progress * 100),
            "elapsed_seconds": elapsed,
            "estimated_remaining_seconds": remaining,
            "exports": self.exports,
            "error": self.error,
            "local": True,
            "history": self.history,
            "events": self.event_details,
            "live": self.live,
            "document_ids": self.document_ids,
            "summary": self.summary,
        }


@dataclass(slots=True)
class ModelInstallRuntime:
    id: str
    key: str
    status: str = "läuft"
    message: str = "Download wird vorbereitet"
    started: float = field(default_factory=time.monotonic)
    error: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "status": self.status,
            "message": self.message,
            "elapsed_seconds": max(0, int(time.monotonic() - self.started)),
            "error": self.error,
        }


class ApplicationState:
    def __init__(self) -> None:
        self.paths = AppPaths.default()
        self.paths.ensure()
        self.settings = Settings.load(self.paths)
        self.database = Database(self.paths.database)
        self.models = ModelManager(self.paths)
        self.search = ArchiveSearch(self.database, self.models)
        self.library = LibraryManager(self.paths, self.settings)
        self.jobs: dict[str, JobRuntime] = {}
        self.model_jobs: dict[str, ModelInstallRuntime] = {}
        self.authorized_sources: dict[str, Path] = {}
        self.authorized_source_context: dict[str, dict[str, Any]] = {}
        self.authorized_output_dirs: dict[str, Path] = {}
        self.downloads: dict[str, Path] = {}
        self.lock = threading.Lock()
        # Apple Silicon unified memory is shared by all local engines. One
        # heavy OCR/model task at a time prevents avoidable system pressure.
        self.processing_slot = threading.Semaphore(1)

    def register_source(
        self,
        path: Path,
        display_name: str | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        resolved = path.expanduser().resolve()
        token = uuid.uuid4().hex
        with self.lock:
            self.authorized_sources[token] = resolved
            if context is not None:
                self.authorized_source_context[token] = context
        return {"id": token, "name": display_name or resolved.name}

    def _prepare_folder_mappings(
        self, sources: list[Path], *, group_images_by_folder: bool
    ) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
        collection_mapping: dict[str, list[str]] = {}
        source_mapping: dict[str, dict[str, Any]] = {}
        for root in (path for path in sources if path.is_dir()):
            root = root.resolve()
            root_collection_id = self.database.ensure_collection(root.name, kind="quellordner")
            existing = self.database.source_folder_by_path(root)
            source_id = str(existing["id"]) if existing else uuid.uuid4().hex
            self.database.upsert_source_folder(source_id, root, root.name, root_collection_id)
            documents = discover_documents([root], group_images_by_folder=group_images_by_folder)
            for document in documents:
                parent = document.source_paths[0].parent.resolve()
                try:
                    relative_parent = parent.relative_to(root)
                except ValueError:
                    relative_parent = Path()
                parts = [root.name, *relative_parent.parts]
                collection_id = self.database.ensure_collection_path(parts)
                collection_mapping[document.id] = [collection_id]
                source_mapping[document.id] = {
                    "source_id": source_id,
                    "root": str(root),
                    "collection_id": collection_id,
                    "paths": [str(path.resolve()) for path in document.source_paths],
                }
        return collection_mapping, source_mapping

    def _finalize_folder_mappings(self, runtime: JobRuntime) -> None:
        for document_id in runtime.document_ids:
            collection_ids = runtime.collection_mapping.get(document_id, [])
            if collection_ids:
                self.database.add_document_to_collections(document_id, collection_ids)
            source = runtime.source_folder_mapping.get(document_id)
            if source is None:
                continue
            root = Path(source["root"])
            for raw_path in source["paths"]:
                path = Path(raw_path)
                if not path.is_file():
                    continue
                stat = path.stat()
                self.database.upsert_source_entry(
                    source["source_id"],
                    path.relative_to(root).as_posix(),
                    sha256_file(path),
                    stat.st_size,
                    stat.st_mtime_ns,
                    document_id=document_id,
                    collection_id=source["collection_id"],
                )

    def register_downloads(self, paths: list[Path]) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        with self.lock:
            for path in paths:
                if not path.is_file():
                    continue
                token = uuid.uuid4().hex
                self.downloads[token] = path.resolve()
                entries.append({"id": token, "name": path.name})
        return entries

    def register_output_directory(self, path: Path) -> dict[str, str]:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError("Ausgabeordner existiert nicht")
        token = uuid.uuid4().hex
        with self.lock:
            self.authorized_output_dirs[token] = resolved
        return {"token": token, "path": str(resolved)}

    def resolve_output_directory(self, value: str, token: str | None) -> Path:
        with self.lock:
            authorized = self.authorized_output_dirs.get(token or "")
        current = Settings.load(self.paths).output_dir
        if authorized is not None and value == str(authorized):
            resolved = authorized
        elif current is not None and value == current:
            # The persisted setting is local trusted state, not an HTTP path.
            resolved = Path(current).expanduser().resolve()
        else:
            raise ValueError("Ausgabeordner bitte über „Auswählen“ freigeben")
        if not resolved.is_dir():
            raise ValueError("Ausgabeordner existiert nicht; bitte zuerst im Finder anlegen")
        if not os.access(resolved, os.W_OK):
            raise ValueError("Ausgabeordner ist nicht beschreibbar")
        return resolved

    def download(self, token: str) -> Path:
        with self.lock:
            path = self.downloads.get(token)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="Ausgabedatei nicht gefunden")
        return path

    def create_job(
        self, payload: JobPayload, *, target_document_id: str | None = None
    ) -> JobRuntime:
        if payload.cloud_model_profile not in {item["key"] for item in cloud_model_status()}:
            raise ValueError("Unbekanntes OpenRouter-Modellprofil")
        with self.lock:
            sources = [self.authorized_sources.get(token) for token in payload.sources]
            source_contexts = {
                token: self.authorized_source_context.get(token) for token in payload.sources
            }
        if (
            not sources
            or any(path is None for path in sources)
            or not all(path.exists() for path in sources if path is not None)
        ):
            raise ValueError(
                "Mindestens eine in SchriftLotse ausgewählte Datei oder ein Ordner ist erforderlich"
            )
        resolved_sources = [path for path in sources if path is not None]
        collection_mapping: dict[str, list[str]] = {}
        source_folder_mapping: dict[str, dict[str, Any]] = {}
        if payload.preserve_folder_structure:
            collection_mapping, source_folder_mapping = self._prepare_folder_mappings(
                resolved_sources,
                group_images_by_folder=payload.group_images_by_folder,
            )
            for token, path in zip(payload.sources, resolved_sources, strict=True):
                context = source_contexts.get(token)
                if context is None or path.is_dir():
                    continue
                documents = discover_documents([path], group_images_by_folder=False)
                if not documents:
                    continue
                document = documents[0]
                collection_mapping[document.id] = [context["collection_id"]]
                source_folder_mapping[document.id] = {
                    **context,
                    "paths": [str(path)],
                }
        request = DocumentRequest(
            sources=resolved_sources,
            year=payload.year,
            script_hint=payload.script,
            quality_profile=payload.quality,
            cloud_policy=CloudPolicy.ADAPTIVE if payload.cloud else CloudPolicy.LOCAL_ONLY,
            cloud_budget_usd=payload.cloud_budget_usd,
            advanced_models=payload.quality != QualityProfile.FAST,
            group_images_by_folder=payload.group_images_by_folder,
            cloud_model_profile=payload.cloud_model_profile,
            document_metadata=payload.document_metadata,
            target_document_id=target_document_id,
        )
        runtime_id = uuid.uuid4().hex
        pipeline = ProcessingPipeline(self.paths, Settings.load(self.paths), self.database)
        runtime = JobRuntime(runtime_id, pipeline)
        runtime.collection_mapping = collection_mapping
        runtime.source_folder_mapping = source_folder_mapping
        pipeline.live_callback = runtime.emit_live
        with self.lock:
            self.jobs[runtime_id] = runtime

        def worker() -> None:
            try:
                runtime.message = "Wartet auf freien lokalen Modellplatz"
                runtime.events.put(runtime.snapshot())
                with self.processing_slot:
                    _pipeline_id, results, exports = pipeline.run(
                        request, progress=runtime.emit, job_id=runtime_id
                    )
                if runtime.status == "abgebrochen":
                    return
                runtime.status = "fertig"
                runtime.progress = 1.0
                runtime.message = "Verarbeitung abgeschlossen"
                runtime.exports = self.register_downloads(exports)
                runtime.document_ids = [result.document.id for result in results]
                self._finalize_folder_mappings(runtime)
                runtime.summary = {
                    "documents": len(results),
                    "pages": sum(len(result.pages) for result in results),
                    "uncertain": sum(
                        1
                        for result in results
                        for page in result.pages
                        for line in page.lines
                        if line.review_status == ReviewStatus.UNCERTAIN
                    ),
                    "models": sorted(
                        {page.selected_model for result in results for page in result.pages}
                    ),
                }
            except BaseException as error:
                runtime.status = "fehlgeschlagen"
                runtime.error = str(error) or error.__class__.__name__
                runtime.message = f"Fehler: {runtime.error}"
            runtime.events.put(runtime.snapshot())

        threading.Thread(target=worker, name=f"schriftlotse-{runtime_id[:8]}", daemon=True).start()
        return runtime

    def resume_job(self, job_id: str) -> JobRuntime:
        row = self.database.job(job_id)
        if row is None:
            raise ValueError("Auftrag nicht gefunden")
        try:
            request = DocumentRequest.model_validate_json(row["request_json"])
        except ValueError as error:
            raise ValueError("Der alte Auftrag enthält keine wiederherstellbaren Daten") from error
        if not request.sources or not all(path.exists() for path in request.sources):
            raise ValueError("Mindestens eine Quelldatei des alten Auftrags fehlt")
        pipeline = ProcessingPipeline(self.paths, Settings.load(self.paths), self.database)
        runtime = JobRuntime(job_id, pipeline)
        pipeline.live_callback = runtime.emit_live
        with self.lock:
            self.jobs[job_id] = runtime

        def worker() -> None:
            try:
                runtime.message = "Wartet auf freien lokalen Modellplatz"
                runtime.events.put(runtime.snapshot())
                with self.processing_slot:
                    _pipeline_id, _results, exports = pipeline.run(
                        request, progress=runtime.emit, job_id=job_id
                    )
                if runtime.status == "abgebrochen":
                    return
                runtime.status = "fertig"
                runtime.progress = 1.0
                runtime.message = "Wiederhergestellter Auftrag abgeschlossen"
                runtime.exports = self.register_downloads(exports)
            except BaseException as error:
                runtime.status = "fehlgeschlagen"
                runtime.error = str(error) or error.__class__.__name__
                runtime.message = f"Fehler: {runtime.error}"
            runtime.events.put(runtime.snapshot())

        threading.Thread(
            target=worker,
            name=f"schriftlotse-resume-{job_id[:8]}",
            daemon=True,
        ).start()
        return runtime

    def install_model(self, key: str, accept_license: bool) -> ModelInstallRuntime:
        task = ModelInstallRuntime(uuid.uuid4().hex, key)
        self.model_jobs[task.id] = task

        def worker() -> None:
            try:
                task.message = "Wartet auf freien lokalen Modellplatz"
                with self.processing_slot:
                    task.message = "Modell wird heruntergeladen und geprüft"
                    self.models.install(key, accept_license=accept_license)
                task.status = "fertig"
                task.message = "Modell ist lokal installiert"
            except BaseException as error:
                task.status = "fehlgeschlagen"
                task.error = str(error) or error.__class__.__name__
                task.message = f"Fehler: {task.error}"

        threading.Thread(
            target=worker,
            name=f"schriftlotse-model-{key}",
            daemon=True,
        ).start()
        return task


class UIController:
    """Compatibility helpers plus presentation-neutral status rendering."""

    @staticmethod
    def _elapsed(started: float) -> str:
        seconds = max(0, round(time.monotonic() - started))
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes}:{seconds:02d} min" if minutes else f"{seconds} s"

    @classmethod
    def _progress_status(
        cls, message: str, value: float, started: float, failed: bool = False
    ) -> str:
        percent = max(0, min(100, round(value * 100)))
        color = "#b42318" if failed else "#173f4b"
        return (
            '<div class="sl-progress">'
            f'<strong style="color:{color}">{escape(message)}</strong>'
            f"<span>{percent}% · {cls._elapsed(started)}</span>"
            '<div class="sl-local">Lokal auf diesem Mac · OCR-/HTR-Modelle</div>'
            "</div>"
        )


def _web_directory() -> Path:
    return Path(__file__).with_name("web")


def _export_current_document(state: ApplicationState, document_id: str) -> list[Path]:
    document = state.database.document(document_id)
    if document is None:
        raise ValueError("Dokument nicht gefunden")
    try:
        result = state.database.load_document_result(document_id)
    except KeyError as error:
        raise ValueError("Dokument nicht gefunden") from error
    if not result.pages:
        raise ValueError("Das Dokument besitzt noch keine verarbeitbaren Seiten")
    output_dir = state.library.derived_dir(document_id)
    return export_document(result, output_dir)


def create_app(state: ApplicationState | None = None) -> FastAPI:
    app_state = state or ApplicationState()
    web = _web_directory()
    templates = Environment(
        loader=FileSystemLoader(web / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )
    app = FastAPI(title="SchriftLotse", version="0.2.0")
    app.state.schriftlotse = app_state
    app.mount("/static", StaticFiles(directory=web / "static"), name="static")

    @app.middleware("http")
    async def prevent_stale_local_ui(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return templates.get_template("index.html").render()

    @app.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        """Small local readiness probe for the native wrapper and diagnostics."""
        expected = os.getenv("SCHRIFTLOTSE_INSTANCE_TOKEN", "")
        supplied = request.headers.get("x-schriftlotse-instance", "")
        if expected and not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="Falsche lokale App-Instanz")
        return {
            "status": "bereit",
            "local": True,
            "version": "0.2.0",
        }

    @app.post("/api/uploads")
    async def upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:  # noqa: B008
        directory = app_state.paths.cache / "uploads" / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=True)
        sources: list[dict[str, str]] = []
        for uploaded in files:
            name = Path(uploaded.filename or "Scan").name
            suffix = Path(name).suffix.casefold()
            supported = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf", ".heic"}
            safe_suffix = suffix if suffix in supported else ""
            clean_name = Path(name).stem[:160].strip() or "Scan"
            destination = directory / f"{clean_name}{safe_suffix}"
            collision = 2
            while destination.exists():
                destination = directory / f"{clean_name}-{collision}{safe_suffix}"
                collision += 1
            with destination.open("wb") as handle:
                while chunk := await uploaded.read(1024 * 1024):
                    handle.write(chunk)
            sources.append(app_state.register_source(destination, name))
        return {"sources": sources}

    @app.post("/api/native-sources")
    def native_sources(payload: NativeSourcesPayload, request: Request) -> dict[str, Any]:
        expected = os.getenv("SCHRIFTLOTSE_INSTANCE_TOKEN", "")
        supplied = request.headers.get("x-schriftlotse-instance", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="Native Dateiauswahl nicht autorisiert")
        sources: list[dict[str, str]] = []
        for raw_path in payload.paths:
            path = _authorized_native_source(raw_path)
            if path is None:
                continue
            sources.append(app_state.register_source(path, path.name))
        if not sources:
            raise HTTPException(status_code=400, detail="Keine gültige Auswahl erhalten")
        return {"sources": sources}

    @app.post("/api/folder")
    def pick_folder() -> dict[str, Any]:
        process = subprocess.run(
            [
                "osascript",
                "-e",
                'POSIX path of (choose folder with prompt "Scan-Ordner auswählen")',
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if process.returncode != 0 or not process.stdout.strip():
            return {"source": None}
        selected = Path(process.stdout.strip()).expanduser().resolve()
        if not selected.is_dir():
            return {"source": None}
        return {"source": app_state.register_source(selected)}

    @app.post("/api/import-preview")
    def preview_import(payload: ImportPreviewPayload) -> dict[str, Any]:
        with app_state.lock:
            sources = [app_state.authorized_sources.get(token) for token in payload.sources]
        if not sources or any(path is None for path in sources):
            raise HTTPException(status_code=400, detail="Quellenauswahl ist nicht mehr gültig")
        try:
            return import_preview(
                [path for path in sources if path is not None],
                group_images_by_folder=payload.group_images_by_folder,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(
                status_code=400, detail=f"Importvorschau fehlgeschlagen: {error}"
            ) from error

    @app.post("/api/jobs", status_code=202)
    def create_job(payload: JobPayload) -> dict[str, Any]:
        try:
            return app_state.create_job(payload).snapshot()
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/recovery")
    def recovery() -> list[dict[str, Any]]:
        return [dict(row) for row in app_state.database.list_incomplete_jobs()]

    @app.get("/api/job-history")
    def job_history(limit: int = 100) -> list[dict[str, Any]]:
        return [dict(row) for row in app_state.database.job_history(min(max(limit, 1), 500))]

    @app.post("/api/jobs/{job_id}/resume", status_code=202)
    def resume(job_id: str) -> dict[str, Any]:
        try:
            return app_state.resume_job(job_id).snapshot()
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        runtime = app_state.jobs.get(job_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail="Auftrag nicht gefunden")
        return runtime.snapshot()

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str) -> StreamingResponse:
        runtime = app_state.jobs.get(job_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail="Auftrag nicht gefunden")

        def stream() -> Iterator[str]:
            yield f"data: {json.dumps(runtime.snapshot(), ensure_ascii=False)}\n\n"
            while runtime.status not in {"fertig", "fehlgeschlagen", "abgebrochen"}:
                try:
                    event = runtime.events.get(timeout=1.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps(runtime.snapshot(), ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps(runtime.snapshot(), ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/jobs/{job_id}/history")
    def job_event_history(job_id: str) -> list[dict[str, Any]]:
        if app_state.database.job(job_id) is None:
            raise HTTPException(status_code=404, detail="Auftrag nicht gefunden")
        events: list[dict[str, Any]] = []
        for row in app_state.database.job_events(job_id):
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"] or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            events.append(item)
        return events

    @app.get("/api/jobs/{job_id}/preview")
    def job_preview(job_id: str) -> FileResponse:
        runtime = app_state.jobs.get(job_id)
        if runtime is None or runtime.preview_path is None or not runtime.preview_path.is_file():
            raise HTTPException(status_code=404, detail="Noch keine Live-Vorschau verfügbar")
        return FileResponse(runtime.preview_path, media_type="image/jpeg")

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict[str, Any]:
        runtime = app_state.jobs.get(job_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail="Auftrag nicht gefunden")
        runtime.pipeline.cancel()
        runtime.status = "abgebrochen"
        runtime.message = "Durch Benutzer abgebrochen"
        runtime.events.put(runtime.snapshot())
        return runtime.snapshot()

    @app.get("/api/documents")
    def documents(
        response: Response,
        collection: str | None = None,
        status: str | None = None,
        archive: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        rows = (
            app_state.database.list_deleted_documents()
            if status == "papierkorb"
            else app_state.database.list_documents()
        )
        collection_rows = [dict(value) for value in app_state.database.list_collections()]
        collection_by_id = {value["id"]: value for value in collection_rows}

        def collection_path(collection_id: str) -> str:
            names: list[str] = []
            current = collection_by_id.get(collection_id)
            seen: set[str] = set()
            while current and current["id"] not in seen:
                seen.add(current["id"])
                names.append(current["name"])
                current = collection_by_id.get(current.get("parent_id"))
            return " / ".join(reversed(names))

        for row in rows:
            item = dict(row)
            memberships = app_state.database.rows(
                "SELECT collection_id FROM collection_documents WHERE document_id=?",
                (item["id"],),
            )
            collection_ids = [str(value["collection_id"]) for value in memberships]
            if collection:
                valid_ids = {
                    collection,
                    *app_state.database.collection_descendant_ids(collection),
                }
                if not valid_ids.intersection(collection_ids):
                    continue
            if status == "unsicher" and not int(item.get("uncertain_count") or 0):
                continue
            if status == "eingang" and item.get("collections"):
                continue
            if status == "dateiprobleme":
                problems = app_state.database.rows(
                    "SELECT 1 FROM integrity_checks WHERE document_id=? AND status!='ok' "
                    "ORDER BY id DESC LIMIT 1",
                    (item["id"],),
                )
                if not problems:
                    continue
            if archive and archive.casefold() not in (item.get("archive") or "").casefold():
                continue
            item["thumbnail_url"] = f"/api/documents/{item['id']}/thumbnail"
            item["managed"] = bool(item.get("library_managed"))
            item["collection_names"] = [
                value.strip()
                for value in (item.get("collections") or "").split(",")
                if value.strip()
            ]
            item["collection_ids"] = collection_ids
            item["collection_paths"] = [
                collection_path(collection_id) for collection_id in collection_ids
            ]
            item["title_needs_review"] = title_needs_review(str(item.get("title") or ""))
            for private_field in ("source_paths", "output_dir", "thumbnail_path"):
                item.pop(private_field, None)
            items.append(item)
        response.headers["X-Total-Count"] = str(len(items))
        safe_limit = min(max(limit, 1), 2000)
        safe_offset = max(offset, 0)
        return items[safe_offset : safe_offset + safe_limit]

    @app.get("/api/documents/{document_id}/transcript")
    def document_transcript(document_id: str) -> dict[str, Any]:
        transcript = app_state.database.document_transcript(document_id)
        if transcript is None:
            raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
        for page in transcript["pages"]:
            page["image_url"] = f"/api/documents/{document_id}/pages/{page['page_index']}/image"
        return transcript

    @app.get("/api/documents/{document_id}")
    def document_detail(document_id: str) -> dict[str, Any]:
        item = app_state.database.document_detail(document_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
        item.pop("source_paths", None)
        item.pop("output_dir", None)
        item.pop("thumbnail_path", None)
        item["files"] = [
            {
                "role": file["role"],
                "original_name": file["original_name"],
                "sha256": file["sha256"],
                "size": file["size"],
                "media_type": file["media_type"],
            }
            for file in item["files"]
        ]
        item["thumbnail_url"] = f"/api/documents/{document_id}/thumbnail"
        item["title_needs_review"] = title_needs_review(str(item.get("title") or ""))
        for page in item["pages"]:
            page.pop("source_path", None)
            page.pop("prepared_path", None)
            page["image_url"] = f"/api/documents/{document_id}/pages/{page['page_index']}/image"
            page["thumbnail_url"] = (
                f"/api/documents/{document_id}/pages/{page['page_index']}/thumbnail"
            )
        return item

    @app.patch("/api/documents/{document_id}")
    def update_document(document_id: str, payload: ArchiveMetadataPayload) -> dict[str, Any]:
        if payload.date_from and payload.date_to and payload.date_from > payload.date_to:
            raise HTTPException(status_code=400, detail="Datierung von darf nicht nach bis liegen")
        if payload.document_status not in {
            "automatisch",
            "in_pruefung",
            "bestaetigt",
            "ground_truth",
            "in_verarbeitung",
            "fehlgeschlagen",
        }:
            raise HTTPException(status_code=400, detail="Unbekannter Dokumentstatus")
        try:
            app_state.database.update_document_metadata(
                document_id,
                payload.model_dump(
                    exclude={"collection_ids", "tags"},
                    exclude_none=True,
                    exclude_unset=True,
                ),
            )
            if "collection_ids" in payload.model_fields_set:
                app_state.database.set_document_collections(document_id, payload.collection_ids)
            if "tags" in payload.model_fields_set:
                app_state.database.set_document_tags(document_id, payload.tags)
        except (KeyError, sqlite3.IntegrityError) as error:
            raise HTTPException(
                status_code=400, detail="Metadaten konnten nicht gespeichert werden"
            ) from error
        return document_detail(document_id)

    @app.delete("/api/documents/{document_id}")
    def trash_document(document_id: str) -> dict[str, str]:
        if app_state.database.document(document_id) is None:
            raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
        app_state.database.trash_document(document_id)
        return {"status": "in den Papierkorb verschoben"}

    @app.post("/api/documents/{document_id}/restore")
    def restore_document(document_id: str) -> dict[str, str]:
        if app_state.database.document(document_id) is None:
            raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
        app_state.database.trash_document(document_id, restore=True)
        return {"status": "wiederhergestellt"}

    @app.delete("/api/documents/{document_id}/permanent")
    def purge_document(document_id: str) -> dict[str, str]:
        try:
            app_state.library.purge_document(app_state.database, document_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"status": "endgültig gelöscht"}

    @app.post("/api/documents/{document_id}/reveal")
    def reveal_document(document_id: str) -> dict[str, str]:
        files = app_state.database.document_files(document_id)
        document = app_state.database.document(document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
        path = Path(files[0]["managed_path"]) if files else None
        if path is None or not path.exists():
            raise HTTPException(status_code=404, detail="Keine verwaltete Originaldatei gefunden")
        subprocess.Popen(["open", "-R", str(path)])
        return {"status": "im Finder angezeigt"}

    @app.get("/api/documents/{document_id}/thumbnail")
    def document_thumbnail(document_id: str) -> FileResponse:
        row = app_state.database.document(document_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
        thumbnail = Path(row["thumbnail_path"]) if row["thumbnail_path"] else None
        if thumbnail is None or not thumbnail.is_file():
            sources = [Path(value) for value in json.loads(row["source_paths"] or "[]")]
            if not sources or not sources[0].is_file():
                raise HTTPException(status_code=404, detail="Keine Vorschau verfügbar")
            thumbnail = app_state.library.make_thumbnail(document_id, sources[0])
        return FileResponse(thumbnail, media_type="image/jpeg")

    @app.get("/api/documents/{document_id}/pages/{page_index}/image")
    def document_page_image(
        document_id: str, page_index: int, view: str = "original"
    ) -> FileResponse:
        rows = app_state.database.rows(
            "SELECT source_path,source_page_index,prepared_path FROM pages "
            "WHERE document_id=? AND page_index=?",
            (document_id, page_index),
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Seite nicht gefunden")
        row = rows[0]
        prepared = Path(row["prepared_path"]) if row["prepared_path"] else None
        if view == "prepared" and prepared and prepared.is_file():
            return FileResponse(prepared, media_type="image/png")
        image = load_page(Path(row["source_path"]), int(row["source_page_index"]))
        destination = (
            app_state.paths.cache / "previews" / f"{document_id}-{page_index}-original.jpg"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.thumbnail((1800, 1800))
        image.convert("RGB").save(destination, "JPEG", quality=84)
        return FileResponse(destination, media_type="image/jpeg")

    @app.get("/api/documents/{document_id}/pages/{page_index}/thumbnail")
    def document_page_thumbnail(document_id: str, page_index: int) -> FileResponse:
        rows = app_state.database.rows(
            "SELECT source_path,source_page_index,prepared_path FROM pages "
            "WHERE document_id=? AND page_index=?",
            (document_id, page_index),
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Seite nicht gefunden")
        destination = (
            app_state.library.document_root(document_id) / "Vorschauen" / f"{page_index:04d}.jpg"
        )
        if destination.is_file():
            return FileResponse(destination, media_type="image/jpeg")
        row = rows[0]
        prepared = Path(row["prepared_path"]) if row["prepared_path"] else None
        image = (
            load_page(prepared, 0)
            if prepared and prepared.is_file()
            else load_page(Path(row["source_path"]), int(row["source_page_index"]))
        )
        destination = app_state.library.make_page_preview(document_id, page_index, image)
        return FileResponse(destination, media_type="image/jpeg")

    @app.get("/api/collections")
    def collections() -> list[dict[str, Any]]:
        rows = [dict(row) for row in app_state.database.list_collections()]
        by_id = {row["id"]: row for row in rows}
        for row in rows:
            names = [row["name"]]
            parent = by_id.get(row.get("parent_id"))
            visited = {row["id"]}
            while parent and parent["id"] not in visited:
                visited.add(parent["id"])
                names.append(parent["name"])
                parent = by_id.get(parent.get("parent_id"))
            descendants = app_state.database.collection_descendant_ids(row["id"])
            ids = [row["id"], *descendants]
            placeholders = ",".join("?" for _ in ids)
            count = app_state.database.rows(
                f"SELECT COUNT(DISTINCT document_id) AS count FROM collection_documents "
                f"WHERE collection_id IN ({placeholders})",
                tuple(ids),
            )[0]["count"]
            row["path"] = " / ".join(reversed(names))
            row["depth"] = len(names) - 1
            row["descendant_document_count"] = int(count or 0)
        return rows

    @app.post("/api/collections", status_code=201)
    def create_collection(payload: CollectionPayload) -> dict[str, Any]:
        collection_id = uuid.uuid4().hex
        try:
            app_state.database.create_collection(
                collection_id,
                payload.name,
                payload.description,
                payload.parent_id,
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(
                status_code=400, detail="Sammlung konnte nicht angelegt werden"
            ) from error
        return {
            "id": collection_id,
            "name": payload.name.strip(),
            "description": payload.description.strip(),
            "parent_id": payload.parent_id,
            "document_count": 0,
        }

    @app.patch("/api/collections/{collection_id}")
    def update_collection(collection_id: str, payload: CollectionUpdatePayload) -> dict[str, str]:
        try:
            app_state.database.update_collection(
                collection_id,
                name=payload.name,
                description=payload.description,
                parent_id=payload.parent_id if payload.update_parent else Ellipsis,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Sammlung nicht gefunden") from error
        except (ValueError, sqlite3.IntegrityError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"status": "gespeichert", "id": collection_id}

    @app.delete("/api/collections/{collection_id}")
    def delete_collection(collection_id: str) -> dict[str, str]:
        try:
            app_state.database.delete_collection(collection_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Sammlung nicht gefunden") from error
        return {"status": "gelöscht", "id": collection_id}

    @app.get("/api/source-folders")
    def source_folders() -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in app_state.database.list_source_folders():
            item = dict(row)
            item["reachable"] = Path(item["root_path"]).is_dir()
            items.append(item)
        return items

    def source_folder_diff(source_id: str) -> dict[str, Any]:
        source = app_state.database.source_folder(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Quellordner nicht gefunden")
        root = Path(source["root_path"])
        if not root.is_dir():
            return {
                "source_id": source_id,
                "label": source["label"],
                "reachable": False,
                "changes": [],
                "counts": {"new": 0, "changed": 0, "moved": 0, "missing": 0},
            }
        supported = IMAGE_SUFFIXES | TIFF_SUFFIXES | PDF_SUFFIXES
        previous = {
            str(row["relative_path"]): dict(row)
            for row in app_state.database.source_entries(source_id)
        }
        previous_by_hash: dict[str, list[dict[str, Any]]] = {}
        for row in previous.values():
            previous_by_hash.setdefault(str(row["sha256"]), []).append(row)
        current_paths = sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() in supported
        )
        current_relative: set[str] = set()
        changes: list[dict[str, Any]] = []
        for path in current_paths:
            relative = path.relative_to(root).as_posix()
            current_relative.add(relative)
            stat = path.stat()
            known = previous.get(relative)
            if (
                known
                and int(known["size"]) == stat.st_size
                and int(known["mtime_ns"]) == stat.st_mtime_ns
            ):
                continue
            digest = sha256_file(path)
            if known:
                kind = "changed" if digest != known["sha256"] else "metadata"
                if kind == "metadata":
                    app_state.database.upsert_source_entry(
                        source_id,
                        relative,
                        digest,
                        stat.st_size,
                        stat.st_mtime_ns,
                        document_id=known["document_id"],
                        collection_id=known["collection_id"],
                    )
                    continue
            else:
                moved_from = next(
                    (
                        row["relative_path"]
                        for row in previous_by_hash.get(digest, [])
                        if row["relative_path"] not in current_relative
                        and not (root / row["relative_path"]).exists()
                    ),
                    None,
                )
                kind = "moved" if moved_from else "new"
            changes.append(
                {
                    "kind": kind,
                    "relative_path": relative,
                    "previous_path": moved_from if not known else None,
                    "size": stat.st_size,
                    "sha256": digest,
                }
            )
        for relative, known in previous.items():
            if relative not in current_relative and not any(
                item.get("previous_path") == relative for item in changes
            ):
                changes.append(
                    {
                        "kind": "missing",
                        "relative_path": relative,
                        "document_id": known["document_id"],
                    }
                )
        counts = {
            key: sum(1 for item in changes if item["kind"] == key)
            for key in ("new", "changed", "moved", "missing")
        }
        return {
            "source_id": source_id,
            "label": source["label"],
            "reachable": True,
            "root_path": str(root),
            "changes": changes,
            "counts": counts,
        }

    @app.get("/api/source-folders/{source_id}/diff")
    def source_folder_changes(source_id: str) -> dict[str, Any]:
        return source_folder_diff(source_id)

    @app.post("/api/source-folders/{source_id}/prepare-sync")
    def prepare_source_sync(source_id: str, payload: SourceSyncPayload) -> dict[str, Any]:
        source = app_state.database.source_folder(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Quellordner nicht gefunden")
        root = Path(source["root_path"])
        if not root.is_dir():
            raise HTTPException(status_code=400, detail="Quellordner ist nicht erreichbar")
        selected = set(payload.relative_paths)
        diff = source_folder_diff(source_id)
        sources: list[dict[str, str]] = []
        moved = 0
        for change in diff["changes"]:
            relative = change["relative_path"]
            if selected and relative not in selected:
                continue
            if change["kind"] not in {"new", "changed", "moved"}:
                continue
            path = (root / relative).resolve()
            if root.resolve() not in path.parents or not path.is_file():
                continue
            relative_parent = path.parent.relative_to(root)
            collection_id = app_state.database.ensure_collection_path(
                [source["label"], *relative_parent.parts]
            )
            if change["kind"] == "moved" and change.get("previous_path"):
                stat = path.stat()
                app_state.database.move_source_entry(
                    source_id,
                    change["previous_path"],
                    relative,
                    change["sha256"],
                    stat.st_size,
                    stat.st_mtime_ns,
                    collection_id,
                )
                moved += 1
                continue
            sources.append(
                app_state.register_source(
                    path,
                    path.name,
                    context={
                        "source_id": source_id,
                        "root": str(root),
                        "collection_id": collection_id,
                    },
                )
            )
        return {"sources": sources, "changes": diff["changes"], "moved": moved}

    @app.get("/api/library/migration-preview")
    def migration_preview() -> dict[str, Any]:
        return app_state.library.migration_preview(app_state.database)

    @app.post("/api/library/migrate")
    def migrate_library(payload: MigrationPayload) -> dict[str, Any]:
        app_state.database.backup("vor-bibliotheksmigration")
        preview = app_state.library.migration_preview(app_state.database)
        selected = payload.document_ids or [
            item["id"] for item in preview["documents"] if not item["managed"]
        ]
        expanded: list[str] = []
        for document_id in selected:
            if document_id in payload.split_document_ids:
                try:
                    expanded.extend(app_state.database.split_document_into_pages(document_id))
                except (KeyError, sqlite3.DatabaseError) as error:
                    LOGGER.warning(
                        "Dokumentgruppe %s konnte nicht getrennt werden",
                        document_id,
                        exc_info=True,
                    )
                    raise HTTPException(
                        status_code=400,
                        detail="Dokumentgruppe konnte nicht getrennt werden",
                    ) from error
            else:
                expanded.append(document_id)
        migrated: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for document_id in expanded:
            try:
                migrated.append(
                    app_state.library.adopt_existing_document(app_state.database, document_id)
                )
            except (KeyError, OSError, ValueError):
                LOGGER.warning(
                    "Dokument %s konnte nicht übernommen werden",
                    document_id,
                    exc_info=True,
                )
                errors.append(
                    {
                        "document_id": document_id,
                        "error": "Original konnte nicht in die Bibliothek übernommen werden",
                    }
                )
        return {"migrated": migrated, "errors": errors, "library": str(app_state.library.root)}

    @app.post("/api/library/integrity")
    def verify_library() -> dict[str, Any]:
        results = [
            app_state.library.verify_document(app_state.database, row["id"])
            for row in app_state.database.list_documents()
            if row["library_managed"]
        ]
        return {
            "documents": len(results),
            "files": sum(item["checked"] for item in results),
            "problems": [problem for item in results for problem in item["problems"]],
        }

    @app.post("/api/documents/{document_id}/repair")
    def repair_document(document_id: str) -> dict[str, Any]:
        if app_state.database.document(document_id) is None:
            raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
        return app_state.library.repair_document(app_state.database, document_id)

    @app.post("/api/documents/{document_id}/reprocess", status_code=202)
    def reprocess_document(document_id: str) -> dict[str, Any]:
        document = app_state.database.document(document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
        paths = [Path(value) for value in json.loads(document["source_paths"] or "[]")]
        if not paths or not all(path.is_file() for path in paths):
            raise HTTPException(status_code=400, detail="Mindestens ein verwaltetes Original fehlt")
        source_tokens = [app_state.register_source(path)["id"] for path in paths]
        settings = Settings.load(app_state.paths)
        quality = (
            QualityProfile(settings.default_quality)
            if settings.default_quality in {"schnell", "beste_lokale_qualitaet", "lizenzklar"}
            else QualityProfile.BEST_LOCAL
        )
        try:
            runtime = app_state.create_job(
                JobPayload(
                    sources=source_tokens,
                    year=document["year"],
                    script=ScriptHint(document["script_hint"]),
                    quality=quality,
                    cloud=False,
                    cloud_budget_usd=0,
                    group_images_by_folder=len(paths) > 1,
                    cloud_model_profile=settings.openrouter_profile,
                    document_metadata={
                        document_id: DocumentMetadata(
                            title=document["title"],
                            year=document["year"],
                            script_hint=ScriptHint(document["script_hint"]),
                        )
                    },
                ),
                target_document_id=document_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return runtime.snapshot()

    @app.post("/api/documents/{document_id}/pagexml-import")
    async def import_pagexml(
        document_id: str,
        files: list[UploadFile] = File(...),  # noqa: B008
    ) -> dict[str, Any]:
        directory = app_state.paths.cache / "pagexml-import" / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for index, uploaded in enumerate(files):
            destination = directory / f"seite-{index:04d}.xml"
            destination.write_bytes(await uploaded.read())
            paths.append(destination)
        try:
            changed = app_state.database.import_pagexml_corrections(document_id, paths)
        except Exception as error:
            raise HTTPException(status_code=400, detail=f"PAGE-XML ungültig: {error}") from error
        return {"document_id": document_id, "corrected_lines": changed}

    @app.post("/api/documents/{document_id}/export")
    def export_current(document_id: str) -> dict[str, Any]:
        try:
            paths = _export_current_document(app_state, document_id)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        primary_names = {
            "schriftlotse-ergebnis.zip",
            "schriftlotse.pdf",
            "schriftlotse.docx",
            "transkription_original.txt",
            "lesefassung.txt",
            "result.json",
        }
        return {
            "status": "aktuelle Fassung exportiert",
            "downloads": app_state.register_downloads(
                [path for path in paths if path.name in primary_names]
            ),
        }

    @app.post("/api/search")
    def search(payload: SearchPayload) -> list[dict[str, Any]]:
        if not payload.text.strip():
            return []
        hits = app_state.search.search(
            SearchQuery(
                text=payload.text,
                mode=payload.mode,
                fuzziness=payload.fuzziness,
                year_from=payload.year_from,
                year_to=payload.year_to,
                limit=payload.limit,
            )
        )
        results: list[dict[str, Any]] = []
        for hit in hits:
            item = hit.model_dump(mode="json")
            item.pop("source_path", None)
            item["image_url"] = f"/api/pages/{hit.line_id}/image"
            results.append(item)
        metadata_rows = app_state.database.search_document_metadata(
            payload.text,
            payload.year_from,
            payload.year_to,
            max(1, min(payload.limit // 3, 50)),
        )
        existing_documents = {item["document_id"] for item in results}
        for row in metadata_rows:
            if row["id"] in existing_documents:
                continue
            label = " · ".join(
                str(value)
                for value in (
                    row["archive"],
                    row["fonds"],
                    row["shelfmark"],
                    row["creator"],
                    row["place"],
                )
                if value
            )
            results.append(
                {
                    "line_id": None,
                    "document_id": row["id"],
                    "document_title": row["title"],
                    "page_index": int(row["page_index"] or 0),
                    "bbox": json.loads(row["bbox"]) if row["bbox"] else [0, 0, 0, 0],
                    "text": label or row["title"],
                    "matched_form": label or row["title"],
                    "reason": "Treffer in Archivangaben oder Tags",
                    "score": 1.0,
                    "confidence": float(row["confidence"] or 0),
                    "year": row["year"],
                    "image_url": f"/api/documents/{row['id']}/thumbnail",
                }
            )
        return results[: payload.limit]

    @app.get("/api/pages/{line_id}/image")
    def page_image(line_id: str) -> FileResponse:
        row = app_state.database.line_context(line_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Zeile nicht gefunden")
        prepared_path = Path(row["prepared_path"]) if row["prepared_path"] else None
        if prepared_path and prepared_path.is_file():
            return FileResponse(prepared_path, media_type="image/png")
        image = load_page(Path(row["source_path"]), int(row["source_page_index"]))
        destination = app_state.paths.cache / "previews" / f"{line_id}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination)
        return FileResponse(destination, media_type="image/png")

    @app.patch("/api/lines/{line_id}")
    def correct(line_id: str, payload: CorrectionPayload) -> dict[str, str]:
        try:
            app_state.database.update_line(line_id, payload.text.strip())
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"status": "gespeichert", "line_id": line_id}

    @app.get("/api/lines/{line_id}")
    def line_details(line_id: str) -> dict[str, Any]:
        row = app_state.database.line_context(line_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Zeile nicht gefunden")
        return {
            "line_id": line_id,
            "text": row["text"],
            "model": row["model"],
            "confidence": row["confidence"],
            "review_status": row["review_status"],
            "readings": [dict(reading) for reading in app_state.database.line_readings(line_id)],
            "engine_runs": [
                dict(run)
                for run in app_state.database.page_engine_runs(
                    row["document_id"], int(row["page_index"])
                )
            ],
        }

    @app.get("/api/review-queue")
    def review_queue(limit: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in app_state.database.review_queue(min(max(limit, 1), 500)):
            item = dict(row)
            item.pop("source_path", None)
            item.update(
                {
                    "bbox": json.loads(row["bbox"]),
                    "reason": "Modelle widersprechen sich oder geringe Sicherheit",
                    "score": 1.0 - float(row["confidence"]),
                    "matched_form": row["text"],
                    "image_url": f"/api/pages/{row['line_id']}/image",
                }
            )
            items.append(item)
        return items

    @app.post("/api/lines/{line_id}/cloud-review")
    def cloud_review(line_id: str, payload: CloudLinePayload) -> dict[str, Any]:
        row = app_state.database.line_context(line_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Zeile nicht gefunden")
        reviewer = OpenRouterReviewer(payload.budget_usd)
        if not reviewer.available():
            raise HTTPException(
                status_code=400,
                detail="Zuerst einen OpenRouter-Schlüssel im Schlüsselbund speichern",
            )
        prepared_path = Path(row["prepared_path"]) if row["prepared_path"] else None
        page = (
            load_page(prepared_path, 0)
            if prepared_path and prepared_path.is_file()
            else load_page(Path(row["source_path"]), int(row["source_page_index"]))
        )
        x1, y1, x2, y2 = json.loads(row["bbox"])
        padding = max(12, round((y2 - y1) * 0.45))
        crop = page.crop(
            (
                max(0, x1 - padding),
                max(0, y1 - padding),
                min(page.width, x2 + padding),
                min(page.height, y2 + padding),
            )
        )
        variants = generate_variants(crop)
        optimized = variants[-1].image if variants else crop
        started = time.monotonic()
        try:
            selected_option = reviewer.option(payload.profile)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            review = reviewer.review(
                crop,
                optimized,
                row["text"],
                row["year"],
                ScriptHint.AUTO,
                profile=payload.profile,
            )
        except (RuntimeError, ValueError, json.JSONDecodeError, httpx.HTTPError) as error:
            app_state.database.record_cloud_usage(
                line_id=line_id,
                model=selected_option.model,
                profile=payload.profile,
                cost_usd=reviewer.spent_usd,
                duration_seconds=time.monotonic() - started,
                status="fehlgeschlagen",
                message=str(error),
            )
            raise HTTPException(status_code=400, detail=str(error)) from error
        reading = Reading(
            id=f"{line_id}:cloud:{uuid.uuid4().hex[:12]}",
            kind=ReadingKind.CLOUD,
            text=review.text,
            model=review.model,
            confidence=review.confidence,
        )
        app_state.database.add_reading(line_id, reading)
        app_state.database.record_cloud_usage(
            line_id=line_id,
            model=review.model,
            profile=payload.profile,
            cost_usd=review.cost,
            duration_seconds=time.monotonic() - started,
            status="erfolgreich",
        )
        return {
            "text": review.text,
            "confidence": review.confidence,
            "model": review.model,
            "cost_usd": review.cost,
            "notes": review.notes,
            "selected": False,
        }

    @app.get("/api/models")
    def models() -> list[dict[str, Any]]:
        return app_state.models.status()

    @app.get("/api/cloud-models")
    def cloud_models() -> list[dict[str, Any]]:
        return cloud_model_status()

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        current = Settings.load(app_state.paths)
        return {name: getattr(current, name) for name in current.__dataclass_fields__}

    @app.put("/api/settings")
    def update_settings(payload: SettingsPayload) -> dict[str, Any]:
        if payload.default_quality not in {
            "schnell",
            "beste_lokale_qualitaet",
            "beste_qualitaet",
            "lizenzklar",
        }:
            raise HTTPException(status_code=400, detail="Unbekanntes Standardprofil")
        if payload.default_script not in {"auto", "handschrift", "druck", "schreibmaschine"}:
            raise HTTPException(status_code=400, detail="Unbekannte Standardschrift")
        if payload.openrouter_profile not in {item["key"] for item in cloud_model_status()}:
            raise HTTPException(status_code=400, detail="Unbekanntes OpenRouter-Modell")
        output_dir = payload.output_dir.strip() if payload.output_dir else None
        if output_dir:
            try:
                output_dir = str(
                    app_state.resolve_output_directory(output_dir, payload.output_token)
                )
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        command = payload.tesseract_command.strip() or "tesseract"
        current_settings = Settings.load(app_state.paths)
        settings = Settings(
            **{
                **payload.model_dump(exclude={"output_token"}),
                "output_dir": output_dir,
                "tesseract_command": command,
                "library_dir": payload.library_dir or current_settings.library_dir,
            }
        )
        settings.save(app_state.paths)
        app_state.settings = settings
        app_state.search = ArchiveSearch(app_state.database, app_state.models)
        app_state.library = LibraryManager(app_state.paths, settings)
        return {"status": "gespeichert", **get_settings()}

    @app.post("/api/settings/output-folder")
    def pick_output_folder() -> dict[str, str | None]:
        process = subprocess.run(
            [
                "osascript",
                "-e",
                'POSIX path of (choose folder with prompt "Ausgabeordner auswählen")',
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if process.returncode != 0 or not process.stdout.strip():
            return {"path": None, "token": None}
        try:
            return app_state.register_output_directory(Path(process.stdout.strip()))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/system")
    def system_status() -> dict[str, Any]:
        documents = app_state.database.rows("SELECT count(*) AS amount FROM documents")[0]["amount"]
        pages = app_state.database.rows("SELECT count(*) AS amount FROM pages")[0]["amount"]
        lines = app_state.database.rows("SELECT count(*) AS amount FROM lines")[0]["amount"]
        models = app_state.models.status()
        settings = Settings.load(app_state.paths)
        return {
            "local": True,
            "version": "0.2.0",
            "documents": documents,
            "pages": pages,
            "lines": lines,
            "models_installed": sum(bool(model["installed"]) for model in models),
            "models_total": len(models),
            "tesseract_available": resolve_executable(settings.tesseract_command) is not None,
            "tesseract_path": resolve_executable(settings.tesseract_command),
            "database": str(app_state.paths.database),
            "output": settings.output_dir or str(app_state.paths.output),
            "library": str(app_state.library.root),
            "library_pending": app_state.library.migration_preview(app_state.database)["pending"],
            "cache": str(app_state.paths.cache),
            "openrouter_configured": OpenRouterReviewer().available(),
            "cloud_usage": app_state.database.cloud_usage_summary(),
            "ground_truth": app_state.database.ground_truth_stats(),
        }

    @app.get("/api/ground-truth")
    def ground_truth_status() -> dict[str, int]:
        return app_state.database.ground_truth_stats()

    @app.post("/api/models/{key}/install", status_code=202)
    def install_model(key: str, payload: ModelInstallPayload) -> dict[str, Any]:
        if key not in MODELS:
            raise HTTPException(status_code=404, detail="Modell nicht gefunden")
        if MODELS[key].requires_acceptance and not payload.accept_license:
            raise HTTPException(status_code=400, detail="Die Modelllizenz muss bestätigt werden")
        try:
            task = app_state.install_model(key, payload.accept_license)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return task.snapshot()

    @app.get("/api/model-installs/{task_id}")
    def model_install_status(task_id: str) -> dict[str, Any]:
        task = app_state.model_jobs.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Installation nicht gefunden")
        return task.snapshot()

    @app.post("/api/openrouter-key")
    def save_openrouter_key(payload: OpenRouterKeyPayload) -> dict[str, Any]:
        key = payload.key.strip()
        if not key:
            raise HTTPException(status_code=400, detail="API-Schlüssel fehlt")
        reviewer = OpenRouterReviewer(api_key=key)
        try:
            status = reviewer.key_status(validate=payload.verify_key)
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=400,
                detail="Schlüssel konnte bei OpenRouter nicht bestätigt werden",
            ) from error
        OpenRouterReviewer.save_api_key(key)
        return {"status": "im macOS-Schlüsselbund gespeichert", **status}

    @app.get("/api/openrouter-key")
    def openrouter_key_status(validate: bool = False) -> dict[str, Any]:
        try:
            return OpenRouterReviewer().key_status(validate=validate)
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=400, detail="OpenRouter-Prüfung fehlgeschlagen"
            ) from error

    @app.delete("/api/openrouter-key")
    def delete_openrouter_key() -> dict[str, str]:
        OpenRouterReviewer.delete_api_key()
        return {"status": "entfernt"}

    @app.get("/api/output/{token}")
    def output(token: str) -> FileResponse:
        return FileResponse(app_state.download(token))

    return app


def launch(*, open_browser: bool = True, port: int = 7860) -> None:
    import uvicorn

    url = f"http://127.0.0.1:{port}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="info")
