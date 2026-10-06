# piqnyx: our image takes the place of the running one, and gives it back if it does not rise.
# SPDX-License-Identifier: AGPL-3.0
"""replace-container.sh, on a made-up server: a real git, a stand-in for docker.

What is checked here is how the script reasons, what it asks docker for and
in what order, and what it leaves on the disk. How a real docker answers is
seen on the server only.

Run from the root of the fork:  python3 -m pytest piqnyx/tests -q -p no:cacheprovider -o addopts=""
"""

import pytest
from test_edit_compose import COMPOSE
from test_scripts import FACTS, OUR_IMAGE, SESSION, THEIR_IMAGE, Fork

THEIRS = COMPOSE.replace("ghcr.io/volcengine/openviking", THEIR_IMAGE[:-7])
OURS = THEIRS.replace(
    f"    image: {THEIR_IMAGE}\n", f"    image: {OUR_IMAGE}\n    pull_policy: never\n"
)
KEPT = "docker-compose.yml.before-piqnyx"
NEVER_HEALTHY = ["running starting"]


class Server(Fork):
    """The fork in little, with what the script is to be run with."""

    def replace(self, *keys, **more):
        more.setdefault("PIQNYX_LOOK_EVERY", "1")
        self.done = self.run("replace-container.sh", *keys, **more)
        return self.done

    def story(self):
        """What was asked of docker that changes something, in order."""
        told = []
        for call in self.calls():
            if call["argv"][0] == "compose" and call["compose"][:1] in (["down"], ["up"]):
                told.append((call["compose"][0], call.get("names")))
        return told

    def kept(self):
        return self.home / KEPT

    def work(self, name):
        return self.root / "piqnyx" / ".work" / "replace" / name


@pytest.fixture
def server(tmp_path):
    return Server(tmp_path)


def last(done):
    return done.stdout.strip().splitlines()[-1]


# ------------------------------------------------------------ without a key


def test_without_a_key_it_shows_what_would_change_and_touches_nothing(server):
    done = server.replace()

    assert done.returncode == 0, done.stdout + done.stderr
    assert f"- image: {THEIR_IMAGE}" in done.stdout
    assert f"+ image: {OUR_IMAGE}" in done.stdout
    assert "+ pull_policy: never" in done.stdout
    assert "архивов в работе нет" in done.stdout
    assert "ничего не тронуто" in last(done)
    assert server.compose.read_text() == THEIRS
    assert not server.kept().exists()
    assert server.story() == []
    assert server.running()["image"] == THEIR_IMAGE


def test_without_a_key_it_says_when_the_time_is_wrong(server):
    (server.data / SESSION / "history" / "archive_003").mkdir()

    done = server.replace()

    assert done.returncode != 0
    assert "archive_003" in done.stdout
    assert "нельзя" in last(done)
    assert server.story() == []


def test_a_key_it_does_not_know_is_refused(server):
    done = server.replace("--now")

    assert done.returncode != 0
    assert "--now" in done.stderr
    assert server.story() == []
    assert server.compose.read_text() == THEIRS


# ------------------------------------------------------------------- --do


def test_our_image_takes_the_place_of_the_running_one(server):
    done = server.replace("--do")

    assert done.returncode == 0, done.stdout + done.stderr
    assert server.story() == [("down", OUR_IMAGE), ("up", OUR_IMAGE)]
    assert server.running()["image"] == OUR_IMAGE
    assert server.compose.read_text() == OURS
    assert server.kept().read_text() == THEIRS
    assert "ИТОГ ЗАМЕНЫ" in last(done) and "ОСТАНОВКА" not in last(done)
    assert OUR_IMAGE in last(done)
    assert f"версия внутри: {FACTS['VERSION']}" in done.stdout


def test_the_image_is_checked_once_more_before_anything_is_touched(server):
    assert server.replace("--do").returncode == 0

    calls = server.calls()
    checked = [n for n, call in enumerate(calls) if "sha256sum" in " ".join(call["argv"])]
    stopped = [n for n, call in enumerate(calls) if call.get("compose", [None])[:1] == ["down"]]
    assert len(checked) == 2 and len(stopped) == 1
    assert max(checked) < stopped[0]
    assert "ИТОГ ПРОВЕРКИ: образ" in server.done.stdout


def test_the_new_file_is_asked_about_before_the_server_is_stopped(server):
    assert server.replace("--do").returncode == 0

    calls = [call for call in server.calls() if call["argv"][0] == "compose"]
    asked = [call["compose"][0] for call in calls if call["compose"][0] != "version"]
    assert asked == ["config", "down", "up"]
    assert calls[[call["compose"][0] for call in calls].index("config")]["names"] == OUR_IMAGE


def test_what_the_old_container_said_is_kept(server):
    assert server.replace("--do").returncode == 0

    said = server.work("old-container.log")
    # Both ways out of the container are kept; which comes first is not ours to say.
    assert sorted(said.read_text().splitlines()) == [
        f"a line of the log of {THEIR_IMAGE}",
        "and one on the other way out",
    ]
    assert oct(said.stat().st_mode & 0o777) == "0o600"
    assert "a line of the log" not in server.done.stdout + server.done.stderr


def test_the_file_keeps_the_mode_it_had(server):
    server.compose.chmod(0o640)

    assert server.replace("--do").returncode == 0

    assert oct(server.compose.stat().st_mode & 0o777) == "0o640"
    assert oct(server.kept().stat().st_mode & 0o777) == "0o640"


def test_the_replacement_does_not_care_where_it_is_called_from(server, tmp_path):
    server.done = server.run("replace-container.sh", "--do", cwd=tmp_path, PIQNYX_LOOK_EVERY="1")

    assert server.done.returncode == 0, server.done.stdout + server.done.stderr
    assert server.running()["image"] == OUR_IMAGE


def test_an_archive_in_work_stops_the_replacement(server):
    (server.data / SESSION / "history" / "archive_003").mkdir()

    done = server.replace("--do")

    assert done.returncode != 0
    assert "archive_003" in done.stdout
    assert "ОСТАНОВКА" in last(done)
    assert server.story() == []
    assert server.compose.read_text() == THEIRS
    assert not server.kept().exists()


def test_data_that_is_not_where_the_file_says_stop_the_replacement(server):
    server.compose.write_text(THEIRS.replace("../data/openviking", "../data/elsewhere"))

    done = server.replace("--do")

    assert done.returncode != 0
    assert "elsewhere" in done.stdout
    assert server.story() == []


def test_an_image_that_did_not_pass_the_check_is_not_put_in(server):
    server.images.new["/app/.venv/bin/surprise"] = ("?", "755", "0:0")

    done = server.replace("--do")

    assert done.returncode != 0
    assert "surprise" in done.stdout
    assert server.story() == []
    assert server.compose.read_text() == THEIRS


def test_an_image_of_ours_that_is_not_on_the_disk_stops_the_replacement(server):
    del server.scenario["images"][OUR_IMAGE]

    done = server.replace("--do")

    assert done.returncode != 0
    assert OUR_IMAGE in done.stderr and "build.sh" in done.stderr
    assert server.story() == []


def test_a_file_that_names_an_image_of_somebody_else_is_not_changed(server):
    server.compose.write_text(THEIRS.replace(THEIR_IMAGE, "registry.example/other:latest"))

    done = server.replace("--do")

    assert done.returncode != 0
    assert "registry.example/other:latest" in done.stdout + done.stderr
    assert server.story() == []


def test_a_file_that_names_another_image_than_we_built_on_is_not_changed(server):
    server.scenario["images"][THEIR_IMAGE] = {"side": "old", "id": "sha256:newer"}
    server.runs({"image": THEIR_IMAGE, "id": "sha256:newer", "looked": 1})

    done = server.replace("--do")

    assert done.returncode != 0
    assert "не тот, на котором собран наш" in done.stderr
    assert server.story() == []


@pytest.mark.parametrize(
    "container, word",
    [
        (None, "нет"),
        ({"image": THEIR_IMAGE, "id": "sha256:other", "looked": 1}, "не на том образе"),
    ],
)
def test_a_container_that_is_not_what_the_file_says_stops_the_replacement(server, container, word):
    server.runs(container)

    done = server.replace("--do")

    assert done.returncode != 0
    assert word in done.stderr
    assert server.story() == []
    assert server.compose.read_text() == THEIRS


@pytest.mark.parametrize("state", ["exited none", "running unhealthy", "running starting"])
def test_a_server_that_is_not_well_is_not_replaced(server, state):
    server.scenario["states"] = {THEIR_IMAGE: [state]}

    done = server.replace("--do")

    assert done.returncode != 0
    assert "сперва разобраться" in done.stderr
    assert server.story() == []


def test_a_copy_kept_from_before_that_is_another_file_stops_the_replacement(server):
    server.kept().write_text(THEIRS.replace("30s", "31s"))

    done = server.replace("--do")

    assert done.returncode != 0
    assert KEPT in done.stderr
    assert server.story() == []
    assert server.compose.read_text() == THEIRS


def test_a_copy_kept_from_before_that_is_the_same_file_is_in_order(server):
    server.kept().write_text(THEIRS)

    assert server.replace("--do").returncode == 0
    assert server.kept().read_text() == THEIRS


def test_a_new_file_that_docker_refuses_is_not_put_in_place(server):
    server.scenario["config_code"] = 1

    done = server.replace("--do")

    assert done.returncode != 0
    assert "compose-файл" in done.stderr
    assert server.story() == []
    assert server.compose.read_text() == THEIRS
    assert server.running()["image"] == THEIR_IMAGE


def test_a_docker_without_compose_stops_the_replacement(server):
    server.scenario["compose"] = False

    done = server.replace("--do")

    assert done.returncode != 0
    assert "compose" in done.stderr
    assert server.story() == []


# ------------------------------------------------------- it does not rise


def taken_back(server, done):
    assert done.returncode != 0
    assert server.compose.read_text() == THEIRS
    assert server.running()["image"] == THEIR_IMAGE
    assert server.story() == [
        ("down", OUR_IMAGE),
        ("up", OUR_IMAGE),
        ("down", THEIR_IMAGE),
        ("up", THEIR_IMAGE),
    ]
    assert "ЗАМЕНА НЕ УДАЛАСЬ" in last(done) and "прежний сервер возвращён" in last(done)
    assert server.kept().read_text() == THEIRS


def test_a_server_that_does_not_get_well_in_time_is_taken_back(server):
    server.scenario["states"] = {OUR_IMAGE: NEVER_HEALTHY}

    done = server.replace("--do")

    taken_back(server, done)
    assert f"не стал здоровым за {FACTS['HEALTHY_WITHIN']} с" in done.stdout
    assert f"a line of the log of {OUR_IMAGE}" in server.work("new-container.log").read_text()
    assert oct(server.work("new-container.log").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("state", ["exited none", "dead none", "running unhealthy"])
def test_a_server_that_fell_is_taken_back_at_once(server, state):
    server.scenario["states"] = {OUR_IMAGE: ["running starting", state]}
    server.facts(HEALTHY_WITHIN="200")
    server.commit("a long wait")

    done = server.replace("--do")

    taken_back(server, done)
    status, health = state.split()
    assert f"упал: состояние {status}, здоровье {health}" in done.stdout


def test_a_server_that_could_not_be_started_is_taken_back(server):
    server.scenario["up_codes"] = {OUR_IMAGE: 1}

    done = server.replace("--do")

    assert done.returncode != 0
    assert server.compose.read_text() == THEIRS
    assert server.running()["image"] == THEIR_IMAGE
    assert server.story()[-2:] == [("down", THEIR_IMAGE), ("up", THEIR_IMAGE)]
    assert "прежний сервер возвращён" in last(done)


def test_a_container_of_another_image_than_ours_is_taken_back(server):
    server.scenario["images"][OUR_IMAGE]["version_inside"] = "0.0.0-another"

    done = server.replace("--do")

    taken_back(server, done)
    assert "0.0.0-another" in done.stdout


def test_when_the_old_server_does_not_rise_either_it_is_said_loudly(server):
    # The old one is well when the script begins and is not when it is brought back.
    server.scenario["states"] = {
        OUR_IMAGE: NEVER_HEALTHY,
        THEIR_IMAGE: ["running starting"] * 50 + ["running healthy"],
    }
    server.runs({"image": THEIR_IMAGE, "id": "sha256:o", "looked": 50})

    done = server.replace("--do")

    assert done.returncode != 0
    assert "ПРЕЖНИЙ СЕРВЕР НЕ ПОДНЯЛСЯ" in last(done)
    assert "docker compose" in done.stdout
    assert server.compose.read_text() == THEIRS
    assert server.story() == [
        ("down", OUR_IMAGE),
        ("up", OUR_IMAGE),
        ("down", THEIR_IMAGE),
        ("up", THEIR_IMAGE),
    ]


# ------------------------------------------------------ done already, back


def test_a_replacement_that_is_done_is_not_done_again(server):
    assert server.replace("--do").returncode == 0
    (server.docker / "calls.jsonl").unlink()

    done = server.replace("--do")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "уже" in last(done)
    assert server.story() == []
    assert server.compose.read_text() == OURS


def test_without_a_key_after_the_replacement_it_says_what_runs(server):
    assert server.replace("--do").returncode == 0

    done = server.replace()

    assert done.returncode == 0
    assert "уже" in last(done) and OUR_IMAGE in done.stdout
    assert server.compose.read_text() == OURS


def test_the_way_back_brings_the_old_server(server):
    assert server.replace("--do").returncode == 0
    (server.docker / "calls.jsonl").unlink()

    done = server.replace("--back")

    assert done.returncode == 0, done.stdout + done.stderr
    assert server.story() == [("down", THEIR_IMAGE), ("up", THEIR_IMAGE)]
    assert server.running()["image"] == THEIR_IMAGE
    assert server.compose.read_text() == THEIRS
    assert "ИТОГ ВОЗВРАТА" in last(done) and THEIR_IMAGE in last(done)
    assert server.kept().read_text() == THEIRS


def test_the_way_back_is_open_though_an_archive_is_in_work(server):
    assert server.replace("--do").returncode == 0
    (server.data / SESSION / "history" / "archive_003").mkdir()

    done = server.replace("--back")

    assert done.returncode == 0, done.stdout + done.stderr
    assert server.running()["image"] == THEIR_IMAGE
    assert "archive_003" in done.stdout


def test_the_way_back_without_a_copy_is_refused(server):
    done = server.replace("--back")

    assert done.returncode != 0
    assert KEPT in done.stderr
    assert server.story() == []
    assert server.compose.read_text() == THEIRS


def test_the_way_back_when_the_old_server_runs_does_nothing(server):
    server.kept().write_text(THEIRS)

    done = server.replace("--back")

    assert done.returncode == 0
    assert "уже" in last(done)
    assert server.story() == []


def test_the_replacement_leaves_the_fork_as_it_was(server):
    assert server.replace("--do").returncode == 0

    assert server.git("status", "--porcelain", "--untracked-files=all") == ""


# ------------------------------------------- an earlier image of ours is there
# PLAN-gorizont 3д: the second build takes the place of the first. The file then
# names an image of ours; the copy of the file before this replacement is kept
# and the older copy is put aside, numbered; the way back leads to the first
# image of ours, not to theirs.

OUR_IMAGE_BEFORE = f"{FACTS['NAME']}:0.0.0-test.0"
ON_OURS = THEIRS.replace(
    f"    image: {THEIR_IMAGE}\n", f"    image: {OUR_IMAGE_BEFORE}\n    pull_policy: never\n"
)


@pytest.fixture
def server_on_ours(server):
    """The server after the first replacement: on an image of ours, theirs kept beside."""
    server.scenario["images"][OUR_IMAGE_BEFORE] = {
        "side": "new",
        "config": server.images.new_config,
        "id": "sha256:p",
        "version_inside": "0.0.0-test.0",
    }
    server.compose.write_text(ON_OURS)
    server.kept().write_text(THEIRS)
    server.runs({"image": OUR_IMAGE_BEFORE, "id": "sha256:p", "looked": 1})
    return server


def test_without_a_key_on_an_earlier_image_of_ours_it_shows_the_change(server_on_ours):
    done = server_on_ours.replace()

    assert done.returncode == 0, done.stdout + done.stderr
    assert f"- image: {OUR_IMAGE_BEFORE}" in done.stdout
    assert f"+ image: {OUR_IMAGE}" in done.stdout
    assert "ничего не тронуто" in last(done)
    assert server_on_ours.compose.read_text() == ON_OURS
    assert server_on_ours.story() == []


def test_an_earlier_image_of_ours_is_replaced_and_both_copies_are_kept(server_on_ours):
    done = server_on_ours.replace("--do")

    assert done.returncode == 0, done.stdout + done.stderr
    assert server_on_ours.story() == [("down", OUR_IMAGE), ("up", OUR_IMAGE)]
    assert server_on_ours.running()["image"] == OUR_IMAGE
    assert server_on_ours.compose.read_text() == OURS
    assert server_on_ours.kept().read_text() == ON_OURS
    assert (server_on_ours.home / f"{KEPT}.1").read_text() == THEIRS
    assert "ИТОГ ЗАМЕНЫ" in last(done) and OUR_IMAGE in last(done)


def test_the_way_back_from_the_second_replacement_leads_to_the_first_image_of_ours(server_on_ours):
    assert server_on_ours.replace("--do").returncode == 0

    done = server_on_ours.replace("--back")

    assert done.returncode == 0, done.stdout + done.stderr
    assert server_on_ours.running()["image"] == OUR_IMAGE_BEFORE
    assert server_on_ours.compose.read_text() == ON_OURS
    assert server_on_ours.story()[-2:] == [("down", OUR_IMAGE_BEFORE), ("up", OUR_IMAGE_BEFORE)]


def test_a_second_replacement_that_does_not_rise_goes_back_to_the_first_image_of_ours(
    server_on_ours,
):
    server_on_ours.scenario["states"] = {OUR_IMAGE: NEVER_HEALTHY}

    done = server_on_ours.replace("--do")

    assert done.returncode != 0
    assert server_on_ours.compose.read_text() == ON_OURS
    assert server_on_ours.running()["image"] == OUR_IMAGE_BEFORE
    assert server_on_ours.story() == [
        ("down", OUR_IMAGE),
        ("up", OUR_IMAGE),
        ("down", OUR_IMAGE_BEFORE),
        ("up", OUR_IMAGE_BEFORE),
    ]
    assert server_on_ours.kept().read_text() == ON_OURS
    assert (server_on_ours.home / f"{KEPT}.1").read_text() == THEIRS


def test_a_third_replacement_numbers_the_next_copy(server_on_ours):
    (server_on_ours.home / f"{KEPT}.1").write_text("an older copy\n")

    assert server_on_ours.replace("--do").returncode == 0

    assert (server_on_ours.home / f"{KEPT}.1").read_text() == "an older copy\n"
    assert (server_on_ours.home / f"{KEPT}.2").read_text() == THEIRS
    assert server_on_ours.kept().read_text() == ON_OURS


def test_a_file_of_theirs_with_another_copy_kept_is_still_refused(server):
    # The first replacement: a stray copy that is not this file stops it, as before.
    server.kept().write_text(THEIRS.replace("30s", "31s"))

    done = server.replace("--do")

    assert done.returncode != 0
    assert server.story() == []
