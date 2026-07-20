from __future__ import annotations

import json
import zipfile
from xml.etree import ElementTree as ET

from PIL import Image

from schriftlotse.database import Database
from schriftlotse.exports import export_document, reading_text
from schriftlotse.pagexml import NS
from tests.test_search import stored_result


def test_all_document_exports_are_created(tmp_path) -> None:
    result = stored_result()
    scan = tmp_path / "scan.png"
    Image.new("RGB", (1000, 1200), "white").save(scan)
    result.document.source_paths = [scan]
    result.pages[0].source_path = scan
    result.pages[0].prepared_path = tmp_path / "missing-prepared-page.png"
    files = export_document(result, tmp_path / "result")
    names = {path.name for path in files}
    assert {
        "transkription_original.txt",
        "lesefassung.txt",
        "result.json",
        "schriftlotse.docx",
        "schriftlotse.pdf",
        "escriptorium-pagexml.zip",
        "schriftlotse-ergebnis.zip",
    } <= names
    payload = json.loads((tmp_path / "result" / "result.json").read_text(encoding="utf-8"))
    assert payload["document"]["title"] == "Kirchenbuch 1872"
    with zipfile.ZipFile(tmp_path / "result" / "schriftlotse-ergebnis.zip") as archive:
        assert "pagexml/seite_0001.xml" in archive.namelist()
        assert "alto/seite_0001.xml" in archive.namelist()
        assert "images/seite_0001.png" in archive.namelist()


def test_reading_text_only_dehyphenates_safe_breaks() -> None:
    result = stored_result()
    result.pages[0].lines[0].text = "Auswan-"
    result.pages[0].lines[1].text = "derung und Alt-Schrift"
    text = reading_text(result)
    assert "Auswanderung" in text
    assert "Alt-Schrift" in text


def test_pagexml_correction_roundtrip(app_paths, tmp_path) -> None:
    result = stored_result()
    scan = tmp_path / "scan.png"
    Image.new("RGB", (1000, 1200), "white").save(scan)
    result.document.source_paths = [scan]
    result.pages[0].source_path = scan
    export_document(result, tmp_path / "result")
    xml_path = tmp_path / "result" / "pagexml" / "seite_0001.xml"
    tree = ET.parse(xml_path)
    line = tree.find(f".//{{{NS}}}TextLine[@id='line-1']")
    assert line is not None
    unicode_node = line.find(f"./{{{NS}}}TextEquiv/{{{NS}}}Unicode")
    assert unicode_node is not None
    unicode_node.text = "Johann Schmidt wanderte nach Brasilien aus."
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)

    database = Database(app_paths.database)
    database.create_job("job")
    database.save_document("job", result)
    assert database.import_pagexml_corrections("doc-1", [xml_path]) == 1
    assert "Brasilien" in database.line_context("line-1")["text"]
