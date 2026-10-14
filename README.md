# ES-Restore

Holt Dateien anhand ihres FLOW-Hashs über die ARK-API vom LTO-Band in einen Zielordner. Besteht aus dem Modul `restorer.py` und dem Marathon-Worker `restore_marathon_<version>.exe`.

## Begriffe

| Begriff | Bedeutung |
|---|---|
| Marathon-Job | Job-Datei von Marathon; wird im Worker zu genau einem Restorer-Job |
| Restorer-Job | Auftrag an den Restorer: Dateien, Zielordner, eindeutiger `key`; ein Ergebnis je Restorer-Job |
| Ark-Job | Restore-Job in Ark (Session-ID); aus einem Restorer-Job werden ein oder mehrere Ark-Jobs |
| Ark-Queue | alle wartenden Jobs in Ark (`Enqueued`), auch fremde |

## Voraussetzungen

- Python 3.11 oder neuer, Zugriff auf das Paket `toolbox` (bringt ArkAPI und FlowAPI mit)
- Netzwerkzugriff auf Ark und die Ziel-Freigabe
- Nur Marathon-Worker: Marathon und Marathon-Adapter ab 1.2.0 mit CR-006 bis CR-010 (`schema_version` 6)

## Installation

```bat
cd ES-Restore
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

Zum Bauen der EXE zusätzlich:

```bat
pip install -e ".[build]"
```

## Zugangsdaten (`cred.env`)

Je Programm genau eine Datei; sie gehört nicht ins Repository.

| Datei | genutzt von |
|---|---|
| `cred.env` im Root | `restorer.py` (Standard für `cred_path`) |
| `marathon/cred.env` | Marathon-Worker; wird beim Bauen in die EXE gelegt |

```ini
ARK_HOST=<ark-host>
ARK_USER=<benutzer>
ARK_PASSWORD=<passwort>
```

## Modul `restorer.py`

### Beispiel

```python
from restorer import RestorerJob, run

def on_result(job, result):
    print(job.key, result.state, result.summary)

job = RestorerJob(
    key="restore-0001",
    items=[{
        "clip_id": "4711",
        "clip_name": "Mein Clip",
        "file": "A001_C002.mxf",
        "flow_hash": "2:9f86d081884c7d65...",
        "tape": "LTO123L8",
        "size": 123456789,
    }],
    output_folder=r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\restore_test",
)
run([job], on_result)
```

### `run(jobs, on_result, *, …) -> None`

`run()` startet die Arbeitsschleife und gibt nichts zurück; jedes Ergebnis kommt über `on_result`.

| Parameter | Standard | Bedeutung |
|---|---|---|
| `jobs` | – | Liste oder Generator von `RestorerJob(key, items, output_folder)`. Der nächste Restorer-Job wird erst geholt, wenn ein neuer Ark-Job gesendet werden darf. Ein Generator liefert `None`, wenn gerade keiner vorliegt; dann wird bei der nächsten Abfrage erneut gefragt. Endet die Liste bzw. der Generator, verfolgt `run()` die laufenden Ark-Jobs bis zum Ende und kehrt zurück. |
| `on_result(job, result)` | Pflicht | wird je fertigem Restorer-Job mit seinem `RestorerResult` aufgerufen |
| `on_log(message)` | Konsole mit Uhrzeit und `log_path` | Ereignisse: Start, Statuswechsel, Stopp, Ende, Fehler |
| `on_status(lines)` | Konsole | Statusblock, je verwaltetem Ark-Job eine Zeile, bei jeder Abfrage neu; der Block wird an Ort und Stelle überschrieben und nicht geloggt |
| `cred_path` | `cred.env` neben `restorer.py` | Zugangsdaten |
| `journal_path` | `restorer.journal.json` neben `restorer.py` | Journal (siehe unten) |
| `log_path` | `restorer.log` neben `restorer.py` | Log; nur ohne `on_log` |
| `poll_seconds` | `10` | Abfrageintervall für Steuerung und Überwachung |
| `max_ark_queue` | `0` | Steuerung der Ark-Queue (siehe unten) |
| `stall_minutes` | `60` | Zeit ohne übertragene Bytes im Status `Running`, nach der ein Ark-Job gestoppt und geteilt wird; wartende Ark-Jobs zählen nicht |
| `reserve_gb` | `50` | Platz in GB (10^9 Byte), der auf der Ziel-Freigabe frei bleiben muss |

### `max_ark_queue`

| Wert | Verhalten |
|---|---|
| `0` | Neue Ark-Jobs werden sofort gesendet, unabhängig von der Ark-Queue. |
| `1` | Gedrosselt: Es wird nur gesendet, wenn in der Ark-Queue kein Job wartet, auch kein fremder. Je Abfrage höchstens ein neuer Ark-Job. |
| `n > 1` | Bei jeder Abfrage wird die Ark-Queue auf n wartende Jobs aufgefüllt; sind es schon n oder mehr, wird nichts gesendet. |

Ein eigener Ark-Job, für den Ark noch keine Session-ID geliefert hat, zählt als wartend. Ark startet wartende Jobs selbst, sobald ein Laufwerk frei ist.

### Einträge in `items` (je Datei einer)

| Feld | Inhalt |
|---|---|
| `clip_id` | Einträge mit gleicher `clip_id` gehören zu einem Clip |
| `clip_name` | Name des Clip-Ordners im Ziel |
| `file` | physischer Dateiname aus dem Userpath, nicht Clip- oder Asset-Name |
| `flow_hash` | FLOW-Hash Version 2, Form `2:<hex>` |
| `tape` | Barcode des Bands |
| `size` | Dateigröße in Byte |

### Zielordner

- `output_folder` muss ein UNC-Pfad `\\server\freigabe\…` sein; Laufwerksbuchstaben werden abgelehnt.
- Der Freigabename muss einem aktiven Ark-Media-Space entsprechen, ohne Groß-/Kleinschreibung; eine Endung `_<Zahl>` darf fehlen. Beispiel: `Ablage KI Proxy_1` → Media Space `Ablage KI Proxy`.
- Ablage: `<output_folder>/<Clip-Ordner>/<file>`. Clip-Ordner = `clip_name`; `<>:"/\|?*` und Steuerzeichen werden zu `_`, Punkte und Leerzeichen am Ende entfallen. Ergibt sich derselbe Ordner für verschiedene `clip_id`, erhalten weitere Clips `__2`, `__3` … Dateinamen bleiben unverändert.
- Dateien, die schon vollständig im Ziel liegen, werden nicht erneut geholt.
- Ein Ark-Job startet nur, wenn auf der Freigabe genug Platz für die offenen Dateien des Restorer-Jobs plus `reserve_gb` frei ist.

### Ergebnis `RestorerResult`

| Feld | Inhalt |
|---|---|
| `key` | wie im Restorer-Job |
| `state` | `ok` (alle Dateien da), `partial` (ein Teil), `failed` (keine) |
| `summary` | Kurztext |
| `files` | je Eintrag die Felder aus `items` plus `state` (`restored`/`failed`), `reason`, vollständiger `path` |
| `ark_jobs` | je Ark-Job: Session-ID, Band, Hashes, Ark-Status, Start, Dauer, Versuch |
| `versions` | wie `versions()` |

Eine Datei ist `restored`, wenn sie im Ziel existiert, ihre Größe `size` entspricht und sie nicht auf `.tmp`, `.part` oder `.partial` endet.

Gründe in `reason` (Kennung plus Kurztext):

| Kennung | Bedeutung |
|---|---|
| `INVALID_INPUT` | Pflichtfeld fehlt, Hash-Format falsch, Zielpfad ungültig oder kein eindeutiger Media Space |
| `NOT_IN_ARK` | Hash in Ark nicht bekannt |
| `TAPES_OFFLINE` | Band nicht in der Library |
| `TAPE_MISMATCH` | angegebenes Band ist keine Kopie der Datei in Ark |
| `NO_SPACE` | zu wenig Platz auf der Freigabe |
| `ARK_ERROR` | Ark meldet einen Fehler |
| `STALLED` | Datei bleibt auch einzeln hängen |
| `INCOMPLETE` | Datei im Ziel hat nicht die erwartete Größe |
| `DUPLICATE_FILE` | gleicher Dateiname im selben Clip-Ordner mit anderem Hash |
| `ORPHANED` | Ark-Job verwaist (Ark kennt ihn nicht mehr) und Datei fehlt oder ist unvollständig |

Auf Bänder wird nicht gewartet: Fehlt ein Band in der Library, scheitern dessen Dateien sofort.

### `versions() -> dict`

Liefert die Versionen von Restorer und verwendeten Bibliotheken.

### Verhalten im Betrieb

- Je Band bzw. Bandkombination ein Ark-Job.
- Ein Ark-Job wird gestoppt, wenn im Status `Running` `stall_minutes` lang keine Bytes fließen, ein Band-Alarm zu seinem Band kommt oder Ark `Error` meldet. Seine offenen Dateien werden halbiert neu eingereiht, bis die hängende Datei isoliert ist (`STALLED`).
- Reihenfolge beim Senden: zuerst Halbierungen und Wiederholungen, dann weitere Bänder des aktuellen Restorer-Jobs, dann der nächste Restorer-Job.
- Die Konsole zeigt alle verwalteten Ark-Jobs gleichzeitig, je Ark-Job eine Zeile, z. B.:

```text
restorejob_ab12  LTO123L8  restore-0001  Running   981 MiB/14.1 GiB  Rest 6h:52m  Versuch 1
restorejob_cd34  LTO124L8  restore-0002  Enqueued  0 of 3 files                     Versuch 1
```

### Journal und Wiederaufnahme

- Das Journal enthält alle offenen Ark-Jobs des Restorers und ihren Restorer-Job (`key`). Nur darüber werden Ark-Jobs zugeordnet; Ark kennt keine Job-Namen.
- Beim Start werden alle Ark-Jobs aus dem Journal in Ark geprüft:
  - läuft oder wartet: Der Restorer verbindet sich wieder und überwacht weiter.
  - fertig: wird ausgewertet.
  - verwaist (Ark kennt die Session-ID nicht): Der Ausgangsordner entscheidet. Liegen die Dateien vollständig dort, ist der Ark-Job abgeschlossen; sonst ist er gescheitert (`ORPHANED`). Es wird kein neuer Ark-Job angelegt.
- Restorer-Jobs, deren `key` im Journal steht, werden nach dem Start sofort angenommen, unabhängig von `max_ark_queue`, und mit ihren Ark-Jobs verbunden. Nur Dateien ohne Ark-Job werden neu angefordert.
- Kommt ein solcher Restorer-Job nicht erneut, werden seine Ark-Jobs nicht gestoppt, sondern bis zum Ende verfolgt und im Log gemeldet.
- Kommt ein Restorer-Job, dessen `key` gerade bearbeitet wird, ein zweites Mal: kein neuer Ark-Job, Meldung im Log, das Ergebnis kommt einmal.
- Kommt ein Restorer-Job nach seinem Abschluss erneut, ist er ein neuer Auftrag; vollständig vorhandene Dateien zählen als `restored`.
- Abgeschlossene Einträge werden automatisch entfernt: die eines Restorer-Jobs, sobald er fertig ist und `on_result` zurückgekehrt ist; Ark-Jobs ohne erneuten Aufruf, sobald sie beendet und im Log gemeldet sind.
- Ein Journal darf nur von einem Prozess genutzt werden. Nicht bearbeiten oder löschen, solange eigene Ark-Jobs laufen.

## Marathon-Worker

### Einstellungen

Im Abschnitt `CONFIG` von `marathon/restore_marathon.py`; Änderungen wirken nach einem neuen Build.

| Eintrag | Wert | Bedeutung |
|---|---|---|
| `root_path` | `\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit` | Marathon-Freigabe |
| `work_dir` | `.marathon` | Arbeitsordner von Marathon |
| `stage` | `Restore` | Stufe |
| `resume` | `True` | eigene offene Marathon-Jobs nach Neustart fortsetzen |
| `poll_seconds` | `10` | Abfrageintervall |
| `heartbeat_seconds` | `30` | Intervall des Heartbeats |
| `max_ark_queue` | `1` | immer ein Job in der Ark-Queue, kein Leerlauf, keine Flut |
| `stall_minutes` | `60` | wie im Modul |
| `reserve_gb` | `50` | wie im Modul |

### Bauen

`marathon/cred.env` muss vorhanden sein.

```bat
python marathon\build_restore.py
```

Ergebnis: `dist\restore_marathon_<version>.exe` mit eingebetteter `marathon/cred.env`.

### Starten und Beenden

```bat
restore_marathon_<version>.exe
```

- Keine Optionen. Log `restore_marathon.log` und Journal `restore_marathon.journal.json` liegen neben der EXE.
- Eine Instanz je Rechner; ein zweiter Start bricht ab.
- Der Worker übernimmt Marathon-Jobs aus `.marathon/restore/offen/`, sobald er einen neuen Ark-Job senden darf, und bearbeitet mehrere gleichzeitig.
- Strg+C beendet den Worker. Laufende Ark-Jobs laufen weiter; beim nächsten Start verbindet er sich wieder mit ihnen und setzt die offenen Marathon-Jobs fort.

### Report an Marathon

- `status`: `ok`, wenn alle Einträge `restored` sind, sonst `failed`; `result`: Kurztext.
- `items`: je Eintrag `clip_id`, `flow_hash`, `status` (`restored`/`failed`), `path` (relativ zu `root_path`, `/` als Trenner) und `reason`.
