from __future__ import annotations

from pathlib import Path

from schriftlotse.database import Database
from schriftlotse.domain import (
    AlternativeReading,
    DocumentResult,
    LineResult,
    PageResult,
    ScriptHint,
    SearchMode,
    SearchQuery,
    SourceDocument,
)
from schriftlotse.search import ArchiveSearch, koelner_phonetik, normalize_text


def stored_result() -> DocumentResult:
    document = SourceDocument(
        id="doc-1",
        title="Kirchenbuch 1872",
        source_paths=[Path("scan.png")],
        kind="images",
        page_count=1,
    )
    page = PageResult(
        page_index=0,
        source_path=Path("scan.png"),
        width=1000,
        height=1200,
        lines=[
            LineResult(
                id="line-1",
                text="Johann Schmidt wanderte nach Amerika aus.",
                bbox=(10, 20, 900, 60),
                confidence=0.82,
                model="test",
                variant="original",
            ),
            LineResult(
                id="line-2",
                text="Anna Müller blieb in Württemberg.",
                bbox=(10, 80, 900, 120),
                confidence=0.91,
                model="test",
                variant="original",
            ),
            LineResult(
                id="line-3",
                text="Wilhelm Kreibig zog nach Sorau.",
                bbox=(10, 140, 900, 180),
                confidence=0.78,
                model="test",
                variant="original",
            ),
            LineResult(
                id="line-4",
                text="Ein unverbundener OCR-Rest.",
                bbox=(10, 200, 900, 240),
                confidence=0.45,
                model="test",
                variant="original",
                alternatives=[AlternativeReading(text="g", model="noise", confidence=0.2)],
            ),
        ],
        mean_confidence=0.86,
        expected_cer=0.08,
        selected_variant="original",
        selected_model="test",
    )
    return DocumentResult(
        document=document, year=1872, script_hint=ScriptHint.HANDWRITING, pages=[page]
    )


def test_normalization_and_phonetics() -> None:
    assert normalize_text("Schröder, Weiß") == "schroeder weiss"
    assert koelner_phonetik("Schmidt") == koelner_phonetik("Schmitt")


def test_exact_fuzzy_and_correction_search(app_paths) -> None:
    database = Database(app_paths.database)
    database.create_job("job")
    database.save_document("job", stored_result())
    engine = ArchiveSearch(database)

    exact = engine.search(SearchQuery(text="Amerika", mode=SearchMode.EXACT))
    assert exact and exact[0].line_id == "line-1"

    names = engine.search(SearchQuery(text="Schmitt", mode=SearchMode.NAME, fuzziness=0.65))
    assert names and names[0].line_id == "line-1"
    assert names[0].reason == "phonetische Namensvariante"

    full_name = engine.search(
        SearchQuery(text="Johan Schmitt", mode=SearchMode.NAME, fuzziness=0.65)
    )
    assert full_name and full_name[0].line_id == "line-1"
    assert full_name[0].reason == "phonetische Namensvariante"

    database.update_line("line-1", "Johann Schmid reiste nach Amerika.")
    corrected = engine.search(SearchQuery(text="Schmid", mode=SearchMode.EXACT))
    assert corrected[0].text.startswith("Johann Schmid")
    old_reading = engine.search(SearchQuery(text="wanderte", mode=SearchMode.EXACT))
    assert old_reading and old_reading[0].text.startswith("Johann Schmid")
    assert old_reading[0].matched_form.startswith("Johann Schmidt wanderte")

    typo_name = engine.search(SearchQuery(text="Kreipig", mode=SearchMode.NAME, fuzziness=0.72))
    assert typo_name and typo_name[0].line_id == "line-3"
    assert all(hit.line_id != "line-4" for hit in typo_name)


def test_semantic_mode_expands_transparent_archival_concepts(app_paths) -> None:
    database = Database(app_paths.database)
    database.create_job("job")
    database.save_document("job", stored_result())
    database.update_line("line-2", "Anna Müller war seine Ehefrau.")

    hits = ArchiveSearch(database).search(
        SearchQuery(text="Eheschließung", mode=SearchMode.SEMANTIC)
    )

    assert hits and hits[0].line_id == "line-2"
    assert hits[0].reason == "verwandter Archivbegriff"

    database.update_line("line-3", "Wilhelm Kreibig ist verstorben.")
    death = ArchiveSearch(database).search(
        SearchQuery(text="Sterbeurkunde", mode=SearchMode.SMART)
    )
    assert death and death[0].line_id == "line-3"
    assert death[0].reason == "verwandter Archivbegriff"


def test_reprocessing_document_removes_stale_fulltext_rows(app_paths) -> None:
    database = Database(app_paths.database)
    database.create_job("first")
    first = stored_result()
    first.pages[0].lines[0].text = "Vermählung des Johann Schmidt"
    database.save_document("first", first)

    database.create_job("second")
    second = stored_result()
    second.pages[0].lines[0].text = "Bereifung und Zubehör"
    database.save_document("second", second)
    engine = ArchiveSearch(database)

    assert not engine.search(SearchQuery(text="Vermählung", mode=SearchMode.EXACT))
    replacement = engine.search(SearchQuery(text="Bereifung", mode=SearchMode.EXACT))
    assert replacement and replacement[0].text == "Bereifung und Zubehör"
