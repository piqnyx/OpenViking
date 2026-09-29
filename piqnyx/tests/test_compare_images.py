# piqnyx: the image is the running one with our files laid over it, and nothing else.
# SPDX-License-Identifier: AGPL-3.0
"""The comparison of two images, file by file, on made-up listings.

Run from the root of the fork:  python3 -m pytest piqnyx/tests -q -p no:cacheprovider --no-cov
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("compare_images", HERE.parent / "compare_images.py")
compare_images = importlib.util.module_from_spec(spec)
# Dataclasses look their module up by name while the class is being made.
sys.modules["compare_images"] = compare_images
spec.loader.exec_module(compare_images)

SITE = "/app/.venv/lib/python3.13/site-packages"
TAG = "cpython-313"
OURS = ["openviking/session/session.py", "openviking/utils/piqnyx_persistence.py"]


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def files_of(image):
    """What `sha256sum` would say of the files of a made-up image."""
    return "".join(f"{digest(text)}  {path}\n" for path, (text, _, _) in image.items())


def entries_of(image):
    """What `find -printf` would say of them: kind, mode, owner, path, link."""
    return "".join(f"f\t{mode}\t{owner}\t{path}\t\n" for path, (_, mode, owner) in image.items())


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

        # What the tag holds of the two packages, as `git archive` gives it.
        self.tag = tmp_path / "tag"
        self.tag_files = {
            "openviking/session/session.py": "session, upstream",
            "openviking/utils/model_retry.py": "retry, upstream",
            "openviking/docs/only-in-the-source.md": "never installed",
        }
        for name, text in self.tag_files.items():
            path = self.tag / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

        self.old_config = {
            "User": "",
            "Entrypoint": ["openviking-entrypoint"],
            "Cmd": None,
            "WorkingDir": "/app",
            "Env": ["HOME=/app", "OPENVIKING_CONFIG_FILE=/app/.openviking/ov.conf"],
            "ExposedPorts": {"1933/tcp": {}},
            "Healthcheck": {"Test": ["CMD", "openviking-entrypoint", "--healthcheck"]},
            "Labels": {"org.opencontainers.image.source": "upstream"},
            "Image": "sha256:old",
        }
        self.new_config = dict(
            self.old_config,
            Labels={"org.opencontainers.image.source": "upstream", "org.piqnyx.version": "1"},
            Image="sha256:new",
        )

    def report(self, overlay=OURS):
        return compare_images.compare(
            old_files=files_of(self.old),
            new_files=files_of(self.new),
            old_entries=entries_of(self.old),
            new_entries=entries_of(self.new),
            overlay=list(overlay),
            site=SITE,
            cache_tag=TAG,
            source_root=str(self.root),
            tag_root=str(self.tag),
            old_config=json.dumps(self.old_config),
            new_config=(
                self.new_config if isinstance(self.new_config, str) else json.dumps(self.new_config)
            ),
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
    assert any(
        "нет в образе" in line and "piqnyx_persistence.py" in line for line in report.problems
    )


def test_our_file_must_be_readable_by_the_user_the_server_runs_as(images):
    path = f"{SITE}/openviking/utils/piqnyx_persistence.py"
    text, _, owner = images.new[path]
    images.new[path] = (text, "600", owner)

    report = images.report()

    assert not report.ok
    assert any(
        "не читается" in line and "piqnyx_persistence.py" in line for line in report.problems
    )


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


def test_the_way_the_image_starts_must_not_change(images):
    images.new_config["Entrypoint"] = ["sh"]
    images.new_config["User"] = "0"

    report = images.report()

    assert not report.ok
    assert any("Entrypoint" in line for line in report.problems)
    assert any("User" in line for line in report.problems)


def test_a_setting_that_went_missing_from_the_image_stops_the_work(images):
    del images.new_config["Healthcheck"]

    report = images.report()

    assert not report.ok
    assert any("Healthcheck" in line for line in report.problems)


def test_settings_that_cannot_be_read_are_a_failure_not_a_pass(images):
    images.new_config = "not json"

    report = images.report()

    assert not report.ok
    assert any("настройки" in line for line in report.problems)


def test_the_file_we_replace_must_be_the_one_of_the_tag(images):
    path = f"{SITE}/openviking/session/session.py"
    images.old[path] = ("session, not of the tag", "644", "0:0")

    report = images.report()

    assert not report.ok
    assert any("тег" in line and "session.py" in line for line in report.problems)


def test_the_running_image_must_be_the_tag_in_every_other_file_too(images):
    path = f"{SITE}/openviking/utils/model_retry.py"
    images.old[path] = ("retry, not of the tag", "644", "0:0")
    images.new[path] = images.old[path]

    report = images.report()

    assert not report.ok
    assert report.unexpected == []
    assert any("тег" in line and "model_retry.py" in line for line in report.problems)


def test_a_file_we_call_new_must_not_be_in_the_running_image(images):
    path = f"{SITE}/openviking/utils/piqnyx_persistence.py"
    images.old[path] = ("somebody's", "644", "0:0")

    report = images.report()

    assert not report.ok
    assert any("новым" in line and "piqnyx_persistence.py" in line for line in report.problems)


def test_a_tag_of_which_nothing_is_found_in_the_image_is_a_failure(images):
    for name in list(images.tag_files):
        (images.tag / name).unlink()
    (images.tag / "openviking" / "elsewhere.py").write_text("x")

    report = images.report()

    assert not report.ok
    assert any("ни один" in line for line in report.problems)


def test_files_of_the_tag_the_image_never_had_are_counted_not_blamed(images):
    report = images.report()

    assert report.ok, report.text
    assert "совпало 2 из 2" in report.text
    assert "в образ не ставились: 1" in report.text


def test_a_label_of_the_running_image_must_stay_as_it_was(images):
    images.new_config["Labels"] = {"org.piqnyx.version": "1"}

    report = images.report()

    assert not report.ok
    assert any("org.opencontainers.image.source" in line for line in report.problems)


def test_a_label_that_is_added_must_be_ours(images):
    images.new_config["Labels"]["com.example.surprise"] = "1"

    report = images.report()

    assert not report.ok
    assert any("com.example.surprise" in line for line in report.problems)


def test_images_without_any_labels_are_in_order(images):
    images.old_config["Labels"] = None
    images.new_config["Labels"] = None

    assert images.report().ok


def test_a_file_we_replace_must_be_there_to_replace(images):
    del images.old[f"{SITE}/openviking/session/session.py"]

    report = images.report()

    assert not report.ok
    assert any("нет в исходном образе" in line and "session.py" in line for line in report.problems)


def test_a_file_we_replace_keeps_the_mode_and_the_owner_it_had(images):
    path = f"{SITE}/openviking/session/session.py"
    text, _, _ = images.new[path]
    images.new[path] = (text, "664", "1001:1001")

    report = images.report()

    assert not report.ok
    assert report.unexpected == [("права или владелец", path)]


def test_the_bytecode_we_replace_keeps_the_mode_and_the_owner_it_had(images):
    path = f"{SITE}/openviking/session/__pycache__/session.{TAG}.pyc"
    text, _, _ = images.new[path]
    images.new[path] = (text, "600", "0:0")

    report = images.report()

    assert not report.ok
    assert report.unexpected == [("права или владелец", path)]
