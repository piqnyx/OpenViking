# piqnyx: the server is not stopped while an archive of a session is in work.
# SPDX-License-Identifier: AGPL-3.0
"""The look at the archives of the sessions on the disk, on made-up folders.

Run from the root of the fork:  python3 -m pytest piqnyx/tests -q -p no:cacheprovider -o addopts=""
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "pending_archives", HERE.parent / "pending_archives.py"
)
pending_archives = importlib.util.module_from_spec(spec)
sys.modules["pending_archives"] = pending_archives
spec.loader.exec_module(pending_archives)

MAIN = "workspace/viking/openclaw-main/user/agent-main/sessions/9478e347"
OTHER = "workspace/viking/openclaw-main/user/agent-igor/sessions/11112222"


class Data:
    """The folder of the data of the server, archives in it."""

    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True)

    def archive(self, session, number, marker):
        folder = self.root / session / "history" / f"archive_{number:03d}"
        folder.mkdir(parents=True)
        (folder / "messages.jsonl").write_text("what was said\n")
        if marker:
            (folder / marker).write_text("{}")
        return folder

    def look(self):
        return pending_archives.look(str(self.root))


@pytest.fixture
def data(tmp_path):
    made = Data(tmp_path / "data")
    for number in range(1, 37):
        made.archive(MAIN, number, ".done")
    made.archive(MAIN, 37, ".failed.json")
    made.archive(OTHER, 1, ".done")
    return made


def test_archives_that_are_all_closed_or_failed_let_the_server_stop(data):
    found = data.look()

    assert found.ok
    assert (found.sessions, found.archives, found.done, found.failed) == (2, 38, 37, 1)
    assert found.pending == []


def test_an_archive_in_work_does_not_let_the_server_stop(data):
    data.archive(MAIN, 38, None)

    found = data.look()

    assert not found.ok
    assert found.pending == [("9478e347", "archive_038")]
    assert any("9478e347" in line and "archive_038" in line for line in found.lines)


def test_an_archive_in_work_of_any_session_counts(data):
    data.archive(OTHER, 2, None)
    data.archive(MAIN, 38, None)

    assert data.look().pending == [("11112222", "archive_002"), ("9478e347", "archive_038")]


def test_an_archive_that_failed_and_was_closed_later_is_closed(data):
    folder = data.archive(MAIN, 38, ".failed.json")
    (folder / ".done").write_text("{}")

    found = data.look()

    assert found.ok
    assert (found.done, found.failed) == (38, 1)


def test_no_archives_at_all_is_not_taken_for_all_is_well(tmp_path):
    empty = Data(tmp_path / "empty")
    (empty.root / "workspace" / "viking").mkdir(parents=True)

    found = empty.look()

    assert not found.ok
    assert any("ни одного" in line for line in found.lines)


def test_a_folder_that_is_not_there_is_a_failure(tmp_path):
    found = pending_archives.look(str(tmp_path / "no-such-data"))

    assert not found.ok
    assert any("нет каталога" in line for line in found.lines)


def test_a_folder_that_cannot_be_read_is_a_failure(data):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    closed = data.root / MAIN / "history"
    closed.chmod(0)

    found = data.look()

    closed.chmod(0o755)
    assert not found.ok
    assert any("не прочитать" in line for line in found.lines)


def test_what_is_said_in_the_archives_is_not_read_and_not_shown(data):
    folder = data.archive(MAIN, 38, None)
    (folder / "messages.jsonl").write_text("a secret said in the chat\n")

    found = data.look()

    assert not any("secret" in line for line in found.lines)


def test_the_archives_themselves_are_not_gone_into(data):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    inside = data.root / MAIN / "history" / "archive_001" / "kept-to-itself"
    inside.mkdir()
    inside.chmod(0)

    found = data.look()

    inside.chmod(0o755)
    assert found.ok, found.lines


def test_folders_that_only_look_like_archives_are_left_out(data):
    (data.root / "workspace" / "resources" / "archive_001").mkdir(parents=True)
    (data.root / MAIN / "history" / "notes").mkdir()
    (data.root / MAIN / "history" / "archive_index.json").write_text("{}")

    found = data.look()

    assert found.ok
    assert found.archives == 38


def test_the_program_says_what_it_found(data, capsys):
    assert pending_archives.main([str(data.root)]) == 0
    assert "архивов 38" in capsys.readouterr().out

    data.archive(MAIN, 38, None)
    assert pending_archives.main([str(data.root)]) == 1
    assert "archive_038" in capsys.readouterr().out
