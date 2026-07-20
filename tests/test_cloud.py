from __future__ import annotations

import json

import pytest
from PIL import Image

from schriftlotse.cloud import (
    CLOUD_MODEL_OPTIONS,
    OpenRouterReviewer,
    cloud_line_crop_bounds,
    cloud_transcription_issues,
    resolve_cloud_profile,
)
from schriftlotse.domain import ScriptHint


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"text": "Johann Schmidt", "confidence": 0.91, "notes": ""}
                        )
                    }
                }
            ],
            "usage": {"cost": 0.02},
        }


class FakeKeyResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "data": {
                "label": "SchriftLotse-Test",
                "limit": 2.0,
                "limit_remaining": 1.5,
                "usage": 0.5,
                "is_free_tier": False,
            }
        }


def test_openrouter_uses_privacy_flags(monkeypatch) -> None:
    captured = {}

    def fake_post(*args, **kwargs):
        captured.update(kwargs["json"])
        return FakeResponse()

    monkeypatch.setattr("schriftlotse.cloud.httpx.post", fake_post)
    reviewer = OpenRouterReviewer(api_key="test", budget_usd=1)
    image = Image.new("RGB", (20, 20), "white")
    result = reviewer.review(image, image, "Johann Schrnidt", 1872, ScriptHint.HANDWRITING)
    assert result.text == "Johann Schmidt"
    assert captured["provider"] == {
        "zdr": True,
        "data_collection": "deny",
        "sort": "throughput",
    }
    assert captured["model"] == "anthropic/claude-sonnet-5"
    assert captured["max_tokens"] == 300
    assert "genau einer Zielzeile" in captured["messages"][0]["content"][0]["text"]
    assert "response_format" not in captured
    assert "temperature" not in captured


def test_cloud_profiles_have_unique_current_model_ids() -> None:
    model_ids = [option.model for option in CLOUD_MODEL_OPTIONS.values()]
    assert len(model_ids) == len(set(model_ids))
    assert CLOUD_MODEL_OPTIONS["quality"].provider_sort == "throughput"
    assert CLOUD_MODEL_OPTIONS["balanced"].model == "openai/gpt-5.6-luna"
    assert CLOUD_MODEL_OPTIONS["quality"].model == "anthropic/claude-sonnet-5"
    assert CLOUD_MODEL_OPTIONS["quality"].recommended is True
    assert CLOUD_MODEL_OPTIONS["balanced"].zdr is False
    assert resolve_cloud_profile("quality", "formular") == "quality"
    assert resolve_cloud_profile("auto", "formular") == "fast"
    assert resolve_cloud_profile("auto", "fliesstext") == "quality"


def test_openrouter_key_can_be_validated_without_inference(monkeypatch) -> None:
    monkeypatch.setattr("schriftlotse.cloud.httpx.get", lambda *args, **kwargs: FakeKeyResponse())

    status = OpenRouterReviewer(api_key="test").key_status(validate=True)

    assert status["configured"] is True
    assert status["validated"] is True
    assert status["label"] == "SchriftLotse-Test"
    assert status["limit_remaining"] == 1.5


def test_cloud_format_gate_removes_reasoning_without_rewriting_transcription() -> None:
    cleaned = OpenRouterReviewer._clean_transcription(
        "<analysis>Ich überlege.</analysis>\n```text\nTranskription: Johann Schmidt\n```",
        "Johann Schrnidt",
    )
    assert cleaned == "Johann Schmidt"


def test_cloud_format_gate_rejects_reasoning_and_multiline_leakage() -> None:
    bad = (
        "Wait, is C. and No. 366 on the same line? Let's represent it.\n"
        "Sorau am 18. Dezember 1893.\nVor dem Standesbeamten erschien"
    )

    issues = cloud_transcription_issues(bad, "No. 366")

    assert any("Modell-Erklärung" in issue for issue in issues)
    assert any("mehrere Zeilen" in issue for issue in issues)
    with pytest.raises(RuntimeError, match="Cloud-Ausgabe verworfen"):
        OpenRouterReviewer._clean_transcription(bad, "No. 366")


def test_cloud_line_crop_uses_tight_vertical_and_wider_horizontal_context() -> None:
    assert cloud_line_crop_bounds((100, 200, 500, 240), 800, 1000) == (84, 193, 516, 247)
