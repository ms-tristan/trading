"""Contract tests of the venue adapters (work package wp4).

Covers, offline and deterministically:

* ``PaperBroker``: market and limit fills, exact prices, fees and cash movements,
  the deterministic partial fill followed by its completion, idempotent
  submission, cancellation, open orders, equity and reconciliation;
* the shared platform wallet the paper venue funds itself from: the broker owns no
  cash of its own, ``fetch_balance`` is the wallet's, two brokers sharing one
  injected wallet see one balance, a live wallet is refused, and ``restore_cash``
  delegates to the wallet;
* the error paths named by the delivery brief (limit without a price, non-positive
  quantity or reference price, duplicate identifier, reconciliation mismatch);
* ``CcxtBroker``: the lazily imported optional extra, the idempotent
  ``clientOrderId`` handshake, the mapping of venue statuses, trades, balances and
  open orders, and the ``ERROR`` event that keeps the loop alive;
* the frozen paper/live separation: ``PaperBroker.mode`` is always
  :attr:`RunMode.PAPER` and no constructor argument can change it.

Nothing here touches the network: the ``ccxt`` extra is replaced by a local fake
module, injected with ``monkeypatch.setitem`` **inside the test body** so the
real package is restored afterwards.  Every broker is driven by a
:class:`ManualClock` and an explicit seed, so two runs produce the very same
event sequence.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
import pytest

from trading_platform.core.errors import BrokerError, BrokerUnavailableError, WalletError
from trading_platform.realtime.broker import Broker, CcxtBroker, PaperBroker
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.credentials import ExchangeCredentials
from trading_platform.realtime.models import (
    BrokerAck,
    BrokerEvent,
    BrokerEventType,
    Order,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderType,
    RunMode,
    new_client_order_id,
)
from trading_platform.realtime.wallet import PlatformWallet

SYMBOL = "BTC/USDT"
PROFILE_ID = "btc-paper"
START = datetime(2024, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# local helpers (no shared fixture: wp4 owns this module only)
# ---------------------------------------------------------------------------


def make_clock(start: datetime = START) -> ManualClock:
    """Return a deterministic clock anchored on a fixed instant."""
    return ManualClock(start=start)


def make_request(
    *,
    client_order_id: str = "ord-1",
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    quantity: float = 1.0,
    price: float | None = None,
    mode: RunMode = RunMode.PAPER,
    profile_id: str = PROFILE_ID,
    symbol: str = SYMBOL,
) -> OrderRequest:
    """Build an order request with deterministic defaults."""
    return OrderRequest(
        profile_id=profile_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        type=order_type,
        quantity=quantity,
        price=price,
        mode=mode,
    )


def make_paper(**kwargs: Any) -> PaperBroker:
    """Build a paper broker with a deterministic clock and explicit seed."""
    kwargs.setdefault("clock", make_clock())
    kwargs.setdefault("initial_balance", 10_000.0)
    kwargs.setdefault("slippage", 0.0)
    kwargs.setdefault("seed", 0)
    return PaperBroker(**kwargs)


def make_wallet(**kwargs: Any) -> PlatformWallet:
    """Build the shared wallet a paper broker is funded from."""
    kwargs.setdefault("initial_balance", 10_000.0)
    return PlatformWallet(**kwargs)


def make_credentials(**overrides: Any) -> ExchangeCredentials:
    """Build configured live credentials without touching the environment."""
    values: dict[str, Any] = {
        "api_key": "test-key",
        "api_secret": "test-secret",
        "exchange": "binance",
        "profile_id": PROFILE_ID,
    }
    values.update(overrides)
    return ExchangeCredentials(**values)


def market_order_id(index: int) -> str:
    """Return the deterministic identifier of the ``index``-th test order."""
    return new_client_order_id(PROFILE_ID, SYMBOL, START, index)


# ---------------------------------------------------------------------------
# fake ccxt -- a local stand-in, installed only for the duration of a test
# ---------------------------------------------------------------------------


class FakeOrderNotFoundError(Exception):
    """Stands in for ``ccxt.OrderNotFound`` without importing ``ccxt``."""


class FakeVenue:
    """Minimal ``ccxt``-compatible exchange recording every call it receives."""

    #: Instances built during a test, in construction order.
    instances: ClassVar[list[FakeVenue]] = []

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[tuple[Any, ...]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.open_payloads: list[dict[str, Any]] = []
        self.balance: dict[str, Any] = {"total": {"USDT": 1234.5, "BTC": 0.25}}
        self.create_status = "open"
        self.create_error: Exception | None = None
        self.open_orders_error: Exception | None = None
        FakeVenue.instances.append(self)

    # -- ccxt surface ------------------------------------------------------

    def fetch_order(self, client_order_id: Any, symbol: Any = None) -> dict[str, Any]:
        key = str(client_order_id)
        if key in self.orders:
            return dict(self.orders[key])
        raise FakeOrderNotFoundError(f"order {key} not found at the venue")

    def create_order(
        self,
        symbol: Any,
        order_type: Any,
        side: Any,
        amount: Any,
        price: Any,
        params: Any = None,
    ) -> dict[str, Any]:
        if self.create_error is not None:
            raise self.create_error
        parameters = dict(params or {})
        self.created.append(
            {
                "symbol": symbol,
                "type": order_type,
                "side": side,
                "amount": amount,
                "price": price,
                "params": parameters,
            }
        )
        client_order_id = str(parameters.get("clientOrderId"))
        order = {
            "id": f"venue-{len(self.created)}",
            "clientOrderId": client_order_id,
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "filled": 0.0,
            "average": None,
            "status": self.create_status,
        }
        self.orders[client_order_id] = order
        return dict(order)

    def cancel_order(self, client_order_id: Any, symbol: Any = None) -> dict[str, Any]:
        self.cancelled.append((str(client_order_id), symbol))
        return {"id": str(client_order_id), "status": "canceled"}

    def fetch_open_orders(self, symbol: Any = None) -> list[dict[str, Any]]:
        if self.open_orders_error is not None:
            raise self.open_orders_error
        return [dict(payload) for payload in self.open_payloads]

    def fetch_my_trades(self, symbol: Any = None) -> list[dict[str, Any]]:
        return [dict(payload) for payload in self.trades]

    def fetch_balance(self) -> dict[str, Any]:
        return dict(self.balance)


def install_fake_ccxt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``ccxt`` with the local fake for the duration of one test."""
    FakeVenue.instances = []
    module = types.ModuleType("ccxt")
    module.binance = FakeVenue  # type: ignore[attr-defined]
    module.OrderNotFound = FakeOrderNotFoundError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ccxt", module)


def venue_of_last_broker() -> FakeVenue:
    """Return the fake venue the most recently built broker is talking to."""
    return FakeVenue.instances[-1]


def make_ccxt(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: RunMode = RunMode.LIVE,
    exchange: str = "binance",
    credentials: ExchangeCredentials | None = None,
) -> tuple[CcxtBroker, FakeVenue]:
    """Install the fake ``ccxt`` and build a broker over it.

    The venue client is built eagerly (through a harmless ``open_orders`` read),
    so a test can configure the fake *before* the broker talks to it.
    """
    install_fake_ccxt(monkeypatch)
    broker = CcxtBroker(
        make_credentials() if credentials is None else credentials,
        clock=make_clock(),
        mode=mode,
        exchange=exchange,
    )
    broker.open_orders()
    return broker, venue_of_last_broker()


def venue_open_order(
    client_order_id: str, *, status: str = "open", price: float = 90.0
) -> dict[str, Any]:
    """Return a venue order payload as ``fetch_open_orders`` would report it."""
    return {
        "id": f"venue-{client_order_id}",
        "clientOrderId": client_order_id,
        "symbol": SYMBOL,
        "type": "limit",
        "side": "buy",
        "amount": 1.0,
        "filled": 0.0,
        "price": price,
        "status": status,
    }


def venue_trade(
    *,
    trade_id: str = "trade-1",
    client_order_id: str = "ord-1",
    side: str = "buy",
    price: float = 101.0,
    amount: float = 1.0,
    fee_cost: float | None = 0.101,
) -> dict[str, Any]:
    """Return a venue trade payload as ``fetch_my_trades`` would report it."""
    fee = {} if fee_cost is None else {"cost": fee_cost, "currency": "USDT"}
    return {
        "id": trade_id,
        "clientOrderId": client_order_id,
        "order": f"venue-{client_order_id}",
        "symbol": SYMBOL,
        "side": side,
        "price": price,
        "amount": amount,
        "timestamp": 1704067200000,
        "fee": fee,
    }


def event_signature(events: list[BrokerEvent]) -> list[str]:
    """Return a comparable, JSON-serialisable rendering of an event sequence."""
    return [json.dumps(event.to_dict(), sort_keys=True) for event in events]


def event_types(events: list[BrokerEvent]) -> list[BrokerEventType]:
    """Return the type of every event, in order."""
    return [event.event_type for event in events]


# ---------------------------------------------------------------------------
# 1. market fills
# ---------------------------------------------------------------------------


def test_paper_broker_is_a_broker_and_fills_a_market_buy_against_the_reference_price() -> None:
    broker = make_paper(fee_rate=0.001, slippage=0.01)
    assert isinstance(broker, Broker)
    assert broker.name == "paper"

    ack = broker.submit(make_request(quantity=2.0), reference_price=100.0)

    assert isinstance(ack, BrokerAck)
    assert ack.accepted is True
    assert ack.state is OrderState.FILLED
    assert ack.client_order_id == "ord-1"
    assert ack.submitted_at == pd.Timestamp(START)
    assert ack.broker_order_id == "paper-ord-1"

    expected_price = 100.0 * 1.01  # the slippage always works against the trader
    expected_fee = 0.001 * 2.0 * expected_price
    assert broker.fetch_balance() == pytest.approx(10_000.0 - 2.0 * expected_price - expected_fee)
    # That balance is not the venue's own counter any more: it is the shared wallet.
    assert broker.wallet.cash == pytest.approx(broker.fetch_balance())
    assert broker.wallet.initial_balance == pytest.approx(10_000.0)

    events = broker.poll()
    assert len(events) == 1
    event = events[0]
    assert event.event_type is BrokerEventType.ORDER_FILLED
    assert event.client_order_id == "ord-1"
    assert event.profile_id == PROFILE_ID
    assert event.fill is not None
    assert event.fill.quantity == pytest.approx(2.0)
    assert event.fill.price == pytest.approx(expected_price, rel=1e-12)
    assert event.fill.fee == pytest.approx(expected_fee)
    assert event.fill.side is OrderSide.BUY
    assert event.fill.fill_id == "ord-1-fill-0001"
    assert event.order is not None
    assert event.order.state is OrderState.FILLED
    assert event.order.filled_quantity == pytest.approx(2.0)

    # Every event is emitted exactly once and no order is left open.
    assert broker.poll() == []
    assert broker.open_orders() == []


def test_paper_broker_fills_a_market_sell_the_other_way_round() -> None:
    broker = make_paper(fee_rate=0.002, slippage=0.01)

    ack = broker.submit(make_request(side=OrderSide.SELL, quantity=4.0), reference_price=200.0)

    assert ack.state is OrderState.FILLED
    expected_price = 200.0 * 0.99
    expected_fee = 0.002 * 4.0 * expected_price
    assert broker.fetch_balance() == pytest.approx(10_000.0 + 4.0 * expected_price - expected_fee)
    fill = broker.poll()[0].fill
    assert fill is not None
    assert fill.price == pytest.approx(expected_price)
    assert fill.fee == pytest.approx(expected_fee)


def test_paper_broker_marks_open_positions_in_its_equity() -> None:
    broker = make_paper(fee_rate=0.0)
    broker.submit(make_request(quantity=2.0), reference_price=100.0)
    cash = broker.fetch_balance()
    assert broker.wallet.cash == pytest.approx(cash)

    assert broker.equity() == pytest.approx(cash + 2.0 * 100.0)  # last fill price
    assert broker.equity(reference_prices={SYMBOL: 150.0}) == pytest.approx(cash + 300.0)
    broker.submit(
        make_request(client_order_id="ord-2", side=OrderSide.SELL, quantity=2.0),
        reference_price=150.0,
    )
    assert broker.equity(reference_prices={SYMBOL: 150.0}) == pytest.approx(broker.fetch_balance())


# ---------------------------------------------------------------------------
# 2. limit orders
# ---------------------------------------------------------------------------


def test_an_untouched_limit_order_rests_without_filling() -> None:
    broker = make_paper()

    buy = broker.submit(make_request(order_type=OrderType.LIMIT, price=90.0), reference_price=100.0)
    sell = broker.submit(
        make_request(
            client_order_id="ord-2", side=OrderSide.SELL, order_type=OrderType.LIMIT, price=110.0
        ),
        reference_price=100.0,
    )

    assert buy.state is OrderState.SUBMITTED
    assert sell.state is OrderState.SUBMITTED
    assert broker.poll() == []
    assert broker.fetch_balance() == 10_000.0
    orders = broker.open_orders()
    assert [order.client_order_id for order in orders] == ["ord-1", "ord-2"]
    assert all(order.filled_quantity == 0.0 for order in orders)
    assert all(order.price == pytest.approx(order.price) for order in orders)


@pytest.mark.parametrize(
    ("side", "limit_price", "reference_price", "touched"),
    [
        (OrderSide.BUY, 110.0, 100.0, True),
        (OrderSide.BUY, 100.0, 100.0, True),
        (OrderSide.BUY, 90.0, 100.0, False),
        (OrderSide.SELL, 90.0, 100.0, True),
        (OrderSide.SELL, 100.0, 100.0, True),
        (OrderSide.SELL, 110.0, 100.0, False),
    ],
)
def test_a_limit_order_fills_at_its_limit_price_only_when_it_is_touched(
    side: OrderSide, limit_price: float, reference_price: float, touched: bool
) -> None:
    broker = make_paper(fee_rate=0.0, slippage=0.05)  # slippage must not apply to limits

    ack = broker.submit(
        make_request(side=side, order_type=OrderType.LIMIT, price=limit_price, quantity=1.0),
        reference_price=reference_price,
    )

    if touched:
        assert ack.state is OrderState.FILLED
        fill = broker.poll()[0].fill
        assert fill is not None
        assert fill.price == pytest.approx(limit_price)
        assert fill.quantity == pytest.approx(1.0)
    else:
        assert ack.state is OrderState.SUBMITTED
        assert broker.poll() == []
        assert len(broker.open_orders()) == 1


def test_open_orders_are_sorted_by_creation_time_then_identifier() -> None:
    clock = make_clock()
    broker = make_paper(clock=clock)
    broker.submit(
        make_request(client_order_id="ord-b", order_type=OrderType.LIMIT, price=1.0),
        reference_price=100.0,
    )
    clock.advance(60)
    broker.submit(
        make_request(client_order_id="ord-c", order_type=OrderType.LIMIT, price=1.0),
        reference_price=100.0,
    )
    clock.advance(60)
    broker.submit(
        make_request(client_order_id="ord-a", order_type=OrderType.LIMIT, price=1.0),
        reference_price=100.0,
    )

    assert [order.client_order_id for order in broker.open_orders()] == [
        "ord-b",
        "ord-c",
        "ord-a",
    ]


# ---------------------------------------------------------------------------
# 3. deterministic partial fills
# ---------------------------------------------------------------------------


def test_a_partial_fill_is_completed_by_the_next_poll() -> None:
    broker = make_paper(fee_rate=0.0, partial_fill_probability=1.0, max_fill_fraction=0.4)

    ack = broker.submit(make_request(quantity=1.0), reference_price=100.0)
    assert ack.state is OrderState.PARTIALLY_FILLED

    first = broker.poll()
    assert event_types(first) == [BrokerEventType.ORDER_PARTIALLY_FILLED]
    assert first[0].fill is not None
    assert first[0].fill.quantity == pytest.approx(0.4)
    assert first[0].order is not None
    assert first[0].order.state is OrderState.PARTIALLY_FILLED
    assert first[0].order.filled_quantity == pytest.approx(0.4)
    open_orders = broker.open_orders()
    assert [order.client_order_id for order in open_orders] == ["ord-1"]
    assert open_orders[0].filled_quantity == pytest.approx(0.4)
    assert broker.fetch_balance() == pytest.approx(10_000.0 - 0.4 * 100.0)

    second = broker.poll()
    assert event_types(second) == [BrokerEventType.ORDER_FILLED]
    assert second[0].fill is not None
    assert second[0].fill.quantity == pytest.approx(0.6)
    assert second[0].order is not None
    assert second[0].order.state is OrderState.FILLED
    assert second[0].order.filled_quantity == 1.0
    assert first[0].fill.fill_id != second[0].fill.fill_id
    assert first[0].fill.quantity + second[0].fill.quantity == pytest.approx(1.0)
    assert broker.fetch_balance() == pytest.approx(10_000.0 - 1.0 * 100.0)

    assert broker.poll() == []
    assert broker.open_orders() == []


def test_a_partial_limit_fill_charges_the_fee_on_each_leg() -> None:
    broker = make_paper(fee_rate=0.01, partial_fill_probability=1.0, max_fill_fraction=0.5)
    broker.submit(
        make_request(order_type=OrderType.LIMIT, price=120.0, quantity=2.0),
        reference_price=100.0,
    )

    first_fill = broker.poll()[0].fill
    second_fill = broker.poll()[0].fill
    assert first_fill is not None and second_fill is not None
    assert first_fill.price == pytest.approx(120.0)
    assert second_fill.price == pytest.approx(120.0)
    assert first_fill.fee == pytest.approx(0.01 * 1.0 * 120.0)
    assert second_fill.fee == pytest.approx(0.01 * 1.0 * 120.0)
    assert broker.fetch_balance() == pytest.approx(
        10_000.0 - 2.0 * 120.0 - (first_fill.fee + second_fill.fee)
    )


def test_the_partial_fill_draw_depends_on_the_identifier_only() -> None:
    identifiers = [market_order_id(index) for index in range(12)]
    options: dict[str, Any] = {
        "partial_fill_probability": 0.5,
        "max_fill_fraction": 0.25,
        "fee_rate": 0.0,
        "seed": 7,
    }
    forward = make_paper(**options)
    for identifier in identifiers:
        forward.submit(
            make_request(client_order_id=identifier, quantity=4.0), reference_price=100.0
        )
    backward = make_paper(**options)
    for identifier in reversed(identifiers):
        backward.submit(
            make_request(client_order_id=identifier, quantity=4.0), reference_price=100.0
        )

    def draws(broker: PaperBroker) -> dict[str, float]:
        return {
            event.client_order_id: round(event.fill.quantity, 12)
            for event in broker.poll()
            if event.fill is not None
        }

    forward_draws = draws(forward)
    backward_draws = draws(backward)

    assert forward_draws == backward_draws
    assert len(forward_draws) == len(identifiers)
    # The draw really varies with the identifier, so the equality above is not
    # the equality of two constant functions.
    assert len(set(forward_draws.values())) > 1


def test_two_identical_runs_produce_identical_event_sequences() -> None:
    identifiers = [market_order_id(index) for index in range(6)]

    def run() -> list[list[str]]:
        clock = make_clock()
        broker = PaperBroker(
            clock=clock,
            fee_rate=0.001,
            slippage=0.002,
            seed=11,
            partial_fill_probability=0.5,
            max_fill_fraction=0.5,
        )
        sequences: list[list[str]] = []
        for index, identifier in enumerate(identifiers):
            order_type = OrderType.LIMIT if index % 3 == 0 else OrderType.MARKET
            broker.submit(
                make_request(
                    client_order_id=identifier,
                    order_type=order_type,
                    price=99.0 if order_type is OrderType.LIMIT else None,
                    quantity=1.0 + index,
                ),
                reference_price=100.0 + index,
            )
            clock.advance(30)
            sequences.append(event_signature(broker.poll()))
            clock.advance(30)
            sequences.append(event_signature(broker.poll()))
        return sequences

    assert run() == run()


# ---------------------------------------------------------------------------
# 4. cancellation
# ---------------------------------------------------------------------------


def test_cancel_reports_true_for_a_working_order_and_emits_one_event() -> None:
    broker = make_paper()
    broker.submit(make_request(order_type=OrderType.LIMIT, price=90.0), reference_price=100.0)

    assert broker.cancel("ord-1") is True
    assert broker.cancel("ord-1") is False  # already cancelled: terminal
    assert broker.cancel("unknown") is False
    assert broker.open_orders() == []

    events = broker.poll()
    assert event_types(events) == [BrokerEventType.ORDER_CANCELLED]
    assert events[0].order is not None
    assert events[0].order.state is OrderState.CANCELLED
    assert events[0].fill is None
    assert broker.poll() == []


def test_cancel_refuses_a_filled_order() -> None:
    broker = make_paper()
    broker.submit(make_request(), reference_price=100.0)
    assert broker.cancel("ord-1") is False


def test_cancelling_a_partially_filled_order_stops_the_completion() -> None:
    broker = make_paper(fee_rate=0.0, partial_fill_probability=1.0, max_fill_fraction=0.5)
    broker.submit(make_request(quantity=2.0), reference_price=100.0)
    assert event_types(broker.poll()) == [BrokerEventType.ORDER_PARTIALLY_FILLED]

    assert broker.cancel("ord-1") is True
    assert event_types(broker.poll()) == [BrokerEventType.ORDER_CANCELLED]
    assert broker.poll() == []
    assert broker.fetch_balance() == pytest.approx(10_000.0 - 1.0 * 100.0)


# ---------------------------------------------------------------------------
# 5. error paths and idempotency
# ---------------------------------------------------------------------------


def test_submit_rejects_a_limit_order_without_a_price() -> None:
    broker = make_paper()
    with pytest.raises(BrokerError, match="limit order requires a price"):
        broker.submit(make_request(order_type=OrderType.LIMIT), reference_price=100.0)


@pytest.mark.parametrize("quantity", [0.0, -1.0])
def test_submit_rejects_a_non_positive_quantity(quantity: float) -> None:
    broker = make_paper()
    with pytest.raises(BrokerError, match="order quantity must be positive"):
        broker.submit(make_request(quantity=quantity), reference_price=100.0)


@pytest.mark.parametrize("reference_price", [0.0, -5.0])
def test_submit_rejects_a_non_positive_reference_price(reference_price: float) -> None:
    broker = make_paper()
    with pytest.raises(BrokerError, match="reference price must be positive"):
        broker.submit(make_request(), reference_price=reference_price)


def test_a_rejected_submission_records_nothing_at_all() -> None:
    broker = make_paper()
    for bad_request, reference_price in (
        (make_request(order_type=OrderType.LIMIT), 1.0),
        (make_request(quantity=0.0), 1.0),
        (make_request(), 0.0),
    ):
        with pytest.raises(BrokerError):
            broker.submit(bad_request, reference_price=reference_price)
    assert broker.poll() == []
    assert broker.open_orders() == []
    assert broker.fetch_balance() == 10_000.0


def test_a_duplicate_identifier_returns_the_existing_order_without_a_second_fill() -> None:
    broker = make_paper(fee_rate=0.001)
    request = make_request(quantity=1.0)

    first = broker.submit(request, reference_price=100.0)
    cash_after_first = broker.fetch_balance()
    assert len(broker.poll()) == 1

    second = broker.submit(request, reference_price=500.0)

    assert second.state is OrderState.FILLED
    assert second.submitted_at == first.submitted_at
    assert second.broker_order_id == first.broker_order_id
    assert broker.fetch_balance() == cash_after_first
    assert broker.poll() == []


def test_a_duplicate_resting_limit_order_is_not_queued_twice() -> None:
    broker = make_paper()
    request = make_request(order_type=OrderType.LIMIT, price=1.0)

    first = broker.submit(request, reference_price=100.0)
    second = broker.submit(request, reference_price=100.0)

    assert first.state is OrderState.SUBMITTED
    assert second.state is OrderState.SUBMITTED
    assert len(broker.open_orders()) == 1


def test_a_duplicate_identifier_that_was_rejected_is_accepted_again() -> None:
    broker = make_paper()
    rejected = Order(
        client_order_id="ord-1",
        profile_id=PROFILE_ID,
        symbol=SYMBOL,
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        quantity=1.0,
        state=OrderState.REJECTED,
        mode=RunMode.PAPER,
        created_at=pd.Timestamp(START),
        updated_at=pd.Timestamp(START),
        reject_reason="venue said no",
    )
    broker.inject_venue_order(rejected)

    ack = broker.submit(make_request(), reference_price=100.0)

    assert ack.state is OrderState.FILLED


# ---------------------------------------------------------------------------
# 5b. the guards that keep a restart from filling twice
# ---------------------------------------------------------------------------


def submit_partial(broker: PaperBroker, *, quantity: float = 2.0) -> Order:
    """Submit a two-legged order, drain the partial fill and return its order."""
    broker.submit(make_request(quantity=quantity), reference_price=100.0)
    event = broker.poll()[0]
    assert event.event_type is BrokerEventType.ORDER_PARTIALLY_FILLED
    assert event.order is not None
    return event.order


def test_a_remainder_is_never_filled_when_the_venue_already_completed_the_order() -> None:
    broker = make_paper(fee_rate=0.0, partial_fill_probability=1.0, max_fill_fraction=0.5)
    order = submit_partial(broker)
    cash = broker.fetch_balance()
    broker.inject_venue_order(dataclasses.replace(order, state=OrderState.FILLED))

    assert broker.poll() == []
    assert broker.fetch_balance() == cash


def test_a_remainder_falls_back_to_the_limit_price_when_no_fill_price_is_known() -> None:
    broker = make_paper(fee_rate=0.0, partial_fill_probability=1.0, max_fill_fraction=0.5)
    order = submit_partial(broker)
    broker.inject_venue_order(
        dataclasses.replace(
            order,
            average_fill_price=None,
            price=90.0,
        )
    )

    events = broker.poll()

    assert event_types(events) == [BrokerEventType.ORDER_FILLED]
    assert events[0].fill is not None
    assert events[0].fill.quantity == pytest.approx(1.0)
    assert events[0].fill.price == pytest.approx(90.0)


def test_a_remainder_is_left_alone_when_it_cannot_be_priced() -> None:
    broker = make_paper(fee_rate=0.0, partial_fill_probability=1.0, max_fill_fraction=0.5)
    order = submit_partial(broker)
    cash = broker.fetch_balance()
    broker.inject_venue_order(dataclasses.replace(order, average_fill_price=None, price=None))

    assert broker.poll() == []
    assert broker.fetch_balance() == cash


def test_a_remainder_falls_back_to_the_known_remaining_quantity() -> None:
    broker = make_paper(fee_rate=0.0, partial_fill_probability=1.0, max_fill_fraction=0.5)
    order = submit_partial(broker)
    broker.inject_venue_order(dataclasses.replace(order, filled_quantity=order.quantity))

    events = broker.poll()

    assert event_types(events) == [BrokerEventType.ORDER_FILLED]
    assert events[0].fill is not None
    assert events[0].fill.quantity == pytest.approx(1.0)
    assert events[0].order is not None
    assert events[0].order.filled_quantity == pytest.approx(order.quantity)


def test_the_simulated_book_closes_a_long_exactly() -> None:
    broker = make_paper(fee_rate=0.0)
    broker.submit(make_request(quantity=2.0), reference_price=100.0)
    broker.poll()

    # Closing the long exactly leaves a flat book: equity is the cash balance.
    broker.submit(
        make_request(client_order_id="ord-2", side=OrderSide.SELL, quantity=2.0),
        reference_price=120.0,
    )

    assert broker.fetch_balance() == pytest.approx(10_000.0 + 2.0 * 120.0 - 2.0 * 100.0)
    assert broker.equity(reference_prices={SYMBOL: 120.0}) == pytest.approx(broker.fetch_balance())
    assert broker.equity() == pytest.approx(broker.fetch_balance())


def test_the_simulated_book_flips_a_long_into_a_short_in_one_fill() -> None:
    broker = make_paper(fee_rate=0.0)
    broker.submit(make_request(quantity=2.0), reference_price=100.0)
    broker.poll()

    broker.submit(
        make_request(client_order_id="ord-2", side=OrderSide.SELL, quantity=3.0),
        reference_price=120.0,
    )

    # One unit short, marked at the price of the flipping fill.
    assert broker.fetch_balance() == pytest.approx(10_000.0 - 2.0 * 100.0 + 3.0 * 120.0)
    assert broker.equity(reference_prices={SYMBOL: 120.0}) == pytest.approx(
        broker.fetch_balance() - 1.0 * 120.0
    )
    assert broker.equity() == pytest.approx(broker.fetch_balance() - 1.0 * 120.0)


def test_partially_selling_a_long_keeps_a_single_open_unit() -> None:
    broker = make_paper(fee_rate=0.0)
    broker.submit(make_request(quantity=2.0), reference_price=100.0)
    broker.submit(
        make_request(client_order_id="ord-2", side=OrderSide.SELL, quantity=1.0),
        reference_price=200.0,
    )

    # One unit is left and it is marked at the last observed fill price.
    assert broker.fetch_balance() == pytest.approx(10_000.0 - 200.0 + 200.0)
    assert broker.equity() == pytest.approx(broker.fetch_balance() + 1.0 * 200.0)
    assert broker.equity(reference_prices={SYMBOL: 150.0}) == pytest.approx(
        broker.fetch_balance() + 150.0
    )


def test_adding_to_a_long_keeps_both_units_open() -> None:
    broker = make_paper(fee_rate=0.0)
    broker.submit(make_request(quantity=1.0), reference_price=100.0)
    broker.submit(make_request(client_order_id="ord-2", quantity=1.0), reference_price=200.0)

    assert broker.fetch_balance() == pytest.approx(10_000.0 - 300.0)
    assert broker.equity() == pytest.approx(broker.fetch_balance() + 2.0 * 200.0)


def test_paper_broker_exposes_its_initial_balance() -> None:
    broker = make_paper(initial_balance=4321.0)

    assert broker.initial_balance == pytest.approx(4321.0)
    assert broker.wallet.initial_balance == pytest.approx(4321.0)


# ---------------------------------------------------------------------------
# 5c. the shared platform wallet the venue is funded from
# ---------------------------------------------------------------------------


def test_the_paper_broker_owns_no_cash_of_its_own() -> None:
    broker = make_paper(initial_balance=2_000.0)

    assert isinstance(broker.wallet, PlatformWallet)
    assert broker.wallet is broker.wallet  # a read-only property, always the same object
    assert broker.wallet.mode is RunMode.PAPER
    assert broker.wallet.is_authoritative is True
    assert broker.wallet.name == "paper-wallet"
    assert broker.wallet.cash == pytest.approx(2_000.0)
    assert broker.fetch_balance() == pytest.approx(broker.wallet.cash)
    assert broker.initial_balance == pytest.approx(broker.wallet.initial_balance)
    # The venue used to keep its own counter: it is gone, the wallet is the ledger.
    assert not hasattr(broker, "_cash")
    assert not hasattr(broker, "_initial_balance")


def test_a_fill_moves_the_shared_wallet_and_nothing_else() -> None:
    wallet = make_wallet()
    broker = make_paper(wallet=wallet, fee_rate=0.0)

    broker.submit(make_request(quantity=2.0), reference_price=100.0)

    assert wallet.cash == pytest.approx(10_000.0 - 2.0 * 100.0)
    assert broker.fetch_balance() == pytest.approx(10_000.0 - 2.0 * 100.0)
    # The mark-to-market lives in the venue, the cash lives in the wallet.
    assert broker.equity(reference_prices={SYMBOL: 150.0}) == pytest.approx(
        10_000.0 - 2.0 * 100.0 + 2.0 * 150.0
    )


def test_two_brokers_sharing_one_wallet_see_one_balance() -> None:
    wallet = make_wallet()
    first = make_paper(wallet=wallet, fee_rate=0.0)
    second = make_paper(wallet=wallet, fee_rate=0.0)

    assert first.fetch_balance() == pytest.approx(10_000.0)
    assert second.fetch_balance() == pytest.approx(10_000.0)

    first.submit(make_request(quantity=1.0), reference_price=100.0)

    # The debit of the first profile is immediately visible on the second one:
    # there is exactly one ledger for the whole platform.
    assert first.fetch_balance() == pytest.approx(9_900.0)
    assert second.fetch_balance() == pytest.approx(9_900.0)
    assert wallet.cash == pytest.approx(9_900.0)

    second.submit(
        make_request(client_order_id="ord-2", side=OrderSide.SELL, quantity=1.0),
        reference_price=120.0,
    )

    assert first.fetch_balance() == pytest.approx(10_020.0)
    assert second.fetch_balance() == pytest.approx(10_020.0)
    assert wallet.cash == pytest.approx(10_020.0)


def test_an_external_debit_of_the_shared_wallet_is_visible_on_the_venue() -> None:
    wallet = make_wallet()
    broker = make_paper(wallet=wallet, fee_rate=0.0)

    wallet.debit(1_500.0, reason="allocation transfer")

    assert broker.fetch_balance() == pytest.approx(8_500.0)
    assert broker.equity() == pytest.approx(8_500.0)
    assert broker.equity(reference_prices={SYMBOL: 100.0}) == pytest.approx(8_500.0)


def test_a_paper_broker_refuses_a_live_wallet() -> None:
    live = make_wallet(mode=RunMode.LIVE)

    with pytest.raises(BrokerError, match="paper broker requires a paper wallet, got mode 'live'"):
        make_paper(wallet=live)


def test_restore_cash_delegates_to_the_shared_wallet() -> None:
    wallet = make_wallet()
    broker = make_paper(wallet=wallet)

    broker.restore_cash(4_321.0)

    assert wallet.cash == pytest.approx(4_321.0)
    assert broker.fetch_balance() == pytest.approx(4_321.0)
    # Every broker sharing the wallet is re-seeded by that one call.
    assert make_paper(wallet=wallet).fetch_balance() == pytest.approx(4_321.0)


def test_restore_cash_keeps_its_finiteness_contract() -> None:
    wallet = make_wallet()
    broker = make_paper(wallet=wallet)

    with pytest.raises(ValueError, match="restored cash must be finite, got nan"):
        broker.restore_cash(float("nan"))

    assert wallet.cash == pytest.approx(10_000.0)
    assert broker.fetch_balance() == pytest.approx(10_000.0)


def test_an_unfunded_fill_is_refused_and_leaves_nothing_behind() -> None:
    wallet = make_wallet(initial_balance=50.0)
    broker = make_paper(wallet=wallet, fee_rate=0.0)

    with pytest.raises(WalletError, match=r"platform wallet cannot debit 100\.00 USDT"):
        broker.submit(make_request(quantity=1.0), reference_price=100.0)

    assert wallet.cash == pytest.approx(50.0)
    assert broker.fetch_balance() == pytest.approx(50.0)
    assert broker.open_orders() == []
    assert broker.poll() == []
    assert broker.equity(reference_prices={SYMBOL: 100.0}) == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# 6. reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_is_ok_when_the_two_sides_agree() -> None:
    broker = make_paper()
    broker.submit(make_request(order_type=OrderType.LIMIT, price=90.0), reference_price=100.0)

    report = broker.reconcile(broker.open_orders())

    assert report.ok is True
    assert report.matched == 1
    assert report.only_at_venue == ()
    assert report.only_locally == ()
    assert report.mismatched == ()
    assert report.profile_id == PROFILE_ID
    assert report.checked_at == pd.Timestamp(START)
    assert report.details["local_orders"] == 1
    assert report.to_dict()["ok"] is True


def test_reconcile_reports_an_order_the_local_state_ignores() -> None:
    broker = make_paper()
    broker.submit(make_request(order_type=OrderType.LIMIT, price=90.0), reference_price=100.0)
    local = broker.open_orders()
    broker.inject_venue_order(dataclasses.replace(local[0], client_order_id="venue-only"))

    report = broker.reconcile(local)

    assert report.ok is False
    assert report.only_at_venue == ("venue-only",)
    assert report.only_locally == ()
    assert report.mismatched == ()
    assert report.matched == 1


def test_reconcile_reports_an_order_the_venue_never_saw() -> None:
    broker = make_paper()
    broker.submit(make_request(order_type=OrderType.LIMIT, price=90.0), reference_price=100.0)
    local = broker.open_orders()

    report = broker.reconcile([*local, dataclasses.replace(local[0], client_order_id="ghost")])

    assert report.ok is False
    assert report.only_locally == ("ghost",)
    assert report.only_at_venue == ()


def test_reconcile_reports_a_state_difference_as_mismatched() -> None:
    broker = make_paper()
    broker.submit(make_request(order_type=OrderType.LIMIT, price=90.0), reference_price=100.0)
    local = broker.open_orders()

    report = broker.reconcile([dataclasses.replace(local[0], state=OrderState.FILLED)])

    assert report.ok is False
    assert report.mismatched == ("ord-1",)
    assert report.matched == 0


def test_reconcile_reports_a_quantity_difference_without_going_degraded() -> None:
    broker = make_paper(partial_fill_probability=1.0, max_fill_fraction=0.5)
    broker.submit(make_request(quantity=2.0), reference_price=100.0)
    event = broker.poll()[0]
    assert event.order is not None

    report = broker.reconcile(
        [dataclasses.replace(event.order, filled_quantity=0.0, state=OrderState.PARTIALLY_FILLED)]
    )

    assert report.ok is True
    assert report.details["quantity_mismatches"] == ["ord-1"]


def test_reconcile_defaults_to_an_empty_local_view() -> None:
    broker = make_paper()
    broker.submit(make_request(order_type=OrderType.LIMIT, price=90.0), reference_price=100.0)

    report = broker.reconcile()

    # The venue holds an order the caller did not even mention.
    assert report.profile_id == PROFILE_ID
    assert report.only_at_venue == ("ord-1",)
    assert report.only_locally == ()
    assert report.ok is False


def test_reconcile_of_an_empty_broker_has_no_profile_and_is_ok() -> None:
    report = make_paper().reconcile()
    assert report.ok is True
    assert report.profile_id == ""
    assert report.matched == 0


def test_reconcile_ignores_terminal_orders_the_venue_no_longer_holds() -> None:
    """The regressed production report: a durable history against an empty venue.

    ``ExecutionGateway.reconcile`` hands the profile's COMPLETE durable history to
    the comparison while the venue only reports what it currently works, so every
    terminal order used to be reported as ``only_locally`` and degraded a healthy
    paper profile.  A terminal order the venue no longer holds is not a divergence.
    """
    broker = make_paper()
    terminal_states = (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)
    local = [
        Order(
            profile_id=PROFILE_ID,
            client_order_id=f"test1-BTC_USDT-20240101T000000-{index:04d}",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            quantity=1.0,
            state=terminal_states[index % len(terminal_states)],
            mode=RunMode.PAPER,
            created_at=pd.Timestamp(START),
            updated_at=pd.Timestamp(START),
            filled_quantity=1.0,
        )
        for index in range(18)
    ]

    report = broker.reconcile(local)

    assert report.ok is True
    assert report.matched == 0
    assert report.only_locally == ()
    assert report.only_at_venue == ()
    assert report.mismatched == ()
    assert report.details["local_orders"] == 0
    assert report.details["venue_orders"] == 0


def test_reconcile_mixes_working_and_terminal_orders() -> None:
    """Only working orders may be compared; terminal ones the venue forgot vanish."""
    broker = make_paper()
    broker.submit(make_request(order_type=OrderType.LIMIT, price=90.0), reference_price=100.0)
    agreed = broker.open_orders()
    working = dataclasses.replace(agreed[0], client_order_id="ghost")
    terminal = dataclasses.replace(agreed[0], client_order_id="gone", state=OrderState.FILLED)

    report = broker.reconcile([*agreed, working, terminal])

    assert report.matched == 1
    assert report.only_locally == ("ghost",)
    assert report.ok is False
    assert report.only_at_venue == ()
    assert report.mismatched == ()
    # The terminal order the venue no longer holds appears nowhere in the report.
    for field in ("only_locally", "only_at_venue", "mismatched"):
        assert "gone" not in getattr(report, field)
    assert report.details["local_orders"] == 2


def test_reconcile_reports_a_terminal_order_the_venue_still_holds_as_mismatched() -> None:
    """A terminal order the venue still knows keeps being compared (the trap)."""
    broker = make_paper()
    broker.submit(make_request(order_type=OrderType.LIMIT, price=90.0), reference_price=100.0)
    local = broker.open_orders()

    report = broker.reconcile([dataclasses.replace(local[0], state=OrderState.FILLED)])

    assert report.ok is False
    assert report.mismatched == ("ord-1",)
    assert report.matched == 0
    assert report.only_locally == ()


def test_reconcile_of_a_venue_only_order_lands_in_only_at_venue() -> None:
    """An order only the venue holds is reported as ``only_at_venue``, never matched."""
    broker = make_paper()
    broker.inject_venue_order(
        Order(
            profile_id=PROFILE_ID,
            client_order_id="venue-only",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            quantity=1.0,
            price=90.0,
            state=OrderState.SUBMITTED,
            mode=RunMode.PAPER,
            created_at=pd.Timestamp(START),
            updated_at=pd.Timestamp(START),
        )
    )

    report = broker.reconcile()

    assert report.ok is False
    assert report.only_at_venue == ("venue-only",)
    assert report.only_locally == ()
    assert report.mismatched == ()
    assert report.matched == 0
    assert report.details["local_orders"] == 0
    assert report.details["venue_orders"] == 1


# ---------------------------------------------------------------------------
# 7. paper mode is never configurable
# ---------------------------------------------------------------------------


def test_paper_broker_mode_is_always_paper() -> None:
    assert make_paper().mode is RunMode.PAPER
    assert make_paper(name="sim", seed=99, fee_rate=0.5).mode is RunMode.PAPER
    assert "mode" not in inspect.signature(PaperBroker.__init__).parameters
    with pytest.raises(TypeError):
        PaperBroker(clock=make_clock(), mode=RunMode.LIVE)  # type: ignore[call-arg]


def test_paper_broker_is_named_paper_by_default_and_can_be_renamed() -> None:
    assert PaperBroker(clock=make_clock()).name == "paper"
    assert PaperBroker(clock=make_clock(), name="").name == "paper"
    assert PaperBroker(clock=make_clock(), name="sim-1").name == "sim-1"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"initial_balance": 0.0}, "initial_balance must be positive"),
        ({"fee_rate": -0.1}, "fee_rate must be non-negative"),
        ({"slippage": -0.1}, "slippage must be non-negative"),
        ({"partial_fill_probability": 1.5}, "partial_fill_probability"),
        ({"partial_fill_probability": -0.1}, "partial_fill_probability"),
        ({"max_fill_fraction": 0.0}, "max_fill_fraction"),
        ({"max_fill_fraction": 1.5}, "max_fill_fraction"),
    ],
)
def test_paper_broker_rejects_an_out_of_range_configuration(
    kwargs: dict[str, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_paper(**kwargs)


def test_paper_broker_keeps_a_faithful_record_of_the_requested_mode() -> None:
    broker = make_paper()
    broker.submit(
        make_request(mode=RunMode.PAPER, order_type=OrderType.LIMIT, price=1.0),
        reference_price=100.0,
    )
    order = broker.open_orders()[0]
    assert order.mode is RunMode.PAPER
    # The broker itself can never become live, whatever the request carried.
    assert broker.mode is RunMode.PAPER


# ---------------------------------------------------------------------------
# 8. the optional extra
# ---------------------------------------------------------------------------


def test_ccxt_broker_requires_configured_credentials() -> None:
    with pytest.raises(BrokerError, match="exchange credentials are not configured"):
        CcxtBroker(ExchangeCredentials(api_key="", api_secret=""), clock=make_clock())


def test_ccxt_broker_is_a_broker() -> None:
    broker = CcxtBroker(
        make_credentials(), clock=make_clock(), mode=RunMode.LIVE, exchange="kraken"
    )
    assert isinstance(broker, Broker)
    assert broker.name == "kraken"
    assert broker.mode is RunMode.LIVE


def test_ccxt_broker_names_the_missing_extra_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "ccxt", None)
    broker = CcxtBroker(make_credentials(), clock=make_clock())

    calls: dict[str, Any] = {
        "submit": lambda: broker.submit(make_request(), reference_price=100.0),
        "cancel": lambda: broker.cancel("ord-1"),
        "poll": broker.poll,
        "open_orders": broker.open_orders,
        "fetch_balance": broker.fetch_balance,
        "reconcile": broker.reconcile,
    }
    for name, call in calls.items():
        with pytest.raises(BrokerUnavailableError) as error:
            call()
        message = str(error.value)
        assert "ccxt is not installed" in message, name
        assert "[exchange]" in message, name
        assert "pip install" in message, name


def test_ccxt_broker_rejects_an_unknown_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ccxt(monkeypatch)
    broker = CcxtBroker(make_credentials(), clock=make_clock(), exchange="not-a-venue")
    with pytest.raises(BrokerError, match="not supported by the installed ccxt"):
        broker.poll()


def test_ccxt_broker_converts_a_client_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = types.ModuleType("ccxt")

    def explode(config: dict[str, Any]) -> Any:
        raise RuntimeError("bad api key shape")

    module.explode = explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ccxt", module)
    broker = CcxtBroker(make_credentials(), clock=make_clock(), exchange="explode")

    with pytest.raises(BrokerError, match="could not build the explode client"):
        broker.poll()


# ---------------------------------------------------------------------------
# 9. the ccxt adapter against a fake venue
# ---------------------------------------------------------------------------


def test_ccxt_submit_passes_the_client_order_id_and_maps_the_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    request = make_request(client_order_id="ord-2", quantity=2.0, mode=RunMode.LIVE)

    ack = broker.submit(request, reference_price=100.0)

    assert venue.config["apiKey"] == "test-key"
    assert venue.config["secret"] == "test-secret"
    assert venue.config["enableRateLimit"] is True
    assert venue.config["options"] == {"defaultType": "spot"}
    assert venue.config["timeout"] == 10_000
    assert len(venue.created) == 1
    created = venue.created[0]
    assert created["symbol"] == SYMBOL
    assert created["type"] == "market"
    assert created["side"] == "buy"
    assert created["amount"] == 2.0
    assert created["price"] is None
    assert created["params"] == {"clientOrderId": "ord-2"}
    assert ack.accepted is True
    assert ack.state is OrderState.SUBMITTED
    assert ack.broker_order_id == "venue-1"
    assert ack.reason == ""
    assert ack.submitted_at == pd.Timestamp(START)


def test_ccxt_submit_is_idempotent_on_the_client_order_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    request = make_request(client_order_id="ord-2", quantity=2.0, mode=RunMode.LIVE)

    first = broker.submit(request, reference_price=100.0)
    second = broker.submit(request, reference_price=100.0)

    assert len(venue.created) == 1, "the same order must never be sent twice"
    assert second.broker_order_id == first.broker_order_id
    assert second.state is first.state


def test_ccxt_submit_maps_a_rejected_status_to_an_unaccepted_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    venue.create_status = "rejected"

    ack = broker.submit(make_request(mode=RunMode.LIVE), reference_price=100.0)

    assert ack.accepted is False
    assert ack.state is OrderState.REJECTED
    assert ack.reason


def test_ccxt_submit_converts_a_venue_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    venue.create_error = RuntimeError("insufficient balance")

    with pytest.raises(BrokerError, match="venue rejected the order ord-1"):
        broker.submit(make_request(mode=RunMode.LIVE), reference_price=100.0)


def test_ccxt_submit_validates_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    with pytest.raises(BrokerError, match="order quantity must be positive"):
        broker.submit(make_request(quantity=0.0, mode=RunMode.LIVE), reference_price=100.0)
    with pytest.raises(BrokerError, match="limit order requires a price"):
        broker.submit(
            make_request(order_type=OrderType.LIMIT, mode=RunMode.LIVE), reference_price=100.0
        )
    assert venue.created == [], "a rejected request never reaches the venue"
    # The validation happens even when the credentials come from the environment
    # and no client has been built yet.
    other, _ = make_ccxt(monkeypatch)
    with pytest.raises(BrokerError, match="limit order requires a price"):
        other.submit(make_request(order_type=OrderType.LIMIT), reference_price=100.0)


def test_ccxt_cancel_reports_the_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, venue = make_ccxt(monkeypatch)
    broker.submit(make_request(mode=RunMode.LIVE), reference_price=100.0)

    assert broker.cancel("ord-1") is True
    assert venue.cancelled == [("ord-1", SYMBOL)]
    assert broker.cancel("never-seen") is True
    assert venue.cancelled[-1] == ("never-seen", None)


def test_ccxt_cancel_converts_a_venue_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)

    def explode(client_order_id: Any) -> dict[str, Any]:
        raise RuntimeError("order is already closed")

    venue.cancel_order = explode  # type: ignore[method-assign]
    with pytest.raises(BrokerError, match="could not cancel the order ord-1"):
        broker.cancel("ord-1")


def test_ccxt_open_orders_reads_the_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, venue = make_ccxt(monkeypatch)
    venue.open_payloads = [venue_open_order("ord-1"), venue_open_order("ord-2", price=95.0)]

    orders = broker.open_orders()

    assert [order.client_order_id for order in orders] == ["ord-1", "ord-2"]
    assert orders[0].symbol == SYMBOL
    assert orders[0].state is OrderState.SUBMITTED
    assert orders[0].mode is RunMode.LIVE
    assert orders[1].broker_order_id == "venue-ord-2"


def test_ccxt_poll_reports_an_accepted_order_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    venue.open_payloads = [venue_open_order("ord-1")]

    first = broker.poll()
    assert event_types(first) == [BrokerEventType.ORDER_ACCEPTED]
    assert first[0].order is not None
    assert first[0].order.client_order_id == "ord-1"
    assert broker.poll() == []


def test_ccxt_poll_reports_a_vanished_order_as_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    venue.open_payloads = [venue_open_order("ord-1")]
    broker.poll()

    venue.open_payloads = []
    events = broker.poll()

    assert event_types(events) == [BrokerEventType.ORDER_CANCELLED]
    assert events[0].client_order_id == "ord-1"


def test_ccxt_poll_reports_terminal_open_order_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    venue.open_payloads = [
        venue_open_order("ord-1", status="canceled"),
        venue_open_order("ord-2", status="closed"),
        venue_open_order("ord-3", status="rejected"),
        {"symbol": SYMBOL},  # no identifier: ignored
    ]

    events = broker.poll()

    assert event_types(events) == [
        BrokerEventType.ORDER_CANCELLED,
        BrokerEventType.ORDER_FILLED,
        BrokerEventType.ORDER_REJECTED,
    ]
    assert [event.client_order_id for event in events] == ["ord-1", "ord-2", "ord-3"]


def test_ccxt_poll_maps_a_trade_to_one_fill_and_never_repeats_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    broker.submit(make_request(mode=RunMode.LIVE), reference_price=100.0)
    assert broker.poll() == []  # the venue holds no order and reports no trade yet

    venue.trades = [venue_trade()]
    first = [event for event in broker.poll() if event.fill is not None]
    assert len(first) == 1
    fill = first[0].fill
    assert fill is not None
    assert fill.fill_id == "ccxt-trade-1"
    assert fill.client_order_id == "ord-1"
    assert fill.symbol == SYMBOL
    assert fill.side is OrderSide.BUY
    assert fill.quantity == pytest.approx(1.0)
    assert fill.price == pytest.approx(101.0)
    assert fill.fee == pytest.approx(0.101)
    assert fill.mode is RunMode.LIVE
    assert fill.timestamp == pd.Timestamp(1704067200000, unit="ms", tz=UTC)

    # The very same trade is never reported twice.
    assert [event for event in broker.poll() if event.fill is not None] == []


def test_ccxt_poll_falls_back_to_the_house_fee_and_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    trade = venue_trade(fee_cost=None)
    trade["timestamp"] = None
    venue.trades = [trade]

    fill = broker.poll()[0].fill
    assert fill is not None
    assert fill.fee == pytest.approx(0.001 * 1.0 * 101.0)
    assert fill.timestamp == pd.Timestamp(START)


def test_ccxt_poll_skips_an_unusable_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, venue = make_ccxt(monkeypatch)
    unknown_side = venue_trade(trade_id="trade-1", side="sideways")
    no_identifier = venue_trade(trade_id="trade-2")
    del no_identifier["clientOrderId"]
    del no_identifier["order"]
    venue.trades = [unknown_side, no_identifier]

    assert broker.poll() == []


def test_ccxt_poll_survives_a_venue_failure_by_reporting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    venue.open_orders_error = RuntimeError("502 bad gateway")

    events = broker.poll()

    assert event_types(events) == [BrokerEventType.ERROR]
    assert "502 bad gateway" in events[0].message
    assert events[0].profile_id == PROFILE_ID


def test_ccxt_open_orders_reports_a_venue_failure_as_an_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    venue.open_orders_error = RuntimeError("rate limited")

    # open_orders() has no event channel: it re-reads through the same helper and
    # therefore returns an empty book rather than raising.
    assert broker.open_orders() == []


def test_ccxt_fetch_balance_returns_the_quote_currency_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    assert broker.fetch_balance() == pytest.approx(1234.5)

    venue.balance = {"total": {"BTC": 0.5}, "USDT": {"total": 42.0}}
    assert broker.fetch_balance() == pytest.approx(42.0)

    venue.balance = {"total": {"BTC": 0.5}}
    assert broker.fetch_balance() == pytest.approx(0.5)

    venue.balance = {"free": {"USDT": 1.0}}
    assert broker.fetch_balance() is None

    venue.balance = {"USDT": {"total": None}}
    assert broker.fetch_balance() is None


def test_ccxt_fetch_balance_converts_a_venue_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)

    def explode() -> dict[str, Any]:
        raise RuntimeError("permission denied")

    venue.fetch_balance = explode  # type: ignore[method-assign]
    with pytest.raises(BrokerError, match="could not read the balance"):
        broker.fetch_balance()


def test_ccxt_reconcile_compares_the_local_state_with_the_venue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    venue.open_payloads = [venue_open_order("ord-1")]

    ok = broker.reconcile(broker.open_orders())
    assert ok.ok is True
    assert ok.matched == 1
    assert ok.profile_id == PROFILE_ID
    assert ok.details["venue_orders"] == 1

    missing = broker.reconcile()
    assert missing.ok is False
    assert missing.only_at_venue == ("ord-1",)


def test_ccxt_reconcile_converts_a_venue_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    venue.open_orders_error = RuntimeError("timeout")

    with pytest.raises(BrokerError, match="could not reconcile against the venue"):
        broker.reconcile()


def test_ccxt_broker_rejects_a_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        CcxtBroker(make_credentials(), clock=make_clock(), timeout_seconds=0.0)


def test_ccxt_broker_falls_back_to_the_credential_exchange_name() -> None:
    broker = CcxtBroker(make_credentials(exchange="kraken"), clock=make_clock(), exchange="")
    assert broker.name == "kraken"


def test_ccxt_broker_passes_the_optional_password_and_learns_the_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = make_credentials(password="passphrase", profile_id="")
    broker, venue = make_ccxt(monkeypatch, credentials=credentials)

    assert venue.config["password"] == "passphrase"
    broker.submit(make_request(mode=RunMode.LIVE, profile_id="eth-live"), reference_price=100.0)
    assert broker.reconcile().profile_id == "eth-live"


def test_ccxt_broker_refuses_a_client_without_the_needed_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CrippledVenue:
        """A venue client that only knows how to place orders."""

        def __init__(self, config: dict[str, Any]) -> None:
            self.config = config

        def create_order(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"id": "venue-1", "clientOrderId": "ord-1", "status": "open"}

        def fetch_open_orders(self, symbol: Any = None) -> list[dict[str, Any]]:
            return []

    module = types.ModuleType("ccxt")
    module.binance = CrippledVenue  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ccxt", module)
    broker = CcxtBroker(make_credentials(), clock=make_clock())

    assert broker.open_orders() == []
    with pytest.raises(BrokerError, match="does not support fetch_my_trades"):
        broker.poll()

    class OrderOnlyVenue(CrippledVenue):
        """A venue client that cannot list its open orders at all."""

        fetch_open_orders = None  # type: ignore[assignment]

    module.binance = OrderOnlyVenue  # type: ignore[attr-defined]
    blind = CcxtBroker(make_credentials(), clock=make_clock())
    with pytest.raises(BrokerError, match="does not support fetch_open_orders"):
        blind.poll()


def test_ccxt_broker_submits_without_probing_a_client_that_cannot_be_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlindVenue:
        """A venue client without ``fetch_order``: nothing to probe with."""

        def __init__(self, config: dict[str, Any]) -> None:
            self.config = config
            self.created: list[Any] = []

        def create_order(
            self,
            symbol: Any,
            order_type: Any,
            side: Any,
            amount: Any,
            price: Any,
            params: Any = None,
        ) -> dict[str, Any]:
            self.created.append(params)
            return {"id": "venue-1", "clientOrderId": "ord-1", "status": "open"}

    module = types.ModuleType("ccxt")
    module.binance = BlindVenue  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ccxt", module)
    broker = CcxtBroker(make_credentials(), clock=make_clock())

    ack = broker.submit(make_request(mode=RunMode.LIVE), reference_price=100.0)

    assert ack.state is OrderState.SUBMITTED
    assert ack.broker_order_id == "venue-1"


def test_ccxt_broker_converts_a_failed_idempotency_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)

    def explode(client_order_id: Any, symbol: Any = None) -> dict[str, Any]:
        raise RuntimeError("the venue is unreachable")

    venue.fetch_order = explode  # type: ignore[method-assign]
    with pytest.raises(BrokerError, match="could not read the venue order ord-1"):
        broker.submit(make_request(mode=RunMode.LIVE), reference_price=100.0)


def test_ccxt_poll_reports_a_trade_read_failure_as_an_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)

    def explode(symbol: Any = None) -> list[dict[str, Any]]:
        raise RuntimeError("trades endpoint is down")

    venue.fetch_my_trades = explode  # type: ignore[method-assign]
    events = broker.poll()

    assert event_types(events) == [BrokerEventType.ERROR]
    assert "trades endpoint is down" in events[0].message


def test_ccxt_broker_never_rewraps_its_own_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, venue = make_ccxt(monkeypatch)
    own_error = BrokerError("an error that is already ours")

    def failing_create(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise own_error

    venue.create_order = failing_create  # type: ignore[method-assign]
    with pytest.raises(BrokerError) as error:
        broker.submit(make_request(mode=RunMode.LIVE), reference_price=100.0)
    assert error.value is own_error

    def failing_open_orders(symbol: Any = None) -> list[dict[str, Any]]:
        raise own_error

    venue.fetch_open_orders = failing_open_orders  # type: ignore[method-assign]
    with pytest.raises(BrokerError) as poll_error:
        broker.poll()
    assert poll_error.value is own_error
    with pytest.raises(BrokerError) as reconcile_error:
        broker.reconcile()
    assert reconcile_error.value is own_error

    def failing_balance() -> dict[str, Any]:
        raise own_error

    venue.fetch_balance = failing_balance  # type: ignore[method-assign]
    with pytest.raises(BrokerError) as balance_error:
        broker.fetch_balance()
    assert balance_error.value is own_error

    def failing_cancel(client_order_id: Any, symbol: Any = None) -> dict[str, Any]:
        raise own_error

    venue.cancel_order = failing_cancel  # type: ignore[method-assign]
    with pytest.raises(BrokerError) as cancel_error:
        broker.cancel("ord-1")
    assert cancel_error.value is own_error

    def failing_probe(client_order_id: Any, symbol: Any = None) -> dict[str, Any]:
        raise own_error

    venue.fetch_order = failing_probe  # type: ignore[method-assign]
    with pytest.raises(BrokerError) as probe_error:
        broker.submit(make_request(order_type=OrderType.MARKET), reference_price=1.0)
    assert probe_error.value is own_error

    def failing_my_trades(symbol: Any = None) -> list[dict[str, Any]]:
        raise own_error

    venue.fetch_open_orders = lambda symbol=None: []
    venue.fetch_my_trades = failing_my_trades  # type: ignore[method-assign]
    with pytest.raises(BrokerError) as trades_error:
        broker.poll()
    assert trades_error.value is own_error


def test_ccxt_broker_tolerates_an_unparsable_trade_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    trade = venue_trade()
    trade["timestamp"] = "not-a-timestamp"
    venue.trades = [trade]

    fill = broker.poll()[0].fill
    assert fill is not None
    assert fill.timestamp == pd.Timestamp(START)


def test_ccxt_broker_decodes_an_unknown_order_type_and_a_broken_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, venue = make_ccxt(monkeypatch)
    payload = venue_open_order("ord-1")
    payload["type"] = "iceberg"
    payload["price"] = "not-a-number"
    venue.open_payloads = [payload]

    order = broker.open_orders()[0]

    assert order.type is OrderType.MARKET
    assert order.price is None


def test_the_paper_broker_needs_no_optional_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paper trading must work with the dev extra only (brief D2)."""
    monkeypatch.setitem(sys.modules, "ccxt", None)
    broker = make_paper(fee_rate=0.0)

    ack = broker.submit(make_request(quantity=1.0), reference_price=100.0)

    assert ack.state is OrderState.FILLED
    assert broker.poll()[0].fill is not None


def test_the_broker_module_does_not_import_the_real_ccxt_at_module_scope() -> None:
    source = Path("src/trading_platform/realtime/broker.py").read_text(encoding="utf-8")
    imports = [
        line
        for line in source.splitlines()
        if line.strip().startswith(("import ccxt", "from ccxt"))
    ]
    assert imports, "the adapter must import ccxt inside the method bodies"
    for line in imports:
        assert line.startswith("        "), "ccxt must only be imported inside a function body"
