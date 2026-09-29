# piqnyx: a Phase 2 step is retried in place until it succeeds (PIQNYX.md, stage 1).
# SPDX-License-Identifier: AGPL-3.0
"""What waiting can cure, how long the wait is, and the loop that does the waiting.

The texts are the ones measured on 29.09.2026: the storm's is copied from the
`.failed.json` of archive_037, the proxy's own are those `gemini-proxy.py` writes.
"""

import asyncio

import pytest

from openviking.utils import piqnyx_persistence as persistence

QUOTA = (
    "You exceeded your current quota, please check your plan and billing details. For more "
    "information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits.\n"
    "* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_input_token_count, limit: 250000, model: gemini-3.5-flash-lite\n"
    "[gemini-proxy] "
)
STORM = (
    "Error code: 503 - [{'error': {'code': 503, 'message': 'This model is currently experiencing "
    "high demand. Spikes in demand are usually temporary. Please try again later.', "
    "'status': 'UNAVAILABLE'}}]"
)

CURABLE = [
    ("the storm of 28.09", STORM, "provider_unavailable"),
    (
        "the proxy has no live key",
        "Error code: 503 - {'error': {'code': 503, 'status': 'UNAVAILABLE', 'message': "
        "'Proxy: no live key left. 3 of 67 are out until the Pacific day turns'}}",
        "provider_unavailable",
    ),
    (
        "the proxy could not price the request",
        "Error code: 502 - {'error': {'code': 502, 'status': 'UNAVAILABLE', 'message': 'Proxy: "
        "could not price the request -- countTokens gave no answer through any of the exits "
        "tried; nothing was sent. Repeat the request.'}}",
        "provider_unavailable",
    ),
    (
        "the network failed on every exit",
        "Error code: 502 - {'error': 'Upstream request failed: All connection attempts failed'}",
        "provider_unavailable",
    ),
    (
        "the proxy's counter broke",
        "Error code: 500 - {'error': {'code': 500, 'status': 'INTERNAL', 'message': 'Proxy: the "
        "counter broke on this request, nothing was sent: KeyError: x'}}",
        "provider_unavailable",
    ),
    ("the connection dropped", "Connection error.", "provider_unavailable"),
    (
        "every key has spent its minute",
        "Error code: 429 - {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', 'message': '"
        + QUOTA
        + "Every one of the 67 keys has spent its tokens per minute for this model. Nothing was "
        "sent: they would all refuse.'}}",
        "quota",
    ),
    (
        "the door will not serve the exit's address",
        "Error code: 400 - [{'error': {'code': 400, 'message': 'User location is not supported "
        "for the API use.', 'status': 'FAILED_PRECONDITION'}}]",
        "exit_refused",
    ),
    (
        "the key was retired",
        "Error code: 403 - [{'error': {'code': 403, 'message': 'Your API key was reported as "
        "leaked. Please use another API key.', 'status': 'PERMISSION_DENIED'}}]",
        "key_or_exit",
    ),
    (
        "the key is not valid",
        "Error code: 401 - [{'error': {'code': 401, 'message': 'API key not valid.', "
        "'status': 'UNAUTHENTICATED'}}]",
        "key_or_exit",
    ),
    ("the client timed out", "Request timed out.", "network"),
    (
        "the stream broke",
        "peer closed connection without sending complete message body (incomplete chunked read)",
        "network",
    ),
]

NOT_CURABLE = [
    (
        "a request heavier than a key's ceiling",
        "Error code: 429 - {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', 'message': '"
        + QUOTA
        + "This single request is about 302385 input tokens. We stop at 247500 of Google's "
        "250000 per minute per key, and no key could serve it: it was not sent, because trying "
        "would spend quota on certain refusals. Shorten the context and repeat.'}}",
    ),
    (
        "a request the door cannot read",
        "Error code: 400 - [{'error': {'code': 400, 'message': 'Request contains an invalid "
        "argument.', 'status': 'INVALID_ARGUMENT'}}]",
    ),
    (
        "an input too large for the model",
        "Error code: 400 - {'error': {'message': 'The input token count exceeds the maximum "
        "context length of the model'}}",
    ),
    ("content the door refuses", "The response was blocked by the content policy"),
    ("a fault in the code itself", "'NoneType' object has no attribute 'items'"),
    ("an empty summary", "Working Memory output is empty for a required checkpoint"),
]


@pytest.mark.parametrize("name,text,kind", CURABLE, ids=[case[0] for case in CURABLE])
def test_what_waiting_cures(name, text, kind):
    assert persistence.curable(Exception(text)) == kind


@pytest.mark.parametrize("name,text", NOT_CURABLE, ids=[case[0] for case in NOT_CURABLE])
def test_what_waiting_does_not_cure(name, text):
    assert persistence.curable(Exception(text)) is None


def test_a_timeout_is_cured_by_its_kind_whatever_it_says():
    assert persistence.curable(asyncio.TimeoutError()) == "network"
    assert persistence.curable(TimeoutError("")) == "network"
    assert persistence.curable(ConnectionResetError("")) == "network"


def test_the_cause_of_an_error_is_looked_at_too():
    wrapped = RuntimeError("extraction step failed")
    wrapped.__cause__ = Exception(STORM)
    assert persistence.curable(wrapped) == "provider_unavailable"


def test_the_wait_doubles_from_two_seconds_to_fifteen_minutes():
    waits = [persistence.retry_delay(n, 2.0, 900.0) for n in range(1, 12)]
    assert waits == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 900.0, 900.0]


def test_the_wait_does_not_overflow_after_a_long_outage():
    assert persistence.retry_delay(5_000, 2.0, 900.0) == 900.0
    assert persistence.retry_delay(10**9, 2.0, 900.0) == 900.0


def test_a_zero_base_means_no_wait():
    assert persistence.retry_delay(3, 0.0, 900.0) == 0.0


def test_settings_come_from_the_environment(monkeypatch):
    monkeypatch.delenv(persistence.ENV_BASE_SECONDS, raising=False)
    monkeypatch.delenv(persistence.ENV_MAX_SECONDS, raising=False)
    assert persistence.retry_settings() == (2.0, 900.0)

    monkeypatch.setenv(persistence.ENV_BASE_SECONDS, "5")
    monkeypatch.setenv(persistence.ENV_MAX_SECONDS, "60")
    assert persistence.retry_settings() == (5.0, 60.0)


@pytest.mark.parametrize(
    "base,cap",
    [("nonsense", "60"), ("-1", "60"), ("5", "nan"), ("50", "10"), ("", "")],
    ids=["not a number", "negative", "nan", "cap under base", "empty"],
)
def test_settings_that_cannot_be_used_fall_back_to_the_defaults(monkeypatch, base, cap):
    monkeypatch.setenv(persistence.ENV_BASE_SECONDS, base)
    monkeypatch.setenv(persistence.ENV_MAX_SECONDS, cap)
    assert persistence.retry_settings() == (2.0, 900.0)


class _Clock:
    """Stands in for asyncio.sleep: records the waits and passes no time."""

    def __init__(self):
        self.waits = []

    async def sleep(self, seconds):
        self.waits.append(seconds)


async def test_a_step_is_repeated_until_it_passes(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(persistence, "_sleep", clock.sleep)
    calls, told = [], []

    async def step():
        calls.append(1)
        if len(calls) <= 4:
            raise Exception(STORM)
        return "done"

    async def on_wait(failed_attempts, kind, wait, error):
        told.append((failed_attempts, kind, wait))

    result = await persistence.until_cured(
        step, operation="archive_summary", settings=(2.0, 900.0), on_wait=on_wait
    )

    assert result == "done"
    assert len(calls) == 5
    assert clock.waits == [2.0, 4.0, 8.0, 16.0]
    assert told == [
        (1, "provider_unavailable", 2.0),
        (2, "provider_unavailable", 4.0),
        (3, "provider_unavailable", 8.0),
        (4, "provider_unavailable", 16.0),
    ]


async def test_what_waiting_does_not_cure_is_raised_at_once(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(persistence, "_sleep", clock.sleep)
    calls = []

    async def step():
        calls.append(1)
        raise ValueError("Working Memory output is empty for a required checkpoint")

    with pytest.raises(ValueError):
        await persistence.until_cured(step, operation="archive_summary", settings=(2.0, 900.0))

    assert len(calls) == 1
    assert clock.waits == []


async def test_a_cure_may_turn_into_something_waiting_does_not_cure(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(persistence, "_sleep", clock.sleep)
    errors = [Exception(STORM), Exception(STORM), Exception(NOT_CURABLE[1][1])]

    async def step():
        raise errors.pop(0)

    with pytest.raises(Exception, match="invalid argument"):
        await persistence.until_cured(step, operation="long_term", settings=(2.0, 900.0))

    assert clock.waits == [2.0, 4.0]


async def test_a_cancel_during_the_wait_goes_through(monkeypatch):
    async def cancelled(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(persistence, "_sleep", cancelled)

    async def step():
        raise Exception(STORM)

    with pytest.raises(asyncio.CancelledError):
        await persistence.until_cured(step, operation="long_term", settings=(2.0, 900.0))


async def test_a_cancel_inside_the_step_goes_through(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(persistence, "_sleep", clock.sleep)

    async def step():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await persistence.until_cured(step, operation="long_term", settings=(2.0, 900.0))

    assert clock.waits == []


async def test_a_listener_that_breaks_does_not_stop_the_repeats(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(persistence, "_sleep", clock.sleep)
    calls = []

    async def step():
        calls.append(1)
        if len(calls) == 1:
            raise Exception(STORM)
        return "done"

    async def on_wait(*_):
        raise RuntimeError("the task tracker is away")

    assert (
        await persistence.until_cured(
            step, operation="execution", settings=(2.0, 900.0), on_wait=on_wait
        )
        == "done"
    )
    assert clock.waits == [2.0]


class _Config:
    def __init__(self, backend):
        self.storage = type(
            "S", (), {"agfs": type("A", (), {"queuefs": type("Q", (), {"backend": backend})()})()}
        )()


@pytest.mark.parametrize(
    "backend,survives",
    [("sqlite", True), ("sqlite3", True), ("memory", False), ("", False), (None, False)],
)
def test_whether_the_queue_outlives_a_stop(backend, survives):
    assert persistence.queue_survives_a_stop(lambda: _Config(backend)) is survives


def test_a_config_that_cannot_be_read_means_the_queue_is_not_trusted():
    def broken():
        raise FileNotFoundError("no config")

    assert persistence.queue_survives_a_stop(broken) is False
    assert persistence.queue_survives_a_stop(lambda: object()) is False
