#!/usr/bin/env python3
# piqnyx: the image is the running one with our files laid over it, and nothing else.
# SPDX-License-Identifier: AGPL-3.0
"""Compares two images file by file (PIQNYX.md, stage 2).

The new image is the old one with the files of `piqnyx/overlay.txt` laid over
it. That is a claim, and this program checks it against the listings of both
images: every file, its hash, its mode and its owner. What may differ is our
files, their bytecode and the two notes the build leaves in /app. Anything
else -- a third file changed, one gone, one nobody asked for, a mode altered --
stops the work.

    compare_images.py --old-files A.sha --new-files B.sha \\
        --old-entries A.ent --new-entries B.ent \\
        --overlay piqnyx/overlay.txt --version VERSION.txt \\
        --site /app/.venv/lib/python3.13/site-packages --cache-tag cpython-313 \\
        --source-root . --tag-root TAG --old-config A.json --new-config B.json

`TAG` holds the two packages as the tag has them (`git archive`): laying files
over an image is sound only while that image is the tag we made our change to.
`VERSION.txt` holds the version of the build and a line end, as the build
writes it. `*.json` is the `Config` of an image as `docker image inspect`
gives it.

`*.sha` is what `sha256sum` prints; `*.ent` is type, mode, owner, path and link
target, tab-separated, one entry a line (`find -printf '%y\\t%m\\t%U:%G\\t%p\\t%l\\n'`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Docker writes these anew for every container; they are not the image's.
RUNTIME = frozenset({"/etc/hostname", "/etc/hosts", "/etc/resolv.conf"})
LIST_NOTE = "/app/PIQNYX-OVERLAY.txt"
VERSION_NOTE = "/app/PIQNYX-VERSION"
OUR_LABELS = "org.piqnyx."
SHOWN = 60


@dataclass
class Report:
    ok: bool
    text: str
    unexpected: List[Tuple[str, str]] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


def _files(text: str) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, _, path = line.partition(" ")
        path = path.lstrip(" ").lstrip("*")
        if digest and path:
            found[path] = digest
    return found


def _entries(text: str) -> Dict[str, Tuple[str, str, str, str]]:
    found: Dict[str, Tuple[str, str, str, str]] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        kind, mode, owner, path = parts[:4]
        link = parts[4] if len(parts) > 4 else ""
        found[path] = (kind, mode, owner, link)
    return found


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _count(number: int, one: str, few: str, many: str) -> str:
    tail = number % 100
    if 11 <= tail <= 14:
        word = many
    elif number % 10 == 1:
        word = one
    elif 2 <= number % 10 <= 4:
        word = few
    else:
        word = many
    return f"{number} {word}"


def _against_the_tag(
    tag_root: str, site: str, old: Dict[str, str], overlay: List[str], problems: List[str]
) -> Tuple[int, int, int]:
    """The running image against the tag: how many files match, were looked at, were never put in."""
    of_the_tag: Dict[str, str] = {}
    for folder, _, names in os.walk(tag_root):
        for name in names:
            path = os.path.join(folder, name)
            try:
                with open(path, "rb") as source:
                    digest = _sha256(source.read())
            except OSError as trouble:
                problems.append(f"не прочитать файл тега: {path} ({trouble})")
                continue
            of_the_tag[os.path.relpath(path, tag_root).replace(os.sep, "/")] = digest

    matched = looked_at = absent = 0
    for name, digest in sorted(of_the_tag.items()):
        path = f"{site}/{name}"
        if path not in old:
            absent += 1
            if name in overlay:
                problems.append(f"файла, который мы меняем, нет в исходном образе: {path}")
            continue
        looked_at += 1
        if old[path] == digest:
            matched += 1
        elif name in overlay:
            problems.append(f"исходный образ расходится с тегом в файле, который мы меняем: {path}")
        else:
            problems.append(f"исходный образ расходится с тегом: {path}")
    for name in overlay:
        if name not in of_the_tag and f"{site}/{name}" in old:
            problems.append(
                f"файл значится новым (в теге его нет), а в исходном образе он есть: {site}/{name}"
            )
    if not matched:
        problems.append("ни один файл тега не найден в исходном образе: сверять не с чем")
    return matched, looked_at, absent


def _labels(before: object, after: object, problems: List[str]) -> Tuple[int, int]:
    """The labels of the running image stay; what is added bears our name."""
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    added = 0
    for key in sorted(set(before) | set(after)):
        if key in before and before[key] != after.get(key):
            problems.append(
                f"метка исходного образа изменилась: {key}: было {before[key]!r}, "
                f"стало {after.get(key)!r}"
            )
        elif key not in before:
            added += 1
            if not key.startswith(OUR_LABELS):
                problems.append(f"лишняя метка, не наша: {key}")
    return len(before), added


def compare(
    *,
    old_files: str,
    new_files: str,
    old_entries: str,
    new_entries: str,
    overlay: List[str],
    site: str,
    cache_tag: str,
    source_root: str,
    overlay_text: Optional[str] = None,
    version_text: Optional[str] = None,
    old_config: Optional[str] = None,
    new_config: Optional[str] = None,
    tag_root: Optional[str] = None,
) -> Report:
    old, new = _files(old_files), _files(new_files)
    old_about, new_about = _entries(old_entries), _entries(new_entries)
    problems: List[str] = []
    unexpected: Dict[str, str] = {}

    if not old or not new:
        problems.append("листинг файлов пуст: сравнивать нечего, это не «всё в порядке»")
    if not old_about or not new_about:
        problems.append("листинг прав пуст: сравнивать нечего, это не «всё в порядке»")

    overlay = [name.strip() for name in overlay if name.strip()]
    site = site.rstrip("/")
    ours = {f"{site}/{name}": name for name in overlay}
    bytecode = []
    for name in overlay:
        if not name.endswith(".py"):
            continue
        folder, base = os.path.split(f"{site}/{name}")
        bytecode.append(f"{folder}/__pycache__/{base[:-3]}.")

    def is_ours(path: str) -> bool:
        if path in ours or path in (LIST_NOTE, VERSION_NOTE):
            return True
        return path.endswith(".pyc") and any(path.startswith(prefix) for prefix in bytecode)

    for path in sorted(set(old) | set(new)):
        if path in RUNTIME or is_ours(path):
            continue
        if path not in new:
            unexpected[path] = "пропал"
        elif path not in old:
            unexpected[path] = "появился"
        elif old[path] != new[path]:
            unexpected[path] = "изменён"
    for path in sorted(set(old_about) | set(new_about)):
        if path in RUNTIME or path in unexpected:
            continue
        if is_ours(path):
            # What we replace keeps the mode and the owner it had.
            if path in old_about and path in new_about and old_about[path] != new_about[path]:
                unexpected[path] = "права или владелец"
            continue
        if path not in new_about:
            unexpected[path] = "пропал"
        elif path not in old_about:
            unexpected[path] = "появился"
        elif old_about[path] != new_about[path]:
            unexpected[path] = "права или владелец"

    for path, name in sorted(ours.items()):
        if path not in new:
            problems.append(f"нашего файла нет в образе: {path}")
            continue
        try:
            with open(os.path.join(source_root, name), "rb") as source:
                wanted = _sha256(source.read())
        except OSError as trouble:
            problems.append(f"не прочитать наш файл в исходниках: {name} ({trouble})")
            continue
        if new[path] != wanted:
            problems.append(f"в образе не тот файл, что в исходниках: {path}")
        about = new_about.get(path)
        if about is not None:
            try:
                readable = int(about[1], 8) & 0o004
            except ValueError:
                readable = 0
            if not readable:
                problems.append(
                    f"наш файл не читается пользователем сервера (права {about[1]}): {path}"
                )
        if name.endswith(".py"):
            folder, base = os.path.split(path)
            compiled = f"{folder}/__pycache__/{base[:-3]}.{cache_tag}.pyc"
            if compiled not in new:
                problems.append(f"нет свежего байткода нашего файла: {compiled}")

    wanted_list = overlay_text if overlay_text is not None else "\n".join(overlay) + "\n"
    if new.get(LIST_NOTE) != _sha256(wanted_list.encode("utf-8")):
        problems.append(f"список наложенного в образе не совпадает с данным: {LIST_NOTE}")
    if version_text is not None and new.get(VERSION_NOTE) != _sha256(version_text.encode("utf-8")):
        problems.append(f"версия в образе не совпадает с данной: {VERSION_NOTE}")

    of_the_tag = None
    if tag_root is not None:
        of_the_tag = _against_the_tag(tag_root, site, old, overlay, problems)

    settings = 0
    labels = None
    if old_config is not None or new_config is not None:
        try:
            before, after = json.loads(old_config or ""), json.loads(new_config or "")
            if not isinstance(before, dict) or not isinstance(after, dict):
                raise ValueError("не словарь")
        except ValueError as trouble:
            problems.append(f"настройки образа не прочитать: {trouble}")
        else:
            # The id names the image itself; the labels have a rule of their own.
            for key in sorted((set(before) | set(after)) - {"Labels", "Image"}):
                settings += 1
                if before.get(key) != after.get(key):
                    problems.append(
                        f"настройка образа изменилась: {key}: было {before.get(key)!r}, "
                        f"стало {after.get(key)!r}"
                    )
            labels = _labels(before.get("Labels"), after.get("Labels"), problems)

    lines = [
        f"файлов в старом образе {len(old)}, в новом {len(new)}",
        f"настроек запуска сверено: {settings}",
    ]
    if labels is not None:
        lines.append(f"меток исходного образа {labels[0]}, наших добавлено {labels[1]}")
    if of_the_tag is not None:
        lines.append(
            f"исходный образ сверен с тегом: совпало {of_the_tag[0]} из {of_the_tag[1]}, "
            f"в образ не ставились: {of_the_tag[2]}"
        )
    lines += [
        f"наложено: {_count(len(ours), 'наш файл', 'наших файла', 'наших файлов')}",
    ]
    for path in sorted(ours):
        state = "изменён" if path in old else "новый"
        lines.append(f"  {state}: {path}")
    if unexpected:
        lines.append(f"ЛИШНЕЕ, чего быть не должно: {len(unexpected)}")
        lines.extend(f"  {reason}: {path}" for path, reason in sorted(unexpected.items())[:SHOWN])
    if problems:
        lines.append(f"НЕ ТАК: {len(problems)}")
        lines.extend(f"  {line}" for line in problems[:SHOWN])
    ok = not unexpected and not problems
    lines.append(
        "ИТОГ: образ -- прежний плюс наши файлы, больше ничего"
        if ok
        else "ИТОГ: ОСТАНОВКА, образ не тот, что заявлен"
    )
    return Report(
        ok=ok,
        text="\n".join(lines),
        unexpected=[(reason, path) for path, reason in sorted(unexpected.items())],
        problems=problems,
    )


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as source:
        return source.read()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for name in ("old-files", "new-files", "old-entries", "new-entries", "overlay", "site"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--cache-tag", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--version")
    parser.add_argument("--old-config")
    parser.add_argument("--new-config")
    parser.add_argument("--tag-root")
    args = parser.parse_args(argv)
    overlay_text = _read(args.overlay)
    report = compare(
        old_files=_read(args.old_files),
        new_files=_read(args.new_files),
        old_entries=_read(args.old_entries),
        new_entries=_read(args.new_entries),
        overlay=overlay_text.splitlines(),
        site=args.site,
        cache_tag=args.cache_tag,
        source_root=args.source_root,
        overlay_text=overlay_text,
        version_text=_read(args.version) if args.version else None,
        old_config=_read(args.old_config) if args.old_config else None,
        new_config=_read(args.new_config) if args.new_config else None,
        tag_root=args.tag_root,
    )
    print(report.text)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
