from __future__ import annotations

import threading
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from schriftlotse.app import ApplicationState, JobRuntime, UIController, create_app
from schriftlotse.config import AppPaths
from schriftlotse.domain import CloudPolicy, QualityProfile
from schriftlotse.ingest import discover_documents


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


def test_runtime_exposes_structured_low_overhead_live_preview(tmp_path) -> None:
    preview = tmp_path / "preview.jpg"
    preview.write_bytes(b"jpeg")
    runtime = JobRuntime("live-job", pipeline=object())  # type: ignore[arg-type]

    runtime.emit_live(
        {
            "stage": "ocr",
            "document_title": "Sterbeurkunde",
            "page_number": 2,
            "model": "TrOCR Kurrent",
            "preview_path": str(preview),
            "width": 1200,
            "height": 1800,
            "boxes": [[10, 20, 100, 40]],
        }
    )

    snapshot = runtime.snapshot()
    assert snapshot["live"]["preview_url"] == "/api/jobs/live-job/preview"
    assert snapshot["live"]["stage"] == "ocr"
    assert snapshot["live"]["boxes"] == [[10, 20, 100, 40]]
    assert "preview_path" not in snapshot["live"]
    assert snapshot["events"][0]["stage"] == "ocr"
    assert snapshot["events"][0]["model"] == "TrOCR Kurrent"


def test_local_health_probe() -> None:
    assert TestClient(create_app()).get("/api/health").json() == {
        "status": "bereit",
        "local": True,
        "version": "0.2.0",
    }


def test_native_health_probe_requires_current_instance_token(monkeypatch) -> None:
    monkeypatch.setenv("SCHRIFTLOTSE_INSTANCE_TOKEN", "native-health-token")
    client = TestClient(create_app())

    assert client.get("/api/health").status_code == 403
    accepted = client.get(
        "/api/health",
        headers={"x-schriftlotse-instance": "native-health-token"},
    )

    assert accepted.status_code == 200
    assert "instance_token" not in accepted.json()


def test_settings_api_persists_visible_configuration(monkeypatch, app_paths) -> None:
    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    state = ApplicationState()
    client = TestClient(create_app(state))
    custom_output = app_paths.output / "custom"
    custom_output.mkdir(parents=True)
    authorization = state.register_output_directory(custom_output)
    payload = {
        "advanced_models": False,
        "semantic_search": False,
        "cloud_budget_usd": 0.25,
        "output_dir": str(custom_output),
        "output_token": authorization["token"],
        "tesseract_command": "tesseract",
        "default_quality": "schnell",
        "default_script": "druck",
        "openrouter_profile": "balanced",
        "show_preprocessing": False,
    }

    saved = client.put("/api/settings", json=payload)

    assert saved.status_code == 200
    assert client.get("/api/settings").json()["default_script"] == "druck"
    assert client.get("/api/settings").json()["output_dir"] == str(custom_output.resolve())

    payload["output_dir"] = str(app_paths.data.parent / "untrusted")
    payload["output_token"] = None
    rejected = client.put("/api/settings", json=payload)
    assert rejected.status_code == 400
    assert "über „Auswählen“ freigeben" in rejected.json()["detail"]


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
    state.authorized_output_dirs = {}
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


def test_import_preview_keeps_loose_scans_separate(monkeypatch, app_paths) -> None:
    from PIL import Image

    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    state = ApplicationState()
    folder = app_paths.cache / "mixed"
    folder.mkdir(parents=True)
    for name in ("urkunde-seite1.jpg", "urkunde-seite2.jpg"):
        Image.new("RGB", (20, 20), "white").save(folder / name)
    source = state.register_source(folder)
    client = TestClient(create_app(state))

    separate = client.post(
        "/api/import-preview",
        json={"sources": [source["id"]], "group_images_by_folder": False},
    )
    grouped = client.post(
        "/api/import-preview",
        json={"sources": [source["id"]], "group_images_by_folder": True},
    )

    assert separate.status_code == 200
    assert separate.json()["document_count"] == 2
    assert grouped.json()["document_count"] == 1
    assert separate.json()["series_suggestions"][0]["pages"] == 2


def test_native_source_registration_requires_current_instance_token(
    monkeypatch, app_paths
) -> None:
    from PIL import Image

    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    monkeypatch.setenv("SCHRIFTLOTSE_INSTANCE_TOKEN", "native-test-token")
    scan = app_paths.cache / "Echter Dateiname.jpg"
    scan.parent.mkdir(parents=True)
    Image.new("RGB", (20, 20), "white").save(scan)
    client = TestClient(create_app(ApplicationState()))

    denied = client.post("/api/native-sources", json={"paths": [str(scan)]})
    accepted = client.post(
        "/api/native-sources",
        json={"paths": [str(scan)]},
        headers={"x-schriftlotse-instance": "native-test-token"},
    )

    assert denied.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["sources"][0]["name"] == "Echter Dateiname.jpg"

    outside_allowed_roots = client.post(
        "/api/native-sources",
        json={"paths": ["/etc/passwd"]},
        headers={"x-schriftlotse-instance": "native-test-token"},
    )
    assert outside_allowed_roots.status_code == 400


def test_folder_mapping_creates_nested_collections_and_linked_source(
    monkeypatch, app_paths
) -> None:
    from PIL import Image

    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    root = app_paths.cache / "Genealogische Funde"
    child = root / "Müller" / "Briefe"
    child.mkdir(parents=True)
    scan = child / "Brief 1891.jpg"
    Image.new("RGB", (20, 20), "white").save(scan)
    state = ApplicationState()
    document_id = discover_documents([root], group_images_by_folder=False)[0].id

    mapping, sources = state._prepare_folder_mappings(  # noqa: SLF001
        [root], group_images_by_folder=False
    )
    rows = [dict(row) for row in state.database.list_collections()]
    by_id = {row["id"]: row for row in rows}
    leaf = by_id[mapping[document_id][0]]

    assert leaf["name"] == "Briefe"
    assert by_id[leaf["parent_id"]]["name"] == "Müller"
    assert sources[document_id]["root"] == str(root.resolve())
    assert state.database.list_source_folders()[0]["label"] == "Genealogische Funde"


def test_adaptive_job_preserves_consent_budget_and_document_metadata(
    monkeypatch, app_paths
) -> None:
    from PIL import Image

    captured = []

    def fake_run(self, request, progress=None, job_id=None):
        captured.append(request)
        return job_id or "job", [], []

    monkeypatch.setattr(AppPaths, "default", classmethod(lambda _cls: app_paths))
    monkeypatch.setattr("schriftlotse.pipeline.ProcessingPipeline.run", fake_run)
    state = ApplicationState()
    scan = app_paths.cache / "scan.jpg"
    Image.new("RGB", (20, 20), "white").save(scan)
    source = state.register_source(scan)
    document_id = discover_documents([scan], group_images_by_folder=False)[0].id
    client = TestClient(create_app(state))

    response = client.post(
        "/api/jobs",
        json={
            "sources": [source["id"]],
            "quality": "beste_qualitaet",
            "cloud": True,
            "cloud_budget_usd": 0.5,
            "cloud_model_profile": "quality",
            "document_metadata": {
                document_id: {
                    "title": "Sterbeurkunde Hermann",
                    "year": 1891,
                    "script_hint": "handschrift",
                }
            },
        },
    )
    for _ in range(50):
        if captured:
            break
        time.sleep(0.01)

    assert response.status_code == 202
    assert captured[0].cloud_policy == CloudPolicy.ADAPTIVE
    assert captured[0].quality_profile == QualityProfile.ADAPTIVE
    assert captured[0].cloud_budget_usd == 0.5
    assert captured[0].document_metadata[document_id].year == 1891
