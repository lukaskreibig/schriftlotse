from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from schriftlotse.model_registry import MODELS
from schriftlotse.ocr import (
    PARTY_MINIMUM_MEMORY_BYTES,
    TESSERACT_HISTORICAL_LANGUAGES,
    OrliLineDetector,
    PartyRecognizer,
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
