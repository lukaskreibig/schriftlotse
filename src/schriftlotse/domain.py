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


class DocumentRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sources: list[Path]
    year: int | None = Field(default=None, ge=800, le=2100)
    script_hint: ScriptHint = ScriptHint.AUTO
    cloud_policy: CloudPolicy = CloudPolicy.LOCAL_ONLY
    cloud_budget_usd: float = Field(default=1.0, ge=0, le=100)
    advanced_models: bool = True


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


class LineResult(BaseModel):
    id: str
    text: str
    bbox: tuple[int, int, int, int]
    confidence: float = Field(ge=0, le=1)
    model: str
    variant: str
    alternatives: list[AlternativeReading] = Field(default_factory=list)
    manually_corrected: bool = False


class PageResult(BaseModel):
    page_index: int
    source_path: Path
    width: int
    height: int
    lines: list[LineResult]
    mean_confidence: float = Field(ge=0, le=1)
    expected_cer: float = Field(ge=0, le=1)
    selected_variant: str
    selected_model: str
    warnings: list[str] = Field(default_factory=list)


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
