# piqnyx: the OpenAI backend asks the price handle before sending (PLAN-gorizont, 3б).
# SPDX-License-Identifier: AGPL-3.0
"""The backend prices the very body it is about to send; a body above the ceiling
never leaves, and the door's own overflow refusal is the same verdict, not a storm."""

from types import SimpleNamespace

import httpx
import openai
import pytest

from openviking.models.vlm.backends import openai_vlm
from openviking.models.vlm.backends.openai_vlm import OpenAIVLM
from openviking.utils.piqnyx_price import TooHeavyForTheDoor

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
STORM_BODY = {
    "error": {
        "code": 503,
        "status": "UNAVAILABLE",
        "message": "This model is currently experiencing high demand. Please try again later.",
    }
}


def sdk_error(status, body):
    request = httpx.Request("POST", "http://127.0.0.1:8787/v1beta/openai/chat/completions")
    response = httpx.Response(status, json=body, request=request)
    return openai.APIStatusError(
        f"Error code: {status} - {body}", response=response, body=body["error"]
    )


def answer(text):
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None
    )


class _Completions:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def stand(monkeypatch, answers, verdict=None):
    vlm = OpenAIVLM(
        {
            "api_key": "sk-test",
            "model": "gemini-3.5-flash-lite",
            "api_base": "http://127.0.0.1:8787/v1beta/openai",
            "max_retries": 1,
        }
    )
    completions = _Completions(answers)
    monkeypatch.setattr(
        vlm, "get_async_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )
    priced = []

    async def price(kwargs, **_ignored):
        priced.append(kwargs)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    monkeypatch.setattr(openai_vlm, "priced_or_too_heavy", price)
    return vlm, completions, priced


@pytest.mark.asyncio
async def test_the_handle_is_asked_with_the_very_body_before_sending(monkeypatch):
    vlm, completions, priced = stand(monkeypatch, [answer("ok")], {"fits": True, "charge": 5})

    assert await vlm.get_completion_async(prompt="hi") == "ok"

    assert len(priced) == 1 and len(completions.calls) == 1
    assert priced[0] == completions.calls[0]
    assert priced[0]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_too_heavy_by_the_handle_never_reaches_the_door(monkeypatch):
    vlm, completions, _priced = stand(
        monkeypatch, [answer("ok")], TooHeavyForTheDoor(284544, 249000, "handle")
    )

    with pytest.raises(TooHeavyForTheDoor):
        await vlm.get_completion_async(prompt="hi")

    assert completions.calls == []


@pytest.mark.asyncio
async def test_the_doors_own_refusal_is_too_heavy_and_not_repeated(monkeypatch):
    vlm, completions, _priced = stand(
        monkeypatch, [sdk_error(400, OVERFLOW_BODY), answer("ok")], {"fits": True}
    )

    with pytest.raises(TooHeavyForTheDoor) as heavy:
        await vlm.get_completion_async(prompt="hi")

    assert heavy.value.source == "door"
    assert (heavy.value.charge, heavy.value.ceiling) == (284544, 249000)
    assert len(completions.calls) == 1


@pytest.mark.asyncio
async def test_a_storm_is_still_repeated_as_before(monkeypatch):
    vlm, completions, _priced = stand(
        monkeypatch, [sdk_error(503, STORM_BODY), answer("ok")], {"fits": True}
    )
    monkeypatch.setattr(openai_vlm.asyncio, "sleep", _no_time)

    assert await vlm.get_completion_async(prompt="hi") == "ok"
    assert len(completions.calls) == 2


async def _no_time(*_args, **_kwargs):
    return None
