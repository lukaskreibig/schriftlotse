# Freie Modelle und externe Dienste

Modellgewichte werden nicht mit SchriftLotse verteilt. Der Modellmanager lädt ausschließlich
die hier dokumentierten Quellen und fixierten Revisionen in den lokalen Benutzer-Cache.

| Schlüssel | Modell | Lizenz | Fixierung | Einsatz |
|---|---|---|---|---|
| `party-v4` | [Party v4](https://zenodo.org/records/20642057) | Apache-2.0 | MD5 `cf165e67061d492b72f600a6a72b7c61` | experimentelle Hochspeicher-Zweitlesung |
| Kraken BLLA | mit Kraken ausgeliefert | Apache-2.0 | durch `uv.lock` fixiert | lokale Standard-Zeilenerkennung |
| `orli` | [Orli](https://zenodo.org/records/20558179) | Apache-2.0 | MD5 `a9a6b0caf497203e758dbd4fc624af10` | experimentelle alternative Grundlinienerkennung |
| `trocr-kurrent-19` | [TrOCR Kurrent](https://huggingface.co/dh-unibe/trocr-kurrent) | MIT | Commit `dd026dc6…` | Kurrent 19. Jh. |
| TrOCR-Prozessor | [Microsoft TrOCR Base Handwritten](https://huggingface.co/microsoft/trocr-base-handwritten) | MIT | Commit `eaacaf45…` | vollständiger lokaler Bildprozessor und Tokenizer |
| `trocr-kurrent-early` | [TrOCR Kurrent XVI–XVII](https://huggingface.co/dh-unibe/trocr-kurrent-XVI-XVII) | MIT | Commit `eaedace4…` | Kurrent 16.–18. Jh. |
| `trocr-modern` | [TrOCR German Handwritten](https://huggingface.co/fhswf/TrOCR_german_handwritten) | AFL-3.0 | Commit `f43d8831…` | neuere lateinische Handschrift |
| `trocr-medieval` | [TrOCR Medieval](https://huggingface.co/dh-unibe/trocr-medieval-escriptmask) | MIT | Commit `bd7124a3…` | experimentell vor 1500 |
| `qwen-embed` | [Qwen3 Embedding 0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | Apache-2.0 | Commit `b0c614be…` | semantische Archivsuche |

Tesseract ist Apache-2.0. Deutsche `tessdata`-Sprachmodelle und das CC0-Modell `frak2021`
behalten ihre jeweiligen eigenen Lizenzen. Abhängigkeiten `party` und `orli` sind im
Python-Projekt auf konkrete Git-Commits fixiert.

OpenRouter und lobid-GND sind optionale Netzwerkdienste, keine eingebetteten Modelle. Die
GND-Daten stehen unter CC0.
