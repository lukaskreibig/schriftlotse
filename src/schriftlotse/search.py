from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from rapidfuzz import fuzz

from schriftlotse.database import Database
from schriftlotse.domain import SearchHit, SearchMode, SearchQuery
from schriftlotse.model_registry import MODELS, ModelManager

WORD_RE = re.compile(r"[\wÄÖÜäöüßſ-]+", re.UNICODE)
OCR_TRANSLATION = str.maketrans({"ſ": "s", "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ꝛ": "r"})

# Embeddings help with open-ended language, while these small transparent
# archival concept families reliably bridge the vocabulary most often used in
# genealogical research and historical records. Every expansion remains visible
# as a search reason; it never changes a transcription.
ARCHIVAL_CONCEPTS: tuple[frozenset[str], ...] = (
    frozenset(
        {
            "eheschliessung",
            "heirat",
            "trauung",
            "vermaehlung",
            "verehelicht",
            "verheiratet",
            "eheleute",
            "ehefrau",
            "ehemann",
        }
    ),
    frozenset({"geburt", "geboren", "entbindung", "taufe", "getauft", "taeufling"}),
    frozenset(
        {
            "tod",
            "gestorben",
            "verstorben",
            "sterbefall",
            "sterbeurkunde",
            "totenschein",
            "beerdigung",
            "bestattung",
            "begraben",
        }
    ),
    frozenset({"wohnort", "wohnhaft", "wohnte", "ansaessig", "adresse", "domizil"}),
    frozenset({"beruf", "profession", "gewerbe", "stand", "beschaeftigung", "taetig"}),
    frozenset({"auswanderung", "ausgewandert", "emigration", "emigriert", "ausreise"}),
)


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFC", text).translate(OCR_TRANSLATION).casefold()
    value = value.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    value = re.sub(r"[^\w\s-]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def koelner_phonetik(text: str) -> str:
    word = normalize_text(text).upper().replace(" ", "")
    if not word:
        return ""
    result: list[str] = []
    for index, character in enumerate(word):
        previous = word[index - 1] if index else ""
        following = word[index + 1] if index + 1 < len(word) else ""
        code = ""
        if character in "AEIJOUY":
            code = "0"
        elif character == "H":
            code = ""
        elif character == "B" or (character == "P" and following != "H"):
            code = "1"
        elif character in "DT" and following not in "CSZ":
            code = "2"
        elif character in "FVW" or (character == "P" and following == "H"):
            code = "3"
        elif character in "GKQ":
            code = "4"
        elif character == "C":
            if index == 0 and following in "AHKLOQRUX":
                code = "4"
            elif previous in "SZ" or following not in "AHKOQUX":
                code = "8"
            else:
                code = "4"
        elif character == "X":
            code = "8" if previous in "CKQ" else "48"
        elif character == "L":
            code = "5"
        elif character in "MN":
            code = "6"
        elif character == "R":
            code = "7"
        elif character in "SZ":
            code = "8"
        for digit in code:
            if not result or result[-1] != digit:
                result.append(digit)
    if result and result[0] == "0":
        return "0" + "".join(digit for digit in result[1:] if digit != "0")
    return "".join(digit for digit in result if digit != "0")


def _fts_query(text: str, exact: bool) -> str:
    terms = [term.replace('"', '""') for term in normalize_text(text).split() if term]
    if not terms:
        return '""'
    if exact:
        return '"' + " ".join(terms) + '"'
    return " OR ".join(f'"{term}"*' for term in terms)


def _archival_expansion(text: str) -> set[str]:
    terms = set(normalize_text(text).split())
    expanded: set[str] = set()
    for family in ARCHIVAL_CONCEPTS:
        if terms & family:
            expanded.update(family)
    return expanded - terms


def _windowed_similarity(variants: set[str], candidate: str) -> float:
    """Compares terms/windows without letting one-character OCR debris score 100%."""
    candidate_words = WORD_RE.findall(candidate)
    if not candidate_words:
        return 0.0
    best = 0.0
    for variant in variants:
        query_words = WORD_RE.findall(variant)
        if not query_words:
            continue
        if len(query_words) == 1:
            query_word = query_words[0]
            minimum = max(2, len(query_word) // 2)
            plausible = [word for word in candidate_words if len(word) >= minimum]
            if plausible:
                best = max(best, max(fuzz.ratio(query_word, word) for word in plausible))
            continue
        for window_size in range(
            max(1, len(query_words) - 1),
            min(len(candidate_words), len(query_words) + 1) + 1,
        ):
            for index in range(len(candidate_words) - window_size + 1):
                window = " ".join(candidate_words[index : index + window_size])
                best = max(best, fuzz.ratio(variant, window))
    return best


class SemanticSearch:
    def __init__(self, database: Database, manager: ModelManager) -> None:
        self.database = database
        self.manager = manager
        self.model: Any | None = None
        model_revision = MODELS["qwen-embed"].revision or "unknown"
        # A historical OCR line is often just "seiner Ehefrau" or half of a
        # hyphenated sentence. Embed the neighbouring lines as retrieval context
        # while the hit still points to the precise centre line.
        self.revision = f"{model_revision}:three-line-context-v1"

    @property
    def available(self) -> bool:
        return self.manager.is_installed("qwen-embed")

    def _load(self) -> Any:
        if self.model is None:
            try:
                import torch
                from sentence_transformers import SentenceTransformer
            except ImportError as error:
                raise RuntimeError("Semantische Modellabhängigkeiten fehlen") from error
            device = "mps" if torch.backends.mps.is_available() else "cpu"
            self.model = SentenceTransformer(
                str(self.manager.path_for("qwen-embed")), device=device, local_files_only=True
            )
        return self.model

    def index_missing(self, batch_size: int = 32) -> int:
        if not self.available:
            return 0
        rows = self.database.rows(
            """
            WITH contextual_lines AS (
                SELECT
                    id,
                    text,
                    trim(
                        coalesce(lag(text) OVER page_lines, '') || char(10) ||
                        text || char(10) ||
                        coalesce(lead(text) OVER page_lines, '')
                    ) AS context
                FROM lines
                WINDOW page_lines AS (
                    PARTITION BY document_id, page_index ORDER BY line_order
                )
            )
            SELECT l.id, l.context
            FROM contextual_lines l
            LEFT JOIN embeddings e ON e.line_id=l.id AND e.model_revision=?
            WHERE e.line_id IS NULL AND length(l.text) > 2
            """,
            (self.revision,),
        )
        if not rows:
            return 0
        model = self._load()
        texts = [row["context"] for row in rows]
        vectors = model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        with self.database.connect() as db:
            for row, vector in zip(rows, vectors, strict=True):
                array = np.asarray(vector, dtype=np.float32)
                db.execute(
                    "INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?)",
                    (row["id"], self.revision, array.size, array.tobytes()),
                )
        return len(rows)

    def query(self, text: str, limit: int = 100) -> list[tuple[str, float]]:
        if not self.available:
            return []
        self.index_missing()
        model = self._load()
        vector = np.asarray(
            model.encode(
                [text],
                prompt_name="query",
                normalize_embeddings=True,
                show_progress_bar=False,
            )[0],
            dtype=np.float32,
        )
        rows = self.database.rows(
            "SELECT e.line_id,e.dimensions,e.vector,l.normalized FROM embeddings e "
            "JOIN lines l ON l.id=e.line_id WHERE e.model_revision=?",
            (self.revision,),
        )
        scores: list[tuple[str, float]] = []
        for row in rows:
            # Tiny OCR fragments and table labels frequently produce deceptively
            # strong embedding scores while carrying too little searchable meaning.
            if len(str(row["normalized"]).replace(" ", "")) < 5:
                continue
            candidate = np.frombuffer(row["vector"], dtype=np.float32, count=row["dimensions"])
            if candidate.size == vector.size:
                scores.append((row["line_id"], float(candidate @ vector)))
        return sorted(scores, key=lambda item: item[1], reverse=True)[:limit]


class ArchiveSearch:
    def __init__(self, database: Database, manager: ModelManager | None = None) -> None:
        self.database = database
        self.semantic = SemanticSearch(database, manager) if manager else None

    def search(self, query: SearchQuery) -> list[SearchHit]:
        lexical = self._lexical(query)
        fuzzy = self._fuzzy(query) if query.mode in {SearchMode.SMART, SearchMode.NAME} else []
        semantic: list[tuple[str, float]] = []
        if query.mode in {SearchMode.SMART, SearchMode.SEMANTIC} and self.semantic is not None:
            try:
                semantic = self.semantic.query(query.text, max(100, query.limit * 2))
            except RuntimeError:
                semantic = []
        fused = self._fuse(lexical, fuzzy, semantic, query)
        return fused[: query.limit]

    def _base_row(self, line_id: str) -> Any | None:
        return self.database.line_context(line_id)

    def _lexical(self, query: SearchQuery) -> list[tuple[str, float, str, str]]:
        expansions = (
            _archival_expansion(query.text)
            if query.mode in {SearchMode.SMART, SearchMode.SEMANTIC}
            else set()
        )
        fts_text = " ".join([query.text, *sorted(expansions)])
        fts = _fts_query(fts_text, query.mode == SearchMode.EXACT)
        sql = """
            SELECT line_id, bm25(lines_fts, 0.0, 1.0, 0.5) AS rank
            FROM lines_fts WHERE lines_fts MATCH ? ORDER BY rank LIMIT ?
        """
        limit = max(200, query.limit * 4)
        try:
            rows = self.database.rows(sql, (fts, limit))
            reading_rows = self.database.rows(
                "SELECT line_id, kind, text, bm25(readings_fts,0.0,0.0,0.0,1.0,0.5) "
                "AS rank FROM readings_fts WHERE readings_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts, limit),
            )
        except Exception:
            rows, reading_rows = [], []
        results = [
            (
                row["line_id"],
                1.0 / (1.0 + abs(float(row["rank"]))),
                (
                    "exakter Texttreffer"
                    if query.mode == SearchMode.EXACT
                    else "verwandter Archivbegriff"
                    if expansions
                    else "Volltexttreffer"
                ),
                query.text,
            )
            for row in rows
        ]
        results.extend(
            (
                row["line_id"],
                1.0 / (1.0 + abs(float(row["rank"]))) * 0.92,
                (
                    "alternative Modell-Lesung"
                    if row["kind"] not in {"bestaetigt", "konsens"}
                    else "verwandter Archivbegriff"
                    if expansions
                    else "Volltexttreffer"
                ),
                row["text"],
            )
            for row in reading_rows
        )
        return results

    def _fuzzy(self, query: SearchQuery) -> list[tuple[str, float, str, str]]:
        normalized = normalize_text(query.text)
        if not normalized:
            return []
        query_phonetics = (
            [koelner_phonetik(word) for word in WORD_RE.findall(normalized)]
            if query.mode == SearchMode.NAME
            else []
        )
        aliases = self.database.rows(
            "SELECT canonical, alias FROM name_aliases WHERE canonical=? OR alias=?",
            (normalized, normalized),
        )
        variants = {normalized}
        for row in aliases:
            variants.update((row["canonical"], row["alias"]))
        # The trigram FTS index narrows spelling-tolerant candidates before
        # RapidFuzz/phonetics. This keeps a large archive responsive and avoids
        # the previous confidence-sorted 20,000-line blind spot.
        candidate_ids: list[str] = []
        compact_query = normalized.replace(" ", "")
        if len(compact_query) >= 3:
            try:
                trigram_query = " OR ".join(
                    f'"{compact_query[index : index + 3]}"'
                    for index in range(len(compact_query) - 2)
                )
                candidate_ids = [
                    row["line_id"]
                    for row in self.database.rows(
                        "SELECT line_id FROM lines_trigram WHERE normalized MATCH ? "
                        "ORDER BY bm25(lines_trigram) LIMIT ?",
                        (trigram_query, max(2000, query.limit * 80)),
                    )
                ]
            except Exception:
                candidate_ids = []
        if candidate_ids:
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = self.database.rows(
                "SELECT l.id, l.text, l.normalized, l.alternatives, "
                "group_concat(r.text, char(30)) AS readings "
                "FROM lines l LEFT JOIN readings r ON r.line_id=l.id "
                f"WHERE l.id IN ({placeholders}) GROUP BY l.id",
                tuple(candidate_ids),
            )
        else:
            rows = self.database.rows(
                "SELECT l.id, l.text, l.normalized, l.alternatives, "
                "group_concat(r.text, char(30)) AS readings "
                "FROM lines l LEFT JOIN readings r ON r.line_id=l.id "
                "GROUP BY l.id ORDER BY l.rowid DESC LIMIT 5000"
            )
        results: list[tuple[str, float, str, str]] = []
        threshold = max(45.0, query.fuzziness * 100)
        for row in rows:
            candidate_texts = [(row["normalized"], "ähnliche Schreibweise", row["text"])]
            for alternative in json.loads(row["alternatives"] or "[]"):
                alternative_text = str(alternative.get("text", ""))
                candidate_texts.append(
                    (
                        normalize_text(alternative_text),
                        "alternative OCR-Lesung",
                        alternative_text,
                    )
                )
            for reading in (row["readings"] or "").split(chr(30)):
                if reading:
                    candidate_texts.append(
                        (normalize_text(reading), "alternative Modell-Lesung", reading)
                    )
            best_score = 0.0
            best_reason = "ähnliche Schreibweise"
            best_form = row["text"]
            for candidate, reason, display_form in candidate_texts:
                score = _windowed_similarity(variants, candidate)
                if query.mode == SearchMode.NAME and query_phonetics:
                    query_words = WORD_RE.findall(normalized)
                    words = [
                        word
                        for word in WORD_RE.findall(candidate)
                        if any(
                            len(word) >= max(2, len(query_word) // 2)
                            and abs(len(word) - len(query_word)) <= max(3, len(query_word) // 2)
                            for query_word in query_words
                        )
                    ]
                    candidate_phonetics = {koelner_phonetik(word) for word in words}
                    matched_codes = sum(
                        bool(code) and code in candidate_phonetics for code in query_phonetics
                    )
                    if matched_codes == len(query_phonetics) and score >= 55.0:
                        score = max(score, 86.0)
                        reason = "phonetische Namensvariante"
                    elif matched_codes and len(query_phonetics) > 1 and score >= 50.0:
                        score = max(score, 68.0 + 12.0 * matched_codes / len(query_phonetics))
                if score > best_score:
                    best_score, best_reason, best_form = score, reason, display_form
            if best_score >= threshold:
                results.append((row["id"], best_score / 100.0, best_reason, best_form))
        return sorted(results, key=lambda item: item[1], reverse=True)[: max(200, query.limit * 4)]

    def _fuse(
        self,
        lexical: list[tuple[str, float, str, str]],
        fuzzy: list[tuple[str, float, str, str]],
        semantic: list[tuple[str, float]],
        query: SearchQuery,
    ) -> list[SearchHit]:
        scores: defaultdict[str, float] = defaultdict(float)
        details: dict[str, tuple[str, str]] = {}
        for weight, items in ((1.0, lexical), (0.72, fuzzy)):
            for rank, (line_id, raw_score, reason, matched) in enumerate(items):
                scores[line_id] += weight / (60 + rank) + raw_score * weight * 0.02
                if (
                    line_id not in details
                    or weight == 1.0
                    or (query.mode == SearchMode.NAME and reason == "phonetische Namensvariante")
                ):
                    details[line_id] = (reason, matched)
        for rank, (line_id, raw_score) in enumerate(semantic):
            if query.mode == SearchMode.SMART and line_id not in scores and raw_score < 0.62:
                continue
            if query.mode == SearchMode.SEMANTIC and raw_score < 0.42:
                continue
            scores[line_id] += 0.82 / (60 + rank) + max(0.0, raw_score) * 0.012
            details.setdefault(line_id, ("inhaltlich ähnlicher Begriff", query.text))
        hits: list[SearchHit] = []
        for line_id, score in scores.items():
            row = self._base_row(line_id)
            if row is None:
                continue
            if query.document_id and row["document_id"] != query.document_id:
                continue
            year = row["year"]
            if query.year_from is not None and (year is None or year < query.year_from):
                continue
            if query.year_to is not None and (year is None or year > query.year_to):
                continue
            reason, matched = details[line_id]
            hits.append(
                SearchHit(
                    line_id=line_id,
                    document_id=row["document_id"],
                    document_title=row["title"],
                    page_index=row["page_index"],
                    source_path=Path(row["source_path"]),
                    bbox=tuple(json.loads(row["bbox"])),
                    text=row["text"],
                    matched_form=matched,
                    reason=reason,
                    # Saturating calibration preserves useful differences at
                    # the top instead of turning unrelated evidence into 1.000.
                    score=1.0 - math.exp(-max(0.0, score) * 12.0),
                    confidence=row["confidence"],
                    year=year,
                )
            )
        return sorted(hits, key=lambda hit: (hit.score, hit.confidence), reverse=True)
