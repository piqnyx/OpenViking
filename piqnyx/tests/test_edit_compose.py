# piqnyx: in the compose file the image changes, and nothing else does.
# SPDX-License-Identifier: AGPL-3.0
"""The edit of the compose file, on the file of the server as Vit showed it and on made-up ones.

Run from the root of the fork:  python3 -m pytest piqnyx/tests -q -p no:cacheprovider -o addopts=""
"""

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("edit_compose", HERE.parent / "edit_compose.py")
edit_compose = importlib.util.module_from_spec(spec)
sys.modules["edit_compose"] = edit_compose
spec.loader.exec_module(edit_compose)

OURS = "piqnyx/openviking:0.4.12-piqnyx.1"
THEIRS = "ghcr.io/volcengine/openviking"

# The compose file of the server, as shown on 29.09.2026.
COMPOSE = """\
services:
  openviking:
    image: ghcr.io/volcengine/openviking:latest
    container_name: openviking
    user: "1001:1001"
    network_mode: host
    environment:
      OPENVIKING_SERVER_HOST: "127.0.0.1"
    volumes:
      - ../data/openviking:/app/.openviking
      - ../templates/build:/app/templates/build:ro
    command: ["--without-bot"]
    healthcheck:
      test: ["CMD", "openviking-entrypoint", "--healthcheck"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    restart: unless-stopped
    stop_grace_period: 30s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
"""


def edit(text=COMPOSE, **how):
    how = {"service": "openviking", "image": OURS, "was_of": THEIRS, **how}
    return edit_compose.edit(text, **how)


def refused(text=COMPOSE, **how):
    with pytest.raises(edit_compose.Refused) as told:
        edit(text, **how)
    return str(told.value)


def test_the_image_is_changed_and_the_policy_is_put_after_it():
    done = edit()

    assert done.changed
    assert done.was == "ghcr.io/volcengine/openviking:latest"
    assert done.text == COMPOSE.replace(
        "    image: ghcr.io/volcengine/openviking:latest\n",
        f"    image: {OURS}\n    pull_policy: never\n",
    )


def test_nothing_else_of_the_file_is_touched():
    done = edit()

    before, after = COMPOSE.splitlines(), done.text.splitlines()
    assert len(after) == len(before) + 1
    assert [line for line in before if line not in after] == [
        "    image: ghcr.io/volcengine/openviking:latest"
    ]
    assert [line for line in after if line not in before] == [
        f"    image: {OURS}",
        "    pull_policy: never",
    ]
    assert edit_compose.only_the_image_changed(COMPOSE, done.text, image=OURS) == []


def test_a_file_that_is_ours_already_is_left_as_it_is():
    ours = edit().text

    done = edit(ours)

    assert not done.changed
    assert done.text == ours
    assert done.was == OURS


def test_an_image_of_ours_without_the_policy_gets_the_policy():
    text = COMPOSE.replace("ghcr.io/volcengine/openviking:latest", OURS)

    done = edit(text)

    assert done.changed
    assert done.text == edit().text


def test_a_policy_that_is_there_is_set_and_not_doubled():
    text = COMPOSE.replace("    user:", "    pull_policy: always\n    user:")

    done = edit(text)

    assert done.text.count("pull_policy") == 1
    assert "    pull_policy: never\n" in done.text
    assert f"    image: {OURS}\n" in done.text
    assert edit_compose.only_the_image_changed(text, done.text, image=OURS) == []


@pytest.mark.parametrize(
    "line",
    [
        '    image: "ghcr.io/volcengine/openviking:latest"',
        "    image: 'ghcr.io/volcengine/openviking:latest'",
        "    image:   ghcr.io/volcengine/openviking:latest   ",
        "    image: ghcr.io/volcengine/openviking:latest  # the running one",
        "    image: ghcr.io/volcengine/openviking@sha256:" + "0" * 64,
        "    image: ghcr.io/volcengine/openviking",
    ],
)
def test_the_image_is_found_however_it_is_written(line):
    text = COMPOSE.replace("    image: ghcr.io/volcengine/openviking:latest", line)

    done = edit(text)

    assert done.changed
    assert f"    image: {OURS}" in done.text
    assert done.text.count("image:") == 1
    assert done.was.startswith(THEIRS)


def test_a_note_at_the_end_of_the_line_is_kept():
    text = COMPOSE.replace(
        "    image: ghcr.io/volcengine/openviking:latest",
        "    image: ghcr.io/volcengine/openviking:latest  # the running one",
    )

    assert f"    image: {OURS}  # the running one\n" in edit(text).text


def test_other_services_and_their_images_are_left_alone():
    text = COMPOSE + "  qdrant:\n    image: qdrant/qdrant:v1.0.0\n    restart: always\n"
    text = "# memory\n\n" + text.replace("services:\n", "services:\n  # the server of memory\n")

    done = edit(text)

    assert "    image: qdrant/qdrant:v1.0.0\n" in done.text
    assert done.text.count("pull_policy: never") == 1
    assert done.text.index("pull_policy: never") < done.text.index("qdrant:")
    assert edit_compose.only_the_image_changed(text, done.text, image=OURS) == []


def test_the_end_of_lines_the_file_has_is_kept():
    text = COMPOSE.replace("\n", "\r\n")

    done = edit(text)

    assert f"    image: {OURS}\r\n    pull_policy: never\r\n" in done.text
    assert "\n" not in done.text.replace("\r\n", "")


def test_a_file_indented_another_way_is_read_too():
    text = COMPOSE.replace("    ", "\t\t").replace("  openviking:", "\topenviking:")
    wider = COMPOSE.replace("  ", "      ")

    assert f"\t\timage: {OURS}\n\t\tpull_policy: never\n" in edit(text).text
    assert f"            image: {OURS}\n            pull_policy: never\n" in edit(wider).text


def test_an_image_of_somebody_else_is_refused():
    text = COMPOSE.replace("ghcr.io/volcengine/openviking:latest", "ghcr.io/other/openviking:1")

    assert "ghcr.io/other/openviking:1" in refused(text)
    assert "openviking-fork" in refused(COMPOSE.replace("openviking:latest", "openviking-fork:1"))


def test_a_file_without_the_service_or_its_image_is_refused():
    assert "нет раздела services" in refused("version: '3'\n")
    assert "нет сервиса openviking" in refused(COMPOSE.replace("  openviking:", "  viking:"))
    assert "нет строки image" in refused(
        COMPOSE.replace("    image: ghcr.io/volcengine/openviking:latest\n", "")
    )


def test_an_image_given_twice_is_refused():
    text = COMPOSE.replace(
        "    user:", "    image: ghcr.io/volcengine/openviking:0.4.12\n    user:"
    )

    assert "дважды" in refused(text)


def test_an_image_of_a_deeper_level_is_not_taken_for_the_image_of_the_service():
    text = COMPOSE.replace(
        "    logging:\n", "    build:\n      image: not-this-one\n    logging:\n"
    )

    done = edit(text)

    assert "      image: not-this-one\n" in done.text
    assert f"    image: {OURS}\n" in done.text


def test_a_change_of_anything_else_is_named():
    ours = edit().text

    assert edit_compose.only_the_image_changed(COMPOSE, ours, image=OURS) == []
    touched = ours.replace('user: "1001:1001"', 'user: "0:0"')
    problems = edit_compose.only_the_image_changed(COMPOSE, touched, image=OURS)
    assert any("user" in line for line in problems)
    lost = ours.replace("      - ../templates/build:/app/templates/build:ro\n", "")
    problems = edit_compose.only_the_image_changed(COMPOSE, lost, image=OURS)
    assert any("templates" in line for line in problems)
    other = ours.replace(OURS, "piqnyx/openviking:another")
    problems = edit_compose.only_the_image_changed(COMPOSE, other, image=OURS)
    assert any("piqnyx/openviking:another" in line for line in problems)
    assert edit_compose.only_the_image_changed(COMPOSE, COMPOSE, image=OURS) != []


def test_the_folder_of_the_data_is_read_from_the_file():
    assert edit_compose.data_folder(COMPOSE, service="openviking") == "../data/openviking"
    quoted = COMPOSE.replace(
        "- ../data/openviking:/app/.openviking", '- "/srv/data:/app/.openviking:rw"'
    )
    assert edit_compose.data_folder(quoted, service="openviking") == "/srv/data"
    none = COMPOSE.replace("      - ../data/openviking:/app/.openviking\n", "")
    assert edit_compose.data_folder(none, service="openviking") is None


def test_the_program_writes_the_new_file_and_says_what_it_did(tmp_path, capsys):
    source = tmp_path / "docker-compose.yml"
    source.write_text(COMPOSE)
    new = tmp_path / "new.yml"
    given = [
        "--file", str(source), "--service", "openviking", "--image", OURS, "--was-of", THEIRS,
    ]  # fmt: skip

    assert edit_compose.main([*given, "--out", str(new)]) == 0
    assert new.read_text() == edit().text
    assert source.read_text() == COMPOSE
    told = capsys.readouterr().out
    assert "- " in told and "+ " in told and OURS in told

    source.write_text(edit().text)
    assert edit_compose.main([*given, "--out", str(new)]) == 3

    source.write_text(COMPOSE.replace("volcengine", "other"))
    assert edit_compose.main([*given, "--out", str(new)]) == 1

    source.write_text(COMPOSE)
    assert edit_compose.main([*given[:4], "--data-folder"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "../data/openviking"


def test_the_program_does_not_trust_its_own_edit(tmp_path, capsys, monkeypatch):
    source = tmp_path / "docker-compose.yml"
    source.write_text(COMPOSE)
    new = tmp_path / "new.yml"
    wrong = edit().text.replace('user: "1001:1001"', 'user: "0:0"')
    monkeypatch.setattr(
        edit_compose, "edit", lambda text, **how: edit_compose.Done(wrong, True, "x")
    )

    code = edit_compose.main(
        ["--file", str(source), "--service", "openviking", "--image", OURS]
        + ["--was-of", THEIRS, "--out", str(new)]
    )

    assert code == 1
    assert not new.exists()
    assert "user" in capsys.readouterr().out


def test_the_image_the_file_names_now_is_told(tmp_path, capsys):
    source = tmp_path / "docker-compose.yml"
    source.write_text(
        COMPOSE.replace(
            "image: ghcr.io/volcengine/openviking:latest",
            'image: "ghcr.io/volcengine/openviking:latest"  # as it runs',
        )
    )
    given = ["--file", str(source), "--service", "openviking", "--image-now"]

    assert edit_compose.main(given) == 0
    assert capsys.readouterr().out.splitlines() == ["ghcr.io/volcengine/openviking:latest"]

    source.write_text(edit().text)
    assert edit_compose.main(given) == 0
    assert capsys.readouterr().out.splitlines() == [OURS]

    source.write_text(COMPOSE.replace("  openviking:", "  viking:"))
    assert edit_compose.main(given) == 1
