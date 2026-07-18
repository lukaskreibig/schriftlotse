from __future__ import annotations

import threading
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from schriftlotse.app import ApplicationState, JobRuntime, UIController, create_app
from schriftlotse.config import AppPaths


def test_visible_progress_status_contains_locality_and_percentage() -> None:
    status = UIController._progress_status(
        "Lokale OCR-/HTR-Modelle arbeiten", 0.42, time.monotonic() - 2
    )
    assert "42%" in status
    assert "Lokal auf diesem Mac" in status
    assert "OCR-/HTR-Modelle" in status


def test_runtime_snapshot_exposes_visible_local_progress() -> None:
    runtime = JobRuntime("job", pipeline=object())  # type: ignore[arg-type]
    runtime.emit("lokale OCR-/HTR-Modelle arbeiten", 0.42)
    snapshot = runtime.snapshot()
    assert snapshot["percent"] == 42
    assert snapshot["local"] is True
    assert "OCR-/HTR-Modelle" in snapshot["message"]


def test_runtime_progress_never_moves_backwards_and_has_eta() -> None:
    runtime = JobRuntime("test", pipeline=object())  # type: ignore[arg-type]
    runtime.started -= 100
    runtime.emit("erste Seite", 0.5)
    runtime.emit("Doppelseite erkannt", 0.4)
    snapshot = runtime.snapshot()
    assert snapshot["percent"] == 50
    assert 95 <= snapshot["estimated_remaining_seconds"] <= 105


def test_local_health_probe() -> None:
    route = next(
        route for route in create_app().routes if getattr(route, "path", "") == "/api/health"
    )
    assert route.endpoint() == {
        "status": "bereit",
        "local": True,
        "version": "0.2.0",
        "instance_token": "browser",
    }


def test_settings_api_persists_visible_configuration(monkeypatch, app_paths) -> None:
    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    client = TestClient(create_app())
    payload = {
        "advanced_models": False,
        "semantic_search": False,
        "cloud_budget_usd": 0.25,
        "output_dir": str(app_paths.output / "custom"),
        "tesseract_command": "tesseract",
        "default_quality": "schnell",
        "default_script": "druck",
        "openrouter_profile": "balanced",
        "show_preprocessing": False,
    }

    saved = client.put("/api/settings", json=payload)

    assert saved.status_code == 200
    assert client.get("/api/settings").json()["default_script"] == "druck"
    assert (app_paths.output / "custom").is_dir()


def test_upload_keeps_original_filename_for_document_title(monkeypatch, app_paths) -> None:
    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    state = ApplicationState()
    client = TestClient(create_app(state))

    response = client.post(
        "/api/uploads",
        files={"files": ("Hermann Sterbeurkunde.jpg", b"image", "image/jpeg")},
    )

    assert response.status_code == 200
    token = response.json()["sources"][0]["id"]
    assert state.authorized_sources[token].name == "Hermann Sterbeurkunde.jpg"


def test_sources_and_downloads_use_opaque_capability_tokens(tmp_path) -> None:
    state = ApplicationState.__new__(ApplicationState)
    state.lock = threading.Lock()
    state.authorized_sources = {}
    state.downloads = {}
    scan = tmp_path / "privater-scan.jpg"
    scan.write_bytes(b"scan")
    result = tmp_path / "schriftlotse.pdf"
    result.write_bytes(b"pdf")

    source = state.register_source(scan, "mein Scan.jpg")
    downloads = state.register_downloads([result])

    assert source["name"] == "mein Scan.jpg"
    assert str(scan) not in source.values()
    assert state.authorized_sources[source["id"]] == scan.resolve()
    assert downloads[0]["name"] == "schriftlotse.pdf"
    assert state.download(downloads[0]["id"]) == result.resolve()
    with pytest.raises(HTTPException):
        state.download(str(result))
