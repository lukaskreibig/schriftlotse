# SchriftLotse

**Alte Schriften. Klar gelesen.**

SchriftLotse ist eine lokale, deutschsprachige Anwendung zum Entziffern und Durchsuchen
historischer Dokumente. Sie verarbeitet einzelne Scans, PDFs, mehrseitige TIFFs und ganze
Ordner. Originalgetreue Transkription, Lesefassung, PDF, DOCX, JSON und PAGE XML werden
gemeinsam erzeugt.

Der Schwerpunkt liegt auf deutschsprachiger Handschrift, Kurrent, Sütterlin, Fraktur,
Antiqua und Schreibmaschinentext von etwa 1800 bis 1945. Spezialisierte freie Modelle decken
auch frühneuzeitliche Quellen ab; die Unterstützung vor 1500 ist experimentell.

## Das Wichtigste

- vollständig lokale Verarbeitung; OpenRouter ist optional und pro Auftrag deaktiviert
- adaptive Bildaufbereitung für Helligkeit, Schatten, Vergilbung, Kontrast und Schieflage
- automatischer Vergleich von Bildvarianten und verfügbaren OCR-/HTR-Modellen
- Batch-Verarbeitung mit natürlicher Dateisortierung und rekursiven Unterordnern
- Archivsuche über alle Dokumente mit OCR-Fehlern, Namensvarianten und Bedeutungsähnlichkeit
- Treffer springen zur Seite und markieren die entsprechende Zeile im Scan
- manuelle Korrekturen aktualisieren den lokalen Suchindex
- optionale, ausdrücklich ausgelöste Personensuche in der GND
- Modellgewichte, Scans, Ergebnisse und API-Schlüssel liegen niemals im GitHub-Repository

## Start auf macOS

Voraussetzungen: macOS auf Apple Silicon, [Homebrew](https://brew.sh/) und Internet für die
erste Einrichtung.

1. Repository herunterladen oder klonen.
2. `SchriftLotse.command` per Doppelklick öffnen. Beim ersten Download kann macOS einmalig
   **Rechtsklick → Öffnen** verlangen.
3. Die optionale Installation von Tesseract und deutschen Sprachdaten bestätigen.
4. Einmalig die empfohlenen freien Kernmodelle TrOCR Kurrent und Orli (zusammen ca. 2 GB)
   bestätigen. Beide Quellen sind auf feste Revisionen bzw. Prüfsummen fixiert.
5. Die Oberfläche öffnet sich lokal unter `http://127.0.0.1:7860`.

Der Launcher installiert Python 3.12 und die Modelladapter reproduzierbar mit `uv`. Weitere
jahrgangsspezifische Gewichte werden erst in **Modelle & Datenschutz** ausgewählt.

Manuell:

```bash
brew install uv tesseract tesseract-lang
uv sync --extra models --extra dev
uv run schriftlotse gui
```

## Verwendung

### Oberfläche

- **Entziffern:** Dateien ablegen oder Stapelordner auswählen, optional Jahr und Schriftart
  angeben, Verarbeitung starten.
- **Archivsuche:** intelligent, exakt, nach Namen oder nach Bedeutung suchen. Ein Treffer
  öffnet Scan, Seite und Zeile.
- **Modelle & Datenschutz:** freie lokale Modelle installieren und optional einen
  OpenRouter-Schlüssel im macOS-Schlüsselbund speichern.

### Kommandozeile

```bash
uv run schriftlotse batch ~/Scans --year 1872 --script handschrift
uv run schriftlotse search "Johann Schmitt" --mode namen
uv run schriftlotse models list
uv run schriftlotse models install-core
uv run schriftlotse models install party-v4
uv run schriftlotse doctor
```

## Bildaufbereitung

Das Original bleibt unangetastet. SchriftLotse erzeugt temporär ein orientiertes Farbbild,
eine beleuchtungsnormalisierte Graustufenversion, eine mild kontrastverstärkte Version und
eine adaptive Sauvola-Binarisierung. Bildmetriken wählen zwei Kandidaten; die OCR-Ergebnisse
entscheiden anschließend. Das freie Orli-Modell erkennt Grundlinien und Lesereihenfolge;
falls es noch nicht installiert ist oder scheitert, übernimmt eine konservative lokale
Zeilenerkennung. Umlaute, Punkte und kleine Satzzeichen werden nicht aggressiv entfernt.

## Lokale Modelle

Modellquellen, feste Revisionen, Einsatzzweck und Lizenzen stehen in
[THIRD_PARTY_MODELS.md](THIRD_PARTY_MODELS.md). Der Basismodus funktioniert mit Tesseract;
für schwierige Handschrift sind die jahrgangsspezifischen TrOCR-Modelle vorgesehen. Party v4
ist wegen seines hohen Speicherbedarfs nur eine experimentelle Option für stärkere Systeme.
Kein Modell wird still aktualisiert.

## Archivsuche

Die lokale Suche kombiniert SQLite FTS5, BM25, Trigramme, RapidFuzz, Kölner Phonetik,
OCR-Alternativen und Namensgruppen. Mit installiertem Qwen3-Embedding-0.6B kommt semantische
Suche hinzu. Die Namenssuche bleibt unabhängig davon vollständig lokal und kombiniert
phonetische Ähnlichkeit, Schreibvarianten und alternative OCR-Lesungen.

## Datenschutz

- Serverbindung ausschließlich `127.0.0.1`, keine Gradio-Freigabe
- OpenRouter nur nach Aktivierung; ZDR und `data_collection: deny`
- Schlüssel im macOS-Schlüsselbund
- GND-Abgleich nur nach Klick und nur mit dem ausgewählten Namen
- alle lokalen Pfade siehe `schriftlotse doctor`

## Qualitätsgrenzen

Unbekannte Handschriften und beschädigte Vorlagen können nicht garantiert fehlerfrei
erkannt werden. SchriftLotse zeigt deshalb Modell, Bildvariante, Konfidenz, erwartete
Fehlerrate und alternative Lesungen an. Historische Rechtschreibung wird nicht automatisch
modernisiert.

## Entwicklung

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy src/schriftlotse
uv run pytest
```

Beiträge sind willkommen; bitte keine privaten Scans oder Transkriptionen in Issues
hochladen. Der eigene Code steht unter Apache-2.0, Modelllizenzen gelten separat.
