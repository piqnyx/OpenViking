# piqnyx: the image is the running one with our files laid over it, and nothing else.
# SPDX-License-Identifier: AGPL-3.0
"""The comparison of two images, file by file, on made-up listings.

Run from the root of the fork:  python3 -m pytest piqnyx/tests -q -p no:cacheprovider --no-cov
"""

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("compare_images", HERE.parent / "compare_images.py")
compare_images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare_images)

SITE = "/app/.venv/lib/python3.13/site-packages"
TAG = "cpython-313"
OURS = ["openviking/session/session.py", "openviking/utils/piqnyx_persistence.py"]


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Images:
    """Two made-up images and the checkout our files come from."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "checkout"
        self.sources = {OURS[0]: "session, ours", OURS[1]: "persistence, ours"}
        for name, text in self.sources.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        base = {
            f"{SITE}/openviking/session/session.py": ("session, upstream", "644", "0:0"),
            f"{SITE}/openviking/session/__pycache__/session.{TAG}.pyc": ("pyc-old", "644", "0:0"),
            f"{SITE}/openviking/utils/model_retry.py": ("retry, upstream", "644", "0:0"),
            f"{SITE}/openviking/utils/__pycache__/model_retry.{TAG}.pyc": ("pyc-r", "644", "0:0"),
            "/usr/local/bin/openviking-entrypoint": ("entrypoint", "755", "0:0"),
            "/etc/hostname": ("old-host", "644", "0:0"),
        }
        self.old = dict(base)
        self.new = dict(base)
        self.new[f"{SITE}/openviking/session/session.py"] = ("session, ours", "644", "0:0")
        self.new[f"{SITE}/openviking/session/__pycache__/session.{TAG}.pyc"] = (
            "pyc-new",
            "644",
            "0:0",
        )
        self.new[f"{SITE}/openviking/utils/piqnyx_persistence.py"] = (
            "persistence, ours",
            "644",
            "0:0",
        )
        self.new[f"{SITE}/openviking/utils/__pycache__/piqnyx_persistence.{TAG}.pyc"] = (
            "pyc-p",
            "644",
            "0:0",
        )
        self.new["/app/PIQNYX-OVERLAY.txt"] = ("\n".join(OURS) + "\n", "644", "0:0")
        self.new["/app/PIQNYX-VERSION"] = ("0.4.12-piqnyx.1\n", "644", "0:0")
        self.new["/etc/hostname"] = ("new-host", "644", "0:0")

    def report(self, overlay=OURS):
        def files(image):
            return "".join(f"{digest(text)}  {path}\n" for path, (text, _, _) in image.items())

        def entries(image):
            return "".join(f"f\t{mode}\t{owner}\t{path}\t\n" for path, (_, mode, owner) in image.items())

        return compare_images.compare(
            old_files=files(self.old),
            new_files=files(self.new),
            old_entries=entries(self.old),
            new_entries=entries(self.new),
            overlay=list(overlay),
            site=SITE,
            cache_tag=TAG,
            source_root=str(self.root),
        )


@pytest.fixture
def images(tmp_path):
    return Images(tmp_path)


def test_our_files_and_nothing_else_is_in_order(images):
    report = images.report()

    assert report.ok, report.text
    assert "2 наших файла" in report.text
    assert report.unexpected == []


def test_a_third_file_that_changed_stops_the_work(images):
    path = f"{SITE}/openviking/utils/model_retry.py"
    images.new[path] = ("retry, touched", "644", "0:0")

    report = images.report()

    assert not report.ok
    assert report.unexpected == [("изменён", path)]


def test_a_file_that_went_missing_stops_the_work(images):
    path = "/usr/local/bin/openviking-entrypoint"
    del images.new[path]

    report = images.report()

    assert not report.ok
    assert report.unexpected == [("пропал", path)]


def test_a_file_nobody_asked_for_stops_the_work(images):
    images.new["/app/.venv/bin/surprise"] = ("?", "755", "0:0")

    report = images.report()

    assert not report.ok
    assert report.unexpected == [("появился", "/app/.venv/bin/surprise")]


def test_the_bytecode_of_another_module_is_not_ours_to_change(images):
    path = f"{SITE}/openviking/utils/__pycache__/model_retry.{TAG}.pyc"
    images.new[path] = ("pyc-r-touched", "644", "0:0")

    report = images.report()

    assert not report.ok
    assert report.unexpected == [("изменён", path)]


def test_our_file_in_the_image_must_be_the_one_in_the_checkout(images):
    images.new[f"{SITE}/openviking/session/session.py"] = ("session, somebody's", "644", "0:0")

    report = images.report()

    assert not report.ok
    assert any("не тот" in line and "session.py" in line for line in report.problems)


def test_our_file_missing_from_the_image_stops_the_work(images):
    del images.new[f"{SITE}/openviking/utils/piqnyx_persistence.py"]

    report = images.report()

    assert not report.ok
    assert any("нет в образе" in line and "piqnyx_persistence.py" in line for line in report.problems)


def test_our_file_must_be_readable_by_the_user_the_server_runs_as(images):
    path = f"{SITE}/openviking/utils/piqnyx_persistence.py"
    text, _, owner = images.new[path]
    images.new[path] = (text, "600", owner)

    report = images.report()

    assert not report.ok
    assert any("не читается" in line and "piqnyx_persistence.py" in line for line in report.problems)


def test_a_mode_or_an_owner_that_changed_elsewhere_stops_the_work(images):
    path = "/usr/local/bin/openviking-entrypoint"
    text, _, owner = images.new[path]
    images.new[path] = (text, "644", owner)

    report = images.report()

    assert not report.ok
    assert report.unexpected == [("права или владелец", path)]


def test_what_docker_writes_at_every_start_is_left_out(images):
    images.new["/etc/hosts"] = ("hosts", "644", "0:0")
    images.new["/etc/resolv.conf"] = ("resolv", "644", "0:0")

    assert images.report().ok


def test_the_list_in_the_image_must_be_the_list_given(images):
    images.new["/app/PIQNYX-OVERLAY.txt"] = (OURS[0] + "\n", "644", "0:0")

    report = images.report()

    assert not report.ok
    assert any("список" in line for line in report.problems)


def test_an_empty_listing_is_a_failure_not_a_pass(images):
    images.old.clear()
    images.new.clear()

    report = images.report()

    assert not report.ok
    assert any("пуст" in line for line in report.problems)
