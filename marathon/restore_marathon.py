# --------- IMPORTS ---------
from pathlib import Path, PureWindowsPath
import re
import sys
import traceback
from toolbox import tb_write_log
from marathon import marathon_adapter as adapter
import restorer


# --------- CONFIG ---------
app_name = "Restore Marathon"
app_version = "0.1.1"

root_path = Path(r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit")   # like root_path in Marathon's res/config.ini
work_dir = ".marathon"                      # like work_dir in Marathon's res/config.ini
stage = "Restore"
resume = True
poll_seconds = 10
heartbeat_seconds = 30
max_ark_queue = 1
stall_minutes = 60
reserve_gb = 500


# --------- STATIC ---------
schema_version = 6
reason_max_chars = 300


def _bundle_folder() -> Path:
    # PyInstaller unpacks the exe content to sys._MEIPASS.
    return Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parents[1]


def _run_folder() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


cred_path = _bundle_folder() / "marathon" / "cred.env"
main_log = _run_folder() / "restore_marathon.log"
journal_path = _run_folder() / "restore_marathon.journal.json"
# root_path up to work_dir, with "\" or "/" as separator, is cut from console and log lines
root_parts = [*PureWindowsPath(root_path).drive.lstrip("\\").split("\\"), *PureWindowsPath(root_path).parts[1:]]
short_prefix = re.compile(r"(?:\\\\|//)" + r"[\\/]".join(map(re.escape, root_parts))
                          + r"[\\/](?=" + re.escape(work_dir) + r"(?:[\\/]|$))", re.IGNORECASE)


# --------- FUNC ---------
def _short(text: str) -> str:
    return short_prefix.sub("", text)


def _relative(path: str | None) -> str | None:
    try:
        return PureWindowsPath(path).relative_to(PureWindowsPath(root_path)).as_posix() if path else None
    except ValueError:
        return None


def _orders():
    """Marathon jobs as Restorer jobs; None while no job is available."""
    while True:
        claimed = adapter.claim()
        if claimed is None:
            yield None
            continue
        job = claimed.job
        if job.get("schema_version") != schema_version:
            adapter.report(job["job_id"], adapter.JobReport(status="failed", items=[],
                           result=f"Job ungültig: schema_version {job.get('schema_version')!r}, erwartet {schema_version}"))
            continue
        yield restorer.RestorerJob(job["job_id"], job.get("items"), claimed.output_folder)


def _on_result(job: restorer.RestorerJob, result: restorer.RestorerResult) -> None:
    items = []
    for f in result.files:
        path = _relative(f["path"]) if f["state"] == "restored" else None
        restored = path is not None
        reason = None if restored else f["reason"] or "Pfad außerhalb von root_path"
        items.append({"clip_id": f["clip_id"], "flow_hash": f["flow_hash"], "status": "restored" if restored else "failed",
                      "path": path, "reason": reason if reason is None or len(reason) <= reason_max_chars else f"{reason[:reason_max_chars - 1]}…"})
    status = "ok" if items and all(item["status"] == "restored" for item in items) else "failed"
    adapter.report(job.key, adapter.JobReport(status=status, result=result.summary, preset=None, items=items))


def work() -> None:
    restorer.run(_orders(), _on_result, on_log=lambda message: adapter.log(_short(message)),
                 on_status=lambda lines: adapter.status(_short("\n".join(lines))), cred_path=cred_path,
                 journal_path=journal_path, poll_seconds=poll_seconds, max_ark_queue=max_ark_queue,
                 stall_minutes=stall_minutes, reserve_gb=reserve_gb)


# --------- MAIN ---------
def main() -> None:
    if not cred_path.is_file():
        raise FileNotFoundError(f"Zugangsdaten fehlen: {cred_path} (ARK_HOST, ARK_USER, ARK_PASSWORD).")
    versions = {"restore_marathon": app_version, "marathon_adapter": adapter.app_version, **restorer.versions()}
    settings = adapter.Settings(root_path=root_path, work_dir=work_dir, stage=stage, poll_seconds=poll_seconds,
                                heartbeat_seconds=heartbeat_seconds, log_path=main_log, versions=versions, resume=resume)
    adapter.run_stream(settings, work)


# --------- EXEC ---------
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_message = f"Unhandled error in main: {exc}"
        print(error_message)
        tb_write_log(main_log, error_message)
        tb_write_log(main_log, traceback.format_exc())
