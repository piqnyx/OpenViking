# piqnyx: the working memory summary through the real generator (PLAN-gorizont, 3ж).
# SPDX-License-Identifier: AGPL-3.0
"""The summary step on the seam the data really crosses.

The stand of 3б replaced ``_generate_archive_summary_async`` whole, so the generator's own
``except Exception`` was never on the table: on the server it swallowed the handle's verdict
and wrote the stub «N turns, M messages» as the working memory, and the cut by turns never
ran. Here the generator is real, the model behind the config is a stand: it reads the
messages back out of the rendered prompt and refuses the weight the way the handle does.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from openviking.message import Message, TextPart, ToolPart
from openviking.service.task_tracker import TaskStatus, TaskTracker, set_task_tracker
from openviking.session.session import WM_SEVEN_SECTIONS, Session
from openviking.utils.piqnyx_price import TooHeavyForTheDoor
from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton
from openviking_cli.utils.config.vlm_config import VLMConfig

URI = "viking://user/sessions/s1"
HISTORY = f"{URI}/history"


def archive(index: int) -> str:
    return f"{HISTORY}/archive_{index:03d}"


def user(i) -> Message:
    return Message(id=f"u{i}", role="user", parts=[TextPart(f"question {i}")])


def assistant(i) -> Message:
    return Message(id=f"a{i}", role="assistant", parts=[TextPart(f"answer {i}")])


def ids(messages):
    return [m.id for m in messages]


FOUR_TURNS = [
    user(1),
    assistant(1),
    user(2),
    assistant(2),
    user(3),
    assistant(3),
    user(4),
    assistant(4),
]


class _TaskStore:
    def __init__(self):
        self.tasks = {}

    async def create(self, task):
        self.tasks[task.task_id] = task

    async def update(self, task):
        self.tasks[task.task_id] = task

    async def get(self, task_id, *, account_id=None, user_id=None):
        return self.tasks.get(task_id)

    async def list(self, account_id, *, user_id=None):
        return list(self.tasks.values())

    async def delete(self, task_id, *, account_id, user_id=None):
        self.tasks.pop(task_id, None)


class _Locks:
    def __getattr__(self, name):
        async def anything(*args, **kwargs):
            return object()

        return anything


class _Storage:
    def __init__(self):
        self.files = {}
        self._async_agfs = _Locks()

    def _uri_to_path(self, uri, ctx=None):
        return uri

    async def read_file(self, uri, ctx=None):
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]

    async def write_file(self, uri, content, ctx=None, lease_ref=None):
        self.files[uri] = content

    async def exists(self, uri, ctx=None):
        return uri in self.files

    async def ls(self, uri, ctx=None):
        prefix = uri.rstrip("/") + "/"
        return sorted(
            {key[len(prefix) :].split("/")[0] for key in self.files if key.startswith(prefix)}
        )

    async def link(self, *args, **kwargs):
        return None

    def marker(self, archive_uri, name):
        raw = self.files.get(f"{archive_uri}/{name}")
        return json.loads(raw) if raw else None


def _write_config(path, workspace):
    path.write_text(
        json.dumps(
            {
                "storage": {"workspace": str(workspace)},
                "embedding": {
                    "dense": {
                        "provider": "openai",
                        "api_key": "none",
                        "api_base": "http://127.0.0.1:9/v1",
                        "model": "none",
                        "dimension": 1024,
                    }
                },
                "vlm": {
                    "provider": "openai",
                    "api_key": "none",
                    "api_base": "http://127.0.0.1:9/v1",
                    "model": "none",
                },
            }
        )
    )
    OpenVikingConfigSingleton.reset_instance()


@pytest.fixture(autouse=True)
def config_of_its_own(monkeypatch, tmp_path):
    config = tmp_path / "ov.conf"
    monkeypatch.setenv("OPENVIKING_CONFIG_FILE", str(config))
    _write_config(config, tmp_path / "data")
    yield
    OpenVikingConfigSingleton.reset_instance()


class _Compressor:
    """Extraction that always passes; the summary is what this file is about."""

    def __init__(self):
        self.calls = []

    async def extract_long_term_memories(self, messages, **kwargs):
        self.calls.append(ids(messages))
        return []


_LINE = re.compile(r"\[(user|assistant)\]: (?:question|answer) (\d+)")


class _Door:
    """The model behind the config, as the summary sees it: it reads the messages back
    out of the rendered prompt and refuses the weight the way the handle does. Asked
    with the update tool it answers with a tool call that updates one section, or,
    told ``tool_answer="none"``, with no tool call at all."""

    def __init__(self, *, limit=8, heavy=None, tool_answer="ops"):
        self.limit = limit
        self.heavy = heavy or (lambda found, prompt, tools: len(found) > self.limit)
        self.tool_answer = tool_answer
        self.calls = []
        self.prompts = []

    @staticmethod
    def found_in(prompt: str):
        return [f"{'u' if role == 'user' else 'a'}{n}" for role, n in _LINE.findall(prompt or "")]

    async def completion(self, prompt=None, tools=None, tool_choice=None, **kwargs):
        found = self.found_in(prompt)
        self.calls.append((found, bool(tools)))
        self.prompts.append(prompt or "")
        if self.heavy(found, prompt or "", bool(tools)):
            raise TooHeavyForTheDoor(len(prompt or "") * 10, self.limit * 1000, "handle")
        last = found[-1] if found else "?"
        if tools:
            if self.tool_answer == "none":
                return SimpleNamespace(
                    has_tool_calls=False, tool_calls=[], finish_reason="stop", usage={}
                )
            ops = {
                "sections": {"Current State": {"op": "UPDATE", "content": f"state after {last}"}}
            }
            return SimpleNamespace(
                has_tool_calls=True,
                tool_calls=[SimpleNamespace(arguments=json.dumps(ops))],
                finish_reason="tool_calls",
                usage={},
            )
        return f"WM after {last}"


V2_PRIOR = "\n".join(f"## {section}\n\n(nothing yet)\n" for section in WM_SEVEN_SECTIONS)


def completed_archive(stand, index, messages, overview):
    """An archive of an earlier commit, closed with its working memory written."""
    uri = stand.archive_with(index, messages)
    stand.storage.files[f"{uri}/.done"] = json.dumps(
        {
            "starting_message_id": messages[0].id,
            "ending_message_id": messages[-1].id,
            "working_memory_enabled": True,
            "coverage_start_archive": f"archive_{index:03d}",
            "coverage_end_archive": f"archive_{index:03d}",
            "covered_failed_archives": [],
            "completed_memory_steps": {
                "archive_summary": sorted(ids(messages)),
                "long_term": sorted(ids(messages)),
            },
        }
    )
    stand.storage.files[f"{uri}/.overview.md"] = overview
    stand.storage.files[f"{uri}/.summary.done"] = json.dumps({"written_at": 0})
    return uri


def fetched(i, output) -> Message:
    """An answer that carries a tool's output, as a scraped page comes."""
    return Message(
        id=f"a{i}",
        role="assistant",
        parts=[
            TextPart(f"answer {i}"),
            ToolPart(
                tool_id=f"t{i}", tool_name="web_fetch", tool_output=output, tool_status="completed"
            ),
        ],
    )


class _Stand:
    def __init__(self, monkeypatch, *, door: _Door, compressor=None):
        self.storage = _Storage()
        self.storage.files[f"{URI}/.meta.json"] = json.dumps({"session_id": "s1"})
        self.compressor = compressor or _Compressor()
        self.door = door
        self.session = Session(
            viking_fs=self.storage,
            session_id="s1",
            session_uri=URI,
            session_compressor=self.compressor,
        )

        async def completion(_config, prompt=None, tools=None, tool_choice=None, **kwargs):
            return await door.completion(prompt=prompt, tools=tools, tool_choice=tool_choice)

        monkeypatch.setattr(VLMConfig, "is_available", lambda _config: True)
        monkeypatch.setattr(VLMConfig, "get_completion_async", completion)
        real_sleep = asyncio.sleep

        async def no_time(seconds, *args, **kwargs):
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", no_time)
        self.tracker = TaskTracker(_TaskStore())
        self.account = self.session.ctx.account_id
        self.user = self.session.ctx.user.user_id
        self.stages = []
        real_update = self.tracker.update_stage

        async def remember_stage(task_id, stage, **kwargs):
            self.stages.append(stage)
            return await real_update(task_id, stage, **kwargs)

        self.tracker.update_stage = remember_stage

    def archive_with(self, index, messages):
        uri = archive(index)
        self.storage.files[f"{uri}/messages.jsonl"] = "".join(m.to_jsonl() + "\n" for m in messages)
        return uri

    async def run(self, index, messages, task_id):
        set_task_tracker(self.tracker)
        try:
            await self.tracker.create(
                "session_commit",
                resource_id="s1",
                task_id=task_id,
                account_id=self.account,
                user_id=self.user,
            )
            await self.session._run_memory_extraction(
                task_id=task_id,
                archive_uri=archive(index),
                messages=messages,
                usage_records=[],
                first_message_id=messages[0].id,
                last_message_id=messages[-1].id,
                memory_policy=None,
            )
        finally:
            set_task_tracker(None)

    async def task(self, task_id):
        return await self.tracker.get(task_id, account_id=self.account, user_id=self.user)

    def prompts_of(self):
        return list(self.door.prompts)


@pytest.mark.asyncio
async def test_a_summary_too_heavy_is_cut_by_turns_and_no_stub_is_written(monkeypatch):
    stand = _Stand(monkeypatch, door=_Door(limit=4))
    stand.archive_with(1, FOUR_TURNS)
    await stand.run(1, FOUR_TURNS, "t1")
    # The whole archive is refused, the halves pass, each on the previous part's memory.
    assert [found for found, _tools in stand.door.calls] == [
        ids(FOUR_TURNS),
        ["u1", "a1", "u2", "a2"],
        ["u3", "a3", "u4", "a4"],
    ]
    assert stand.storage.files[f"{archive(1)}/.overview.md"] == "WM after a4"
    assert f"{archive(1)}/.summary.done" in stand.storage.files
    done = stand.storage.marker(archive(1), ".done")
    assert done is not None, sorted(stand.storage.files)
    assert done["completed_memory_steps"]["archive_summary"] == sorted(ids(FOUR_TURNS))
    assert any(stage.startswith("cutting archive_summary") for stage in stand.stages), stand.stages
    assert (await stand.task("t1")).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_one_turn_too_heavy_goes_into_the_summary_with_its_tool_outputs_as_previews(
    monkeypatch,
):
    # A scraped page in one turn, heavier than the door takes on its own: the turn
    # cannot be cut, so the summary takes it with the tool's output cut to the preview
    # the archive keeps of it. Nothing is skipped, no stub is written.
    page = "x" * 30000
    turn = [user(1), fetched(1, page)]
    stand = _Stand(
        monkeypatch, door=_Door(heavy=lambda found, prompt, tools: ("x" * 5000) in prompt)
    )
    stand.archive_with(1, turn)
    await stand.run(1, turn, "t1")
    assert [found for found, _tools in stand.door.calls] == [["u1", "a1"], ["u1", "a1"]]
    whole, light = stand.prompts_of()
    assert ("x" * 5000) in whole and ("x" * 5000) not in light and len(light) < len(whole)
    assert "web_fetch" in light
    assert stand.storage.files[f"{archive(1)}/.overview.md"] == "WM after a1"
    done = stand.storage.marker(archive(1), ".done")
    assert done["completed_memory_steps"]["archive_summary"] == ["a1", "u1"]
    assert "skipped_turns" not in (stand.storage.marker(archive(1), ".meta.json") or {})
    assert not any(stage.startswith("skipping") for stage in stand.stages), stand.stages
    assert (await stand.task("t1")).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_one_turn_too_heavy_with_nothing_to_lighten_is_skipped_and_the_rest_summarized(
    monkeypatch,
):
    # The weight sits in the words themselves, not in a tool's output: there is no
    # lighter form, the turn is skipped and counted done, the turns around it are
    # summarized, and the meta says which turn and why.
    wall = Message(id="u2", role="user", parts=[TextPart("question 2 " + "y" * 30000)])
    turns = [
        user(1),
        assistant(1),
        wall,
        assistant(2),
        user(3),
        assistant(3),
        user(4),
        assistant(4),
    ]
    stand = _Stand(
        monkeypatch, door=_Door(heavy=lambda found, prompt, tools: ("y" * 5000) in prompt)
    )
    stand.archive_with(1, turns)
    await stand.run(1, turns, "t1")
    assert [found for found, _tools in stand.door.calls] == [
        ids(turns),
        ["u1", "a1", "u2", "a2"],
        ["u1", "a1"],
        ["u2", "a2"],
        ["u3", "a3", "u4", "a4"],
    ]
    assert stand.storage.files[f"{archive(1)}/.overview.md"] == "WM after a4"
    done = stand.storage.marker(archive(1), ".done")
    assert done["completed_memory_steps"]["archive_summary"] == sorted(ids(turns))
    meta = stand.storage.marker(archive(1), ".meta.json")
    assert [(record["step"], record["message_ids"]) for record in meta["skipped_turns"]] == [
        ("archive_summary", ["u2", "a2"])
    ]
    assert any(
        stage.startswith("skipping archive_summary: one turn alone (2 messages, first u2)")
        for stage in stand.stages
    ), stand.stages
    assert (await stand.task("t1")).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_summary_updating_a_prior_working_memory_is_cut_too_and_never_falls_back(
    monkeypatch,
):
    # With a working memory already standing the generator asks for an update with the
    # tool forced on. Refused by weight, it used to fall back to the creation prompt --
    # the same weight to the same door -- and then to the stub. Now the refusal goes up
    # and the halves are updated one after another, each on the memory the one before
    # left.
    stand = _Stand(monkeypatch, door=_Door(limit=4))
    completed_archive(stand, 1, FOUR_TURNS, V2_PRIOR)
    new = [
        user(5),
        assistant(5),
        user(6),
        assistant(6),
        user(7),
        assistant(7),
        user(8),
        assistant(8),
    ]
    stand.archive_with(2, new)
    await stand.run(2, new, "t2")
    assert stand.door.calls == [
        (ids(new), True),
        (["u5", "a5", "u6", "a6"], True),
        (["u7", "a7", "u8", "a8"], True),
    ]
    overview = stand.storage.files[f"{archive(2)}/.overview.md"]
    assert "## Current State" in overview and "state after a8" in overview, overview
    assert "turns," not in overview
    assert (await stand.task("t2")).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_the_creation_fallback_after_an_answer_without_a_tool_call_is_cut_too(monkeypatch):
    # The model answers the update request without the tool call: the generator asks
    # again with the creation prompt, as upstream does. Refused by weight there, the
    # refusal goes up instead of becoming the stub, and the halves follow the same road.
    stand = _Stand(
        monkeypatch,
        door=_Door(
            tool_answer="none", heavy=lambda found, prompt, tools: not tools and len(found) > 4
        ),
    )
    completed_archive(stand, 1, FOUR_TURNS, V2_PRIOR)
    new = [
        user(5),
        assistant(5),
        user(6),
        assistant(6),
        user(7),
        assistant(7),
        user(8),
        assistant(8),
    ]
    stand.archive_with(2, new)
    await stand.run(2, new, "t2")
    assert stand.door.calls == [
        (ids(new), True),
        (ids(new), False),
        (["u5", "a5", "u6", "a6"], True),
        (["u5", "a5", "u6", "a6"], False),
        (["u7", "a7", "u8", "a8"], False),
    ]
    assert stand.storage.files[f"{archive(2)}/.overview.md"] == "WM after a8"
    assert (await stand.task("t2")).status == TaskStatus.COMPLETED
