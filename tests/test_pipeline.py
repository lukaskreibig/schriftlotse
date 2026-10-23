from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from schriftlotse.config import Settings
from schriftlotse.domain import DocumentRequest, LineResult
from schriftlotse.ocr import RecognitionCandidate
from schriftlotse.pipeline import ProcessingPipeline


class FakeRouter:
    def __init__(self) -> None:
        self.calls = 0

    def preclassify_print(self, _image, script_hint):
        return script_hint, "", 0.0

    def recognize_variants(self, variants, year, script_hint):
        self.calls += 1
        return RecognitionCandidate(
            model="fake",
            variant=variants[0].metadata.name,
            lines=[
                LineResult(
                    id="temporary",
                    text="Johann Schmidt, geboren 1872.",
                    bbox=(20, 20, 300, 60),
                    confidence=0.94,
                    model="fake",
                    variant=variants[0].metadata.name,
                )
            ],
            score=0.94,
            expected_cer=0.05,
        )


class FailingRouter(FakeRouter):
    def recognize_variants(self, variants, year, script_hint):
        raise RuntimeError("simulierter Modellfehler")


def test_pipeline_persists_indexes_and_exports(app_paths, tmp_path: Path) -> None:
    scan = tmp_path / "scan.png"
    Image.new("RGB", (500, 300), "white").save(scan)
    pipeline = ProcessingPipeline(app_paths, Settings(advanced_models=False))
    router = FakeRouter()
    pipeline.router = router
    progress: list[tuple[str, float]] = []
    job_id, results, exports = pipeline.run(
        DocumentRequest(sources=[scan], advanced_models=False),
        progress=lambda message, value: progress.append((message, value)),
    )
    assert job_id
    assert results[0].pages[0].lines[0].text.startswith("Johann")
    assert any(path.name == "schriftlotse.pdf" for path in exports)
    rows = pipeline.database.rows("SELECT text FROM lines")
    assert rows[0]["text"].startswith("Johann")
    messages = "\n".join(message for message, _value in progress)
    assert "Scan wird analysiert" in messages
    assert "lokale OCR-/HTR-Modelle arbeiten" in messages
    assert "Ausgabedateien werden formatiert" in messages
    assert progress[-1] == ("Verarbeitung abgeschlossen", 1.0)

    calls = router.calls
    resumed_job, resumed, _ = pipeline.run(
        DocumentRequest(sources=[scan], advanced_models=False),
        job_id=job_id,
    )
    assert resumed_job == job_id
    assert resumed[0].pages[0].lines[0].text.startswith("Johann")
    assert router.calls == calls

    document_id = results[0].document.id
    managed_source = Path(json.loads(pipeline.database.document(document_id)["source_paths"])[0])
    pipeline.run(
        DocumentRequest(
            sources=[managed_source],
            advanced_models=False,
            target_document_id=document_id,
        )
    )
    assert len(pipeline.database.list_documents()) == 1
    assert pipeline.database.document(document_id)["library_managed"] == 1
    assert Path(pipeline.database.document_files(document_id)[0]["original_path"]) == scan.resolve()


def test_failed_ocr_keeps_managed_original_visible_for_retry(app_paths, tmp_path: Path) -> None:
    scan = tmp_path / "important-scan.png"
    Image.new("RGB", (500, 300), "white").save(scan)
    pipeline = ProcessingPipeline(app_paths, Settings(advanced_models=False))
    pipeline.router = FailingRouter()

    with pytest.raises(RuntimeError, match="simulierter Modellfehler"):
        pipeline.run(DocumentRequest(sources=[scan], advanced_models=False))

    document = pipeline.database.list_documents()[0]
    assert document["library_managed"] == 1
    assert document["document_status"] == "fehlgeschlagen"
    assert Path(document["thumbnail_path"]).is_file()
    assert Path(json.loads(document["source_paths"])[0]).is_file()
