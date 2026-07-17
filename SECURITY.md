# Sicherheit und Datenschutz

Sicherheitsprobleme bitte nicht mit privaten Dokumenten in einem öffentlichen Issue melden.
Nutze stattdessen GitHubs private Sicherheitsmeldung für dieses Repository.

Nicht in Meldungen aufnehmen:

- OpenRouter- oder andere API-Schlüssel
- private Scans und Transkriptionen
- vollständige lokale Benutzerpfade
- lokale Datenbank oder Suchindex

SchriftLotse bindet die Oberfläche ausschließlich an `127.0.0.1`. Cloud- und GND-Funktionen
sind optional und werden in der Oberfläche ausdrücklich ausgelöst.

## Bekannte Einschränkung der optionalen Modellumgebung

Kraken 7.0.2 begrenzt PyTorch derzeit auf höchstens 2.10. Eine als niedrig eingestufte
PyTorch-JIT-Lücke ist erst ab PyTorch 2.13 behoben. SchriftLotse ruft `torch.jit.script`
nicht auf, akzeptiert nur fest fixierte Modellquellen und behandelt die Meldung deshalb bis
zu einem kompatiblen Kraken-Update als tolerierbares Restrisiko. Die Kernanwendung ohne das
optionale Extra `models` ist davon nicht betroffen.
