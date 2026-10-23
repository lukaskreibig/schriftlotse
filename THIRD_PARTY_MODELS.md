# Freie Modelle und externe Dienste

Modellgewichte werden nicht mit SchriftLotse verteilt. Der Modellmanager lädt ausschließlich
die hier dokumentierten Quellen und fixierten Revisionen in den lokalen Benutzer-Cache.

| Schlüssel | Modell | Lizenz | Fixierung | Einsatz |
|---|---|---|---|---|
| `party-v4` | [Party v4](https://zenodo.org/records/20642057) | Apache-2.0 | MD5 `cf165e67061d492b72f600a6a72b7c61` | allgemeine CPU-Ganzseiten-Zweitlesung |
| Kraken BLLA | mit Kraken ausgeliefert | Apache-2.0 | durch `uv.lock` fixiert | lokale Standard-Zeilenerkennung |
| `orli` | [Orli](https://zenodo.org/records/20558179) | Apache-2.0 | MD5 `a9a6b0caf497203e758dbd4fc624af10` | experimentelle alternative Grundlinienerkennung |
| `trocr-kurrent-19` | [TrOCR Kurrent](https://huggingface.co/dh-unibe/trocr-kurrent) | MIT | Commit `dd026dc6…` | Kurrent 19. Jh. |
| TrOCR-Prozessor | [Microsoft TrOCR Base Handwritten](https://huggingface.co/microsoft/trocr-base-handwritten) | MIT | Commit `eaacaf45…` | vollständiger lokaler Bildprozessor und Tokenizer |
| `trocr-kurrent-early` | [TrOCR Kurrent XVI–XVII](https://huggingface.co/dh-unibe/trocr-kurrent-XVI-XVII) | MIT | Commit `eaedace4…` | Kurrent 16.–18. Jh. |
| `trocr-modern` | [TrOCR German Handwritten](https://huggingface.co/fhswf/TrOCR_german_handwritten) | AFL-3.0 | Commit `f43d8831…` | neuere lateinische Handschrift |
| `trocr-medieval` | [TrOCR Medieval](https://huggingface.co/dh-unibe/trocr-medieval-escriptmask) | MIT | Commit `bd7124a3…` | experimentell vor 1500 |
| `ub-german-handwriting` | [UB Mannheim German Handwriting](https://zenodo.org/records/7933463) | CC-BY-SA-4.0 | MD5 `6c41ae2c…` | kleiner allgemeiner Kraken-Zweitleser |
| `churro-mlx-8bit` | [Stanford CHURRO 3B](https://huggingface.co/stanford-oval/churro-3B) | Qwen Research License | Commit `ca2150ea…`, lokal aus Originalgewichten in MLX 8-Bit konvertiert | Standard-Ganzseiten-Zweitleser im Qualitätsprofil |
| `qwen-embed` | [Qwen3 Embedding 0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | Apache-2.0 | Commit `97b0c614…` | semantische Archivsuche |

Tesseract ist Apache-2.0. Deutsche `tessdata`-Sprachmodelle und das CC0-Modell `frak2021`
behalten ihre jeweiligen eigenen Lizenzen. Abhängigkeiten `party` und `orli` sind im
Python-Projekt auf konkrete Git-Commits fixiert.

CHURRO wird nicht im lizenzklaren Profil geladen und verlangt vor Download eine ausdrückliche
Bestätigung. Die Modellgewichte und der CHURRO-Datensatz sind nicht mit der Apache-Lizenz des
Anwendungscodes gleichzusetzen. OpenRouter ist ein optionaler Netzwerkdienst und kein
eingebettetes Modell.

## Optionale OpenRouter-Modelle

Die folgenden IDs wurden am 18. Juli 2026 im Live-Katalog als bildfähige Modelle bestätigt.
Sie werden niemals automatisch auf einen Stapel angewendet:

| Profil | Modell-ID | Zweck |
|---|---|---|
| Schnell & stark | `google/gemini-3.5-flash` | empfohlene alltägliche Einzelprüfung |
| OCR-Preis/Leistung | `qwen/qwen3-vl-235b-a22b-instruct` | günstige Dokument-/Tabellenalternative |
| Maximale Zweitprüfung | `anthropic/claude-opus-4.8` | schwierige, kritische Einzelstelle |
| GPT-Spitzenmodell | `openai/gpt-5.5` | hochwertige unabhängige Alternative |
| Kostenlos | `openrouter/free` | wechselndes Gratis-Visionmodell, nicht reproduzierbar |

Modellverfügbarkeit, Provider und Preise können sich unabhängig von SchriftLotse ändern.
Jede Anfrage verlangt einen Zero-Data-Retention-Endpunkt und lehnt Provider-Datensammlung ab;
wenn kein kompatibler Endpunkt verfügbar ist, soll die Anfrage fehlschlagen statt still auf
einen weniger privaten Provider auszuweichen.
