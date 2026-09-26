"""Tests of the shared bounded-wait primitive of the realtime layer (wp-1).

Covers, offline and deterministically:

* :func:`wait_bound` -- the exact head-room (5 % + 50 ms) and the strict inequality
  that keeps a bound from colliding with the wait it wraps;
* :func:`paced_wait` -- the normal path, the *deterministic* frozen-loop path (an
  aggressor queued before the pacing wait blocks the loop with ``time.sleep`` past
  the bound, which is the crash reproduced in isolation), the ``on_delayed``
  reporting, the clamping of a negative delay and the propagation of a caller
  cancellation with no orphan sleeper left behind;
* :func:`awaited_within` -- the value, the call's own failure, a *late* answer
  returned instead of a timeout, the named :class:`TimeoutError` of a call that
  never answers, and the cancellation of the inner call;
* :func:`~trading_platform.realtime.observability.failure_text` -- the failure text
  that is never empty, which is what removes the persisted ``"TimeoutError: "``.

The freeze is driven explicitly (a known blocking duration queued in front of the
pacing wait), never by wall-clock luck: no test of this file depends on a race.
Every coroutine is awaited through :func:`run`, which wraps it in
``asyncio.wait_for(..., timeout=5)`` so a hung implementation fails the suite
instead of hanging it.  No network, no import of ``stream``/``runner``/
``orchestrator`` -- this module is the lowest layer and stays testable on its own.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from trading_platform.core.errors import MarketStreamError
from trading_platform.realtime.clock import ManualClock, SystemClock
from trading_platform.realtime.observability import failure_text
from trading_platform.realtime.waits import (
    LATE_GRACE_SECONDS,
    PACING_MARGIN_FLOOR_SECONDS,
    PACING_MARGIN_RATIO,
    awaited_within,
    paced_wait,
    wait_bound,
)

#: Every await of this module is bounded by this explicit timeout.
TIMEOUT = 5.0

#: Pacing delay of the freeze tests: its bound (0.26 s) is overdue while the loop
#: is frozen, which is exactly the production mechanism.
FREEZE_DELAY = 0.2

#: How long the aggressor freezes the whole event loop with a blocking call.
#: Longer than the bound of :data:`FREEZE_DELAY`, so a naive bounded wait expires.
FREEZE_SECONDS = 0.3


def run(coro: Any) -> Any:
    """Run one coroutine on a fresh loop, bounded by :data:`TIMEOUT`."""
    return asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


async def _blocking_aggressor() -> None:
    """Freeze the whole event loop the way a synchronous provider call used to.

    ``time.sleep`` is the exact shape of the defect: no other coroutine sharing the
    loop -- no other profile's idle poll -- can take a single step while it runs.
    """
    time.sleep(FREEZE_SECONDS)


async def _aggressor_freezing_after_one_step() -> None:
    """Let every peer register its bound, then freeze the loop past all of them."""
    await asyncio.sleep(0)
    time.sleep(FREEZE_SECONDS)


class RecordingClock(SystemClock):
    """A system clock that records the task of every sleep it is asked for."""

    def __init__(self) -> None:
        self.sleepers: list[asyncio.Task[Any]] = []

    async def sleep(self, seconds: float) -> None:
        """Record the running task, then sleep for ``seconds`` of real time."""
        current = asyncio.current_task()
        assert current is not None
        self.sleepers.append(current)
        await super().sleep(seconds)


# ---------------------------------------------------------------------------
# 1. wait_bound -- one definition of the head-room, no collision with the wait
# ---------------------------------------------------------------------------


def test_wait_bound_is_the_documented_margin() -> None:
    """The bound is ``wait * 1.05 + 0.05``, bit for bit."""
    assert (PACING_MARGIN_RATIO, PACING_MARGIN_FLOOR_SECONDS) == (0.05, 0.05)
    assert wait_bound(0.0) == 0.05
    assert wait_bound(15.0) == 15.8
    assert wait_bound(-1.0) == 0.05
    assert wait_bound(-1e9) == 0.05
    assert wait_bound(0.2) == 0.26
    # The two values of the live deployment: a 5 s poll interval and a 30 s one.
    assert wait_bound(5.0) == 5.3
    assert wait_bound(30.0) == 31.55


@pytest.mark.parametrize("delay", [0.0, 0.001, 0.05, 0.2, 1.0, 5.0, 15.0, 30.0, 300.0])
def test_wait_bound_is_strictly_greater_than_the_wait(delay: float) -> None:
    """A bound equal to its wait is a race, so the margin is never zero."""
    bound = wait_bound(delay)
    assert bound > delay
    assert bound - delay >= PACING_MARGIN_FLOOR_SECONDS


def test_late_grace_is_the_documented_window() -> None:
    """The grace offered to a late-but-healthy call is a documented constant."""
    assert LATE_GRACE_SECONDS == 0.25


# ---------------------------------------------------------------------------
# 2. paced_wait -- a delayed loop is never a fatal timeout
# ---------------------------------------------------------------------------


def test_paced_wait_returns_after_a_completed_sleep() -> None:
    """The nominal path returns the elapsed duration and reports no delay."""
    delayed: list[tuple[float, float]] = []

    def observe(expected: float, real: float) -> None:
        delayed.append((expected, real))

    elapsed = run(paced_wait(SystemClock(), 0.05, on_delayed=observe))
    assert elapsed >= 0.05
    assert delayed == []


def test_paced_wait_returns_the_elapsed_time_of_a_manual_clock() -> None:
    """The elapsed duration is read through the injected clock, never by this module."""
    clock = ManualClock()
    elapsed = run(paced_wait(clock, 0.4))
    assert elapsed >= 0.4
    assert clock.monotonic() == pytest.approx(0.4)


def test_paced_wait_clamps_a_negative_delay_and_never_returns_a_negative_elapsed() -> None:
    """A negative delay is clamped to zero, so the returned elapsed cannot go below it."""
    elapsed = run(paced_wait(ManualClock(), -3.0))
    assert elapsed == 0.0


def test_paced_wait_survives_a_loop_frozen_by_a_blocking_call() -> None:
    """The regression: a freeze spanning the bound does not raise ``TimeoutError``.

    The aggressor is queued *before* the pacing wait, so it runs in the window
    between the registration of the bound and the first step of the sleeper -- the
    loop is then frozen for longer than the bound, the sleeper's deadline is
    already overdue when the loop resumes, and a naive bounded wait reports the
    healthy sleep as a timeout.  ``paced_wait`` returns normally instead, and the
    delay it survived is reported through ``on_delayed``.
    """
    delayed: list[tuple[float, float]] = []

    async def main() -> float:
        blocker = asyncio.ensure_future(_blocking_aggressor())
        elapsed = await paced_wait(
            SystemClock(),
            FREEZE_DELAY,
            on_delayed=lambda expected, real: delayed.append((expected, real)),
        )
        await blocker
        return elapsed

    elapsed = run(main())
    assert elapsed >= FREEZE_SECONDS
    assert len(delayed) == 1
    assert delayed[0][0] == FREEZE_DELAY
    assert delayed[0][1] >= FREEZE_SECONDS


def test_the_same_freeze_kills_a_naive_bounded_wait() -> None:
    """The scenario above is not vacuous: it is the crash of the shipped code.

    The pre-fix polish loop bounded its sleep with :func:`asyncio.wait_for`, which
    turns exactly this freeze into the bare ``TimeoutError`` (empty message) that
    killed three healthy profiles.  Driving the same freeze through
    ``asyncio.wait_for`` fails, while :func:`paced_wait` -- the replacement --
    returns: this is the failing-first regression made explicit.
    """

    async def main() -> None:
        blocker = asyncio.ensure_future(_blocking_aggressor())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                SystemClock().sleep(FREEZE_DELAY), timeout=wait_bound(FREEZE_DELAY)
            )
        await blocker

    run(main())


@pytest.mark.parametrize("delay", [0.0, 0.05, 0.2])
def test_paced_wait_never_raises_timeout_for_any_configured_delay(delay: float) -> None:
    """No ``(poll interval, stream timeout)`` combination can make pacing fatal."""
    elapsed = run(paced_wait(SystemClock(), delay))
    assert elapsed >= delay


def test_three_profiles_share_one_frozen_loop_without_a_fatal_timeout() -> None:
    """Three healthy pacing waits, one blocking neighbour, nobody dies.

    Every bound is registered first, then the aggressor freezes the loop for longer
    than all of them: each poll was overdue when the loop resumed, which is exactly
    the shared-loop race that killed three of the five live profiles.  All three
    waits return normally -- no ``TimeoutError`` escapes, so no profile is recorded
    as ``ERROR``.
    """
    clock = SystemClock()
    delayed: list[tuple[float, float]] = []

    def observe(expected: float, real: float) -> None:
        delayed.append((expected, real))

    async def profile() -> float:
        return await paced_wait(clock, FREEZE_DELAY, on_delayed=observe)

    async def main() -> list[float]:
        blocker = asyncio.ensure_future(_aggressor_freezing_after_one_step())
        elapsed = list(await asyncio.gather(profile(), profile(), profile()))
        await blocker
        return elapsed

    elapsed = run(main())
    assert len(elapsed) == 3
    assert all(real >= FREEZE_SECONDS for real in elapsed)
    assert len(delayed) == 3
    assert all(expected == FREEZE_DELAY for expected, _real in delayed)


def test_paced_wait_propagates_cancellation_and_leaves_no_sleeper() -> None:
    """A stopped profile stays stopped: the sleeper is cancelled and awaited."""
    clock = RecordingClock()

    async def main() -> None:
        task = asyncio.ensure_future(paced_wait(clock, 30.0))
        for _ in range(10):  # let the sleeper take its first step, without a wall clock
            if clock.sleepers:
                break
            await asyncio.sleep(0)
        assert len(clock.sleepers) == 1
        known = {other for other in asyncio.all_tasks() if not other.done()}
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        # The sleeper was cancelled and awaited before the cancellation went out:
        # no task outlives the profile that owned it.
        assert clock.sleepers[0].done()
        assert clock.sleepers[0].cancelled()
        survivors = [
            other for other in asyncio.all_tasks() if not other.done() and other not in known
        ]
        assert survivors == []

    run(main())


# ---------------------------------------------------------------------------
# 3. awaited_within -- late is not lost, and a timeout always has a message
# ---------------------------------------------------------------------------


def test_awaited_within_returns_the_value_of_a_call_that_answers() -> None:
    """The nominal path returns the result unchanged."""

    async def answer() -> int:
        return 42

    assert run(awaited_within(answer(), bound=1.0, label="poll")) == 42


def test_awaited_within_propagates_the_failure_of_the_call() -> None:
    """The call's own exception type reaches the caller untouched."""

    async def failing() -> None:
        raise MarketStreamError("no candle in the window")

    with pytest.raises(MarketStreamError, match="no candle in the window"):
        run(awaited_within(failing(), bound=1.0, label="poll"))


def test_awaited_within_returns_a_late_answer_instead_of_timing_out() -> None:
    """A call that finishes after the bound is a *late* call, not a lost one."""
    answered = asyncio.Event()

    async def slow() -> str:
        await asyncio.sleep(0.15)
        answered.set()
        return "late but alive"

    result = run(awaited_within(slow(), bound=0.05, label="history fetch"))
    assert result == "late but alive"
    assert answered.is_set()


def test_awaited_within_names_the_call_it_could_not_get() -> None:
    """A call that never answers raises a ``TimeoutError`` naming it."""
    label = "ohlcv fetch"

    async def never_answers() -> None:
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError) as raised:
        run(awaited_within(never_answers(), bound=0.05, label=label, grace_seconds=0.1))
    assert type(raised.value) is TimeoutError
    assert str(raised.value).strip() != ""
    assert label in str(raised.value)
    assert "0.150" in str(raised.value)


def test_the_default_grace_window_is_applied_to_a_lost_call() -> None:
    """The grace window of the module is used when the caller does not pass one."""
    started = time.monotonic()

    async def never_answers() -> None:
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError, match="dashboard poll"):
        run(awaited_within(never_answers(), bound=0.05, label="dashboard poll"))
    assert time.monotonic() - started >= 0.05 + LATE_GRACE_SECONDS


def test_cancelling_the_caller_of_awaited_within_cancels_the_inner_call() -> None:
    """A cancelled caller cancels the call it was waiting for, and awaits it."""
    inner: list[asyncio.Task[Any]] = []

    async def main() -> None:
        started = asyncio.Event()

        async def never_answers() -> None:
            current = asyncio.current_task()
            assert current is not None
            inner.append(current)
            started.set()
            await asyncio.Event().wait()

        caller = asyncio.ensure_future(
            awaited_within(never_answers(), bound=30.0, label="slow call")
        )
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert inner and inner[0].cancelled()

    run(main())


# ---------------------------------------------------------------------------
# 4. failure_text -- the persisted error of a profile is never an empty message
# ---------------------------------------------------------------------------


def test_failure_text_always_carries_the_type_and_a_message() -> None:
    """``TimeoutError()`` used to be persisted as ``"TimeoutError: "``."""
    assert failure_text(RuntimeError("boom")) == "RuntimeError: boom"
    assert failure_text(TimeoutError()) == "TimeoutError (no message)"
    assert failure_text(ValueError("  ")) == "ValueError (no message)"
    assert failure_text(MarketStreamError("history failed")) == "MarketStreamError: history failed"
    assert failure_text(TimeoutError("poll did not answer within 5.300 s")) == (
        "TimeoutError: poll did not answer within 5.300 s"
    )
    for exc in (RuntimeError(""), TimeoutError(), ValueError("\n\t ")):
        assert failure_text(exc).strip() != ""
