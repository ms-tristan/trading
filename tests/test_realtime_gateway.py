"""Tests of the execution gateway: the single order lifecycle shared by both modes.

Everything here is offline and deterministic.  The three foreign seams
(:class:`~trading_platform.realtime.store.StateStore`,
:class:`~trading_platform.realtime.broker.Broker`) are replaced by the local fakes
defined below, so this package never depends on the internals of another package;
the two tests that do exercise the real ``PaperBroker`` import it lazily inside the
test body and skip when it is unavailable.

The last test of the file is the mechanical proof of delivery brief D5: the gateway
source contains no ``if paper`` / ``if live`` branch beyond the live gate and the
broker-mode assertion.
"""

from __future__ import annotations

import ast
import inspect
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_platform.config.models import ProfileConfig, RiskLimitsConfig
from trading_platform.core.errors import (
    GatewayError,
    KillSwitchActiveError,
    LiveTradingForbiddenError,
    OrderRejectedError,
    RiskLimitExceededError,
)
from trading_platform.core.models import Direction, ExitReason, TradeRecord
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.gateway import ExecutionGateway
from trading_platform.realtime.models import (
    BrokerAck,
    BrokerEvent,
    BrokerEventType,
    EngineCounters,
    Fill,
    Order,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    ProfileState,
    ProfileStatus,
    ReconciliationReport,
    RunMode,
)
from trading_platform.realtime.risk import (
    KillSwitch,
    LiveTradingGate,
    RiskDecision,
    RiskLimits,
    RiskManager,
)
from trading_platform.realtime.wallet import PlatformWallet

LOGGER_NAME = "trading_platform.realtime.gateway"

#: Fixed instant every fake stamps its records with (no wall clock anywhere).
TS = pd.Timestamp("2024-01-01T00:00:00Z")

SYMBOL = "BTC/USDT"


# ---------------------------------------------------------------------------
# local fakes -- the store and the venue, both in memory
# ---------------------------------------------------------------------------


class FakeStore:
    """Dict-backed :class:`StateStore` recording every write it receives."""

    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.order_history: list[Order] = []
        self.fills: dict[str, Fill] = {}
        self.positions: dict[tuple[str, str], Position] = {}
        self.trades: list[TradeRecord] = []
        self.trade_keys: set[str] = set()
        self.equity: list[Any] = []
        self.statuses: dict[str, ProfileState] = {}
        self.meta: dict[str, str] = {}
        self.candles: dict[str, pd.Timestamp] = {}
        self.deleted_positions: list[tuple[str, str]] = []
        self.initialized = False
        self.closed = False

    # -- lifecycle ---------------------------------------------------------
    def initialize(self) -> None:
        self.initialized = True

    def close(self) -> None:
        self.closed = True

    def save_profile(self, spec: ProfileConfig) -> None:
        self.meta[f"profile:{spec.id}"] = spec.model_dump_json()

    def load_profiles(self) -> list[ProfileConfig]:
        return []

    def state_path(self) -> Path | None:
        """Answer ``None``: an in-memory double has no database file of its own."""
        return None

    # -- orders ------------------------------------------------------------
    def upsert_order(self, order: Order) -> None:
        self.orders[order.client_order_id] = order
        self.order_history.append(order)

    def get_order(self, client_order_id: str) -> Order | None:
        return self.orders.get(client_order_id)

    def list_orders(self, profile_id: str, *, limit: int = 100) -> list[Order]:
        matching = [order for order in self.orders.values() if order.profile_id == profile_id]
        return list(reversed(matching))[: int(limit)]

    # -- fills -------------------------------------------------------------
    def append_fill(self, fill: Fill) -> bool:
        if fill.fill_id in self.fills:
            return False
        self.fills[fill.fill_id] = fill
        return True

    # -- positions ---------------------------------------------------------
    def upsert_position(self, position: Position) -> None:
        self.positions[(position.profile_id, position.symbol)] = position

    def delete_position(self, profile_id: str, symbol: str) -> None:
        self.deleted_positions.append((profile_id, symbol))
        self.positions.pop((profile_id, symbol), None)

    def get_position(self, profile_id: str, symbol: str) -> Position | None:
        return self.positions.get((profile_id, symbol))

    def list_positions(self, profile_id: str) -> list[Position]:
        return [position for (pid, _), position in self.positions.items() if pid == profile_id]

    # -- equity / trades ---------------------------------------------------
    def append_equity(self, point: Any) -> bool:
        self.equity.append(point)
        return True

    def equity_curve(self, profile_id: str) -> list[Any]:
        return list(self.equity)

    def append_trade(self, trade: TradeRecord, *, profile_id: str) -> bool:
        key = (
            f"{profile_id}|{pd.Timestamp(trade.entry_time).isoformat()}"
            f"|{pd.Timestamp(trade.exit_time).isoformat()}|{trade.direction.value}"
        )
        if key in self.trade_keys:
            return False
        self.trade_keys.add(key)
        self.trades.append(trade)
        return True

    def list_trades(self, profile_id: str, *, limit: int = 1000) -> list[TradeRecord]:
        return list(self.trades)[: int(limit)]

    # -- status / meta -----------------------------------------------------
    def save_status(self, profile_id: str, status: ProfileStatus, detail: str = "") -> None:
        self.statuses[profile_id] = ProfileState(
            profile_id=profile_id,
            status=status,
            mode=RunMode.PAPER,
            last_error=detail or None,
        )

    def load_status(self, profile_id: str) -> ProfileState | None:
        return self.statuses.get(profile_id)

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = str(value)

    def last_processed_candle(self, profile_id: str) -> pd.Timestamp | None:
        return self.candles.get(profile_id)

    def mark_candle_processed(self, profile_id: str, timestamp: pd.Timestamp) -> None:
        self.candles[profile_id] = pd.Timestamp(timestamp)

    def profile_state(self, profile_id: str) -> ProfileState:
        return self.statuses.get(
            profile_id,
            ProfileState(profile_id=profile_id, status=ProfileStatus.STOPPED, mode=RunMode.PAPER),
        )


class FakeBroker:
    """In-memory venue: records what the gateway asks and replays what it queued."""

    def __init__(
        self,
        *,
        name: str = "fake-paper",
        mode: Any = RunMode.PAPER,
        accepted: bool = True,
        ack_state: OrderState = OrderState.SUBMITTED,
        reason: str = "",
        balance: float | None = 10_000.0,
        report: ReconciliationReport | None = None,
    ) -> None:
        self._name = name
        self._mode = mode
        self.accepted = accepted
        self.ack_state = ack_state
        self.reason = reason
        self.balance_value = balance
        self.report = report
        self.submits: list[tuple[OrderRequest, float]] = []
        self.cancels: list[str] = []
        self.reconcile_expected: list[Order] = []
        self.reconcile_calls = 0
        self._events: list[BrokerEvent] = []
        self._open: list[Order] = []

    # -- Broker protocol ---------------------------------------------------
    @property
    def name(self) -> str:
        return self._name

    @property
    def mode(self) -> Any:
        return self._mode

    def submit(self, request: OrderRequest, *, reference_price: float) -> BrokerAck:
        self.submits.append((request, float(reference_price)))
        if not self.accepted:
            return BrokerAck(
                client_order_id=request.client_order_id,
                accepted=False,
                state=OrderState.REJECTED,
                reason=self.reason,
                submitted_at=TS,
            )
        return BrokerAck(
            client_order_id=request.client_order_id,
            accepted=True,
            state=self.ack_state,
            broker_order_id=f"{self._name}-{request.client_order_id}",
            submitted_at=TS,
        )

    def cancel(self, client_order_id: str) -> bool:
        self.cancels.append(client_order_id)
        return client_order_id in {order.client_order_id for order in self._open}

    def poll(self) -> list[BrokerEvent]:
        events = list(self._events)
        self._events.clear()
        return events

    def open_orders(self) -> list[Order]:
        return list(self._open)

    def fetch_balance(self) -> float | None:
        return self.balance_value

    def reconcile(self, expected: Any = ()) -> ReconciliationReport:
        self.reconcile_calls += 1
        self.reconcile_expected = list(expected)
        if self.report is not None:
            return self.report
        return ReconciliationReport(profile_id="", ok=True, checked_at=TS, matched=len(expected))

    # -- test helpers ------------------------------------------------------
    def add_open_order(self, order: Order) -> None:
        self._open.append(order)

    def queue_fill(
        self,
        *,
        client_order_id: str,
        quantity: float,
        price: float,
        side: OrderSide = OrderSide.BUY,
        profile_id: str = "btc-paper",
        fill_id: str | None = None,
        fee: float = 0.0,
        timestamp: pd.Timestamp = TS,
        state: OrderState = OrderState.FILLED,
        with_order: bool = True,
        order_mode: RunMode = RunMode.PAPER,
        filled_quantity: float | None = None,
    ) -> Fill:
        """Build one fill (and its event) and queue it for the next ``poll()``."""
        fill = Fill(
            fill_id=fill_id or f"{client_order_id}-fill-0001",
            client_order_id=client_order_id,
            profile_id=profile_id,
            symbol=SYMBOL,
            side=side,
            quantity=float(quantity),
            price=float(price),
            fee=float(fee),
            timestamp=pd.Timestamp(timestamp),
            mode=order_mode,
        )
        order = None
        if with_order:
            order = Order(
                client_order_id=client_order_id,
                profile_id=profile_id,
                symbol=SYMBOL,
                side=side,
                type=OrderType.MARKET,
                quantity=float(quantity),
                state=state,
                mode=order_mode,
                created_at=pd.Timestamp(timestamp),
                updated_at=pd.Timestamp(timestamp),
                filled_quantity=(
                    float(filled_quantity) if filled_quantity is not None else float(quantity)
                ),
                average_fill_price=float(price),
            )
            self._open = [
                existing for existing in self._open if existing.client_order_id != client_order_id
            ]
        self._events.append(
            BrokerEvent(
                event_type=_event_type_for(state),
                client_order_id=client_order_id,
                profile_id=profile_id,
                order=order,
                fill=fill,
                message="venue fill",
                timestamp=pd.Timestamp(timestamp),
            )
        )
        return fill

    def queue_event(
        self,
        event_type: BrokerEventType,
        *,
        client_order_id: str,
        profile_id: str = "btc-paper",
        message: str = "",
        order: Order | None = None,
    ) -> None:
        self._events.append(
            BrokerEvent(
                event_type=event_type,
                client_order_id=client_order_id,
                profile_id=profile_id,
                order=order,
                message=message,
                timestamp=TS,
            )
        )


class FakeRisk:
    """Recording :class:`RiskManager` stand-in returning a canned verdict."""

    def __init__(self, decision: RiskDecision | None = None) -> None:
        self.decision = RiskDecision.allow() if decision is None else decision
        self.calls: list[dict[str, Any]] = []
        self.published: list[dict[str, float]] = []

    def check_order(self, request: OrderRequest, **kwargs: Any) -> RiskDecision:
        self.calls.append({"request": request, **kwargs})
        return self.decision

    def publish_platform_state(self, profile_id: str, *, exposure: float, daily_pnl: float) -> None:
        self.published.append(
            {"profile_id": profile_id, "exposure": float(exposure), "daily_pnl": float(daily_pnl)}
        )


def _logged_events(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the structured event names captured by ``caplog``, in order."""
    return [str(record.event) for record in caplog.records if hasattr(record, "event")]


def _event_type_for(state: OrderState) -> BrokerEventType:
    """Map an order state onto the venue event that reports it."""
    mapping = {
        OrderState.FILLED: BrokerEventType.ORDER_FILLED,
        OrderState.PARTIALLY_FILLED: BrokerEventType.ORDER_PARTIALLY_FILLED,
        OrderState.CANCELLED: BrokerEventType.ORDER_CANCELLED,
        OrderState.REJECTED: BrokerEventType.ORDER_REJECTED,
        OrderState.SUBMITTED: BrokerEventType.ORDER_ACCEPTED,
        OrderState.PENDING: BrokerEventType.ORDER_ACCEPTED,
    }
    return mapping[state]


# ---------------------------------------------------------------------------
# fixtures and builders
# ---------------------------------------------------------------------------


def profile(
    *, profile_id: str = "btc-paper", mode: str = "paper", initial_balance: float = 10_000.0
) -> ProfileConfig:
    """Build the profile every test trades with."""
    return ProfileConfig(
        id=profile_id,
        symbol=SYMBOL,
        timeframe="1h",
        strategy="basic",
        mode=mode,  # type: ignore[arg-type]
        initial_balance=initial_balance,
    )


def order_request(
    *,
    client_order_id: str = "btc-paper-BTC_USDT-20240101T000000Z-0000",
    side: OrderSide = OrderSide.BUY,
    quantity: float = 1.0,
    reason: str = "enter_long",
    stop_price: float | None = None,
    profile_id: str = "btc-paper",
    mode: RunMode = RunMode.PAPER,
    order_type: OrderType = OrderType.MARKET,
    price: float | None = None,
) -> OrderRequest:
    """Build the order request the gateway routes."""
    return OrderRequest(
        profile_id=profile_id,
        client_order_id=client_order_id,
        symbol=SYMBOL,
        side=side,
        type=order_type,
        quantity=quantity,
        price=price,
        stop_price=stop_price,
        mode=mode,
        reason=reason,
        created_at=TS,
    )


def build_gateway(
    *,
    profile_config: ProfileConfig | None = None,
    broker: FakeBroker | None = None,
    store: FakeStore | None = None,
    clock: ManualClock | None = None,
    risk: Any = None,
    live_gate: LiveTradingGate | None = None,
    counters: EngineCounters | None = None,
) -> tuple[ExecutionGateway, FakeBroker, FakeStore, ManualClock]:
    """Assemble a gateway around the local fakes and return every part."""
    resolved_profile = profile() if profile_config is None else profile_config
    resolved_broker = (
        FakeBroker(name="fake", mode=RunMode(resolved_profile.mode)) if broker is None else broker
    )
    resolved_store = FakeStore() if store is None else store
    resolved_clock = ManualClock(pd.Timestamp(TS).to_pydatetime()) if clock is None else clock
    gateway = ExecutionGateway(
        profile=resolved_profile,
        broker=resolved_broker,
        store=resolved_store,
        clock=resolved_clock,
        risk=risk,
        live_gate=live_gate,
        counters=counters,
    )
    return gateway, resolved_broker, resolved_store, resolved_clock


def submit_kwargs(**overrides: float | int | bool) -> dict[str, Any]:
    """Return a complete, valid set of risk inputs for ``submit``."""
    payload: dict[str, Any] = {
        "reference_price": 100.0,
        "equity": 10_000.0,
        "open_positions": 0,
        "position_notional": 0.0,
        "daily_pnl": 0.0,
        "daily_trades": 0,
        "peak_equity": 10_000.0,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. nominal paper path
# ---------------------------------------------------------------------------


def test_nominal_paper_path_submit_then_poll() -> None:
    gateway, broker, store, _ = build_gateway()
    request = order_request()

    submitted = gateway.submit(request, **submit_kwargs())

    assert gateway.profile_id == "btc-paper"
    assert gateway.mode is RunMode.PAPER
    assert gateway.broker is broker
    assert gateway.profile.id == "btc-paper"
    assert submitted.state is OrderState.SUBMITTED
    assert submitted.mode is RunMode.PAPER
    assert submitted.broker_order_id == "fake-" + request.client_order_id
    assert [order.state for order in store.order_history] == [
        OrderState.PENDING,
        OrderState.SUBMITTED,
    ]
    assert store.get_order(request.client_order_id) is not None
    assert len(broker.submits) == 1
    assert broker.submits[0][1] == 100.0
    assert gateway.counters.orders_submitted == 1
    assert gateway.counters.orders_filled == 0
    assert gateway.drain_fills() == []

    broker.queue_fill(client_order_id=request.client_order_id, quantity=1.0, price=100.0, fee=0.1)
    events = gateway.poll()

    assert [event.event_type for event in events] == [BrokerEventType.ORDER_FILLED]
    position = store.get_position("btc-paper", SYMBOL)
    assert position is not None
    assert position.quantity == pytest.approx(1.0)
    assert position.average_price == pytest.approx(100.0)
    assert position.direction is Direction.LONG
    assert position.opened_at == TS
    fills = gateway.drain_fills()
    assert len(fills) == 1
    assert fills[0].fill_id == f"{request.client_order_id}-fill-0001"
    assert gateway.drain_fills() == []
    assert gateway.counters.orders_filled == 1
    assert gateway.counters.orders_submitted == 1
    assert gateway.closed_trade() is None


def test_counters_are_rebound_and_visible_through_the_property() -> None:
    counters = EngineCounters()
    gateway, broker, _, _ = build_gateway(counters=counters)
    assert gateway.counters == counters

    broker.queue_fill(client_order_id="c-1", quantity=1.0, price=100.0, with_order=False)
    gateway.submit(order_request(), **submit_kwargs())
    gateway.poll()

    assert counters.orders_submitted == 0  # the injected snapshot is immutable
    assert gateway.counters.orders_submitted == 1
    assert gateway.counters.orders_filled == 1
    assert gateway.counters.to_dict()["orders_filled"] == 1


# ---------------------------------------------------------------------------
# 2. idempotency (restart between submit and fill)
# ---------------------------------------------------------------------------


def test_resubmitting_a_known_order_never_reaches_the_venue() -> None:
    gateway, broker, store, _ = build_gateway()
    request = order_request()

    first = gateway.submit(request, **submit_kwargs())
    second = gateway.submit(request, **submit_kwargs())

    assert second is store.get_order(request.client_order_id)
    assert second.client_order_id == first.client_order_id
    assert len(broker.submits) == 1
    assert gateway.counters.orders_submitted == 1
    assert len(store.order_history) == 2  # PENDING then SUBMITTED, from the single call


def test_restart_between_submit_and_fill_returns_the_stored_order() -> None:
    store = FakeStore()
    seeded = Order(
        client_order_id="btc-paper-BTC_USDT-20240101T000000Z-0000",
        profile_id="btc-paper",
        symbol=SYMBOL,
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        quantity=1.0,
        state=OrderState.FILLED,
        mode=RunMode.PAPER,
        created_at=TS,
        updated_at=TS,
        filled_quantity=1.0,
    )
    store.upsert_order(seeded)
    store.order_history.clear()
    gateway, broker, _, _ = build_gateway(store=store)

    request = order_request(client_order_id=seeded.client_order_id)
    result = gateway.submit(request, **submit_kwargs())

    assert result is seeded
    assert broker.submits == []
    assert gateway.counters.orders_submitted == 0
    assert store.order_history == []


def test_a_rejected_order_can_be_retried() -> None:
    store = FakeStore()
    rejected = Order(
        client_order_id="c-retry",
        profile_id="btc-paper",
        symbol=SYMBOL,
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        quantity=1.0,
        state=OrderState.REJECTED,
        mode=RunMode.PAPER,
        created_at=TS,
        updated_at=TS,
        reject_reason="rate limited",
    )
    store.upsert_order(rejected)
    store.order_history.clear()
    gateway, broker, _, _ = build_gateway(store=store)

    result = gateway.submit(order_request(client_order_id="c-retry"), **submit_kwargs())

    assert result.state is OrderState.SUBMITTED
    assert len(broker.submits) == 1
    assert gateway.counters.orders_submitted == 1


def test_replayed_fill_is_never_counted_twice(caplog: pytest.LogCaptureFixture) -> None:
    gateway, broker, store, _ = build_gateway()
    request = order_request()
    gateway.submit(request, **submit_kwargs())
    fill = broker.queue_fill(client_order_id=request.client_order_id, quantity=1.0, price=100.0)

    gateway.poll()
    first_drain = gateway.drain_fills()
    assert len(first_drain) == 1
    assert gateway.counters.orders_filled == 1

    # The very same execution is replayed (same fill_id): it must be ignored.
    broker.queue_fill(
        client_order_id=request.client_order_id,
        quantity=1.0,
        price=100.0,
        fill_id=fill.fill_id,
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        gateway.poll()

    assert gateway.counters.orders_filled == 1
    assert gateway.drain_fills() == []
    assert store.get_position("btc-paper", SYMBOL).quantity == pytest.approx(1.0)  # type: ignore[union-attr]
    assert _logged_events(caplog) == ["fill_replayed"]


# ---------------------------------------------------------------------------
# 3. order rejection
# ---------------------------------------------------------------------------


def test_order_rejection_is_persisted_and_raised() -> None:
    broker = FakeBroker(
        name="fake", mode=RunMode.PAPER, accepted=False, reason="insufficient funds"
    )
    gateway, broker, store, _ = build_gateway(broker=broker)
    request = order_request()

    with pytest.raises(OrderRejectedError) as excinfo:
        gateway.submit(request, **submit_kwargs())

    assert request.client_order_id in str(excinfo.value)
    assert "insufficient funds" in str(excinfo.value)
    stored = store.get_order(request.client_order_id)
    assert stored is not None
    assert stored.state is OrderState.REJECTED
    assert stored.reject_reason == "insufficient funds"
    assert [order.state for order in store.order_history] == [
        OrderState.PENDING,
        OrderState.REJECTED,
    ]
    assert gateway.counters.orders_rejected == 1
    assert gateway.counters.orders_submitted == 0
    assert len(broker.submits) == 1
    assert store.positions == {}
    assert gateway.drain_fills() == []


def test_rejection_log_is_structured(caplog: pytest.LogCaptureFixture) -> None:
    broker = FakeBroker(name="fake", mode=RunMode.PAPER, accepted=False, reason="venue down")
    gateway, _, _, _ = build_gateway(broker=broker)

    with (
        caplog.at_level(logging.WARNING, logger=LOGGER_NAME),
        pytest.raises(OrderRejectedError),
    ):
        gateway.submit(order_request(), **submit_kwargs())

    record = caplog.records[-1]
    assert record.event == "order_rejected"  # type: ignore[attr-defined]
    assert record.profile_id == "btc-paper"  # type: ignore[attr-defined]
    assert record.reason == "venue down"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 4. partial fill then completion
# ---------------------------------------------------------------------------


def test_partial_fill_then_completion() -> None:
    gateway, broker, store, _ = build_gateway()
    request = order_request()
    gateway.submit(request, **submit_kwargs())

    broker.queue_fill(
        client_order_id=request.client_order_id,
        quantity=0.4,
        price=100.0,
        state=OrderState.PARTIALLY_FILLED,
        filled_quantity=0.4,
    )
    gateway.poll()

    partial = store.get_position("btc-paper", SYMBOL)
    assert partial is not None
    assert partial.quantity == pytest.approx(0.4)
    assert partial.average_price == pytest.approx(100.0)
    assert store.get_order(request.client_order_id).state is OrderState.PARTIALLY_FILLED  # type: ignore[union-attr]
    assert gateway.closed_trade() is None
    assert gateway.counters.orders_filled == 1
    assert len(gateway.drain_fills()) == 1

    broker.queue_fill(
        client_order_id=request.client_order_id,
        quantity=0.6,
        price=101.0,
        fill_id=f"{request.client_order_id}-fill-0002",
        filled_quantity=1.0,
    )
    gateway.poll()

    complete = store.get_position("btc-paper", SYMBOL)
    assert complete is not None
    assert complete.quantity == pytest.approx(1.0)
    assert complete.average_price == pytest.approx(100.6)
    assert store.get_order(request.client_order_id).state is OrderState.FILLED  # type: ignore[union-attr]
    assert gateway.counters.orders_filled == 2
    assert gateway.closed_trade() is None  # a partial fill never closes a round trip
    assert len(gateway.drain_fills()) == 1


# ---------------------------------------------------------------------------
# 5. closed round trip
# ---------------------------------------------------------------------------


def test_closed_round_trip_is_exposed_once() -> None:
    gateway, broker, store, _ = build_gateway()
    entry = order_request(reason="enter_long", stop_price=95.0)
    gateway.submit(entry, **submit_kwargs())
    broker.queue_fill(
        client_order_id=entry.client_order_id,
        quantity=1.0,
        price=100.0,
        fee=0.1,
        timestamp=TS,
    )
    gateway.poll()

    exit_request = order_request(
        client_order_id="btc-paper-BTC_USDT-20240101T010000Z-0000",
        side=OrderSide.SELL,
        reason="take_profit",
    )
    gateway.submit(exit_request, **submit_kwargs(), closes_position=True)
    broker.queue_fill(
        client_order_id=exit_request.client_order_id,
        quantity=1.0,
        price=110.0,
        side=OrderSide.SELL,
        fee=0.11,
        timestamp=TS + pd.Timedelta(hours=1),
    )
    gateway.poll()

    trade = gateway.closed_trade()
    assert isinstance(trade, TradeRecord)
    assert trade.entry_time == TS
    assert trade.exit_time == TS + pd.Timedelta(hours=1)
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.exit_price == pytest.approx(110.0)
    assert trade.size == pytest.approx(1.0)
    assert trade.direction is Direction.LONG
    assert trade.fees == pytest.approx(0.21)
    assert trade.pnl == pytest.approx(10.0 - 0.21)
    assert trade.pnl_pct == pytest.approx((10.0 - 0.21) / 100.0)
    assert trade.duration_minutes == pytest.approx(60.0)
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert trade.stop_price == pytest.approx(95.0)

    assert gateway.closed_trade() is None  # consumed once
    assert store.get_position("btc-paper", SYMBOL) is None
    assert store.deleted_positions == [("btc-paper", SYMBOL)]
    assert store.list_trades("btc-paper") == [trade]


def test_short_round_trip_and_unknown_exit_reason() -> None:
    gateway, broker, store, _ = build_gateway()
    entry = order_request(side=OrderSide.SELL, reason="enter_short")
    gateway.submit(entry, **submit_kwargs())
    broker.queue_fill(
        client_order_id=entry.client_order_id,
        quantity=2.0,
        price=100.0,
        side=OrderSide.SELL,
        fee=0.2,
        timestamp=TS,
    )
    gateway.poll()
    assert store.get_position("btc-paper", SYMBOL).quantity == pytest.approx(-2.0)  # type: ignore[union-attr]
    assert store.get_position("btc-paper", SYMBOL).direction is Direction.SHORT  # type: ignore[union-attr]

    exit_request = order_request(client_order_id="c-exit", side=OrderSide.BUY, reason="exit_short")
    gateway.submit(exit_request, **submit_kwargs(), closes_position=True)
    broker.queue_fill(
        client_order_id="c-exit",
        quantity=2.0,
        price=90.0,
        side=OrderSide.BUY,
        fee=0.18,
        timestamp=TS + pd.Timedelta(minutes=30),
    )
    gateway.poll()

    trade = gateway.closed_trade()
    assert trade is not None
    assert trade.direction is Direction.SHORT
    assert trade.exit_reason is ExitReason.SIGNAL  # "exit_short" is not a frozen ExitReason
    assert trade.pnl == pytest.approx(20.0 - 0.38)
    assert trade.pnl_pct == pytest.approx((20.0 - 0.38) / 200.0)
    assert trade.duration_minutes == pytest.approx(30.0)
    assert trade.stop_price is None


def test_partial_reduction_keeps_the_position_open() -> None:
    gateway, broker, store, _ = build_gateway()
    entry = order_request()
    gateway.submit(entry, **submit_kwargs())
    broker.queue_fill(client_order_id=entry.client_order_id, quantity=2.0, price=100.0)
    gateway.poll()

    trim = order_request(client_order_id="c-trim", side=OrderSide.SELL, reason="exit_long")
    gateway.submit(trim, **submit_kwargs(), closes_position=True)
    broker.queue_fill(client_order_id="c-trim", quantity=1.0, price=110.0, side=OrderSide.SELL)
    gateway.poll()

    position = store.get_position("btc-paper", SYMBOL)
    assert position is not None
    assert position.quantity == pytest.approx(1.0)
    assert position.average_price == pytest.approx(100.0)
    assert position.realized_pnl == pytest.approx(10.0)
    assert gateway.closed_trade() is None
    assert store.list_trades("btc-paper") == []


def test_reversal_closes_the_round_trip_and_opens_the_remainder() -> None:
    gateway, broker, store, _ = build_gateway()
    entry = order_request(stop_price=95.0)
    gateway.submit(entry, **submit_kwargs())
    broker.queue_fill(client_order_id=entry.client_order_id, quantity=1.0, price=100.0)
    gateway.poll()

    reversal = order_request(client_order_id="c-rev", side=OrderSide.SELL, reason="exit_long")
    gateway.submit(reversal, **submit_kwargs(), closes_position=True)
    broker.queue_fill(client_order_id="c-rev", quantity=3.0, price=105.0, side=OrderSide.SELL)
    gateway.poll()

    trade = gateway.closed_trade()
    assert trade is not None
    assert trade.size == pytest.approx(1.0)
    assert trade.pnl == pytest.approx(5.0)
    remainder = store.get_position("btc-paper", SYMBOL)
    assert remainder is not None
    assert remainder.quantity == pytest.approx(-2.0)
    assert remainder.direction is Direction.SHORT
    assert remainder.average_price == pytest.approx(105.0)


def test_restart_mid_position_rebuilds_the_accumulator() -> None:
    store = FakeStore()
    store.upsert_position(
        Position(
            profile_id="btc-paper",
            symbol=SYMBOL,
            quantity=1.0,
            average_price=100.0,
            direction=Direction.LONG,
            opened_at=TS,
            updated_at=TS,
            stop_price=95.0,
        )
    )
    gateway, broker, _, _ = build_gateway(store=store)
    closing = order_request(client_order_id="c-close", side=OrderSide.SELL, reason="")
    gateway.submit(closing, **submit_kwargs(), closes_position=True)
    broker.queue_fill(
        client_order_id="c-close",
        quantity=1.0,
        price=120.0,
        side=OrderSide.SELL,
        timestamp=TS + pd.Timedelta(hours=2),
    )
    gateway.poll()

    trade = gateway.closed_trade()
    assert trade is not None
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.exit_price == pytest.approx(120.0)
    assert trade.entry_time == TS
    assert trade.exit_reason is ExitReason.SIGNAL  # the closing request carried no reason
    assert trade.stop_price == pytest.approx(95.0)  # recovered from the persisted position
    assert trade.pnl == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# 6. risk, before any venue call
# ---------------------------------------------------------------------------


def test_risk_limit_rejection_blocks_before_the_venue() -> None:
    clock = ManualClock(pd.Timestamp(TS).to_pydatetime())
    risk = RiskManager(
        RiskLimits.from_config(RiskLimitsConfig(max_order_notional=1.0)),
        clock=clock,
    )
    gateway, broker, store, _ = build_gateway(risk=risk)

    with pytest.raises(RiskLimitExceededError) as excinfo:
        gateway.submit(order_request(), **submit_kwargs(reference_price=100.0))

    assert excinfo.value.issues == ("max_order_notional",)
    assert "max_order_notional" in str(excinfo.value)
    assert broker.submits == []
    assert store.order_history == []
    assert store.orders == {}
    assert store.positions == {}
    assert gateway.counters.risk_rejections == 1
    assert gateway.counters.orders_submitted == 0


def test_engaged_kill_switch_raises_kill_switch_error() -> None:
    clock = ManualClock(pd.Timestamp(TS).to_pydatetime())
    kill_switch = KillSwitch(clock=clock, environ={})
    kill_switch.engage("operator stop", source="test")
    risk = RiskManager(RiskLimits(), clock=clock, kill_switch=kill_switch)
    gateway, broker, store, _ = build_gateway(risk=risk)

    with pytest.raises(KillSwitchActiveError) as excinfo:
        gateway.submit(order_request(), **submit_kwargs())

    assert "operator stop" in str(excinfo.value)
    assert broker.submits == []
    assert store.order_history == []
    assert gateway.counters.risk_rejections == 1


def test_risk_rejection_is_structured_logged(caplog: pytest.LogCaptureFixture) -> None:
    clock = ManualClock(pd.Timestamp(TS).to_pydatetime())
    risk = RiskManager(
        RiskLimits.from_config(RiskLimitsConfig(max_open_positions=0)),
        clock=clock,
    )
    gateway, _, _, _ = build_gateway(risk=risk)

    with (
        caplog.at_level(logging.WARNING, logger=LOGGER_NAME),
        pytest.raises(RiskLimitExceededError),
    ):
        gateway.submit(order_request(), **submit_kwargs())

    record = caplog.records[-1]
    assert record.event == "risk_rejected"  # type: ignore[attr-defined]
    assert record.limit == "max_open_positions"  # type: ignore[attr-defined]
    assert record.profile_id == "btc-paper"  # type: ignore[attr-defined]


def test_closes_position_is_forwarded_to_the_risk_manager() -> None:
    risk = FakeRisk()
    gateway, broker, _, _ = build_gateway(risk=risk)

    gateway.submit(order_request(), **submit_kwargs(), closes_position=True)

    assert len(risk.calls) == 1
    call = risk.calls[0]
    assert call["closes_position"] is True
    assert call["reference_price"] == 100.0
    assert call["equity"] == 10_000.0
    assert call["open_positions"] == 0
    assert call["position_notional"] == 0.0
    assert call["daily_pnl"] == 0.0
    assert call["daily_trades"] == 0
    assert call["peak_equity"] == 10_000.0
    assert len(broker.submits) == 1


def test_without_risk_manager_no_limit_is_applied() -> None:
    gateway, broker, _, _ = build_gateway(risk=None)

    order = gateway.submit(order_request(quantity=1_000.0), **submit_kwargs())

    assert order.state is OrderState.SUBMITTED
    assert len(broker.submits) == 1


# ---------------------------------------------------------------------------
# 7. paper / live separation
# ---------------------------------------------------------------------------


def test_paper_profile_never_routes_to_a_live_broker() -> None:
    broker = FakeBroker(name="binance-live", mode=RunMode.LIVE)
    gateway, broker, store, _ = build_gateway(broker=broker)

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(order_request(), **submit_kwargs())

    assert "a paper profile can never be routed to a live broker" in str(excinfo.value)
    assert "binance-live" in str(excinfo.value)
    assert broker.submits == []
    assert store.order_history == []


def test_live_profile_never_routes_to_a_paper_broker() -> None:
    live_gate = LiveTradingGate({LiveTradingGate.ENV_VAR: LiveTradingGate.REQUIRED_VALUE})
    broker = FakeBroker(name="paper", mode=RunMode.PAPER)
    gateway, broker, store, _ = build_gateway(
        profile_config=profile(profile_id="btc-live", mode="live"),
        broker=broker,
        live_gate=live_gate,
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(order_request(profile_id="btc-live", mode=RunMode.LIVE), **submit_kwargs())

    assert "is live but the injected broker" in str(excinfo.value)
    assert broker.submits == []
    assert store.order_history == []


def test_live_profile_without_the_opt_in_is_forbidden() -> None:
    broker = FakeBroker(name="binance", mode=RunMode.LIVE)
    gateway, broker, store, _ = build_gateway(
        profile_config=profile(profile_id="btc-live", mode="live"),
        broker=broker,
        live_gate=LiveTradingGate({}),
    )

    with pytest.raises(LiveTradingForbiddenError) as excinfo:
        gateway.submit(order_request(profile_id="btc-live", mode=RunMode.LIVE), **submit_kwargs())

    assert LiveTradingGate.ENV_VAR in str(excinfo.value)
    assert broker.submits == []
    assert store.order_history == []


def test_live_profile_armed_through_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LiveTradingGate.ENV_VAR, LiveTradingGate.REQUIRED_VALUE)
    broker = FakeBroker(name="binance", mode=RunMode.LIVE)
    # No live gate is injected: the real one is built lazily from the environment.
    gateway, broker, store, _ = build_gateway(
        profile_config=profile(profile_id="btc-live", mode="live"), broker=broker
    )

    order = gateway.submit(
        order_request(profile_id="btc-live", mode=RunMode.LIVE), **submit_kwargs()
    )

    assert gateway.mode is RunMode.LIVE
    assert order.mode is RunMode.LIVE
    assert store.get_order(order.client_order_id).mode is RunMode.LIVE  # type: ignore[union-attr]
    assert len(broker.submits) == 1


def test_broker_with_an_unknown_mode_is_refused() -> None:
    broker = FakeBroker(name="mystery", mode="mystery")
    gateway, broker, _, _ = build_gateway(broker=broker)

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(order_request(), **submit_kwargs())

    assert "unknown mode" in str(excinfo.value)
    assert broker.submits == []


def test_venue_order_mode_is_corrected_to_the_profile_mode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(LiveTradingGate.ENV_VAR, LiveTradingGate.REQUIRED_VALUE)
    broker = FakeBroker(name="binance", mode=RunMode.LIVE)
    gateway, broker, store, _ = build_gateway(
        profile_config=profile(profile_id="btc-live", mode="live"), broker=broker
    )
    gateway.submit(
        order_request(
            profile_id="btc-live",
            mode=RunMode.LIVE,
            client_order_id="c-live",
        ),
        **submit_kwargs(),
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        broker.queue_fill(
            client_order_id="c-live",
            profile_id="btc-live",
            quantity=1.0,
            price=100.0,
            order_mode=RunMode.PAPER,  # the venue reports the wrong side of the fence
        )
        gateway.poll()

    assert store.get_order("c-live").mode is RunMode.LIVE  # type: ignore[union-attr]
    assert "order_mode_corrected" in _logged_events(caplog)


# ---------------------------------------------------------------------------
# 8. reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_forwards_the_report_and_never_repairs() -> None:
    report = ReconciliationReport(
        profile_id="btc-paper",
        ok=False,
        checked_at=TS,
        matched=1,
        only_at_venue=("venue-only-1",),
    )
    broker = FakeBroker(name="fake", mode=RunMode.PAPER, report=report)
    gateway, broker, store, _ = build_gateway(broker=broker)
    submitted = gateway.submit(order_request(), **submit_kwargs())
    history = list(store.order_history)

    result = gateway.reconcile()

    assert result is report
    assert result.ok is False
    assert "venue-only-1" in result.only_at_venue
    assert broker.reconcile_calls == 1
    assert [order.client_order_id for order in broker.reconcile_expected] == [
        submitted.client_order_id
    ]
    assert store.order_history == history  # nothing was "fixed" silently
    assert store.positions == {}


def test_reconcile_logs_a_mismatch(caplog: pytest.LogCaptureFixture) -> None:
    report = ReconciliationReport(
        profile_id="btc-paper",
        ok=False,
        checked_at=TS,
        only_locally=("local-only-1",),
    )
    gateway, _, _, _ = build_gateway(
        broker=FakeBroker(name="fake", mode=RunMode.PAPER, report=report)
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        gateway.reconcile()

    record = caplog.records[-1]
    assert record.event == "reconciliation_mismatch"  # type: ignore[attr-defined]
    assert record.only_locally == ["local-only-1"]  # type: ignore[attr-defined]


def test_reconcile_against_the_real_paper_broker() -> None:
    broker_module = pytest.importorskip("trading_platform.realtime.broker")
    clock = ManualClock(pd.Timestamp(TS).to_pydatetime())
    real_broker = broker_module.PaperBroker(clock=clock, seed=0, partial_fill_probability=0.0)
    gateway, _, store, _ = build_gateway(broker=real_broker)

    submitted = gateway.submit(order_request(), **submit_kwargs())
    healthy = gateway.reconcile()
    assert healthy.ok is True
    assert healthy.matched == 1

    ghost = replace(
        submitted,
        client_order_id="ghost-order",
        broker_order_id="paper-ghost-order",
    )
    real_broker.inject_venue_order(ghost)

    diverged = gateway.reconcile()
    assert diverged.ok is False
    assert "ghost-order" in diverged.only_at_venue
    # ... and symmetrically: a WORKING local order the venue knows nothing about.
    # (It must stay working: a terminal one is deliberately no longer a divergence.)
    local_only = replace(submitted, client_order_id="local-only-order", state=OrderState.SUBMITTED)
    store.upsert_order(local_only)
    mirror = gateway.reconcile()
    assert "local-only-order" in mirror.only_locally


def test_reconcile_of_a_profile_holding_only_terminal_orders_is_healthy() -> None:
    """The reported production defect: durable history against a venue that forgot.

    A paper market order fills immediately, so the store's durable history is made
    of terminal orders while a fresh venue holds nothing.  Reconciliation must stay
    healthy, must report nothing as ``only_locally`` and must repair nothing.
    """
    broker_module = pytest.importorskip("trading_platform.realtime.broker")
    clock = ManualClock(pd.Timestamp(TS).to_pydatetime())
    venue = broker_module.PaperBroker(clock=clock, seed=0, partial_fill_probability=0.0)
    gateway, _, store, _ = build_gateway(broker=venue)

    submitted = gateway.submit(order_request(), **submit_kwargs())
    assert submitted.state is OrderState.FILLED  # terminal, and the venue has finished with it
    history = list(store.order_history)

    # A fresh venue that never saw this profile: the "forgotten" venue of the report.
    fresh = broker_module.PaperBroker(clock=clock, seed=0, partial_fill_probability=0.0)
    fresh_gateway, _, fresh_store, _ = build_gateway(broker=fresh)
    fresh_store.upsert_order(submitted)

    report = fresh_gateway.reconcile()

    assert report.ok is True
    assert report.matched == 0
    assert report.only_locally == ()
    assert report.only_at_venue == ()
    assert report.mismatched == ()
    assert list(store.order_history) == history  # nothing was "fixed" silently
    assert store.positions == {}


def test_reconcile_keeps_the_degraded_path_for_genuine_divergences() -> None:
    """Regression: the terminal-order rule must not weaken any divergence."""
    broker_module = pytest.importorskip("trading_platform.realtime.broker")
    clock = ManualClock(pd.Timestamp(TS).to_pydatetime())
    venue = broker_module.PaperBroker(clock=clock, seed=0, partial_fill_probability=0.0)
    gateway, _, store, _ = build_gateway(broker=venue)

    submitted = gateway.submit(order_request(), **submit_kwargs())
    assert submitted.state is OrderState.FILLED

    # (a) a WORKING order only the venue holds still degrades the profile.
    venue_only = replace(submitted, client_order_id="venue-only-order", state=OrderState.SUBMITTED)
    venue.inject_venue_order(venue_only)
    diverged = gateway.reconcile()
    assert diverged.ok is False
    assert "venue-only-order" in diverged.only_at_venue

    # (b) a WORKING order only the local state knows still degrades the profile.
    local_only = replace(submitted, client_order_id="local-only-order", state=OrderState.SUBMITTED)
    store.upsert_order(local_only)
    mirror = gateway.reconcile()
    assert mirror.ok is False
    assert "local-only-order" in mirror.only_locally


def test_paper_broker_shares_the_whole_lifecycle() -> None:
    broker_module = pytest.importorskip("trading_platform.realtime.broker")
    clock = ManualClock(pd.Timestamp(TS).to_pydatetime())
    real_broker = broker_module.PaperBroker(clock=clock, seed=7, partial_fill_probability=0.0)
    gateway, broker, store, _ = build_gateway(broker=real_broker)

    entry = order_request(reason="enter_long", stop_price=95.0)
    accepted = gateway.submit(entry, **submit_kwargs())
    assert accepted.state is OrderState.FILLED  # a market order fills immediately in paper
    gateway.poll()

    position = store.get_position("btc-paper", SYMBOL)
    assert position is not None
    assert position.quantity == pytest.approx(1.0)
    assert len(gateway.drain_fills()) == 1
    assert gateway.counters.orders_submitted == 1
    assert gateway.counters.orders_filled == 1

    exit_request = order_request(
        client_order_id="c-exit", side=OrderSide.SELL, reason="take_profit"
    )
    gateway.submit(exit_request, **submit_kwargs(reference_price=110.0), closes_position=True)
    gateway.poll()

    trade = gateway.closed_trade()
    assert trade is not None
    assert trade.direction is Direction.LONG
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert trade.stop_price == pytest.approx(95.0)
    assert store.get_position("btc-paper", SYMBOL) is None
    assert store.list_trades("btc-paper") == [trade]
    # The same lifecycle, the same calls: only the venue changed (D5).
    assert broker.mode is RunMode.PAPER


# ---------------------------------------------------------------------------
# 9. reads, events without an order, cancellation
# ---------------------------------------------------------------------------


def test_equity_position_and_snapshot_fields() -> None:
    """The read model is attributed, and its exact key set is frozen.

    This test pinned the *venue* cash before the shared platform wallet existed;
    it now pins the attributed figures deliberately: a profile funds its orders
    from one ledger shared by the whole platform, so ``cash`` is its own share
    (``allocation - deployed + realized_pnl``), never the wallet's balance.
    """
    gateway, broker, _, _ = build_gateway()
    empty = gateway.snapshot_fields()
    assert empty == {
        "equity": 10_000.0,
        "cash": 10_000.0,
        "position_value": 0.0,
        "open_positions": 0,
        "allocation": 10_000.0,
        "deployed": 0.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
    }
    assert gateway.equity(reference_price=100.0) == pytest.approx(10_000.0)
    assert gateway.position() is None
    assert gateway.position("ETH/USDT") is None
    # the venue balance is a different question, answered by ``balance()``
    assert gateway.balance() == pytest.approx(10_000.0)

    request = order_request()
    gateway.submit(request, **submit_kwargs())  # marks the gateway at 100.0
    broker.queue_fill(client_order_id=request.client_order_id, quantity=1.0, price=100.0)
    gateway.poll()

    assert gateway.equity(reference_price=120.0) == pytest.approx(10_020.0)
    assert gateway.position().symbol == SYMBOL  # type: ignore[union-attr]
    assert gateway.position("ETH/USDT") is None
    snapshot = gateway.snapshot_fields()
    assert snapshot["equity"] == pytest.approx(10_000.0)
    assert snapshot["cash"] == pytest.approx(9_900.0)
    assert snapshot["position_value"] == pytest.approx(100.0)
    assert snapshot["open_positions"] == 1
    assert snapshot["allocation"] == pytest.approx(10_000.0)
    assert snapshot["deployed"] == pytest.approx(100.0)
    assert snapshot["realized_pnl"] == pytest.approx(0.0)
    assert snapshot["unrealized_pnl"] == pytest.approx(0.0)
    # the attributed identity of a long position holds exactly
    assert snapshot["equity"] == pytest.approx(snapshot["cash"] + snapshot["position_value"])
    assert snapshot["equity"] == pytest.approx(
        snapshot["allocation"] + snapshot["realized_pnl"] + snapshot["unrealized_pnl"]
    )


def test_a_partial_close_frees_the_deployed_capital() -> None:
    """A reduction shrinks ``deployed``; the round trip is realized only when it closes."""
    gateway, broker, store, _ = build_gateway()
    entry = order_request()
    gateway.submit(entry, **submit_kwargs())
    broker.queue_fill(client_order_id=entry.client_order_id, quantity=1.0, price=100.0)
    gateway.poll()

    partial = order_request(client_order_id="c-partial", side=OrderSide.SELL)
    gateway.submit(partial, **submit_kwargs(reference_price=110.0), closes_position=True)
    broker.queue_fill(
        client_order_id=partial.client_order_id,
        quantity=0.4,
        price=110.0,
        side=OrderSide.SELL,
    )
    gateway.poll()

    snapshot = gateway.snapshot_fields()
    assert snapshot["open_positions"] == 1
    assert snapshot["deployed"] == pytest.approx(60.0)
    assert snapshot["position_value"] == pytest.approx(66.0)  # 0.6 marked at the new 110.0
    assert snapshot["cash"] == pytest.approx(9_940.0)
    assert snapshot["unrealized_pnl"] == pytest.approx(6.0)
    assert snapshot["equity"] == pytest.approx(10_006.0)
    # the closed *part* is not a stored round trip yet: nothing is claimed as realized
    assert snapshot["realized_pnl"] == pytest.approx(0.0)
    assert store.list_trades("btc-paper") == []
    assert snapshot["equity"] == pytest.approx(snapshot["cash"] + snapshot["position_value"])


def test_two_gateways_sharing_one_wallet_report_their_own_attributed_cash() -> None:
    """The wallet is shared; the *figures* stay attributed to each profile.

    The mechanical proof that no profile can ever read the platform's cash as its
    own: two gateways spend from one :class:`PlatformWallet`, yet each reports the
    cash of its own allocation -- neither the wallet's balance nor the venue's.
    """
    broker_module = pytest.importorskip("trading_platform.realtime.broker")
    clock = ManualClock(pd.Timestamp(TS).to_pydatetime())
    wallet = PlatformWallet(initial_balance=1_000.0, mode=RunMode.PAPER, name="platform")
    store = FakeStore()
    first, first_venue, _, _ = build_gateway(
        profile_config=profile(profile_id="btc-paper", initial_balance=600.0),
        broker=broker_module.PaperBroker(clock=clock, seed=1, wallet=wallet),
        store=store,
        clock=clock,
    )
    second, _second_venue, _, _ = build_gateway(
        profile_config=profile(profile_id="eth-paper", initial_balance=400.0),
        broker=broker_module.PaperBroker(clock=clock, seed=2, wallet=wallet),
        store=store,
        clock=clock,
    )

    assert first.balance() == pytest.approx(1_000.0)
    assert second.balance() == pytest.approx(1_000.0)
    assert first.cash() == pytest.approx(600.0)
    assert second.cash() == pytest.approx(400.0)

    entry = order_request(quantity=1.0)
    first.submit(entry, **submit_kwargs(reference_price=100.0))
    first.poll()  # the real paper venue fills immediately, into the shared wallet

    assert wallet.cash < 1_000.0, "the shared ledger really paid for the order"
    assert first.cash() == pytest.approx(500.0), "the buyer spent its own share"
    assert second.cash() == pytest.approx(400.0), "the other profile is untouched"
    assert first.balance() == pytest.approx(wallet.cash)
    assert first.balance() != pytest.approx(first.cash())
    assert first_venue.wallet is wallet


def test_snapshot_fields_without_a_mark_use_the_entry_price() -> None:
    store = FakeStore()
    store.upsert_position(
        Position(
            profile_id="btc-paper",
            symbol=SYMBOL,
            quantity=2.0,
            average_price=50.0,
            direction=Direction.LONG,
            opened_at=TS,
            updated_at=TS,
        )
    )
    gateway, _, _, _ = build_gateway(store=store)

    snapshot = gateway.snapshot_fields()

    assert snapshot["position_value"] == pytest.approx(100.0)
    assert snapshot["equity"] == pytest.approx(10_000.0)
    assert snapshot["open_positions"] == 1
    assert snapshot["deployed"] == pytest.approx(100.0)
    assert snapshot["unrealized_pnl"] == pytest.approx(0.0)
    assert snapshot["cash"] == pytest.approx(9_900.0)


def test_balance_none_falls_back_to_the_initial_balance() -> None:
    gateway, _, _, _ = build_gateway(broker=FakeBroker(name="fake", balance=None))

    assert gateway.balance() is None
    assert gateway.cash() == pytest.approx(10_000.0)
    assert gateway.equity(reference_price=100.0) == pytest.approx(10_000.0)


def test_shorts_reduce_cash_the_same_way() -> None:
    """A short is attributed like a long: its mark-to-market enters the cash.

    Rewritten deliberately with the shared-wallet semantics: the entry deploys
    100.0 of the allocation whatever the venue reports, so the attributed cash is
    ``allocation - deployed`` and the equity adds the (negative) position value.
    """
    gateway, broker, _, _ = build_gateway(broker=FakeBroker(name="fake", balance=11_000.0))
    request = order_request(side=OrderSide.SELL)
    gateway.submit(request, **submit_kwargs())
    broker.queue_fill(
        client_order_id=request.client_order_id, quantity=1.0, price=100.0, side=OrderSide.SELL
    )
    gateway.poll()

    assert gateway.balance() == pytest.approx(11_000.0), "the venue balance is unchanged"
    assert gateway.cash() == pytest.approx(9_900.0)
    assert gateway.equity(reference_price=90.0) == pytest.approx(9_810.0)
    snapshot = gateway.snapshot_fields()
    assert snapshot["deployed"] == pytest.approx(100.0)
    assert snapshot["position_value"] == pytest.approx(-100.0)  # a short marked at 100.0
    assert snapshot["unrealized_pnl"] == pytest.approx(-200.0)
    assert snapshot["equity"] == pytest.approx(9_800.0)
    assert snapshot["equity"] == pytest.approx(snapshot["cash"] + snapshot["position_value"])


def test_publish_platform_state_forwards_the_profile_and_both_numbers() -> None:
    """The optional platform seam forwards through the injected risk manager."""
    risk = FakeRisk()
    gateway, _, _, _ = build_gateway(risk=risk)

    gateway.publish_platform_state(exposure=123.5, daily_pnl=-4.25)

    assert risk.published == [{"profile_id": "btc-paper", "exposure": 123.5, "daily_pnl": -4.25}]


def test_publish_platform_state_is_a_no_op_without_the_seam() -> None:
    """Neither an absent risk manager nor one without the seam may raise."""

    class SilentRisk:
        """A manager written before the platform aggregation existed."""

        def check_order(self, request: OrderRequest, **kwargs: Any) -> RiskDecision:
            return RiskDecision.allow()

    bare, _, _, _ = build_gateway(risk=None)
    bare.publish_platform_state(exposure=1.0, daily_pnl=2.0)

    silent, _, _, _ = build_gateway(risk=SilentRisk())
    silent.publish_platform_state(exposure=1.0, daily_pnl=2.0)


def test_an_unfunded_order_is_refused_and_persists_nothing() -> None:
    """The shared wallet funds every entry; when it cannot, nothing is left half-applied."""
    clock = ManualClock(pd.Timestamp(TS).to_pydatetime())
    risk = RiskManager(RiskLimits(), clock=clock, wallet=PlatformWallet(initial_balance=50.0))
    gateway, broker, store, _ = build_gateway(risk=risk)

    with pytest.raises(RiskLimitExceededError) as excinfo:
        gateway.submit(order_request(quantity=1.0), **submit_kwargs(reference_price=100.0))

    assert excinfo.value.issues == ("platform_wallet",)
    assert str(excinfo.value).startswith(
        "platform wallet cannot fund order: requires 100.00 USDT, available 50.00 USDT"
    )
    assert broker.submits == []
    assert store.order_history == []
    assert store.orders == {}
    assert store.fills == {}
    assert store.positions == {}
    assert store.trades == []
    assert gateway.counters.risk_rejections == 1
    assert gateway.snapshot_fields()["open_positions"] == 0


def test_event_without_an_order_patches_the_stored_row() -> None:
    gateway, broker, store, _ = build_gateway()
    request = order_request()
    gateway.submit(request, **submit_kwargs())

    broker.queue_event(BrokerEventType.ORDER_CANCELLED, client_order_id=request.client_order_id)
    gateway.poll()

    assert store.get_order(request.client_order_id).state is OrderState.CANCELLED  # type: ignore[union-attr]


def test_rejection_without_an_order_records_the_reason() -> None:
    gateway, broker, store, _ = build_gateway()
    request = order_request()
    gateway.submit(request, **submit_kwargs())

    broker.queue_event(
        BrokerEventType.ORDER_REJECTED,
        client_order_id=request.client_order_id,
        message="post-only violation",
    )
    gateway.poll()

    stored = store.get_order(request.client_order_id)
    assert stored is not None
    assert stored.state is OrderState.REJECTED
    assert stored.reject_reason == "post-only violation"


def test_unknown_order_event_and_venue_error_are_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway, broker, store, _ = build_gateway()
    history = list(store.order_history)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        broker.queue_event(BrokerEventType.ORDER_CANCELLED, client_order_id="never-seen")
        broker.queue_event(BrokerEventType.ERROR, client_order_id="never-seen", message="502")
        events = gateway.poll()

    assert len(events) == 2
    assert store.order_history == history
    assert _logged_events(caplog) == ["unknown_order_event", "broker_event_ignored"]


def test_event_state_already_stored_is_not_rewritten() -> None:
    gateway, broker, store, _ = build_gateway()
    request = order_request()
    gateway.submit(request, **submit_kwargs())
    history = list(store.order_history)

    broker.queue_event(
        BrokerEventType.ORDER_ACCEPTED,
        client_order_id=request.client_order_id,
        order=None,
    )
    gateway.poll()

    assert store.order_history == history
    assert store.get_order(request.client_order_id).state is OrderState.SUBMITTED  # type: ignore[union-attr]


def test_cancel_and_open_orders_are_forwarded_to_the_venue() -> None:
    gateway, broker, store, _ = build_gateway()
    submitted = gateway.submit(
        order_request(order_type=OrderType.LIMIT, price=90.0), **submit_kwargs()
    )
    broker.add_open_order(submitted)

    assert gateway.open_orders() == [submitted]
    assert gateway.cancel(submitted.client_order_id) is True
    assert gateway.cancel("unknown") is False
    assert broker.cancels == [submitted.client_order_id, "unknown"]
    assert store.get_order(submitted.client_order_id).state is OrderState.SUBMITTED  # type: ignore[union-attr]


def test_zero_quantity_fill_books_no_position() -> None:
    gateway, broker, store, _ = build_gateway()
    request = order_request()
    gateway.submit(request, **submit_kwargs())

    broker.queue_fill(client_order_id=request.client_order_id, quantity=0.0, price=100.0)
    gateway.poll()

    assert len(gateway.drain_fills()) == 1
    assert store.get_position("btc-paper", SYMBOL) is None
    assert store.positions == {}
    assert gateway.counters.orders_filled == 1


def test_drain_fills_is_fifo() -> None:
    gateway, broker, _, _ = build_gateway()
    first = order_request(client_order_id="c-1")
    second = order_request(client_order_id="c-2")
    gateway.submit(first, **submit_kwargs())
    gateway.submit(second, **submit_kwargs())
    broker.queue_fill(client_order_id="c-1", quantity=1.0, price=100.0)
    broker.queue_fill(client_order_id="c-2", quantity=0.5, price=100.0, fill_id="c-2-fill-0001")

    gateway.poll()

    fills = gateway.drain_fills()
    assert [fill.client_order_id for fill in fills] == ["c-1", "c-2"]
    assert gateway.drain_fills() == []


def test_repr_names_the_profile_and_carries_no_secret() -> None:
    gateway, _, _, _ = build_gateway()

    text = repr(gateway)

    assert "btc-paper" in text
    assert "paper" in text
    for forbidden in ("api_key", "api_secret", "password", "token", "secret"):
        assert forbidden not in text.lower()


# ---------------------------------------------------------------------------
# 10. D5 -- the single execution path, mechanically
# ---------------------------------------------------------------------------


def _gateway_source() -> str:
    """Return the source text of the gateway module."""
    source_file = inspect.getsourcefile(ExecutionGateway)
    assert source_file is not None
    return Path(source_file).read_text(encoding="utf-8")


def test_gateway_has_no_paper_or_live_branch() -> None:
    source = _gateway_source()
    branches = [
        ast.get_source_segment(source, node.test) or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.If)
        and any(
            keyword in (ast.get_source_segment(source, node.test) or "")
            for keyword in ("'paper'", '"paper"', "'live'", '"live"')
        )
    ]

    assert branches == ['self._profile.mode == "live"']  # the live gate, and only it


def test_gateway_imports_no_concrete_venue_and_branches_on_no_run_mode() -> None:
    source = _gateway_source()
    module = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(alias.name for alias in node.names)

    assert "PaperBroker" not in imported
    assert "CcxtBroker" not in imported
    # No branch anywhere keys off the run mode: only the broker-mode assertion does,
    # and it is an ``if`` on the *broker* (``RunMode(self._broker.mode)``), not on a
    # per-mode implementation.
    mode_branches = [
        ast.get_source_segment(source, node.test) or ""
        for node in ast.walk(module)
        if isinstance(node, ast.If)
        and any(
            token in (ast.get_source_segment(source, node.test) or "")
            for token in ("RunMode.PAPER", "RunMode.LIVE")
        )
    ]
    assert mode_branches == []
