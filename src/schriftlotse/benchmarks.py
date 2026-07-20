from __future__ import annotations

import hashlib
import json
import re
import tarfile
import tempfile
import time
import unicodedata
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree

import numpy as np
from PIL import Image

from schriftlotse.cloud import CLOUD_MODEL_OPTIONS, OpenRouterReviewer
from schriftlotse.config import AppPaths
from schriftlotse.database import Database
from schriftlotse.domain import (
    DocumentResult,
    LineResult,
    PageResult,
    ScriptHint,
    SearchQuery,
    SourceDocument,
)
from schriftlotse.model_registry import ModelManager
from schriftlotse.ocr import TrOCRRecognizer
from schriftlotse.search import ArchiveSearch

PUBLIC_GOLD: dict[str, dict[str, Any]] = {
    "kurrent-19": {
        "title": "Swiss Federal Council minutes 1848–1903",
        "doi": "10.5281/zenodo.4746342",
        "license": "CC BY 4.0",
        "model": "trocr-kurrent-19",
        "downloads": {
            "images.tar.gz": "https://zenodo.org/api/records/4746342/files/images.tar.gz/content",
            "page.zip": "https://zenodo.org/api/records/4746342/files/page.zip/content",
        },
        "checksums": {
            "images.tar.gz": "2c0f881c06480594d95f6f686e758c20",
            "page.zip": "4df0dfcd78065fe9bc687ec3f02c9219",
        },
    },
    "kurrent-1665": {
        "title": "Dresdner Hofdiarium 1665",
        "doi": "10.5281/zenodo.14356190",
        "license": "CC BY 4.0",
        "model": "trocr-kurrent-early",
        "downloads": {
            "hofdiarium.zip": "https://zenodo.org/api/records/14356190/files/"
            "Mscr.Dresd.K.80%20GT%20Sample%20Set.zip/content",
        },
        "checksums": {"hofdiarium.zip": "e239d02c32529f1eb2e7f40fcb5d6895"},
    },
}


def edit_distance(reference: list[str] | str, hypothesis: list[str] | str) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, reference_item in enumerate(reference, start=1):
        current = [row]
        for column, hypothesis_item in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (reference_item != hypothesis_item),
                )
            )
        previous = current
    return previous[-1]


def normalize_transcription(text: str) -> str:
    value = unicodedata.normalize("NFC", text).replace("ſ", "s")
    value = re.sub(r"\s+", " ", value).strip()
    return value


@dataclass(slots=True)
class TextMetrics:
    lines: int
    reference_characters: int
    character_errors: int
    cer: float
    reference_words: int
    word_errors: int
    wer: float


def text_metrics(pairs: list[tuple[str, str]], *, normalize: bool = False) -> TextMetrics:
    character_errors = 0
    reference_characters = 0
    word_errors = 0
    reference_words = 0
    for reference, hypothesis in pairs:
        if normalize:
            reference = normalize_transcription(reference)
            hypothesis = normalize_transcription(hypothesis)
        character_errors += edit_distance(reference, hypothesis)
        reference_characters += len(reference)
        reference_tokens = re.findall(r"[\wÄÖÜäöüßſ]+", reference, flags=re.UNICODE)
        hypothesis_tokens = re.findall(r"[\wÄÖÜäöüßſ]+", hypothesis, flags=re.UNICODE)
        word_errors += edit_distance(reference_tokens, hypothesis_tokens)
        reference_words += len(reference_tokens)
    return TextMetrics(
        lines=len(pairs),
        reference_characters=reference_characters,
        character_errors=character_errors,
        cer=character_errors / max(1, reference_characters),
        reference_words=reference_words,
        word_errors=word_errors,
        wer=word_errors / max(1, reference_words),
    )


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path, checksum: str) -> None:
    if destination.is_file() and _md5(destination) == checksum:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    urllib.request.urlretrieve(url, temporary)  # noqa: S310
    if _md5(temporary) != checksum:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Prüfsumme des Goldstandards stimmt nicht: {destination.name}")
    temporary.replace(destination)


def prepare_public_gold(dataset: str, paths: AppPaths | None = None) -> Path:
    if dataset not in PUBLIC_GOLD:
        raise ValueError(f"Unbekannter Goldstandard: {dataset}")
    paths = paths or AppPaths.default()
    root = paths.cache / "benchmarks" / dataset
    extracted = root / "data"
    marker = extracted / ".ready"
    if marker.is_file():
        return extracted
    root.mkdir(parents=True, exist_ok=True)
    downloads = cast(dict[str, str], PUBLIC_GOLD[dataset]["downloads"])
    checksums = cast(dict[str, str], PUBLIC_GOLD[dataset]["checksums"])
    for filename, url in downloads.items():
        archive = root / filename
        _download(url, archive, checksums[filename])
        extracted.mkdir(parents=True, exist_ok=True)
        if filename.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(extracted)
        elif filename.endswith(".tar.gz"):
            with tarfile.open(archive) as bundle:
                bundle.extractall(extracted, filter="data")
    marker.write_text("downloaded from pinned Zenodo record\n", encoding="utf-8")
    return extracted


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _points(value: str) -> tuple[int, int, int, int]:
    coordinates = [tuple(map(int, pair.split(","))) for pair in value.split()]
    xs = [point[0] for point in coordinates]
    ys = [point[1] for point in coordinates]
    return min(xs), min(ys), max(xs), max(ys)


def gold_lines(root: Path) -> list[tuple[str, Image.Image]]:
    images = {
        path.name: path
        for path in root.rglob("*")
        if path.suffix.casefold() in {".jpg", ".jpeg", ".png"}
    }
    samples: list[tuple[str, Image.Image]] = []
    for xml_path in sorted(root.rglob("*.xml")):
        try:
            page = ElementTree.parse(xml_path).getroot()
        except ElementTree.ParseError:
            continue
        page_node = next((node for node in page.iter() if _local_name(node.tag) == "Page"), None)
        if page_node is None:
            continue
        image_path = images.get(Path(page_node.attrib.get("imageFilename", "")).name)
        if image_path is None:
            continue
        with Image.open(image_path) as source:
            image = source.convert("RGB")
            for line in (node for node in page.iter() if _local_name(node.tag) == "TextLine"):
                unicode_node = next(
                    (node for node in line.iter() if _local_name(node.tag) == "Unicode"), None
                )
                coords = next((node for node in line if _local_name(node.tag) == "Coords"), None)
                reference = "" if unicode_node is None else "".join(unicode_node.itertext()).strip()
                if not reference or coords is None or not coords.attrib.get("points"):
                    continue
                left, top, right, bottom = _points(coords.attrib["points"])
                vertical = max(3, round((bottom - top) * 0.10))
                horizontal = max(3, round((right - left) * 0.02))
                crop = image.crop(
                    (
                        max(0, left - horizontal),
                        max(0, top - vertical),
                        min(image.width, right + horizontal),
                        min(image.height, bottom + vertical),
                    )
                )
                samples.append((reference, crop.copy()))
    return samples


def gold_references(root: Path) -> list[str]:
    references: list[str] = []
    for xml_path in sorted(root.rglob("*.xml")):
        try:
            page = ElementTree.parse(xml_path).getroot()
        except ElementTree.ParseError:
            continue
        for line in (node for node in page.iter() if _local_name(node.tag) == "TextLine"):
            unicode_node = next(
                (node for node in line.iter() if _local_name(node.tag) == "Unicode"), None
            )
            reference = "" if unicode_node is None else "".join(unicode_node.itertext()).strip()
            if reference:
                references.append(reference)
    return references


def run_public_gold(dataset: str, sample_size: int = 96) -> dict[str, object]:
    paths = AppPaths.default()
    definition = PUBLIC_GOLD[dataset]
    root = prepare_public_gold(dataset, paths)
    samples = gold_lines(root)
    if not samples:
        raise RuntimeError("Goldstandard enthält keine lesbaren PAGE-XML-Zeilen")
    indices = np.linspace(0, len(samples) - 1, min(sample_size, len(samples))).round().astype(int)
    selected = [samples[int(index)] for index in indices]
    manager = ModelManager(paths)
    model_key = str(definition["model"])
    if not manager.is_installed(model_key):
        raise RuntimeError(f"Modell {model_key} ist nicht installiert")
    recognizer = TrOCRRecognizer(
        manager.path_for(model_key),
        manager.processor_path_for(model_key),
        model_key,
        lambda image: [(0, 0, image.width, image.height)],
    )
    started = time.monotonic()
    pairs: list[tuple[str, str]] = []
    examples: list[dict[str, str]] = []
    for reference, image in selected:
        lines = recognizer.recognize(image, "gold-crop")
        hypothesis = lines[0].text if lines else ""
        pairs.append((reference, hypothesis))
        if len(examples) < 8:
            examples.append({"reference": reference, "hypothesis": hypothesis})
    return {
        "dataset": dataset,
        "title": definition["title"],
        "doi": definition["doi"],
        "license": definition["license"],
        "model": model_key,
        "available_lines": len(samples),
        "sample_strategy": "evenly-spaced-v1",
        "seconds": round(time.monotonic() - started, 2),
        "raw": asdict(text_metrics(pairs)),
        "normalized": asdict(text_metrics(pairs, normalize=True)),
        "examples": examples,
    }


def evaluate_search(engine: ArchiveSearch, qrels_path: Path, limit: int = 10) -> dict[str, object]:
    queries = json.loads(qrels_path.read_text(encoding="utf-8"))
    reciprocal_ranks: list[float] = []
    recalls: list[float] = []
    details: list[dict[str, object]] = []
    for item in queries:
        relevant = set(item["relevant_line_ids"])
        hits = engine.search(
            SearchQuery(text=item["query"], mode=item.get("mode", "intelligent"), limit=limit)
        )
        returned = [hit.line_id for hit in hits]
        ranks = [index + 1 for index, line_id in enumerate(returned) if line_id in relevant]
        reciprocal_ranks.append(1.0 / min(ranks) if ranks else 0.0)
        recalls.append(len(set(returned) & relevant) / max(1, len(relevant)))
        details.append(
            {
                "query": item["query"],
                "returned": returned,
                "relevant": sorted(relevant),
                "first_rank": min(ranks) if ranks else None,
            }
        )
    return {
        "queries": len(queries),
        f"recall@{limit}": sum(recalls) / max(1, len(recalls)),
        "mrr": sum(reciprocal_ranks) / max(1, len(reciprocal_ranks)),
        "details": details,
    }


def run_public_search_benchmark(dataset: str, query_count: int = 40) -> dict[str, object]:
    root = prepare_public_gold(dataset)
    references = gold_references(root)
    if not references:
        raise RuntimeError("Goldstandard enthält keine Referenzzeilen")
    word_lines: dict[str, set[int]] = {}
    for line_index, reference in enumerate(references):
        for word in set(re.findall(r"[A-Za-zÄÖÜäöüßſ]{6,}", reference)):
            word_lines.setdefault(normalize_transcription(word).casefold(), set()).add(line_index)
    unique = sorted(
        (word, next(iter(lines))) for word, lines in word_lines.items() if len(lines) == 1
    )[:query_count]
    with tempfile.TemporaryDirectory(prefix="schriftlotse-search-gold-") as raw_directory:
        database = Database(Path(raw_directory) / "benchmark.sqlite3")
        database.create_job("gold")
        source = SourceDocument(
            id=f"gold-{dataset}",
            title=str(PUBLIC_GOLD[dataset]["title"]),
            source_paths=[Path("public-gold.xml")],
            kind="pagexml-gold",
            page_count=1,
        )
        lines = [
            LineResult(
                id=f"gold-line-{index}",
                text=text,
                bbox=(0, index * 20, 1000, index * 20 + 18),
                confidence=1.0,
                model="ground-truth",
                variant="reference",
            )
            for index, text in enumerate(references)
        ]
        result = DocumentResult(
            document=source,
            year=None,
            script_hint=ScriptHint.HANDWRITING,
            pages=[
                PageResult(
                    page_index=0,
                    source_path=Path("public-gold.xml"),
                    width=1000,
                    height=max(1000, len(lines) * 20),
                    lines=lines,
                    mean_confidence=1.0,
                    expected_cer=0.0,
                    selected_variant="reference",
                    selected_model="ground-truth",
                )
            ],
        )
        database.save_document("gold", result)
        qrels = []
        for index, (word, line_index) in enumerate(unique):
            query = word
            if index % 2 and len(word) >= 6:
                middle = len(word) // 2
                replacement = "e" if word[middle] != "e" else "a"
                query = word[:middle] + replacement + word[middle + 1 :]
            qrels.append(
                {
                    "query": query,
                    "mode": "intelligent",
                    "relevant_line_ids": [f"gold-line-{line_index}"],
                }
            )
        qrels_path = Path(raw_directory) / "qrels.json"
        qrels_path.write_text(json.dumps(qrels), encoding="utf-8")
        report = evaluate_search(ArchiveSearch(database), qrels_path, limit=10)
    return {
        "dataset": dataset,
        "doi": PUBLIC_GOLD[dataset]["doi"],
        "query_design": (
            "unique terms with >=6 characters; every second query has one substitution"
        ),
        **report,
    }


def run_cloud_manifest(manifest: Path, budget_usd: float = 2.0) -> dict[str, object]:
    items = [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][:8]
    reviewer = OpenRouterReviewer(budget_usd)
    if not reviewer.available():
        raise RuntimeError("Kein OpenRouter-Schlüssel eingerichtet")
    results: list[dict[str, object]] = []
    for item in items:
        with Image.open(manifest.parent / item["image"]) as source:
            image = source.convert("RGB")
        for profile in ("fast", "balanced", "quality", "ocr_value"):
            option = CLOUD_MODEL_OPTIONS[profile]
            try:
                review = reviewer.review(
                    image,
                    image,
                    "",
                    item.get("year"),
                    ScriptHint.AUTO,
                    profile=profile,
                    single_line=False,
                )
                metrics = text_metrics([(item["reference"], review.text)], normalize=True)
                results.append(
                    {
                        "id": item.get("id", item["image"]),
                        "profile": profile,
                        "model": option.model,
                        "cer": metrics.cer,
                        "wer": metrics.wer,
                        "cost_usd": review.cost,
                        "text": review.text,
                    }
                )
            except Exception as error:
                results.append(
                    {
                        "id": item.get("id", item["image"]),
                        "profile": profile,
                        "model": option.model,
                        "error": str(error),
                    }
                )
    return {
        "items": len(items),
        "budget_usd": budget_usd,
        "spent_usd": reviewer.spent_usd,
        "results": results,
    }
