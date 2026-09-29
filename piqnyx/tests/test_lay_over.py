# piqnyx: our files go over the image one by one, and nothing else is touched.
# SPDX-License-Identifier: AGPL-3.0
"""The step of the build that lays our files over the image, on made-up folders.

Run from the root of the fork:  python3 -m pytest piqnyx/tests -q -p no:cacheprovider --no-cov
"""

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "lay_over.sh"

spec = importlib.util.spec_from_file_location("inside_check", HERE.parent / "inside_check.py")
inside_check = importlib.util.module_from_spec(spec)
sys.modules.setdefault("inside_check", inside_check)
spec.loader.exec_module(inside_check)


def listing(root):
    """Every file and folder under `root`: its kind, its mode and what it holds."""
    found = {}
    for folder, folders, names in os.walk(root):
        for name in folders + names:
            path = Path(folder) / name
            mode = oct(path.stat().st_mode & 0o7777)
            held = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "folder"
            found[str(path.relative_to(root))] = (mode, held)
    return found


class Build:
    """The checkout lent to the build, and the image it builds on."""

    def __init__(self, tmp_path):
        self.source = tmp_path / "checkout"
        self.site = tmp_path / "image" / "site-packages"
        self.notes = tmp_path / "image" / "app"
        self.notes.mkdir(parents=True)
        self.put(self.site / "pkg/__init__.py", "")
        self.put(self.site / "pkg/old.py", "X = 'as the image had it'\n")
        self.put(self.site / "pkg/other.py", "Y = 'not ours'\n")
        self.put(self.source / "pkg/old.py", "X = 'ours'\n")
        self.put(self.source / "pkg/new.py", "Z = 'ours, new'\n")
        self.list = self.source / "piqnyx" / "overlay.txt"
        self.put(self.list, "pkg/old.py\npkg/new.py\n")
        # `python` is what the image finds first; here it is the Python of the tests.
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "python").symlink_to(sys.executable)

    @staticmethod
    def put(path, text, mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def run(self, version="0.0.0-test.1"):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        env.pop("SOURCE_DATE_EPOCH", None)
        return subprocess.run(
            ["sh", str(SCRIPT), str(self.source), str(self.site), version, str(self.notes)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )


@pytest.fixture
def build(tmp_path):
    return Build(tmp_path)


def test_our_files_are_laid_over_and_nothing_else_is_touched(build):
    before = listing(build.site)

    done = build.run()

    assert done.returncode == 0, done.stderr
    after = listing(build.site)
    tag = sys.implementation.cache_tag
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
    assert changed == {
        "pkg/old.py",
        "pkg/new.py",
        "pkg/__pycache__",
        f"pkg/__pycache__/old.{tag}.pyc",
        f"pkg/__pycache__/new.{tag}.pyc",
    }
    assert (build.site / "pkg/old.py").read_text() == "X = 'ours'\n"
    assert (build.site / "pkg/new.py").read_text() == "Z = 'ours, new'\n"


def test_the_bytecode_it_leaves_is_of_our_source(build):
    assert build.run().returncode == 0

    for name in ("pkg/old.py", "pkg/new.py"):
        source = str(build.site / name)
        compiled = importlib.util.cache_from_source(source)
        assert inside_check.bytecode_trouble(source, compiled) is None


def test_bytecode_the_image_had_of_the_file_we_replace_is_replaced(build):
    import py_compile

    stale = py_compile.compile(str(build.site / "pkg/old.py"), doraise=True)
    had = Path(stale).read_bytes()

    assert build.run().returncode == 0

    assert Path(stale).read_bytes() != had
    assert inside_check.bytecode_trouble(str(build.site / "pkg/old.py"), stale) is None


def test_the_mode_is_the_one_of_the_image_whatever_the_checkout_has(build):
    (build.source / "pkg/old.py").chmod(0o600)
    (build.source / "pkg/new.py").chmod(0o664)

    assert build.run().returncode == 0

    tag = sys.implementation.cache_tag
    for name in (
        "pkg/old.py",
        "pkg/new.py",
        f"pkg/__pycache__/old.{tag}.pyc",
        f"pkg/__pycache__/new.{tag}.pyc",
    ):
        assert oct((build.site / name).stat().st_mode & 0o7777) == "0o644", name


def test_the_notes_say_what_was_laid_over_and_of_what_version(build):
    assert build.run(version="0.4.12-piqnyx.1").returncode == 0

    assert (build.notes / "PIQNYX-OVERLAY.txt").read_bytes() == build.list.read_bytes()
    assert (build.notes / "PIQNYX-VERSION").read_text() == "0.4.12-piqnyx.1\n"
    for name in ("PIQNYX-OVERLAY.txt", "PIQNYX-VERSION"):
        assert oct((build.notes / name).stat().st_mode & 0o7777) == "0o644", name


def test_blank_lines_and_a_last_line_without_an_end_are_read_right(build):
    build.put(build.list, "\npkg/old.py\n\npkg/new.py")

    assert build.run().returncode == 0

    assert (build.site / "pkg/new.py").read_text() == "Z = 'ours, new'\n"


def test_a_file_of_the_list_that_the_checkout_lacks_stops_the_build(build):
    build.put(build.list, "pkg/old.py\npkg/gone.py\n")

    done = build.run()

    assert done.returncode != 0
    assert "pkg/gone.py" in done.stderr
    assert not (build.notes / "PIQNYX-VERSION").exists()


def test_a_file_that_does_not_compile_stops_the_build(build):
    build.put(build.source / "pkg/new.py", "def (:\n")

    done = build.run()

    assert done.returncode != 0
    assert "pkg/new.py" in done.stderr
    assert not (build.notes / "PIQNYX-VERSION").exists()


@pytest.mark.parametrize("text", ["", "\n\n"])
def test_an_empty_list_stops_the_build(build, text):
    build.put(build.list, text)

    done = build.run()

    assert done.returncode != 0
    assert "пуст" in done.stderr
    assert not (build.notes / "PIQNYX-VERSION").exists()


@pytest.mark.parametrize("name", ["../outside.py", "/etc/outside.py", "pkg/../../outside.py"])
def test_a_name_that_leads_out_of_the_packages_stops_the_build(build, name):
    build.put(build.source / "outside.py", "X = 1\n")
    build.put(build.list, f"pkg/old.py\n{name}\n")

    done = build.run()

    assert done.returncode != 0
    assert "вон" in done.stderr
    assert not (build.site.parent / "outside.py").exists()


def test_a_build_without_a_version_stops(build):
    done = build.run(version="")

    assert done.returncode != 0
    assert "верси" in done.stderr
    assert not (build.notes / "PIQNYX-VERSION").exists()
    assert (build.site / "pkg/old.py").read_text() == "X = 'as the image had it'\n"


def test_a_site_that_is_not_there_stops_the_build(build):
    build.site = build.site.parent / "no-such-site"

    done = build.run()

    assert done.returncode != 0
    assert "no-such-site" in done.stderr
    assert not build.site.exists()
