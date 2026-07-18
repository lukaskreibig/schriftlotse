# SchriftLotse

**Alte Schriften. Klar gelesen.**

SchriftLotse ist eine lokale, deutschsprachige Anwendung zum Entziffern und Durchsuchen
historischer Dokumente. Sie verarbeitet Scans, PDFs, mehrseitige TIFFs und ganze Ordner.
Der Schwerpunkt liegt auf deutscher Handschrift, Kurrent, Sütterlin, Fraktur, Antiqua und
Schreibmaschinentext von etwa 1800 bis 1945; ältere Quellen werden bestmöglich unterstützt.

## Funktionen

- lokale Stapelverarbeitung; optionale adaptive Cloud-Zweitprüfung nur nach Auftragsfreigabe
- Importvorschau mit getrennten Einzelbildern, Serienvorschlag und Metadaten je Dokument
- automatische Orientierung, konservativer Randbeschnitt und Buchfalztrennung
- adaptive Varianten für Beleuchtung, Kontrast, Schatten und Binarisierung
- goldstandardgestütztes Routing freier Tesseract-, Kraken-, TrOCR- und Party-Modelle;
  CHURRO arbeitet lokal als Ganzseiten-Zweitleser und Fallback
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

Die kleine native App verwendet AppKit/WebKit und startet einen eigenen lokalen Python-Dienst
auf einem freien Loopback-Port. Ein Instanz-Token verhindert, dass sie versehentlich eine
alte laufende Oberfläche übernimmt. Dateidialog, Zwischenablage und Standard-Tastenkürzel
sind nativ angebunden.
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
  Lose Bilder gelten zunächst als eigene Dokumente. Über **Prüfen & Metadaten** lassen sich
  Titel, Jahr und Schrift pro Dokument setzen; echte Bildserien können bewusst gruppiert werden.
- **Archivsuche:** intelligent, exakt, nach Namen oder nach Bedeutung suchen; Treffer öffnen
  direkt die richtige logische Seite und Zeile. Unsichere Stellen lassen sich priorisiert
  abarbeiten; anschließend erzeugt **Aktuelle Fassung exportieren** alle Ausgabeformate neu.
- **Modelle:** lokale Gewichte und Installationsstatus verwalten.
- **Einstellungen:** Standardprofil, Schrift, Ausgabeordner, Suche und alle Cloudoptionen
  sichtbar konfigurieren; OpenRouter-Schlüssel prüfen und im macOS-Schlüsselbund speichern.

Das Jahr darf leer bleiben. Eine Zahl im Dateinamen wie `brief-1872.jpg` gilt als starker
Hinweis. Ein einzelnes möglicherweise falsch gelesenes OCR-Jahr wird bewusst nicht als
Wahrheit übernommen. Bei schwierigen Beständen ist eine manuelle Jahres- oder Epocheneingabe
weiterhin die zuverlässigste Modellsteuerung.

## Modellprofile

- **Schnell:** Tesseract und leichte lokale Verarbeitung.
- **Lizenzklar:** TrOCR, Kraken/UB, Party und Tesseract ohne Forschungsgewichte. Party läuft
  bewusst nur hier, weil es auf dem M3-CPU-Pfad erheblich langsamer als CHURRO ist.
- **Beste lokale Qualität (Standard):** das epochenpassende TrOCR und CHURRO 3B MLX 8-Bit
  werden verglichen. Bei bekanntem Jahr priorisiert der Router den am unabhängigen
  Goldstandard geprüften Spezialisten; CHURRO bleibt Ganzseiten-Zweitleser und Fallback.
- **Beste Qualität:** dieselbe lokale Pipeline; anschließend werden höchstens vier besonders
  unsichere Zeilen je Seite bis zum bestätigten Auftragsbudget per Cloud gegengelesen. Die
  Cloud-Fassung bleibt eine eigene, unbestätigte Lesung.

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
- OpenRouter nur nach Klick auf eine Fundstelle oder nach ausdrücklicher Freigabe des Profils
  **Beste Qualität** samt Kostenlimit
- `data_collection: deny` für alle Profile; ZDR wird nur bei entsprechend verfügbaren
  Endpunkten verlangt und in der Oberfläche pro Modell ausgewiesen
- lokale Pfade und Modellstatus über `uv run schriftlotse doctor`

Für die optionale OpenRouter-Zweitprüfung stehen vier explizite, reproduzierbare Profile bereit:

- **Formulare & Seiten:** Gemini 3.5 Flash
- **Ausgewogen/experimentell:** GPT-5.6 Luna (derzeit ohne ZDR-Endpunkt)
- **OCR-Preis/Leistung:** Qwen3 VL 235B A22B Instruct
- **Textstellen (Standard):** Claude Sonnet 5

Die IDs und Preise wurden am 18. Juli 2026 gegen den Live-Katalog geprüft. Es gibt keinen
öffentlichen Benchmark, der diese Modelle belastbar auf genau deutscher Kurrent dieses
Bestands vergleicht; deshalb kann die Benchmark-CLI acht private Goldausschnitte kontrolliert
gegen alle vier Modelle testen. Die Bezeichnungen sind Empfehlungen, keine Garantie.
Die App fordert eine reine Transkription ohne JSON-Zwang an, entfernt klar abgegrenzte
Reasoning-/Markdown-Hüllen lokal und verwirft leere, abgelehnte oder unplausibel lange
Antworten. Modell, Laufzeit, Kosten und Fehler werden dauerhaft protokolliert. Ohne
API-Schlüssel bleibt die Funktion vollständig inaktiv.

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

Reproduzierbare Qualitätsläufe und die gemessenen CER-Werte stehen in
[docs/BENCHMARKS.md](docs/BENCHMARKS.md). Browser-E2E-Tests prüfen Dateiupload, Dropdown,
Einstellungen, Tastaturbedienung und die scrollfreie 1100×800-/1320×860-Arbeitsfläche.

Bitte keine privaten Scans in Issues oder Commits hochladen. Der Anwendungscode steht unter
Apache-2.0; Modelllizenzen gelten separat.
