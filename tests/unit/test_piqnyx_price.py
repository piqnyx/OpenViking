# piqnyx: Phase 2 asks the proxy's price handle before every request (PLAN-gorizont, 3б).
# SPDX-License-Identifier: AGPL-3.0
"""The price handle client: where it is, what it asks, what it answers, what it refuses.

The handle is gemini-proxy's `POST /price`: the same count the proxy's gate makes,
without sending. `fits` false means the door would refuse the request as too heavy,
and the step must cut its part instead of sending. The door's own refusal, in
Google's overflow shape with the proxy's note, is the same verdict arriving late.
"""

import json

import httpx
import openai
import pytest

from openviking.utils import piqnyx_persistence as persistence
from openviking.utils import piqnyx_price as price

KWARGS = {"model": "gemini-3.5-flash-lite", "messages": [{"role": "user", "content": "hi"}]}
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
QUOTA_BODY = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": (
            "You exceeded your current quota, please check your plan and billing details.\n"
            "* Quota exceeded for metric: generativelanguage.googleapis.com/"
            "generate_content_free_tier_input_token_count, limit: 250000, model: x\n"
            "[gemini-proxy] every key's quota spent"
        ),
    }
}


def sdk_error(status: int, body: dict) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://127.0.0.1:8787/v1beta/openai/chat/completions")
    response = httpx.Response(status, json=body, request=request)
    return openai.APIStatusError(
        f"Error code: {status} - {body}", response=response, body=body.get("error")
    )


class TestWhereTheHandleIs:
    def test_the_default_is_the_proxy_on_this_machine(self, monkeypatch):
        monkeypatch.delenv(price.ENV_PRICE_URL, raising=False)
        assert price.price_url() == "http://127.0.0.1:8787/price"

    def test_the_environment_names_another(self, monkeypatch):
        monkeypatch.setenv(price.ENV_PRICE_URL, " http://10.0.0.2:8787/price ")
        assert price.price_url() == "http://10.0.0.2:8787/price"

    def test_an_empty_value_turns_pricing_off(self, monkeypatch):
        monkeypatch.setenv(price.ENV_PRICE_URL, "   ")
        assert price.price_url() is None


def _handle(answer, status=200):
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if isinstance(answer, Exception):
            raise answer
        return (
            httpx.Response(status, json=answer)
            if isinstance(answer, dict)
            else httpx.Response(status, text=answer)
        )

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(respond))


class TestAskingTheHandle:
    @pytest.mark.asyncio
    async def test_the_body_goes_as_it_is_and_the_answer_comes_back(self):
        seen, client = _handle({"charge": 12, "ceiling": 249000, "fits": True})
        answer = await price.price(KWARGS, url="http://h/price", client=client)
        assert seen == [KWARGS]
        assert answer == {"charge": 12, "ceiling": 249000, "fits": True}

    @pytest.mark.asyncio
    async def test_pricing_off_asks_nothing(self, monkeypatch):
        monkeypatch.setenv(price.ENV_PRICE_URL, "")
        seen, client = _handle({"fits": True})
        assert await price.price(KWARGS, client=client) is None
        assert seen == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "trouble",
        [
            httpx.ConnectError("down"),
            ("not json at all", 200),
            ({"error": {"code": 503, "message": "no key to count on"}}, 503),
            ({"error": {"code": 400, "message": "the gate refuses this body"}}, 400),
        ],
    )
    async def test_a_handle_that_cannot_answer_is_no_verdict(self, trouble, caplog):
        # The step goes on without a price: the proxy's gate is still there.
        if isinstance(trouble, tuple):
            seen, client = _handle(trouble[0], trouble[1])
        else:
            seen, client = _handle(trouble)
        with caplog.at_level("WARNING"):
            assert await price.price(KWARGS, url="http://h/price", client=client) is None
        assert any("price" in record.getMessage().lower() for record in caplog.records)


class TestTheVerdict:
    @pytest.mark.asyncio
    async def test_a_body_that_fits_passes_with_its_price(self):
        _seen, client = _handle({"charge": 200, "ceiling": 249000, "fits": True})
        answer = await price.priced_or_too_heavy(KWARGS, url="http://h/price", client=client)
        assert answer["charge"] == 200

    @pytest.mark.asyncio
    async def test_a_body_above_the_ceiling_is_too_heavy(self):
        _seen, client = _handle({"charge": 284544, "ceiling": 249000, "fits": False})
        with pytest.raises(price.TooHeavyForTheDoor) as heavy:
            await price.priced_or_too_heavy(KWARGS, url="http://h/price", client=client)
        assert heavy.value.charge == 284544
        assert heavy.value.ceiling == 249000
        assert heavy.value.source == "handle"
        assert "284544" in str(heavy.value) and "249000" in str(heavy.value)

    @pytest.mark.asyncio
    async def test_no_verdict_is_not_too_heavy(self):
        _seen, client = _handle(httpx.ConnectError("down"))
        assert await price.priced_or_too_heavy(KWARGS, url="http://h/price", client=client) is None


class TestTheDoorsOwnRefusal:
    def test_the_proxy_overflow_refusal_is_too_heavy(self):
        assert price.door_refused_as_too_heavy(sdk_error(400, OVERFLOW_BODY)) is True

    def test_googles_own_overflow_wording_is_too_heavy(self):
        body = {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "The input token count (1196265) exceeds the maximum number of tokens allowed (1048575).",
            }
        }
        assert price.door_refused_as_too_heavy(sdk_error(400, body)) is True

    def test_a_quota_refusal_is_not(self):
        assert price.door_refused_as_too_heavy(sdk_error(429, QUOTA_BODY)) is False

    def test_another_400_is_not(self):
        body = {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "Request contains an invalid argument.",
            }
        }
        assert price.door_refused_as_too_heavy(sdk_error(400, body)) is False

    def test_the_numbers_are_read_off_the_refusal(self):
        heavy = price.too_heavy_from_the_door(sdk_error(400, OVERFLOW_BODY))
        assert (heavy.charge, heavy.ceiling, heavy.source) == (284544, 249000, "door")


class TestWaitingDoesNotHelp:
    def test_the_handles_verdict_is_not_curable(self):
        assert persistence.curable(price.TooHeavyForTheDoor(284544, 249000, "handle")) is None

    def test_the_doors_refusal_in_the_new_shape_is_not_curable(self):
        assert persistence.curable(sdk_error(400, OVERFLOW_BODY)) is None
