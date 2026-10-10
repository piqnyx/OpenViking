# piqnyx: a Phase 2 step is retried in place until it succeeds (PIQNYX.md, stage 1).
# SPDX-License-Identifier: AGPL-3.0
"""Session commit Phase 2 through a storm, on in-memory stand-ins.

Upstream gives a step four calls and then marks the archive failed; a failed
newest archive takes the summary out of the session's context. With the step
repeated until it passes the archive stays pending instead, and a pending
archive keeps the context whole.
"""

import asyncio
import json

import pytest

from openviking.message import Message, TextPart
from openviking.service.task_tracker import TaskStatus, TaskTracker, set_task_tracker
from openviking.session.session import Session
from openviking.utils import piqnyx_persistence as persistence
from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton

STORM = (
    "Error code: 503 - [{'error': {'code': 503, 'message': 'This model is currently experiencing "
    "high demand. Spikes in demand are usually temporary. Please try again later.', "
    "'status': 'UNAVAILABLE'}}]"
)
MALFORMED = (
    "Error code: 400 - [{'error': {'code': 400, 'message': 'Request contains an invalid "
    "argument.', 'status': 'INVALID_ARGUMENT'}}]"
)
URI = "viking://user/sessions/s1"
ARCHIVE = f"{URI}/history/archive_001"


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

    def markers(self):
        return sorted(
            key.rsplit("/", 1)[1]
            for key in self.files
            if key.startswith(ARCHIVE + "/") and key.rsplit("/", 1)[1] in (".done", ".failed.json")
        )


class _Compressor:
    """Long-term memory extraction that fails as told and then passes."""

    def __init__(self, failures=()):
        self.failures = list(failures)
        self.calls = 0

    async def extract_long_term_memories(self, **kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return []


def _write_config(path, workspace, storage=None):
    path.write_text(
        json.dumps(
            {
                "storage": {"workspace": str(workspace), **(storage or {})},
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
    """The phase reads the server's config; give it one that names nothing real.

    Yields a way to write it anew with other storage settings."""
    config = tmp_path / "ov.conf"
    monkeypatch.setenv("OPENVIKING_CONFIG_FILE", str(config))
    _write_config(config, tmp_path / "data")
    yield lambda storage: _write_config(config, tmp_path / "data", storage)
    OpenVikingConfigSingleton.reset_instance()


class _Stand:
    def __init__(self, monkeypatch, *, memory_failures=(), summary_failures=(), messages=None):
        self.storage = _Storage()
        self.messages = list(messages or ()) or [
            Message(id="m1", role="user", parts=[TextPart("hello")])
        ]
        self.message = self.messages[0]
        self.storage.files[f"{ARCHIVE}/messages.jsonl"] = "".join(
            message.to_jsonl() + "\n" for message in self.messages
        )
        self.storage.files[f"{URI}/.meta.json"] = json.dumps({"session_id": "s1"})
        self.compressor = _Compressor(memory_failures)
        self.session = Session(
            viking_fs=self.storage,
            session_id="s1",
            session_uri=URI,
            session_compressor=self.compressor,
        )
        summary_failures = list(summary_failures)
        self.summaries = 0

        async def summary(*args, **kwargs):
            self.summaries += 1
            if summary_failures:
                raise summary_failures.pop(0)
            return "# Summary\nall good"

        monkeypatch.setattr(self.session, "_generate_archive_summary_async", summary)
        # Upstream's own quick repeats wait one to eight seconds; here they pass no time.
        real_sleep = asyncio.sleep
        self.quick_waits = []

        async def no_time(seconds, *args, **kwargs):
            self.quick_waits.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", no_time)
        self.tracker = TaskTracker(_TaskStore())
        self.account = self.session.ctx.account_id
        self.user = self.session.ctx.user.user_id

    async def run(self):
        set_task_tracker(self.tracker)
        try:
            await self.tracker.create(
                "session_commit",
                resource_id="s1",
                task_id="t1",
                account_id=self.account,
                user_id=self.user,
            )
            await self.session._run_memory_extraction(
                task_id="t1",
                archive_uri=ARCHIVE,
                messages=self.messages,
                usage_records=[],
                first_message_id=self.messages[0].id,
                last_message_id=self.messages[-1].id,
                memory_policy=None,
            )
        finally:
            set_task_tracker(None)

    async def task(self):
        return await self.tracker.get("t1", account_id=self.account, user_id=self.user)

    async def stop_in_the_middle_of_a_wait(self, monkeypatch):
        """Run the phase, wait until a step is waiting out the storm, and stop it from
        outside -- the way the queue worker does when the server goes down."""
        waiting = asyncio.Event()

        async def wait_for_the_stop(_seconds):
            waiting.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(persistence, "_sleep", wait_for_the_stop)
        job = asyncio.ensure_future(self.run())
        await asyncio.wait_for(waiting.wait(), timeout=5)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job


async def test_a_storm_longer_than_upstream_patience_does_not_fail_the_archive(monkeypatch):
    stand = _Stand(monkeypatch, memory_failures=[Exception(STORM)] * 12)
    seen = []

    async def clock(seconds):
        # What the archive and the task look like in the middle of a wait.
        task = await stand.task()
        seen.append((seconds, stand.storage.markers(), task.status, task.stage))

    monkeypatch.setattr(persistence, "_sleep", clock)
    monkeypatch.setattr(persistence, "retry_settings", lambda: (2.0, 900.0))

    await stand.run()

    # Upstream gives the step four calls; three times that was not enough, the fourth passed.
    assert stand.compressor.calls == 13
    assert [wait for wait, *_ in seen] == [2.0, 4.0, 8.0]
    # Through every wait the archive was pending: no marker, so its raw messages and the
    # last closed summary are what the session's context is made of.
    assert [markers for _, markers, *_ in seen] == [[], [], []]
    assert [status for *_, status, _ in seen] == [TaskStatus.RUNNING] * 3
    assert "long_term_memory_extraction" in seen[0][3]
    assert "attempt 1" in seen[0][3] and "attempt 3" in seen[2][3]
    assert "provider_unavailable" in seen[0][3]
    assert stand.storage.markers() == [".done"]
    assert (await stand.task()).status == TaskStatus.COMPLETED


async def test_the_summary_is_repeated_too_and_the_memory_is_not_extracted_twice(monkeypatch):
    stand = _Stand(monkeypatch, summary_failures=[Exception(STORM)] * 9)
    waits = []

    async def clock(seconds):
        waits.append(seconds)

    monkeypatch.setattr(persistence, "_sleep", clock)
    monkeypatch.setattr(persistence, "retry_settings", lambda: (2.0, 900.0))

    await stand.run()

    assert stand.summaries == 10
    assert waits == [2.0, 4.0]
    assert stand.compressor.calls == 1
    assert stand.storage.markers() == [".done"]
    assert stand.storage.files[f"{ARCHIVE}/.overview.md"] == "# Summary\nall good"


async def test_what_waiting_does_not_cure_fails_the_archive_as_before(monkeypatch):
    # A 400 on every request: nothing to wait for. PLAN-gorizont 3ж cuts the part to find
    # the message that earned the refusal, and a refusal on every single message is not
    # the content's doing: the step fails with the door's words, the archive as before.
    turns = [
        Message(id=f"{role}{i}", role=role, parts=[TextPart(f"{role} {i}")])
        for i in range(1, 5)
        for role in ("user", "assistant")
    ]
    stand = _Stand(monkeypatch, memory_failures=[Exception(MALFORMED)] * 40, messages=turns)
    waits = []

    async def clock(seconds):
        waits.append(seconds)

    monkeypatch.setattr(persistence, "_sleep", clock)

    await stand.run()

    assert stand.compressor.calls >= 4
    assert waits == []
    assert stand.storage.markers() == [".failed.json"]
    failed = json.loads(stand.storage.files[f"{ARCHIVE}/.failed.json"])
    assert failed["stage"] == "memory_extraction"
    assert "invalid argument" in failed["error"]
    assert (await stand.task()).status == TaskStatus.FAILED


async def test_a_refusal_of_one_request_is_cut_out_and_the_archive_completes(monkeypatch):
    # PLAN-gorizont 3ж: the door refuses once (a 400 on the whole), the halves pass: the
    # refusal was the content's, it is isolated, and the archive completes.
    stand = _Stand(monkeypatch, memory_failures=[Exception(MALFORMED)])
    waits = []

    async def clock(seconds):
        waits.append(seconds)

    monkeypatch.setattr(persistence, "_sleep", clock)

    await stand.run()

    assert stand.compressor.calls == 1
    assert waits == []
    assert stand.storage.markers() == [".done"]
    assert (await stand.task()).status == TaskStatus.COMPLETED


async def test_a_stop_in_the_middle_of_a_wait_leaves_the_archive_pending(monkeypatch):
    # The server is stopped while a step waits out a storm. The queue keeps the job and
    # hands it out again at the next start; a failed marker would make that start give up.
    stand = _Stand(monkeypatch, memory_failures=[Exception(STORM)] * 8)

    await stand.stop_in_the_middle_of_a_wait(monkeypatch)

    assert stand.storage.markers() == []
    assert (await stand.task()).status == TaskStatus.RUNNING


async def test_a_stop_marks_the_archive_when_the_queue_would_forget_the_job(
    monkeypatch, config_of_its_own
):
    # A queue kept in memory does not outlive the server. Nothing would ever take the
    # archive up again, and the archives after it wait for it without an end: so here
    # the marker is written as upstream writes it.
    config_of_its_own({"agfs": {"queuefs": {"backend": "memory"}}})
    stand = _Stand(monkeypatch, memory_failures=[Exception(STORM)] * 8)

    await stand.stop_in_the_middle_of_a_wait(monkeypatch)

    assert stand.storage.markers() == [".failed.json"]
    assert json.loads(stand.storage.files[f"{ARCHIVE}/.failed.json"])["stage"] == "cancelled"


async def test_a_cancel_that_was_asked_for_marks_the_archive_as_before(monkeypatch):
    stand = _Stand(monkeypatch, memory_failures=[Exception(STORM)] * 8)
    monkeypatch.setattr(stand.tracker, "is_cancellation_requested", lambda task_id: True)

    await stand.stop_in_the_middle_of_a_wait(monkeypatch)

    assert stand.storage.markers() == [".failed.json"]
    assert json.loads(stand.storage.files[f"{ARCHIVE}/.failed.json"])["stage"] == "cancelled"


async def test_a_calm_day_is_as_it_was(monkeypatch):
    stand = _Stand(monkeypatch)
    waits = []

    async def clock(seconds):
        waits.append(seconds)

    monkeypatch.setattr(persistence, "_sleep", clock)

    await stand.run()

    assert (stand.compressor.calls, stand.summaries, waits, stand.quick_waits) == (1, 1, [], [])
    assert stand.storage.markers() == [".done"]
    assert (await stand.task()).status == TaskStatus.COMPLETED


async def test_a_blip_is_still_cured_by_upstream_own_quick_repeats(monkeypatch):
    stand = _Stand(monkeypatch, memory_failures=[Exception(STORM)] * 2)
    waits = []

    async def clock(seconds):
        waits.append(seconds)

    monkeypatch.setattr(persistence, "_sleep", clock)

    await stand.run()

    assert stand.compressor.calls == 3
    assert waits == []
    assert len(stand.quick_waits) == 2
    assert stand.storage.markers() == [".done"]


async def test_an_archive_waiting_for_the_one_before_it_asks_less_and_less_often(monkeypatch):
    # Upstream looks ten times a second, which is nothing while a Phase 2 takes a minute.
    # Behind an archive that waits out a storm it would be hours of reading every marker
    # of the session ten times a second.
    stand = _Stand(monkeypatch)
    second = f"{URI}/history/archive_002"
    stand.storage.files[f"{second}/messages.jsonl"] = stand.message.to_jsonl() + "\n"
    looks = []
    real_sleep = asyncio.sleep

    async def clock(seconds, *args, **kwargs):
        looks.append(seconds)
        if len(looks) == 9:
            stand.storage.files[f"{ARCHIVE}/.done"] = json.dumps({})
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", clock)

    assert await stand.session._wait_for_previous_archive_done(2) is True
    assert looks == [0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0, 5.0]


async def test_while_an_archive_waits_out_a_storm_the_context_is_whole(monkeypatch):
    # What the whole change rests on: a pending archive gives its raw messages and lets
    # the last closed summary through; a failed one takes the summary away.
    stand = _Stand(monkeypatch)
    files = stand.storage.files
    closed = Message(id="old", role="user", parts=[TextPart("said long ago")])
    files[f"{ARCHIVE}/messages.jsonl"] = closed.to_jsonl() + "\n"
    files[f"{ARCHIVE}/.overview.md"] = "# Summary\nwhat was said long ago"
    files[f"{ARCHIVE}/.done"] = json.dumps({"working_memory_enabled": True})
    second = f"{URI}/history/archive_002"
    waiting = Message(id="stormy", role="user", parts=[TextPart("said during the storm")])
    files[f"{second}/messages.jsonl"] = waiting.to_jsonl() + "\n"
    live = Message(id="fresh", role="assistant", parts=[TextPart("said just now")])
    stand.session._messages = [live]

    context = await stand.session.get_session_context(token_budget=100_000)

    assert context["latest_archive_overview"] == "# Summary\nwhat was said long ago"
    assert [message["id"] for message in context["messages"]] == ["stormy", "fresh"]

    # The same archive given up on, as upstream gives it up after four calls.
    files[f"{second}/.failed.json"] = json.dumps({"stage": "memory_extraction", "error": STORM})

    context = await stand.session.get_session_context(token_budget=100_000)

    assert context["latest_archive_overview"] == ""
    assert [message["id"] for message in context["messages"]] == ["fresh"]
