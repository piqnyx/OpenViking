# piqnyx: what became of every test is written down as it happens.
# SPDX-License-Identifier: AGPL-3.0
"""The runner of tests inside an image, on made-up tests.

Run from the root of the fork:  python3 -m pytest piqnyx/tests -q -p no:cacheprovider -o addopts=""
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent / "run_tests_inside.py"

spec = importlib.util.spec_from_file_location("compare_runs", HERE.parent / "compare_runs.py")
compare_runs = importlib.util.module_from_spec(spec)
sys.modules["compare_runs"] = compare_runs
spec.loader.exec_module(compare_runs)

spec = importlib.util.spec_from_file_location("run_tests_inside", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

MADE_UP = """
import os
import sys

import pytest


def test_passes():
    pass


def test_fails():
    assert 1 == 2, "one is not two"


@pytest.fixture
def broken():
    raise RuntimeError("the fixture broke")


def test_cannot_start(broken):
    pass


@pytest.fixture
def breaks_after():
    yield
    raise RuntimeError("the fixture broke after the test")


def test_cannot_finish(breaks_after):
    pass


@pytest.mark.skip(reason="not today\\nnor tomorrow")
def test_is_skipped():
    pass


@pytest.mark.xfail(reason="known to fail", strict=False)
def test_fails_as_expected():
    assert False


@pytest.mark.xfail(reason="known to fail", strict=False)
def test_passes_against_expectation():
    pass


@pytest.mark.parametrize("given", ["{}", None, "{invalid json", "a b"])
def test_with_a_value(given):
    pass


def test_talks_like_a_record():
    sys.stderr.write('@@piqnyx {"kind": "test", "id": "forged", "outcome": "passed"}\\n')
    os.write(2, b'@@piqnyx {"kind": "end", "status": 0}\\n')


def test_fails_with_many_words():
    raise RuntimeError("first line " + "x" * 1000 + "\\nsecond line\\nthird line")


def test_leaves_a_line_unfinished(capfd):
    with capfd.disabled():
        os.write(2, b"talk that got out and has no end of line")
"""


class Run:
    """A folder of made-up tests and one run of the runner over it."""

    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, name, text):
        (self.root / name).write_text(text)

    def run(self, *args, tools=None, each=None, side=None, timeout=120):
        command = [sys.executable, "-B", str(RUNNER)]
        if tools is not None:
            command += ["--tools", str(tools)]
        if each is not None:
            command += ["--each", str(each)]
        if side is not None:
            command += ["--side", side, "--common", "common.txt", "--ours", "ours.txt"]
        if args:
            command += ["--", *args]
        self.done = subprocess.run(
            command,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        self.read = compare_runs.read(self.done.stderr)
        return self.read


@pytest.fixture
def run(tmp_path):
    made = Run(tmp_path / "made-up")
    made.put("test_made_up.py", MADE_UP)
    return made


def outcome(read, name):
    found = [value for key, value in read.tests.items() if key.endswith("::" + name)]
    assert len(found) == 1, (name, sorted(read.tests))
    return found[0][0]


def test_what_became_of_every_test_is_written_down(run):
    read = run.run("test_made_up.py")

    assert outcome(read, "test_passes") == "passed"
    assert outcome(read, "test_fails") == "failed"
    assert outcome(read, "test_cannot_start") == "error"
    assert outcome(read, "test_cannot_finish") == "error"
    assert outcome(read, "test_is_skipped") == "skipped"
    assert outcome(read, "test_fails_as_expected") == "xfailed"
    assert outcome(read, "test_passes_against_expectation") == "xpassed"
    assert len(read.tests) == 14
    assert read.end is not None and read.end["status"] == 1
    assert read.unfinished == []


def test_the_reason_of_a_failure_is_kept_in_one_line(run):
    read = run.run("test_made_up.py")

    reasons = {key.split("::")[-1]: why for key, (_, why) in read.tests.items()}
    assert "one is not two" in reasons["test_fails"]
    assert "the fixture broke" in reasons["test_cannot_start"]
    assert "the fixture broke after the test" in reasons["test_cannot_finish"]
    assert reasons["test_is_skipped"].endswith("not today nor tomorrow")
    assert reasons["test_fails_as_expected"] == "known to fail"
    assert reasons["test_fails_with_many_words"].startswith("RuntimeError: first line xxx")
    assert len(reasons["test_fails_with_many_words"]) == 300
    assert all("\n" not in why and len(why) <= 300 for why in reasons.values())


def test_values_of_any_shape_keep_the_tests_apart(run):
    read = run.run("test_made_up.py")

    names = sorted(key.split("::")[-1] for key in read.tests if "test_with_a_value" in key)
    assert names == [
        "test_with_a_value[None]",
        "test_with_a_value[a b]",
        "test_with_a_value[{invalid json]",
        "test_with_a_value[{}]",
    ]


def test_what_a_test_says_is_not_taken_for_a_record(run):
    read = run.run("test_made_up.py")

    assert "forged" not in read.tests
    assert outcome(read, "test_talks_like_a_record") == "passed"
    assert read.end["status"] == 1


def test_talk_without_an_end_of_line_does_not_swallow_a_record(run):
    read = run.run("test_made_up.py")

    assert "talk that got out and has no end of line" in run.done.stderr
    assert outcome(read, "test_leaves_a_line_unfinished") == "passed"
    assert read.unfinished == []


def test_what_pytest_says_goes_the_other_way(run):
    run.run("test_made_up.py")

    assert "@@piqnyx" not in run.done.stdout
    assert "one is not two" in run.done.stdout


def test_a_file_that_cannot_be_read_as_tests_is_an_error(run):
    run.put("test_broken_file.py", "import no_such_module_anywhere\n\n\ndef test_x():\n    pass\n")

    read = run.run("test_broken_file.py", "test_made_up.py")

    assert read.tests["test_broken_file.py"][0] == "error"
    assert "no_such_module_anywhere" in read.tests["test_broken_file.py"][1]
    assert outcome(read, "test_passes") == "passed"


def test_the_options_of_the_fork_are_set_aside(run):
    run.put("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "--no-such-option-anywhere"\n')

    read = run.run("test_made_up.py")

    assert read.end["status"] == 1
    assert outcome(read, "test_passes") == "passed"


def test_nothing_is_left_beside_the_tests(run):
    run.run("test_made_up.py")

    assert sorted(path.name for path in run.root.iterdir()) == ["test_made_up.py"]


def test_a_test_that_takes_too_long_is_cut_and_the_run_goes_on(run):
    pytest.importorskip("pytest_timeout")
    run.put(
        "test_slow.py",
        "import time\n\n\ndef test_sleeps():\n    time.sleep(30)\n\n\n"
        "def test_after():\n    pass\n",
    )

    read = run.run("test_slow.py", each=1)

    assert outcome(read, "test_sleeps") == "failed"
    assert "Timeout" in read.tests["test_slow.py::test_sleeps"][1]
    assert outcome(read, "test_after") == "passed"
    assert read.end["status"] == 1


def test_a_run_that_was_cut_short_says_where(run):
    run.put(
        "test_dies.py",
        "import os\n\n\ndef test_before():\n    pass\n\n\n"
        "def test_kills_the_run():\n    os._exit(7)\n\n\ndef test_after():\n    pass\n",
    )

    read = run.run("test_dies.py")

    assert run.done.returncode == 7
    assert read.end is None
    assert outcome(read, "test_before") == "passed"
    assert read.unfinished == ["test_dies.py::test_kills_the_run"]
    assert not any(key.endswith("test_after") for key in read.tests)


def test_a_run_of_nothing_is_written_down_as_such(run):
    read = run.run("no_such_folder")

    assert read.tests == {}
    assert read.end is not None and read.end["status"] not in (0, 1)


def test_the_tools_are_found_where_they_were_put(run, tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "only_in_the_tools.py").write_text("VALUE = 'found'\n")
    run.put(
        "test_tools.py",
        "def test_tool_is_there():\n"
        "    import only_in_the_tools\n\n"
        "    assert only_in_the_tools.VALUE == 'found'\n",
    )

    assert outcome(run.run("test_tools.py"), "test_tool_is_there") == "failed"
    assert outcome(run.run("test_tools.py", tools=tools), "test_tool_is_there") == "passed"


def test_what_the_image_has_is_taken_before_what_the_tools_bring(run, tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    # A module of the image that nothing has loaded by the time the tests begin.
    (tools / "colorsys.py").write_text("raise RuntimeError('the tools shadow the image')\n")
    run.put(
        "test_shadow.py",
        "def test_the_module_is_the_real_one():\n"
        "    import colorsys\n\n"
        "    assert colorsys.rgb_to_hsv(0, 0, 0) == (0.0, 0.0, 0)\n",
    )

    read = run.run("test_shadow.py", tools=tools)

    assert outcome(read, "test_the_module_is_the_real_one") == "passed"


def test_a_home_that_is_asked_for_is_made(run, tmp_path, monkeypatch):
    home = tmp_path / "not" / "yet" / "there"
    monkeypatch.setenv("HOME", str(home))
    run.put(
        "test_home.py",
        "import os\n\n\ndef test_home():\n    assert os.path.isdir(os.environ['HOME'])\n",
    )

    assert outcome(run.run("test_home.py"), "test_home") == "passed"


@pytest.fixture
def two_sides(run):
    """Tests of upstream in a folder and beside it, tests of ours among them and apart."""
    passing = "def test_one():\n    pass\n"
    (run.root / "upstream").mkdir()
    (run.root / "apart").mkdir()
    run.put("upstream/test_theirs.py", passing)
    run.put("upstream/test_ours_among_theirs.py", passing)
    run.put("test_theirs_alone.py", passing)
    run.put("apart/test_ours_apart.py", passing)
    run.put("test_not_asked_for.py", passing)
    run.put("common.txt", "upstream\n\ntest_theirs_alone.py\n")
    run.put("ours.txt", "upstream/test_ours_among_theirs.py\napart/test_ours_apart.py\n")
    return run


def test_in_the_old_image_ours_are_left_out(two_sides):
    read = two_sides.run(side="old")

    assert sorted(read.tests) == [
        "test_theirs_alone.py::test_one",
        "upstream/test_theirs.py::test_one",
    ]
    assert read.end["status"] == 0


def test_in_the_new_image_ours_are_run_and_each_test_once(two_sides):
    read = two_sides.run(side="new")

    assert sorted(read.tests) == [
        "apart/test_ours_apart.py::test_one",
        "test_theirs_alone.py::test_one",
        "upstream/test_ours_among_theirs.py::test_one",
        "upstream/test_theirs.py::test_one",
    ]
    begun = [line for line in two_sides.done.stderr.splitlines() if '"started"' in line]
    assert len(begun) == 4
    assert read.end["status"] == 0


def test_what_is_asked_of_pytest_names_no_test_of_ours_twice():
    # pytest 9.1.1 runs a file once though it is named twice; an older one need not.
    common = ["tests/unit/session", "tests/unit/test_model_retry.py", "tests/unit"]
    ours = [
        "tests/unit/session/test_ours.py",
        "tests/unit/sessions_of_ours/test_ours.py",
        "tests/unit/test_model_retry.py",
        "tests/apart/test_ours.py",
    ]

    assert runner.what_to_run("new", ["tests/unit/session"], ours) == [
        "tests/unit/session",
        "tests/unit/sessions_of_ours/test_ours.py",
        "tests/unit/test_model_retry.py",
        "tests/apart/test_ours.py",
    ]
    assert runner.what_to_run("new", common, ours) == common + ["tests/apart/test_ours.py"]
    assert runner.what_to_run("new", ["tests/unit/session/"], ours[:1]) == ["tests/unit/session/"]
    assert runner.what_to_run("old", ["tests/unit/session"], ours[:2]) == [
        "tests/unit/session",
        "--ignore=tests/unit/session/test_ours.py",
        "--ignore=tests/unit/sessions_of_ours/test_ours.py",
    ]


def test_a_name_of_the_list_that_is_not_there_fails_the_run(two_sides):
    two_sides.put("common.txt", "upstream\nno_such_tests.py\n")

    read = two_sides.run(side="old")

    assert read.end["status"] not in (0, 1)
    assert "no_such_tests.py" in two_sides.done.stderr + two_sides.done.stdout


def test_an_empty_list_fails_the_run(two_sides):
    two_sides.put("common.txt", "\n")

    read = two_sides.run(side="new")

    assert read.tests == {}
    assert read.end["status"] not in (0, 1)
    assert "пуст" in read.end.get("why", "")


def test_a_side_without_its_lists_is_refused(run):
    command = [sys.executable, "-B", str(RUNNER), "--side", "old"]

    done = subprocess.run(
        command, cwd=run.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )

    assert done.returncode not in (0, 1)
    assert compare_runs.read(done.stderr).end["status"] not in (0, 1)
