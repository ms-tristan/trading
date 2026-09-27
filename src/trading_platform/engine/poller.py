"""The snapshot poller: one measurement per profile and per minute.

The poller is the clock of the platform. Every ``snapshot_interval_seconds``
(``TB_SNAPSHOT_INTERVAL_SECONDS``, 60 s by default) it asks the supervisor for
one poll cycle and turns the answer into rows of ``profile_snapshots``:

* **running profile, successful read** -- exactly one snapshot, with ``ts``
  rounded down to the minute. The store writes it as an ``INSERT OR REPLACE``
  on the ``(profile_id, ts)`` primary key, so a second poll inside the same
  minute replaces the row instead of appending a duplicate and the minute stays
  idempotent;
* **running profile, failed read** -- **no snapshot**. The failure is counted by
  the supervisor, which applies the health and restart policy; publishing a
  stale measurement would claim the worker answered when it did not;
* **non-running profile** -- a snapshot only when its state (or the reason of
  that state) changed since the previous tick, and only when the profile has a
  history to carry forward. That is what makes a stop, a queue or a crash
  visible in the equity curve without ever inventing a measurement: a profile
  that never ran gets no row at all, and a profile that stays stopped produces
  no further rows.

``tick`` and ``run_forever`` are the whole API; :meth:`SnapshotPoller.stop` is
safe to call from another task and interrupts the wait at once.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from ..metrics import compute_profit_pct
from ..models import ProfileSnapshot, format_ts
from .supervisor import Supervisor

__all__ = ["MINIMUM_INTERVAL_SECONDS", "SnapshotPoller", "round_to_minute"]

#: Floor of the polling interval: a misconfigured value must not spin the loop.
MINIMUM_INTERVAL_SECONDS = 0.05

logger = logging.getLogger(__name__)


def round_to_minute(ts: datetime) -> datetime:
    """Return ``ts`` in UTC with seconds and microseconds zeroed.

    A naive timestamp is read as UTC (the platform convention), and an aware
    timestamp in another zone is converted first, so the minute boundary is
    always the UTC one the snapshots are keyed on.

    >>> from datetime import datetime
    >>> round_to_minute(datetime(2026, 9, 27, 17, 31, 45, 123456, tzinfo=UTC))
    datetime.datetime(2026, 9, 27, 17, 31, tzinfo=datetime.timezone.utc)
    """
    moment = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return moment.astimezone(UTC).replace(second=0, microsecond=0)


class SnapshotPoller:
    """Drive the supervisor and persist the history of the fleet.

    One poller drives one supervisor. A poller that was stopped stays stopped --
    create a new one to resume polling -- which keeps ``stop`` idempotent and
    race-free even when it is called before the loop ever ran.
    """

    def __init__(self, supervisor: Supervisor) -> None:
        self._supervisor = supervisor
        self._stop_event = asyncio.Event()
        self._seen_states: dict[str, tuple[str, str | None]] = {}
        self._running = False
        #: Number of snapshot rows written since the poller was created.
        self.snapshots_written = 0

    @property
    def supervisor(self) -> Supervisor:
        """Return the supervisor this poller drives."""
        return self._supervisor

    @property
    def running(self) -> bool:
        """Whether :meth:`run_forever` is currently looping."""
        return self._running

    def interval(self) -> float:
        """Return the configured interval in seconds, floored at a sane minimum."""
        configured = float(self._supervisor.settings.snapshot_interval_seconds)
        return max(MINIMUM_INTERVAL_SECONDS, configured)

    def stop(self) -> None:
        """Ask the loop to return: the current tick finishes, the wait does not."""
        self._stop_event.set()

    async def run_forever(self) -> None:
        """Tick immediately, then once per interval, until :meth:`stop` is called.

        A tick that fails is logged and the loop carries on: one hiccup of the
        state store or of a worker must not silently end the history of the
        whole fleet.
        """
        self._running = True
        try:
            while not self._stop_event.is_set():
                try:
                    await self.tick()
                except Exception as exc:  # a failing tick must not end the history
                    logger.warning("snapshot tick failed: %s", exc)
                if self._stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval())
                except TimeoutError:
                    continue
        finally:
            self._running = False

    async def tick(self) -> None:
        """Poll the fleet once and write the snapshots of the current minute."""
        supervisor = self._supervisor
        await supervisor.refresh_once()
        ts = format_ts(round_to_minute(supervisor.now()))
        latest = supervisor.latest_snapshots()
        for record in supervisor.profiles():
            state_key = (record.state, record.state_reason)
            previous_key = self._seen_states.get(record.id)
            self._seen_states[record.id] = state_key
            if supervisor.is_running(record.id):
                self._write_running_snapshot(record.id, ts)
                continue
            if previous_key is None or previous_key == state_key:
                continue
            self._write_transition_snapshot(record.id, ts, latest.get(record.id))

    # -- internals ---------------------------------------------------------
    def _write_running_snapshot(self, profile_id: str, ts: str) -> None:
        """Write the measurement of a running worker, when it has one."""
        supervisor = self._supervisor
        metrics = supervisor.metrics_for(profile_id)
        if metrics is None:
            # The read failed in this tick: the supervisor counted the failure
            # and the health policy owns the decision, not the poller.
            return
        record = supervisor.store.get_profile(profile_id)
        initial_capital = 0.0 if record is None else record.initial_capital
        supervisor.store.record_snapshot(
            ProfileSnapshot(
                profile_id=profile_id,
                ts=ts,
                portfolio_value=metrics.portfolio_value,
                cash=metrics.cash,
                positions_value=metrics.positions_value,
                profit_abs=metrics.profit_abs,
                profit_pct=compute_profit_pct(metrics.portfolio_value, initial_capital),
                realized_profit_abs=metrics.realized_profit_abs,
                unrealized_profit_abs=metrics.unrealized_profit_abs,
                open_trades=metrics.open_trades,
                closed_trades=metrics.closed_trades,
                win_rate=metrics.win_rate,
                profit_factor=metrics.profit_factor,
                max_drawdown_pct=metrics.max_drawdown_pct,
                healthy=True,
            )
        )
        self.snapshots_written += 1

    def _write_transition_snapshot(
        self,
        profile_id: str,
        ts: str,
        previous: ProfileSnapshot | None,
    ) -> None:
        """Carry the last known values of a profile into the minute it changed.

        The values come from the store: the poller never invents a figure, so a
        profile without history stays without one.
        """
        if previous is None:
            return
        self._supervisor.store.record_snapshot(
            previous.model_copy(update={"ts": ts, "healthy": False})
        )
        self.snapshots_written += 1
