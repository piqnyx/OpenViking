#!/usr/bin/env python3
# piqnyx: what became of every test is written down as it happens.
# SPDX-License-Identifier: AGPL-3.0
"""Runs tests inside an image and writes down what became of each (PIQNYX.md, stage 2.5).

    python run_tests_inside.py [--tools DIR] [--each SECONDS] -- <what to run>

What pytest says goes to the first way out, as ever. What became of every
test goes to the second one, a record a line, each marked `@@piqnyx`. While a
test runs pytest keeps what it says to itself, and the records are written
between the tests: what a test says does not get among them. A record is out
the moment it is made, so a run that is cut short still says where. Each
begins a line of its own, whatever was left unfinished before it.

`--tools` names a folder with pytest and its helpers. It is looked into after
everything the image has, so nothing the image has is shadowed by it.
`--each` is the longest a single test may take; it needs pytest-timeout.

The options the fork gives to pytest are set aside (they ask for a coverage
tool), nothing is written beside the tests, and a file that cannot be read as
tests does not stop the others.
"""

import argparse
import json
import os
import sys
import time

MARK = "@@piqnyx "
# From the best to the worst: of the three parts of a test the worst is kept.
ORDER = ("passed", "skipped", "xfailed", "xpassed", "failed", "error")
LONGEST = 300
ALWAYS = ["-q", "-p", "no:cacheprovider", "-o", "addopts=", "--continue-on-collection-errors"]


def one_line(text):
    text = " ".join(str(text).split())
    return text[:LONGEST]


def why(report):
    """The reason of what became of a test, in one line."""
    told = getattr(report, "wasxfail", None)
    if told is not None:
        return one_line(told)
    repr_ = report.longrepr
    if repr_ is None:
        return ""
    crash = getattr(repr_, "reprcrash", None)
    if crash is not None and getattr(crash, "message", None):
        return one_line(crash.message.splitlines()[0])
    if isinstance(repr_, tuple) and len(repr_) == 3:
        return one_line(repr_[2])
    lines = [line for line in str(repr_).splitlines() if line.strip()]
    errors = [line[1:] for line in lines if line.startswith("E ")]
    return one_line((errors or lines or [""])[-1])


class Recorder:
    """A plugin of pytest: every test is written down when it begins and when it is over."""

    def __init__(self, way_out):
        self.way_out = way_out
        self.now = {}

    def write(self, **record):
        # One write, so nothing gets into the middle of a record.
        line = "\n" + MARK + json.dumps(record, ensure_ascii=False) + "\n"
        os.write(self.way_out, line.encode("utf-8", "replace"))

    def pytest_runtest_logstart(self, nodeid, location):
        self.now[nodeid] = ("passed", "")
        self.write(kind="started", id=nodeid)

    def pytest_runtest_logreport(self, report):
        outcome = report.outcome
        if hasattr(report, "wasxfail"):
            outcome = "xfailed" if report.skipped else "xpassed"
        elif outcome == "failed" and report.when != "call":
            outcome = "error"
        kept = self.now.get(report.nodeid, ("passed", ""))
        if ORDER.index(outcome) > ORDER.index(kept[0]):
            self.now[report.nodeid] = (outcome, why(report))

    def pytest_runtest_logfinish(self, nodeid, location):
        outcome, reason = self.now.pop(nodeid, ("error", "no report of the test"))
        self.write(kind="test", id=nodeid, outcome=outcome, why=reason)

    def pytest_collectreport(self, report):
        if report.failed:
            self.write(
                kind="test", id=report.nodeid or "(collection)", outcome="error", why=why(report)
            )


def main(argv):
    ours, what = argv, []
    if "--" in argv:
        at = argv.index("--")
        ours, what = argv[:at], argv[at + 1 :]
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tools")
    parser.add_argument("--each", type=float)
    args = parser.parse_args(ours)

    recorder = Recorder(2)
    home = os.environ.get("HOME")
    if home:
        os.makedirs(home, exist_ok=True)
    if args.tools:
        sys.path.append(args.tools)
    started = time.time()
    try:
        import pytest
    except ImportError as trouble:
        recorder.write(kind="end", status=3, seconds=0.0, why=one_line(trouble))
        return 3
    for_pytest = list(ALWAYS)
    if args.each is not None:
        for_pytest.append("--timeout=%s" % args.each)
    status = int(pytest.main(for_pytest + list(what), plugins=[recorder]))
    recorder.write(kind="end", status=status, seconds=round(time.time() - started, 1))
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
