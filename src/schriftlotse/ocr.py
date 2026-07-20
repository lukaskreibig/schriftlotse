from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import warnings
from collections import defaultdict
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout, suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol
from xml.etree import ElementTree

import numpy as np
import pytesseract
from PIL import Image
from pytesseract import Output

from schriftlotse.config import AppPaths, Settings, resolve_executable
from schriftlotse.domain import (
    AlternativeReading,
    EngineRun,
    LineResult,
    QualityProfile,
    Reading,
    ReadingKind,
    ReviewStatus,
    ScriptHint,
)
from schriftlotse.model_registry import MODELS, ModelManager
from schriftlotse.pagexml import parse_recognized, write_segmentation
from schriftlotse.preprocessing import PreparedVariant, detect_text_lines

TESSERACT_HISTORICAL_LANGUAGES = (
    "frak2021",
    "deu_latf",
    "script/Fraktur",
    "frk",
    "deu",
)


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
    engine_runs: list[EngineRun] | None = None


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
        self._cache: dict[tuple[int, int, str], list[tuple[int, int, int, int]]] = {}
        self._geometry_cache: dict[
            tuple[int, int, str],
            dict[tuple[int, int, int, int], tuple[list[tuple[int, int]], list[tuple[int, int]]]],
        ] = {}

    @staticmethod
    def _key(image: Image.Image) -> tuple[int, int, str]:
        fingerprint = image.convert("L")
        fingerprint.thumbnail((256, 256))
        return (*image.size, hashlib.sha1(fingerprint.tobytes()).hexdigest())

    def __call__(self, image: Image.Image) -> list[tuple[int, int, int, int]]:
        key = self._key(image)
        if key not in self._cache:
            captured = io.StringIO()
            try:
                with redirect_stdout(captured), redirect_stderr(captured):
                    segmentation = self._task.predict(image, self._config)
                boxes = []
                geometry = {}
                for line in segmentation.lines:
                    line_boxes = OrliLineDetector._boxes([line], image.width, image.height)
                    if not line_boxes:
                        continue
                    box = line_boxes[0]
                    boxes.append(box)
                    polygon = [
                        (int(point[0]), int(point[1]))
                        for point in (getattr(line, "boundary", None) or [])
                    ]
                    baseline = [
                        (int(point[0]), int(point[1]))
                        for point in (getattr(line, "baseline", None) or [])
                    ]
                    geometry[box] = (polygon, baseline)
                self._geometry_cache[key] = geometry
            except Exception:
                boxes = detect_text_lines(image)
                self._geometry_cache[key] = {}
            minimum_width = max(20, image.width // 100)
            # Kraken's segmentation order is the canonical reading order and
            # may differ from a naive top-to-bottom sort on forms and columns.
            self._cache[key] = [
                box for box in boxes if box[2] - box[0] >= minimum_width and box[3] - box[1] >= 8
            ]
        return list(self._cache[key])

    def enrich(self, image: Image.Image, lines: list[LineResult]) -> None:
        geometry = self._geometry_cache.get(self._key(image), {})
        for line in lines:
            polygon, baseline = geometry.get(line.bbox, ([], []))
            if polygon:
                line.polygon = polygon
            if baseline:
                line.baseline = baseline


class TesseractRecognizer:
    def __init__(self, language: str, command: str = "tesseract") -> None:
        self.language = language
        self.name = f"tesseract:{language}"
        self.command = resolve_executable(command) or command
        pytesseract.pytesseract.tesseract_cmd = self.command
        self._cache: dict[tuple[str, str], list[LineResult]] = {}

    @staticmethod
    def available(command: str = "tesseract") -> bool:
        return resolve_executable(command) is not None

    @staticmethod
    def installed_languages(command: str = "tesseract") -> set[str]:
        resolved = resolve_executable(command)
        if resolved is None:
            return set()
        process = subprocess.run(
            [resolved, "--list-langs"],
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
        fingerprint = image.convert("L")
        fingerprint.thumbnail((256, 256))
        cache_key = (variant, hashlib.sha1(fingerprint.tobytes()).hexdigest())
        if cache_key in self._cache:
            return [line.model_copy(deep=True) for line in self._cache[cache_key]]
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
        results.sort(key=lambda item: (item.bbox[1], item.bbox[0]))
        self._cache[cache_key] = [line.model_copy(deep=True) for line in results]
        return results


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
            confidences = [float(value) for value in data["conf"] if float(value) >= 0]
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
            # Independent Kurrent gold data showed that tight/whole-line crops
            # can more than triple CER. Preserve a small context rim around the
            # detected baseline without changing the jump coordinates.
            crops = []
            for left, top, right, bottom in batch_boxes:
                vertical = max(3, round((bottom - top) * 0.10))
                horizontal = max(3, round((right - left) * 0.02))
                crops.append(
                    image.crop(
                        (
                            max(0, left - horizontal),
                            max(0, top - vertical),
                            min(image.width, right + horizontal),
                            min(image.height, bottom + vertical),
                        )
                    ).convert("RGB")
                )
            pixels = self.processor(images=crops, return_tensors="pt").pixel_values.to(self.device)
            with self.torch.inference_mode():
                generated = self.model.generate(
                    pixels,
                    num_beams=4,
                    max_length=128,
                    no_repeat_ngram_size=5,
                    repetition_penalty=1.08,
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
                calibration_ceiling = 0.90 if self.name == "trocr-kurrent-early" else 0.80
                confidence = min(confidence, calibration_ceiling)
                confidence *= 0.55 + 0.45 * _language_quality(text)
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

    def __init__(self, manager: ModelManager, line_detector: LineDetector | None = None) -> None:
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
            # Party's fixed 2560x1920 encoder currently requests an oversized
            # MPS attention buffer on 18/24-GB Apple Silicon. The CPU SDPA path
            # is stable and processes a page in well under a minute.
            device = "cpu" if os.uname().sysname == "Darwin" else "cpu"
            precision = "32-true"
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


class KrakenModelRecognizer:
    """Runs a locally installed Kraken recognition model on shared line geometry."""

    def __init__(self, manager: ModelManager, line_detector: LineDetector | None = None) -> None:
        self.model_path = manager.path_for("ub-german-handwriting")
        self.name = "ub-german-handwriting"
        if not self.model_path.is_file() or shutil.which("kraken") is None:
            raise RuntimeError("Das UB-Mannheim-Kraken-Modell ist nicht vollständig installiert")
        self.line_detector = line_detector

    def recognize(self, image: Image.Image, variant: str) -> list[LineResult]:
        boxes = self.line_detector(image) if self.line_detector else detect_text_lines(image)
        if not boxes:
            return []
        with tempfile.TemporaryDirectory(prefix="schriftlotse-kraken-") as raw_directory:
            directory = Path(raw_directory)
            image_path = directory / "page.png"
            input_xml = directory / "input.xml"
            output_xml = directory / "output.xml"
            image.save(image_path)
            write_segmentation(input_xml, image_path, image.width, image.height, boxes)
            command = [
                "kraken",
                "-f",
                "page",
                "-x",
                "-d",
                "cpu",
                "-r",
                "-i",
                str(input_xml),
                str(output_xml),
                "ocr",
                "-m",
                str(self.model_path),
                "-B",
                "1",
                "--num-line-workers",
                "0",
            ]
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=600, check=False
            )
            if process.returncode != 0 or not output_xml.exists():
                raise RuntimeError(process.stderr.strip() or "Kraken-Erkennung fehlgeschlagen")
            return parse_recognized(output_xml, self.name, variant)


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _churro_node_text(node: ElementTree.Element) -> str:
    parts = [node.text or ""]
    for child in node:
        if _xml_local_name(child.tag) != "Deletion":
            parts.append(_churro_node_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _extract_churro_lines(output: str) -> list[str]:
    """Keep document lines and discard CHURRO metadata or malformed special tokens."""
    cleaned = re.sub(r"<\|[^>]+\|>", "", output).strip()
    start = cleaned.find("<HistoricalDocument")
    end = cleaned.rfind("</HistoricalDocument>")
    if start >= 0 and end >= start:
        xml = cleaned[start : end + len("</HistoricalDocument>")]
        try:
            root = ElementTree.fromstring(xml)
            return [
                text
                for node in root.iter()
                if _xml_local_name(node.tag) == "Line"
                and (text := " ".join(_churro_node_text(node).split()))
            ]
        except ElementTree.ParseError:
            pass
    lines: list[str] = []
    for match in re.findall(r"<Line(?:\s[^>]*)?>(.*?)</Line>", cleaned, flags=re.DOTALL):
        without_deletions = re.sub(
            r"<Deletion(?:\s[^>]*)?>.*?</Deletion>", "", match, flags=re.DOTALL
        )
        plain = re.sub(r"<[^>]+>", "", without_deletions)
        plain = " ".join(plain.replace("&amp;", "&").replace("&lt;", "<").split())
        if plain:
            lines.append(plain)
    return lines


class ChurroMLXRecognizer:
    """Research-profile whole-page reader, quantized for Apple Silicon."""

    name = "churro-mlx-8bit"

    def __init__(self, manager: ModelManager, line_detector: LineDetector | None = None) -> None:
        try:
            from mlx_vlm import generate, load
            from mlx_vlm.prompt_utils import apply_chat_template
        except ImportError as error:
            raise RuntimeError("CHURRO benötigt das optionale MLX-Modellpaket") from error
        path = manager.path_for(self.name)
        if not manager.is_installed(self.name):
            raise RuntimeError("CHURRO MLX ist noch nicht installiert")
        self.model, self.processor = load(str(path))
        self._generate = generate
        self._apply_chat_template = apply_chat_template
        # CHURRO has no archival line geometry. A second neural BLLA pass on
        # the normalized page is expensive and unstable on dense tables, so
        # rough jump coordinates deliberately use the fast OpenCV detector.
        del line_detector

    def recognize(self, image: Image.Image, variant: str) -> list[LineResult]:
        conversation = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Transcribe the entirety of this historical document to XML format."
                        ),
                    }
                ],
            },
            {"role": "user", "content": [{"type": "image"}]},
        ]
        processor_logger = logging.getLogger("transformers.processing_utils")
        previous_processor_level = processor_logger.level
        try:
            # mlx-vlm passes num_images through the legacy processor kwargs
            # route. Transformers logs a harmless migration hint for it.
            processor_logger.setLevel(logging.ERROR)
            formatted = self._apply_chat_template(
                self.processor,
                self.model.config,
                conversation,
                num_images=1,
            )
        finally:
            processor_logger.setLevel(previous_processor_level)
        with tempfile.TemporaryDirectory(prefix="schriftlotse-churro-") as raw_directory:
            image_path = Path(raw_directory) / "page.png"
            image.save(image_path)
            text_lines: list[str] = []
            for _attempt in range(2):
                captured = io.StringIO()
                transformers_logger = logging.getLogger("transformers")
                previous_level = transformers_logger.level
                try:
                    # mlx-vlm currently forwards one harmless processor-kwargs
                    # warning through a handler bound before stderr redirection.
                    # Hide it during inference so the user's status stream stays
                    # useful; genuine generation failures are still raised.
                    transformers_logger.setLevel(logging.ERROR)
                    with redirect_stdout(captured), redirect_stderr(captured):
                        generated = self._generate(
                            self.model,
                            self.processor,
                            formatted,
                            image=str(image_path),
                            max_tokens=3072,
                            temperature=0.0,
                            verbose=False,
                        )
                finally:
                    transformers_logger.setLevel(previous_level)
                text = str(getattr(generated, "text", generated)).strip()
                text_lines = _extract_churro_lines(text)
                if text_lines:
                    break
        if not text_lines:
            return []
        boxes = detect_text_lines(image)
        if boxes and len(boxes) != len(text_lines):
            box_indices = np.linspace(0, len(boxes) - 1, len(text_lines)).round().astype(int)
            boxes = [boxes[index] for index in box_indices]
        elif not boxes:
            line_height = max(1, image.height // len(text_lines))
            boxes = [
                (
                    0,
                    index * line_height,
                    image.width,
                    min(image.height, (index + 1) * line_height),
                )
                for index in range(len(text_lines))
            ]
        return [
            LineResult(
                id=_line_id(self.name, variant, index, text_line),
                text=text_line,
                bbox=boxes[index],
                confidence=0.72,
                model=self.name,
                variant=variant,
            )
            for index, text_line in enumerate(text_lines)
        ]


class RecognizerRouter:
    def __init__(self, paths: AppPaths, settings: Settings) -> None:
        self.paths = paths
        self.settings = settings
        self.manager = ModelManager(paths)
        self.quality_profile = QualityProfile.BEST_LOCAL
        self._recognizer_cache: dict[str, Recognizer] = {}
        self._line_detector: LineDetector | None = None
        self._line_detector_initialized = False

    def _cached_recognizer(self, key: str, factory: Callable[[], Recognizer]) -> Recognizer:
        if key not in self._recognizer_cache:
            self._recognizer_cache[key] = factory()
        return self._recognizer_cache[key]

    def _model_keys(self, year: int | None, script_hint: ScriptHint) -> list[str]:
        if script_hint in {ScriptHint.PRINT, ScriptHint.TYPEWRITER}:
            return []
        if year is None:
            return ["trocr-kurrent-19", "trocr-kurrent-early"]
        if year is not None and year < 1500:
            return ["trocr-medieval"]
        if year is not None and year < 1800:
            return ["trocr-kurrent-early"]
        if year <= 1945:
            return ["trocr-kurrent-19"]
        return ["trocr-modern"]

    @staticmethod
    def _benchmark_expected_cer(model: str, year: int | None) -> float | None:
        """Conservative priors from independent, pinned German gold sets.

        These are routing priors, not promises for an individual page. The
        values deliberately avoid the much lower in-domain model-card figures.
        """
        if model == "trocr-kurrent-early" and year is not None and year < 1800:
            return 0.093
        if model == "trocr-kurrent-19" and year is not None and 1800 <= year <= 1945:
            return 0.212
        if model.startswith("churro-") and year is not None and year < 1800:
            return 0.284
        return None

    def preclassify_print(
        self, image: Image.Image, script_hint: ScriptHint
    ) -> tuple[ScriptHint, str, float]:
        """Detect confidently machine-made text with one reusable cheap OCR pass.

        Historical handwriting is deliberately the negative/default class.  The
        threshold was calibrated on the bundled private evaluation corpus so a
        noisy charter or a sparse form cannot accidentally disable CHURRO/TrOCR.
        """
        if script_hint != ScriptHint.AUTO or self.quality_profile == QualityProfile.FAST:
            return script_hint, "", 0.0
        installed = TesseractRecognizer.installed_languages(self.settings.tesseract_command)
        language = next(
            (name for name in TESSERACT_HISTORICAL_LANGUAGES if name in installed),
            None,
        )
        if language is None:
            return script_hint, "", 0.0
        recognizer = self._cached_recognizer(
            f"tesseract:{language}",
            partial(TesseractRecognizer, language, self.settings.tesseract_command),
        )
        try:
            lines = recognizer.recognize(image, "original")
        except (RuntimeError, subprocess.SubprocessError):
            return script_hint, "", 0.0
        text = "\n".join(line.text for line in lines)
        characters = sum(len(line.text.strip()) for line in lines)
        confidence = float(np.mean([line.confidence for line in lines])) if lines else 0.0
        language_quality = _language_quality(text)
        if (characters >= 500 and confidence >= 0.50 and language_quality >= 0.78) or (
            characters >= 3000 and confidence >= 0.55 and language_quality >= 0.55
        ):
            return ScriptHint.PRINT, text, confidence
        return script_hint, text, confidence

    @staticmethod
    def party_memory_available() -> bool:
        return True

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
                key = f"tesseract:{language}"
                recognizers.append(
                    self._cached_recognizer(
                        key,
                        partial(
                            TesseractRecognizer,
                            language,
                            self.settings.tesseract_command,
                        ),
                    )
                )
        if self.settings.advanced_models and self.quality_profile != QualityProfile.FAST:
            model_keys = self._model_keys(year, script_hint)
            handwriting_models = script_hint not in {ScriptHint.PRINT, ScriptHint.TYPEWRITER}
            party_available = (
                handwriting_models
                and self.quality_profile == QualityProfile.LICENSE_CLEAR
                and self.manager.is_installed("party-v4")
                and self.party_memory_available()
            )
            needs_line_detector = handwriting_models and (
                party_available
                or self.manager.is_installed("ub-german-handwriting")
                or self.manager.is_installed("churro-mlx-8bit")
                or any(self.manager.is_installed(key) for key in model_keys)
            )
            if needs_line_detector and not self._line_detector_initialized:
                self._line_detector_initialized = True
                with suppress(RuntimeError):
                    self._line_detector = KrakenLineDetector()
                if self._line_detector is None and self.manager.is_installed("orli"):
                    with suppress(RuntimeError):
                        self._line_detector = OrliLineDetector(self.manager)
            line_detector = self._line_detector
            if needs_line_detector and line_detector is not None:
                for language in preferred:
                    if language in installed:
                        key = f"tesseract-line:{language}"
                        recognizers.append(
                            self._cached_recognizer(
                                key,
                                partial(
                                    SegmentedTesseractRecognizer,
                                    language,
                                    line_detector,
                                    self.settings.tesseract_command,
                                ),
                            )
                        )
            if party_available:
                with suppress(RuntimeError):
                    recognizers.append(
                        self._cached_recognizer(
                            "party-v4",
                            lambda: PartyRecognizer(self.manager, line_detector),
                        )
                    )
            if handwriting_models and self.manager.is_installed("ub-german-handwriting"):
                with suppress(RuntimeError):
                    recognizers.append(
                        self._cached_recognizer(
                            "ub-german-handwriting",
                            lambda: KrakenModelRecognizer(self.manager, line_detector),
                        )
                    )
            for key in model_keys:
                if self.manager.is_installed(key):
                    try:
                        recognizers.append(
                            self._cached_recognizer(
                                key,
                                partial(
                                    TrOCRRecognizer,
                                    self.manager.path_for(key),
                                    self.manager.processor_path_for(key),
                                    key,
                                    line_detector,
                                ),
                            )
                        )
                    except RuntimeError:
                        continue
            if (
                self.quality_profile in {QualityProfile.BEST_LOCAL, QualityProfile.ADAPTIVE}
                and script_hint not in {ScriptHint.PRINT, ScriptHint.TYPEWRITER}
                and self.manager.is_installed("churro-mlx-8bit")
            ):
                with suppress(RuntimeError):
                    recognizers.append(
                        self._cached_recognizer(
                            "churro-mlx-8bit",
                            lambda: ChurroMLXRecognizer(self.manager, line_detector),
                        )
                    )
        if not recognizers:
            if not TesseractRecognizer.available(self.settings.tesseract_command):
                raise RuntimeError(
                    "Keine OCR-Engine installiert. Bitte Tesseract installieren "
                    "oder freie Modelle laden."
                )
            fallback_language = "deu" if "deu" in installed else next(iter(installed), "eng")
            recognizers.append(
                self._cached_recognizer(
                    f"tesseract:{fallback_language}",
                    lambda: TesseractRecognizer(fallback_language, self.settings.tesseract_command),
                )
            )
        return recognizers

    def recognize_variants(
        self,
        variants: list[PreparedVariant],
        year: int | None,
        script_hint: ScriptHint,
    ) -> RecognitionCandidate:
        candidates: list[RecognitionCandidate] = []
        engine_runs: list[EngineRun] = []
        recognizers = self.recognizers(year, script_hint)
        for variant in variants:
            for recognizer in recognizers:
                if isinstance(
                    recognizer,
                    (
                        TrOCRRecognizer,
                        PartyRecognizer,
                        KrakenModelRecognizer,
                        ChurroMLXRecognizer,
                        SegmentedTesseractRecognizer,
                    ),
                ):
                    required_variant = (
                        "normalisiert"
                        if isinstance(recognizer, ChurroMLXRecognizer)
                        else "original"
                    )
                    if variant.metadata.name != required_variant:
                        continue
                try:
                    started = time.monotonic()
                    lines = recognizer.recognize(variant.image, variant.metadata.name)
                except (RuntimeError, subprocess.SubprocessError) as error:
                    engine_runs.append(
                        EngineRun(
                            engine=recognizer.name,
                            revision=(
                                MODELS[recognizer.name].revision
                                if recognizer.name in MODELS
                                else None
                            ),
                            backend=self._backend_name(recognizer),
                            duration_seconds=time.monotonic() - started,
                            success=False,
                            message=str(error),
                        )
                    )
                    continue
                engine_runs.append(
                    EngineRun(
                        engine=recognizer.name,
                        revision=(
                            MODELS[recognizer.name].revision if recognizer.name in MODELS else None
                        ),
                        backend=self._backend_name(recognizer),
                        duration_seconds=time.monotonic() - started,
                        success=bool(lines),
                        message="" if lines else "Keine Textzeilen geliefert",
                    )
                )
                if isinstance(self._line_detector, KrakenLineDetector):
                    self._line_detector.enrich(variant.image, lines)
                text = "\n".join(line.text for line in lines)
                if not text.strip():
                    continue
                confidence = float(np.mean([line.confidence for line in lines])) if lines else 0.0
                language = _language_quality(text)
                coverage = _recognition_coverage(lines, text, variant.image.height)
                score = 0.45 * confidence + 0.15 * language + 0.40 * coverage
                if isinstance(recognizer, ChurroMLXRecognizer):
                    # In the best-local profile CHURRO is the intended full-page
                    # reader. Its text remains reviewable and all specialist
                    # readings are retained alongside it.
                    score += 0.10
                expected_cer = max(
                    0.01,
                    min(
                        0.99,
                        1.0 - (0.55 * confidence + 0.15 * language + 0.30 * coverage),
                    ),
                )
                if recognizer.name.startswith("trocr-"):
                    expected_cer = max(0.12, expected_cer)
                if isinstance(recognizer, ChurroMLXRecognizer):
                    expected_cer = max(0.14, expected_cer)
                benchmark_cer = self._benchmark_expected_cer(recognizer.name, year)
                if benchmark_cer is not None:
                    expected_cer = max(benchmark_cer, expected_cer * 0.45 + benchmark_cer * 0.55)
                    score = 0.55 * (1.0 - expected_cer) + 0.30 * coverage + 0.15 * language
                if year is not None and (
                    (year < 1800 and recognizer.name == "trocr-kurrent-19")
                    or (year >= 1800 and recognizer.name == "trocr-kurrent-early")
                ):
                    # Epoch-incompatible specialists must not win merely through
                    # an overconfident sequence score.
                    expected_cer = max(expected_cer, 0.55)
                    score -= 0.35
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
        for candidate in candidates:
            candidate.engine_runs = engine_runs
        candidates.sort(key=lambda item: item.score, reverse=True)
        best = candidates[0]
        if self.quality_profile in {QualityProfile.BEST_LOCAL, QualityProfile.ADAPTIVE}:
            # Gold-standard routing: specialized line HTR is the primary reader
            # when the supplied/detected year matches its validated epoch.
            # CHURRO remains a valuable full-page second opinion and fallback.
            preferred_model = None
            if year is not None and year < 1800:
                preferred_model = "trocr-kurrent-early"
            elif year is not None and year <= 1945:
                preferred_model = "trocr-kurrent-19"
            viable_specialists = [
                candidate
                for candidate in candidates
                if candidate.model == preferred_model
                and candidate.coverage >= 0.03
                and len("".join(line.text for line in candidate.lines).strip()) >= 40
                and _language_quality("\n".join(line.text for line in candidate.lines)) >= 0.20
            ]
            if viable_specialists:
                best = max(viable_specialists, key=lambda item: item.score)
        self._attach_spatial_alternatives(
            best, [candidate for candidate in candidates if candidate is not best]
        )
        for index, line in enumerate(best.lines):
            line.readings = [
                Reading(
                    id=f"{line.id}:konsens:{index}",
                    kind=ReadingKind.CONSENSUS,
                    text=line.text,
                    model=line.model,
                    confidence=line.confidence,
                ),
                *[
                    Reading(
                        id=f"{line.id}:alternative:{index}:{alt_index}",
                        kind=ReadingKind.ENGINE,
                        text=alternative.text,
                        model=alternative.model,
                        confidence=alternative.confidence,
                    )
                    for alt_index, alternative in enumerate(line.alternatives)
                ],
            ]
            disagreement = any(
                alternative.text.casefold() != line.text.casefold()
                for alternative in line.alternatives
            )
            if disagreement or line.confidence < 0.65:
                line.review_status = ReviewStatus.UNCERTAIN
        return best

    @staticmethod
    def _backend_name(recognizer: Recognizer) -> str:
        device = getattr(recognizer, "device", None)
        if device:
            return str(device)
        if isinstance(recognizer, ChurroMLXRecognizer):
            return "mlx-metal"
        return "cpu"

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
                    if best.model.startswith("churro-") and best.lines:
                        matched = min(
                            best.lines,
                            key=lambda line: abs(
                                ((line.bbox[1] + line.bbox[3]) / 2) - alternative_center
                            ),
                        )
                        if not any(
                            alternative.model == candidate.model
                            for alternative in matched.alternatives
                        ):
                            matched.alternatives.append(
                                AlternativeReading(
                                    text=alternative_line.text,
                                    model=candidate.model,
                                    confidence=alternative_line.confidence,
                                )
                            )
                        continue
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
                    key=lambda line: abs(((line.bbox[1] + line.bbox[3]) / 2) - alternative_center),
                )
                if matched.text == alternative_line.text or any(
                    alternative.model == candidate.model for alternative in matched.alternatives
                ):
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
                    and not matched.model.startswith("churro-")
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
