from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, ImageSequence

from schriftlotse.domain import SourceDocument

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".webp", ".bmp"}
TIFF_SUFFIXES = {".tif", ".tiff"}
PDF_SUFFIXES = {".pdf"}
GENERIC_TITLE = re.compile(
    r"^(?:temp(?:orary)?image[\w-]*|image[\w-]*|scan[\s_-]*\d*|document[\s_-]*\d*|img[\s_-]*\d*)$",
    flags=re.IGNORECASE,
)


def title_needs_review(title: str) -> bool:
    return not title.strip() or bool(GENERIC_TITLE.fullmatch(title.strip()))


def natural_key(path: Path) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)
    )


def _document_id(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        resolved = path.resolve()
        digest.update(str(resolved).encode("utf-8"))
        try:
            stat = resolved.stat()
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
        except OSError:
            pass
    return digest.hexdigest()[:20]


def discover_documents(
    sources: Sequence[Path], *, group_images_by_folder: bool = True
) -> list[SourceDocument]:
    documents: list[SourceDocument] = []
    explicit_images: list[Path] = []
    seen: set[Path] = set()

    def add_file(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        suffix = path.suffix.casefold()
        if suffix in PDF_SUFFIXES:
            documents.append(
                SourceDocument(
                    id=_document_id([path]),
                    title=path.stem,
                    source_paths=[path],
                    kind="pdf",
                    page_count=pdf_page_count(path),
                )
            )
        elif suffix in TIFF_SUFFIXES:
            documents.append(
                SourceDocument(
                    id=_document_id([path]),
                    title=path.stem,
                    source_paths=[path],
                    kind="tiff",
                    page_count=tiff_page_count(path),
                )
            )
        elif suffix in IMAGE_SUFFIXES:
            explicit_images.append(path)

    for raw_source in sources:
        source = raw_source.expanduser()
        if source.is_dir():
            for directory in [source, *sorted(p for p in source.rglob("*") if p.is_dir())]:
                files = sorted((p for p in directory.iterdir() if p.is_file()), key=natural_key)
                images = [p for p in files if p.suffix.casefold() in IMAGE_SUFFIXES]
                for standalone in files:
                    if standalone.suffix.casefold() in PDF_SUFFIXES | TIFF_SUFFIXES:
                        add_file(standalone)
                if images:
                    for image in images:
                        seen.add(image.resolve())
                    groups = [images] if group_images_by_folder else [[image] for image in images]
                    for group in groups:
                        documents.append(
                            SourceDocument(
                                id=_document_id(group),
                                title=(directory.name if group_images_by_folder else group[0].stem),
                                source_paths=group,
                                kind="images",
                                page_count=len(group),
                            )
                        )
        else:
            add_file(source)

    if explicit_images:
        explicit_images.sort(key=natural_key)
        groups = (
            [explicit_images] if group_images_by_folder else [[image] for image in explicit_images]
        )
        for group in groups:
            title = group[0].parent.name if len(group) > 1 else group[0].stem
            documents.append(
                SourceDocument(
                    id=_document_id(group),
                    title=title,
                    source_paths=group,
                    kind="images",
                    page_count=len(group),
                )
            )
    return sorted(documents, key=lambda item: (item.title.casefold(), item.id))


def import_preview(sources: Sequence[Path], *, group_images_by_folder: bool = False) -> dict:
    documents = discover_documents(sources, group_images_by_folder=group_images_by_folder)
    proposals: list[dict[str, Any]] = []
    for document in documents:
        source_path = document.source_paths[0]
        root = next(
            (
                candidate.resolve()
                for candidate in sources
                if candidate.is_dir()
                and (
                    source_path.resolve() == candidate.resolve()
                    or candidate.resolve() in source_path.resolve().parents
                )
            ),
            None,
        )
        relative_folder = ""
        collection_path: list[str] = []
        if root is not None:
            relative = source_path.parent.resolve().relative_to(root)
            relative_folder = relative.as_posix() if relative.parts else ""
            collection_path = [root.name, *relative.parts]
        proposals.append(
            {
                "id": document.id,
                "title": document.title,
                "kind": document.kind,
                "pages": document.page_count,
                "files": [path.name for path in document.source_paths],
                "relative_folder": relative_folder,
                "collection_path": collection_path,
                "title_needs_review": title_needs_review(document.title),
            }
        )
    grouped = discover_documents(sources, group_images_by_folder=True)
    suggestions: list[dict[str, object]] = []
    for document in grouped:
        sequences: dict[str, list[Path]] = {}
        for path in document.source_paths:
            match = re.match(
                r"^(.*?)(?:[-_ ]?(?:seite|page|scan))?[-_ ]*(\d+)$",
                path.stem.casefold(),
            )
            if match:
                signature = match.group(1).rstrip("-_ ") or document.title.casefold()
                sequences.setdefault(signature, []).append(path)
        for paths in sequences.values():
            if len(paths) < 2:
                continue
            ordered = sorted(paths, key=natural_key)
            suggestions.append(
                {
                    "folder": str(paths[0].parent.name),
                    "title": document.title,
                    "pages": len(ordered),
                    "files": [path.name for path in ordered],
                }
            )
    supported = IMAGE_SUFFIXES | TIFF_SUFFIXES | PDF_SUFFIXES

    def tree_node(directory: Path, root: Path) -> dict[str, object]:
        files = sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file() and path.suffix.casefold() in supported
            ),
            key=natural_key,
        )
        children = [
            tree_node(child, root)
            for child in sorted(
                (path for path in directory.iterdir() if path.is_dir()),
                key=lambda path: path.name.casefold(),
            )
        ]
        return {
            "name": directory.name,
            "relative_path": ("" if directory == root else directory.relative_to(root).as_posix()),
            "files": [path.name for path in files],
            "file_count": len(files),
            "document_count": sum(
                1
                for proposal in proposals
                if proposal["collection_path"]
                and proposal["collection_path"][-1] == directory.name
                and proposal["relative_folder"]
                == ("" if directory == root else directory.relative_to(root).as_posix())
            ),
            "children": children,
        }

    trees = [tree_node(source.resolve(), source.resolve()) for source in sources if source.is_dir()]
    return {
        "documents": proposals,
        "document_count": len(proposals),
        "page_count": sum(document.page_count for document in documents),
        "series_suggestions": suggestions,
        "group_images_by_folder": group_images_by_folder,
        "folder_trees": trees,
        "preserve_folder_structure": bool(trees),
        "title_review_count": sum(1 for proposal in proposals if proposal["title_needs_review"]),
    }


def pdf_page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        return len(document)
    finally:
        document.close()


def pdf_text_layer(path: Path, page_index: int) -> str:
    """Returns an existing PDF text layer as a candidate, never as trusted ground truth."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        if page_index < 0 or page_index >= len(document):
            return ""
        page = document[page_index]
        text_page = page.get_textpage()
        try:
            character_count = text_page.count_chars()
            return text_page.get_text_range(0, character_count).strip()
        finally:
            text_page.close()
            page.close()
    except Exception:
        return ""
    finally:
        document.close()


def tiff_page_count(path: Path) -> int:
    with Image.open(path) as image:
        return sum(1 for _ in ImageSequence.Iterator(image))


def _open_image(path: Path) -> Image.Image:
    if path.suffix.casefold() in {".heic", ".heif"}:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def iter_document_pages(
    document: SourceDocument, dpi: int = 300
) -> Iterator[tuple[int, Path, Image.Image]]:
    if document.kind == "images":
        for index, path in enumerate(document.source_paths):
            yield index, path, _open_image(path)
        return
    if document.kind == "tiff":
        path = document.source_paths[0]
        with Image.open(path) as image:
            for index, frame in enumerate(ImageSequence.Iterator(image)):
                yield index, path, ImageOps.exif_transpose(frame.copy()).convert("RGB")
        return
    if document.kind == "pdf":
        import pypdfium2 as pdfium

        path = document.source_paths[0]
        pdf = pdfium.PdfDocument(str(path))
        scale = dpi / 72.0
        try:
            for index in range(len(pdf)):
                page = pdf[index]
                bitmap = page.render(scale=scale, rotation=0)
                yield index, path, bitmap.to_pil().convert("RGB")
                page.close()
        finally:
            pdf.close()
        return
    raise ValueError(f"Unbekannter Dokumenttyp: {document.kind}")


def load_page(source_path: Path, page_index: int, kind: str | None = None) -> Image.Image:
    suffix = source_path.suffix.casefold()
    inferred = kind or (
        "pdf" if suffix == ".pdf" else "tiff" if suffix in TIFF_SUFFIXES else "images"
    )
    document = SourceDocument(
        id="preview",
        title=source_path.stem,
        source_paths=[source_path],
        kind=inferred,
        page_count=page_index + 1,
    )
    for index, _, image in iter_document_pages(document):
        if index == page_index:
            return image
    raise IndexError(page_index)
