"""One profile, one loop: the realtime twin of ``strategy/engine.py``.

A :class:`ProfileRunner` drives a single profile -- asset, strategy, timeframe,
paper-or-live -- over a :class:`~trading_platform.realtime.stream.MarketStream`.
It is the realtime counterpart of the backtest engine, and the equivalence is
deliberate and documented:

======================================  ==========================================
backtest (``strategy/engine.py``)        realtime (this module)
======================================  ==========================================
signals evaluated on the close of t      signals evaluated on the close of t
order filled at the open of t+1          order routed at the close of t, which *is*
                                         the trigger instant on a continuous market
static stop checked intrabar             static stop checked intrabar, from the
                                         candle's low/high
``allow_short`` from the strategy params  identical, read the same way
======================================  ==========================================

The only thing the runner does *not* do is recompute anything: indicators and
entry/exit rules come from :meth:`trading_platform.strategy.base.Strategy.run`
(the single entry point), metrics come from the metrics layer, OHLCV hygiene comes
from :func:`trading_platform.data.validation.ensure_ohlcv`, and the whole order
lifecycle (risk, idempotency, persistence, reconciliation) belongs to the
:class:`~trading_platform.realtime.gateway.ExecutionGateway`.  What is left here is
the *cadence*: read a candle, warm the frame, decide, route, poll, persist.

Frozen order of one tick (each step numbered as in the delivery brief)
---------------------------------------------------------------------
1. read the next candle, bounded by ``asyncio.wait_for``;
2. skip a candle that is not strictly newer than the persisted watermark, so a
   restart neither replays nor skips one;
3. rebuild the frame ending at that candle (``history`` + the candle), through
   ``ensure_ohlcv``; fewer than two rows means the warm-up is incomplete;
4. run the strategy -- ``prepare`` then ``signals`` -- and read the **last** row,
   which is the just-closed candle;
5. check the static stop of an open position *first*, intrabar;
6. otherwise map the signal row to an action;
7. build the deterministic order (client id from profile + symbol + candle
   timestamp + per-candle sequence, reference price = the candle close);
8. submit through the gateway, turning a risk/kill-switch/venue refusal into a
   blocked decision instead of a dead loop;
9. poll the venue and persist a closed round trip;
10. append the equity point, watermark the candle and publish the health;
11. return the decision.

Every ``await`` of a wait this module owns is bounded by an explicit
``asyncio.wait_for`` timeout, so no tick can hang.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import pandas as pd

from trading_platform.config.models import ProfileConfig
from trading_platform.core.constants import OHLCV_INDEX_NAME, UTC
from trading_platform.core.errors import (
    KillSwitchActiveError,
    OrderRejectedError,
    RealtimeError,
    RiskLimitExceededError,
)
from trading_platform.core.models import Direction, ExitReason
from trading_platform.data.validation import ensure_ohlcv
from trading_platform.realtime.clock import Clock
from trading_platform.realtime.models import (
    _FAULT_STATUSES,
    CandleEvent,
    EngineCounters,
    EquityPoint,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    ProfileHealth,
    ProfileSnapshot,
    ProfileState,
    ProfileStatus,
    RunMode,
    SignalAction,
    TradeSignalDecision,
    new_client_order_id,
    status_error,
)
from trading_platform.realtime.observability import LOGGER_NAME, Counters, log_event
from trading_platform.realtime.strategies import resolve_strategy
from trading_platform.strategy.base import Strategy

if TYPE_CHECKING:
    from trading_platform.realtime.gateway import ExecutionGateway
    from trading_platform.realtime.store import StateStore
    from trading_platform.realtime.stream import MarketStream

__all__ = ["ProfileRunner"]

_LOGGER = logging.getLogger(LOGGER_NAME)

#: Lowest number of rows a frame must hold before a strategy may decide.
MIN_FRAME_ROWS = 2

#: Relative and absolute head-room added to every bound this module applies.
#:
#: A bound *equal to the wait it wraps* is a race, not a bound: the pacing sleep and
#: the stream calls were bounded by exactly the stream timeout, so whenever a
#: profile's poll interval equalled that timeout -- the natural thing to configure,
#: and the shape the Docker deployment ships -- the two deadlines fell on the same
#: instant and the tick died with ``TimeoutError`` on the first idle poll, taking
#: the whole platform down with it.  The margin stays proportional so a tight bound
#: (a test, or an operator asking for a 50 ms budget) is still tight: 5 % plus 50 ms
#: leaves a 0.05 s budget at ~0.10 s and a 30 s budget at ~31.6 s.
_BOUND_MARGIN_RATIO = 0.05
_BOUND_MARGIN_FLOOR_SECONDS = 0.05

#: Order type every order of the engine uses (the venue decides the fill).
_ORDER_TYPE = OrderType.MARKET

#: Prefix every ``STOPPED`` detail carries in front of the last error.
_STOP_PREFIX = "stopped after: "


def _strip_stop_prefix(text: str) -> str:
    """Remove every nested ``stopped after:`` prefix from a persisted detail.

    The store keeps one string per profile (the ``STOPPED`` detail *is* the
    persisted ``last_error``), so a platform restarted N times used to accumulate
    ``stopped after: `` N times in front of the real message -- a status field that
    grows without bound and hides the error it is supposed to carry.
    """
    stripped = str(text)
    while stripped.startswith(_STOP_PREFIX):
        stripped = stripped[len(_STOP_PREFIX) :]
    return stripped


# ---------------------------------------------------------------------------
# private value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OrderPlan:
    """What one tick decided to do, before risk and routing."""

    action: SignalAction
    direction: Direction | None
    reference_price: float
    reason: str = ""
    stop_price: float | None = None
    closes_position: bool = False

    @property
    def side(self) -> OrderSide | None:
        """Return the venue side of the plan (``None`` for a ``HOLD``)."""
        if self.action is SignalAction.ENTER_LONG:
            return OrderSide.BUY
        if self.action is SignalAction.EXIT_LONG:
            return OrderSide.SELL
        if self.action is SignalAction.ENTER_SHORT:
            return OrderSide.SELL
        if self.action is SignalAction.EXIT_SHORT:
            return OrderSide.BUY
        if self.action is SignalAction.STOP_LOSS:
            return OrderSide.SELL if self.direction is Direction.LONG else OrderSide.BUY
        return None


@dataclass(frozen=True)
class _EquityState:
    """The accounting read of one profile at one candle, risk inputs included."""

    equity: float
    cash: float
    position_value: float
    quantity: float
    open_positions: int
    daily_pnl: float
    daily_trades: int
    peak_equity: float


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _as_utc(value: Any) -> pd.Timestamp:
    """Return ``value`` as a timezone-aware UTC :class:`pandas.Timestamp`."""
    stamp = pd.Timestamp(value)
    return stamp.tz_localize(UTC) if stamp.tz is None else stamp.tz_convert(UTC)


def _as_utc_optional(value: Any) -> pd.Timestamp | None:
    """Return ``value`` as an aware UTC timestamp, or ``None`` when absent."""
    return None if value is None else _as_utc(value)


def _finite_or_none(value: Any) -> float | None:
    """Return ``value`` as a finite ``float``, or ``None`` (``NaN`` included)."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive, signals are numeric
        return None
    return number if math.isfinite(number) else None


def _flag(row: pd.Series, name: str) -> bool:
    """Return the boolean cell ``row[name]``, treating a missing/NaN one as ``False``."""
    if name not in row.index:
        return False
    value = row[name]
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return False
    return bool(value)


class ProfileRunner:
    """Run one trading profile over one market stream, through one gateway.

    Parameters
    ----------
    profile:
        The profile to run: asset, timeframe, strategy, mode and risk limits.
    stream:
        Injected market-data seam; the runner never builds a stream itself.
    gateway:
        The single order lifecycle shared by paper and live profiles.
    store:
        Durable state: status, watermark, equity curve and closed trades.
    clock:
        Time seam.  Every duration and timestamp of the runner goes through it.
    warmup_candles:
        How many candles :meth:`run_once` asks the stream for; defaults to the
        profile's own ``warmup_candles``.
    counters:
        Optional shared counters; a private one is created when omitted.
    timeout_seconds:
        Bound applied to every ``await`` this runner owns.  The orchestrator
        passes ``RealtimeConfig.stream_poll_timeout_seconds``; the default is the
        profile's own poll interval.
    """

    def __init__(
        self,
        *,
        profile: ProfileConfig,
        stream: MarketStream,
        gateway: ExecutionGateway,
        store: StateStore,
        clock: Clock,
        warmup_candles: int | None = None,
        counters: Counters | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._profile = profile
        self._stream = stream
        self._gateway = gateway
        self._store = store
        self._clock = clock
        self._warmup = int(
            profile.warmup_candles if warmup_candles is None else max(1, int(warmup_candles))
        )
        self._timeout = float(
            profile.poll_interval_seconds if timeout_seconds is None else timeout_seconds
        )
        if self._timeout <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds!r}")
        self._counters: Counters = Counters() if counters is None else counters
        self._strategy: Strategy | None = None
        self._started = False
        self._status = ProfileStatus.STOPPED
        self._degraded_detail = ""
        self._last_processed: pd.Timestamp | None = None
        self._lag_seconds = 0.0
        self._last_error: str | None = None
        self._sequence = 0
        self._sequence_stamp: pd.Timestamp | None = None
        self._started_at: pd.Timestamp | None = None
        self._peak_equity = float(profile.initial_balance)
        self._day_start_equity = float(profile.initial_balance)
        self._day: Any = None
        self._daily_trades = 0

    # -- introspection ------------------------------------------------------

    @property
    def profile_id(self) -> str:
        """Return the identifier of the profile this runner drives."""
        return str(self._profile.id)

    @property
    def profile(self) -> ProfileConfig:
        """Return the profile this runner drives."""
        return self._profile

    @property
    def stream(self) -> MarketStream:
        """Return the injected market stream."""
        return self._stream

    @property
    def gateway(self) -> ExecutionGateway:
        """Return the injected execution gateway."""
        return self._gateway

    def counters(self) -> EngineCounters:
        """Return the counters of this profile, reconnect count included."""
        snapshot = self._counters.snapshot()
        reconnects = self._reconnect_count()
        if reconnects is None:
            return snapshot
        return replace(snapshot, stream_reconnects=max(snapshot.stream_reconnects, reconnects))

    def __repr__(self) -> str:
        """Return a short, secret-free representation of the runner."""
        return (
            f"ProfileRunner(profile_id={self.profile_id!r}, symbol={self._profile.symbol!r}, "
            f"timeframe={self._profile.timeframe!r}, mode={self._profile.mode!r})"
        )

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Resolve the strategy and restore the profile's persisted state.

        Idempotent: a started runner returns immediately.  After :meth:`stop` and
        another :meth:`start`, the state is read from the store again, which is
        exactly what a process restart does.
        """
        if self._started:
            return
        self._prepare()
        self._sequence = 0
        self._sequence_stamp = None
        # The status *before* this call is what tells a current fault (recorded by
        # this very process: a reconciliation mismatch the orchestrator found before
        # the first tick, a crash of this loop) from a stale one (restored from a
        # previous process, which the dashboard must stop reporting).
        previous_status = self._status
        degraded = previous_status is ProfileStatus.DEGRADED
        current_fault = previous_status in _FAULT_STATUSES
        self._status = ProfileStatus.STARTING
        state = self._store.profile_state(self.profile_id)
        self._last_processed = _as_utc_optional(self._store.last_processed_candle(self.profile_id))
        self._lag_seconds = float(state.lag_seconds)
        previous_error = state.last_error
        self._last_error = (
            previous_error if current_fault else status_error(ProfileStatus.STARTING, "")
        )
        if previous_error and not current_fault:
            log_event(
                _LOGGER,
                "stale_error_cleared",
                profile_id=self.profile_id,
                symbol=str(self._profile.symbol),
                previous_error=str(previous_error),
            )
        if not degraded:
            self._degraded_detail = ""
        self._restore_curve()
        self._restore_daily_trades()
        self._started_at = _as_utc(self._clock.now())
        self._store.save_status(self.profile_id, ProfileStatus.STARTING, detail="starting")
        log_event(
            _LOGGER,
            "profile_starting",
            profile_id=self.profile_id,
            symbol=self._profile.symbol,
            timeframe=self._profile.timeframe,
            strategy=self._profile.strategy,
            mode=str(self._profile.mode),
            warmup_candles=self._warmup,
            last_processed=(
                None if self._last_processed is None else self._last_processed.isoformat()
            ),
        )
        if degraded:
            # A reconciliation mismatch detected before the first tick must not be
            # erased by the very act of starting: the profile starts, degraded.
            self._status = ProfileStatus.DEGRADED
            self._store.save_status(
                self.profile_id, ProfileStatus.DEGRADED, detail=self._degraded_detail
            )
        else:
            self._status = ProfileStatus.RUNNING
            self._store.save_status(self.profile_id, ProfileStatus.RUNNING, detail="running")
        self._started = True

    def _bound(self) -> float:
        """Return the bound applied to one wait of this profile's loop.

        Derived from ``self._timeout`` and always strictly greater than it, so no
        wait of the loop can be cut short by a bound equal to its own nominal
        duration (see :data:`_BOUND_MARGIN_RATIO`).
        """
        return self._timeout * (1.0 + _BOUND_MARGIN_RATIO) + _BOUND_MARGIN_FLOOR_SECONDS

    async def stop(self) -> None:
        """Persist ``STOPPED``; the runner closes nothing it does not own.

        The last error is kept -- and written into the detail -- so a profile that
        crashed keeps saying why even after the platform was shut down.
        """
        self._status = ProfileStatus.STOPPED
        self._started = False
        detail = (
            "stopped"
            if not self._last_error
            else f"stopped after: {_strip_stop_prefix(self._last_error)}"
        )
        self._store.save_status(self.profile_id, ProfileStatus.STOPPED, detail=detail)
        log_event(_LOGGER, "profile_stopped", profile_id=self.profile_id, detail=detail)

    def mark_crashed(self, exc: BaseException) -> None:
        """Persist the last-resort failure of a profile that left its loop.

        Called by the orchestrator when :meth:`run` raised: the store is the durable
        record an operator reads through the dashboard, so a profile that died must
        not keep saying ``running`` until the process disappears.
        """
        self._record_error(exc)

    def mark_degraded(self, detail: str) -> None:
        """Mark the profile degraded and keep it degraded across its next ticks.

        Used by the orchestrator after a reconciliation mismatch: the store is the
        durable record, and :meth:`run_once` re-publishes ``DEGRADED`` instead of
        overwriting it with ``RUNNING``.
        """
        self._degraded_detail = str(detail)
        self._status = ProfileStatus.DEGRADED
        self._last_error = status_error(
            ProfileStatus.DEGRADED, self._degraded_detail, self._last_error
        )
        self._store.save_status(
            self.profile_id, ProfileStatus.DEGRADED, detail=self._degraded_detail
        )

    async def run(self, *, max_iterations: int | None = None) -> None:
        """Loop over :meth:`run_once` until cancelled or ``max_iterations`` is met.

        Each iteration is followed by a bounded sleep so the profile paces itself
        instead of busy-waiting.

        Raises
        ------
        asyncio.CancelledError
            Re-raised after the profile was persisted as ``STOPPED``.
        TradingBacktestError
            An unexpected failure of one tick is logged as ``profile_error``,
            persisted as ``ERROR`` and re-raised: supervision belongs to the
            caller, which is the only one able to decide what to do about it.
        """
        self._prepare()
        iterations = 0
        try:
            while max_iterations is None or iterations < max_iterations:
                iterations += 1
                try:
                    await self.run_once()
                    await asyncio.wait_for(
                        self._clock.sleep(
                            min(float(self._profile.poll_interval_seconds), self._timeout)
                        ),
                        timeout=self._bound(),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._record_error(exc)
                    raise
        except asyncio.CancelledError:
            # The cancellation may land in the tick *or* in the pacing sleep: both
            # must persist STOPPED before the task really ends.
            await self.stop()
            raise

    # -- one tick -----------------------------------------------------------

    async def run_once(self) -> TradeSignalDecision | None:
        """Process the next candle and return the decision it produced.

        Returns
        -------
        TradeSignalDecision | None
            ``None`` when there is nothing to do: no new candle, a candle already
            processed, or an incomplete warm-up.
        """
        self._prepare()
        symbol = str(self._profile.symbol)
        timeframe = str(self._profile.timeframe)

        # 1. the next candle, always bounded.  The bound carries the same
        #    head-room as the pacing sleep: a stream that is idle legitimately
        #    waits a whole poll interval, which may equal ``self._timeout``.
        candle = await asyncio.wait_for(
            self._stream.next_candle(symbol, timeframe),
            timeout=self._bound(),
        )
        if candle is None:
            return None
        stamp = _as_utc(candle.timestamp)

        # 2. a restart neither replays nor skips a candle.
        if self._last_processed is not None and stamp <= self._last_processed:
            log_event(
                _LOGGER,
                "candle_skipped",
                profile_id=self.profile_id,
                symbol=symbol,
                timestamp=stamp.isoformat(),
                last_processed=self._last_processed.isoformat(),
            )
            return None
        if self._sequence_stamp is None or stamp != self._sequence_stamp:
            self._sequence = 0
            self._sequence_stamp = stamp

        # 3. the frame ending at this candle.
        history = await asyncio.wait_for(
            self._stream.history(symbol, timeframe, self._warmup),
            timeout=self._bound(),
        )
        frame = ensure_ohlcv(
            self._build_frame(history, candle, stamp), name=f"realtime:{self.profile_id}"
        )
        if len(frame) < MIN_FRAME_ROWS:
            self._lag_seconds = self._lag(stamp)
            log_event(
                _LOGGER,
                "warmup_incomplete",
                level=logging.WARNING,
                profile_id=self.profile_id,
                symbol=symbol,
                timeframe=timeframe,
                rows=len(frame),
                warmup_candles=self._warmup,
            )
            return None

        # 4. the strategy is the only producer of indicators and rules.
        _prepared, signals = self._strategy_run(frame)

        # 5. and 6. stop first, then the signal row of the just-closed candle.
        plan = self._plan(candle, signals)
        state = self._equity_state(stamp, plan.reference_price)

        # 7. and 8. the order, routed through the one lifecycle.
        if plan.action is SignalAction.HOLD:
            decision = self._hold_decision(stamp, plan)
        elif plan.closes_position and state.quantity == 0.0:
            log_event(
                _LOGGER,
                "exit_without_position",
                level=logging.WARNING,
                profile_id=self.profile_id,
                symbol=symbol,
                action=plan.action.value,
                timestamp=stamp.isoformat(),
            )
            decision = TradeSignalDecision(
                profile_id=self.profile_id,
                timestamp=stamp,
                action=plan.action,
                direction=plan.direction,
                stop_price=plan.stop_price,
                quantity=0.0,
                reference_price=float(plan.reference_price),
                reason="no open position",
            )
        else:
            decision = self._route(stamp, plan, state)
        if decision.blocked:
            # Risk, the kill switch and the venue all refuse *before* anything is
            # persisted: the tick stops here, the candle is not watermarked, and
            # the loop lives on (the refusal is a decision, not a crash).
            return decision

        # 9. fold the venue's answer back into the local state.
        self._gateway.poll()
        trade = self._gateway.closed_trade()
        if trade is not None:
            self._store.append_trade(trade, profile_id=self.profile_id)
            self._counters.increment("orders_filled")
            log_event(
                _LOGGER,
                "round_trip_closed",
                profile_id=self.profile_id,
                symbol=symbol,
                pnl=float(trade.pnl),
                exit_reason=str(trade.exit_reason),
            )

        # 10. publish the tick: equity point, watermark, status, counters.
        after = self._equity_state(stamp, float(candle.close))
        self._store.append_equity(
            EquityPoint(
                profile_id=self.profile_id,
                timestamp=stamp,
                equity=float(after.equity),
                cash=float(after.cash),
                position_value=float(after.position_value),
            )
        )
        self._store.mark_candle_processed(self.profile_id, stamp)
        self._last_processed = stamp
        self._lag_seconds = self._lag(stamp)
        published = (
            ProfileStatus.DEGRADED
            if self._status is ProfileStatus.DEGRADED
            else ProfileStatus.RUNNING
        )
        self._status = published
        # A degraded profile keeps the *reason* of its degradation: the candle it
        # just processed is already published through ``last_candle_at`` and
        # ``lag_seconds``, while the reconciliation report would be lost for ever.
        detail = (
            self._degraded_detail
            if published is ProfileStatus.DEGRADED and self._degraded_detail
            else f"last candle {stamp.isoformat()}"
        )
        recovered_from = None if published in _FAULT_STATUSES else self._last_error
        self._last_error = status_error(published, detail, self._last_error)
        self._store.save_status(self.profile_id, published, detail=detail)
        self._counters.increment("candles_processed")
        if self._last_error is None and recovered_from is not None:
            # A tick that ran to completion proves the condition is over: the
            # dashboard must stop reporting an error the profile no longer has.
            recovered_from = self._last_error
            self._last_error = None
            log_event(
                _LOGGER,
                "error_cleared",
                profile_id=self.profile_id,
                symbol=symbol,
                previous_error=str(recovered_from),
            )
        log_event(
            _LOGGER,
            "candle_processed",
            profile_id=self.profile_id,
            symbol=symbol,
            timestamp=stamp.isoformat(),
            action=plan.action.value,
            equity=float(after.equity),
            lag_seconds=self._lag_seconds,
        )
        return decision

    # -- read model ---------------------------------------------------------

    def state(self) -> ProfileState:
        """Return the persistable health of this profile."""
        return ProfileState(
            profile_id=self.profile_id,
            status=self._status,
            mode=RunMode(self._profile.mode),
            last_candle_at=self._last_processed,
            lag_seconds=float(self._lag_seconds),
            last_error=self._last_error,
            reconnect_count=self._reconnect_count() or 0,
            updated_at=_as_utc(self._clock.now()),
        )

    def health(self) -> ProfileHealth:
        """Return the health block of this profile (status, lag, counters)."""
        return ProfileHealth(
            profile_id=self.profile_id,
            status=self._status,
            last_candle_at=self._last_processed,
            lag_seconds=float(self._lag_seconds),
            last_error=self._last_error,
            reconnect_count=self._reconnect_count() or 0,
            counters=self.counters(),
        )

    def snapshot(self) -> ProfileSnapshot:
        """Return everything the monitoring layer shows for this profile."""
        fields = self._gateway.snapshot_fields()
        cash = float(fields.get("cash", self._profile.initial_balance))
        position_value = float(fields.get("position_value", 0.0))
        equity = float(fields.get("equity", cash + position_value))
        initial = float(self._profile.initial_balance)
        return ProfileSnapshot(
            profile_id=self.profile_id,
            symbol=str(self._profile.symbol),
            timeframe=str(self._profile.timeframe),
            strategy=str(self._profile.strategy),
            mode=RunMode(self._profile.mode),
            status=self._status,
            initial_balance=initial,
            equity=equity,
            cash=cash,
            position_value=position_value,
            total_return=(equity - initial) / initial if initial else 0.0,
            n_trades=len(self._store.list_trades(self.profile_id)),
            open_positions=int(fields.get("open_positions", 0)),
            health=self.health(),
            started_at=self._started_at,
            updated_at=_as_utc(self._clock.now()),
        )

    # -- internals: decision -------------------------------------------------

    def _prepare(self) -> None:
        """Resolve the strategy exactly once (the single construction path)."""
        if self._strategy is None:
            self._strategy = resolve_strategy(self._profile)

    def _strategy_run(self, frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Run the strategy's ``prepare`` and ``signals`` on ``frame``."""
        strategy = self._strategy
        if strategy is None:  # pragma: no cover - _prepare always runs first
            raise RealtimeError(f"profile {self.profile_id!r} has no resolved strategy")
        return strategy.run(frame)

    def _build_frame(
        self, history: pd.DataFrame | None, candle: CandleEvent, stamp: pd.Timestamp
    ) -> pd.DataFrame:
        """Return the frame of the strategy window, ending at ``stamp``.

        The history rows are the candles already emitted strictly *before* the
        candle being decided on, so the frame ends exactly at ``stamp`` and the
        strategy only ever sees the close of ``t`` as its last row.
        """
        row = pd.DataFrame(
            {
                "open": [float(candle.open)],
                "high": [float(candle.high)],
                "low": [float(candle.low)],
                "close": [float(candle.close)],
                "volume": [float(candle.volume)],
            },
            index=pd.DatetimeIndex([stamp], name=OHLCV_INDEX_NAME),
        )
        if history is None or len(history) == 0:
            return row
        window = history.copy()
        index = pd.DatetimeIndex(window.index)
        window.index = index.tz_localize(UTC) if index.tz is None else index.tz_convert(UTC)
        window = window[window.index < stamp]
        if len(window) == 0:
            return row
        return pd.concat([window, row])

    def _plan(self, candle: CandleEvent, signals: pd.DataFrame) -> _OrderPlan:
        """Decide what to do on this candle: the static stop first, the signal else."""
        close = float(candle.close)
        position = self._gateway.position()
        if position is not None and position.stop_price is not None:
            stop = float(position.stop_price)
            hit = (
                candle.low <= stop if position.direction is Direction.LONG else candle.high >= stop
            )
            if hit:
                return _OrderPlan(
                    action=SignalAction.STOP_LOSS,
                    direction=position.direction,
                    reference_price=stop,
                    reason=ExitReason.STOP_LOSS.value,
                    stop_price=stop,
                    closes_position=True,
                )
        row = signals.iloc[-1]
        raw_stop = _finite_or_none(row["stop_loss"]) if "stop_loss" in row.index else None
        params = self._strategy.params if self._strategy is not None else None
        short_enabled = bool(getattr(params, "allow_short", False))
        if _flag(row, "entry_long"):
            if position is not None:
                return self._entry_while_open(close, position)
            return _OrderPlan(
                action=SignalAction.ENTER_LONG,
                direction=Direction.LONG,
                reference_price=close,
                reason=ExitReason.SIGNAL.value,
                stop_price=raw_stop,
            )
        if _flag(row, "exit_long"):
            return _OrderPlan(
                action=SignalAction.EXIT_LONG,
                direction=Direction.LONG,
                reference_price=close,
                reason=ExitReason.SIGNAL.value,
                stop_price=raw_stop,
                closes_position=True,
            )
        if short_enabled and _flag(row, "entry_short"):
            if position is not None:
                return self._entry_while_open(close, position)
            return _OrderPlan(
                action=SignalAction.ENTER_SHORT,
                direction=Direction.SHORT,
                reference_price=close,
                reason=ExitReason.SIGNAL.value,
                stop_price=self._short_stop(raw_stop, close, close),
            )
        if short_enabled and _flag(row, "exit_short"):
            return _OrderPlan(
                action=SignalAction.EXIT_SHORT,
                direction=Direction.SHORT,
                reference_price=close,
                reason=ExitReason.SIGNAL.value,
                stop_price=raw_stop,
                closes_position=True,
            )
        return _OrderPlan(action=SignalAction.HOLD, direction=None, reference_price=close)

    def _entry_while_open(self, close: float, position: Position) -> _OrderPlan:
        """Ignore an entry signal while a position is open, exactly like the engine.

        ``strategy.engine`` honours an entry only when it is flat; without this
        guard a profile already long would keep buying on every candle that carries
        the same entry signal (and would size the second order on the cash the first
        one just spent).  The decision is a ``HOLD``: the tick still publishes its
        equity point.
        """
        log_event(
            _LOGGER,
            "entry_ignored",
            profile_id=self.profile_id,
            symbol=str(self._profile.symbol),
            reason="a position is already open",
            direction=str(position.direction),
        )
        return _OrderPlan(
            action=SignalAction.HOLD,
            direction=None,
            reference_price=close,
            reason="a position is already open",
        )

    @staticmethod
    def _short_stop(
        raw_stop: float | None, signal_close: float, reference_price: float
    ) -> float | None:
        """Mirror a long-style stop column above the entry, exactly like the engine.

        ``strategy.engine._resolve_stop`` reads the ``stop_loss`` cell of a short as
        the mirror of the long formula: ``entry_price + (signal_close - raw_stop)``.
        The reference price plays the role of the fill price here, because the
        close of ``t`` *is* the trigger instant of a continuous market.
        """
        if raw_stop is None:
            return None
        return float(reference_price + (signal_close - raw_stop))

    def _hold_decision(self, stamp: pd.Timestamp, plan: _OrderPlan) -> TradeSignalDecision:
        """Return the decision of a candle that produced no order."""
        return TradeSignalDecision(
            profile_id=self.profile_id,
            timestamp=stamp,
            action=SignalAction.HOLD,
            direction=None,
            stop_price=None,
            quantity=0.0,
            reference_price=float(plan.reference_price),
            reason=plan.reason,
        )

    def _route(
        self, stamp: pd.Timestamp, plan: _OrderPlan, state: _EquityState
    ) -> TradeSignalDecision:
        """Build the deterministic order, submit it, and report the outcome."""
        side = plan.side
        if side is None:  # pragma: no cover - only HOLD has no side, handled by the caller
            return self._hold_decision(stamp, plan)
        quantity = self._quantity(plan, state)
        if quantity <= 0.0:
            log_event(
                _LOGGER,
                "order_skipped",
                level=logging.WARNING,
                profile_id=self.profile_id,
                action=plan.action.value,
                reason="non-positive quantity",
            )
            return TradeSignalDecision(
                profile_id=self.profile_id,
                timestamp=stamp,
                action=plan.action,
                direction=plan.direction,
                stop_price=plan.stop_price,
                quantity=0.0,
                reference_price=float(plan.reference_price),
                blocked=True,
                block_reason="non-positive quantity",
            )
        client_order_id = new_client_order_id(
            self.profile_id, str(self._profile.symbol), stamp, self._sequence
        )
        self._sequence += 1
        request = OrderRequest(
            profile_id=self.profile_id,
            client_order_id=client_order_id,
            symbol=str(self._profile.symbol),
            side=side,
            type=_ORDER_TYPE,
            quantity=float(quantity),
            price=None,
            stop_price=plan.stop_price,
            mode=RunMode(self._profile.mode),
            reason=plan.reason,
            created_at=stamp,
        )
        try:
            self._gateway.submit(
                request,
                reference_price=float(plan.reference_price),
                equity=float(state.equity),
                open_positions=int(state.open_positions),
                position_notional=float(abs(state.position_value)),
                daily_pnl=float(state.daily_pnl),
                daily_trades=int(state.daily_trades),
                peak_equity=float(state.peak_equity),
                closes_position=bool(plan.closes_position),
            )
        except (RiskLimitExceededError, KillSwitchActiveError, OrderRejectedError) as exc:
            self._counters.increment(
                "orders_rejected" if isinstance(exc, OrderRejectedError) else "risk_rejections"
            )
            log_event(
                _LOGGER,
                "order_blocked",
                level=logging.WARNING,
                profile_id=self.profile_id,
                client_order_id=client_order_id,
                symbol=str(self._profile.symbol),
                action=plan.action.value,
                error=type(exc).__name__,
                reason=str(exc),
            )
            return TradeSignalDecision(
                profile_id=self.profile_id,
                timestamp=stamp,
                action=plan.action,
                direction=plan.direction,
                stop_price=plan.stop_price,
                quantity=float(quantity),
                reference_price=float(plan.reference_price),
                client_order_id=client_order_id,
                blocked=True,
                block_reason=str(exc),
                reason=plan.reason,
            )
        self._counters.increment("orders_submitted")
        self._daily_trades += 1
        log_event(
            _LOGGER,
            "order_submitted",
            profile_id=self.profile_id,
            client_order_id=client_order_id,
            symbol=str(self._profile.symbol),
            action=plan.action.value,
            quantity=float(quantity),
            reference_price=float(plan.reference_price),
        )
        return TradeSignalDecision(
            profile_id=self.profile_id,
            timestamp=stamp,
            action=plan.action,
            direction=plan.direction,
            stop_price=plan.stop_price,
            quantity=float(quantity),
            reference_price=float(plan.reference_price),
            client_order_id=client_order_id,
            reason=plan.reason,
        )

    def _quantity(self, plan: _OrderPlan, state: _EquityState) -> float:
        """Return the order quantity: the whole position to close, a stake to open.

        An exit closes exactly what is open.  An entry spends the profile's
        ``stake_amount`` when configured, otherwise the available cash -- the
        backtest divides by ``fill * (1 + fee)``; no fee rate belongs to a profile
        definition, so the notional is the configured stake and the venue's own
        accounting carries the fees.
        """
        reference = float(plan.reference_price)
        if reference <= 0.0:
            return 0.0
        if plan.closes_position:
            return abs(float(state.quantity))
        stake = self._profile.stake_amount
        notional = float(state.cash) if stake is None else float(stake)
        return max(0.0, notional) / reference

    # -- internals: accounting ----------------------------------------------

    def _equity_state(self, stamp: pd.Timestamp, price: float) -> _EquityState:
        """Return the equity read of the profile at ``price`` and refresh the peaks."""
        fields = self._gateway.snapshot_fields()
        cash = float(fields.get("cash", self._profile.initial_balance))
        position = self._gateway.position()
        quantity = 0.0 if position is None else float(position.quantity)
        position_value = quantity * float(price)
        equity = cash + position_value
        day = stamp.date()
        if self._day != day:
            self._day = day
            self._day_start_equity = equity
            self._daily_trades = 0
        self._peak_equity = max(self._peak_equity, equity)
        return _EquityState(
            equity=equity,
            cash=cash,
            position_value=position_value,
            quantity=quantity,
            open_positions=len(self._store.list_positions(self.profile_id)),
            daily_pnl=equity - float(self._day_start_equity),
            daily_trades=int(self._daily_trades),
            peak_equity=float(self._peak_equity),
        )

    def _restore_curve(self) -> None:
        """Restore the peak equity, the day baseline and the day from the curve."""
        points = self._store.equity_curve(self.profile_id)
        initial = float(self._profile.initial_balance)
        if not points:
            self._peak_equity = initial
            self._day_start_equity = initial
            self._day = None
            return
        self._peak_equity = max(float(point.equity) for point in points)
        last_day = _as_utc(points[-1].timestamp).date()
        self._day = last_day
        opened = [
            float(point.equity) for point in points if _as_utc(point.timestamp).date() == last_day
        ]
        self._day_start_equity = opened[0] if opened else float(points[-1].equity)

    def _restore_daily_trades(self) -> None:
        """Count the orders already routed today, so a restart honours the cap.

        The day is the one the runner is already aligned on: the day of the last
        persisted equity point, or -- for a profile that never traded -- the current
        UTC day of the injected clock.  An order the venue refused was never routed,
        so it does not count against ``max_daily_trades``.
        """
        day = self._day
        if day is None:
            day = _as_utc(self._clock.now()).date()
        self._day = day
        orders = self._store.list_orders(self.profile_id, limit=1000)
        self._daily_trades = sum(
            1
            for order in orders
            if order.state is not OrderState.REJECTED and _as_utc(order.created_at).date() == day
        )

    def _lag(self, stamp: pd.Timestamp) -> float:
        """Return how far behind the wall clock the processed candle is, in seconds."""
        return max(0.0, (_as_utc(self._clock.now()) - stamp).total_seconds())

    def _reconnect_count(self) -> int | None:
        """Return the stream's reconnect count, when the stream exposes one."""
        value = getattr(self._stream, "reconnect_count", None)
        return None if value is None else int(value)

    def _record_error(self, exc: BaseException) -> None:
        """Log, persist and remember an unexpected failure of one tick."""
        message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        self._last_error = message
        self._status = ProfileStatus.ERROR
        self._counters.increment("errors")
        log_event(
            _LOGGER,
            "profile_error",
            level=logging.ERROR,
            profile_id=self.profile_id,
            symbol=str(self._profile.symbol),
            error=message,
        )
        self._store.save_status(self.profile_id, ProfileStatus.ERROR, detail=message)
