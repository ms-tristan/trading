"""Tests of the realtime read model (work package wp8).

The monitor is exercised against a **local** in-memory ``FakeStore`` that
implements the ``StateStore`` protocol, and against an injected ``ManualClock``:
nothing here touches SQLite, the network or wall-clock time, and every assertion
is reproducible on two runs.

The suite deliberately re-derives its reference values by calling
``compute_metrics`` / ``compare_benchmark`` / ``validate_benchmark`` directly, so
a silently re-implemented formula inside the monitor would fail these tests
instead of passing them.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from trading_platform.config.models import ProfileConfig, RealtimeConfig
from trading_platform.core.constants import (
    DEFAULT_FEE_RATE,
    DEFAULT_INITIAL_BALANCE,
    DEFAULT_SLIPPAGE,
    DEFAULT_TIMEFRAME,
)
from trading_platform.core.errors import MonitoringError, StateStoreError
from trading_platform.core.models import BacktestResult, Direction, ExitReason, TradeRecord
from trading_platform.metrics import METRIC_NAMES, compare_benchmark, compute_metrics
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import (
    EngineCounters,
    EquityPoint,
    Order,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    ProfileHealth,
    ProfileState,
    ProfileStatus,
    RunMode,
)
from trading_platform.realtime.monitor import Monitor, ProfileReport

# ---------------------------------------------------------------------------
# deterministic fixtures (no randomness, no wall clock)
# ---------------------------------------------------------------------------

PROFILE_ID = "btc-paper"
OTHER_ID = "eth-live"
START = datetime(2024, 1, 1, tzinfo=UTC)
_FREQUENCIES: dict[str, str] = {"1h": "1h", "4h": "4h"}


def make_clock() -> ManualClock:
    """Return a manual clock anchored after the fixture data."""
    return ManualClock(datetime(2024, 6, 1, tzinfo=UTC))


def make_spec(
    profile_id: str = PROFILE_ID,
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    strategy: str = "basic",
    mode: str = "paper",
    initial_balance: float = 10_000.0,
    allocation: float | None = None,
) -> ProfileConfig:
    """Return one fully deterministic profile specification.

    ``allocation`` is the profile's share of the one shared platform wallet and
    stays optional: when it is absent the profile's own ``initial_balance`` **is**
    its allocation (the backward-compatible rule of the delivery brief).
    """
    return ProfileConfig(
        id=profile_id,
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        params={"fast": 10, "slow": 30},
        mode=mode,
        initial_balance=initial_balance,
        allocation=allocation,
        warmup_candles=50,
        poll_interval_seconds=1.0,
    )


def make_equity(
    profile_id: str = PROFILE_ID,
    *,
    count: int = 50,
    timeframe: str = "1h",
    initial_balance: float = 10_000.0,
    nan_at: int | None = None,
) -> list[EquityPoint]:
    """Return a deterministic equity curve of ``count`` points."""
    step = timedelta(hours=1 if timeframe == "1h" else 4)
    points: list[EquityPoint] = []
    equity = initial_balance
    for index in range(count):
        equity = equity * (1.0 + 0.001 * (1 if index % 3 else -1))
        value = float("nan") if nan_at == index else equity
        points.append(
            EquityPoint(
                profile_id=profile_id,
                timestamp=pd.Timestamp(START + index * step),
                equity=value,
                cash=value * 0.4,
                position_value=value * 0.6,
            )
        )
    return points


def make_trade(index: int, profile_id: str = PROFILE_ID) -> TradeRecord:
    """Return one deterministic closed round-trip."""
    entry = pd.Timestamp(START + timedelta(hours=index * 5))
    exit_time = entry + timedelta(hours=3)
    entry_price = 100.0 + index
    exit_price = entry_price + 2.0
    return TradeRecord(
        entry_time=entry,
        exit_time=exit_time,
        entry_price=entry_price,
        exit_price=exit_price,
        size=1.0,
        direction=Direction.LONG,
        pnl=2.0 * (1 if index % 2 else -1),
        pnl_pct=0.02 * (1 if index % 2 else -1),
        fees=0.2,
        exit_reason=ExitReason.SIGNAL,
        duration_minutes=180.0,
        params_id=f"p{index}",
    )


def make_trades(profile_id: str = PROFILE_ID, *, count: int = 3) -> list[TradeRecord]:
    """Return ``count`` deterministic round-trips."""
    return [make_trade(index, profile_id) for index in range(count)]


def make_position(profile_id: str = PROFILE_ID, symbol: str = "BTC/USDT") -> Position:
    """Return one deterministic open position."""
    return Position(
        profile_id=profile_id,
        symbol=symbol,
        quantity=0.5,
        average_price=100.0,
        direction=Direction.LONG,
        opened_at=pd.Timestamp(START),
        updated_at=pd.Timestamp(START + timedelta(hours=1)),
        unrealized_pnl=1.5,
    )


def make_order(profile_id: str = PROFILE_ID) -> Order:
    """Return one deterministic persisted order."""
    stamp = pd.Timestamp(START)
    return Order(
        client_order_id=f"{profile_id}-BTC_USDT-20240101T000000Z-0000",
        profile_id=profile_id,
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        quantity=0.5,
        state=OrderState.FILLED,
        mode=RunMode.PAPER,
        created_at=stamp,
        updated_at=stamp,
        filled_quantity=0.5,
        average_fill_price=100.5,
    )


def make_frame(*, count: int = 200, timeframe: str = "1h") -> pd.DataFrame:
    """Return a deterministic OHLCV frame usable by the metrics layer."""
    index = pd.date_range(
        START,
        periods=count,
        freq=_FREQUENCIES[timeframe],
        tz="UTC",
        name="timestamp",
    )
    opens = [100.0 + 5.0 * math.sin(position / 7.0) + position * 0.05 for position in range(count)]
    closes = [value + 0.3 for value in opens]
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(open_, close) + 0.5 for open_, close in zip(opens, closes, strict=True)],
            "low": [min(open_, close) - 0.5 for open_, close in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [10.0 + position for position in range(count)],
        },
        index=index,
    )


def make_state(
    profile_id: str = PROFILE_ID, *, status: ProfileStatus = ProfileStatus.RUNNING
) -> ProfileState:
    """Return one deterministic persisted profile state."""
    return ProfileState(
        profile_id=profile_id,
        status=status,
        mode=RunMode.PAPER,
        last_candle_at=pd.Timestamp(START + timedelta(hours=49)),
        lag_seconds=3.5,
        last_error=None,
        reconnect_count=2,
        updated_at=pd.Timestamp(START + timedelta(hours=49)),
    )


# ---------------------------------------------------------------------------
# local fake of the StateStore seam (never imports wp3)
# ---------------------------------------------------------------------------


class FakeStore:
    """Minimal in-memory ``StateStore`` used by every test of this module."""

    def __init__(self) -> None:
        self.profiles: dict[str, ProfileConfig] = {}
        self.equity: dict[str, list[EquityPoint]] = {}
        self.trades: dict[str, list[TradeRecord]] = {}
        self.positions: dict[str, list[Position]] = {}
        self.orders: dict[str, Order] = {}
        self.status: dict[str, ProfileState] = {}
        self.meta: dict[str, str] = {}
        self.candles: dict[str, pd.Timestamp] = {}
        self.failure: Exception | None = None
        self.calls: list[str] = []

    # -- helper -------------------------------------------------------------

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.failure is not None:
            raise self.failure

    # -- lifecycle ----------------------------------------------------------

    def initialize(self) -> None:
        self._record("initialize")

    def close(self) -> None:
        self._record("close")

    # -- profiles -----------------------------------------------------------

    def save_profile(self, spec: ProfileConfig) -> None:
        self._record("save_profile")
        self.profiles[str(spec.id)] = spec

    def load_profiles(self) -> list[ProfileConfig]:
        self._record("load_profiles")
        return [self.profiles[key] for key in sorted(self.profiles)]

    # -- orders -------------------------------------------------------------

    def upsert_order(self, order: Order) -> None:
        self._record("upsert_order")
        self.orders[order.client_order_id] = order

    def get_order(self, client_order_id: str) -> Order | None:
        self._record("get_order")
        return self.orders.get(client_order_id)

    def list_orders(self, profile_id: str, *, limit: int = 100) -> list[Order]:
        self._record("list_orders")
        found = [order for order in self.orders.values() if order.profile_id == profile_id]
        return found[:limit]

    # -- positions ----------------------------------------------------------

    def upsert_position(self, position: Position) -> None:
        self._record("upsert_position")
        self.positions.setdefault(position.profile_id, [])
        kept = [p for p in self.positions[position.profile_id] if p.symbol != position.symbol]
        self.positions[position.profile_id] = [*kept, position]

    def delete_position(self, profile_id: str, symbol: str) -> None:
        self._record("delete_position")
        self.positions[profile_id] = [
            p for p in self.positions.get(profile_id, []) if p.symbol != symbol
        ]

    def get_position(self, profile_id: str, symbol: str) -> Position | None:
        self._record("get_position")
        for position in self.positions.get(profile_id, []):
            if position.symbol == symbol:
                return position
        return None

    def list_positions(self, profile_id: str) -> list[Position]:
        self._record("list_positions")
        return sorted(self.positions.get(profile_id, []), key=lambda item: item.symbol)

    # -- equity, fills and trades ------------------------------------------

    def append_equity(self, point: EquityPoint) -> bool:
        self._record("append_equity")
        stored = self.equity.setdefault(point.profile_id, [])
        if any(existing.timestamp == point.timestamp for existing in stored):
            return False
        stored.append(point)
        return True

    def equity_curve(self, profile_id: str) -> list[EquityPoint]:
        self._record("equity_curve")
        return sorted(self.equity.get(profile_id, []), key=lambda item: item.timestamp)

    def append_fill(self, fill: Any) -> bool:
        self._record("append_fill")
        return True

    def append_trade(self, trade: TradeRecord, *, profile_id: str) -> bool:
        self._record("append_trade")
        self.trades.setdefault(profile_id, []).append(trade)
        return True

    def list_trades(self, profile_id: str, *, limit: int = 1000) -> list[TradeRecord]:
        self._record("list_trades")
        return self.trades.get(profile_id, [])[:limit]

    # -- status and meta ----------------------------------------------------

    def save_status(self, profile_id: str, status: ProfileStatus, detail: str = "") -> None:
        self._record("save_status")
        self.status[profile_id] = ProfileState(
            profile_id=profile_id,
            status=status,
            mode=RunMode.PAPER,
            last_error=detail or None,
        )

    def load_status(self, profile_id: str) -> ProfileState | None:
        self._record("load_status")
        return self.status.get(profile_id)

    def get_meta(self, key: str) -> str | None:
        self._record("get_meta")
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self._record("set_meta")
        self.meta[key] = value

    def last_processed_candle(self, profile_id: str) -> pd.Timestamp | None:
        self._record("last_processed_candle")
        return self.candles.get(profile_id)

    def mark_candle_processed(self, profile_id: str, timestamp: pd.Timestamp) -> None:
        self._record("mark_candle_processed")
        self.candles[profile_id] = pd.Timestamp(timestamp)

    def profile_state(self, profile_id: str) -> ProfileState:
        self._record("profile_state")
        state = self.status.get(profile_id)
        if state is not None:
            return state
        return ProfileState(
            profile_id=profile_id,
            status=ProfileStatus.STOPPED,
            mode=RunMode.PAPER,
            updated_at=None,
        )


def make_store(
    *,
    profile_id: str = PROFILE_ID,
    timeframe: str = "1h",
    symbol: str = "BTC/USDT",
    initial_balance: float = 10_000.0,
    allocation: float | None = None,
    equity_points: int | None = 50,
    trade_count: int = 3,
    with_position: bool = True,
    nan_at: int | None = None,
) -> FakeStore:
    """Return a fake store holding one fully deterministic profile."""
    store = FakeStore()
    store.save_profile(
        make_spec(
            profile_id,
            symbol=symbol,
            timeframe=timeframe,
            initial_balance=initial_balance,
            allocation=allocation,
        )
    )
    if equity_points:
        for point in make_equity(
            profile_id,
            count=equity_points,
            timeframe=timeframe,
            initial_balance=initial_balance,
            nan_at=nan_at,
        ):
            store.append_equity(point)
    for trade in make_trades(profile_id, count=trade_count):
        store.append_trade(trade, profile_id=profile_id)
    if with_position:
        store.upsert_position(make_position(profile_id, symbol))
    store.upsert_order(make_order(profile_id))
    store.status[profile_id] = make_state(profile_id)
    return store


def build_monitor(store: FakeStore, **kwargs: Any) -> Monitor:
    """Return a monitor wired to ``store`` and to a deterministic clock."""
    return Monitor(store, clock=make_clock(), **kwargs)


def assert_json_safe(payload: Any) -> None:
    """Assert that ``payload`` carries no ``NaN``/``Inf`` and is serialisable."""
    if isinstance(payload, float):
        assert math.isfinite(payload), f"non-finite float in payload: {payload!r}"
        return
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            assert isinstance(key, str)
            assert_json_safe(value)
        return
    if isinstance(payload, (list, tuple)):
        for item in payload:
            assert_json_safe(item)
        return
    assert payload is None or isinstance(payload, (bool, int, str))
    json.dumps(payload, allow_nan=False)


# ---------------------------------------------------------------------------
# 1. build_result on a populated profile
# ---------------------------------------------------------------------------


def test_build_result_from_persisted_state() -> None:
    """``build_result`` rebuilds the documented result from the stored rows only."""
    store = make_store()
    monitor = build_monitor(store)
    points = store.equity_curve(PROFILE_ID)
    trades = store.list_trades(PROFILE_ID)

    result = monitor.build_result(PROFILE_ID)

    assert isinstance(result, BacktestResult)
    assert result.strategy_name == "basic"
    assert result.symbol == "BTC/USDT"
    assert result.timeframe == "1h"
    assert result.initial_balance == pytest.approx(10_000.0)
    assert result.final_balance == pytest.approx(points[-1].equity)
    assert result.trades == trades
    assert result.n_trades == 3
    assert result.params == {"fast": 10, "slow": 30}
    assert result.metadata == {"source": "realtime", "profile_id": PROFILE_ID}
    assert pd.Timestamp(result.start) == pd.Timestamp(points[0].timestamp)
    assert pd.Timestamp(result.end) == pd.Timestamp(points[-1].timestamp)

    curve = result.equity_curve
    assert isinstance(curve, pd.Series)
    assert curve.dtype == "float64"
    assert curve.name == "equity"
    assert isinstance(curve.index, pd.DatetimeIndex)
    assert curve.index.name == "timestamp"
    assert curve.index.tz is not None
    assert str(curve.index.tz) == "UTC"
    assert list(curve.to_numpy()) == [point.equity for point in points]


def test_build_result_ignores_orders_and_positions() -> None:
    """The rebuilt result only depends on the curve, the trades and the profile."""
    store = make_store()
    monitor = build_monitor(store)
    store.orders.clear()
    store.positions.clear()

    result = monitor.build_result(PROFILE_ID)

    assert result.n_trades == 3
    assert len(result.equity_curve) == 50


# ---------------------------------------------------------------------------
# 2. build_result on an empty profile
# ---------------------------------------------------------------------------


def _reference_empty_result(profile_id: str = PROFILE_ID) -> BacktestResult:
    """Reference result of an empty profile, built without the monitor."""
    moment = pd.Timestamp(make_clock().now())
    return BacktestResult(
        strategy_name="basic",
        symbol="BTC/USDT",
        timeframe="1h",
        start=moment,
        end=moment,
        initial_balance=10_000.0,
        final_balance=10_000.0,
        trades=[],
        equity_curve=pd.Series(
            dtype="float64",
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
            name="equity",
        ),
        params={"fast": 10, "slow": 30},
        metadata={"source": "realtime", "profile_id": profile_id},
    )


def test_build_result_empty_profile_is_total() -> None:
    """An empty profile yields an empty float64 UTC curve and no exception."""
    store = make_store(equity_points=None, trade_count=0, with_position=False)
    monitor = build_monitor(store)

    result = monitor.build_result(PROFILE_ID)
    curve = result.equity_curve

    assert len(curve) == 0
    assert curve.dtype == "float64"
    assert isinstance(curve.index, pd.DatetimeIndex)
    assert curve.index.tz is not None
    assert str(curve.index.tz) == "UTC"
    assert curve.index.name == "timestamp"
    assert curve.name == "equity"
    assert result.n_trades == 0
    assert result.final_balance == pytest.approx(10_000.0)
    assert pd.Timestamp(result.start) == pd.Timestamp(result.end)

    reference = compute_metrics(
        _reference_empty_result(), timeframe="1h", risk_free_rate=0.0
    ).as_dict()
    assert monitor.metrics(PROFILE_ID) == reference


def test_unknown_profile_falls_back_to_defaults() -> None:
    """A profile that was never saved reports the documented defaults."""
    store = FakeStore()
    monitor = build_monitor(store)

    result = monitor.build_result("ghost")

    assert result.strategy_name == ""
    assert result.symbol == ""
    assert result.timeframe == DEFAULT_TIMEFRAME
    assert result.initial_balance == pytest.approx(DEFAULT_INITIAL_BALANCE)
    assert result.final_balance == pytest.approx(DEFAULT_INITIAL_BALANCE)
    assert result.params == {}
    assert monitor.initial_balance("ghost") == pytest.approx(DEFAULT_INITIAL_BALANCE)


def test_initial_balance_prefers_the_persisted_profile() -> None:
    """The stored profile wins over the constant default."""
    store = make_store(initial_balance=2_500.0)
    monitor = build_monitor(store)

    assert monitor.initial_balance(PROFILE_ID) == pytest.approx(2_500.0)
    assert monitor.build_result(PROFILE_ID).initial_balance == pytest.approx(2_500.0)


def test_the_capital_base_is_attributed_to_the_allocation() -> None:
    """A profile is measured against its *share* of the one shared wallet.

    The profile is configured with ``initial_balance = 10000`` and
    ``allocation = 2500``: the read model reports the allocation as its capital
    base, so every metric and every report line is attributed to 2500 -- never to
    the whole platform ledger -- while the configured capital itself is untouched.
    """
    store = make_store(initial_balance=10_000.0, allocation=2_500.0)
    monitor = build_monitor(store)
    spec = store.load_profiles()[0]
    points = store.equity_curve(PROFILE_ID)

    result = monitor.build_result(PROFILE_ID)
    report = monitor.report(PROFILE_ID)

    assert spec.initial_balance == pytest.approx(10_000.0)
    assert spec.effective_allocation == pytest.approx(2_500.0)
    assert monitor.initial_balance(PROFILE_ID) == pytest.approx(2_500.0)
    assert result.initial_balance == pytest.approx(2_500.0)
    assert report.initial_balance == pytest.approx(2_500.0)
    # the equity curve itself is the profile's own, untouched
    assert result.final_balance == pytest.approx(points[-1].equity)
    assert report.final_balance == pytest.approx(points[-1].equity)
    # the metric set is exactly the one of the attributed capital base ...
    attributed = replace(result, initial_balance=2_500.0)
    assert (
        monitor.metrics(PROFILE_ID)
        == compute_metrics(attributed, timeframe="1h", risk_free_rate=0.0).as_dict()
    )
    # ... and the return denominator is the allocation, derived by hand here
    expected_return = (points[-1].equity - 2_500.0) / 2_500.0
    assert monitor.metrics(PROFILE_ID)["total_return"] == pytest.approx(expected_return)
    assert report.total_return == pytest.approx(expected_return)


def test_a_profile_without_an_allocation_is_unchanged() -> None:
    """Backward compatibility: no ``allocation`` means ``initial_balance`` **is** it."""
    store = make_store(initial_balance=10_000.0)
    monitor = build_monitor(store)
    spec = store.load_profiles()[0]

    result = monitor.build_result(PROFILE_ID)
    report = monitor.report(PROFILE_ID)

    assert spec.allocation is None
    assert spec.effective_allocation == pytest.approx(10_000.0)
    assert monitor.initial_balance(PROFILE_ID) == pytest.approx(10_000.0)
    assert result.initial_balance == pytest.approx(10_000.0)
    assert report.initial_balance == pytest.approx(10_000.0)
    # the numbers are exactly the ones the profile reported before the shared
    # wallet existed: the configured balance is the capital base of the metrics
    assert (
        monitor.metrics(PROFILE_ID)
        == compute_metrics(result, timeframe="1h", risk_free_rate=0.0).as_dict()
    )
    assert report.total_return == pytest.approx(
        compute_metrics(result, timeframe="1h", risk_free_rate=0.0).as_dict()["total_return"]
    )


def test_window_falls_back_to_the_trades_and_the_positions() -> None:
    """A profile without a saved spec is still dated and named by its own rows."""
    store = FakeStore()
    for trade in make_trades(PROFILE_ID, count=3):
        store.append_trade(trade, profile_id=PROFILE_ID)
    store.upsert_position(make_position(PROFILE_ID))
    monitor = build_monitor(store)
    trades = store.list_trades(PROFILE_ID)

    result = monitor.build_result(PROFILE_ID)
    report = monitor.report(PROFILE_ID)

    assert result.symbol == "BTC/USDT"
    assert result.strategy_name == ""
    assert result.timeframe == DEFAULT_TIMEFRAME
    assert pd.Timestamp(result.start) == min(pd.Timestamp(trade.entry_time) for trade in trades)
    assert pd.Timestamp(result.end) == max(pd.Timestamp(trade.exit_time) for trade in trades)
    assert len(result.equity_curve) == 0
    assert result.final_balance == pytest.approx(DEFAULT_INITIAL_BALANCE)
    assert report.mode is RunMode.PAPER
    assert report.status is ProfileStatus.STOPPED
    assert report.n_trades == 3


# ---------------------------------------------------------------------------
# 3. metrics are delegated, never recomputed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", ["1h", "4h"])
@pytest.mark.parametrize("risk_free_rate", [0.0, 0.05])
def test_metrics_equal_compute_metrics(timeframe: str, risk_free_rate: float) -> None:
    """``metrics()`` is exactly ``compute_metrics(...).as_dict()``."""
    store = make_store(timeframe=timeframe)
    monitor = build_monitor(store, risk_free_rate=risk_free_rate)

    result = monitor.build_result(PROFILE_ID)
    reference = compute_metrics(
        result, timeframe=timeframe, risk_free_rate=risk_free_rate
    ).as_dict()

    assert monitor.metrics(PROFILE_ID) == reference
    assert set(reference) == set(METRIC_NAMES)
    assert monitor.risk_free_rate == pytest.approx(risk_free_rate)


# ---------------------------------------------------------------------------
# 4. benchmark
# ---------------------------------------------------------------------------


def test_benchmark_is_none_without_a_frame() -> None:
    """No frame, or a single row, leaves the benchmark block null."""
    store = make_store()
    monitor = build_monitor(store)

    assert monitor.benchmark(PROFILE_ID) is None
    assert monitor.benchmark(PROFILE_ID, None) is None
    assert monitor.benchmark(PROFILE_ID, make_frame(count=1)) is None


def test_benchmark_equals_compare_benchmark() -> None:
    """The buy & hold payload is the metrics layer's own payload."""
    store = make_store()
    monitor = build_monitor(store)
    frame = make_frame(count=200)

    result = monitor.build_result(PROFILE_ID)
    expected = compare_benchmark(
        result,
        frame,
        variant="buy_and_hold",
        initial_balance=result.initial_balance,
        fee_rate=DEFAULT_FEE_RATE,
        slippage=DEFAULT_SLIPPAGE,
        risk_free_rate=0.0,
    )
    assert expected is not None
    expected_payload = expected.to_dict()

    payload = monitor.benchmark(PROFILE_ID, frame)

    assert payload == expected_payload
    assert payload is not None
    assert set(payload) == {
        "variant",
        "timeframe",
        "initial_balance",
        "fee_rate",
        "slippage",
        "n_periods",
        "n_returns",
        "alpha",
        "beta",
        "correlation",
        "strategy",
        "benchmark",
        "gap",
    }
    assert payload["variant"] == "buy_and_hold"
    assert_json_safe(payload)


def test_benchmark_variant_none_is_null() -> None:
    """Variant ``none`` disables the benchmark entirely."""
    store = make_store()
    monitor = build_monitor(store, benchmark_variant="none")

    assert monitor.benchmark_variant == "none"
    assert monitor.benchmark(PROFILE_ID, make_frame(count=200)) is None
    assert monitor.gate(PROFILE_ID, make_frame(count=200)) is None


def test_benchmark_random_entry_goes_through_the_validation_layer() -> None:
    """Variant ``random_entry`` is answered by ``validate_random_entry``."""
    try:  # pragma: no cover - the validation layer always ships with this repo
        from trading_platform.validation.random_entry import validate_random_entry
    except ImportError:  # pragma: no cover - defensive, mirrors the lazy import
        pytest.skip("the validation layer is not importable in this environment")

    store = make_store()
    monitor = build_monitor(store, benchmark_variant="random_entry")
    frame = make_frame(count=200)

    payload = monitor.benchmark(PROFILE_ID, frame)

    assert payload is not None
    assert payload["distribution"]["variant"] == "random_entry"
    assert set(payload) >= {"percentile", "p_value", "distribution", "strategy_total_return"}
    assert_json_safe(payload)

    reference = validate_random_entry(
        monitor.build_result(PROFILE_ID),
        frame,
        fee_rate=DEFAULT_FEE_RATE,
        slippage=DEFAULT_SLIPPAGE,
    ).to_dict()
    assert payload == reference


# ---------------------------------------------------------------------------
# 5. gate
# ---------------------------------------------------------------------------


def test_gate_payload_matches_validate_benchmark() -> None:
    """The gate payload is the one produced by the validation layer."""
    from trading_platform.validation.benchmark import validate_benchmark

    store = make_store()
    monitor = build_monitor(store)
    frame = make_frame(count=200)

    payload = monitor.gate(PROFILE_ID, frame)

    reference = validate_benchmark(
        monitor.build_result(PROFILE_ID),
        frame,
        variant="buy_and_hold",
        fee_rate=DEFAULT_FEE_RATE,
        slippage=DEFAULT_SLIPPAGE,
        risk_free_rate=0.0,
    )
    assert reference is not None
    assert payload == reference.to_dict()
    assert payload is not None
    assert set(payload) == {
        "variant",
        "strategy_beats_benchmark",
        "alpha",
        "beta",
        "correlation",
        "min_alpha",
        "n_returns",
        "initial_balance",
        "fee_rate",
        "slippage",
        "strategy_total_return",
        "benchmark_total_return",
    }
    assert_json_safe(payload)


# ---------------------------------------------------------------------------
# 6. the report payload is JSON-safe
# ---------------------------------------------------------------------------


def test_report_to_dict_is_json_safe() -> None:
    """No NaN/Inf, the documented equity keys and TradeRecord payloads."""
    store = make_store(nan_at=10)
    monitor = build_monitor(store)
    frame = make_frame(count=200)

    report = monitor.report(PROFILE_ID, ohlcv=frame)

    assert isinstance(report, ProfileReport)
    payload = report.to_dict()
    assert_json_safe(payload)
    json.dumps(payload, allow_nan=False)

    assert payload["profile_id"] == PROFILE_ID
    assert payload["symbol"] == "BTC/USDT"
    assert payload["timeframe"] == "1h"
    assert payload["strategy"] == "basic"
    assert payload["mode"] == "paper"
    assert payload["status"] == "running"
    assert payload["n_trades"] == 3
    assert payload["generated_at"] == make_clock().now().isoformat()
    assert payload["health"]["profile_id"] == PROFILE_ID
    assert payload["health"]["status"] == "running"
    assert payload["health"]["counters"] == EngineCounters().to_dict()

    assert len(payload["equity"]) == 50
    for point in payload["equity"]:
        assert set(point) == {"timestamp", "equity", "cash", "position_value"}
    assert payload["equity"][10]["equity"] is None  # the injected NaN became null

    assert payload["trades"] == [trade.to_dict() for trade in report.trades]
    assert payload["positions"] == [position.to_dict() for position in report.positions]
    assert isinstance(payload["benchmark"], dict)


def test_report_without_frame_has_a_null_benchmark() -> None:
    """Without an OHLCV frame the benchmark block is ``None``."""
    store = make_store()
    monitor = build_monitor(store)

    payload = monitor.report(PROFILE_ID).to_dict()

    assert payload["benchmark"] is None
    assert_json_safe(payload)


def test_report_total_return_comes_from_the_metric_set() -> None:
    """``total_return`` is read out of the delegated metric set."""
    store = make_store()
    monitor = build_monitor(store)

    report = monitor.report(PROFILE_ID)

    assert report.total_return == report.metrics["total_return"]
    assert report.final_balance == pytest.approx(report.equity[-1].equity)
    assert report.mode is RunMode.PAPER
    assert report.status is ProfileStatus.RUNNING


def test_report_of_a_nan_final_balance_stays_total() -> None:
    """A non-finite final balance never poisons the report."""
    store = make_store(nan_at=49)
    monitor = build_monitor(store)

    report = monitor.report(PROFILE_ID)

    assert report.final_balance == pytest.approx(DEFAULT_INITIAL_BALANCE)
    assert_json_safe(report.to_dict())


# ---------------------------------------------------------------------------
# 7. reports, collections and health
# ---------------------------------------------------------------------------


def test_reports_are_sorted_by_profile_id() -> None:
    """One report per stored profile, sorted by identifier."""
    store = make_store()
    store.save_profile(make_spec(OTHER_ID, symbol="ETH/USDT", timeframe="1h", mode="live"))
    store.status[OTHER_ID] = make_state(OTHER_ID, status=ProfileStatus.DEGRADED)
    monitor = build_monitor(store)

    reports = monitor.reports()

    assert monitor.profile_ids() == sorted([PROFILE_ID, OTHER_ID])
    assert [report.profile_id for report in reports] == sorted([PROFILE_ID, OTHER_ID])
    assert {report.status for report in reports} == {
        ProfileStatus.RUNNING,
        ProfileStatus.DEGRADED,
    }
    for report in reports:
        assert_json_safe(report.to_dict())


def test_reports_accept_a_per_profile_frame_mapping() -> None:
    """The frame mapping feeds the benchmark block of the matching profile."""
    store = make_store()
    store.save_profile(make_spec(OTHER_ID, symbol="ETH/USDT"))
    monitor = build_monitor(store)

    reports = {
        report.profile_id: report
        for report in monitor.reports(ohlcv={PROFILE_ID: make_frame(count=200)})
    }

    assert reports[PROFILE_ID].benchmark is not None
    assert reports[OTHER_ID].benchmark is None


def test_health_of_an_unknown_profile_is_the_stopped_default() -> None:
    """An unknown profile renders a stopped badge instead of raising."""
    store = FakeStore()
    monitor = build_monitor(store)

    health = monitor.health("ghost")

    assert isinstance(health, ProfileHealth)
    assert health.profile_id == "ghost"
    assert health.status is ProfileStatus.STOPPED
    assert health.last_candle_at is None
    assert health.lag_seconds == 0.0
    assert health.last_error is None
    assert health.reconnect_count == 0
    assert health.counters == EngineCounters()
    assert health.to_dict()["status"] == "stopped"


def test_health_and_collections_come_from_the_store() -> None:
    """Health, equity, trades, orders and positions are read through the seam."""
    store = make_store()
    monitor = build_monitor(store)

    health = monitor.health(PROFILE_ID)

    assert health.status is ProfileStatus.RUNNING
    assert health.reconnect_count == 2
    assert health.lag_seconds == pytest.approx(3.5)
    assert health.last_candle_at == pd.Timestamp(START + timedelta(hours=49))
    assert len(monitor.equity(PROFILE_ID)) == 50
    assert len(monitor.trades(PROFILE_ID)) == 3
    assert len(monitor.orders(PROFILE_ID)) == 1
    assert len(monitor.orders(PROFILE_ID, limit=0)) == 0
    assert [position.symbol for position in monitor.positions(PROFILE_ID)] == ["BTC/USDT"]
    assert all(isinstance(item, EquityPoint) for item in monitor.equity(PROFILE_ID))
    assert all(isinstance(item, TradeRecord) for item in monitor.trades(PROFILE_ID))


def test_repr_is_secret_free_and_descriptive() -> None:
    """The monitor never holds credentials, and says what it will do."""
    monitor = build_monitor(make_store(), risk_free_rate=0.05)

    text = repr(monitor)

    assert "Monitor(" in text
    assert "buy_and_hold" in text
    assert "0.05" in text


# ---------------------------------------------------------------------------
# 8. persistence failures become MonitoringError
# ---------------------------------------------------------------------------


def test_store_failure_becomes_monitoring_error() -> None:
    """A failing store is reported as a read-model failure naming the profile."""
    store = make_store()
    store.failure = StateStoreError("database is locked")
    monitor = build_monitor(store)

    with pytest.raises(MonitoringError) as error:
        monitor.report(PROFILE_ID)

    assert PROFILE_ID in str(error.value)
    assert "database is locked" in str(error.value)


@pytest.mark.parametrize(
    "call",
    [
        lambda monitor: monitor.build_result(PROFILE_ID),
        lambda monitor: monitor.metrics(PROFILE_ID),
        lambda monitor: monitor.equity(PROFILE_ID),
        lambda monitor: monitor.trades(PROFILE_ID),
        lambda monitor: monitor.orders(PROFILE_ID),
        lambda monitor: monitor.positions(PROFILE_ID),
        lambda monitor: monitor.health(PROFILE_ID),
        lambda monitor: monitor.initial_balance(PROFILE_ID),
        lambda monitor: monitor.profile_ids(),
    ],
)
def test_every_read_path_converts_store_failures(call: Any) -> None:
    """Each public read method converts a store failure at its boundary."""
    store = make_store()
    store.failure = StateStoreError("schema version 99 is not supported")
    monitor = build_monitor(store)

    with pytest.raises(MonitoringError) as error:
        call(monitor)

    assert "schema version 99 is not supported" in str(error.value)


# ---------------------------------------------------------------------------
# 9. wiring: the injected RealtimeConfig only seeds the defaults
# ---------------------------------------------------------------------------


def test_realtime_config_seeds_the_defaults() -> None:
    """A configuration seeds the benchmark variant and the risk-free rate."""
    store = make_store()
    config = RealtimeConfig(benchmark_variant="cash", risk_free_rate=0.05)

    seeded = Monitor(store, clock=make_clock(), realtime=config)

    assert seeded.benchmark_variant == "cash"
    assert seeded.risk_free_rate == pytest.approx(0.05)


def test_explicit_arguments_win_over_the_configuration() -> None:
    """An explicit non-default argument always beats the injected configuration."""
    store = make_store()
    config = RealtimeConfig(benchmark_variant="cash", risk_free_rate=0.05)

    monitor = Monitor(
        store,
        clock=make_clock(),
        realtime=config,
        benchmark_variant="none",
        risk_free_rate=0.02,
    )

    assert monitor.benchmark_variant == "none"
    assert monitor.risk_free_rate == pytest.approx(0.02)
