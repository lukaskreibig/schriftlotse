from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Sequence
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

from schriftlotse.domain import SourceDocument

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".webp", ".bmp"}
TIFF_SUFFIXES = {".tif", ".tiff"}
PDF_SUFFIXES = {".pdf"}


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


def discover_documents(sources: Sequence[Path]) -> list[SourceDocument]:
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
                    documents.append(
                        SourceDocument(
                            id=_document_id(images),
                            title=directory.name,
                            source_paths=images,
                            kind="images",
                            page_count=len(images),
                        )
                    )
        else:
            add_file(source)

    if explicit_images:
        explicit_images.sort(key=natural_key)
        title = (
            explicit_images[0].parent.name if len(explicit_images) > 1 else explicit_images[0].stem
        )
        documents.append(
            SourceDocument(
                id=_document_id(explicit_images),
                title=title,
                source_paths=explicit_images,
                kind="images",
                page_count=len(explicit_images),
            )
        )
    return sorted(documents, key=lambda item: (item.title.casefold(), item.id))


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
