#!/usr/bin/env python3
# piqnyx: the list of our files is the difference from the tag, no more and no less.
# SPDX-License-Identifier: AGPL-3.0
"""Holds the list of our files against the difference from the tag (PIQNYX.md, stage 2).

    git diff --name-status --no-renames TAG HEAD > changes.txt
    check_list.py --changes changes.txt --overlay piqnyx/overlay.txt

The build lays over the image the files of the list and nothing else. So the
list must name every file of the two packages that our branch changed or
added, and only those. A file we removed cannot be laid over, and neither can
a change of what the image is made of besides the two packages: either stops
the build, since the image would not be what the branch says.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Tuple

# What the image keeps in its site-packages, by the same names.
IN_THE_IMAGE = ("openviking/", "openviking_cli/")
# What lies beside the image: tests, this recipe, the plan.
BESIDE = ("tests/", "piqnyx/")
BESIDE_FILES = ("PIQNYX.md",)


def check(changes_text: str, overlay_text: str) -> Tuple[bool, List[str]]:
    problems: List[str] = []

    names: List[str] = []
    for line in overlay_text.split("\n"):
        if not line:
            continue
        if line != line.strip():
            problems.append(f"в имени лишние пробелы или возврат каретки: {line.strip()!r}")
        name = line.strip()
        if not name:
            continue
        if name in names:
            problems.append(f"имя дано в списке дважды: {name}")
            continue
        names.append(name)
        if not name.startswith(IN_THE_IMAGE):
            problems.append(f"имя в списке не из пакетов образа: {name}")
    if not names:
        problems.append("список наших файлов пуст: накладывать нечего, это не «всё в порядке»")

    changed, added = [], []
    for line in changes_text.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        if not path or not status or not status[0].isalpha():
            problems.append(f"строку разницы с тегом не прочитать: {line!r}")
            continue
        if path.startswith(IN_THE_IMAGE):
            if status == "M":
                changed.append(path)
            elif status == "A":
                added.append(path)
            else:
                problems.append(
                    f"файл убран или сменил вид в нашей ветке ({status}), "
                    f"наложением этого не сделать: {path}"
                )
                continue
            if path not in names:
                problems.append(f"файл изменён нами, а в списке его нет: {path}")
        elif not (path.startswith(BESIDE) or path in BESIDE_FILES):
            problems.append(f"изменено то, что рецепт в образ не кладёт: {path}")

    for name in names:
        if name.startswith(IN_THE_IMAGE) and name not in changed and name not in added:
            problems.append(f"файл в списке есть, а от тега он не отличается: {name}")

    lines = [f"в списке {len(names)}: изменено {len(changed)}, новых {len(added)}"]
    lines.extend(f"  {line}" for line in problems)
    lines.append(
        "ИТОГ: ОСТАНОВКА, список не равен разнице с тегом"
        if problems
        else "ИТОГ: список равен разнице с тегом"
    )
    return not problems, lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--changes", required=True)
    parser.add_argument("--overlay", required=True)
    args = parser.parse_args(argv)
    with open(args.changes, encoding="utf-8", newline="") as source:
        changes = source.read()
    with open(args.overlay, encoding="utf-8", newline="") as source:
        overlay = source.read()
    ok, lines = check(changes, overlay)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
