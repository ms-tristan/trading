"""The bounded waits of the realtime layer: one definition, no fatal timeout.

:class:`~trading_platform.realtime.clock.Clock` owns *time*; this module owns every
*bound* applied around a wait, so the stream, the runner and the orchestrator can no
longer derive three slightly different margins from the same configuration.

:func:`paced_wait`
    the pacing sleep of a poll loop.  It awaits the configured delay under
    :func:`wait_bound` and **never** turns a delayed event loop into an exception: a
    sleep the loop could not honour in time is abandoned quietly, reported through
    the optional ``on_delayed`` callback and returned as an elapsed duration.  A
    process sharing one event loop between several profiles -- one of them freezing
    that loop with a synchronous call -- therefore cannot kill a healthy profile
    with the empty ``TimeoutError`` that :func:`asyncio.wait_for` raises.

:func:`awaited_within`
    the bound applied around a call that must answer (a provider poll, a history
    fetch).  A call that *finishes*, however late, is returned normally; only a call
    still pending after the bound **plus** :data:`LATE_GRACE_SECONDS` is abandoned,
    and it then raises :class:`TimeoutError` carrying the name of the call and its
    budget -- never the empty message of a crashed profile (``error="TimeoutError: "``).

The module imports nothing but the standard library and
:mod:`trading_platform.realtime.clock`, so every realtime module may import it
without creating a cycle.  It never logs, never reads time on its own and never
opens a socket: the clock it is handed is its only source of elapsed time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from trading_platform.realtime.clock import Clock

__all__ = [
    "LATE_GRACE_SECONDS",
    "PACING_MARGIN_FLOOR_SECONDS",
    "PACING_MARGIN_RATIO",
    "awaited_within",
    "paced_wait",
    "wait_bound",
]

#: Relative head-room every bound adds on top of the wait it wraps.
PACING_MARGIN_RATIO: float = 0.05

#: Absolute head-room every bound adds on top of the wait it wraps (seconds).
PACING_MARGIN_FLOOR_SECONDS: float = 0.05

#: Extra window granted to a call that must answer before it is declared lost.
#:
#: A shared event loop can be frozen by another coroutine (a blocking provider call,
#: a collection, a slow disk), which pushes the answer of a *healthy* call past any
#: bounded window.  One grace period of this length is therefore offered before the
#: call is cancelled: "late" is not "lost".
LATE_GRACE_SECONDS: float = 0.25

_T = TypeVar("_T")


def wait_bound(wait_seconds: float) -> float:
    """Return the bound that wraps a wait of ``wait_seconds``.

    The bound is always strictly greater than the wait it wraps -- 5 % plus 50 ms --
    so the two deadlines can never be registered on the same instant.  This is
    head-room, **not** a guarantee: a shared loop frozen by a blocking call can still
    push a healthy wait past this bound, which is why :func:`paced_wait` never raises
    when it expires.  A negative wait is clamped to ``0.0``.
    """
    return max(0.0, float(wait_seconds)) * (1.0 + PACING_MARGIN_RATIO) + (
        PACING_MARGIN_FLOOR_SECONDS
    )


async def _abandon(task: asyncio.Future[Any]) -> None:
    """Cancel ``task`` and wait until its cancellation has been delivered.

    Awaiting the cancelled task keeps the loop free of an orphan sleeper and of a
    "Task was destroyed but it is pending" warning.  The result is consumed instead
    of raised: an abandoned wait is not an error of the caller.
    """
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def paced_wait(
    clock: Clock,
    seconds: float,
    *,
    on_delayed: Callable[[float, float], None] | None = None,
) -> float:
    """Await ``seconds`` of pacing time and return how long it really took.

    The sleep runs under :func:`wait_bound`.  When the bound expires the sleeper is
    cancelled and the call returns normally -- **this coroutine never raises
    ``TimeoutError``**, whatever the configured poll interval, stream timeout,
    reconnect budget and number of profiles sharing the loop, and however long
    another coroutine froze that loop.  A cancelled caller is the only failing path:
    the sleeper is cancelled and awaited first, then the
    :class:`asyncio.CancelledError` is re-raised, so a stopped profile stays stopped.

    Parameters
    ----------
    clock:
        The time seam: it provides the sleep and the monotonic reading the elapsed
        duration is measured with.
    seconds:
        Requested pacing delay; a negative value is clamped to ``0.0``.
    on_delayed:
        Optional observer called with ``(expected_seconds, elapsed_seconds)`` when the
        bound expired before the sleep did.  The caller owns the reporting.

    Returns
    -------
    float
        Elapsed seconds, read from ``clock.monotonic()`` and never negative.
    """
    delay = max(0.0, float(seconds))
    started = clock.monotonic()
    sleeper: asyncio.Future[Any] = asyncio.ensure_future(clock.sleep(delay))
    try:
        _done, pending = await asyncio.wait({sleeper}, timeout=wait_bound(delay))
    except asyncio.CancelledError:
        await _abandon(sleeper)
        raise
    if pending:
        await _abandon(sleeper)
        elapsed = max(0.0, clock.monotonic() - started)
        if on_delayed is not None:
            on_delayed(delay, elapsed)
        return elapsed
    sleeper.result()
    return max(0.0, clock.monotonic() - started)


async def awaited_within(
    awaitable: Awaitable[_T],
    *,
    bound: float,
    label: str,
    grace_seconds: float = LATE_GRACE_SECONDS,
) -> _T:
    """Return the result of ``awaitable``, or raise a *named* timeout.

    The call is given ``bound`` seconds, then one further ``grace_seconds`` window.  A
    call that answers inside either window is returned as-is, and its own failure (for
    instance :class:`~trading_platform.core.errors.MarketStreamError`) propagates
    unchanged: a call that *finished*, however late, is never reported as a timeout.
    Only a call still pending after ``bound + grace_seconds`` is abandoned, and it
    then raises the built-in ``TimeoutError`` naming ``label`` and the budget it was
    given -- a message is always present, which is what removes the persisted
    ``"TimeoutError: "`` of a profile that used to die with no explanation.

    A negative ``bound`` or ``grace_seconds`` is clamped to ``0.0``.  A cancelled
    caller cancels the inner call and awaits it before re-raising.
    """
    grace = max(0.0, float(grace_seconds))
    window = max(0.0, float(bound))
    task = asyncio.ensure_future(awaitable)
    try:
        _done, pending = await asyncio.wait({task}, timeout=window)
        if pending:
            _done, pending = await asyncio.wait({task}, timeout=grace)
    except asyncio.CancelledError:
        await _abandon(task)
        raise
    if not pending:
        return task.result()
    await _abandon(task)
    raise TimeoutError(f"{label} did not answer within {window + grace:.3f} s")
