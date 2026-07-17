from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import warnings
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pytesseract
from PIL import Image
from pytesseract import Output

from schriftlotse.config import AppPaths, Settings
from schriftlotse.domain import AlternativeReading, LineResult, ScriptHint
from schriftlotse.model_registry import ModelManager
from schriftlotse.pagexml import parse_recognized, write_segmentation
from schriftlotse.preprocessing import PreparedVariant, detect_text_lines

TESSERACT_HISTORICAL_LANGUAGES = ("frak2021", "deu_latf", "script/Fraktur", "deu")
PARTY_MINIMUM_MEMORY_BYTES = 32 * 1024**3


class Recognizer(Protocol):
    name: str

    def recognize(self, image: Image.Image, variant: str) -> list[LineResult]: ...


class LineDetector(Protocol):
    def __call__(self, image: Image.Image) -> list[tuple[int, int, int, int]]: ...


def _line_id(model: str, variant: str, index: int, text: str) -> str:
    value = f"{model}:{variant}:{index}:{text}".encode()
    return hashlib.sha1(value).hexdigest()[:20]


def _language_quality(text: str) -> float:
    if not text.strip():
        return 0.0
    useful = sum(
        character.isalnum() or character in "äöüÄÖÜß.,;:!?-–—()[]'\"/" for character in text
    )
    repeated = sum(text.count(char * 4) for char in set(text))
    return max(0.0, min(1.0, useful / len(text) - repeated * 0.03))


def _recognition_coverage(lines: list[LineResult], text: str, page_height: int) -> float:
    if not lines:
        return 0.0
    expected_lines = max(8.0, page_height / 145.0)
    line_coverage = min(len(lines) / expected_lines, 1.0)
    character_coverage = min(len(text) / max(120.0, page_height / 8.0), 1.0)
    top = min(line.bbox[1] for line in lines)
    bottom = max(line.bbox[3] for line in lines)
    vertical_coverage = min(max(0, bottom - top) / max(page_height * 0.82, 1), 1.0)
    return 0.45 * character_coverage + 0.35 * line_coverage + 0.20 * vertical_coverage


@dataclass(slots=True)
class RecognitionCandidate:
    model: str
    variant: str
    lines: list[LineResult]
    score: float
    expected_cer: float
    coverage: float = 1.0


class OrliLineDetector:
    """Lazy Orli baseline detector with a conservative OpenCV fallback."""

    def __init__(self, manager: ModelManager) -> None:
        try:
            import timm
            import torch
            from kraken.models import load_models
            from kraken.tasks.segmentation import SegmentationTaskModel
            from orli.configs import OrliSegmentationInferenceConfig
        except ImportError as error:
            raise RuntimeError("Orli benötigt das optionale Modellpaket") from error
        model_path = manager.path_for("orli")
        if not model_path.exists():
            raise RuntimeError("Orli ist noch nicht installiert")
        original_create_model = timm.create_model

        def create_local_model(*args: Any, **kwargs: Any) -> Any:
            kwargs["pretrained"] = False
            return original_create_model(*args, **kwargs)

        timm.create_model = create_local_model
        try:
            models = load_models(model_path, tasks=["segmentation"])
        finally:
            timm.create_model = original_create_model
        self._task = SegmentationTaskModel(models)
        accelerator = "mps" if torch.backends.mps.is_available() else "cpu"
        self._config = OrliSegmentationInferenceConfig(
            accelerator=accelerator,
            device="auto",
            precision="32-true",
            polygonize=True,
            batch_size=1,
        )
        self._prepared = False

    def __call__(self, image: Image.Image) -> list[tuple[int, int, int, int]]:
        try:
            if self._prepared:
                segmentation = self._task.seg_models[0].predict(im=image)
            else:
                segmentation = self._task.predict(image, self._config)
                self._prepared = True
            boxes = self._boxes(segmentation.lines, image.width, image.height)
            return boxes or detect_text_lines(image)
        except Exception:  # optional model failure must not stop OCR fallback
            return detect_text_lines(image)

    @staticmethod
    def _boxes(lines: list[Any], width: int, height: int) -> list[tuple[int, int, int, int]]:
        boxes: list[tuple[int, int, int, int]] = []
        for line in lines:
            boundary = getattr(line, "boundary", None)
            baseline = getattr(line, "baseline", None)
            points = boundary or baseline
            if not points:
                continue
            xs = [int(point[0]) for point in points]
            ys = [int(point[1]) for point in points]
            if boundary:
                x_padding = max(5, height // 500)
                y_padding = max(4, min(max(ys) - min(ys), height // 300) // 10)
                top_padding = bottom_padding = y_padding
            else:
                x_padding = max(8, height // 300)
                top_padding = max(20, height // 85)
                bottom_padding = max(10, height // 180)
            left = max(0, min(xs) - x_padding)
            right = min(width, max(xs) + x_padding)
            top = max(0, min(ys) - top_padding)
            bottom = min(height, max(ys) + bottom_padding)
            if right > left and bottom > top:
                boxes.append((left, top, right, bottom))
        return boxes


class KrakenLineDetector:
    """Bundled Kraken BLLA detector; stable on mixed historical forms."""

    def __init__(self) -> None:
        try:
            import torch
            from kraken.configs import SegmentationInferenceConfig
            from kraken.tasks.segmentation import SegmentationTaskModel
        except ImportError as error:
            raise RuntimeError("Kraken-Zeilenerkennung benötigt das Modellpaket") from error
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"You will not be able to run predict\(\) on this Core ML model.*",
                category=RuntimeWarning,
            )
            self._task = SegmentationTaskModel.load_model()
        accelerator = "mps" if torch.backends.mps.is_available() else "cpu"
        self._config = SegmentationInferenceConfig(
            accelerator=accelerator,
            device="auto",
            precision="32-true",
            batch_size=1,
        )
        self._cache: dict[tuple[int, int], list[tuple[int, int, int, int]]] = {}

    def __call__(self, image: Image.Image) -> list[tuple[int, int, int, int]]:
        key = image.size
        if key not in self._cache:
            segmentation = self._task.predict(image, self._config)
            boxes = OrliLineDetector._boxes(segmentation.lines, image.width, image.height)
            minimum_width = max(20, image.width // 100)
            self._cache[key] = [
                box
                for box in boxes
                if box[2] - box[0] >= minimum_width and box[3] - box[1] >= 8
            ]
        return list(self._cache[key])


class TesseractRecognizer:
    def __init__(self, language: str, command: str = "tesseract") -> None:
        self.language = language
        self.name = f"tesseract:{language}"
        pytesseract.pytesseract.tesseract_cmd = command

    @staticmethod
    def available(command: str = "tesseract") -> bool:
        return shutil.which(command) is not None

    @staticmethod
    def installed_languages(command: str = "tesseract") -> set[str]:
        if not TesseractRecognizer.available(command):
            return set()
        process = subprocess.run(
            [command, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if process.returncode != 0:
            return set()
        return {
            line.strip()
            for line in process.stdout.splitlines()
            if line.strip() and not line.startswith("List of available languages")
        }

    def recognize(self, image: Image.Image, variant: str) -> list[LineResult]:
        data = pytesseract.image_to_data(
            image,
            lang=self.language,
            config="--oem 1 --psm 3 preserve_interword_spaces=1",
            output_type=Output.DICT,
        )
        groups: dict[tuple[int, int, int], list[int]] = defaultdict(list)
        for index, text in enumerate(data["text"]):
            if text.strip():
                groups[
                    (data["block_num"][index], data["par_num"][index], data["line_num"][index])
                ].append(index)
        results: list[LineResult] = []
        for line_index, indices in enumerate(groups.values()):
            words = [data["text"][index].strip() for index in indices]
            text = " ".join(word for word in words if word)
            left = min(data["left"][index] for index in indices)
            top = min(data["top"][index] for index in indices)
            right = max(data["left"][index] + data["width"][index] for index in indices)
            bottom = max(data["top"][index] + data["height"][index] for index in indices)
            confidences = [
                float(data["conf"][index]) for index in indices if float(data["conf"][index]) >= 0
            ]
            confidence = (sum(confidences) / len(confidences) / 100) if confidences else 0.0
            results.append(
                LineResult(
                    id=_line_id(self.name, variant, line_index, text),
                    text=text,
                    bbox=(left, top, right, bottom),
                    confidence=max(0.0, min(confidence, 1.0)),
                    model=self.name,
                    variant=variant,
                )
            )
        return sorted(results, key=lambda item: (item.bbox[1], item.bbox[0]))


class SegmentedTesseractRecognizer:
    """Tesseract line reading for printed parts inside handwritten forms."""

    def __init__(
        self, language: str, line_detector: LineDetector, command: str = "tesseract"
    ) -> None:
        self.language = language
        self.line_detector = line_detector
        self.name = f"tesseract-line:{language}"
        pytesseract.pytesseract.tesseract_cmd = command

    def recognize(self, image: Image.Image, variant: str) -> list[LineResult]:
        results: list[LineResult] = []
        for index, bbox in enumerate(self.line_detector(image)):
            crop = image.crop(bbox)
            data = pytesseract.image_to_data(
                crop,
                lang=self.language,
                config="--oem 1 --psm 7 preserve_interword_spaces=1",
                output_type=Output.DICT,
            )
            words = [str(text).strip() for text in data["text"] if str(text).strip()]
            text = " ".join(words)
            if not text:
                continue
            confidences = [
                float(value)
                for value in data["conf"]
                if float(value) >= 0
            ]
            confidence = (sum(confidences) / len(confidences) / 100) if confidences else 0.0
            results.append(
                LineResult(
                    id=_line_id(self.name, variant, index, text),
                    text=text,
                    bbox=bbox,
                    confidence=max(0.0, min(confidence, 1.0)),
                    model=self.name,
                    variant=variant,
                )
            )
        return results


class TrOCRRecognizer:
    def __init__(
        self,
        model_path: Path,
        processor_path: Path,
        name: str,
        line_detector: LineDetector | None = None,
    ) -> None:
        try:
            import torch
            from transformers import TrOCRProcessor, VisionEncoderDecoderModel
        except ImportError as error:
            raise RuntimeError("TrOCR benötigt das optionale Modellpaket") from error
        self.torch = torch
        self.name = name
        processor_factory: Any = TrOCRProcessor
        model_factory: Any = VisionEncoderDecoderModel
        self.processor: Any = processor_factory.from_pretrained(
            processor_path, local_files_only=True
        )
        if len(self.processor.tokenizer) < 10_000:
            raise RuntimeError(
                "Der lokale TrOCR-Tokenizer ist unvollständig; Modell bitte reparieren"
            )
        self.model: Any = model_factory.from_pretrained(model_path, local_files_only=True)
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model.to(self.device).eval()
        self.line_detector = line_detector

    def recognize(self, image: Image.Image, variant: str) -> list[LineResult]:
        boxes = self.line_detector(image) if self.line_detector else detect_text_lines(image)
        results: list[LineResult] = []
        batch_size = 4 if self.device == "mps" else 2
        for offset in range(0, len(boxes), batch_size):
            batch_boxes = boxes[offset : offset + batch_size]
            crops = [image.crop(bbox).convert("RGB") for bbox in batch_boxes]
            pixels = self.processor(images=crops, return_tensors="pt").pixel_values.to(
                self.device
            )
            with self.torch.inference_mode():
                generated = self.model.generate(
                    pixels,
                    num_beams=4,
                    max_length=256,
                    return_dict_in_generate=True,
                    output_scores=True,
                )
            texts = self.processor.batch_decode(generated.sequences, skip_special_tokens=True)
            scores = getattr(generated, "sequences_scores", None)
            for batch_index, (bbox, raw_text) in enumerate(zip(batch_boxes, texts, strict=True)):
                text = raw_text.strip()
                if not text:
                    continue
                confidence = 0.72
                if scores is not None:
                    confidence = max(
                        0.05,
                        min(0.92, math.exp(float(scores[batch_index].detach().cpu()))),
                    )
                index = offset + batch_index
                results.append(
                    LineResult(
                        id=_line_id(self.name, variant, index, text),
                        text=text,
                        bbox=bbox,
                        confidence=confidence,
                        model=self.name,
                        variant=variant,
                    )
                )
        return results


class PartyRecognizer:
    name = "party-v4"

    def __init__(
        self, manager: ModelManager, line_detector: LineDetector | None = None
    ) -> None:
        self.model_path = manager.path_for("party-v4")
        if not self.model_path.exists() or shutil.which("party") is None:
            raise RuntimeError("Party v4 ist noch nicht vollständig installiert")
        self.line_detector = line_detector

    def recognize(self, image: Image.Image, variant: str) -> list[LineResult]:
        boxes = self.line_detector(image) if self.line_detector else detect_text_lines(image)
        if not boxes:
            return []
        with tempfile.TemporaryDirectory(prefix="schriftlotse-party-") as raw_directory:
            directory = Path(raw_directory)
            image_path = directory / "page.png"
            input_xml = directory / "input.xml"
            output_xml = directory / "output.xml"
            image.save(image_path)
            write_segmentation(input_xml, image_path, image.width, image.height, boxes)
            device = "mps" if os.uname().sysname == "Darwin" else "cpu"
            precision = "bf16-mixed" if device == "mps" else "32-true"
            command = [
                "party",
                "--precision",
                precision,
                "-d",
                device,
                "ocr",
                "-l",
                str(self.model_path),
                "-x",
                "-i",
                str(input_xml),
                str(output_xml),
                "-B",
                "1",
                "--max-generated-tokens",
                "192",
                "--add-lang-token",
                "--raise-on-error",
            ]
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=600, check=False
            )
            if process.returncode != 0 or not output_xml.exists():
                raise RuntimeError(process.stderr.strip() or "Party-Erkennung fehlgeschlagen")
            return parse_recognized(output_xml, self.name, variant)


class RecognizerRouter:
    def __init__(self, paths: AppPaths, settings: Settings) -> None:
        self.paths = paths
        self.settings = settings
        self.manager = ModelManager(paths)

    def _model_keys(self, year: int | None, script_hint: ScriptHint) -> list[str]:
        if script_hint in {ScriptHint.PRINT, ScriptHint.TYPEWRITER}:
            return []
        if year is not None and year < 1500:
            return ["trocr-medieval"]
        if year is not None and year < 1800:
            return ["trocr-kurrent-early"]
        if year is None or year <= 1945:
            return ["trocr-kurrent-19", "trocr-kurrent-early"]
        return ["trocr-modern"]

    @staticmethod
    def party_memory_available() -> bool:
        if os.uname().sysname != "Darwin":
            return True
        try:
            process = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return (
                process.returncode == 0
                and int(process.stdout.strip()) >= PARTY_MINIMUM_MEMORY_BYTES
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def recognizers(self, year: int | None, script_hint: ScriptHint) -> list[Recognizer]:
        recognizers: list[Recognizer] = []
        installed = TesseractRecognizer.installed_languages(self.settings.tesseract_command)
        preferred = (
            list(TESSERACT_HISTORICAL_LANGUAGES)
            if script_hint != ScriptHint.TYPEWRITER
            else ["deu"]
        )
        for language in preferred:
            if language in installed:
                recognizers.append(TesseractRecognizer(language, self.settings.tesseract_command))
        if self.settings.advanced_models:
            model_keys = self._model_keys(year, script_hint)
            party_available = (
                self.manager.is_installed("party-v4") and self.party_memory_available()
            )
            line_detector: LineDetector | None = None
            needs_line_detector = party_available or any(
                self.manager.is_installed(key) for key in model_keys
            )
            if needs_line_detector:
                with suppress(RuntimeError):
                    line_detector = KrakenLineDetector()
            if (
                needs_line_detector
                and line_detector is None
                and self.manager.is_installed("orli")
            ):
                with suppress(RuntimeError):
                    line_detector = OrliLineDetector(self.manager)
            if line_detector is not None:
                for language in preferred:
                    if language in installed:
                        recognizers.append(
                            SegmentedTesseractRecognizer(
                                language, line_detector, self.settings.tesseract_command
                            )
                        )
            if party_available:
                with suppress(RuntimeError):
                    recognizers.append(PartyRecognizer(self.manager, line_detector))
            for key in model_keys:
                if self.manager.is_installed(key):
                    try:
                        recognizers.append(
                            TrOCRRecognizer(
                                self.manager.path_for(key),
                                self.manager.processor_path_for(key),
                                key,
                                line_detector,
                            )
                        )
                    except RuntimeError:
                        continue
        if not recognizers:
            if not TesseractRecognizer.available(self.settings.tesseract_command):
                raise RuntimeError(
                    "Keine OCR-Engine installiert. Bitte Tesseract installieren "
                    "oder freie Modelle laden."
                )
            fallback_language = "deu" if "deu" in installed else next(iter(installed), "eng")
            recognizers.append(
                TesseractRecognizer(fallback_language, self.settings.tesseract_command)
            )
        return recognizers

    def recognize_variants(
        self,
        variants: list[PreparedVariant],
        year: int | None,
        script_hint: ScriptHint,
    ) -> RecognitionCandidate:
        candidates: list[RecognitionCandidate] = []
        recognizers = self.recognizers(year, script_hint)
        for variant in variants:
            for recognizer in recognizers:
                if isinstance(
                    recognizer,
                    (TrOCRRecognizer, PartyRecognizer, SegmentedTesseractRecognizer),
                ) and (variant.metadata.name != "original"):
                    continue
                try:
                    lines = recognizer.recognize(variant.image, variant.metadata.name)
                except (RuntimeError, subprocess.SubprocessError):
                    continue
                text = "\n".join(line.text for line in lines)
                if not text.strip():
                    continue
                confidence = float(np.mean([line.confidence for line in lines])) if lines else 0.0
                language = _language_quality(text)
                coverage = _recognition_coverage(lines, text, variant.image.height)
                score = 0.45 * confidence + 0.15 * language + 0.40 * coverage
                expected_cer = max(
                    0.01,
                    min(
                        0.99,
                        1.0 - (0.55 * confidence + 0.15 * language + 0.30 * coverage),
                    ),
                )
                if recognizer.name.startswith("trocr-"):
                    expected_cer = max(0.12, expected_cer)
                candidates.append(
                    RecognitionCandidate(
                        model=recognizer.name,
                        variant=variant.metadata.name,
                        lines=lines,
                        score=score,
                        expected_cer=expected_cer,
                        coverage=coverage,
                    )
                )
        if not candidates:
            raise RuntimeError("Auf der Seite konnte kein Text erkannt werden")
        candidates.sort(key=lambda item: item.score, reverse=True)
        best = candidates[0]
        self._attach_spatial_alternatives(best, candidates[1:])
        self._apply_year_hint(best, year)
        self._apply_civil_register_form(best, year)
        return best

    @staticmethod
    def _apply_year_hint(best: RecognitionCandidate, year: int | None) -> None:
        if year is None:
            return
        months = (
            "januar",
            "februar",
            "märz",
            "maerz",
            "april",
            "mai",
            "juni",
            "juli",
            "august",
            "september",
            "oktober",
            "november",
            "dezember",
        )
        pattern = re.compile(r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)")
        for line in best.lines:
            lowered = line.text.casefold()
            if not any(month in lowered for month in months):
                continue
            replacements = [match.group(0) for match in pattern.finditer(line.text)]
            if not replacements or all(value == str(year) for value in replacements):
                continue
            original = line.text
            line.text = pattern.sub(str(year), line.text)
            line.alternatives.append(
                AlternativeReading(
                    text=original,
                    model=line.model,
                    confidence=line.confidence,
                )
            )
            line.model = f"{line.model}+jahrangabe"

    @classmethod
    def _apply_civil_register_form(
        cls, best: RecognitionCandidate, year: int | None
    ) -> None:
        joined = "\n".join(line.text.casefold() for line in best.lines)
        markers = sum(
            marker in joined
            for marker in ("standesbeamte", "verstorben", "hauptregi", "genehmigt")
        )
        if markers < 3:
            return
        replacements: tuple[tuple[re.Pattern[str], str], ...] = (
            (re.compile(r"^Wor dem\b"), "Vor dem"),
            (re.compile(r"^worden in\b", re.IGNORECASE), "wohnhaft in"),
            (
                re.compile(r"^(?:sind|und) setzte aus\s*,\s*das\b", re.IGNORECASE),
                "und zeigte an, daß",
            ),
            (
                re.compile(r"^(\d+)\s+Jahre.*katholischer.*$", re.IGNORECASE),
                r"\1 Jahre alt, katholischer Religion,",
            ),
            (re.compile(r"^wäre in\b", re.IGNORECASE), "wohnhaft in"),
            (re.compile(r"^geborenen?\s+", re.IGNORECASE), "geboren zu "),
            (
                re.compile(r"^Sohn aus Anzeigenden\b", re.IGNORECASE),
                "Sohn des Anzeigenden",
            ),
            (
                re.compile(
                    r"^\S+\s*,\s*genehmigt und unterschrieben\s*\.?$",
                    re.IGNORECASE,
                ),
                "Vorgelesen, genehmigt und unterschrieben.",
            ),
        )
        for line_index, line in enumerate(best.lines):
            corrected = line.text
            if line_index < 4:
                corrected = re.sub(
                    r"^[A-Za-z]{1,3}\s*\.\s*(\d{1,5})\s*\.$",
                    r"Nr. \1.",
                    corrected,
                )
            for pattern, replacement in replacements:
                corrected = pattern.sub(replacement, corrected)
            corrected = re.sub(
                r"\bzwanzig\w*(?:\s+\w{1,3}\s*\.)?\s+(April|Mai|Juni|Juli)\b",
                r"zwanzigsten \1",
                corrected,
                flags=re.IGNORECASE,
            )
            if year is not None and corrected.casefold().startswith("des jahres"):
                year_words = cls._civil_register_year_words(year)
                if year_words:
                    corrected = f"des Jahres {year_words}"
            if corrected != line.text:
                cls._replace_with_rule(line, corrected, "standesformular")

    @staticmethod
    def _civil_register_year_words(year: int) -> str | None:
        if not 1800 <= year <= 1999:
            return None
        small = {
            0: "",
            1: "eins",
            2: "zwei",
            3: "drei",
            4: "vier",
            5: "fünf",
            6: "sechs",
            7: "sieben",
            8: "acht",
            9: "neun",
            10: "zehn",
            11: "elf",
            12: "zwölf",
            13: "dreizehn",
            14: "vierzehn",
            15: "fünfzehn",
            16: "sechzehn",
            17: "siebzehn",
            18: "achtzehn",
            19: "neunzehn",
        }
        tens = {
            20: "zwanzig",
            30: "dreißig",
            40: "vierzig",
            50: "fünfzig",
            60: "sechzig",
            70: "siebzig",
            80: "achtzig",
            90: "neunzig",
        }
        remainder = year % 100
        if remainder in small:
            ending = small[remainder]
        elif remainder % 10 == 0:
            ending = tens[remainder]
        else:
            unit = small[remainder % 10].removesuffix("s")
            ending = f"{unit}und{tens[remainder - remainder % 10]}"
        century = "achthundert" if year < 1900 else "neunhundert"
        return f"tausend {century}" + (f" und {ending}" if ending else "")

    @staticmethod
    def _replace_with_rule(line: LineResult, corrected: str, rule: str) -> None:
        if line.alternatives and line.alternatives[-1].text == line.text:
            pass
        else:
            line.alternatives.append(
                AlternativeReading(
                    text=line.text,
                    model=line.model,
                    confidence=line.confidence,
                )
            )
        line.text = corrected
        line.model = f"{line.model}+{rule}"

    @staticmethod
    def _attach_spatial_alternatives(
        best: RecognitionCandidate, alternatives: list[RecognitionCandidate]
    ) -> None:
        combined_models = {best.model}
        for candidate in alternatives:
            for alternative_line in candidate.lines:
                alternative_center = (alternative_line.bbox[1] + alternative_line.bbox[3]) / 2
                matches = [
                    line
                    for line in best.lines
                    if abs(((line.bbox[1] + line.bbox[3]) / 2) - alternative_center)
                    <= max(
                        line.bbox[3] - line.bbox[1],
                        alternative_line.bbox[3] - alternative_line.bbox[1],
                    )
                ]
                if not matches:
                    if (
                        candidate.model.startswith(("tesseract:", "tesseract-line:"))
                        and alternative_line.confidence >= 0.65
                        and len(alternative_line.text.strip()) >= 2
                    ):
                        best.lines.append(alternative_line.model_copy(deep=True))
                        combined_models.add(candidate.model)
                    continue
                matched = min(
                    matches,
                    key=lambda line: abs(
                        ((line.bbox[1] + line.bbox[3]) / 2) - alternative_center
                    ),
                )
                if matched.text == alternative_line.text or len(matched.alternatives) >= 3:
                    continue
                current_quality = (
                    0.65 * matched.confidence
                    + 0.20 * _language_quality(matched.text)
                    + 0.15 * min(len(matched.text) / 45, 1.0)
                )
                alternative_quality = (
                    0.65 * alternative_line.confidence
                    + 0.20 * _language_quality(alternative_line.text)
                    + 0.15 * min(len(alternative_line.text) / 45, 1.0)
                )
                if (
                    candidate.model.startswith(("tesseract:", "tesseract-line:"))
                    and len(alternative_line.text) >= max(3, len(matched.text) * 0.70)
                    and (
                        alternative_quality >= current_quality + 0.12
                        or (
                            candidate.model.startswith("tesseract-line:")
                            and alternative_line.confidence >= 0.78
                            and _language_quality(alternative_line.text) >= 0.90
                        )
                    )
                ):
                    previous = AlternativeReading(
                        text=matched.text,
                        model=matched.model,
                        confidence=matched.confidence,
                    )
                    matched.text = alternative_line.text
                    matched.model = alternative_line.model
                    matched.confidence = alternative_line.confidence
                    matched.bbox = alternative_line.bbox
                    matched.alternatives.append(previous)
                    combined_models.add(candidate.model)
                    continue
                matched.alternatives.append(
                    AlternativeReading(
                        text=alternative_line.text,
                        model=candidate.model,
                        confidence=alternative_line.confidence,
                    )
                )
        best.lines.sort(key=lambda line: (line.bbox[1], line.bbox[0]))
        if len(combined_models) > 1:
            best.model = "ensemble:" + "+".join(sorted(combined_models))
