from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from schriftlotse.config import Settings
from schriftlotse.domain import QualityProfile, ScriptHint
from schriftlotse.model_registry import MODELS, ModelManager
from schriftlotse.ocr import (
    TESSERACT_HISTORICAL_LANGUAGES,
    ChurroMLXRecognizer,
    OrliLineDetector,
    PartyRecognizer,
    RecognitionCandidate,
    RecognizerRouter,
    TesseractRecognizer,
    _extract_churro_lines,
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


def test_party_cpu_path_is_available_on_18_gib_macos(monkeypatch) -> None:
    monkeypatch.setattr("schriftlotse.ocr.os.uname", lambda: SimpleNamespace(sysname="Darwin"))
    monkeypatch.setattr(
        "schriftlotse.ocr.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=str(18 * 1024**3),
        ),
    )
    assert RecognizerRouter.party_memory_available() is True


def test_churro_xml_keeps_document_lines_and_prefers_additions() -> None:
    output = """<|im_start|><HistoricalDocument xmlns="urn:test"><Metadata>
    <Description>Nicht als Transkription übernehmen</Description></Metadata><Page><Body>
    <Line>Sorau, am 7. Mai 1923.</Line>
    <Line>Johann <Deletion>Reichig</Deletion><Addition>Kreibig</Addition></Line>
    </Body></Page></HistoricalDocument>"""
    assert _extract_churro_lines(output) == [
        "Sorau, am 7. Mai 1923.",
        "Johann Kreibig",
    ]


def test_churro_standard_uses_eight_bit_mlx_quantization() -> None:
    model = MODELS["churro-mlx-8bit"]
    assert model.quantization_bits == 8
    assert model.requires_acceptance is True


def test_model_router_uses_only_epoch_appropriate_trocr(app_paths) -> None:
    router = RecognizerRouter(app_paths, Settings())
    assert router._model_keys(1923, ScriptHint.HANDWRITING) == ["trocr-kurrent-19"]
    assert router._model_keys(1750, ScriptHint.HANDWRITING) == ["trocr-kurrent-early"]
    assert router._model_keys(None, ScriptHint.HANDWRITING) == [
        "trocr-kurrent-19",
        "trocr-kurrent-early",
    ]


def test_churro_retries_one_empty_mlx_first_generation() -> None:
    recognizer = ChurroMLXRecognizer.__new__(ChurroMLXRecognizer)
    recognizer.model = SimpleNamespace(config=SimpleNamespace())
    recognizer.processor = SimpleNamespace()
    recognizer._apply_chat_template = lambda *_args, **_kwargs: "prompt"
    outputs = iter(
        [
            SimpleNamespace(text="<|im_start|>"),
            SimpleNamespace(
                text=(
                    '<HistoricalDocument xmlns="urn:test"><Page><Body>'
                    "<Line>Sorau, am 7. Mai 1923.</Line>"
                    "</Body></Page></HistoricalDocument>"
                )
            ),
        ]
    )
    recognizer._generate = lambda *_args, **_kwargs: next(outputs)

    lines = recognizer.recognize(Image.new("RGB", (100, 40), "white"), "normalisiert")

    assert [line.text for line in lines] == ["Sorau, am 7. Mai 1923."]


def test_fast_profile_skips_advanced_model_router(monkeypatch, app_paths) -> None:
    router = RecognizerRouter(app_paths, Settings(advanced_models=True))
    router.quality_profile = QualityProfile.FAST
    monkeypatch.setattr(
        TesseractRecognizer,
        "installed_languages",
        staticmethod(lambda _command="tesseract": {"deu"}),
    )
    recognizers = router.recognizers(1923, ScriptHint.HANDWRITING)
    assert [recognizer.name for recognizer in recognizers] == ["tesseract:deu"]


def test_tesseract_never_overwrites_churro_master_line() -> None:
    master = RecognitionCandidate(
        model="churro-mlx-8bit",
        variant="normalisiert",
        lines=[
            SimpleNamespace(
                text="Johann Alois Richard",
                model="churro-mlx-8bit",
                confidence=0.72,
                bbox=(0, 100, 500, 140),
                alternatives=[],
            )
        ],
        score=0.9,
        expected_cer=0.14,
    )
    tesseract = RecognitionCandidate(
        model="tesseract-line:deu",
        variant="original",
        lines=[
            SimpleNamespace(
                text="geboren worden sei und daß das Kind",
                model="tesseract-line:deu",
                confidence=0.99,
                bbox=(0, 100, 500, 140),
            )
        ],
        score=0.8,
        expected_cer=0.2,
    )

    RecognizerRouter._attach_spatial_alternatives(master, [tesseract])

    assert master.lines[0].text == "Johann Alois Richard"
    assert master.lines[0].alternatives[0].text.startswith("geboren")


def test_best_local_prefers_viable_churro_over_overconfident_trocr(monkeypatch, app_paths) -> None:
    router = RecognizerRouter(app_paths, Settings())
    router.quality_profile = QualityProfile.BEST_LOCAL
    image = Image.new("RGB", (800, 1000), "white")
    churro = SimpleNamespace(
        name="churro-mlx-8bit",
        recognize=lambda _image, variant: [
            SimpleNamespace(
                id="churro-line",
                text=(
                    "Helvetien zu König, in unserem Herzogtum Österreich, "
                    "geben wir diesen offenen Brief."
                ),
                model="churro-mlx-8bit",
                confidence=0.72,
                bbox=(10, 100, 790, 850),
                alternatives=[],
                readings=[],
                variant=variant,
                review_status=None,
            )
        ],
    )
    trocr = SimpleNamespace(
        name="trocr-kurrent-19",
        recognize=lambda _image, variant: [
            SimpleNamespace(
                id="trocr-line",
                text="Anteilierungs Telegramm Anweisung Telegramm",
                model="trocr-kurrent-19",
                confidence=0.99,
                bbox=(10, 100, 790, 850),
                alternatives=[],
                readings=[],
                variant=variant,
                review_status=None,
            )
        ],
    )
    monkeypatch.setattr(router, "recognizers", lambda _year, _script: [trocr, churro])

    result = router.recognize_variants(
        [SimpleNamespace(image=image, metadata=SimpleNamespace(name="normalisiert"))],
        1556,
        ScriptHint.HANDWRITING,
    )

    assert result.model == "churro-mlx-8bit"


def test_gold_routing_prefers_early_kurrent_specialist_over_churro(monkeypatch, app_paths) -> None:
    router = RecognizerRouter(app_paths, Settings())
    router.quality_profile = QualityProfile.BEST_LOCAL
    image = Image.new("RGB", (1000, 1200), "white")

    def line(identifier, text, model, confidence):
        return SimpleNamespace(
            id=identifier,
            text=text,
            model=model,
            confidence=confidence,
            bbox=(20, 100, 980, 1050),
            alternatives=[],
            readings=[],
            variant="original",
            review_status=None,
        )

    early = SimpleNamespace(
        name="trocr-kurrent-early",
        recognize=lambda _image, _variant: [
            line(
                "early",
                "dem ChurPrinz auf den RiesenSaal zur Einsegnung geführet",
                "trocr-kurrent-early",
                0.78,
            )
        ],
    )
    churro = SimpleNamespace(
        name="churro-mlx-8bit",
        recognize=lambda _image, _variant: [
            line(
                "churro",
                "dem Kurprinzen wurde auf dem Riesensaal die Segnung gegeben",
                "churro-mlx-8bit",
                0.90,
            )
        ],
    )
    monkeypatch.setattr(router, "recognizers", lambda _year, _script: [churro, early])

    result = router.recognize_variants(
        [SimpleNamespace(image=image, metadata=SimpleNamespace(name="original"))],
        1665,
        ScriptHint.HANDWRITING,
    )

    assert result.model == "trocr-kurrent-early"
    assert result.expected_cer >= 0.093


def test_router_reuses_loaded_recognizers_between_pages(monkeypatch, app_paths) -> None:
    router = RecognizerRouter(app_paths, Settings(advanced_models=False))
    monkeypatch.setattr(
        TesseractRecognizer,
        "installed_languages",
        staticmethod(lambda _command="tesseract": {"deu"}),
    )

    first = router.recognizers(1923, ScriptHint.HANDWRITING)
    second = router.recognizers(1923, ScriptHint.HANDWRITING)

    assert first[0] is second[0]


def test_print_preclassification_is_conservative(monkeypatch, app_paths) -> None:
    router = RecognizerRouter(app_paths, Settings())
    monkeypatch.setattr(
        TesseractRecognizer,
        "installed_languages",
        staticmethod(lambda _command="tesseract": {"script/Fraktur", "deu"}),
    )
    lines = [
        SimpleNamespace(text="Bekanntmachung " * 25, confidence=0.82),
        SimpleNamespace(text="Standesamt und Gemeinde " * 12, confidence=0.78),
    ]
    recognizer = SimpleNamespace(recognize=lambda _image, _variant: lines)
    monkeypatch.setattr(router, "_cached_recognizer", lambda _key, _factory: recognizer)

    hint, text, confidence = router.preclassify_print(
        Image.new("RGB", (800, 1000), "white"), ScriptHint.AUTO
    )

    assert hint == ScriptHint.PRINT
    assert "Bekanntmachung" in text
    assert confidence == pytest.approx(0.80)


def test_sparse_handwriting_does_not_become_print(monkeypatch, app_paths) -> None:
    router = RecognizerRouter(app_paths, Settings())
    monkeypatch.setattr(
        TesseractRecognizer,
        "installed_languages",
        staticmethod(lambda _command="tesseract": {"deu"}),
    )
    recognizer = SimpleNamespace(
        recognize=lambda _image, _variant: [SimpleNamespace(text="Hermann", confidence=0.91)]
    )
    monkeypatch.setattr(router, "_cached_recognizer", lambda _key, _factory: recognizer)

    hint, _text, _confidence = router.preclassify_print(
        Image.new("RGB", (800, 1000), "white"), ScriptHint.AUTO
    )

    assert hint == ScriptHint.AUTO


def test_loaded_line_detector_is_not_reused_for_later_print(monkeypatch, app_paths) -> None:
    router = RecognizerRouter(app_paths, Settings(advanced_models=True))
    router._line_detector_initialized = True
    router._line_detector = lambda _image: [(0, 0, 100, 20)]
    monkeypatch.setattr(
        TesseractRecognizer,
        "installed_languages",
        staticmethod(lambda _command="tesseract": {"deu"}),
    )

    recognizers = router.recognizers(1862, ScriptHint.PRINT)

    assert [recognizer.name for recognizer in recognizers] == ["tesseract:deu"]
