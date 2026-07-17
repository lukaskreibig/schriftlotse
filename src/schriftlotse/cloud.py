from __future__ import annotations

import base64
import io
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
import keyring
from PIL import Image

from schriftlotse.domain import ScriptHint

MODEL_BY_TASK = {
    "print": "google/gemini-3.5-flash",
    "handwriting": "anthropic/claude-opus-4.8",
    "premium": "openai/gpt-5.5",
    "economy": "qwen/qwen3-vl-235b-a22b-instruct",
    "free": "google/gemma-4-31b-it:free",
}


@dataclass(slots=True)
class CloudReview:
    text: str
    confidence: float
    model: str
    cost: float
    notes: str = ""


class OpenRouterReviewer:
    def __init__(self, budget_usd: float = 1.0, api_key: str | None = None) -> None:
        self.api_key = (
            api_key
            or os.getenv("OPENROUTER_API_KEY")
            or keyring.get_password("SchriftLotse", "openrouter")
        )
        self.budget_usd = budget_usd
        self.spent_usd = 0.0

    @staticmethod
    def save_api_key(api_key: str) -> None:
        keyring.set_password("SchriftLotse", "openrouter", api_key.strip())

    def available(self) -> bool:
        return bool(self.api_key)

    def choose_model(self, script_hint: ScriptHint) -> str:
        if script_hint in {ScriptHint.HANDWRITING, ScriptHint.AUTO}:
            return MODEL_BY_TASK["handwriting"]
        return MODEL_BY_TASK["print"]

    @staticmethod
    def _data_url(image: Image.Image) -> str:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=92, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    def review(
        self,
        original: Image.Image,
        optimized: Image.Image,
        local_text: str,
        year: int | None,
        script_hint: ScriptHint,
        model: str | None = None,
    ) -> CloudReview:
        if not self.api_key:
            raise RuntimeError("Kein OpenRouter-Schlüssel im macOS-Schlüsselbund")
        if self.spent_usd >= self.budget_usd:
            raise RuntimeError("Cloud-Kostenlimit erreicht")
        selected = model or self.choose_model(script_hint)
        prompt = (
            "Transkribiere den deutschsprachigen historischen Text originalgetreu. "
            "Bewahre historische Rechtschreibung, Großschreibung und Zeichensetzung. "
            "Erfinde nichts. Markiere unleserliche Stellen als ⟦unleserlich⟧ und unsichere "
            "Lesungen als ⟦Lesung?⟧. Antworte ausschließlich als JSON mit den Feldern "
            '"text", "confidence" (0 bis 1) und "notes". '
            f"Angegebenes Jahr: {year or 'unbekannt'}. Lokale Vorlesung: {local_text}"
        )
        payload = {
            "model": selected,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": self._data_url(original)}},
                        {"type": "image_url", "image_url": {"url": self._data_url(optimized)}},
                    ],
                }
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "provider": {"zdr": True, "data_collection": "deny", "require_parameters": True},
            "usage": {"include": True},
        }
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/lukaskreibig/schriftlotse",
                "X-Title": "SchriftLotse",
            },
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        body = response.json()
        content: Any = body["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        parsed = json.loads(str(content))
        cost = float(body.get("usage", {}).get("cost", 0.0) or 0.0)
        if self.spent_usd + cost > self.budget_usd:
            raise RuntimeError("Antwort würde das Cloud-Kostenlimit überschreiten")
        self.spent_usd += cost
        return CloudReview(
            text=str(parsed.get("text", "")).strip(),
            confidence=max(0.0, min(float(parsed.get("confidence", 0.5)), 1.0)),
            model=selected,
            cost=cost,
            notes=str(parsed.get("notes", "")),
        )
