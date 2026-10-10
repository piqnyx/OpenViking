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


STORM = (
    "Error code: 503 - [{'error': {'code': 503, 'message': 'This model is currently experiencing "
    "high demand. Spikes in demand are usually temporary. Please try again later.', "
    "'status': 'UNAVAILABLE'}}]"
)


class _Compressor:
    """Long-term extraction the door takes only up to `limit` messages at once."""

    def __init__(self, limit=8, heavy=None, fail_on=None, storms_on=None, storms=0, hold=None):
        self.limit = limit
        self.heavy = heavy or (lambda part: len(part) > self.limit)
        self.fail_on = fail_on
        self.storms_on = storms_on
        self.storms = storms
        # An event the extraction waits for before it answers: the summary is
        # written while the extraction is still at work.
        self.hold = hold
        self.calls = []

    async def extract_long_term_memories(self, messages, **kwargs):
        part = ids(messages)
        self.calls.append(part)
        if self.hold is not None:
            await self.hold.wait()
        if self.fail_on is not None and self.fail_on(part):
            raise ValueError("broken json from the model")
        if self.storms and self.storms_on is not None and self.storms_on(part):
            self.storms -= 1
            raise Exception(STORM)
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
    def __init__(
        self, monkeypatch, *, compressor, summary_limit=8, summary_heavy=None, summary_fail_on=None
    ):
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
            if summary_fail_on is not None and summary_fail_on(messages):
                # Trouble of the model's own making: nothing to cut, nothing to wait for.
                raise ValueError("broken json from the model")
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

    assert stand.compressor.calls == [
        ids(FOUR_TURNS),
        ["u1", "a1", "u2", "a2"],
        ["u3", "a3", "u4", "a4"],
    ]
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

    assert stand.compressor.calls == [
        ids(FOUR_TURNS),
        ["u1", "a1", "u2", "a2"],
        ["u3", "a3", "u4", "a4"],
    ]
    assert stand.storage.marker(archive(1), ".done") is not None


@pytest.mark.asyncio
async def test_a_part_that_fails_for_good_keeps_the_parts_done_and_the_next_archive_replays_the_rest(
    monkeypatch,
):
    compressor = _Compressor(limit=4, fail_on=lambda part: part == ["u3", "a3", "u4", "a4"])
    stand = _Stand(monkeypatch, compressor=compressor, summary_limit=4)
    stand.archive_with(1, FOUR_TURNS)

    # Phase 2 does not raise: it writes the failed marker and fails the task.
    await stand.run(1, FOUR_TURNS, "t1")

    first = await stand.task("t1")
    assert first.status == TaskStatus.FAILED and "broken json" in (first.error or "")
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
    # PLAN-gorizont 3г: the next summary builds on the failed archive's finished WM.
    assert stand.summaries == [(["u5", "a5"], "# WM after a4")]
    done = stand.storage.marker(archive(2), ".done")
    assert done["covered_failed_archives"] == ["archive_001"]
    assert (await stand.task("t2")).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_single_turn_too_heavy_is_skipped_and_the_rest_goes_on(monkeypatch):
    # PLAN-gorizont 3ж: one turn alone above the ceiling cannot be cut, and the
    # extraction has no lighter form of it: the turn is skipped and counted done, the
    # parts after it run, the meta says which turn and why, and the archive does not
    # fail for good over one turn.
    compressor = _Compressor(heavy=lambda messages: "u3" in ids(messages))
    stand = _Stand(monkeypatch, compressor=compressor, summary_limit=8)
    stand.archive_with(1, FOUR_TURNS)
    stages = []
    real_update_stage = stand.tracker.update_stage

    async def remember(task_id, stage, **kwargs):
        stages.append(stage)
        await real_update_stage(task_id, stage, **kwargs)

    monkeypatch.setattr(stand.tracker, "update_stage", remember)

    await stand.run(1, FOUR_TURNS, "t1")

    assert (await stand.task("t1")).status == TaskStatus.COMPLETED
    assert compressor.calls == [
        ids(FOUR_TURNS),
        ["u1", "a1", "u2", "a2"],
        ["u3", "a3", "u4", "a4"],
        ["u3", "a3"],
        ["u4", "a4"],
    ]
    done = stand.storage.marker(archive(1), ".done")
    assert done["completed_memory_steps"]["long_term"] == sorted(ids(FOUR_TURNS))
    meta = stand.storage.marker(archive(1), ".meta.json")
    assert [(record["step"], record["message_ids"]) for record in meta["skipped_turns"]] == [
        ("long_term_memory_extraction", ["u3", "a3"])
    ]
    assert any(
        stage.startswith(
            "skipping long_term_memory_extraction: one turn alone (2 messages, first u3)"
        )
        for stage in stages
    ), stages


@pytest.mark.asyncio
async def test_a_storm_inside_a_part_is_waited_out_and_the_part_is_marked_once(monkeypatch):
    # PLAN-gorizont 3в: waiting cures the door and the road, part by part. Stage 1's
    # repeats live inside the part now: the second half meets the storm twice, is
    # repeated in place, passes, and the archive completes with every part marked.
    from openviking.utils import piqnyx_persistence as persistence

    compressor = _Compressor(
        limit=4, storms_on=lambda part: part == ["u3", "a3", "u4", "a4"], storms=2
    )
    stand = _Stand(monkeypatch, compressor=compressor, summary_limit=8)
    stand.archive_with(1, FOUR_TURNS)
    waits = []

    async def no_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(persistence, "_sleep", no_wait)

    await stand.run(1, FOUR_TURNS, "t1")

    assert compressor.calls == [
        ids(FOUR_TURNS),
        ["u1", "a1", "u2", "a2"],
        ["u3", "a3", "u4", "a4"],
        ["u3", "a3", "u4", "a4"],
        ["u3", "a3", "u4", "a4"],
    ]
    assert (await stand.task("t1")).status == TaskStatus.COMPLETED
    done = stand.storage.marker(archive(1), ".done")
    assert done["completed_memory_steps"]["long_term"] == sorted(ids(FOUR_TURNS))


# PLAN-gorizont 3г (Вит, 06.10): a written summary is a summary. An archive whose
# working memory is complete but whose extraction failed feeds the context at once;
# the extraction is finished later, in parts. The task says in words what was
# done and what failed.


@pytest.mark.asyncio
async def test_a_written_summary_feeds_the_context_even_when_extraction_failed(monkeypatch):
    compressor = _Compressor(limit=4, fail_on=lambda part: part == ["u3", "a3", "u4", "a4"])
    stand = _Stand(monkeypatch, compressor=compressor, summary_limit=4)
    stand.archive_with(1, FOUR_TURNS)

    await stand.run(1, FOUR_TURNS, "t1")

    assert (await stand.task("t1")).status == TaskStatus.FAILED
    failed = stand.storage.marker(archive(1), ".failed.json")
    assert failed["summary_complete"] is True
    context = await stand.session._collect_session_context_components()
    assert context["latest_archive"]["overview"] == "# WM after a4"
    assert context["failed_archives"] == 1
    assert await stand.session._get_latest_completed_archive_overview() == "# WM after a4"


@pytest.mark.asyncio
async def test_an_unfinished_summary_does_not_feed_the_context(monkeypatch):
    stand = _Stand(
        monkeypatch,
        compressor=_Compressor(limit=8),
        # PLAN-gorizont 3ж: a turn too heavy on its own is skipped now, not failed; what
        # leaves a summary unfinished is trouble of the model's own making in a part.
        summary_limit=4,
        summary_fail_on=lambda messages: "u3" in ids(messages) and len(messages) <= 4,
    )
    stand.archive_with(1, FOUR_TURNS)

    await stand.run(1, FOUR_TURNS, "t1")

    assert (await stand.task("t1")).status == TaskStatus.FAILED
    failed = stand.storage.marker(archive(1), ".failed.json")
    assert failed["summary_complete"] is False
    assert failed["completed_memory_steps"]["archive_summary"] == ["a1", "a2", "u1", "u2"]
    context = await stand.session._collect_session_context_components()
    assert context["latest_archive"] is None
    assert await stand.session._get_latest_completed_archive_overview() == ""


@pytest.mark.asyncio
async def test_the_task_tells_what_was_done_and_what_failed(monkeypatch):
    compressor = _Compressor(limit=4, fail_on=lambda part: part == ["u3", "a3", "u4", "a4"])
    stand = _Stand(monkeypatch, compressor=compressor, summary_limit=4)
    stand.archive_with(1, FOUR_TURNS)
    stages = []
    real_update_stage = stand.tracker.update_stage

    async def remember(task_id, stage, **kwargs):
        stages.append(stage)
        await real_update_stage(task_id, stage, **kwargs)

    monkeypatch.setattr(stand.tracker, "update_stage", remember)

    await stand.run(1, FOUR_TURNS, "t1")

    assert "working memory written" in stages
    assert "long_term_memory_extraction: 4 of 8 messages done" in stages
    task = await stand.task("t1")
    assert task.status == TaskStatus.FAILED
    assert task.error.startswith("working memory written; ")
    assert "long_term_memory_extraction: 4 of 8 messages done" in task.error
    assert "broken json" in task.error


# PLAN-gorizont 3е (Вит, 06.10): the summary stands as soon as it is written. An
# archive whose extraction is still at work, or failed, carries the mark
# `.summary.done`; the context takes its overview and leaves its raw messages out,
# the way it does for a closed archive. An archive without the mark is replayed raw
# and counted, so the plugin knows not to pour again until the count is nought.

SUMMARY_MARK = ".summary.done"


def _stages_of(monkeypatch, stand):
    stages = []
    real_update_stage = stand.tracker.update_stage

    async def remember(task_id, stage, **kwargs):
        stages.append(stage)
        await real_update_stage(task_id, stage, **kwargs)

    monkeypatch.setattr(stand.tracker, "update_stage", remember)
    return stages


async def _context_of(stand):
    return await stand.session._collect_session_context_components()


@pytest.mark.asyncio
async def test_a_written_summary_stands_in_the_context_while_extraction_still_runs(monkeypatch):
    hold = asyncio.Event()
    stand = _Stand(monkeypatch, compressor=_Compressor(limit=8, hold=hold))
    stand.archive_with(1, FOUR_TURNS)
    stand.session._messages = [user(5)]
    stages = _stages_of(monkeypatch, stand)

    run = asyncio.create_task(stand.run(1, FOUR_TURNS, "t1"))
    for _ in range(1000):
        if stand.storage.marker(archive(1), SUMMARY_MARK):
            break
        await asyncio.sleep(0)
    else:
        hold.set()
        await run
        pytest.fail("the summary mark never appeared while the extraction was held")

    mark = stand.storage.marker(archive(1), SUMMARY_MARK)
    assert isinstance(mark["written_at"], str) and mark["written_at"]
    assert stand.storage.marker(archive(1), ".done") is None
    assert "working memory written" in stages
    context = await _context_of(stand)
    assert context["latest_archive"]["overview"] == "# WM after a4"
    assert ids(context["messages"]) == ["u5"]
    assert context["failed_archives"] == 0
    assert context["unsummarized_archives"] == 0
    assert await stand.session.unsummarized_archives() == 0

    hold.set()
    await run
    assert (await stand.task("t1")).status == TaskStatus.COMPLETED
    assert stand.storage.marker(archive(1), ".done") is not None
    after = await _context_of(stand)
    assert after["latest_archive"]["overview"] == "# WM after a4"
    assert ids(after["messages"]) == ["u5"]
    assert after["unsummarized_archives"] == 0


@pytest.mark.asyncio
async def test_the_summary_mark_is_written_even_when_extraction_fails(monkeypatch):
    compressor = _Compressor(limit=4, fail_on=lambda part: part == ["u3", "a3", "u4", "a4"])
    stand = _Stand(monkeypatch, compressor=compressor, summary_limit=4)
    stand.archive_with(1, FOUR_TURNS)

    await stand.run(1, FOUR_TURNS, "t1")

    assert (await stand.task("t1")).status == TaskStatus.FAILED
    assert stand.storage.marker(archive(1), SUMMARY_MARK) is not None
    assert stand.storage.marker(archive(1), ".failed.json")["summary_complete"] is True
    assert await stand.session.unsummarized_archives() == 0


@pytest.mark.asyncio
async def test_an_unfinished_summary_leaves_no_mark(monkeypatch):
    stand = _Stand(
        monkeypatch,
        compressor=_Compressor(limit=8),
        # PLAN-gorizont 3ж: a turn too heavy on its own is skipped now, not failed; what
        # leaves a summary unfinished is trouble of the model's own making in a part.
        summary_limit=4,
        summary_fail_on=lambda messages: "u3" in ids(messages) and len(messages) <= 4,
    )
    stand.archive_with(1, FOUR_TURNS)

    await stand.run(1, FOUR_TURNS, "t1")

    assert (await stand.task("t1")).status == TaskStatus.FAILED
    assert stand.storage.marker(archive(1), SUMMARY_MARK) is None


@pytest.mark.asyncio
async def test_a_rerun_that_finds_the_summary_already_written_marks_it(monkeypatch):
    # The archive was summarized by the server of image .2, which left the part
    # marks and the overview but knew no summary mark; the task comes round again.
    stand = _Stand(monkeypatch, compressor=_Compressor(limit=8))
    stand.archive_with(1, FOUR_TURNS)
    stand.storage.files[f"{archive(1)}/.overview.md"] = "# WM after a4"
    stand.storage.files[f"{archive(1)}/.meta.json"] = json.dumps(
        {"completed_memory_steps": {"archive_summary": sorted(ids(FOUR_TURNS))}}
    )

    await stand.run(1, FOUR_TURNS, "t1")

    assert (await stand.task("t1")).status == TaskStatus.COMPLETED
    assert stand.summaries == []
    assert stand.storage.marker(archive(1), SUMMARY_MARK) is not None
    assert stand.storage.marker(archive(1), ".done") is not None


@pytest.mark.asyncio
async def test_an_archive_without_a_summary_yet_is_replayed_raw_and_counted(monkeypatch):
    stand = _Stand(monkeypatch, compressor=_Compressor(limit=8))
    stand.archive_with(1, FOUR_TURNS)
    stand.session._messages = [user(5)]

    context = await _context_of(stand)
    assert context["latest_archive"] is None
    assert ids(context["messages"]) == ids(FOUR_TURNS) + ["u5"]
    assert context["unsummarized_archives"] == 1
    assert await stand.session.unsummarized_archives() == 1
    whole = await stand.session.get_session_context(token_budget=1_000_000)
    assert whole["stats"]["unsummarizedArchives"] == 1
    assert [m["id"] for m in whole["messages"]] == ids(FOUR_TURNS) + ["u5"]


@pytest.mark.asyncio
async def test_a_newer_archive_without_a_summary_rides_on_the_older_summarized_one(monkeypatch):
    stand = _Stand(monkeypatch, compressor=_Compressor(limit=8))
    stand.archive_with(1, FOUR_TURNS)
    stand.storage.files[f"{archive(1)}/.overview.md"] = "# WM one"
    stand.storage.files[f"{archive(1)}/{SUMMARY_MARK}"] = json.dumps({"written_at": "x"})
    stand.archive_with(2, [user(5), assistant(5)])
    stand.session._messages = [user(6)]

    context = await _context_of(stand)
    assert context["latest_archive"]["overview"] == "# WM one"
    assert context["latest_archive"]["archive_id"] == "archive_001"
    assert ids(context["messages"]) == ["u5", "a5", "u6"]
    assert context["failed_archives"] == 0
    assert context["unsummarized_archives"] == 1
    assert await stand.session.unsummarized_archives() == 1
    whole = await stand.session.get_session_context(token_budget=1_000_000)
    assert whole["latest_archive_overview"] == "# WM one"
    assert whole["stats"]["unsummarizedArchives"] == 1


@pytest.mark.asyncio
async def test_a_summary_mark_without_a_readable_overview_hides_nothing(monkeypatch):
    stand = _Stand(monkeypatch, compressor=_Compressor(limit=8))
    stand.archive_with(1, FOUR_TURNS)
    stand.storage.files[f"{archive(1)}/{SUMMARY_MARK}"] = json.dumps({"written_at": "x"})
    stand.session._messages = [user(5)]

    context = await _context_of(stand)
    assert context["latest_archive"] is None
    assert ids(context["messages"]) == ids(FOUR_TURNS) + ["u5"]
    assert context["unsummarized_archives"] == 1
    assert await stand.session.unsummarized_archives() == 1


@pytest.mark.asyncio
async def test_a_failed_archive_with_the_mark_alone_feeds_the_context(monkeypatch):
    # Written by a server that knows the mark but a failure marker without the flag.
    stand = _Stand(monkeypatch, compressor=_Compressor(limit=8))
    stand.archive_with(1, FOUR_TURNS)
    stand.storage.files[f"{archive(1)}/.overview.md"] = "# WM one"
    stand.storage.files[f"{archive(1)}/{SUMMARY_MARK}"] = json.dumps({"written_at": "x"})
    stand.storage.files[f"{archive(1)}/.failed.json"] = json.dumps(
        {"stage": "memory_extraction", "error": "broken json"}
    )
    stand.session._messages = [user(5)]

    context = await _context_of(stand)
    assert context["latest_archive"]["overview"] == "# WM one"
    assert ids(context["messages"]) == ["u5"]
    assert context["failed_archives"] == 1
    assert context["unsummarized_archives"] == 0


@pytest.mark.asyncio
async def test_the_checkpoints_of_a_standing_summary_are_inserted(monkeypatch):
    def checkpoints_for(anchor):
        return json.dumps(
            {
                "checkpoints": [
                    {
                        "turn_anchor_message_id": anchor,
                        "source_message_ids": ["u1", "a1"],
                        "abstract": f"what was said before {anchor}",
                        "checkpoint_version": 2,
                    }
                ]
            }
        )

    # The summary stands with the extraction still at work ...
    pending = _Stand(monkeypatch, compressor=_Compressor(limit=8))
    pending.archive_with(1, FOUR_TURNS)
    pending.storage.files[f"{archive(1)}/.overview.md"] = "# WM one"
    pending.storage.files[f"{archive(1)}/{SUMMARY_MARK}"] = json.dumps({"written_at": "x"})
    pending.storage.files[f"{archive(1)}/.meta.json"] = checkpoints_for("u5")
    pending.session._messages = [user(5), assistant(5)]
    context = await _context_of(pending)
    assert ids(context["messages"]) == ["u5", "checkpoint_archive_001_u5", "a5"]
    assert context["messages"][1].parts[0].abstract == "what was said before u5"

    # ... and with the extraction failed after it.
    failed = _Stand(monkeypatch, compressor=_Compressor(limit=8))
    failed.archive_with(1, FOUR_TURNS)
    failed.storage.files[f"{archive(1)}/.overview.md"] = "# WM one"
    failed.storage.files[f"{archive(1)}/.meta.json"] = checkpoints_for("u5")
    failed.storage.files[f"{archive(1)}/.failed.json"] = json.dumps(
        {"stage": "memory_extraction", "error": "broken json", "summary_complete": True}
    )
    failed.session._messages = [user(5), assistant(5)]
    context = await _context_of(failed)
    assert ids(context["messages"]) == ["u5", "checkpoint_archive_001_u5", "a5"]
