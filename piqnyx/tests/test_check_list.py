# piqnyx: the list of our files is the difference from the tag, no more and no less.
# SPDX-License-Identifier: AGPL-3.0
"""The check of the list before the build, on made-up differences.

Run from the root of the fork:  python3 -m pytest piqnyx/tests -q -p no:cacheprovider --no-cov
"""

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_list", HERE.parent / "check_list.py")
check_list = importlib.util.module_from_spec(spec)
sys.modules["check_list"] = check_list
spec.loader.exec_module(check_list)

# What `git diff --name-status --no-renames TAG HEAD` says of our branch.
CHANGES = (
    "A\tPIQNYX.md\n"
    "M\topenviking/session/session.py\n"
    "A\topenviking/utils/piqnyx_persistence.py\n"
    "A\tpiqnyx/Dockerfile\n"
    "A\ttests/unit/test_piqnyx_persistence.py\n"
)
LIST = "openviking/session/session.py\nopenviking/utils/piqnyx_persistence.py\n"


def told(lines, *words):
    return any(all(word in line for word in words) for line in lines)


def test_the_list_that_is_the_difference_is_in_order():
    ok, lines = check_list.check(CHANGES, LIST)

    assert ok, lines
    assert told(lines, "в списке 2", "изменено 1", "новых 1")


def test_a_file_we_changed_and_left_out_of_the_list_stops_the_build():
    ok, lines = check_list.check(CHANGES + "M\topenviking/utils/model_retry.py\n", LIST)

    assert not ok
    assert told(lines, "openviking/utils/model_retry.py", "в списке его нет")


def test_a_file_of_the_other_package_counts_too():
    ok, lines = check_list.check(CHANGES + "M\topenviking_cli/utils/logger.py\n", LIST)

    assert not ok
    assert told(lines, "openviking_cli/utils/logger.py", "в списке его нет")


def test_a_file_in_the_list_that_is_as_the_tag_has_it_stops_the_build():
    ok, lines = check_list.check(CHANGES, LIST + "openviking/utils/model_retry.py\n")

    assert not ok
    assert told(lines, "openviking/utils/model_retry.py", "не отличается")


def test_a_file_we_removed_cannot_be_laid_over():
    ok, lines = check_list.check(CHANGES + "D\topenviking/utils/gone.py\n", LIST)

    assert not ok
    assert told(lines, "openviking/utils/gone.py", "убран")


def test_a_change_of_what_the_recipe_does_not_put_in_the_image_stops_the_build():
    for path in ("docker/openviking-entrypoint.sh", "pyproject.toml", "crates/ragfs/src/lib.rs"):
        ok, lines = check_list.check(CHANGES + f"M\t{path}\n", LIST)

        assert not ok, path
        assert told(lines, path, "в образ не кладёт")


def test_what_lies_beside_the_image_may_change_freely():
    more = "M\ttests/session/conftest.py\nA\tpiqnyx/build.sh\nM\tPIQNYX.md\n"

    ok, lines = check_list.check(CHANGES + more, LIST)

    assert ok, lines


def test_a_name_given_twice_stops_the_build():
    ok, lines = check_list.check(CHANGES, LIST + "openviking/session/session.py\n")

    assert not ok
    assert told(lines, "openviking/session/session.py", "дважды")


def test_a_list_with_foreign_line_ends_or_spaces_stops_the_build():
    ok, lines = check_list.check(CHANGES, LIST.replace("\n", "\r\n"))
    assert not ok
    assert told(lines, "лишние")

    ok, lines = check_list.check(CHANGES, LIST.replace("session.py", "session.py "))
    assert not ok
    assert told(lines, "лишние")


def test_a_name_outside_the_two_packages_stops_the_build():
    ok, lines = check_list.check(CHANGES, LIST + "piqnyx/Dockerfile\n")

    assert not ok
    assert told(lines, "piqnyx/Dockerfile", "не из пакетов")


def test_an_empty_list_is_a_failure_not_a_pass():
    ok, lines = check_list.check("A\tPIQNYX.md\n", "\n")

    assert not ok
    assert told(lines, "пуст")


def test_a_difference_that_cannot_be_read_is_a_failure():
    ok, lines = check_list.check("what is this\n", LIST)

    assert not ok
    assert told(lines, "не прочитать")


def test_the_program_reads_both_from_files(tmp_path, capsys):
    changes = tmp_path / "changes.txt"
    changes.write_text(CHANGES)
    overlay = tmp_path / "overlay.txt"
    overlay.write_text(LIST)

    assert check_list.main(["--changes", str(changes), "--overlay", str(overlay)]) == 0
    assert "ИТОГ" in capsys.readouterr().out.splitlines()[-1]

    overlay.write_text(LIST + "openviking/utils/model_retry.py\n")
    assert check_list.main(["--changes", str(changes), "--overlay", str(overlay)]) == 1
