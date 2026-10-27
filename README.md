# ES-Restore

Restore-Worker für Marathon und Python-Modul zum Zurückholen von Dateien per FLOW-Hash über Ark. Der Restorer prüft Clips vor dem Senden, erstellt kleine Ark-Jobs und meldet die Ergebnisse über die bestehenden Marathon-Reports und Teilberichte.

## Installation und EXE-Bau

Python 3.11 oder neuer. Im Repo:

```console
pip config set global.extra-index-url https://artifacts.editshare.com/artifactory/api/pypi/editshare-pypi-public/simple
pip install -e ".[build]"
python marathon/build_restore.py
```

Für den Build `marathon/cred.env` lokal anlegen:

```dotenv
ARK_HOST=<host>
ARK_USER=<user>
ARK_PASSWORD=<password>
```

Die Zugangsdaten bleiben außerhalb von Git, werden aber wie bisher in die EXE eingebaut. Die EXE nur an berechtigte Rechner weitergeben.

Ergebnis: `dist/restore_marathon_0.3.0.exe` und daneben `blocked_tapes.txt`. Die Sperrdatei wird **nicht in die EXE eingebaut**. Beim erneuten Build bleibt eine bereits vorhandene Sperrdatei in `dist/` unverändert. Code, Launcher-Einstellungen oder Zugangsdaten ändern: neu bauen. Sperrliste ändern: **kein Neubau und kein Neustart nötig**.

## Start und Update

EXE und Sperrdatei gemeinsam in den Betriebsordner kopieren. Bei einem Update die dort bereits gepflegte `blocked_tapes.txt` behalten. Bestehendes `restore_marathon.journal.json` ebenfalls behalten; nicht löschen oder manuell bearbeiten.

Als Skript im Repo:

```console
python -m marathon.restore_marathon
```

Der Rechner benötigt Zugriff auf Ark und Lese-/Schreibzugriff auf die Marathon-Freigabe sowie auf Sperrdatei und Journal. Je Rechner darf genau ein Restore-Worker laufen. Beenden mit `Strg+C`: Bereits angelegte Ark-Jobs laufen weiter und werden beim nächsten Start aus dem Journal übernommen.

Neben der EXE, beim Skriptstart in `marathon/`:

| Datei | Bedeutung |
|---|---|
| `blocked_tapes.txt` | Manuelle und automatisch ergänzte Bandsperren |
| `restore_marathon.log` | Ereignisse, Vorprüfungsgründe, automatische Sperren, Fehler |
| `restore_marathon.journal.json` | Wiederaufnahme und bereits gemeldete Clips |

Voraussetzung bleibt Marathon mit Zustandsschema 8 und Jobschema 6 sowie Marathon-Adapter 1.3.0. Es ist **keine Anpassung oder Migration von `marathon.json` nötig**. Der gemeinsame `marathon/marathon_adapter.py` bleibt unverändert.

## Marathon konfigurieren

Marathon bleibt unverändert. In seiner vorhandenen Config:

```ini
[limits]
restore_big_job = 0
restore_small_job = 2
```

`[restore] small_job_clips` begrenzt die Clips je kleinem Marathon-Job. Die vorhandene Marathon-Auswahlregel und Fehlerzählung bleiben bestehen. Zwei kleine Marathon-Jobs sind nicht dasselbe wie zwei gleichzeitig laufende Ark-Jobs; Ark-Jobs unterliegen zusätzlich Queue-, Band- und Speicherplatzsteuerung.

## Sperrliste pflegen

`blocked_tapes.txt` ist eine UTF-8-Datei. Eine Bandkennung je Zeile; leere Zeilen und Kommentare mit `#` sind erlaubt. Groß-/Kleinschreibung wird ignoriert.

```text
# Manuelle Sperren
LS2044L8 # Band fehlt
LS2048L8 # Auffälliges Medium, vorerst nicht verwenden
```

Die mitgelieferte Startliste enthält zehn als fehlend bzw. außerhalb der Library gemeldete Bänder und zwei Bänder mit Media-Alarm aus den bereitgestellten Protokollen vom 23.–25. Oktober 2026. Das sind **historische Befunde**, kein Nachweis des heutigen Zustands. Nach Prüfung betreffende Einträge manuell entfernen. Laufwerksfehler wurden nicht als Bandsperren übernommen.

- Vor neuen Ark-Jobs, auch vor Wiederholungen, wird die Datei erneut gelesen.
- Eine gesperrte Kennung in **irgendeiner gemeldeten Bandkopie** einer noch zu restaurierenden Datei stellt den gesamten Clip zurück. Eine andere verfügbare Kopie hebt die Sperre nicht auf.
- Eine nicht vollständig in der Library liegende Bandkopie stellt den Clip ebenfalls zurück, auch wenn eine andere Kopie verfügbar wäre.
- Die Datei muss vorhanden, lesbar und gültig sein. Fehler oder noch nicht speicherbare automatische Einträge pausieren neue Ark-Jobs; bereits angelegte Jobs werden weiter überwacht. Datei reparieren bzw. Schreibrechte herstellen: Die Prüfung wird automatisch erneut versucht.
- Doppelte Kennungen sind zulässig und wirken wie eine Sperre. Automatische Ergänzungen erzeugen keine neuen Duplikate und überschreiben keine manuellen Zeilen oder Kommentare.

### Automatische Ergänzungen

Der Restorer nutzt strukturierte Ark-Benachrichtigungen und Tape-Alarme:

| Befund | Automatische Sperre |
|---|---|
| Neues Ereignis „benötigtes Band fehlt“, mit Kennung und Zuordnung zu einem eigenen Restore | Ja, Grund „fehlend“ |
| Neuer Media-Alarm 4, mit Kennung, Tape-Job-Nummer und passendem Ereigniszeitpunkt seit dem aktuellen Programmstart für einen eigenen Restore | Ja, Grund „Medienfehler“ |
| Nur Zuordnung über vermutetes Band, alte/fremde Meldung oder fehlende eindeutige Kennung | Nein |
| Laufwerksdiagnose, allgemeiner Hard-Error, Stillstand oder unklare Session | Nein |

Neue Einträge erhalten Zeitpunkt und Grund, etwa `LS2048L8 # auto ...: Medienfehler (Tape-Alarm 4)`. Eine Sperre wird **nie automatisch entfernt**. Nach manueller Entfernung kann ein neuer eindeutiger Fehler dasselbe Band erneut sperren. Bereits im eigenen Journal gespeicherte, noch nicht dauerhaft eingetragene Bandbefunde werden bei der Wiederaufnahme nachgetragen.

## Restore-Ablauf

1. Eingaben, Zielpfad und Media Space prüfen. Bereits vollständig vorhandene Zieldateien werden nicht erneut angefordert.
2. Für die übrigen Dateien alle von Ark gemeldeten Bandkopien auf Sperren und Library-Anwesenheit prüfen. Nicht erreichbare oder unvollständige Ark-Auskunft: warten statt Clips endgültig scheitern lassen.
3. **Unsichere Clips vollständig zurückstellen, sichere Clips desselben Marathon-Jobs ausführen.** Eine fehlende/ungültige Datei verhindert auch das Anfordern der übrigen Dateien dieses Clips.
4. Normale Ark-Jobs nach dem vermuteten Band gruppieren und durch `max_files_per_ark_job` begrenzen. Die erste nichtleere Bandkopie aus Arks Auskunft ist die Planungsannahme – keine Garantie für Arks tatsächliche Auswahl. Eine über mehrere Bänder verteilte Datei erhält einen eigenen Mehrband-Job mit genau einer Datei.
5. Vor dem Senden erneut prüfen. Kopienwechsel können eine neue Aufteilung erforderlich machen. Queue-, Band- und Speicherplatzgrenzen gelten weiterhin.
6. Nach erkanntem Ark-Ende bis zu `finish_wait_minutes` auf vollständige Zieldateien warten. Erst danach Erfolg bzw. Fehler melden. Jeder Clip wird erst freigegeben, wenn alle seine Ark-Jobs beendet sind; danach fasst der Restorer seinen Ordner nicht mehr an.
7. Eindeutig beendete Jobs mit vorübergehenden Fehlern höchstens **einmal** wiederholen, weiterhin in kleinen Jobs. Keine rekursive Halbierung. Keine Wiederholung bei bekannten Bandproblemen oder manuellem Abbruch.

Eine nicht auffindbare oder nicht eindeutig zugeordnete Session wird **nicht automatisch neu gesendet oder allein deswegen als beendet gewertet**. Der Restorer sucht weiter. Sind alle erwarteten Zieldateien vollständig vorhanden, kann er deren Ergebnis übernehmen. Andernfalls erscheint „Session unklar; in Ark prüfen“. Das kann neue Jobs blockieren: In Ark den wirklichen Zustand prüfen; das Journal nicht zur Umgehung löschen.

„Sicher“ heißt nur „kein bekanntes Risiko festgestellt“. Unbekannte Medienfehler, unvollständige Ark-Metadaten und eine abweichende Kopienwahl bleiben möglich.

## Einstellungen im Launcher

`marathon/restore_marathon.py`:

| Eintrag | Standard | Bedeutung |
|---|---|---|
| `root_path` | vorhandene Marathon-Freigabe | Muss mit Marathons `root_path` übereinstimmen |
| `work_dir` | `.marathon` | Muss mit Marathons `work_dir` übereinstimmen |
| `stage` | `Restore` | Worker-Stufe |
| `resume` | `True` | Jobs nach Neustart wieder aufnehmen |
| `poll_seconds` | `10` | Sekunden zwischen Abfragen |
| `heartbeat_seconds` | `30` | Abstand der Lebenszeichen; 1–60 Sekunden |
| `max_ark_queue` | `1` | Nur senden, wenn kein Ark-Job wartet, auch kein fremder; `0` hebt diese Queue-Grenze auf |
| `max_files_per_ark_job` | `50` | Dateigrenze für normale Ark-Jobs; `None` bedeutet unbegrenzt, ohne Änderung der Kopienwahl |
| `stall_minutes` | `60` | Running ohne Fortschritt: Stopp |
| `reserve_gb` | `2000` | Platzreserve im Ziel in GB (10^9 Byte) |
| `finish_wait_minutes` | `5` | Nachlaufzeit für die Zielübergabe nach erkanntem Ark-Ende |
| `blocked_tapes_path` | `blocked_tapes.txt` neben EXE/Skript | Externe, laufend neu gelesene Sperrdatei |
| `app_version` | `0.3.0` | Version der EXE; Restorer-Paketversion ist `0.4.0` |

Die Bandbelegung basiert auf der Planungsannahme; normale eigene Ark-Jobs auf demselben vermuteten Band werden nicht parallel gesendet. Mehrband-Jobs sperren ihre gesamte vermutete Bandkombination. Neue Jobs warten bei zu wenig Speicherplatz; die Prüfung berücksichtigt ausstehende Restores zum selben Media Space und die Reserve.

## Ergebnisse für Marathon

Der bestehende Vertrag bleibt erhalten:

- Je Datei `clip_id`, `flow_hash`, `status` (`restored` oder `failed`), `path` und `reason`.
- Erfolg verlangt eine Datei im erwarteten Ziel mit exakt der angegebenen Größe und ohne temporäre Endung `.tmp`, `.part` oder `.partial`.
- Vorprüfung meldet `PREFLIGHT_BLOCKED`, `PREFLIGHT_OFFLINE` oder `PREFLIGHT_INVALID` als Fehlergrund; es gibt keinen neuen Marathon-Status.
- Ein gemischter Job hat `status=failed`. Marathon übernimmt trotzdem seine vollständigen erfolgreichen Clips. Zurückgestellte Clips werden wie bisher inaktiv und als fehlgeschlagener Versuch gezählt.
- Nach Beheben der Ursache mit Marathons vorhandenem `retry-restore` bewusst wieder freigeben. **Dieser Befehl gibt alle dafür geeigneten inaktiven Restore-Clips frei**, nicht nur Vorprüfungsfehler.
- Teilberichte je fertigem Clip; Abschlussreport enthält weiterhin jeden Job-Eintrag. Bereits gemeldete Clips werden nach Neustart nicht erneut angefordert oder gemeldet.

## Python-Modul verwenden

Ohne EXE genügt `pip install -e .`; Zugangsdaten in einer eigenen `cred.env` führen. Die Sperrdatei vorher anlegen und ihren Pfad übergeben:

```python
from restorer import RestorerJob, run

items = [{"clip_id": "4711", "clip_name": "Film A", "file": "A001.mov",
          "flow_hash": "2:" + "0" * 32, "tape": "TAPE001", "size": 12345}]

def on_result(job, result):
    print(result.key, result.state, result.summary)

run([RestorerJob("auftrag-1", items, r"\\server\share\restore")], on_result,
    cred_path="cred.env", journal_path="restorer.journal.json",
    blocked_tapes_path="blocked_tapes.txt", max_files_per_ark_job=50,
    finish_wait_minutes=5)
```

Beispielwerte durch eigene Daten ersetzen. `output_folder` muss ein UNC-Pfad sein und zur aktiven Ark-Freigabe passen; Laufwerksbuchstaben werden abgelehnt.

`run(jobs, on_result, *, on_log=None, on_status=None, on_clip=None, cred_path=None, journal_path=None, log_path=None, poll_seconds=10, max_ark_queue=0, stall_minutes=60, reserve_gb=50, max_files_per_ark_job=None, blocked_tapes_path=None, finish_wait_minutes=5)`

- `jobs`: Liste oder Generator von `RestorerJob(key, items, output_folder)`; eindeutiger `key`. Ein Generator darf vorübergehend `None` liefern.
- `on_result(job, result)`: einmal je abgeschlossenem Auftrag. `run()` liefert nichts zurück.
- `on_clip(job, files)`: optional, einmal je fertigem Clip. Danach bleibt dessen Ordner unangetastet.
- `on_log(message)` und `on_status(lines)`: optional, andernfalls Konsole/Log.
- `cred_path`, `journal_path`, `log_path`: bei Installation in einem anderen Projekt eigene beschreibbare Pfade verwenden. Standardpfade liegen im Installationsverzeichnis.
- `blocked_tapes_path=None`: keine externe Liste; automatisch erkannte Sperren wirken in der Sitzung. Für den Betrieb eine Datei übergeben. Der Marathon-Launcher tut das immer.
- `versions()`: Versionen von Restorer, toolbox und ArkAPI.

`items` enthält je Datei `clip_id`, `clip_name`, `file`, `flow_hash` (Version 2), `tape` und `size` (Byte). `RestorerResult` enthält `key`, `state` (`ok`, `partial`, `failed`), `summary`, `files`, `ark_jobs` und `versions`. `files` enthält die Eingabefelder plus Ergebnis, Grund und vollständigen Zielpfad.

## Lokale Tests

Ohne Ark-Zugriff, echte Zugangsdaten oder EXE-Bau:

```console
python -m unittest discover -s tests -v
```

Die Tests verwenden lokale temporäre Dateien und isolierte Ark-/Build-Platzhalter. Ein echter Windows-EXE-Bau und das Verhalten der Produktions-Ark-Installation müssen in der jeweiligen Umgebung geprüft werden.
