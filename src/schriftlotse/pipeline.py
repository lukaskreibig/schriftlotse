from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from schriftlotse.cloud import OpenRouterReviewer
from schriftlotse.config import AppPaths, Settings
from schriftlotse.database import Database
from schriftlotse.domain import (
    CloudPolicy,
    DocumentRequest,
    DocumentResult,
    JobStatus,
    LayoutClass,
    PageResult,
    Reading,
    ReadingKind,
    RegionResult,
    ScriptHint,
)
from schriftlotse.exports import export_document
from schriftlotse.ingest import discover_documents, iter_document_pages, pdf_text_layer
from schriftlotse.library import LibraryManager
from schriftlotse.ocr import RecognizerRouter
from schriftlotse.preprocessing import (
    generate_variants,
    profile_page,
    select_preflight_variants,
    split_logical_pages,
)

ProgressCallback = Callable[[str, float], None]
LiveCallback = Callable[[dict[str, Any]], None]


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
        self.library = LibraryManager(self.paths, self.settings)
        self.live_callback: LiveCallback | None = None
        self._cancel = threading.Event()

    def _live(self, **payload: Any) -> None:
        if self.live_callback is not None:
            self.live_callback(payload)

    def cancel(self) -> None:
        self._cancel.set()

    def run(
        self,
        request: DocumentRequest,
        progress: ProgressCallback | None = None,
        job_id: str | None = None,
    ) -> tuple[str, list[DocumentResult], list[Path]]:
        job_id = job_id or uuid.uuid4().hex
        self.database.create_job(
            job_id,
            json.dumps(request.model_dump(mode="json"), ensure_ascii=False),
        )
        self.database.update_job(job_id, JobStatus.RUNNING)
        if progress:
            progress("Scan wird analysiert · Dateien und Seiten werden erfasst", 0.001)
        documents = discover_documents(
            request.sources, group_images_by_folder=request.group_images_by_folder
        )
        if not documents:
            self.database.update_job(
                job_id, JobStatus.FAILED, "Keine unterstützten Dateien gefunden"
            )
            raise ValueError("Keine unterstützten Dateien gefunden")
        if request.target_document_id:
            if len(documents) != 1:
                raise ValueError("Gezielte Neuverarbeitung benötigt genau ein gruppiertes Dokument")
            documents[0].id = request.target_document_id
        # Starts as the number of physical source pages and grows if a spread is
        # split.  This keeps the denominator truthful for checkpoints and ETA.
        total_pages = sum(document.page_count for document in documents)
        completed = 0
        results: list[DocumentResult] = []
        exports: list[Path] = []
        self.settings.advanced_models = request.advanced_models
        self.router.quality_profile = request.quality_profile
        if (
            progress
            and request.advanced_models
            and request.quality_profile.value in {"beste_lokale_qualitaet", "beste_qualitaet"}
            and not self.router.manager.is_installed("churro-mlx-8bit")
        ):
            progress(
                "Beste verfügbare lokale Modelle aktiv; CHURRO ist noch nicht installiert",
                0.02,
            )
        try:
            cloud_reviewer = (
                OpenRouterReviewer(request.cloud_budget_usd)
                if request.cloud_policy == CloudPolicy.ADAPTIVE
                else None
            )
            if cloud_reviewer is not None and not cloud_reviewer.available():
                raise RuntimeError(
                    "Adaptive Cloud-Prüfung gewählt, aber kein OpenRouter-Schlüssel gespeichert"
                )
            for document in documents:
                if self._cancel.is_set():
                    self.database.update_job(
                        job_id, JobStatus.CANCELLED, "Durch Benutzer abgebrochen"
                    )
                    return job_id, results, exports
                metadata = request.document_metadata.get(document.id)
                document_year = metadata.year if metadata and metadata.year else request.year
                document_script = metadata.script_hint if metadata else request.script_hint
                if metadata and metadata.title and metadata.title.strip():
                    document.title = metadata.title.strip()
                original_sources = list(document.source_paths)
                if progress:
                    progress(
                        f"{document.title} · Originale werden sicher in die Bibliothek übernommen",
                        min(0.03, completed / max(total_pages, 1)),
                    )
                existing_managed = self.library.existing_managed_files(self.database, document.id)
                existing_paths = {
                    item.managed_path.expanduser().resolve() for item in existing_managed
                }
                if (
                    existing_managed
                    and {path.expanduser().resolve() for path in original_sources} == existing_paths
                ):
                    managed_files = existing_managed
                else:
                    managed_files = self.library.adopt_sources(document.id, original_sources)
                document.source_paths = [item.managed_path for item in managed_files]
                thumbnail_path = self.library.make_thumbnail(
                    document.id, document.source_paths[0], 0
                )
                self.database.register_document_shell(
                    job_id, document, document_year, document_script
                )
                self.database.mark_document_managed(
                    document.id,
                    managed_files,
                    {str(item.original_path): str(item.managed_path) for item in managed_files},
                    thumbnail_path,
                )
                pages: list[PageResult] = []
                logical_page_index = 0
                for source_page_index, source_path, source_image in iter_document_pages(document):
                    if self._cancel.is_set():
                        break
                    logical_pages = split_logical_pages(
                        source_image, f"{document.id}-{source_page_index:04d}"
                    )
                    total_pages += max(0, len(logical_pages) - 1)
                    for logical in logical_pages:
                        page_index = logical_page_index
                        logical_page_index += 1
                        image = logical.image
                        checkpoint_path = (
                            self.paths.cache
                            / "checkpoints"
                            / job_id
                            / document.id
                            / f"{page_index:04d}.json"
                        )
                        if checkpoint_path.is_file():
                            try:
                                restored = PageResult.model_validate_json(
                                    checkpoint_path.read_text(encoding="utf-8")
                                )
                                if (
                                    restored.logical_page_id == logical.id
                                    and restored.source_path == source_path
                                ):
                                    pages.append(restored)
                                    completed += 1
                                    if progress:
                                        progress(
                                            f"{document.title}: Seite {page_index + 1} "
                                            "aus sicherem Zwischenstand geladen",
                                            min(0.95, completed / max(total_pages, 1)),
                                        )
                                    continue
                            except (ValueError, OSError):
                                pass
                        prepared_path = (
                            self.library.prepared_dir(document.id) / f"{page_index:04d}.png"
                        )
                        prepared_path.parent.mkdir(parents=True, exist_ok=True)
                        image.save(prepared_path, format="PNG")
                        live_preview_path = self.library.make_page_preview(
                            document.id, page_index, image
                        )
                        page_share = 1.0 / max(total_pages, 1)
                        page_start = min(0.94, completed / max(total_pages, 1))
                        suffix = (
                            f" · {logical.id.rsplit('-', 1)[-1]}" if len(logical_pages) > 1 else ""
                        )
                        page_label = (
                            f"{document.title}: Seite {source_page_index + 1}/"
                            f"{document.page_count}{suffix}"
                        )
                        self._live(
                            stage="voranalyse",
                            document_id=document.id,
                            document_title=document.title,
                            page_index=page_index,
                            page_number=page_index + 1,
                            page_count=total_pages,
                            preview_path=str(live_preview_path),
                            model="Bildanalyse",
                            width=image.width,
                            height=image.height,
                            boxes=[],
                        )
                        self.database.update_job_page(
                            job_id,
                            document.id,
                            page_index,
                            logical.id,
                            "voranalyse",
                            "läuft",
                            page_label,
                        )
                        if progress:
                            progress(
                                f"{page_label} · Orientierung, Seitenrand und Bundsteg "
                                "werden geprüft",
                                min(0.94, page_start + 0.05 * page_share),
                            )
                        variants = generate_variants(image)
                        selected = select_preflight_variants(
                            variants,
                            limit=1 if request.quality_profile.value == "schnell" else 2,
                        )
                        routing_script, probe_text, probe_confidence = (
                            self.router.preclassify_print(image, document_script)
                        )
                        if (
                            routing_script == ScriptHint.AUTO
                            and len(probe_text) >= 200
                            and probe_confidence >= 0.45
                            and re.search(
                                r"zeitung|tageblatt|anzeige|bekanntmachung|druck",
                                source_path.stem,
                                flags=re.IGNORECASE,
                            )
                        ):
                            routing_script = ScriptHint.PRINT
                        if (
                            request.advanced_models
                            and request.quality_profile.value
                            in {"beste_lokale_qualitaet", "beste_qualitaet"}
                            and routing_script not in {ScriptHint.PRINT, ScriptHint.TYPEWRITER}
                            and self.router.manager.is_installed("churro-mlx-8bit")
                            and all(variant.metadata.name != "normalisiert" for variant in selected)
                        ):
                            selected.append(
                                next(
                                    variant
                                    for variant in variants
                                    if variant.metadata.name == "normalisiert"
                                )
                            )
                        if progress:
                            progress(
                                f"{page_label} · Beleuchtung und Kontrast angepasst",
                                min(0.94, page_start + 0.22 * page_share),
                            )
                            if routing_script == ScriptHint.PRINT:
                                progress(
                                    f"{page_label} · Druckschrift sicher vorerkannt "
                                    f"({probe_confidence:.0%})",
                                    min(0.94, page_start + 0.27 * page_share),
                                )
                            progress(
                                f"{page_label} · lokale OCR-/HTR-Modelle arbeiten",
                                min(0.94, page_start + 0.30 * page_share),
                            )
                        self._live(
                            stage="ocr",
                            document_id=document.id,
                            document_title=document.title,
                            page_index=page_index,
                            page_number=page_index + 1,
                            page_count=total_pages,
                            preview_path=str(live_preview_path),
                            model="Lokale Modellroute",
                            width=image.width,
                            height=image.height,
                            boxes=[],
                        )
                        self.database.update_job_page(
                            job_id,
                            document.id,
                            page_index,
                            logical.id,
                            "ocr",
                            "läuft",
                            "Lokale Modelle arbeiten",
                        )
                        preliminary_profile = profile_page(
                            image,
                            filename=source_path.name,
                            quick_text=probe_text,
                            year_hint=document_year,
                            script_hint=routing_script,
                        )
                        routing_year = document_year or preliminary_profile.period.exact_year
                        if routing_year is None and probe_text:
                            # A single OCR year is useful for model routing, but is
                            # intentionally not persisted as trusted metadata.
                            candidates = [
                                int(value)
                                for value in re.findall(
                                    r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)", probe_text
                                )
                            ]
                            if candidates:
                                routing_year = max(set(candidates), key=candidates.count)
                        if routing_script == ScriptHint.PRINT:
                            route_reason = (
                                f"Druck/Fraktur wurde mit {probe_confidence:.0%} "
                                "vorerkannt; spezialisierte Handschriftmodelle werden übersprungen."
                            )
                        elif routing_year is not None and routing_year < 1800:
                            route_reason = (
                                f"Jahr {routing_year}: frühe Kurrent und CHURRO werden verglichen."
                            )
                        elif routing_year is not None and routing_year <= 1945:
                            route_reason = (
                                f"Jahr {routing_year}: TrOCR Kurrent des 19./20. Jahrhunderts "
                                "und CHURRO werden verglichen."
                            )
                        else:
                            route_reason = (
                                "Keine sichere Datierung: die allgemeine lokale Modellroute "
                                "entscheidet anhand Schrift und Textqualität."
                            )
                        self._live(
                            stage="modellwahl",
                            document_id=document.id,
                            document_title=document.title,
                            page_index=page_index,
                            page_number=page_index + 1,
                            page_count=total_pages,
                            preview_path=str(live_preview_path),
                            model="Modelle werden verglichen",
                            width=image.width,
                            height=image.height,
                            boxes=[],
                            script=preliminary_profile.script.value,
                            period=preliminary_profile.period.model_dump(mode="json"),
                            evidence=preliminary_profile.evidence,
                            reason=route_reason,
                            print_probe_confidence=probe_confidence,
                        )
                        candidate = self.router.recognize_variants(
                            selected, routing_year, routing_script
                        )
                        quick_text = "\n".join(line.text for line in candidate.lines)
                        existing_pdf_text = (
                            pdf_text_layer(source_path, source_page_index)
                            if document.kind == "pdf"
                            else ""
                        )
                        page_profile = profile_page(
                            image,
                            filename=source_path.name,
                            quick_text=f"{quick_text}\n{existing_pdf_text}",
                            year_hint=document_year,
                            script_hint=routing_script,
                            selected_model=candidate.model,
                        )
                        if progress:
                            progress(
                                f"{page_label} · erkannt mit {candidate.model}",
                                min(0.94, page_start + 0.82 * page_share),
                            )
                        self._live(
                            stage="auswertung",
                            document_id=document.id,
                            document_title=document.title,
                            page_index=page_index,
                            page_number=page_index + 1,
                            page_count=total_pages,
                            preview_path=str(live_preview_path),
                            model=candidate.model,
                            width=image.width,
                            height=image.height,
                            boxes=[list(line.bbox) for line in candidate.lines[:250]],
                            confidence=(
                                sum(line.confidence for line in candidate.lines)
                                / len(candidate.lines)
                                if candidate.lines
                                else 0.0
                            ),
                            script=page_profile.script.value,
                            period=page_profile.period.model_dump(mode="json"),
                            evidence=page_profile.evidence,
                            reason=route_reason,
                            engines=[
                                {
                                    "engine": run.engine,
                                    "backend": run.backend,
                                    "duration_seconds": run.duration_seconds,
                                    "success": run.success,
                                    "message": run.message,
                                }
                                for run in (candidate.engine_runs or [])
                            ],
                        )
                        warnings: list[str] = list(logical.warnings)
                        if candidate.coverage < 0.35:
                            warnings.append(
                                "Ergebnis wahrscheinlich unvollständig – "
                                "zu wenig Textfläche erkannt"
                            )
                        if candidate.expected_cer > 0.10:
                            warnings.append(
                                "Niedrige Erkennungssicherheit – manuelle Prüfung empfohlen"
                            )
                        if page_profile.requires_review:
                            warnings.append(
                                "Jahresangabe und sichtbarer Jahreskandidat widersprechen sich"
                            )
                        if existing_pdf_text and candidate.lines:
                            candidate.lines[0].readings.append(
                                Reading(
                                    id=f"{logical.id}:pdf-text",
                                    kind=ReadingKind.PDF_TEXT,
                                    text=existing_pdf_text,
                                    model="eingebettete PDF-Textschicht",
                                    confidence=0.35,
                                )
                            )
                            warnings.append(
                                "Vorhandene PDF-Textschicht wurde nur als unbestätigte "
                                "Alternative übernommen"
                            )
                        for order, line in enumerate(candidate.lines):
                            old_id = line.id
                            line.id = f"{document.id}-{page_index:04d}-{order:04d}"
                            for reading_index, reading in enumerate(line.readings):
                                reading.id = f"{line.id}:{reading.kind.value}:{reading_index}"
                            if not line.polygon:
                                x1, y1, x2, y2 = line.bbox
                                line.polygon = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
                            if not line.baseline:
                                x1, _, x2, y2 = line.bbox
                                line.baseline = [(x1, y2 - 2), (x2, y2 - 2)]
                            del old_id
                        if cloud_reviewer is not None and candidate.lines:
                            cloud_profile = (
                                "fast"
                                if page_profile.layout in {LayoutClass.FORM, LayoutClass.TABLE}
                                else request.cloud_model_profile
                            )
                            if progress:
                                progress(
                                    f"{page_label} · unsichere Stellen werden per Cloud "
                                    "gegengeprüft",
                                    min(0.94, page_start + 0.88 * page_share),
                                )
                            cloud_warning = self._review_uncertain_lines(
                                cloud_reviewer,
                                image,
                                candidate.lines,
                                routing_year,
                                routing_script,
                                cloud_profile,
                                job_id,
                            )
                            if cloud_warning:
                                warnings.append(cloud_warning)
                        mean_confidence = (
                            sum(line.confidence for line in candidate.lines) / len(candidate.lines)
                            if candidate.lines
                            else 0.0
                        )
                        region_id = f"{document.id}-{page_index:04d}-region-0000"
                        for line in candidate.lines:
                            line.region_id = region_id
                        page_result = PageResult(
                            page_index=page_index,
                            source_path=source_path,
                            source_page_index=source_page_index,
                            prepared_path=prepared_path,
                            width=image.width,
                            height=image.height,
                            lines=candidate.lines,
                            mean_confidence=mean_confidence,
                            expected_cer=candidate.expected_cer,
                            selected_variant=candidate.variant,
                            selected_model=candidate.model,
                            warnings=warnings,
                            logical_page_id=logical.id,
                            source_bbox=logical.source_bbox,
                            transform=logical.transform,
                            profile=page_profile,
                            regions=[
                                RegionResult(
                                    id=region_id,
                                    polygon=[
                                        (0, 0),
                                        (image.width, 0),
                                        (image.width, image.height),
                                        (0, image.height),
                                    ],
                                    reading_order=0,
                                )
                            ],
                            engine_runs=candidate.engine_runs or [],
                            image_diagnostics=next(
                                (
                                    variant.metadata.diagnostics
                                    for variant in variants
                                    if variant.metadata.name == candidate.variant
                                ),
                                variants[0].metadata.diagnostics if variants else None,
                            ),
                        )
                        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                        checkpoint_path.write_text(
                            page_result.model_dump_json(indent=2), encoding="utf-8"
                        )
                        pages.append(page_result)
                        self.database.update_job_page(
                            job_id,
                            document.id,
                            page_index,
                            logical.id,
                            "fertig",
                            "fertig",
                        )
                        completed += 1
                        if progress:
                            progress(
                                f"{page_label} · Seite fertig",
                                min(0.95, (completed - 0.10) / max(total_pages, 1)),
                            )
                document.page_count = len(pages)
                detected_years = [
                    page.profile.period.exact_year
                    for page in pages
                    if page.profile.period.exact_year is not None
                ]
                result_year = document_year
                if result_year is None and detected_years:
                    result_year = max(set(detected_years), key=detected_years.count)
                result = DocumentResult(
                    document=document,
                    year=result_year,
                    script_hint=document_script,
                    pages=pages,
                )
                output_dir = self.library.derived_dir(document.id)
                if progress:
                    progress(
                        f"{document.title} · Ausgabedateien werden formatiert",
                        min(0.97, completed / max(total_pages, 1)),
                    )
                exports.extend(export_document(result, output_dir))
                if self.settings.output_dir:
                    external_output = (
                        Path(self.settings.output_dir)
                        / f"{slugify(document.title)}-{document.id[:8]}"
                    )
                    if external_output.resolve() != output_dir.resolve():
                        shutil.copytree(output_dir, external_output, dirs_exist_ok=True)
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
            self.database.mark_job_documents_status(job_id, "fehlgeschlagen")
            self.database.update_job(job_id, JobStatus.FAILED, str(error))
            raise

    def _review_uncertain_lines(
        self,
        reviewer: OpenRouterReviewer,
        image: Any,
        lines: list[Any],
        year: int | None,
        script_hint: ScriptHint,
        profile: str,
        job_id: str,
    ) -> str | None:
        """Review a small, risk-ranked subset without replacing local readings."""
        import time

        uncertain = sorted(
            (line for line in lines if line.review_status.value == "unsicher"),
            key=lambda line: line.confidence,
        )[:4]
        for line in uncertain:
            if reviewer.spent_usd >= reviewer.budget_usd:
                return "Cloud-Kostenlimit erreicht; weitere Stellen bleiben lokal"
            x1, y1, x2, y2 = line.bbox
            padding = max(12, round((y2 - y1) * 0.45))
            crop = image.crop(
                (
                    max(0, x1 - padding),
                    max(0, y1 - padding),
                    min(image.width, x2 + padding),
                    min(image.height, y2 + padding),
                )
            )
            started = time.monotonic()
            option = reviewer.option(profile)
            try:
                review = reviewer.review(
                    crop,
                    crop,
                    line.text,
                    year,
                    script_hint,
                    profile=profile,
                )
            except Exception as error:
                self.database.record_cloud_usage(
                    job_id=job_id,
                    line_id=None,
                    model=option.model,
                    profile=profile,
                    cost_usd=reviewer.spent_usd,
                    duration_seconds=time.monotonic() - started,
                    status="fehlgeschlagen",
                    message=str(error),
                )
                return f"Cloud-Zweitprüfung beendet: {error}"
            line.readings.append(
                Reading(
                    id=f"{line.id}:cloud:{uuid.uuid4().hex[:12]}",
                    kind=ReadingKind.CLOUD,
                    text=review.text,
                    model=review.model,
                    confidence=review.confidence,
                )
            )
            self.database.record_cloud_usage(
                job_id=job_id,
                line_id=None,
                model=review.model,
                profile=profile,
                cost_usd=review.cost,
                duration_seconds=time.monotonic() - started,
                status="erfolgreich",
            )
        return None

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
