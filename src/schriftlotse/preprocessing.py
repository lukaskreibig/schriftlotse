from __future__ import annotations

import math
import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from skimage.filters import threshold_sauvola

from schriftlotse.domain import (
    DocumentProfile,
    ImageDiagnostics,
    ImageVariant,
    LayoutClass,
    PeriodEstimate,
    ScriptClass,
    ScriptHint,
)


@dataclass(slots=True)
class PreparedVariant:
    metadata: ImageVariant
    image: Image.Image


@dataclass(slots=True)
class LogicalPageImage:
    id: str
    image: Image.Image
    source_bbox: tuple[int, int, int, int]
    transform: list[list[float]]
    warnings: list[str]


def _to_rgb_array(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("RGB"), dtype=np.uint8)


def _gray(array: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)


def estimate_skew(gray: np.ndarray) -> float:
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLines(edges, 1, np.pi / 1800, max(80, gray.shape[1] // 8))
    if lines is None:
        return 0.0
    angles: list[float] = []
    for line in lines[:100]:
        theta = float(line[0][1])
        angle = math.degrees(theta) - 90.0
        if -8 <= angle <= 8:
            angles.append(angle)
    return float(np.median(angles)) if angles else 0.0


def diagnose(image: Image.Image) -> ImageDiagnostics:
    array = _to_rgb_array(image)
    gray = _gray(array)
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    return ImageDiagnostics(
        brightness=float(gray.mean() / 255.0),
        contrast=float(gray.std() / 128.0),
        sharpness=float(min(laplacian.var() / 1000.0, 1.0)),
        skew_degrees=estimate_skew(gray),
        clipped_dark=float(np.mean(gray <= 3)),
        clipped_light=float(np.mean(gray >= 252)),
    )


def _deskew(array: np.ndarray, angle: float) -> tuple[np.ndarray, list[list[float]]]:
    height, width = array.shape[:2]
    if abs(angle) < 0.2 or abs(angle) > 8:
        return array, np.eye(3).tolist()
    center = (width / 2, height / 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    border = 255 if array.ndim == 2 else (255, 255, 255)
    rotated = cv2.warpAffine(
        array,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border,
    )
    homogeneous = np.vstack([matrix, [0, 0, 1]])
    return rotated, homogeneous.tolist()


def _illumination_normalized(gray: np.ndarray) -> np.ndarray:
    smallest = min(gray.shape)
    size = max(3, (smallest // 20) | 1)
    size = min(size, smallest if smallest % 2 else smallest - 1)
    background = cv2.medianBlur(gray, size)
    normalized = cv2.divide(gray, np.maximum(background, 1), scale=240)
    destination = np.empty_like(normalized)
    result = cv2.normalize(normalized, destination, 0, 255, cv2.NORM_MINMAX)
    return np.asarray(result, dtype=np.uint8)


def _contrast_variant(gray: np.ndarray) -> np.ndarray:
    normalized = _illumination_normalized(gray)
    denoised = cv2.fastNlMeansDenoising(
        normalized, None, h=4, templateWindowSize=7, searchWindowSize=21
    )
    clahe = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 8))
    enhanced = clahe.apply(denoised)
    blurred = cv2.GaussianBlur(enhanced, (0, 0), 0.7)
    return cv2.addWeighted(enhanced, 1.12, blurred, -0.12, 0)


def _binary_variant(gray: np.ndarray) -> np.ndarray:
    normalized = _illumination_normalized(gray)
    window = max(25, (min(gray.shape) // 40) | 1)
    threshold = threshold_sauvola(normalized, window_size=window, k=0.18)
    return np.where(normalized > threshold, 255, 0).astype(np.uint8)


def generate_variants(image: Image.Image) -> list[PreparedVariant]:
    rgb = _to_rgb_array(image)
    initial_diagnostics = diagnose(image)
    deskewed, transform = _deskew(rgb, initial_diagnostics.skew_degrees)
    gray = _gray(deskewed)
    candidates: list[tuple[str, str, np.ndarray, dict[str, float | str]]] = [
        (
            "original",
            "Orientiertes, ansonsten unverändertes Farbbild",
            deskewed,
            {"deskew_degrees": initial_diagnostics.skew_degrees},
        ),
        (
            "normalisiert",
            "Lokal beleuchtungsnormalisierte Graustufen",
            _illumination_normalized(gray),
            {"method": "background_division"},
        ),
        (
            "kontrast",
            "Mild entrauscht und lokal kontrastverstärkt",
            _contrast_variant(gray),
            {"method": "nlmeans_clahe_unsharp", "clahe_limit": 1.8},
        ),
        (
            "binarisiert",
            "Adaptive Sauvola-Binarisierung",
            _binary_variant(gray),
            {"method": "sauvola", "k": 0.18},
        ),
    ]
    variants: list[PreparedVariant] = []
    for name, description, array, parameters in candidates:
        pil = Image.fromarray(array).convert("RGB")
        variants.append(
            PreparedVariant(
                metadata=ImageVariant(
                    name=name,
                    description=description,
                    diagnostics=diagnose(pil),
                    transform=transform,
                    parameters=parameters,
                ),
                image=pil,
            )
        )
    return variants


def variant_quality(metadata: ImageVariant) -> float:
    diagnostics = metadata.diagnostics
    exposure = 1.0 - min(abs(diagnostics.brightness - 0.78) / 0.78, 1.0)
    clipping = 1.0 - min(diagnostics.clipped_dark + diagnostics.clipped_light, 1.0)
    contrast = min(diagnostics.contrast, 1.0)
    return 0.35 * exposure + 0.30 * clipping + 0.20 * contrast + 0.15 * diagnostics.sharpness


def select_preflight_variants(
    variants: list[PreparedVariant], limit: int = 2
) -> list[PreparedVariant]:
    if not variants:
        return []
    original = variants[0]
    ranked = sorted(variants[1:], key=lambda item: variant_quality(item.metadata), reverse=True)
    selected = [original, *ranked[: max(0, limit - 1)]]
    return selected[:limit]


def detect_text_lines(image: Image.Image) -> list[tuple[int, int, int, int]]:
    gray = _gray(_to_rgb_array(image))
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    width = gray.shape[1]
    kernel_width = max(15, width // 60)
    joined = cv2.morphologyEx(
        binary,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 3)),
    )
    contours, _ = cv2.findContours(joined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < width * 0.04 or h < 8 or h > gray.shape[0] * 0.2:
            continue
        margin = max(2, h // 8)
        boxes.append(
            (
                max(0, x - margin),
                max(0, y - margin),
                min(width, x + w + margin),
                min(gray.shape[0], y + h + margin),
            )
        )
    return sorted(boxes, key=lambda box: (box[1], box[0]))


def _orientation(image: Image.Image) -> int:
    """Returns a conservative OCR orientation in clockwise degrees."""
    if min(image.size) < 180:
        return 0
    with suppress(Exception):
        import pytesseract

        sample = image.copy()
        sample.thumbnail((1400, 1400))
        result = pytesseract.image_to_osd(sample, output_type=pytesseract.Output.DICT)
        confidence = float(result.get("orientation_conf", 0.0))
        rotation = int(result.get("rotate", 0)) % 360
        if confidence >= 4.0 and rotation in {0, 90, 180, 270}:
            return rotation
    return 0


def _content_bbox(image: Image.Image) -> tuple[int, int, int, int]:
    """Finds a conservative photographed page rectangle without clipping marginalia."""
    array = _to_rgb_array(image)
    gray = _gray(array)
    height, width = gray.shape
    threshold = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    contours, _ = cv2.findContours(threshold, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w * h >= width * height * 0.45 and w >= width * 0.55 and h >= height * 0.55:
            candidates.append((x, y, x + w, y + h))
    if not candidates:
        return (0, 0, width, height)
    x1, y1, x2, y2 = max(candidates, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
    padding = max(8, min(width, height) // 100)
    return (
        max(0, x1 - padding),
        max(0, y1 - padding),
        min(width, x2 + padding),
        min(height, y2 + padding),
    )


def _spread_split(image: Image.Image) -> int | None:
    width, height = image.size
    aspect = width / max(height, 1)
    # Wide newspaper snippets and tables also contain column gutters. A real
    # photographed spread still has appreciable height relative to its width.
    if aspect < 1.22 or height / max(width, 1) < 0.62:
        return None
    gray = _gray(_to_rgb_array(image))
    scale = min(1.0, 1200 / max(width, height))
    if scale < 1.0:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ink = 255 - gray
    column_ink = np.mean(ink, axis=0)
    dark_fraction = np.mean(gray < 100, axis=0)
    lo, hi = int(len(column_ink) * 0.44), int(len(column_ink) * 0.56)
    if hi <= lo:
        return None
    seam = lo + int(np.argmax(dark_fraction[lo:hi]))
    # Film photographs of open books usually show a continuous dark binding.
    # A pale gutter remains a conservative fallback.
    center = seam if float(dark_fraction[seam]) >= 0.28 else lo + int(np.argmin(column_ink[lo:hi]))
    left_ink = float(np.mean(column_ink[int(len(column_ink) * 0.08) : center]))
    right_ink = float(np.mean(column_ink[center : int(len(column_ink) * 0.92)]))
    gutter = float(np.mean(column_ink[max(0, center - 3) : center + 4]))
    dark_seam = float(dark_fraction[center]) >= 0.28
    if min(left_ink, right_ink) < 4.0 or (
        not dark_seam and gutter > min(left_ink, right_ink) * 0.82
    ):
        return None
    return int(center / scale)


def _logical_transform(
    rotation: int,
    original_size: tuple[int, int],
    offset: tuple[int, int],
) -> list[list[float]]:
    """Maps logical-page coordinates back into the untouched source image."""
    width, height = original_size
    x, y = offset
    match rotation % 360:
        case 90:
            return [[0.0, 1.0, float(y)], [-1.0, 0.0, float(height - 1 - x)], [0.0, 0.0, 1.0]]
        case 180:
            return [
                [-1.0, 0.0, float(width - 1 - x)],
                [0.0, -1.0, float(height - 1 - y)],
                [0.0, 0.0, 1.0],
            ]
        case 270:
            return [[0.0, -1.0, float(width - 1 - y)], [1.0, 0.0, float(x)], [0.0, 0.0, 1.0]]
        case _:
            return [[1.0, 0.0, float(x)], [0.0, 1.0, float(y)], [0.0, 0.0, 1.0]]


def _transformed_bbox(
    transform: list[list[float]], size: tuple[int, int], original_size: tuple[int, int]
) -> tuple[int, int, int, int]:
    width, height = size
    points: list[tuple[float, float]] = []
    for x, y in ((0, 0), (width, 0), (width, height), (0, height)):
        points.append(
            (
                transform[0][0] * x + transform[0][1] * y + transform[0][2],
                transform[1][0] * x + transform[1][1] * y + transform[1][2],
            )
        )
    return (
        max(0, round(min(point[0] for point in points))),
        max(0, round(min(point[1] for point in points))),
        min(original_size[0], round(max(point[0] for point in points))),
        min(original_size[1], round(max(point[1] for point in points))),
    )


def split_logical_pages(image: Image.Image, source_id: str) -> list[LogicalPageImage]:
    """Orients, conservatively crops, and optionally splits a photographed spread."""
    rotation = _orientation(image)
    oriented = image.rotate(-rotation, expand=True, fillcolor="white") if rotation else image
    crop = _content_bbox(oriented)
    page = oriented.crop(crop)
    split = _spread_split(page)
    warnings: list[str] = []
    if rotation:
        warnings.append(f"Seite automatisch um {rotation}° gedreht")
    if crop != (0, 0, oriented.width, oriented.height):
        warnings.append("Äußerer Aufnahme-/Filmrand wurde für die Analyse ausgeblendet")
    if split is None:
        transform = _logical_transform(rotation, image.size, (crop[0], crop[1]))
        source_bbox = _transformed_bbox(transform, page.size, image.size)
        return [LogicalPageImage(source_id, page, source_bbox, transform, warnings)]
    gap = max(2, page.width // 500)
    results: list[LogicalPageImage] = []
    for suffix, local_bbox in (
        ("links", (0, 0, max(1, split - gap), page.height)),
        ("rechts", (min(page.width - 1, split + gap), 0, page.width, page.height)),
    ):
        x1, y1, x2, y2 = local_bbox
        split_page = page.crop(local_bbox)
        inner_crop = _content_bbox(split_page)
        logical_page = split_page.crop(inner_crop)
        inner_x1, inner_y1, inner_x2, inner_y2 = inner_crop
        offset = (crop[0] + x1 + inner_x1, crop[1] + y1 + inner_y1)
        transform = _logical_transform(rotation, image.size, offset)
        source_bbox = _transformed_bbox(transform, logical_page.size, image.size)
        page_warnings = [*warnings, "Doppelseite automatisch am Bundsteg getrennt"]
        if inner_crop != (0, 0, x2 - x1, y2 - y1):
            page_warnings.append("Film-/Aufnahmerand der Einzelseite ausgeblendet")
        results.append(
            LogicalPageImage(
                id=f"{source_id}-{suffix}",
                image=logical_page,
                source_bbox=source_bbox,
                transform=transform,
                warnings=page_warnings,
            )
        )
    return results


YEAR_RE = re.compile(r"(?<!\d)(1[4-9]\d{2}|20\d{2})(?!\d)")
COMPACT_DATE_RE = re.compile(
    r"(?<!\d)((?:1[4-9]|20)\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"
)


def profile_page(
    image: Image.Image,
    *,
    filename: str = "",
    quick_text: str = "",
    year_hint: int | None = None,
    script_hint: ScriptHint = ScriptHint.AUTO,
    selected_model: str = "",
) -> DocumentProfile:
    evidence: list[str] = []
    filename_stem = Path(filename).stem
    filename_candidates = [int(value) for value in YEAR_RE.findall(filename_stem)]
    filename_candidates.extend(int(value) for value in COMPACT_DATE_RE.findall(filename_stem))
    text_candidates = [int(value) for value in YEAR_RE.findall(quick_text)]
    exact_year: int | None = None
    confidence = 0.35
    if filename_candidates:
        exact_year = max(set(filename_candidates), key=filename_candidates.count)
        confidence = 0.88
        evidence.append(f"Jahresangabe {exact_year} im Dateinamen")
    elif text_candidates:
        candidate = max(set(text_candidates), key=text_candidates.count)
        occurrences = text_candidates.count(candidate)
        if occurrences >= 2:
            exact_year = candidate
            confidence = 0.74
            evidence.append(f"mehrfach erkannter Jahreskandidat {candidate}")
        else:
            evidence.append(
                f"einzelner unsicherer OCR-Jahreskandidat {candidate} (nicht übernommen)"
            )
    if year_hint is not None:
        if exact_year is not None and exact_year != year_hint:
            evidence.append(
                f"manuelle Angabe {year_hint} widerspricht sichtbarem Jahr {exact_year}"
            )
        elif exact_year is None:
            exact_year = year_hint
            confidence = 1.0
            evidence.append(f"manuelle Jahresangabe {year_hint}")
    if exact_year is not None:
        period = PeriodEstimate(
            year_from=exact_year,
            year_to=exact_year,
            exact_year=exact_year,
            confidence=confidence,
            evidence=list(evidence),
        )
    else:
        period = PeriodEstimate(
            year_from=1800,
            year_to=1945,
            confidence=0.30,
            evidence=["nur grobe Schätzung aus dem Modellprofil"],
        )
    if script_hint == ScriptHint.PRINT:
        script = ScriptClass.FRAKTUR if "frak" in selected_model else ScriptClass.ANTIQUA
    elif script_hint == ScriptHint.TYPEWRITER:
        script = ScriptClass.TYPEWRITER
    elif script_hint == ScriptHint.HANDWRITING or (
        "trocr" in selected_model or "party" in selected_model
    ):
        script = ScriptClass.KURRENT
    elif "frak" in selected_model or "latf" in selected_model:
        script = ScriptClass.FRAKTUR
    else:
        script = ScriptClass.UNKNOWN
    lowered = quick_text.casefold()
    horizontal = cv2.HoughLinesP(
        cv2.Canny(_gray(_to_rgb_array(image)), 50, 150),
        1,
        np.pi / 180,
        threshold=max(50, image.width // 8),
        minLineLength=max(80, image.width // 4),
        maxLineGap=12,
    )
    line_count = 0 if horizontal is None else len(horizontal)
    if any(word in lowered for word in ("standesbeam", "geboren", "register", "formular")):
        layout = LayoutClass.FORM
        evidence.append("typische Formularbegriffe erkannt")
    elif line_count >= 6:
        layout = LayoutClass.TABLE
        evidence.append("mehrere Tabellenlinien erkannt")
    elif image.width / max(image.height, 1) > 1.22:
        layout = LayoutClass.SPREAD
    else:
        layout = LayoutClass.PLAIN
    return DocumentProfile(
        script=script,
        layout=layout,
        period=period,
        confidence=max(period.confidence, 0.45 if script != ScriptClass.UNKNOWN else 0.25),
        evidence=evidence,
        requires_review=bool(
            year_hint is not None
            and period.exact_year is not None
            and year_hint != period.exact_year
        ),
    )
