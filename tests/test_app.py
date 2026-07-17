from __future__ import annotations

import time
from types import SimpleNamespace

from schriftlotse.app import UIController


def test_visible_progress_status_contains_locality_and_percentage() -> None:
    status = UIController._progress_status(
        "Lokale OCR-/HTR-Modelle arbeiten", 0.42, time.monotonic() - 2
    )
    assert "42%" in status
    assert "Lokal auf diesem Mac" in status
    assert "OCR-/HTR-Modelle" in status


def test_process_streams_visible_updates_before_files(monkeypatch, tmp_path) -> None:
    export = tmp_path / "schriftlotse.pdf"
    export.write_bytes(b"pdf")

    class FakePipeline:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self, request, progress):
            progress("Scan wird analysiert", 0.1)
            progress("lokale OCR-/HTR-Modelle arbeiten", 0.4)
            result = SimpleNamespace(
                document=SimpleNamespace(title="Testscan"),
                pages=[SimpleNamespace(expected_cer=0.05)],
                output_dir=tmp_path,
            )
            return "12345678job", [result], [export]

    monkeypatch.setattr("schriftlotse.app.ProcessingPipeline", FakePipeline)
    controller = UIController.__new__(UIController)
    controller.paths = None
    controller.settings = None
    controller.database = None
    updates = list(
        controller.process(
            [str(tmp_path / "scan.png")],
            "",
            1872,
            "Druck/Fraktur",
            False,
            False,
            0,
        )
    )
    assert any("OCR-/HTR-Modelle arbeiten" in status for status, _files in updates)
    assert updates[-1][1] == [str(export)]
    assert "Alle Verarbeitungsschritte liefen lokal" in updates[-1][0]
