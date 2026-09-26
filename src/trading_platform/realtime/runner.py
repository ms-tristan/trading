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
1. read the next candle, bounded by
   :func:`~trading_platform.realtime.waits.awaited_within`;
2. skip a candle that is not strictly newer than the persisted watermark, so a
   restart neither replays nor skips one;
3. rebuild the frame ending at that candle (``history`` + the candle), through
   ``ensure_ohlcv``; fewer rows than ``max(MIN_FRAME_ROWS,
   strategy.required_candles(grid))`` means the warm-up is **not satisfied yet**
   -- a warning, never fatal, because the frame grows with every candle;
4. run the strategy -- ``prepare`` then ``signals`` -- and read the **last** row,
   which is the just-closed candle;
5. check the static stop of an open position *first*, intrabar;
6. otherwise map the signal row to an action.  The **entry** decision may read the
   last ``1 + entry_lookback_candles`` rows (``0``, the default, keeps reading the
   last row only): a crossover that happened within that window is caught up on
   when the current row still confirms the trend and that signal row has never
   been acted upon, while **exits and stops keep reading the last row only**.  A
   catch-up entry is an ordinary entry: it travels the frozen risk path of step 8
   (same counters, same daily cap, same risk inputs, same shared-wallet funding
   check) and is never marked as acted upon until that path accepted it;
7. build the deterministic order (client id from profile + symbol + candle
   timestamp + per-candle sequence, reference price = the candle close);
8. submit through the gateway, turning a risk/kill-switch/venue refusal into a
   blocked decision instead of a dead loop;
9. poll the venue and persist a closed round trip;
10. append the processed candle, the equity point, watermark the candle and publish
    the health;
11. return the decision.

Pause semantics
---------------
``pause()`` is an **entry-only** gate: the profile stops opening new positions but
keeps managing the one it holds -- the static stop is still checked intrabar and the
exit signals are still routed, so no position is ever left unmanaged.  A paused tick
that carries an entry signal is a ``HOLD`` (never a blocked decision): it still
appends its candle, its equity point and its watermark, exactly like any other tick.
The gate is plain in-memory state of the runner and never touches the store, the
status, the strategy or the gateway; ``resume()`` clears it.

Every wait this module owns goes through
:mod:`trading_platform.realtime.waits`, so no tick can hang **and** no healthy
wait can be turned into a fatal error by a delayed event loop:

* a call that must answer -- the next candle, the history window -- runs under
  :func:`~trading_platform.realtime.waits.awaited_within` with a budget derived
  from the longest wait that call may legitimately take: the configured timeout
  *and* the wait the stream itself declares (see :func:`stream_wait_bound`).  The
  budget is strictly greater than that wait, and it is derived from the wait
  rather than from the timeout alone.  Head-room is nevertheless **not** a
  guarantee -- a shared loop frozen by a blocking call overruns any margin -- so
  the *behaviour* is what carries the tick: a call that answers late is returned
  as its own result, and only a call still pending after the budget **plus**
  :data:`~trading_platform.realtime.waits.LATE_GRACE_SECONDS` is abandoned, with a
  :class:`TimeoutError` naming the call and its budget.  The hang detector is
  intact; a late answer is no longer a failure, and the persisted error of a
  failed call is never the empty ``"TimeoutError: "`` of the incident;
* the pacing wait between two ticks runs through
  :func:`~trading_platform.realtime.waits.paced_wait`, which can **not** raise
  ``TimeoutError`` at all: a sleep the frozen loop could not honour in time is
  abandoned and reported once as ``profile_pacing_delayed``, so a delayed idle
  poll can never end a healthy profile, whatever
  (``poll_interval_seconds``, ``stream_poll_timeout_seconds``, ``max_reconnects``,
  ``reconnect_backoff_seconds``, number of profiles) the platform runs with.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import pandas as pd

from trading_platform.config.models import MAX_ENTRY_LOOKBACK_CANDLES, ProfileConfig
from trading_platform.core.constants import OHLCV_INDEX_NAME, UTC
from trading_platform.core.errors import (
    KillSwitchActiveError,
    OrderRejectedError,
    RealtimeError,
    RiskLimitExceededError,
    WalletError,
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
from trading_platform.realtime.observability import (
    LOGGER_NAME,
    Counters,
    failure_text,
    log_event,
)
from trading_platform.realtime.strategies import resolve_strategy
from trading_platform.realtime.waits import awaited_within, paced_wait, wait_bound
from trading_platform.realtime.warmup import (
    SEVERITY_ERROR,
    candles_per_day,
    effective_warmup_candles,
    profile_warmup_findings,
)
from trading_platform.strategy.base import Strategy

if TYPE_CHECKING:
    from trading_platform.realtime.gateway import ExecutionGateway
    from trading_platform.realtime.store import StateStore
    from trading_platform.realtime.stream import MarketStream

__all__ = ["ProfileRunner", "stream_wait_bound"]

_LOGGER = logging.getLogger(LOGGER_NAME)

#: Lowest number of rows a frame must hold before a strategy may decide.
MIN_FRAME_ROWS = 2

#: Order type every order of the engine uses (the venue decides the fill).
_ORDER_TYPE = OrderType.MARKET

#: Prefix every ``STOPPED`` detail carries in front of the last error.
_STOP_PREFIX = "stopped after: "


def stream_wait_bound(timeout_seconds: float, max_wait_seconds: float = 0.0) -> float:
    """Return the budget a caller must apply around one call that may legitimately idle.

    The budget is the larger of the two declared waits -- the configured stream
    timeout and the stream's own ``max_wait_seconds`` -- plus the single head-room
    defined in :mod:`trading_platform.realtime.waits`
    (:func:`~trading_platform.realtime.waits.wait_bound`): 5 % plus 50 ms, so the
    deadline of the call and the deadline of its budget are never registered on the
    same event-loop instant.  The value is pinned by ``tests/test_cli_realtime.py``,
    which builds the ``realtime run --once`` tick budget from it, and by
    ``RealtimeOrchestrator._warn_on_idle_bound``, which reports the very budget the
    runner applies.

    This member is the arithmetic *and* the seam: the identical value can no longer
    be derived twice, because the ratio and the floor used to live here as well as
    in the stream.  Note what the number is and is not: it is head-room, **not** a
    guarantee.  A shared event loop frozen by a blocking call overruns any margin,
    which is why the callers of this budget reach for
    :func:`~trading_platform.realtime.waits.awaited_within` -- a call that answers
    late is returned, and only a call that never answers is reported as a timeout.

    Parameters
    ----------
    timeout_seconds:
        Configured bound of one stream call -- ``stream_poll_timeout_seconds``.
    max_wait_seconds:
        Longest wait the stream declares it may legitimately take
        (``MarketStream.max_wait_seconds``).  ``0.0`` for a stream that never waits,
        which reduces the budget to the configured timeout plus its head-room.

    Returns
    -------
    float
        The larger of the two waits, plus the shared proportional and absolute
        head-room.
    """
    return wait_bound(max(0.0, float(timeout_seconds), float(max_wait_seconds)))


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
class _EntryBasis:
    """Which row of the signals frame the entry decision is taken from.

    ``row_index`` is a **positional** index into the signals frame and ``-1`` is
    the historical decision: the last row.  ``timestamp`` is the timestamp of the
    signal row itself -- *not* the candle being processed -- and ``reference_price``
    is the close of that very same row.

    Keeping the reference price and the ATR stop on the **same** row is what makes
    the coherence of a catch-up entry structural rather than a convention: the stop
    flows from :meth:`ProfileRunner._catch_up_plan` into the same ``_OrderPlan`` as
    the reference price, :meth:`ProfileRunner._route` passes that reference price as
    the fill reference and :meth:`ProfileRunner._quantity` divides the stake by it,
    so the size, the fill and the protective stop can never describe two different
    rows of the market.
    """

    row_index: int
    timestamp: pd.Timestamp
    reference_price: float
    is_catch_up: bool


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
        How many candles :meth:`run_once` asks the stream for.  When omitted, the
        profile's **resolved** warm-up is used
        (:func:`~trading_platform.realtime.warmup.effective_warmup_candles`): its
        explicit ``warmup_candles`` override when it declares one, and otherwise
        the strategy's own requirement on the profile's timeframe -- so "no
        override" can never mean "warm up for ever".  An explicit argument wins
        verbatim (clamped to at least one candle).
    counters:
        Optional shared counters; a private one is created when omitted.
    timeout_seconds:
        Bound applied to every ``await`` this runner owns.  The orchestrator
        passes ``RealtimeConfig.stream_poll_timeout_seconds``; the default is the
        profile's own poll interval.  The bound actually applied here additionally
        carries the stream's own longest legitimate wait
        (``MarketStream.max_wait_seconds``), because a stream that idles is allowed
        to idle a whole poll interval -- see :meth:`_bound`.
    history_candles:
        The window the live stream is configured to serve this profile
        (``RealtimeConfig.history_candles``, or the profile's own override -- see
        :meth:`~trading_platform.config.models.ProfileConfig.effective_history_candles`).
        It is used by the start-time warm-up check to name a profile that asks for
        more candles than the stream serves (a warning: the frame the strategy
        receives is bounded by ``warmup_candles``, so the profile still runs).
        ``None`` means "no stream window was resolved", in which case the check
        falls back to this runner's own ``warmup_candles`` and changes nothing.
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
        history_candles: int | None = None,
    ) -> None:
        self._profile = profile
        self._stream = stream
        self._gateway = gateway
        self._store = store
        self._clock = clock
        # The warm-up this runner *really* serves, and the ONE value every
        # consumer below reads: the ``profile_starting`` context, the step-3
        # history call, the incomplete-frame counter, ``_check_warmup`` and
        # ``_required_candles``.  It is resolved through the single arithmetic
        # authority (:func:`effective_warmup_candles`) rather than read off the
        # raw optional field, because ``warmup_candles`` being optional means
        # "not overridden" resolves to the strategy's own requirement -- reading
        # the raw field here asked the stream for ``None``-turned-``200`` while
        # the strategy needed 40321, which is exactly the silent no-op of the
        # incident.  An explicit ``warmup_candles=`` argument still wins verbatim.
        self._warmup = int(
            effective_warmup_candles(profile)
            if warmup_candles is None
            else max(1, int(warmup_candles))
        )
        self._history = None if history_candles is None else max(1, int(history_candles))
        self._timeout = float(
            profile.poll_interval_seconds if timeout_seconds is None else timeout_seconds
        )
        if self._timeout <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds!r}")
        # The stream declares its own longest legitimate wait.  Read defensively:
        # an external duck-typed stream that predates the member must not crash the
        # boot, it simply declares no wait.
        self._stream_wait = max(0.0, float(getattr(stream, "max_wait_seconds", 0.0)))
        self._counters: Counters = Counters() if counters is None else counters
        self._strategy: Strategy | None = None
        self._warmup_checked = False
        self._started = False
        self._status = ProfileStatus.STOPPED
        self._degraded_detail = ""
        self._last_processed: pd.Timestamp | None = None
        self._last_acted_entry_crossing: pd.Timestamp | None = None
        self._lag_seconds = 0.0
        self._last_error: str | None = None
        self._sequence = 0
        self._sequence_stamp: pd.Timestamp | None = None
        self._started_at: pd.Timestamp | None = None
        self._paused = False
        self._history_seeded = False
        self._peak_equity = float(profile.effective_allocation)
        self._day_start_equity = float(profile.effective_allocation)
        self._day: Any = None
        self._daily_trades = 0
        self._last_block_reason = ""

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

    @property
    def paused(self) -> bool:
        """Return whether the entry gate of this profile is closed."""
        return self._paused

    @property
    def last_block_reason(self) -> str:
        """Return why the last routed order was refused (empty when none was).

        Set on **every** blocked path of :meth:`_route` -- the shared-wallet
        funding refusal, a platform cap, a per-profile limit, the kill switch, a
        venue rejection and a non-positive quantity -- and cleared as soon as an
        order is accepted, so the dashboard can always answer "why is this profile
        not trading?" without reading the log stream.
        """
        return self._last_block_reason

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
        # The entry watermark is durable and per profile: without this read a
        # restart would forget the crossover it already acted upon and open the
        # same entry a second time.  A store that does not expose the pair at all
        # answers "nothing was ever acted upon", which is the exact behaviour of
        # every store written before it existed.
        crossing_reader = getattr(self._store, "last_acted_entry_crossing", None)
        crossing_value = None if crossing_reader is None else crossing_reader(self.profile_id)
        self._last_acted_entry_crossing = _as_utc_optional(crossing_value)
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
        """Return the budget given to one call of this tick that must answer.

        The budget is derived from the wait itself -- the larger of ``self._timeout``
        and the wait the stream declares through ``MarketStream.max_wait_seconds`` --
        plus the single head-room defined in
        :mod:`trading_platform.realtime.waits` (see :func:`stream_wait_bound`), never
        from ``self._timeout`` alone.  For ANY
        (``poll_interval_seconds``, ``stream_poll_timeout_seconds``) pair, the
        deployment's 30 s / 10 s included, the deadline of the call and the deadline
        of this budget are therefore never registered on the same event-loop instant.

        Head-room is a margin, **not** a guarantee: a shared event loop frozen by
        another profile's blocking call overruns any margin, and the earlier "5 %
        plus 50 ms" bound was exactly what the incident outran.  What keeps a delayed
        profile alive is not this number but the behaviour its callers get from
        :mod:`trading_platform.realtime.waits`: :meth:`run_once` hands this budget to
        :func:`~trading_platform.realtime.waits.awaited_within`, which returns a call
        that answered late and reserves its named ``TimeoutError`` for a call that
        never answered at all, and the pacing wait of :meth:`run` never raises on
        expiry.  This member is what the hang detector measures against; it is not
        what protects a healthy tick from a frozen loop.
        """
        return stream_wait_bound(self._timeout, self._stream_wait)

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

    def mark_crashed(self, exc: BaseException) -> bool:
        """Persist the last-resort failure of a profile that left its loop.

        Called by the orchestrator when :meth:`run` raised: the store is the durable
        record an operator reads through the dashboard, so a profile that died must
        not keep saying ``running`` until the process disappears.

        Idempotent, because two records describe one profile that left its loop: the
        tick loop records the failure *inside* the tick (``profile_error``, persisted
        by :meth:`_record_error`) and the supervision records that the profile is no
        longer supervised.  A failure whose rendering already **is** the persisted
        error of this profile has therefore been recorded once already, and this
        method then logs nothing, writes nothing and answers ``False`` -- one failure
        never leaves two ``ERROR`` rows and two ``profile_error`` records behind.
        Otherwise the failure is recorded exactly once and the answer is ``True``.

        The answer is what the orchestrator's supervision relies on to know whether
        it still had to write; callers that ignore it keep working unchanged.

        Returns
        -------
        bool
            ``True`` when this call recorded the failure, ``False`` when the very
            same failure was already the error of this profile.
        """
        if failure_text(exc) == self._last_error:
            return False
        self._record_error(exc)
        return True

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

    # -- runtime control: the entry gate ------------------------------------

    def pause(self) -> None:
        """Stop opening new positions, keep managing the open one. Idempotent.

        The gate is consulted **only** where an entry would be decided, so the
        static stop of an open position and every exit signal keep working while the
        profile is paused.  Nothing else is touched: no store write, no status
        change (the profile stays ``RUNNING`` -- ``HALTED`` is a fault status and
        would degrade ``/api/health``) and no gateway call.
        """
        if self._paused:
            return
        self._paused = True
        log_event(_LOGGER, "profile_paused", profile_id=self.profile_id)

    def resume(self) -> None:
        """Re-open the entry gate of this profile. Idempotent, and the mirror of pause."""
        if not self._paused:
            return
        self._paused = False
        log_event(_LOGGER, "profile_resumed", profile_id=self.profile_id)

    async def run(self, *, max_iterations: int | None = None) -> None:
        """Loop over :meth:`run_once` until cancelled or ``max_iterations`` is met.

        Each iteration is followed by a bounded sleep so the profile paces itself
        instead of busy-waiting.  That sleep is a **pacing** wait, not a call that
        must answer: it runs through
        :func:`~trading_platform.realtime.waits.paced_wait`, which returns when the
        delay elapsed and, when a shared event loop was frozen past the delay's
        budget, abandons the sleeper, logs one ``profile_pacing_delayed`` warning and
        returns.  ``run`` therefore never raises ``TimeoutError``, whatever
        (``poll_interval_seconds``, ``stream_poll_timeout_seconds``) the profile
        holds and however long another profile blocked the loop.

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
                    await paced_wait(
                        self._clock,
                        min(float(self._profile.poll_interval_seconds), self._timeout),
                        on_delayed=self._log_pacing_delayed,
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

    def _log_pacing_delayed(self, expected_seconds: float, elapsed_seconds: float) -> None:
        """Report one pacing sleep the event loop could not honour in time.

        The observer of :func:`~trading_platform.realtime.waits.paced_wait`.  A
        delayed pacing wait is a **warning about the loop**, never a failure of the
        profile: the tick that follows is a perfectly ordinary tick, and the profile
        stays ``RUNNING``.  The record carries the configured delay, the budget the
        delay was given and what it really took, so "the loop was frozen for N
        seconds" can be read off the log without a second measurement.
        """
        log_event(
            _LOGGER,
            "profile_pacing_delayed",
            level=logging.WARNING,
            profile_id=self.profile_id,
            expected_seconds=float(expected_seconds),
            bound_seconds=wait_bound(float(expected_seconds)),
            elapsed_seconds=float(elapsed_seconds),
        )

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

        # 1. the next candle, always bounded.  The budget carries the same
        #    head-room as the pacing sleep, plus the stream's own declared wait:
        #    a stream that is idle legitimately waits a whole poll interval, which
        #    may equal -- or exceed -- ``self._timeout``.  The call is a call that
        #    must answer, so it goes through ``awaited_within``: it hangs the tick
        #    when it never answers (with a message naming it), and it is *returned*
        #    when it answers late because another profile froze the shared loop.
        candle = await awaited_within(
            self._stream.next_candle(symbol, timeframe),
            bound=self._bound(),
            label=(
                f"the market stream next_candle for {symbol} {timeframe} "
                f"of profile {self.profile_id}"
            ),
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
        history = await awaited_within(
            self._stream.history(symbol, timeframe, self._warmup),
            bound=self._bound(),
            label=(
                f"the market stream history for {symbol} {timeframe} of profile {self.profile_id}"
            ),
        )
        frame = ensure_ohlcv(
            self._build_frame(history, candle, stamp), name=f"realtime:{self.profile_id}"
        )
        # The warm-up contract, "not warm YET" side: a frame shorter than what the
        # strategy needs cannot emit a signal, but the history grows with every
        # candle, so this is a warning and the tick simply does nothing.  A frame
        # that can NEVER warm up was already refused at start time (see
        # :meth:`_check_warmup`); it never reaches this branch.
        required = self._required_candles()
        if len(frame) < max(MIN_FRAME_ROWS, required):
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
                required_candles=required,
                candles_per_day=candles_per_day(timeframe),
            )
            return None

        # 4. the strategy is the only producer of indicators and rules.
        prepared, signals = self._strategy_run(frame)

        # 3b. seed the persisted candle history once, from the very window the
        #     strategy just consumed. It is display only: these rows feed the
        #     bounded ``candles`` table the dashboard draws, they are never
        #     replayed as ticks and they never touch the watermark that gates a
        #     decision, so seeding changes no trading behaviour. Without it the
        #     chart would stay empty until the profile had processed a full
        #     window one timeframe at a time.
        if not self._history_seeded:
            self._seed_candle_history(frame)
            self._history_seeded = True

        # 5. and 6. stop first, then the signal row of the just-closed candle.
        basis = self._entry_basis(signals, float(candle.close), prepared)
        plan = self._plan(candle, signals, prepared)
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

        # 8b. only a catch-up entry the risk path accepted is marked as acted
        #     upon: a refused one must be retried on the next tick, through this
        #     very same path, and never through a side door of its own.
        if basis.is_catch_up and decision.action is SignalAction.ENTER_LONG:
            self._record_catch_up(basis)

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

        # 10. publish the tick: the processed candle, the equity point, the
        #     watermark, the status and the counters.
        self._store.append_candle(candle, profile_id=self.profile_id)
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
        """Return everything the monitoring layer shows for this profile.

        Every cash figure is read from the gateway's *attributed* read model -- the
        profile's share of the one shared wallet -- with a backward-compatible
        fallback per field, so a gateway written before the shared wallet existed
        reports exactly what it used to.
        """
        fields = self._gateway.snapshot_fields()
        allocation = float(fields.get("allocation", self._profile.effective_allocation))
        cash = float(fields.get("cash", allocation))
        position_value = float(fields.get("position_value", 0.0))
        position = self._gateway.position()
        quantity = 0.0 if position is None else float(position.quantity)
        average = 0.0 if position is None else float(position.average_price)
        deployed = float(fields.get("deployed", abs(quantity * average)))
        realized_pnl = float(fields.get("realized_pnl", 0.0))
        unrealized_pnl = float(fields.get("unrealized_pnl", position_value - deployed))
        equity = float(fields.get("equity", allocation + realized_pnl + unrealized_pnl))
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
            total_return=(equity - allocation) / allocation if allocation else 0.0,
            n_trades=len(self._store.list_trades(self.profile_id)),
            open_positions=int(fields.get("open_positions", 0)),
            health=self.health(),
            started_at=self._started_at,
            updated_at=_as_utc(self._clock.now()),
            allocation=allocation,
            deployed=deployed,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            last_block_reason=self._last_block_reason or None,
        )

    # -- internals: decision -------------------------------------------------

    def _prepare(self) -> None:
        """Resolve the strategy exactly once (the single construction path).

        Every caller of this method -- :meth:`start`, :meth:`run` and
        :meth:`run_once`, and therefore ``realtime run``, ``realtime run --once``
        and the orchestrator's ``_build_runner`` -- goes through it, so an unknown
        strategy name or a rejected parameter set fails **here, at startup**,
        before the first candle, instead of starting inert.

        Once the strategy is resolved, the warm-up contract is applied **once**
        per runner (see :meth:`_check_warmup`): a profile whose strategy needs more
        candles than the profile ever asks the stream for goes to ``ERROR`` here,
        before the first tick, instead of polling for ever with zero signals.
        """
        if self._strategy is None:
            self._strategy = resolve_strategy(self._profile)
        if not self._warmup_checked:
            self._check_warmup()
            self._warmup_checked = True

    def _check_warmup(self) -> None:
        """Apply the warm-up contract at start time.

        The arithmetic belongs to :mod:`trading_platform.realtime.warmup`; this is
        the runner's side of it, and the two halves are deliberately different:

        * an **impossible** profile -- the strategy needs more candles than the
          profile asks the stream for, so the frame can *never* warm up -- is
          logged as ``warmup_impossible``, persisted as
          :attr:`~trading_platform.realtime.models.ProfileStatus.ERROR` with the
          actionable message as its detail, and re-raised as a
          :class:`~trading_platform.core.errors.RealtimeError`.  The orchestrator
          catches it at boot (:data:`_PROFILE_BOOT_ERRORS`) and quarantines the
          profile, or through ``_supervise`` and persists ``ERROR``: the loop ends,
          so the profile does **not** run for ever with zero signals;
        * a **merely incoherent** profile -- it asks for more candles than the
          stream window holds -- is logged as ``warmup_coherence`` and the profile
          keeps running: the frame the strategy receives is bounded by
          ``warmup_candles``, so this is reported, never fatal;
        * a profile whose frame is simply **not warm yet** (short history at the
          first ticks) is untouched here: :meth:`run_once` logs
          ``warmup_incomplete`` and lets the candles accumulate.

        The check is idempotent and runs once per healthy runner: the guard is
        armed only after a passing check, so a runner that raises here keeps
        raising on every later call instead of silently starting to trade.
        """
        findings = profile_warmup_findings(
            self._profile,
            history_candles=self._warmup if self._history is None else self._history,
        )
        for finding in findings:
            if finding.severity == SEVERITY_ERROR:
                log_event(
                    _LOGGER,
                    "warmup_impossible",
                    level=logging.ERROR,
                    profile_id=self.profile_id,
                    symbol=str(self._profile.symbol),
                    timeframe=str(self._profile.timeframe),
                    required_candles=self._required_candles(),
                    warmup_candles=self._warmup,
                )
                self._status = ProfileStatus.ERROR
                self._store.save_status(
                    self.profile_id, ProfileStatus.ERROR, detail=finding.message
                )
                raise RealtimeError(finding.message)
            log_event(
                _LOGGER,
                "warmup_coherence",
                level=logging.WARNING,
                profile_id=self.profile_id,
                symbol=str(self._profile.symbol),
                timeframe=str(self._profile.timeframe),
                warmup_candles=self._warmup,
                history_candles=self._warmup if self._history is None else self._history,
                message=finding.message,
            )

    def _required_candles(self) -> int:
        """Return the candles the resolved strategy needs on this profile's grid.

        The grid comes from the profile's own timeframe through the platform's
        single arithmetic authority
        (:func:`~trading_platform.realtime.warmup.candles_per_day`), so what the
        start-time check, the tick guard and ``realtime check`` compare can never
        drift apart.

        The member is read **defensively**: an external duck-typed strategy that
        predates the warm-up contract (a test seam, a third-party strategy)
        declares no requirement and keeps the historical ``MIN_FRAME_ROWS``
        behaviour -- exactly as the stream's ``max_wait_seconds`` is read -- because
        a seam written before a member existed must not crash the tick.
        """
        strategy = self._strategy
        if strategy is None:  # pragma: no cover - _prepare always runs first
            raise RealtimeError(f"profile {self.profile_id!r} has no resolved strategy")
        reader = getattr(strategy, "required_candles", None)
        if reader is None:
            return 0
        return int(reader(candles_per_day(str(self._profile.timeframe))))

    def _strategy_run(self, frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Run the strategy's ``prepare`` and ``signals`` on ``frame``."""
        strategy = self._strategy
        if strategy is None:  # pragma: no cover - _prepare always runs first
            raise RealtimeError(f"profile {self.profile_id!r} has no resolved strategy")
        return strategy.run(frame)

    def _seed_candle_history(self, frame: pd.DataFrame) -> None:
        """Persist the warmup window once, so the dashboard chart is never empty.

        Every row goes through the store's idempotent :meth:`append_candle` (one
        row per ``(profile_id, timestamp)``, bounded retention), so a restart
        re-seeds nothing and the table stays bounded. The rows are **display
        only**: they are never replayed as ticks and the watermark that gates a
        trading decision is untouched.
        """
        symbol = str(self._profile.symbol)
        timeframe = str(self._profile.timeframe)
        written = 0
        for stamp, row in frame.iterrows():
            known = self._store.append_candle(
                CandleEvent(
                    symbol=symbol,
                    timeframe=timeframe,
                    timestamp=_as_utc(stamp),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    closed=True,
                ),
                profile_id=self.profile_id,
            )
            if not known:
                written += 1
        log_event(
            _LOGGER,
            "candle_history_seeded",
            profile_id=self.profile_id,
            symbol=symbol,
            timeframe=timeframe,
            rows=written,
        )

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

    def _plan(
        self,
        candle: CandleEvent,
        signals: pd.DataFrame,
        prepared: pd.DataFrame | None = None,
    ) -> _OrderPlan:
        """Decide what to do on this candle: the static stop first, the signal else.

        The static stop of an open position is read intrabar, **on the current
        candle**, exactly as it always was: exits and stops act on the last row
        only and never consult the entry lookback.  The entry decision is the only
        one the lookback widens (see :meth:`_entry_basis`), and a catch-up entry
        only happens when the frame's last row still confirms the trend.
        """
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
        if position is None and self._entry_lookback() > 0:
            basis = self._entry_basis(signals, close, prepared)
            if basis.is_catch_up and basis.row_index != -1:
                return self._catch_up_plan(basis, signals, candle, position)
        return self._plan_at(signals.iloc[-1], close, position)

    def _plan_at(self, row: pd.Series, close: float, position: Position | None) -> _OrderPlan:
        """Map one signal row to a plan (the historical decision, unchanged)."""
        raw_stop = _finite_or_none(row["stop_loss"]) if "stop_loss" in row.index else None
        params = self._strategy.params if self._strategy is not None else None
        short_enabled = bool(getattr(params, "allow_short", False))
        if _flag(row, "entry_long"):
            if position is not None:
                return self._entry_while_open(close, position)
            if self._paused:
                return self._paused_plan(close)
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
            if self._paused:
                return self._paused_plan(close)
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

    def _entry_lookback(self) -> int:
        """Return the effective catch-up window of this profile, clamped.

        ``ProfileConfig`` already refuses a negative or absurd value, so the clamp
        is unreachable for a validated configuration -- it is what keeps "an
        absurd lookback degrades safely instead of raising" true even for a
        profile object built in memory that bypassed validation.
        """
        lookback = int(getattr(self._profile, "entry_lookback_candles", 0))
        return min(max(lookback, 0), MAX_ENTRY_LOOKBACK_CANDLES)

    def _entry_basis(
        self,
        signals: pd.DataFrame,
        close: float,
        prepared: pd.DataFrame | None = None,
    ) -> _EntryBasis:
        """Return the row of the signals frame the entry decision is taken from.

        With no lookback configured -- or with a frame too short to hold a
        decision -- this is the historical basis: the last row, no catch-up.
        Otherwise the last ``1 + lookback`` rows are scanned **newest first** and
        the first admissible crossover wins, provided that

        * the row carries ``entry_long``;
        * the row is strictly newer than the persisted entry watermark, so a
          crossover already acted upon can never trigger again;
        * the **current last row still confirms the trend**: ``entry_long`` is a
          one-candle cross event, so a cross N candles old is confirmed by the
          current row *not* carrying its reversal (see :meth:`_trend_reversed`).
          A cross whose trend has already reversed is refused, and every older
          candidate fails the same test, so the outcome is a ``HOLD``.

        A candidate that *is* the last row is not a catch-up at all: it is the
        historical single-row decision, and the lookback leaves it to ``_plan``.
        ``entry_lookback_candles=0`` and ``entry_lookback_candles=N`` therefore
        agree whenever the crossover sits on the current row.  The scan is bounded
        by ``len(signals)``, so a lookback larger than the frame degrades into
        scanning the whole frame -- never an index error, never a raise.

        The reference price of a row is its ``close``.  The frozen signal contract
        (:data:`~trading_platform.core.constants.SIGNAL_COLUMNS`) carries no OHLCV
        column, so the price is read from ``prepared`` -- the frame the strategy
        just consumed, indexed exactly like ``signals`` -- and falls back to the
        injected candle close when that column is unavailable.
        """
        last_index = signals.index[-1]
        prices = self._candidate_prices(signals, prepared)
        last_price = None if prices is None else _finite_or_none(prices.iloc[-1])
        fallback_price = float(close) if last_price is None else float(last_price)
        flat = _EntryBasis(
            row_index=-1,
            timestamp=_as_utc(last_index),
            reference_price=fallback_price,
            is_catch_up=False,
        )
        lookback = self._entry_lookback()
        if lookback <= 0 or len(signals) < MIN_FRAME_ROWS or prices is None:
            return flat
        last_row = signals.iloc[-1]
        watermark = self._last_acted_entry_crossing
        window = min(1 + lookback, len(signals))
        for offset in range(1, window + 1):
            index = len(signals) - offset
            row = signals.iloc[index]
            if not _flag(row, "entry_long"):
                continue
            if offset == 1:
                # The candidate is the last row itself, so this is not a
                # catch-up at all: the historical single-row branch of ``_plan``
                # takes it, and the lookback stays out of the way.  It is the
                # last candidate of the scan, because nothing older can confirm
                # a trend the current row has already acted upon.
                return flat
            if self._trend_reversed(last_row):
                # A cross N candles old whose trend has already reversed is
                # stale: ``entry_long`` is a one-candle cross event, so the
                # confirmation available on the current row is the absence of
                # its reversal.  Every older candidate fails the same test, so
                # the outcome is a HOLD.
                self._log_catch_up_refused(
                    signals, index, "the current row no longer confirms the trend"
                )
                return flat
            row_timestamp = _as_utc(signals.index[index])
            if watermark is not None and row_timestamp <= watermark:
                # Already acted upon: this candidate can never trigger again.
                continue
            price = _finite_or_none(prices.iloc[index])
            if price is None or price <= 0.0:
                # An unusable price is never traded on: skip the candidate.
                continue
            return _EntryBasis(
                row_index=index,
                timestamp=row_timestamp,
                reference_price=float(price),
                is_catch_up=True,
            )
        return flat

    @staticmethod
    def _trend_reversed(last_row: pd.Series) -> bool:
        """Return whether the frame's last row signals the reversal of the trend.

        The frozen signal contract exposes no regime column: ``entry_long`` and
        ``exit_long`` are one-candle **cross events** of ``BasicStrategy`` (a
        cross is true on the single candle where the fast line overtakes the slow
        one).  A crossover that happened N candles ago therefore *cannot* coexist
        with ``entry_long`` on the current row, and the confirmation requirement 2
        asks for is the current row **not** carrying the opposite event: an
        ``exit_long`` on the last row means the bullish trend the old cross opened
        has already been reversed, so the catch-up is refused.  A short-enabled
        profile mirrors it with ``entry_short``.
        """
        return _flag(last_row, "exit_long") or _flag(last_row, "entry_short")

    @staticmethod
    def _candidate_prices(signals: pd.DataFrame, prepared: pd.DataFrame | None) -> pd.Series | None:
        """Return the per-row close aligned with ``signals``, or ``None``.

        ``prepared`` is preferred -- it is the strategy's own frame, indexed like
        the signal one -- and a foreign signal frame carrying its own ``close``
        column is accepted as well.  Anything else returns ``None``, and the
        caller then keeps the historical last-row decision rather than raising.
        """
        for source in (prepared, signals):
            if source is None or "close" not in source.columns:
                continue
            if len(source) != len(signals) or not source.index.equals(signals.index):
                # A frame that is not aligned with the signal one cannot price a
                # signal row: an unaligned lookup would be worse than no price.
                continue
            return source["close"]
        return None

    def _catch_up_plan(
        self,
        basis: _EntryBasis,
        signals: pd.DataFrame,
        candle: CandleEvent,
        position: Position | None,
    ) -> _OrderPlan:
        """Return the plan of a catch-up entry, through the ordinary risk path.

        The plan is an ordinary ``ENTER_LONG``: it is routed by :meth:`_route`,
        counted by the same counters and funded by the same shared wallet as any
        other entry.  The reference price and the stop both come from the signal
        row the decision was taken on, so the pair can never disagree.
        """
        close = float(candle.close)
        if position is not None:
            return self._entry_while_open(close, position)
        if self._paused:
            return self._paused_plan(close)
        params = self._strategy.params if self._strategy is not None else None
        if bool(getattr(params, "allow_short", False)) and _flag(signals.iloc[-1], "entry_short"):
            # The catch-up window covers the long entry only: the short entry has
            # its own mirrored reference and no measured problem to solve.
            log_event(
                _LOGGER,
                "catch_up_refused",
                level=logging.WARNING,
                profile_id=self.profile_id,
                symbol=str(self._profile.symbol),
                reason="catch-up unavailable for short entries",
                signal_row_timestamp=basis.timestamp.isoformat(),
            )
            return _OrderPlan(
                action=SignalAction.HOLD,
                direction=None,
                reference_price=close,
                reason="catch-up unavailable for short entries",
            )
        row = signals.iloc[basis.row_index]
        raw_stop = _finite_or_none(row["stop_loss"]) if "stop_loss" in row.index else None
        log_event(
            _LOGGER,
            "catch_up_entry",
            profile_id=self.profile_id,
            symbol=str(self._profile.symbol),
            signal_row_timestamp=basis.timestamp.isoformat(),
            candles_behind=int(len(signals) - 1 - basis.row_index),
            reference_price=float(basis.reference_price),
            stop_price=None if raw_stop is None else float(raw_stop),
        )
        return _OrderPlan(
            action=SignalAction.ENTER_LONG,
            direction=Direction.LONG,
            reference_price=float(basis.reference_price),
            reason=ExitReason.SIGNAL.value,
            stop_price=raw_stop,
        )

    def _log_catch_up_refused(self, signals: pd.DataFrame, index: int, reason: str) -> None:
        """Record why an entry candidate of the lookback window was not acted upon."""
        log_event(
            _LOGGER,
            "catch_up_refused",
            level=logging.WARNING,
            profile_id=self.profile_id,
            symbol=str(self._profile.symbol),
            reason=reason,
            signal_row_timestamp=_as_utc(signals.index[index]).isoformat(),
        )

    def _record_catch_up(self, basis: _EntryBasis) -> None:
        """Persist the signal row acted upon and move the in-memory cursor, once.

        Called from the single point of :meth:`run_once` where the tick has been
        accepted, so a refused catch-up entry leaves the watermark untouched and
        the very same crossover is retried on the next tick through the ordinary
        risk path.  The store's own write is monotonic, and a store that does not
        expose it is simply left alone -- its runner then keeps the in-memory
        cursor, which is exactly one tick's worth of protection.
        """
        writer = getattr(self._store, "mark_acted_entry_crossing", None)
        if writer is not None:
            writer(self.profile_id, basis.timestamp)
        self._last_acted_entry_crossing = basis.timestamp

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

    def _paused_plan(self, close: float) -> _OrderPlan:
        """Turn an entry signal into a ``HOLD`` while the profile is paused.

        The gate is entry-only: the caller reaches this helper *after* the static
        stop of an open position and *only* from the two entry branches, so a paused
        profile keeps managing what it holds.  The decision is a ``HOLD``, never a
        blocked one, which is what makes the tick publish its candle, its equity
        point and its watermark exactly like any other tick.
        """
        log_event(
            _LOGGER,
            "entry_ignored",
            profile_id=self.profile_id,
            symbol=str(self._profile.symbol),
            reason="profile is paused",
        )
        return _OrderPlan(
            action=SignalAction.HOLD,
            direction=None,
            reference_price=close,
            reason="profile is paused",
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
        """Build the deterministic order, submit it, and report the outcome.

        Every refusal leaves its reason on :attr:`last_block_reason` -- a blocked
        order is a decision the operator must be able to read, not only a log
        line -- and an accepted order clears it.  A refusal raised by the *venue*
        counts too, the shared wallet's own one included: the ledger is debited
        only after every check passed, so a refused fill leaves no position and no
        cash movement behind.
        """
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
            self._last_block_reason = "non-positive quantity"
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
        except (
            RiskLimitExceededError,
            KillSwitchActiveError,
            OrderRejectedError,
            WalletError,
        ) as exc:
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
            self._last_block_reason = str(exc)
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
        self._last_block_reason = ""
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
        """Return the equity read of the profile at ``price`` and refresh the peaks.

        The figures are *attributed*: the cash is the profile's share of the one
        shared wallet (``allocation - deployed + realized_pnl``), never the whole
        wallet, and the open position is marked at ``price`` -- the price of the
        candle being decided on.  Marking at the tick price (instead of the last
        price the gateway routed an order at) is what keeps the published equity
        curve following the market between two orders; for a consistent attributed
        read it *is* the frozen formula ``allocation + realized_pnl +
        unrealized_pnl``, with ``unrealized_pnl = quantity * price - deployed``,
        because ``cash + position_value`` reduces to it exactly.

        Every read falls back to its historical local computation when the gateway
        does not publish it, so a gateway written before the shared wallet existed
        reports what it always did.  At the end of the read the platform-wide
        aggregates are published through the *optional* gateway seam (the house
        idiom of ``restore_cash``): that is what feeds the platform caps.
        """
        fields = self._gateway.snapshot_fields()
        cash = float(fields.get("cash", self._profile.effective_allocation))
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
        state = _EquityState(
            equity=equity,
            cash=cash,
            position_value=position_value,
            quantity=quantity,
            open_positions=len(self._store.list_positions(self.profile_id)),
            daily_pnl=equity - float(self._day_start_equity),
            daily_trades=int(self._daily_trades),
            peak_equity=float(self._peak_equity),
        )
        # The platform aggregation is fed through the optional gateway seam: the
        # exposure published is the absolute notional the profile holds, and the
        # daily P&L is the one just computed.  A gateway without the seam (or with
        # no platform state injected) is simply left alone.
        publish = getattr(self._gateway, "publish_platform_state", None)
        if publish is not None:
            publish(
                exposure=abs(float(state.position_value)),
                daily_pnl=float(state.daily_pnl),
            )
        return state

    def _restore_curve(self) -> None:
        """Restore the peak equity, the day baseline and the day from the curve.

        Without a persisted curve the peak and the day baseline start at the
        profile's **allocation** -- its share of the shared wallet -- because that
        is the capital its drawdown and its daily loss are measured against.  A
        profile with no ``allocation`` configured falls back to its own
        ``initial_balance``, exactly as before.
        """
        points = self._store.equity_curve(self.profile_id)
        initial = float(self._profile.effective_allocation)
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
        """Log, persist and remember an unexpected failure of one tick.

        The message is rendered by
        :func:`~trading_platform.realtime.observability.failure_text`, which always
        names the type of the failure: ``str(TimeoutError())`` is the empty string,
        so a profile that died on a bare timeout used to be persisted as
        ``health.last_error = "TimeoutError: "`` -- an error an operator cannot act
        on, and one that said nothing about *what* had timed out.  A failure with no
        message is now recorded as ``"<Type> (no message)"``.
        """
        message = failure_text(exc)
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
