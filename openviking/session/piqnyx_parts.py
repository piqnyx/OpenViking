# piqnyx: a Phase 2 step too heavy for the door is cut in halves by whole turns (PLAN-gorizont, 3б).
# SPDX-License-Identifier: AGPL-3.0
"""Running a Phase 2 step in parts the door takes.

A part is tried as it is. When the price handle or the door says it is too heavy
(`TooHeavyForTheDoor`), it is cut in two halves at a turn boundary -- a turn is a
user's question with everything up to the next question, tool transport included
(`retention.build_turns`) -- and the halves are tried one after another, left to
right, each cut again if still too heavy. A single turn that is too heavy on its
own cannot be cut: it is tried in its lighter form if the step has one, else it is
skipped and counted done if the step allows that (PLAN-gorizont 3ж), else that is a
permanent failure of the step (`TurnTooHeavy`); the parts done before it stay done.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from openviking.message import Message
from openviking.session.retention import build_turns
from openviking.utils.piqnyx_price import (
    TooHeavyForTheDoor,
    door_refused_as_too_heavy,
    too_heavy_from_the_door,
)

logger = logging.getLogger(__name__)

RunPart = Callable[[List[Message]], Awaitable[Any]]
OnPartDone = Callable[[List[Message], Any], Awaitable[None]]
OnSplit = Callable[[List[Message], TooHeavyForTheDoor], Awaitable[None]]
# piqnyx (PLAN-gorizont 3ж): a lighter form of one turn that is too heavy on its own (the
# summary's: tool outputs cut to the previews the archive keeps), or None when there is
# nothing to lighten; and what to do when even that is too heavy -- told, the turn is
# skipped and counted done, so the archive does not fail for good over one turn.
Lighter = Callable[[List[Message]], Optional[List[Message]]]
OnTurnSkipped = Callable[[List[Message], TooHeavyForTheDoor], Awaitable[None]]


class TurnTooHeavy(Exception):
    """One turn alone is above the ceiling: nothing left to cut."""

    def __init__(self, message_ids: List[str], heavy: TooHeavyForTheDoor) -> None:
        self.message_ids = list(message_ids)
        self.heavy = heavy
        first = self.message_ids[0] if self.message_ids else "?"
        super().__init__(
            f"one turn alone ({len(self.message_ids)} messages, first {first}) is too heavy "
            f"for the door: {heavy}"
        )


def halves_by_turns(messages: List[Message]) -> Optional[Tuple[List[Message], List[Message]]]:
    """The messages cut in two at the turn boundary nearest the middle, by turn count;
    None when there is only one turn (or nothing) to cut."""
    turns = build_turns(messages)
    if len(turns) < 2:
        return None
    cut = len(turns) // 2
    left = [message for turn in turns[:cut] for message in turn.messages]
    right = [message for turn in turns[cut:] for message in turn.messages]
    return left, right


async def run_in_parts(
    messages: List[Message],
    run_part: RunPart,
    *,
    on_part_done: Optional[OnPartDone] = None,
    on_split: Optional[OnSplit] = None,
    lighter: Optional[Lighter] = None,
    on_turn_skipped: Optional[OnTurnSkipped] = None,
    operation: str = "",
) -> List[Any]:
    """Run `run_part` on the messages, cutting by turns wherever the door refuses the
    weight; the results of the parts that passed, in order."""
    results: List[Any] = []

    async def one_turn(part: List[Message], heavy: TooHeavyForTheDoor) -> None:
        """A single turn above the ceiling: its lighter form, then a skip, then failure."""
        first = part[0].id if part else "?"
        light = lighter(part) if lighter is not None else None
        if light is not None:
            logger.info(
                "%s: one turn alone (%d messages, first %s) is too heavy for the door (%s); "
                "tried in its lighter form",
                operation or "step",
                len(part),
                first,
                heavy,
            )
            try:
                result = await run_part(light)
            except TooHeavyForTheDoor as still:
                heavy = still
            except Exception as error:
                if not door_refused_as_too_heavy(error):
                    raise
                heavy = too_heavy_from_the_door(error)
            else:
                results.append(result)
                if on_part_done is not None:
                    await on_part_done(part, result)
                return
        if on_turn_skipped is None:
            raise TurnTooHeavy([message.id for message in part], heavy) from heavy
        logger.warning(
            "%s: one turn alone (%d messages, first %s) is too heavy for the door (%s); "
            "skipped, its messages stay in the archive",
            operation or "step",
            len(part),
            first,
            heavy,
        )
        await on_turn_skipped(part, heavy)
        if on_part_done is not None:
            await on_part_done(part, None)

    async def cut(part: List[Message], heavy: TooHeavyForTheDoor) -> None:
        halves = halves_by_turns(part)
        if halves is None:
            await one_turn(part, heavy)
            return
        logger.info(
            "%s: %d messages too heavy for the door (%s); cut in halves of %d and %d by turns",
            operation or "step",
            len(part),
            heavy,
            len(halves[0]),
            len(halves[1]),
        )
        if on_split is not None:
            await on_split(part, heavy)
        await go(halves[0])
        await go(halves[1])

    async def go(part: List[Message]) -> None:
        try:
            result = await run_part(part)
        except TooHeavyForTheDoor as heavy:
            await cut(part, heavy)
            return
        except Exception as error:
            if door_refused_as_too_heavy(error):
                await cut(part, too_heavy_from_the_door(error))
                return
            raise
        results.append(result)
        if on_part_done is not None:
            await on_part_done(part, result)

    await go(list(messages))
    return results
