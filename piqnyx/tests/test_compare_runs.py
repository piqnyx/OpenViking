# piqnyx: the same tests give the same in both images, and ours pass in the new one.
# SPDX-License-Identifier: AGPL-3.0
"""The comparison of two runs of tests, on made-up records.

Run from the root of the fork:  python3 -m pytest piqnyx/tests -q -p no:cacheprovider -o addopts=""
"""

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("compare_runs", HERE.parent / "compare_runs.py")
compare_runs = importlib.util.module_from_spec(spec)
sys.modules["compare_runs"] = compare_runs
spec.loader.exec_module(compare_runs)

OURS = ["tests/unit/test_piqnyx_persistence.py"]
COMMON = {
    "tests/session/test_session_commit.py::TestCommit::test_commit_success": "passed",
    "tests/session/test_session_commit.py::TestCommit::test_commit_empty_session": "passed",
    "tests/unit/session/test_session_commit_resume.py::test_resume[{}]": "failed",
    "tests/unit/test_model_retry.py::test_retry": "passed",
    "tests/unit/test_model_retry.py::test_skipped_here": "skipped",
}
NEW_ONLY = {
    "tests/unit/test_piqnyx_persistence.py::test_curable": "passed",
    "tests/unit/test_piqnyx_persistence.py::test_retry_delay[3]": "passed",
}


def records(tests, status=1, seconds=12.5, cut_at=None, noise=True):
    """What a run leaves on its second way out, other talk mixed in."""
    lines = ["warning: something of no concern"] if noise else []
    for name, outcome in tests.items():
        lines.append("@@piqnyx " + json.dumps({"kind": "started", "id": name}))
        if name == cut_at:
            return "\n".join(lines) + "\n"
        why = "" if outcome in ("passed", "skipped") else "AssertionError: made up"
        record = {"kind": "test", "id": name, "outcome": outcome, "why": why}
        lines.append("@@piqnyx " + json.dumps(record))
    end = {"kind": "end", "status": status, "seconds": seconds}
    lines.append("@@piqnyx " + json.dumps(end))
    return "\n".join(lines) + "\n"


def compare(old=None, new=None, ours=OURS, **how):
    old = records(COMMON) if old is None else old
    new = records({**COMMON, **NEW_ONLY}) if new is None else new
    return compare_runs.compare(old, new, ours, **how)


def told(lines, *words):
    return any(all(word in line for word in words) for line in lines)


def test_the_same_in_both_and_ours_passing_is_in_order():
    ok, lines = compare()

    assert ok, lines
    assert told(lines, "общих тестов 5", "исход одинаков у 5")
    assert told(lines, "наших тестов", "2", "прошли 2")
    assert told(lines, "старый образ", "прошло 3", "упало 1", "пропущено 1")
    assert told(lines, "новый образ", "прошло 5", "упало 1", "пропущено 1")


def test_a_test_that_passed_and_now_fails_stops_the_work():
    name = "tests/session/test_session_commit.py::TestCommit::test_commit_success"

    ok, lines = compare(new=records({**COMMON, **NEW_ONLY, name: "failed"}))

    assert not ok
    assert told(lines, "было passed", "стало failed", name, "made up")


def test_a_test_that_failed_and_now_passes_is_a_difference_too():
    name = "tests/unit/session/test_session_commit_resume.py::test_resume[{}]"

    ok, lines = compare(new=records({**COMMON, **NEW_ONLY, name: "passed"}))

    assert not ok
    assert told(lines, "было failed", "стало passed", name)


def test_a_test_that_one_run_lacks_stops_the_work():
    name = "tests/unit/test_model_retry.py::test_retry"
    less = {key: value for key, value in COMMON.items() if key != name}

    ok, lines = compare(new=records({**less, **NEW_ONLY}))
    assert not ok
    assert told(lines, "только в старом", name)

    ok, lines = compare(old=records(less))
    assert not ok
    assert told(lines, "только в новом", name)


def test_one_of_ours_that_does_not_pass_stops_the_work():
    for outcome in ("failed", "error", "skipped", "xfailed"):
        mine = dict(NEW_ONLY)
        mine["tests/unit/test_piqnyx_persistence.py::test_curable"] = outcome

        ok, lines = compare(new=records({**COMMON, **mine}))

        assert not ok, outcome
        assert told(lines, "наш тест", outcome, "test_curable")


def test_ours_are_looked_for_in_the_new_image_only():
    ok, lines = compare(old=records({**COMMON, **NEW_ONLY}))

    assert not ok
    assert told(lines, "старом", "наш", "test_piqnyx_persistence.py")


def test_no_tests_of_ours_is_a_failure_not_a_pass():
    ok, lines = compare(new=records(COMMON))

    assert not ok
    assert told(lines, "наших тестов", "ни одного")

    ok, lines = compare(ours=[])
    assert not ok
    assert told(lines, "наших", "не назван")


def test_a_run_that_was_cut_short_stops_the_work():
    name = "tests/unit/test_model_retry.py::test_retry"

    ok, lines = compare(new=records({**COMMON, **NEW_ONLY}, cut_at=name))

    assert not ok
    assert told(lines, "новый образ", "оборван", name)


def test_a_run_that_pytest_could_not_carry_out_stops_the_work():
    for status in (2, 3, 4, 5):
        ok, lines = compare(old=records(COMMON, status=status))

        assert not ok, status
        assert told(lines, "старый образ", f"кодом {status}")


def test_a_run_that_wrote_nothing_is_a_failure_not_a_pass():
    ok, lines = compare(old="", new="")

    assert not ok
    assert told(lines, "старый образ", "ничего не записал")
    assert told(lines, "новый образ", "ничего не записал")


def test_talk_that_is_not_a_record_is_left_out():
    text = records(COMMON) + '@@piqnyx not json at all\n@@piqnyxx {"kind": "test"}\nplain talk\n'

    read = compare_runs.read(text)

    assert len(read.tests) == 5
    assert read.noise == 1


def test_a_test_written_down_twice_keeps_the_worse_of_the_two():
    name = "tests/unit/test_model_retry.py::test_retry"
    twice = records(COMMON, noise=False).replace(
        "@@piqnyx " + json.dumps({"kind": "end", "status": 1, "seconds": 12.5}),
        "@@piqnyx "
        + json.dumps({"kind": "test", "id": name, "outcome": "error", "why": "again"})
        + "\n@@piqnyx "
        + json.dumps({"kind": "end", "status": 1, "seconds": 12.5}),
    )

    read = compare_runs.read(twice)

    assert read.tests[name] == ("error", "again")


def test_many_differences_are_counted_and_the_first_are_shown():
    many = {f"tests/unit/test_model_retry.py::test_{n:03d}": "passed" for n in range(80)}
    worse = dict.fromkeys(many, "failed")

    ok, lines = compare(old=records(many), new=records({**worse, **NEW_ONLY}))

    assert not ok
    assert told(lines, "РАЗЛИЧИЯ", "80")
    assert sum("было passed" in line for line in lines) == compare_runs.SHOWN


def test_the_program_reads_both_runs_from_files(tmp_path, capsys):
    old = tmp_path / "old.txt"
    old.write_text(records(COMMON))
    new = tmp_path / "new.txt"
    new.write_text(records({**COMMON, **NEW_ONLY}))
    ours = tmp_path / "ours.txt"
    ours.write_text("\n".join(OURS) + "\n")
    given = ["--old", str(old), "--new", str(new), "--ours", str(ours)]

    assert compare_runs.main(given) == 0
    assert "ИТОГ" in capsys.readouterr().out.splitlines()[-1]

    new.write_text(records(COMMON))
    assert compare_runs.main(given) == 1
