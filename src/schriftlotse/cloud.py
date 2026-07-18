from __future__ import annotations

import base64
import io
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

import httpx
import keyring
from PIL import Image

from schriftlotse.domain import ScriptHint


@dataclass(frozen=True, slots=True)
class CloudModelOption:
    key: str
    model: str
    label: str
    description: str
    price_hint: str
    recommended: bool = False
    provider_sort: str = "latency"


# Verified against OpenRouter's live model catalog on 2026-07-18.  These are
# deliberately explicit rather than floating aliases, so a saved reading stays
# reproducible.  No vendor has a public benchmark for this exact Kurrent corpus;
# the labels therefore describe the intended operating point, not a guarantee.
CLOUD_MODEL_OPTIONS: dict[str, CloudModelOption] = {
    "fast": CloudModelOption(
        key="fast",
        model="google/gemini-3.5-flash",
        label="Schnell & stark (empfohlen)",
        description="Sehr schnelles aktuelles Visionmodell für die tägliche Zweitprüfung.",
        price_hint="$1,50 Eingabe / $9 Ausgabe je 1 Mio. Token",
        recommended=True,
    ),
    "ocr_value": CloudModelOption(
        key="ocr_value",
        model="qwen/qwen3-vl-235b-a22b-instruct",
        label="OCR-Preis/Leistung",
        description="Großes offenes Visionmodell mit Dokument- und Tabellenfokus.",
        price_hint="ab $0,21 Eingabe / $1,90 Ausgabe je 1 Mio. Token",
    ),
    "quality": CloudModelOption(
        key="quality",
        model="anthropic/claude-opus-4.8",
        label="Maximale Zweitprüfung",
        description="Langsameres Spitzenmodell für besonders schwierige Einzelstellen.",
        price_hint="$5 Eingabe / $25 Ausgabe je 1 Mio. Token",
        provider_sort="throughput",
    ),
    "gpt": CloudModelOption(
        key="gpt",
        model="openai/gpt-5.5",
        label="GPT-Spitzenmodell",
        description="Alternative hochwertige Lesung zum direkten Modellvergleich.",
        price_hint="$5 Eingabe / $30 Ausgabe je 1 Mio. Token",
        provider_sort="throughput",
    ),
    "free": CloudModelOption(
        key="free",
        model="openrouter/free",
        label="Kostenloser Router (wechselnd)",
        description="Wählt ein verfügbares Gratis-Visionmodell; Qualität ist nicht reproduzierbar.",
        price_hint="kostenlos, Verfügbarkeit schwankt",
    ),
}


def cloud_model_status() -> list[dict[str, Any]]:
    return [asdict(option) for option in CLOUD_MODEL_OPTIONS.values()]


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

    @staticmethod
    def option(profile: str) -> CloudModelOption:
        try:
            return CLOUD_MODEL_OPTIONS[profile]
        except KeyError as error:
            raise ValueError("Unbekanntes OpenRouter-Modellprofil") from error

    def choose_model(self, script_hint: ScriptHint) -> str:
        del script_hint
        return CLOUD_MODEL_OPTIONS["fast"].model

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
        profile: str = "fast",
    ) -> CloudReview:
        if not self.api_key:
            raise RuntimeError("Kein OpenRouter-Schlüssel im macOS-Schlüsselbund")
        if self.spent_usd >= self.budget_usd:
            raise RuntimeError("Cloud-Kostenlimit erreicht")
        option = self.option(profile)
        selected = model or option.model
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
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
            "provider": {
                "zdr": True,
                "data_collection": "deny",
                "require_parameters": True,
                "sort": option.provider_sort,
            },
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
