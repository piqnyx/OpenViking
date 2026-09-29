#!/usr/bin/env python3
# piqnyx: the same tests give the same in both images, and ours pass in the new one.
# SPDX-License-Identifier: AGPL-3.0
"""Compares two runs of tests, one in the old image and one in the new (PIQNYX.md, stage 2.5).

    compare_runs.py --old OLD.txt --new NEW.txt --ours OURS.txt

`OLD.txt` and `NEW.txt` hold what `run_tests_inside.py` wrote down, other talk
mixed in or not. `OURS.txt` names the files of the tests our branch added, one
a line: they are run in the new image only and must all pass there.

Every other test must be there in both runs and give the same in both. It
need not pass: the tests of upstream that fail in the image as it runs now
fail for reasons of their own. What matters is that our change moved nothing.
A difference is not judged here, it is shown, and it stops the work until
somebody has looked at it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MARK = "@@piqnyx "
ORDER = ("passed", "skipped", "xfailed", "xpassed", "failed", "error")
SHOWN = 40


@dataclass
class Run:
    tests: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    unfinished: List[str] = field(default_factory=list)
    end: Optional[dict] = None
    noise: int = 0


def read(text: str) -> Run:
    """What a run wrote down; lines that are not records are left out."""
    run = Run()
    begun: List[str] = []
    for line in text.splitlines():
        if not line.startswith(MARK):
            continue
        try:
            record = json.loads(line[len(MARK) :])
        except ValueError:
            run.noise += 1
            continue
        kind = record.get("kind") if isinstance(record, dict) else None
        name = record.get("id") if isinstance(record, dict) else None
        if kind == "started" and isinstance(name, str):
            begun.append(name)
        elif kind == "test" and isinstance(name, str) and record.get("outcome") in ORDER:
            outcome, reason = record["outcome"], str(record.get("why") or "")
            kept = run.tests.get(name)
            if kept is None or ORDER.index(outcome) > ORDER.index(kept[0]):
                run.tests[name] = (outcome, reason)
        elif kind == "end":
            run.end = record
        else:
            run.noise += 1
    for name in begun:
        if name not in run.tests and name not in run.unfinished:
            run.unfinished.append(name)
    return run


def _told_of(title: str, run: Run, problems: List[str]) -> str:
    count = dict.fromkeys(ORDER, 0)
    for outcome, _ in run.tests.values():
        count[outcome] += 1
    line = (
        f"{title}: тестов {len(run.tests)}: прошло {count['passed']}, упало {count['failed']}, "
        f"ошибок {count['error']}, пропущено {count['skipped']}"
    )
    if count["xfailed"] or count["xpassed"]:
        line += f", упало как ждали {count['xfailed']}, прошло вопреки {count['xpassed']}"
    if run.end is not None and "seconds" in run.end:
        line += f", за {run.end['seconds']} с"

    if run.end is None and not run.tests:
        problems.append(f"{title}: прогон ничего не записал")
    elif run.end is None:
        where = f" на тесте {run.unfinished[-1]}" if run.unfinished else ""
        problems.append(f"{title}: прогон оборван{where}")
    elif run.end.get("status") not in (0, 1):
        problems.append(
            f"{title}: pytest кончил кодом {run.end.get('status')}, прогон не состоялся как положено"
        )
    return line


def compare(old_text: str, new_text: str, ours: List[str]) -> Tuple[bool, List[str]]:
    old, new = read(old_text), read(new_text)
    problems: List[str] = []
    lines = [
        _told_of("старый образ", old, problems),
        _told_of("новый образ", new, problems),
    ]

    ours = [name.strip() for name in ours if name.strip()]

    def is_ours(test: str) -> bool:
        return test.split("::", 1)[0] in ours

    mine = {test: what for test, what in new.tests.items() if is_ours(test)}
    passed = sum(1 for outcome, _ in mine.values() if outcome == "passed")
    lines.append(f"наших тестов в новом образе {len(mine)}: прошли {passed}")
    if not ours:
        problems.append("файлы наших тестов не названы: проверять нечего, это не «всё в порядке»")
    elif not mine:
        problems.append("наших тестов в новом образе ни одного: проверять нечего")
    for test, (outcome, reason) in sorted(mine.items()):
        if outcome != "passed":
            problems.append(f"наш тест не прошёл ({outcome}): {test} -- {reason}")
    stray = sorted({test.split("::", 1)[0] for test in old.tests if is_ours(test)})
    for name in stray:
        problems.append(f"в старом образе гонялся наш тест, его там быть не должно: {name}")

    before = {test: what for test, what in old.tests.items() if not is_ours(test)}
    after = {test: what for test, what in new.tests.items() if not is_ours(test)}
    both = sorted(set(before) & set(after))
    same = [test for test in both if before[test][0] == after[test][0]]
    lines.append(f"общих тестов {len(both)}: исход одинаков у {len(same)}")
    for test in sorted(set(before) - set(after)):
        problems.append(f"тест есть только в старом образе: {test}")
    for test in sorted(set(after) - set(before)):
        problems.append(f"тест есть только в новом образе: {test}")

    moved = [test for test in both if before[test][0] != after[test][0]]
    if moved:
        lines.append(f"РАЗЛИЧИЯ: {len(moved)}")
        for test in moved[:SHOWN]:
            reason = after[test][1] or before[test][1]
            lines.append(f"  было {before[test][0]}, стало {after[test][0]}: {test} -- {reason}")
    if problems:
        lines.append(f"НЕ ТАК: {len(problems)}")
        lines.extend(f"  {line}" for line in problems[:SHOWN])

    ok = not moved and not problems
    lines.append(
        "ИТОГ: тесты исходника идут в обоих образах одинаково, наши в новом проходят"
        if ok
        else "ИТОГ: ОСТАНОВКА, прогоны надо разобрать"
    )
    return ok, lines


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as source:
        return source.read()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--ours", required=True)
    args = parser.parse_args(argv)
    ok, lines = compare(_read(args.old), _read(args.new), _read(args.ours).splitlines())
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
