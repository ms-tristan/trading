"""Tests of the two non-additive migrations of the store and of the quarantine contract.

Schema version ``4`` exists because a field *removal* is not an additive change.  The
release that dropped the forecast subsystem removed ``forecast`` from
:class:`~trading_platform.config.models.ProfileConfig` (which is ``extra="forbid"``)
and removed the ``timesfm`` strategy from the registry, but nothing migrated the rows
already persisted in SQLite -- and SQLite is the single source of truth of the realtime
platform.  The next boot therefore read a row the model refuses, ``load_profiles()``
aborted on the *first* such row, and one stale key took the whole platform down.

Schema version ``5`` exists for a different reason: a ``CHECK`` constraint cannot be
widened in place.  The single-row ``wallet`` table of version ``3`` pinned
``wallet_id`` to ``1``, and one ledger per mode needs ``2`` as well, so the table is
**rebuilt** rather than redeclared -- and the legacy row is *migrated*, not dropped:
its id ``1`` is the paper id, so the ledger the previous build held survives as the
paper ledger.

Two properties are pinned here, and neither of them needs a binary fixture: every
database is built from the store's public API plus raw ``sqlite3`` writes on a
``tmp_path`` file.

1. **the forward migrations** -- opening a version-3 database rewrites every
   ``profiles`` row so that it carries exactly the keys the *current*
   ``ProfileConfig`` declares, and opening a version-4 database rebuilds the
   ``wallet`` table so that it accepts both ledger ids while keeping the row it
   already held.
   The prune is driven off ``ProfileConfig.model_fields`` and not off a hard-coded list
   of removed names, so the next removed field is handled by the same code; it is
   top-level only, it never touches ``updated_at``, it never rewrites a row that has
   nothing to prune, and it leaves a payload it cannot read as a JSON object alone (the
   quarantine below reports that row instead of mangling it).  Both steps are
   idempotent, and both are no-ops on a clean database;
2. **the quarantine** -- a read of the profile table skips an unreadable row, logs it
   loudly with a *sanitised* reason and keeps loading every other profile, while
   ``load_profiles(strict=True)`` keeps the previous raising behaviour for the callers
   that want it.  A profile row is exactly where an operator might have hand-written a
   credential, so no test here ever allows a payload value to reach a log record or a
   reported reason.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from trading_platform.config.models import ProfileConfig
from trading_platform.core.errors import StateStoreError
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.store import SCHEMA_VERSION, SqliteStateStore

#: A fixed anchor: every test starts the virtual clock here.
START = pd.Timestamp("2024-01-01T00:00:00Z")

#: The version the migration starts from (the last schema shipped before the prune).
VERSION_THREE = 3

#: The version the ``profiles`` payload prune introduced.
VERSION_FOUR = 4

#: The version this build writes: one ledger per mode.
VERSION_FIVE = 5

#: The instant a legacy row carries in ``updated_at``.  Distinctive on purpose: the
#: migration must never touch it, so any rewrite is visible at a glance.
LEGACY_UPDATED_AT = "2023-12-31T23:59:59+00:00"

#: The two ledger amounts of the hand-written legacy ``wallet`` row.  They are kept as
#: Python floats and bound as parameters rather than inlined in the SQL text, so the
#: statement stays valid on every SQLite build the test suite runs against.
LEGACY_CASH = 4_242.0
LEGACY_INITIAL_BALANCE = 10_000.0

#: The profile whose row is unreadable in the quarantine fixtures.
BROKEN_PROFILE_ID = "bbb-paper"

#: A distinctive value that is only ever written *inside* a profile payload.  It stands
#: for the credential an operator may have hand-edited into a row: it must never appear
#: in a log record, nor in a reason the monitoring read model exposes.
BROKEN_SECRET = "s3cr3t-hunter2"

#: A profile payload that is not valid JSON at all (an interrupted hand edit).
BROKEN_PAYLOAD = '{"id": "bbb-paper", "symbol": "BTC/USDT", "initial_balance": "s3cr3t-hunter2"'

#: The logger the store reports through.
STORE_LOGGER = "trading_platform.realtime.store"


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
    """Open a second connection used to write or inspect the database directly."""
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


def seed_version_three_database(db_path: Path, clock: ManualClock, *specs: ProfileConfig) -> None:
    """Write one clean profile row per spec, then stamp the file as version 3.

    The rows go in through the store's public API -- exactly the way the deployed
    build wrote them -- and the version is then put back to ``3`` with a raw write,
    which is the only way to build the *previous* schema's file from this build.
    """
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        for spec in specs:
            store.save_profile(spec)
    finally:
        store.close()
    force_schema_version(db_path, VERSION_THREE)


def force_schema_version(db_path: Path, version: int) -> None:
    """Stamp the stored schema version, as the build that wrote the file left it."""
    with raw_connection(db_path) as conn:
        conn.execute("UPDATE schema_version SET version = ?", (version,))


def set_profile_row(
    db_path: Path,
    profile_id: str,
    payload: str,
    *,
    updated_at: str = LEGACY_UPDATED_AT,
) -> None:
    """Replace one profile row verbatim, as an older build or an operator left it."""
    with raw_connection(db_path) as conn:
        conn.execute(
            "UPDATE profiles SET payload = ?, updated_at = ? WHERE profile_id = ?",
            (payload, updated_at, profile_id),
        )


def insert_profile_row(
    db_path: Path,
    profile_id: str,
    payload: str,
    *,
    updated_at: str = LEGACY_UPDATED_AT,
) -> None:
    """Add one profile row verbatim, bypassing the store's own serialisation."""
    with raw_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO profiles (profile_id, payload, updated_at) VALUES (?, ?, ?)",
            (profile_id, payload, updated_at),
        )


def profile_row(db_path: Path, profile_id: str) -> tuple[str, str]:
    """Return ``(payload, updated_at)`` of one profile row, exactly as stored."""
    rows = all_profile_rows(db_path)
    matches = [(payload, updated_at) for pid, payload, updated_at in rows if pid == profile_id]
    assert len(matches) == 1, f"expected exactly one row for {profile_id!r}, got {len(matches)}"
    return matches[0]


def all_profile_rows(db_path: Path) -> list[tuple[str, str, str]]:
    """Return ``(profile_id, payload, updated_at)`` of every profile row, ordered."""
    with raw_connection(db_path) as conn:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute(
                "SELECT profile_id, payload, updated_at FROM profiles ORDER BY profile_id ASC"
            )
        ]


def schema_versions(db_path: Path) -> list[int]:
    """Return the stored schema version rows of the database, ordered."""
    with raw_connection(db_path) as conn:
        return [
            int(row[0])
            for row in conn.execute("SELECT version FROM schema_version ORDER BY version ASC")
        ]


def wallet_rows(db_path: Path) -> list[tuple[int, float, float, str]]:
    """Return ``(wallet_id, cash, initial_balance, updated_at)`` of every ledger row."""
    with raw_connection(db_path) as conn:
        return [
            (int(row[0]), float(row[1]), float(row[2]), str(row[3]))
            for row in conn.execute(
                "SELECT wallet_id, cash, initial_balance, updated_at FROM wallet ORDER BY wallet_id"
            )
        ]


def wallet_table_sql(db_path: Path) -> str:
    """Return the DDL SQLite stores for the ``wallet`` table."""
    with raw_connection(db_path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'wallet'"
        ).fetchone()
    assert row is not None, "the database has no wallet table at all"
    return str(row[0])


def seed_version_four_database(db_path: Path, clock: ManualClock, cash: float = 7_500.0) -> None:
    """Rebuild the **previous build's** ``wallet`` table, and stamp the file version 4.

    Schema version ``4`` is the last release before the per-mode ledgers, and its
    ``wallet`` table pinned ``wallet_id`` to ``1`` with a ``CHECK``.  That exact shape
    is reproduced here -- DDL included -- because it is the one
    ``CREATE TABLE IF NOT EXISTS`` cannot widen and the one the ``4 -> 5`` migration
    has to rebuild.  A legacy ledger row is written through it, so a test can prove
    the row survives as the paper ledger.
    """
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    store.save_wallet(cash=cash, initial_balance=10_000.0)
    store.close()
    with raw_connection(db_path) as conn:
        conn.execute("DROP TABLE wallet")
        conn.execute(
            "CREATE TABLE wallet ("
            "wallet_id INTEGER PRIMARY KEY CHECK (wallet_id = 1), cash REAL NOT NULL, "
            "initial_balance REAL NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO wallet (wallet_id, cash, initial_balance, updated_at) "
            "VALUES (1, ?, 10000.0, ?)",
            (cash, LEGACY_UPDATED_AT),
        )
    force_schema_version(db_path, VERSION_FOUR)


def seed_quarantined_database(db_path: Path, clock: ManualClock) -> None:
    """Build a version-3 database with two readable rows and one unreadable row.

    The unreadable row is not valid JSON at all and carries
    :data:`BROKEN_SECRET`, so every test using this fixture also pins that neither a
    log record nor a reported reason ever echoes a payload value.
    """
    seed_version_three_database(
        db_path,
        clock,
        make_profile("aaa-paper"),
        make_profile("ccc-paper", symbol="ETH/USDT"),
    )
    insert_profile_row(db_path, BROKEN_PROFILE_ID, BROKEN_PAYLOAD)


def record_text(caplog: pytest.LogCaptureFixture) -> str:
    """Return everything the captured log records carry, message and fields alike."""
    return "\n".join(
        f"{record.getMessage()} {record.args!r} {getattr(record, 'context', '')!r}"
        for record in caplog.records
    )


def warning_records_naming(
    caplog: pytest.LogCaptureFixture, profile_id: str
) -> list[logging.LogRecord]:
    """Return the WARNING records that name ``profile_id``.

    The message is the first place to look; a structured event carries the identifier
    as a field instead, so both forms count as naming the profile.
    """
    return [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and profile_id
        in (
            f"{record.getMessage()} "
            f"{getattr(record, 'profile_id', '')} "
            f"{getattr(record, 'context', '')}"
        )
    ]


# ---------------------------------------------------------------------------
# (a) the forward migration: a stale key is pruned, nothing else moves
# ---------------------------------------------------------------------------


def test_a_version_three_row_with_an_undeclared_key_is_pruned_on_open(
    db_path: Path, clock: ManualClock
) -> None:
    """Opening a version-3 database drops exactly the keys the model does not declare.

    The row reproduces the incident -- ``forecast``, removed by the release that
    shipped this migration -- *and* a name no release ever declared, so a migration
    driven off a hard-coded list of removed names fails here.  The ``forecast`` nested
    inside ``params`` proves the prune is top-level only, and the second row proves a
    payload with nothing to prune is not rewritten at all.
    """
    btc = make_profile("btc-paper", params={"forecast": 1.5})
    eth = make_profile("eth-paper", symbol="ETH/USDT")
    seed_version_three_database(db_path, clock, btc, eth)

    stored = json.loads(profile_row(db_path, "btc-paper")[0])
    stored["forecast"] = None  # the field the previous release removed
    stored["news_sentiment"] = {"enabled": False}  # a name never declared anywhere
    payload_before = json.dumps(stored, sort_keys=True)
    set_profile_row(db_path, "btc-paper", payload_before)
    clean_before = profile_row(db_path, "eth-paper")
    rows_before = all_profile_rows(db_path)

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()  # the stale key no longer bricks the boot
    try:
        payload_after, updated_after = profile_row(db_path, "btc-paper")
        pruned = json.loads(payload_after)
        # the unknown keys are gone and every other key/value pair is identical
        assert pruned == {
            key: value for key, value in stored.items() if key in ProfileConfig.model_fields
        }
        assert "forecast" not in pruned
        assert "news_sentiment" not in pruned
        assert pruned["params"] == {"forecast": 1.5}
        # the row keeps the instant the deployed build wrote
        assert updated_after == LEGACY_UPDATED_AT
        # the row that had nothing to prune is byte-for-byte the one on disk
        assert profile_row(db_path, "eth-paper") == clean_before
        assert len(all_profile_rows(db_path)) == len(rows_before)
        # the stored version is the one of this build
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]
        # and both profiles load, readable again, in profile_id order
        loaded = store.load_profiles()
        assert [item.id for item in loaded] == ["btc-paper", "eth-paper"]
        assert loaded == [btc, eth]
        assert store.load_profile_failures() == {}
    finally:
        store.close()


def test_the_migration_prunes_every_undeclared_key_not_only_the_known_one(
    db_path: Path, clock: ManualClock
) -> None:
    """Two undeclared keys in the same row: both go, and only they go.

    This is the same property as the test above read from the other side -- a
    migration that special-cases ``forecast`` still passes *that* test but fails this
    one, which is why the prune has to be driven off ``ProfileConfig.model_fields``.
    """
    seed_version_three_database(db_path, clock, make_profile("btc-paper"))
    stored = json.loads(profile_row(db_path, "btc-paper")[0])
    stored["forecast"] = {"enabled": True, "model": "timesfm"}
    stored["future_extras"] = [1, 2, 3]
    set_profile_row(db_path, "btc-paper", json.dumps(stored, sort_keys=True))

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        pruned = json.loads(profile_row(db_path, "btc-paper")[0])
        assert set(pruned) == set(ProfileConfig.model_fields)
        assert store.load_profiles() == [make_profile("btc-paper")]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# (b) a payload the migration cannot read is left alone and quarantined
# ---------------------------------------------------------------------------


def test_a_payload_that_is_not_json_is_left_untouched_and_quarantined(
    db_path: Path, clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    """Invalid JSON is never mangled by the migration, and never blocks the platform."""
    seed_quarantined_database(db_path, clock)
    before = all_profile_rows(db_path)

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        # the migration left the unreadable row byte-for-byte as it found it
        assert profile_row(db_path, BROKEN_PROFILE_ID) == (BROKEN_PAYLOAD, LEGACY_UPDATED_AT)
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]

        with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
            loaded = store.load_profiles()

        # every readable profile still loads, in profile_id order
        assert [item.id for item in loaded] == ["aaa-paper", "ccc-paper"]
        # the failed row is reported, and only it
        failures = store.load_profile_failures()
        assert set(failures) == {BROKEN_PROFILE_ID}
        reason = failures[BROKEN_PROFILE_ID]
        assert reason, "the reported reason must tell an operator something"
        assert BROKEN_SECRET not in reason
        # it was logged loudly, and the value never reached the logs
        assert warning_records_naming(caplog, BROKEN_PROFILE_ID)
        assert BROKEN_SECRET not in record_text(caplog)
        # a read is a read: no row was rewritten by it
        assert all_profile_rows(db_path) == before
    finally:
        store.close()


def test_a_validation_failure_is_reported_without_echoing_the_value(
    db_path: Path, clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    """The reason keeps the field path and the rule; the offending value never leaks.

    A profile row is where an operator may have hand-written a credential, and the
    validation message echoes the offending value.  The quarantine reports the
    sanitised reason only -- this is the property
    :func:`~trading_platform.realtime.store.redact_profile_error` exists for.
    """
    seed_version_three_database(
        db_path, clock, make_profile("aaa-paper"), make_profile(BROKEN_PROFILE_ID)
    )
    set_profile_row(
        db_path,
        BROKEN_PROFILE_ID,
        json.dumps(
            {
                "id": BROKEN_PROFILE_ID,
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "initial_balance": BROKEN_SECRET,
            }
        ),
    )

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
            loaded = store.load_profiles()

        assert [item.id for item in loaded] == ["aaa-paper"]
        reason = store.load_profile_failures()[BROKEN_PROFILE_ID]
        assert "initial_balance" in reason  # the field path is what an operator needs
        assert BROKEN_SECRET not in reason
        assert warning_records_naming(caplog, BROKEN_PROFILE_ID)
        assert BROKEN_SECRET not in record_text(caplog)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# (c) the strict path keeps the raising contract
# ---------------------------------------------------------------------------


def test_strict_mode_still_raises_on_the_quarantined_row(db_path: Path, clock: ManualClock) -> None:
    """``load_profiles(strict=True)`` restores the previous, raising behaviour."""
    seed_quarantined_database(db_path, clock)

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        with pytest.raises(StateStoreError, match="corrupted payload") as excinfo:
            store.load_profiles(strict=True)

        message = str(excinfo.value)
        assert message.startswith(
            f"state store read failed (load_profiles): corrupted payload for {BROKEN_PROFILE_ID}: "
        )
        assert BROKEN_SECRET not in message
        # the tolerant read of the very same store answers the readable profiles
        assert [item.id for item in store.load_profiles()] == ["aaa-paper", "ccc-paper"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# (d) load_profile_failures(): the read-only accessor
# ---------------------------------------------------------------------------


def test_load_profile_failures_is_empty_when_there_is_nothing_to_report(
    store: SqliteStateStore,
) -> None:
    """``{}`` before any read, after a clean read, and once every row is readable."""
    assert store.load_profile_failures() == {}
    store.save_profile(make_profile())
    assert store.load_profile_failures() == {}
    assert [item.id for item in store.load_profiles()] == ["btc-paper"]
    assert store.load_profile_failures() == {}


def test_load_profile_failures_never_raises_on_a_store_that_was_never_initialized(
    db_path: Path,
) -> None:
    """It is a read-only report: it must not open, lock or raise anything."""
    unopened = SqliteStateStore(db_path)
    assert unopened.is_initialized() is False
    assert unopened.load_profile_failures() == {}
    # and it opened nothing at all: no database file, no writer lock
    assert not db_path.exists()
    assert not unopened.lock_path.exists()


def test_load_profile_failures_answers_for_the_most_recent_read_and_returns_a_copy(
    store: SqliteStateStore, db_path: Path
) -> None:
    """The mapping is the verdict of the last ``load_profiles()``, and it is a copy."""
    store.save_profile(make_profile("aaa-paper"))
    insert_profile_row(db_path, BROKEN_PROFILE_ID, BROKEN_PAYLOAD)

    assert [item.id for item in store.load_profiles()] == ["aaa-paper"]
    failures = store.load_profile_failures()
    assert set(failures) == {BROKEN_PROFILE_ID}
    # mutating the answer never reaches the store
    failures.clear()
    failures["aaa-paper"] = "invented by the caller"
    assert set(store.load_profile_failures()) == {BROKEN_PROFILE_ID}

    # once the row is readable again, the next read reports nothing
    repaired = make_profile(BROKEN_PROFILE_ID)
    set_profile_row(db_path, BROKEN_PROFILE_ID, json.dumps(repaired.model_dump(mode="json")))
    assert [item.id for item in store.load_profiles()] == ["aaa-paper", BROKEN_PROFILE_ID]
    assert store.load_profile_failures() == {}


# ---------------------------------------------------------------------------
# (e) the migration is idempotent
# ---------------------------------------------------------------------------


def test_running_the_payload_migration_twice_changes_nothing(
    db_path: Path, clock: ManualClock
) -> None:
    """The second open finds nothing left to prune and rewrites nothing."""
    seed_version_three_database(
        db_path, clock, make_profile("btc-paper"), make_profile("eth-paper", symbol="ETH/USDT")
    )
    stored = json.loads(profile_row(db_path, "btc-paper")[0])
    stored["forecast"] = None
    set_profile_row(db_path, "btc-paper", json.dumps(stored, sort_keys=True))

    first = SqliteStateStore(db_path, clock=clock)
    first.initialize()
    migrated = all_profile_rows(db_path)
    first.close()
    assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]

    # a second pass over the payload path finds nothing left to prune
    for _profile_id, payload, _updated_at in migrated:
        assert set(json.loads(payload)) <= set(ProfileConfig.model_fields)

    second = SqliteStateStore(db_path, clock=clock)
    second.initialize()
    try:
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]
        assert all_profile_rows(db_path) == migrated
        assert [item.id for item in second.load_profiles()] == ["btc-paper", "eth-paper"]
        assert second.load_profile_failures() == {}
    finally:
        second.close()


# ---------------------------------------------------------------------------
# (f) a database written by this build is opened as-is
# ---------------------------------------------------------------------------


def test_a_database_written_by_this_build_opens_with_no_migration(
    db_path: Path, clock: ManualClock
) -> None:
    """Nothing to migrate -- neither payload nor wallet table -- so no row is touched."""
    first = SqliteStateStore(db_path, clock=clock)
    first.initialize()
    first.save_profile(make_profile("btc-paper", params={"forecast": 1.5}))
    first.save_profile(make_profile("eth-paper", symbol="ETH/USDT"))
    first.save_wallet(cash=7_500.0, initial_balance=10_000.0)
    first.close()

    before = all_profile_rows(db_path)
    wallet_before = wallet_rows(db_path)
    assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]

    reopened = SqliteStateStore(db_path, clock=clock)
    reopened.initialize()
    try:
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]
        assert all_profile_rows(db_path) == before
        # the wallet table this build created already carries the per-mode CHECK, so
        # the 4 -> 5 rebuild is a no-op and its row is byte-for-byte the one written
        assert wallet_rows(db_path) == wallet_before
        # ``before`` is ordered by profile_id, so its first row is ``btc-paper``: the
        # payload and the ``updated_at`` of that row are byte-for-byte the ones written
        assert profile_row(db_path, "btc-paper") == before[0][1:]
        assert [item.id for item in reopened.load_profiles()] == ["btc-paper", "eth-paper"]
        assert reopened.load_profile_failures() == {}
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# (g) the documented skip: valid JSON that is not an object
# ---------------------------------------------------------------------------


def test_a_valid_json_payload_that_is_not_an_object_is_left_untouched(
    db_path: Path, clock: ManualClock
) -> None:
    """The prune only rewrites a payload it can read as a JSON object.

    A list is valid JSON and carries no key to prune, so the migration leaves it
    byte-for-byte alone; the quarantine reports it as unreadable instead of the
    migration inventing a shape for it.
    """
    seed_version_three_database(
        db_path, clock, make_profile("aaa-paper"), make_profile("bbb-paper")
    )
    array_payload = '["bbb-paper", "forecast"]'
    set_profile_row(db_path, BROKEN_PROFILE_ID, array_payload)

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        assert profile_row(db_path, BROKEN_PROFILE_ID) == (array_payload, LEGACY_UPDATED_AT)
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]
        assert [item.id for item in store.load_profiles()] == ["aaa-paper"]
        assert set(store.load_profile_failures()) == {BROKEN_PROFILE_ID}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# (h) the 4 -> 5 step: the wallet table becomes one ledger per mode
# ---------------------------------------------------------------------------


def test_a_version_four_ledger_row_survives_the_rebuild_as_the_paper_ledger(
    db_path: Path, clock: ManualClock
) -> None:
    """The legacy single row is MIGRATED, never lost: it becomes the paper ledger.

    A version-4 database carries the narrow ``CHECK (wallet_id = 1)`` that
    ``CREATE TABLE IF NOT EXISTS`` cannot widen, so the ``4 -> 5`` step rebuilds the
    table.  The rebuild copies the row instead of dropping it, and the row's
    ``wallet_id`` -- ``1`` -- *is* the paper id, so the ledger the previous build held
    is the paper ledger of this build, byte for byte: same cash, same initial balance,
    same ``updated_at``.  And the rebuilt table accepts a second ledger, which is the
    whole reason the step exists.
    """
    seed_version_four_database(db_path, clock, cash=7_500.0)
    assert "wallet_id = 1" in wallet_table_sql(db_path), "the fixture must be the v4 table"
    assert wallet_rows(db_path) == [(1, 7_500.0, 10_000.0, LEGACY_UPDATED_AT)]
    assert schema_versions(db_path) == [VERSION_FOUR]

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        # (a) the table now accepts both documented ids ...
        table_sql = wallet_table_sql(db_path)
        assert "wallet_id IN (1, 2)" in table_sql
        assert "wallet_id = 1" not in table_sql.replace("wallet_id IN (1, 2)", "")
        # (b) ... the stored version is the one of this build ...
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]
        # (c) ... and the legacy row is STILL THERE, as the paper ledger
        assert wallet_rows(db_path) == [(1, 7_500.0, 10_000.0, LEGACY_UPDATED_AT)]
        paper = store.load_wallet()
        assert paper is not None
        assert paper.cash == pytest.approx(7_500.0)
        assert paper.initial_balance == pytest.approx(10_000.0)
        assert paper.updated_at == pd.Timestamp(LEGACY_UPDATED_AT)
        assert store.load_wallet(mode="paper") == paper
        # (d) ... the live ledger simply has no row yet
        assert store.load_wallet(mode="live") is None
        # (e) ... and a live row can now be written as ``wallet_id = 2``
        store.save_wallet(cash=640.0, initial_balance=500.0, mode="live")
        assert wallet_rows(db_path) == [
            (1, 7_500.0, 10_000.0, LEGACY_UPDATED_AT),
            (2, 640.0, 500.0, START.isoformat()),
        ]
        assert store.load_wallet().cash == pytest.approx(7_500.0)  # type: ignore[union-attr]
        live = store.load_wallet(mode="live")
        assert live is not None and live.cash == pytest.approx(640.0)
    finally:
        store.close()


def test_the_wallet_mode_migration_is_idempotent(db_path: Path, clock: ManualClock) -> None:
    """A second open finds the table already correct and rewrites nothing.

    The rebuild is driven by the shape of the table on disk -- its ``CHECK`` -- and
    not by the stored version, so an already-migrated file (and a file whose version
    row was hand-edited) is left byte for byte alone.
    """
    seed_version_four_database(db_path, clock, cash=3_210.5)

    first = SqliteStateStore(db_path, clock=clock)
    first.initialize()
    migrated_rows = wallet_rows(db_path)
    migrated_sql = wallet_table_sql(db_path)
    first.close()
    assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]

    second = SqliteStateStore(db_path, clock=clock)
    second.initialize()
    try:
        assert wallet_rows(db_path) == migrated_rows
        assert wallet_table_sql(db_path) == migrated_sql
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]
        assert second.load_wallet(mode="live") is None
    finally:
        second.close()


def test_a_version_four_database_with_no_ledger_row_still_gains_the_per_mode_table(
    db_path: Path, clock: ManualClock
) -> None:
    """An empty legacy wallet table is rebuilt too: the shape, not the row, is migrated.

    The previous build could legitimately ship the table empty (the engine writes its
    first row at boot), and that file still has to accept the live ledger afterwards.
    """
    seed_version_four_database(db_path, clock)
    with raw_connection(db_path) as conn:
        conn.execute("DELETE FROM wallet")
    assert wallet_rows(db_path) == []

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        assert "wallet_id IN (1, 2)" in wallet_table_sql(db_path)
        assert wallet_rows(db_path) == []
        assert store.load_wallet() is None
        assert store.load_wallet(mode="live") is None
        # both ledgers can be written afterwards, and only the addressed one changes
        store.save_wallet(cash=1.0, initial_balance=2.0)
        store.save_wallet(cash=3.0, initial_balance=4.0, mode="live")
        assert wallet_rows(db_path) == [
            (1, 1.0, 2.0, START.isoformat()),
            (2, 3.0, 4.0, START.isoformat()),
        ]
    finally:
        store.close()


def test_the_wallet_rebuild_keeps_the_document_version_bump_in_one_transaction(
    db_path: Path, clock: ManualClock
) -> None:
    """The rebuild and the version bump commit together, as the prune does.

    A file whose version row is hand-edited **down** to ``4`` while its table is
    already per-mode must reopen cleanly: the rebuild is a no-op, the version is
    corrected, and no ledger row is touched.  That is the observable half of "one
    transaction" a test can pin without instrumenting SQLite.
    """
    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    store.save_wallet(cash=9_000.0, initial_balance=10_000.0)
    store.close()
    before = wallet_rows(db_path)
    force_schema_version(db_path, VERSION_FOUR)

    reopened = SqliteStateStore(db_path, clock=clock)
    reopened.initialize()
    try:
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]
        assert wallet_rows(db_path) == before
        assert reopened.load_wallet() is not None
        assert reopened.load_wallet().cash == pytest.approx(9_000.0)  # type: ignore[union-attr]
    finally:
        reopened.close()


def test_the_two_non_additive_steps_run_together_on_a_version_three_database(
    db_path: Path, clock: ManualClock
) -> None:
    """A version-3 file crosses both steps -- payload prune and wallet rebuild -- at once.

    Version ``3`` predates both: its ``profiles`` rows may carry an undeclared key and
    its ``wallet`` table is the narrow single-row one.  One boot has to fix both, and
    the ledger row must come out the other side as the paper ledger.
    """
    btc = make_profile("btc-paper")
    seed_version_three_database(db_path, clock, btc)
    stored = json.loads(profile_row(db_path, "btc-paper")[0])
    stored["forecast"] = None
    set_profile_row(db_path, "btc-paper", json.dumps(stored, sort_keys=True))
    # rebuild the v3 single-row wallet table with a legacy ledger
    with raw_connection(db_path) as conn:
        conn.execute("DROP TABLE wallet")
        conn.execute(
            "CREATE TABLE wallet ("
            "wallet_id INTEGER PRIMARY KEY CHECK (wallet_id = 1), cash REAL NOT NULL, "
            "initial_balance REAL NOT NULL, updated_at TEXT NOT NULL)"
        )
        # The two amounts are bound parameters, never spelled out in the SQL
        # text: the underscore digit separators of a Python float literal
        # (``10_000.0``) are not valid SQL on the SQLite builds older than
        # 3.46 that the CI runner ships, so an inlined literal would make this
        # test pass on a developer machine and fail on the runner.
        conn.execute(
            "INSERT INTO wallet (wallet_id, cash, initial_balance, updated_at) VALUES (1, ?, ?, ?)",
            (LEGACY_CASH, LEGACY_INITIAL_BALANCE, LEGACY_UPDATED_AT),
        )

    store = SqliteStateStore(db_path, clock=clock)
    store.initialize()
    try:
        # both rewrites happened in the one migration transaction
        assert set(json.loads(profile_row(db_path, "btc-paper")[0])) == set(
            ProfileConfig.model_fields
        )
        assert "wallet_id IN (1, 2)" in wallet_table_sql(db_path)
        assert wallet_rows(db_path) == [(1, LEGACY_CASH, LEGACY_INITIAL_BALANCE, LEGACY_UPDATED_AT)]
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FIVE]
        assert [item.id for item in store.load_profiles()] == ["btc-paper"]
        paper = store.load_wallet()
        assert paper is not None and paper.cash == pytest.approx(LEGACY_CASH)
        assert store.load_wallet(mode="live") is None
    finally:
        store.close()
