from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
from PIL import Image

from schriftlotse.cloud import OpenRouterReviewer
from schriftlotse.config import AppPaths, Settings
from schriftlotse.database import Database
from schriftlotse.domain import (
    AlternativeReading,
    CloudPolicy,
    DocumentRequest,
    DocumentResult,
    JobStatus,
    LineResult,
    PageResult,
)
from schriftlotse.exports import export_document
from schriftlotse.ingest import discover_documents, iter_document_pages
from schriftlotse.ocr import RecognizerRouter
from schriftlotse.preprocessing import (
    PreparedVariant,
    generate_variants,
    select_preflight_variants,
)

ProgressCallback = Callable[[str, float], None]


def slugify(value: str) -> str:
    text = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE).strip("-.")
    return text or "dokument"


class ProcessingPipeline:
    def __init__(
        self,
        paths: AppPaths | None = None,
        settings: Settings | None = None,
        database: Database | None = None,
    ) -> None:
        self.paths = paths or AppPaths.default()
        self.paths.ensure()
        self.settings = settings or Settings.load(self.paths)
        self.database = database or Database(self.paths.database)
        self.router = RecognizerRouter(self.paths, self.settings)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(
        self,
        request: DocumentRequest,
        progress: ProgressCallback | None = None,
    ) -> tuple[str, list[DocumentResult], list[Path]]:
        job_id = uuid.uuid4().hex
        self.database.create_job(job_id)
        self.database.update_job(job_id, JobStatus.RUNNING)
        if progress:
            progress("Dateien und Seiten werden erfasst", 0.01)
        documents = discover_documents(request.sources)
        if not documents:
            self.database.update_job(
                job_id, JobStatus.FAILED, "Keine unterstützten Dateien gefunden"
            )
            raise ValueError("Keine unterstützten Dateien gefunden")
        total_pages = sum(document.page_count for document in documents)
        completed = 0
        results: list[DocumentResult] = []
        exports: list[Path] = []
        reviewer = (
            OpenRouterReviewer(request.cloud_budget_usd)
            if request.cloud_policy == CloudPolicy.ADAPTIVE
            else None
        )
        self.settings.advanced_models = request.advanced_models
        try:
            for document in documents:
                if self._cancel.is_set():
                    self.database.update_job(
                        job_id, JobStatus.CANCELLED, "Durch Benutzer abgebrochen"
                    )
                    return job_id, results, exports
                pages: list[PageResult] = []
                for page_index, source_path, image in iter_document_pages(document):
                    if self._cancel.is_set():
                        break
                    page_share = 1.0 / max(total_pages, 1)
                    page_start = completed / max(total_pages, 1)
                    page_label = f"{document.title}: Seite {page_index + 1}/{document.page_count}"
                    if progress:
                        progress(
                            f"{page_label} · Scan wird analysiert", page_start + 0.05 * page_share
                        )
                    variants = generate_variants(image)
                    selected = select_preflight_variants(variants, limit=2)
                    if progress:
                        progress(
                            f"{page_label} · Helligkeit, Kontrast und Schieflage angepasst",
                            page_start + 0.22 * page_share,
                        )
                        progress(
                            f"{page_label} · lokale OCR-/HTR-Modelle arbeiten",
                            page_start + 0.30 * page_share,
                        )
                    candidate = self.router.recognize_variants(
                        selected, request.year, request.script_hint
                    )
                    if progress:
                        progress(
                            f"{page_label} · erkannt mit {candidate.model}",
                            page_start + 0.82 * page_share,
                        )
                    warnings: list[str] = []
                    if candidate.expected_cer > 0.10:
                        warnings.append(
                            "Niedrige Erkennungssicherheit – manuelle Prüfung empfohlen"
                        )
                    if (
                        reviewer is not None
                        and reviewer.available()
                        and candidate.expected_cer > 0.10
                        and candidate.lines
                    ):
                        if progress:
                            progress(
                                f"{page_label} · optionale Zweitlesung wird geprüft",
                                page_start + 0.86 * page_share,
                            )
                        self._cloud_review(
                            reviewer,
                            image,
                            selected,
                            candidate.lines,
                            request,
                            warnings,
                        )
                    for order, line in enumerate(candidate.lines):
                        line.id = f"{document.id}-{page_index:04d}-{order:04d}"
                    mean_confidence = (
                        sum(line.confidence for line in candidate.lines) / len(candidate.lines)
                        if candidate.lines
                        else 0.0
                    )
                    pages.append(
                        PageResult(
                            page_index=page_index,
                            source_path=source_path,
                            width=image.width,
                            height=image.height,
                            lines=candidate.lines,
                            mean_confidence=mean_confidence,
                            expected_cer=candidate.expected_cer,
                            selected_variant=candidate.variant,
                            selected_model=candidate.model,
                            warnings=warnings,
                        )
                    )
                    completed += 1
                    if progress:
                        progress(
                            f"{page_label} · Seite fertig",
                            (completed - 0.10) / max(total_pages, 1),
                        )
                result = DocumentResult(
                    document=document,
                    year=request.year,
                    script_hint=request.script_hint,
                    pages=pages,
                )
                base_output = (
                    Path(self.settings.output_dir)
                    if self.settings.output_dir
                    else self.paths.output
                )
                output_dir = base_output / f"{slugify(document.title)}-{document.id[:8]}"
                if progress:
                    progress(
                        f"{document.title} · Ausgabedateien werden formatiert",
                        min(0.97, completed / max(total_pages, 1)),
                    )
                exports.extend(export_document(result, output_dir))
                if progress:
                    progress(
                        f"{document.title} · Suchindex wird aktualisiert",
                        min(0.98, completed / max(total_pages, 1)),
                    )
                self.database.save_document(job_id, result)
                results.append(result)
            index_path = self._write_batch_index(job_id, results)
            exports.append(index_path)
            self.database.update_job(
                job_id, JobStatus.DONE, f"{len(results)} Dokumente verarbeitet"
            )
            if progress:
                progress("Verarbeitung abgeschlossen", 1.0)
            return job_id, results, exports
        except Exception as error:
            self.database.update_job(job_id, JobStatus.FAILED, str(error))
            raise

    @staticmethod
    def _cloud_review(
        reviewer: OpenRouterReviewer,
        original: Image.Image,
        variants: list[PreparedVariant],
        lines: list[LineResult],
        request: DocumentRequest,
        warnings: list[str],
    ) -> None:
        if not variants:
            return
        local_text = "\n".join(line.text for line in lines)
        try:
            review = reviewer.review(
                original,
                variants[-1].image,
                local_text,
                request.year,
                request.script_hint,
            )
        except (RuntimeError, ValueError, json.JSONDecodeError, httpx.HTTPError):
            warnings.append("Optionale Cloud-Prüfung war nicht verfügbar")
            return
        if review.text and review.text != local_text and lines:
            lines[0].alternatives.append(
                AlternativeReading(
                    text=review.text, model=review.model, confidence=review.confidence
                )
            )
            warnings.append(f"Cloud-Zweitlesung vorhanden ({review.model}, ${review.cost:.4f})")

    def _write_batch_index(self, job_id: str, results: list[DocumentResult]) -> Path:
        directory = self.paths.output / f"auftrag-{job_id[:8]}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "stapelindex.json"
        payload = [
            {
                "document_id": result.document.id,
                "title": result.document.title,
                "pages": len(result.pages),
                "output_dir": str(result.output_dir),
            }
            for result in results
        ]
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path
