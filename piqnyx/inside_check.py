#!/usr/bin/env python3
# piqnyx: the server in the image takes our files, and takes them fresh.
# SPDX-License-Identifier: AGPL-3.0
"""Runs inside the new image, as the user the server runs as (PIQNYX.md, stage 2).

    python - SITE --first openviking.server.bootstrap \\
        --package openviking --package-version 0.4.12 < inside_check.py

The comparison of the images says that our files are in place. This says that
the server will run them: every file of the list the build left in the image
is readable by this user, its bytecode is of this very source and of this
Python, and the module loads from our file and from no other place.

The way the server starts is walked first (`--first`): the package cannot be
entered from any side, `import openviking.session` as the first import fails
in the untouched 0.4.12 too.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys

LIST_NOTE = "/app/PIQNYX-OVERLAY.txt"
LOAD_SECONDS = 300

# Runs in a Python of its own: what is loaded there is what the server would load.
LOADER = r"""
import importlib, json, sys
first, modules, package = json.loads(sys.argv[1])
found = {"modules": {}, "version": None}
for name in first:
    importlib.import_module(name)
for name in modules:
    try:
        module = importlib.import_module(name)
    except BaseException as trouble:
        found["modules"][name] = {"trouble": "%s: %s" % (type(trouble).__name__, trouble)}
        continue
    found["modules"][name] = {
        "file": getattr(module, "__file__", None),
        "cached": getattr(getattr(module, "__spec__", None), "cached", None),
    }
if package:
    from importlib import metadata
    try:
        found["version"] = metadata.version(package)
    except Exception as trouble:
        found["version_trouble"] = "%s: %s" % (type(trouble).__name__, trouble)
print("\n" + json.dumps(found))
"""


def module_of(name):
    """`pkg/sub/mod.py` is `pkg.sub.mod`; what is not Python has no module."""
    if not name.endswith(".py"):
        return None
    parts = name[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def bytecode_trouble(source, compiled):
    """None when the bytecode is of this source and of this Python, else what is wrong."""
    try:
        with open(compiled, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return "нет байткода"
    if len(head) < 16:
        return "байткод оборван"
    if head[:4] != importlib.util.MAGIC_NUMBER:
        return "байткод от другого Python"
    flags = int.from_bytes(head[4:8], "little")
    if flags == 0:
        about = os.stat(source)
        same_time = int.from_bytes(head[8:12], "little") == int(about.st_mtime) & 0xFFFFFFFF
        same_size = int.from_bytes(head[12:16], "little") == about.st_size & 0xFFFFFFFF
        if not (same_time and same_size):
            return "байткод не от этого исходника (время или размер не те)"
        return None
    if flags in (0b01, 0b11):
        with open(source, "rb") as handle:
            if head[8:16] != importlib.util.source_hash(handle.read()):
                return "байткод не от этого исходника (хеш не тот)"
        return None
    return "байткод с непонятными флагами %d" % flags


def _load(first, modules, package):
    """What a Python of its own loads: (found, None) or (None, what went wrong)."""
    try:
        done = subprocess.run(
            [sys.executable, "-B", "-c", LOADER, json.dumps([first, modules, package])],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=LOAD_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "загрузка не кончилась за %d с" % LOAD_SECONDS
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, said[-1] if said else "код выхода %d" % done.returncode
    try:
        return json.loads(done.stdout.decode("utf-8", "replace").strip().splitlines()[-1]), None
    except (IndexError, ValueError) as trouble:
        return None, "ответ загрузки не прочитать: %s" % trouble


def check(site, names, first=(), package=None, package_version=None):
    """(in order or not, the lines to show)."""
    site = site.rstrip("/")
    names = [name.strip() for name in names if name.strip()]
    lines = [
        "Python %s, пользователь %d:%d" % (sys.version.split()[0], os.geteuid(), os.getegid()),
    ]
    if not names:
        return False, lines + ["список наших файлов пуст: проверять нечего, это не «всё в порядке»"]

    troubles = {}
    for name in names:
        source = "%s/%s" % (site, name)
        try:
            with open(source, "rb") as handle:
                handle.read(1)
        except OSError as trouble:
            troubles[name] = "не читается этим пользователем (%s)" % type(trouble).__name__
            continue
        if module_of(name) is None:
            continue
        wrong = bytecode_trouble(source, importlib.util.cache_from_source(source))
        if wrong:
            troubles[name] = wrong

    modules = {module_of(name): name for name in names if module_of(name)}
    found, wrong = _load(list(first), sorted(modules), package)
    if found is None:
        lines.append("путь, которым стартует сервер, не пройден: %s" % wrong)
        lines.append("ИТОГ: ОСТАНОВКА, сервер в образе не поднимется")
        return False, lines

    in_order = True
    if package:
        version = found.get("version")
        if version is None:
            in_order = False
            lines.append("версию пакета %s не узнать: %s" % (package, found.get("version_trouble")))
        elif package_version and version != package_version:
            in_order = False
            lines.append("пакет %s версии %s, а строим на %s" % (package, version, package_version))
        else:
            lines.append("пакет %s версии %s" % (package, version))

    for name in names:
        module = module_of(name)
        if name not in troubles and module is not None:
            loaded = found["modules"].get(module) or {"trouble": "о модуле нет ответа"}
            source = "%s/%s" % (site, name)
            if "trouble" in loaded:
                troubles[name] = "не загрузился: %s" % loaded["trouble"]
            elif os.path.realpath(loaded.get("file") or "") != os.path.realpath(source):
                troubles[name] = "загружен не наш файл, а %s" % loaded.get("file")
        if name in troubles:
            in_order = False
            lines.append("наш файл: %s -- %s" % (name, troubles[name]))
        elif module is None:
            lines.append("наш файл: %s -- читается" % name)
        else:
            lines.append("наш файл: %s -- загружен, байткод свежий" % name)

    lines.append(
        "ИТОГ: сервер в образе берёт наши файлы"
        if in_order
        else "ИТОГ: ОСТАНОВКА, сервер в образе возьмёт не то, что мы положили"
    )
    return in_order, lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("site")
    parser.add_argument("--list", default=LIST_NOTE)
    parser.add_argument("--first", action="append", default=[])
    parser.add_argument("--package")
    parser.add_argument("--package-version")
    args = parser.parse_args(argv)
    try:
        with open(args.list, encoding="utf-8") as handle:
            names = handle.read().splitlines()
    except OSError as trouble:
        print("список наших файлов не прочитать: %s (%s)" % (args.list, type(trouble).__name__))
        print("ИТОГ: ОСТАНОВКА, в образе нет списка наложенного")
        return 1
    in_order, lines = check(
        args.site,
        names,
        first=args.first,
        package=args.package,
        package_version=args.package_version,
    )
    print("\n".join(lines))
    return 0 if in_order else 1


if __name__ == "__main__":
    sys.exit(main())
