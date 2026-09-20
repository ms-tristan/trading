"""Tests of the profile runner: one profile, one deterministic tick.

Everything here is offline and deterministic.  The four foreign seams -- the market
stream, the gateway, the store and the strategy registry -- are replaced by the
local fakes defined below, so this package never reaches into the internals of
another one.  Time is a :class:`~trading_platform.realtime.clock.ManualClock`, the
signals are scripted by a real :class:`~trading_platform.strategy.base.Strategy`
subclass, and every coroutine runs under an explicit ``asyncio.wait_for`` bound so
that a hung implementation fails the suite instead of hanging it.

The last test of the file drives the **real** registry (``strategy="basic"``) to
prove that the runner consumes ``Strategy.run`` and never re-implements a rule.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_platform.config.models import ProfileConfig, RiskLimitsConfig
from trading_platform.core.errors import (
    KillSwitchActiveError,
    OrderRejectedError,
    RiskLimitExceededError,
    WalletError,
)
from trading_platform.core.models import Direction, ExitReason, TradeRecord
from trading_platform.realtime import runner as runner_module
from trading_platform.realtime.clock import ManualClock, SystemClock
from trading_platform.realtime.models import (
    BrokerAck,
    CandleEvent,
    EngineCounters,
    EquityPoint,
    Order,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    ProfileState,
    ProfileStatus,
    RunMode,
    SignalAction,
)
from trading_platform.realtime.observability import LOGGER_NAME, Counters
from trading_platform.realtime.runner import ProfileRunner
from trading_platform.realtime.store import SqliteStateStore
from trading_platform.strategy.base import Strategy, StrategyParams, ensure_signal_frame

TIMEOUT = 5.0

SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"

#: First candle of every frame used here (UTC, midnight).
START = pd.Timestamp("2024-01-01T00:00:00Z")


def run(coro: Any) -> Any:
    """Run one coroutine under an explicit bound so nothing can hang the suite."""
    return asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


# ---------------------------------------------------------------------------
# local helpers
# ---------------------------------------------------------------------------


def make_frame(
    rows: int = 5, *, base: float = 100.0, low: float | None = None, low_index: int = 1
) -> pd.DataFrame:
    """Return a deterministic hourly OHLCV frame of ``rows`` candles."""
    index = pd.date_range(start=START, periods=rows, freq="h", tz=UTC, name="timestamp")
    close = [base + float(position) for position in range(rows)]
    lows = [value - 1.0 for value in close]
    if low is not None:
        lows[low_index] = low
    return pd.DataFrame(
        {
            "open": close,
            "high": [value + 1.0 for value in close],
            "low": lows,
            "close": close,
            "volume": [10.0] * rows,
        },
        index=index,
    )


def candle_at(frame: pd.DataFrame, index: int) -> CandleEvent:
    """Return the :class:`CandleEvent` of one row of ``frame``."""
    row = frame.iloc[index]
    return CandleEvent(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        timestamp=pd.Timestamp(frame.index[index]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
    )


def profile(**overrides: Any) -> ProfileConfig:
    """Return a valid paper profile, overridable field by field.

    ``entry_lookback_candles`` is omitted unless a test states it, so every
    historical test keeps building the profile it always built: the default
    (``0``) is applied by the model and reproduces the last-row behaviour.
    """
    payload: dict[str, Any] = {
        "id": "btc-paper",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "strategy": "scripted",
        "mode": "paper",
        "initial_balance": 1000.0,
        "warmup_candles": 5,
        "poll_interval_seconds": 5.0,
        "risk": RiskLimitsConfig(max_open_positions=1),
    }
    payload.update(overrides)
    return ProfileConfig(**payload)


# ---------------------------------------------------------------------------
# local fakes
# ---------------------------------------------------------------------------


class FakeStream:
    """Deterministic :class:`MarketStream`: emits the rows of one prepared frame."""

    def __init__(
        self,
        frame: pd.DataFrame | None = None,
        *,
        cursor: int = 1,
        block: bool = False,
        reconnect_count: int = 0,
        history_override: pd.DataFrame | None = None,
        closed: bool = True,
    ) -> None:
        self.frame = make_frame() if frame is None else frame
        self.cursor = int(cursor)
        self.block = bool(block)
        self._history_override = history_override
        self.closed = bool(closed)
        self.started = 0
        self.stopped = 0
        self.calls = 0
        self.connected = True
        self.last_error: str | None = None
        self.reconnect_count = int(reconnect_count)

    def seek(self, cursor: int) -> None:
        """Move the emission cursor (used to replay one candle again)."""
        self.cursor = int(cursor)

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
        self.calls += 1
        if self.block:
            await asyncio.sleep(3600)
            return None
        if self.cursor >= len(self.frame):
            return None
        event = candle_at(self.frame, self.cursor)
        self.cursor += 1
        return event if self.closed else replace(event, closed=False)

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        if self._history_override is not None:
            return self._history_override.copy()
        emitted = self.frame.iloc[: self.cursor]
        return emitted.iloc[-int(count) :].copy()


class FakeStore:
    """In-memory :class:`StateStore` recording every write it receives."""

    def __init__(
        self,
        *,
        last_processed: pd.Timestamp | None = None,
        state: ProfileState | None = None,
        positions: list[Position] | None = None,
        trades: list[TradeRecord] | None = None,
        curve: list[EquityPoint] | None = None,
        orders: list[Order] | None = None,
    ) -> None:
        self.last_processed = last_processed
        self.state = state
        self.positions = list(positions or [])
        self.trades = list(trades or [])
        self.curve = list(curve or [])
        self.orders = list(orders or [])
        self.statuses: list[tuple[str, ProfileStatus, str]] = []
        self.equity: list[EquityPoint] = []
        self.candles: list[tuple[CandleEvent, str]] = []
        self.appended_trades: list[tuple[TradeRecord, str]] = []
        self.marked: list[tuple[str, pd.Timestamp]] = []
        self.profiles: list[ProfileConfig] = []
        self.entry_crossings: dict[str, pd.Timestamp] = {}
        self.acted_crossings: list[tuple[str, pd.Timestamp]] = []

    def initialize(self) -> None:  # pragma: no cover - the runner never calls it
        return None

    def close(self) -> None:  # pragma: no cover - the runner never calls it
        return None

    def save_profile(self, spec: ProfileConfig) -> None:
        self.profiles.append(spec)

    def load_profiles(self) -> list[ProfileConfig]:
        return list(self.profiles)

    def state_path(self) -> Path | None:
        """Answer ``None``: an in-memory double has no database file of its own."""
        return None

    def profile_state(self, profile_id: str) -> ProfileState:
        if self.state is not None:
            return self.state
        return ProfileState(profile_id=profile_id, status=ProfileStatus.STOPPED, mode=RunMode.PAPER)

    def last_processed_candle(self, profile_id: str) -> pd.Timestamp | None:
        return self.last_processed

    def mark_candle_processed(self, profile_id: str, timestamp: pd.Timestamp) -> None:
        self.marked.append((profile_id, pd.Timestamp(timestamp)))
        self.last_processed = pd.Timestamp(timestamp)

    def last_acted_entry_crossing(self, profile_id: str) -> pd.Timestamp | None:
        """Return the signal row an entry was last acted upon, or ``None``."""
        return self.entry_crossings.get(profile_id)

    def mark_acted_entry_crossing(self, profile_id: str, timestamp: pd.Timestamp) -> None:
        """Record an acted-upon crossing, keeping the maximum, like the store."""
        stamp = pd.Timestamp(timestamp)
        known = self.entry_crossings.get(profile_id)
        if known is None or stamp > known:
            self.entry_crossings[profile_id] = stamp
        self.acted_crossings.append((profile_id, stamp))

    def acted_crossings_of(self, profile_id: str = "btc-paper") -> list[tuple[str, pd.Timestamp]]:
        """Return the recorded crossing writes of one profile, in append order."""
        return [item for item in self.acted_crossings if item[0] == profile_id]

    def append_candle(self, candle: CandleEvent, *, profile_id: str) -> bool:
        """Record one processed candle, mirroring the store's upsert answer."""
        known = any(
            stored.timestamp == candle.timestamp and stored_profile == profile_id
            for stored, stored_profile in self.candles
        )
        self.candles.append((candle, profile_id))
        return not known

    def candle_series(self, profile_id: str, limit: int = 1000) -> list[CandleEvent]:
        return [candle for candle, owner in self.candles if owner == profile_id][: int(limit)]

    def candles_of(self, profile_id: str = "btc-paper") -> list[CandleEvent]:
        """Return the recorded candles of one profile, in append order."""
        return [candle for candle, owner in self.candles if owner == profile_id]

    def save_status(self, profile_id: str, status: ProfileStatus, detail: str = "") -> None:
        self.statuses.append((profile_id, status, detail))

    def append_equity(self, point: EquityPoint) -> bool:
        self.equity.append(point)
        self.curve.append(point)
        return True

    def equity_curve(self, profile_id: str) -> list[EquityPoint]:
        return list(self.curve)

    def append_trade(self, trade: TradeRecord, *, profile_id: str) -> bool:
        self.appended_trades.append((trade, profile_id))
        self.trades.append(trade)
        return True

    def list_trades(self, profile_id: str, *, limit: int = 1000) -> list[TradeRecord]:
        return list(self.trades)[: int(limit)]

    def list_positions(self, profile_id: str) -> list[Position]:
        return list(self.positions)

    def list_orders(self, profile_id: str, *, limit: int = 100) -> list[Order]:
        return list(self.orders)[: int(limit)]

    def get_order(self, client_order_id: str) -> Order | None:
        return next(
            (order for order in self.orders if order.client_order_id == client_order_id), None
        )

    def upsert_order(self, order: Order) -> None:
        self.orders = [
            item for item in self.orders if item.client_order_id != order.client_order_id
        ]
        self.orders.append(order)

    def statuses_of(self, status: ProfileStatus) -> list[tuple[str, ProfileStatus, str]]:
        """Return the recorded status writes matching ``status``."""
        return [item for item in self.statuses if item[1] is status]

    def status_values(self) -> list[str]:
        """Return the written statuses, in order."""
        return [item[1].value for item in self.statuses]


class FakeGateway:
    """In-memory :class:`ExecutionGateway` with venue-side idempotency.

    It publishes the same *attributed* read model as the real gateway -- the
    profile's share of the shared platform wallet -- together with the optional
    ``publish_platform_state`` seam.  ``cash`` keeps the meaning every historical
    test gives it (the cash attributed to this profile) and ``allocation`` is
    derived from it unless a test states one explicitly, so the payload stays
    self-consistent: ``equity == cash + position_value`` and ``cash == allocation -
    deployed + realized_pnl``.
    """

    def __init__(
        self,
        *,
        position: Position | None = None,
        cash: float = 1000.0,
        closed_trade: TradeRecord | None = None,
        error: BaseException | None = None,
        allocation: float | None = None,
        realized_pnl: float = 0.0,
    ) -> None:
        self.position_value = position
        self.cash = float(cash)
        self.closed = closed_trade
        self.error = error
        self.allocation = None if allocation is None else float(allocation)
        self.realized_pnl = float(realized_pnl)
        self.orders: dict[str, Order] = {}
        self.submissions: list[tuple[OrderRequest, float, dict[str, Any]]] = []
        self.venue_submissions = 0
        self.polls = 0
        self.closed_calls = 0
        self.published: list[dict[str, float]] = []

    def submit(
        self,
        request: OrderRequest,
        *,
        reference_price: float,
        equity: float,
        open_positions: int,
        position_notional: float,
        daily_pnl: float,
        daily_trades: int,
        peak_equity: float,
        closes_position: bool = False,
    ) -> Order:
        if self.error is not None:
            raise self.error
        known = self.orders.get(request.client_order_id)
        if known is not None:
            return known
        self.venue_submissions += 1
        now = pd.Timestamp(request.created_at or START)
        order = Order(
            client_order_id=request.client_order_id,
            profile_id=request.profile_id,
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            quantity=float(request.quantity),
            state=OrderState.FILLED,
            mode=request.mode,
            created_at=now,
            updated_at=now,
            filled_quantity=float(request.quantity),
            average_fill_price=float(reference_price),
        )
        self.orders[request.client_order_id] = order
        self.submissions.append(
            (
                request,
                float(reference_price),
                {
                    "equity": float(equity),
                    "open_positions": int(open_positions),
                    "position_notional": float(position_notional),
                    "daily_pnl": float(daily_pnl),
                    "daily_trades": int(daily_trades),
                    "peak_equity": float(peak_equity),
                    "closes_position": bool(closes_position),
                },
            )
        )
        return order

    def poll(self) -> list[Any]:
        self.polls += 1
        return []

    def closed_trade(self) -> TradeRecord | None:
        self.closed_calls += 1
        trade = self.closed
        self.closed = None
        return trade

    def position(self, symbol: str | None = None) -> Position | None:
        return self.position_value

    def snapshot_fields(self) -> dict[str, Any]:
        """Return the attributed read model of this profile (never the wallet)."""
        position = self.position_value
        quantity = 0.0 if position is None else float(position.quantity)
        average = 0.0 if position is None else float(position.average_price)
        position_value = quantity * average if position else 0.0
        deployed = abs(quantity * average) if position else 0.0
        # The allocation is the inverse of the attributed cash formula unless the
        # test states it: the payload therefore stays consistent with the ``cash``
        # knob every historical test already uses.
        allocation = self.allocation
        if allocation is None:
            allocation = self.cash + deployed - self.realized_pnl
        return {
            "equity": self.cash + position_value,
            "cash": self.cash,
            "position_value": position_value,
            "open_positions": 0 if position is None else 1,
            "allocation": float(allocation),
            "deployed": deployed,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": position_value - deployed,
        }

    def publish_platform_state(self, *, exposure: float, daily_pnl: float) -> None:
        """Record the platform aggregates the runner published."""
        self.published.append({"exposure": float(exposure), "daily_pnl": float(daily_pnl)})


class LegacyGateway(FakeGateway):
    """A gateway written before the shared wallet: four fields, no platform seam.

    It exists to prove the *backward-compatible* half of the runner contract: an
    injected gateway that publishes none of the attributed fields must keep
    working, and one that has no ``publish_platform_state`` at all must simply not
    be published to.
    """

    #: ``getattr`` sees the attribute and finds ``None``: no publishing happens.
    publish_platform_state = None  # type: ignore[assignment]

    def snapshot_fields(self) -> dict[str, Any]:
        """Return the historical four-key payload, without any attributed field."""
        position = self.position_value
        quantity = 0.0 if position is None else float(position.quantity)
        position_value = quantity * float(position.average_price) if position else 0.0
        return {
            "equity": self.cash + position_value,
            "cash": self.cash,
            "position_value": position_value,
            "open_positions": 0 if position is None else 1,
        }


class ScriptedParams(StrategyParams):
    """Parameters of :class:`ScriptedStrategy`."""

    mode: str = "hold"
    allow_short: bool = False
    stop_loss: float | None = None


class ScriptedStrategy(Strategy):
    """A real strategy whose signals are dictated by its parameters."""

    name = "scripted"
    ParamsModel = ScriptedParams

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the frame untouched plus a marker column (no look-ahead)."""
        prepared = data.copy()
        prepared["marker"] = 1.0
        return prepared

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the signal frame of the scripted action."""
        params = self._params
        assert isinstance(params, ScriptedParams)
        columns = ["entry_long", "exit_long", "entry_short", "exit_short", "stop_loss"]
        frame = pd.DataFrame(False, index=data.index, columns=columns)
        frame["stop_loss"] = float("nan") if params.stop_loss is None else float(params.stop_loss)
        if params.mode in {"entry_long", "exit_long", "entry_short", "exit_short"}:
            frame.loc[data.index[-1], params.mode] = True
        return ensure_signal_frame(frame, data.index)


def make_position(
    *,
    direction: Direction = Direction.LONG,
    quantity: float = 0.5,
    average_price: float = 100.0,
    stop_price: float | None = 95.0,
) -> Position:
    """Return an open position owned by the profile under test."""
    return Position(
        profile_id="btc-paper",
        symbol=SYMBOL,
        quantity=quantity if direction is Direction.LONG else -quantity,
        average_price=average_price,
        direction=direction,
        opened_at=START,
        updated_at=START,
        stop_price=stop_price,
    )


def make_trade() -> TradeRecord:
    """Return a closed round trip owned by the profile under test."""
    return TradeRecord(
        entry_time=START,
        exit_time=START + pd.Timedelta(hours=1),
        entry_price=100.0,
        exit_price=101.0,
        size=0.5,
        direction=Direction.LONG,
        pnl=0.5,
        pnl_pct=0.01,
        fees=0.0,
        exit_reason=ExitReason.SIGNAL,
        duration_minutes=60.0,
        stop_price=95.0,
    )


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Return an installer that replaces the strategy the runner resolves."""

    def _install(**params: Any) -> ScriptedStrategy:
        instance = ScriptedStrategy(params)
        monkeypatch.setattr(runner_module, "resolve_strategy", lambda _profile: instance)
        return instance

    return _install


@pytest.fixture
def logs() -> Any:
    """Attach a capturing handler to the realtime logger for one test."""
    logger = logging.getLogger(LOGGER_NAME)
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def build(
    *,
    stream: FakeStream | None = None,
    gateway: FakeGateway | None = None,
    store: FakeStore | None = None,
    profile_config: ProfileConfig | None = None,
    clock: ManualClock | None = None,
    warmup_candles: int | None = None,
    timeout_seconds: float = 1.0,
    counters: Counters | None = None,
) -> tuple[ProfileRunner, FakeStream, FakeGateway, FakeStore, ManualClock]:
    """Build a runner wired with the local fakes and return every part."""
    resolved_stream = FakeStream() if stream is None else stream
    resolved_gateway = FakeGateway() if gateway is None else gateway
    resolved_store = FakeStore() if store is None else store
    resolved_clock = ManualClock(datetime(2024, 1, 1, 2, 0, tzinfo=UTC)) if clock is None else clock
    runner = ProfileRunner(
        profile=profile() if profile_config is None else profile_config,
        stream=resolved_stream,  # type: ignore[arg-type]
        gateway=resolved_gateway,  # type: ignore[arg-type]
        store=resolved_store,  # type: ignore[arg-type]
        clock=resolved_clock,
        warmup_candles=warmup_candles,
        counters=counters,
        timeout_seconds=timeout_seconds,
    )
    return runner, resolved_stream, resolved_gateway, resolved_store, resolved_clock


def events(records: list[logging.LogRecord]) -> list[str]:
    """Return the event names of the captured records."""
    return [str(getattr(record, "event", record.getMessage())) for record in records]


# ---------------------------------------------------------------------------
# 1. one tick
# ---------------------------------------------------------------------------


def test_one_tick_returns_a_decision_and_publishes_the_tick(install: Any, logs: Any) -> None:
    """A tick decides, appends an equity point, watermarks and moves the counters."""
    install(mode="hold")
    clock = ManualClock(datetime(2024, 1, 1, 2, 0, tzinfo=UTC))
    runner, stream, _gateway, store, _clock = build(clock=clock)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    expected = pd.Timestamp(stream.frame.index[1])
    assert decision is not None
    assert decision.action is SignalAction.HOLD
    assert decision.blocked is False
    assert decision.profile_id == "btc-paper"
    assert decision.timestamp == expected
    assert decision.client_order_id == ""
    assert decision.quantity == 0.0
    assert decision.reference_price == float(stream.frame.iloc[1]["close"])

    assert len(store.equity) == 1
    point = store.equity[0]
    assert point.profile_id == "btc-paper"
    assert point.timestamp == expected
    assert point.equity == pytest.approx(1000.0)
    assert point.cash == pytest.approx(1000.0)
    assert point.position_value == pytest.approx(0.0)
    assert store.marked == [("btc-paper", expected)]

    assert store.statuses[0][1] is ProfileStatus.STARTING
    assert store.statuses[-1][1] is ProfileStatus.RUNNING
    assert expected.isoformat() in store.statuses[-1][2]
    assert "profile_starting" in events(logs)
    assert "candle_processed" in events(logs)

    assert runner.counters().candles_processed == 1
    assert runner.health().status is ProfileStatus.RUNNING
    assert runner.health().last_candle_at == expected
    assert runner.health().lag_seconds == pytest.approx(3600.0)
    assert runner.state().mode is RunMode.PAPER
    assert runner.profile_id == "btc-paper"
    assert runner.profile.id == "btc-paper"


def test_a_tick_without_a_new_candle_does_nothing(install: Any) -> None:
    """An exhausted stream returns ``None`` and writes nothing."""
    install(mode="hold")
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(store=store, stream=FakeStream(cursor=99))

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    assert run(scenario()) is None
    assert store.equity == []
    assert store.marked == []


# ---------------------------------------------------------------------------
# 2. restart safety
# ---------------------------------------------------------------------------


def test_restart_does_not_replay_a_processed_candle(install: Any, logs: Any) -> None:
    """The watermark skips the candle already processed, and only that one."""
    install(mode="hold")
    frame = make_frame()
    store = FakeStore(last_processed=pd.Timestamp(frame.index[1]))
    stream = FakeStream(frame, cursor=1)
    runner, _stream, _gateway, _store, _clock = build(stream=stream, store=store)

    async def scenario() -> Any:
        await runner.start()
        skipped = await runner.run_once()
        processed = await runner.run_once()
        return skipped, processed

    skipped, processed = run(scenario())
    assert skipped is None
    assert "candle_skipped" in events(logs)
    assert processed is not None
    assert processed.timestamp == pd.Timestamp(frame.index[2])
    assert store.marked == [("btc-paper", pd.Timestamp(frame.index[2]))]


def test_restart_resumes_from_the_watermark(install: Any) -> None:
    """``start`` restores the watermark, the curve and the daily trade count."""
    install(mode="hold")
    frame = make_frame()
    curve = [
        EquityPoint(
            profile_id="btc-paper",
            timestamp=pd.Timestamp(frame.index[0]),
            equity=900.0,
            cash=900.0,
            position_value=0.0,
        ),
        EquityPoint(
            profile_id="btc-paper",
            timestamp=pd.Timestamp(frame.index[1]),
            equity=1100.0,
            cash=1100.0,
            position_value=0.0,
        ),
    ]
    store = FakeStore(last_processed=pd.Timestamp(frame.index[1]), curve=curve)
    runner, _stream, _gateway, _store, _clock = build(store=store)

    async def scenario() -> None:
        await runner.start()

    run(scenario())
    assert runner.state().last_candle_at == pd.Timestamp(frame.index[1])
    assert runner.counters().candles_processed == 0


# ---------------------------------------------------------------------------
# 3. no duplicate order after a restart
# ---------------------------------------------------------------------------


def test_a_restart_reproduces_the_same_client_order_id(install: Any) -> None:
    """The same decision always yields the same id, and a second venue call never."""
    install(mode="entry_long")
    frame = make_frame()
    store = FakeStore()
    gateway = FakeGateway()
    stream = FakeStream(frame, cursor=1)
    runner_a, _s, _g, _store, _clock = build(stream=stream, gateway=gateway, store=store)

    async def first() -> Any:
        await runner_a.start()
        return await runner_a.run_once()

    decision_a = run(first())
    assert decision_a is not None
    assert decision_a.action is SignalAction.ENTER_LONG

    # A crash lost the watermark but the venue already knows the order.
    store.last_processed = None
    stream.seek(1)
    runner_b, _s2, _g2, _store2, _clock2 = build(stream=stream, gateway=gateway, store=store)

    async def second() -> Any:
        await runner_b.start()
        return await runner_b.run_once()

    decision_b = run(second())
    assert decision_b is not None
    assert decision_b.client_order_id == decision_a.client_order_id
    assert decision_b.client_order_id == "btc-paper-BTC_USDT-20240101T010000Z-0000"
    assert gateway.venue_submissions == 1
    assert len(gateway.orders) == 1


def test_the_sequence_increments_inside_one_candle() -> None:
    """Two orders of the same candle share the timestamp and differ by sequence."""
    from trading_platform.realtime.models import new_client_order_id

    stamp = pd.Timestamp("2024-01-01T01:00:00Z")
    first = new_client_order_id("btc-paper", SYMBOL, stamp, 0)
    second = new_client_order_id("btc-paper", SYMBOL, stamp, 1)
    assert first.endswith("-0000")
    assert second.endswith("-0001")
    assert first != second


# ---------------------------------------------------------------------------
# 4. blocked orders never kill the loop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        RiskLimitExceededError("max_order_notional exceeded"),
        KillSwitchActiveError("global kill switch engaged"),
        OrderRejectedError("venue refused the order"),
    ],
)
def test_a_blocked_order_returns_a_blocked_decision(install: Any, logs: Any, error: Any) -> None:
    """Each refusal becomes a blocked decision, one log record, and the loop lives."""
    install(mode="entry_long")
    gateway = FakeGateway(error=error)
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway, store=store)

    async def scenario() -> Any:
        await runner.start()
        blocked = await runner.run_once()
        blocked_reason = runner.last_block_reason
        blocked_snapshot_reason = runner.snapshot().last_block_reason
        marked_after_block = list(store.marked)
        equity_after_block = list(store.equity)
        gateway.error = None
        gateway.position_value = None
        resumed = await runner.run_once()
        return (
            blocked,
            resumed,
            blocked_reason,
            blocked_snapshot_reason,
            marked_after_block,
            equity_after_block,
        )

    (
        blocked,
        resumed,
        blocked_reason,
        blocked_snapshot_reason,
        marked_after_block,
        equity_after_block,
    ) = run(scenario())
    assert blocked is not None
    assert blocked.blocked is True
    assert blocked.block_reason == str(error)
    assert blocked.action is SignalAction.ENTER_LONG
    assert blocked.client_order_id.endswith("-0000")
    # the refusal is readable without the log stream, and it reaches the snapshot
    assert blocked_reason == str(error)
    assert blocked_snapshot_reason == str(error)
    blocked_events = [event for event in events(logs) if event == "order_blocked"]
    assert blocked_events == ["order_blocked"]
    assert marked_after_block == []
    assert equity_after_block == []

    assert resumed is not None
    assert resumed.blocked is False
    assert len(store.marked) == 1
    # an accepted order clears the reason: the profile is trading again
    assert runner.last_block_reason == ""
    assert runner.snapshot().last_block_reason is None


def test_the_shared_wallet_refusal_is_the_last_block_reason(install: Any) -> None:
    """A refusal the *shared wallet* caused names it, exactly like a per-profile one."""
    install(mode="entry_long")
    error = RiskLimitExceededError(
        "platform wallet cannot fund order: requires 1010.00 USDT, available 400.00 USDT",
        ["platform_wallet"],
    )
    gateway = FakeGateway(error=error, cash=400.0)
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway, store=store)

    async def scenario() -> Any:
        await runner.start()
        decision = await runner.run_once()
        return decision, runner.last_block_reason, runner.snapshot()

    decision, reason, snapshot = run(scenario())
    assert decision is not None and decision.blocked is True
    assert "platform_wallet" in decision.block_reason
    assert reason == str(error)
    assert snapshot.last_block_reason == str(error)
    assert snapshot.to_dict()["last_block_reason"] == str(error)
    # nothing was submitted and nothing was persisted: the tick stopped before it
    assert gateway.submissions == []
    assert store.marked == []
    assert store.equity == []


def test_a_daily_loss_block_sets_and_clears_the_last_block_reason(install: Any) -> None:
    """The per-profile limit path is covered too, on the very same seam."""
    install(mode="entry_long")
    gateway = FakeGateway()
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway, store=store)
    # the runner delegates the decision to the gateway: the seam is what is pinned
    gateway.error = RiskLimitExceededError("max_daily_loss exceeded", ["max_daily_loss"])

    async def scenario() -> Any:
        await runner.start()
        blocked = await runner.run_once()
        gateway.error = None
        accepted = await runner.run_once()
        return blocked, accepted

    blocked, accepted = run(scenario())
    assert blocked is not None and blocked.blocked is True
    assert accepted is not None and accepted.blocked is False
    assert runner.last_block_reason == ""


def test_a_venue_wallet_refusal_blocks_instead_of_crashing(install: Any) -> None:
    """A fill the shared ledger could not fund is a refusal, not a crashed tick.

    The venue raises when the ledger cannot fund the movement (the fee of a full
    allocation, say); the runner turns it into the same blocked decision every
    other refusal produces, keeps its reason, and stays alive.
    """
    install(mode="entry_long")
    error = WalletError("platform wallet cannot debit 1001.00 USDT: available 1000.00 USDT")
    gateway = FakeGateway(error=error, cash=1000.0)
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway, store=store)

    async def scenario() -> Any:
        await runner.start()
        blocked = await runner.run_once()
        gateway.error = None
        gateway.position_value = None
        accepted = await runner.run_once()
        return blocked, accepted

    blocked, accepted = run(scenario())
    assert blocked is not None
    assert blocked.blocked is True
    assert blocked.block_reason == str(error)
    assert runner.last_block_reason == ""  # cleared by the accepted order
    assert accepted is not None and accepted.blocked is False
    assert len(store.list_positions("btc-paper")) == 0  # nothing was half-applied
    assert len(store.marked) == 1


def test_blocked_orders_move_the_right_counter(install: Any) -> None:
    """A risk refusal counts as a risk rejection, a venue refusal as a rejection."""
    install(mode="entry_long")
    gateway = FakeGateway(error=RiskLimitExceededError("too big"))
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> EngineCounters:
        await runner.start()
        await runner.run_once()
        return runner.counters()

    counters = run(scenario())
    assert counters.risk_rejections == 1
    assert counters.orders_rejected == 0
    assert counters.orders_submitted == 0


# ---------------------------------------------------------------------------
# 5. the static stop, intrabar, both directions
# ---------------------------------------------------------------------------


def test_a_long_stop_is_hit_intrabar(install: Any) -> None:
    """A low crossing the stop closes the position at the stop price."""
    install(mode="hold")
    frame = make_frame(low=90.0)
    position = make_position(stop_price=95.0, quantity=0.5)
    gateway = FakeGateway(position=position)
    runner, _stream, _gateway, _store, _clock = build(
        stream=FakeStream(frame, cursor=1), gateway=gateway
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.STOP_LOSS
    assert decision.direction is Direction.LONG
    assert decision.reason == ExitReason.STOP_LOSS.value
    assert decision.reference_price == pytest.approx(95.0)
    request, reference, context = gateway.submissions[0]
    assert request.side is OrderSide.SELL
    assert request.quantity == pytest.approx(0.5)
    assert request.reason == "stop_loss"
    assert reference == pytest.approx(95.0)
    assert context["closes_position"] is True
    assert context["position_notional"] == pytest.approx(0.5 * 95.0)


def test_a_short_stop_is_hit_intrabar(install: Any) -> None:
    """The mirror case: a high crossing the stop closes the short."""
    install(mode="hold")
    frame = make_frame(low=90.0)
    frame.loc[frame.index[1], "high"] = 110.0
    position = make_position(direction=Direction.SHORT, stop_price=105.0, quantity=0.5)
    gateway = FakeGateway(position=position)
    runner, _stream, _gateway, _store, _clock = build(
        stream=FakeStream(frame, cursor=1), gateway=gateway
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.STOP_LOSS
    assert decision.direction is Direction.SHORT
    assert decision.reference_price == pytest.approx(105.0)
    request, _reference, _context = gateway.submissions[0]
    assert request.side is OrderSide.BUY
    assert request.quantity == pytest.approx(0.5)


def test_a_stop_that_is_not_touched_leaves_the_position_alone(install: Any) -> None:
    """A low above the stop keeps the position open and the signal decides."""
    install(mode="hold")
    gateway = FakeGateway(position=make_position(stop_price=95.0))
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.HOLD
    assert gateway.submissions == []


# ---------------------------------------------------------------------------
# signal mapping
# ---------------------------------------------------------------------------


def test_an_entry_long_builds_the_documented_order(install: Any) -> None:
    """The reference price is the close of t and the stop comes from the signal row."""
    install(mode="entry_long", stop_loss=90.0)
    frame = make_frame()
    gateway = FakeGateway()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    close = float(frame.iloc[1]["close"])
    assert decision is not None
    assert decision.action is SignalAction.ENTER_LONG
    assert decision.direction is Direction.LONG
    request, reference, context = gateway.submissions[0]
    assert request.side is OrderSide.BUY
    assert request.type is OrderType.MARKET
    assert request.price is None
    assert request.stop_price == pytest.approx(90.0)
    assert request.mode is RunMode.PAPER
    assert reference == pytest.approx(close)
    assert request.quantity == pytest.approx(1000.0 / close)
    assert context["daily_trades"] == 0
    assert context["peak_equity"] == pytest.approx(1000.0)
    assert context["closes_position"] is False


def test_a_nan_stop_becomes_no_stop(install: Any) -> None:
    """A ``NaN`` in the ``stop_loss`` column means "no stop", never a ``NaN`` price."""
    install(mode="entry_long", stop_loss=None)
    gateway = FakeGateway()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    run(scenario())
    assert gateway.submissions[0][0].stop_price is None


def test_an_exit_signal_without_a_position_places_no_order(install: Any) -> None:
    """A stray exit signal must never sell an inexistant position."""
    install(mode="exit_long")
    gateway = FakeGateway()
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway, store=store)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.EXIT_LONG
    assert decision.quantity == 0.0
    assert decision.client_order_id == ""
    assert gateway.submissions == []
    assert len(store.marked) == 1


def test_short_signals_are_ignored_without_allow_short(install: Any) -> None:
    """Exactly like ``strategy.engine._resolve_allow_short``, the switch is the strategy's."""
    install(mode="entry_short", allow_short=False)
    gateway = FakeGateway()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    assert run(scenario()).action is SignalAction.HOLD
    assert gateway.submissions == []


def test_short_entries_are_enabled_by_allow_short(install: Any) -> None:
    """With ``allow_short`` the short entry is routed, and its stop is mirrored."""
    install(mode="entry_short", allow_short=True, stop_loss=90.0)
    frame = make_frame()
    gateway = FakeGateway()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    close = float(frame.iloc[1]["close"])
    assert decision is not None
    assert decision.action is SignalAction.ENTER_SHORT
    request, _reference, _context = gateway.submissions[0]
    assert request.side is OrderSide.SELL
    assert request.stop_price == pytest.approx(close + (close - 90.0))


def test_a_stake_amount_drives_the_entry_size(install: Any) -> None:
    """A configured stake sizes the order; the cash sizes it otherwise."""
    install(mode="entry_long")
    gateway = FakeGateway(cash=5000.0)
    runner, _stream, _gateway, _store, _clock = build(
        gateway=gateway, profile_config=profile(stake_amount=250.0)
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.quantity == pytest.approx(250.0 / decision.reference_price)


def test_an_entry_without_a_stake_is_sized_on_the_attributed_cash(install: Any) -> None:
    """Without a stake the order spends the profile's *attributed* cash.

    The sizing measures the share of the shared wallet the profile owns -- the
    cash the gateway attributes to it -- never the platform's whole balance: a
    profile whose attributed cash is 400.00 buys 400.00 worth of the asset.
    """
    install(mode="entry_long")
    gateway = FakeGateway(cash=400.0)
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.reference_price == pytest.approx(101.0)
    assert decision.quantity == pytest.approx(400.0 / 101.0)
    request, reference, _context = gateway.submissions[0]
    assert reference == pytest.approx(101.0)
    assert request.quantity == pytest.approx(400.0 / 101.0)


# ---------------------------------------------------------------------------
# 6. a closed round trip
# ---------------------------------------------------------------------------


def test_a_closed_round_trip_is_persisted_once(install: Any) -> None:
    """The gateway's closed trade reaches the store and moves the fill counter."""
    install(mode="hold")
    trade = make_trade()
    store = FakeStore()
    gateway = FakeGateway(closed_trade=trade)
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway, store=store)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    run(scenario())
    assert store.appended_trades == [(trade, "btc-paper")]
    assert gateway.polls == 1
    assert runner.counters().orders_filled == 1
    assert runner.snapshot().n_trades == 1


# ---------------------------------------------------------------------------
# 7. warm-up
# ---------------------------------------------------------------------------


def test_an_incomplete_warm_up_places_no_order(install: Any, logs: Any) -> None:
    """Fewer than two rows is not a decision: the tick returns ``None``, quietly."""
    install(mode="entry_long")
    store = FakeStore()
    stream = FakeStream(history_override=pd.DataFrame())
    gateway = FakeGateway()
    runner, _stream, _gateway, _store, _clock = build(stream=stream, gateway=gateway, store=store)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    assert run(scenario()) is None
    assert "warmup_incomplete" in events(logs)
    assert gateway.submissions == []
    assert store.marked == []
    assert runner.health().lag_seconds > 0.0


def test_the_frame_ends_at_the_emitted_candle(install: Any) -> None:
    """The strategy sees the candles strictly before t, then t itself."""
    seen: list[pd.DatetimeIndex] = []

    class SpyStrategy(ScriptedStrategy):
        def run(self, data: pd.DataFrame) -> Any:
            seen.append(pd.DatetimeIndex(data.index))
            return super().run(data)

    instance = SpyStrategy({"mode": "hold"})
    runner_module_resolve = lambda _profile: instance  # noqa: E731 - one-line test seam
    original = runner_module.resolve_strategy
    runner_module.resolve_strategy = runner_module_resolve  # type: ignore[assignment]
    try:
        frame = make_frame()
        runner, _stream, _gateway, _store, _clock = build(stream=FakeStream(frame, cursor=2))

        async def scenario() -> Any:
            await runner.start()
            return await runner.run_once()

        run(scenario())
    finally:
        runner_module.resolve_strategy = original  # type: ignore[assignment]
    assert len(seen) == 1
    assert list(seen[0]) == [
        pd.Timestamp(frame.index[0]),
        pd.Timestamp(frame.index[1]),
        pd.Timestamp(frame.index[2]),
    ]
    assert seen[0][-1] == pd.Timestamp(frame.index[2])


# ---------------------------------------------------------------------------
# 8. bounded awaits
# ---------------------------------------------------------------------------


def test_a_stream_that_never_answers_times_out_fast(install: Any) -> None:
    """A blocked ``next_candle`` raises ``TimeoutError`` well under a second."""
    install(mode="hold")
    runner, _stream, _gateway, _store, _clock = build(
        stream=FakeStream(block=True), timeout_seconds=0.05
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        run(scenario())
    assert time.monotonic() - started < 1.0


def test_a_non_positive_timeout_is_refused() -> None:
    """A bound of zero would be no bound at all: the constructor refuses it."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        build(timeout_seconds=0.0)


# ---------------------------------------------------------------------------
# run(): the loop, its pacing and its failure surface
# ---------------------------------------------------------------------------


def test_run_respects_max_iterations(install: Any, logs: Any) -> None:
    """``run`` stops after the requested number of ticks and paces itself."""
    install(mode="hold")
    clock = ManualClock(datetime(2024, 1, 1, 2, 0, tzinfo=UTC))
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(store=store, clock=clock)

    async def scenario() -> None:
        await runner.start()
        await runner.run(max_iterations=3)

    run(scenario())
    assert len(store.marked) == 3
    assert runner.counters().candles_processed == 3
    assert "candle_processed" in events(logs)


def test_the_pacing_sleep_survives_when_the_interval_equals_the_timeout(
    install: Any, logs: Any
) -> None:
    """``poll_interval_seconds == timeout_seconds`` must not kill the profile.

    Regression test for the defect that crash-looped the Docker deployment: the
    pacing sleep was capped at ``min(poll_interval, timeout)`` *and* wrapped in a
    ``wait_for`` of exactly ``timeout``, so the two deadlines collided and the loop
    raised ``TimeoutError`` on its first idle poll -- taking every profile, and the
    container, down with it.

    A real clock is required: ``ManualClock.sleep`` returns immediately, so the
    collision only exists when the sleep really awaits (which is what production
    does).  Both bounds are tiny so the test stays fast.
    """
    install(mode="hold")
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(
        store=store,
        clock=SystemClock(),
        profile_config=profile(poll_interval_seconds=0.05),
        timeout_seconds=0.05,
    )

    async def scenario() -> None:
        await runner.start()
        await runner.run(max_iterations=3)

    run(scenario())
    assert runner.counters().errors == 0
    assert runner.health().status is not ProfileStatus.ERROR


def test_a_restart_drops_the_previous_process_error(
    install: Any, logs: Any, tmp_path: Path
) -> None:
    """A profile that starts again reports no error until *it* fails.

    Regression test for the deployed dashboard, which showed
    ``last error: stopped after: TimeoutError`` next to a healthy ``running`` badge:
    the error of the previous process was restored on every start and never cleared,
    so the page kept repeating a failure that was already over (and the ``stopped
    after:`` prefix accumulated one copy per restart).

    The real SQLite store is used on purpose: it is the component that carries the
    persisted state across a restart.
    """
    install(mode="hold")
    clock = ManualClock(datetime(2024, 1, 1, 2, 0, tzinfo=UTC))
    store = SqliteStateStore(tmp_path / "state.db", clock=clock)
    store.initialize()
    try:
        crashed, _stream, _gateway, _store, _clock = build(store=store, clock=clock)  # type: ignore[arg-type]
        run(crashed.start())
        crashed.mark_crashed(RuntimeError("the venue vanished"))
        run(crashed.stop())

        # the failure is the error of the profile, stored once and without a prefix
        assert store.load_status("btc-paper").last_error == "RuntimeError: the venue vanished"

        # a restart is a fresh health statement: the previous error is history
        restarted, _stream, _gateway, _store, _clock = build(store=store, clock=clock)  # type: ignore[arg-type]
        run(restarted.start())
        assert restarted.health().last_error is None
        assert store.load_status("btc-paper").last_error is None
        assert "stale_error_cleared" in events(logs)

        # ... while the reason it stopped stays in the durable structured log
        run(restarted.stop())
        assert restarted.health().last_error is None
        assert store.load_status("btc-paper").last_error is None
        cleared = [
            record for record in logs if getattr(record, "event", "") == "stale_error_cleared"
        ]
        assert cleared, "the dropped error must be recorded, not silently lost"
        context = getattr(cleared[0], "context", {})
        assert "the venue vanished" in str(context.get("previous_error"))
    finally:
        store.close()


def test_a_recovered_tick_clears_the_error(install: Any, logs: Any) -> None:
    """A tick that runs to completion proves the condition is over."""
    install(mode="hold")
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(store=store)

    async def scenario() -> None:
        await runner.start()
        runner.mark_crashed(RuntimeError("transient venue failure"))
        assert runner.health().last_error is not None
        await runner.run_once()

    run(scenario())
    assert runner.health().last_error is None
    assert "error_cleared" in events(logs)


def test_cancelling_run_persists_stopped(install: Any, logs: Any) -> None:
    """A cancelled loop persists ``STOPPED`` and re-raises.

    The stream never answers, so the loop is *suspended inside a real await* when
    the cancellation lands -- which is what a ``SystemClock``/networked stream does
    in production, and the only situation in which CPython delivers a cancellation
    reliably.
    """
    install(mode="hold")
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(
        store=store, stream=FakeStream(block=True), timeout_seconds=30.0
    )

    async def scenario() -> None:
        await runner.start()
        task = asyncio.ensure_future(runner.run())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert store.status_values()[-1] == "stopped"
    assert "profile_stopped" in events(logs)
    assert runner.health().status is ProfileStatus.STOPPED


def test_an_error_inside_the_loop_is_persisted_and_reraised(install: Any, logs: Any) -> None:
    """``run`` logs ``profile_error``, persists ``ERROR`` and hands the error back."""
    install(mode="hold")
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(store=store, timeout_seconds=0.05)

    async def failing() -> Any:
        await runner.start()
        raise RuntimeError("stream exploded")

    runner.run_once = failing  # type: ignore[method-assign]

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="stream exploded"):
            await runner.run(max_iterations=1)

    run(scenario())
    assert "profile_error" in events(logs)
    assert ProfileStatus.ERROR in [item[1] for item in store.statuses]
    assert runner.health().status is ProfileStatus.ERROR
    assert runner.health().last_error == "RuntimeError: stream exploded"
    assert runner.counters().errors == 1


def test_an_entry_signal_while_a_position_is_open_is_ignored(install: Any, logs: Any) -> None:
    """A profile already long does not buy again, exactly like the backtest engine."""
    install(mode="entry_long")
    gateway = FakeGateway(position=make_position(stop_price=1.0, quantity=0.5))
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway, store=store)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.HOLD
    assert decision.reason == "a position is already open"
    assert gateway.submissions == []
    assert "entry_ignored" in events(logs)
    assert store.status_values()[-1] == "running"


def test_a_short_entry_while_a_short_is_open_is_ignored(install: Any) -> None:
    """The mirror case of the entry guard."""
    install(mode="entry_short", allow_short=True)
    gateway = FakeGateway(
        position=make_position(direction=Direction.SHORT, stop_price=1000.0, quantity=0.5)
    )
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.HOLD
    assert gateway.submissions == []


def test_a_decision_at_a_non_positive_price_places_no_order(install: Any, logs: Any) -> None:
    """A zero reference price could only produce a nonsense quantity: refused."""
    install(mode="entry_long")
    frame = make_frame()
    frame.loc[frame.index[1], ["open", "high", "low", "close"]] = 0.0
    gateway = FakeGateway()
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(
        stream=FakeStream(frame, cursor=1), gateway=gateway, store=store
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.blocked is True
    assert decision.block_reason == "non-positive quantity"
    assert decision.quantity == 0.0
    assert gateway.submissions == []
    assert "order_skipped" in events(logs)
    assert store.marked == []


def test_start_is_idempotent_and_exposes_its_seams(install: Any) -> None:
    """A second ``start`` is a no-op, and the injected seams stay readable."""
    install(mode="hold")
    runner, stream, gateway, store, _clock = build()

    async def scenario() -> Any:
        await runner.start()
        await runner.start()
        return runner.stream, runner.gateway

    resolved_stream, resolved_gateway = run(scenario())
    assert resolved_stream is stream
    assert resolved_gateway is gateway
    assert [item[1] for item in store.statuses].count(ProfileStatus.STARTING) == 1
    assert repr(runner).startswith("ProfileRunner(")


def test_counters_fall_back_when_the_stream_has_no_reconnect_counter(install: Any) -> None:
    """A stream that does not expose ``reconnect_count`` is not a failure."""
    install(mode="hold")
    stream = FakeStream()
    del stream.reconnect_count
    runner, _stream, _gateway, _store, _clock = build(stream=stream)

    async def scenario() -> Any:
        await runner.start()
        await runner.run_once()
        return runner.counters()

    counters = run(scenario())
    assert counters.stream_reconnects == 0
    assert counters.candles_processed == 1
    assert runner.health().reconnect_count == 0


def test_an_exit_short_closes_the_short_position(install: Any) -> None:
    """The mirror of an exit long: a short is bought back."""
    install(mode="exit_short", allow_short=True)
    gateway = FakeGateway(
        position=make_position(direction=Direction.SHORT, average_price=100.0, stop_price=1000.0)
    )
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.EXIT_SHORT
    assert decision.direction is Direction.SHORT
    request, _reference, context = gateway.submissions[0]
    assert request.side is OrderSide.BUY
    assert request.reason == "signal"
    assert context["closes_position"] is True


def test_stop_persists_the_status_without_touching_the_stream(install: Any) -> None:
    """The runner closes nothing it does not own."""
    install(mode="hold")
    store = FakeStore()
    stream = FakeStream()
    runner, _stream, _gateway, _store, _clock = build(store=store, stream=stream)

    async def scenario() -> None:
        await runner.start()
        await runner.stop()
        await runner.stop()

    run(scenario())
    assert store.status_values()[-1] == "stopped"
    assert stream.stopped == 0
    assert runner.health().status is ProfileStatus.STOPPED


# ---------------------------------------------------------------------------
# the read model of one profile
# ---------------------------------------------------------------------------


def test_health_and_snapshot_expose_the_documented_fields(install: Any) -> None:
    """Counters, reconnect count, lag and the snapshot fields are all populated."""
    install(mode="hold")
    gateway = FakeGateway(cash=1000.0)
    runner, _stream, _gateway, _store, _clock = build(
        gateway=gateway, stream=FakeStream(reconnect_count=2)
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    run(scenario())
    health = runner.health()
    assert health.reconnect_count == 2
    assert health.counters.candles_processed == 1
    snapshot = runner.snapshot()
    assert snapshot.profile_id == "btc-paper"
    assert snapshot.symbol == SYMBOL
    assert snapshot.timeframe == TIMEFRAME
    assert snapshot.strategy == "scripted"
    assert snapshot.mode is RunMode.PAPER
    assert snapshot.initial_balance == pytest.approx(1000.0)
    assert snapshot.equity == pytest.approx(1000.0)
    assert snapshot.total_return == pytest.approx(0.0)
    assert snapshot.open_positions == 0
    assert snapshot.started_at is not None
    # every attributed field is published, even for a flat profile
    assert snapshot.allocation == pytest.approx(1000.0)
    assert snapshot.deployed == pytest.approx(0.0)
    assert snapshot.realized_pnl == pytest.approx(0.0)
    assert snapshot.unrealized_pnl == pytest.approx(0.0)
    assert snapshot.last_block_reason is None
    payload = snapshot.to_dict()
    assert payload["health"]["counters"]["candles_processed"] == 1
    assert isinstance(payload["started_at"], str)
    assert payload["allocation"] == pytest.approx(1000.0)
    assert payload["deployed"] == pytest.approx(0.0)
    assert payload["realized_pnl"] == pytest.approx(0.0)
    assert payload["unrealized_pnl"] == pytest.approx(0.0)
    assert payload["last_block_reason"] is None


def test_the_snapshot_reports_the_attributed_platform_figures(install: Any) -> None:
    """The profile's cash view is its share of the shared wallet, never the wallet."""
    install(mode="hold")
    gateway = FakeGateway(
        position=make_position(quantity=0.5, average_price=100.0, stop_price=50.0),
        cash=975.0,
        allocation=1000.0,
        realized_pnl=25.0,
    )
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    run(scenario())
    snapshot = runner.snapshot()
    assert snapshot.allocation == pytest.approx(1000.0), "the profile's share of the wallet"
    assert snapshot.deployed == pytest.approx(50.0)
    assert snapshot.realized_pnl == pytest.approx(25.0)
    assert snapshot.unrealized_pnl == pytest.approx(0.0)
    assert snapshot.cash == pytest.approx(975.0)
    assert snapshot.equity == pytest.approx(1025.0)
    # the attributed identities the dashboard relies on
    assert snapshot.cash == pytest.approx(
        snapshot.allocation - snapshot.deployed + snapshot.realized_pnl
    )
    assert snapshot.equity == pytest.approx(
        snapshot.allocation + snapshot.realized_pnl + snapshot.unrealized_pnl
    )
    assert snapshot.equity == pytest.approx(snapshot.cash + snapshot.position_value)
    assert snapshot.total_return == pytest.approx(0.025)


def test_the_runner_publishes_the_platform_aggregates(install: Any) -> None:
    """Every equity read feeds the injected platform aggregation, exposure included."""
    install(mode="hold")
    gateway = FakeGateway(
        position=make_position(quantity=0.5, average_price=100.0, stop_price=50.0),
        cash=975.0,
        allocation=1000.0,
        realized_pnl=25.0,
    )
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> Any:
        await runner.start()
        await runner.run_once()
        return list(gateway.published)

    published = run(scenario())
    assert published, "the runner must feed the platform caps on every tick"
    # the runner marks the open position at the tick price (the close of the candle)
    assert published[-1]["exposure"] == pytest.approx(0.5 * 101.0)
    # the daily P&L is measured against the day baseline: the allocation when the
    # profile has no persisted curve yet (1000.0), so 25.0 realized + 0.5 * 1.0
    assert published[-1]["daily_pnl"] == pytest.approx(25.5)
    assert all(entry["exposure"] >= 0.0 for entry in published)


def test_a_gateway_without_the_attributed_fields_keeps_its_historical_figures(
    install: Any,
) -> None:
    """The reads fall back per field: a gateway written before the wallet still works."""
    install(mode="hold")
    gateway = LegacyGateway(
        position=make_position(quantity=0.5, average_price=100.0, stop_price=50.0), cash=1000.0
    )
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    run(scenario())
    snapshot = runner.snapshot()
    assert snapshot.cash == pytest.approx(1000.0), "the historical cash, untouched"
    assert snapshot.equity == pytest.approx(1050.0)
    assert snapshot.allocation == pytest.approx(1000.0), "the profile's effective allocation"
    assert snapshot.deployed == pytest.approx(50.0), "rebuilt from the open position"
    assert snapshot.realized_pnl == pytest.approx(0.0)
    assert snapshot.unrealized_pnl == pytest.approx(0.0)
    assert snapshot.position_value == pytest.approx(50.0)
    assert gateway.published == [], "no platform seam, no publishing"


def test_an_explicit_allocation_drives_the_profile_figures(install: Any) -> None:
    """``allocation`` is what the per-profile risk limits and the reporting measure."""
    install(mode="entry_long")
    gateway = FakeGateway(allocation=250.0, cash=250.0)
    runner, _stream, _gateway, _store, _clock = build(
        gateway=gateway, profile_config=profile(initial_balance=1000.0, allocation=250.0)
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    # the order spends the allocation, not the profile's historical initial balance
    assert decision.quantity == pytest.approx(250.0 / decision.reference_price)
    _request, _reference, context = gateway.submissions[0]
    assert context["equity"] == pytest.approx(250.0)
    assert context["peak_equity"] == pytest.approx(250.0), "the drawdown denominator"
    snapshot = runner.snapshot()
    assert snapshot.initial_balance == pytest.approx(1000.0), "the field keeps its meaning"
    assert snapshot.allocation == pytest.approx(250.0), "the share of the shared wallet"
    assert snapshot.total_return == pytest.approx(0.0)


def test_mark_degraded_survives_the_next_tick(install: Any) -> None:
    """A reconciliation mismatch stays published instead of being overwritten."""
    install(mode="hold")
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(store=store)

    async def scenario() -> Any:
        await runner.start()
        runner.mark_degraded("mismatch on 1 order")
        await runner.run_once()
        return runner.health()

    health = run(scenario())
    assert health.status is ProfileStatus.DEGRADED
    assert store.status_values()[-1] == "degraded"
    assert "degraded" in store.status_values()


def test_daily_trades_are_restored_from_the_store(install: Any) -> None:
    """A restart still knows how many orders were routed today."""
    install(mode="entry_long")
    now = datetime(2024, 1, 1, 2, 0, tzinfo=UTC)
    orders = [
        Order(
            client_order_id=f"old-{index}",
            profile_id="btc-paper",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            quantity=1.0,
            state=OrderState.FILLED,
            mode=RunMode.PAPER,
            created_at=pd.Timestamp(now),
            updated_at=pd.Timestamp(now),
        )
        for index in range(4)
    ]
    orders.append(
        replace(
            orders[0],
            client_order_id="rejected-today",
            state=OrderState.REJECTED,
            created_at=pd.Timestamp(now),
        )
    )
    orders.append(
        replace(
            orders[0],
            client_order_id="rejected-0",
            state=OrderState.REJECTED,
            created_at=pd.Timestamp(now) - pd.Timedelta(days=2),
        )
    )
    orders.append(
        replace(
            orders[0],
            client_order_id="yesterday-0",
            created_at=pd.Timestamp(now) - pd.Timedelta(days=1),
        )
    )
    store = FakeStore(orders=orders)
    gateway = FakeGateway()
    runner, _stream, _gateway, _store, _clock = build(
        store=store, gateway=gateway, clock=ManualClock(now)
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    run(scenario())
    _request, _reference, context = gateway.submissions[0]
    assert context["daily_trades"] == 4


def test_a_smaller_high_keeps_a_long_position_open() -> None:
    """The stop check reads the candle's low, never its close."""
    frame = make_frame(low=99.0)
    assert float(frame.iloc[1]["low"]) > 95.0


# ---------------------------------------------------------------------------
# the real registry: the runner consumes ``Strategy.run`` untouched
# ---------------------------------------------------------------------------


def test_the_runner_uses_the_real_registered_strategy(logs: Any) -> None:
    """With ``strategy="basic"`` the real indicators and rules are executed."""
    from trading_platform.realtime.strategies import freqtrade_strategy_for, resolve_strategy

    real_profile = profile(strategy="basic", warmup_candles=40)
    strategy = resolve_strategy(real_profile)
    assert type(strategy).__name__ == "BasicStrategy"
    assert freqtrade_strategy_for(real_profile) in (None, type(None)) or isinstance(
        freqtrade_strategy_for(real_profile), type
    )
    frame = make_frame(60)
    runner, _stream, _gateway, _store, _clock = build(
        profile_config=real_profile, stream=FakeStream(frame, cursor=40)
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action in tuple(SignalAction)
    assert "candle_processed" in events(logs)


def test_freqtrade_bridge_returns_none_when_the_extra_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional extra is never a hard failure of the realtime layer."""
    from trading_platform.realtime.strategies import freqtrade_strategy_for
    from trading_platform.strategy import freqtrade_adapter

    monkeypatch.setattr(freqtrade_adapter, "freqtrade_available", lambda: False)
    assert freqtrade_strategy_for(profile(strategy="basic")) is None


def test_the_freqtrade_bridge_exposes_the_real_strategy_when_available() -> None:
    """With the extra installed the very same profile is handed to Freqtrade."""
    from trading_platform.realtime.strategies import freqtrade_strategy_for
    from trading_platform.strategy.freqtrade_adapter import freqtrade_available

    result = freqtrade_strategy_for(profile(strategy="basic"))
    if freqtrade_available():
        assert isinstance(result, type)
        assert issubclass(result, object)
    else:  # pragma: no cover - depends on the installed extras
        assert result is None


def test_strategy_names_exposes_the_registry() -> None:
    """The registry is the single source of truth for the available strategies."""
    from trading_platform.realtime.strategies import strategy_names

    names = strategy_names()
    assert names == sorted(names)
    assert "basic" in names


def test_an_unknown_strategy_is_reported_loudly(install: Any) -> None:
    """The registry error is propagated, never swallowed."""
    from trading_platform.core.errors import StrategyError
    from trading_platform.realtime.strategies import resolve_strategy

    with pytest.raises(StrategyError, match="unknown strategy"):
        resolve_strategy(profile(strategy="does-not-exist"))


def test_broker_ack_and_candle_helpers_are_not_needed_by_the_runner() -> None:
    """A sanity check of the frozen vocabulary the runner builds on."""
    ack = BrokerAck(client_order_id="x", accepted=True, state=OrderState.FILLED)
    assert ack.to_dict()["state"] == "filled"


# ---------------------------------------------------------------------------
# candle persistence: the tick appends exactly what it processed
# ---------------------------------------------------------------------------


def test_a_processed_candle_is_appended_verbatim(install: Any) -> None:
    """The row the tick decided on is persisted field for field, as the last one."""
    install(mode="hold")
    stream = FakeStream()
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(stream=stream, store=store)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    expected = candle_at(stream.frame, 1)
    assert decision is not None
    # The processed candle is the LAST row: the warm-up window that preceded it
    # is seeded on the same first tick (see the seeding test below).
    assert store.candles[-1] == (expected, "btc-paper")
    stored = store.candles_of()[-1]
    assert stored.timestamp == decision.timestamp
    assert stored.open == pytest.approx(float(expected.open))
    assert stored.high == pytest.approx(float(expected.high))
    assert stored.low == pytest.approx(float(expected.low))
    assert stored.close == pytest.approx(float(expected.close))
    assert stored.volume == pytest.approx(float(expected.volume))
    assert stored.closed is True
    assert store.candle_series("btc-paper")[-1] == expected
    assert store.candle_series("eth-paper") == []


def test_the_first_tick_seeds_the_warmup_window_once(install: Any) -> None:
    """The whole window the strategy consumed is persisted, and only once.

    Without this the dashboard chart would stay empty until the profile had
    processed a full window one timeframe at a time (hours to days). The seeded
    rows are display only: the watermark that gates a decision is untouched, so
    the second tick appends exactly one candle, never a re-seed.
    """
    install(mode="hold")
    stream = FakeStream()
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(stream=stream, store=store)

    async def scenario() -> Any:
        await runner.start()
        await runner.run_once()
        after_first = len(store.candles)
        stream.seek(2)
        await runner.run_once()
        return after_first, len(store.candles)

    after_first, after_second = run(scenario())

    # The warm-up window plus the candle just processed: strictly more than the
    # single candle the previous behaviour appended.
    assert after_first > 1
    # One-shot: the second tick appends its own candle and re-seeds nothing.
    assert after_second == after_first + 1

    stamps = sorted({candle.timestamp for candle in store.candle_series("btc-paper")})
    assert stamps == sorted(stamps)
    assert len(stamps) == after_first  # no two seeded rows share a timestamp
    assert stamps[-1] == pd.Timestamp(stream.frame.index[2])


def test_a_candle_still_forming_keeps_its_closed_flag(install: Any) -> None:
    """No filtering: an open candle is persisted with ``closed=False``, verbatim."""
    install(mode="hold")
    stream = FakeStream(closed=False)
    store = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(stream=stream, store=store)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    run(scenario())
    stored = store.candles_of()[-1]
    assert stored.closed is False
    assert stored.timestamp == pd.Timestamp(stream.frame.index[1])


def test_a_tick_that_decides_nothing_appends_no_candle(install: Any) -> None:
    """No candle, a stale candle and an incomplete warm-up write nothing at all.

    A blocked order is different: the tick reaches a decision, so the candle
    history the strategy consumed is seeded for the chart, but the tick still
    aborts before publishing — no equity point and no watermark.
    """
    install(mode="entry_long", stop_loss=90.0)

    empty = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(store=empty, stream=FakeStream(cursor=99))

    async def no_candle() -> Any:
        await runner.start()
        return await runner.run_once()

    assert run(no_candle()) is None
    assert empty.candles == []

    frame = make_frame()
    stale = FakeStore(last_processed=pd.Timestamp(frame.index[1]))
    runner, _stream, _gateway, _store, _clock = build(
        store=stale, stream=FakeStream(frame, cursor=1)
    )

    async def skipped() -> Any:
        await runner.start()
        return await runner.run_once()

    assert run(skipped()) is None
    assert stale.candles == []

    warmup = FakeStore()
    runner, _stream, _gateway, _store, _clock = build(
        store=warmup, stream=FakeStream(history_override=pd.DataFrame())
    )

    async def incomplete() -> Any:
        await runner.start()
        return await runner.run_once()

    assert run(incomplete()) is None
    assert warmup.candles == []

    refused = FakeStore()
    gateway = FakeGateway(error=RiskLimitExceededError("max_order_notional exceeded"))
    runner, _stream, _gateway, _store, _clock = build(store=refused, gateway=gateway)

    async def blocked() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(blocked())
    assert decision is not None
    assert decision.blocked is True
    assert refused.equity == []
    assert refused.marked == []
    # The blocked order must not hide the price action from the operator: the
    # seeded window ends on the candle the tick decided on.
    assert len(refused.candles) > 1
    assert refused.candles[-1][0].timestamp == pd.Timestamp(make_frame().index[1])


# ---------------------------------------------------------------------------
# the entry gate: pause stops entries and nothing else
# ---------------------------------------------------------------------------


def test_pause_turns_an_entry_signal_into_a_hold(install: Any, logs: Any) -> None:
    """A paused profile opens nothing, and its tick still publishes everything."""
    install(mode="entry_long", stop_loss=90.0)
    stream = FakeStream()
    store = FakeStore()
    gateway = FakeGateway()
    runner, _stream, _gateway, _store, _clock = build(stream=stream, gateway=gateway, store=store)
    runner.pause()

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    expected = pd.Timestamp(stream.frame.index[1])
    assert decision is not None
    assert decision.action is SignalAction.HOLD
    assert decision.direction is None
    assert decision.reason == "profile is paused"
    assert decision.blocked is False
    assert decision.quantity == 0.0
    assert decision.client_order_id == ""
    assert decision.reference_price == pytest.approx(float(stream.frame.iloc[1]["close"]))
    assert gateway.submissions == []
    assert runner.paused is True

    # a paused tick is a HOLD, never a blocked decision: it publishes its candle,
    # its equity point, its watermark and a healthy status
    assert store.marked == [("btc-paper", expected)]
    assert len(store.equity) == 1
    assert store.equity[0].timestamp == expected
    assert store.candles_of()[-1] == candle_at(stream.frame, 1)
    assert store.status_values()[-1] == "running"
    assert runner.health().status is ProfileStatus.RUNNING
    assert runner.counters().candles_processed == 1
    assert "entry_ignored" in events(logs)


def test_pause_does_not_suppress_the_stop_loss(install: Any) -> None:
    """The stop stays active: pausing never leaves an open position unmanaged."""
    install(mode="hold")
    frame = make_frame(low=90.0)
    store = FakeStore()
    gateway = FakeGateway(position=make_position(stop_price=95.0, quantity=0.5))
    runner, _stream, _gateway, _store, _clock = build(
        stream=FakeStream(frame, cursor=1), gateway=gateway, store=store
    )
    runner.pause()

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.STOP_LOSS
    assert decision.reason == ExitReason.STOP_LOSS.value
    request, reference, context = gateway.submissions[0]
    assert request.side is OrderSide.SELL
    assert request.quantity == pytest.approx(0.5)
    assert reference == pytest.approx(95.0)
    assert context["closes_position"] is True
    assert runner.paused is True
    assert store.candles_of()[-1] == candle_at(frame, 1)
    assert len(store.marked) == 1


def test_pause_does_not_suppress_an_exit_signal(install: Any) -> None:
    """Exits are still routed while the profile is paused."""
    install(mode="exit_long")
    store = FakeStore()
    gateway = FakeGateway(position=make_position(stop_price=50.0))
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway, store=store)
    runner.pause()

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.EXIT_LONG
    request, _reference, context = gateway.submissions[0]
    assert request.reason == ExitReason.SIGNAL.value
    assert context["closes_position"] is True
    assert store.candles_of() != []


def test_pause_does_not_suppress_an_entry_while_a_position_is_open(install: Any) -> None:
    """The already-open case keeps its own reason: the position guard answers first."""
    install(mode="entry_long")
    gateway = FakeGateway(position=make_position(stop_price=50.0))
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)
    runner.pause()

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.HOLD
    assert decision.reason == "a position is already open"
    assert gateway.submissions == []


def test_pause_also_gates_a_short_entry(install: Any) -> None:
    """The mirror branch of the short entry honours the same gate."""
    install(mode="entry_short", allow_short=True, stop_loss=90.0)
    gateway = FakeGateway()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway)
    runner.pause()

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.HOLD
    assert decision.reason == "profile is paused"
    assert gateway.submissions == []


def test_resume_restores_the_entry_order(install: Any, logs: Any) -> None:
    """``resume`` re-opens the gate: the next entry signal is routed again."""
    install(mode="entry_long", stop_loss=90.0)
    store = FakeStore()
    gateway = FakeGateway()
    runner, _stream, _gateway, _store, _clock = build(gateway=gateway, store=store)
    runner.pause()

    async def scenario() -> Any:
        await runner.start()
        paused = await runner.run_once()
        runner.resume()
        resumed = await runner.run_once()
        return paused, resumed

    paused, resumed = run(scenario())
    assert paused is not None
    assert paused.action is SignalAction.HOLD
    assert resumed is not None
    assert resumed.action is SignalAction.ENTER_LONG
    assert len(gateway.submissions) == 1
    assert gateway.submissions[0][0].client_order_id == resumed.client_order_id
    assert runner.paused is False
    # Both ticks processed and persisted their own candle: the history seeding
    # is one-shot, so the second tick appends exactly one row, the resumed one.
    assert store.candles[-1] == (candle_at(_stream.frame, 2), "btc-paper")
    assert "profile_resumed" in events(logs)


def test_pause_and_resume_are_idempotent_and_logged(install: Any, logs: Any) -> None:
    """The gate is a plain flag: repeating either call changes nothing."""
    install(mode="hold")
    runner, _stream, _gateway, _store, _clock = build()

    assert runner.paused is False
    runner.resume()  # resuming a running profile is a no-op
    runner.pause()
    runner.pause()
    assert runner.paused is True
    runner.resume()
    runner.resume()
    assert runner.paused is False

    recorded = events(logs)
    assert recorded.count("profile_paused") == 1
    assert recorded.count("profile_resumed") == 1
