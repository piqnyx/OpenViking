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


@pytest.mark.skip(reason="not today")
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
"""


class Run:
    """A folder of made-up tests and one run of the runner over it."""

    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, name, text):
        (self.root / name).write_text(text)

    def run(self, *args, tools=None, timeout=120):
        command = [sys.executable, "-B", str(RUNNER)]
        if tools is not None:
            command += ["--tools", str(tools)]
        command += ["--", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *args]
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
    assert len(read.tests) == 12
    assert read.end is not None and read.end["status"] == 1
    assert read.unfinished == []


def test_the_reason_of_a_failure_is_kept_in_one_line(run):
    read = run.run("test_made_up.py")

    reasons = {key.split("::")[-1]: why for key, (_, why) in read.tests.items()}
    assert "one is not two" in reasons["test_fails"]
    assert "the fixture broke" in reasons["test_cannot_start"]
    assert "the fixture broke after the test" in reasons["test_cannot_finish"]
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
    (tools / "json.py").write_text("raise RuntimeError('the tools must not shadow the image')\n")
    run.put(
        "test_json.py", "def test_json_is_the_real_one():\n    import json\n\n    json.dumps({})\n"
    )

    assert outcome(run.run("test_json.py", tools=tools), "test_json_is_the_real_one") == "passed"


def test_a_home_that_is_asked_for_is_made(run, tmp_path, monkeypatch):
    home = tmp_path / "not" / "yet" / "there"
    monkeypatch.setenv("HOME", str(home))
    run.put(
        "test_home.py",
        "import os\n\n\ndef test_home():\n    assert os.path.isdir(os.environ['HOME'])\n",
    )

    assert outcome(run.run("test_home.py"), "test_home") == "passed"
