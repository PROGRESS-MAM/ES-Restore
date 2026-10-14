######################## SAMPLE IMPLEMENTATION ###############################

# --------- IMPORTS ---------
from pathlib import Path
import traceback
from toolbox import tb_write_log
import restorer


# --------- CONFIG ---------
app_name = "Restore Runner"
app_version = "0.1"

base_path = Path(__file__).parent
main_log = base_path / "restore_runner.log"
cred_path = base_path / "cred.env"                      # ARK_HOST, ARK_USER, ARK_PASSWORD
journal_path = base_path / "restore_runner.journal.json"

output_folder = r""     # FILL IN: \\server\freigabe\…; the share must match an active media space in Ark


# --------- ITEMS ---------
# FILL IN: one entry per file; entries with the same clip_id end up in one clip folder.
items = [
    {
        "clip_id": "",          # EditShare clip_id
        "clip_name": "",        # name of the clip folder in output_folder
        "file": "",             # physical file name from the userpath
        "flow_hash": "",        # FLOW hash version 2, "2:<hex>"
        "tape": "",             # barcode of the tape
        "size": 0,              # file size in bytes
    },
]


# --------- FUNC ---------
def on_result(job: restorer.RestorerJob, result: restorer.RestorerResult) -> None:
    lines = [f"{result.key}: {result.state} - {result.summary}"]
    lines += [f"  {f['file']}: {f['state']} {f['path'] or f['reason']}" for f in result.files]
    for line in lines:
        print(line)
        tb_write_log(main_log, line)


# --------- MAIN ---------
def main() -> None:
    tb_write_log(main_log, f"{app_name} {app_version} started, {restorer.app_name} {restorer.app_version} verlinkt.")
    job = restorer.RestorerJob("sample-1", items, output_folder)
    restorer.run([job], on_result, cred_path=cred_path, journal_path=journal_path, log_path=main_log)


# --------- EXEC ---------
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_message = f"Unhandled error in main: {exc}"
        print(error_message)
        tb_write_log(main_log, error_message)
        tb_write_log(main_log, traceback.format_exc())
