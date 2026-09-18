"""Time seam of the realtime layer.

Everything in the realtime layer reads time through this seam: production code
rewires :class:`SystemClock`, tests inject :class:`ManualClock` and advance it
explicitly, so no test ever depends on wall-clock time and no production module
calls ``datetime.now()``, ``datetime.utcnow()`` or ``time.time()`` outside
:class:`SystemClock`.

``Clock`` is a :class:`typing.Protocol`, so an implementation living in another
module -- or a local fake in a test -- satisfies it structurally.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "ManualClock", "SystemClock"]

#: Default anchor of :class:`ManualClock`: a fixed, obviously synthetic instant.
DEFAULT_MANUAL_START = datetime(2024, 1, 1, tzinfo=UTC)


def _require_aware(moment: datetime) -> datetime:
    """Return ``moment`` normalised to UTC, rejecting a naive datetime."""
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"naive datetime is not allowed in the realtime layer: {moment!r}")
    return moment.astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    """The three time primitives every realtime component is allowed to use."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic clock reading in seconds (never goes backwards)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait for ``seconds`` of wall time (virtual time with a manual clock)."""
        ...


class SystemClock:
    """Production clock backed by the operating system."""

    def now(self) -> datetime:
        """Return ``datetime.now(UTC)``."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Return the monotonic reading of the process."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` of real time."""
        await asyncio.sleep(seconds)


class ManualClock:
    """Deterministic clock advanced explicitly by its owner (tests, replay).

    Both the wall-clock reading (:meth:`now`) and the monotonic reading
    (:meth:`monotonic`) move together: :meth:`advance` adds the same amount to
    the two of them, which keeps every derived duration and timeout consistent.
    :meth:`sleep` only advances the virtual time, it never blocks.
    """

    def __init__(self, start: datetime | None = None, *, monotonic_start: float = 0.0) -> None:
        self._now = DEFAULT_MANUAL_START if start is None else _require_aware(start)
        self._monotonic = float(monotonic_start)

    def now(self) -> datetime:
        """Return the current virtual instant (tz-aware UTC)."""
        return self._now

    def monotonic(self) -> float:
        """Return the current virtual monotonic reading in seconds."""
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        """Advance the virtual time by ``seconds`` and return immediately."""
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds`` (fractional seconds allowed).

        Raises
        ------
        ValueError
            If ``seconds`` is negative.
        """
        delta = float(seconds)
        if delta < 0:
            raise ValueError(f"a ManualClock can only advance, got {delta}")
        self._now = self._now + timedelta(seconds=delta)
        self._monotonic += delta

    def set(self, moment: datetime) -> None:
        """Set the wall-clock reading to ``moment`` without moving the monotonic one.

        Raises
        ------
        ValueError
            If ``moment`` is naive.
        """
        self._now = _require_aware(moment)

    def advance_to(self, moment: datetime) -> None:
        """Advance the clock up to ``moment`` (identical to an explicit advance).

        Raises
        ------
        ValueError
            If ``moment`` is naive, or earlier than the current virtual instant.
        """
        target = _require_aware(moment)
        delta = (target - self._now).total_seconds()
        if delta < 0:
            raise ValueError(
                f"advance_to cannot move the clock backwards: {target.isoformat()} < "
                f"{self._now.isoformat()}"
            )
        self.advance(delta)
