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
    with a tool forced on it answers with the tool call -- the working memory with the
    checkpoint notes the prompt marks, or one section updated -- unless told to be
    flaky (no tool call, so many times) or to answer without the tool at all. Told
    what to refuse, it refuses the content the way a moderation answer does."""

    def __init__(self, *, limit=8, heavy=None, tool_answer="ops", flaky=0, refuse=None):
        self.limit = limit
        self.heavy = heavy or (lambda found, prompt, tools: len(found) > self.limit)
        self.tool_answer = tool_answer
        self.flaky = flaky
        self.refuse = refuse
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
        if self.refuse is not None and self.refuse(found, prompt or ""):
            raise RuntimeError("the door refused it: content policy violation")
        last = found[-1] if found else "?"
        if tools:
            if self.flaky > 0 or self.tool_answer == "none":
                self.flaky = max(0, self.flaky - 1)
                return SimpleNamespace(
                    has_tool_calls=False, tool_calls=[], finish_reason="stop", usage={}
                )
            notes = [
                f"note {index} after {last}"
                for index in range((prompt or "").count("<checkpoint_source index="))
            ]
            if tools[0]["function"]["name"] == "create_working_memory":
                args = {"working_memory": f"WM after {last}", "checkpoint_summaries": notes}
            else:
                args = {
                    "sections": {
                        "Current State": {"op": "UPDATE", "content": f"state after {last}"}
                    }
                }
                if notes:
                    args["checkpoint_summaries"] = notes
            return SimpleNamespace(
                has_tool_calls=True,
                tool_calls=[SimpleNamespace(arguments=json.dumps(args))],
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


def partial_turn(stand, index, older, anchor, sources):
    """An archive the server cut inside a turn: the older turns, a copy of the retained
    user message (the anchor) and the archived prefix of its answer (the sources), with
    the retention plan that asks Phase 2 for a checkpoint note of that prefix."""
    messages = [*older, anchor, *sources]
    uri = stand.archive_with(index, messages)
    stand.storage.files[f"{uri}/.meta.json"] = json.dumps(
        {
            "retention_plan": {
                "mode": "turn_budget",
                "partial_turn": True,
                "turn_anchor_message_id": anchor.id,
                "checkpoint_source_message_ids": [message.id for message in sources],
                "retained_message_token_budget": 1000,
                "estimated_active_tokens": 100,
                "budget_exceeded": False,
            }
        }
    )
    return messages


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
async def test_one_turn_too_heavy_with_nothing_to_lighten_is_cut_by_messages_and_by_text(
    monkeypatch,
):
    # The weight sits in the words themselves, not in a tool's output: there is no
    # lighter form, so the turn is cut by messages, the heavy message by its text, as
    # deep as it takes; every piece goes into the summary and nothing is left out.
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
    assert stand.storage.files[f"{archive(1)}/.overview.md"] == "WM after a4"
    pieces = [
        prompt for prompt in stand.prompts_of() if "yyyy" in prompt and "question 2" not in prompt
    ]
    assert len(pieces) >= 4, len(pieces)
    done = stand.storage.marker(archive(1), ".done")
    assert done["completed_memory_steps"]["archive_summary"] == sorted(ids(turns))
    meta = stand.storage.marker(archive(1), ".meta.json")
    assert "refused_messages" not in meta and "skipped_turns" not in meta
    assert any(
        stage.startswith(
            "cutting archive_summary inside one turn (2 messages, first u2): halves by messages"
        )
        for stage in stand.stages
    ), stand.stages
    assert any("message u2 in two pieces by its text" in stage for stage in stand.stages), (
        stand.stages
    )
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


@pytest.mark.asyncio
async def test_a_checkpoint_rides_with_the_part_that_holds_its_sources(monkeypatch):
    # The server cut the archive inside a turn: Phase 2 owes a checkpoint note of the
    # archived prefix. Too heavy as a whole, the input is cut by turns as any other; the
    # note is asked of the part that holds the prefix, the other parts go without, and
    # the record lands in the meta as it would from one request.
    stand = _Stand(monkeypatch, door=_Door(limit=4))
    messages = partial_turn(stand, 1, FOUR_TURNS, user(5), [assistant(5)])
    await stand.run(1, messages, "t1")
    assert stand.door.calls == [
        (ids(messages), True),
        (["u1", "a1", "u2", "a2"], False),
        (["u3", "a3", "u4", "a4", "u5", "a5"], True),
        (["u3", "a3"], False),
        (["u4", "a4", "u5", "a5"], True),
    ]
    assert stand.storage.files[f"{archive(1)}/.overview.md"] == "WM after a5"
    meta = stand.storage.marker(archive(1), ".meta.json")
    assert [
        (r["turn_anchor_message_id"], r["source_message_ids"], r["abstract"])
        for r in meta["checkpoints"]
    ] == [("u5", ["a5"], "note 0 after a5")]
    done = stand.storage.marker(archive(1), ".done")
    assert done["completed_memory_steps"]["archive_summary"] == sorted(ids(messages))
    assert (await stand.task("t1")).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_an_answer_that_cannot_be_used_is_asked_again_before_anything_else(monkeypatch):
    # The model answers the forced tool call without the tool call, twice; the third
    # answer is right. Asked again at once, as a passing failure, not a refusal.
    stand = _Stand(monkeypatch, door=_Door(flaky=2))
    messages = partial_turn(stand, 1, [user(1), assistant(1)], user(2), [assistant(2)])
    await stand.run(1, messages, "t1")
    assert [tools for _found, tools in stand.door.calls] == [True, True, True]
    assert stand.storage.files[f"{archive(1)}/.overview.md"] == "WM after a2"
    meta = stand.storage.marker(archive(1), ".meta.json")
    assert [r["abstract"] for r in meta["checkpoints"]] == ["note 0 after a2"]
    assert (await stand.task("t1")).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_message_refused_for_its_content_is_left_out_with_a_record(monkeypatch):
    # The door refuses the content of one answer (a moderation answer). The part is cut
    # down to that one message, it is left out with a record in the meta and the task's
    # stage, and everything around it is summarized.
    poisoned = Message(id="a3", role="assistant", parts=[TextPart("answer 3 POISON")])
    turns = [user(1), assistant(1), user(2), assistant(2), user(3), poisoned, user(4), assistant(4)]
    stand = _Stand(monkeypatch, door=_Door(refuse=lambda found, prompt: "POISON" in prompt))
    stand.archive_with(1, turns)
    await stand.run(1, turns, "t1")
    assert [found for found, _tools in stand.door.calls] == [
        ids(turns),
        ["u1", "a1", "u2", "a2"],
        ["u3", "a3", "u4", "a4"],
        ["u3", "a3"],
        ["u3"],
        ["a3"],
        ["u4", "a4"],
    ]
    assert stand.storage.files[f"{archive(1)}/.overview.md"] == "WM after a4"
    meta = stand.storage.marker(archive(1), ".meta.json")
    assert [(r["step"], r["message_id"]) for r in meta["refused_messages"]] == [
        ("archive_summary", "a3")
    ]
    assert "content policy" in meta["refused_messages"][0]["reason"]
    done = stand.storage.marker(archive(1), ".done")
    assert done["completed_memory_steps"]["archive_summary"] == sorted(ids(turns))
    assert any(
        stage.startswith("archive_summary: message a3 refused by the door")
        for stage in stand.stages
    ), stand.stages
    assert (await stand.task("t1")).status == TaskStatus.COMPLETED
