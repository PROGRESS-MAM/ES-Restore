# --------- IMPORTS ---------
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
import ctypes
import json
import math
import os
import queue
import re
import shutil
import sys
import threading
import time
import uuid

import ArkAPI
import toolbox
from toolbox import tb_link_api, tb_write_log


# --------- CONFIG ---------
app_name = "ES-Restore"
app_version = "0.3.0"

project_folder = Path(__file__).resolve().parents[1]   # installed: site-packages, callers pass their paths
default_cred_path = project_folder / "cred.env"
default_journal_path = project_folder / "restorer.journal.json"
default_log_path = project_folder / "restorer.log"

ark_sources = ["tape"]
lost_search_minutes = 15            # minimum search for an Ark job whose creation got no answer
not_found_limit = 3                 # polls without hit before a new Ark job counts as orphaned
restop_minutes = 5
mediaspace_cache_minutes = 5
clock_tolerance_seconds = 120       # Ark clock vs. local clock when matching a job without session id
bytes_tolerance = 0.15              # rounded total_bytes of Ark vs. sum of sizes
ark_text_chars = 160


# --------- STATIC ---------
reason_texts = {
    "INVALID_INPUT": "Eingabe ungültig",
    "NOT_IN_ARK": "nicht in Ark",
    "TAPES_OFFLINE": "Band nicht in der Library",
    "TAPE_MISMATCH": "Band passt nicht zu Arks Kopien",
    "NO_SPACE": "zu wenig Speicherplatz im Ziel",
    "ARK_ERROR": "Fehler in Ark",
    "STALLED": "kein Fortschritt",
    "INCOMPLETE": "Datei fehlt oder Größe falsch",
    "DUPLICATE_FILE": "gleicher Dateiname im Clip-Ordner mit anderem Hash",
    "ORPHANED": "Ark-Job verwaist, Datei fehlt oder unvollständig",
    "CANCELLED": "Ark-Job in Ark von Hand abgebrochen",
    "TAPE_DEFECT": "Band meldet Medienfehler",
    "DRIVE_ERROR": "Laufwerk meldet Fehler",
}
stop_ranks = {code: rank for rank, code in enumerate(("STALLED", "ARK_ERROR", "DRIVE_ERROR", "TAPES_OFFLINE", "TAPE_DEFECT"))}
item_fields = ("clip_id", "clip_name", "file", "flow_hash", "tape", "size")
text_fields = item_fields[:-1]
invalid_name_chars = frozenset('<>:"/\\|?*' + "".join(map(chr, range(32))))
partial_suffixes = (".tmp", ".part", ".partial")
hash_format = re.compile(r"2:[0-9A-Fa-f]{32}")
drive_letter = re.compile(r"^[A-Za-z]:")
byte_factors = {**{unit.casefold(): 1000 ** power for power, unit in enumerate(("B", "KB", "MB", "GB", "TB", "PB"))},
                **{unit.casefold(): 1024 ** power for power, unit in enumerate(("B", "KiB", "MiB", "GiB", "TiB", "PiB"))}}
byte_amount = re.compile(r"(\d+(?:[.,]\d+)?)\s*([KMGTP]?i?B)\b", re.IGNORECASE)
session_time = re.compile(r"---(\d{4})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})(?:-(\d{1,6}))?$")
alert_hard_error, alert_media, alert_diagnostics = 3, 4, 39
alert_ranks = {alert_hard_error: 0, alert_diagnostics: 1, alert_media: 2}
kept_alerts = frozenset({alert_media, alert_diagnostics})
tape_missing_tag = "ark_tape_next_required_tape_missing"
notification_job_keys = ("job_id", "action_id", "queue_restore_job_id")
pending_states = {"pending": "wartet auf Steuerung", "creating": "wird angelegt", "lost": "Session-ID wird gesucht"}


# --------- TYPES ---------
@dataclass
class RestorerJob:
    """One order: items (one dict per file) are restored to output_folder; key must be unique."""
    key: str
    items: list[dict]
    output_folder: str | Path


@dataclass
class RestorerResult:
    """Result of one RestorerJob; state is ok (all restored), partial or failed (none restored)."""
    key: str
    state: str
    summary: str
    files: list[dict]
    ark_jobs: list[dict]
    versions: dict[str, str]


@dataclass(eq=False)
class _File:
    flow_hash: str
    size: int
    tape: str
    target: Path
    restore_path: str
    copies: list[list[str]] = field(default_factory=list)
    group: tuple[str, ...] = ()
    state: str = "open"                     # open | restored | failed
    reason: str | None = None
    covered: bool = False                   # handled by an Ark job from the journal


@dataclass(eq=False)
class _Task:
    job: RestorerJob
    entries: list[dict] = field(default_factory=list)
    files: list[_File] = field(default_factory=list)
    ark_jobs: list["_ArkJob"] = field(default_factory=list)
    defect_tapes: set[str] = field(default_factory=set)
    reported: set[str] = field(default_factory=set)        # clip_ids already handed to on_clip
    problem: str | None = None
    mediaspace: str = ""
    share_root: str = ""


@dataclass(eq=False)
class _ArkJob:
    key: str
    files: list[_File]
    mediaspace: str
    share_root: str
    attempt: int = 1
    task: _Task | None = None
    jid: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "pending"                  # pending | creating | lost | active | done
    session_id: str | None = None
    created_at: datetime | None = None
    start: datetime | None = None
    end: datetime | None = None
    status: str = ""
    progress: str = ""
    remaining: str = ""
    transferred: int = 0
    stall_since: float = field(default_factory=time.monotonic)
    not_found: int = 0
    from_journal: bool = False
    stop_code: str | None = None
    stop_reason: str | None = None
    stop_sent: float | None = None
    resent: set[str] = field(default_factory=set)
    process_ids: set[str] = field(default_factory=set)
    acks: list[tuple[str, dict]] = field(default_factory=list)
    ark_status: str = ""
    ark_text: str = ""
    results: dict[str, str | None] = field(default_factory=dict)    # target -> None (restored) or reason
    decided: bool = False
    tape_wait: bool = False                 # waiting for a tape held by another own Ark job, logged once
    space_wait: str | None = None           # missing space while waiting to be sent

    @property
    def short(self) -> str:
        return _short_id(self.session_id) if self.session_id else "–"

    @property
    def tapes(self) -> str:
        return "+".join(self.tape_list()) or "–"

    def tape_list(self) -> list[str]:
        return list(dict.fromkeys(tape for f in self.files for tape in (f.group or (f.tape,))))

    def tape_set(self) -> set[str]:
        return {tape.casefold() for tape in self.tape_list()}

    def copy_tapes(self) -> set[str]:
        return {tape.casefold() for f in self.files for copy in f.copies or [[f.tape]] for tape in copy}

    def remaining_bytes(self) -> int:
        return max(0, sum(f.size for f in self.files if f.state == "open") - self.transferred)


# --------- FUNC HELPERS ---------
def _ok(result) -> bool:
    return 200 <= result.code < 300


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def _short_id(session_id: str) -> str:
    return str(session_id).split("-__", 1)[0]


def _session_start(session_id: str | None) -> datetime | None:
    match = session_time.search(str(session_id or ""))
    if not match:
        return None
    *parts, micro = match.groups()
    try:
        return datetime(*map(int, parts), int((micro or "0").ljust(6, "0")[:6]))
    except ValueError:
        return None


def _parse_date(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_bytes(text: str) -> int:
    """First amount in a text like '981 MiB / 14.1 GiB' in bytes, 0 if none."""
    match = byte_amount.search(str(text or ""))
    return int(float(match.group(1).replace(",", ".")) * byte_factors[match.group(2).casefold()]) if match else 0


def _gb(size: int) -> str:
    return f"{size / 1e9:.1f} GB"


def _reason(code: str, text: str | None = None, job: _ArkJob | None = None) -> str:
    return f"{code}: {text or reason_texts[code]}" + (f" ({job.short})" if job and job.session_id else "")


def _complete(f: _File) -> bool:
    try:
        return f.target.is_file() and f.target.stat().st_size == f.size and not f.target.name.casefold().endswith(partial_suffixes)
    except OSError:
        return False


def _free_bytes(share_root: str) -> int:
    return shutil.disk_usage(share_root).free


def _listed(data, *keys: str) -> list:
    """List from an Ark answer that is either a list or a dict holding the list under one of keys."""
    if isinstance(data, dict):
        data = next((data[key] for key in keys if isinstance(data.get(key), list)), [])
    return [entry for entry in data or [] if isinstance(entry, dict)]


# --------- FUNC INPUT ---------
def _split_target(output_folder) -> tuple[str, str, list[str]]:
    """(share root '\\\\server\\share\\', share name, folders below the share); ValueError for other forms."""
    text = str(output_folder or "")
    if drive_letter.match(text):
        raise ValueError(f"Zielpfad {text} mit Laufwerksbuchstaben wird nicht unterstützt, bitte als \\\\server\\freigabe\\… angeben")
    path = PureWindowsPath(text)
    names = path.drive[2:].split("\\")
    if not path.drive.startswith("\\\\") or len(names) != 2 or not all(names) or names[0] in ("?", "."):
        raise ValueError(f"Zielpfad {text!r} hat nicht die Form \\\\server\\freigabe\\…")
    rest = list(path.parts[1:])
    if any(part in (".", "..") for part in rest):
        raise ValueError(f"Zielpfad {text} enthält '.' oder '..'")
    return f"{path.drive}\\", names[1], rest


def _item_problem(item) -> str | None:
    if not isinstance(item, dict):
        return "Eintrag ist kein Objekt"
    if missing := [name for name in item_fields if name not in item]:
        return f"Pflichtfeld fehlt: {', '.join(missing)}"
    if empty := [name for name in text_fields if not isinstance(item[name], str) or not item[name].strip()]:
        return f"leer oder kein Text: {', '.join(empty)}"
    if not isinstance(item["size"], int) or isinstance(item["size"], bool) or item["size"] < 0:
        return f"size ist keine Byte-Zahl: {item['size']!r}"
    if not hash_format.fullmatch(item["flow_hash"]):
        return f"flow_hash ist kein Hash der Version 2 (2:<hex>): {item['flow_hash']!r}"
    if any(char in invalid_name_chars for char in item["file"]) or item["file"] in (".", ".."):
        return f"file ist kein gültiger Dateiname: {item['file']!r}"
    return None


def _clip_folder(clip_name: str, used: set[str]) -> str | None:
    """Clip folder for clip_name; the same folder for another clip gets __2, __3 …"""
    base = "".join("_" if char in invalid_name_chars else char for char in clip_name).rstrip(". ")
    if not base:
        return None
    folder, number = base, 1
    while folder.casefold() in used:
        number += 1
        folder = f"{base}__{number}"
    used.add(folder.casefold())
    return folder


def _mediaspace_name(share: str, names: list[str]) -> str:
    """Media space for a share name: exact match first, then both without '_<number>'."""
    def plain(name: str) -> str:
        return re.sub(r"_\d+$", "", name).casefold()
    exact = [name for name in names if name.casefold() == share.casefold()]
    found = exact or [name for name in names if plain(name) == plain(share)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ValueError(f"Freigabe {share} passt zu keinem aktiven Media Space in Ark")
    raise ValueError(f"Freigabe {share} passt zu mehreren Media Spaces: {', '.join(found)}")


# --------- FUNC CONSOLE ---------
def _enable_ansi() -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle, mode = kernel32.GetStdHandle(-11), ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)    # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (AttributeError, OSError):
        pass


class _Console:
    """Default output: event lines with time to console and log, status block only on a real console."""

    def __init__(self, log_path: Path) -> None:
        self.log_path, self.lines, self.lock = log_path, 0, threading.Lock()
        self.tty = sys.stdout.isatty()
        if self.tty:
            _enable_ansi()

    def _clear(self) -> None:
        if self.lines:
            sys.stdout.write(f"\x1b[{self.lines}F\x1b[J")
            self.lines = 0

    def log(self, message: str) -> None:
        with self.lock:
            self._clear()
            print(f"{datetime.now():%H:%M:%S} {message}", flush=True)
            try:
                tb_write_log(self.log_path, message)
            except OSError as exc:
                print(f"Log nicht schreibbar: {exc}", flush=True)

    def status(self, lines: list[str]) -> None:
        if not self.tty:
            return
        with self.lock:
            self._clear()
            width = max(20, shutil.get_terminal_size().columns - 1)
            shown = [line if len(line) <= width else f"{line[:width - 1]}…" for line in lines]
            sys.stdout.write("".join(f"{line}\n" for line in shown))
            sys.stdout.flush()
            self.lines = len(shown)


# --------- FUNC JOURNAL ---------
def _journal_entry(job: _ArkJob) -> dict:
    return {
        "id": job.jid, "key": job.key, "state": job.state,
        "created_at": job.created_at.isoformat() if job.created_at else None, "session_id": job.session_id,
        "mediaspace": job.mediaspace, "share_root": job.share_root, "attempt": job.attempt,
        "stop_code": job.stop_code, "stop_reason": job.stop_reason, "resent": sorted(job.resent),
        "process_ids": sorted(job.process_ids), "ark_status": job.ark_status, "ark_text": job.ark_text,
        "decided": job.decided, "results": job.results,
        "files": [{"flow_hash": f.flow_hash, "restore_path": f.restore_path, "target": str(f.target), "size": f.size,
                   "tape": f.tape, "group": list(f.group), "copies": f.copies} for f in job.files],
    }


def _job_from_entry(entry: dict) -> _ArkJob:
    files = [_File(f["flow_hash"], int(f["size"]), f["tape"], Path(f["target"]), f["restore_path"],
                   copies=f.get("copies") or [], group=tuple(f.get("group") or ())) for f in entry["files"]]
    job = _ArkJob(entry["key"], files, entry["mediaspace"], entry["share_root"], attempt=int(entry.get("attempt") or 1),
                  jid=entry["id"], session_id=entry.get("session_id"), from_journal=True,
                  created_at=_parse_date(entry.get("created_at")) or datetime.now(),
                  stop_code=entry.get("stop_code"), stop_reason=entry.get("stop_reason"),
                  resent=set(entry.get("resent") or ()), process_ids=set(entry.get("process_ids") or ()),
                  ark_status=entry.get("ark_status") or "", ark_text=entry.get("ark_text") or "",
                  results=dict(entry.get("results") or {}), decided=bool(entry.get("decided")))
    job.state = "done" if entry.get("state") == "done" else "active" if job.session_id else "lost"
    job.start = _session_start(job.session_id)
    if job.stop_code:
        job.stop_sent = time.monotonic()
    return job


class _Journal:
    """All Ark jobs this restorer created and not yet cleaned up, plus clips of open orders already handed to on_clip;
    used by exactly one process."""

    def __init__(self, path: Path) -> None:
        self.path, self.entries = path, {}
        self.reported: dict[str, dict[str, list[dict]]] = {}     # key -> clip_id -> file results

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Journal {self.path} nicht lesbar ({exc}). Datei prüfen; ohne Journal können Ark-Jobs doppelt laufen.") from exc
        entries = data.get("ark_jobs") if isinstance(data, dict) else None
        reported = (data.get("reported") or {}) if isinstance(data, dict) else None    # missing in journals before 0.3.0
        if not isinstance(entries, list) or not all(isinstance(entry, dict) and entry.get("id") for entry in entries) \
                or not isinstance(reported, dict) or not all(isinstance(clips, dict) and all(isinstance(files, list)
                                                                 for files in clips.values()) for clips in reported.values()):
            raise RuntimeError(f"Journal {self.path} hat ein unbekanntes Format. Datei prüfen.")
        self.entries, self.reported = {entry["id"]: entry for entry in entries}, reported
        return entries

    def put(self, job: _ArkJob) -> None:
        self.entries[job.jid] = _journal_entry(job)
        self._save()

    def remove(self, jids: Iterable[str]) -> None:
        if [self.entries.pop(jid) for jid in jids if jid in self.entries]:
            self._save()

    def report(self, key: str, clips: dict[str, list[dict]]) -> None:
        self.reported.setdefault(key, {}).update(clips)
        self._save()

    def forget(self, keys: Iterable[str]) -> None:
        if [self.reported.pop(key) for key in list(keys) if key in self.reported]:
            self._save()

    def session_ids(self) -> set[str]:
        return {entry["session_id"] for entry in self.entries.values() if entry.get("session_id")}

    def _save(self) -> None:
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        text = json.dumps({"app_version": app_version, "ark_jobs": list(self.entries.values()), "reported": self.reported},
                          ensure_ascii=False, indent=1)
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.path)


# --------- FUNC RUNNER ---------
class _Runner:
    def __init__(self, jobs, on_result, on_log, on_status, cred_path, journal_path, poll_seconds, max_ark_queue,
                 stall_minutes, reserve_gb, on_clip=None) -> None:
        self.jobs, self.on_result, self.log, self.on_status = iter(jobs), on_result, on_log, on_status
        self.on_clip = on_clip
        self.cred_path, self.poll_seconds, self.max_ark_queue = cred_path, poll_seconds, max_ark_queue
        self.stall_seconds, self.reserve = stall_minutes * 60, int(reserve_gb * 1e9)
        self.journal = _Journal(journal_path)
        self.ark = _connect(cred_path)
        self.exhausted = self.resume = self.ark_down = False
        self.tasks: dict[str, _Task] = {}
        self.ark_list: list[_ArkJob] = []         # all Ark jobs not done, in creation order
        self.held: list[_ArkJob] = []             # journal Ark jobs whose Restorer job has not come back yet
        self.retry_queue: deque[_ArkJob] = deque()
        self.group_queue: deque[_ArkJob] = deque()
        self.mediaspaces: tuple[float, list[str]] = (-math.inf, [])
        self.tape_servers: dict[str, str] = {}
        self.alert_totals: dict[str, int] = {}
        self.alert_last: dict[str, int] = {}
        self.notification_last = 0
        self.details_cache: dict[str, dict] = {}
        self.create_queue, self.created_queue = queue.Queue(), queue.Queue()
        threading.Thread(target=self._creator, daemon=True).start()

    # --------- loop ---------
    def loop(self) -> None:
        self.log(f"{app_name} {app_version} gestartet: Journal {self.journal.path}, max_ark_queue {self.max_ark_queue}, "
                 f"Stillstand {self.stall_seconds / 60:g} min, Reserve {_gb(self.reserve)}")
        self._recover()
        self._read_notifications(baseline=True)
        while True:
            self._collect_created()
            if self.resume:
                self._take_resumed()
            active = self._read_active()
            if active is not None:
                self._watch(active)
                self._read_notifications()
                self._check_alerts()
                self._send(active)
            self._report_clips()
            self._finish_tasks()
            self.on_status(self._status_lines())
            if self.exhausted and not self.tasks and not self.ark_list:
                self.log("Alle Restorer-Jobs und Ark-Jobs beendet.")
                return
            time.sleep(self.poll_seconds)

    def _read_active(self) -> dict[str, dict] | None:
        result = self.ark.get_jobs_active()
        if not _ok(result):
            if not self.ark_down:
                self.log(f"Ark nicht erreichbar ({result}); neuer Versuch bei jeder Abfrage.")
            self.ark_down = True
            return None
        if self.ark_down:
            self.log("Ark wieder erreichbar.")
        self.ark_down = False
        return {str(entry.get("id")): entry for entry in _listed(result.data, "active_jobs")}

    # --------- journal and resume ---------
    def _recover(self) -> None:
        try:
            jobs = [_job_from_entry(entry) for entry in self.journal.load()]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Journal {self.journal.path} hat einen ungültigen Eintrag ({exc}). Datei prüfen.") from exc
        self.held = jobs
        self.ark_list += [job for job in jobs if job.state != "done"]
        self.resume = bool(jobs or self.journal.reported)
        if jobs:
            self.log(f"Journal: {len(jobs)} Ark-Jobs übernommen, davon {sum(job.state != 'done' for job in jobs)} offen.")
        if clips := sum(map(len, self.journal.reported.values())):
            self.log(f"Journal: {clips} schon gemeldete Clips aus {len(self.journal.reported)} Restorer-Jobs übernommen.")

    def _take_resumed(self) -> None:
        """Accept every order whose key is in the journal, regardless of the control; another order ends the phase."""
        while self.resume:
            job = self._next_order()
            if job is None:
                self._end_resume()
                return
            resumed = job.key in self.journal.reported or any(held.key == job.key for held in self.held)
            self._accept(job)
            if not resumed:
                self._end_resume()

    def _end_resume(self) -> None:
        self.resume = False
        for job in list(self.held):
            if job.state == "done":
                self.held.remove(job)
                self.journal.remove([job.jid])
                self.log(f"Ark-Job {job.short} ({job.key}) ohne Restorer-Job beendet, aus dem Journal entfernt.")
            else:
                self.log(f"Ark-Job {job.short} ({job.key}) läuft ohne Restorer-Job weiter und wird bis zum Ende verfolgt.")
        if gone := [key for key in self.journal.reported if key not in self.tasks]:
            self.journal.forget(gone)
            self.log(f"Gemeldete Clips von {', '.join(gone)} ohne Restorer-Job aus dem Journal entfernt.")

    def _next_order(self) -> RestorerJob | None:
        if self.exhausted:
            return None
        try:
            job = next(self.jobs)
        except StopIteration:
            self.exhausted = True
            return None
        if job is not None and not isinstance(job, RestorerJob):
            raise TypeError(f"jobs liefert {type(job).__name__} statt RestorerJob")
        return job

    # --------- accept a Restorer job ---------
    def _accept(self, job: RestorerJob) -> None:
        if job.key in self.tasks:
            self.log(f"Restorer-Job {job.key} wird bereits bearbeitet; doppelter Auftrag verworfen.")
            return
        task = _Task(job)
        self.tasks[job.key] = task
        code, problem = self._prepare(task)
        if problem:
            task.problem = _reason(code, problem)
            for entry in task.entries:
                entry["reason"] = entry["reason"] or task.problem
            self.log(f"Restorer-Job {job.key} abgelehnt: {task.problem}")
            return
        clips = len({entry["item"]["clip_id"] for entry in task.entries if entry.get("folder")})
        self.log(f"Restorer-Job {job.key} angenommen: {len(task.entries)} Einträge, {clips} Clips, Ziel {job.output_folder} "
                 f"(Media Space {task.mediaspace})")
        self._attach(task)
        self._precheck(task)

    def _prepare(self, task: _Task) -> tuple[str, str | None]:
        """Validate the order and build its files; returns (reason code, problem of the whole order or None)."""
        job = task.job
        if not isinstance(job.key, str) or not job.key.strip():
            return "INVALID_INPUT", "key fehlt"
        if not isinstance(job.items, list) or not job.items:
            return "INVALID_INPUT", "items fehlt oder ist leer"
        task.entries = [{"item": item if isinstance(item, dict) else {}, "file": None, "reason": None} for item in job.items]
        self._take_reported(task)
        try:
            task.share_root, share, rest = _split_target(job.output_folder)
        except ValueError as exc:
            return "INVALID_INPUT", str(exc)
        try:
            task.mediaspace = self._mediaspace(share)
        except ValueError as exc:
            return "INVALID_INPUT", str(exc)
        except RuntimeError as exc:
            return "ARK_ERROR", str(exc)
        folders, used, by_target = {}, set(), {}
        for entry, item in zip(task.entries, job.items):
            if problem := _item_problem(item):
                entry["reason"] = _reason("INVALID_INPUT", problem)
                continue
            if item["clip_id"] not in folders:
                folders[item["clip_id"]] = _clip_folder(item["clip_name"], used)
            if not (folder := folders[item["clip_id"]]):
                entry["reason"] = _reason("INVALID_INPUT", f"clip_name ergibt keinen Ordnernamen: {item['clip_name']!r}")
                continue
            entry["folder"] = folder
            if entry.get("result"):
                continue
            by_target.setdefault(f"{folder}/{item['file']}".casefold(), []).append(entry)
        for entries in by_target.values():
            first = entries[0]["item"]
            if len({entry["item"]["flow_hash"].casefold() for entry in entries}) > 1:
                for entry in entries:
                    entry["reason"] = _reason("DUPLICATE_FILE", f"{entries[0]['folder']}/{first['file']} mit verschiedenen Hashes")
                continue
            restore_path = "/".join([*rest, entries[0]["folder"]]) + "/"
            f = _File(first["flow_hash"], first["size"], first["tape"], Path(job.output_folder) / entries[0]["folder"] / first["file"], restore_path)
            task.files.append(f)
            for entry in entries:
                entry["file"] = f
        return "", None

    def _take_reported(self, task: _Task) -> None:
        """Clips handed to on_clip before a restart keep that result; their files are not requested again."""
        stored = self.journal.reported.get(task.job.key) or {}
        clips: dict[str, list[dict]] = {}
        for entry in task.entries:
            clips.setdefault(entry["item"].get("clip_id"), []).append(entry)
        for clip_id, files in stored.items():
            if len(entries := clips.get(clip_id) or []) == len(files):
                for entry, result in zip(entries, files):
                    entry["result"] = result
                task.reported.add(clip_id)
        if task.reported:
            self.log(f"Restorer-Job {task.job.key}: {len(task.reported)} Clips schon gemeldet, ihre Ordner bleiben unverändert.")

    def _mediaspace(self, share: str) -> str:
        loaded, names = self.mediaspaces
        if time.monotonic() - loaded > mediaspace_cache_minutes * 60:
            result = self.ark.get_mediaspaces()
            if not _ok(result):
                raise RuntimeError(f"Media Spaces in Ark nicht lesbar: {result}")
            spaces = _listed(result.data, "esa_spaces")
            names = list(dict.fromkeys(str(space["name"]) for space in spaces if space.get("name")))
            self.mediaspaces = (time.monotonic(), names)
        return _mediaspace_name(share, names)

    def _attach(self, task: _Task) -> None:
        """Give the order its Ark jobs from the journal; only files without Ark job are requested again."""
        index = {(f.flow_hash.casefold(), str(f.target).casefold()): f for f in task.files}
        for job in [held for held in self.held if held.key == task.job.key]:
            self.held.remove(job)
            files = []
            for old in job.files:
                if f := index.get((old.flow_hash.casefold(), str(old.target).casefold())):
                    f.copies, f.group = f.copies or old.copies, f.group or old.group
                    files.append(f)
            old_results, job.files, job.task = job.results, files, task
            job.results = {str(f.target): old_results[key] for f in files for key in old_results if key.casefold() == str(f.target).casefold()}
            task.ark_jobs.append(job)
            if job.state != "done":
                for f in files:
                    f.covered = True
                continue
            for f in files:
                if str(f.target) in job.results:
                    reason = job.results[str(f.target)]
                    f.state, f.reason = ("restored", None) if reason is None and _complete(f) else ("failed", reason or _reason("INCOMPLETE", job=job))
                    f.covered = True
            if not job.decided:
                missing = [f for f in files if f.state == "open" and not _complete(f)]
                for f in files:
                    if f.state == "open" and f not in missing:
                        f.state, job.results[str(f.target)] = "restored", None
                for f in missing:
                    f.covered = True
                self._decide(job, missing)
            self.journal.put(job)
        if task.ark_jobs:
            self.log(f"Restorer-Job {task.job.key}: {len(task.ark_jobs)} Ark-Jobs aus dem Journal zugeordnet.")

    def _precheck(self, task: _Task) -> None:
        todo = [f for f in task.files if f.state == "open" and not f.covered]
        for f in todo:
            if _complete(f):
                f.state = "restored"
        todo = [f for f in todo if f.state == "open"]
        if todo:
            todo = self._check_ark(todo)
        groups: dict[tuple[str, ...], list[_File]] = {}
        for f in todo:
            groups.setdefault(f.group, []).append(f)
        for files in groups.values():
            job = _ArkJob(task.job.key, files, task.mediaspace, task.share_root, task=task)
            task.ark_jobs.append(job)
            self.ark_list.append(job)
            self.group_queue.append(job)
        states = Counter(f.state for f in task.files)
        failed = sum(1 for entry in task.entries if entry["reason"]) + states["failed"]
        self.log(f"Restorer-Job {task.job.key}: {states['restored']} Dateien schon vollständig, {failed} gescheitert, "
                 f"{len(groups)} Ark-Jobs geplant" + (f" (Bänder {', '.join('+'.join(group) for group in groups)})" if groups else ""))

    def _check_ark(self, files: list[_File]) -> list[_File]:
        def fail(f, code, text=None):
            f.state, f.reason = "failed", _reason(code, text)
        check = self.ark.check_restore([f.flow_hash for f in files])
        if not _ok(check):
            for f in files:
                fail(f, "ARK_ERROR", f"Prüfung bei Ark gescheitert: {check}")
            return []
        infos = check.data.get("files") or {}
        for f in files:
            info = infos.get(f.flow_hash) or {}
            copies = [list(copy) for copy in info.get("copies") or []]
            copy = next((copy for copy in copies if f.tape.casefold() in {tape.casefold() for tape in copy}), None)
            if info.get("reason") == "INVALID_HASH":
                fail(f, "INVALID_INPUT", "Ark lehnt den Hash ab")
            elif not info.get("in_ark"):
                fail(f, "NOT_IN_ARK")
            elif copy is None:
                fail(f, "TAPE_MISMATCH", f"Band {f.tape} nicht unter Arks Kopien ({', '.join('+'.join(c) or 'Disk' for c in copies)})")
            else:
                f.copies, f.group = copies, tuple(sorted(copy, key=str.casefold))
        files = [f for f in files if f.state == "open"]
        tapes = list(dict.fromkeys(tape for f in files for tape in f.group))
        if not tapes:
            return files
        location = self.ark.get_tape_location(tapes)
        if not _ok(location):
            for f in files:
                fail(f, "ARK_ERROR", f"Bandort in Ark nicht lesbar: {location}")
            return []
        for f in files:
            if offline := [tape for tape in f.group if not (location.data.get(tape) or {}).get("in_changer")]:
                fail(f, "TAPES_OFFLINE", f"Band {', '.join(offline)} nicht in der Library")
        return [f for f in files if f.state == "open"]

    # --------- control and creation ---------
    def _send(self, active: dict[str, dict]) -> None:
        waiting = sum(str(entry.get("current_status") or "").startswith("Enqueued") for entry in active.values())
        waiting += sum(job.state in ("creating", "lost") or (job.state == "active" and job.session_id not in active) for job in self.ark_list)
        free = math.inf if self.max_ark_queue == 0 else self.max_ark_queue - waiting
        while free > 0 and (job := self._next_job()):
            if job.attempt > 1:
                self._delete_partials(job)
            if (problem := self._space_problem(job, active)) and problem.startswith("NO_SPACE"):
                self._wait_for_space(job, problem)
                break   # No overtaking: the next Ark job would take the space this one waits for.
            if job.space_wait:
                self.log(f"Ark-Job für {job.key}: Speicherplatz reicht wieder.")
                job.space_wait = None
            if problem:
                self._fail(job, job.files, problem)
                job.state = "done"
                self.ark_list.remove(job)
                self.log(f"Ark-Job für {job.key} nicht gesendet: {problem}")
                continue
            self._launch(job)
            free -= 1

    def _space_problem(self, job: _ArkJob, active: dict[str, dict]) -> str | None:
        """Free space at the share minus outstanding bytes of all restores in Ark to the same media space."""
        try:
            free = _free_bytes(job.share_root)
        except OSError as exc:
            return _reason("INVALID_INPUT", f"Ziel {job.share_root} nicht erreichbar: {exc}")
        space = job.mediaspace.casefold()
        own_ids = self.journal.session_ids() | {other.session_id for other in self.ark_list if other.session_id}
        own = sum(other.remaining_bytes() for other in self.ark_list
                  if other is not job and other.state in ("creating", "lost", "active") and other.mediaspace.casefold() == space)
        foreign = 0
        for session_id, entry in active.items():
            current = str(entry.get("current_space") or "").casefold()
            if session_id in own_ids or not session_id.startswith("restorejob_") or (current and current != space):
                continue
            # Waiting jobs have no byte figures yet; reserve_gb covers them.
            foreign += sum(max(0, _parse_bytes(item.get("total_bytes")) - _parse_bytes(item.get("bytes")))
                           for item in _listed(self._details(session_id, "active_jobs").get("spaces"))
                           if str(item.get("name") or "").casefold() == space)
        need = sum(f.size for f in job.files) + self.reserve
        if free - own - foreign >= need:
            return None
        return _reason("NO_SPACE", f"es fehlen {_gb(need - (free - own - foreign))}; frei {_gb(free)}, ausstehend "
                                   f"{_gb(own + foreign)} (davon fremd {_gb(foreign)}), nötig {_gb(need)} inkl. Reserve")

    def _wait_for_space(self, job: _ArkJob, problem: str) -> None:
        """Keep the Ark job first in line; the space check is repeated at every poll."""
        if not job.space_wait:
            self.log(f"Ark-Job für {job.key} wartet auf Speicherplatz: {problem.split(': ', 1)[1]}")
        job.space_wait = problem.split(": ", 1)[1].split(";")[0]
        self.retry_queue.appendleft(job)

    def _busy_tapes(self) -> set[str]:
        """Tapes of own Ark jobs that are being created, searched for, waiting or running in Ark."""
        return {tape for job in self.ark_list if job.state in ("creating", "lost", "active") for tape in job.tape_set()}

    def _next_job(self) -> _ArkJob | None:
        """Next Ark job to send; jobs on a tape of another own Ark job wait, later jobs on free tapes go first."""
        while True:
            busy = self._busy_tapes()
            for pending in (self.retry_queue, self.group_queue):
                for job in list(pending):
                    if not self._still_needed(job):
                        pending.remove(job)
                    elif blocked := [tape for tape in job.tape_list() if tape.casefold() in busy]:
                        if not job.tape_wait:
                            self.log(f"Ark-Job für {job.key} wartet auf Band {', '.join(blocked)}: eigener Ark-Job auf demselben Band.")
                        job.tape_wait = True
                    else:
                        pending.remove(job)
                        job.tape_wait = False
                        return job
            if (order := self._next_order()) is None:
                return None
            self._accept(order)

    def _still_needed(self, job: _ArkJob) -> bool:
        job.files = [f for f in job.files if f.state == "open"]
        if job.task and (defect := sorted({tape for f in job.files for tape in f.group} & job.task.defect_tapes)):
            self._fail(job, job.files, _reason("TAPE_DEFECT", f"Band {', '.join(defect)} meldet Medienfehler (früherer Ark-Job)"))
            job.files = []
        if job.files:
            return True
        job.state = "done"
        self.ark_list.remove(job)
        return False

    def _delete_partials(self, job: _ArkJob) -> None:
        for f in job.files:
            if f.target.exists() and not _complete(f):
                try:
                    f.target.unlink()
                    self.log(f"Unvollständige Datei gelöscht: {f.target}")
                except OSError as exc:
                    self.log(f"Unvollständige Datei nicht löschbar: {f.target} ({exc})")

    def _launch(self, job: _ArkJob) -> None:
        job.state, job.created_at = "creating", datetime.now()
        self.journal.put(job)
        self.create_queue.put(job)
        self.log(f"Ark-Job für {job.key} wird angelegt: Band {job.tapes}, {len(job.files)} Dateien, "
                 f"{_gb(sum(f.size for f in job.files))}, Versuch {job.attempt}")

    def _creator(self) -> None:
        # Own connection: restore_files can block for minutes and the connection is not thread safe.
        ark = None
        while True:
            job = self.create_queue.get()
            try:
                ark = ark or _connect(self.cred_path)
                result = ark.restore_files([{"flow_hash": f.flow_hash, "restore_path": f.restore_path} for f in job.files],
                                           job.mediaspace, require_all=True, ark_sources=ark_sources,
                                           prefer_source=ark_sources[0], on_progress=None)
            except Exception as exc:  # handed to the main loop as outcome of this creation
                result, ark = exc, None
            self.created_queue.put((job, result))

    def _collect_created(self) -> None:
        while True:
            try:
                job, result = self.created_queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(result, ValueError):
                self._drop_created(job, _reason("ARK_ERROR", f"Restore nicht angelegt: {result}"))
            elif isinstance(result, Exception) or result.code == 0 or (_ok(result) and not (result.data or {}).get("session_id")):
                job.state = "lost"
                self.journal.put(job)
                self.log(f"Ark-Job für {job.key}: keine Antwort beim Anlegen ({result}); suche ihn mindestens {lost_search_minutes} min.")
            elif _ok(result):
                job.state, job.session_id = "active", result.data["session_id"]
                job.start = _session_start(job.session_id) or datetime.now()
                job.stall_since = time.monotonic()
                self.journal.put(job)
                self.log(f"Ark-Job {job.short} angelegt für {job.key}: Band {job.tapes}, {len(job.files)} Dateien → "
                         f"{job.mediaspace}/{job.files[0].restore_path.rsplit('/', 2)[0]}")
            elif result.error == "HASHES_NOT_RESTORABLE":
                skipped = (result.data or {}).get("skipped") or {}
                codes = {"INVALID_HASH": "INVALID_INPUT", "NOT_IN_ARK": "NOT_IN_ARK"}
                for f in job.files:
                    if info := skipped.get(f.flow_hash):
                        offline = ", ".join(info.get("tapes_offline") or [])
                        code = codes.get(info.get("reason"), "TAPES_OFFLINE")
                        self._fail(job, [f], _reason(code, f"Band {offline} nicht in der Library" if code == "TAPES_OFFLINE" and offline else None))
                rest = [f for f in job.files if f.state == "open"]
                if len(rest) == len(job.files):
                    self._drop_created(job, _reason("ARK_ERROR", f"Ark legt keinen Restore an: {_cut(result.message, ark_text_chars)}"))
                    continue
                self._drop_created(job, None)
                if rest:
                    self._requeue(job, rest, attempt=job.attempt, front=True)
            else:
                self._drop_created(job, _reason("ARK_ERROR", f"Ark lehnt den Restore ab: {result.code} {result.error} {_cut(result.message, ark_text_chars)}".strip()))

    def _drop_created(self, job: _ArkJob, reason: str | None) -> None:
        if reason:
            self._fail(job, [f for f in job.files if f.state == "open"], reason)
            self.log(f"Ark-Job für {job.key} nicht angelegt: {reason}")
        job.state = "done"
        self.ark_list.remove(job)
        self.journal.remove([job.jid])

    # --------- watching ---------
    def _watch(self, active: dict[str, dict]) -> None:
        self.details_cache = {key: value for key, value in self.details_cache.items() if key.startswith("completed_jobs/")}
        for job in [job for job in self.ark_list if job.state == "active"]:
            if (entry := active.get(job.session_id)) is None:
                self._check_gone(job)
            else:
                job.not_found = 0
                self._update(job, entry)
        if lost := [job for job in self.ark_list if job.state == "lost"]:
            completed = self._completed_sessions()
            for job in lost:
                self._search_lost(job, active, completed)

    def _update(self, job: _ArkJob, entry: dict) -> None:
        status = str(entry.get("current_status") or "")
        word = status.split(":")[0].strip()
        if word != job.status.split(":")[0].strip():
            self.log(f"Ark-Job {job.short} ({job.key}): {word or '?'}")
        job.status, now = status, time.monotonic()
        if word.casefold() == "running":
            details = self.ark.get_job_details(job.session_id, "active_jobs")
            data = details.data if _ok(details) and isinstance(details.data, dict) else {}
            spaces = _listed(data.get("spaces"))
            progress = " + ".join(str(space["bytes"]) for space in spaces if space.get("bytes")) or str(entry.get("progress_txt") or "")
            job.remaining = str(data.get("remaining") or job.remaining)
            if progress != job.progress:
                job.progress, job.stall_since = progress, now
                job.transferred = sum(_parse_bytes(space.get("bytes")) for space in spaces) or _parse_bytes(progress)
            if now - job.stall_since > self.stall_seconds:
                self._stop(job, "STALLED", f"seit {self.stall_seconds / 60:g} min kein Fortschritt (zuletzt {job.progress or '–'})", "Überwachung")
        else:
            job.stall_since = now
        if word.casefold().startswith("error"):
            self._stop(job, "ARK_ERROR", f"Ark-Status {_cut(status, ark_text_chars)}", "Ark-Status")
        if job.stop_sent is not None and now - job.stop_sent >= restop_minutes * 60:
            self.log(f"Ark-Job {job.short} ({job.key}) ist noch aktiv; Stopp wird wiederholt.")
            self._send_stop(job)

    def _stop(self, job: _ArkJob, code: str, text: str, source: str) -> None:
        if job.stop_code and stop_ranks[code] <= stop_ranks[job.stop_code]:
            return
        upgrade = job.stop_code is not None
        job.stop_code, job.stop_reason = code, _reason(code, text, job)
        self.journal.put(job)   # the own stop must be in the journal before Ark reports Cancelled
        self.log(f"Ark-Job {job.short} ({job.key}) {'neuer Stoppgrund' if upgrade else 'wird gestoppt'}: {job.stop_reason} (Quelle: {source})")
        if not upgrade:
            self._send_stop(job)

    def _send_stop(self, job: _ArkJob) -> None:
        result = self.ark.stop_job(job.session_id, timeout=0, on_progress=None)
        job.stop_sent = time.monotonic()
        if not _ok(result) and result.code != 404:
            self.log(f"Stopp von Ark-Job {job.short} gescheitert ({result}); neuer Versuch in {restop_minutes} min.")

    def _check_gone(self, job: _ArkJob) -> None:
        found = self.ark.find_job(job.session_id)
        where = (found.data or {}).get("list") if _ok(found) else None
        if where == "completed":
            self._finish(job, found.data.get("entry") or {})
        elif where == "active":
            return
        elif found.code == 404 or where:
            job.not_found += 1
            if job.from_journal or job.not_found >= not_found_limit:
                self._orphan(job)
        else:
            self.log(f"Ark-Job {job.short}: Abfrage gescheitert ({found}); neuer Versuch bei der nächsten Abfrage.")

    def _details(self, session_id: str, job_list: str) -> dict:
        key = f"{job_list}/{session_id}"
        if key not in self.details_cache:
            result = self.ark.get_job_details(session_id, job_list)
            if not _ok(result) or not isinstance(result.data, dict):
                return {}
            self.details_cache[key] = result.data
        return self.details_cache[key]

    def _completed_sessions(self) -> list[dict]:
        result = self.ark.get_jobs_completed()
        return [session for job in _listed(result.data if _ok(result) else None, "completed_jobs")
                for session in _listed(job.get("sessions"))] if _ok(result) else []

    def _search_lost(self, job: _ArkJob, active: dict[str, dict], completed: list[dict]) -> None:
        """Find an Ark job created without session id: start time, media space, file count, data volume."""
        known = self.journal.session_ids() | {other.session_id for other in self.ark_list if other.session_id}
        since = job.created_at - timedelta(seconds=clock_tolerance_seconds)
        candidates = [(str(entry.get("id")), job_list, entry)
                      for job_list, entries in (("active_jobs", list(active.values())), ("completed_jobs", completed))
                      for entry in entries
                      if str(entry.get("id", "")).startswith("restorejob_") and entry.get("id") not in known
                      and (_session_start(entry.get("id")) or datetime.min) >= since]
        matches = []
        for session_id, job_list, entry in candidates:
            spaces = _listed(self._details(session_id, job_list).get("spaces"))
            names = {str(space.get("name", "")).casefold() for space in spaces} | {str(entry.get("current_space", "")).casefold()}
            if job.mediaspace.casefold() in names and sum(_int(space.get("total_files")) for space in spaces) == len(job.files):
                matches.append((session_id, sum(_parse_bytes(space.get("total_bytes")) for space in spaces)))
        if len(matches) > 1:
            size = sum(f.size for f in job.files)
            matches = [match for match in matches if abs(match[1] - size) <= size * bytes_tolerance] or matches
        if len(matches) == 1:
            job.session_id, job.state = matches[0][0], "active"
            job.start = _session_start(job.session_id)
            job.stall_since = time.monotonic()
            self.journal.put(job)
            self.log(f"Ark-Job {job.short} für {job.key} gefunden und zugeordnet.")
            return
        if any(job_list == "active_jobs" for _, job_list, _ in candidates):
            return
        if datetime.now() < job.created_at + timedelta(minutes=lost_search_minutes):
            return
        missing = [f for f in job.files if f.state == "open" and not _complete(f)]
        for f in job.files:
            if f.state == "open" and f not in missing:
                f.state = "restored"
        self.log(f"Ark-Job für {job.key} nicht gefunden; {len(job.files) - len(missing)} Dateien vollständig, "
                 f"{len(missing)} werden neu angefordert.")
        job.state, job.decided = "done", True
        self.ark_list.remove(job)
        self.journal.remove([job.jid])
        if missing and job.task:
            self._requeue(job, missing, front=True)

    # --------- end of an Ark job ---------
    def _finish(self, job: _ArkJob, entry: dict) -> None:
        data = self._details(job.session_id, "completed_jobs")
        job.ark_status = str(data.get("status") or entry.get("status") or "")
        job.ark_text, job.end = _cut(data.get("info") or "", ark_text_chars), datetime.now()
        open_files = [f for f in job.files if f.state == "open"]
        missing = [f for f in open_files if not _complete(f)]
        for f in open_files:
            if f not in missing:
                f.state, job.results[str(f.target)] = "restored", None
        self.log(f"Ark-Job {job.short} ({job.key}) beendet: {job.ark_status or '?'}, {len(open_files) - len(missing)} von "
                 f"{len(open_files)} Dateien vollständig" + (f"; Ark: {job.ark_text}" if missing and job.ark_text else ""))
        if job.task:
            self._decide(job, missing)
        self._close(job)

    def _decide(self, job: _ArkJob, missing: list[_File]) -> None:
        job.decided = True
        if not missing:
            return
        code, status = job.stop_code, job.ark_status.casefold()
        ark_text = _cut(f"Ark-Status {job.ark_status}" + (f", {job.ark_text}" if job.ark_text else ""), ark_text_chars)
        if code in ("TAPES_OFFLINE", "TAPE_DEFECT"):
            self._fail(job, missing, job.stop_reason)
        elif code == "DRIVE_ERROR" or (not code and status.startswith("unknown")):
            marker = code or "UNKNOWN"
            if marker in job.resent and code:
                self._fail(job, missing, f"{job.stop_reason} (auch nach Wiederholung)")
            elif marker in job.resent:
                self._halve(job, missing, _reason("ARK_ERROR", ark_text, job))
            else:
                self.log(f"Ark-Job {job.short} ({job.key}) wird einmal unverändert neu gesendet ({marker}).")
                self._requeue(job, missing, resent={marker})
        elif code:
            self._halve(job, missing, job.stop_reason)
        elif "cancel" in status:
            self._fail(job, missing, _reason("CANCELLED", job=job))
        elif "insufficient space" in status and "NO_SPACE" not in job.resent:
            self.log(f"Ark-Job {job.short} ({job.key}): Ark meldet {job.ark_status}; neuer Ark-Job wartet auf Speicherplatz.")
            self._requeue(job, missing, resent={"NO_SPACE"})
        elif "insufficient space" in status:
            self._fail(job, missing, _reason("NO_SPACE", f"Ark meldet {job.ark_status} (auch nach Warten auf Speicherplatz)", job))
        elif status == "complete":
            self._halve(job, missing, _reason("INCOMPLETE", "Ark meldet Complete, Datei fehlt oder Größe falsch", job))
        else:
            self._halve(job, missing, _reason("ARK_ERROR", ark_text, job))

    def _halve(self, job: _ArkJob, missing: list[_File], reason: str) -> None:
        if len(job.files) == 1:
            self._fail(job, missing, reason)
            return
        half = (len(missing) + 1) // 2
        parts = [part for part in (missing[:half], missing[half:]) if part]
        for part in parts:
            self._requeue(job, part)
        self.log(f"Ark-Job {job.short} ({job.key}): {len(missing)} offene Dateien in {len(parts)} neue Ark-Jobs aufgeteilt.")

    def _requeue(self, job: _ArkJob, files: list[_File], *, resent: set[str] = frozenset(), attempt: int | None = None,
                 front: bool = False) -> None:
        new = _ArkJob(job.key, files, job.mediaspace, job.share_root, attempt=attempt or job.attempt + 1, task=job.task,
                      resent=job.resent | set(resent))
        job.task.ark_jobs.append(new)
        self.ark_list.append(new)
        self.retry_queue.appendleft(new) if front else self.retry_queue.append(new)

    def _fail(self, job: _ArkJob, files: list[_File], reason: str) -> None:
        for f in files:
            f.state, f.reason = "failed", reason
            job.results[str(f.target)] = reason

    def _orphan(self, job: _ArkJob) -> None:
        open_files = [f for f in job.files if f.state == "open"]
        missing = [f for f in open_files if not _complete(f)]
        for f in open_files:
            if f not in missing:
                f.state, job.results[str(f.target)] = "restored", None
        self._fail(job, missing, _reason("ORPHANED", f"{job.short} in Ark nicht bekannt, Datei fehlt oder unvollständig"))
        job.decided, job.end = True, datetime.now()
        self.log(f"Ark-Job {job.short} ({job.key}) in Ark nicht bekannt (verwaist): {len(open_files) - len(missing)} Dateien "
                 f"vollständig, {len(missing)} gescheitert.")
        self._close(job)

    def _close(self, job: _ArkJob) -> None:
        self._acknowledge(job)
        job.state = "done"
        self.ark_list.remove(job)
        if job.task is None and not self.resume:
            if job in self.held:
                self.held.remove(job)
            self.journal.remove([job.jid])
            self.log(f"Ark-Job {job.short} ({job.key}) ohne Restorer-Job beendet, aus dem Journal entfernt.")
        else:
            self.journal.put(job)

    # --------- tape alerts and notifications ---------
    def _check_alerts(self) -> None:
        counters = self.ark.get_tape_alerts()
        if not _ok(counters) or not isinstance(counters.data, dict):
            return
        fresh = []
        for address, counts in counters.data.items():
            total = _int((counts or {}).get("total")) if isinstance(counts, dict) else 0
            if address in self.alert_totals and total <= self.alert_totals[address]:
                continue
            if not (server_id := self._tape_server(address)):
                continue
            listing = self.ark.get_tape_alerts(server_id)
            if not _ok(listing):
                continue
            last = self.alert_last.get(server_id, 0)
            alerts = sorted((alert for alert in _listed(listing.data, "data") if _int(alert.get("TapeAlertLogId")) > last),
                            key=lambda alert: _int(alert.get("TapeAlertLogId")))
            self.alert_last[server_id] = max([last] + [_int(alert.get("TapeAlertLogId")) for alert in alerts])
            self.alert_totals[address] = total
            fresh += [(server_id, alert) for alert in alerts]
        if fresh and any(job.state == "active" for job in self.ark_list):
            self._handle_alerts(fresh)

    def _tape_server(self, address: str) -> str | None:
        if address not in self.tape_servers:
            result = self.ark.get_tape_servers()
            if _ok(result):
                self.tape_servers = {str(server.get("address")): str(server.get("id")) for server in _listed(result.data, "data", "servers")}
        return self.tape_servers.get(address)

    def _alert_job(self, alert: dict) -> tuple[_ArkJob | None, bool]:
        """Own Ark job of a tape alert and whether it was matched by the tape job number."""
        number, volume = str(alert.get("JobId") or "").strip(), str(alert.get("VolumeName") or "").casefold()
        jobs = [job for job in self.ark_list if job.state == "active" and job.session_id]
        if number:
            for job in jobs:
                if any(pid.endswith(number) for pid in job.process_ids) and (not volume or volume in job.copy_tapes()):
                    return job, True
        date = _parse_date(alert.get("AlertDate"))
        for job in jobs:
            if job.status.startswith("Running") and volume in job.copy_tapes() and date and job.start and date >= job.start:
                return job, False
        return None, False

    def _handle_alerts(self, fresh: list[tuple[str, dict]]) -> None:
        actions: dict[_ArkJob, dict] = {}
        for server_id, alert in fresh:
            job, by_number = self._alert_job(alert)
            if job is None:
                continue
            number = _int(alert.get("Number"), -1)
            self.log(f"Tape-Alarm {number} {alert.get('Name')} auf {alert.get('VolumeName')}, Laufwerk {alert.get('Drive')} "
                     f"→ Ark-Job {job.short} ({job.key})")
            acknowledged = str(alert.get("Acknowledged")).strip().casefold() in {"true", "1", "yes"}
            if by_number and number not in kept_alerts and not acknowledged:
                job.acks.append((server_id, alert))
            if number in alert_ranks and (job not in actions or alert_ranks[number] > alert_ranks[_int(actions[job].get("Number"))]):
                actions[job] = alert
        for job, alert in actions.items():
            number, volume, drive = _int(alert.get("Number")), alert.get("VolumeName"), alert.get("Drive")
            if number == alert_media:
                if job.task:
                    job.task.defect_tapes.add(str(volume))
                self._stop(job, "TAPE_DEFECT", f"Media-Alarm auf {volume}, Laufwerk {drive}", "Tape-Alarm")
            elif number == alert_diagnostics:
                self._stop(job, "DRIVE_ERROR", f"Diagnostics Required auf Laufwerk {drive} ({volume})", "Tape-Alarm")
            else:
                self._stop(job, "ARK_ERROR", f"Tape-Alarm {alert.get('Name')} auf {volume}, Laufwerk {drive}", "Tape-Alarm")

    def _acknowledge(self, job: _ArkJob) -> None:
        for server_id, alert in job.acks:
            alert_id = alert.get("TapeAlertLogId")
            result = self.ark.acknowledge_tape_alert(server_id, str(alert_id), confirm=True)
            outcome = "quittiert" if _ok(result) else f"nicht quittiert ({result})"
            self.log(f"Tape-Alarm {alert_id} ({alert.get('Name')}, {alert.get('VolumeName')}, Laufwerk {alert.get('Drive')}) "
                     f"zu Ark-Job {job.short} {outcome}.")
        job.acks.clear()

    def _read_notifications(self, *, baseline: bool = False) -> None:
        """New notifications at every poll while own Ark jobs are in Ark; the start only sets the baseline."""
        jobs = [job for job in self.ark_list if job.state == "active" and job.session_id]
        if not baseline and not jobs:
            return
        result = self.ark.get_notifications()
        if not _ok(result):
            return
        entries = sorted((entry for entry in _listed(result.data, "notifications", "data") if _int(entry.get("id")) > self.notification_last),
                         key=lambda entry: _int(entry.get("id")))
        self.notification_last = max([self.notification_last] + [_int(entry.get("id")) for entry in entries])
        for entry in entries:
            self._handle_notification(entry, jobs, act=not baseline)

    def _handle_notification(self, entry: dict, jobs: list[_ArkJob], act: bool) -> None:
        details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
        shorts = {_short_id(value) for key in notification_job_keys for value in (details.get(key), entry.get(key)) if value}
        own = [job for job in jobs if job.short in shorts]
        if shorts and not own:
            return
        if process_id := details.get("process_id"):
            for job in own:
                if str(process_id) not in job.process_ids:
                    job.process_ids.add(str(process_id))
                    self.journal.put(job)
        if not act or entry.get("tag") != tape_missing_tag:
            return
        barcodes = details.get("barcodes") or []
        barcodes = [str(code) for code in (re.split(r"[,;\s]+", barcodes) if isinstance(barcodes, str) else barcodes) if code]
        for job in own or jobs:
            if hits := [code for code in barcodes if code.casefold() in job.copy_tapes()]:
                self._stop(job, "TAPES_OFFLINE", f"Band {', '.join(hits)} fehlt in der Library ({_cut(entry.get('msg'), 100)})", "Benachrichtigung")

    # --------- results and status ---------
    def _report_clips(self) -> None:
        """Hand every clip whose entries are all finished to on_clip, once per clip; its folder is not touched again."""
        if self.on_clip is None:
            return
        for task in self.tasks.values():
            if task.problem:
                continue  # The whole order is reported at once.
            clips: dict[str, list[dict]] = {}
            for entry in task.entries:
                if isinstance(clip_id := entry["item"].get("clip_id"), str) and clip_id.strip():
                    clips.setdefault(clip_id, []).append(entry)
            ready = {clip_id: [_file_result(entry) for entry in entries] for clip_id, entries in clips.items()
                     if clip_id not in task.reported
                     and all(entry["reason"] or (entry["file"] and entry["file"].state != "open") for entry in entries)}
            if not ready:
                continue
            task.reported.update(ready)
            self.journal.report(task.job.key, ready)    # before on_clip: after a restart these folders stay untouched
            for clip_id, files in ready.items():
                try:
                    self.on_clip(task.job, files)
                except Exception as exc:  # A failing partial report must not stop the restorer; the final result follows.
                    self.log(f"Teilbericht für Clip {clip_id} ({task.job.key}) nicht gemeldet: {exc}")

    def _finish_tasks(self) -> None:
        for key, task in list(self.tasks.items()):
            if any(job.state != "done" for job in task.ark_jobs):
                continue
            if stray := [f for f in task.files if f.state == "open"]:
                for f in stray:
                    f.state, f.reason = "failed", _reason("ARK_ERROR", "kein Ark-Job mehr offen")
                self.log(f"Restorer-Job {key}: {len(stray)} Dateien ohne Ark-Job als gescheitert gemeldet.")
            result = self._result(task)
            self.log(f"Restorer-Job {key}: {result.state} - {result.summary}")
            self.on_result(task.job, result)
            self.journal.remove(job.jid for job in task.ark_jobs)
            self.journal.forget([key])
            del self.tasks[key]

    def _result(self, task: _Task) -> RestorerResult:
        files = [_file_result(entry) for entry in task.entries]
        ark_jobs = [{"session_id": job.session_id, "tapes": job.tapes, "hashes": [f.flow_hash for f in job.files],
                     "ark_status": job.ark_status or job.status or None, "start": job.start.isoformat() if job.start else None,
                     "duration": round((job.end - job.start).total_seconds()) if job.end and job.start else None,
                     "attempt": job.attempt,
                     "reason": job.stop_reason or (job.ark_text or job.ark_status if job.ark_status and job.ark_status != "Complete" else None)}
                    for job in task.ark_jobs if job.session_id or job.created_at]
        restored = sum(f["state"] == "restored" for f in files)
        state = "ok" if files and restored == len(files) else "failed" if not restored else "partial"
        counts = Counter(f["reason"].split(":", 1)[0] for f in files if f["state"] == "failed")
        summary = f"{restored} von {len(files)} Dateien restauriert"
        summary += f"; gescheitert: {', '.join(f'{code} {count}' for code, count in counts.items())}" if counts else ""
        summary += f"; {task.problem}" if task.problem and not files else ""
        return RestorerResult(task.job.key, state, summary, files, ark_jobs, versions())

    def _status_lines(self) -> list[str]:
        lines, busy = [], self._busy_tapes()
        for job in self.ark_list:
            status = pending_states.get(job.state) or job.status or "?"
            blocked = [tape for tape in job.tape_list() if tape.casefold() in busy] if job.state == "pending" else []
            if job.state == "pending" and job.space_wait:
                status = f"wartet auf Speicherplatz ({job.space_wait})"
            elif blocked:
                status = f"wartet auf Band {', '.join(blocked)}"
            status += f" (Stopp {job.stop_code})" if job.stop_code else ""
            lines.append(f"{job.short:<18} {job.tapes:<10} {job.key}  {status}  {job.progress or '–'}  "
                         f"{job.remaining or '–'}  Versuch {job.attempt}")
        return lines or ["Keine Ark-Jobs"]


def _file_result(entry: dict) -> dict:
    """Result of one order entry: the item fields plus state, reason and full path."""
    if entry.get("result"):
        return entry["result"]
    f = entry["file"]
    restored = f is not None and f.state == "restored"
    reason = None if restored else entry["reason"] or (f.reason if f else None) or _reason("INVALID_INPUT")
    return {**{name: entry["item"].get(name) for name in item_fields}, "state": "restored" if restored else "failed",
            "reason": reason, "path": str(f.target) if restored else None}


def _connect(cred_path: Path):
    if not Path(cred_path).is_file():
        raise FileNotFoundError(f"Zugangsdaten fehlen: {cred_path} mit ARK_HOST, ARK_USER, ARK_PASSWORD anlegen.")
    ark = tb_link_api(Path(cred_path), "ark")
    if ark is None:
        raise RuntimeError(f"Keine Verbindung zu Ark mit {cred_path}.")
    return ark


# --------- PUBLIC ---------
def versions() -> dict[str, str]:
    """Versions of restorer, toolbox and ArkAPI, e.g. for reports."""
    return {"restorer": app_version, "toolbox": str(getattr(toolbox, "__version__", "?")),
            "ArkAPI": str(getattr(ArkAPI, "__version__", getattr(ArkAPI, "ARK_VERSION", "?")))}


def run(jobs: Iterable[RestorerJob | None], on_result: Callable[[RestorerJob, RestorerResult], object], *,
        on_log: Callable[[str], object] | None = None, on_status: Callable[[list[str]], object] | None = None,
        on_clip: Callable[[RestorerJob, list[dict]], object] | None = None,
        cred_path: str | Path | None = None, journal_path: str | Path | None = None, log_path: str | Path | None = None,
        poll_seconds: float = 10, max_ark_queue: int = 0, stall_minutes: float = 60, reserve_gb: float = 50) -> None:
    """Restore the files of all jobs from LTO tape via Ark; returns when jobs is exhausted and all Ark jobs ended.

    jobs: list or generator of RestorerJob; a generator yields None while no order is available.
    on_result(job, result) is called once per finished order; run() collects and returns nothing.
    on_log(message) gets event lines, on_status(lines) the status block (one line per Ark job) at every poll.
    on_clip(job, files) is called once per clip as soon as all its entries are finished (files as in RestorerResult);
    the restorer does not change that clip folder afterwards, also not after a restart (the journal keeps such clips).
    max_ark_queue: 0 sends new Ark jobs at once, n >= 1 sends while fewer than n Ark jobs wait in Ark.
    Ctrl+C stops the restorer, not the Ark jobs; the next start resumes them from the journal.
    Raises ValueError for invalid parameters, FileNotFoundError/RuntimeError if Ark or the journal are not usable.
    """
    if not callable(on_result):
        raise ValueError("on_result ist Pflicht und muss aufrufbar sein.")
    if on_clip is not None and not callable(on_clip):
        raise ValueError("on_clip muss aufrufbar sein.")
    numbers = {"poll_seconds": poll_seconds, "stall_minutes": stall_minutes}
    if bad := [name for name, value in numbers.items() if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0]:
        raise ValueError(f"{', '.join(bad)} muss eine Zahl > 0 sein.")
    if isinstance(max_ark_queue, bool) or not isinstance(max_ark_queue, int) or max_ark_queue < 0:
        raise ValueError("max_ark_queue muss eine ganze Zahl >= 0 sein.")
    if isinstance(reserve_gb, bool) or not isinstance(reserve_gb, (int, float)) or reserve_gb < 0:
        raise ValueError("reserve_gb muss eine Zahl >= 0 sein.")
    console = _Console(Path(log_path or default_log_path)) if on_log is None or on_status is None else None
    on_log, on_status = on_log or console.log, on_status or console.status
    runner = _Runner(jobs, on_result, on_log, on_status, Path(cred_path or default_cred_path),
                     Path(journal_path or default_journal_path), poll_seconds, max_ark_queue, stall_minutes, reserve_gb, on_clip)
    try:
        runner.loop()
    except KeyboardInterrupt:
        on_log("Restorer mit Strg+C beendet. Laufende Ark-Jobs laufen weiter und werden beim nächsten Start aus dem Journal übernommen.")
        raise
