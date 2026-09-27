"""The SQLite state store: the single source of truth of a running platform.

One file, one writer, six tables (:data:`TABLES`), schema version
:data:`SCHEMA_VERSION` tracked with ``PRAGMA user_version``:

* ``profiles`` -- one row per profile: its declarative fields, its runtime state
  and the *generated* REST credentials of its worker. No exchange credential is
  ever stored here;
* ``profile_snapshots`` -- one minute-rounded measurement per profile and
  timestamp, replaced in place (``INSERT OR REPLACE`` on ``(profile_id, ts)``);
* ``profile_trades`` -- the trade read model of a profile, one row per
  ``(profile_id, trade_id)``, refreshed in place so an open trade becomes a
  closed one without ever being duplicated;
* ``profile_daily`` -- the per-day profit read model of a profile, one row per
  ``(profile_id, date)``, refreshed in place as the day unfolds;
* ``settings`` -- the persisted platform settings, JSON-encoded under the
  ``platform_settings`` key;
* ``events`` -- the engine journal read by ``GET /api/events``.

The store also owns the **legacy-database archive**. The previous product left
its own ``state.db`` in the ``trading-state`` volume; a foreign file is renamed
to ``state.db.legacy-<UTC timestamp>`` and a fresh database is created next to
it, so a deployment boots deterministically and the old file is never parsed,
never migrated and never overwritten. A database written by an **older revision
of this platform** is not foreign: it is migrated in place, because the schema
of every revision is additive. See :meth:`StateStore.bootstrap` and the
standalone :func:`archive_legacy_database`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, cast

from ..models import (
    MODE_LIVE,
    MODE_PAPER,
    PROFILE_SOURCES,
    STATE_STOPPED,
    Event,
    ProfileConfig,
    ProfileRecord,
    ProfileSnapshot,
    finite_float,
    format_ts,
    utc_now,
)

__all__ = [
    "EVENT_LEVELS",
    "LEGACY_SUFFIX",
    "MIGRATABLE_SCHEMA_VERSIONS",
    "PROFILE_COLUMNS",
    "PROFILE_DAILY_COLUMNS",
    "PROFILE_TRADE_COLUMNS",
    "PROFILE_UPDATE_FIELDS",
    "SCHEMA_VERSION",
    "TABLES",
    "ProfileDailyRecord",
    "ProfileTradeRecord",
    "StateStore",
    "archive_legacy_database",
]

#: The two trading modes of a profile, matching :class:`ProfileRecord`.
ProfileMode = Literal["paper", "live"]

#: Schema revision of the state database (``PRAGMA user_version``).
SCHEMA_VERSION = 2

#: Older revisions of **this platform** that :meth:`StateStore.bootstrap`
#: migrates in place instead of archiving: the schema of every revision is
#: additive, so a database written by revision 1 of this platform is completed
#: with the tables and the index it misses and re-stamped :data:`SCHEMA_VERSION`
#: -- it is never renamed to ``state.db.legacy-*`` and never loses a row.
MIGRATABLE_SCHEMA_VERSIONS: tuple[int, ...] = (1,)

#: The six tables the platform owns; anything else in the file is foreign.
TABLES: tuple[str, ...] = (
    "profiles",
    "profile_snapshots",
    "profile_trades",
    "profile_daily",
    "settings",
    "events",
)

#: Every column of the ``profiles`` table, in DDL order.
PROFILE_COLUMNS: tuple[str, ...] = (
    "id",
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
    "state",
    "state_reason",
    "api_port",
    "api_username",
    "api_password",
    "pid",
    "started_at",
    "last_error",
    "created_at",
    "updated_at",
)

#: Every column of the ``profile_trades`` table, in DDL order.
PROFILE_TRADE_COLUMNS: tuple[str, ...] = (
    "profile_id",
    "trade_id",
    "pair",
    "is_open",
    "open_date",
    "close_date",
    "amount",
    "open_rate",
    "close_rate",
    "stake_amount",
    "profit_abs",
    "profit_pct",
    "exit_reason",
    "updated_at",
)

#: Every column of the ``profile_daily`` table, in DDL order.
PROFILE_DAILY_COLUMNS: tuple[str, ...] = (
    "profile_id",
    "date",
    "abs_profit",
    "rel_profit",
    "starting_balance",
    "trade_count",
    "updated_at",
)

#: Event levels accepted by :meth:`StateStore.record_event`.
EVENT_LEVELS: tuple[str, ...] = ("info", "warning", "error")

#: Infix of an archived foreign database: ``state.db.legacy-20260927T173000Z``.
LEGACY_SUFFIX = ".legacy-"

#: Columns :meth:`StateStore.update_profile_fields` is allowed to write.
PROFILE_UPDATE_FIELDS: tuple[str, ...] = (
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

_LEGACY_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    name TEXT,
    strategy TEXT,
    timeframe TEXT,
    mode TEXT,
    exchange TEXT,
    pairs TEXT,
    initial_capital REAL,
    max_open_trades INTEGER,
    priority INTEGER,
    enabled INTEGER,
    source TEXT,
    state TEXT,
    state_reason TEXT,
    api_port INTEGER,
    api_username TEXT,
    api_password TEXT,
    pid INTEGER,
    started_at TEXT,
    last_error TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS profile_snapshots (
    profile_id TEXT,
    ts TEXT,
    portfolio_value REAL,
    cash REAL,
    positions_value REAL,
    profit_abs REAL,
    profit_pct REAL,
    realized_profit_abs REAL,
    unrealized_profit_abs REAL,
    open_trades INTEGER,
    closed_trades INTEGER,
    win_rate REAL,
    profit_factor REAL,
    max_drawdown_pct REAL,
    healthy INTEGER,
    PRIMARY KEY (profile_id, ts)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    profile_id TEXT,
    level TEXT,
    kind TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS profile_trades (
    profile_id TEXT NOT NULL,
    trade_id INTEGER NOT NULL,
    pair TEXT NOT NULL,
    is_open INTEGER NOT NULL DEFAULT 0,
    open_date TEXT,
    close_date TEXT,
    amount REAL,
    open_rate REAL,
    close_rate REAL,
    stake_amount REAL,
    profit_abs REAL,
    profit_pct REAL,
    exit_reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, trade_id)
);

CREATE TABLE IF NOT EXISTS profile_daily (
    profile_id TEXT NOT NULL,
    date TEXT NOT NULL,
    abs_profit REAL NOT NULL DEFAULT 0,
    rel_profit REAL NOT NULL DEFAULT 0,
    starting_balance REAL NOT NULL DEFAULT 0,
    trade_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, date)
);

CREATE INDEX IF NOT EXISTS idx_profile_snapshots_ts ON profile_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_profile_trades_open ON profile_trades(profile_id, is_open);
"""

_INSERT_PROFILE_SQL = """
INSERT INTO profiles (
    id, name, strategy, timeframe, mode, exchange, pairs, initial_capital,
    max_open_trades, priority, enabled, source, state, state_reason, api_port,
    api_username, api_password, pid, started_at, last_error, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_DECLARATIVE_SQL = """
UPDATE profiles SET
    name = ?, strategy = ?, timeframe = ?, mode = ?, exchange = ?, pairs = ?,
    initial_capital = ?, max_open_trades = ?, priority = ?, enabled = ?,
    source = ?,
    created_at = COALESCE(NULLIF(created_at, ''), ?),
    updated_at = ?
WHERE id = ?
"""

_INSERT_SNAPSHOT_SQL = """
INSERT OR REPLACE INTO profile_snapshots (
    profile_id, ts, portfolio_value, cash, positions_value, profit_abs,
    profit_pct, realized_profit_abs, unrealized_profit_abs, open_trades,
    closed_trades, win_rate, profit_factor, max_drawdown_pct, healthy
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_LATEST_SNAPSHOTS_SQL = """
SELECT snapshot.*
FROM profile_snapshots AS snapshot
JOIN (
    SELECT profile_id, MAX(ts) AS max_ts
    FROM profile_snapshots
    GROUP BY profile_id
) AS newest
    ON newest.profile_id = snapshot.profile_id AND newest.max_ts = snapshot.ts
ORDER BY snapshot.profile_id ASC
"""

_UPSERT_TRADE_SQL = """
INSERT INTO profile_trades (
    profile_id, trade_id, pair, is_open, open_date, close_date, amount,
    open_rate, close_rate, stake_amount, profit_abs, profit_pct, exit_reason,
    updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(profile_id, trade_id) DO UPDATE SET
    pair = excluded.pair,
    is_open = excluded.is_open,
    open_date = excluded.open_date,
    close_date = excluded.close_date,
    amount = excluded.amount,
    open_rate = excluded.open_rate,
    close_rate = excluded.close_rate,
    stake_amount = excluded.stake_amount,
    profit_abs = excluded.profit_abs,
    profit_pct = excluded.profit_pct,
    exit_reason = excluded.exit_reason,
    updated_at = excluded.updated_at
"""

#: The trades of a profile, newest ``open_date`` first; a trade without an
#: ``open_date`` is the oldest thing the store knows and comes last.
_TRADE_RECORDS_SQL = """
SELECT * FROM profile_trades
WHERE profile_id = ?
ORDER BY (open_date IS NULL) ASC, open_date DESC, trade_id DESC
"""

_UPSERT_DAILY_SQL = """
INSERT INTO profile_daily (
    profile_id, date, abs_profit, rel_profit, starting_balance, trade_count,
    updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(profile_id, date) DO UPDATE SET
    abs_profit = excluded.abs_profit,
    rel_profit = excluded.rel_profit,
    starting_balance = excluded.starting_balance,
    trade_count = excluded.trade_count,
    updated_at = excluded.updated_at
"""

#: The days of a profile, newest ``date`` first; the API layer reverses the list
#: when it wants the chronological order of a chart.
_DAILY_RECORDS_SQL = """
SELECT * FROM profile_daily
WHERE profile_id = ?
ORDER BY date DESC
"""


class _Unset:
    """Sentinel type of a keyword argument that was not passed at all."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return "UNSET"


#: Default of :meth:`StateStore.set_profile_runtime`: "leave this column alone".
#: Passing ``None`` explicitly clears the column instead.
UNSET = _Unset()


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProfileTradeRecord:
    """One trade of a profile, as stored in the ``profile_trades`` read model.

    ``trade_id`` is the freqtrade trade id of the worker; ``(profile_id,
    trade_id)`` is the primary key, so re-upserting a trade that just closed
    flips ``is_open`` in place instead of appending a second row.
    """

    profile_id: str
    trade_id: int
    pair: str = ""
    is_open: bool = False
    open_date: str | None = None
    close_date: str | None = None
    amount: float = 0.0
    open_rate: float = 0.0
    close_rate: float = 0.0
    stake_amount: float = 0.0
    profit_abs: float = 0.0
    profit_pct: float = 0.0
    exit_reason: str | None = None
    updated_at: str = ""


@dataclass(frozen=True)
class ProfileDailyRecord:
    """One day of trading of a profile, as stored in the ``profile_daily`` read model.

    ``date`` is the ``YYYY-MM-DD`` day reported by the worker and ``(profile_id,
    date)`` is the primary key, so the current day is refreshed in place as its
    numbers move.
    """

    profile_id: str
    date: str
    abs_profit: float = 0.0
    rel_profit: float = 0.0
    starting_balance: float = 0.0
    trade_count: int = 0
    updated_at: str = ""


def _encode_pairs(pairs: Sequence[Any] | str) -> str:
    """Encode the declarative pair list as the JSON array stored in the column."""
    if isinstance(pairs, str):
        return pairs
    return json.dumps([str(pair) for pair in pairs])


def _decode_pairs(value: Any) -> list[str]:
    """Decode the ``pairs`` column back into a list (tolerating a hand-edited row)."""
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except ValueError:
        return [text]
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


def _mode_from_row(value: Any) -> ProfileMode:
    """Return the stored trading mode, falling back to ``paper`` for anything else."""
    text = str(value or MODE_PAPER)
    return cast("ProfileMode", MODE_LIVE if text == MODE_LIVE else MODE_PAPER)


def _record_from_row(row: sqlite3.Row) -> ProfileRecord:
    """Build the model of a ``profiles`` row."""
    return ProfileRecord(
        id=str(row["id"]),
        name=str(row["name"] or ""),
        strategy=str(row["strategy"] or ""),
        timeframe=str(row["timeframe"] or "1h"),
        mode=_mode_from_row(row["mode"]),
        exchange=str(row["exchange"] or "binance"),
        pairs=_decode_pairs(row["pairs"]),
        initial_capital=finite_float(row["initial_capital"]),
        max_open_trades=int(row["max_open_trades"] or 0),
        priority=int(row["priority"] or 0),
        enabled=bool(row["enabled"]),
        source=str(row["source"] or "catalogue"),
        state=str(row["state"] or STATE_STOPPED),
        state_reason=row["state_reason"],
        api_port=int(row["api_port"] or 0),
        api_username=row["api_username"],
        api_password=row["api_password"],
        pid=row["pid"],
        started_at=row["started_at"],
        last_error=row["last_error"],
        created_at=str(row["created_at"] or format_ts()),
        updated_at=str(row["updated_at"] or format_ts()),
    )


def _snapshot_from_row(row: sqlite3.Row) -> ProfileSnapshot:
    """Build the model of a ``profile_snapshots`` row."""
    return ProfileSnapshot(
        profile_id=str(row["profile_id"]),
        ts=str(row["ts"]),
        portfolio_value=finite_float(row["portfolio_value"]),
        cash=finite_float(row["cash"]),
        positions_value=finite_float(row["positions_value"]),
        profit_abs=finite_float(row["profit_abs"]),
        profit_pct=finite_float(row["profit_pct"]),
        realized_profit_abs=finite_float(row["realized_profit_abs"]),
        unrealized_profit_abs=finite_float(row["unrealized_profit_abs"]),
        open_trades=int(row["open_trades"] or 0),
        closed_trades=int(row["closed_trades"] or 0),
        win_rate=finite_float(row["win_rate"]),
        profit_factor=finite_float(row["profit_factor"]),
        max_drawdown_pct=finite_float(row["max_drawdown_pct"]),
        healthy=bool(row["healthy"]),
    )


def _event_from_row(row: sqlite3.Row) -> Event:
    """Build the model of an ``events`` row."""
    return Event(
        id=int(row["id"]),
        ts=str(row["ts"]),
        profile_id=row["profile_id"],
        level=str(row["level"] or "info"),
        kind=str(row["kind"] or "generic"),
        message=str(row["message"] or ""),
    )


def _trade_record_from_row(row: sqlite3.Row) -> ProfileTradeRecord:
    """Build the record of a ``profile_trades`` row."""
    return ProfileTradeRecord(
        profile_id=str(row["profile_id"]),
        trade_id=int(row["trade_id"]),
        pair=str(row["pair"] or ""),
        is_open=bool(row["is_open"]),
        open_date=row["open_date"],
        close_date=row["close_date"],
        amount=finite_float(row["amount"]),
        open_rate=finite_float(row["open_rate"]),
        close_rate=finite_float(row["close_rate"]),
        stake_amount=finite_float(row["stake_amount"]),
        profit_abs=finite_float(row["profit_abs"]),
        profit_pct=finite_float(row["profit_pct"]),
        exit_reason=row["exit_reason"],
        updated_at=str(row["updated_at"]),
    )


def _daily_record_from_row(row: sqlite3.Row) -> ProfileDailyRecord:
    """Build the record of a ``profile_daily`` row."""
    return ProfileDailyRecord(
        profile_id=str(row["profile_id"]),
        date=str(row["date"]),
        abs_profit=finite_float(row["abs_profit"]),
        rel_profit=finite_float(row["rel_profit"]),
        starting_balance=finite_float(row["starting_balance"]),
        trade_count=int(row["trade_count"] or 0),
        updated_at=str(row["updated_at"]),
    )


# ---------------------------------------------------------------------------
# Foreign-database detection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _DatabaseInfo:
    """What a database file says about itself before it is touched."""

    readable: bool
    user_version: int
    tables: frozenset[str]
    columns: frozenset[str]

    @property
    def has_profiles_table(self) -> bool:
        """Whether the file already carries a ``profiles`` table."""
        return "profiles" in self.tables

    @property
    def has_profile_columns(self) -> bool:
        """Whether that ``profiles`` table carries the platform's columns."""
        return self.has_profiles_table and set(PROFILE_COLUMNS) <= self.columns

    @property
    def is_foreign(self) -> bool:
        """Whether the file belongs to the previous product or to a broken schema.

        A ``profiles`` table written by another product is foreign even when it
        happens to advertise a known revision, because a single ``CREATE TABLE IF
        NOT EXISTS`` statement would then leave it in place and every later write
        would fail. A file that cannot be parsed as SQLite at all is foreign too:
        it must not abort the boot, and it must not be silently overwritten
        either. A revision listed in :data:`MIGRATABLE_SCHEMA_VERSIONS` carrying
        the platform's own ``profiles`` columns is **not** foreign -- that is a
        database of an older release of this platform, and it is completed in
        place by :meth:`StateStore.bootstrap` instead of being archived.
        """
        if not self.readable:
            return True
        if not self.has_profiles_table:
            return False
        return (
            self.user_version not in (*MIGRATABLE_SCHEMA_VERSIONS, SCHEMA_VERSION)
            or not self.has_profile_columns
        )

    @property
    def is_own_schema(self) -> bool:
        """Whether the file already is a complete database of this revision."""
        return (
            self.readable
            and self.user_version == SCHEMA_VERSION
            and self.has_profile_columns
            and all(name in self.tables for name in TABLES)
        )


def _inspect_connection(connection: sqlite3.Connection) -> _DatabaseInfo:
    """Read ``user_version``, the table names and the profile columns of a connection."""
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    tables = frozenset(
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    )
    columns = frozenset(str(row[1]) for row in connection.execute("PRAGMA table_info(profiles)"))
    return _DatabaseInfo(
        readable=True,
        user_version=version,
        tables=tables,
        columns=columns,
    )


def _inspect_database(path: Path) -> _DatabaseInfo:
    """Read ``user_version``, the table names and the profile columns of ``path``."""
    unreadable = _DatabaseInfo(
        readable=False,
        user_version=0,
        tables=frozenset(),
        columns=frozenset(),
    )
    try:
        connection = sqlite3.connect(str(path))
    except sqlite3.Error:  # pragma: no cover - unreadable file
        return unreadable
    try:
        return _inspect_connection(connection)
    except (sqlite3.Error, TypeError, ValueError):
        return unreadable
    finally:
        connection.close()


def _archive_target(path: Path) -> Path:
    """Return the free archive name of ``path`` (``<name>.legacy-<stamp>``)."""
    stamp = utc_now().strftime(_LEGACY_TIMESTAMP_FORMAT)
    candidate = path.with_name(f"{path.name}{LEGACY_SUFFIX}{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}{LEGACY_SUFFIX}{stamp}-{counter}")
        counter += 1
    return candidate


def _move_legacy_database(path: Path) -> Path:
    """Rename ``path`` (and its SQLite sidecars) to a timestamped archive."""
    archive = _archive_target(path)
    path.rename(archive)
    # A stale ``-wal``/``-shm`` next to a fresh database would be replayed into
    # it by SQLite, so the sidecars follow the file they belong to.
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar = path.with_name(f"{path.name}{suffix}")
        if sidecar.exists():
            sidecar.rename(archive.with_name(f"{archive.name}{suffix}"))
    return archive


def archive_legacy_database(path: Path) -> Path | None:
    """Archive the database at ``path`` when it holds a foreign schema.

    Returns the archive path, or ``None`` when the file does not exist or
    already belongs to the platform, that is a ``profiles`` table with the
    platform's columns at ``PRAGMA user_version == SCHEMA_VERSION``. A database
    of an older revision of this platform is not foreign either -- see
    :data:`MIGRATABLE_SCHEMA_VERSIONS` -- so it is returned untouched and
    migrated in place by :meth:`StateStore.bootstrap`. Only a genuinely foreign
    file (another product, an unknown revision, a database without the
    platform's profile columns, or a file that is not SQLite at all) is moved
    aside.
    """
    target = Path(path)
    if not target.exists():
        return None
    if not _inspect_database(target).is_foreign:
        return None
    return _move_legacy_database(target)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class StateStore:
    """The SQLite file holding the profiles, their history, the events and the settings.

    Every public method makes sure the file is open and carries the platform
    schema, so a caller may either use the store as a context manager or call
    :meth:`bootstrap` once at boot::

        with StateStore(state_db) as store:
            store.bootstrap()
            store.upsert_profile(profile)

    One instance is shared by several threads: the API serves its requests from a
    thread pool while the poller writes a tick from the event loop. One
    ``sqlite3.Connection`` may not be used by two threads at the same time, so
    every statement -- read or write -- is issued under :attr:`_lock`. The lock
    is re-entrant and held for the duration of a single statement only, so the
    WAL journal still lets a reader and the writer of *other* connections work in
    parallel.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._conn: sqlite3.Connection | None = None
        self._ready = False
        #: Serialises the use of the shared connection across threads.
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        """Open the database file, creating its parent directory and schema when needed."""
        with self._lock:
            self._raw_connection()
            self._ensure_schema()

    def close(self) -> None:
        """Close the connection; a later call reopens the store lazily."""
        with self._lock:
            connection, self._conn = self._conn, None
            self._ready = False
            if connection is not None:
                connection.close()

    def __enter__(self) -> StateStore:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def bootstrap(self) -> Path | None:
        """Prepare the state database for this revision of the platform.

        A file that exists and carries a ``profiles`` table the platform does not
        own -- a ``PRAGMA user_version`` other than :data:`SCHEMA_VERSION` and
        other than a revision of :data:`MIGRATABLE_SCHEMA_VERSIONS`, or a table
        without the platform's columns -- is the state database of the product
        this one replaces: it is renamed to ``<name>.legacy-<UTC timestamp>`` in
        the same directory and a fresh database is created next to it. The
        archive path is returned in that case, ``None`` otherwise. A file that is
        not a SQLite database at all is archived the same way, so a corrupt
        leftover cannot abort the boot.

        A database written by an **older revision of this platform** is not
        archived: it is migrated in place -- the six tables and their indexes are
        created when missing, its existing rows are kept, and ``PRAGMA
        user_version`` is re-stamped :data:`SCHEMA_VERSION`.
        """
        with self._lock:
            self.close()
            archived: Path | None = None
            if self.path.exists() and _inspect_database(self.path).is_foreign:
                archived = _move_legacy_database(self.path)
            self._create_schema()
            if archived is not None:
                self.record_event(
                    "warning",
                    "legacy_db_archived",
                    f"archived the foreign state database as {archived.name}",
                )
            return archived

    # -- connection helpers ------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """Create the connection, its parent directory and the store pragmas."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            connection.execute("PRAGMA user_version")
        except sqlite3.Error:
            # The file is not a SQLite database. The connection is returned
            # as-is so that :meth:`_ensure_schema` archives the file and starts
            # a fresh one -- no journal pragma can be set on it in the meantime.
            return connection
        # One writer, many readers: WAL keeps a dashboard read from blocking the
        # poller, and the busy timeout absorbs the rare overlapping write.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _raw_connection(self) -> sqlite3.Connection:
        """Return the connection, opening it when necessary."""
        with self._lock:
            if self._conn is None:
                self._conn = self._connect()
            return self._conn

    def _connection(self) -> sqlite3.Connection:
        """Return the connection of a database that carries the platform schema."""
        with self._lock:
            self._raw_connection()
            self._ensure_schema()
            return self._raw_connection()

    def _ensure_schema(self) -> None:
        """Adopt a current schema, or bootstrap the file when it is not one."""
        with self._lock:
            if self._ready:
                return
            try:
                info: _DatabaseInfo | None = _inspect_connection(self._raw_connection())
            except sqlite3.Error:
                info = None
            if info is not None and info.is_own_schema:
                self._ready = True
                return
            self.bootstrap()

    def _create_schema(self) -> None:
        """Create the missing tables and indexes, then stamp the schema version."""
        with self._lock:
            connection = self._raw_connection()
            connection.executescript(_SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
            connection.commit()
            self._ready = True

    # -- statements --------------------------------------------------------
    def _fetch_all(self, statement: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection().execute(statement, params).fetchall()

    def _fetch_one(self, statement: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._connection().execute(statement, params).fetchone()

    def _write(self, statement: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            connection = self._connection()
            cursor = connection.execute(statement, params)
            connection.commit()
            return cursor

    # -- profiles ----------------------------------------------------------
    def list_profiles(self) -> list[ProfileRecord]:
        """Return every profile, ordered by ``priority`` DESC then ``id`` ASC."""
        rows = self._fetch_all(
            "SELECT * FROM profiles ORDER BY priority DESC, id ASC",
        )
        return [_record_from_row(row) for row in rows]

    def get_profile(self, profile_id: str) -> ProfileRecord | None:
        """Return the profile stored under ``profile_id``, or ``None``."""
        row = self._fetch_one("SELECT * FROM profiles WHERE id = ?", (str(profile_id),))
        return None if row is None else _record_from_row(row)

    def upsert_profile(
        self,
        profile: ProfileConfig,
        *,
        source: str = "catalogue",
        state: str = STATE_STOPPED,
        state_reason: str | None = None,
    ) -> ProfileRecord:
        """Create or refresh the declarative fields of ``profile``.

        Only the declared fields are written. An existing row keeps its state,
        its generated API credentials and its timestamps, and the columns that
        were never set are filled in; ``created_at`` is therefore stable across
        catalogue applies while ``updated_at`` always moves.
        """
        now = format_ts()
        existing = self.get_profile(profile.id)
        if existing is None:
            self._write(
                _INSERT_PROFILE_SQL,
                (
                    profile.id,
                    profile.name,
                    profile.strategy,
                    profile.timeframe,
                    profile.mode,
                    profile.exchange,
                    _encode_pairs(profile.pairs),
                    finite_float(profile.initial_capital),
                    int(profile.max_open_trades),
                    int(profile.priority),
                    int(bool(profile.enabled)),
                    str(source),
                    str(state),
                    state_reason,
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    now,
                    now,
                ),
            )
        else:
            self._write(
                _UPDATE_DECLARATIVE_SQL,
                (
                    profile.name,
                    profile.strategy,
                    profile.timeframe,
                    profile.mode,
                    profile.exchange,
                    _encode_pairs(profile.pairs),
                    finite_float(profile.initial_capital),
                    int(profile.max_open_trades),
                    int(profile.priority),
                    int(bool(profile.enabled)),
                    str(source),
                    now,
                    now,
                    profile.id,
                ),
            )
        record = self.get_profile(profile.id)
        if record is None:  # pragma: no cover - the row was just written
            raise KeyError(f"profile {profile.id!r} disappeared during the upsert")
        return record

    def update_profile_fields(
        self,
        profile_id: str,
        fields: Mapping[str, Any],
    ) -> ProfileRecord:
        """Write the allowed columns of ``fields`` on an existing profile.

        Raises :class:`KeyError` when ``profile_id`` is unknown and
        :class:`ValueError` when a field is not one of
        :data:`PROFILE_UPDATE_FIELDS`.
        """
        existing = self.get_profile(profile_id)
        if existing is None:
            raise KeyError(f"unknown profile id: {profile_id!r}")
        unknown = sorted(set(fields) - set(PROFILE_UPDATE_FIELDS))
        if unknown:
            allowed = ", ".join(PROFILE_UPDATE_FIELDS)
            raise ValueError(
                f"unknown profile field(s): {', '.join(unknown)}; allowed fields: {allowed}"
            )
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            values = [_encode_field(name, value) for name, value in fields.items()]
            values.append(format_ts())
            values.append(str(profile_id))
            self._write(
                f"UPDATE profiles SET {assignments}, updated_at = ? WHERE id = ?",
                values,
            )
        record = self.get_profile(profile_id)
        if record is None:  # pragma: no cover - the row was just written
            raise KeyError(f"unknown profile id: {profile_id!r}")
        return record

    def delete_profile(self, profile_id: str) -> bool:
        """Delete the profile and its trade and daily read models.

        ``True`` when a ``profiles`` row was removed. The two read models are
        cleared first so a deleted profile never leaves an orphan series behind;
        their own return values are deliberately ignored, because a profile
        without history is the normal case.
        """
        key = str(profile_id)
        self._write("DELETE FROM profile_trades WHERE profile_id = ?", (key,))
        self._write("DELETE FROM profile_daily WHERE profile_id = ?", (key,))
        cursor = self._write("DELETE FROM profiles WHERE id = ?", (key,))
        return cursor.rowcount > 0

    def set_profile_state(
        self,
        profile_id: str,
        state: str,
        reason: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """Record the worker state of a profile and why it is in it.

        ``reason`` and ``last_error`` are written as given, so starting a
        profile clears the stale reason of the previous run and an explicit
        failure clears an old error. An unknown id is a no-op.
        """
        self._write(
            "UPDATE profiles SET state = ?, state_reason = ?, last_error = ?, updated_at = ? "
            "WHERE id = ?",
            (str(state), reason, last_error, format_ts(), str(profile_id)),
        )

    def set_profile_runtime(
        self,
        profile_id: str,
        *,
        pid: int | None | _Unset = UNSET,
        api_port: int | None | _Unset = UNSET,
        started_at: str | None | _Unset = UNSET,
    ) -> None:
        """Write the runtime columns of a profile.

        An argument that is not passed at all is left alone; an argument passed
        as ``None`` clears the column, which is how a stopped worker releases
        its pid and its start timestamp. An unknown id is a no-op.
        """
        changes: dict[str, Any] = {}
        if not isinstance(pid, _Unset):
            changes["pid"] = pid
        if not isinstance(api_port, _Unset):
            changes["api_port"] = api_port
        if not isinstance(started_at, _Unset):
            changes["started_at"] = started_at
        if not changes:
            return
        assignments = ", ".join(f"{name} = ?" for name in changes)
        values: list[Any] = list(changes.values())
        values.append(format_ts())
        values.append(str(profile_id))
        self._write(
            f"UPDATE profiles SET {assignments}, updated_at = ? WHERE id = ?",
            values,
        )

    def set_api_credentials(self, profile_id: str, username: str, password: str) -> None:
        """Store the generated REST credentials of a profile's worker.

        These are the credentials the platform generates for its own worker, the
        only ones that ever reach this database; exchange keys stay in the
        environment. An unknown id is a no-op.
        """
        self._write(
            "UPDATE profiles SET api_username = ?, api_password = ?, updated_at = ? WHERE id = ?",
            (str(username), str(password), format_ts(), str(profile_id)),
        )

    def count_by_source(self) -> dict[str, int]:
        """Count the profiles per origin: ``catalogue`` and ``operator``."""
        counts = dict.fromkeys(PROFILE_SOURCES, 0)
        for row in self._fetch_all(
            "SELECT source, COUNT(*) AS total FROM profiles GROUP BY source"
        ):
            source = str(row["source"] or "")
            if source in counts:
                counts[source] = int(row["total"])
        return counts

    # -- snapshots ---------------------------------------------------------
    def record_snapshot(self, snapshot: ProfileSnapshot) -> None:
        """Write one measurement, replacing the row of the same profile and minute.

        The caller passes the already minute-rounded ``ts``; the
        ``(profile_id, ts)`` primary key turns a repeated poll inside the same
        minute into an update instead of a duplicate.
        """
        self._write(
            _INSERT_SNAPSHOT_SQL,
            (
                str(snapshot.profile_id),
                str(snapshot.ts),
                finite_float(snapshot.portfolio_value),
                finite_float(snapshot.cash),
                finite_float(snapshot.positions_value),
                finite_float(snapshot.profit_abs),
                finite_float(snapshot.profit_pct),
                finite_float(snapshot.realized_profit_abs),
                finite_float(snapshot.unrealized_profit_abs),
                int(snapshot.open_trades),
                int(snapshot.closed_trades),
                finite_float(snapshot.win_rate),
                finite_float(snapshot.profit_factor),
                finite_float(snapshot.max_drawdown_pct),
                int(bool(snapshot.healthy)),
            ),
        )

    def latest_snapshots(self) -> dict[str, ProfileSnapshot]:
        """Return the most recent snapshot of every profile that has one."""
        return {
            str(row["profile_id"]): _snapshot_from_row(row)
            for row in self._fetch_all(_LATEST_SNAPSHOTS_SQL)
        }

    def list_snapshots(
        self,
        profile_id: str,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[ProfileSnapshot]:
        """Return the snapshots of a profile, oldest first.

        ``since`` is inclusive. ``limit`` keeps the ``limit`` **most recent**
        rows of the window and still returns them oldest first, which is what an
        equity curve wants; a non-positive limit returns nothing.
        """
        statement = "SELECT * FROM profile_snapshots WHERE profile_id = ?"
        params: list[Any] = [str(profile_id)]
        if since is not None:
            statement += " AND ts >= ?"
            params.append(format_ts(since))
        if limit is None:
            statement += " ORDER BY ts ASC"
        else:
            params.append(max(int(limit), 0))
            statement = f"SELECT * FROM ({statement} ORDER BY ts DESC LIMIT ?) ORDER BY ts ASC"
        return [_snapshot_from_row(row) for row in self._fetch_all(statement, params)]

    def snapshot_series(
        self,
        *,
        since: datetime | None = None,
    ) -> dict[str, list[ProfileSnapshot]]:
        """Return the snapshots of every profile, grouped by profile id."""
        statement = "SELECT * FROM profile_snapshots"
        params: list[Any] = []
        if since is not None:
            statement += " WHERE ts >= ?"
            params.append(format_ts(since))
        statement += " ORDER BY profile_id ASC, ts ASC"
        series: dict[str, list[ProfileSnapshot]] = {}
        for row in self._fetch_all(statement, params):
            snapshot = _snapshot_from_row(row)
            series.setdefault(snapshot.profile_id, []).append(snapshot)
        return series

    def prune_snapshots(self, *, retention_days: int) -> int:
        """Delete the snapshots older than the retention window; return the count."""
        cutoff = format_ts(utc_now() - timedelta(days=int(retention_days)))
        cursor = self._write("DELETE FROM profile_snapshots WHERE ts < ?", (cutoff,))
        return max(int(cursor.rowcount), 0)

    # -- trade read model --------------------------------------------------
    def upsert_trade_records(self, records: Sequence[ProfileTradeRecord]) -> int:
        """Create or refresh the trades of ``records``, keyed by ``(profile_id, trade_id)``.

        One statement per record on the ``(profile_id, trade_id)`` primary key,
        so a trade first seen open is *flipped* in place when it closes instead
        of being appended a second time. ``updated_at`` is written as the record
        carries it and stamped with :func:`format_ts` when it is empty. Returns
        the number of rows written; an empty sequence writes nothing and returns
        ``0`` without touching the database.
        """
        rows = [
            (
                str(record.profile_id),
                int(record.trade_id),
                str(record.pair),
                int(bool(record.is_open)),
                record.open_date,
                record.close_date,
                finite_float(record.amount),
                finite_float(record.open_rate),
                finite_float(record.close_rate),
                finite_float(record.stake_amount),
                finite_float(record.profit_abs),
                finite_float(record.profit_pct),
                record.exit_reason,
                str(record.updated_at or format_ts()),
            )
            for record in records
        ]
        if not rows:
            return 0
        with self._lock:
            connection = self._connection()
            cursor = connection.executemany(_UPSERT_TRADE_SQL, rows)
            connection.commit()
            return max(int(cursor.rowcount), 0)

    def upsert_daily_records(self, records: Sequence[ProfileDailyRecord]) -> int:
        """Create or refresh the days of ``records``, keyed by ``(profile_id, date)``.

        Same discipline as :meth:`upsert_trade_records` on the ``(profile_id,
        date)`` primary key, so a day that is still unfolding is replaced in
        place as its numbers move. Returns the number of rows written; an empty
        sequence writes nothing and returns ``0``.
        """
        rows = [
            (
                str(record.profile_id),
                str(record.date),
                finite_float(record.abs_profit),
                finite_float(record.rel_profit),
                finite_float(record.starting_balance),
                int(record.trade_count),
                str(record.updated_at or format_ts()),
            )
            for record in records
        ]
        if not rows:
            return 0
        with self._lock:
            connection = self._connection()
            cursor = connection.executemany(_UPSERT_DAILY_SQL, rows)
            connection.commit()
            return max(int(cursor.rowcount), 0)

    def trade_records(self, profile_id: str) -> list[ProfileTradeRecord]:
        """Return every trade of a profile, newest ``open_date`` first.

        A row without an ``open_date`` is the oldest thing the store knows and
        therefore comes last; rows sharing an ``open_date`` are ordered by
        ``trade_id`` DESC. An unknown profile simply has no rows.
        """
        rows = self._fetch_all(_TRADE_RECORDS_SQL, (str(profile_id),))
        return [_trade_record_from_row(row) for row in rows]

    def daily_records(self, profile_id: str, limit: int | None = None) -> list[ProfileDailyRecord]:
        """Return the days of a profile, newest ``date`` first.

        ``limit`` keeps the ``limit`` most recent days; a non-positive limit
        returns nothing without querying. The API layer reverses the list when it
        wants the chronological order of a chart.
        """
        if limit is not None and int(limit) <= 0:
            return []
        statement = _DAILY_RECORDS_SQL
        params: list[Any] = [str(profile_id)]
        if limit is not None:
            statement += " LIMIT ?"
            params.append(int(limit))
        return [_daily_record_from_row(row) for row in self._fetch_all(statement, params)]

    # -- events ------------------------------------------------------------
    def record_event(
        self,
        level: str,
        kind: str,
        message: str,
        profile_id: str | None = None,
    ) -> Event:
        """Append one line to the engine journal and return it with its id."""
        ts = format_ts()
        resolved_level = str(level).strip() or EVENT_LEVELS[0]
        cursor = self._write(
            "INSERT INTO events (ts, profile_id, level, kind, message) VALUES (?, ?, ?, ?, ?)",
            (ts, profile_id, resolved_level, str(kind), str(message)),
        )
        return Event(
            id=int(cursor.lastrowid or 0),
            ts=ts,
            profile_id=profile_id,
            level=resolved_level,
            kind=str(kind),
            message=str(message),
        )

    def list_events(self, *, limit: int = 50, since: str | None = None) -> list[Event]:
        """Return the newest events first, optionally from ``since`` (inclusive)."""
        statement = "SELECT * FROM events"
        params: list[Any] = []
        if since is not None:
            statement += " WHERE ts >= ?"
            params.append(str(since))
        statement += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(int(limit), 0))
        return [_event_from_row(row) for row in self._fetch_all(statement, params)]

    # -- settings ----------------------------------------------------------
    def get_setting(self, key: str) -> str | None:
        """Return the stored value of ``key``, or ``None`` when it was never set."""
        row = self._fetch_one("SELECT value FROM settings WHERE key = ?", (str(key),))
        return None if row is None else row["value"]

    def set_setting(self, key: str, value: str) -> None:
        """Store ``value`` under ``key`` (an object is JSON-encoded)."""
        text = value if isinstance(value, str) else json.dumps(value)
        self._write(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (str(key), text, format_ts()),
        )

    def all_settings(self) -> dict[str, str]:
        """Return every stored setting, keyed by name."""
        return {
            str(row["key"]): str(row["value"] or "")
            for row in self._fetch_all("SELECT key, value FROM settings ORDER BY key ASC")
        }


def _encode_field(name: str, value: Any) -> Any:
    """Encode one value of :meth:`StateStore.update_profile_fields` for its column."""
    if name == "pairs":
        return _encode_pairs(value if isinstance(value, (str, list, tuple)) else [])
    if name == "enabled":
        return int(bool(value))
    return value
