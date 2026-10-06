# piqnyx: a Phase 2 step too heavy for the door is cut in halves by whole turns (PLAN-gorizont, 3б).
# SPDX-License-Identifier: AGPL-3.0
"""Cutting a step's messages into parts the door takes, by whole turns.

A part is tried as it is; when the handle or the door says it is too heavy, it is
cut in two halves at a turn boundary and each half is tried in turn, left to right.
A single turn that is too heavy on its own cannot be cut and is a permanent failure
of that part; the parts before it stay done.
"""

import pytest

from openviking.message import Message, TextPart, ToolPart
from openviking.session import piqnyx_parts as parts
from openviking.utils.piqnyx_price import TooHeavyForTheDoor


def user(i: str) -> Message:
    return Message(id=f"u{i}", role="user", parts=[TextPart(f"question {i}")])


def assistant(i: str) -> Message:
    return Message(id=f"a{i}", role="assistant", parts=[TextPart(f"answer {i}")])


def transport(i: str) -> Message:
    return Message(
        id=f"t{i}",
        role="user",
        parts=[
            ToolPart(tool_name="read", tool_input="x", tool_output="y", tool_status="completed")
        ],
    )


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


class TestHalvesByTurns:
    def test_four_turns_are_cut_two_and_two(self):
        left, right = parts.halves_by_turns(FOUR_TURNS)
        assert ids(left) == ["u1", "a1", "u2", "a2"]
        assert ids(right) == ["u3", "a3", "u4", "a4"]

    def test_a_tool_transport_stays_with_its_turn(self):
        messages = [
            user(1),
            assistant(1),
            transport(1),
            assistant("1b"),
            user(2),
            assistant(2),
            user(3),
        ]
        left, right = parts.halves_by_turns(messages)
        assert ids(left) == ["u1", "a1", "t1", "a1b"]
        assert ids(right) == ["u2", "a2", "u3"]

    def test_three_turns_are_cut_one_and_two(self):
        left, right = parts.halves_by_turns(FOUR_TURNS[:6])
        assert ids(left) == ["u1", "a1"]
        assert ids(right) == ["u2", "a2", "u3", "a3"]

    def test_one_turn_cannot_be_cut(self):
        assert parts.halves_by_turns([user(1), assistant(1), transport(1), assistant("1b")]) is None

    def test_a_leading_answer_without_a_question_is_a_turn_of_its_own(self):
        left, right = parts.halves_by_turns([assistant(0), user(1), assistant(1)])
        assert ids(left) == ["a0"]
        assert ids(right) == ["u1", "a1"]

    def test_nothing_cannot_be_cut(self):
        assert parts.halves_by_turns([]) is None


def heavy_when(limit: int):
    """A step that the door takes only when the part has at most `limit` messages."""
    calls = []

    async def run(part):
        calls.append(ids(part))
        if len(part) > limit:
            raise TooHeavyForTheDoor(len(part) * 1000, limit * 1000, "handle")
        return f"done {len(part)}"

    return calls, run


class TestRunInParts:
    @pytest.mark.asyncio
    async def test_a_part_that_fits_runs_as_it_is(self):
        calls, run = heavy_when(8)
        assert await parts.run_in_parts(FOUR_TURNS, run) == ["done 8"]
        assert calls == [ids(FOUR_TURNS)]

    @pytest.mark.asyncio
    async def test_too_heavy_is_cut_in_halves_until_each_half_fits(self):
        calls, run = heavy_when(4)
        done = []

        async def record(part, result):
            done.append((ids(part), result))

        results = await parts.run_in_parts(FOUR_TURNS, run, on_part_done=record)
        assert calls == [ids(FOUR_TURNS), ["u1", "a1", "u2", "a2"], ["u3", "a3", "u4", "a4"]]
        assert results == ["done 4", "done 4"]
        assert done == [(["u1", "a1", "u2", "a2"], "done 4"), (["u3", "a3", "u4", "a4"], "done 4")]

    @pytest.mark.asyncio
    async def test_halves_are_cut_again_when_still_too_heavy(self):
        calls, run = heavy_when(2)
        await parts.run_in_parts(FOUR_TURNS, run)
        assert calls[0] == ids(FOUR_TURNS)
        # The leaves, in order; the halves of four are tried (and refused) between them.
        assert [part for part in calls if len(part) == 2] == [
            ["u1", "a1"],
            ["u2", "a2"],
            ["u3", "a3"],
            ["u4", "a4"],
        ]

    @pytest.mark.asyncio
    async def test_a_single_turn_too_heavy_is_a_permanent_failure_with_the_parts_before_kept(self):
        calls = []
        done = []

        async def run(part):
            calls.append(ids(part))
            if "u3" in ids(part):
                raise TooHeavyForTheDoor(300000, 249000, "door")
            return "ok"

        async def record(part, _result):
            done.append(ids(part))

        with pytest.raises(parts.TurnTooHeavy) as too_heavy:
            await parts.run_in_parts(FOUR_TURNS, run, on_part_done=record)
        assert too_heavy.value.message_ids == ["u3", "a3"]
        assert "300000" in str(too_heavy.value) and "249000" in str(too_heavy.value)
        # The half before the heavy turn was done and recorded; nothing after it ran.
        assert done == [["u1", "a1", "u2", "a2"]]
        assert ["u4", "a4"] not in calls

    @pytest.mark.asyncio
    async def test_other_trouble_passes_through_untouched(self):
        async def run(_part):
            raise ValueError("broken json")

        with pytest.raises(ValueError, match="broken json"):
            await parts.run_in_parts(FOUR_TURNS, run)

    @pytest.mark.asyncio
    async def test_every_cut_is_told(self):
        _calls, run = heavy_when(4)
        told = []

        async def on_split(part, heavy):
            told.append((len(part), heavy.charge, heavy.ceiling))

        await parts.run_in_parts(FOUR_TURNS, run, on_split=on_split)
        assert told == [(8, 8000, 4000)]
