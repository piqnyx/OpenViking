#!/usr/bin/env python3
# piqnyx: in the compose file the image changes, and nothing else does.
# SPDX-License-Identifier: AGPL-3.0
"""Changes the image of a service in a compose file (PIQNYX.md, stage 2.6).

    edit_compose.py --file docker-compose.yml --service openviking \\
        --image piqnyx/openviking:0.4.12-piqnyx.1 --was-of ghcr.io/volcengine/openviking \\
        --out NEW.yml
    edit_compose.py --file docker-compose.yml --service openviking --data-folder

The line of the image is changed and `pull_policy: never` is put after it, so
that the image is taken from the disk only. The file is read line by line and
written back as it was, line ends and all: no line is touched but these two.
The given file is not written to; the new one goes to `--out`.

Ends with 0 when the file was changed, 3 when it is ours already, 1 when the
edit is refused. `--data-folder` prints what the service keeps its data in.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

POLICY = "never"
KEY = re.compile(r"^([ \t]*)([A-Za-z_][\w.-]*):(.*)$")


class Refused(Exception):
    """The file is not one this program may change."""


@dataclass
class Done:
    text: str
    changed: bool
    was: str


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _empty(line: str) -> bool:
    bare = line.strip()
    return not bare or bare.startswith("#")


def _end(line: str) -> str:
    return line[len(line.rstrip("\r\n")) :]


def _value(rest: str) -> Tuple[str, str]:
    """The value of a key and what follows it on the line: a note, spaces."""
    body = rest.strip()
    if body[:1] in ("'", '"'):
        close = body.find(body[0], 1)
        if close > 0:
            return body[1:close], body[close + 1 :]
    note = re.search(r"\s+#", body)
    if note:
        return body[: note.start()], body[note.start() :]
    return body, ""


def _service(lines: List[str], service: str) -> Tuple[int, int]:
    """Where the lines of the service begin and where they end."""
    top = [n for n, line in enumerate(lines) if line.rstrip() == "services:"]
    if not top:
        raise Refused("в файле нет раздела services")
    level = None
    begin = None
    for n in range(top[0] + 1, len(lines)):
        line = lines[n]
        if _empty(line):
            continue
        indent = _indent(line)
        if not indent:
            break
        if level is None:
            level = indent
        if indent == level:
            if begin is not None:
                return begin, n
            key = KEY.match(line.rstrip("\r\n"))
            if key and key.group(2) == service and _empty(key.group(3)):
                begin = n + 1
    else:
        n = len(lines)
    if begin is None:
        raise Refused(f"в файле нет сервиса {service}")
    return begin, n


def _keys(lines: List[str], begin: int, end: int) -> List[Tuple[int, str, str]]:
    """The keys of the service itself, not of what lies deeper: line, key, the rest."""
    level = None
    found = []
    for n in range(begin, end):
        if _empty(lines[n]):
            continue
        indent = _indent(lines[n])
        if level is None:
            level = indent
        if indent != level:
            continue
        key = KEY.match(lines[n].rstrip("\r\n"))
        if key:
            found.append((n, key.group(2), key.group(3)))
    return found


def edit(text: str, *, service: str, image: str, was_of: str) -> Done:
    lines = text.splitlines(keepends=True)
    begin, end = _service(lines, service)
    keys = _keys(lines, begin, end)
    images = [(n, rest) for n, key, rest in keys if key == "image"]
    if not images:
        raise Refused(f"у сервиса {service} нет строки image")
    if len(images) > 1:
        raise Refused(f"у сервиса {service} строка image дана дважды")
    at, rest = images[0]
    was, tail = _value(rest)
    if was != image and was != was_of and not was.startswith((was_of + ":", was_of + "@")):
        raise Refused(f"образ в файле не тот, что ждали: {was} (ждали {was_of} или {image})")

    indent = _indent(lines[at])
    policies = [(n, rest) for n, key, rest in keys if key == "pull_policy"]
    new = list(lines)
    new[at] = f"{indent}image: {image}{tail}{_end(lines[at]) or chr(10)}"
    if policies:
        where, rest = policies[0]
        tail = _value(rest)[1]
        new[where] = f"{_indent(lines[where])}pull_policy: {POLICY}{tail}{_end(lines[where])}"
    else:
        new.insert(at + 1, f"{indent}pull_policy: {POLICY}{_end(new[at])}")
    text_now = "".join(new)
    return Done(text=text_now, changed=text_now != text, was=was)


def only_the_image_changed(old: str, new: str, *, image: str) -> List[str]:
    """What else changed between two files; nothing, when the edit did what it says."""
    before, after = old.splitlines(), new.splitlines()
    gone: List[str] = []
    come: List[str] = []
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for what, a1, a2, b1, b2 in matcher.get_opcodes():
        if what != "equal":
            gone.extend(before[a1:a2])
            come.extend(after[b1:b2])

    problems = []
    if not come and not gone:
        problems.append("файл не изменился: образ в нём прежний")
    for line in gone:
        key = KEY.match(line)
        if not key or key.group(2) not in ("image", "pull_policy"):
            problems.append(f"пропала строка: {line.strip()}")
    for line in come:
        key = KEY.match(line)
        value = _value(key.group(3))[0] if key else ""
        if key and key.group(2) == "image" and value == image:
            continue
        if key and key.group(2) == "pull_policy" and value == POLICY:
            continue
        problems.append(f"появилась строка: {line.strip()}")
    for name in ("image", "pull_policy"):
        for lines, word in ((gone, "пропало"), (come, "появилось")):
            count = sum(1 for line in lines if KEY.match(line) and KEY.match(line).group(2) == name)
            if count > 1:
                problems.append(f"строк {name} {word} {count}, а меняется одна")
    return problems


def data_folder(text: str, *, service: str, inside: str = "/app/.openviking") -> Optional[str]:
    """What the service keeps its data in: the side of the host of the mount to `inside`."""
    lines = text.splitlines(keepends=True)
    begin, end = _service(lines, service)
    volumes = [n for n, key, _ in _keys(lines, begin, end) if key == "volumes"]
    if not volumes:
        return None
    level = _indent(lines[volumes[0]])
    for n in range(volumes[0] + 1, end):
        line = lines[n]
        if _empty(line):
            continue
        if len(_indent(line)) <= len(level):
            break
        item = line.strip()
        if not item.startswith("- "):
            continue
        parts = item[2:].strip().strip("'\"").split(":")
        if len(parts) >= 2 and parts[1] == inside:
            return parts[0]
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--file", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--image")
    parser.add_argument("--was-of")
    parser.add_argument("--out")
    parser.add_argument("--data-folder", action="store_true")
    args = parser.parse_args(argv)
    with open(args.file, encoding="utf-8", newline="") as source:
        text = source.read()

    try:
        if args.data_folder:
            folder = data_folder(text, service=args.service)
            if folder is None:
                print(f"ОСТАНОВКА: у сервиса {args.service} не найден каталог данных")
                return 1
            print(folder)
            return 0
        if not (args.image and args.was_of and args.out):
            parser.error("--image, --was-of and --out are needed")
        done = edit(text, service=args.service, image=args.image, was_of=args.was_of)
    except Refused as why:
        print(f"ОСТАНОВКА: {why}")
        return 1

    if not done.changed:
        print(f"в файле уже наш образ: {done.was}")
        return 3
    problems = only_the_image_changed(text, done.text, image=args.image)
    if problems:
        print("ОСТАНОВКА: правка задела лишнее")
        print("\n".join(f"  {line}" for line in problems))
        return 1
    with open(args.out, "w", encoding="utf-8", newline="") as out:
        out.write(done.text)
    before, after = text.splitlines(), done.text.splitlines()
    for line in difflib.unified_diff(before, after, lineterm="", n=0):
        if line[:1] in "+-" and line[:3] not in ("+++", "---"):
            print(f"{line[0]} {line[1:].strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
