"""Tests of the persisted acted-entry-crossing watermark (work-package wp2).

The catch-up entry window (``entry_lookback_candles``) lets the live engine act on a
crossover that happened within the last ``N`` candles instead of only on the last row
of the signals frame.  That power has one failure mode and requirement 1 of the brief
names it: **a crossover must never be acted upon twice**.  The persistence layer owns
the guard -- a per-profile watermark of the *signal row an entry was already acted
upon* -- and this module pins its whole contract.

Everything runs against a real :class:`~trading_platform.realtime.store.SqliteStateStore`
on a ``tmp_path`` database: no fixed path, no fixed port, no network, no injected
clock beyond the one the other store tests already use.

The key properties, and the guard each test fails without:

1. absent key -> ``None`` (the state of every profile written before the feature);
2. round-trip in UTC, with naive and aware inputs normalised to one value;
3. monotonic: an older or equal write is a no-op, so a replay can only move forward;
4. per-profile isolation;
5. restart durability (write, ``close()``, re-open the same file);
6. independence from the candle watermark -- the two keys are never collapsed;
7. the entry watermark itself needs no DDL change and no migration of its own: it
   reuses the ``meta`` table that already backs the kill switch and the candle
   watermark (the stored schema version of this build is ``4``, raised by the
   profile-payload prune shipped in the same delivery);
8. a corrupted row degrades to ``None`` instead of killing a tick;
9. both names are part of the documented :class:`StateStore` protocol.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.store import (
    SCHEMA_VERSION,
    SqliteStateStore,
    StateStore,
)

#: A fixed anchor: every test starts the virtual clock here.
START = pd.Timestamp("2024-01-01T00:00:00Z")

#: The production key the watermark is stored under, prefixed by the profile.
ENTRY_WATERMARK_PREFIX = "entry_crossing:"


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
    """Open a second connection used to inspect the database directly."""
    connection = sqlite3.connect(db_path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def stored_meta(db_path: Path, key: str) -> str:
    """Return the raw ``meta`` text stored under ``key``."""
    with raw_connection(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    assert row is not None, f"the store must persist the meta key {key!r}"
    return str(row[0])


# ---------------------------------------------------------------------------
# 1. the absent key
# ---------------------------------------------------------------------------


def test_the_entry_watermark_starts_absent(store: SqliteStateStore) -> None:
    """A profile that never entered answers ``None``, never a default instant.

    ``None`` is the signal wp3 reads as "no crossover has ever been acted upon":
    the exact state of every profile persisted before this watermark existed, which
    is why the key must not be materialised at ``initialize`` time.
    """
    assert store.last_acted_entry_crossing("btc-paper") is None
    assert store.last_acted_entry_crossing("never-seen-profile") is None
    assert store.get_meta(ENTRY_WATERMARK_PREFIX + "btc-paper") is None


# ---------------------------------------------------------------------------
# 2. round-trip and UTC normalisation
# ---------------------------------------------------------------------------


def test_the_entry_watermark_round_trips_utc(store: SqliteStateStore, db_path: Path) -> None:
    """Mark then read returns the same instant, in UTC, stored as ISO-8601 text.

    The watermark has to be comparable across processes, so the stored text is an
    ISO-8601 UTC string and an aware input and the naive input of the *same instant*
    must collapse onto one value -- otherwise a naive and an aware caller would write
    two distinct watermarks for a single crossover and requirement 1 would leak.
    """
    store.mark_acted_entry_crossing("btc-paper", stamp(5))

    read_back = store.last_acted_entry_crossing("btc-paper")
    assert read_back == stamp(5)
    assert read_back is not None
    assert read_back.tz is not None
    assert read_back.tz.utc == read_back.tz

    raw = stored_meta(db_path, ENTRY_WATERMARK_PREFIX + "btc-paper")
    assert raw == "2024-01-01T00:05:00+00:00"
    assert pd.Timestamp(raw) == stamp(5)

    # an aware input expressed in another zone, then the naive input of the same
    # instant: both describe 2024-01-01T06:00:00Z and must land on one value
    aware = pd.Timestamp("2024-01-01T07:00:00+01:00")
    assert aware == stamp(360)
    store.mark_acted_entry_crossing("btc-paper", aware)
    after_aware = stored_meta(db_path, ENTRY_WATERMARK_PREFIX + "btc-paper")
    assert after_aware == "2024-01-01T06:00:00+00:00"
    assert store.last_acted_entry_crossing("btc-paper") == stamp(360)

    store.mark_acted_entry_crossing("eth-paper", stamp(360).tz_localize(None))
    assert stored_meta(db_path, ENTRY_WATERMARK_PREFIX + "eth-paper") == after_aware
    assert store.last_acted_entry_crossing("eth-paper") == store.last_acted_entry_crossing(
        "btc-paper"
    )

    # sub-second precision is preserved, so two distinct rows never alias
    precise = pd.Timestamp("2024-01-01T08:00:00.123456+00:00")
    store.mark_acted_entry_crossing("btc-paper", precise)
    assert store.last_acted_entry_crossing("btc-paper") == precise


# ---------------------------------------------------------------------------
# 3. monotonicity -- the heart of requirement 1
# ---------------------------------------------------------------------------


def test_the_entry_watermark_only_moves_forward(store: SqliteStateStore) -> None:
    """An older or equal write is a no-op: a replay can only move the watermark on.

    This is the guard that makes requirement 1 hold under any replay order -- a
    replayed tick, an out-of-order replay, or a restart re-reading the same frame
    can never make the engine act on a crossover it already traded.
    """
    store.mark_acted_entry_crossing("btc-paper", stamp(10))
    assert store.last_acted_entry_crossing("btc-paper") == stamp(10)

    store.mark_acted_entry_crossing("btc-paper", stamp(3))
    assert store.last_acted_entry_crossing("btc-paper") == stamp(10)

    store.mark_acted_entry_crossing("btc-paper", stamp(10))
    assert store.last_acted_entry_crossing("btc-paper") == stamp(10)

    store.mark_acted_entry_crossing("btc-paper", stamp(11))
    assert store.last_acted_entry_crossing("btc-paper") == stamp(11)

    # the same instant expressed in another zone is *equal*, hence also a no-op
    store.mark_acted_entry_crossing("btc-paper", pd.Timestamp("2024-01-01T01:11:00+01:00"))
    assert store.last_acted_entry_crossing("btc-paper") == stamp(11)


# ---------------------------------------------------------------------------
# 4. per-profile isolation
# ---------------------------------------------------------------------------


def test_the_entry_watermark_is_per_profile(store: SqliteStateStore) -> None:
    """One profile's watermark never gates another's entry."""
    store.mark_acted_entry_crossing("btc-paper", stamp(5))

    assert store.last_acted_entry_crossing("btc-paper") == stamp(5)
    assert store.last_acted_entry_crossing("eth-paper") is None

    store.mark_acted_entry_crossing("eth-paper", stamp(9))

    assert store.last_acted_entry_crossing("btc-paper") == stamp(5)
    assert store.last_acted_entry_crossing("eth-paper") == stamp(9)


# ---------------------------------------------------------------------------
# 5. restart durability
# ---------------------------------------------------------------------------


def test_the_entry_watermark_survives_a_reopen(db_path: Path, clock: ManualClock) -> None:
    """The watermark is durable: a restart must not forget what it already traded.

    Durability is the whole point of requirement 1 -- an in-memory guard would let
    the engine re-enter on the same crossover after every restart.
    """
    first = SqliteStateStore(db_path, clock=clock)
    first.initialize()
    first.mark_acted_entry_crossing("btc-paper", stamp(42))
    first.close()

    second = SqliteStateStore(db_path, clock=clock)
    second.initialize()
    try:
        assert second.last_acted_entry_crossing("btc-paper") == stamp(42)
        # and the reopened store keeps enforcing monotonicity
        second.mark_acted_entry_crossing("btc-paper", stamp(7))
        assert second.last_acted_entry_crossing("btc-paper") == stamp(42)
    finally:
        second.close()


# ---------------------------------------------------------------------------
# 6. independence from the candle watermark
# ---------------------------------------------------------------------------


def test_the_entry_watermark_is_independent_of_the_candle_watermark(
    store: SqliteStateStore, db_path: Path
) -> None:
    """The two watermarks answer two different questions and never share a key.

    ``last_candle:`` answers "which candle was processed"; ``entry_crossing:``
    answers "which signal row was traded on".  Collapsing them would silently
    suppress entries (a processed candle is not an acted crossover) or re-enable
    double entries, so each write leaves the other watermark untouched.
    """
    store.mark_candle_processed("btc-paper", stamp(5))
    assert store.last_processed_candle("btc-paper") == stamp(5)
    assert store.last_acted_entry_crossing("btc-paper") is None

    store.mark_acted_entry_crossing("eth-paper", stamp(9))
    assert store.last_acted_entry_crossing("eth-paper") == stamp(9)
    assert store.last_processed_candle("eth-paper") is None

    with raw_connection(db_path) as conn:
        keys = {str(row[0]) for row in conn.execute("SELECT key FROM meta")}
    assert "last_candle:btc-paper" in keys
    assert "entry_crossing:eth-paper" in keys
    assert "entry_crossing:btc-paper" not in keys
    assert "last_candle:eth-paper" not in keys


# ---------------------------------------------------------------------------
# 7. no DDL change of its own, no migration of its own
# ---------------------------------------------------------------------------


def test_the_entry_watermark_needs_no_table_and_no_migration(
    db_path: Path, clock: ManualClock
) -> None:
    """An existing database opens with no migration of its own and no version bump.

    The watermark reuses the free-form ``meta`` table that already backs the candle
    watermark and the kill switch, so ``_DDL`` is untouched by it: a database written
    by the previous build -- here one holding a ``meta`` row written by the frozen
    code path -- is opened as-is and still reports the schema version of this build.
    That version is ``4``, raised by the profile-payload prune shipped in the same
    delivery (see ``tests/test_realtime_store_migration.py``), which neither adds nor
    moves anything the entry watermark relies on.
    """
    assert SCHEMA_VERSION == 4

    first = SqliteStateStore(db_path, clock=clock)
    first.initialize()
    first.mark_candle_processed("btc-paper", stamp(5))
    first.set_meta("kill_switch", "false")
    first.close()

    before = stored_meta(db_path, "last_candle:btc-paper")

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        with raw_connection(db_path) as conn:
            versions = [int(row[0]) for row in conn.execute("SELECT version FROM schema_version")]
        assert versions == [SCHEMA_VERSION] == [4]
        # the pre-existing rows are untouched by the boot
        assert stored_meta(db_path, "last_candle:btc-paper") == before
        assert stored_meta(db_path, "kill_switch") == "false"
        # and the new watermark simply starts absent on that old database
        assert store.last_acted_entry_crossing("btc-paper") is None
        store.mark_acted_entry_crossing("btc-paper", stamp(6))
        assert store.last_acted_entry_crossing("btc-paper") == stamp(6)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 8. corrupted value
# ---------------------------------------------------------------------------


def test_a_corrupted_entry_watermark_degrades_to_none(
    store: SqliteStateStore, db_path: Path
) -> None:
    """A corrupted row answers ``None`` instead of raising inside a live tick.

    A single bad byte in one key must never take the whole engine down: the tick
    treats it as "nothing was ever acted upon", which at worst re-arms one entry
    instead of stopping the profile.
    """
    with raw_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (ENTRY_WATERMARK_PREFIX + "btc-paper", "not-a-timestamp"),
        )

    assert store.last_acted_entry_crossing("btc-paper") is None

    # the store is not poisoned: the next real write takes the key over
    store.mark_acted_entry_crossing("btc-paper", stamp(4))
    assert store.last_acted_entry_crossing("btc-paper") == stamp(4)


# ---------------------------------------------------------------------------
# 9. the documented protocol
# ---------------------------------------------------------------------------


def test_the_new_methods_are_part_of_the_documented_protocol() -> None:
    """Both names live on :class:`StateStore`, so an incomplete store is detectable.

    The engine consumes the seam, not the SQLite class: a store implementing only
    the old surface must be detectably incomplete at the boundary rather than
    silently accepted and then crash on the first entry decision.
    """
    assert "last_acted_entry_crossing" in vars(StateStore)
    assert "mark_acted_entry_crossing" in vars(StateStore)
    assert callable(StateStore.last_acted_entry_crossing)
    assert callable(StateStore.mark_acted_entry_crossing)
    assert callable(SqliteStateStore.last_acted_entry_crossing)
    assert callable(SqliteStateStore.mark_acted_entry_crossing)
