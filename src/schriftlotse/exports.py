from __future__ import annotations

import html
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from schriftlotse.domain import DocumentResult, PageResult
from schriftlotse.pagexml import NS


def original_text(result: DocumentResult) -> str:
    pages = []
    for page in result.pages:
        pages.append("\n".join(line.text for line in page.lines))
    return "\n\n--- Seite ---\n\n".join(pages).strip() + "\n"


def reading_text(result: DocumentResult) -> str:
    text = original_text(result)
    text = re.sub(r"(?<=\w)-\n(?=[a-zäöüß])", "", text)
    text = re.sub(r"(?<!\n)\n(?!\n|--- Seite ---)", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip() + "\n"


def _page_xml(result: DocumentResult, page: PageResult, destination: Path) -> None:
    root = ET.Element(f"{{{NS}}}PcGts")
    metadata = ET.SubElement(root, f"{{{NS}}}Metadata")
    ET.SubElement(metadata, f"{{{NS}}}Creator").text = "SchriftLotse 0.1.0"
    page_node = ET.SubElement(
        root,
        f"{{{NS}}}Page",
        imageFilename=page.source_path.name,
        imageWidth=str(page.width),
        imageHeight=str(page.height),
    )
    region = ET.SubElement(page_node, f"{{{NS}}}TextRegion", id="region_1")
    ET.SubElement(
        region,
        f"{{{NS}}}Coords",
        points=f"0,0 {page.width},0 {page.width},{page.height} 0,{page.height}",
    )
    for line in page.lines:
        x1, y1, x2, y2 = line.bbox
        line_node = ET.SubElement(
            region,
            f"{{{NS}}}TextLine",
            id=line.id,
            conf=f"{line.confidence:.5f}",
            custom=f"model:{line.model}; variant:{line.variant}",
        )
        ET.SubElement(
            line_node,
            f"{{{NS}}}Coords",
            points=f"{x1},{y1} {x2},{y1} {x2},{y2} {x1},{y2}",
        )
        text_equiv = ET.SubElement(line_node, f"{{{NS}}}TextEquiv", conf=f"{line.confidence:.5f}")
        ET.SubElement(text_equiv, f"{{{NS}}}Unicode").text = line.text
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)


def _docx(result: DocumentResult, destination: Path) -> None:
    document = Document()
    styles = document.styles
    styles["Normal"].font.name = "Aptos"
    styles["Normal"].font.size = Pt(10.5)
    title = document.add_heading("SchriftLotse", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle = document.add_paragraph(result.document.title)
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    document.add_paragraph(f"Jahr: {result.year or 'nicht angegeben'}")
    document.add_paragraph(f"Schrifthinweis: {result.script_hint.value}")
    document.add_heading("Originalgetreue Transkription", level=1)
    for page in result.pages:
        document.add_heading(f"Seite {page.page_index + 1}", level=2)
        for line in page.lines:
            paragraph = document.add_paragraph(line.text)
            if line.confidence < 0.9:
                paragraph.add_run(f"  [Sicherheit {line.confidence:.0%}]").italic = True
    document.add_page_break()
    document.add_heading("Lesefassung", level=1)
    for paragraph_text in reading_text(result).split("\n\n"):
        document.add_paragraph(paragraph_text)
    document.add_heading("Technischer Anhang", level=1)
    for page in result.pages:
        document.add_paragraph(
            f"Seite {page.page_index + 1}: {page.selected_model}, "
            f"Variante {page.selected_variant}, "
            f"mittlere Sicherheit {page.mean_confidence:.1%}, erwartete CER {page.expected_cer:.1%}"
        )
    document.save(str(destination))


def _pdf(result: DocumentResult, destination: Path) -> None:
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="Cover", parent=styles["Title"], alignment=TA_CENTER, fontSize=26, leading=32
        )
    )
    styles.add(
        ParagraphStyle(
            name="Muted",
            parent=styles["BodyText"],
            textColor=colors.HexColor("#59636f"),
            fontSize=8,
        )
    )
    document = SimpleDocTemplate(
        str(destination),
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    story = [
        Spacer(1, 45 * mm),
        Paragraph("SchriftLotse", styles["Cover"]),
        Spacer(1, 5 * mm),
        Paragraph(html.escape(result.document.title), styles["Title"]),
        Spacer(1, 8 * mm),
        Paragraph(
            f"Jahr: {result.year or 'nicht angegeben'} · Schrift: {result.script_hint.value}",
            styles["Normal"],
        ),
        PageBreak(),
        Paragraph("Originalgetreue Transkription", styles["Heading1"]),
    ]
    for page in result.pages:
        story.append(Paragraph(f"Seite {page.page_index + 1}", styles["Heading2"]))
        for line in page.lines:
            story.append(Paragraph(html.escape(line.text) or "&nbsp;", styles["BodyText"]))
        story.append(
            Paragraph(
                f"{html.escape(page.selected_model)} · {html.escape(page.selected_variant)} · "
                f"Sicherheit {page.mean_confidence:.0%}",
                styles["Muted"],
            )
        )
        story.append(Spacer(1, 4 * mm))
    story.extend([PageBreak(), Paragraph("Lesefassung", styles["Heading1"])])
    for paragraph_text in reading_text(result).split("\n\n"):
        story.append(
            Paragraph(html.escape(paragraph_text).replace("\n", "<br/>"), styles["BodyText"])
        )
        story.append(Spacer(1, 3 * mm))
    table_data = [["Seite", "Modell", "Variante", "Sicherheit", "erw. CER"]]
    for page in result.pages:
        table_data.append(
            [
                str(page.page_index + 1),
                page.selected_model,
                page.selected_variant,
                f"{page.mean_confidence:.1%}",
                f"{page.expected_cer:.1%}",
            ]
        )
    story.extend(
        [
            PageBreak(),
            Paragraph("Technischer Anhang", styles["Heading1"]),
            Table(table_data, repeatRows=1),
        ]
    )
    table = story[-1]
    if isinstance(table, Table):
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4d5c")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c8d1d5")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
    document.build(story)


def export_document(result: DocumentResult, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    result.output_dir = output_dir
    original = output_dir / "transkription_original.txt"
    readable = output_dir / "lesefassung.txt"
    json_path = output_dir / "result.json"
    docx_path = output_dir / "schriftlotse.docx"
    pdf_path = output_dir / "schriftlotse.pdf"
    original.write_text(original_text(result), encoding="utf-8")
    readable.write_text(reading_text(result), encoding="utf-8")
    json_path.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _docx(result, docx_path)
    _pdf(result, pdf_path)
    pagexml_dir = output_dir / "pagexml"
    pagexml_dir.mkdir(exist_ok=True)
    page_files: list[Path] = []
    for page in result.pages:
        page_path = pagexml_dir / f"seite_{page.page_index + 1:04d}.xml"
        _page_xml(result, page, page_path)
        page_files.append(page_path)
    archive = output_dir / "schriftlotse-ergebnis.zip"
    files = [original, readable, json_path, docx_path, pdf_path, *page_files]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for path in files:
            zip_file.write(path, path.relative_to(output_dir))
    return [*files, archive]


def export_search_results(rows: list[dict[str, object]], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    headers = ["Dokument", "Jahr", "Seite", "Text", "Trefferart", "Bewertung"]
    table_data = [headers] + [
        [
            str(row.get("document", "")),
            str(row.get("year", "")),
            str(row.get("page", "")),
            str(row.get("text", "")),
            str(row.get("reason", "")),
            str(row.get("score", "")),
        ]
        for row in rows
    ]
    pdf = SimpleDocTemplate(str(destination), pagesize=A4, rightMargin=10 * mm, leftMargin=10 * mm)
    table = Table(
        table_data, repeatRows=1, colWidths=[35 * mm, 15 * mm, 13 * mm, 75 * mm, 35 * mm, 18 * mm]
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4d5c")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    pdf.build([Paragraph("SchriftLotse – Suchergebnisse", getSampleStyleSheet()["Title"]), table])
    return destination
