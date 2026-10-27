import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


ark_api = ModuleType("ArkAPI")
toolbox = ModuleType("toolbox")
toolbox.tb_link_api = lambda *args, **kwargs: None
toolbox.tb_write_log = lambda *args, **kwargs: None
sys.modules.setdefault("ArkAPI", ark_api)
sys.modules.setdefault("toolbox", toolbox)

from restorer.restorer import _File, _Runner, _plan_ark_files
from marathon import restore_marathon


class ArkResult:
    def __init__(self, data):
        self.code = 200
        self.data = data


class MockArk:
    def __init__(self, copies, in_changer=None):
        self.copies = copies
        self.in_changer = in_changer or {}
        self.location_requests = []

    def check_restore(self, hashes):
        return ArkResult({
            "files": {
                flow_hash: {"in_ark": True, "copies": self.copies[flow_hash]}
                for flow_hash in hashes
            }
        })

    def get_tape_location(self, tapes):
        self.location_requests.append(list(tapes))
        return ArkResult({
            tape: {"in_changer": self.in_changer.get(tape, True)}
            for tape in tapes
        })


def _file(flow_hash, tape, group=()):
    file = _File(flow_hash, 10, tape, Path(flow_hash), "restore/")
    file.group = tuple(group)
    return file


def _resolve_files(files, copies, in_changer=None, max_files_per_ark_job=50):
    ark = MockArk(copies, in_changer)
    runner = SimpleNamespace(ark=ark, max_files_per_ark_job=max_files_per_ark_job)
    unresolved = _Runner._check_ark(runner, files)
    return ark, unresolved


def _resolve_with_ark(file, copies, in_changer=None, max_files_per_ark_job=50):
    return _resolve_files([file], {file.flow_hash: copies}, in_changer, max_files_per_ark_job)


def test_ark_jobs_use_one_tape_per_job():
    files = [_file("a", "naive"), _file("b", "naive"), _file("c", "naive")]
    _, files = _resolve_files(files, {"a": [["T1"]], "b": [["T1"]], "c": [["T2"]]})

    plans = _plan_ark_files(files, 50)

    assert [len(plan) for plan in plans] == [2, 1]
    assert all(len({file.group for file in plan}) == 1 for plan in plans)
    assert [plan[0].group for plan in plans] == [("T1",), ("T2",)]


def test_ark_job_file_count_is_limited_to_n():
    files = [_file(str(index), "naive") for index in range(7)]
    _, files = _resolve_files(files, {file.flow_hash: [["T1"]] for file in files})

    plans = _plan_ark_files(files, 3)

    assert [len(plan) for plan in plans] == [3, 3, 1]
    assert all(len(plan) <= 3 for plan in plans)


def test_tape_with_fewer_than_n_files_uses_only_available_files():
    files = [_file("a", "naive"), _file("b", "naive")]
    _, files = _resolve_files(files, {file.flow_hash: [["T1"]] for file in files})

    plans = _plan_ark_files(files, 50)

    assert len(plans) == 1
    assert plans[0] == files


def test_spanning_file_gets_its_own_job():
    ordinary = _file("ordinary", "naive")
    spanning = _file("spanning", "naive")
    another = _file("another", "naive")
    _, files = _resolve_files([ordinary, spanning, another], {
        "ordinary": [["T1"]], "spanning": [["T1", "T2"]], "another": [["T1", "T2"]],
    })

    plans = _plan_ark_files(files, 50)

    assert plans == [[ordinary], [spanning], [another]]
    assert all(len(plan) == 1 for plan in plans[1:])


def test_planning_uses_ark_first_copy_not_naive_tape():
    file = _file("hash", "NAIVE")

    ark, unresolved = _resolve_with_ark(file, [["ARK_PRIMARY"], ["NAIVE"]])

    assert unresolved == [file]
    assert file.group == ("ARK_PRIMARY",)
    plans = _plan_ark_files(unresolved, 50)
    assert plans == [[file]]
    assert ark.location_requests == [["ARK_PRIMARY"]]


def test_unconfigured_restorer_keeps_naive_tape_selection():
    file = _file("hash", "NAIVE")

    _, unresolved = _resolve_with_ark(file, [["ARK_PRIMARY"], ["NAIVE"]], max_files_per_ark_job=None)

    assert unresolved == [file]
    assert file.group == ("NAIVE",)


def test_tape_only_request_skips_disk_copy():
    file = _file("hash", "NAIVE")

    ark, unresolved = _resolve_with_ark(file, [[], ["ARK_PRIMARY"], ["NAIVE"]])

    assert unresolved == [file]
    assert file.group == ("ARK_PRIMARY",)
    assert ark.location_requests == [["ARK_PRIMARY"]]


def test_offline_ark_selected_copy_is_rejected_before_restore():
    file = _file("hash", "NAIVE")

    ark, unresolved = _resolve_with_ark(file, [["ARK_PRIMARY"], ["NAIVE"]], {"ARK_PRIMARY": False})

    assert unresolved == []
    assert file.state == "failed"
    assert file.reason.startswith("TAPES_OFFLINE:")
    assert "ARK_PRIMARY" in file.reason
    assert ark.location_requests == [["ARK_PRIMARY"]]


def test_unlimited_mode_preserves_existing_copy_grouping():
    first = _file("a", "naive", ("T1", "T2"))
    second = _file("b", "naive", ("T1", "T2"))

    plans = _plan_ark_files([first, second], None)

    assert plans == [[first, second]]


def test_marathon_default_file_limit_is_50():
    assert restore_marathon.max_files_per_ark_job == 50
