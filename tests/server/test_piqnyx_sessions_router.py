# piqnyx: the session's details and the context's stats count the archives waiting
# for a summary (PLAN-gorizont, 3е).
# SPDX-License-Identifier: AGPL-3.0
"""The sessions router on the server of ours: `unsummarized_archives` in the session's
details, `stats.unsummarizedArchives` in the context.

A file of its own, not a test added to `test_api_sessions.py`: the in-image run
(PIQNYX.md, stage 2.5) takes the tests of upstream's files as common to both images
and halts on any difference between them, and this test can only fail in the old
image, whose server has no such field. The tests of a file our branch added are run
in the new image alone.

The environment is the one `test_api_sessions.py` builds: its autouse fixture is
imported here so the same config file and the same mock storage serve this test.
"""

import json

import httpx

from openviking.message import Message, TextPart
from openviking.server.identity import RequestContext, Role
from openviking_cli.session.user_id import UserIdentifier
from tests.server.test_api_sessions import (  # noqa: F401  -- the autouse fixture
    DEFAULT_USER,
    _configure_test_env,
)


async def test_get_session_tells_how_many_archives_wait_for_a_summary(
    client: httpx.AsyncClient, service
):
    """The plugin pours again only when nought waits.

    An archive whose summary is not written yet is replayed raw and counted, in
    the session's details and in the context's stats; once its summary stands
    (the mark `.summary.done` beside a readable overview) the context takes the
    overview, leaves the raw messages out, and the count is nought.

    No message is posted here: in this harness the mock storage has no path
    lock, and every message write fails on it -- as it does in the upstream
    tests of `test_api_sessions.py` that post one. The archive is written to
    the storage directly, the way those tests write theirs.
    """
    create_resp = await client.post("/api/v1/sessions", json={})
    session_id = create_resp.json()["result"]["session_id"]

    ctx = RequestContext(user=UserIdentifier.the_default_user(), role=Role.ROOT)
    session = service.sessions.session(ctx, session_id)
    await session.load()
    archived = [
        Message(
            id="archived-user",
            role="user",
            parts=[TextPart("Archived question")],
            peer_id=DEFAULT_USER.user_id,
        ),
        Message(
            id="archived-assistant",
            role="assistant",
            parts=[TextPart("Archived answer")],
            peer_id="assistant-default",
        ),
    ]
    archive_uri = f"{session.uri}/history/archive_001"
    await session._viking_fs.write_file(
        uri=f"{archive_uri}/messages.jsonl",
        content="\n".join(msg.to_jsonl() for msg in archived) + "\n",
        ctx=session.ctx,
    )

    waiting = await client.get(f"/api/v1/sessions/{session_id}")
    assert waiting.status_code == 200
    assert waiting.json()["result"]["unsummarized_archives"] == 1
    context = await client.get(f"/api/v1/sessions/{session_id}/context")
    assert context.status_code == 200
    body = context.json()["result"]
    assert body["stats"]["unsummarizedArchives"] == 1
    assert body["latest_archive_overview"] == ""
    assert [m["parts"][0]["text"] for m in body["messages"]] == [
        "Archived question",
        "Archived answer",
    ]

    await session._viking_fs.write_file(
        uri=f"{archive_uri}/.overview.md", content="# WM after the archive", ctx=session.ctx
    )
    await session._viking_fs.write_file(
        uri=f"{archive_uri}/.summary.done",
        content=json.dumps({"written_at": "2026-10-06T20:00:00Z"}),
        ctx=session.ctx,
    )

    standing = await client.get(f"/api/v1/sessions/{session_id}")
    assert standing.json()["result"]["unsummarized_archives"] == 0
    context = await client.get(f"/api/v1/sessions/{session_id}/context")
    body = context.json()["result"]
    assert body["stats"]["unsummarizedArchives"] == 0
    assert body["latest_archive_overview"] == "# WM after the archive"
    assert body["messages"] == []
