from __future__ import annotations

import json
import zipfile

from schriftlotse.exports import export_document, reading_text
from tests.test_search import stored_result


def test_all_document_exports_are_created(tmp_path) -> None:
    result = stored_result()
    files = export_document(result, tmp_path / "result")
    names = {path.name for path in files}
    assert {
        "transkription_original.txt",
        "lesefassung.txt",
        "result.json",
        "schriftlotse.docx",
        "schriftlotse.pdf",
        "schriftlotse-ergebnis.zip",
    } <= names
    payload = json.loads((tmp_path / "result" / "result.json").read_text(encoding="utf-8"))
    assert payload["document"]["title"] == "Kirchenbuch 1872"
    with zipfile.ZipFile(tmp_path / "result" / "schriftlotse-ergebnis.zip") as archive:
        assert "pagexml/seite_0001.xml" in archive.namelist()


def test_reading_text_only_dehyphenates_safe_breaks() -> None:
    result = stored_result()
    result.pages[0].lines[0].text = "Auswan-"
    result.pages[0].lines[1].text = "derung und Alt-Schrift"
    text = reading_text(result)
    assert "Auswanderung" in text
    assert "Alt-Schrift" in text
