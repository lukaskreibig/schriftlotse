from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image
from skimage.filters import threshold_sauvola

from schriftlotse.domain import ImageDiagnostics, ImageVariant


@dataclass(slots=True)
class PreparedVariant:
    metadata: ImageVariant
    image: Image.Image


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
