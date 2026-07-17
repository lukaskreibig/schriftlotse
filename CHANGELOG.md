# Änderungsprotokoll

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
