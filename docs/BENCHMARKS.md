# Lokale Test- und Profilnotiz

Stand: 18. Juli 2026, M3 MacBook Pro mit 18 GB gemeinsamem Speicher.

## Bestand und Grenzen

Der vollständige private Testordner bestand aus 15 Quelldateien (drei PDFs und zwölf
Einzelbildern). Nach der automatischen Trennung zweier Doppelseiten wurden 17 logische Seiten
verarbeitet. Enthalten waren Fraktur-/Antiquadruck, Schreibmaschine, Kurrent aus dem 18. bis
20. Jahrhundert, Tabellen/Formulare, ein sehr früher Text, Kartenbeschriftung, Schatten,
Filmrand und unterschiedliche Auflösungen.

Für diesen Bestand existiert noch keine vollständig manuell kontrollierte Ground Truth.
Deshalb wären CER-/WER-Zahlen Scheingenauigkeit. Bewertet wurden Vollständigkeit, sichtbare
Lesbarkeit, ausgelassene beziehungsweise doppelte Zeilen, Modellstabilität und Laufzeit.

## Beobachtungen

- CHURRO MLX 8-Bit lieferte auf schwieriger Handschrift und ganzen Seiten die kohärentesten
  Lesungen. Die 4-Bit-MLX-Fassung und ein verfügbares Q6_K-GGUF-Konvertat waren lokal nicht
  ausgabestabil; deshalb ist 8-Bit der Standard.
- Die erste MLX-Generierung nach dem Laden kann bei CHURRO leer enden. Ein einziger begrenzter
  Wiederholungsversuch löste dies reproduzierbar; weitere Endlosschleifen gibt es nicht.
- TrOCR Kurrent (19. Jahrhundert) und TrOCR XVI–XVIII bleiben wertvolle zeilenweise
  Spezialleser. Bei unbekannter Epoche werden beide verglichen, bei bekanntem Jahr nur das
  passende Modell. Alle Lesungen bleiben prüfbar und suchbar.
- Auf einer Kurrentseite von 1743 war CHURRO als Ganzseitenleser deutlich kohärenter als die
  fragmentierte TrOCR-Vorlesung. TrOCR blieb bei einzelnen Wörtern und als unabhängige
  Alternative nützlich.
- Party v4 war auf dem stabilen CPU-Pfad wesentlich langsamer und im Test weniger hilfreich
  als CHURRO. Es bleibt deshalb im lizenzklaren Profil, läuft aber nicht im Standardprofil.
- Gemischte Tabellen dürfen nicht anhand ihrer gedruckten Überschrift als reine Druckseite
  klassifiziert werden. Die Druck-Vorerkennung verlangt deshalb mindestens 500 erkannte
  Zeichen bei 50 % Tesseract-Sicherheit; sprechende Druck-Dateinamen dürfen ab 200 Zeichen
  konservativ unterstützen.

## Gemessene Druckprofile

Repräsentative deutschsprachige Bekanntmachung von 1862, gleiche Seite und Maschine:

| Konfiguration | Laufzeit | Ergebnis |
|---|---:|---|
| Schnell | 3,73 s | Tesseract, eine Bildvariante |
| Qualitätsprofil vor Druckrouting | 44,30 s | unnötige Zeilensegmentierung/Handschriftmodelle, Duplikatrisiko |
| Beste lokale Qualität nach Druckrouting | 6,67 s | mehrere Tesseract-Sprachen/-Varianten, keine unnötigen HTR-Modelle |

Die Werte sind keine allgemeingültigen Benchmarks; Auflösung, Zeilenzahl und thermischer
Zustand verändern sie. Sie zeigen aber klar, dass Modellrouting auf sicheren Druckseiten
größeren Nutzen bringt als ein pauschales „alle Modelle immer“.

## Vollständiger lokaler Korpuslauf

Der private Entwicklungsbestand umfasst 15 Quelldateien (12 Bilder und 3 PDFs), nach
automatischer Doppelseitentrennung 17 logische Seiten. Auf dem M3 MacBook Pro mit 18 GB
benötigte der abschließende Lauf im Profil **Beste lokale Qualität** 27 Minuten 23 Sekunden.
Alle vier Ausgabedokumente wurden vollständig exportiert und indexiert. Der vorherige Lauf
ohne konservatives Druckrouting benötigte 63 Minuten 21 Sekunden. Das Routing spart hier
also rund 57 Prozent Laufzeit, ohne die Handschriftseiten aus dem CHURRO-/TrOCR-Vergleich
zu nehmen.

Auf einer besonders frühen, undatierten Urkunde lieferte das 19.-Jahrhundert-TrOCR trotz
hoher interner Konfidenz einen sprachlich unplausiblen Text. CHURRO las dagegen unter anderem
die historische Titulatur und Satzstruktur nachvollziehbar. Deshalb ist CHURRO im Profil
**Beste lokale Qualität** nun der Masterleser, sobald seine Ganzseitenausgabe eine kleine
Plausibilitäts- und Abdeckungsgrenze erfüllt; TrOCR, Kraken und Tesseract bleiben als
vergleichbare Alternativen an denselben Fundstellen erhalten.

## Gewählte Profile

- **Schnell:** eine Bildvariante, Tesseract, keine neuronalen HTR-Modelle. Für Sichtung und
  große Vorläufe.
- **Beste lokale Qualität:** konservative Druck-Vorerkennung; sonst CHURRO 8-Bit plus
  epochengerechtes TrOCR, Kraken/UB und räumlich zugeordnete Alternativen. Für die eigentliche
  Transkription.
- **Nur lizenzklar:** CHURRO-Forschungsgewichte entfallen; TrOCR/Kraken/Party bilden die
  nachvollziehbare Alternative.

## Nächster wissenschaftlicher Qualitätsschritt

Für echte Genauigkeitszahlen sollten je Dokumenttyp kontrollierte Referenzzeilen erstellt und
CER, WER, ausgelassene Zeilen sowie Korrekturzeit gemessen werden. Bei wiederkehrenden Händen
werden einige hundert bestätigte Zeilen für Fine-Tuning voraussichtlich mehr bringen als ein
weiteres allgemeines Großmodell. Die PAGE-XML-/eScriptorium-Ausgabe ist genau dafür vorhanden.
