# ES-Restore

Holt Dateien per FLOW-Hash über die ARK-API vom LTO-Band in einen Zielordner. `restorer.py` ist universell einsetzbar. Der Marathon-Adapter macht daraus einen Restore-Worker für Marathon: Er übernimmt Restore-Jobs aus dem Pool, lässt die Dateien von Ark zurückholen und meldet je Datei das Ergebnis an Marathon.

## Aufbau

| Pfad | Aufgabe |
|---|---|
| `restorer.py` | Modul: `run()`, `versions()` |
| `cred.env` | Ark-Zugangsdaten für das Modul, nur lokal |
| `marathon/marathon_adapter.py` | allgemeiner Teil aller Marathon-Adapter, **nicht verändern** (ab 1.2.0) |
| `marathon/restore_marathon.py` | Restore-Adapter: Einstellungen, Einstieg der EXE |
| `marathon/cred.env` | Ark-Zugangsdaten für die EXE, nur lokal; wird beim Build in die EXE gelegt |
| `marathon/build_restore.py` | baut die EXE |

## Installation

Python 3.11+. Einmal je Rechner die Paketquelle der Flow-API eintragen:

```
pip config set global.extra-index-url https://artifacts.editshare.com/artifactory/api/pypi/editshare-pypi-public/simple
```

Dann im Projektordner:

```
pip install -e .            # toolbox mit ArkAPI und FlowAPI
pip install -e ".[build]"   # zusätzlich pyinstaller für den Build
```

## Zugangsdaten `cred.env`

```
ARK_HOST=<host>
ARK_USER=<user>
ARK_PASSWORD=<passwort>
```

Je Programm genau eine Datei: `cred.env` im Projektordner für das Modul, `marathon/cred.env` für die EXE. Beide stehen in `.gitignore` und werden nie eingecheckt. Die EXE enthält ihre Zugangsdaten und darf nur auf berechtigte Rechner.

## Marathon-Worker starten

Voraussetzung: Marathon mit `schema_version` 6 und Marathon-Adapter 1.2.0 (CR-006 bis CR-011 in `INTERFACES.md`). `marathon/marathon_adapter.py` ist eine unveränderte Kopie dieser Referenz.

Als Skript aus dem Projektordner:

```
python -m marathon.restore_marathon
```

Als EXE: `restore_marathon_<version>.exe` auf den Rechner kopieren und starten. Der Rechner braucht Lese- und Schreibzugriff auf die Marathon-Freigabe und Zugriff auf Ark.

- Der Worker heißt `<Rechnername>-Restore`. Je Rechner läuft ein Restore-Worker, daneben QC- und Transcode-Worker; ein zweiter Restore-Worker auf demselben Rechner bricht ab.
- Er übernimmt mehrere Restore-Jobs gleichzeitig. Einen neuen Job holt er nur, wenn die Steuerung einen neuen Ark-Job erlaubt (`max_ark_queue`).
- Beenden mit `Strg+C`. Laufende Ark-Jobs werden nicht gestoppt; beim nächsten Start werden die übernommenen Jobs und ihre Ark-Jobs aus dem Journal wieder aufgenommen.
- Log `restore_marathon.log` und Journal `restore_marathon.journal.json` neben der EXE bzw. in `marathon/`.

### Konsolenausgabe

Ereigniszeilen mit Uhrzeit (Übernahme, Ark-Job angelegt, Statuswechsel, Stopp, Ende, Fehler, Ergebnis) stehen in Konsole und Log. Darunter steht ein Statusblock mit einer Zeile je verwaltetem Ark-Job: Session, Band, `key`, Status, Fortschritt, Restzeit, Versuch. Er wird bei jeder Abfrage an Ort und Stelle erneuert und nie ins Log geschrieben.

Beispiel:

```text
restorejob_N9SMTL  LS2048L8  20261014T…__restore1  Running   981 MiB / 14.1 GiB  6h:52m:22s  Versuch 1
restorejob_K2PQRA  LS2051L8  20261014T…__restore2  Enqueued  –                   –           Versuch 1
```

Pfade werden in Konsole und Log ab einschließlich `.marathon` gekürzt (`.marathon\restore\…`). Report und Journal enthalten vollständige Pfade.

## EXE bauen

```
python marathon/build_restore.py
```

Ergebnis: `dist/restore_marathon_<app_version>.exe` mit Adapter, `restorer.py`, toolbox und `marathon/cred.env`. Änderungen an Einstellungen oder Zugangsdaten gelten erst mit einem neuen Build.

## Einstellungen in `restore_marathon.py`

| Eintrag | Wert | Bedeutung |
|---|---|---|
| `app_version` | | Version der EXE |
| `root_path` | `\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit` | Wurzel der Marathon-Freigabe, wie `root_path` in Marathons `res/config.ini` |
| `work_dir` | `.marathon` | Arbeitsordner von Marathon, wie `work_dir` in Marathons `res/config.ini` |
| `stage` | `Restore` | Stufe |
| `resume` | `True` | übernommene Jobs nach einem Neustart wieder aufnehmen statt als `failed` zu melden |
| `poll_seconds` | `10` | Abfrageintervall für Pool und Ark |
| `heartbeat_seconds` | `30` | Abstand der Lebenszeichen an Marathon, 1 bis 60 |
| `max_ark_queue` | `1` | Steuerung, siehe „Steuerung“ |
| `stall_minutes` | `60` | Minuten ohne Fortschritt im Status `Running` bis zum Stopp |
| `reserve_gb` | `50` | Platzreserve im Ziel in GB (10^9 Byte) |

## Ergebnis eines Marathon-Jobs

- Dateien: `<output_folder>/<Clip-Ordner>/<file>`.
- Report in `report_folder`: `<job_id>.json` mit `status`, `result` (Kurztext), `items`, `worker` und `versions`.
- `status`: `ok`, wenn alle Einträge `restored` sind, sonst `failed`.
- `items`: je Job-Eintrag genau einmal `clip_id`, `flow_hash`, `status` (`restored`/`failed`), `path` (relativ zu `root_path`, `/` als Trenner) und `reason`.
- `reason` einer gescheiterten Datei, höchstens 300 Zeichen: Grund, Band, wenn bekannt Laufwerk, kurze Session-ID und gekürzter Ark-Text, z. B. `TAPE_DEFECT: Media-Alarm auf LS2048L8, Laufwerk 1 (restorejob_N9SMTL)`.

Der Adapter verarbeitet nur Jobs mit `schema_version = 6`.

## Restorer direkt einbinden

```python
from restorer import RestorerJob, run

items = [{"clip_id": "4711", "clip_name": "Film A.mov", "file": "A001.mov",
          "flow_hash": "2:9f3c…", "tape": "LS2048L8", "size": 15142012345}]

def on_result(job, result):
    print(result.key, result.state, result.summary)

run([RestorerJob("auftrag-1", items, r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\restore")], on_result)
```

`run()` kehrt zurück, wenn `jobs` erschöpft ist und alle Ark-Jobs beendet sind. Es gibt nichts zurück; jedes Ergebnis kommt über `on_result`.

| Parameter | Standard | Bedeutung |
|---|---|---|
| `jobs` | Pflicht | Liste oder Generator von `RestorerJob(key, items, output_folder)`; `key` eindeutig. Ein Generator darf `None` liefern, wenn gerade kein Auftrag vorliegt, und wird bei der nächsten Abfrage erneut gefragt |
| `on_result(job, result)` | Pflicht | wird je fertigem Auftrag mit seinem `RestorerResult` aufgerufen |
| `on_log(message)` | Konsole mit Uhrzeit und `log_path` | Ereigniszeilen |
| `on_status(lines)` | Konsole | Statusblock, je Ark-Job eine Zeile; ohne echte Konsole nicht ausgegeben |
| `cred_path` | `cred.env` im Projektordner | Ark-Zugangsdaten |
| `journal_path` | `restorer.journal.json` neben `restorer.py` | Journal |
| `log_path` | `restorer.log` neben `restorer.py` | Log, nur ohne `on_log` |
| `poll_seconds` | `10` | Abfrageintervall |
| `max_ark_queue` | `0` | Steuerung, siehe „Steuerung“ |
| `stall_minutes` | `60` | Minuten ohne Fortschritt im Status `Running` bis zum Stopp |
| `reserve_gb` | `50` | Platzreserve im Ziel in GB (10^9 Byte) |

`versions()` liefert die Versionen von Restorer, toolbox und ArkAPI.

### Einträge in `items`

Je Datei ein Eintrag, alle Felder Pflicht:

| Feld | Inhalt |
|---|---|
| `clip_id` | Einträge mit gleicher `clip_id` gehören zu einem Clip |
| `clip_name` | Name des Clip-Ordners |
| `file` | physischer Dateiname aus dem Userpath, nicht Clip- oder Asset-Name |
| `flow_hash` | FLOW-Hash Version 2, `2:<hex>` |
| `tape` | Barcode des Bands |
| `size` | Größe in Byte |

### Ergebnis `RestorerResult`

| Feld | Inhalt |
|---|---|
| `key` | wie im Auftrag |
| `state` | `ok` (alle Dateien `restored`), `partial` (einige), `failed` (keine) |
| `summary` | Kurztext |
| `files` | je Eintrag die Felder aus `items` plus `state` (`restored`/`failed`), `reason`, vollständiger `path` |
| `ark_jobs` | je Ark-Job Session-ID, Band, Hashes, Ark-Status, Start, Dauer, Versuch, `reason` (Grund für eigenen Stopp oder Ark-Text, sonst `null`) |
| `versions` | wie `versions()` |

## Zielpfad und Clip-Ordner

- `output_folder` muss die Form `\\server\freigabe\…` haben; Laufwerksbuchstaben werden abgelehnt.
- Die Freigabe muss einem aktiven Media Space in Ark entsprechen, ohne Groß-/Kleinschreibung, zuerst genau, dann ohne Endung `_<Zahl>`: `Ablage KI Proxy_1` → `Ablage KI Proxy`. Kein oder mehrere Treffer: Auftrag `failed` mit `INVALID_INPUT`.
- Ziel jeder Datei: `<output_folder>/<Clip-Ordner>/<file>`. Clip-Ordner = `clip_name`; `<>:"/\|?*` und Steuerzeichen werden zu `_`, Punkte und Leerzeichen am Ende entfallen. Gleicher Clip-Ordner für verschiedene `clip_id` im selben Auftrag: erster ohne Zusatz, weitere `__2`, `__3` …
- Dateinamen werden nie verändert.
- Eine Datei ist `restored`, wenn sie am Ziel liegt, ihre Größe `size` entspricht und sie nicht auf `.tmp`, `.part` oder `.partial` endet. Solche Dateien werden nicht erneut geholt, auch wenn ihr Band nicht in der Library liegt.

## Ablauf je Auftrag

1. Eingabe prüfen: Pflichtfelder, Hash-Format, doppelte Einträge, Zielpfad.
2. Bereits vollständige Dateien als `restored` übernehmen.
3. Bei Ark prüfen: Datei bekannt, angegebenes Band unter ihren Kopien, Band in der Library. Sonst sofort `failed`; auf Bänder wird nicht gewartet.
4. Speicherplatz prüfen: freier Platz an `\\server\freigabe\` abzüglich der noch ausstehenden Bytes laufender eigener Ark-Jobs muss für die offenen Dateien plus `reserve_gb` reichen. Sonst `failed` mit `NO_SPACE`, kein Ark-Job.
5. Je Band bzw. Bandkombination ein Ark-Job, gesendet nach der Steuerung.
6. Überwachen, bei Bedarf stoppen, neu senden oder halbieren (siehe „Fälle“).
7. Jede Datei prüfen und Ergebnis über `on_result` melden.

## Steuerung

Gilt für alle eigenen Ark-Jobs gemeinsam. Die Ark-Queue umfasst alle wartenden Ark-Jobs (`Enqueued`), auch fremde; ein eigener Ark-Job ohne Session-ID zählt mit.

| `max_ark_queue` | Verhalten |
|---|---|
| `0` | neue Ark-Jobs werden sofort gesendet |
| `1` | gesendet wird nur, wenn kein Ark-Job wartet, auch kein fremder (Einstellung für Marathon) |
| `n` | gesendet wird, bis die Ark-Queue `n` Jobs enthält |

Reihenfolge: zuerst Halbierungen und Neusendungen, dann weitere Bänder des aktuellen Auftrags, dann der nächste Auftrag. Mehr Durchsatz: `max_ark_queue` erhöhen oder `0`.

## Fälle

Halbieren: Die offenen Dateien eines Ark-Jobs werden in zwei neuen Ark-Jobs angefordert, wiederholt, bis eine einzelne Datei scheitert. Unvollständige Dateien am Ziel werden vorher gelöscht.

| Fall | Aktion | Grund der Datei |
|---|---|---|
| Band fehlt während des Laufs | stoppen | `TAPES_OFFLINE` |
| Band defekt (Tape-Alarm `Media`) | stoppen, Band nicht erneut anfordern | `TAPE_DEFECT` |
| Laufwerk braucht Diagnose (Tape-Alarm `Diagnostics Required`) | stoppen, einmal unverändert neu senden | `DRIVE_ERROR` |
| Lese-/Schreibfehler (Tape-Alarm `Hard error`) | stoppen, halbieren | `ARK_ERROR` |
| Fehler im Lauf (Status `Error`) | stoppen, halbieren | `ARK_ERROR` |
| Kein Fortschritt im Status `Running` für `stall_minutes` | stoppen, halbieren | `STALLED` |
| Ende `Partially Complete` | fehlende Dateien halbieren | `ARK_ERROR` |
| Ende `Complete`, Datei fehlt oder Größe falsch | halbieren | `INCOMPLETE` |
| Ende `Unknown` | einmal unverändert neu senden | `ARK_ERROR` |
| Ende `Insufficient Space` | – | `NO_SPACE` |
| in Ark von Hand abgebrochen | – | `CANCELLED` |

Tape-Alarme, die über die Tape-Job-Nummer sicher zu einem eigenen Ark-Job gehören, werden nach dessen Ende in Ark quittiert, außer Nr. 4 „Media“ und Nr. 39 „Diagnostics Required“. Alarme fremder Jobs werden nie quittiert. Jede Quittierung steht im Log.

Weitere Gründe: `INVALID_INPUT` (Eingabe, Zielpfad, Media Space), `NOT_IN_ARK`, `TAPE_MISMATCH` (Band nicht unter Arks Kopien), `DUPLICATE_FILE` (gleicher Dateiname im selben Clip-Ordner mit anderem Hash; alle betroffenen Einträge scheitern), `ORPHANED` (Ark-Job nach Neustart in Ark unbekannt, Datei fehlt oder unvollständig). Jede Aktion steht mit Quelle und Ark-Text im Log.

## Journal und Neustart

- Das Journal enthält alle angelegten Ark-Jobs mit `key` und Ergebnis. Nur darüber werden Ark-Jobs nach einem Neustart ihrem Auftrag zugeordnet. Nicht löschen oder bearbeiten, solange Einträge offen sind.
- Ein Journal wird von genau einem Prozess genutzt.
- Beim Start werden offene Ark-Jobs wieder überwacht oder ausgewertet. Aufträge mit `key` im Journal werden sofort angenommen; nur Dateien ohne Ark-Job werden neu angefordert.
- Kommt ein Auftrag nach dem Neustart nicht erneut, laufen seine Ark-Jobs bis zum Ende weiter und werden im Log gemeldet.
- Ein `key`, der gerade bearbeitet wird, wird verworfen (Meldung im Log). Abgeschlossene Einträge werden automatisch entfernt; derselbe `key` danach ist ein neuer Auftrag.

## Grenzen

- Bänder werden weder angefordert noch eingelegt; fehlende Bänder führen zu `TAPES_OFFLINE`.
- Dateien werden nicht umbenannt.
- Benachrichtigungen in Ark werden nicht quittiert (dafür gibt es keinen Endpunkt); quittiert werden nur eigene Tape-Alarme (siehe „Fälle“).
