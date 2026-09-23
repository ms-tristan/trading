"""Tests of the ``v3 -> v4`` profile-payload migration and of the quarantine contract.

Schema version ``4`` exists because a field *removal* is not an additive change.  The
release that dropped the forecast subsystem removed ``forecast`` from
:class:`~trading_platform.config.models.ProfileConfig` (which is ``extra="forbid"``)
and removed the ``timesfm`` strategy from the registry, but nothing migrated the rows
already persisted in SQLite -- and SQLite is the single source of truth of the realtime
platform.  The next boot therefore read a row the model refuses, ``load_profiles()``
aborted on the *first* such row, and one stale key took the whole platform down.

Two properties are pinned here, and neither of them needs a binary fixture: every
database is built from the store's public API plus raw ``sqlite3`` writes on a
``tmp_path`` file.

1. **the forward migration** -- opening a version-3 database rewrites every ``profiles``
   row so that it carries exactly the keys the *current* ``ProfileConfig`` declares.
   The prune is driven off ``ProfileConfig.model_fields`` and not off a hard-coded list
   of removed names, so the next removed field is handled by the same code; it is
   top-level only, it never touches ``updated_at``, it never rewrites a row that has
   nothing to prune, and it leaves a payload it cannot read as a JSON object alone (the
   quarantine below reports that row instead of mangling it);
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

#: The version this build writes: the profile-payload prune.
VERSION_FOUR = 4

#: The instant a legacy row carries in ``updated_at``.  Distinctive on purpose: the
#: migration must never touch it, so any rewrite is visible at a glance.
LEGACY_UPDATED_AT = "2023-12-31T23:59:59+00:00"

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
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FOUR]
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
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FOUR]

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
    assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FOUR]

    # a second pass over the payload path finds nothing left to prune
    for _profile_id, payload, _updated_at in migrated:
        assert set(json.loads(payload)) <= set(ProfileConfig.model_fields)

    second = SqliteStateStore(db_path, clock=clock)
    second.initialize()
    try:
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FOUR]
        assert all_profile_rows(db_path) == migrated
        assert [item.id for item in second.load_profiles()] == ["btc-paper", "eth-paper"]
        assert second.load_profile_failures() == {}
    finally:
        second.close()


# ---------------------------------------------------------------------------
# (f) a database already at version 4 is opened as-is
# ---------------------------------------------------------------------------


def test_a_version_four_database_written_by_this_build_opens_with_no_migration(
    db_path: Path, clock: ManualClock
) -> None:
    """Nothing to migrate, so not a single row is touched."""
    first = SqliteStateStore(db_path, clock=clock)
    first.initialize()
    first.save_profile(make_profile("btc-paper", params={"forecast": 1.5}))
    first.save_profile(make_profile("eth-paper", symbol="ETH/USDT"))
    first.close()

    before = all_profile_rows(db_path)
    assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FOUR]

    reopened = SqliteStateStore(db_path, clock=clock)
    reopened.initialize()
    try:
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FOUR]
        assert all_profile_rows(db_path) == before
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
        assert schema_versions(db_path) == [SCHEMA_VERSION] == [VERSION_FOUR]
        assert [item.id for item in store.load_profiles()] == ["aaa-paper"]
        assert set(store.load_profile_failures()) == {BROKEN_PROFILE_ID}
    finally:
        store.close()
