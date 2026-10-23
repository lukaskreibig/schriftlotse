from __future__ import annotations

import json

from schriftlotse.benchmarks import (
    edit_distance,
    evaluate_search,
    normalize_transcription,
    text_metrics,
)
from schriftlotse.database import Database
from schriftlotse.search import ArchiveSearch

from .test_search import stored_result


def test_cer_and_wer_use_known_reference() -> None:
    metrics = text_metrics([("Johann Schmidt", "Johan Schmitt")])

    assert edit_distance("abc", "adc") == 1
    assert metrics.character_errors == 2
    assert metrics.reference_characters == 14
    assert metrics.word_errors == 2
    assert normalize_transcription("groſſe\n  Sache") == "grosse Sache"


def test_search_qrels_report_recall_and_reciprocal_rank(app_paths, tmp_path) -> None:
    database = Database(app_paths.database)
    database.create_job("job")
    database.save_document("job", stored_result())
    qrels = tmp_path / "qrels.json"
    qrels.write_text(
        json.dumps(
            [
                {"query": "Schmitt", "mode": "namen", "relevant_line_ids": ["line-1"]},
                {"query": "Sorau", "mode": "exakt", "relevant_line_ids": ["line-3"]},
            ]
        ),
        encoding="utf-8",
    )

    report = evaluate_search(ArchiveSearch(database), qrels, limit=5)

    assert report["recall@5"] == 1.0
    assert report["mrr"] == 1.0
