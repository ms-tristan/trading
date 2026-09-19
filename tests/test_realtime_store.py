"""Tests of the persistent state store (work-package wp3).

Everything here is offline and deterministic: each test owns a ``tmp_path``
database, injects a :class:`~trading_platform.realtime.clock.ManualClock` and never
touches the network.  Timestamps are explicit UTC instants, so two runs of the suite
produce equivalent databases.

The group numbering follows the work-package brief:

1. nominal round-trip of every ``StateStore`` member;
2. idempotency of the appending writes;
3. restart safety (write, close, reopen);
4. schema version: newer, older, missing, corrupt;
5. single-writer lock;
6. thread safety;
7. error paths (uninitialized store, unwritable directory, empty limits, failing write);
8. candle watermark;
9. ``profile_state`` on an unknown profile;
10. the bounded candle history;
11. the shared platform wallet (schema version 3): persistence, single row, migration.

Corruption is injected through a *second*, direct SQLite connection: the store's
``flock`` protects writers that go through the store, it is not a file-system lock.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_platform.config.models import ProfileConfig
from trading_platform.core.errors import StateStoreError
from trading_platform.core.models import Direction, ExitReason, TradeRecord
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import (
    CandleEvent,
    EquityPoint,
    Fill,
    Order,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    ProfileState,
    ProfileStatus,
    RunMode,
)
from trading_platform.realtime.store import (
    CANDLE_WINDOW,
    SCHEMA_VERSION,
    CandleRow,
    SqliteStateStore,
    StateStore,
    WalletRow,
)

#: A fixed anchor: every test starts the virtual clock here.
START = pd.Timestamp("2024-01-01T00:00:00Z")


def stamp(minutes: int = 0) -> pd.Timestamp:
    """Return ``START`` shifted by ``minutes`` minutes."""
    return START + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> ManualClock:
    """Deterministic clock injected into every store of this module."""
    return ManualClock(start=START.to_pydatetime())


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path of the database owned by one test."""
    return tmp_path / "realtime" / "state.db"


@pytest.fixture
def store(db_path: Path, clock: ManualClock) -> Iterator[SqliteStateStore]:
    """An initialized store, always closed at the end of the test."""
    instance = SqliteStateStore(db_path, clock=clock)
    instance.initialize()
    try:
        yield instance
    finally:
        instance.close()


@contextmanager
def raw_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a second connection used to corrupt or inspect the database directly."""
    connection = sqlite3.connect(db_path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def make_profile(profile_id: str = "btc-paper", **overrides: object) -> ProfileConfig:
    """Build a valid profile specification."""
    payload: dict[str, object] = {"id": profile_id, "symbol": "BTC/USDT", "timeframe": "1h"}
    payload.update(overrides)
    return ProfileConfig.model_validate(payload)


def make_order(
    client_order_id: str = "btc-paper-BTC_USDT-20240101T000000Z-0000",
    *,
    profile_id: str = "btc-paper",
    symbol: str = "BTC/USDT",
    state: OrderState = OrderState.SUBMITTED,
    created_at: pd.Timestamp | None = None,
) -> Order:
    """Build an order with every optional field filled in."""
    return Order(
        client_order_id=client_order_id,
        profile_id=profile_id,
        symbol=symbol,
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        quantity=0.5,
        state=state,
        mode=RunMode.PAPER,
        created_at=stamp(0) if created_at is None else created_at,
        updated_at=stamp(1),
        filled_quantity=0.25,
        price=41_000.5,
        average_fill_price=41_001.25,
        broker_order_id="venue-42",
    )


def make_fill(fill_id: str = "fill-1", *, client_order_id: str = "order-1") -> Fill:
    """Build a fill."""
    return Fill(
        fill_id=fill_id,
        client_order_id=client_order_id,
        profile_id="btc-paper",
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        quantity=0.5,
        price=41_000.0,
        fee=8.2,
        timestamp=stamp(2),
        mode=RunMode.PAPER,
    )


def make_position(symbol: str = "BTC/USDT", *, quantity: float = 0.5) -> Position:
    """Build an open position."""
    return Position(
        profile_id="btc-paper",
        symbol=symbol,
        quantity=quantity,
        average_price=41_000.0,
        direction=Direction.LONG,
        opened_at=stamp(2),
        updated_at=stamp(3),
        realized_pnl=12.5,
        unrealized_pnl=-3.25,
        stop_price=39_000.0,
    )


def make_equity(minutes: int = 4, *, equity: float = 10_500.0) -> EquityPoint:
    """Build one equity point."""
    return EquityPoint(
        profile_id="btc-paper",
        timestamp=stamp(minutes),
        equity=equity,
        cash=9_500.0,
        position_value=1_000.0,
    )


def make_trade(*, exit_minutes: int = 60, pnl: float = 120.0) -> TradeRecord:
    """Build a closed round-trip."""
    return TradeRecord(
        entry_time=stamp(0),
        exit_time=stamp(exit_minutes),
        entry_price=41_000.0,
        exit_price=41_240.0,
        size=0.5,
        direction=Direction.LONG,
        pnl=pnl,
        pnl_pct=0.0058,
        fees=16.4,
        exit_reason=ExitReason.TAKE_PROFIT,
        duration_minutes=float(exit_minutes),
        stop_price=40_000.0,
        take_profit_price=42_000.0,
        params_id="default",
    )


def make_candle(
    minutes: int = 0,
    *,
    close: float = 41_100.0,
    closed: bool = True,
    symbol: str = "BTC/USDT",
) -> CandleEvent:
    """Build one candle as the live engine emits it."""
    return CandleEvent(
        symbol=symbol,
        timeframe="1h",
        timestamp=stamp(minutes),
        open=close - 50.0,
        high=close + 25.0,
        low=close - 75.0,
        close=close,
        volume=12.5,
        closed=closed,
    )


def make_candle_row(candle: CandleEvent, *, profile_id: str = "btc-paper") -> CandleRow:
    """Return the :class:`CandleRow` a stored ``candle`` must decode back to."""
    return CandleRow(
        profile_id=profile_id,
        timestamp=candle.timestamp,
        open=float(candle.open),
        high=float(candle.high),
        low=float(candle.low),
        close=float(candle.close),
        volume=float(candle.volume),
        closed=bool(candle.closed),
    )


# ---------------------------------------------------------------------------
# 1. nominal round-trip of every StateStore member
# ---------------------------------------------------------------------------


def test_store_satisfies_the_protocol(store: SqliteStateStore) -> None:
    assert isinstance(store, StateStore)
    assert store.is_initialized() is True
    assert store.path.name == "state.db"
    assert "SqliteStateStore" in repr(store)


def test_the_protocol_exposes_exactly_the_contracted_members() -> None:
    """Pin the seam the other packages consume: no member added, none forgotten."""
    expected = {
        "append_candle",
        "append_equity",
        "append_fill",
        "append_trade",
        "candle_series",
        "close",
        "delete_position",
        "equity_curve",
        "get_meta",
        "get_order",
        "get_position",
        "initialize",
        "last_acted_entry_crossing",
        "last_processed_candle",
        "list_orders",
        "list_positions",
        "list_trades",
        "load_profiles",
        "load_status",
        "load_wallet",
        "mark_acted_entry_crossing",
        "mark_candle_processed",
        "profile_state",
        "save_profile",
        "save_status",
        "save_wallet",
        "set_meta",
        "upsert_order",
        "upsert_position",
    }
    assert {name for name in vars(StateStore) if not name.startswith("_")} == expected

    for name in expected:
        assert callable(getattr(SqliteStateStore, name)), name


def test_profile_round_trip(store: SqliteStateStore) -> None:
    spec = make_profile("btc-paper", mode="live", strategy="ema_cross", warmup_candles=321)
    store.save_profile(spec)
    store.save_profile(make_profile("eth-paper", symbol="ETH/USDT"))

    loaded = store.load_profiles()
    assert [item.id for item in loaded] == ["btc-paper", "eth-paper"]
    assert loaded[0] == spec


def test_profile_upsert_updates_in_place(store: SqliteStateStore) -> None:
    store.save_profile(make_profile("btc-paper", symbol="BTC/USDT"))
    store.save_profile(make_profile("btc-paper", symbol="ETH/USDT"))

    loaded = store.load_profiles()
    assert len(loaded) == 1
    assert loaded[0].symbol == "ETH/USDT"


def test_order_round_trip(store: SqliteStateStore) -> None:
    order = make_order()
    store.upsert_order(order)

    assert store.get_order(order.client_order_id) == order
    assert store.get_order("unknown") is None
    assert store.list_orders("btc-paper") == [order]
    assert store.list_orders("other-profile") == []


def test_order_upsert_updates_in_place(store: SqliteStateStore) -> None:
    store.upsert_order(make_order(state=OrderState.SUBMITTED))
    store.upsert_order(make_order(state=OrderState.FILLED))

    orders = store.list_orders("btc-paper")
    assert len(orders) == 1
    assert orders[0].state is OrderState.FILLED


def test_order_listing_is_most_recent_first(store: SqliteStateStore) -> None:
    store.upsert_order(make_order("order-old", created_at=stamp(0)))
    store.upsert_order(make_order("order-new", created_at=stamp(10)))
    store.upsert_order(make_order("order-mid", created_at=stamp(5)))

    assert [order.client_order_id for order in store.list_orders("btc-paper")] == [
        "order-new",
        "order-mid",
        "order-old",
    ]
    assert [order.client_order_id for order in store.list_orders("btc-paper", limit=2)] == [
        "order-new",
        "order-mid",
    ]


def test_fill_round_trip(store: SqliteStateStore, db_path: Path) -> None:
    fill = make_fill()
    assert store.append_fill(fill) is True
    assert store.append_fill(make_fill("fill-2", client_order_id="order-2")) is True
    assert store.append_fill(fill) is False

    with raw_connection(db_path) as conn:
        rows = list(
            conn.execute("SELECT fill_id, profile_id, timestamp FROM fills ORDER BY fill_id")
        )
    assert [row[0] for row in rows] == ["fill-1", "fill-2"]
    assert rows[0][1] == "btc-paper"
    assert rows[0][2] == "2024-01-01T00:02:00+00:00"


def test_position_round_trip(store: SqliteStateStore) -> None:
    position = make_position()
    store.upsert_position(position)

    assert store.get_position("btc-paper", "BTC/USDT") == position
    assert store.get_position("btc-paper", "ETH/USDT") is None
    assert store.list_positions("btc-paper") == [position]


def test_position_listing_is_ordered_by_symbol(store: SqliteStateStore) -> None:
    store.upsert_position(make_position("ETH/USDT"))
    store.upsert_position(make_position("ADA/USDT"))
    store.upsert_position(make_position("BTC/USDT"))

    assert [item.symbol for item in store.list_positions("btc-paper")] == [
        "ADA/USDT",
        "BTC/USDT",
        "ETH/USDT",
    ]


def test_position_upsert_and_delete(store: SqliteStateStore) -> None:
    store.upsert_position(make_position(quantity=0.5))
    store.upsert_position(make_position(quantity=1.5))

    positions = store.list_positions("btc-paper")
    assert len(positions) == 1
    assert positions[0].quantity == 1.5

    store.delete_position("btc-paper", "BTC/USDT")
    store.delete_position("btc-paper", "BTC/USDT")
    assert store.list_positions("btc-paper") == []
    assert store.get_position("btc-paper", "BTC/USDT") is None


def test_equity_round_trip_is_ordered_ascending(store: SqliteStateStore) -> None:
    store.append_equity(make_equity(10, equity=10_100.0))
    store.append_equity(make_equity(4, equity=10_050.0))

    curve = store.equity_curve("btc-paper")
    assert [point.timestamp for point in curve] == [stamp(4), stamp(10)]
    assert [point.equity for point in curve] == [10_050.0, 10_100.0]
    assert curve[0] == make_equity(4, equity=10_050.0)


def test_equity_curve_of_an_unknown_profile_is_empty(store: SqliteStateStore) -> None:
    assert store.equity_curve("never-seen") == []


def test_trade_round_trip(store: SqliteStateStore) -> None:
    trade = make_trade()
    assert store.append_trade(trade, profile_id="btc-paper") is True
    assert (
        store.append_trade(make_trade(exit_minutes=120, pnl=-5.0), profile_id="eth-paper") is True
    )
    assert store.append_trade(trade, profile_id="other") is True

    trades = store.list_trades("btc-paper")
    assert trades == [trade]
    assert trade.to_dict()["pnl"] == 120.0
    assert [item.exit_time for item in store.list_trades("other")] == [stamp(60)]
    assert store.list_trades("never-seen") == []


def test_trade_listing_is_oldest_exit_first(store: SqliteStateStore) -> None:
    store.append_trade(make_trade(exit_minutes=120), profile_id="btc-paper")
    store.append_trade(make_trade(exit_minutes=60), profile_id="btc-paper")

    assert [item.exit_time for item in store.list_trades("btc-paper")] == [stamp(60), stamp(120)]
    assert len(store.list_trades("btc-paper", limit=1)) == 1


def test_status_round_trip(store: SqliteStateStore, clock: ManualClock) -> None:
    store.save_status("btc-paper", ProfileStatus.STARTING)
    state = store.load_status("btc-paper")
    assert state is not None
    assert state.status is ProfileStatus.STARTING
    assert state.mode is RunMode.PAPER
    assert state.updated_at == START

    clock.advance(30)
    store.mark_candle_processed("btc-paper", stamp(1))
    store.save_status("btc-paper", ProfileStatus.RUNNING, "engine warm")

    state = store.load_status("btc-paper")
    assert state is not None
    assert state.status is ProfileStatus.RUNNING
    assert state.last_candle_at == stamp(1)
    # the detail describes the *status*; it is not an error (see save_status)
    assert state.last_error is None
    assert state.updated_at == START + timedelta(seconds=30)
    assert state == store.profile_state("btc-paper")
    assert store.load_status("unknown-profile") is None


def test_status_keeps_the_mode_of_the_saved_profile(db_path: Path, clock: ManualClock) -> None:
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        store.save_profile(make_profile("btc-live", mode="live"))
        store.save_status("btc-live", ProfileStatus.RUNNING)
        assert store.profile_state("btc-live").mode is RunMode.LIVE
    finally:
        store.close()


def test_status_preserves_lag_and_reconnect_count(store: SqliteStateStore) -> None:
    """A status write keeps the rest of the health block, and clears a stale error."""
    degraded = ProfileState(
        profile_id="btc-paper",
        status=ProfileStatus.DEGRADED,
        mode=RunMode.PAPER,
        last_candle_at=stamp(3),
        lag_seconds=42.5,
        last_error="venue timeout",
        reconnect_count=7,
        updated_at=stamp(9),
    )
    store.set_meta("profile_state:btc-paper", json.dumps(degraded.to_dict(), sort_keys=True))

    store.save_status("btc-paper", ProfileStatus.RUNNING, "recovered")

    state = store.profile_state("btc-paper")
    assert state.status is ProfileStatus.RUNNING
    assert state.lag_seconds == 42.5
    assert state.reconnect_count == 7
    # the profile is running again: the previous failure is history, not a status
    assert state.last_error is None


def test_the_last_error_is_only_ever_an_error(store: SqliteStateStore, db_path: Path) -> None:
    """``ERROR`` sets it, a healthy status clears it, the detail never becomes it.

    Regression test for the deployed dashboard: the detail of every status write
    used to be stored as ``last_error``, so a healthy profile reported
    ``last error: last candle 2024-01-05T23:00:00+00:00`` -- and after a restart it
    reported the reason its *previous* process had stopped, for as long as the new
    process had not failed.
    """
    store.save_status("btc-paper", ProfileStatus.ERROR, "BrokerError: venue refused")
    assert store.profile_state("btc-paper").last_error == "BrokerError: venue refused"

    store.save_status("btc-paper", ProfileStatus.STARTING, "starting")
    assert store.profile_state("btc-paper").last_error is None

    store.save_status("btc-paper", ProfileStatus.ERROR, "MarketStreamError: gone")
    # a stop keeps the reason, it does not invent one from its own detail
    store.save_status("btc-paper", ProfileStatus.STOPPED, "stopped after: MarketStreamError: gone")
    assert store.profile_state("btc-paper").last_error == "MarketStreamError: gone"

    # an error status without a message clears rather than storing an empty string
    store.save_status("btc-paper", ProfileStatus.ERROR, "")
    assert store.profile_state("btc-paper").last_error is None

    # and the human-readable detail is still persisted on the status row
    with raw_connection(db_path) as raw:
        row = raw.execute(
            "SELECT detail FROM status WHERE profile_id = ?", ("btc-paper",)
        ).fetchone()
    assert row is not None
    assert row[0] == ""


def test_meta_round_trip(store: SqliteStateStore) -> None:
    assert store.get_meta("kill_switch") is None
    store.set_meta("kill_switch", "false")
    store.set_meta("kill_switch", "true")
    assert store.get_meta("kill_switch") == "true"


# ---------------------------------------------------------------------------
# 2. idempotency of the appending writes (mandatory)
# ---------------------------------------------------------------------------


def test_append_fill_is_idempotent(store: SqliteStateStore, db_path: Path) -> None:
    fill = make_fill()
    assert store.append_fill(fill) is True
    assert store.append_fill(fill) is False

    with raw_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM fills").fetchone()
    assert count is not None
    assert count[0] == 1


def test_append_equity_is_idempotent(store: SqliteStateStore) -> None:
    point = make_equity()
    assert store.append_equity(point) is True
    assert store.append_equity(point) is False
    assert store.append_equity(make_equity(equity=1.0)) is False  # same (profile, timestamp)

    assert store.equity_curve("btc-paper") == [point]


def test_append_trade_is_idempotent(store: SqliteStateStore) -> None:
    trade = make_trade()
    assert store.append_trade(trade, profile_id="btc-paper") is True
    assert store.append_trade(trade, profile_id="btc-paper") is False

    assert store.list_trades("btc-paper") == [trade]


def test_append_trade_key_depends_on_the_profile(store: SqliteStateStore) -> None:
    trade = make_trade()
    assert store.append_trade(trade, profile_id="btc-paper") is True
    assert store.append_trade(trade, profile_id="eth-paper") is True
    assert len(store.list_trades("btc-paper")) == 1
    assert len(store.list_trades("eth-paper")) == 1


# ---------------------------------------------------------------------------
# 3. restart safety
# ---------------------------------------------------------------------------


def test_state_survives_a_close_and_reopen(db_path: Path, clock: ManualClock) -> None:
    spec = make_profile("btc-paper")
    order = make_order()
    trade = make_trade()
    point = make_equity()

    first = SqliteStateStore(db_path, clock=clock)
    first.initialize()
    first.save_profile(spec)
    first.upsert_order(order)
    first.upsert_position(make_position())
    first.append_fill(make_fill())
    first.append_equity(point)
    first.append_trade(trade, profile_id="btc-paper")
    first.mark_candle_processed("btc-paper", stamp(5))
    first.save_status("btc-paper", ProfileStatus.RUNNING, "warm")
    first.close()
    assert first.is_initialized() is False

    second = SqliteStateStore(db_path, clock=clock)
    second.initialize()
    try:
        assert second.is_initialized() is True
        assert second.load_profiles() == [spec]
        assert second.get_order(order.client_order_id) == order
        assert second.list_positions("btc-paper") == [make_position()]
        assert second.equity_curve("btc-paper") == [point]
        assert second.list_trades("btc-paper") == [trade]
        assert second.last_processed_candle("btc-paper") == stamp(5)
        assert second.append_fill(make_fill()) is False
        assert second.append_equity(point) is False
        assert second.append_trade(trade, profile_id="btc-paper") is False

        state = second.profile_state("btc-paper")
        assert state.status is ProfileStatus.RUNNING
        assert state.last_candle_at == stamp(5)
    finally:
        second.close()


def test_initialize_and_close_are_idempotent(db_path: Path, clock: ManualClock) -> None:
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    store.initialize()
    store.close()
    store.close()
    assert store.is_initialized() is False


def test_schema_version_row_is_written_once(db_path: Path, clock: ManualClock) -> None:
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    store.initialize()
    store.close()

    with raw_connection(db_path) as conn:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_version")]
    assert versions == [SCHEMA_VERSION]


def test_the_database_is_opened_in_wal_mode(db_path: Path, clock: ManualClock) -> None:
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    store.close()

    with raw_connection(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()
    assert mode is not None
    assert str(mode[0]).lower() == "wal"


# ---------------------------------------------------------------------------
# 4. schema version: newer, older, missing, corrupt
# ---------------------------------------------------------------------------


def test_newer_schema_version_is_refused(db_path: Path, clock: ManualClock) -> None:
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    store.close()

    with raw_connection(db_path) as conn:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 1,))

    store = SqliteStateStore(db_path, clock=clock)
    with pytest.raises(StateStoreError) as excinfo:
        store.initialize()
    assert str(excinfo.value) == (
        f"state database {db_path} uses schema version {SCHEMA_VERSION + 1}, "
        f"newer than the supported {SCHEMA_VERSION}"
    )
    assert store.is_initialized() is False


def test_older_schema_version_is_migrated_quietly(db_path: Path, clock: ManualClock) -> None:
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    store.close()

    with raw_connection(db_path) as conn:
        conn.execute("UPDATE schema_version SET version = 0")

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        assert store.get_meta("anything") is None
    finally:
        store.close()

    with raw_connection(db_path) as conn:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_version")]
    assert versions == [SCHEMA_VERSION]


def test_missing_schema_version_row_is_recreated(db_path: Path, clock: ManualClock) -> None:
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    store.close()

    with raw_connection(db_path) as conn:
        conn.execute("DELETE FROM schema_version")

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        store.set_meta("key", "value")
        assert store.get_meta("key") == "value"
    finally:
        store.close()


def test_a_file_that_is_not_sqlite_is_refused(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"not a sqlite database at all" * 40)

    store = SqliteStateStore(db_path)
    with pytest.raises(StateStoreError):
        store.initialize()
    assert store.is_initialized() is False


def test_corrupted_order_payload_is_reported_as_a_store_error(
    store: SqliteStateStore, db_path: Path
) -> None:
    order = make_order()
    store.upsert_order(order)
    with raw_connection(db_path) as conn:
        conn.execute(
            "UPDATE orders SET payload = ? WHERE client_order_id = ?",
            ("{oops", order.client_order_id),
        )

    with pytest.raises(StateStoreError, match="corrupted payload"):
        store.list_orders("btc-paper")


def test_corrupted_trade_payload_is_reported_as_a_store_error(
    store: SqliteStateStore, db_path: Path
) -> None:
    store.append_trade(make_trade(), profile_id="btc-paper")
    with raw_connection(db_path) as conn:
        conn.execute("UPDATE trades SET payload = ?", ("{}",))

    with pytest.raises(StateStoreError, match="corrupted payload"):
        store.list_trades("btc-paper")


def test_corrupted_profile_payload_is_reported_as_a_store_error(
    store: SqliteStateStore, db_path: Path
) -> None:
    store.save_profile(make_profile())
    with raw_connection(db_path) as conn:
        conn.execute("UPDATE profiles SET payload = ?", ('{"id": "bad id!"}',))

    with pytest.raises(StateStoreError, match="corrupted payload"):
        store.load_profiles()


def test_corrupted_status_is_reported_as_a_store_error(
    store: SqliteStateStore, db_path: Path
) -> None:
    store.save_status("btc-paper", ProfileStatus.RUNNING)
    with raw_connection(db_path) as conn:
        conn.execute("UPDATE status SET status = ?", ("banana",))

    with pytest.raises(StateStoreError, match="invalid status"):
        store.load_status("btc-paper")


def test_corrupted_candle_watermark_is_reported_as_a_store_error(store: SqliteStateStore) -> None:
    store.set_meta("last_candle:btc-paper", "not-a-timestamp")

    with pytest.raises(StateStoreError, match="invalid timestamp"):
        store.last_processed_candle("btc-paper")


# ---------------------------------------------------------------------------
# 5. single-writer lock (mandatory)
# ---------------------------------------------------------------------------


def test_two_stores_cannot_initialize_the_same_file(db_path: Path, clock: ManualClock) -> None:
    first = SqliteStateStore(db_path, clock=clock)
    first.initialize()
    second = SqliteStateStore(db_path, clock=clock)
    try:
        with pytest.raises(StateStoreError) as excinfo:
            second.initialize()
        assert str(excinfo.value) == f"state store {db_path} is already locked by another process"
        assert second.is_initialized() is False
        assert first.is_initialized() is True
        assert first.lock_path == Path(f"{db_path}.lock")
    finally:
        first.close()

    # the rejected store is not poisoned: it takes the lock once it is free
    second.initialize()
    try:
        assert second.is_initialized() is True
    finally:
        second.close()


def test_the_lock_is_released_on_close(db_path: Path, clock: ManualClock) -> None:
    first = SqliteStateStore(db_path, clock=clock)
    first.initialize()
    first.close()

    second = SqliteStateStore(db_path, clock=clock)
    second.initialize()
    try:
        assert second.is_initialized() is True
    finally:
        second.close()


def test_close_deletes_nothing(db_path: Path, clock: ManualClock) -> None:
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    store.set_meta("kept", "yes")
    store.close()

    assert db_path.exists() is True
    assert store.lock_path.exists() is True

    again = SqliteStateStore(db_path, clock=clock)
    again.initialize()
    try:
        assert again.get_meta("kept") == "yes"
    finally:
        again.close()


# ---------------------------------------------------------------------------
# 6. threading: one connection per caller thread
# ---------------------------------------------------------------------------


def test_two_threads_can_write_through_one_store(store: SqliteStateStore) -> None:
    errors: list[BaseException] = []

    def writer(prefix: str) -> None:
        try:
            for index in range(50):
                store.upsert_order(make_order(f"{prefix}-{index}", created_at=stamp(index)))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(prefix,)) for prefix in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert thread.is_alive() is False

    assert errors == []
    assert len(store.list_orders("btc-paper", limit=1000)) == 100


def test_connections_of_finished_threads_are_reaped(store: SqliteStateStore) -> None:
    """The retained connection set is bounded by the *live* threads, not by history.

    Regression test for the file-descriptor exhaustion that took the deployed
    dashboard down: ``http.server.ThreadingHTTPServer`` runs one thread per request,
    the store hands one connection per caller thread, and every connection was
    retained for the life of the process.  A browser polling every two seconds
    therefore leaked descriptors until ``OSError: Too many open files`` made every
    request answer ``500``.

    The threads run *concurrently* on purpose: threads alive at the same time
    necessarily have distinct thread ids, which makes the assertion independent of
    the id-reuse behaviour of the host (a sequential loop can be masked by the OS
    handing the same id to the next thread).
    """
    readers = 30

    def reader(index: int) -> None:
        store.load_status(f"profile-{index}")

    threads = [threading.Thread(target=reader, args=(index,)) for index in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert thread.is_alive() is False

    # one request from the main thread is what a polling server does constantly:
    # it is the moment the finished threads' connections are reaped
    store.load_status("after")

    assert store.connection_count == 1, (
        f"{store.connection_count} connections retained for {readers} finished threads"
    )


def test_a_live_thread_keeps_its_own_connection(store: SqliteStateStore) -> None:
    """Reaping never closes a connection a live thread is using."""
    opened = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def long_lived() -> None:
        try:
            store.load_status("long-lived")
            opened.set()
            release.wait(timeout=30)
            # still usable after other threads died and were reaped
            store.load_status("long-lived-again")
        except BaseException as exc:  # pragma: no cover - only on a reaping bug
            errors.append(exc)

    thread = threading.Thread(target=long_lived)
    thread.start()
    assert opened.wait(timeout=30) is True
    for index in range(5):  # short-lived threads, all reaped
        short = threading.Thread(target=reader_probe, args=(store, index))
        short.start()
        short.join(timeout=30)
    release.set()
    thread.join(timeout=30)

    assert errors == []
    # exactly the two live users of the store: this test's thread and the
    # long-lived one -- never one per finished thread
    assert store.connection_count <= 2


def reader_probe(store: SqliteStateStore, index: int) -> None:
    """One read from a short-lived thread (used to trigger reaping)."""
    store.load_status(f"short-{index}")


# ---------------------------------------------------------------------------
# 7. error paths
# ---------------------------------------------------------------------------


def test_every_method_requires_initialize(db_path: Path) -> None:
    store = SqliteStateStore(db_path)
    calls: list[tuple[str, Callable[[], object]]] = [
        ("save_profile", lambda: store.save_profile(make_profile())),
        ("load_profiles", store.load_profiles),
        ("save_wallet", lambda: store.save_wallet(cash=1.0, initial_balance=1.0)),
        ("load_wallet", store.load_wallet),
        ("upsert_order", lambda: store.upsert_order(make_order())),
        ("get_order", lambda: store.get_order("x")),
        ("list_orders", lambda: store.list_orders("btc-paper")),
        ("append_fill", lambda: store.append_fill(make_fill())),
        ("upsert_position", lambda: store.upsert_position(make_position())),
        ("delete_position", lambda: store.delete_position("btc-paper", "BTC/USDT")),
        ("get_position", lambda: store.get_position("btc-paper", "BTC/USDT")),
        ("list_positions", lambda: store.list_positions("btc-paper")),
        ("append_equity", lambda: store.append_equity(make_equity())),
        ("equity_curve", lambda: store.equity_curve("btc-paper")),
        ("append_trade", lambda: store.append_trade(make_trade(), profile_id="btc-paper")),
        ("list_trades", lambda: store.list_trades("btc-paper")),
        ("save_status", lambda: store.save_status("btc-paper", ProfileStatus.RUNNING)),
        ("load_status", lambda: store.load_status("btc-paper")),
        ("get_meta", lambda: store.get_meta("key")),
        ("set_meta", lambda: store.set_meta("key", "value")),
        ("last_processed_candle", lambda: store.last_processed_candle("btc-paper")),
        ("mark_candle_processed", lambda: store.mark_candle_processed("btc-paper", stamp(1))),
        ("append_candle", lambda: store.append_candle(make_candle(), profile_id="btc-paper")),
        ("candle_series", lambda: store.candle_series("btc-paper")),
        ("profile_state", lambda: store.profile_state("btc-paper")),
    ]

    for name, call in calls:
        with pytest.raises(StateStoreError) as excinfo:
            call()
        assert "is not initialized" in str(excinfo.value), name
    assert store.is_initialized() is False


def test_an_unwritable_parent_directory_is_a_store_error(tmp_path: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root: a read-only directory is not enforced")
    directory = tmp_path / "readonly"
    directory.mkdir()
    directory.chmod(0o500)
    if os.access(directory, os.W_OK):
        directory.chmod(0o700)
        pytest.skip("the file-system does not enforce directory permissions")

    store = SqliteStateStore(directory / "state.db")
    try:
        with pytest.raises(StateStoreError):
            store.initialize()
        assert store.is_initialized() is False
    finally:
        directory.chmod(0o700)


def test_a_zero_limit_returns_an_empty_list(store: SqliteStateStore) -> None:
    store.upsert_order(make_order())
    store.append_trade(make_trade(), profile_id="btc-paper")

    assert store.list_orders("btc-paper", limit=0) == []
    assert store.list_orders("btc-paper", limit=-3) == []
    assert store.list_trades("btc-paper", limit=0) == []
    assert store.list_trades("btc-paper", limit=-1) == []


def test_a_failing_write_rolls_back_and_keeps_the_store_usable(
    store: SqliteStateStore, db_path: Path
) -> None:
    store.upsert_order(make_order("kept"))
    store.append_equity(make_equity(1, equity=10_000.0))

    # ``NaN`` is stored as SQL NULL by SQLite, which violates ``equity REAL NOT NULL``.
    with pytest.raises(StateStoreError, match=r"state store write failed \(append_equity\)"):
        store.append_equity(make_equity(2, equity=float("nan")))

    with raw_connection(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM equity").fetchone()
    assert rows is not None
    assert rows[0] == 1
    assert [order.client_order_id for order in store.list_orders("btc-paper")] == ["kept"]
    assert [point.timestamp for point in store.equity_curve("btc-paper")] == [stamp(1)]


# ---------------------------------------------------------------------------
# 8. candle watermark
# ---------------------------------------------------------------------------


def test_last_processed_candle_starts_empty(store: SqliteStateStore) -> None:
    assert store.last_processed_candle("btc-paper") is None


def test_mark_candle_processed_keeps_the_maximum(store: SqliteStateStore) -> None:
    store.mark_candle_processed("btc-paper", stamp(5))
    store.mark_candle_processed("btc-paper", stamp(3))
    assert store.last_processed_candle("btc-paper") == stamp(5)

    store.mark_candle_processed("btc-paper", stamp(6))
    assert store.last_processed_candle("btc-paper") == stamp(6)


def test_mark_candle_processed_is_per_profile(store: SqliteStateStore) -> None:
    store.mark_candle_processed("btc-paper", stamp(5))
    assert store.last_processed_candle("eth-paper") is None


def test_the_watermark_survives_a_restart(db_path: Path, clock: ManualClock) -> None:
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    store.mark_candle_processed("btc-paper", stamp(7))
    store.save_status("btc-paper", ProfileStatus.RUNNING)
    store.close()

    reopened = SqliteStateStore(db_path, clock=clock)
    reopened.initialize()
    try:
        assert reopened.last_processed_candle("btc-paper") == stamp(7)
        assert reopened.profile_state("btc-paper").last_candle_at == stamp(7)
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# 9. profile_state on an unknown profile
# ---------------------------------------------------------------------------


def test_profile_state_of_an_unknown_profile_is_a_stopped_default(
    store: SqliteStateStore,
) -> None:
    state = store.profile_state("never-seen")

    assert state == ProfileState(
        profile_id="never-seen",
        status=ProfileStatus.STOPPED,
        mode=RunMode.PAPER,
        updated_at=None,
    )
    assert state.last_candle_at is None
    assert state.last_error is None
    assert state.reconnect_count == 0


def test_profile_state_falls_back_when_only_a_status_row_exists(
    store: SqliteStateStore, db_path: Path
) -> None:
    with raw_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO status (profile_id, status, detail, last_candle_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("eth-paper", "degraded", "reconciliation mismatch", None, "2024-01-01T00:00:00+00:00"),
        )

    state = store.profile_state("eth-paper")
    assert state.status is ProfileStatus.DEGRADED
    assert state.mode is RunMode.PAPER
    assert state.last_candle_at is None
    assert state.updated_at == START
    assert state.last_error is None


def test_profile_state_of_a_deleted_profile_never_raises(store: SqliteStateStore) -> None:
    store.save_status("btc-paper", ProfileStatus.RUNNING)
    store.delete_position("btc-paper", "BTC/USDT")

    assert store.profile_state("btc-paper").status is ProfileStatus.RUNNING
    assert store.profile_state("").status is ProfileStatus.STOPPED


# ---------------------------------------------------------------------------
# 10. candles: the bounded history the live engine appends
# ---------------------------------------------------------------------------

#: Every domain table of the schema (the ``candles`` table is deliberately absent:
#: the v1 fixture below must reproduce the schema build 1 actually shipped).
_DOMAIN_TABLES = (
    "profiles",
    "orders",
    "fills",
    "positions",
    "equity",
    "trades",
    "status",
    "meta",
)

#: The complete DDL of schema version 1, verbatim (no ``candles`` table).
_V1_DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    (
        "CREATE TABLE IF NOT EXISTS profiles ("
        "profile_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS orders ("
        "client_order_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, symbol TEXT NOT NULL, "
        "state TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    ),
    "CREATE INDEX IF NOT EXISTS idx_orders_profile ON orders(profile_id, created_at)",
    (
        "CREATE TABLE IF NOT EXISTS fills ("
        "fill_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL, profile_id TEXT NOT NULL, "
        "payload TEXT NOT NULL, timestamp TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS positions ("
        "profile_id TEXT NOT NULL, symbol TEXT NOT NULL, payload TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, PRIMARY KEY(profile_id, symbol))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS equity ("
        "profile_id TEXT NOT NULL, timestamp TEXT NOT NULL, equity REAL NOT NULL, "
        "payload TEXT NOT NULL, PRIMARY KEY(profile_id, timestamp))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS trades ("
        "profile_id TEXT NOT NULL, trade_id TEXT NOT NULL, payload TEXT NOT NULL, "
        "exit_time TEXT NOT NULL, PRIMARY KEY(profile_id, trade_id))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS status ("
        "profile_id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL, "
        "last_candle_at TEXT, updated_at TEXT NOT NULL)"
    ),
)


def write_version_1_database(db_path: Path, *, version: int = 1) -> None:
    """Create a deployed version-1 database with one real row per table.

    The rows are written by hand, exactly as build 1 wrote them, so the migration
    test can compare them byte for byte afterwards.  The two profiles are the two
    the deployed database holds (``btc-paper`` at 10 000 and ``eth-paper`` at
    5 000), so the fixture reproduces the shape the migration has to carry.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    profile = make_profile("btc-paper", mode="live")
    second_profile = make_profile("eth-paper", symbol="ETH/USDT", initial_balance=5_000.0)
    order = make_order()
    fill = make_fill()
    position = make_position()
    point = make_equity()
    trade = make_trade()
    state = ProfileState(
        profile_id="btc-paper",
        status=ProfileStatus.RUNNING,
        mode=RunMode.LIVE,
        last_candle_at=stamp(5),
        lag_seconds=1.5,
        last_error=None,
        reconnect_count=2,
        updated_at=stamp(6),
    )
    connection = sqlite3.connect(db_path)
    try:
        for statement in _V1_DDL:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        connection.execute(
            "INSERT INTO profiles (profile_id, payload, updated_at) VALUES (?, ?, ?)",
            (
                "btc-paper",
                json.dumps(profile.model_dump(mode="json"), sort_keys=True),
                "2024-01-01",
            ),
        )
        connection.execute(
            "INSERT INTO profiles (profile_id, payload, updated_at) VALUES (?, ?, ?)",
            (
                "eth-paper",
                json.dumps(second_profile.model_dump(mode="json"), sort_keys=True),
                "2024-01-02",
            ),
        )
        connection.execute(
            "INSERT INTO orders (client_order_id, profile_id, symbol, state, payload, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                order.client_order_id,
                "btc-paper",
                "BTC/USDT",
                order.state.value,
                json.dumps(order.to_dict(), sort_keys=True),
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:01:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO fills (fill_id, client_order_id, profile_id, payload, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                fill.fill_id,
                fill.client_order_id,
                "btc-paper",
                json.dumps(fill.to_dict(), sort_keys=True),
                "2024-01-01T00:02:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO positions (profile_id, symbol, payload, updated_at) VALUES (?, ?, ?, ?)",
            (
                "btc-paper",
                "BTC/USDT",
                json.dumps(position.to_dict(), sort_keys=True),
                "2024-01-01T00:03:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO equity (profile_id, timestamp, equity, payload) VALUES (?, ?, ?, ?)",
            (
                "btc-paper",
                "2024-01-01T00:04:00+00:00",
                float(point.equity),
                json.dumps(point.to_dict(), sort_keys=True),
            ),
        )
        connection.execute(
            "INSERT INTO trades (profile_id, trade_id, payload, exit_time) VALUES (?, ?, ?, ?)",
            (
                "btc-paper",
                "btc-paper-trade-1",
                json.dumps(trade.to_dict(), sort_keys=True),
                "2024-01-01T01:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO status (profile_id, status, detail, last_candle_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "btc-paper",
                "running",
                "last candle 2024-01-01T00:05:00+00:00",
                "2024-01-01T00:05:00+00:00",
                "2024-01-01T00:06:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("last_candle:btc-paper", "2024-01-01T00:05:00+00:00"),
        )
        connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("profile_state:btc-paper", json.dumps(state.to_dict(), sort_keys=True)),
        )
        connection.commit()
    finally:
        connection.close()


def table_snapshot(db_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Return every row of every domain table, as plain tuples."""
    with raw_connection(db_path) as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in _DOMAIN_TABLES
        }


def _table_names(db_path: Path) -> list[str]:
    """Return the name of every table of the database, sorted."""
    with raw_connection(db_path) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]


def _row_counts(db_path: Path) -> dict[str, int]:
    """Return the number of rows of every table of the database."""
    with raw_connection(db_path) as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _table_names(db_path)
            if not table.startswith("sqlite_")
        }


def test_append_candle_stores_the_processed_candle(store: SqliteStateStore) -> None:
    candle = make_candle(7, close=41_234.5)
    forming = make_candle(8, close=41_300.0, closed=False)

    assert store.append_candle(candle, profile_id="btc-paper") is True
    assert store.append_candle(forming, profile_id="btc-paper") is True

    series = store.candle_series("btc-paper")
    assert series == [make_candle_row(candle), make_candle_row(forming)]
    stored = series[0]
    assert isinstance(stored, CandleRow)
    assert stored.profile_id == "btc-paper"
    assert stored.timestamp == stamp(7)
    assert stored.open == pytest.approx(float(candle.open))
    assert stored.high == pytest.approx(float(candle.high))
    assert stored.low == pytest.approx(float(candle.low))
    assert stored.close == pytest.approx(41_234.5)
    assert stored.volume == pytest.approx(12.5)
    assert stored.closed is True
    # the ``closed`` flag of a candle still forming is stored verbatim, never forced
    assert series[1].closed is False
    assert stored.to_dict() == {
        "profile_id": "btc-paper",
        "timestamp": stamp(7).isoformat(),
        "open": float(candle.open),
        "high": float(candle.high),
        "low": float(candle.low),
        "close": 41_234.5,
        "volume": 12.5,
        "closed": True,
    }


def test_candles_of_another_profile_are_invisible(store: SqliteStateStore) -> None:
    store.append_candle(make_candle(1), profile_id="btc-paper")
    store.append_candle(make_candle(1, symbol="ETH/USDT"), profile_id="eth-paper")

    assert [row.profile_id for row in store.candle_series("btc-paper")] == ["btc-paper"]
    assert [row.profile_id for row in store.candle_series("eth-paper")] == ["eth-paper"]
    assert store.candle_series("never-seen") == []


def test_candle_series_is_oldest_first(store: SqliteStateStore) -> None:
    for minutes in (2, 0, 1):
        store.append_candle(make_candle(minutes), profile_id="btc-paper")

    series = store.candle_series("btc-paper")
    assert [row.timestamp for row in series] == [stamp(0), stamp(1), stamp(2)]


def test_append_candle_is_idempotent_and_refreshes_in_place(
    store: SqliteStateStore, db_path: Path
) -> None:
    assert store.append_candle(make_candle(3, close=100.0), profile_id="btc-paper") is True
    assert store.append_candle(make_candle(3, close=101.0), profile_id="btc-paper") is False
    # the same instant of another profile is a different row, and a brand new one
    assert store.append_candle(make_candle(3, symbol="ETH/USDT"), profile_id="eth-paper") is True

    series = store.candle_series("btc-paper")
    assert len(series) == 1
    assert series[0].close == pytest.approx(101.0)
    assert store.candle_series("eth-paper")[0].close == pytest.approx(
        float(make_candle(3, symbol="ETH/USDT").close)
    )

    with raw_connection(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM candles").fetchone()
    assert rows is not None
    assert rows[0] == 2


def test_the_candle_window_keeps_only_the_most_recent_candles(store: SqliteStateStore) -> None:
    total = CANDLE_WINDOW + 25
    for minutes in range(total):
        store.append_candle(make_candle(minutes), profile_id="btc-paper")

    series = store.candle_series("btc-paper", limit=CANDLE_WINDOW + 1)
    assert len(series) == CANDLE_WINDOW
    assert series[0].timestamp == stamp(25)
    assert series[-1].timestamp == stamp(total - 1)
    assert series == store.candle_series("btc-paper")


def test_candle_series_limit_boundaries(store: SqliteStateStore) -> None:
    for minutes in range(3):
        store.append_candle(make_candle(minutes), profile_id="btc-paper")

    assert store.candle_series("btc-paper", limit=0) == []
    assert store.candle_series("btc-paper", limit=-1) == []
    assert len(store.candle_series("btc-paper", limit=CANDLE_WINDOW + 100)) == 3
    assert [row.timestamp for row in store.candle_series("btc-paper", limit=2)] == [
        stamp(1),
        stamp(2),
    ]
    assert store.candle_series("never-seen") == []


def test_a_version_1_database_is_migrated_to_version_3_without_touching_a_row(
    db_path: Path, clock: ManualClock
) -> None:
    """A deployed version-1 database is carried to version 3, losing nothing.

    Version 3 adds the ``wallet`` table. The migration is purely additive, so every
    row the deployed build wrote must survive it byte for byte -- that is the
    property this test pins, and it is the reason the assertion compares whole
    snapshots rather than a count.
    """
    write_version_1_database(db_path)
    before = table_snapshot(db_path)
    assert all(rows for rows in before.values()), "the fixture must hold one row per table"
    assert _row_counts(db_path) == {
        "schema_version": 1,
        "profiles": 2,
        "orders": 1,
        "fills": 1,
        "positions": 1,
        "equity": 1,
        "trades": 1,
        "status": 1,
        "meta": 2,
    }

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        # (a) every pre-existing row is still there, byte for byte
        assert table_snapshot(db_path) == before
        # (b) the stored version is now the one of this build
        with raw_connection(db_path) as conn:
            versions = [row[0] for row in conn.execute("SELECT version FROM schema_version")]
        assert versions == [SCHEMA_VERSION] == [3]
        # (c) the wallet table exists, is empty, and records no wallet at all: the
        #     engine must initialise the configured value instead of a silent 0.0
        assert "wallet" in _table_names(db_path)
        assert _row_counts(db_path)["wallet"] == 0
        assert store.load_wallet() is None
        # (d) both deployed profiles survive the migration with their balances: a
        #     profile whose configuration carries no explicit allocation resolves to
        #     its initial balance, which is the backward-compatibility rule
        profiles = store.load_profiles()
        assert [item.id for item in profiles] == ["btc-paper", "eth-paper"]
        allocated = {
            item.id: (
                item.initial_balance
                if getattr(item, "allocation", None) is None
                else float(item.allocation)
            )
            for item in profiles
        }
        assert allocated == {"btc-paper": 10_000.0, "eth-paper": 5_000.0}
        # (e) the migrated file accepts a candle *and* a wallet, and the old data
        #     still decodes with the same meaning as before
        candle = make_candle(9)
        assert store.append_candle(candle, profile_id="btc-paper") is True
        assert store.candle_series("btc-paper") == [make_candle_row(candle)]
        store.save_wallet(cash=7_500.0, initial_balance=10_000.0)
        assert store.load_wallet() == WalletRow(
            cash=7_500.0, initial_balance=10_000.0, updated_at=START
        )
        assert store.get_order("btc-paper-BTC_USDT-20240101T000000Z-0000") is not None
        assert store.profile_state("btc-paper").status is ProfileStatus.RUNNING
        assert store.last_processed_candle("btc-paper") == stamp(5)
    finally:
        store.close()

    # (f) the eight tables the deployed build shipped are untouched by the migration
    #     itself: only ``candles``/``wallet`` gained rows, and they did so after it
    after = table_snapshot(db_path)
    assert after == before


def test_a_version_2_database_gains_the_wallet_table(db_path: Path, clock: ManualClock) -> None:
    """Version 2 had the candles table but no wallet: reopening it only adds the wallet.

    The DDL is applied to a database of *any* older version, so the same additive
    migration that carries version 1 to version 3 has to carry version 2 as well.
    """
    first = SqliteStateStore(db_path, clock=clock)
    first.initialize()
    first.save_profile(make_profile("btc-paper"))
    first.append_candle(make_candle(1), profile_id="btc-paper")
    first.close()

    # rebuild the version-2 shape: candles yes, wallet no
    with raw_connection(db_path) as conn:
        conn.execute("DROP TABLE wallet")
        conn.execute("UPDATE schema_version SET version = 2")
    assert "wallet" not in _table_names(db_path)
    before = table_snapshot(db_path)

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        assert table_snapshot(db_path) == before
        with raw_connection(db_path) as conn:
            versions = [row[0] for row in conn.execute("SELECT version FROM schema_version")]
        assert versions == [SCHEMA_VERSION] == [3]
        assert _row_counts(db_path)["wallet"] == 0
        assert store.load_wallet() is None
        assert [item.id for item in store.load_profiles()] == ["btc-paper"]
        assert [row.timestamp for row in store.candle_series("btc-paper")] == [stamp(1)]
        store.save_wallet(cash=1_234.5, initial_balance=10_000.0)
        row = store.load_wallet()
        assert row is not None
        assert row.cash == pytest.approx(1_234.5)
    finally:
        store.close()


def test_a_database_newer_than_this_build_is_still_refused(
    db_path: Path, clock: ManualClock
) -> None:
    write_version_1_database(db_path, version=SCHEMA_VERSION + 1)
    before = table_snapshot(db_path)

    store = SqliteStateStore(db_path, clock=clock)
    with pytest.raises(StateStoreError) as excinfo:
        store.initialize()
    assert str(excinfo.value) == (
        f"state database {db_path} uses schema version {SCHEMA_VERSION + 1}, "
        f"newer than the supported {SCHEMA_VERSION}"
    )
    assert store.is_initialized() is False
    # a refused database is never touched: neither its rows nor its version
    assert table_snapshot(db_path) == before
    with raw_connection(db_path) as conn:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_version")]
    assert versions == [SCHEMA_VERSION + 1]


def test_a_broken_candles_table_is_reported_as_a_store_error(
    store: SqliteStateStore, db_path: Path
) -> None:
    store.append_candle(make_candle(1), profile_id="btc-paper")
    with raw_connection(db_path) as conn:
        conn.execute("DROP TABLE candles")

    with pytest.raises(StateStoreError, match=r"state store read failed \(candle_series\)"):
        store.candle_series("btc-paper")
    with pytest.raises(StateStoreError, match=r"state store write failed \(append_candle\)"):
        store.append_candle(make_candle(2), profile_id="btc-paper")


# ---------------------------------------------------------------------------
# 11. the shared platform wallet (schema version 3)
# ---------------------------------------------------------------------------


def test_the_wallet_table_is_created_by_the_current_schema(
    store: SqliteStateStore, db_path: Path
) -> None:
    """A brand new database ships the wallet table, empty."""
    assert "wallet" in _table_names(db_path)
    assert _row_counts(db_path)["wallet"] == 0
    assert SCHEMA_VERSION == 3


def test_load_wallet_of_an_empty_table_is_none(store: SqliteStateStore) -> None:
    """``None`` -- never a silent ``0.0`` -- is what makes the engine initialise config."""
    assert store.load_wallet() is None


def test_wallet_round_trip(store: SqliteStateStore) -> None:
    store.save_wallet(cash=9_750.25, initial_balance=10_000.0)

    row = store.load_wallet()
    assert row is not None
    assert isinstance(row, WalletRow)
    assert row.cash == pytest.approx(9_750.25)
    assert row.initial_balance == pytest.approx(10_000.0)
    assert row.updated_at == START
    assert row.to_dict() == {
        "cash": 9_750.25,
        "initial_balance": 10_000.0,
        "updated_at": START.isoformat(),
    }


def test_save_wallet_upserts_the_single_row(store: SqliteStateStore, db_path: Path) -> None:
    """Saving twice updates the one row in place: the wallet can never be duplicated."""
    store.save_wallet(cash=10_000.0, initial_balance=10_000.0)
    store.save_wallet(cash=8_500.0, initial_balance=12_000.0)

    assert _row_counts(db_path)["wallet"] == 1
    row = store.load_wallet()
    assert row is not None
    assert row.cash == pytest.approx(8_500.0)
    assert row.initial_balance == pytest.approx(12_000.0)
    # the primary key is pinned to 1 by a CHECK constraint, not only by convention
    with raw_connection(db_path) as conn:
        wallet_ids = [row[0] for row in conn.execute("SELECT wallet_id FROM wallet")]
    assert wallet_ids == [1]


def test_a_wallet_id_other_than_one_is_refused_by_the_schema(
    store: SqliteStateStore, db_path: Path
) -> None:
    with raw_connection(db_path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO wallet (wallet_id, cash, initial_balance, updated_at) "
            "VALUES (2, 1.0, 1.0, '2024-01-01T00:00:00+00:00')"
        )


def test_a_zero_cash_wallet_round_trips(store: SqliteStateStore) -> None:
    """An empty wallet is a value, not an absence: ``0.0`` is stored and read back."""
    store.save_wallet(cash=0.0, initial_balance=10_000.0)

    row = store.load_wallet()
    assert row is not None
    assert row.cash == 0.0
    assert row.cash is not None
    assert row.initial_balance == pytest.approx(10_000.0)


def test_a_non_finite_wallet_cash_is_refused(store: SqliteStateStore, db_path: Path) -> None:
    """``NaN`` is refused, so nothing is ever funded from a meaningless wallet.

    SQLite stores ``NaN`` as ``NULL``, which violates ``cash REAL NOT NULL`` exactly
    like the other numeric writers of this store (see ``append_equity``).  The write
    is refused as a :class:`StateStoreError` and the previous wallet is left intact.
    """
    store.save_wallet(cash=4_000.0, initial_balance=10_000.0)

    with pytest.raises(StateStoreError, match=r"state store write failed \(save_wallet\)"):
        store.save_wallet(cash=float("nan"), initial_balance=10_000.0)

    assert _row_counts(db_path)["wallet"] == 1
    row = store.load_wallet()
    assert row is not None
    assert row.cash == pytest.approx(4_000.0)


def test_a_malformed_wallet_timestamp_is_a_store_error(
    store: SqliteStateStore, db_path: Path
) -> None:
    store.save_wallet(cash=1_000.0, initial_balance=10_000.0)
    with raw_connection(db_path) as conn:
        conn.execute("UPDATE wallet SET updated_at = ?", ("not-a-timestamp",))

    with pytest.raises(StateStoreError, match=r"state store read failed \(load_wallet\)"):
        store.load_wallet()


def test_a_missing_wallet_table_is_reported_as_a_store_error(
    store: SqliteStateStore, db_path: Path
) -> None:
    with raw_connection(db_path) as conn:
        conn.execute("DROP TABLE wallet")

    with pytest.raises(StateStoreError, match=r"state store read failed \(load_wallet\)"):
        store.load_wallet()
    with pytest.raises(StateStoreError, match=r"state store write failed \(save_wallet\)"):
        store.save_wallet(cash=1.0, initial_balance=1.0)


def test_the_wallet_survives_a_close_and_reopen(db_path: Path, clock: ManualClock) -> None:
    """A restart must never reset the shared wallet to the configured value."""
    first = SqliteStateStore(db_path, clock=clock)
    first.initialize()
    first.save_wallet(cash=3_210.5, initial_balance=15_000.0)
    first.close()

    second = SqliteStateStore(db_path, clock=clock)
    second.initialize()
    try:
        assert second.load_wallet() == WalletRow(
            cash=3_210.5, initial_balance=15_000.0, updated_at=START
        )
    finally:
        second.close()


def test_the_wallet_row_carries_the_clock_instant(db_path: Path) -> None:
    clock = ManualClock(start=START.to_pydatetime())
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        store.save_wallet(cash=1.0, initial_balance=2.0)
        clock.advance(30 * 60)
        store.save_wallet(cash=1.5, initial_balance=2.0)

        row = store.load_wallet()
        assert row is not None
        assert row.updated_at == stamp(30)
        assert row.to_dict()["updated_at"] == stamp(30).isoformat()
    finally:
        store.close()


def test_two_threads_can_write_the_wallet_through_one_store(
    store: SqliteStateStore, db_path: Path
) -> None:
    """The store serialises the concurrent cash writes of the profile threads."""
    errors: list[BaseException] = []
    written = {float(index * 100 + step) for index in range(4) for step in range(20)}

    def writer(index: int) -> None:
        try:
            for step in range(20):
                store.save_wallet(cash=float(index * 100 + step), initial_balance=10_000.0)
        except BaseException as exc:  # pragma: no cover - only on a regression
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # no exception, one single row, and a value one of the writers actually wrote:
    # a torn or interleaved update would leave something outside this set
    assert errors == []
    assert _row_counts(db_path)["wallet"] == 1
    row = store.load_wallet()
    assert row is not None
    assert row.cash in written
