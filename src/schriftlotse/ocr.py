from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
import tempfile
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


class Recognizer(Protocol):
    name: str

    def recognize(self, image: Image.Image, variant: str) -> list[LineResult]: ...


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


@dataclass(slots=True)
class RecognitionCandidate:
    model: str
    variant: str
    lines: list[LineResult]
    score: float
    expected_cer: float


class OrliLineDetector:
    """Lazy Orli baseline detector with a conservative OpenCV fallback."""

    def __init__(self, manager: ModelManager) -> None:
        try:
            import torch
            from kraken.models import load_models
            from kraken.tasks.segmentation import SegmentationTaskModel
            from orli.configs import OrliSegmentationInferenceConfig
        except ImportError as error:
            raise RuntimeError("Orli benötigt das optionale Modellpaket") from error
        model_path = manager.path_for("orli")
        if not model_path.exists():
            raise RuntimeError("Orli ist noch nicht installiert")
        self._task = SegmentationTaskModel(load_models(model_path, tasks=["segmentation"]))
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
        padding = max(12, height // 120)
        for line in lines:
            points = getattr(line, "boundary", None) or getattr(line, "baseline", None)
            if not points:
                continue
            xs = [int(point[0]) for point in points]
            ys = [int(point[1]) for point in points]
            left = max(0, min(xs) - padding)
            right = min(width, max(xs) + padding)
            top = max(0, min(ys) - 2 * padding)
            bottom = min(height, max(ys) + padding)
            if right > left and bottom > top:
                boxes.append((left, top, right, bottom))
        return boxes


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
        pytesseract.pytesseract.tesseract_cmd = command
        return set(pytesseract.get_languages(config=""))

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


class TrOCRRecognizer:
    def __init__(
        self, model_path: Path, name: str, line_detector: OrliLineDetector | None = None
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
        self.processor: Any = processor_factory.from_pretrained(model_path, local_files_only=True)
        self.model: Any = model_factory.from_pretrained(model_path, local_files_only=True)
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model.to(self.device).eval()
        self.line_detector = line_detector

    def recognize(self, image: Image.Image, variant: str) -> list[LineResult]:
        boxes = self.line_detector(image) if self.line_detector else detect_text_lines(image)
        results: list[LineResult] = []
        for index, bbox in enumerate(boxes):
            crop = image.crop(bbox).convert("RGB")
            pixels = self.processor(images=crop, return_tensors="pt").pixel_values.to(self.device)
            with self.torch.inference_mode():
                generated = self.model.generate(
                    pixels,
                    num_beams=4,
                    max_new_tokens=256,
                    return_dict_in_generate=True,
                    output_scores=True,
                )
            text = self.processor.batch_decode(generated.sequences, skip_special_tokens=True)[
                0
            ].strip()
            if not text:
                continue
            confidence = 0.72
            if getattr(generated, "sequences_scores", None) is not None:
                confidence = max(0.05, min(0.98, math.exp(float(generated.sequences_scores[0]))))
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
        self, manager: ModelManager, line_detector: OrliLineDetector | None = None
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
                "4",
                "--add-lang-token",
            ]
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=600, check=False
            )
            if process.returncode != 0:
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

    def recognizers(self, year: int | None, script_hint: ScriptHint) -> list[Recognizer]:
        recognizers: list[Recognizer] = []
        installed = TesseractRecognizer.installed_languages(self.settings.tesseract_command)
        preferred = (
            ["frak2021", "deu_latf", "deu"] if script_hint != ScriptHint.TYPEWRITER else ["deu"]
        )
        for language in preferred:
            if language in installed:
                recognizers.append(TesseractRecognizer(language, self.settings.tesseract_command))
        if self.settings.advanced_models:
            line_detector: OrliLineDetector | None = None
            if self.manager.is_installed("orli"):
                with suppress(RuntimeError):
                    line_detector = OrliLineDetector(self.manager)
            if self.manager.is_installed("party-v4"):
                with suppress(RuntimeError):
                    recognizers.append(PartyRecognizer(self.manager, line_detector))
            for key in self._model_keys(year, script_hint):
                if self.manager.is_installed(key):
                    try:
                        recognizers.append(
                            TrOCRRecognizer(self.manager.path_for(key), key, line_detector)
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
                try:
                    lines = recognizer.recognize(variant.image, variant.metadata.name)
                except (RuntimeError, subprocess.SubprocessError):
                    continue
                text = "\n".join(line.text for line in lines)
                if not text.strip():
                    continue
                confidence = float(np.mean([line.confidence for line in lines])) if lines else 0.0
                language = _language_quality(text)
                coverage = min(len(text) / max(80, variant.image.height / 4), 1.0)
                score = 0.62 * confidence + 0.23 * language + 0.15 * coverage
                expected_cer = max(0.01, min(0.99, 1.0 - (0.80 * confidence + 0.20 * language)))
                candidates.append(
                    RecognitionCandidate(
                        model=recognizer.name,
                        variant=variant.metadata.name,
                        lines=lines,
                        score=score,
                        expected_cer=expected_cer,
                    )
                )
        if not candidates:
            raise RuntimeError("Auf der Seite konnte kein Text erkannt werden")
        candidates.sort(key=lambda item: item.score, reverse=True)
        best = candidates[0]
        for alternative in candidates[1:3]:
            for best_line, alternative_line in zip(best.lines, alternative.lines, strict=False):
                if best_line.text != alternative_line.text:
                    best_line.alternatives.append(
                        AlternativeReading(
                            text=alternative_line.text,
                            model=alternative.model,
                            confidence=alternative_line.confidence,
                        )
                    )
        return best
