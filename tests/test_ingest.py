from __future__ import annotations

from pathlib import Path

from PIL import Image

from schriftlotse.ingest import discover_documents, iter_document_pages


def test_folder_images_are_one_naturally_sorted_document(tmp_path: Path) -> None:
    folder = tmp_path / "Briefe"
    folder.mkdir()
    for name in ("seite10.png", "seite2.png", "seite1.png"):
        Image.new("RGB", (20, 20), "white").save(folder / name)
    documents = discover_documents([folder])
    assert len(documents) == 1
    assert [path.name for path in documents[0].source_paths] == [
        "seite1.png",
        "seite2.png",
        "seite10.png",
    ]
    pages = list(iter_document_pages(documents[0]))
    assert len(pages) == 3


def test_subfolders_become_separate_documents(tmp_path: Path) -> None:
    for folder_name in ("A", "B"):
        folder = tmp_path / folder_name
        folder.mkdir()
        Image.new("RGB", (10, 10), "white").save(folder / "1.jpg")
    documents = discover_documents([tmp_path])
    assert {document.title for document in documents} == {"A", "B"}


def test_loose_images_can_be_individual_documents(tmp_path: Path) -> None:
    for name in ("urkunde-a.jpg", "urkunde-b.jpg"):
        Image.new("RGB", (10, 10), "white").save(tmp_path / name)
    documents = discover_documents([tmp_path], group_images_by_folder=False)
    assert {document.title for document in documents} == {"urkunde-a", "urkunde-b"}
    assert all(document.page_count == 1 for document in documents)
