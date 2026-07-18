from __future__ import annotations

import json

from PIL import Image

from schriftlotse.cloud import CLOUD_MODEL_OPTIONS, OpenRouterReviewer
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
        "require_parameters": True,
        "sort": "latency",
    }
    assert captured["model"] == "google/gemini-3.5-flash"
    assert captured["max_tokens"] == 1200


def test_cloud_profiles_have_unique_current_model_ids() -> None:
    model_ids = [option.model for option in CLOUD_MODEL_OPTIONS.values()]
    assert len(model_ids) == len(set(model_ids))
    assert CLOUD_MODEL_OPTIONS["quality"].provider_sort == "throughput"
    assert CLOUD_MODEL_OPTIONS["balanced"].model == "openai/gpt-5.6-luna"
    assert CLOUD_MODEL_OPTIONS["quality"].model == "anthropic/claude-sonnet-5"


def test_openrouter_key_can_be_validated_without_inference(monkeypatch) -> None:
    monkeypatch.setattr("schriftlotse.cloud.httpx.get", lambda *args, **kwargs: FakeKeyResponse())

    status = OpenRouterReviewer(api_key="test").key_status(validate=True)

    assert status["configured"] is True
    assert status["validated"] is True
    assert status["label"] == "SchriftLotse-Test"
    assert status["limit_remaining"] == 1.5
