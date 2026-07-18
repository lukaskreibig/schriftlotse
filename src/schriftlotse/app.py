from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
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
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from schriftlotse.cloud import OpenRouterReviewer, cloud_model_status
from schriftlotse.config import AppPaths, Settings
from schriftlotse.database import Database
from schriftlotse.domain import (
    CloudPolicy,
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
from schriftlotse.ingest import load_page
from schriftlotse.model_registry import MODELS, ModelManager
from schriftlotse.pipeline import ProcessingPipeline
from schriftlotse.preprocessing import generate_variants
from schriftlotse.search import ArchiveSearch


class JobPayload(BaseModel):
    sources: list[str]
    year: int | None = Field(default=None, ge=800, le=2100)
    script: ScriptHint = ScriptHint.AUTO
    quality: QualityProfile = QualityProfile.BEST_LOCAL
    cloud: bool = False
    cloud_budget_usd: float = Field(default=1.0, ge=0, le=100)


class SearchPayload(BaseModel):
    text: str
    mode: SearchMode = SearchMode.SMART
    fuzziness: float = Field(default=0.72, ge=0, le=1)
    year_from: int | None = None
    year_to: int | None = None
    limit: int = Field(default=50, ge=1, le=500)


class CorrectionPayload(BaseModel):
    text: str


class ModelInstallPayload(BaseModel):
    accept_license: bool = False


class CloudLinePayload(BaseModel):
    budget_usd: float = Field(default=0.5, gt=0, le=10)
    profile: str = "fast"


class SettingsPayload(BaseModel):
    advanced_models: bool = True
    semantic_search: bool = True
    cloud_budget_usd: float = Field(default=1.0, ge=0, le=100)
    output_dir: str | None = None
    tesseract_command: str = "tesseract"
    default_quality: str = "beste_lokale_qualitaet"
    default_script: str = "auto"
    openrouter_profile: str = "fast"
    show_preprocessing: bool = True
    output_token: str | None = None


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

    def emit(self, message: str, progress: float) -> None:
        self.status = "läuft"
        self.message = message
        # Page splitting can increase the denominator after work has started;
        # the visible bar must nevertheless never jump backwards.
        self.progress = max(self.progress, max(0.0, min(1.0, progress)))
        if not self.history or self.history[-1] != message:
            self.history.append(message)
            self.history = self.history[-80:]
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
        self.jobs: dict[str, JobRuntime] = {}
        self.model_jobs: dict[str, ModelInstallRuntime] = {}
        self.authorized_sources: dict[str, Path] = {}
        self.authorized_output_dirs: dict[str, Path] = {}
        self.downloads: dict[str, Path] = {}
        self.lock = threading.Lock()
        # Apple Silicon unified memory is shared by all local engines. One
        # heavy OCR/model task at a time prevents avoidable system pressure.
        self.processing_slot = threading.Semaphore(1)

    def register_source(self, path: Path, display_name: str | None = None) -> dict[str, str]:
        resolved = path.expanduser().resolve()
        token = uuid.uuid4().hex
        with self.lock:
            self.authorized_sources[token] = resolved
        return {"id": token, "name": display_name or resolved.name}

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

    def create_job(self, payload: JobPayload) -> JobRuntime:
        with self.lock:
            sources = [self.authorized_sources.get(token) for token in payload.sources]
        if (
            not sources
            or any(path is None for path in sources)
            or not all(path.exists() for path in sources if path is not None)
        ):
            raise ValueError(
                "Mindestens eine in SchriftLotse ausgewählte Datei oder ein Ordner ist erforderlich"
            )
        resolved_sources = [path for path in sources if path is not None]
        request = DocumentRequest(
            sources=resolved_sources,
            year=payload.year,
            script_hint=payload.script,
            quality_profile=payload.quality,
            # Cloud review is intentionally never part of an unattended job.
            # It is only available for a crop explicitly selected by the user.
            cloud_policy=CloudPolicy.LOCAL_ONLY,
            cloud_budget_usd=payload.cloud_budget_usd,
            advanced_models=payload.quality != QualityProfile.FAST,
        )
        runtime_id = uuid.uuid4().hex
        pipeline = ProcessingPipeline(self.paths, Settings.load(self.paths), self.database)
        runtime = JobRuntime(runtime_id, pipeline)
        with self.lock:
            self.jobs[runtime_id] = runtime

        def worker() -> None:
            try:
                runtime.message = "Wartet auf freien lokalen Modellplatz"
                runtime.events.put(runtime.snapshot())
                with self.processing_slot:
                    _pipeline_id, _results, exports = pipeline.run(
                        request, progress=runtime.emit, job_id=runtime_id
                    )
                if runtime.status == "abgebrochen":
                    return
                runtime.status = "fertig"
                runtime.progress = 1.0
                runtime.message = "Verarbeitung abgeschlossen"
                runtime.exports = self.register_downloads(exports)
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
    if document is None or not document["output_dir"]:
        raise ValueError("Dokument oder Ausgabeordner nicht gefunden")
    output_dir = Path(document["output_dir"]).expanduser().resolve()
    result_path = output_dir / "result.json"
    if not result_path.is_file():
        raise ValueError("Die technische Ergebnisdatei des Dokuments fehlt")
    from schriftlotse.domain import DocumentResult

    result = DocumentResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    current_rows = state.database.rows(
        "SELECT id,text,manually_corrected,review_status FROM lines WHERE document_id=?",
        (document_id,),
    )
    current = {row["id"]: row for row in current_rows}
    for page in result.pages:
        for line in page.lines:
            row = current.get(line.id)
            if row is None:
                continue
            line.text = row["text"]
            line.manually_corrected = bool(row["manually_corrected"])
            line.review_status = ReviewStatus(row["review_status"])
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
    def health() -> dict[str, Any]:
        """Small local readiness probe for the native wrapper and diagnostics."""
        return {
            "status": "bereit",
            "local": True,
            "version": "0.2.0",
            "instance_token": os.getenv("SCHRIFTLOTSE_INSTANCE_TOKEN", "browser"),
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

    @app.post("/api/jobs", status_code=202)
    def create_job(payload: JobPayload) -> dict[str, Any]:
        try:
            return app_state.create_job(payload).snapshot()
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/recovery")
    def recovery() -> list[dict[str, Any]]:
        return [dict(row) for row in app_state.database.list_incomplete_jobs()]

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
    def documents() -> list[dict[str, Any]]:
        return [dict(row) for row in app_state.database.list_documents()]

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
        return [
            {
                **hit.model_dump(mode="json"),
                "image_url": f"/api/pages/{hit.line_id}/image",
            }
            for hit in hits
        ]

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
        }

    @app.get("/api/review-queue")
    def review_queue(limit: int = 100) -> list[dict[str, Any]]:
        return [
            {
                **dict(row),
                "bbox": json.loads(row["bbox"]),
                "reason": "Modelle widersprechen sich oder geringe Sicherheit",
                "score": 1.0 - float(row["confidence"]),
                "matched_form": row["text"],
                "image_url": f"/api/pages/{row['line_id']}/image",
            }
            for row in app_state.database.review_queue(min(max(limit, 1), 500))
        ]

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
            raise HTTPException(status_code=400, detail=str(error)) from error
        reading = Reading(
            id=f"{line_id}:cloud:{uuid.uuid4().hex[:12]}",
            kind=ReadingKind.CLOUD,
            text=review.text,
            model=review.model,
            confidence=review.confidence,
        )
        app_state.database.add_reading(line_id, reading)
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
        if payload.default_quality not in {"schnell", "beste_lokale_qualitaet", "lizenzklar"}:
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
        settings = Settings(
            **{
                **payload.model_dump(exclude={"output_token"}),
                "output_dir": output_dir,
                "tesseract_command": command,
            }
        )
        settings.save(app_state.paths)
        app_state.settings = settings
        app_state.search = ArchiveSearch(app_state.database, app_state.models)
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
            "tesseract_available": shutil.which(settings.tesseract_command) is not None,
            "database": str(app_state.paths.database),
            "output": settings.output_dir or str(app_state.paths.output),
            "cache": str(app_state.paths.cache),
            "openrouter_configured": OpenRouterReviewer().available(),
        }

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
