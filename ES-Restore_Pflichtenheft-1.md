# ES-Restore – Pflichtenheft

Stand: 2026-10-13 · Phase: Planung · Pflichtenheft freigegeben · nächster Schritt: README zur Freigabe, danach Code

Zugehörige Schnittstelle: `INTERFACES-5.md` (CR-006 bis CR-009). Am Ende jedes Threads die aktualisierte INTERFACES-Datei zurückgeben (Regel 8 dort).

## 1. Ziel und Umfang

- `restorer.py`: Modul, holt Dateien per FLOW-Hash über die ARK-API vom LTO-Band in Zielordner. Universell nutzbar, kein Core.
- `marathon/restore_marathon.py`: Restore-Adapter, macht daraus einen Marathon-Worker (EXE).
- Nicht im Umfang: Bänder anfordern oder einlegen, Dateien umbenennen, Ark-Meldungen in der UI quittieren.

## 2. Projektstruktur

```
ES-Restore/
├── .gitignore               # cred.env (Root und marathon/), *.log, Journal, build/, dist/, __pycache__/, .venv
├── README.md
├── pyproject.toml           # Python ≥ 3.11; toolbox (bringt ArkAPI und FlowAPI mit); Extra "build": pyinstaller
├── cred.env                 # ARK_HOST, ARK_USER, ARK_PASSWORD; nur lokal
├── restorer.py              # Modul: run(), versions()
└── marathon/
    ├── __init__.py
    ├── cred.env             # nur lokal; wird beim Build in die EXE gelegt und dort verwendet
    ├── marathon_adapter.py  # unveränderte Kopie der Referenz, ab 1.2.0 (CR-001, CR-007, CR-008, CR-009)
    ├── restore_marathon.py  # Einstellungen, work(), Startpunkt der EXE
    └── build_restore.py     # baut dist/restore_marathon_<version>.exe
```

- Abhängigkeiten: toolbox → ArkAPI → FlowAPI (kein Kreis). Verbindung: `tb_link_api(cred_path, "ark")`.
- Je Programm genau eine `cred.env`; dafür ist der Programmierer verantwortlich.
- Laufzeitdateien: Log und Journal neben `restorer.py` bzw. neben der EXE (Pfade per Parameter änderbar).
- Codestil und Gliederung wie AQC (`IMPORTS`, `CONFIG` mit `app_name`/`app_version`, `MAIN`, `EXEC`); öffentliche Funktionen mit Signatur und Docstring, private mit `_`.

## 3. Schnittstelle des Moduls

```python
restorer.run(batches, *, on_result=None, cred_path=None, journal_path=None,
             log_path=None, on_progress=None, <Einstellwerte>) -> list[BatchResult]
```

| Parameter | Bedeutung |
|---|---|
| `batches` | Liste oder Generator von `Batch(key, items, output_folder)`; der nächste Batch wird erst geholt, wenn die Steuerung (7.) einen neuen Ark-Job erlaubt. Ein einzelner Restore: `run([batch])` |
| `on_result(batch, result)` | optional; wird je fertigem Batch aufgerufen |
| `cred_path` | Standard `cred.env` im Root |
| `journal_path` | Standard `restorer.journal.json` neben `restorer.py` |
| `log_path`, `on_progress(text, done)` | wie beim QC-Worker: Laufzeilen mit `done=False`, die nächste Zeile ersetzt sie |
| Einstellwerte | u. a. Abfrageintervall der Steuerung (niedrig, z. B. 10 s), Zeit ohne Fortschritt bis Abbruch, Platzreserve; Standardwerte im README festlegen |

Pflichtfelder je Eintrag in `items` (je Datei ein Eintrag):

| Feld | Inhalt |
|---|---|
| `clip_id` | Einträge mit gleicher `clip_id` gehören zu einem Clip |
| `clip_name` | Name des Clip-Ordners |
| `file` | physischer Dateiname aus dem Userpath, nicht Clip- oder Asset-Name |
| `flow_hash` | Version 2, `2:<hex>` (Ark nimmt nur Version 2) |
| `tape` | Barcode |
| `size` | Größe in Byte |

Ergebnis je Batch (`BatchResult`): `key`, `state` (`ok` / `partial` / `failed`), `summary`, `files` (je Eintrag: Felder oben, `state` `restored`/`failed`, `reason`, vollständiger `path`), `jobs` (je Ark-Job: Session-ID, Band, Hashes, Ark-Status, Start, Dauer, Versuch), `versions`.

Gründe (`reason`) als feste Kennung plus Kurztext: `INVALID_INPUT`, `NOT_IN_ARK`, `TAPES_OFFLINE`, `TAPE_MISMATCH` (Band passt nicht zu Arks Kopien), `NO_SPACE`, `ARK_ERROR`, `STALLED` (Querschläger nach Halbieren), `INCOMPLETE` (Größe stimmt nicht), `DUPLICATE_FILE` (gleicher Dateiname im selben Clip-Ordner mit anderem Hash).

## 4. Zielpfad, Media Space, Speicherplatz

- `output_folder` muss die Form `\\server\freigabe\…` haben; Laufwerksbuchstaben werden mit klarer Meldung abgelehnt.
- Media Space: `\\server` wird abgeschnitten. Freigabename gegen `esa_spaces[].name` aus `ark.get_mediaspaces()` (`GET /sources/mediaSpaces`, nur aktive Spaces) vergleichen, ohne Unterscheidung von Groß-/Kleinschreibung: erst genau, dann ohne Endung `_<Zahl>`. `Ablage KI Proxy_1` und `Ablage KI Proxy` ergeben beide `Ablage KI Proxy`. Kein oder mehrdeutiger Treffer: Abbruch vor dem Start. An Ark geht der Name aus der Ark-Liste.
- `restore_path` je Datei = Rest des Pfads mit `/` plus Clip-Ordner, z. B. `Proxy 10 Mbit/.marathon/…/<Clip-Ordner>/`. `restore_name` wird nie gesetzt.
- Speicherplatz: freier Platz am Root `\\server\freigabe\` (wie angegeben) gegen die Summe der noch offenen `size` des Batches plus Reserve, abzüglich der noch ausstehenden Bytes laufender eigener Jobs. Reicht er nicht: Batch `failed` mit `NO_SPACE`, kein Ark-Job.

## 5. Clip-Ordner

- Immer `<output_folder>/<Clip-Ordner>/<file>`. Clip-Ordner = `clip_name`; `<>:"/\|?*` und Steuerzeichen werden zu `_`, Punkte und Leerzeichen am Ende entfallen.
- Gleicher Clip-Ordner (ohne Groß-/Kleinschreibung) für verschiedene `clip_id` im selben Batch: erster ohne Zusatz, weitere `__2`, `__3` … in der Reihenfolge des ersten Auftretens.
- Dateinamen werden nie verändert.

## 6. Ablauf je Batch

1. Eingabe prüfen: Pflichtfelder, Hash-Format, Duplikate (stabile Reihenfolge), `DUPLICATE_FILE`, Pfadform.
2. Media Space und `restore_path` ermitteln (4.).
3. Vorab bei Ark (`check_restore`): in Ark, angegebenes Band unter den Kopien, Band in der Library. Sonst sofort `failed` mit Grund; es wird nicht auf Bänder gewartet.
4. Dateien, die schon vollständig im Ziel liegen, gelten als `restored` und werden nicht geholt.
5. Speicherplatz prüfen (4.).
6. Gruppen je Band bzw. Bandkombination bilden; je Gruppe ein Ark-Job (`restore_files`, Quelle Band, `require_all=True`), gestartet nach der Steuerung (7.). Ark wählt die Kopie selbst; die Gruppierung spart Bandwechsel, garantiert sie aber nicht.
7. Überwachen, bei Bedarf stoppen und halbieren (8.).
8. Nach jedem Ark-Job: Ark-Status auswerten (`Complete`, `Partially Complete`, `Error`, `Cancelled`; Fehlertext über `get_session_files` nach Abschluss) und jede Datei prüfen (10.).
9. Ergebnis über `on_result` und Rückgabe; Konsole und Log laufend.

## 7. Steuerung (alle Ark-Jobs gemeinsam)

- Eine Steuerung für alle eigenen Ark-Jobs: Bandgruppen, Halbierungen, mehrere Batches, aus dem Journal wieder aufgenommene Jobs.
- Regel: Bei jeder Abfrage (Abfrageintervall) wird genau ein weiterer Ark-Job gesendet, wenn in der Ark-Warteschlange kein Job wartet (Ark-weit, auch fremde Jobs; `current_status` beginnt mit `Enqueued`). Restzeit und Laufwerkszahl spielen keine Rolle; Ark startet wartende Jobs selbst, sobald ein Laufwerk frei ist.
- Wird der gesendete Job sofort verarbeitet, folgt bei der nächsten Abfrage der nächste. Die Warteschlange ist so höchstens ein Abfrageintervall lang leer.
- Eine eigene Anlage, für die Ark noch keine Session-ID geliefert hat, zählt als wartender Job; solange wird kein weiterer gesendet.
- Reihenfolge: zuerst Halbierungen und Wiederholungen, dann weitere Gruppen des aktuellen Batches, dann der nächste Batch.
- Die Last steuert der Caller (bei Marathon über die Zahl der ausgelegten Jobs).

## 8. Überwachung, Abbruch, Halbieren

- Abfrage über `get_jobs_active()` und `get_job_details(session_id, "active_jobs")`, Alarme über `get_tape_alerts()`.
- Fortschritt = übertragene Bytes (`spaces[].bytes` bzw. `progress_txt`), nicht die Dateizahl: `Running: (5 of 5 files)` zählt begonnene Dateien.
- Stillstand zählt nur im Status `Running`; `Enqueued` ist kein Stillstand.
- Abbruch bei: keine Byte-Änderung länger als eingestellt, Band-Alarm zu einem Band des Jobs seit Jobstart, Ark-Status `Error`. Dann `stop_job()` (mit laufendem Job getestet).
- Halbieren: offene Dateien des Jobs in zwei Hälften neu einreihen, rekursiv, bis eine einzelne Datei scheitert (`STALLED`). Unvollständige Dateien vorher behandeln (siehe 13., Testpunkt).

## 9. Journal und Wiederaufnahme

- Der Restorer führt immer ein Journal aller Ark-Jobs, die er anlegt. Je Eintrag: Status (`wird angelegt` mit Uhrzeit vor dem Anlegen, dann Session-ID), Hashes, Media Space, `restore_path`, Ausgang, Batch-`key`, Ergebnis. Schreiben über temporären Namen und Umbenennen.
- Beim Start immer alle offenen Einträge prüfen (`find_job`): läuft oder wartet → regulär weiter überwachen und in die Steuerung aufnehmen; fertig → auswerten; Ark kennt ihn nicht → Dateien gelten als offen.
- Batches, deren Jobs im Journal stehen, werden beim erneuten Aufruf mit gleichem `key` fortgesetzt. Ark-Jobs ohne erneuten Aufruf werden nicht gestoppt, sondern bis zum Ende verfolgt und im Log gemeldet.
- Absturz zwischen Anlegen und Session-ID: Kandidaten sind Ark-Restore-Jobs, die nach der Journal-Uhrzeit gestartet wurden und keinem Journal-Eintrag gehören. Zuordnung, wenn genau einer passt: Startzeit, Ziel-Media-Space (`current_space` bzw. `spaces[].name`), Dateizahl (`total_files`), gerundete Datenmenge (`spaces[].bytes`, nur Nebenmerkmal). Sonst warten, bis diese Kandidaten fertig sind, dann Ausgang prüfen und nur Fehlendes holen.

## 10. Vollständigkeit

Eine Datei ist `restored`, wenn `<output_folder>/<Clip-Ordner>/<file>` existiert, die Größe `size` entspricht und sie nicht auf `.tmp`, `.part`, `.partial` endet (Dateinamen ohne Groß-/Kleinschreibung verglichen).

## 11. Marathon-Adapter

- `root_path` = `\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit`, `work_dir` = `.marathon`, Stage `Restore`, `resume=True`.
- Nutzt `adapter.run_stream(settings, work)` (CR-009). `work()` ruft `restorer.run` mit einem Generator auf, der über `adapter.claim()` den nächsten Job holt; `on_result` meldet über `adapter.report(job_id, JobReport(status, result, preset, items))` (CR-007).
- Job → Batch: `key` = `job_id`, `items` aus dem Job (CR-006), `output_folder` aus `claim()`.
- Report: `status` `ok`, wenn alle Einträge `restored`, sonst `failed`; `items` je Eintrag `clip_id`, `flow_hash`, `status`, `path` (relativ zu `root_path`, `/`), `reason`; `result` als Kurztext.
- Zugangsdaten aus `marathon/cred.env` in der EXE; Log `restore_marathon.log` und Journal neben der EXE.
- Lauffähig mit Marathon erst, wenn CR-006 bis CR-009 umgesetzt sind.

## 12. Gesicherte Ark-Fakten (Ark 2026.3.0, getestet)

- `POST /restore/hashes`: nur `files[flow_hash, restore_path, restore_name]`, `destination[mediaspace | space_uuid]`, `source[ark_sources, prefer_source]`. Kein Job-Name; Ark vergibt `Restore Job`.
- Session-ID real: `restorejob_<kurz>-restorejob---YYYY-M-D-H-M-S-µs`, enthält die Startzeit.
- `get_jobs_active()` je Job: `id`, `current_status` (`Enqueued: (0 of 1 files)`, `Running: (5 of 5 files)`), `progress_txt` (`981 MiB/14.1 GiB, Est. 6 hours, 52 min, 22 sec`), `current_name` (aktuelle Datei), `current_space`, `current_speed`, `avg_speed`.
- `get_job_details(id, "active_jobs")`: `status`, `start`, `elapsed` (`0h:9m:18s`), `remaining` (`6h:52m:22s` bzw. `Processing`), `spaces[]` mit `name`, `status`, `number_files`, `bytes`, `total_files`, `total_bytes` (gröber gerundet). Mit `"active"`: Fehler 500.
- `get_session_files()` für laufende Jobs: Fehler 500 (`dbArkJobSessionDoesNotExist`).
- `stop_job()` stoppt auch laufende Jobs.
- `get_mediaspaces()`: `esa_spaces[].name` lesbar ohne `_1`; `esa_address` (10.0.77.13) weicht von der Freigabe-IP (10.0.77.11) ab, beide gültig.

## 13. Offen bzw. in der Entwicklung zu testen

- Format von `remaining` über 24 h (Tage?); nur für die Anzeige.
- Verhalten nach `stop_job`: Bleiben Teil-Dateien unter dem endgültigen Namen liegen, und überschreibt Ark beim nächsten Restore oder legt es eine neue Datei an? Davon hängt ab, ob der Restorer unvollständige Dateien vor dem Neustart löscht.
- Wurzel der Freigabe = Wurzel des Media Space (`restore_path` relativ dazu).
- Format von `get_tape_alerts()` und Zuordnung Alarm → Job/Band.
- Optional: Die UI-Meldung nach dem Stoppen per API quittieren. Möglich nur, wenn sie ein Tape-Alarm ist (`acknowledge_tape_alert`, ungetestet, braucht `confirm=True`); für Benachrichtigungen ist kein Endpunkt bekannt.
- ARK-API (eigenes Repo): `_short_id` trennt an `-__`, reale IDs nutzen `-` (betrifft `find_job` bei inaktiven Jobs und Meldungstexte); `get_job_details` und `stop_job` (laufend) als getestet markieren; README-Beispiele der Session-ID anpassen.

## 14. Erfolgskriterien

- Jede angefragte Datei ist am Ende `restored` mit Pfad oder `failed` mit Grund.
- Kein Ark-Job ohne ausreichenden Speicherplatz.
- Jeder Ark-Job enthält nur Dateien eines Bands bzw. einer Bandkombination.
- Ein hängender Job wird automatisch gestoppt und der Querschläger isoliert.
- Nach einem Neustart läuft kein Ark-Job doppelt oder ohne Zuordnung weiter.
- Neue Ark-Jobs werden nach der Regel in 7. gesendet; solange Batches vorliegen, ist die Ark-Warteschlange höchstens ein Abfrageintervall lang ohne wartenden Job.
- Konsole zeigt je Ark-Job Start, Status, Fortschritt, Restzeit und Ergebnis; das Log enthält alle Zeilen außer Laufzeilen.
