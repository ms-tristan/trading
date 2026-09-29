"""Tests of the SQLite state store: schema, profiles, snapshots, trades, days, events, settings.

Every test runs against a temporary database file, so the suite never touches
``data/realtime/state.db``. The store is the only writer of the platform, and
these tests pin the exact column semantics the rest of the platform relies on:
``priority DESC, id ASC`` ordering, the declarative-only upsert, the
``(profile_id, ts)`` snapshot replacement, the in-place flip of a trade keyed by
``(profile_id, trade_id)``, the newest-first day series and the string
comparison of the event timeline.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from trading_platform.models import ProfileConfig, ProfileSnapshot, format_ts, utc_now
from trading_platform.profiles.store import (
    EVENT_LEVELS,
    MIGRATABLE_SCHEMA_VERSIONS,
    PROFILE_DAILY_COLUMNS,
    PROFILE_TRADE_COLUMNS,
    PROFILE_UPDATE_FIELDS,
    SCHEMA_VERSION,
    TABLES,
    ProfileDailyRecord,
    ProfileTradeRecord,
    StateStore,
)

PROFILE_DOCUMENT = {
    "id": "alpha-btc-1h",
    "name": "Alpha BTC 1h",
    "strategy": "basic",
    "timeframe": "1h",
    "mode": "paper",
    "exchange": "binance",
    "pairs": ["BTC/USDT"],
    "initial_capital": 1000.0,
    "max_open_trades": 2,
    "priority": 100,
    "enabled": True,
}


@pytest.fixture
def db_path(tmp_state_dir: Path) -> Path:
    """The state database of a temporary ``data/realtime`` directory."""
    return tmp_state_dir / "state.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[StateStore]:
    """An open, bootstrapped store."""
    with StateStore(db_path) as opened:
        opened.bootstrap()
        yield opened


def make_profile(profile_id: str = "alpha-btc-1h", **overrides: object) -> ProfileConfig:
    """Build the profile declared by ``PROFILE_DOCUMENT`` with ``overrides`` applied."""
    payload: dict[str, object] = {**PROFILE_DOCUMENT, "id": profile_id}
    payload.update(overrides)
    return ProfileConfig.model_validate(payload)


def snapshot_payload(
    profile_id: str = "alpha-btc-1h",
    ts: str | None = None,
    **overrides: object,
) -> dict[str, object]:
    """Return a snapshot document with ``overrides`` applied."""
    payload: dict[str, object] = {
        "profile_id": profile_id,
        "ts": format_ts() if ts is None else ts,
    }
    payload.update(overrides)
    return payload


def snapshot_at(store: StateStore, profile_id: str, minutes_ago: int, **overrides: object) -> str:
    """Record a snapshot ``minutes_ago`` minutes in the past and return its timestamp."""
    ts = format_ts(utc_now() - timedelta(minutes=minutes_ago))
    store.record_snapshot(
        ProfileSnapshot.model_validate(snapshot_payload(profile_id, ts, **overrides))
    )
    return ts


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def test_schema_constants() -> None:
    assert SCHEMA_VERSION == 2
    assert MIGRATABLE_SCHEMA_VERSIONS == (1,)
    assert TABLES == (
        "profiles",
        "profile_snapshots",
        "profile_trades",
        "profile_daily",
        "settings",
        "events",
    )
    assert EVENT_LEVELS == ("info", "warning", "error")
    assert PROFILE_UPDATE_FIELDS == (
        "name",
        "strategy",
        "timeframe",
        "mode",
        "exchange",
        "pairs",
        "initial_capital",
        "max_open_trades",
        "priority",
        "enabled",
        "source",
    )


# The documented DDL: column names and types are part of the contract.
EXPECTED_COLUMNS = {
    "profiles": [
        ("id", "TEXT"),
        ("name", "TEXT"),
        ("strategy", "TEXT"),
        ("timeframe", "TEXT"),
        ("mode", "TEXT"),
        ("exchange", "TEXT"),
        ("pairs", "TEXT"),
        ("initial_capital", "REAL"),
        ("max_open_trades", "INTEGER"),
        ("priority", "INTEGER"),
        ("enabled", "INTEGER"),
        ("source", "TEXT"),
        ("state", "TEXT"),
        ("state_reason", "TEXT"),
        ("api_port", "INTEGER"),
        ("api_username", "TEXT"),
        ("api_password", "TEXT"),
        ("pid", "INTEGER"),
        ("started_at", "TEXT"),
        ("last_error", "TEXT"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
    ],
    "profile_snapshots": [
        ("profile_id", "TEXT"),
        ("ts", "TEXT"),
        ("portfolio_value", "REAL"),
        ("cash", "REAL"),
        ("positions_value", "REAL"),
        ("profit_abs", "REAL"),
        ("profit_pct", "REAL"),
        ("realized_profit_abs", "REAL"),
        ("unrealized_profit_abs", "REAL"),
        ("open_trades", "INTEGER"),
        ("closed_trades", "INTEGER"),
        ("win_rate", "REAL"),
        ("profit_factor", "REAL"),
        ("max_drawdown_pct", "REAL"),
        ("healthy", "INTEGER"),
    ],
    "profile_trades": [
        ("profile_id", "TEXT"),
        ("trade_id", "INTEGER"),
        ("pair", "TEXT"),
        ("is_open", "INTEGER"),
        ("open_date", "TEXT"),
        ("close_date", "TEXT"),
        ("amount", "REAL"),
        ("open_rate", "REAL"),
        ("close_rate", "REAL"),
        ("stake_amount", "REAL"),
        ("profit_abs", "REAL"),
        ("profit_pct", "REAL"),
        ("exit_reason", "TEXT"),
        ("updated_at", "TEXT"),
    ],
    "profile_daily": [
        ("profile_id", "TEXT"),
        ("date", "TEXT"),
        ("abs_profit", "REAL"),
        ("rel_profit", "REAL"),
        ("starting_balance", "REAL"),
        ("trade_count", "INTEGER"),
        ("updated_at", "TEXT"),
    ],
    "settings": [("key", "TEXT"), ("value", "TEXT"), ("updated_at", "TEXT")],
    "events": [
        ("id", "INTEGER"),
        ("ts", "TEXT"),
        ("profile_id", "TEXT"),
        ("level", "TEXT"),
        ("kind", "TEXT"),
        ("message", "TEXT"),
    ],
}

EXPECTED_PRIMARY_KEYS = {
    "profiles": ["id"],
    "profile_snapshots": ["profile_id", "ts"],
    "profile_trades": ["profile_id", "trade_id"],
    "profile_daily": ["profile_id", "date"],
    "settings": ["key"],
    "events": ["id"],
}


def test_exported_column_tuples_match_the_documented_ddl() -> None:
    assert list(PROFILE_TRADE_COLUMNS) == [name for name, _ in EXPECTED_COLUMNS["profile_trades"]]
    assert list(PROFILE_DAILY_COLUMNS) == [name for name, _ in EXPECTED_COLUMNS["profile_daily"]]


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_schema_matches_the_documented_columns(
    store: StateStore, db_path: Path, table: str
) -> None:
    connection = sqlite3.connect(db_path)
    try:
        described = list(connection.execute(f"PRAGMA table_info({table})"))
    finally:
        connection.close()

    assert [(str(row[1]), str(row[2]).upper()) for row in described] == EXPECTED_COLUMNS[table]
    assert [str(row[1]) for row in sorted(described, key=lambda row: row[5]) if row[5]] == (
        EXPECTED_PRIMARY_KEYS[table]
    )


def test_profiles_pairs_column_holds_a_json_array(store: StateStore, db_path: Path) -> None:
    store.upsert_profile(make_profile(pairs=["BTC/USDT", "ETH/USDT"]))

    connection = sqlite3.connect(db_path)
    try:
        raw = connection.execute("SELECT pairs FROM profiles").fetchone()[0]
    finally:
        connection.close()

    assert json.loads(raw) == ["BTC/USDT", "ETH/USDT"]


def test_bootstrap_creates_the_schema(store: StateStore, db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()

    assert set(TABLES) <= names
    assert {"idx_profile_snapshots_ts", "idx_events_ts", "idx_profile_trades_open"} <= indexes
    assert version == SCHEMA_VERSION
    assert store.list_profiles() == []


def test_store_creates_its_parent_directory_lazily(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "realtime" / "state.db"
    store = StateStore(db_path)
    assert not db_path.parent.exists()

    with store:
        store.bootstrap()

    assert db_path.is_file()
    assert not list(db_path.parent.glob("*.legacy-*"))


def test_bootstrap_is_idempotent(store: StateStore) -> None:
    store.upsert_profile(make_profile())

    assert store.bootstrap() is None
    assert [record.id for record in store.list_profiles()] == ["alpha-btc-1h"]


def test_context_manager_supports_further_use(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    with store:
        store.bootstrap()
        store.upsert_profile(make_profile())

    assert [record.id for record in store.list_profiles()] == ["alpha-btc-1h"]
    store.close()


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def test_upsert_profile_creates_the_row(store: StateStore) -> None:
    record = store.upsert_profile(make_profile())

    assert record.id == "alpha-btc-1h"
    assert record.name == "Alpha BTC 1h"
    assert record.strategy == "basic"
    assert record.timeframe == "1h"
    assert record.mode == "paper"
    assert record.exchange == "binance"
    assert record.pairs == ["BTC/USDT"]
    assert record.initial_capital == 1000.0
    assert record.max_open_trades == 2
    assert record.priority == 100
    assert record.enabled is True
    assert record.source == "catalogue"
    assert record.state == "stopped"
    assert record.state_reason is None
    assert record.api_port == 0
    assert record.api_username is None
    assert record.api_password is None
    assert record.pid is None
    assert record.started_at is None
    assert record.last_error is None
    assert record.created_at == record.updated_at
    assert record.created_at.endswith("Z")

    assert store.get_profile("alpha-btc-1h") == record
    assert store.get_profile("absent") is None
    assert store.list_profiles() == [record]


def test_upsert_profile_records_the_requested_source_and_state(store: StateStore) -> None:
    record = store.upsert_profile(
        make_profile("operator-1"),
        source="operator",
        state="blocked",
        state_reason="live trading refused",
    )

    assert record.source == "operator"
    assert record.state == "blocked"
    assert record.state_reason == "live trading refused"


def test_upsert_profile_updates_the_declarative_fields_only(store: StateStore) -> None:
    created = store.upsert_profile(make_profile())
    store.set_api_credentials("alpha-btc-1h", "worker-user", "worker-password")
    store.set_profile_state("alpha-btc-1h", "error", reason="crash", last_error="traceback")
    store.set_profile_runtime(
        "alpha-btc-1h",
        pid=4242,
        api_port=8101,
        started_at="2026-01-01T00:00:00Z",
    )

    updated = store.upsert_profile(
        make_profile(
            name="Renamed",
            timeframe="4h",
            pairs=["ETH/USDT"],
            initial_capital=250.0,
            max_open_trades=1,
            priority=50,
            enabled=False,
        ),
        source="catalogue",
        state="running",
        state_reason=None,
    )

    assert updated.name == "Renamed"
    assert updated.timeframe == "4h"
    assert updated.pairs == ["ETH/USDT"]
    assert updated.initial_capital == 250.0
    assert updated.max_open_trades == 1
    assert updated.priority == 50
    assert updated.enabled is False

    # Runtime bookkeeping survives a catalogue apply.
    assert updated.state == "error"
    assert updated.state_reason == "crash"
    assert updated.last_error == "traceback"
    assert updated.api_port == 8101
    assert updated.api_username == "worker-user"
    assert updated.api_password == "worker-password"
    assert updated.pid == 4242
    assert updated.started_at == "2026-01-01T00:00:00Z"
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at


def test_upsert_profile_fills_the_timestamps_that_were_never_set(
    store: StateStore, db_path: Path
) -> None:
    store.upsert_profile(make_profile())
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE profiles SET created_at = NULL, updated_at = ''")
        connection.commit()
    finally:
        connection.close()

    updated = store.upsert_profile(make_profile(name="Renamed"))

    assert updated.created_at.endswith("Z")
    assert updated.updated_at.endswith("Z")
    connection = sqlite3.connect(db_path)
    try:
        stored = connection.execute(
            "SELECT created_at, updated_at FROM profiles WHERE id = ?", ("alpha-btc-1h",)
        ).fetchone()
    finally:
        connection.close()
    assert list(stored) == [updated.created_at, updated.updated_at]


def test_upsert_profile_tolerates_a_hand_edited_pairs_column(
    store: StateStore, db_path: Path
) -> None:
    rows = [
        ("empty", ""),
        ("missing", None),
        ("plain", "BTC/USDT"),
        ("object", '{"pair": "BTC/USDT"}'),
        ("json", '["BTC/USDT", "ETH/USDT"]'),
    ]
    connection = sqlite3.connect(db_path)
    try:
        connection.executemany(
            "INSERT INTO profiles (id, strategy, pairs) VALUES (?, 'basic', ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()

    records = {record.id: record for record in store.list_profiles()}

    assert records["empty"].pairs == []
    assert records["missing"].pairs == []
    assert records["plain"].pairs == ["BTC/USDT"]
    assert records["object"].pairs == []
    assert records["json"].pairs == ["BTC/USDT", "ETH/USDT"]


def test_update_profile_fields_accepts_a_json_pairs_string(store: StateStore) -> None:
    store.upsert_profile(make_profile())

    record = store.update_profile_fields("alpha-btc-1h", {"pairs": '["ADA/USDT"]'})

    assert record.pairs == ["ADA/USDT"]


def test_list_profiles_is_ordered_by_priority_then_id(store: StateStore) -> None:
    store.upsert_profile(make_profile("zeta", priority=100))
    store.upsert_profile(make_profile("alpha", priority=100))
    store.upsert_profile(make_profile("mid", priority=50))

    assert [record.id for record in store.list_profiles()] == ["alpha", "zeta", "mid"]


def test_update_profile_fields_writes_the_allowed_columns(store: StateStore) -> None:
    store.upsert_profile(make_profile())

    record = store.update_profile_fields(
        "alpha-btc-1h",
        {
            "name": "Operator name",
            "pairs": ["SOL/USDT", "BNB/USDT"],
            "priority": 10,
            "enabled": False,
            "mode": "live",
            "source": "operator",
        },
    )

    assert record.name == "Operator name"
    assert record.pairs == ["SOL/USDT", "BNB/USDT"]
    assert record.priority == 10
    assert record.enabled is False
    assert record.mode == "live"
    assert record.source == "operator"
    assert record.timeframe == "1h"
    assert store.get_profile("alpha-btc-1h") == record


def test_update_profile_fields_without_fields_returns_the_record(store: StateStore) -> None:
    created = store.upsert_profile(make_profile())

    assert store.update_profile_fields("alpha-btc-1h", {}) == created


def test_update_profile_fields_rejects_an_unknown_profile(store: StateStore) -> None:
    with pytest.raises(KeyError, match="absent"):
        store.update_profile_fields("absent", {"name": "Nope"})


def test_update_profile_fields_rejects_an_unknown_column(store: StateStore) -> None:
    store.upsert_profile(make_profile())

    with pytest.raises(ValueError, match="bogus") as excinfo:
        store.update_profile_fields("alpha-btc-1h", {"name": "Nope", "bogus": 1})

    assert "bogus" in str(excinfo.value)
    assert "name" in str(excinfo.value)

    unchanged = store.get_profile("alpha-btc-1h")
    assert unchanged is not None
    assert unchanged.name == "Alpha BTC 1h"


def test_delete_profile_reports_whether_a_row_was_removed(store: StateStore) -> None:
    store.upsert_profile(make_profile())

    assert store.delete_profile("alpha-btc-1h") is True
    assert store.delete_profile("alpha-btc-1h") is False
    assert store.get_profile("alpha-btc-1h") is None
    assert store.list_profiles() == []


def test_set_profile_state_writes_and_clears_the_reason(store: StateStore) -> None:
    store.upsert_profile(make_profile())

    store.set_profile_state("alpha-btc-1h", "blocked", reason="live trading refused")
    blocked = store.get_profile("alpha-btc-1h")
    assert blocked is not None
    assert blocked.state == "blocked"
    assert blocked.state_reason == "live trading refused"

    store.set_profile_state("alpha-btc-1h", "error", reason="crash", last_error="traceback")
    failed = store.get_profile("alpha-btc-1h")
    assert failed is not None
    assert failed.state == "error"
    assert failed.last_error == "traceback"

    store.set_profile_state("alpha-btc-1h", "running")
    running = store.get_profile("alpha-btc-1h")
    assert running is not None
    assert running.state == "running"
    assert running.state_reason is None
    assert running.last_error is None


def test_set_profile_state_on_an_unknown_profile_is_a_noop(store: StateStore) -> None:
    store.set_profile_state("absent", "running", reason="nobody")
    assert store.list_profiles() == []


def test_set_profile_runtime_writes_and_clears_columns(store: StateStore) -> None:
    store.upsert_profile(make_profile())

    store.set_profile_runtime(
        "alpha-btc-1h",
        pid=4242,
        api_port=8101,
        started_at="2026-01-01T00:00:00Z",
    )
    started = store.get_profile("alpha-btc-1h")
    assert started is not None
    assert started.pid == 4242
    assert started.api_port == 8101
    assert started.started_at == "2026-01-01T00:00:00Z"

    store.set_profile_runtime("alpha-btc-1h", pid=None, started_at=None)
    stopped = store.get_profile("alpha-btc-1h")
    assert stopped is not None
    assert stopped.pid is None
    assert stopped.started_at is None
    assert stopped.api_port == 8101


def test_set_profile_runtime_without_arguments_changes_nothing(store: StateStore) -> None:
    store.upsert_profile(make_profile())
    store.set_profile_runtime("alpha-btc-1h", pid=4242, api_port=8101)

    store.set_profile_runtime("alpha-btc-1h")

    record = store.get_profile("alpha-btc-1h")
    assert record is not None
    assert record.pid == 4242
    assert record.api_port == 8101


def test_set_profile_runtime_on_an_unknown_profile_is_a_noop(store: StateStore) -> None:
    store.set_profile_runtime("absent", pid=1)
    assert store.list_profiles() == []


def test_set_api_credentials_stores_the_generated_pair(store: StateStore) -> None:
    store.upsert_profile(make_profile())

    store.set_api_credentials("alpha-btc-1h", "worker-user", "worker-password")

    record = store.get_profile("alpha-btc-1h")
    assert record is not None
    assert record.api_username == "worker-user"
    assert record.api_password == "worker-password"


def test_count_by_source_always_reports_both_sources(store: StateStore) -> None:
    assert store.count_by_source() == {"catalogue": 0, "operator": 0}

    store.upsert_profile(make_profile("cat-1"))
    store.upsert_profile(make_profile("cat-2"))
    store.upsert_profile(make_profile("ops-1"), source="operator")

    assert store.count_by_source() == {"catalogue": 2, "operator": 1}


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
def test_snapshot_round_trip_preserves_every_column(store: StateStore) -> None:
    store.upsert_profile(make_profile())
    snapshot = ProfileSnapshot.model_validate(
        snapshot_payload(
            ts="2026-01-02T03:04:00Z",
            portfolio_value=1000.25,
            cash=250.5,
            positions_value=749.75,
            profit_abs=0.25,
            profit_pct=0.025,
            realized_profit_abs=12.5,
            unrealized_profit_abs=-12.25,
            open_trades=3,
            closed_trades=7,
            win_rate=0.4285,
            profit_factor=1.75,
            max_drawdown_pct=4.5,
            healthy=False,
        )
    )

    store.record_snapshot(snapshot)

    stored = store.list_snapshots("alpha-btc-1h")
    assert stored == [snapshot]
    assert store.latest_snapshots() == {"alpha-btc-1h": snapshot}


def test_record_snapshot_replaces_the_row_of_the_same_minute(store: StateStore) -> None:
    store.upsert_profile(make_profile())
    timestamp = format_ts(utc_now() - timedelta(minutes=1))

    store.record_snapshot(
        ProfileSnapshot.model_validate(
            snapshot_payload(ts=timestamp, portfolio_value=1000.0, open_trades=1)
        )
    )
    store.record_snapshot(
        ProfileSnapshot.model_validate(
            snapshot_payload(ts=timestamp, portfolio_value=1010.0, open_trades=0)
        )
    )

    stored = store.list_snapshots("alpha-btc-1h")
    assert len(stored) == 1
    assert stored[0].portfolio_value == 1010.0
    assert stored[0].open_trades == 0


def test_list_snapshots_is_ascending_and_filters_since(store: StateStore) -> None:
    store.upsert_profile(make_profile())
    oldest = snapshot_at(store, "alpha-btc-1h", 3, portfolio_value=1000.0)
    middle = snapshot_at(store, "alpha-btc-1h", 2, portfolio_value=1005.0)
    newest = snapshot_at(store, "alpha-btc-1h", 1, portfolio_value=1010.0)

    assert [item.ts for item in store.list_snapshots("alpha-btc-1h")] == [
        oldest,
        middle,
        newest,
    ]

    window = store.list_snapshots(
        "alpha-btc-1h", since=utc_now() - timedelta(minutes=2, seconds=30)
    )
    assert [item.ts for item in window] == [middle, newest]
    assert store.list_snapshots("other-profile") == []


def test_list_snapshots_limit_keeps_the_most_recent_rows(store: StateStore) -> None:
    store.upsert_profile(make_profile())
    snapshot_at(store, "alpha-btc-1h", 3, portfolio_value=1000.0)
    middle = snapshot_at(store, "alpha-btc-1h", 2, portfolio_value=1005.0)
    newest = snapshot_at(store, "alpha-btc-1h", 1, portfolio_value=1010.0)

    limited = store.list_snapshots("alpha-btc-1h", limit=2)
    assert [item.ts for item in limited] == [middle, newest]
    assert [item.portfolio_value for item in limited] == [1005.0, 1010.0]
    assert store.list_snapshots("alpha-btc-1h", limit=0) == []


def test_latest_snapshots_returns_the_newest_row_per_profile(store: StateStore) -> None:
    store.upsert_profile(make_profile("alpha"))
    store.upsert_profile(make_profile("beta"))
    snapshot_at(store, "alpha", 2, portfolio_value=1000.0)
    snapshot_at(store, "alpha", 1, portfolio_value=1010.0)
    snapshot_at(store, "beta", 5, portfolio_value=500.0)

    latest = store.latest_snapshots()

    assert set(latest) == {"alpha", "beta"}
    assert latest["alpha"].portfolio_value == 1010.0
    assert latest["beta"].portfolio_value == 500.0


def test_latest_snapshots_is_empty_without_history(store: StateStore) -> None:
    assert store.latest_snapshots() == {}


def test_snapshot_series_groups_by_profile_and_filters_since(store: StateStore) -> None:
    store.upsert_profile(make_profile("alpha"))
    store.upsert_profile(make_profile("beta"))
    alpha_old = snapshot_at(store, "alpha", 3, portfolio_value=1000.0)
    alpha_new = snapshot_at(store, "alpha", 1, portfolio_value=1010.0)
    beta_new = snapshot_at(store, "beta", 1, portfolio_value=500.0)

    series = store.snapshot_series()

    assert list(series) == ["alpha", "beta"]
    assert [item.ts for item in series["alpha"]] == [alpha_old, alpha_new]
    assert [item.ts for item in series["beta"]] == [beta_new]

    recent = store.snapshot_series(since=utc_now() - timedelta(minutes=2))
    assert list(recent) == ["alpha", "beta"]
    assert [item.ts for item in recent["alpha"]] == [alpha_new]
    assert store.snapshot_series(since=utc_now() + timedelta(minutes=1)) == {}


def test_prune_snapshots_deletes_the_rows_outside_the_retention_window(store: StateStore) -> None:
    store.upsert_profile(make_profile())
    old_ts = format_ts(utc_now() - timedelta(days=120))
    recent_ts = format_ts(utc_now() - timedelta(days=2))
    store.record_snapshot(ProfileSnapshot.model_validate(snapshot_payload(ts=old_ts)))
    store.record_snapshot(ProfileSnapshot.model_validate(snapshot_payload(ts=recent_ts)))

    removed = store.prune_snapshots(retention_days=90)

    assert removed == 1
    assert [item.ts for item in store.list_snapshots("alpha-btc-1h")] == [recent_ts]
    assert store.prune_snapshots(retention_days=90) == 0


# ---------------------------------------------------------------------------
# Trade read model
# ---------------------------------------------------------------------------
def trade_count(db_path: Path, table: str) -> int:
    """Return the number of rows physically stored in ``table``."""
    connection = sqlite3.connect(db_path)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def test_upsert_trade_records_round_trips_every_column(store: StateStore) -> None:
    trade = ProfileTradeRecord(
        profile_id="alpha-btc-1h",
        trade_id=17,
        pair="ETH/USDT",
        is_open=False,
        open_date="2026-01-02T03:04:05Z",
        close_date="2026-01-02T07:08:09Z",
        amount=1.5,
        open_rate=2410.5,
        close_rate=2450.25,
        stake_amount=3615.75,
        profit_abs=59.625,
        profit_pct=1.65,
        exit_reason="roi",
        updated_at="2026-01-02T07:09:00Z",
    )

    assert store.upsert_trade_records([trade]) == 1

    assert store.trade_records("alpha-btc-1h") == [trade]
    assert store.trade_records("absent") == []


def test_upsert_trade_records_keeps_the_empty_columns_of_an_open_trade(store: StateStore) -> None:
    open_trade = ProfileTradeRecord(
        profile_id="alpha-btc-1h",
        trade_id=3,
        pair="BTC/USDT",
        is_open=True,
        open_date="2026-01-02T00:00:00Z",
        amount=0.5,
        open_rate=60000.0,
        stake_amount=30000.0,
        updated_at="2026-01-02T00:01:00Z",
    )

    store.upsert_trade_records([open_trade])

    stored = store.trade_records("alpha-btc-1h")[0]
    assert stored == open_trade
    assert stored.close_date is None
    assert stored.exit_reason is None
    assert stored.close_rate == 0.0
    assert stored.profit_abs == 0.0
    assert stored.is_open is True


def test_upsert_trade_records_stamps_a_missing_updated_at(store: StateStore) -> None:
    store.upsert_trade_records([ProfileTradeRecord(profile_id="alpha-btc-1h", trade_id=1)])

    stored = store.trade_records("alpha-btc-1h")[0]
    assert stored.updated_at.endswith("Z")
    assert stored.updated_at != ""


def test_upserting_a_closing_trade_flips_it_instead_of_duplicating_it(
    store: StateStore, db_path: Path
) -> None:
    opened = ProfileTradeRecord(
        profile_id="alpha-btc-1h",
        trade_id=7,
        pair="BTC/USDT",
        is_open=True,
        open_date="2026-01-02T00:00:00Z",
        amount=1.0,
        open_rate=60000.0,
        stake_amount=60000.0,
        updated_at="2026-01-02T00:00:00Z",
    )
    closed = replace(
        opened,
        is_open=False,
        close_date="2026-01-02T06:00:00Z",
        close_rate=61000.0,
        profit_abs=1000.0,
        profit_pct=1.6667,
        exit_reason="roi",
        updated_at="2026-01-02T06:00:00Z",
    )

    assert store.upsert_trade_records([opened]) == 1
    assert store.upsert_trade_records([closed]) == 1

    rows = store.trade_records("alpha-btc-1h")
    assert len(rows) == 1
    assert rows[0] == closed
    assert rows[0].is_open is False
    assert trade_count(db_path, "profile_trades") == 1


def test_trade_records_puts_the_newest_open_date_first(store: StateStore) -> None:
    store.upsert_trade_records(
        [
            ProfileTradeRecord(
                profile_id="alpha-btc-1h", trade_id=1, open_date="2026-01-02T00:00:00Z"
            ),
            ProfileTradeRecord(
                profile_id="alpha-btc-1h", trade_id=3, open_date="2026-01-05T00:00:00Z"
            ),
            ProfileTradeRecord(profile_id="alpha-btc-1h", trade_id=2, open_date=None),
            ProfileTradeRecord(
                profile_id="alpha-btc-1h", trade_id=4, open_date="2026-01-02T00:00:00Z"
            ),
            ProfileTradeRecord(profile_id="other", trade_id=9, open_date="2026-01-09T00:00:00Z"),
        ]
    )

    # Newest first, the shared date broken by trade id DESC, the undated last.
    assert [item.trade_id for item in store.trade_records("alpha-btc-1h")] == [3, 4, 1, 2]
    assert [item.trade_id for item in store.trade_records("other")] == [9]


def test_upsert_daily_records_round_trips_every_column(store: StateStore) -> None:
    day = ProfileDailyRecord(
        profile_id="alpha-btc-1h",
        date="2026-01-02",
        abs_profit=42.5,
        rel_profit=4.25,
        starting_balance=1000.0,
        trade_count=7,
        updated_at="2026-01-02T23:59:00Z",
    )

    assert store.upsert_daily_records([day]) == 1

    assert store.daily_records("alpha-btc-1h") == [day]
    assert store.daily_records("absent") == []


def test_upserting_a_day_twice_updates_it_in_place(store: StateStore, db_path: Path) -> None:
    store.upsert_daily_records(
        [ProfileDailyRecord(profile_id="alpha-btc-1h", date="2026-01-02", abs_profit=1.0)]
    )
    store.upsert_daily_records(
        [
            ProfileDailyRecord(
                profile_id="alpha-btc-1h",
                date="2026-01-02",
                abs_profit=25.0,
                rel_profit=2.5,
                starting_balance=1000.0,
                trade_count=4,
                updated_at="2026-01-02T23:59:00Z",
            )
        ]
    )

    rows = store.daily_records("alpha-btc-1h")
    assert len(rows) == 1
    assert rows[0].abs_profit == 25.0
    assert rows[0].trade_count == 4
    assert rows[0].updated_at == "2026-01-02T23:59:00Z"
    assert trade_count(db_path, "profile_daily") == 1


def test_daily_records_is_newest_first_and_honours_the_limit(store: StateStore) -> None:
    store.upsert_daily_records(
        [
            ProfileDailyRecord(profile_id="alpha-btc-1h", date="2026-01-01", abs_profit=1.0),
            ProfileDailyRecord(profile_id="alpha-btc-1h", date="2026-01-03", abs_profit=3.0),
            ProfileDailyRecord(profile_id="alpha-btc-1h", date="2026-01-02", abs_profit=2.0),
        ]
    )

    assert [day.date for day in store.daily_records("alpha-btc-1h")] == [
        "2026-01-03",
        "2026-01-02",
        "2026-01-01",
    ]
    assert [day.date for day in store.daily_records("alpha-btc-1h", limit=2)] == [
        "2026-01-03",
        "2026-01-02",
    ]
    assert store.daily_records("alpha-btc-1h", limit=0) == []
    assert store.daily_records("alpha-btc-1h", limit=-5) == []
    assert store.daily_records("absent", limit=2) == []


def test_upserting_an_empty_sequence_writes_nothing(store: StateStore, db_path: Path) -> None:
    assert store.upsert_trade_records([]) == 0
    assert store.upsert_daily_records([]) == 0

    assert trade_count(db_path, "profile_trades") == 0
    assert trade_count(db_path, "profile_daily") == 0


def test_delete_profile_removes_its_trades_and_days_only(store: StateStore, db_path: Path) -> None:
    store.upsert_profile(make_profile("alpha-btc-1h"))
    store.upsert_profile(make_profile("beta-btc-1h"))
    store.upsert_trade_records(
        [
            ProfileTradeRecord(
                profile_id="alpha-btc-1h", trade_id=1, open_date="2026-01-02T00:00:00Z"
            ),
            ProfileTradeRecord(
                profile_id="beta-btc-1h", trade_id=1, open_date="2026-01-02T00:00:00Z"
            ),
        ]
    )
    store.upsert_daily_records(
        [
            ProfileDailyRecord(profile_id="alpha-btc-1h", date="2026-01-02"),
            ProfileDailyRecord(profile_id="beta-btc-1h", date="2026-01-02"),
        ]
    )

    assert store.delete_profile("alpha-btc-1h") is True

    assert store.get_profile("alpha-btc-1h") is None
    assert store.trade_records("alpha-btc-1h") == []
    assert store.daily_records("alpha-btc-1h") == []
    assert [trade.trade_id for trade in store.trade_records("beta-btc-1h")] == [1]
    assert [day.date for day in store.daily_records("beta-btc-1h")] == ["2026-01-02"]
    assert trade_count(db_path, "profile_trades") == 1
    assert trade_count(db_path, "profile_daily") == 1
    assert store.delete_profile("absent") is False


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def test_record_event_returns_the_stored_event(store: StateStore) -> None:
    event = store.record_event("info", "start", "profile started", "alpha-btc-1h")

    assert event.id is not None
    assert event.id >= 1
    assert event.ts.endswith("Z")
    assert event.profile_id == "alpha-btc-1h"
    assert event.level == "info"
    assert event.kind == "start"
    assert event.message == "profile started"
    assert store.list_events() == [event]


def test_record_event_defaults_a_blank_level_to_info(store: StateStore) -> None:
    event = store.record_event("  ", "generic", "platform wide event")
    assert event.level == "info"
    assert event.profile_id is None
    assert store.list_events()[0] == event


def test_list_events_is_newest_first_and_limited(store: StateStore) -> None:
    first = store.record_event("info", "start", "first")
    second = store.record_event("warning", "blocked_live", "second")
    third = store.record_event("error", "crash", "third")

    assert [event.id for event in store.list_events()] == [third.id, second.id, first.id]
    assert [event.id for event in store.list_events(limit=2)] == [third.id, second.id]
    assert store.list_events(limit=0) == []

    store.record_event("info", "start", "fourth")
    assert len(store.list_events(limit=2)) == 2


def test_list_events_compares_since_as_a_string(store: StateStore, db_path: Path) -> None:
    store.record_event("info", "start", "recent")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO events (ts, profile_id, level, kind, message) VALUES (?, ?, ?, ?, ?)",
            ("2020-01-01T00:00:00Z", None, "info", "start", "ancient"),
        )
        connection.commit()
    finally:
        connection.close()

    messages = [event.message for event in store.list_events(since="2021-01-01T00:00:00Z")]
    assert messages == ["recent"]
    assert [event.message for event in store.list_events()] == ["recent", "ancient"]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def test_get_setting_is_none_when_absent(store: StateStore) -> None:
    assert store.get_setting("snapshot_interval_seconds") is None
    assert store.all_settings() == {}


def test_set_setting_inserts_then_updates(store: StateStore) -> None:
    store.set_setting("snapshot_interval_seconds", "120")
    assert store.get_setting("snapshot_interval_seconds") == "120"

    store.set_setting("snapshot_interval_seconds", "60")
    assert store.get_setting("snapshot_interval_seconds") == "60"
    assert store.all_settings() == {"snapshot_interval_seconds": "60"}


def test_platform_settings_are_stored_as_json(store: StateStore) -> None:
    document = {"snapshot_interval_seconds": 120, "kill_switch_engaged": False}

    store.set_setting("platform_settings", json.dumps(document))
    raw = store.get_setting("platform_settings")

    assert raw is not None
    assert json.loads(raw) == document
    assert store.all_settings()["platform_settings"] == raw

    store.set_setting("platform_settings", {"snapshot_interval_seconds": 60})
    updated = store.get_setting("platform_settings")
    assert updated is not None
    assert json.loads(updated) == {"snapshot_interval_seconds": 60}


def test_all_settings_is_sorted_by_key(store: StateStore) -> None:
    store.set_setting("snapshot_interval_seconds", "60")
    store.set_setting("kill_switch_engaged", "false")
    store.set_setting("platform_settings", "{}")

    assert list(store.all_settings()) == [
        "kill_switch_engaged",
        "platform_settings",
        "snapshot_interval_seconds",
    ]


# ---------------------------------------------------------------------------
# Thread safety: one connection, several threads
# ---------------------------------------------------------------------------
def test_concurrent_readers_and_a_writer_never_break_the_shared_connection(
    db_path: Path,
) -> None:
    """Drive the store from several threads at once, as the running platform does.

    The state store is a single instance shared by the API thread pool -- every
    request of the dashboard reads the profiles, their snapshots and their read
    model from it -- and by the poller, which writes a tick from the event loop.
    One ``sqlite3.Connection`` must never be used by two threads at the same
    time: without the store serialising its own access this test fails with
    ``sqlite3.InterfaceError``, which is exactly what a dashboard page load did
    before the store took its lock.
    """
    store = StateStore(db_path)
    store.bootstrap()
    store.upsert_profile(make_profile("alpha-btc-1h"))
    for index in range(20):
        snapshot_at(store, "alpha-btc-1h", minutes_ago=index)

    errors: list[BaseException] = []
    barrier = threading.Barrier(9)

    def read() -> None:
        barrier.wait()
        try:
            for _ in range(80):
                store.list_snapshots("alpha-btc-1h", limit=60)
                store.list_profiles()
        except BaseException as exc:  # noqa: BLE001 - reported to the assertion
            errors.append(exc)

    def write() -> None:
        barrier.wait()
        try:
            for index in range(80):
                snapshot_at(store, "alpha-btc-1h", minutes_ago=index)
        except BaseException as exc:  # noqa: BLE001 - reported to the assertion
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    threads.append(threading.Thread(target=write))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
