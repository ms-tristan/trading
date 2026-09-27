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

The same tick also persists the **monitoring read model** of every running
worker: the trades and the daily rows the supervisor read in that cycle are
upserted into ``profile_trades`` and ``profile_daily``, so
``GET /api/profiles/{id}`` answers from the state database instead of calling
the worker inside a request. Those two writes follow the same honesty rule as
the snapshots: rows come only from a successful read
(``supervisor.history_for`` answers ``None`` otherwise, and then nothing is
written), the upserts are keyed on ``(profile_id, trade_id)`` and
``(profile_id, date)`` so a trade that closes flips in place and a day still
unfolding is refreshed, and a profile that is not running is skipped entirely --
its last persisted rows are neither touched nor deleted.

``tick`` and ``run_forever`` are the whole API; :meth:`SnapshotPoller.stop` is
safe to call from another task and interrupts the wait at once.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from ..metrics import compute_profit_pct
from ..models import ProfileSnapshot, finite_float, format_ts
from ..profiles.store import ProfileDailyRecord, ProfileTradeRecord
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
        #: Number of trade rows upserted into the monitoring read model.
        self.trade_rows_written = 0
        #: Number of daily rows upserted into the monitoring read model.
        self.daily_rows_written = 0

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
        """Poll the fleet once and write the snapshots of the current minute.

        The monitoring read model of every running worker -- its trades and its
        daily rows -- is persisted in the same pass, from the read the
        supervisor just performed.
        """
        supervisor = self._supervisor
        await supervisor.refresh_once()
        ts = format_ts(round_to_minute(supervisor.now()))
        latest = supervisor.latest_snapshots()
        for record in supervisor.profiles():
            state_key = (record.state, record.state_reason)
            previous_key = self._seen_states.get(record.id)
            self._seen_states[record.id] = state_key
            if supervisor.is_running(record.id):
                self._write_history(record.id)
                self._write_running_snapshot(record.id, ts)
                continue
            if previous_key is None or previous_key == state_key:
                continue
            self._write_transition_snapshot(record.id, ts, latest.get(record.id))

    # -- internals ---------------------------------------------------------
    def _write_history(self, profile_id: str) -> None:
        """Upsert the trades and the daily rows of one running worker.

        ``history_for`` answers ``None`` when the profile has never been read or
        when its last history pass failed; nothing is then written and nothing is
        deleted, because the platform never invents data and never drops the
        history of a profile whose worker stopped.
        """
        history = self._supervisor.history_for(profile_id)
        if history is None:
            return
        daily_rows, trade_rows = history
        store = self._supervisor.store
        self.trade_rows_written += store.upsert_trade_records(
            [_trade_record(profile_id, row) for row in trade_rows]
        )
        self.daily_rows_written += store.upsert_daily_records(
            [_daily_record(profile_id, row) for row in daily_rows]
        )

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


# ---------------------------------------------------------------------------
# Store rows: the read model the poller feeds
# ---------------------------------------------------------------------------
def _trade_record(profile_id: str, row: dict[str, Any]) -> ProfileTradeRecord:
    """Build the stored trade row of one normalised trade payload.

    ``updated_at`` is left empty on purpose: the store stamps
    :func:`~trading_platform.models.format_ts` itself. The ``(profile_id,
    trade_id)`` primary key is what flips a trade from open to closed in place
    instead of duplicating it.
    """
    return ProfileTradeRecord(
        profile_id=profile_id,
        trade_id=int(row["trade_id"]),
        pair=str(row["pair"]),
        is_open=bool(row["is_open"]),
        open_date=row["open_date"],
        close_date=row["close_date"],
        amount=finite_float(row["amount"]),
        open_rate=finite_float(row["open_rate"]),
        close_rate=finite_float(row["close_rate"]),
        stake_amount=finite_float(row["stake_amount"]),
        profit_abs=finite_float(row["profit_abs"]),
        profit_pct=finite_float(row["profit_pct"]),
        exit_reason=row["exit_reason"],
    )


def _daily_record(profile_id: str, row: dict[str, Any]) -> ProfileDailyRecord:
    """Build the stored daily row of one normalised daily payload.

    Same discipline as :func:`_trade_record` on the ``(profile_id, date)``
    primary key, so a day that is still unfolding is replaced in place.
    """
    return ProfileDailyRecord(
        profile_id=profile_id,
        date=str(row["date"]),
        abs_profit=finite_float(row["abs_profit"]),
        rel_profit=finite_float(row["rel_profit"]),
        starting_balance=finite_float(row["starting_balance"]),
        trade_count=int(finite_float(row["trade_count"])),
    )
