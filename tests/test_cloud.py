from __future__ import annotations

import json

from PIL import Image

from schriftlotse.cloud import OpenRouterReviewer
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
    }
