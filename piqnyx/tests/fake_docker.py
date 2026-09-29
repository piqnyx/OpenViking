#!/usr/bin/env python3
# piqnyx: a stand-in for docker, for the tests of the scripts.
# SPDX-License-Identifier: AGPL-3.0
"""Answers the few calls our scripts make, from a scenario the test wrote.

It checks how the scripts reason, not how docker behaves: what a real docker
says to these calls is seen on the server only.

`FAKE_DOCKER` names a folder with `scenario.json` and the listings the images
are to give; every call is added to `calls.jsonl` there.
"""

import hashlib
import json
import os
import sys
import time


def main(argv):
    home = os.environ["FAKE_DOCKER"]
    with open(os.path.join(home, "scenario.json"), encoding="utf-8") as source:
        scenario = json.load(source)
    images = scenario.get("images", {})
    call = {"argv": argv}

    def done(code=0, out="", err=""):
        call["code"] = code
        with open(os.path.join(home, "calls.jsonl"), "a", encoding="utf-8") as log:
            log.write(json.dumps(call) + "\n")
        sys.stdout.write(out)
        sys.stderr.write(err)
        return code

    if argv[:2] == ["buildx", "version"]:
        if scenario.get("buildkit", True):
            return done(0, "github.com/docker/buildx v0.0.0-fake\n")
        return done(1, err="docker: 'buildx' is not a docker command.\n")

    if argv[:2] == ["image", "inspect"]:
        name = argv[-1]
        if name not in images:
            return done(1, "[]\n", f"Error response from daemon: No such image: {name}\n")
        if "--format" in argv:
            wanted = argv[argv.index("--format") + 1]
            if ".Config" in wanted:
                return done(0, json.dumps(images[name].get("config", {})) + "\n")
            if ".Id" in wanted:
                return done(0, images[name].get("id", "sha256:fake") + "\n")
        return done(0, json.dumps([{"Id": images[name].get("id", "sha256:fake")}]) + "\n")

    if argv[:1] == ["ps"]:
        asked = [item for item in argv if item.startswith("ancestor=")]
        name = asked[0].split("=", 1)[1] if asked else ""
        if name not in images:
            # What a real docker says of an image it does not have is not known here.
            return done(
                1, err=f"fake docker: asked for the containers of an unknown image {name}\n"
            )
        return done(0, "".join(f"{one}\n" for one in scenario.get("containers", {}).get(name, [])))

    if argv[:1] == ["build"]:
        return done(scenario.get("build_code", 0), "fake build\n")

    if argv[:1] == ["rm"]:
        return done(0)

    if argv[:1] == ["run"]:
        at = next((n for n, item in enumerate(argv) if n and item in images), None)
        if at is None:
            return done(125, err="Unable to find image locally\n")
        image, command = images[argv[at]], argv[at + 1 :]
        call["image"] = argv[at]
        said = " ".join(command)
        if "sha256sum" in said or "-printf" in said:
            kind = "sha" if "sha256sum" in said else "ent"
            code = scenario.get("listing_codes", {}).get(kind, 0)
            with open(os.path.join(home, f"{image['side']}.{kind}"), encoding="utf-8") as source:
                return done(code, source.read())
        if "cache_tag" in said:
            return done(0, scenario["cache_tag"] + "\n")
        if "purelib" in said:
            return done(0, scenario["site"] + "\n")
        if "pip" in command and "install" in command:
            code = scenario.get("tools_code", 0)
            into = [item.split(":")[0] for item in argv if item.endswith(":/tools")]
            if code == 0 and into:
                os.makedirs(os.path.join(into[0], "pytest"), exist_ok=True)
                with open(os.path.join(into[0], "pytest", "__init__.py"), "w") as made:
                    made.write("# put here by the stand-in for docker\n")
            return done(code, err="" if code == 0 else "ERROR: no network in the stand-in\n")
        if any(item.endswith("run_tests_inside.py") for item in command):
            side = command[command.index("--side") + 1]
            call["side"] = side
            records = os.path.join(home, f"{side}.rec")
            told = ""
            if os.path.exists(records):
                with open(records, encoding="utf-8") as source:
                    told = source.read()
            # A run that takes its time writes its records down as it goes.
            lasts = scenario.get("tests_last", 0)
            if lasts:
                half = len(told) // 2
                cut = told.rfind("\n", 0, half) + 1
                sys.stderr.write(told[:cut])
                sys.stderr.flush()
                time.sleep(lasts)
                told = told[cut:]
            return done(scenario.get("tests_code", {}).get(side, 1), "fake talk of pytest\n", told)
        if command[:1] == ["-"]:
            given = sys.stdin.buffer.read()
            call["stdin_sha256"] = hashlib.sha256(given).hexdigest()
            lines = scenario.get("inside_lines", ["ИТОГ: сервер в образе берёт наши файлы"])
            return done(scenario.get("inside_code", 0), "".join(f"{one}\n" for one in lines))
        return done(127, err=f"fake docker: no answer for the command {command}\n")

    return done(64, err=f"fake docker: no answer for {argv}\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
