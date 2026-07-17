from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from schriftlotse.domain import DocumentResult, EntityMention, JobStatus, LineResult

SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    job_id TEXT,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    year INTEGER,
                    script_hint TEXT NOT NULL,
                    source_paths TEXT NOT NULL,
                    output_dir TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS pages (
                    document_id TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    expected_cer REAL NOT NULL,
                    variant TEXT NOT NULL,
                    model TEXT NOT NULL,
                    warnings TEXT NOT NULL,
                    PRIMARY KEY(document_id, page_index),
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS lines (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    line_order INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    bbox TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    model TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    manually_corrected INTEGER NOT NULL DEFAULT 0,
                    alternatives TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY(document_id, page_index)
                        REFERENCES pages(document_id, page_index) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS edits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    line_id TEXT NOT NULL,
                    old_text TEXT NOT NULL,
                    new_text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(line_id) REFERENCES lines(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    line_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    label TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    gnd_id TEXT,
                    UNIQUE(line_id, text, label),
                    FOREIGN KEY(line_id) REFERENCES lines(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS name_aliases (
                    canonical TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    PRIMARY KEY(canonical, alias)
                );
                CREATE TABLE IF NOT EXISTS embeddings (
                    line_id TEXT PRIMARY KEY,
                    model_revision TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    FOREIGN KEY(line_id) REFERENCES lines(id) ON DELETE CASCADE
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS lines_fts USING fts5(
                    line_id UNINDEXED,
                    text,
                    normalized,
                    tokenize='unicode61 remove_diacritics 0'
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS lines_trigram USING fts5(
                    line_id UNINDEXED,
                    normalized,
                    tokenize='trigram'
                );
                """
            )
            db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def create_job(self, job_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO jobs(id, status, message) VALUES(?, ?, '')",
                (job_id, JobStatus.QUEUED.value),
            )

    def update_job(self, job_id: str, status: JobStatus, message: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?, message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status.value, message, job_id),
            )

    def save_document(self, job_id: str, result: DocumentResult) -> None:
        document = result.document
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO documents(
                    id, job_id, title, kind, year, script_hint, source_paths, output_dir
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.id,
                    job_id,
                    document.title,
                    document.kind,
                    result.year,
                    result.script_hint.value,
                    json.dumps([str(path) for path in document.source_paths], ensure_ascii=False),
                    str(result.output_dir) if result.output_dir else None,
                ),
            )
            db.execute(
                "DELETE FROM lines_fts WHERE line_id IN (SELECT id FROM lines WHERE document_id=?)",
                (document.id,),
            )
            db.execute(
                "DELETE FROM lines_trigram WHERE line_id IN "
                "(SELECT id FROM lines WHERE document_id=?)",
                (document.id,),
            )
            db.execute("DELETE FROM pages WHERE document_id=?", (document.id,))
            db.execute("DELETE FROM lines WHERE document_id=?", (document.id,))
            for page in result.pages:
                db.execute(
                    """
                    INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document.id,
                        page.page_index,
                        str(page.source_path),
                        page.width,
                        page.height,
                        page.mean_confidence,
                        page.expected_cer,
                        page.selected_variant,
                        page.selected_model,
                        json.dumps(page.warnings, ensure_ascii=False),
                    ),
                )
                for order, line in enumerate(page.lines):
                    self._insert_line(db, document.id, page.page_index, order, line)

    @staticmethod
    def _insert_line(
        db: sqlite3.Connection,
        document_id: str,
        page_index: int,
        order: int,
        line: LineResult,
    ) -> None:
        from schriftlotse.search import normalize_text

        normalized = normalize_text(line.text)
        db.execute(
            """
            INSERT INTO lines(
                id, document_id, page_index, line_order, text, normalized, bbox,
                confidence, model, variant, manually_corrected, alternatives
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                line.id,
                document_id,
                page_index,
                order,
                line.text,
                normalized,
                json.dumps(line.bbox),
                line.confidence,
                line.model,
                line.variant,
                int(line.manually_corrected),
                json.dumps([item.model_dump() for item in line.alternatives], ensure_ascii=False),
            ),
        )
        db.execute(
            "INSERT INTO lines_fts(line_id, text, normalized) VALUES (?, ?, ?)",
            (line.id, line.text, normalized),
        )
        db.execute(
            "INSERT INTO lines_trigram(line_id, normalized) VALUES (?, ?)",
            (line.id, normalized),
        )

    def update_line(self, line_id: str, new_text: str) -> None:
        from schriftlotse.search import normalize_text

        with self.connect() as db:
            row = db.execute("SELECT text FROM lines WHERE id=?", (line_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unbekannte Zeile: {line_id}")
            db.execute(
                "INSERT INTO edits(line_id, old_text, new_text) VALUES (?, ?, ?)",
                (line_id, row["text"], new_text),
            )
            normalized = normalize_text(new_text)
            db.execute(
                "UPDATE lines SET text=?, normalized=?, manually_corrected=1 WHERE id=?",
                (new_text, normalized, line_id),
            )
            db.execute("DELETE FROM lines_fts WHERE line_id=?", (line_id,))
            db.execute("DELETE FROM lines_trigram WHERE line_id=?", (line_id,))
            db.execute(
                "INSERT INTO lines_fts(line_id, text, normalized) VALUES (?, ?, ?)",
                (line_id, new_text, normalized),
            )
            db.execute(
                "INSERT INTO lines_trigram(line_id, normalized) VALUES (?, ?)",
                (line_id, normalized),
            )
            db.execute("DELETE FROM embeddings WHERE line_id=?", (line_id,))

    def add_entities(self, mentions: Iterable[EntityMention]) -> None:
        with self.connect() as db:
            db.executemany(
                """
                INSERT OR REPLACE INTO entities(
                    line_id, text, normalized, label, confidence, gnd_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.line_id,
                        item.text,
                        item.normalized,
                        item.label,
                        item.confidence,
                        item.gnd_id,
                    )
                    for item in mentions
                ],
            )

    def add_name_alias(self, canonical: str, alias: str) -> None:
        from schriftlotse.search import normalize_text

        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO name_aliases(canonical, alias) VALUES (?, ?)",
                (normalize_text(canonical), normalize_text(alias)),
            )

    def rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute(sql, parameters).fetchall())

    def line_context(self, line_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                """
                SELECT l.*, d.title, d.year, p.source_path, p.width, p.height
                FROM lines l
                JOIN documents d ON d.id=l.document_id
                JOIN pages p ON p.document_id=l.document_id AND p.page_index=l.page_index
                WHERE l.id=?
                """,
                (line_id,),
            ).fetchone()

    def list_documents(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM documents ORDER BY created_at DESC, title")
