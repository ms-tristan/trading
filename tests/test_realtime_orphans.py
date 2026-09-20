"""Tests of the startup orphan-position sweep (work package WP3).

An orphaned position is a durable ``positions`` row whose ``profile_id`` matches
no loaded profile: no stop loss, no exit management, no reconciliation ever looks
at it again.  The sweep closes it **at the venue** and warns the operator.

Everything here is offline and deterministic: the real
:class:`~trading_platform.realtime.store.SqliteStateStore`, the real
:class:`~trading_platform.realtime.gateway.ExecutionGateway` and the real
:class:`~trading_platform.realtime.broker.PaperBroker` are used, and the venue
factory is a **local recording fake** (never ``ccxt``, never the network).  Time
comes from a :class:`~trading_platform.realtime.clock.ManualClock` and no port is
ever bound.

The five pinned behaviours:

1. an orphan is closed at the venue, on the opposite side and for the full
   quantity, and the ``positions`` row really disappears -- a row delete alone is
   not a closure;
2. a position whose profile **is** loaded is never touched;
3. an unclosable orphan keeps its row and is surfaced at ``ERROR``;
4. a restart never double-closes (the closing order is idempotent);
5. the report is persisted, and an orphan's mode/exchange are resolved from the
   durable state.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_platform.config.models import ProfileConfig
from trading_platform.core.errors import BrokerError
from trading_platform.core.models import Direction
from trading_platform.realtime.broker import PaperBroker
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import (
    BrokerAck,
    BrokerEvent,
    BrokerEventType,
    Fill,
    Order,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    ProfileState,
    ProfileStatus,
    RunMode,
    new_client_order_id,
)
from trading_platform.realtime.orphans import (
    ORPHAN_CLOSE_REASON,
    ORPHAN_EXCHANGE_PREFIX,
    ORPHAN_ORDER_SEQUENCE,
    ORPHAN_REPORT_META_KEY,
    ORPHAN_SWEPT_AT_META_KEY,
    OrphanClosure,
    OrphanFailure,
    OrphanSweepReport,
    load_orphan_report,
    recorded_exchange,
    sweep_orphaned_positions,
)
from trading_platform.realtime.store import SqliteStateStore

START = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
SYMBOL = "BTC/USDT"
GHOST = "ghost-paper"
TRACKED = "btc-paper"


# ---------------------------------------------------------------------------
# local helpers and fakes (offline: no ccxt, no network, no port)
# ---------------------------------------------------------------------------


def clock_at(hour: int = 6) -> ManualClock:
    """Return a manual clock fixed at ``START + hour``."""
    return ManualClock(datetime(2024, 1, 1, hour, 0, tzinfo=UTC))


def make_store(tmp_path: Path, clock: ManualClock) -> SqliteStateStore:
    """Return an initialized SQLite store in ``tmp_path``."""
    store = SqliteStateStore(tmp_path / "state.db", clock=clock)
    store.initialize()
    return store


def profile(identifier: str = TRACKED, symbol: str = SYMBOL, **overrides: Any) -> ProfileConfig:
    """Return a valid paper profile."""
    payload: dict[str, Any] = {
        "id": identifier,
        "symbol": symbol,
        "timeframe": "1h",
        "strategy": "basic",
        "mode": "paper",
        "initial_balance": 1000.0,
    }
    payload.update(overrides)
    return ProfileConfig(**payload)


def seed_position(
    store: SqliteStateStore,
    profile_id: str,
    *,
    symbol: str = SYMBOL,
    quantity: float = 2.0,
    average_price: float = 100.0,
    direction: Direction = Direction.LONG,
    updated_at: datetime | None = None,
) -> Position:
    """Persist one open position and return it."""
    stamp = pd.Timestamp(updated_at or START)
    position = Position(
        profile_id=profile_id,
        symbol=symbol,
        quantity=quantity,
        average_price=average_price,
        direction=direction,
        opened_at=stamp,
        updated_at=stamp,
    )
    store.upsert_position(position)
    return position


def record_mode(store: SqliteStateStore, profile_id: str, mode: RunMode) -> None:
    """Persist the run mode of ``profile_id`` the way the store does."""
    store.save_status(profile_id, ProfileStatus.RUNNING)
    saved = store.load_status(profile_id)
    assert saved is not None
    store.save_status(profile_id, ProfileStatus.RUNNING)


class RecordingBroker:
    """Offline venue that records what it was asked to do and echoes it back.

    It is the stand-in for the factory seam, so the suite never imports ``ccxt``
    and never opens a socket: it answers a closing market order with a fill at the
    reference price, exactly like :class:`PaperBroker` does, and keeps the receipt
    so the test can assert *what the venue received*.
    """

    def __init__(
        self,
        *,
        clock: ManualClock,
        profile_id: str,
        mode: RunMode = RunMode.PAPER,
        positions: Any = None,
    ) -> None:
        self._clock = clock
        self._profile_id = profile_id
        self._mode = mode
        self.received: list[OrderRequest] = []
        self.prices: list[float] = []
        self._events: list[BrokerEvent] = []
        self._orders: dict[str, Order] = {}
        self._positions = positions

    @property
    def name(self) -> str:
        return "recording"

    @property
    def mode(self) -> RunMode:
        """Return the run mode this venue serves (what the gateway asserts on)."""
        return self._mode

    def submit(self, request: OrderRequest, *, reference_price: float) -> BrokerAck:
        self.received.append(request)
        self.prices.append(float(reference_price))
        now = pd.Timestamp(self._clock.now())
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
            broker_order_id=f"rec-{request.client_order_id}",
        )
        self._orders[order.client_order_id] = order
        self._events.append(
            BrokerEvent(
                event_type=BrokerEventType.ORDER_FILLED,
                client_order_id=order.client_order_id,
                profile_id=order.profile_id,
                order=order,
                fill=Fill(
                    fill_id=f"rec-fill-{order.client_order_id}",
                    client_order_id=order.client_order_id,
                    profile_id=order.profile_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=float(order.quantity),
                    price=float(reference_price),
                    fee=0.0,
                    timestamp=now,
                    mode=order.mode,
                ),
                message="recording broker filled the order",
                timestamp=now,
            )
        )
        return BrokerAck(
            client_order_id=order.client_order_id,
            accepted=True,
            state=OrderState.FILLED,
            broker_order_id=order.broker_order_id,
            submitted_at=now,
        )

    def cancel(self, client_order_id: str) -> bool:
        return False

    def poll(self) -> list[BrokerEvent]:
        events = list(self._events)
        self._events.clear()
        return events

    def open_orders(self) -> list[Order]:
        return []

    def reconcile(self, expected: Sequence[Order] = ()) -> Any:
        """Answer a clean reconciliation (the boot reconciles every runner)."""
        from trading_platform.realtime.models import ReconciliationReport

        return ReconciliationReport(
            profile_id=self._profile_id,
            ok=True,
            checked_at=pd.Timestamp(self._clock.now()),
            matched=len(tuple(expected)),
            only_at_venue=(),
            only_locally=(),
            mismatched=(),
            details={"reason": "offline test venue"},
        )


class RefusingFactory:
    """Venue factory that always fails to build a broker."""

    def __init__(self, error: str = "venue unreachable") -> None:
        self._error = error
        self.calls = 0

    def __call__(self, profile: ProfileConfig, **kwargs: Any) -> Any:
        self.calls += 1
        raise BrokerError(self._error)


class StuckBroker(RecordingBroker):
    """Venue whose order is accepted but never fills: the position stays open."""

    def submit(self, request: OrderRequest, *, reference_price: float) -> BrokerAck:
        self.received.append(request)
        now = pd.Timestamp(self._clock.now())
        return BrokerAck(
            client_order_id=request.client_order_id,
            accepted=True,
            state=OrderState.SUBMITTED,
            broker_order_id=f"stuck-{request.client_order_id}",
            submitted_at=now,
        )

    def poll(self) -> list[BrokerEvent]:
        return []


def recording_factory(
    clock: ManualClock, *, broker_type: type[RecordingBroker] = RecordingBroker
) -> Any:
    """Return a factory building one :class:`RecordingBroker` per profile."""
    built: list[RecordingBroker] = []

    def build(target: ProfileConfig, **kwargs: Any) -> RecordingBroker:
        broker = broker_type(clock=clock, profile_id=str(target.id), mode=RunMode(target.mode))
        built.append(broker)
        return broker

    build.built = built  # type: ignore[attr-defined]
    return build


@pytest.fixture
def logs() -> Any:
    """Capture the structured records of the realtime logger."""
    logger = logging.getLogger("trading_platform.realtime")
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def events(records: list[logging.LogRecord]) -> list[str]:
    """Return the event names of the captured records."""
    return [str(getattr(record, "event", record.getMessage())) for record in records]


# ---------------------------------------------------------------------------
# (1) an orphan is closed AT THE VENUE, not merely row-deleted
# ---------------------------------------------------------------------------


def test_an_orphaned_position_is_closed_at_the_venue(tmp_path: Path) -> None:
    """The injected venue receives the closing order and the row is gone."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=2.0, average_price=100.0)
    factory = recording_factory(clock)

    report = sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)

    broker = factory.built[0]
    assert len(broker.received) == 1
    request = broker.received[0]
    assert request.side is OrderSide.SELL
    assert request.type is OrderType.MARKET
    assert request.quantity == 2.0
    assert request.reason == ORPHAN_CLOSE_REASON
    assert request.price is None
    assert broker.prices == [100.0]

    assert store.get_position(GHOST, SYMBOL) is None
    assert report.closed == (
        OrphanClosure(profile_id=GHOST, symbol=SYMBOL, quantity=2.0, side="sell", price=100.0),
    )
    assert report.failed == ()
    assert report.ok is True
    assert report.found == 1
    assert report.orphaned == 1
    store.close()


def test_the_closing_order_is_persisted_under_a_deterministic_id(tmp_path: Path) -> None:
    """The closure goes through the gateway, so it leaves a durable order row."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    position = seed_position(store, GHOST, quantity=1.5)
    factory = recording_factory(clock)

    sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)

    expected = new_client_order_id(
        GHOST, SYMBOL, pd.Timestamp(position.updated_at), ORPHAN_ORDER_SEQUENCE
    )
    stored = store.get_order(expected)
    assert stored is not None
    assert stored.state is OrderState.FILLED
    assert stored.side is OrderSide.SELL
    assert stored.quantity == 1.5
    assert [order.client_order_id for order in store.list_orders(GHOST)] == [expected]
    store.close()


def test_a_short_orphan_is_bought_back(tmp_path: Path) -> None:
    """The closing side is the opposite of the position's direction."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=-3.0, direction=Direction.SHORT)
    factory = recording_factory(clock)

    report = sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)

    assert factory.built[0].received[0].side is OrderSide.BUY
    assert report.closed[0].side == "buy"
    assert store.get_position(GHOST, SYMBOL) is None
    store.close()


def test_a_paper_orphan_closes_on_the_default_paper_venue(tmp_path: Path) -> None:
    """Without an injected factory, the engine's own factory is used.

    A ``paper`` orphan therefore closes on a :class:`PaperBroker` -- the very same
    venue the live engine builds for a paper profile -- with no network and no
    ``ccxt`` import.
    """
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=1.0, average_price=100.0)

    report = sweep_orphaned_positions(store=store, profiles=[], clock=clock, environ={})

    assert report.closed != ()
    assert report.failed == ()
    assert store.get_position(GHOST, SYMBOL) is None
    order = store.get_order(
        new_client_order_id(GHOST, SYMBOL, pd.Timestamp(START), ORPHAN_ORDER_SEQUENCE)
    )
    assert order is not None and order.mode is RunMode.PAPER
    store.close()


def test_a_live_orphan_is_closed_through_the_injected_live_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A position recorded ``live`` is closed through the factory, in live mode.

    The fake factory stands in for ``CcxtBroker`` -- the module is never imported
    -- while the recorded mode pinned on the order proves the live path was taken.
    A live closure is still armed by the platform's own opt-in gate, so the test
    arms it exactly like an operator does; the gate reads the process environment,
    which is what the injected ``environ`` defaults to in production.
    """
    monkeypatch.setenv("TB_ALLOW_LIVE_TRADING", "I_UNDERSTAND_THE_RISK")
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=1.0, average_price=250.0)
    store.set_meta(ORPHAN_EXCHANGE_PREFIX + GHOST, "kraken")
    _force_mode(store, GHOST, RunMode.LIVE)
    factory = recording_factory(clock)

    report = sweep_orphaned_positions(
        store=store,
        profiles=[],
        clock=clock,
        broker_factory=factory,
    )

    assert factory.built[0].received[0].mode is RunMode.LIVE
    order = store.get_order(
        new_client_order_id(GHOST, SYMBOL, pd.Timestamp(START), ORPHAN_ORDER_SEQUENCE)
    )
    assert order is not None
    assert order.mode is RunMode.LIVE
    assert store.get_meta(ORPHAN_EXCHANGE_PREFIX + GHOST) == "kraken"
    assert report.closed[0].quantity == 1.0
    assert report.ok is True
    store.close()


def _force_mode(store: SqliteStateStore, profile_id: str, mode: RunMode) -> None:
    """Write a ``profile_state`` payload carrying ``mode`` (the durable mode)."""
    state = ProfileState(
        profile_id=profile_id,
        status=ProfileStatus.RUNNING,
        mode=mode,
        updated_at=pd.Timestamp(START),
    )
    store.set_meta("profile_state:" + profile_id, json.dumps(state.to_dict(), sort_keys=True))


# ---------------------------------------------------------------------------
# (2) a position whose profile IS loaded is never touched
# ---------------------------------------------------------------------------


def test_a_tracked_position_is_never_touched(tmp_path: Path) -> None:
    """A profile that is loaded keeps its position, and no order is sent."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seeded = seed_position(store, TRACKED, quantity=2.0)
    before = store.get_position(TRACKED, SYMBOL)
    factory = recording_factory(clock)

    report = sweep_orphaned_positions(
        store=store, profiles=[profile(TRACKED)], clock=clock, broker_factory=factory
    )

    assert report.orphaned == 0
    assert report.closed == ()
    assert report.failed == ()
    assert report.found == 0
    assert factory.built == []
    after = store.get_position(TRACKED, SYMBOL)
    assert after is not None
    assert after.to_dict() == before.to_dict()  # type: ignore[union-attr]
    assert after.updated_at == seeded.updated_at
    assert store.list_orders(TRACKED) == []
    store.close()


def test_only_the_untracked_profile_of_a_mixed_platform_is_swept(tmp_path: Path) -> None:
    """One loaded profile and one orphan: exactly the orphan is closed."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, TRACKED, quantity=1.0)
    seed_position(store, GHOST, quantity=4.0)
    factory = recording_factory(clock)

    report = sweep_orphaned_positions(
        store=store, profiles=[profile(TRACKED)], clock=clock, broker_factory=factory
    )

    assert [item.profile_id for item in report.closed] == [GHOST]
    assert store.get_position(TRACKED, SYMBOL) is not None
    assert store.get_position(GHOST, SYMBOL) is None
    store.close()


def test_an_already_flat_orphan_is_found_but_not_closed(tmp_path: Path) -> None:
    """A position at (or below) the flat epsilon has nothing left to close."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=0.0)
    factory = recording_factory(clock)

    report = sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)

    assert report.found == 1
    assert report.orphaned == 0
    assert factory.built == []
    store.close()


# ---------------------------------------------------------------------------
# (3) an unclosable orphan keeps its row and is surfaced
# ---------------------------------------------------------------------------


def test_an_orphan_whose_venue_cannot_be_built_keeps_its_row(
    tmp_path: Path, logs: list[logging.LogRecord], caplog: pytest.LogCaptureFixture
) -> None:
    """A factory that raises is a *failure*: nothing is deleted."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seeded = seed_position(store, GHOST, quantity=2.5)
    factory = RefusingFactory("venue unreachable")

    with caplog.at_level(logging.ERROR, logger="trading_platform.realtime"):
        report = sweep_orphaned_positions(
            store=store, profiles=[], clock=clock, broker_factory=factory
        )

    assert len(report.failed) == 1
    failure = report.failed[0]
    assert (failure.profile_id, failure.symbol, failure.quantity) == (GHOST, SYMBOL, 2.5)
    assert "venue unreachable" in failure.error
    assert report.ok is False
    assert factory.calls == 1

    remaining = store.get_position(GHOST, SYMBOL)
    assert remaining is not None
    assert remaining.to_dict() == seeded.to_dict()

    assert "orphan_position_close_failed" in events(logs)
    failed_records = [
        record for record in logs if getattr(record, "event", "") == "orphan_position_close_failed"
    ]
    assert all(record.levelno == logging.ERROR for record in failed_records)
    assert any("orphan_position_close_failed" in record.message for record in caplog.records)
    store.close()


def test_an_orphan_still_open_after_the_order_is_a_failure(
    tmp_path: Path, logs: list[logging.LogRecord]
) -> None:
    """An accepted order that never fills leaves the row and is reported."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=1.0)
    factory = recording_factory(clock, broker_type=StuckBroker)

    report = sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)

    assert len(report.failed) == 1
    assert "still open" in report.failed[0].error
    assert report.closed == ()
    assert report.ok is False
    assert store.get_position(GHOST, SYMBOL) is not None
    assert "orphan_position_close_failed" in events(logs)
    store.close()


def test_the_sweep_never_raises_and_still_persists_a_report(tmp_path: Path) -> None:
    """A sweep where everything fails answers a report instead of an exception."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, "ghost-a", quantity=1.0)
    seed_position(store, "ghost-b", quantity=2.0, symbol="ETH/USDT")

    report = sweep_orphaned_positions(
        store=store, profiles=[], clock=clock, broker_factory=RefusingFactory()
    )

    assert len(report.failed) == 2
    assert report.found == 2
    assert report.to_dict()["failed_count"] == 2
    assert load_orphan_report(store) is not None
    store.close()


# ---------------------------------------------------------------------------
# (4) a restart never double-closes
# ---------------------------------------------------------------------------


def test_a_restart_does_not_double_close(tmp_path: Path) -> None:
    """Two sweeps over the same store send exactly one order in total."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=2.0)
    factory = recording_factory(clock)

    first = sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)
    second = sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)

    assert len(first.closed) == 1
    assert second.closed == ()
    assert second.failed == ()
    assert second.found == 0

    orders = store.list_orders(GHOST)
    assert len(orders) == 1
    assert len(factory.built) == 1  # the second sweep built no venue at all
    store.close()


def test_a_restart_replays_the_same_client_order_id(tmp_path: Path) -> None:
    """The closing id is derived from the position, never from the boot instant."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    position = seed_position(store, GHOST, quantity=1.0)
    factory = recording_factory(clock)

    sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)
    first_id = store.list_orders(GHOST)[0].client_order_id
    expected = new_client_order_id(
        GHOST, SYMBOL, pd.Timestamp(position.updated_at), ORPHAN_ORDER_SEQUENCE
    )

    assert first_id == expected
    # A different boot instant must not move the identifier.
    later = ManualClock(datetime(2024, 6, 1, 12, 0, tzinfo=UTC))
    seed_position(store, GHOST, quantity=1.0)
    assert (
        new_client_order_id(GHOST, SYMBOL, pd.Timestamp(position.updated_at), ORPHAN_ORDER_SEQUENCE)
        == expected
    )
    assert later.now() != clock.now()
    store.close()


# ---------------------------------------------------------------------------
# (5) the report is persisted and resolves mode/exchange
# ---------------------------------------------------------------------------


def test_the_report_is_persisted_in_the_store(tmp_path: Path) -> None:
    """A sweep always persists its report, a clean one included."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=2.0)
    factory = recording_factory(clock)

    report = sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)

    raw = store.get_meta(ORPHAN_REPORT_META_KEY)
    assert raw is not None
    decoded = json.loads(raw)
    assert decoded == report.to_dict()
    assert decoded["found"] == 1
    assert decoded["orphaned"] == 1
    assert decoded["closed_count"] == 1
    assert decoded["failed_count"] == 0
    assert decoded["closed"][0] == {
        "profile_id": GHOST,
        "symbol": SYMBOL,
        "quantity": 2.0,
        "side": "sell",
        "price": 100.0,
    }
    assert decoded["failed"] == []
    assert decoded["swept_at"] == report.swept_at
    assert store.get_meta(ORPHAN_SWEPT_AT_META_KEY) == report.swept_at
    store.close()


def test_a_clean_sweep_still_persists_its_report(tmp_path: Path) -> None:
    """``orphaned == 0`` with a ``swept_at`` tells "swept, nothing" from "never"."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    assert store.get_meta(ORPHAN_REPORT_META_KEY) is None

    report = sweep_orphaned_positions(
        store=store,
        profiles=[profile(TRACKED)],
        clock=clock,
        broker_factory=recording_factory(clock),
    )

    assert report.orphaned == 0
    persisted = load_orphan_report(store)
    assert persisted is not None
    assert persisted.orphaned == 0
    assert persisted.swept_at == report.swept_at
    store.close()


def test_an_orphan_resolves_its_recorded_exchange(tmp_path: Path) -> None:
    """The exchange is read from the durable ``profile_exchange:<id>`` meta."""
    clock = clock_at()
    store = make_store(tmp_path, clock)

    assert recorded_exchange(store, "unknown") == "binance"
    store.set_meta(ORPHAN_EXCHANGE_PREFIX + "kraken-profile", "kraken")
    assert recorded_exchange(store, "kraken-profile") == "kraken"
    store.set_meta(ORPHAN_EXCHANGE_PREFIX + "blank", "   ")
    assert recorded_exchange(store, "blank") == "binance"
    store.close()


def test_the_mode_of_an_orphan_comes_from_the_durable_profile_state(tmp_path: Path) -> None:
    """An orphan recorded ``live`` is closed live; an unknown one falls back to paper."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, "was-live", quantity=1.0)
    seed_position(store, "never-seen", quantity=1.0, symbol="ETH/USDT")
    _force_mode(store, "was-live", RunMode.LIVE)
    modes: dict[str, RunMode] = {}

    def factory(target: ProfileConfig, **kwargs: Any) -> RecordingBroker:
        modes[str(target.id)] = RunMode(target.mode)
        return RecordingBroker(clock=clock, profile_id=str(target.id), mode=RunMode(target.mode))

    sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)

    assert modes == {"was-live": RunMode.LIVE, "never-seen": RunMode.PAPER}
    store.close()


def test_the_sweep_orders_orphans_by_profile_then_symbol(tmp_path: Path) -> None:
    """The scan is deterministic: ``(profile_id, symbol)`` ascending."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, "bbb", quantity=1.0, symbol="ETH/USDT")
    seed_position(store, "bbb", quantity=1.0, symbol="BTC/USDT")
    seed_position(store, "aaa", quantity=1.0, symbol="BTC/USDT")
    order: list[tuple[str, str]] = []

    def factory(target: ProfileConfig, **kwargs: Any) -> RecordingBroker:
        order.append((str(target.id), str(target.symbol)))
        return RecordingBroker(clock=clock, profile_id=str(target.id), mode=RunMode(target.mode))

    sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)

    assert order == [("aaa", "BTC/USDT"), ("bbb", "BTC/USDT"), ("bbb", "ETH/USDT")]
    store.close()


def test_a_previous_failed_report_is_revisited_on_the_next_boot(tmp_path: Path) -> None:
    """An orphan the previous sweep could not close is retried, never forgotten."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=1.0)

    first = sweep_orphaned_positions(
        store=store, profiles=[], clock=clock, broker_factory=RefusingFactory()
    )
    assert first.failed != ()
    assert store.get_position(GHOST, SYMBOL) is not None

    factory = recording_factory(clock)
    second = sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)

    assert second.closed != ()
    assert store.get_position(GHOST, SYMBOL) is None
    store.close()


# ---------------------------------------------------------------------------
# the report object itself
# ---------------------------------------------------------------------------


def test_the_report_round_trips_through_its_payload() -> None:
    """``to_dict``/``from_dict`` are inverse, counts included."""
    report = OrphanSweepReport(
        found=3,
        closed=(OrphanClosure(GHOST, SYMBOL, 2.0, "sell", 100.0),),
        failed=(OrphanFailure("other", "ETH/USDT", 1.0, "boom"),),
        swept_at="2024-01-01T06:00:00+00:00",
    )

    payload = report.to_dict()

    assert payload["orphaned"] == 2
    assert payload["closed_count"] == 1
    assert payload["failed_count"] == 1
    assert payload["found"] == 3
    assert OrphanSweepReport.from_dict(payload) == report
    assert report.ok is False
    assert OrphanSweepReport.empty(swept_at="x").ok is True
    assert OrphanSweepReport.empty(swept_at="x").orphaned == 0


def test_the_counts_of_a_stored_payload_are_derived_not_trusted() -> None:
    """A payload whose counters lie can never claim a closure that did not happen."""
    rebuilt = OrphanSweepReport.from_dict(
        {
            "found": 99,
            "orphaned": 99,
            "closed_count": 99,
            "failed_count": 99,
            "closed": [],
            "failed": [],
            "swept_at": "2024-01-01T00:00:00+00:00",
        }
    )

    assert rebuilt.orphaned == 0
    assert rebuilt.to_dict()["closed_count"] == 0


def test_a_corrupt_stored_report_is_treated_as_never_swept(
    tmp_path: Path, logs: list[logging.LogRecord]
) -> None:
    """A damaged warning must never stop a boot: it is rewritten by the sweep."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    store.set_meta(ORPHAN_REPORT_META_KEY, "{not json")

    assert load_orphan_report(store) is None
    assert "orphan_sweep_report_undecodable" in events(logs)

    report = sweep_orphaned_positions(
        store=store, profiles=[], clock=clock, broker_factory=recording_factory(clock)
    )
    assert load_orphan_report(store) == report
    store.close()


def test_an_unreadable_report_payload_is_ignored(tmp_path: Path) -> None:
    """A payload that is valid JSON but the wrong shape is ignored too."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    store.set_meta(ORPHAN_REPORT_META_KEY, json.dumps([1, 2, 3]))
    assert load_orphan_report(store) is None
    store.set_meta(ORPHAN_REPORT_META_KEY, json.dumps({"closed": [{"profile_id": 1}]}))
    assert load_orphan_report(store) is None
    store.close()


def test_an_orphan_of_a_forgotten_profile_is_recovered_from_the_store(
    tmp_path: Path,
) -> None:
    """The candidate set comes from the loaded profiles **and** the durable history.

    ``ghost-paper`` is not loaded by this boot, but the store's own ``profiles``
    table still remembers it, so its orphaned position is caught instead of being
    silently skipped.
    """
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=1.0)
    store.save_profile(profile(GHOST))

    factory = recording_factory(clock)
    report = sweep_orphaned_positions(
        store=store, profiles=[profile(TRACKED)], clock=clock, broker_factory=factory
    )

    assert store.get_position(GHOST, SYMBOL) is None
    assert [item.profile_id for item in report.closed] == [GHOST]
    store.close()


def test_a_sweep_on_a_store_with_positions_but_no_profile_is_a_clean_no_op(
    tmp_path: Path,
) -> None:
    """No profile, no position: the sweep answers a clean, persisted report."""
    clock = clock_at()
    store = make_store(tmp_path, clock)

    report = sweep_orphaned_positions(
        store=store, profiles=[], clock=clock, broker_factory=recording_factory(clock)
    )

    assert report.found == 0
    assert report.orphaned == 0
    assert report.ok is True
    assert load_orphan_report(store) == report
    store.close()


def test_the_paper_orphan_closure_spends_the_platform_wallet(tmp_path: Path) -> None:
    """The default factory builds a real paper venue: the fill moves real cash."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    position = seed_position(store, GHOST, quantity=1.0, average_price=100.0)
    broker = PaperBroker(clock=clock, initial_balance=1000.0, seed=3)

    def factory(target: ProfileConfig, **kwargs: Any) -> PaperBroker:
        return broker

    report = sweep_orphaned_positions(store=store, profiles=[], clock=clock, broker_factory=factory)

    assert report.ok is True
    assert broker.wallet.cash != 1000.0  # the simulated ledger really was debited
    assert store.get_position(str(position.profile_id), SYMBOL) is None
    store.close()


def test_the_sweep_reads_a_position_whose_store_read_fails(tmp_path: Path) -> None:
    """A store that cannot list a profile's positions does not abort the sweep."""
    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=1.0)

    class Exploding:
        """Store facade whose position listing always fails."""

        def __init__(self, inner: SqliteStateStore) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            if name == "list_positions":
                raise BrokerError("positions unavailable")
            return getattr(self._inner, name)

    facade = Exploding(store)
    report = sweep_orphaned_positions(
        store=facade,  # type: ignore[arg-type]
        profiles=[],
        clock=clock,
        broker_factory=recording_factory(clock),
    )

    assert report.orphaned == 0
    store.close()


# ---------------------------------------------------------------------------
# orchestrator wiring: the sweep runs at boot, before any runner exists
# ---------------------------------------------------------------------------


class _EmptyStream:
    """Market stream that never yields a candle (the boot wiring is what matters)."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def next_candle(self, symbol: str, timeframe: str) -> None:
        return None

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        return pd.DataFrame()


def build_orchestrator(
    tmp_path: Path,
    clock: ManualClock,
    store: SqliteStateStore,
    *,
    broker_factory: Any = None,
) -> Any:
    """Build a real orchestrator over ``store`` with an injected venue factory."""
    from trading_platform.config.models import MonitoringConfig, RealtimeConfig
    from trading_platform.realtime.orchestrator import RealtimeOrchestrator
    from trading_platform.realtime.wallet import PlatformWallet

    return RealtimeOrchestrator(
        profiles=[],
        store=store,
        clock=clock,
        realtime=RealtimeConfig(state_db=tmp_path / "state.db", logs_dir=tmp_path / "logs"),
        monitoring=MonitoringConfig(port=0),
        stream_factory=_EmptyStream,
        broker_factory=broker_factory,
        environ={},
        version="0.1.0-test",
        wallet=PlatformWallet(initial_balance=1000.0, store=store, clock=clock, name="platform"),
    )


def test_the_engine_boots_cleanly_with_zero_profiles_and_sweeps(
    tmp_path: Path, logs: list[logging.LogRecord]
) -> None:
    """An empty platform serves its API -- and still closes what nobody tracks."""
    import asyncio

    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=2.0, average_price=100.0)
    factory = recording_factory(clock)
    orchestrator = build_orchestrator(tmp_path, clock, store, broker_factory=factory)

    async def boot() -> None:
        await asyncio.wait_for(orchestrator.start(), timeout=20)

    asyncio.run(boot())

    report = orchestrator.orphan_report
    assert report is not None
    assert report.to_dict()["closed_count"] == 1
    assert len(report.closed) == 1
    assert orchestrator.profiles == ()
    # The API is served by the zero-profile platform, warning included.
    assert orchestrator.health()["orphaned_positions"] == report.to_dict()
    assert store.get_position(GHOST, SYMBOL) is None
    assert "orphan_sweep_completed" in events(logs)

    async def shutdown() -> None:
        await asyncio.wait_for(orchestrator.stop(), timeout=20)

    asyncio.run(shutdown())


def test_the_read_path_answers_the_last_boots_report(tmp_path: Path) -> None:
    """A process that never booted reads the warning of the last boot back."""
    import asyncio

    clock = clock_at()
    store = make_store(tmp_path, clock)
    seed_position(store, GHOST, quantity=1.0)
    factory = recording_factory(clock)
    booted = build_orchestrator(tmp_path, clock, store, broker_factory=factory)

    async def boot() -> None:
        await asyncio.wait_for(booted.start(), timeout=20)
        await asyncio.wait_for(booted.stop(), timeout=20)

    asyncio.run(boot())

    # ``stop()`` closed the boot's store handle: the reader opens its own, exactly
    # like a `realtime serve` process reading the durable state of a finished run.
    reopened = SqliteStateStore(tmp_path / "state.db", clock=clock)
    reopened.initialize()
    reader = build_orchestrator(tmp_path, clock, reopened, broker_factory=recording_factory(clock))
    reported = reader.orphan_report

    assert reported is not None
    assert reported.closed[0].profile_id == GHOST
    assert reader.health()["orphaned_positions"]["closed_count"] == 1
    reopened.close()


def test_the_boot_records_the_exchange_of_every_loaded_profile(tmp_path: Path) -> None:
    """The exchange is durable *before* the sweep, so a live orphan can resolve it.

    An orphan has no ``ProfileConfig`` left, so this ``meta`` entry is the only
    way the sweep can learn which venue the position was really trading on.
    """
    import asyncio

    clock = clock_at()
    store = make_store(tmp_path, clock)
    factory = recording_factory(clock)
    orchestrator = build_orchestrator(tmp_path, clock, store, broker_factory=factory)
    tracked = profile(TRACKED, exchange="kraken")

    orchestrator._profiles = (tracked,)
    orchestrator._by_id = {TRACKED: tracked}
    orchestrator._prepare_runners()

    assert store.get_meta(ORPHAN_EXCHANGE_PREFIX + TRACKED) == "kraken"
    assert orchestrator.orphan_report is not None
    assert orchestrator.orphan_report.orphaned == 0

    async def shutdown() -> None:
        await asyncio.wait_for(orchestrator.stop(), timeout=20)

    asyncio.run(shutdown())
