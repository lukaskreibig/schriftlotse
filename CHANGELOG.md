# Änderungsprotokoll

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
