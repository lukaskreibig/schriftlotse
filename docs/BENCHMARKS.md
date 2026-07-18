# Reproduzierbare Qualitätsmessung

Stand: 18. Juli 2026, M3 MacBook Pro mit 18 GB gemeinsamem Speicher.

## Bestand und Grenzen

Der vollständige private Testordner bestand aus 15 Quelldateien (drei PDFs und zwölf
Einzelbildern). Nach der automatischen Trennung zweier Doppelseiten wurden 17 logische Seiten
verarbeitet. Enthalten waren Fraktur-/Antiquadruck, Schreibmaschine, Kurrent aus dem 18. bis
20. Jahrhundert, Tabellen/Formulare, ein sehr früher Text, Kartenbeschriftung, Schatten,
Filmrand und unterschiedliche Auflösungen.

Für diesen privaten Bestand existiert noch keine vollständig manuell kontrollierte Ground
Truth. Seine qualitative Beurteilung wird deshalb klar von den öffentlichen, zeilengenaue
Solltexte enthaltenden Benchmarks getrennt.

## Öffentliche Goldstandards

SchriftLotse enthält einen reproduzierbaren Benchmark-Läufer. Daten werden erst auf Befehl in
den lokalen Benutzer-Cache geladen und niemals in Git eingecheckt.

```bash
uv run schriftlotse benchmark gold kurrent-1665 --sample 96 --output hofdiarium.json
uv run schriftlotse benchmark gold kurrent-19 --sample 96 --output bundesrat.json
```

| Goldstandard | Umfang des Kontrolllaufs | Modell | normalisierte CER |
|---|---:|---|---:|
| Dresdner Hofdiarium 1665, CC BY 4.0, DOI 10.5281/zenodo.14356190 | 96 gleichmäßig verteilte Zeilen | TrOCR XVI–XVIII | 9,33 % |
| Schweizer Bundesratsprotokolle 1848–1903, CC BY 4.0, DOI 10.5281/zenodo.4746342 | 96 unabhängige Hände/Zeilen | TrOCR Kurrent 19. Jh. | 21,17 % |
| Dresdner Hofdiarium 1665, vollständige Seite 177 | 23 Sollzeilen, 18 ausgegeben | CHURRO MLX 8-Bit | 28,42 % |

Ein erneuter 16-Zeilen-Smoke-Test des eingebauten Läufers ergab am 18. Juli 2026 für das
Hofdiarium 9,63 % normalisierte CER in 20,21 Sekunden. Kleine Stichproben schwanken; die
96-Zeilen-Werte steuern deshalb das Routing. Die erheblich schlechtere Kurrent-19-CER ist
ein unabhängiger Realitätscheck gegenüber dem wesentlich niedrigeren In-Domain-Wert der
Modellkarte.

Die Zeilenkoordinaten sind entscheidend: Auf den Bundesratsdaten ergab das komplette,
großzügig gepolsterte Zeilenbild 64,41 % CER, der PAGE-XML-TextLine-Ausschnitt mit kleinem
Kontextrand 21,17 %. SchriftLotse erweitert erkannte TrOCR-Zeilen deshalb um 10 % vertikalen
und 2 % horizontalen Kontext.

## Beobachtungen

- CHURRO MLX 8-Bit lieferte auf schwieriger Handschrift und ganzen Seiten die kohärentesten
  Lesungen. Die 4-Bit-MLX-Fassung und ein verfügbares Q6_K-GGUF-Konvertat waren lokal nicht
  ausgabestabil; deshalb ist 8-Bit der Standard.
- Die erste MLX-Generierung nach dem Laden kann bei CHURRO leer enden. Ein einziger begrenzter
  Wiederholungsversuch löste dies reproduzierbar; weitere Endlosschleifen gibt es nicht.
- TrOCR Kurrent (19. Jahrhundert) und TrOCR XVI–XVIII sind die zeilenweisen Spezialleser.
  Bei bekannter Epoche erhält das am unabhängigen Goldstandard passende Modell Vorrang;
  CHURRO bleibt Ganzseiten-Zweitleser und Fallback. Alle Lesungen bleiben prüfbar und suchbar.
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
hoher interner Konfidenz einen sprachlich unplausiblen Text. CHURRO las die Satzstruktur
kohärenter. Das zeigt, warum die App bei unbekannter Epoche mehrere Kandidaten braucht; es
rechtfertigt aber keine pauschale CHURRO-Hauptlesung. Bei einem bekannten Jahr schließt der
Router ein epochenfalsches TrOCR aus und priorisiert den validierten Spezialisten.

## Gewählte Profile

- **Schnell:** eine Bildvariante, Tesseract, keine neuronalen HTR-Modelle. Für Sichtung und
  große Vorläufe.
- **Beste lokale Qualität:** konservative Druck-Vorerkennung; sonst CHURRO 8-Bit plus
  epochengerechtes TrOCR, Kraken/UB und räumlich zugeordnete Alternativen. Für die eigentliche
  Transkription.
- **Nur lizenzklar:** CHURRO-Forschungsgewichte entfallen; TrOCR/Kraken/Party bilden die
  nachvollziehbare Alternative.

## Suche und private Cloud-Stichprobe

Für Suchtests wird eine private JSON-Datei außerhalb von Git verwendet:

```json
[{"query":"Kreipig","mode":"namen","relevant_line_ids":["ZEILEN-ID"]}]
```

`uv run schriftlotse benchmark search qrels.json --limit 10` berichtet Recall@10 und MRR.
Der Test berücksichtigt dadurch echte relevante und irrelevante Treffer statt nur künstlich
zu prüfen, ob irgendein Ergebnis erscheint.

Zusätzlich erzeugt `uv run schriftlotse benchmark search-public kurrent-1665 --queries 40`
einen isolierten Index aus den echten Goldtextzeilen. Verwendet werden eindeutige Wörter ab
sechs Zeichen; jede zweite Anfrage erhält einen reproduzierbaren OCR-ähnlichen
Ein-Zeichen-Fehler. Der Lauf am 18. Juli 2026 erreichte **Recall@10 1,00 und MRR 1,00 bei 40
Anfragen**. Das belegt die Tippfehlertoleranz auf diesem kontrollierten Test, nicht die
Korrektheit der vorgelagerten OCR oder beliebiger semantischer Anfragen.

Ein Cloudmanifest besteht aus bis zu acht JSONL-Zeilen mit `id`, `image`, `reference` und
optional `year`. `uv run schriftlotse benchmark cloud manifest.jsonl --budget 2` vergleicht
dieselben Ausschnitte mit den vier kuratierten, fest benannten Modellen. Ohne kontrollierte
Referenz wird kein Sieger behauptet. Private Bilder und Ergebnisse bleiben außerhalb von Git.

Bei wiederkehrenden Händen werden einige hundert bestätigte Zeilen für Fine-Tuning
voraussichtlich mehr bringen als ein weiteres allgemeines Großmodell. Die PAGE-XML-/
eScriptorium-Ausgabe ist genau dafür vorhanden.
