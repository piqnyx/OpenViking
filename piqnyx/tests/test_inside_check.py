# piqnyx: the server in the image takes our files, and takes them fresh.
# SPDX-License-Identifier: AGPL-3.0
"""The check that runs inside the image, on made-up packages.

Run from the root of the fork:  python3 -m pytest piqnyx/tests -q -p no:cacheprovider --no-cov
"""

import importlib.util
import os
import py_compile
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("inside_check", HERE.parent / "inside_check.py")
inside_check = importlib.util.module_from_spec(spec)
sys.modules["inside_check"] = inside_check
spec.loader.exec_module(inside_check)

BY_TIME = py_compile.PycInvalidationMode.TIMESTAMP
BY_HASH = py_compile.PycInvalidationMode.CHECKED_HASH
NEVER = py_compile.PycInvalidationMode.UNCHECKED_HASH


class Site:
    """A made-up site-packages that the checked Python finds first."""

    def __init__(self, root, monkeypatch):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("PYTHONPATH", str(self.root))
        monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)

    def put(self, name, text, *, compiled=BY_TIME):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if compiled is not None:
            py_compile.compile(str(path), doraise=True, invalidation_mode=compiled)
        return path

    def bytecode(self, name):
        return Path(importlib.util.cache_from_source(str(self.root / name)))

    def check(self, names, **how):
        return inside_check.check(str(self.root), list(names), **how)


@pytest.fixture
def site(tmp_path, monkeypatch):
    made = Site(tmp_path / "site", monkeypatch)
    made.put("pkg/__init__.py", "")
    return made


def told(lines, *words):
    return any(all(word in line for word in words) for line in lines)


def test_our_file_with_fresh_bytecode_is_in_order(site):
    site.put("pkg/mod.py", "X = 1\n")

    ok, lines = site.check(["pkg/mod.py"])

    assert ok, lines
    assert told(lines, "pkg/mod.py", "загружен", "байткод свежий")


def test_bytecode_left_from_another_source_is_refused(site):
    site.put("pkg/mod.py", "X = 1\n")
    (site.root / "pkg/mod.py").write_text("X = 2222\n")

    ok, lines = site.check(["pkg/mod.py"])

    assert not ok
    assert told(lines, "pkg/mod.py", "не от этого исходника")


def test_bytecode_checked_by_hash_is_in_order(site):
    site.put("pkg/mod.py", "X = 1\n", compiled=BY_HASH)

    ok, lines = site.check(["pkg/mod.py"])

    assert ok, lines


def test_bytecode_that_never_looks_at_the_source_must_still_be_of_it(site):
    site.put("pkg/mod.py", "X = 1\n", compiled=NEVER)
    assert site.check(["pkg/mod.py"])[0]

    (site.root / "pkg/mod.py").write_text("X = 2222\n")
    ok, lines = site.check(["pkg/mod.py"])

    assert not ok
    assert told(lines, "pkg/mod.py", "не от этого исходника")


def test_our_file_without_bytecode_is_refused(site):
    site.put("pkg/mod.py", "X = 1\n", compiled=None)

    ok, lines = site.check(["pkg/mod.py"])

    assert not ok
    assert told(lines, "pkg/mod.py", "нет байткода")


def test_bytecode_of_another_python_is_refused(site):
    site.put("pkg/mod.py", "X = 1\n")
    compiled = site.bytecode("pkg/mod.py")
    compiled.write_bytes(b"\x00\x00\r\n" + compiled.read_bytes()[4:])

    ok, lines = site.check(["pkg/mod.py"])

    assert not ok
    assert told(lines, "pkg/mod.py", "другого Python")


def test_our_file_that_does_not_load_is_refused(site):
    site.put("pkg/mod.py", "raise RuntimeError('broken on purpose')\n")

    ok, lines = site.check(["pkg/mod.py"])

    assert not ok
    assert told(lines, "pkg/mod.py", "RuntimeError", "broken on purpose")


def test_a_module_taken_from_another_place_is_refused(site, tmp_path, monkeypatch):
    site.put("pkg/mod.py", "X = 1\n")
    other = Site(tmp_path / "other", monkeypatch)
    other.put("pkg/__init__.py", "")
    other.put("pkg/mod.py", "X = 'not ours'\n")

    ok, lines = inside_check.check(str(site.root), ["pkg/mod.py"])

    assert not ok
    assert told(lines, "pkg/mod.py", "не наш файл", str(other.root))


def test_the_way_the_server_starts_is_walked_first(site):
    site.put("starter.py", "READY = True\n")
    site.put("pkg/late.py", "import sys\nassert 'starter' in sys.modules, 'too early'\n")

    early, lines = site.check(["pkg/late.py"])
    assert not early
    assert told(lines, "pkg/late.py", "too early")

    ok, lines = site.check(["pkg/late.py"], first=["starter"])
    assert ok, lines


def test_a_way_of_starting_that_fails_is_a_failure(site):
    site.put("pkg/mod.py", "X = 1\n")

    ok, lines = site.check(["pkg/mod.py"], first=["no_such_starter"])

    assert not ok
    assert told(lines, "no_such_starter")


def test_an_empty_list_is_a_failure_not_a_pass(site):
    ok, lines = site.check([])

    assert not ok
    assert told(lines, "пуст")


def test_our_file_that_is_not_python_has_only_to_be_readable(site):
    site.put("pkg/words.yaml", "a: 1\n", compiled=None)

    ok, lines = site.check(["pkg/words.yaml"])
    assert ok, lines
    assert told(lines, "pkg/words.yaml", "читается")

    ok, lines = site.check(["pkg/none.yaml"])
    assert not ok
    assert told(lines, "pkg/none.yaml", "не читается")


def test_our_file_that_cannot_be_read_is_refused(site):
    path = site.put("pkg/mod.py", "X = 1\n")
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    path.chmod(0)

    ok, lines = site.check(["pkg/mod.py"])

    path.chmod(0o644)
    assert not ok
    assert told(lines, "pkg/mod.py", "не читается")


def test_the_name_of_a_module_comes_from_the_name_of_its_file():
    assert inside_check.module_of("pkg/sub/mod.py") == "pkg.sub.mod"
    assert inside_check.module_of("pkg/__init__.py") == "pkg"
    assert inside_check.module_of("pkg/words.yaml") is None


def test_the_package_must_be_of_the_version_we_build_on(site):
    info = site.root / "fakepkg-1.2.3.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: fakepkg\nVersion: 1.2.3\n")
    site.put("pkg/mod.py", "X = 1\n")

    ok, lines = site.check(["pkg/mod.py"], package="fakepkg", package_version="1.2.3")
    assert ok, lines
    assert told(lines, "fakepkg", "1.2.3")

    ok, lines = site.check(["pkg/mod.py"], package="fakepkg", package_version="1.2.4")
    assert not ok
    assert told(lines, "fakepkg", "1.2.3", "1.2.4")


def test_the_list_is_the_one_the_build_left_in_the_image(site, tmp_path, capsys):
    site.put("pkg/mod.py", "X = 1\n")
    note = tmp_path / "PIQNYX-OVERLAY.txt"
    note.write_text("pkg/mod.py\n")

    code = inside_check.main([str(site.root), "--list", str(note)])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "ИТОГ" in out.splitlines()[-1]

    note.write_text("pkg/gone.py\n")
    assert inside_check.main([str(site.root), "--list", str(note)]) == 1

    assert inside_check.main([str(site.root), "--list", str(tmp_path / "no-note")]) == 1
