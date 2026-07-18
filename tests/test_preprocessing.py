from __future__ import annotations

from PIL import Image, ImageDraw

from schriftlotse.domain import ScriptHint
from schriftlotse.preprocessing import (
    detect_text_lines,
    generate_variants,
    profile_page,
    select_preflight_variants,
    split_logical_pages,
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


def test_period_hint_never_overwrites_visible_year() -> None:
    profile = profile_page(
        sample_image(),
        filename="urkunde-1849.jpg",
        quick_text="Sorau, am 28. April 1849",
        year_hint=1919,
        script_hint=ScriptHint.AUTO,
    )
    assert profile.period.exact_year == 1849
    assert profile.requires_review is True


def test_compact_date_in_filename_routes_by_its_year() -> None:
    profile = profile_page(
        sample_image(),
        filename="SNP27443449-19230726-0-1-0-0.pdf",
        script_hint=ScriptHint.AUTO,
    )
    assert profile.period.exact_year == 1923
    assert "Jahresangabe 1923 im Dateinamen" in profile.period.evidence


def test_unknown_year_gets_only_a_coarse_model_supported_epoch() -> None:
    profile = profile_page(
        sample_image(),
        filename="undatiert.jpg",
        script_hint=ScriptHint.AUTO,
        selected_model="trocr-kurrent-early",
    )
    assert profile.period.exact_year is None
    assert (profile.period.year_from, profile.period.year_to) == (1500, 1799)
    assert profile.period.confidence < 0.5


def test_wide_text_strip_is_not_mistaken_for_book_spread() -> None:
    image = Image.new("RGB", (1800, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 180), "Linke Zeitungsspalte", fill="black")
    draw.text((1000, 180), "Rechte Zeitungsspalte", fill="black")
    assert len(split_logical_pages(image, "newspaper")) == 1


def test_split_book_pages_are_individually_cropped_from_film_border() -> None:
    image = Image.new("RGB", (1800, 1200), "black")
    draw = ImageDraw.Draw(image)
    draw.rectangle((140, 90, 850, 1090), fill="white")
    draw.rectangle((950, 90, 1660, 1090), fill="white")
    for y in range(220, 920, 90):
        draw.line((240, y, 760, y), fill="black", width=5)
        draw.line((1040, y, 1560, y), fill="black", width=5)

    pages = split_logical_pages(image, "book")

    assert len(pages) == 2
    assert all(page.image.width < 800 for page in pages)
    assert all(page.image.height < 1100 for page in pages)
    assert all("Film-/Aufnahmerand der Einzelseite ausgeblendet" in page.warnings for page in pages)
