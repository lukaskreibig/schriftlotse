from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from schriftlotse.cloud import cloud_transcription_issues
from schriftlotse.domain import (
    AlternativeReading,
    DocumentResult,
    EngineRun,
    EntityMention,
    ImageDiagnostics,
    JobStatus,
    LineResult,
    PageResult,
    ReadingKind,
    RegionResult,
    ReviewStatus,
    ScriptHint,
    SourceDocument,
)

SCHEMA_VERSION = 7


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._backup_before_migration()
        self.initialize()

    def _backup_before_migration(self) -> None:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return
        source = sqlite3.connect(self.path, timeout=30)
        try:
            has_meta = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if not has_meta:
                return
            row = source.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            previous = int(row[0]) if row else 0
            if previous >= SCHEMA_VERSION:
                return
            backup = self.path.with_name(
                f"{self.path.stem}.schema-v{previous}-backup{self.path.suffix}"
            )
            if backup.exists():
                return
            destination = sqlite3.connect(backup)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()

    def backup(self, label: str) -> Path:
        safe_label = "".join(
            character for character in label if character.isalnum() or character in "-_"
        )
        if not safe_label:
            raise ValueError("Ungültige Sicherungsbezeichnung")
        destination_path = self.path.with_name(f"{self.path.stem}.{safe_label}{self.path.suffix}")
        if destination_path.exists():
            return destination_path
        source = sqlite3.connect(self.path, timeout=30)
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return destination_path

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
                CREATE TABLE IF NOT EXISTS regions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    region_type TEXT NOT NULL,
                    polygon TEXT NOT NULL,
                    reading_order INTEGER NOT NULL,
                    FOREIGN KEY(document_id, page_index)
                        REFERENCES pages(document_id, page_index) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS readings (
                    id TEXT PRIMARY KEY,
                    line_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    model TEXT NOT NULL,
                    model_revision TEXT,
                    confidence REAL NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(line_id) REFERENCES lines(id) ON DELETE CASCADE
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS readings_fts USING fts5(
                    reading_id UNINDEXED,
                    line_id UNINDEXED,
                    kind UNINDEXED,
                    text,
                    normalized,
                    tokenize='unicode61 remove_diacritics 0'
                );
                CREATE TABLE IF NOT EXISTS transcript_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(document_id, label, created_at),
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS job_pages (
                    job_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(job_id, document_id, page_index),
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS engine_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    engine TEXT NOT NULL,
                    revision TEXT,
                    backend TEXT NOT NULL,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    success INTEGER NOT NULL DEFAULT 1,
                    message TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(document_id, page_index)
                        REFERENCES pages(document_id, page_index) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS cloud_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    line_id TEXT,
                    model TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL,
                    FOREIGN KEY(line_id) REFERENCES lines(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS collections (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'sammlung',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(parent_id) REFERENCES collections(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS collection_documents (
                    collection_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(collection_id, document_id),
                    FOREIGN KEY(collection_id) REFERENCES collections(id) ON DELETE CASCADE,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS tags (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    color TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS document_tags (
                    document_id TEXT NOT NULL,
                    tag_id TEXT NOT NULL,
                    PRIMARY KEY(document_id, tag_id),
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
                    FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS document_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'original',
                    original_name TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    managed_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(document_id, managed_path),
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS document_files_sha256 ON document_files(sha256);
                CREATE TABLE IF NOT EXISTS page_diagnostics (
                    document_id TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    brightness REAL,
                    contrast REAL,
                    sharpness REAL,
                    skew_degrees REAL,
                    clipped_dark REAL,
                    clipped_light REAL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(document_id, page_index),
                    FOREIGN KEY(document_id, page_index)
                        REFERENCES pages(document_id, page_index) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    progress REAL NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS integrity_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    file_id INTEGER,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
                    FOREIGN KEY(file_id) REFERENCES document_files(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS source_folders (
                    id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    root_collection_id TEXT NOT NULL,
                    last_scanned_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(root_collection_id) REFERENCES collections(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS source_entries (
                    source_folder_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    document_id TEXT,
                    collection_id TEXT,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'vorhanden',
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(source_folder_id,relative_path),
                    FOREIGN KEY(source_folder_id) REFERENCES source_folders(id) ON DELETE CASCADE,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE SET NULL,
                    FOREIGN KEY(collection_id) REFERENCES collections(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS lines_document_page
                    ON lines(document_id,page_index,line_order);
                CREATE INDEX IF NOT EXISTS collection_documents_document
                    ON collection_documents(document_id,collection_id);
                CREATE UNIQUE INDEX IF NOT EXISTS collections_unique_name
                    ON collections(COALESCE(parent_id,''),name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS document_tags_document
                    ON document_tags(document_id,tag_id);
                CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id,id);
                CREATE INDEX IF NOT EXISTS documents_created ON documents(created_at DESC);
                CREATE INDEX IF NOT EXISTS source_entries_hash
                    ON source_entries(source_folder_id,sha256);
                """
            )
            self._ensure_column(db, "pages", "logical_page_id", "TEXT")
            self._ensure_column(db, "pages", "source_page_index", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "pages", "prepared_path", "TEXT")
            self._ensure_column(db, "pages", "source_bbox", "TEXT")
            self._ensure_column(db, "pages", "transform", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(db, "pages", "profile", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(db, "lines", "region_id", "TEXT")
            self._ensure_column(db, "lines", "baseline", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(db, "lines", "polygon", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(db, "lines", "review_status", "TEXT NOT NULL DEFAULT 'automatisch'")
            self._ensure_column(db, "jobs", "request_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(db, "documents", "archive", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "documents", "fonds", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "documents", "series", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "documents", "shelfmark", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "documents", "external_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "documents", "source_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "documents", "creator", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "documents", "place", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "documents", "date_from", "INTEGER")
            self._ensure_column(db, "documents", "date_to", "INTEGER")
            self._ensure_column(db, "documents", "description", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "documents", "rights", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "documents", "notes", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "documents", "custom_fields", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(
                db, "documents", "document_status", "TEXT NOT NULL DEFAULT 'automatisch'"
            )
            self._ensure_column(db, "documents", "thumbnail_path", "TEXT")
            self._ensure_column(db, "documents", "library_managed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "documents", "deleted_at", "TEXT")
            self._migrate_readings(db)
            version_row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            previous_version = int(version_row["value"]) if version_row else 0
            if previous_version < 4:
                self._rebuild_search_indexes(db)
            db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _migrate_readings(db: sqlite3.Connection) -> None:
        rows = db.execute(
            "SELECT id, text, normalized, model, confidence, manually_corrected FROM lines "
            "WHERE id NOT IN (SELECT line_id FROM readings)"
        ).fetchall()
        for row in rows:
            kind = (
                ReadingKind.VERIFIED.value
                if row["manually_corrected"]
                else ReadingKind.CONSENSUS.value
            )
            reading_id = f"{row['id']}:{kind}:legacy"
            db.execute(
                "INSERT OR IGNORE INTO readings(id,line_id,kind,text,normalized,model,"
                "confidence,selected) VALUES(?,?,?,?,?,?,?,1)",
                (
                    reading_id,
                    row["id"],
                    kind,
                    row["text"],
                    row["normalized"],
                    row["model"],
                    row["confidence"],
                ),
            )
            db.execute(
                "INSERT INTO readings_fts(reading_id,line_id,kind,text,normalized) "
                "VALUES(?,?,?,?,?)",
                (reading_id, row["id"], kind, row["text"], row["normalized"]),
            )

    @staticmethod
    def _rebuild_search_indexes(db: sqlite3.Connection) -> None:
        """Remove duplicate/stale FTS rows left by document reprocessing."""
        db.execute("DELETE FROM lines_fts")
        db.execute(
            "INSERT INTO lines_fts(line_id,text,normalized) SELECT id,text,normalized FROM lines"
        )
        db.execute("DELETE FROM lines_trigram")
        db.execute("INSERT INTO lines_trigram(line_id,normalized) SELECT id,normalized FROM lines")
        db.execute("DELETE FROM readings_fts")
        db.execute(
            "INSERT INTO readings_fts(reading_id,line_id,kind,text,normalized) "
            "SELECT id,line_id,kind,text,normalized FROM readings"
        )

    def create_job(self, job_id: str, request_json: str = "{}") -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,status,message,request_json) VALUES(?,?,'',?) "
                "ON CONFLICT(id) DO UPDATE SET request_json=excluded.request_json",
                (job_id, JobStatus.QUEUED.value, request_json),
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
            # FTS virtual tables do not participate in foreign-key cascades.
            # Purge their old rows before deleting the relational document.
            db.execute(
                "DELETE FROM lines_fts WHERE line_id IN (SELECT id FROM lines WHERE document_id=?)",
                (document.id,),
            )
            db.execute(
                "DELETE FROM lines_trigram WHERE line_id IN "
                "(SELECT id FROM lines WHERE document_id=?)",
                (document.id,),
            )
            db.execute(
                "DELETE FROM readings_fts WHERE line_id IN "
                "(SELECT id FROM lines WHERE document_id=?)",
                (document.id,),
            )
            db.execute(
                """
                INSERT INTO documents(
                    id, job_id, title, kind, year, script_hint, source_paths, output_dir
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    job_id=excluded.job_id,
                    title=excluded.title,
                    kind=excluded.kind,
                    year=excluded.year,
                    script_hint=excluded.script_hint,
                    source_paths=excluded.source_paths,
                    output_dir=excluded.output_dir,
                    deleted_at=NULL
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
            # Preserve archive metadata and collection membership while replacing
            # only the reproducible OCR layer of a document.
            db.execute("DELETE FROM pages WHERE document_id=?", (document.id,))
            for page in result.pages:
                db.execute(
                    """
                    INSERT INTO pages(
                        document_id,page_index,source_path,width,height,confidence,
                        expected_cer,variant,model,warnings,logical_page_id,source_bbox,
                        transform,profile,source_page_index,prepared_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        page.logical_page_id,
                        json.dumps(page.source_bbox),
                        json.dumps(page.transform),
                        page.profile.model_dump_json(),
                        page.source_page_index,
                        str(page.prepared_path) if page.prepared_path else None,
                    ),
                )
                if page.image_diagnostics is not None:
                    diagnostics = page.image_diagnostics
                    db.execute(
                        "INSERT INTO page_diagnostics(document_id,page_index,brightness,"
                        "contrast,sharpness,skew_degrees,clipped_dark,clipped_light) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            document.id,
                            page.page_index,
                            diagnostics.brightness,
                            diagnostics.contrast,
                            diagnostics.sharpness,
                            diagnostics.skew_degrees,
                            diagnostics.clipped_dark,
                            diagnostics.clipped_light,
                        ),
                    )
                for region in page.regions:
                    db.execute(
                        "INSERT INTO regions VALUES(?,?,?,?,?,?)",
                        (
                            region.id,
                            document.id,
                            page.page_index,
                            region.region_type,
                            json.dumps(region.polygon),
                            region.reading_order,
                        ),
                    )
                for run in page.engine_runs:
                    db.execute(
                        "INSERT INTO engine_runs(document_id,page_index,engine,revision,backend,"
                        "duration_seconds,success,message) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            document.id,
                            page.page_index,
                            run.engine,
                            run.revision,
                            run.backend,
                            run.duration_seconds,
                            int(run.success),
                            run.message,
                        ),
                    )
                for order, line in enumerate(page.lines):
                    self._insert_line(db, document.id, page.page_index, order, line)
            db.execute(
                "UPDATE documents SET document_status=CASE "
                "WHEN document_status IN ('bestaetigt','ground_truth') THEN 'in_pruefung' "
                "ELSE 'automatisch' END WHERE id=?",
                (document.id,),
            )

    def register_document_shell(
        self,
        job_id: str,
        document: SourceDocument,
        year: int | None,
        script_hint: ScriptHint,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO documents(
                    id,job_id,title,kind,year,script_hint,source_paths,document_status
                ) VALUES(?,?,?,?,?,?,?,'in_verarbeitung')
                ON CONFLICT(id) DO UPDATE SET
                    job_id=excluded.job_id,title=excluded.title,kind=excluded.kind,
                    year=excluded.year,script_hint=excluded.script_hint,
                    source_paths=excluded.source_paths,deleted_at=NULL,
                    document_status='in_verarbeitung'
                """,
                (
                    document.id,
                    job_id,
                    document.title,
                    document.kind,
                    year,
                    script_hint.value,
                    json.dumps([str(path) for path in document.source_paths], ensure_ascii=False),
                ),
            )

    def mark_job_documents_status(self, job_id: str, status: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE documents SET document_status=? WHERE job_id=? "
                "AND document_status='in_verarbeitung'",
                (status, job_id),
            )

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
                confidence, model, variant, manually_corrected, alternatives,
                region_id, baseline, polygon, review_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                line.region_id,
                json.dumps(line.baseline),
                json.dumps(line.polygon),
                line.review_status.value,
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
        readings = list(line.readings)
        if not readings:
            from schriftlotse.domain import Reading

            readings = [
                Reading(
                    id=f"{line.id}:konsens",
                    kind=ReadingKind.CONSENSUS,
                    text=line.text,
                    model=line.model,
                    confidence=line.confidence,
                )
            ]
        for reading in readings:
            reading_normalized = normalize_text(reading.text)
            selected = int(reading.text == line.text)
            db.execute(
                "INSERT INTO readings(id,line_id,kind,text,normalized,model,model_revision,"
                "confidence,selected) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    reading.id,
                    line.id,
                    reading.kind.value,
                    reading.text,
                    reading_normalized,
                    reading.model,
                    reading.model_revision,
                    reading.confidence,
                    selected,
                ),
            )
            db.execute(
                "INSERT INTO readings_fts(reading_id,line_id,kind,text,normalized) "
                "VALUES(?,?,?,?,?)",
                (reading.id, line.id, reading.kind.value, reading.text, reading_normalized),
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
                "UPDATE lines SET text=?, normalized=?, manually_corrected=1, review_status=? "
                "WHERE id=?",
                (new_text, normalized, ReviewStatus.VERIFIED.value, line_id),
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
            db.execute("UPDATE readings SET selected=0 WHERE line_id=?", (line_id,))
            edit_count = db.execute(
                "SELECT COUNT(*) FROM edits WHERE line_id=?", (line_id,)
            ).fetchone()[0]
            reading_id = f"{line_id}:bestaetigt:{edit_count}"
            db.execute(
                "INSERT INTO readings(id,line_id,kind,text,normalized,model,confidence,selected) "
                "VALUES(?,?,?,?,?,'manuell',1.0,1)",
                (reading_id, line_id, ReadingKind.VERIFIED.value, new_text, normalized),
            )
            db.execute(
                "INSERT INTO readings_fts(reading_id,line_id,kind,text,normalized) "
                "VALUES(?,?,?,?,?)",
                (reading_id, line_id, ReadingKind.VERIFIED.value, new_text, normalized),
            )

    def add_reading(self, line_id: str, reading: Any) -> None:
        """Adds a non-destructive engine/cloud reading without selecting it."""
        from schriftlotse.search import normalize_text

        with self.connect() as db:
            if db.execute("SELECT 1 FROM lines WHERE id=?", (line_id,)).fetchone() is None:
                raise KeyError(f"Unbekannte Zeile: {line_id}")
            normalized = normalize_text(reading.text)
            db.execute(
                "INSERT INTO readings(id,line_id,kind,text,normalized,model,model_revision,"
                "confidence,selected) VALUES(?,?,?,?,?,?,?,?,0)",
                (
                    reading.id,
                    line_id,
                    reading.kind.value,
                    reading.text,
                    normalized,
                    reading.model,
                    reading.model_revision,
                    reading.confidence,
                ),
            )
            db.execute(
                "INSERT INTO readings_fts(reading_id,line_id,kind,text,normalized) "
                "VALUES(?,?,?,?,?)",
                (reading.id, line_id, reading.kind.value, reading.text, normalized),
            )

    def update_job_page(
        self,
        job_id: str,
        document_id: str,
        page_index: int,
        fingerprint: str,
        stage: str,
        status: str,
        message: str = "",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO job_pages(
                    job_id,document_id,page_index,fingerprint,stage,status,message
                )
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(job_id,document_id,page_index) DO UPDATE SET
                  fingerprint=excluded.fingerprint, stage=excluded.stage,
                  status=excluded.status, message=excluded.message,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (job_id, document_id, page_index, fingerprint, stage, status, message),
            )

    def list_incomplete_jobs(self) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT * FROM jobs WHERE status IN ('wartend','läuft') ORDER BY updated_at DESC"
        )

    def job(self, job_id: str) -> sqlite3.Row | None:
        rows = self.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        return rows[0] if rows else None

    def job_history(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.rows(
            """
            SELECT j.*,
                   (SELECT COUNT(*) FROM documents d WHERE d.job_id=j.id) AS document_count,
                   (SELECT COUNT(*) FROM pages p JOIN documents d ON d.id=p.document_id
                    WHERE d.job_id=j.id) AS page_count,
                   (SELECT COALESCE(SUM(cost_usd),0) FROM cloud_usage c
                    WHERE c.job_id=j.id) AS cloud_cost
            FROM jobs j ORDER BY j.updated_at DESC LIMIT ?
            """,
            (limit,),
        )

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
                SELECT l.*, d.title, d.year, p.source_path, p.source_page_index,
                       p.prepared_path, p.source_bbox, p.transform, p.profile,p.width,p.height
                FROM lines l
                JOIN documents d ON d.id=l.document_id
                JOIN pages p ON p.document_id=l.document_id AND p.page_index=l.page_index
                WHERE l.id=?
                """,
                (line_id,),
            ).fetchone()

    def line_readings(self, line_id: str) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT id,kind,text,model,model_revision,confidence,selected,created_at "
            "FROM readings "
            "WHERE line_id=? ORDER BY selected DESC, confidence DESC, created_at DESC",
            (line_id,),
        )

    def page_engine_runs(self, document_id: str, page_index: int) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT engine,revision,backend,duration_seconds,success,message "
            "FROM engine_runs WHERE document_id=? AND page_index=? ORDER BY id",
            (document_id, page_index),
        )

    def record_cloud_usage(
        self,
        *,
        line_id: str | None,
        model: str,
        profile: str,
        cost_usd: float,
        duration_seconds: float,
        status: str,
        message: str = "",
        job_id: str | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO cloud_usage(job_id,line_id,model,profile,cost_usd,"
                "duration_seconds,status,message) VALUES(?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    line_id,
                    model,
                    profile,
                    max(0.0, cost_usd),
                    max(0.0, duration_seconds),
                    status,
                    message,
                ),
            )

    def cloud_usage_summary(self) -> dict[str, Any]:
        row = self.rows(
            "SELECT COUNT(*) AS requests,COALESCE(SUM(cost_usd),0) AS cost_usd,"
            "SUM(CASE WHEN status='erfolgreich' THEN 1 ELSE 0 END) AS successful "
            "FROM cloud_usage"
        )[0]
        return dict(row)

    def ground_truth_stats(self) -> dict[str, int]:
        row = self.rows(
            "SELECT COUNT(*) AS verified_lines,COUNT(DISTINCT document_id) AS documents "
            "FROM lines WHERE manually_corrected=1"
        )[0]
        return {key: int(value or 0) for key, value in dict(row).items()}

    def review_queue(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.rows(
            """
            SELECT l.id AS line_id,l.document_id,d.title AS document_title,
                   l.page_index,p.source_path,l.bbox,l.text,l.confidence,d.year
            FROM lines l
            JOIN documents d ON d.id=l.document_id
            JOIN pages p ON p.document_id=l.document_id AND p.page_index=l.page_index
            WHERE l.review_status=?
            ORDER BY l.confidence ASC,d.created_at DESC,l.document_id,l.page_index,l.line_order
            LIMIT ?
            """,
            (ReviewStatus.UNCERTAIN.value, limit),
        )

    def list_documents(self) -> list[sqlite3.Row]:
        return self.rows(
            """
            SELECT d.*,
                   (SELECT COUNT(*) FROM pages p WHERE p.document_id=d.id) AS page_count,
                   (SELECT COUNT(*) FROM lines l WHERE l.document_id=d.id) AS line_count,
                   (SELECT COUNT(*) FROM lines l WHERE l.document_id=d.id
                    AND l.review_status='unsicher') AS uncertain_count,
                   (SELECT AVG(confidence) FROM pages p
                    WHERE p.document_id=d.id) AS mean_confidence,
                   (SELECT GROUP_CONCAT(c.name) FROM collections c
                    JOIN collection_documents cd ON cd.collection_id=c.id
                    WHERE cd.document_id=d.id) AS collections,
                   (SELECT GROUP_CONCAT(t.name) FROM tags t
                    JOIN document_tags dt ON dt.tag_id=t.id
                    WHERE dt.document_id=d.id) AS tags
            FROM documents d
            WHERE d.deleted_at IS NULL
            ORDER BY d.created_at DESC,d.title
            """
        )

    def list_deleted_documents(self) -> list[sqlite3.Row]:
        return self.rows(
            """
            SELECT d.*,
                   (SELECT COUNT(*) FROM pages p WHERE p.document_id=d.id) AS page_count,
                   (SELECT COUNT(*) FROM lines l WHERE l.document_id=d.id) AS line_count,
                   (SELECT COUNT(*) FROM lines l WHERE l.document_id=d.id
                    AND l.review_status='unsicher') AS uncertain_count,
                   (SELECT AVG(confidence) FROM pages p
                    WHERE p.document_id=d.id) AS mean_confidence,
                   (SELECT GROUP_CONCAT(c.name) FROM collections c
                    JOIN collection_documents cd ON cd.collection_id=c.id
                    WHERE cd.document_id=d.id) AS collections,
                   (SELECT GROUP_CONCAT(t.name) FROM tags t
                    JOIN document_tags dt ON dt.tag_id=t.id
                    WHERE dt.document_id=d.id) AS tags
            FROM documents d WHERE d.deleted_at IS NOT NULL
            ORDER BY d.deleted_at DESC,d.title
            """
        )

    def list_document_rows(self, include_deleted: bool = False) -> list[sqlite3.Row]:
        where = "" if include_deleted else "WHERE deleted_at IS NULL"
        return self.rows(f"SELECT * FROM documents {where} ORDER BY created_at DESC,title")

    def document(self, document_id: str) -> sqlite3.Row | None:
        rows = self.rows("SELECT * FROM documents WHERE id=?", (document_id,))
        return rows[0] if rows else None

    def create_collection(
        self,
        collection_id: str,
        name: str,
        description: str = "",
        parent_id: str | None = None,
        *,
        kind: str = "sammlung",
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO collections(id,parent_id,name,description,kind) VALUES(?,?,?,?,?)",
                (collection_id, parent_id, name.strip(), description.strip(), kind),
            )

    def ensure_collection(
        self,
        name: str,
        parent_id: str | None = None,
        *,
        kind: str = "sammlung",
    ) -> str:
        clean = name.strip()
        if not clean:
            raise ValueError("Sammlungsname fehlt")
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM collections WHERE COALESCE(parent_id,'')=COALESCE(?, '') "
                "AND name=? COLLATE NOCASE",
                (parent_id, clean),
            ).fetchone()
            if row is not None:
                return str(row["id"])
            collection_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO collections(id,parent_id,name,kind) VALUES(?,?,?,?)",
                (collection_id, parent_id, clean, kind),
            )
            return collection_id

    def ensure_collection_path(self, parts: list[str], *, kind: str = "sammlung") -> str:
        parent_id: str | None = None
        for part in parts:
            parent_id = self.ensure_collection(part, parent_id, kind=kind)
        if parent_id is None:
            raise ValueError("Leerer Sammlungspfad")
        return parent_id

    def list_collections(self) -> list[sqlite3.Row]:
        return self.rows(
            """
            SELECT c.*,COUNT(cd.document_id) AS document_count
            FROM collections c
            LEFT JOIN collection_documents cd ON cd.collection_id=c.id
            GROUP BY c.id ORDER BY c.sort_order,c.name COLLATE NOCASE
            """
        )

    def update_collection(
        self,
        collection_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        parent_id: str | None | object = Ellipsis,
    ) -> None:
        fields: list[tuple[str, Any]] = []
        if name is not None:
            clean = name.strip()
            if not clean:
                raise ValueError("Sammlungsname fehlt")
            fields.append(("name", clean))
        if description is not None:
            fields.append(("description", description.strip()))
        if parent_id is not Ellipsis:
            if parent_id == collection_id:
                raise ValueError("Eine Sammlung kann nicht ihr eigener Überordner sein")
            if parent_id is not None:
                descendants = set(self.collection_descendant_ids(collection_id))
                if parent_id in descendants:
                    raise ValueError(
                        "Eine Sammlung kann nicht in ihre Unterstruktur verschoben werden"
                    )
            fields.append(("parent_id", parent_id))
        if not fields:
            return
        with self.connect() as db:
            if (
                db.execute("SELECT 1 FROM collections WHERE id=?", (collection_id,)).fetchone()
                is None
            ):
                raise KeyError(collection_id)
            assignment = ",".join(f"{key}=?" for key, _value in fields)
            db.execute(
                f"UPDATE collections SET {assignment} WHERE id=?",
                (*[value for _key, value in fields], collection_id),
            )

    def collection_descendant_ids(self, collection_id: str) -> list[str]:
        return [
            str(row["id"])
            for row in self.rows(
                """
                WITH RECURSIVE descendants(id) AS (
                    SELECT id FROM collections WHERE parent_id=?
                    UNION ALL
                    SELECT c.id FROM collections c JOIN descendants d ON c.parent_id=d.id
                ) SELECT id FROM descendants
                """,
                (collection_id,),
            )
        ]

    def delete_collection(self, collection_id: str) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT parent_id FROM collections WHERE id=?", (collection_id,)
            ).fetchone()
            if row is None:
                raise KeyError(collection_id)
            db.execute(
                "UPDATE collections SET parent_id=? WHERE parent_id=?",
                (row["parent_id"], collection_id),
            )
            db.execute("DELETE FROM collections WHERE id=?", (collection_id,))

    def add_document_to_collections(self, document_id: str, collection_ids: list[str]) -> None:
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO collection_documents(collection_id,document_id) VALUES(?,?)",
                [(collection_id, document_id) for collection_id in collection_ids],
            )

    def set_document_collections(self, document_id: str, collection_ids: list[str]) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM collection_documents WHERE document_id=?", (document_id,))
            db.executemany(
                "INSERT INTO collection_documents(collection_id,document_id) VALUES(?,?)",
                [(collection_id, document_id) for collection_id in collection_ids],
            )

    def set_document_tags(self, document_id: str, names: list[str]) -> None:
        clean = list(dict.fromkeys(name.strip() for name in names if name.strip()))
        with self.connect() as db:
            db.execute("DELETE FROM document_tags WHERE document_id=?", (document_id,))
            for name in clean:
                tag_id = name.casefold()
                db.execute(
                    "INSERT INTO tags(id,name) VALUES(?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                    (tag_id, name),
                )
                db.execute(
                    "INSERT INTO document_tags(document_id,tag_id) VALUES(?,?)",
                    (document_id, tag_id),
                )

    def update_document_metadata(self, document_id: str, values: dict[str, Any]) -> None:
        allowed = {
            "title",
            "year",
            "archive",
            "fonds",
            "series",
            "shelfmark",
            "external_id",
            "source_url",
            "creator",
            "place",
            "date_from",
            "date_to",
            "description",
            "rights",
            "notes",
            "document_status",
        }
        fields = [(key, value) for key, value in values.items() if key in allowed]
        if not fields:
            return
        with self.connect() as db:
            if db.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone() is None:
                raise KeyError(document_id)
            assignment = ",".join(f"{key}=?" for key, _value in fields)
            db.execute(
                f"UPDATE documents SET {assignment} WHERE id=?",
                (*[value for _key, value in fields], document_id),
            )

    def trash_document(self, document_id: str, restore: bool = False) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE documents SET deleted_at="
                + ("NULL" if restore else "CURRENT_TIMESTAMP")
                + " WHERE id=?",
                (document_id,),
            )

    def purge_document(self, document_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM lines_fts WHERE line_id IN (SELECT id FROM lines WHERE document_id=?)",
                (document_id,),
            )
            db.execute(
                "DELETE FROM lines_trigram WHERE line_id IN "
                "(SELECT id FROM lines WHERE document_id=?)",
                (document_id,),
            )
            db.execute(
                "DELETE FROM readings_fts WHERE line_id IN "
                "(SELECT id FROM lines WHERE document_id=?)",
                (document_id,),
            )
            db.execute("DELETE FROM documents WHERE id=?", (document_id,))

    def split_document_into_pages(self, document_id: str) -> list[str]:
        """Split an accidentally grouped legacy image series without losing OCR data."""
        created: list[str] = []
        with self.connect() as db:
            document = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if document is None:
                raise KeyError(document_id)
            pages = db.execute(
                "SELECT * FROM pages WHERE document_id=? ORDER BY page_index", (document_id,)
            ).fetchall()
            if len(pages) < 2:
                return [document_id]
            memberships = db.execute(
                "SELECT collection_id FROM collection_documents WHERE document_id=?",
                (document_id,),
            ).fetchall()
            tag_memberships = db.execute(
                "SELECT tag_id FROM document_tags WHERE document_id=?", (document_id,)
            ).fetchall()
            versions = db.execute(
                "SELECT label,kind,created_at FROM transcript_versions WHERE document_id=?",
                (document_id,),
            ).fetchall()
            for page in pages:
                new_id = f"{document_id}-seite-{int(page['page_index']) + 1:04d}"
                title = Path(page["source_path"]).stem or (
                    f"{document['title']} – Seite {int(page['page_index']) + 1}"
                )
                db.execute(
                    """
                    INSERT OR IGNORE INTO documents(
                        id,job_id,title,kind,year,script_hint,source_paths,output_dir,
                        archive,fonds,series,shelfmark,external_id,source_url,creator,place,
                        date_from,date_to,description,rights,notes,custom_fields,document_status
                    ) VALUES(?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id,
                        document["job_id"],
                        title,
                        document["kind"],
                        document["year"],
                        document["script_hint"],
                        json.dumps([page["source_path"]], ensure_ascii=False),
                        document["archive"],
                        document["fonds"],
                        document["series"],
                        document["shelfmark"],
                        document["external_id"],
                        document["source_url"],
                        document["creator"],
                        document["place"],
                        document["date_from"],
                        document["date_to"],
                        document["description"],
                        document["rights"],
                        document["notes"],
                        document["custom_fields"],
                        document["document_status"],
                    ),
                )
                db.execute(
                    """
                    INSERT OR REPLACE INTO pages(
                        document_id,page_index,source_path,width,height,confidence,expected_cer,
                        variant,model,warnings,logical_page_id,source_page_index,prepared_path,
                        source_bbox,transform,profile
                    ) VALUES(?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id,
                        page["source_path"],
                        page["width"],
                        page["height"],
                        page["confidence"],
                        page["expected_cer"],
                        page["variant"],
                        page["model"],
                        page["warnings"],
                        page["logical_page_id"],
                        page["source_page_index"],
                        page["prepared_path"],
                        page["source_bbox"],
                        page["transform"],
                        page["profile"],
                    ),
                )
                old_index = int(page["page_index"])
                db.execute(
                    "UPDATE lines SET document_id=?,page_index=0 "
                    "WHERE document_id=? AND page_index=?",
                    (new_id, document_id, old_index),
                )
                db.execute(
                    "UPDATE regions SET document_id=?,page_index=0 "
                    "WHERE document_id=? AND page_index=?",
                    (new_id, document_id, old_index),
                )
                db.execute(
                    "UPDATE engine_runs SET document_id=?,page_index=0 "
                    "WHERE document_id=? AND page_index=?",
                    (new_id, document_id, old_index),
                )
                db.execute(
                    "UPDATE page_diagnostics SET document_id=?,page_index=0 "
                    "WHERE document_id=? AND page_index=?",
                    (new_id, document_id, old_index),
                )
                db.executemany(
                    "INSERT OR IGNORE INTO collection_documents(collection_id,document_id) "
                    "VALUES(?,?)",
                    [(membership["collection_id"], new_id) for membership in memberships],
                )
                db.executemany(
                    "INSERT OR IGNORE INTO document_tags(document_id,tag_id) VALUES(?,?)",
                    [(new_id, membership["tag_id"]) for membership in tag_memberships],
                )
                db.executemany(
                    "INSERT OR IGNORE INTO transcript_versions(document_id,label,kind,created_at) "
                    "VALUES(?,?,?,?)",
                    [
                        (new_id, version["label"], version["kind"], version["created_at"])
                        for version in versions
                    ],
                )
                db.execute(
                    "DELETE FROM pages WHERE document_id=? AND page_index=?",
                    (document_id, old_index),
                )
                created.append(new_id)
            db.execute("DELETE FROM documents WHERE id=?", (document_id,))
        return created

    def mark_document_managed(
        self,
        document_id: str,
        managed_files: Iterable[Any],
        path_mapping: dict[str, str],
        thumbnail: Path,
    ) -> None:
        managed_files = list(managed_files)
        with self.connect() as db:
            source_paths = [str(item.managed_path) for item in managed_files]
            db.execute(
                "UPDATE documents SET source_paths=?,thumbnail_path=?,library_managed=1 WHERE id=?",
                (json.dumps(source_paths, ensure_ascii=False), str(thumbnail), document_id),
            )
            for original, managed in path_mapping.items():
                db.execute(
                    "UPDATE pages SET source_path=? WHERE document_id=? AND source_path=?",
                    (managed, document_id, original),
                )
            db.execute(
                "DELETE FROM document_files WHERE document_id=? AND role='original'", (document_id,)
            )
            db.executemany(
                "INSERT INTO document_files(document_id,role,original_name,original_path,"
                "managed_path,sha256,size,media_type) VALUES(?,'original',?,?,?,?,?,?)",
                [
                    (
                        document_id,
                        item.original_name,
                        str(item.original_path),
                        str(item.managed_path),
                        item.sha256,
                        item.size,
                        item.media_type,
                    )
                    for item in managed_files
                ],
            )

    def update_page_prepared_path(
        self, document_id: str, page_index: int, prepared_path: Path
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE pages SET prepared_path=? WHERE document_id=? AND page_index=?",
                (str(prepared_path), document_id, page_index),
            )

    def document_files(self, document_id: str) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT * FROM document_files WHERE document_id=? ORDER BY role,id", (document_id,)
        )

    def record_integrity_check(
        self, document_id: str, file_id: int | None, status: str, message: str
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO integrity_checks(document_id,file_id,status,message) VALUES(?,?,?,?)",
                (document_id, file_id, status, message),
            )

    def record_job_event(
        self,
        job_id: str,
        event_type: str,
        message: str,
        progress: float,
        payload: dict[str, Any] | None = None,
    ) -> None:
        stage = str((payload or {}).get("stage", ""))
        with self.connect() as db:
            db.execute(
                "INSERT INTO job_events(job_id,event_type,stage,message,progress,payload) "
                "VALUES(?,?,?,?,?,?)",
                (
                    job_id,
                    event_type,
                    stage,
                    message,
                    max(0.0, min(1.0, progress)),
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )

    def job_events(self, job_id: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,))

    def upsert_source_folder(
        self, source_id: str, root_path: Path, label: str, root_collection_id: str
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO source_folders(id,root_path,label,root_collection_id,last_scanned_at)
                VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(root_path) DO UPDATE SET
                    label=excluded.label,
                    root_collection_id=excluded.root_collection_id,
                    last_scanned_at=CURRENT_TIMESTAMP
                """,
                (source_id, str(root_path.resolve()), label, root_collection_id),
            )

    def source_folder_by_path(self, root_path: Path) -> sqlite3.Row | None:
        rows = self.rows(
            "SELECT * FROM source_folders WHERE root_path=?", (str(root_path.resolve()),)
        )
        return rows[0] if rows else None

    def source_folder(self, source_id: str) -> sqlite3.Row | None:
        rows = self.rows("SELECT * FROM source_folders WHERE id=?", (source_id,))
        return rows[0] if rows else None

    def list_source_folders(self) -> list[sqlite3.Row]:
        return self.rows(
            """
            SELECT sf.*,c.name AS collection_name,
                   COUNT(se.relative_path) AS file_count,
                   SUM(CASE WHEN se.state='fehlt' THEN 1 ELSE 0 END) AS missing_count
            FROM source_folders sf
            JOIN collections c ON c.id=sf.root_collection_id
            LEFT JOIN source_entries se ON se.source_folder_id=sf.id
            GROUP BY sf.id ORDER BY sf.label COLLATE NOCASE
            """
        )

    def source_entries(self, source_id: str) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT * FROM source_entries WHERE source_folder_id=? ORDER BY relative_path",
            (source_id,),
        )

    def upsert_source_entry(
        self,
        source_id: str,
        relative_path: str,
        sha256: str,
        size: int,
        mtime_ns: int,
        *,
        document_id: str | None = None,
        collection_id: str | None = None,
        state: str = "vorhanden",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO source_entries(
                    source_folder_id,relative_path,document_id,collection_id,
                    sha256,size,mtime_ns,state,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(source_folder_id,relative_path) DO UPDATE SET
                    document_id=COALESCE(excluded.document_id,source_entries.document_id),
                    collection_id=COALESCE(excluded.collection_id,source_entries.collection_id),
                    sha256=excluded.sha256,size=excluded.size,mtime_ns=excluded.mtime_ns,
                    state=excluded.state,last_seen_at=CURRENT_TIMESTAMP
                """,
                (
                    source_id,
                    relative_path,
                    document_id,
                    collection_id,
                    sha256,
                    size,
                    mtime_ns,
                    state,
                ),
            )

    def mark_missing_source_entries(self, source_id: str, present_paths: list[str]) -> None:
        with self.connect() as db:
            if present_paths:
                placeholders = ",".join("?" for _ in present_paths)
                db.execute(
                    f"UPDATE source_entries SET state='fehlt' WHERE source_folder_id=? "
                    f"AND relative_path NOT IN ({placeholders})",
                    (source_id, *present_paths),
                )
            else:
                db.execute(
                    "UPDATE source_entries SET state='fehlt' WHERE source_folder_id=?",
                    (source_id,),
                )
            db.execute(
                "UPDATE source_folders SET last_scanned_at=CURRENT_TIMESTAMP WHERE id=?",
                (source_id,),
            )

    def move_source_entry(
        self,
        source_id: str,
        previous_path: str,
        relative_path: str,
        sha256: str,
        size: int,
        mtime_ns: int,
        collection_id: str,
    ) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM source_entries WHERE source_folder_id=? AND relative_path=?",
                (source_id, previous_path),
            ).fetchone()
            if row is None:
                raise KeyError(previous_path)
            document_id = row["document_id"]
            old_collection_id = row["collection_id"]
            db.execute(
                "DELETE FROM source_entries WHERE source_folder_id=? AND relative_path=?",
                (source_id, previous_path),
            )
            db.execute(
                """
                INSERT OR REPLACE INTO source_entries(
                    source_folder_id,relative_path,document_id,collection_id,
                    sha256,size,mtime_ns,state,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,'vorhanden',CURRENT_TIMESTAMP)
                """,
                (
                    source_id,
                    relative_path,
                    document_id,
                    collection_id,
                    sha256,
                    size,
                    mtime_ns,
                ),
            )
            if document_id:
                if old_collection_id:
                    db.execute(
                        "DELETE FROM collection_documents WHERE collection_id=? AND document_id=?",
                        (old_collection_id, document_id),
                    )
                db.execute(
                    "INSERT OR IGNORE INTO collection_documents(collection_id,document_id) "
                    "VALUES(?,?)",
                    (collection_id, document_id),
                )

    def document_transcript(self, document_id: str) -> dict[str, Any] | None:
        document = self.document(document_id)
        if document is None:
            return None
        pages = self.rows(
            "SELECT page_index,width,height FROM pages WHERE document_id=? ORDER BY page_index",
            (document_id,),
        )
        result_pages: list[dict[str, Any]] = []
        for page in pages:
            lines = self.rows(
                """
                SELECT id,line_order,text,bbox,polygon,confidence,model,variant,
                       manually_corrected,review_status
                FROM lines WHERE document_id=? AND page_index=? ORDER BY line_order
                """,
                (document_id, page["page_index"]),
            )
            line_items: list[dict[str, Any]] = []
            for row in lines:
                item = dict(row)
                item["bbox"] = json.loads(item["bbox"] or "[]")
                item["polygon"] = json.loads(item["polygon"] or "[]")
                item["manually_corrected"] = bool(item["manually_corrected"])
                item["readings"] = []
                for reading in self.line_readings(str(item["id"])):
                    reading_item = dict(reading)
                    reading_item["quality_issues"] = (
                        cloud_transcription_issues(reading_item["text"], item["text"])
                        if reading_item["kind"] == ReadingKind.CLOUD.value
                        else []
                    )
                    item["readings"].append(reading_item)
                line_items.append(item)
            readable = self._readable_text([item["text"] for item in line_items])
            result_pages.append(
                {
                    "page_index": int(page["page_index"]),
                    "width": int(page["width"]),
                    "height": int(page["height"]),
                    "lines": line_items,
                    "reading_text": readable,
                }
            )
        model_versions = self._document_model_versions(result_pages)
        return {
            "document_id": document_id,
            "title": document["title"],
            "pages": result_pages,
            "line_count": sum(len(page["lines"]) for page in result_pages),
            "cloud_summary": self._document_cloud_summary(document_id, document["job_id"]),
            "model_versions": model_versions,
        }

    @staticmethod
    def _readable_text(lines: list[str]) -> str:
        """Repair layout artefacts without modernising historic German."""
        reading = "\n".join(lines)
        reading = re.sub(r"(?<=\w)-\n(?=[a-zäöüß])", "", reading)
        reading = re.sub(r"(?<!\n)\n(?!\n)", " ", reading)
        return re.sub(r"[ \t]+", " ", reading).strip()

    def _document_model_versions(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        model_names = sorted(
            {
                str(reading["model"])
                for page in pages
                for line in page["lines"]
                for reading in line["readings"]
                if reading.get("model") and reading.get("text")
            }
        )
        total_lines = sum(len(page["lines"]) for page in pages)
        versions: list[dict[str, Any]] = []
        for model in model_names:
            covered = 0
            changed = 0
            flagged = 0
            character_count = 0
            version_pages: list[dict[str, Any]] = []
            kinds: set[str] = set()
            for page in pages:
                version_lines: list[str] = []
                page_covered = 0
                for line in page["lines"]:
                    matching = [
                        reading
                        for reading in line["readings"]
                        if reading.get("model") == model and reading.get("text")
                    ]
                    if matching:
                        chosen = matching[0]
                        text = str(chosen["text"])
                        kinds.add(str(chosen["kind"]))
                        covered += 1
                        page_covered += 1
                        changed += int(text != line["text"])
                        flagged += int(bool(chosen.get("quality_issues")))
                        character_count += len(text.strip())
                        version_lines.append(text)
                version_pages.append(
                    {
                        "page_index": page["page_index"],
                        "reading_text": self._readable_text(version_lines),
                        "covered_lines": page_covered,
                        "total_lines": len(page["lines"]),
                    }
                )
            average_characters = character_count / covered if covered else 0.0
            quality_notes: list[str] = []
            if covered >= 5 and average_characters < 3:
                quality_notes.append("ungewöhnlich kurze, wahrscheinlich unbrauchbare Lesungen")
            if flagged:
                quality_notes.append("formal auffällige Cloud-Ausgaben enthalten")
            versions.append(
                {
                    "id": model,
                    "model": model,
                    "kinds": sorted(kinds),
                    "covered_lines": covered,
                    "total_lines": total_lines,
                    "changed_lines": changed,
                    "quality_issue_lines": flagged,
                    "average_characters_per_line": average_characters,
                    "quality_notes": quality_notes,
                    "complete": bool(total_lines and covered == total_lines),
                    "pages": version_pages,
                }
            )
        return sorted(
            versions,
            key=lambda item: (
                bool(item["quality_notes"]),
                -int(item["covered_lines"]),
                str(item["model"]),
            ),
        )

    def _document_cloud_summary(self, document_id: str, job_id: str | None) -> dict[str, Any]:
        models = self.rows(
            """
            SELECT r.model,COUNT(*) AS reading_count,COUNT(DISTINCT r.line_id) AS line_count
            FROM readings r JOIN lines l ON l.id=r.line_id
            WHERE l.document_id=? AND r.kind='cloud'
            GROUP BY r.model ORDER BY reading_count DESC,r.model
            """,
            (document_id,),
        )
        line_count = self.rows(
            "SELECT COUNT(DISTINCT r.line_id) AS amount FROM readings r "
            "JOIN lines l ON l.id=r.line_id WHERE l.document_id=? AND r.kind='cloud'",
            (document_id,),
        )[0]["amount"]
        usage = self.rows(
            "SELECT COUNT(*) AS requests,COALESCE(SUM(cost_usd),0) AS cost_usd,"
            "SUM(CASE WHEN status='fehlgeschlagen' THEN 1 ELSE 0 END) AS failed_requests "
            "FROM cloud_usage WHERE (? IS NOT NULL AND job_id=?) OR line_id IN "
            "(SELECT id FROM lines WHERE document_id=?)",
            (job_id, job_id, document_id),
        )[0]
        return {
            "reading_count": sum(int(row["reading_count"]) for row in models),
            "line_count": int(line_count or 0),
            "models": [dict(row) for row in models],
            "requests": int(usage["requests"] or 0),
            "cost_usd": float(usage["cost_usd"] or 0),
            "failed_requests": int(usage["failed_requests"] or 0),
        }

    def document_detail(self, document_id: str) -> dict[str, Any] | None:
        row = self.document(document_id)
        if row is None:
            return None
        pages = self.rows(
            """
            SELECT p.*,pd.brightness,pd.contrast,pd.sharpness,pd.skew_degrees,
                   pd.clipped_dark,pd.clipped_light,
                   (SELECT COUNT(*) FROM lines l WHERE l.document_id=p.document_id
                    AND l.page_index=p.page_index) AS line_count,
                   (SELECT COUNT(*) FROM lines l WHERE l.document_id=p.document_id
                    AND l.page_index=p.page_index AND l.review_status='unsicher') AS uncertain_count
            FROM pages p LEFT JOIN page_diagnostics pd
              ON pd.document_id=p.document_id AND pd.page_index=p.page_index
            WHERE p.document_id=? ORDER BY p.page_index
            """,
            (document_id,),
        )
        collections = self.rows(
            "SELECT c.id,c.name FROM collections c JOIN collection_documents cd "
            "ON cd.collection_id=c.id WHERE cd.document_id=? ORDER BY c.name",
            (document_id,),
        )
        integrity = self.rows(
            "SELECT status,message,checked_at FROM integrity_checks WHERE document_id=? "
            "ORDER BY id DESC LIMIT 20",
            (document_id,),
        )
        result = dict(row)
        result["source_paths"] = json.loads(result["source_paths"] or "[]")
        result["pages"] = []
        for page in pages:
            item = dict(page)
            for key in ("warnings", "transform", "profile", "source_bbox"):
                item[key] = (
                    json.loads(item[key]) if item.get(key) else ([] if key != "profile" else {})
                )
            item["engine_runs"] = [
                dict(run) for run in self.page_engine_runs(document_id, int(page["page_index"]))
            ]
            result["pages"].append(item)
        result["files"] = [dict(item) for item in self.document_files(document_id)]
        result["collections"] = [dict(item) for item in collections]
        result["tags"] = [
            item["name"]
            for item in self.rows(
                "SELECT t.name FROM tags t JOIN document_tags dt ON dt.tag_id=t.id "
                "WHERE dt.document_id=? ORDER BY t.name COLLATE NOCASE",
                (document_id,),
            )
        ]
        result["integrity"] = [dict(item) for item in integrity]
        return result

    def search_document_metadata(
        self,
        text: str,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        pattern = f"%{text.strip().casefold()}%"
        clauses = ["d.deleted_at IS NULL"]
        parameters: list[Any] = [pattern] * 10
        if year_from is not None:
            clauses.append("COALESCE(d.date_to,d.year,9999)>=?")
            parameters.append(year_from)
        if year_to is not None:
            clauses.append("COALESCE(d.date_from,d.year,0)<=?")
            parameters.append(year_to)
        parameters.append(limit)
        metadata_clauses = [
            "LOWER(d.title) LIKE ?",
            "LOWER(d.archive) LIKE ?",
            "LOWER(d.fonds) LIKE ?",
            "LOWER(d.series) LIKE ?",
            "LOWER(d.shelfmark) LIKE ?",
            "LOWER(d.creator) LIKE ?",
            "LOWER(d.place) LIKE ?",
            "LOWER(d.description) LIKE ?",
            "LOWER(d.notes) LIKE ?",
            "EXISTS(SELECT 1 FROM document_tags dt JOIN tags t ON t.id=dt.tag_id "
            "WHERE dt.document_id=d.id AND LOWER(t.name) LIKE ?)",
        ]
        metadata_where = " OR ".join(metadata_clauses)
        filter_where = " AND ".join(clauses)
        return self.rows(
            f"""
            SELECT d.id,d.title,d.year,d.archive,d.fonds,d.series,d.shelfmark,d.creator,d.place,
                   p.page_index,p.source_path,p.confidence,l.id AS line_id,l.bbox
            FROM documents d
            LEFT JOIN pages p ON p.document_id=d.id AND p.page_index=(
                SELECT MIN(page_index) FROM pages WHERE document_id=d.id
            )
            LEFT JOIN lines l ON l.document_id=d.id AND l.page_index=p.page_index
              AND l.line_order=0
            WHERE ({metadata_where}) AND {filter_where}
            ORDER BY d.created_at DESC LIMIT ?
            """,
            tuple(parameters),
        )

    def load_document_result(self, document_id: str) -> DocumentResult:
        document = self.document(document_id)
        if document is None:
            raise KeyError(document_id)
        page_rows = self.rows(
            "SELECT * FROM pages WHERE document_id=? ORDER BY page_index", (document_id,)
        )
        pages: list[PageResult] = []
        for page_row in page_rows:
            line_rows = self.rows(
                "SELECT * FROM lines WHERE document_id=? AND page_index=? ORDER BY line_order",
                (document_id, page_row["page_index"]),
            )
            lines: list[LineResult] = []
            for line in line_rows:
                alternatives = [
                    AlternativeReading.model_validate(item)
                    for item in json.loads(line["alternatives"] or "[]")
                ]
                readings = []
                from schriftlotse.domain import Reading

                for reading in self.line_readings(line["id"]):
                    readings.append(
                        Reading(
                            id=reading["id"],
                            kind=ReadingKind(reading["kind"]),
                            text=reading["text"],
                            model=reading["model"],
                            model_revision=reading["model_revision"],
                            confidence=reading["confidence"],
                            created_at=reading["created_at"],
                        )
                    )
                lines.append(
                    LineResult(
                        id=line["id"],
                        text=line["text"],
                        bbox=tuple(json.loads(line["bbox"])),
                        confidence=line["confidence"],
                        model=line["model"],
                        variant=line["variant"],
                        alternatives=alternatives,
                        manually_corrected=bool(line["manually_corrected"]),
                        region_id=line["region_id"],
                        baseline=json.loads(line["baseline"] or "[]"),
                        polygon=json.loads(line["polygon"] or "[]"),
                        readings=readings,
                        review_status=ReviewStatus(line["review_status"]),
                    )
                )
            regions = [
                RegionResult(
                    id=item["id"],
                    region_type=item["region_type"],
                    polygon=json.loads(item["polygon"]),
                    reading_order=item["reading_order"],
                )
                for item in self.rows(
                    "SELECT * FROM regions WHERE document_id=? AND page_index=? "
                    "ORDER BY reading_order",
                    (document_id, page_row["page_index"]),
                )
            ]
            engines = [
                EngineRun(**dict(item))
                for item in self.page_engine_runs(document_id, int(page_row["page_index"]))
            ]
            diagnostics_row = self.rows(
                "SELECT * FROM page_diagnostics WHERE document_id=? AND page_index=?",
                (document_id, page_row["page_index"]),
            )
            diagnostics = None
            if diagnostics_row:
                raw = diagnostics_row[0]
                diagnostics = ImageDiagnostics(
                    **{
                        key: raw[key]
                        for key in (
                            "brightness",
                            "contrast",
                            "sharpness",
                            "skew_degrees",
                            "clipped_dark",
                            "clipped_light",
                        )
                    }
                )
            pages.append(
                PageResult(
                    page_index=page_row["page_index"],
                    source_path=Path(page_row["source_path"]),
                    source_page_index=page_row["source_page_index"],
                    prepared_path=Path(page_row["prepared_path"])
                    if page_row["prepared_path"]
                    else None,
                    width=page_row["width"],
                    height=page_row["height"],
                    lines=lines,
                    mean_confidence=page_row["confidence"],
                    expected_cer=page_row["expected_cer"],
                    selected_variant=page_row["variant"],
                    selected_model=page_row["model"],
                    warnings=json.loads(page_row["warnings"] or "[]"),
                    logical_page_id=page_row["logical_page_id"],
                    source_bbox=tuple(json.loads(page_row["source_bbox"]))
                    if page_row["source_bbox"] and page_row["source_bbox"] != "null"
                    else None,
                    transform=json.loads(page_row["transform"] or "[]"),
                    profile=json.loads(page_row["profile"] or "{}"),
                    regions=regions,
                    engine_runs=engines,
                    image_diagnostics=diagnostics,
                )
            )
        source_paths = [Path(value) for value in json.loads(document["source_paths"] or "[]")]
        return DocumentResult(
            document=SourceDocument(
                id=document_id,
                title=document["title"],
                source_paths=source_paths,
                kind=document["kind"],
                page_count=len(pages),
            ),
            year=document["year"],
            script_hint=ScriptHint(document["script_hint"]),
            pages=pages,
            output_dir=Path(document["output_dir"]) if document["output_dir"] else None,
        )

    def import_pagexml_corrections(self, document_id: str, paths: Iterable[Path]) -> int:
        from schriftlotse.pagexml import parse_text_by_id

        known = {
            row["id"]: row["text"]
            for row in self.rows("SELECT id,text FROM lines WHERE document_id=?", (document_id,))
        }
        corrections: dict[str, str] = {}
        for path in paths:
            corrections.update(parse_text_by_id(path))
        changed = 0
        for line_id, text in corrections.items():
            if line_id in known and text and text != known[line_id]:
                self.update_line(line_id, text)
                changed += 1
        return changed
