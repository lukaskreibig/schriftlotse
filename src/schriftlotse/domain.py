from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ScriptHint(StrEnum):
    AUTO = "auto"
    HANDWRITING = "handschrift"
    PRINT = "druck"
    TYPEWRITER = "schreibmaschine"


class CloudPolicy(StrEnum):
    LOCAL_ONLY = "nur_lokal"
    ADAPTIVE = "adaptiv"


class QualityProfile(StrEnum):
    FAST = "schnell"
    BEST_LOCAL = "beste_lokale_qualitaet"
    ADAPTIVE = "beste_qualitaet"
    LICENSE_CLEAR = "lizenzklar"


class ScriptClass(StrEnum):
    FRAKTUR = "fraktur"
    ANTIQUA = "antiqua"
    KURRENT = "kurrent"
    SUETTERLIN = "suetterlin"
    TYPEWRITER = "schreibmaschine"
    MIXED = "gemischt"
    UNKNOWN = "unbekannt"


class LayoutClass(StrEnum):
    PLAIN = "fliesstext"
    FORM = "formular"
    TABLE = "tabelle"
    MAP = "karte_plan"
    SPREAD = "doppelseite"
    UNKNOWN = "unbekannt"


class ReadingKind(StrEnum):
    ENGINE = "modell"
    CONSENSUS = "konsens"
    NORMALIZED = "lesefassung"
    VERIFIED = "bestaetigt"
    CLOUD = "cloud"
    PDF_TEXT = "pdf_text"


class ReviewStatus(StrEnum):
    AUTOMATIC = "automatisch"
    UNCERTAIN = "unsicher"
    VERIFIED = "bestaetigt"


class SearchMode(StrEnum):
    SMART = "intelligent"
    EXACT = "exakt"
    NAME = "namen"
    SEMANTIC = "bedeutung"


class JobStatus(StrEnum):
    QUEUED = "wartend"
    RUNNING = "läuft"
    DONE = "fertig"
    FAILED = "fehlgeschlagen"
    CANCELLED = "abgebrochen"


class DocumentMetadata(BaseModel):
    title: str | None = Field(default=None, max_length=240)
    year: int | None = Field(default=None, ge=800, le=2100)
    script_hint: ScriptHint = ScriptHint.AUTO


class DocumentRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sources: list[Path]
    year: int | None = Field(default=None, ge=800, le=2100)
    script_hint: ScriptHint = ScriptHint.AUTO
    cloud_policy: CloudPolicy = CloudPolicy.LOCAL_ONLY
    cloud_budget_usd: float = Field(default=1.0, ge=0, le=100)
    advanced_models: bool = True
    quality_profile: QualityProfile = QualityProfile.BEST_LOCAL
    group_images_by_folder: bool = False
    cloud_model_profile: str = "quality"
    document_metadata: dict[str, DocumentMetadata] = Field(default_factory=dict)
    target_document_id: str | None = None


class PeriodEstimate(BaseModel):
    year_from: int | None = None
    year_to: int | None = None
    exact_year: int | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class DocumentProfile(BaseModel):
    script: ScriptClass = ScriptClass.UNKNOWN
    layout: LayoutClass = LayoutClass.UNKNOWN
    period: PeriodEstimate = Field(default_factory=PeriodEstimate)
    language: str = "deu"
    confidence: float = Field(default=0.0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    requires_review: bool = False


class SourceDocument(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    title: str
    source_paths: list[Path]
    kind: str
    page_count: int


class ImageDiagnostics(BaseModel):
    brightness: float
    contrast: float
    sharpness: float
    skew_degrees: float
    clipped_dark: float
    clipped_light: float


class ImageVariant(BaseModel):
    name: str
    description: str
    diagnostics: ImageDiagnostics
    transform: list[list[float]]
    parameters: dict[str, Any] = Field(default_factory=dict)


class AlternativeReading(BaseModel):
    text: str
    model: str
    confidence: float = Field(ge=0, le=1)


class Reading(BaseModel):
    id: str
    kind: ReadingKind
    text: str
    model: str
    model_revision: str | None = None
    confidence: float = Field(ge=0, le=1)
    created_at: str | None = None


class RegionResult(BaseModel):
    id: str
    region_type: str = "text"
    polygon: list[tuple[int, int]]
    reading_order: int


class EngineRun(BaseModel):
    engine: str
    revision: str | None = None
    backend: str
    duration_seconds: float = 0.0
    success: bool = True
    message: str = ""


class LineResult(BaseModel):
    id: str
    text: str
    bbox: tuple[int, int, int, int]
    confidence: float = Field(ge=0, le=1)
    model: str
    variant: str
    alternatives: list[AlternativeReading] = Field(default_factory=list)
    manually_corrected: bool = False
    region_id: str | None = None
    baseline: list[tuple[int, int]] = Field(default_factory=list)
    polygon: list[tuple[int, int]] = Field(default_factory=list)
    readings: list[Reading] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.AUTOMATIC


class PageResult(BaseModel):
    page_index: int
    source_path: Path
    source_page_index: int = 0
    prepared_path: Path | None = None
    width: int
    height: int
    lines: list[LineResult]
    mean_confidence: float = Field(ge=0, le=1)
    expected_cer: float = Field(ge=0, le=1)
    selected_variant: str
    selected_model: str
    warnings: list[str] = Field(default_factory=list)
    logical_page_id: str | None = None
    source_bbox: tuple[int, int, int, int] | None = None
    transform: list[list[float]] = Field(default_factory=list)
    profile: DocumentProfile = Field(default_factory=DocumentProfile)
    regions: list[RegionResult] = Field(default_factory=list)
    engine_runs: list[EngineRun] = Field(default_factory=list)
    image_diagnostics: ImageDiagnostics | None = None


class DocumentResult(BaseModel):
    document: SourceDocument
    year: int | None
    script_hint: ScriptHint
    pages: list[PageResult]
    output_dir: Path | None = None


class SearchQuery(BaseModel):
    text: str = Field(min_length=1)
    mode: SearchMode = SearchMode.SMART
    fuzziness: float = Field(default=0.72, ge=0, le=1)
    year_from: int | None = None
    year_to: int | None = None
    document_id: str | None = None
    limit: int = Field(default=50, ge=1, le=500)


class SearchHit(BaseModel):
    line_id: str
    document_id: str
    document_title: str
    page_index: int
    source_path: Path
    bbox: tuple[int, int, int, int]
    text: str
    matched_form: str
    reason: str
    score: float
    confidence: float
    year: int | None = None


class EntityMention(BaseModel):
    line_id: str
    text: str
    label: str
    confidence: float
    normalized: str
    gnd_id: str | None = None
