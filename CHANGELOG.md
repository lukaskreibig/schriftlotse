# Änderungsprotokoll

## 0.2.0 – 2026-07-18

- vollständiger Scan-/Text-Dokumentarbeitsplatz mit synchronen Zeilenmarkierungen,
  Original-/Aufbereitet-Ansicht, Transkription, Lesefassung und direkter Korrektur
- verschachtelte Sammlungen und strukturierter Ordnerimport mit sichtbarem Zielpfad
- verknüpfte Quellordner mit manuellem, nichtdestruktivem Prüfsummen-Abgleich
- Eingang erklärt fertige Ergebnisse und führt direkt zu Transkription und Ablage
- dauerhaft geöffnetes strukturiertes Laufprotokoll zeigt Bildanalyse, Modellroute,
  Auswahlbegründung, Backends und Laufzeiten
- native Dateiauswahl umgeht WebKit-Temporärnamen; generische Titel werden vor OCR abgefangen

- vollständiger Archivbrowser zeigt Dokumente ohne vorherige Suche in Raster oder Liste
- flexible Sammlungen sowie Archiv-, Bestands-, Serien- und Signaturmetadaten
- verwaltete Originalbibliothek mit Inhaltsprüfung, Dublettenerkennung und Papierkorb
- bestätigte Migrationsvorschau für alte Referenzen; versehentliche Bildserien lassen sich
  ohne Verlust vorhandener OCR-Zeilen in einzelne Dokumente trennen
- Exporte werden aus der Datenbank regeneriert und hängen nicht mehr von `result.json` ab
- technische Seitenberichte speichern Bilddiagnostik, Profil, Modellläufe und Unsicherheiten
- ressourcenschonende Live-Seitenvorschau mit Stufe, Modell und erkannten Zeilenbereichen
- Dokumentmetadaten und Sammlungszuordnungen bleiben bei einer erneuten OCR erhalten

- nativer WKWebView-Dateidialog und vollständiges Bearbeiten-Menü für ⌘X/⌘C/⌘V/⌘A
- eigener dynamischer Loopback-Port und Instanzprüfung verhindern eine veraltete Oberfläche
- kompakter Vier-Schritt-Arbeitsfluss mit barrierearmem Schrift-Combobox und sichtbaren
  Einstellungen für Profile, Suche, Ausgabe, Tesseract und OpenRouter
- vier kuratierte OpenRouter-Modelle zeigen Anbieter und exakte, fest benannte Modell-ID;
  Schlüssel lassen sich vor Nutzung über die Key-API prüfen
- unabhängige Goldbenchmarks für Kurrent 1665 und 1848–1903 mit CER/WER-CLI
- epochenpassendes TrOCR erhält bei bekanntem Jahr Goldstandard-Vorrang; CHURRO bleibt
  Ganzseiten-Zweitleser und Fallback statt pauschaler Hauptlesung
- Trigram-Vorauswahl skaliert die Fuzzy-/Namenssuche über den gesamten Index; Scores
  saturieren nicht mehr pauschal bei 1,000
- Such-Qrels (Recall/MRR), privater 8×4-Cloudvergleich und Browser-E2E-Tests ergänzt

- lokale FastAPI-Oberfläche mit dauerhaftem Status und Verarbeitungsprotokoll
- automatische Orientierung, konservativer Seitenrand und robuste Buchfalztrennung
- getrennte Buchseiten werden nochmals einzeln vom Film-/Aufnahmerand befreit
- Kraken/UB und CHURRO-MLX-8-Bit in den lokalen Modellrouter integriert
- CHURRO nutzt als Standard-Ganzseitenleser die im Dokumenttest zuverlässigere
  Beleuchtungsnormalisierung und übernimmt nur Dokumentzeilen aus der XML-Ausgabe
- Party bleibt im lizenzklaren Profil verfügbar, bremst aber nicht mehr das CHURRO-Profil
- achtstellige Datumsangaben in Dateinamen (z. B. `19230726`) steuern das Epochenmodell
- CLI-Stapel können mit `--resume-job` aus Seiten-Checkpoints fortgesetzt werden
- CHURRO-Sprungkoordinaten verwenden schnelle Bildboxen statt einer zweiten Kraken-BLLA-Runde
- Profile für schnell, beste lokale Qualität und lizenzklar
- konservative Jahreserkennung ohne stille Jahres- oder Formularumschreibungen
- getrennte Modell-Lesungen, bestätigte Fassungen, Regionen, Grundlinien und Polygone
- Suche über alternative OCR-/HTR-Lesungen und manuelle Fassungen
- semantische Suche mit Drei-Zeilen-Kontext und transparenten deutschen Archivbegriffen
- einmalige FTS-Neuindizierung auf Schema v4; keine veralteten Treffer nach Wiederholungsläufen
- PAGE XML, ALTO, eScriptorium-Paket und PAGE-XML-Rückimport
- pixelgenaue logische Seitenbilder nach einer Doppelseitentrennung
- seitenweise Zwischenstände und Fortsetzen unterbrochener Aufträge
- OpenRouter ausschließlich für ausgewählte Zeilenausschnitte
- sichtbare Modellinstallation mit Lizenz-, Prüfsummen- und Speicherprüfung
- konservative Druck-Vorerkennung; unnötige Handschriftmodelle werden auf sicheren
  Druckseiten nicht mehr gestartet (Testseite: 44,3 s auf 6,7 s)
- monotone Fortschrittsanzeige mit grober Restzeit auch nach Doppelseitentrennung
- priorisierte Prüfliste unsicherer Zeilen und sichtbare alternative Modell-Lesungen
- vier auswählbare OpenRouter-Profile mit Live-Katalog-geprüften Modell-IDs,
  Kostenbestätigung und ZDR-Weiterleitung
- nativer, lokal signierter AppKit/WebKit-Rahmen über `scripts/build_macos_app.sh`
- CLI-Profilwahl über `--profile`
- Qwen3 Embedding 0.6B gehört nun zu den empfohlenen Kernmodellen, damit die
  Bedeutungsähnlichkeit nach der Ersteinrichtung tatsächlich lokal verfügbar ist

## 0.1.4 – 2026-07-17

- fehlenden TrOCR-Tokenizer erkannt und mit vollständig gepinntem Offline-Prozessor repariert
- robuste lokale Kraken-BLLA-Zeilenerkennung für gemischte historische Formulare
- Kurrent-Zeilen werden gebündelt auf Apple Silicon verarbeitet
- gedruckte Fraktur wird zeilenweise separat gelesen und räumlich zusammengeführt
- Modellwahl bewertet jetzt Textabdeckung und Vollständigkeit wesentlich stärker
- Jahresangaben in Datumszeilen können transparent durch die eingegebene Jahreszahl gestützt werden
- unvollständige Ergebnisse werden deutlich gekennzeichnet statt als zuverlässig ausgegeben
- Orli ist nur noch eine experimentelle Alternative und kein unnötiger Kerndownload

## 0.1.3 – 2026-07-17

- dauerhaft sichtbarer Live-Status mit Prozent, Arbeitsschritt, Laufzeit und Lokal-Hinweis
- Fortschritt während langer Modellläufe wird sekündlich aktualisiert
- Gradio darf erzeugte Dateien aus dem SchriftLotse-Ausgabeordner bereitstellen
- wiederholte TrOCR-Generierungswarnung entfernt
- Party wird auf Macs unter 32 GiB nicht automatisch gestartet
- Orli wird ohne Online-Abfrage aus den bereits installierten Gewichten aufgebaut
- Start nutzt den fixierten Lockfile-Stand ohne erneute Abhängigkeitsauflösung

## 0.1.2 – 2026-07-17

- Homebrews aktuelles Tesseract-Modell `script/Fraktur` wird automatisch mitbewertet

## 0.1.1 – 2026-07-17

- Kernmodell-Empfehlung auf das für 18-GB-Apple-Silicon passendere TrOCR Kurrent umgestellt
- unnötige doppelte PyTorch-Gewichte beim Hugging-Face-Download ausgeschlossen
- Party-Fehler werden zuverlässig erkannt und lösen den sicheren Modell-Fallback aus
- Party-Speicherbedarf in Oberfläche und Dokumentation klar gekennzeichnet

## 0.1.0 – 2026-07-17

- erste öffentliche Version von SchriftLotse
- lokale OCR/HTR-Pipeline mit Party, Orli, TrOCR und Tesseract
- adaptive, originalschonende Bildaufbereitung und automatische Kandidatenauswahl
- Stapelverarbeitung für Bilder, TIFF und PDF
- deutsche PDF-, DOCX-, TXT-, JSON- und PAGE-XML-Ausgaben
- lokale Archivsuche mit Volltext, Fuzzy-Matching, Kölner Phonetik und optionaler Semantik
- Sprung zur Fundstelle, Scan-Markierung und indexierte manuelle Korrekturen
- optionale OpenRouter-Zweitlesung mit Datenschutzvorgaben und Budgetgrenze
