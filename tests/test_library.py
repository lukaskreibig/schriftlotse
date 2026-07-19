from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from schriftlotse.app import ApplicationState, create_app
from schriftlotse.config import AppPaths, Settings
from schriftlotse.database import Database
from schriftlotse.domain import (
    DocumentResult,
    ImageDiagnostics,
    LineResult,
    PageResult,
    ScriptHint,
    SourceDocument,
)
from schriftlotse.library import LibraryManager, sha256_file


def _result(scan: Path, document_id: str = "document-1") -> DocumentResult:
    return DocumentResult(
        document=SourceDocument(
            id=document_id,
            title="Sterbeurkunde Hermann",
            source_paths=[scan],
            kind="image",
            page_count=1,
        ),
        year=1891,
        script_hint=ScriptHint.HANDWRITING,
        pages=[
            PageResult(
                page_index=0,
                source_path=scan,
                width=320,
                height=240,
                lines=[
                    LineResult(
                        id=f"{document_id}-0000-0000",
                        text="Hermann Müller",
                        bbox=(10, 20, 200, 50),
                        confidence=0.82,
                        model="testmodell",
                        variant="kontrast",
                    )
                ],
                mean_confidence=0.82,
                expected_cer=0.08,
                selected_variant="kontrast",
                selected_model="testmodell",
                image_diagnostics=ImageDiagnostics(
                    brightness=0.7,
                    contrast=0.5,
                    sharpness=0.4,
                    skew_degrees=0.3,
                    clipped_dark=0.01,
                    clipped_light=0.02,
                ),
            )
        ],
    )


def test_managed_library_copies_original_and_verifies_fixity(app_paths) -> None:
    scan = app_paths.cache / "Hermann.jpg"
    scan.parent.mkdir(parents=True)
    Image.new("RGB", (320, 240), "white").save(scan)
    database = Database(app_paths.database)
    database.create_job("job")
    database.save_document("job", _result(scan))
    library = LibraryManager(app_paths, Settings())

    migrated = library.adopt_existing_document(database, "document-1")
    detail = database.document_detail("document-1")

    assert migrated["files"] == 1
    assert detail is not None
    assert detail["library_managed"] == 1
    assert Path(detail["source_paths"][0]).is_file()
    assert detail["files"][0]["sha256"]
    assert library.verify_document(database, "document-1")["problems"] == []
    managed_path = Path(detail["source_paths"][0])
    digest = detail["files"][0]["sha256"]
    managed_path.write_bytes(b"accidentally changed")
    assert sha256_file(library.objects / digest[:2] / digest) == digest
    assert library.verify_document(database, "document-1")["problems"][0]["status"] == "verändert"
    assert library.repair_document(database, "document-1")["repaired"] == ["Hermann.jpg"]
    assert library.verify_document(database, "document-1")["problems"] == []


def test_reprocessing_preserves_archive_metadata_and_collections(app_paths) -> None:
    scan = app_paths.cache / "scan.png"
    scan.parent.mkdir(parents=True)
    Image.new("RGB", (320, 240), "white").save(scan)
    database = Database(app_paths.database)
    database.create_job("job")
    result = _result(scan)
    database.save_document("job", result)
    database.create_collection("family", "Familienarchiv")
    database.update_document_metadata("document-1", {"archive": "Stadtarchiv", "shelfmark": "A 42"})
    database.set_document_collections("document-1", ["family"])
    database.set_document_tags("document-1", ["Genealogie", "Standesamt"])

    database.save_document("job", result)
    detail = database.document_detail("document-1")

    assert detail is not None
    assert detail["archive"] == "Stadtarchiv"
    assert detail["shelfmark"] == "A 42"
    assert detail["collections"][0]["name"] == "Familienarchiv"
    assert detail["tags"] == ["Genealogie", "Standesamt"]
    assert database.search_document_metadata("Stadtarchiv")[0]["id"] == "document-1"
    assert database.search_document_metadata("Genealogie")[0]["id"] == "document-1"
    rebuilt = database.load_document_result("document-1")
    assert rebuilt.pages[0].lines[0].text == "Hermann Müller"
    assert rebuilt.pages[0].image_diagnostics is not None


def test_legacy_image_series_can_be_split_without_losing_ocr(app_paths) -> None:
    first = app_paths.cache / "seite-eins.png"
    second = app_paths.cache / "seite-zwei.png"
    first.parent.mkdir(parents=True)
    Image.new("RGB", (320, 240), "white").save(first)
    Image.new("RGB", (320, 240), "white").save(second)
    result = _result(first, "legacy-series")
    result.document.title = "testdocs"
    result.document.source_paths = [first, second]
    result.document.page_count = 2
    second_page = result.pages[0].model_copy(deep=True)
    second_page.page_index = 1
    second_page.source_path = second
    second_page.lines[0].id = "legacy-series-0001-0000"
    second_page.lines[0].text = "Zweite Seite"
    result.pages.append(second_page)
    database = Database(app_paths.database)
    database.create_job("job")
    database.save_document("job", result)
    database.set_document_tags("legacy-series", ["Altbestand"])

    split = database.split_document_into_pages("legacy-series")

    assert len(split) == 2
    assert database.document("legacy-series") is None
    assert {row["title"] for row in database.list_documents()} == {
        "seite-eins",
        "seite-zwei",
    }
    assert {row["text"] for row in database.rows("SELECT text FROM lines")} == {
        "Hermann Müller",
        "Zweite Seite",
    }
    assert all(
        database.document_detail(document_id)["tags"] == ["Altbestand"] for document_id in split
    )


def test_schema_upgrade_creates_a_recoverable_database_backup(app_paths) -> None:
    app_paths.database.parent.mkdir(parents=True)
    with sqlite3.connect(app_paths.database) as connection:
        connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT INTO meta VALUES('schema_version','5')")

    Database(app_paths.database)

    backup = app_paths.database.with_name("test.schema-v5-backup.sqlite3")
    assert backup.is_file()
    with sqlite3.connect(backup) as connection:
        assert (
            connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            == "5"
        )


def test_trash_is_recoverable_and_permanent_delete_removes_managed_assets(app_paths) -> None:
    scan = app_paths.cache / "delete-me.png"
    scan.parent.mkdir(parents=True)
    Image.new("RGB", (320, 240), "white").save(scan)
    database = Database(app_paths.database)
    database.create_job("job")
    database.save_document("job", _result(scan, "delete-me"))
    library = LibraryManager(app_paths, Settings())
    library.adopt_existing_document(database, "delete-me")
    document_root = library.document_root("delete-me")

    database.trash_document("delete-me")
    assert database.document("delete-me")["deleted_at"] is not None
    database.trash_document("delete-me", restore=True)
    assert database.document("delete-me")["deleted_at"] is None
    database.trash_document("delete-me")
    library.purge_document(database, "delete-me")

    assert database.document("delete-me") is None
    assert not document_root.exists()


def test_archive_api_workflow_from_library_to_metadata_search(app_paths, monkeypatch) -> None:
    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    scan = app_paths.cache / "api-document.png"
    scan.parent.mkdir(parents=True)
    Image.new("RGB", (320, 240), "white").save(scan)
    state = ApplicationState()
    state.database.create_job("job")
    state.database.save_document("job", _result(scan, "api-document"))
    state.library.adopt_existing_document(state.database, "api-document")
    client = TestClient(create_app(state))

    collection = client.post(
        "/api/collections",
        json={"name": "Familienforschung", "description": "Private Arbeitsgruppe"},
    )
    assert collection.status_code == 201
    collection_id = collection.json()["id"]
    updated = client.patch(
        "/api/documents/api-document",
        json={
            "archive": "Stadtarchiv München",
            "fonds": "Standesamt",
            "shelfmark": "A 42",
            "collection_ids": [collection_id],
            "tags": ["Genealogie"],
        },
    )

    assert updated.status_code == 200
    assert updated.json()["archive"] == "Stadtarchiv München"
    assert "source_paths" not in updated.json()
    assert "managed_path" not in updated.json()["files"][0]
    documents = client.get("/api/documents").json()
    assert documents[0]["managed"] is True
    assert documents[0]["collection_names"] == ["Familienforschung"]
    search = client.post(
        "/api/search",
        json={"text": "Stadtarchiv", "mode": "intelligent", "fuzziness": 0.72},
    )
    assert search.status_code == 200
    assert search.json()[0]["document_id"] == "api-document"
    assert "Archivangaben" in search.json()[0]["reason"]
    integrity = client.post("/api/library/integrity")
    assert integrity.status_code == 200
    assert integrity.json()["problems"] == []
    exported = client.post("/api/documents/api-document/export")
    assert exported.status_code == 200
    assert any(item["name"] == "schriftlotse.pdf" for item in exported.json()["downloads"])


def test_live_preview_is_a_small_independent_page_asset(app_paths) -> None:
    library = LibraryManager(app_paths, Settings())
    source = Image.new("RGB", (4000, 3000), "white")

    preview = library.make_page_preview("live-document", 3, source)

    assert preview.is_file()
    with Image.open(preview) as image:
        assert max(image.size) == 900
    assert preview.stat().st_size < 500_000


def test_nested_collections_and_full_transcript_api(app_paths, monkeypatch) -> None:
    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    scan = app_paths.cache / "Hermann.jpg"
    scan.parent.mkdir(parents=True)
    Image.new("RGB", (320, 240), "white").save(scan)
    state = ApplicationState()
    state.database.create_job("job")
    state.database.save_document("job", _result(scan, "nested-document"))
    client = TestClient(create_app(state))

    root = client.post("/api/collections", json={"name": "Genealogische Funde"}).json()
    child = client.post(
        "/api/collections",
        json={"name": "Familie Müller", "parent_id": root["id"]},
    ).json()
    updated = client.patch("/api/documents/nested-document", json={"collection_ids": [child["id"]]})
    transcript = client.get("/api/documents/nested-document/transcript")
    collections = client.get("/api/collections").json()
    documents = client.get("/api/documents").json()

    assert updated.status_code == 200
    assert transcript.status_code == 200
    assert transcript.json()["line_count"] == 1
    assert transcript.json()["pages"][0]["lines"][0]["text"] == "Hermann Müller"
    assert next(item for item in collections if item["id"] == child["id"])["path"] == (
        "Genealogische Funde / Familie Müller"
    )
    assert documents[0]["collection_ids"] == [child["id"]]
    assert documents[0]["collection_paths"] == ["Genealogische Funde / Familie Müller"]


def test_folder_preview_preserves_tree_and_flags_temporary_titles(app_paths, monkeypatch) -> None:
    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    root = app_paths.cache / "Genealogische Funde"
    family = root / "Müller" / "Sterbeurkunden"
    family.mkdir(parents=True)
    Image.new("RGB", (40, 30), "white").save(family / "tempImageWMXyJH.jpg")
    state = ApplicationState()
    source = state.register_source(root)
    client = TestClient(create_app(state))

    response = client.post(
        "/api/import-preview",
        json={"sources": [source["id"]], "group_images_by_folder": False},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["title_review_count"] == 1
    assert data["documents"][0]["collection_path"] == [
        "Genealogische Funde",
        "Müller",
        "Sterbeurkunden",
    ]
    assert data["folder_trees"][0]["children"][0]["name"] == "Müller"


def test_linked_folder_diff_is_non_destructive(app_paths, monkeypatch) -> None:
    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    root = app_paths.cache / "Archiv"
    root.mkdir(parents=True)
    scan = root / "fund.jpg"
    Image.new("RGB", (30, 30), "white").save(scan)
    state = ApplicationState()
    state.database.create_job("source-job")
    state.database.save_document("source-job", _result(scan, "kept-document"))
    collection_id = state.database.ensure_collection("Archiv", kind="quellordner")
    state.database.upsert_source_folder("source-1", root, "Archiv", collection_id)
    stat = scan.stat()
    state.database.upsert_source_entry(
        "source-1",
        "fund.jpg",
        sha256_file(scan),
        stat.st_size,
        stat.st_mtime_ns,
        document_id="kept-document",
        collection_id=collection_id,
    )
    scan.unlink()
    client = TestClient(create_app(state))

    response = client.get("/api/source-folders/source-1/diff")

    assert response.status_code == 200
    assert response.json()["counts"]["missing"] == 1
    assert response.json()["changes"][0]["document_id"] == "kept-document"
    assert state.database.source_entries("source-1")[0]["state"] == "vorhanden"


def test_linked_folder_move_reuses_document_without_new_ocr(app_paths, monkeypatch) -> None:
    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    root = app_paths.cache / "Archiv"
    target = root / "Familie Müller"
    target.mkdir(parents=True)
    scan = root / "fund.jpg"
    Image.new("RGB", (30, 30), "white").save(scan)
    state = ApplicationState()
    state.database.create_job("move-job")
    state.database.save_document("move-job", _result(scan, "moved-document"))
    collection_id = state.database.ensure_collection("Archiv", kind="quellordner")
    state.database.set_document_collections("moved-document", [collection_id])
    state.database.upsert_source_folder("source-1", root, "Archiv", collection_id)
    stat = scan.stat()
    state.database.upsert_source_entry(
        "source-1",
        "fund.jpg",
        sha256_file(scan),
        stat.st_size,
        stat.st_mtime_ns,
        document_id="moved-document",
        collection_id=collection_id,
    )
    moved = target / scan.name
    scan.rename(moved)
    client = TestClient(create_app(state))

    diff = client.get("/api/source-folders/source-1/diff").json()
    response = client.post(
        "/api/source-folders/source-1/prepare-sync",
        json={"relative_paths": ["Familie Müller/fund.jpg"]},
    )

    assert diff["counts"]["moved"] == 1
    assert response.status_code == 200
    assert response.json()["moved"] == 1
    assert response.json()["sources"] == []
    entry = state.database.source_entries("source-1")[0]
    assert entry["relative_path"] == "Familie Müller/fund.jpg"
    detail = state.database.document_detail("moved-document")
    assert detail is not None
    assert detail["collections"][0]["name"] == "Familie Müller"
