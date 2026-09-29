# piqnyx: the scripts of the image stop where they must, and ask docker for what we mean.
# SPDX-License-Identifier: AGPL-3.0
"""build.sh and verify-image.sh, on a made-up fork with a real git and a stand-in for docker.

What is checked here is how the scripts reason and what they ask docker for.
How a real docker answers is seen on the server only.

Run from the root of the fork:  python3 -m pytest piqnyx/tests -q -p no:cacheprovider -o addopts=""
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_compare_images import OURS, SITE, TAG, Images, entries_of, files_of
from test_compare_runs import records

HERE = Path(__file__).resolve().parent
RECIPE = HERE.parent
FAKE = HERE / "fake_docker.py"
CARRIED = (
    "Dockerfile",
    "Dockerfile.dockerignore",
    ".gitignore",
    "common.sh",
    "build.sh",
    "verify-image.sh",
    "test-in-image.sh",
    "lay_over.sh",
    "check_list.py",
    "compare_images.py",
    "inside_check.py",
    "run_tests_inside.py",
    "compare_runs.py",
    "test-ov.conf",
)
FACTS = {
    "TAG": "v0.4.12",
    "PACKAGE_VERSION": "0.4.12",
    "BASE": "registry.example/upstream@sha256:" + "a" * 64,
    "SITE": SITE,
    "RUN_AS": "1001:1001",
    "NAME": "piqnyx/openviking",
    "VERSION": "0.0.0-test.1",
    "TEST_TOOLS": "pytest==0.0.1 pytest-helper==0.0.2",
    "TEST_EACH": "45",
    "TEST_LONGEST": "7",
    "TEST_CPUS": "2",
    "TEST_MEMORY": "3g",
}
OUR_IMAGE = f"{FACTS['NAME']}:{FACTS['VERSION']}"


class Fork:
    """A fork in little: upstream at the tag, our change on top, the recipe beside."""

    def __init__(self, tmp_path):
        self.images = Images(tmp_path)
        self.images.new["/app/PIQNYX-VERSION"] = (FACTS["VERSION"] + "\n", "644", "0:0")
        self.root = tmp_path / "fork"
        self.root.mkdir()
        self.git("init", "-q", "-b", "upstream")
        for name, text in self.images.tag_files.items():
            self.put(name, text)
        self.put("openviking_cli/__init__.py", "")
        self.put("docker/openviking-entrypoint.sh", "#!/bin/sh\n")
        self.put("pyproject.toml", "[tool.pytest.ini_options]\n")
        self.put("tests/unit/test_theirs.py", "def test_one():\n    pass\n")
        self.put("tests/session/test_theirs.py", "def test_one():\n    pass\n")
        self.commit("upstream at the tag")
        self.git("tag", FACTS["TAG"])
        self.git("checkout", "-q", "-b", "piqnyx/0.4.12")
        for name, text in self.images.sources.items():
            self.put(name, text)
        for name in CARRIED:
            shutil.copy(RECIPE / name, self.path("piqnyx", name))
        self.put("piqnyx/overlay.txt", "\n".join(OURS) + "\n")
        self.put("piqnyx/tests-inside.txt", "tests/unit\ntests/session/test_theirs.py\n")
        self.put("tests/unit/test_ours.py", "def test_one():\n    pass\n")
        self.facts()
        self.commit("ours")
        self.theirs = {
            "tests/unit/test_theirs.py::test_one": "passed",
            "tests/session/test_theirs.py::test_one": "failed",
        }
        self.mine = {"tests/unit/test_ours.py::test_one": "passed"}

        self.docker = tmp_path / "docker"
        self.docker.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        (self.bin / "docker").chmod(0o755)
        self.scenario = {
            "images": {
                FACTS["BASE"]: {"side": "old", "config": self.images.old_config, "id": "sha256:o"},
                OUR_IMAGE: {"side": "new", "config": self.images.new_config, "id": "sha256:n"},
            },
            "cache_tag": TAG,
            "site": SITE,
        }

    def path(self, *parts):
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put(self, name, text):
        self.path(name).write_text(text)

    def facts(self, **changed):
        facts = dict(FACTS, **changed)
        text = "# the facts of a made-up image\n"
        text += "".join(f"{key}={value}\n" for key, value in facts.items() if value is not None)
        self.put("piqnyx/image.conf", text)

    def git(self, *args):
        done = subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
            + ["-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "core.hooksPath="]
            + list(args),
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def run(self, script, cwd=None):
        (self.docker / "scenario.json").write_text(json.dumps(self.scenario))
        for side, image in (("old", self.images.old), ("new", self.images.new)):
            (self.docker / f"{side}.sha").write_text(files_of(image))
            (self.docker / f"{side}.ent").write_text(entries_of(image))
        if self.theirs is not None:
            (self.docker / "old.rec").write_text(records(self.theirs))
            (self.docker / "new.rec").write_text(records({**self.theirs, **self.mine}))
        env = dict(
            os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_DOCKER=str(self.docker)
        )
        return subprocess.run(
            ["bash", str(self.root / "piqnyx" / script)],
            cwd=cwd or self.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            check=False,
        )

    def calls(self, *first):
        log = self.docker / "calls.jsonl"
        if not log.exists():
            return []
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return [call for call in calls if call["argv"][: len(first)] == list(first)]


@pytest.fixture
def fork(tmp_path):
    return Fork(tmp_path)


def after(argv, flag):
    """Everything given to a flag that may come more than once."""
    return [argv[n + 1] for n, item in enumerate(argv[:-1]) if item == flag]


# ---------------------------------------------------------------- build.sh


def test_the_build_is_asked_for_with_the_facts_and_the_commit(fork):
    done = fork.run("build.sh")

    assert done.returncode == 0, done.stdout + done.stderr
    (build,) = fork.calls("build")
    argv = build["argv"]
    assert sorted(after(argv, "--build-arg")) == [
        f"BASE={FACTS['BASE']}",
        f"SITE={SITE}",
        f"VERSION={FACTS['VERSION']}",
    ]
    assert sorted(after(argv, "--label")) == [
        f"org.piqnyx.base={FACTS['BASE']}",
        f"org.piqnyx.commit={fork.git('rev-parse', 'HEAD')}",
        f"org.piqnyx.version={FACTS['VERSION']}",
    ]
    assert after(argv, "--tag") == [OUR_IMAGE]
    assert after(argv, "--file") == [str(fork.root / "piqnyx" / "Dockerfile")]
    assert after(argv, "--network") == ["none"]
    assert argv[-1] == str(fork.root)
    assert "СОБРАНО" in done.stdout and OUR_IMAGE in done.stdout


def test_the_build_does_not_care_where_it_is_called_from(fork, tmp_path):
    done = fork.run("build.sh", cwd=tmp_path)

    assert done.returncode == 0, done.stdout + done.stderr
    assert fork.calls("build")[0]["argv"][-1] == str(fork.root)


@pytest.mark.parametrize(
    "name",
    ["openviking/session/session.py", "openviking/utils/left_lying.py", "piqnyx/lay_over.sh"],
)
def test_what_is_not_committed_stops_the_build(fork, name):
    fork.put(name, "not committed\n")

    done = fork.run("build.sh")

    assert done.returncode != 0
    assert "незакоммичен" in done.stderr and name in done.stderr
    assert fork.calls("build") == []


def test_what_the_checks_leave_behind_does_not_stop_the_build(fork):
    fork.put("piqnyx/.work/check/old.sha", "left by a check\n")
    fork.put("piqnyx/__pycache__/x.pyc", "left by Python\n")

    done = fork.run("build.sh")

    assert done.returncode == 0, done.stdout + done.stderr


def test_a_list_that_is_not_the_difference_stops_the_build(fork):
    fork.put("openviking/utils/model_retry.py", "retry, touched by us\n")
    fork.commit("one more change, the list forgotten")

    done = fork.run("build.sh")

    assert done.returncode != 0
    assert "model_retry.py" in done.stdout + done.stderr
    assert fork.calls("build") == []


def test_a_change_the_recipe_cannot_carry_stops_the_build(fork):
    fork.put("docker/openviking-entrypoint.sh", "#!/bin/sh\n# touched by us\n")
    fork.commit("the entrypoint touched")

    done = fork.run("build.sh")

    assert done.returncode != 0
    assert "openviking-entrypoint.sh" in done.stdout + done.stderr
    assert fork.calls("build") == []


def test_a_fork_without_the_tag_stops_the_build(fork):
    fork.git("tag", "-d", FACTS["TAG"])

    done = fork.run("build.sh")

    assert done.returncode != 0
    assert FACTS["TAG"] in done.stderr and "git fetch --tags" in done.stderr
    assert fork.calls("build") == []


def test_a_base_that_is_not_on_the_disk_stops_the_build(fork):
    del fork.scenario["images"][FACTS["BASE"]]

    done = fork.run("build.sh")

    assert done.returncode != 0
    assert "docker pull" in done.stderr and FACTS["BASE"] in done.stderr
    assert fork.calls("build") == []


def test_a_docker_without_buildkit_stops_the_build(fork):
    fork.scenario["buildkit"] = False

    done = fork.run("build.sh")

    assert done.returncode != 0
    assert "BuildKit" in done.stderr
    assert fork.calls("build") == []


def test_a_version_that_is_in_work_is_not_built_again(fork):
    fork.scenario["containers"] = {OUR_IMAGE: ["openviking"]}

    done = fork.run("build.sh")

    assert done.returncode != 0
    assert "в работе" in done.stderr and "openviking" in done.stderr
    assert fork.calls("build") == []


def test_the_first_build_asks_nothing_of_an_image_that_is_not_there_yet(fork):
    del fork.scenario["images"][OUR_IMAGE]

    done = fork.run("build.sh")

    assert done.returncode == 0, done.stdout + done.stderr
    assert fork.calls("ps") == []
    assert len(fork.calls("build")) == 1


def test_a_build_that_failed_is_not_called_built(fork):
    fork.scenario["build_code"] = 1

    done = fork.run("build.sh")

    assert done.returncode != 0
    assert "СОБРАНО" not in done.stdout


@pytest.mark.parametrize("fact", sorted(FACTS))
def test_a_fact_that_is_missing_stops_the_build(fork, fact):
    fork.facts(**{fact: None})
    fork.commit("a fact lost")

    done = fork.run("build.sh")

    assert done.returncode != 0
    assert fact in done.stderr
    assert fork.calls("build") == []


def test_a_fact_given_twice_stops_the_build(fork):
    with fork.path("piqnyx/image.conf").open("a") as facts:
        facts.write("VERSION=0.0.0-test.2\n")
    fork.commit("a fact doubled")

    done = fork.run("build.sh")

    assert done.returncode != 0
    assert "VERSION" in done.stderr and "2 раз" in done.stderr
    assert fork.calls("build") == []


def test_the_facts_of_the_real_image_are_all_there():
    text = (RECIPE / "image.conf").read_text()
    facts = dict(
        line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#")
    )

    assert sorted(facts) == sorted(FACTS)
    assert all(value and value == value.strip() for value in facts.values())
    assert facts["BASE"].count("@sha256:") == 1
    assert facts["VERSION"].startswith(facts["PACKAGE_VERSION"] + "-piqnyx.")
    assert facts["TAG"] == "v" + facts["PACKAGE_VERSION"]


def test_the_real_list_names_files_that_are_there():
    names = (RECIPE / "overlay.txt").read_text().splitlines()

    assert names
    for name in names:
        assert (RECIPE.parent / name).is_file(), name


# --------------------------------------------------------- verify-image.sh


def test_an_image_that_is_what_it_claims_passes_the_check(fork):
    done = fork.run("verify-image.sh")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "ИТОГ: образ -- прежний плюс наши файлы, больше ничего" in done.stdout
    assert "ИТОГ: сервер в образе берёт наши файлы" in done.stdout
    assert "исходный образ сверен с тегом: совпало 2 из 2" in done.stdout
    last = done.stdout.strip().splitlines()[-1]
    assert "ИТОГ ПРОВЕРКИ" in last and OUR_IMAGE in last and "ОСТАНОВКА" not in last


def test_the_check_asks_docker_for_what_we_mean(fork):
    assert fork.run("verify-image.sh").returncode == 0

    runs = fork.calls("run")
    assert runs, "no container was asked for"
    for run in runs:
        argv = run["argv"]
        assert "--rm" in argv
        assert after(argv, "--network") == ["none"], argv
        assert not any(item in ("-v", "--volume", "--mount", "-p") for item in argv), argv
    listings = [run for run in runs if "sha256sum" in " ".join(run["argv"])]
    assert sorted(run["image"] for run in listings) == sorted([FACTS["BASE"], OUR_IMAGE])
    for run in listings:
        assert after(run["argv"], "--user") == ["0:0"]
    (inside,) = [run for run in runs if "stdin_sha256" in run]
    assert inside["image"] == OUR_IMAGE
    assert after(inside["argv"], "--user") == [FACTS["RUN_AS"]]
    assert "-i" in inside["argv"]
    script = (fork.root / "piqnyx" / "inside_check.py").read_bytes()
    assert inside["stdin_sha256"] == hashlib.sha256(script).hexdigest()
    assert after(inside["argv"], "--first") == [
        "openviking_cli.server_bootstrap",
        "openviking.server.bootstrap",
    ]
    assert after(inside["argv"], "--package-version") == [FACTS["PACKAGE_VERSION"]]


def test_the_check_does_not_care_where_it_is_called_from(fork, tmp_path):
    done = fork.run("verify-image.sh", cwd=tmp_path)

    assert done.returncode == 0, done.stdout + done.stderr


def test_a_file_nobody_asked_for_fails_the_check(fork):
    fork.images.new["/usr/local/lib/python3.13/__pycache__/os.cpython-313.pyc"] = (
        "written by the build",
        "644",
        "0:0",
    )

    done = fork.run("verify-image.sh")

    assert done.returncode != 0
    assert "появился" in done.stdout and "os.cpython-313.pyc" in done.stdout
    assert "ОСТАНОВКА" in done.stdout.strip().splitlines()[-1]


def test_a_server_that_would_not_take_our_files_fails_the_check(fork):
    fork.scenario["inside_code"] = 1
    fork.scenario["inside_lines"] = ["ИТОГ: ОСТАНОВКА, сервер в образе возьмёт не то"]

    done = fork.run("verify-image.sh")

    assert done.returncode != 0
    assert "ИТОГ: образ -- прежний плюс наши файлы, больше ничего" in done.stdout
    assert "ОСТАНОВКА" in done.stdout.strip().splitlines()[-1]


def test_an_image_that_starts_another_way_fails_the_check(fork):
    fork.images.new_config["Entrypoint"] = ["sh"]

    done = fork.run("verify-image.sh")

    assert done.returncode != 0
    assert "Entrypoint" in done.stdout


def test_a_running_image_that_is_not_the_tag_fails_the_check(fork):
    path = f"{SITE}/openviking/utils/model_retry.py"
    fork.images.old[path] = ("retry, not of the tag", "644", "0:0")
    fork.images.new[path] = fork.images.old[path]

    done = fork.run("verify-image.sh")

    assert done.returncode != 0
    assert "расходится с тегом" in done.stdout and "model_retry.py" in done.stdout


@pytest.mark.parametrize("gone", ["ours", "base"])
def test_an_image_that_is_not_on_the_disk_stops_the_check(fork, gone):
    name = OUR_IMAGE if gone == "ours" else FACTS["BASE"]
    del fork.scenario["images"][name]

    done = fork.run("verify-image.sh")

    assert done.returncode != 0
    assert name in done.stderr
    assert fork.calls("run") == []


def test_packages_kept_in_another_place_stop_the_check(fork):
    fork.scenario["site"] = "/somewhere/else/site-packages"

    done = fork.run("verify-image.sh")

    assert done.returncode != 0
    assert "/somewhere/else/site-packages" in done.stderr and SITE in done.stderr


@pytest.mark.parametrize("kind", ["sha", "ent"])
def test_a_listing_that_failed_is_not_compared(fork, kind):
    fork.scenario["listing_codes"] = {kind: 1}

    done = fork.run("verify-image.sh")

    assert done.returncode != 0
    assert "ИТОГ: образ --" not in done.stdout
    assert "не получен" in done.stderr
    assert ("с правами" in done.stderr) == (kind == "ent")


def test_every_check_is_taken_anew(fork):
    assert fork.run("verify-image.sh").returncode == 0
    fork.images.new["/app/.venv/bin/surprise"] = ("?", "755", "0:0")

    done = fork.run("verify-image.sh")

    assert done.returncode != 0
    assert "surprise" in done.stdout


def test_what_an_earlier_check_left_is_not_counted(fork):
    first = fork.run("verify-image.sh")
    assert first.returncode == 0
    fork.put("piqnyx/.work/check/tag/openviking/left_by_an_earlier_check.py", "stale\n")

    second = fork.run("verify-image.sh")

    assert second.returncode == 0
    assert second.stdout == first.stdout


def test_a_fork_without_the_tag_stops_the_check(fork):
    fork.git("tag", "-d", FACTS["TAG"])

    done = fork.run("verify-image.sh")

    assert done.returncode != 0
    assert FACTS["TAG"] in done.stderr and "git fetch --tags" in done.stderr
    assert fork.calls("run") == []


def test_the_check_leaves_the_fork_as_it_was(fork):
    before = fork.git("status", "--porcelain", "--untracked-files=all")

    assert fork.run("verify-image.sh").returncode == 0

    assert fork.git("status", "--porcelain", "--untracked-files=all") == before == ""
    assert (fork.root / "piqnyx" / ".work" / "check" / "new.sha").is_file()


# -------------------------------------------------------- test-in-image.sh


def test_tests_that_give_the_same_in_both_images_pass(fork):
    done = fork.run("test-in-image.sh")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "общих тестов 2: исход одинаков у 2" in done.stdout
    assert "наших тестов в новом образе 1: прошли 1" in done.stdout
    last = done.stdout.strip().splitlines()[-1]
    assert "ИТОГ ТЕСТОВ" in last and "ОСТАНОВКА" not in last


def test_the_tools_are_the_only_thing_taken_from_the_network(fork):
    assert fork.run("test-in-image.sh").returncode == 0

    runs = fork.calls("run")
    (tools,) = [run for run in runs if "pip" in run["argv"]]
    assert after(tools["argv"], "--network") == ["host"]
    assert after(tools["argv"], "--user") == [f"{os.getuid()}:{os.getgid()}"]
    assert tools["image"] == FACTS["BASE"]
    given = tools["argv"][tools["argv"].index("install") :]
    assert given[-2:] == ["pytest==0.0.1", "pytest-helper==0.0.2"]
    assert after(tools["argv"], "-v") == [f"{fork.root}/piqnyx/.work/tests/tools:/tools"]
    for run in runs:
        if run is not tools:
            assert after(run["argv"], "--network") == ["none"], run["argv"]


def test_the_tests_run_as_the_server_does_and_can_write_nowhere_on_the_disk(fork):
    assert fork.run("test-in-image.sh").returncode == 0

    inside = [run for run in fork.calls("run") if "side" in run]
    assert [(run["side"], run["image"]) for run in inside] == [
        ("old", FACTS["BASE"]),
        ("new", OUR_IMAGE),
    ]
    for run in inside:
        argv = run["argv"]
        assert "--rm" in argv
        assert after(argv, "--user") == [FACTS["RUN_AS"]]
        assert after(argv, "--cpus") == [FACTS["TEST_CPUS"]]
        assert after(argv, "--memory") == [FACTS["TEST_MEMORY"]]
        assert after(argv, "--each") == [FACTS["TEST_EACH"]]
        mounts = after(argv, "-v")
        assert mounts and all(mount.endswith(":ro") for mount in mounts), mounts
        assert f"{fork.root}/tests:/src/tests:ro" in mounts
        assert f"{fork.root}/piqnyx/.work/tests/tools:/tools:ro" in mounts
        (scratch,) = after(argv, "--tmpfs")
        assert (
            scratch.startswith("/src/test_data:")
            and "uid=1001" in scratch
            and "gid=1001" in scratch
        )
        assert "OPENVIKING_CONFIG_FILE=/src/ov.conf" in after(argv, "-e")
        assert "HOME=/src/test_data/home" in after(argv, "-e")
        assert not any(item in ("-p", "--publish", "--privileged") for item in argv)


def test_a_test_that_gives_another_thing_in_our_image_stops_the_work(fork):
    fork.theirs = None
    fork.docker.joinpath("old.rec").write_text(
        records({"tests/unit/test_theirs.py::test_one": "passed"})
    )
    fork.docker.joinpath("new.rec").write_text(
        records({"tests/unit/test_theirs.py::test_one": "failed", **fork.mine})
    )

    done = fork.run("test-in-image.sh")

    assert done.returncode != 0
    assert "было passed, стало failed: tests/unit/test_theirs.py::test_one" in done.stdout
    assert "ОСТАНОВКА" in done.stdout.strip().splitlines()[-1]


def test_a_run_that_was_cut_short_stops_the_work(fork):
    fork.theirs = None
    name = "tests/unit/test_theirs.py::test_one"
    fork.docker.joinpath("old.rec").write_text(records({name: "passed"}))
    fork.docker.joinpath("new.rec").write_text(records({name: "passed", **fork.mine}, cut_at=name))

    done = fork.run("test-in-image.sh")

    assert done.returncode != 0
    assert "оборван" in done.stdout and name in done.stdout


def test_tools_that_did_not_come_stop_the_tests(fork):
    fork.scenario["tools_code"] = 1

    done = fork.run("test-in-image.sh")

    assert done.returncode != 0
    assert "инструменты" in done.stderr
    assert [run for run in fork.calls("run") if "side" in run] == []


@pytest.mark.parametrize("gone", ["ours", "base"])
def test_an_image_that_is_not_on_the_disk_stops_the_tests(fork, gone):
    name = OUR_IMAGE if gone == "ours" else FACTS["BASE"]
    del fork.scenario["images"][name]

    done = fork.run("test-in-image.sh")

    assert done.returncode != 0
    assert name in done.stderr
    assert fork.calls("run") == []


def test_a_branch_that_added_no_tests_stops_the_tests(fork):
    fork.git("rm", "-q", "tests/unit/test_ours.py")
    fork.commit("our test taken away")

    done = fork.run("test-in-image.sh")

    assert done.returncode != 0
    assert "не добавила" in done.stderr
    assert fork.calls("run") == []


@pytest.mark.parametrize(
    "name, word",
    [
        ("tests/unit/test_ours.py", "не из тега"),
        ("tests/no_such_tests", "не из тега"),
        ("", "пуст"),
    ],
)
def test_a_list_of_common_tests_that_is_not_of_upstream_stops_the_tests(fork, name, word):
    fork.put("piqnyx/tests-inside.txt", f"{name}\n")
    fork.commit("the list changed")

    done = fork.run("test-in-image.sh")

    assert done.returncode != 0
    assert word in done.stderr
    assert fork.calls("run") == []


def test_every_run_of_the_tests_is_taken_anew(fork):
    assert fork.run("test-in-image.sh").returncode == 0
    fork.put("piqnyx/.work/tests/tools/left_by_a_run_before.py", "stale\n")
    fork.theirs = None
    fork.docker.joinpath("new.rec").unlink()

    done = fork.run("test-in-image.sh")

    assert done.returncode != 0
    assert "новый образ: прогон ничего не записал" in done.stdout
    tools = fork.root / "piqnyx" / ".work" / "tests" / "tools"
    assert sorted(path.name for path in tools.iterdir()) == ["pytest"]


def test_the_tests_of_ours_are_the_test_files_our_branch_added(fork):
    fork.put("tests/unit/helpers_of_ours.py", "VALUE = 1\n")
    fork.put("tests/unit/fixtures/made_by_us.json", "{}\n")
    fork.put("tests/unit/test_theirs.py", "def test_one():\n    assert True\n")
    fork.put("tests/deep/er/test_ours_too.py", "def test_one():\n    pass\n")
    fork.commit("more of ours, and one of theirs changed")
    fork.mine["tests/deep/er/test_ours_too.py::test_one"] = "passed"

    done = fork.run("test-in-image.sh")

    assert done.returncode == 0, done.stdout + done.stderr
    ours = (fork.root / "piqnyx" / ".work" / "tests" / "ours.txt").read_text().splitlines()
    assert sorted(ours) == ["tests/deep/er/test_ours_too.py", "tests/unit/test_ours.py"]


def test_containers_left_by_a_run_before_are_put_away(fork):
    assert fork.run("test-in-image.sh").returncode == 0

    story = []
    for call in fork.calls():
        if call["argv"][0] == "rm":
            story.append(("put away", call["argv"][-1]))
        elif "side" in call:
            story.append(("run", after(call["argv"], "--name")[0]))
    assert story[:6] == [
        ("put away", "piqnyx-ov-tests-old"),
        ("run", "piqnyx-ov-tests-old"),
        ("put away", "piqnyx-ov-tests-old"),
        ("put away", "piqnyx-ov-tests-new"),
        ("run", "piqnyx-ov-tests-new"),
        ("put away", "piqnyx-ov-tests-new"),
    ]
    assert set(story[6:]) == {
        ("put away", "piqnyx-ov-tests-old"),
        ("put away", "piqnyx-ov-tests-new"),
    }


def test_the_tests_leave_the_fork_as_it_was(fork):
    assert fork.run("test-in-image.sh").returncode == 0

    assert fork.git("status", "--porcelain", "--untracked-files=all") == ""
    assert (fork.root / "piqnyx" / ".work" / "tests" / "new.rec").is_file()
