# piqnyx: a Phase 2 step is retried in place until it succeeds (PIQNYX.md, stage 1).
# SPDX-License-Identifier: AGPL-3.0
"""A step of session commit Phase 2 that waiting can cure is repeated until it passes.

Upstream repeats a failed step sixteen times within minutes and then marks the
archive failed. A storm at the model's door lasts hours (28.09.2026: four), and
a failed newest archive takes the session's summary out of the context until a
later commit mends it. Here the step is repeated in place instead, the wait
doubling from two seconds to fifteen minutes, for as long as it takes -- the
way our graphiti does it (`piqnyx_reliable_queue.py`). The archive stays
pending meanwhile, and a pending archive keeps the context whole: the last
closed summary plus its own raw messages.

Only what waiting can cure is repeated: the door or the road to it. A request
the door will never take -- refused for its content, too large, malformed -- and
a fault in the code itself are raised at once, as upstream does, and the
archive is marked failed. They must be: the commit queue has four places for
every session of every agent, and an archive that can never pass would in time
hold all four.

Which answers count as curable is decided on measured texts only
(PIQNYX.md, "Что сервер делает с ответами нашего прокси").
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any, Awaitable, Callable, List, Optional, Tuple, TypeVar

from openviking.utils.model_retry import (
    _PERMANENT_IO_ERRORS,
    ERROR_CLASS_AUTH,
    ERROR_CLASS_CONTENT_SAFETY,
    ERROR_CLASS_PERMANENT,
    ERROR_CLASS_QUOTA_EXCEEDED,
    ERROR_CLASS_TRANSIENT,
    ERROR_CLASS_UNKNOWN,
    classify_api_error,
)
from openviking.utils.piqnyx_price import TooHeavyForTheDoor, door_refused_as_too_heavy

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_BASE_SECONDS = 2.0
DEFAULT_MAX_SECONDS = 900.0
ENV_BASE_SECONDS = "OPENVIKING_PHASE2_RETRY_BASE_SECONDS"
ENV_MAX_SECONDS = "OPENVIKING_PHASE2_RETRY_MAX_SECONDS"

# gemini-proxy refuses by itself a request heavier than one key's minute ceiling.
# Until 06.10.2026 it wore the shape of a quota refusal; since PLAN-gorizont 1б it
# wears Google's overflow shape (`door_refused_as_too_heavy`). No wait makes the
# request lighter: it is cut by turns instead (`piqnyx_parts`).
_TOO_HEAVY_MARKERS = ("no key could serve it",)

# The door will not serve the address the request left from. It comes as a 400,
# yet it is about the exit, not the request: the proxy turns the exit meanwhile.
_EXIT_REFUSED_MARKERS = ("user location is not supported",)

# Trouble on the road that upstream's classifier does not know by its words.
_NETWORK_MARKERS = (
    "timed out",
    "timeout",
    "peer closed connection",
    "incomplete chunked read",
    "server disconnected",
)
_NETWORK_ERRORS = (asyncio.TimeoutError, TimeoutError, ConnectionError)

# Read through the module, so that a test can stand a clock in its place.
_sleep = asyncio.sleep


def _chain(error: BaseException) -> List[BaseException]:
    """The error and what caused it, nearest first."""
    chain: List[BaseException] = []
    seen = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen and len(chain) < 8:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def curable(error: BaseException) -> Optional[str]:
    """The kind of trouble waiting can cure, or None when waiting will not help."""
    chain = _chain(error)
    if any(isinstance(item, TooHeavyForTheDoor) for item in chain):
        return None
    if door_refused_as_too_heavy(error):
        return None
    texts = [str(item).lower() for item in chain]

    if any(marker in text for text in texts for marker in _TOO_HEAVY_MARKERS):
        return None

    error_class = classify_api_error(error)  # type: ignore[arg-type]
    if error_class == ERROR_CLASS_TRANSIENT:
        return "provider_unavailable"
    if error_class == ERROR_CLASS_QUOTA_EXCEEDED:
        return "quota"
    if error_class == ERROR_CLASS_AUTH:
        return "key_or_exit"
    if error_class == ERROR_CLASS_PERMANENT:
        if any(marker in text for text in texts for marker in _EXIT_REFUSED_MARKERS):
            return "exit_refused"
        return None
    if error_class == ERROR_CLASS_UNKNOWN:
        if any(isinstance(item, _NETWORK_ERRORS) for item in chain):
            return "network"
        if any(marker in text for text in texts for marker in _NETWORK_MARKERS):
            return "network"
        return None
    # Refused for its content, or too large for the model: the request itself.
    return None


def retry_delay(failed_attempts: int, base: float, cap: float) -> float:
    """The wait after this many failed attempts: `base` doubling, never over `cap`."""
    if base <= 0 or cap <= 0:
        return 0.0
    # The exponent is capped before it is raised: after a long outage 2**attempts
    # overflows although the wait it stands for was capped long before.
    largest = max(0, math.ceil(math.log2(cap) - math.log2(base)))
    exponent = min(max(int(failed_attempts) - 1, 0), largest)
    return min(math.ldexp(base, exponent), cap)


def retry_settings() -> Tuple[float, float]:
    """(base, cap) in seconds, from the environment; the defaults when it is unusable."""
    raw_base = os.environ.get(ENV_BASE_SECONDS, "").strip()
    raw_cap = os.environ.get(ENV_MAX_SECONDS, "").strip()
    if not raw_base and not raw_cap:
        return DEFAULT_BASE_SECONDS, DEFAULT_MAX_SECONDS
    try:
        base = float(raw_base) if raw_base else DEFAULT_BASE_SECONDS
        cap = float(raw_cap) if raw_cap else DEFAULT_MAX_SECONDS
    except ValueError:
        base = cap = float("nan")
    if not (math.isfinite(base) and math.isfinite(cap)) or base < 0 or cap < 0 or cap < base:
        logger.warning(
            "%s=%r and %s=%r cannot be used (numbers, not negative, the second not under the "
            "first); waiting %.0f to %.0f seconds",
            ENV_BASE_SECONDS,
            raw_base,
            ENV_MAX_SECONDS,
            raw_cap,
            DEFAULT_BASE_SECONDS,
            DEFAULT_MAX_SECONDS,
        )
        return DEFAULT_BASE_SECONDS, DEFAULT_MAX_SECONDS
    return base, cap


def queue_survives_a_stop(read_config: Callable[[], Any]) -> bool:
    """Whether a job taken from the commit queue is handed out again after a restart.

    Only then may an archive be left pending when the server is stopped in the
    middle of its Phase 2. A queue kept in memory forgets the job, nothing would
    ever take the archive up again, and the archives after it wait for it without
    an end. Anything that cannot be read is taken for a queue that forgets.
    """
    try:
        backend = read_config().storage.agfs.queuefs.backend
    except Exception:
        return False
    return isinstance(backend, str) and backend.strip().lower() in ("sqlite", "sqlite3")


class UnusableAnswer(ValueError):
    """The model answered, and the answer could not be used: no tool call where one was
    forced, arguments of the wrong shape, an empty required field. Not the door's doing:
    repeated a few times at once, then a refusal of that content (PLAN-gorizont 3ж)."""


def refused_for_its_content(error: BaseException) -> bool:
    """Whether a step failed over the request's own content: a moderation refusal, a 400
    that is not a weight, or an answer of the model's that could not be used. Cutting the
    part isolates it and waiting cannot cure it. A weight is cut by weight instead; a file
    error or a bug of ours is nobody's content and goes up as it is (PLAN-gorizont 3ж)."""
    chain = _chain(error)
    if any(isinstance(item, UnusableAnswer) for item in chain):
        return True
    if any(isinstance(item, (TooHeavyForTheDoor, *_PERMANENT_IO_ERRORS)) for item in chain):
        return False
    if door_refused_as_too_heavy(error):
        return False
    texts = [str(item).lower() for item in chain]
    if any(marker in text for text in texts for marker in _TOO_HEAVY_MARKERS):
        return False
    error_class = classify_api_error(error)  # type: ignore[arg-type]
    return error_class in (ERROR_CLASS_CONTENT_SAFETY, ERROR_CLASS_PERMANENT)


async def until_cured(
    fn: Callable[[], Awaitable[T]],
    *,
    operation: str,
    settings: Optional[Tuple[float, float]] = None,
    on_wait: Optional[Callable[[int, str, float, BaseException], Awaitable[Any]]] = None,
) -> T:
    """Run `fn` and repeat it for as long as it fails with trouble waiting can cure.

    `on_wait(failed_attempts, kind, wait, error)` is told before every wait; if it
    breaks, that is written down and the repeats go on. Trouble waiting cannot
    cure is raised as it came. A cancel goes through at once, from the step and
    from the wait alike.
    """
    failed_attempts = 0
    while True:
        try:
            result = await fn()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            kind = curable(error)
            if kind is None:
                raise
            failed_attempts += 1
            base, cap = settings if settings is not None else retry_settings()
            wait = retry_delay(failed_attempts, base, cap)
            logger.warning(
                "%s failed (%s), attempt %d; repeating in %.0f s: %s",
                operation,
                kind,
                failed_attempts,
                wait,
                error,
            )
            if on_wait is not None:
                try:
                    await on_wait(failed_attempts, kind, wait, error)
                except asyncio.CancelledError:
                    raise
                except Exception as trouble:
                    logger.warning(
                        "%s: could not tell of the wait, repeating all the same: %s",
                        operation,
                        trouble,
                    )
            await _sleep(wait)
            continue
        if failed_attempts:
            logger.info("%s passed after %d failed attempts", operation, failed_attempts)
        return result
