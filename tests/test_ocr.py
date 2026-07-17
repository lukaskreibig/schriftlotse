from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from schriftlotse.domain import LineResult
from schriftlotse.model_registry import MODELS, ModelManager
from schriftlotse.ocr import (
    PARTY_MINIMUM_MEMORY_BYTES,
    TESSERACT_HISTORICAL_LANGUAGES,
    OrliLineDetector,
    PartyRecognizer,
    RecognitionCandidate,
    RecognizerRouter,
    TesseractRecognizer,
)


def test_orli_boundaries_become_clamped_line_boxes() -> None:
    lines = [
        SimpleNamespace(boundary=[(-4, 30), (120, 28), (125, 55), (0, 57)]),
        SimpleNamespace(boundary=None, baseline=[(20, 90), (180, 92)]),
    ]
    boxes = OrliLineDetector._boxes(lines, width=200, height=120)
    assert boxes[0][0] == 0
    assert boxes[0][2] <= 200
    assert boxes[1][1] < 90 < boxes[1][3]


def test_party_missing_output_becomes_controlled_fallback(monkeypatch, tmp_path) -> None:
    model = tmp_path / "model.safetensors"
    model.write_bytes(b"test")
    manager = SimpleNamespace(path_for=lambda _key: model)
    monkeypatch.setattr("schriftlotse.ocr.shutil.which", lambda _name: "/usr/bin/party")
    monkeypatch.setattr("schriftlotse.ocr.detect_text_lines", lambda _image: [(5, 5, 100, 40)])
    monkeypatch.setattr(
        "schriftlotse.ocr.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=""),
    )
    recognizer = PartyRecognizer(manager)
    with pytest.raises(RuntimeError, match="Party-Erkennung fehlgeschlagen"):
        recognizer.recognize(Image.new("RGB", (120, 60), "white"), "original")


def test_huggingface_models_use_full_commit_revisions() -> None:
    revisions = [spec.revision for spec in MODELS.values() if spec.kind == "huggingface"]
    assert all(revision is not None and len(revision) == 40 for revision in revisions)
    processor_revisions = [
        spec.processor_revision for spec in MODELS.values() if spec.processor_source
    ]
    assert all(revision is not None and len(revision) == 40 for revision in processor_revisions)


def test_trocr_install_requires_complete_local_processor(app_paths) -> None:
    manager = ModelManager(app_paths)
    model = manager.path_for("trocr-kurrent-19")
    model.mkdir(parents=True)
    (model / ".schriftlotse-model.json").write_text("{}", encoding="utf-8")
    assert manager.is_installed("trocr-kurrent-19") is False
    processor = manager.processor_path_for("trocr-kurrent-19")
    processor.mkdir()
    for filename in manager._PROCESSOR_FILES:
        (processor / filename).write_text("test", encoding="utf-8")
    assert manager.is_installed("trocr-kurrent-19") is True


def test_supplied_year_corrects_date_line_transparently() -> None:
    line = LineResult(
        id="line",
        text="Sorau, am 28. April 1849",
        bbox=(0, 0, 300, 40),
        confidence=0.8,
        model="trocr-kurrent-19",
        variant="original",
    )
    candidate = RecognitionCandidate(
        model="trocr-kurrent-19",
        variant="original",
        lines=[line],
        score=0.8,
        expected_cer=0.12,
    )
    RecognizerRouter._apply_year_hint(candidate, 1919)
    assert line.text == "Sorau, am 28. April 1919"
    assert line.alternatives[0].text.endswith("1849")
    assert line.model.endswith("+jahrangabe")


def test_civil_register_rules_require_markers_and_keep_originals() -> None:
    texts = [
        "Dc. 145.",
        "Der Standesbeamte.",
        "verstorben sei.",
        "Die Übereinstimmung mit dem Hauptregister beglaubigt.",
        "worden in Sorau, Markt 18/19",
        "3 Jahre od. katholischer aus",
        "des Jahres taufend ausendruckt und nungehe",
    ]
    lines = [
        LineResult(
            id=str(index),
            text=text,
            bbox=(0, index * 40, 500, index * 40 + 30),
            confidence=0.8,
            model="trocr-kurrent-19",
            variant="original",
        )
        for index, text in enumerate(texts)
    ]
    candidate = RecognitionCandidate(
        model="trocr-kurrent-19",
        variant="original",
        lines=lines,
        score=0.8,
        expected_cer=0.12,
    )
    RecognizerRouter._apply_civil_register_form(candidate, 1919)
    assert lines[0].text == "Nr. 145."
    assert lines[4].text.startswith("wohnhaft in Sorau")
    assert lines[5].text == "3 Jahre alt, katholischer Religion,"
    assert lines[6].text == "des Jahres tausend neunhundert und neunzehn"
    assert lines[4].alternatives[0].text.startswith("worden in")
    assert lines[4].model.endswith("+standesformular")


def test_homebrew_fraktur_language_is_considered() -> None:
    assert "script/Fraktur" in TESSERACT_HISTORICAL_LANGUAGES


def test_nested_tesseract_script_languages_are_discovered(monkeypatch) -> None:
    monkeypatch.setattr("schriftlotse.ocr.shutil.which", lambda _name: "/opt/tesseract")
    monkeypatch.setattr(
        "schriftlotse.ocr.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="List of available languages in tessdata (2):\ndeu\nscript/Fraktur\n",
        ),
    )
    assert TesseractRecognizer.installed_languages() == {"deu", "script/Fraktur"}


def test_party_is_not_automatic_below_32_gib_on_macos(monkeypatch) -> None:
    monkeypatch.setattr("schriftlotse.ocr.os.uname", lambda: SimpleNamespace(sysname="Darwin"))
    monkeypatch.setattr(
        "schriftlotse.ocr.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=str(PARTY_MINIMUM_MEMORY_BYTES - 1),
        ),
    )
    assert RecognizerRouter.party_memory_available() is False
