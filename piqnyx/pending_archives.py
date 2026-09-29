#!/usr/bin/env python3
# piqnyx: the server is not stopped while an archive of a session is in work.
# SPDX-License-Identifier: AGPL-3.0
"""Looks whether an archive of a session is in work (PIQNYX.md, stage 2.6).

    pending_archives.py DATA

`DATA` is the folder the server keeps its data in. A session is kept there
with its archives, `.../sessions/<session>/history/archive_NNN`. An archive
that is closed has the mark `.done`, one that failed has `.failed.json`; one
that has neither is in work. The server of upstream marks an archive in work
as failed when it is stopped, so it is not to be stopped while there is one.

Only the names of folders and marks are read. What is said in the archives is
neither read nor shown.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

ARCHIVE = re.compile(r"^archive_\d+$")
SHOWN = 20


@dataclass
class Found:
    ok: bool = False
    sessions: int = 0
    archives: int = 0
    done: int = 0
    failed: int = 0
    pending: List[Tuple[str, str]] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)


def look(data: str) -> Found:
    found = Found()
    if not os.path.isdir(data):
        found.lines.append(f"нет каталога данных: {data}")
        found.lines.append("ИТОГ: ОСТАНОВКА, архивы не посмотреть")
        return found

    unread: List[str] = []

    def could_not(trouble: OSError) -> None:
        unread.append(f"{trouble.filename} ({type(trouble).__name__})")

    for folder, folders, _ in os.walk(data, onerror=could_not):
        if os.path.basename(folder) != "history":
            continue
        archives = sorted(name for name in folders if ARCHIVE.match(name))
        # The archives are not gone into: what is said there is not ours to read.
        folders[:] = []
        if not archives:
            continue
        found.sessions += 1
        session = os.path.basename(os.path.dirname(folder))
        for name in archives:
            found.archives += 1
            try:
                marks = set(os.listdir(os.path.join(folder, name)))
            except OSError as trouble:
                could_not(trouble)
                continue
            if ".done" in marks:
                found.done += 1
            elif ".failed.json" in marks:
                found.failed += 1
            else:
                found.pending.append((session, name))
    found.pending.sort()

    found.lines.append(
        f"сессий {found.sessions}, архивов {found.archives}: закрыто {found.done}, "
        f"сорвано {found.failed}, в работе {len(found.pending)}"
    )
    for session, name in found.pending[:SHOWN]:
        found.lines.append(f"  в работе: сессия {session}, {name}")
    for name in unread[:SHOWN]:
        found.lines.append(f"  не прочитать: {name}")
    if not found.archives and not unread:
        found.lines.append("  архивов не найдено ни одного: либо их нет, либо они лежат не там")

    found.ok = bool(found.archives) and not found.pending and not unread
    found.lines.append(
        "ИТОГ: архивов в работе нет, сервер можно останавливать"
        if found.ok
        else "ИТОГ: ОСТАНОВКА, сервер сейчас останавливать нельзя"
    )
    return found


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("data")
    args = parser.parse_args(argv)
    found = look(args.data)
    print("\n".join(found.lines))
    return 0 if found.ok else 1


if __name__ == "__main__":
    sys.exit(main())
