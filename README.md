# SchriftLotse

**Alte Schriften. Klar gelesen.**

SchriftLotse ist eine lokale, deutschsprachige Anwendung zum Entziffern und Durchsuchen
historischer Dokumente. Sie verarbeitet Scans, PDFs, mehrseitige TIFFs und ganze Ordner.
Der Schwerpunkt liegt auf deutscher Handschrift, Kurrent, Sütterlin, Fraktur, Antiqua und
Schreibmaschinentext von etwa 1800 bis 1945; ältere Quellen werden bestmöglich unterstützt.

## Funktionen

- lokale Stapelverarbeitung; OpenRouter nur für einen ausdrücklich gewählten Ausschnitt
- automatische Orientierung, konservativer Randbeschnitt und Buchfalztrennung
- adaptive Varianten für Beleuchtung, Kontrast, Schatten und Binarisierung
- Vergleich freier Tesseract-, Kraken-, TrOCR- und Party-Modelle mit CHURRO als lokalem
  Standard-Ganzseitenleser im Qualitätsprofil
- sichere Seiten-Zwischenstände und Fortsetzen nach einem Abbruch
- originalgetreue Fassung, Lesefassung, PDF, DOCX, JSON, PAGE XML und ALTO
- eScriptorium-Paket und Rückimport korrigierter PAGE-XML-Dateien
- Volltext-, Namens-, Fuzzy- und optional semantische Suche über alle Modell-Lesungen
- Sprung zur Fundstelle, pixelgenaue Markierung und indexierte manuelle Korrekturen
- keine Scans, Modellgewichte oder Ergebnisse im GitHub-Repository

## Start auf Apple Silicon

Voraussetzungen: macOS, [Homebrew](https://brew.sh/) und Internet für die erste Einrichtung.

1. Repository klonen oder herunterladen.
2. `SchriftLotse.command` per Doppelklick öffnen. Falls nötig einmal
   **Rechtsklick → Öffnen** verwenden.
3. Die Installation von Tesseract und deutschen Sprachdaten bestätigen.
4. Die lizenzklaren lokalen Kernmodelle TrOCR, Kraken/UB, Party und die semantische Suche
   (ca. 4,2 GB) bestätigen.
5. Für **Beste lokale Qualität** die Qwen Research License bestätigen; CHURRO 3B wird dann
   lokal als MLX-8-Bit-Modell eingerichtet (ca. 4,4 GB).
6. SchriftLotse öffnet ausschließlich lokal unter `http://127.0.0.1:7860`.

Nach der ersten Einrichtung kann zusätzlich ein richtiges macOS-App-Fenster gebaut werden:

```bash
scripts/build_macos_app.sh
open dist/SchriftLotse.app
```

Die kleine native App verwendet AppKit/WebKit und startet denselben lokalen Python-Dienst.
Sie enthält bewusst keine Modelle oder privaten Dokumente. Das Build-Skript hinterlegt den
aktuellen Repository-Pfad; nach einem Verschieben des Repositories die App einfach neu bauen.

Manuell:

```bash
brew install uv tesseract tesseract-lang
uv sync --frozen --extra models --extra mlx --extra dev
uv run schriftlotse models install-core
uv run schriftlotse models install-best --accept-research-license
uv run schriftlotse gui
```

## Bedienung

- **Entziffern:** Dateien ablegen oder einen Ordner wählen, optional Jahr und Schrift angeben.
- **Archivsuche:** intelligent, exakt, nach Namen oder nach Bedeutung suchen; Treffer öffnen
  direkt die richtige logische Seite und Zeile. Unsichere Stellen lassen sich priorisiert
  abarbeiten; anschließend erzeugt **Aktuelle Fassung exportieren** alle Ausgabeformate neu.
- **Modelle & Datenschutz:** lokale Modelle installieren und optional einen OpenRouter-Key im
  macOS-Schlüsselbund speichern.

Das Jahr darf leer bleiben. Eine Zahl im Dateinamen wie `brief-1872.jpg` gilt als starker
Hinweis. Ein einzelnes möglicherweise falsch gelesenes OCR-Jahr wird bewusst nicht als
Wahrheit übernommen. Bei schwierigen Beständen ist eine manuelle Jahres- oder Epocheneingabe
weiterhin die zuverlässigste Modellsteuerung.

## Modellprofile

- **Schnell:** Tesseract und leichte lokale Verarbeitung.
- **Lizenzklar:** TrOCR, Kraken/UB, Party und Tesseract ohne Forschungsgewichte. Party läuft
  bewusst nur hier, weil es auf dem M3-CPU-Pfad erheblich langsamer als CHURRO ist.
- **Beste lokale Qualität (Standard):** zusätzlich CHURRO 3B als MLX-8-Bit-Ganzseitenleser,
  nachdem dessen Qwen Research License ausdrücklich bestätigt wurde.

TrOCR nutzt auf dem M3 das MPS-Backend. Kraken/UB und Party laufen auf macOS über den stabilen
CPU-Pfad. CHURRO nutzt MLX/Metal und erhält gezielt die beleuchtungsnormalisierte Seite. Seine
XML-Metadaten werden nicht als Fakten übernommen; nur erkannte Dokumentzeilen gelangen als
prüfbare Lesung in das Ergebnis. Generative Lesungen überschreiben keine manuell bestätigte
Zeile.

Bei sicher vorerkanntem Druck werden Kraken, TrOCR und CHURRO übersprungen: Sie verbessern
solche Seiten nicht, erhöhen aber Laufzeit und Duplikate. Im lokalen Vergleich sank eine
repräsentative Fraktur-/Antiqua-Seite von 44,3 auf 6,7 Sekunden; das Schnellprofil benötigte
3,7 Sekunden. Gemischte Tabellen werden absichtlich nicht vorschnell als reine Druckseite
klassifiziert. Die ausführliche Testnotiz steht in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

`CHURRO Q6_K + mmproj Q8_0` ist eine GGUF-Kombination für llama.cpp/Metal, nicht für MLX-VLM.
Sie ist kleiner, lieferte im lokalen Vergleich mit dem verfügbaren Community-Konvertat aber
keine verlässliche Ausgabe. SchriftLotse verwendet deshalb die selbst aus der fixierten
Originalrevision erzeugte MLX-8-Bit-Fassung. Eine MLX-4-Bit-Fassung war zwar kleiner, brach bei
den schwierigen Testseiten jedoch leer oder mit degenerierter XML-Ausgabe ab.

Modellquellen, Revisionen und Lizenzen stehen in
[THIRD_PARTY_MODELS.md](THIRD_PARTY_MODELS.md). Kein Modell wird still aktualisiert.

## Bildaufbereitung und Nachvollziehbarkeit

Das Original bleibt unangetastet. SchriftLotse erzeugt lokale Arbeitsbilder, erkennt echte
Doppelseiten über den Buchfalz und speichert die Abbildung zur Originalseite. Dadurch bleiben
Suchmarkierungen auch nach Drehung, Beschnitt und Trennung korrekt. Jahres- oder Formularregeln
schreiben die Rohtranskription niemals heimlich um. Automatische, alternative und manuell
bestätigte Lesungen werden getrennt gespeichert und durchsucht.

## Kraken und eScriptorium

Kraken ist direkt als Segmentierungs- und Erkennungsbibliothek eingebaut. eScriptorium bleibt
die stärkere Spezialoberfläche für umfangreiche manuelle Segmentierung, Ground Truth und
Fine-Tuning, wäre als zwingender App-Unterbau aber unnötig schwer. Jeder Lauf erzeugt deshalb
`escriptorium-pagexml.zip` mit Bildern und PAGE XML. Korrigierte XML-Dateien lassen sich
anschließend zurückspielen:

```bash
uv run schriftlotse import-pagexml DOKUMENT_ID korrigiert/*.xml
```

## Suche

Die lokale Suche kombiniert SQLite FTS5/BM25, Trigramme, RapidFuzz, Kölner Phonetik,
Namensvarianten und sämtliche OCR-/HTR-Lesungen. Mit installiertem Qwen3-Embedding-0.6B kommt
semantische Suche hinzu. Sie bettet jeweils drei benachbarte Zeilen ein und kennt transparente
Archivwort-Familien wie Eheschließung/Trauung/verheiratet oder Tod/Bestattung/verstorben. Eine
bestätigte Korrektur ersetzt den sichtbaren Text, frühere Modell-Lesungen bleiben jedoch als
mögliche Suchspur erhalten.

## Datenschutz

- Bindung ausschließlich an `127.0.0.1`, keine öffentliche Freigabe
- OpenRouter nur nach Klick auf eine konkrete Fundstelle
- ZDR und `data_collection: deny`; Key im macOS-Schlüsselbund
- lokale Pfade und Modellstatus über `uv run schriftlotse doctor`

Für die optionale OpenRouter-Zweitprüfung stehen fünf explizite Profile bereit:

- **Schnell & stark:** Gemini 3.5 Flash (Standard)
- **OCR-Preis/Leistung:** Qwen3 VL 235B A22B Instruct
- **Maximale Zweitprüfung:** Claude Opus 4.8
- **GPT-Spitzenmodell:** GPT-5.5
- **Kostenlos:** OpenRouters wechselnder Free Models Router

Die IDs und Preise wurden am 18. Juli 2026 gegen den Live-Katalog geprüft. Es gibt keinen
öffentlichen Benchmark, der diese Modelle belastbar auf genau deutscher Kurrent dieses
Bestands vergleicht; die Bezeichnungen sind daher Empfehlungen, keine Genauigkeitsgarantie.
Pro Aufruf werden ZDR, `data_collection: deny`, strukturierte Ausgabe und ein nutzerseitiges
Kostenlimit verlangt. Ohne API-Schlüssel bleibt die Funktion vollständig inaktiv.

## Qualitätsgrenzen

Unbekannte Hände, beschädigte Vorlagen und komplexe Tabellen können nicht garantiert
fehlerfrei gelesen werden. Konfidenzen verschiedener Engines sind nicht direkt vergleichbar;
die erwartete Fehlerrate ist ohne eigene Referenzdaten nur eine Heuristik. Für wiederkehrende
Bestände ist Fine-Tuning mit korrigierten eigenen Zeilen langfristig wirksamer als ein immer
größeres Universalmodell.

## Entwicklung

```bash
uv sync --extra models --extra mlx --extra dev
uv run ruff check .
uv run mypy src
uv run pytest
scripts/build_macos_app.sh
```

Bitte keine privaten Scans in Issues oder Commits hochladen. Der Anwendungscode steht unter
Apache-2.0; Modelllizenzen gelten separat.
