from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from schriftlotse.domain import (
    DocumentResult,
    EntityMention,
    JobStatus,
    LineResult,
    ReadingKind,
    ReviewStatus,
)

SCHEMA_VERSION = 5


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
            db.execute("DELETE FROM documents WHERE id=?", (document.id,))
            db.execute(
                """
                INSERT INTO documents(
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
                       p.prepared_path, p.source_bbox, p.transform, p.width, p.height
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
        return {key: int(row[key] or 0) for key in row}

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
        return self.rows("SELECT * FROM documents ORDER BY created_at DESC, title")

    def document(self, document_id: str) -> sqlite3.Row | None:
        rows = self.rows("SELECT * FROM documents WHERE id=?", (document_id,))
        return rows[0] if rows else None

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
