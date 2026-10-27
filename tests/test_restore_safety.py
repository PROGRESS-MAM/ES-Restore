import json
import queue
import tempfile
import unittest
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import Mock, patch
import sys

import test_restore_planning  # Installs isolated dependency stubs before importing the runner.
from restorer import restorer as core
from restorer.tape_safety import _TapeSafety
from marathon import restore_marathon as launcher


class Result:
    def __init__(self, data=None, code=200, error=""):
        self.code, self.data, self.error = code, data, error
        self.message = ""


class FakeArk:
    def __init__(self):
        self.infos = {}
        self.online = {}
        self.find = Result(code=404)
        self.check_code = 200
        self.location_code = 200
        self.query_hashes = []

    def check_restore(self, hashes):
        self.query_hashes.append(list(hashes))
        return Result({"files": {h: self.infos.get(h, {"in_ark": True, "copies": [["T1"]]}) for h in hashes}}, self.check_code)

    def get_tape_location(self, tapes):
        return Result({t: {"in_changer": self.online.get(t, True)} for t in tapes}, self.location_code)

    def get_mediaspaces(self):
        return Result({"esa_spaces": [{"name": "Space"}]})

    def find_job(self, session):
        return self.find

    def get_job_details(self, session, job_list):
        return Result({})

    def stop_job(self, *args, **kwargs):
        return Result({})

    def get_notifications(self):
        return Result({"notifications": []})

    def get_jobs_active(self):
        return Result({"active_jobs": []})

    def acknowledge_tape_alert(self, *args, **kwargs):
        return Result({})


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.block_file = self.root / "blocked_tapes.txt"
        self.block_file.write_text("# manual list\n", encoding="utf-8")
        self.runner = core._Runner.__new__(core._Runner)
        r = self.runner
        r.ark = FakeArk()
        r.safety = _TapeSafety(self.block_file)
        r.started_at = datetime.now() - timedelta(seconds=1)
        r.preflight_issue = None
        r.max_files_per_ark_job = 2
        r.finish_wait_seconds = 300
        r.log = Mock()
        r.journal = core._Journal(self.root / "journal.json")
        r.tasks, r.ark_list, r.held = {}, [], []
        r.retry_queue, r.group_queue = deque(), deque()
        r.create_queue, r.created_queue = queue.Queue(), queue.Queue()
        r.mediaspaces = (-float("inf"), [])
        r.on_clip, r.on_result, r.on_status = Mock(), Mock(), Mock()
        r.resume, r.exhausted, r.ark_down = False, True, False
        r.max_ark_queue, r.reserve = 1, 0
        r.stall_seconds, r.poll_seconds = 3600, 1
        r.notification_last = 0
        r.details_cache = {}
        r.tape_servers, r.alert_totals, r.alert_last = {}, {}, {}
        r.jobs = iter(())

    def file(self, name, tapes=None):
        f = core._File(name, 10, "NAIVE", self.root / (name + ".mxf"), "out/")
        f.copies = tapes or [["T1"]]
        f.group = tuple(f.copies[0])
        self.runner.ark.infos[name] = {"in_ark": True, "copies": f.copies}
        return f

    def task(self, files, clips=None):
        clips = clips or ["1"] * len(files)
        items = [{"clip_id": cid, "clip_name": cid, "file": f.target.name,
                  "flow_hash": f.flow_hash, "tape": f.tape, "size": f.size} for f, cid in zip(files, clips)]
        job = core.RestorerJob("job-1", items, self.root / "out")
        task = core._Task(job, files=files, prepared=True)
        task.entries = [{"item": item, "file": f, "reason": None} for item, f in zip(items, files)]
        self.runner.tasks[job.key] = task
        return task

    def ark_job(self, task, files=None, state="active", attempt=1):
        j = core._ArkJob(task.job.key, files or task.files, "Space", str(self.root), task=task, attempt=attempt)
        j.state = state
        if state != "pending":
            j.session_id = "restorejob_TEST-__restorejob__---2026-10-27-10-0-0-000001"
            j.start = datetime.now() - timedelta(minutes=1)
        task.ark_jobs.append(j)
        self.runner.ark_list.append(j)
        return j

    def test_manual_list_comments_bom_case_and_duplicates(self):
        self.block_file.write_text("\ufeff# manual\nT1 # reason\nt1\n\nT2\n", encoding="utf-8")
        self.assertEqual(self.runner.safety.read(), {"t1", "t2"})

    def test_invalid_line_rejected(self):
        self.block_file.write_text("T1 T2\n")
        with self.assertRaises(ValueError):
            self.runner.safety.read()

    def test_auto_append_preserves_manual_text(self):
        before = self.block_file.read_bytes()
        self.assertTrue(self.runner.safety.block("T2", "missing\nreason"))
        after = self.block_file.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertIn(b"T2 # auto", after)
        self.assertFalse(self.runner.safety.block("t2", "second"))
        self.assertEqual(after, self.block_file.read_bytes())

    def test_append_to_file_without_final_newline(self):
        self.block_file.write_text("T1 # comment")
        self.runner.safety.block("T2", "media")
        self.assertEqual(self.runner.safety.read(), {"t1", "t2"})

    def test_missing_file_keeps_observed_fault_for_retry(self):
        self.block_file.unlink()
        with self.assertRaises(OSError):
            self.runner.safety.block("T2", "missing")
        self.assertIn("t2", self.runner.safety.pending)
        self.block_file.write_text("T1\n")
        self.assertEqual(self.runner.safety.read(), {"t1", "t2"})
        self.assertIn("T2 # auto", self.block_file.read_text())

    def test_manual_removal_takes_effect_without_restart(self):
        self.runner.safety.block("T1", "media")
        self.block_file.write_text("# cleared\n")
        self.assertEqual(self.runner.safety.read(), set())

    def test_blocked_secondary_copy_rejects_whole_clip_not_safe_clip(self):
        self.block_file.write_text("BAD\n")
        a, b, c = self.file("a", [["T1"], ["BAD"]]), self.file("b"), self.file("c", [["T2"]])
        task = self.task([a, b, c], ["1", "1", "2"])
        self.runner._precheck(task)
        self.assertEqual([j.files for j in task.ark_jobs], [[c]])
        self.assertTrue(all(e["reason"].startswith("PREFLIGHT_BLOCKED:") for e in task.entries[:2]))
        self.runner._report_clips()
        self.runner.on_clip.assert_called_once()
        self.assertTrue(all(f["state"] == "failed" for f in self.runner.on_clip.call_args.args[1]))

    def test_offline_secondary_copy_rejects_entire_clip(self):
        a, b = self.file("a", [["T1"], ["T2"]]), self.file("b")
        self.runner.ark.online["T2"] = False
        task = self.task([a, b])
        self.runner._precheck(task)
        self.assertEqual(task.ark_jobs, [])
        self.assertTrue(all(e["reason"].startswith("PREFLIGHT_OFFLINE:") for e in task.entries))

    def test_api_failure_waits_and_recovers(self):
        task = self.task([self.file("a")])
        self.runner.ark.check_code = 503
        self.runner._precheck(task)
        self.assertTrue(task.precheck_waiting)
        self.runner._finish_tasks()
        self.runner.on_result.assert_not_called()
        self.runner.ark.check_code = 200
        self.runner._advance_tasks()
        self.assertFalse(task.precheck_waiting)
        self.assertEqual(len(task.ark_jobs), 1)

    def test_library_api_failure_waits(self):
        task = self.task([self.file("a")])
        self.runner.ark.location_code = 503
        self.runner._precheck(task)
        self.assertTrue(task.precheck_waiting)
        self.assertEqual(task.files[0].state, "open")

    def test_incomplete_file_answer_waits(self):
        f = self.file("a")
        self.runner.ark.infos["a"] = {"copies": [["T1"]]}
        self.assertIsNone(self.runner._check_ark([f]))
        self.assertEqual(f.state, "open")

    def test_no_tape_copy_rejects_whole_clip(self):
        a, b = self.file("a"), self.file("b")
        self.runner.ark.infos["a"]["copies"] = [[]]
        task = self.task([a, b])
        self.runner._precheck(task)
        self.assertEqual(task.ark_jobs, [])
        self.assertTrue(all(e["reason"] for e in task.entries))

    def test_missing_hash_does_not_send_sibling_files(self):
        a, b = self.file("a"), self.file("b")
        self.runner.ark.infos["a"] = {"in_ark": False, "copies": []}
        task = self.task([a, b])
        self.runner._precheck(task)
        self.assertEqual(task.ark_jobs, [])

    def test_spanning_file_is_separate_and_still_checks_all_tapes(self):
        a, b, c = self.file("a"), self.file("b", [["T1", "T2"]]), self.file("c")
        task = self.task([a, b, c], ["1", "2", "3"])
        self.runner._precheck(task)
        self.assertEqual([j.files for j in task.ark_jobs], [[a, c], [b]])
        self.assertEqual(task.ark_jobs[1].tape_set(), {"t1", "t2"})

    def test_pending_job_rechecks_manual_changes_in_sibling_copy(self):
        a, b = self.file("a"), self.file("b", [["T2"], ["BAD"]])
        task = self.task([a, b])
        self.runner._precheck(task)
        self.block_file.write_text("BAD\n")
        first = task.ark_jobs[0]
        self.runner.group_queue.remove(first)
        self.assertFalse(self.runner._preflight_pending(first))
        self.assertTrue(all(e["reason"] for e in task.entries))

    def test_pending_job_replans_changed_ark_copy(self):
        a, b = self.file("a"), self.file("b")
        task = self.task([a, b], ["1", "2"])
        j = self.ark_job(task, state="pending")
        self.runner.ark.infos["b"]["copies"] = [["T2"]]
        self.assertFalse(self.runner._preflight_pending(j))
        self.assertEqual([n.files for n in self.runner.retry_queue], [[a], [b]])
        self.assertTrue(all(n.attempt == 1 for n in self.runner.retry_queue))

    def test_missing_block_file_pauses_new_job(self):
        task = self.task([self.file("a")])
        self.block_file.unlink()
        self.runner._precheck(task)
        self.assertTrue(task.precheck_waiting)
        self.assertEqual(task.ark_jobs, [])
        self.assertTrue(any("Vorprüfung" in line for line in self.runner._status_lines()))

    def test_media_alarm_persisted_only_with_owned_number_and_date(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        j.process_ids.add("restore_process_123")
        alert = {"Number": 4, "Name": "Media", "VolumeName": "T1", "Drive": 1, "JobId": "123", "AlertDate": datetime.now().isoformat()}
        self.runner._handle_alerts([("server", alert)])
        self.assertIn("t1", self.runner.safety.read())
        self.assertEqual(j.stop_code, "TAPE_DEFECT")
        self.assertIn("T1", core._journal_entry(j)["unsafe_tapes"])

    def test_old_media_alarm_not_learned(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        j.process_ids.add("restore_process_123")
        alert = {"Number": 4, "VolumeName": "T1", "JobId": "123", "AlertDate": (j.start - timedelta(days=1)).isoformat()}
        self.runner._handle_alerts([("server", alert)])
        self.assertEqual(self.runner.safety.read(), set())

    def test_weak_band_only_match_does_not_modify_file(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        j.status = "Running"
        alert = {"Number": 4, "VolumeName": "T1", "JobId": "", "AlertDate": datetime.now().isoformat()}
        self.runner._handle_alerts([("server", alert)])
        self.assertEqual(self.runner.safety.read(), set())

    def test_drive_and_hard_errors_are_not_band_blocks(self):
        for number in [3, 39]:
            with self.subTest(number=number):
                task = self.task([self.file("a" + str(number))])
                j = self.ark_job(task)
                j.process_ids.add("process_123")
                self.runner._handle_alerts([("server", {"Number": number, "VolumeName": "T1", "JobId": "123", "AlertDate": datetime.now().isoformat()})])
                self.assertEqual(self.runner.safety.read(), set())

    def test_process_number_suffix_does_not_match_wrong_job(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        j.process_ids.add("restore_process_1123")
        alert = {"JobId": "123", "VolumeName": "T1"}
        self.assertEqual(self.runner._alert_job(alert), (None, False))

    def test_aware_alert_date_does_not_crash(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        j.status = "Running"
        alert = {"VolumeName": "T1", "AlertDate": datetime.now().astimezone().isoformat()}
        self.assertEqual(self.runner._alert_job(alert), (j, False))

    def test_owned_missing_notification_is_learned(self):
        task = self.task([self.file("a", [["T1"], ["BAD"]])])
        j = self.ark_job(task)
        entry = {"tag": core.tape_missing_tag, "details": {"job_id": j.short, "barcodes": ["BAD"]}, "msg": "missing"}
        self.runner._handle_notification(entry, [j], True)
        self.assertIn("bad", self.runner.safety.read())
        self.assertEqual(j.stop_code, "TAPES_OFFLINE")

    def test_foreign_or_startup_notification_is_not_learned(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        for owned, act in [(False, True), (True, False)]:
            entry = {"tag": core.tape_missing_tag, "details": {"job_id": j.short if owned else "foreign", "barcodes": ["T1"]}}
            self.runner._handle_notification(entry, [j], act)
        self.assertEqual(self.runner.safety.read(), set())

    def test_finish_waits_for_target_then_reports(self):
        f = self.file("a")
        task = self.task([f])
        j = self.ark_job(task)
        self.runner._finish(j, {"status": "Complete"})
        self.assertEqual(j.state, "finishing")
        self.runner._report_clips()
        self.runner.on_clip.assert_not_called()
        f.target.write_bytes(b"x" * 10)
        self.runner._finish_targets(j)
        self.assertEqual(j.state, "done")
        self.runner._report_clips()
        self.assertEqual(self.runner.on_clip.call_args.args[1][0]["state"], "restored")

    def test_partial_target_does_not_complete(self):
        f = self.file("a")
        f.target.write_bytes(b"x")
        task = self.task([f])
        j = self.ark_job(task)
        self.runner._finish(j, {"status": "Complete"})
        self.assertEqual(j.state, "finishing")
        self.assertEqual(f.state, "open")

    def test_first_finished_failure_retries_once_without_halving(self):
        files = [self.file(str(i)) for i in range(5)]
        task = self.task(files)
        j = self.ark_job(task)
        j.ark_status = "Partially Complete"
        self.runner._decide(j, files)
        self.assertEqual([len(n.files) for n in self.runner.retry_queue], [2, 2, 1])
        self.assertTrue(all(n.attempt == 2 for n in self.runner.retry_queue))
        self.assertFalse(hasattr(self.runner, "_halve"))

    def test_second_failure_is_final(self):
        f = self.file("a")
        task = self.task([f])
        j = self.ark_job(task, attempt=2)
        j.ark_status = "Complete"
        self.runner._decide(j, [f])
        self.assertEqual(f.state, "failed")
        self.assertEqual(len(self.runner.retry_queue), 0)

    def test_band_failure_and_manual_cancel_do_not_retry(self):
        for code, status in [("TAPE_DEFECT", "Cancelled"), ("TAPES_OFFLINE", "Cancelled"), (None, "Cancelled")]:
            f = self.file(str(code))
            task = self.task([f])
            j = self.ark_job(task)
            j.stop_code, j.stop_reason, j.ark_status = code, "failure", status
            self.runner._decide(j, [f])
            self.assertEqual(f.state, "failed")
        self.assertEqual(len(self.runner.retry_queue), 0)

    def test_missing_session_keeps_monitoring_no_report_or_retry(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        for _ in range(5):
            self.runner._check_gone(j)
        self.assertEqual(j.state, "active")
        self.assertEqual(len(self.runner.retry_queue), 0)
        self.runner._finish_tasks()
        self.runner.on_result.assert_not_called()

    def test_missing_session_with_valid_target_can_complete(self):
        f = self.file("a")
        f.target.write_bytes(b"x" * 10)
        task = self.task([f])
        j = self.ark_job(task)
        self.runner._check_gone(j)
        self.assertEqual(j.state, "done")
        self.assertEqual(f.state, "restored")

    def test_uncertain_creation_not_resubmitted(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task, state="lost")
        j.session_id = None
        j.created_at = datetime.now() - timedelta(hours=1)
        self.runner._search_lost(j, {}, [])
        self.assertEqual(j.state, "lost")
        self.assertEqual(len(self.runner.retry_queue), 0)

    def test_new_job_cannot_be_mistaken_for_old_uncertain_creation(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task, state="lost")
        j.session_id = None
        j.created_at = datetime.now() - timedelta(hours=1)
        sid = "restorejob_OTHER-__restorejob__---" + datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        self.runner._search_lost(j, {sid: {"id": sid}}, [])
        self.assertEqual(j.state, "lost")

    def test_finishing_and_learned_blocks_survive_journal_roundtrip(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        j.state = "finishing"
        j.finish_since = datetime.now()
        j.unsafe_tapes = {"BAD": "missing"}
        restored = core._job_from_entry(core._journal_entry(j))
        self.assertEqual(restored.state, "finishing")
        self.assertEqual(restored.finish_since, j.finish_since)
        self.assertEqual(restored.unsafe_tapes, j.unsafe_tapes)

    def test_legacy_journal_without_new_fields_is_supported(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        entry = core._journal_entry(j)
        del entry["finish_since"], entry["unsafe_tapes"]
        restored = core._job_from_entry(entry)
        self.assertEqual(restored.state, "active")
        self.assertEqual(restored.unsafe_tapes, {})

    def test_preflight_rejection_does_not_release_an_active_sibling(self):
        a, b = self.file("a"), self.file("b", [["T2"], ["BAD"]])
        task = self.task([a, b])
        active = self.ark_job(task, [a])
        pending = self.ark_job(task, [b], state="pending")
        self.block_file.write_text("BAD\n")
        self.runner._preflight_pending(pending)
        self.runner._report_clips()
        self.runner.on_clip.assert_not_called()
        self.assertEqual(active.state, "active")

    def test_already_reported_clip_keeps_final_result(self):
        f = self.file("a", [["T1"], ["BAD"]])
        f.state = "restored"
        task = self.task([f, self.file("b")], ["1", "2"])
        task.reported.add("1")
        self.block_file.write_text("BAD\n")
        j = self.ark_job(task, [task.files[1]], state="pending")
        self.runner._preflight_pending(j)
        self.assertEqual(core._file_result(task.entries[0])["state"], "restored")

    def test_api_validation_rejects_invalid_wait_minutes(self):
        for value in [0, -1, True, "5"]:
            with self.assertRaises(ValueError):
                core.run([], lambda *args: None, finish_wait_minutes=value)

    def test_launcher_uses_external_file_and_old_job_schema(self):
        self.assertEqual(launcher.blocked_tapes_path.parent, launcher._run_folder())
        self.assertEqual(launcher.blocked_tapes_path.name, "blocked_tapes.txt")
        self.assertEqual(launcher.schema_version, 6)
        with patch.object(launcher.restorer, "run") as call:
            launcher.work()
        self.assertEqual(call.call_args.kwargs["blocked_tapes_path"], launcher.blocked_tapes_path)
        self.assertEqual(call.call_args.kwargs["finish_wait_minutes"], 5)

    def test_marathon_report_keeps_safe_clip_on_failed_job(self):
        files = [{"clip_id": "1", "flow_hash": "hash1", "state": "failed", "reason": "PREFLIGHT_BLOCKED: BAD", "path": None},
                 {"clip_id": "2", "flow_hash": "hash2", "state": "restored", "reason": None,
                  "path": str(launcher.root_path) + "\\.marathon\\out\\clip\\file.mxf"}]
        result = SimpleNamespace(files=files, summary="mixed")
        with patch.object(launcher.adapter, "report") as report:
            launcher._on_result(SimpleNamespace(key="job"), result)
        payload = report.call_args.args[1]
        self.assertEqual(payload.status, "failed")
        self.assertEqual([item["status"] for item in payload.items], ["failed", "restored"])

    def test_build_copies_external_file_without_overwriting_operational_list(self):
        pyinstaller = ModuleType("PyInstaller")
        main = ModuleType("PyInstaller.__main__")
        pyinstaller.__main__ = main
        main.run = Mock()
        with patch.dict(sys.modules, {"PyInstaller": pyinstaller, "PyInstaller.__main__": main}):
            import importlib.util
            spec = importlib.util.spec_from_file_location("local_build", Path(launcher.__file__).with_name("build_restore.py"))
            build = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(build)
        (self.root / "dist").mkdir()
        build.project = self.root
        build.build_folder = self.root / "build"
        build.cred_file = self.root / "cred.env"
        build.cred_file.write_text("ARK_HOST=<host>\nARK_USER=<user>\nARK_PASSWORD=<password>\n")
        build.blocked_tapes_file = self.block_file
        build.main()
        args = main.run.call_args.args[0]
        self.assertNotIn(str(self.block_file), args)
        destination = self.root / "dist" / "blocked_tapes.txt"
        self.assertEqual(destination.read_bytes(), self.block_file.read_bytes())
        destination.write_text("KEEP\n")
        build.main()
        self.assertEqual(destination.read_text(), "KEEP\n")


    def test_finished_timeout_retries_only_after_grace(self):
        f = self.file("a")
        task = self.task([f])
        j = self.ark_job(task)
        self.runner._finish(j, {"status": "Complete"})
        self.assertEqual(len(self.runner.retry_queue), 0)
        j.finish_since -= timedelta(minutes=6)
        self.runner._finish_targets(j)
        self.assertEqual(j.state, "done")
        self.assertEqual(len(self.runner.retry_queue), 1)
        self.assertEqual(self.runner.retry_queue[0].attempt, 2)

    def test_io_exception_during_preflight_is_waiting_not_failed(self):
        f = self.file("a")
        self.runner.ark.check_restore = Mock(side_effect=OSError("temporarily unavailable"))
        self.assertIsNone(self.runner._check_ark([f]))
        self.assertEqual(f.state, "open")

    def test_server_timeout_during_creation_stays_uncertain(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task, state="creating")
        self.runner.created_queue.put((j, Result({}, code=504)))
        self.runner._collect_created()
        self.assertEqual(j.state, "lost")
        self.assertEqual(len(self.runner.retry_queue), 0)

    def test_dynamic_ark_rejection_skips_entire_clip(self):
        a, b, c = self.file("a"), self.file("b"), self.file("c")
        task = self.task([a, b, c], ["1", "1", "2"])
        j = self.ark_job(task, state="creating")
        result = Result({"skipped": {"a": {"reason": "NOT_IN_ARK"}}}, code=404, error="HASHES_NOT_RESTORABLE")
        self.runner.created_queue.put((j, result))
        self.runner._collect_created()
        self.assertEqual([n.files for n in self.runner.retry_queue], [[c]])
        self.assertTrue(all(e["reason"].startswith("PREFLIGHT_INVALID:") for e in task.entries[:2]))

    def test_nan_and_infinite_wait_or_reserve_are_rejected(self):
        for key in ["finish_wait_minutes", "reserve_gb"]:
            for value in [float("nan"), float("inf")]:
                with self.assertRaises(ValueError):
                    core.run([], lambda *args: None, **{key: value})

    def test_full_mixed_job_lifecycle_and_journal_cleanup(self):
        hashes = ["2:" + format(i, "032x") for i in [1, 2]]
        items = [{"clip_id": str(i), "clip_name": str(i), "file": f"file{i}.mxf", "flow_hash": h,
                  "size": 10, "tape": "T1"} for i, h in enumerate(hashes, 1)]
        self.runner.ark.infos[hashes[0]] = {"in_ark": True, "copies": [["T1"], ["BAD"]]}
        self.runner.ark.infos[hashes[1]] = {"in_ark": True, "copies": [["T2"]]}
        self.block_file.write_text("BAD\n")
        job = core.RestorerJob("job-1", items, self.root / "out")
        with patch.object(core, "_split_target", return_value=(str(self.root), "Space", [])):
            self.runner._accept(job)
        self.runner._report_clips()
        self.assertEqual(self.runner.on_clip.call_count, 1)
        self.runner._send({})
        j = self.runner.create_queue.get_nowait()
        self.runner.created_queue.put((j, Result({"session_id": "restorejob_TEST-__restorejob__---2026-10-27-10-0-0-000001"})))
        self.runner._collect_created()
        self.assertEqual(j.state, "active")
        self.runner._finish(j, {"status": "Complete"})
        self.assertEqual(j.state, "finishing")
        target = j.files[0].target
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x" * 10)
        self.runner._finish_targets(j)
        self.runner._report_clips()
        self.runner._finish_tasks()
        self.assertEqual(self.runner.on_clip.call_count, 2)
        result = self.runner.on_result.call_args.args[1]
        self.assertEqual(result.state, "partial")
        self.assertEqual([f["state"] for f in result.files], ["failed", "restored"])
        self.assertEqual(self.runner.journal.entries, {})
        self.assertEqual(self.runner.journal.reported, {})
        self.assertEqual(self.runner.tasks, {})

    def test_recovery_replays_unpersisted_owned_band_fault(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        j.unsafe_tapes = {"BAD": "missing"}
        self.runner.journal.put(j)
        self.runner.ark_list, self.runner.tasks = [], {}
        self.runner._recover()
        self.assertIn("bad", self.runner.safety.read())
        self.assertEqual(len(self.runner.ark_list), 1)
        self.assertEqual(self.runner.ark_list[0].session_id, j.session_id)

    def test_changed_copy_waits_for_occupied_new_band(self):
        a, b = self.file("a"), self.file("b", [["T2"]])
        task = self.task([a, b], ["1", "2"])
        pending = self.ark_job(task, [a], state="pending")
        active = self.ark_job(task, [b])
        self.runner.group_queue.append(pending)
        self.runner.ark.infos["a"]["copies"] = [["T2"]]
        self.runner._send({active.session_id: {"current_status": "Running"}})
        self.assertEqual(pending.state, "pending")
        self.assertTrue(self.runner.create_queue.empty())



    def test_startup_historical_alarm_for_resumed_job_not_learned(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        j.start = datetime.now() - timedelta(days=1)
        j.process_ids.add("process_123")
        alert = {"Number": 4, "VolumeName": "T1", "JobId": "123", "AlertDate": (datetime.now() - timedelta(hours=1)).isoformat()}
        self.runner._handle_alerts([("server", alert)])
        self.assertEqual(self.runner.safety.read(), set())
        self.assertIsNone(j.stop_code)

    def test_future_alarm_is_not_learned(self):
        task = self.task([self.file("a")])
        j = self.ark_job(task)
        j.process_ids.add("process_123")
        alert = {"Number": 4, "VolumeName": "T1", "JobId": "123", "AlertDate": (datetime.now() + timedelta(hours=1)).isoformat()}
        self.runner._handle_alerts([("server", alert)])
        self.assertEqual(self.runner.safety.read(), set())


if __name__ == "__main__":
    unittest.main()
