# piqnyx: a Phase 2 step too heavy for the door is cut in halves by whole turns (PLAN-gorizont, 3б).
# SPDX-License-Identifier: AGPL-3.0
"""Running a Phase 2 step in parts the door takes.

A part is tried as it is. When the price handle or the door says it is too heavy
(`TooHeavyForTheDoor`), it is cut in two halves at a turn boundary -- a turn is a
user's question with everything up to the next question, tool transport included
(`retention.build_turns`) -- and the halves are tried one after another, left to
right, each cut again if still too heavy.

A single turn that is too heavy on its own goes on being cut (PLAN-gorizont 3ж):
first in its lighter form if the step has one (the summary's: tool outputs cut to
the previews the archive keeps), then in halves by messages, then one message in
halves by its text, as deep as it takes. A piece no bigger than
`SMALLEST_PIECE_CHARS` that is still too heavy is not the data's fault but the
ceiling's: that is a permanent failure of the step (`TurnTooHeavy`), and the parts
done before it stay done. A message cut by text counts done only when all its
pieces are.

A part the door refuses for its content (the step says which errors mean that,
`refused`) is cut the same way down to one message, and that message is left out
with a record (`on_refused`): the others go on. More than `REFUSED_CAP` such
messages in one run is not the content's fault either (`RefusedBeyondMeasure`).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from openviking.message import Message
from openviking.message.part import ContextPart, TextPart, ToolPart
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
# How a single turn is cut further: "lighter", "messages" or "text".
OnCutInside = Callable[[List[Message], str, BaseException], Awaitable[None]]
Lighter = Callable[[List[Message]], Optional[List[Message]]]
Refused = Callable[[BaseException], bool]
OnRefused = Callable[[Message, BaseException], Awaitable[None]]

# A piece of one message this small that the door still refuses by weight is not the
# data's fault: the ceiling or the prompt around it is broken.
SMALLEST_PIECE_CHARS = 1000
# Messages refused for their content in one run beyond this number: not the content's fault.
REFUSED_CAP = 3


class TurnTooHeavy(Exception):
    """Nothing left to cut, and the door still refuses the weight."""

    def __init__(self, message_ids: List[str], heavy: TooHeavyForTheDoor) -> None:
        self.message_ids = list(message_ids)
        self.heavy = heavy
        first = self.message_ids[0] if self.message_ids else "?"
        super().__init__(
            f"a piece of one message ({first}) no bigger than {SMALLEST_PIECE_CHARS} characters "
            f"is still too heavy for the door: {heavy}"
        )


class RefusedBeyondMeasure(Exception):
    """More messages refused for their content in one run than the content can explain."""

    def __init__(self, message_ids: List[str], error: BaseException) -> None:
        self.message_ids = list(message_ids)
        self.error = error
        super().__init__(
            f"{len(self.message_ids)} messages refused in one run (cap {REFUSED_CAP}), "
            f"the last {self.message_ids[-1] if self.message_ids else '?'}: {error}"
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


def halves_by_messages(messages: List[Message]) -> Optional[Tuple[List[Message], List[Message]]]:
    """The messages cut in two at the middle, by count; None when there is one or none."""
    if len(messages) < 2:
        return None
    cut = len(messages) // 2
    return list(messages[:cut]), list(messages[cut:])


def _text_of(part: Any) -> Optional[str]:
    if isinstance(part, TextPart):
        return part.text or ""
    if isinstance(part, ToolPart):
        return part.tool_output or ""
    if isinstance(part, ContextPart):
        return part.abstract or ""
    return None


def _with_text(part: Any, text: str) -> None:
    if isinstance(part, TextPart):
        part.text = text
    elif isinstance(part, ToolPart):
        part.tool_output = text
    elif isinstance(part, ContextPart):
        part.abstract = text


def piece_chars(message: Message) -> int:
    """How much text a message carries: its text parts, tool outputs and context abstracts."""
    return sum(len(_text_of(part) or "") for part in message.parts)


def _copy_with_parts(message: Message, parts: List[Any]) -> Message:
    copy = Message.from_dict(message.to_dict())
    copy.parts = list(parts)
    return copy


def halves_by_text(message: Message) -> Optional[Tuple[Message, Message]]:
    """One message cut in two pieces that carry the same id: by its parts when it has
    several, else by the text of its one part; None when there is nothing to cut."""
    parts = list(message.parts)
    if len(parts) >= 2:
        cut = len(parts) // 2
        return _copy_with_parts(message, parts[:cut]), _copy_with_parts(message, parts[cut:])
    if not parts:
        return None
    text = _text_of(parts[0])
    if text is None or len(text) < 2:
        return None
    cut = len(text) // 2
    left = Message.from_dict(message.to_dict())
    right = Message.from_dict(message.to_dict())
    _with_text(left.parts[0], text[:cut])
    _with_text(right.parts[0], text[cut:])
    return left, right


def _first(part: List[Message]) -> str:
    return part[0].id if part else "?"


async def run_in_parts(
    messages: List[Message],
    run_part: RunPart,
    *,
    on_part_done: Optional[OnPartDone] = None,
    on_split: Optional[OnSplit] = None,
    on_cut_inside: Optional[OnCutInside] = None,
    lighter: Optional[Lighter] = None,
    refused: Optional[Refused] = None,
    on_refused: Optional[OnRefused] = None,
    operation: str = "",
) -> List[Any]:
    """Run `run_part` on the messages, cutting wherever the door refuses the weight or
    the content; the results of the parts that passed, in order."""
    results: List[Any] = []
    refused_ids: List[str] = []
    name = operation or "step"

    async def done(part: List[Message], result: Any, mark: bool) -> None:
        results.append(result)
        if mark and on_part_done is not None:
            await on_part_done(part, result)

    async def go(part: List[Message], mark: bool = True) -> None:
        try:
            result = await run_part(part)
        except TooHeavyForTheDoor as heavy:
            await cut(part, heavy, mark)
            return
        except Exception as error:
            if door_refused_as_too_heavy(error):
                await cut(part, too_heavy_from_the_door(error), mark)
                return
            if refused is not None and refused(error):
                await isolate(part, error, mark)
                return
            raise
        await done(part, result, mark)

    async def cut(part: List[Message], heavy: TooHeavyForTheDoor, mark: bool) -> None:
        halves = halves_by_turns(part)
        if halves is None:
            await one_turn(part, heavy, mark)
            return
        logger.info(
            "%s: %d messages too heavy for the door (%s); cut in halves of %d and %d by turns",
            name,
            len(part),
            heavy,
            len(halves[0]),
            len(halves[1]),
        )
        if on_split is not None:
            await on_split(part, heavy)
        await go(halves[0], mark)
        await go(halves[1], mark)

    async def one_turn(part: List[Message], heavy: TooHeavyForTheDoor, mark: bool) -> None:
        """A single turn above the ceiling: its lighter form, then halves by messages."""
        light = lighter(part) if lighter is not None else None
        if light is not None:
            logger.info(
                "%s: one turn alone (%d messages, first %s) is too heavy for the door (%s); "
                "tried in its lighter form",
                name,
                len(part),
                _first(part),
                heavy,
            )
            if on_cut_inside is not None:
                await on_cut_inside(part, "lighter", heavy)
            try:
                result = await run_part(light)
            except TooHeavyForTheDoor as still:
                heavy, part = still, light
            except Exception as error:
                if door_refused_as_too_heavy(error):
                    heavy, part = too_heavy_from_the_door(error), light
                elif refused is not None and refused(error):
                    await isolate(light, error, mark)
                    return
                else:
                    raise
            else:
                await done(part, result, mark)
                return
        await by_messages(part, heavy, mark)

    async def by_messages(part: List[Message], heavy: TooHeavyForTheDoor, mark: bool) -> None:
        halves = halves_by_messages(part)
        if halves is None:
            await by_text(part[0], heavy, mark)
            return
        logger.info(
            "%s: one turn alone (%d messages, first %s) is too heavy for the door (%s); "
            "cut in halves of %d and %d by messages",
            name,
            len(part),
            _first(part),
            heavy,
            len(halves[0]),
            len(halves[1]),
        )
        if on_cut_inside is not None:
            await on_cut_inside(part, "messages", heavy)
        await go(halves[0], mark)
        await go(halves[1], mark)

    async def by_text(message: Message, heavy: TooHeavyForTheDoor, mark: bool) -> None:
        pieces = halves_by_text(message)
        if pieces is None or piece_chars(message) <= SMALLEST_PIECE_CHARS:
            raise TurnTooHeavy([message.id], heavy) from heavy
        logger.info(
            "%s: one message (%s, %d characters) is too heavy for the door (%s); cut in two "
            "pieces by its text",
            name,
            message.id,
            piece_chars(message),
            heavy,
        )
        if on_cut_inside is not None:
            await on_cut_inside([message], "text", heavy)
        await go([pieces[0]], mark=False)
        await go([pieces[1]], mark=False)
        if mark and on_part_done is not None:
            await on_part_done([message], None)

    async def isolate(part: List[Message], error: BaseException, mark: bool) -> None:
        """A part refused for its content: cut down to the one message that earned it."""
        halves = halves_by_turns(part) or halves_by_messages(part)
        if halves is not None:
            logger.info(
                "%s: %d messages refused by the door (%s); cut in halves of %d and %d to find "
                "the one refused",
                name,
                len(part),
                error,
                len(halves[0]),
                len(halves[1]),
            )
            await go(halves[0], mark)
            await go(halves[1], mark)
            return
        message = part[0]
        refused_ids.append(message.id)
        if len(refused_ids) > REFUSED_CAP:
            raise RefusedBeyondMeasure(refused_ids, error) from error
        logger.warning(
            "%s: message %s refused by the door (%s); left out, it stays in the archive",
            name,
            message.id,
            error,
        )
        if on_refused is not None:
            await on_refused(message, error)
        if mark and on_part_done is not None:
            await on_part_done(part, None)

    await go(list(messages))
    return results
