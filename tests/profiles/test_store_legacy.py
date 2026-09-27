"""Tests of the legacy-database archive: the reason the deployed stack survives.

The previous product left its own ``state.db`` in the ``trading-state`` volume.
A foreign file must be renamed to ``state.db.legacy-<UTC timestamp>`` and a
fresh database must be created next to it, without parsing, migrating or
deleting the old file. The cases below are table driven: each one builds a
different foreign (or already valid) file, boots the store on it and checks both
what happened to the file and what the fresh database answers.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from trading_platform.models import ProfileConfig, utc_now
from trading_platform.profiles.store import (
    PROFILE_COLUMNS,
    SCHEMA_VERSION,
    TABLES,
    StateStore,
    archive_legacy_database,
)

LEGACY_NAME_PATTERN = re.compile(r"\.legacy-\d{8}T\d{6}Z$")
LEGACY_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True)
class ForeignDatabase:
    """One foreign file to boot the store on."""

    case_id: str
    build: Callable[[Path], None]
    #: Whether the archived file must still be a readable SQLite database.
    readable: bool = True

    def __str__(self) -> str:  # pragma: no cover - used as a pytest id
        return self.case_id


def build_unrelated_profiles_table(path: Path) -> None:
    """The state database of the previous product: a ``profiles`` table of its own."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE profiles (id INTEGER PRIMARY KEY, payload TEXT, created TEXT)"
        )
        connection.execute(
            "INSERT INTO profiles (payload, created) VALUES ('legacy row', '2025-01-01T00:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()


def build_unrelated_profiles_table_at_revision_one(path: Path) -> None:
    """A foreign ``profiles`` table that happens to claim the platform revision."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE profiles (id INTEGER PRIMARY KEY, payload TEXT)")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()


def build_profiles_table_without_id(path: Path) -> None:
    """A ``profiles`` table whose columns have nothing to do with the platform."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE profiles (anything TEXT, other INTEGER)")
        connection.execute("INSERT INTO profiles (anything, other) VALUES ('x', 1)")
        connection.commit()
    finally:
        connection.close()


def build_newer_schema_revision(path: Path) -> None:
    """A database that claims a schema revision the platform does not know."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE profiles (id TEXT PRIMARY KEY, hash TEXT)")
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    finally:
        connection.close()


def build_platform_prefixed_database(path: Path) -> None:
    """A database with the platform's table names but an older revision stamp."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE profiles (id TEXT PRIMARY KEY, state TEXT)")
        connection.execute("CREATE TABLE profile_snapshots (profile_id TEXT, ts TEXT)")
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
    finally:
        connection.close()


def build_not_a_database(path: Path) -> None:
    """A leftover that is not a SQLite database at all."""
    path.write_bytes(b"this is not a sqlite database\n" * 8)


def build_partial_platform_database(path: Path) -> None:
    """A complete ``profiles`` table of this revision, without the other three."""
    connection = sqlite3.connect(path)
    try:
        columns = ", ".join(f"{name} TEXT" for name in PROFILE_COLUMNS if name != "id")
        connection.execute(f"CREATE TABLE profiles (id TEXT PRIMARY KEY, {columns})")
        connection.execute("INSERT INTO profiles (id, name) VALUES ('half', 'Half written')")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()


FOREIGN_DATABASES = [
    ForeignDatabase("previous-product-profiles-table", build_unrelated_profiles_table),
    ForeignDatabase(
        "foreign-profiles-table-at-revision-one",
        build_unrelated_profiles_table_at_revision_one,
    ),
    ForeignDatabase("profiles-table-without-id", build_profiles_table_without_id),
    ForeignDatabase("newer-schema-revision", build_newer_schema_revision),
    ForeignDatabase("platform-names-older-revision", build_platform_prefixed_database),
    ForeignDatabase("not-a-database", build_not_a_database, readable=False),
]


def legacy_archives(db_path: Path) -> list[Path]:
    """Return the archives sitting next to ``db_path``."""
    return sorted(db_path.parent.glob(f"{db_path.name}.legacy-*"))


@pytest.mark.parametrize("case", FOREIGN_DATABASES, ids=str)
def test_bootstrap_archives_a_foreign_database(case: ForeignDatabase, tmp_path: Path) -> None:
    db_path = tmp_path / "realtime" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    case.build(db_path)
    original = db_path.read_bytes()

    store = StateStore(db_path)
    archived = store.bootstrap()

    assert archived is not None
    assert archived.parent == db_path.parent
    assert archived.name.startswith(f"{db_path.name}.legacy-")
    assert LEGACY_NAME_PATTERN.search(archived.name)
    assert archived.is_file()
    assert legacy_archives(db_path) == [archived]

    # The old file is kept as it was: never parsed, never migrated, never deleted.
    if case.readable:
        connection = sqlite3.connect(archived)
        try:
            names = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            connection.close()
        assert "profiles" in names
    else:
        assert archived.read_bytes() == original

    # A fresh database answers next to it, and the archive is journalled.
    assert store.list_profiles() == []
    assert store.latest_snapshots() == {}
    assert store.get_profile("anything") is None
    assert [event.kind for event in store.list_events()] == ["legacy_db_archived"]
    assert store.list_events()[0].level == "warning"
    assert store.list_events()[0].message.endswith(archived.name)

    connection = sqlite3.connect(db_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()
    assert version == SCHEMA_VERSION
    assert set(TABLES) <= names

    # Booting again finds a platform database and archives nothing.
    assert store.bootstrap() is None
    assert legacy_archives(db_path) == [archived]


def test_bootstrap_keeps_a_platform_database(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    with StateStore(db_path) as store:
        store.bootstrap()
        store.upsert_profile(ProfileConfig(id="kept-profile", strategy="basic"))
        store.record_event("info", "start", "kept event")

    reopened = StateStore(db_path)

    assert reopened.bootstrap() is None
    assert [record.id for record in reopened.list_profiles()] == ["kept-profile"]
    assert [event.message for event in reopened.list_events()] == ["kept event"]
    assert legacy_archives(db_path) == []


def test_bootstrap_creates_a_fresh_database_without_an_archive(tmp_path: Path) -> None:
    db_path = tmp_path / "realtime" / "state.db"

    store = StateStore(db_path)

    assert store.bootstrap() is None
    assert db_path.is_file()
    assert store.list_profiles() == []
    assert store.list_events() == []
    assert legacy_archives(db_path) == []


def test_bootstrap_creates_the_tables_around_an_existing_profiles_table(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    build_partial_platform_database(db_path)

    store = StateStore(db_path)

    assert store.bootstrap() is None
    assert legacy_archives(db_path) == []
    kept = store.get_profile("half")
    assert kept is not None
    assert kept.name == "Half written"

    connection = sqlite3.connect(db_path)
    try:
        names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()
    assert set(TABLES) <= names


def test_bootstrap_keeps_a_foreign_file_without_a_profiles_table(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, symbol TEXT)")
        connection.execute("INSERT INTO orders (symbol) VALUES ('BTC/USDT')")
        connection.commit()
    finally:
        connection.close()

    store = StateStore(db_path)

    # Only an unusable ``profiles`` table marks a foreign platform database, so
    # this file is completed in place instead of being archived.
    assert store.bootstrap() is None
    assert legacy_archives(db_path) == []
    assert store.list_profiles() == []

    connection = sqlite3.connect(db_path)
    try:
        names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        orders = connection.execute("SELECT symbol FROM orders").fetchall()
    finally:
        connection.close()
    assert "orders" in names
    assert set(TABLES) <= names
    assert orders == [("BTC/USDT",)]


def test_open_archives_a_corrupt_state_file(tmp_path: Path) -> None:
    db_path = tmp_path / "realtime" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    build_not_a_database(db_path)

    # Opening the store is enough: the corrupt leftover is archived, not parsed.
    with StateStore(db_path) as store:
        assert store.list_profiles() == []
        assert [event.kind for event in store.list_events()] == ["legacy_db_archived"]

    archives = legacy_archives(db_path)
    assert len(archives) == 1
    assert archives[0].read_bytes().startswith(b"this is not a sqlite database")


def test_archiving_moves_the_stale_sqlite_sidecar_files(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("CREATE TABLE profiles (id INTEGER PRIMARY KEY, payload TEXT)")
        writer.execute("INSERT INTO profiles (payload) VALUES ('legacy row')")
        writer.commit()
        sidecar = db_path.with_name(f"{db_path.name}-wal")
        assert sidecar.exists()

        archived = StateStore(db_path).bootstrap()

        assert archived is not None
        assert archived.with_name(f"{archived.name}-wal").exists()
    finally:
        writer.close()


def test_archive_legacy_database_handles_the_three_states(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"
    assert archive_legacy_database(missing) is None

    platform_db = tmp_path / "platform.db"
    with StateStore(platform_db) as store:
        store.bootstrap()
    assert archive_legacy_database(platform_db) is None
    assert legacy_archives(platform_db) == []

    foreign_db = tmp_path / "foreign.db"
    build_unrelated_profiles_table(foreign_db)

    archived = archive_legacy_database(foreign_db)

    assert archived is not None
    assert archived.name.startswith("foreign.db.legacy-")
    assert LEGACY_NAME_PATTERN.search(archived.name)
    assert not foreign_db.exists()


def test_archive_name_is_a_utc_timestamp(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    build_unrelated_profiles_table(db_path)

    archived = StateStore(db_path).bootstrap()

    assert archived is not None
    timestamp = archived.name.removeprefix("state.db.legacy-")
    assert re.fullmatch(r"\d{8}T\d{6}Z", timestamp) is not None
    # Lexicographic order is chronological order for this timestamp layout.
    assert timestamp <= utc_now().strftime(LEGACY_TIMESTAMP_FORMAT)


def test_two_archives_never_overwrite_each_other(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    build_unrelated_profiles_table(db_path)
    first = StateStore(db_path).bootstrap()
    assert first is not None

    # Make the state path a foreign file again, dropping only the fresh database.
    for suffix in ("", "-wal", "-shm", "-journal"):
        leftover = db_path.with_name(f"{db_path.name}{suffix}")
        if leftover.exists():
            leftover.unlink()
    build_unrelated_profiles_table(db_path)

    second = StateStore(db_path).bootstrap()

    assert second is not None
    assert first.is_file()
    assert second.is_file()
    assert first != second
    assert {path.name for path in legacy_archives(db_path)} == {first.name, second.name}
