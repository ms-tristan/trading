"""Regression harness of the shared-event-loop idle-timeout crash (work package wp-4).

Five config-identical live profiles ran the same strategy, the same timeframe and
the same cadence; three of them were persisted as ``ERROR`` with
``health.last_error = "TimeoutError"`` and logged ``profile_crashed
error="TimeoutError: "`` -- an **empty** message -- tens of seconds after the start,
while the other two kept trading.  The venue was healthy and no profile ever
reported ``market_data.poll_failed``: the provider never failed.

The mechanism is a *shared* event loop.  :class:`PollingMarketStream` called
``provider.fetch_ohlcv(...)`` synchronously, so a fetch froze the one loop every
profile of the process shares for the whole duration of the call.  Every profile
paced its idle poll under a bound derived from that same poll interval: when a
freeze from *any* profile spanned another profile's bound, the loop resumed with the
bound already overdue while the idle sleeper had not been allowed to run at all, and
the bounded wait raised a bare ``TimeoutError('')`` for a poll that was perfectly
healthy.  ``_idle()`` was called outside the ``try``/``except`` of ``next_candle``,
so that timeout left the stream, reached the runner and was recorded as a *fatal*
error.  Which profiles died was arbitrary: it was a race.

This module is the failing-first artifact of the fix, and it is self-contained: it
uses the real :class:`PollingMarketStream`, a real :class:`SystemClock` and local
fakes only -- no import of another test module, no network, no wall-clock luck.

The freeze is **driven explicitly** through the ready queue, never waited for:

1. the aggressor task is created first and parks on a gate, so its resumption is
   queued in front of everything the victims do;
2. the victim tasks are created next and each of them reaches its idle poll;
3. the gate is released *from inside the victim's own poll* -- the one instant at
   which that victim has already registered the bound of its bounded wait and has
   not yet let its sleeper take its first step.  That is the **bound-registration
   window** of the production race: the freeze lands with the bound running and the
   sleeper still only *scheduled*, so the bound expires and the sleeper is cancelled
   before it ever slept.

Nothing here depends on how fast the machine is: the two orderings that matter (the
aggressor before the victims, the bound before the sleeper) are decided by
``asyncio.create_task`` and by the release happening inside the poll, not by the
clock.  Only the durations are real, and they are short.

``test_the_pre_fix_idle_wait_dies_on_the_same_freeze`` keeps the old arithmetic
frozen in this file: it stays red on any tree and proves that this freeze really is
lethal to a bounded wait.  That control releases the gate from *inside the frozen
pre-fix idle poll itself* -- queued in front of that poll's own sleeper -- so its
freeze cannot depend on where the delivered code runs the provider read.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytest

from trading_platform.core.constants import OHLCV_INDEX_NAME, REQUIRED_OHLCV_COLUMNS, candle_delta
from trading_platform.realtime.clock import SystemClock
from trading_platform.realtime.stream import PollingMarketStream

#: Every coroutine of this module is bounded by this explicit timeout, so a hung
#: implementation fails the suite instead of hanging it.
TIMEOUT = 5.0

SYMBOL = "BTC/USDT"
TIMEFRAME = "1m"

#: Cadence and bound of the victim stream of the single-victim regression.  This is
#: the equal pair -- the shape the five live profiles ran -- whose pre-fix idle bound
#: was ``0.2 * 1.05 + 0.05 = 0.26 s``.
VICTIM_POLL_INTERVAL_SECONDS = 0.2
VICTIM_TIMEOUT_SECONDS = 0.2

#: How long the aggressor's synchronous ``fetch_ohlcv`` freezes the shared loop.
#: Strictly longer than the victim's pre-fix idle bound (0.26 s), so the freeze
#: overruns the bound while the victim is inside its idle poll.
FREEZE_SECONDS = 0.3

#: The slowest victim of the configuration sweep idles 0.8 s, so its pre-fix bound
#: was ``0.8 * 1.05 + 0.05 = 0.89 s``: one freeze of this length overruns the bound
#: of *every* victim of the sweep at once.  The arithmetic is written here on purpose
#: instead of importing the module's private margin constants.
SWEEP_FREEZE_SECONDS = 1.1

#: ``(poll_interval_seconds, timeout_seconds, max_reconnects,
#: reconnect_backoff_seconds)`` of the swept victim profiles.  The sweep is the
#: "any combination" clause of the contract: the equal pair that killed the live
#: profiles, a poll interval well inside the stream timeout, a poll interval four
#: times the timeout, and no retry backoff at all.
VICTIM_CONFIGURATIONS: tuple[tuple[float, float, int, float], ...] = (
    (0.2, 0.2, 3, 0.05),
    (0.1, 0.5, 5, 0.02),
    (0.8, 0.2, 2, 0.05),
    (0.3, 0.1, 4, 0.0),
)


def run(coro: Any) -> Any:
    """Run one coroutine on a fresh loop, bounded by :data:`TIMEOUT`."""
    return asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


# ---------------------------------------------------------------------------
# local fakes
# ---------------------------------------------------------------------------


def ohlcv_frame(timestamp: pd.Timestamp, *, close: float = 100.0) -> pd.DataFrame:
    """Return a valid one-row OHLCV frame stamped at ``timestamp``."""
    values = {
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 5.0,
    }
    index = pd.DatetimeIndex([pd.Timestamp(timestamp)], name=OHLCV_INDEX_NAME)
    return pd.DataFrame(
        {column: [values[column]] for column in REQUIRED_OHLCV_COLUMNS}, index=index
    )


class Gate:
    """The aggressor's release gate, settable from the loop or from a worker thread.

    The release is called by the *victim's own poll*, which is what puts it inside
    the victim's bound-registration window (see the module docstring).  A poll the
    delivery moved off the loop runs on a worker thread, where touching an
    :class:`asyncio.Event` directly would not be safe: the release then travels
    through :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe` instead.  Either
    way it happens exactly once per poll, from inside the poll that triggered it.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.releases = 0

    async def wait(self) -> None:
        """Park until a victim's poll releases the gate."""
        self._loop = asyncio.get_running_loop()
        await self._event.wait()

    def release(self) -> None:
        """Release the waiting aggressor from wherever the victim's poll runs."""
        self.releases += 1
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:  # the poll is off the loop: a worker thread owns it
            running = None
        if running is not None and running is self._loop:
            # Synchronous on the loop: the aggressor's step is queued *inside this
            # one*, ahead of the sleeper the idle poll is about to start.
            self._event.set()
            return
        loop = self._loop
        if loop is None:  # pragma: no cover - defensive: the aggressor always parks first
            self._event.set()
            return
        loop.call_soon_threadsafe(self._event.set)


class FormingCandleProvider:
    """Provider whose only candle is still forming when the stream reads it.

    The row is stamped exactly at ``until``, so it closes one candle delta later:
    :meth:`PollingMarketStream.next_candle` finds no *closed* candle and takes its
    idle poll -- the branch that used to be cut by the caller's own bound.
    """

    def __init__(self, on_fetch: Callable[[], None] | None = None) -> None:
        self.calls = 0
        self._on_fetch = on_fetch

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: pd.Timestamp, until: pd.Timestamp
    ) -> pd.DataFrame:
        """Answer one still-forming candle, whatever window was asked for."""
        self.calls += 1
        if self._on_fetch is not None:
            # The release belongs *here*: the caller has not reached its idle poll
            # yet, so its sleeper has not started -- the window the race needs.
            self._on_fetch()
        return ohlcv_frame(pd.Timestamp(until))


class BlockingFetchProvider:
    """Provider whose synchronous fetch freezes the thread that calls it.

    That is the production defect in one line: the provider is synchronous by
    contract, and the stream used to call it straight from the shared event loop.
    """

    def __init__(self, *, block_seconds: float) -> None:
        self.block_seconds = float(block_seconds)
        self.calls = 0

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: pd.Timestamp, until: pd.Timestamp
    ) -> pd.DataFrame:
        """Freeze the calling thread, then answer a candle that closed long ago."""
        self.calls += 1
        time.sleep(self.block_seconds)
        return ohlcv_frame(pd.Timestamp(until) - 2 * candle_delta(timeframe))


def polling_stream(provider: Any, **overrides: Any) -> PollingMarketStream:
    """Build a real polling stream on the real system clock."""
    parameters: dict[str, Any] = {
        "clock": SystemClock(),
        "poll_interval_seconds": VICTIM_POLL_INTERVAL_SECONDS,
        "timeout_seconds": VICTIM_TIMEOUT_SECONDS,
    }
    parameters.update(overrides)
    return PollingMarketStream(provider, **parameters)


# ---------------------------------------------------------------------------
# the ready-queue harness
# ---------------------------------------------------------------------------


async def shared_loop_outcomes(
    *,
    victims: Sequence[PollingMarketStream],
    aggressor: Callable[[], Awaitable[Any]],
    gate: Gate,
) -> list[Any]:
    """Freeze the loop once every victim sits inside its bound-registration window.

    Returns ``[aggressor_outcome, *victim_outcomes]`` exactly as
    :func:`asyncio.gather` answers them with ``return_exceptions=True``: a failure
    of a victim is *captured*, not raised, so a red run names the profile that died
    instead of looking like a hung test.

    The order of the two steps is the harness contract, not an implementation detail
    -- see the module docstring.  ``asyncio.create_task`` queues one step per task
    and the single ``await asyncio.sleep(0)`` of this coroutine reschedules *itself
    behind* the steps it just queued, so every victim is guaranteed to run before
    the aggressor resumes: the gate is released from inside a victim's poll, which
    is strictly after that victim registered its bound and strictly before its
    sleeper started.
    """

    async def released_aggressor() -> Any:
        await gate.wait()
        return await aggressor()

    aggressor_task = asyncio.create_task(released_aggressor())
    await asyncio.sleep(0)  # the aggressor parks on the gate, ahead of the victims
    victim_tasks = [
        asyncio.create_task(victim.next_candle(SYMBOL, TIMEFRAME)) for victim in victims
    ]
    await asyncio.sleep(0)  # every victim steps first and reaches its idle poll
    return list(await asyncio.gather(aggressor_task, *victim_tasks, return_exceptions=True))


def poll_failure(outcome: Any) -> str:
    """Render one captured outcome for a readable red output."""
    if isinstance(outcome, BaseException):
        return f"raised {type(outcome).__name__}({str(outcome)!r})"
    return "returned without raising"


def assert_no_victim_died(
    configuration: Any, outcome: Any, victim: PollingMarketStream, calls: int
) -> None:
    """Assert one victim reported "nothing new" instead of dying.

    The poll it made was a success -- its only candle was still forming -- so the
    victim must be left connected, without an error and without a reconnect, and its
    idle poll must answer ``None``.  ``TimeoutError`` is called out by name because
    that is the failure this whole harness exists for.
    """
    assert not isinstance(outcome, TimeoutError), (
        f"configuration {configuration} was cut by a TimeoutError: {outcome!r}"
    )
    assert not isinstance(outcome, BaseException), (
        f"configuration {configuration} failed: {poll_failure(outcome)}"
    )
    assert outcome is None, f"configuration {configuration} answered {outcome!r}"
    assert calls == 1, f"configuration {configuration} never polled"
    assert victim.last_error is None, f"configuration {configuration} recorded an error"
    assert victim.connected is True, f"configuration {configuration} was disconnected"
    assert victim.reconnect_count == 0, f"configuration {configuration} reconnected"


# ---------------------------------------------------------------------------
# (a) the regression: a peer's blocking fetch must not kill a parked idle poll
# ---------------------------------------------------------------------------


def test_an_idle_poll_survives_a_peers_blocking_fetch_on_the_shared_loop() -> None:
    """Two real streams, one loop, one blocking fetch: no profile may die.

    The aggressor's ``fetch_ohlcv`` freezes the loop for 0.3 s while the victim is
    inside its 0.2 s idle poll under a 0.26 s bound.  Pre-fix this raised
    ``TimeoutError('')`` out of ``next_candle`` -- the ``profile_crashed
    error="TimeoutError: "`` of the live incident; a healthy idle poll must survive
    it instead, with the stream's health untouched (nothing failed, so nothing may
    be reported as a failed poll).
    """
    gate = Gate()
    victim_provider = FormingCandleProvider(on_fetch=gate.release)
    victim = polling_stream(victim_provider)
    blocking = BlockingFetchProvider(block_seconds=FREEZE_SECONDS)
    # The aggressor is a peer, not a subject of these assertions: its own read budget
    # is deliberately generous, so a delivery that bounds the (off-loop) provider read
    # by ``timeout_seconds`` does not abandon the aggressor's own fetch and turn the
    # peer into a second victim of the harness.
    aggressor = polling_stream(
        blocking,
        poll_interval_seconds=VICTIM_POLL_INTERVAL_SECONDS,
        timeout_seconds=FREEZE_SECONDS + 2.0,
    )

    async def scenario() -> list[Any]:
        await victim.start()
        await aggressor.start()
        return await shared_loop_outcomes(
            victims=[victim],
            aggressor=lambda: aggressor.next_candle(SYMBOL, TIMEFRAME),
            gate=gate,
        )

    started = time.monotonic()
    outcomes = run(scenario())
    elapsed = time.monotonic() - started
    aggressor_outcome, victim_outcome = outcomes

    assert gate.releases >= 1, "the gate was never released: the harness never armed"
    assert blocking.calls == 1, "the aggressor never made the blocking fetch"
    # The scenario cannot have finished before the freeze ended, whatever the fix
    # does with the blocking call: the loop really was frozen for that long.
    assert elapsed >= FREEZE_SECONDS, f"the freeze never happened: {elapsed:.3f} s"
    assert not isinstance(aggressor_outcome, BaseException), (
        f"the aggressor stream itself failed: {aggressor_outcome!r}"
    )
    # The victim's only candle was still forming, so its idle poll reports "nothing
    # new" -- the documented answer of next_candle, not an exception.
    assert_no_victim_died(
        "the equal poll interval / timeout pair", victim_outcome, victim, victim_provider.calls
    )


# ---------------------------------------------------------------------------
# (b) the executable proof that (a) bites
# ---------------------------------------------------------------------------


def pre_fix_idle(gate: Gate) -> Callable[[PollingMarketStream], Awaitable[None]]:
    """Build the frozen pre-fix ``_idle`` of :class:`PollingMarketStream`.

    Verbatim the implementation this delivery replaced: the idle poll was bounded
    by ``asyncio.wait_for`` with 5 % + 50 ms over its own nominal wait.  The literals
    are written out instead of importing the module's private margin constants,
    because this control has to keep describing the *old* arithmetic whatever the
    delivered code does with its constants.

    The gate is released from inside that poll, *before* the bounded wait is entered.
    That single line is what makes the control independent of the delivered tree: the
    aggressor's step is queued in front of the sleeper this very poll is about to
    start, so the freeze lands with the bound already registered and the sleeper still
    only scheduled -- the **bound-registration window** of the production race.
    """

    async def idle(self: PollingMarketStream) -> None:
        # Queued from inside the poll: the aggressor is ready to run before this
        # poll's sleeper takes its first step, whatever the caller does with the
        # provider read.
        gate.release()
        await asyncio.wait_for(
            self._clock.sleep(self._poll_interval_seconds),
            timeout=self._poll_interval_seconds * 1.05 + 0.05,
        )

    return idle


def test_the_pre_fix_idle_wait_dies_on_the_same_freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frozen pre-fix wait must still die: the regression is real, not stale.

    Same victim, same provider, same 0.3 s freeze as the regression above.  The
    control does two things differently, and both are on purpose: it restores the old
    ``_idle`` body, and it drives the freeze from inside that body -- *not* from the
    provider hook the regression uses, because the delivered code runs the provider
    read on a worker thread and the release would then be queued before the victim
    even reaches its idle poll.  Released from inside the poll instead, the queued
    aggressor runs after the old bound is registered and before its sleeper takes its
    first step, so the control stays red on any tree, including one that moved the
    stream's provider call off the loop.  It pins the mechanism the live profiles died
    of: a freeze that overruns the bound of a bounded wait turns that wait into a bare
    ``TimeoutError`` with an empty message, while the stream reports no failed poll at
    all.
    """
    # The freeze has to overrun the frozen bound for the control to mean anything.
    assert FREEZE_SECONDS > VICTIM_POLL_INTERVAL_SECONDS * 1.05 + 0.05

    gate = Gate()
    monkeypatch.setattr(PollingMarketStream, "_idle", pre_fix_idle(gate))
    victim_provider = FormingCandleProvider()
    victim = polling_stream(victim_provider)
    blocking = BlockingFetchProvider(block_seconds=FREEZE_SECONDS)

    async def raw_blocking_fetch() -> pd.DataFrame:
        until = pd.Timestamp(datetime.now(UTC))
        return blocking.fetch_ohlcv(SYMBOL, TIMEFRAME, until - 300 * candle_delta(TIMEFRAME), until)

    async def scenario() -> list[Any]:
        await victim.start()
        return await shared_loop_outcomes(victims=[victim], aggressor=raw_blocking_fetch, gate=gate)

    outcomes = run(scenario())
    victim_outcome = outcomes[1]

    assert blocking.calls == 1, "the control never ran its blocking fetch"
    assert isinstance(victim_outcome, TimeoutError), (
        "the frozen pre-fix idle wait was expected to die on this freeze, "
        f"but the victim {poll_failure(victim_outcome)}"
    )
    # The empty message is the signature of the outage: this is why three live
    # profiles were persisted with ``health.last_error = "TimeoutError"``.
    assert str(victim_outcome) == ""
    # Nothing was wrong with the data: the poll had succeeded and the stream had
    # already recorded itself healthy before it entered the idle wait.
    assert victim_provider.calls == 1
    assert victim.last_error is None
    assert victim.connected is True
    assert victim.reconnect_count == 0


# ---------------------------------------------------------------------------
# (c) the invariant, for any configuration, on the same frozen loop
# ---------------------------------------------------------------------------


def test_every_configuration_survives_a_peers_blocking_fetch_on_the_shared_loop() -> None:
    """The "any combination" clause: one frozen loop, four configurations, no death.

    Four real streams share the loop with an aggressor whose synchronous fetch
    freezes it for 1.1 s -- longer than the pre-fix bound of every one of them
    (``0.8 * 1.05 + 0.05 = 0.89 s`` at worst).  Whatever the poll interval, the
    stream timeout, the reconnect budget and the retry backoff are, a delayed idle
    poll must report "nothing new" instead of failing: ``TimeoutError`` included,
    nothing may escape and no victim may be left disconnected.
    """
    gate = Gate()
    victims: list[PollingMarketStream] = []
    providers: list[FormingCandleProvider] = []
    blocking = BlockingFetchProvider(block_seconds=SWEEP_FREEZE_SECONDS)
    for poll_interval, timeout, max_reconnects, backoff in VICTIM_CONFIGURATIONS:
        provider = FormingCandleProvider(on_fetch=gate.release)
        providers.append(provider)
        victims.append(
            polling_stream(
                provider,
                poll_interval_seconds=poll_interval,
                timeout_seconds=timeout,
                max_reconnects=max_reconnects,
                reconnect_backoff_seconds=backoff,
            )
        )
    # See the single-victim regression: the aggressor's own read budget is generous
    # on purpose, so only the victims are under test.
    aggressor = polling_stream(
        blocking,
        poll_interval_seconds=SWEEP_FREEZE_SECONDS,
        timeout_seconds=SWEEP_FREEZE_SECONDS + 2.0,
    )

    async def scenario() -> list[Any]:
        for victim in victims:
            await victim.start()
        await aggressor.start()
        return await shared_loop_outcomes(
            victims=victims,
            aggressor=lambda: aggressor.next_candle(SYMBOL, TIMEFRAME),
            gate=gate,
        )

    started = time.monotonic()
    outcomes = run(scenario())
    elapsed = time.monotonic() - started
    aggressor_outcome, *victim_outcomes = outcomes

    assert gate.releases >= 1, "the gate was never released: the harness never armed"
    assert blocking.calls == 1, "the aggressor never made the blocking fetch"
    # The sweep cannot have finished before the freeze ended: every victim really
    # spent its bound inside one single frozen loop.
    assert elapsed >= SWEEP_FREEZE_SECONDS, f"the freeze never happened: {elapsed:.3f} s"
    assert not isinstance(aggressor_outcome, BaseException), (
        f"the aggressor stream itself failed: {aggressor_outcome!r}"
    )
    for configuration, provider, victim, outcome in zip(
        VICTIM_CONFIGURATIONS, providers, victims, victim_outcomes, strict=True
    ):
        assert_no_victim_died(configuration, outcome, victim, provider.calls)
