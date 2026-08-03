# Besondere Kategorien personenbezogener Daten (Art. 9 DSGVO)

Kurzbeschreibung der Verarbeitung für Anliegen, deren bloße Auswahl eine
besondere Kategorie personenbezogener Daten offenbart. Gedacht als Grundlage
für die Abstimmung mit den Datenschutzbeauftragten der beteiligten Städte.

Stand: 2026-08-03. Verantwortlicher: siehe Impressum auf
<https://buergerwecker.de/impressum>.

## Worum es geht

Bürgerwecker fragt die öffentlichen Terminseiten der Städte ab und
benachrichtigt Abonnentinnen und Abonnenten per E-Mail, sobald ein Termin für
das von ihnen gewählte Anliegen frei wird. Gespeichert werden dafür
E-Mail-Adresse, Stadt/Amt, Sprache, die gewählten Filter und Zeitstempel.

Bei den allermeisten Anliegen (Personalausweis, Zulassung, Gewerbe …) ist das
eine gewöhnliche Verarbeitung auf Grundlage einer Einwilligung,
Art. 6 Abs. 1 lit. a DSGVO.

Bei einzelnen Anliegen ist es das nicht. Zwei Beispiele aus Münster:

| Anliegen | Amt | Datenkategorie |
| --- | --- | --- |
| Beratung zu sexuell übertragbaren Infektionen | Gesundheitsamt | Gesundheitsdaten |
| Erklärung nach dem Selbstbestimmungsgesetz (zwei Anliegen) | Standesamt | Angaben zur Geschlechtsidentität |

Hier ist bereits die Zuordnung „diese E-Mail-Adresse interessiert sich für
dieses Anliegen" die schützenswerte Information. Ein Datensatz besteht aus
E-Mail-Adresse und Anliegen — ohne Namen, ohne Anschrift, ohne
Terminbuchung —, ist aber trotzdem eine Verarbeitung besonderer Kategorien
nach Art. 9 Abs. 1 DSGVO.

## Rechtsgrundlage

Art. 9 Abs. 2 lit. a DSGVO — ausdrückliche Einwilligung, zusätzlich zu
Art. 6 Abs. 1 lit. a DSGVO.

Umsetzung: Wählt jemand ein solches Anliegen, erscheint im Anmeldeformular ein
gesondertes, nicht vorausgewähltes Kästchen mit eigenem Einwilligungstext. Ohne
dieses Häkchen kommt keine Anmeldung zustande; die Prüfung erfolgt
serverseitig, nicht nur im Formular. Der Zeitpunkt der Einwilligung wird
gespeichert (Nachweispflicht, Art. 7 Abs. 1 DSGVO). Zusätzlich gilt weiterhin
das Double-Opt-In: die Anmeldung wird erst nach Klick auf den Bestätigungslink
in der E-Mail aktiv.

Widerruf jederzeit über den Abmeldelink in jeder E-Mail (Art. 7 Abs. 3 DSGVO);
er löscht die Anmeldung.

## Zusätzliche Schutzmaßnahmen

- **Keine Nennung in E-Mails.** Benachrichtigungen zu solchen Anliegen nennen
  weder das Anliegen noch das Amt — nur Datum, Uhrzeit und einen Buchungslink.
  Auch der Link enthält den Namen des Amtes nicht: er trägt eine signierte
  Kennung und wird erst auf dem Server aufgelöst. Eine E-Mail im Posteingang
  oder auf einem Sperrbildschirm verrät damit nichts.
- **Kürzere Speicherdauer.** 30 Tage statt 90; danach läuft die Anmeldung
  automatisch ab. Verlängerung nur wieder um 30 Tage.
- **Kein Referrer.** Alle Seiten senden `Referrer-Policy: no-referrer`, damit
  die aufgerufene Adresse (die das Amt benennt) nicht an Dritte weitergegeben
  wird, wenn jemand einem Link folgt.
- **Datensparsamkeit wie bisher.** Keine IP-Adressen, keine Namen, keine
  Tracking-Cookies, keine Weitergabe an Dritte außer den
  E-Mail-Versanddienstleistern (AVV vorhanden, EU-Server).
- **Kein Kontakt zur Behörde.** Es findet keine Buchung statt und es werden
  keine Daten an die Stadt übermittelt. Abgefragt wird ausschließlich die
  öffentliche Terminübersicht — ununterscheidbar von einem Seitenaufruf.

## Verbleibendes Risiko

Das Restrisiko ist die Kompromittierung des Servers oder eines
E-Mail-Postfachs. Im ersten Fall wären Paare aus E-Mail-Adresse und Anliegen
lesbar. Dagegen wirken die kurze Speicherdauer, die Beschränkung auf genau
zwei Felder und der Umstand, dass keine weiteren Identifikatoren (Name,
Anschrift, Geburtsdatum, IP) vorliegen. Im zweiten Fall greift die Redaktion
der E-Mail-Inhalte: dort steht das Anliegen nicht.

## Umsetzungsstand

Die technische Umsetzung ist vollständig vorhanden. Freigeschaltet werden die
betroffenen Anliegen erst nach Abstimmung mit dem Datenschutz der jeweiligen
Stadt; bis dahin sind sie im Katalog als nicht buchbar hinterlegt
(`exclude_services`) und erscheinen nicht im Formular.

Der Quellcode ist offen: <https://github.com/jakubwaller/buergerwecker>
(AGPLv3).
