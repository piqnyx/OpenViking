# piqnyx: Phase 2 asks the proxy's price handle before every request (PLAN-gorizont, 3б).
# SPDX-License-Identifier: AGPL-3.0
"""The proxy's price handle, and the verdict «too heavy for the door».

gemini-proxy refuses by itself any request heavier than one key's minute ceiling,
and its `POST /price` makes the very same count without sending. A Phase 2 step
asks it with the body it is about to send; `fits` false means the door would
refuse, so the step cuts its part by whole turns instead of sending
(`openviking/session/piqnyx_parts.py`). The door's own refusal -- Google's
overflow shape, «The input token count (N) exceeds the maximum number of tokens
allowed (M)», with the proxy's note after it -- is the same verdict arriving late.

A handle that cannot answer (down, 5xx, no JSON) is no verdict at all: the step
goes on without a price, and the proxy's gate is still there to refuse.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

ENV_PRICE_URL = "OPENVIKING_PHASE2_PRICE_URL"
DEFAULT_PRICE_URL = "http://127.0.0.1:8787/price"
DEFAULT_TIMEOUT_SECONDS = 180.0

# Google's overflow wording, which the proxy borrows for its own refusal.
_OVERFLOW_RE = re.compile(
    r"input token count \((\d+)\) exceeds the maximum number of tokens allowed \((\d+)\)",
    re.IGNORECASE,
)
# The proxy's own note after it; the wording the proxy writes (gemini-proxy.py, 1б).
_PROXY_NOTE = "[gemini-proxy] this single request weighs"


class TooHeavyForTheDoor(Exception):
    """The request is above the ceiling of any one key: cut it, do not wait."""

    def __init__(
        self, charge: Optional[int], ceiling: Optional[int], source: str, note: str = ""
    ) -> None:
        self.charge = charge
        self.ceiling = ceiling
        self.source = source
        super().__init__(
            f"request of {charge if charge is not None else '?'} tokens by the counter is above "
            f"the door's ceiling of {ceiling if ceiling is not None else '?'} "
            f"(said by the {source}); cut it by turns{note}"
        )


def price_url() -> Optional[str]:
    """Where the handle is: the environment's word, the proxy on this machine by
    default, nothing (pricing off) when the environment names an empty value."""
    raw = os.environ.get(ENV_PRICE_URL)
    if raw is None:
        return DEFAULT_PRICE_URL
    value = raw.strip()
    return value or None


async def price(
    kwargs: Dict[str, Any],
    *,
    url: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Optional[Dict[str, Any]]:
    """The handle's answer for this body as it is, or None when there is no verdict."""
    target = url if url is not None else price_url()
    if not target:
        return None
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as own:
                response = await own.post(target, json=kwargs)
        else:
            response = await client.post(target, json=kwargs, timeout=timeout)
    except Exception as trouble:  # the road to the handle, whatever broke on it
        logger.warning(
            "price handle %s gave no answer (%s); sending without a price", target, trouble
        )
        return None
    if response.status_code != 200:
        logger.warning(
            "price handle %s answered %s: %s; sending without a price",
            target,
            response.status_code,
            response.text[:300],
        )
        return None
    try:
        answer = response.json()
    except ValueError:
        logger.warning("price handle %s answered no JSON; sending without a price", target)
        return None
    if not isinstance(answer, dict) or "fits" not in answer:
        logger.warning(
            "price handle %s answered without a verdict; sending without a price", target
        )
        return None
    return answer


def _as_int(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


async def priced_or_too_heavy(
    kwargs: Dict[str, Any],
    *,
    url: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Dict[str, Any]]:
    """The handle's answer when the body fits; TooHeavyForTheDoor when it does not;
    None when the handle gave no verdict."""
    answer = await price(kwargs, url=url, client=client)
    if answer is None:
        return None
    if answer.get("fits") is False:
        raise TooHeavyForTheDoor(
            _as_int(answer.get("charge")), _as_int(answer.get("ceiling")), "handle"
        )
    return answer


def _texts_of(error: BaseException) -> List[str]:
    """The error's words, its body's words, and its causes', lower-cased."""
    texts: List[str] = []
    seen = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen and len(texts) < 16:
        seen.add(id(current))
        texts.append(str(current).lower())
        body = getattr(current, "body", None)
        if isinstance(body, dict):
            inner = body.get("error") if isinstance(body.get("error"), dict) else body
            message = inner.get("message") if isinstance(inner, dict) else None
            if isinstance(message, str):
                texts.append(message.lower())
        current = current.__cause__ or current.__context__
    return texts


def _status_of(error: BaseException) -> Optional[int]:
    for name in ("status_code", "status"):
        value = getattr(error, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def door_refused_as_too_heavy(error: BaseException) -> bool:
    """Whether the door (or the proxy in its place) refused the request as an overflow."""
    status = _status_of(error)
    if status is not None and status != 400:
        return False
    return any(_OVERFLOW_RE.search(text) or _PROXY_NOTE in text for text in _texts_of(error))


def too_heavy_from_the_door(error: BaseException) -> TooHeavyForTheDoor:
    """The verdict with the numbers the refusal carries."""
    charge: Optional[int] = None
    ceiling: Optional[int] = None
    for text in _texts_of(error):
        found = _OVERFLOW_RE.search(text)
        if found:
            charge, ceiling = int(found.group(1)), int(found.group(2))
            break
    return TooHeavyForTheDoor(charge, ceiling, "door")
