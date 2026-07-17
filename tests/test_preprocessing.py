from __future__ import annotations

from PIL import Image, ImageDraw

from schriftlotse.preprocessing import (
    detect_text_lines,
    generate_variants,
    select_preflight_variants,
)


def sample_image() -> Image.Image:
    image = Image.new("RGB", (800, 500), "#e7d9af")
    draw = ImageDraw.Draw(image)
    draw.text((70, 80), "Johann Schmidt 1872", fill="#3b2d22")
    draw.text((70, 160), "Geboren zu Stuttgart", fill="#423126")
    draw.rectangle((0, 0, 799, 10), fill="black")
    return image


def test_adaptive_variants_are_distinct() -> None:
    variants = generate_variants(sample_image())
    assert [variant.metadata.name for variant in variants] == [
        "original",
        "normalisiert",
        "kontrast",
        "binarisiert",
    ]
    assert all(variant.image.size == (800, 500) for variant in variants)
    assert len(select_preflight_variants(variants)) == 2
    assert len(variants[-1].image.getcolors(maxcolors=10)) <= 2


def test_line_detection_returns_ordered_boxes() -> None:
    boxes = detect_text_lines(sample_image())
    assert boxes == sorted(boxes, key=lambda box: (box[1], box[0]))
