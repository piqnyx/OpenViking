# piqnyx: Phase 2 steps run in parts the door takes (PLAN-gorizont, 3б).
# SPDX-License-Identifier: AGPL-3.0
"""Session commit Phase 2 on in-memory stand-ins, with steps too heavy for the door.

The summary and the long-term extraction each get the archive's messages; when a
step is too heavy -- the price handle or the door says so -- it is cut in halves by
whole turns, the halves run one after another, each done part is marked in the
archive's metadata, and the next Phase 2 replays only what was not done.
"""

import asyncio
import json

import httpx
import openai
import pytest

from openviking.message import Message, TextPart
from openviking.service.task_tracker import TaskStatus, TaskTracker, set_task_tracker
from openviking.session.session import Session
from openviking.utils.piqnyx_price import TooHeavyForTheDoor
from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton

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


FOUR_TURNS = [user(1), assistant(1), user(2), assistant(2), user(3), assistant(3), user(4), assistant(4)]
OVERFLOW_BODY = {
    "error": {
        "code": 400,
        "status": "INVALID_ARGUMENT",
        "message": (
            "The input token count (284544) exceeds the maximum number of tokens allowed "
            "(249000). [gemini-proxy] This single request weighs 284544 by the counter, above "
            "the ceiling of 249000 for any one key; it was not sent. Shorten the context and repeat."
        ),
    }
}


def door_overflow() -> openai.APIStatusError:
    request = httpx.Request("POST", "http://127.0.0.1:8787/v1beta/openai/chat/completions")
    response = httpx.Response(400, json=OVERFLOW_BODY, request=request)
    return openai.APIStatusError(
        f"Error code: 400 - {OVERFLOW_BODY}", response=response, body=OVERFLOW_BODY["error"]
    )


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


class _Compressor:
    """Long-term extraction the door takes only up to `limit` messages at once."""

    def __init__(self, limit=8, heavy=None, fail_on=None):
        self.limit = limit
        self.heavy = heavy or (lambda part: len(part) > self.limit)
        self.fail_on = fail_on
        self.calls = []

    async def extract_long_term_memories(self, messages, **kwargs):
        part = ids(messages)
        self.calls.append(part)
        if self.fail_on is not None and self.fail_on(part):
            raise ValueError("broken json from the model")
        if self.heavy(messages):
            raise TooHeavyForTheDoor(len(messages) * 1000, self.limit * 1000, "handle")
        return []


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


class _Stand:
    def __init__(self, monkeypatch, *, compressor, summary_limit=8, summary_heavy=None):
        self.storage = _Storage()
        self.storage.files[f"{URI}/.meta.json"] = json.dumps({"session_id": "s1"})
        self.compressor = compressor
        self.session = Session(
            viking_fs=self.storage,
            session_id="s1",
            session_uri=URI,
            session_compressor=self.compressor,
        )
        self.summaries = []
        heavy = summary_heavy or (lambda part: len(part) > summary_limit)

        async def summary(messages, latest_archive_overview="", **kwargs):
            self.summaries.append((ids(messages), latest_archive_overview))
            if heavy(messages):
                raise TooHeavyForTheDoor(len(messages) * 1000, summary_limit * 1000, "handle")
            return f"# WM after {ids(messages)[-1]}"

        monkeypatch.setattr(self.session, "_generate_archive_summary_async", summary)
        real_sleep = asyncio.sleep

        async def no_time(seconds, *args, **kwargs):
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", no_time)
        self.tracker = TaskTracker(_TaskStore())
        self.account = self.session.ctx.account_id
        self.user = self.session.ctx.user.user_id

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


@pytest.mark.asyncio
async def test_steps_too_heavy_are_done_in_halves_by_turns(monkeypatch):
    stand = _Stand(monkeypatch, compressor=_Compressor(limit=4), summary_limit=4)
    stand.archive_with(1, FOUR_TURNS)

    await stand.run(1, FOUR_TURNS, "t1")

    assert stand.compressor.calls == [ids(FOUR_TURNS), ["u1", "a1", "u2", "a2"], ["u3", "a3", "u4", "a4"]]
    # The summary is built part by part, each on the previous part's working memory.
    assert stand.summaries == [
        (ids(FOUR_TURNS), ""),
        (["u1", "a1", "u2", "a2"], ""),
        (["u3", "a3", "u4", "a4"], "# WM after a2"),
    ]
    assert stand.storage.files[f"{archive(1)}/.overview.md"] == "# WM after a4"
    done = stand.storage.marker(archive(1), ".done")
    assert done is not None, sorted(stand.storage.files)
    assert done["completed_memory_steps"]["long_term"] == sorted(ids(FOUR_TURNS))
    assert done["completed_memory_steps"]["archive_summary"] == sorted(ids(FOUR_TURNS))
    assert (await stand.task("t1")).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_the_doors_own_refusal_cuts_the_part_like_the_handles_verdict(monkeypatch):
    def heavy(messages):
        if len(messages) > 4:
            raise door_overflow()
        return False

    stand = _Stand(monkeypatch, compressor=_Compressor(heavy=heavy), summary_limit=8)
    stand.archive_with(1, FOUR_TURNS)

    await stand.run(1, FOUR_TURNS, "t1")

    assert stand.compressor.calls == [ids(FOUR_TURNS), ["u1", "a1", "u2", "a2"], ["u3", "a3", "u4", "a4"]]
    assert stand.storage.marker(archive(1), ".done") is not None


@pytest.mark.asyncio
async def test_a_part_that_fails_for_good_keeps_the_parts_done_and_the_next_archive_replays_the_rest(monkeypatch):
    compressor = _Compressor(limit=4, fail_on=lambda part: part == ["u3", "a3", "u4", "a4"])
    stand = _Stand(monkeypatch, compressor=compressor, summary_limit=4)
    stand.archive_with(1, FOUR_TURNS)

    with pytest.raises(ValueError, match="broken json"):
        await stand.run(1, FOUR_TURNS, "t1")

    failed = stand.storage.marker(archive(1), ".failed.json")
    assert failed is not None
    assert failed["completed_memory_steps"]["long_term"] == ["a1", "a2", "u1", "u2"]
    assert failed["completed_memory_steps"]["archive_summary"] == sorted(ids(FOUR_TURNS))
    assert stand.storage.files[f"{archive(1)}/.overview.md"] == "# WM after a4"

    # The next archive: the compressor takes six at once now; only the undone half
    # of archive_001 rides along with the two new messages, not all ten.
    new = [user(5), assistant(5)]
    compressor.limit = 6
    compressor.fail_on = None
    compressor.calls.clear()
    stand.summaries.clear()
    stand.archive_with(2, new)

    await stand.run(2, new, "t2")

    assert compressor.calls == [["u3", "a3", "u4", "a4", "u5", "a5"]]
    assert [part for part, _overview in stand.summaries] == [["u5", "a5"]]
    done = stand.storage.marker(archive(2), ".done")
    assert done["covered_failed_archives"] == ["archive_001"]
    assert (await stand.task("t2")).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_single_turn_too_heavy_fails_the_step_for_good_and_keeps_the_parts_before(monkeypatch):
    compressor = _Compressor(heavy=lambda messages: "u3" in ids(messages))
    stand = _Stand(monkeypatch, compressor=compressor, summary_limit=8)
    stand.archive_with(1, FOUR_TURNS)

    with pytest.raises(Exception) as trouble:
        await stand.run(1, FOUR_TURNS, "t1")

    assert "turn" in str(trouble.value).lower()
    failed = stand.storage.marker(archive(1), ".failed.json")
    assert failed is not None
    assert failed["completed_memory_steps"]["long_term"] == ["a1", "a2", "u1", "u2"]
    assert ["u4", "a4"] not in compressor.calls
    assert (await stand.task("t1")).status == TaskStatus.FAILED
