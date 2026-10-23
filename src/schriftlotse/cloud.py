from __future__ import annotations

import base64
import io
import json
import os
import re
from contextlib import suppress
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
    zdr: bool = True
    experimental: bool = False
    best_for: str = "Einzelne schwierige Textstellen"


# Verified against OpenRouter's live model catalog on 2026-07-18.  These are
# deliberately explicit rather than floating aliases, so a saved reading stays
# reproducible.  No vendor has a public benchmark for this exact Kurrent corpus;
# the labels therefore describe the intended operating point, not a guarantee.
CLOUD_MODEL_OPTIONS: dict[str, CloudModelOption] = {
    "auto": CloudModelOption(
        key="auto",
        model="layoutabhängige Auswahl",
        label="Automatisch · nach Dokumenttyp",
        description=(
            "Wählt Gemini für erkannte Formulare/Tabellen und Sonnet für schwierige Textzeilen."
        ),
        price_hint="Kosten richten sich nach dem ausgewählten Modell",
        best_for="Unbekannte oder gemischte Dokumenttypen",
    ),
    "fast": CloudModelOption(
        key="fast",
        model="google/gemini-3.5-flash",
        label="Gemini 3.5 Flash · Formulare & Seiten",
        description="Schnell und im Test stark bei Formularen und vollständigen Seiten.",
        price_hint="$1,50 Eingabe / $9 Ausgabe je 1 Mio. Token",
        best_for="Formulare, Tabellen und gut strukturierte Seiten",
    ),
    "balanced": CloudModelOption(
        key="balanced",
        model="openai/gpt-5.6-luna",
        label="GPT-5.6 Luna · ausgewogen",
        description="Schnelle, kostengünstige hochwertige Vergleichslesung.",
        price_hint="$1 Eingabe / $6 Ausgabe je 1 Mio. Token",
        zdr=False,
        experimental=True,
        best_for="Experimentelle Vergleichslesung; nicht für sensible Quellen",
    ),
    "ocr_value": CloudModelOption(
        key="ocr_value",
        model="qwen/qwen3-vl-235b-a22b-instruct",
        label="OCR-Preis/Leistung",
        description="Großes offenes Visionmodell mit Dokument- und Tabellenfokus.",
        price_hint="ab $0,21 Eingabe / $1,90 Ausgabe je 1 Mio. Token",
        experimental=True,
        best_for="Preiswerte experimentelle Gegenprobe",
    ),
    "quality": CloudModelOption(
        key="quality",
        model="anthropic/claude-sonnet-5",
        label="Claude Sonnet 5 · Textstellen (empfohlen)",
        description="Im Goldstandard-Test die zuverlässigste Cloud-Zweitlesung.",
        price_hint="$2 Eingabe / $10 Ausgabe je 1 Mio. Token",
        recommended=True,
        provider_sort="throughput",
        best_for="Schwierige Zeilen und konservative, originalgetreue Lesungen",
    ),
}


def resolve_cloud_profile(profile: str, layout: str | None = None) -> str:
    """Resolve only the explicit automatic option; named models are never overridden."""
    if profile != "auto":
        return profile
    return "fast" if layout in {"formular", "tabelle"} else "quality"


def cloud_model_status() -> list[dict[str, Any]]:
    return [asdict(option) for option in CLOUD_MODEL_OPTIONS.values()]


@dataclass(slots=True)
class CloudReview:
    text: str
    confidence: float
    model: str
    cost: float
    notes: str = ""


def cloud_transcription_issues(
    text: str,
    local_text: str,
    *,
    single_line: bool = True,
) -> list[str]:
    """Return transparent format warnings without attempting to rewrite model output."""
    issues: list[str] = []
    reasoning = re.compile(
        r"(?:^|\n)\s*(?:wait[,.:]|let(?:'s| us)\b|i (?:see|think|need|will)\b|"
        r"the (?:image|text|line)\b|analysis\s*:|überlegung\s*:)",
        re.IGNORECASE,
    )
    if reasoning.search(text):
        issues.append("enthält wahrscheinlich Modell-Erklärung statt nur Abschrift")
    nonempty_lines = [line for line in text.splitlines() if line.strip()]
    if single_line and len(nonempty_lines) > 1:
        issues.append("enthält mehrere Zeilen für einen Einzelzeilen-Ausschnitt")
    plausible_limit = max(
        240 if single_line else 2_000,
        len(local_text) * (4 if single_line else 8),
    )
    if len(text) > plausible_limit:
        issues.append("ist für den geprüften Ausschnitt unplausibel lang")
    return issues


def cloud_line_crop_bounds(
    bbox: tuple[int, int, int, int] | list[int],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    """Keep ascenders/descenders while excluding neighbouring text lines."""
    x1, y1, x2, y2 = (int(value) for value in bbox)
    line_height = max(1, y2 - y1)
    horizontal = max(10, round(line_height * 0.4))
    vertical = max(4, round(line_height * 0.18))
    return (
        max(0, x1 - horizontal),
        max(0, y1 - vertical),
        min(width, x2 + horizontal),
        min(height, y2 + vertical),
    )


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

    @staticmethod
    def delete_api_key() -> None:
        with suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password("SchriftLotse", "openrouter")

    def available(self) -> bool:
        return bool(self.api_key)

    def key_status(self, *, validate: bool = False) -> dict[str, Any]:
        status: dict[str, Any] = {"configured": self.available(), "validated": False}
        if not self.api_key or not validate:
            return status
        response = httpx.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        status.update(
            {
                "validated": True,
                "label": data.get("label"),
                "limit": data.get("limit"),
                "limit_remaining": data.get("limit_remaining"),
                "usage": data.get("usage"),
                "is_free_tier": bool(data.get("is_free_tier", False)),
            }
        )
        return status

    @staticmethod
    def option(profile: str) -> CloudModelOption:
        try:
            return CLOUD_MODEL_OPTIONS[profile]
        except KeyError as error:
            raise ValueError("Unbekanntes OpenRouter-Modellprofil") from error

    def choose_model(self, script_hint: ScriptHint) -> str:
        del script_hint
        return CLOUD_MODEL_OPTIONS["quality"].model

    @staticmethod
    def _data_url(image: Image.Image) -> str:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=92, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    @staticmethod
    def _clean_transcription(
        content: str,
        local_text: str,
        *,
        single_line: bool = True,
    ) -> str:
        text = content.strip()
        text = re.sub(r"<analysis>.*?</analysis>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = text.strip()
        text = text.removeprefix("```text").removeprefix("```").removesuffix("```").strip()
        text = re.sub(
            r"^(?:Transkription|Antwort|Ergebnis)\s*:\s*",
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        if re.match(r"^(?:I cannot|I can't|Es tut mir leid|Ich kann)", text, re.IGNORECASE):
            raise RuntimeError("Das Cloud-Modell hat die Transkription abgelehnt")
        issues = cloud_transcription_issues(text, local_text, single_line=single_line)
        if issues:
            raise RuntimeError(f"Cloud-Ausgabe verworfen: {'; '.join(issues)}")
        return text

    def review(
        self,
        original: Image.Image,
        optimized: Image.Image,
        local_text: str,
        year: int | None,
        script_hint: ScriptHint,
        model: str | None = None,
        profile: str = "quality",
        single_line: bool = True,
    ) -> CloudReview:
        del optimized  # Kept in the API for callers; one image avoids model confusion.
        if not self.api_key:
            raise RuntimeError("Kein OpenRouter-Schlüssel im macOS-Schlüsselbund")
        if self.spent_usd >= self.budget_usd:
            raise RuntimeError("Cloud-Kostenlimit erreicht")
        option = self.option(profile)
        selected = model or option.model
        prompt = (
            "Du bist ein konservativer Transkriptor historischer deutscher Quellen. "
            "Gib ausschließlich die sichtbare Transkription zurück, ohne Einleitung, "
            "Erklärung, Markdown oder Modernisierung. Bewahre Zeilenumbrüche, historische "
            "Rechtschreibung, Großschreibung und Zeichensetzung. Dies ist eine diplomatische "
            "Zeichenabschrift: Korrigiere niemals Grammatik, Flexion, Bindestriche, Abkürzungen "
            "oder vermeintliche Schreibfehler. Schreibe nur Zeichen, die im Bild stehen. "
            "Erfinde oder ergänze nichts. "
            + (
                "Der Ausschnitt dient der Prüfung genau einer Zielzeile. Gib genau eine "
                "Textzeile zurück und ignoriere angeschnittene Nachbarzeilen. "
                if single_line
                else "Transkribiere den gesamten sichtbaren Ausschnitt in seiner Zeilenfolge. "
            )
            + "Unleserliches: ⟦unleserlich⟧; unsichere Lesung: ⟦Lesung?⟧. "
            f"Kontextjahr: {year or 'unbekannt'}; Schriftangabe: {script_hint.value}. "
            f"Eine lokale, unbestätigte Vorlesung lautet: {local_text or 'keine'}. "
            "Nutze sie nur als Hinweis und korrigiere sie anhand des Bildes."
        )
        payload = {
            "model": selected,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": self._data_url(original)}},
                    ],
                }
            ],
            "max_tokens": 300 if single_line else 1200,
            "provider": {
                "zdr": option.zdr,
                "data_collection": "deny",
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
        cost = float(body.get("usage", {}).get("cost", 0.0) or 0.0)
        if self.spent_usd + cost > self.budget_usd:
            raise RuntimeError("Antwort würde das Cloud-Kostenlimit überschreiten")
        self.spent_usd += cost
        content: Any = body["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        text = self._clean_transcription(
            str(content),
            local_text,
            single_line=single_line,
        )
        notes = ""
        confidence = 0.5
        # Backwards-compatible parsing for providers that still return a JSON
        # object despite the plain-text contract.
        candidate = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        if candidate.startswith("{"):
            try:
                parsed = json.loads(candidate)
                text = str(parsed.get("text", "")).strip()
                notes = str(parsed.get("notes", ""))
                confidence = max(0.0, min(float(parsed.get("confidence", 0.5)), 1.0))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if not text:
            raise RuntimeError("Das Cloud-Modell hat keine Transkription geliefert")
        return CloudReview(
            text=text,
            confidence=confidence,
            model=selected,
            cost=cost,
            notes=notes,
        )
