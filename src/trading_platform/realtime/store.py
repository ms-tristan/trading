"""Persistent state of the realtime layer (delivery brief D7).

The whole platform keeps its state in **one SQLite file** whose default location is
``data/realtime/state.db`` (git-ignored).  The module is written with the standard
library only -- ``sqlite3``, ``json``, ``threading``, ``fcntl``, ``os`` -- so the
realtime engine gains durability without gaining a single third-party dependency
(delivery brief D2).

Three properties carry the restart safety required by D7:

* **idempotent writes** -- every table has a natural primary key and every insert is
  an ``INSERT ... ON CONFLICT(...) DO UPDATE``/``DO NOTHING``.  Replaying the very
  same fill, equity point or round-trip twice changes nothing and reports ``False``
  the second time, which is what lets the gateway re-submit nothing after a crash.
* **explicit schema version** -- the ``schema_version`` row is compared with
  :data:`SCHEMA_VERSION` on every boot.  A *newer* database (written by a more
  recent build) is refused with ``StateStoreError`` instead of being silently
  mangled; an older one is migrated.
* **single writer** -- :meth:`SqliteStateStore.initialize` takes an exclusive,
  non-blocking ``flock`` on ``<path>.lock``.  Two orchestrators over the same file
  is unsupported and fails loudly instead of interleaving transactions.

Threading model: one connection **per caller thread** (built lazily, tracked under a
lock and closed by :meth:`SqliteStateStore.close`).  SQLite handles are cheap; the
alternative -- sharing one connection between the asyncio runner thread and the HTTP
server thread -- is not.  Every write runs inside ``BEGIN IMMEDIATE ... COMMIT`` so no
half-written row ever survives an error.

Payloads: complex objects are stored as the JSON produced by their ``to_dict()``
counterpart and rebuilt with ``from_dict()`` (orders, fills, positions, equity
points, profile state, trades).  Profile specifications use the pydantic
``model_dump(mode="json")`` / ``model_validate`` pair.  Whenever a timestamp is part
of a natural key it is *also* written to an indexable ISO-8601 UTC ``TEXT`` column,
so ordering and lookups never depend on the JSON blob.

Candles are the one exception to that JSON rule: they are stored as plain numeric
columns (one row per profile and timestamp, see :class:`CandleRow`) and the store
keeps only the most recent :data:`CANDLE_WINDOW` rows of each profile, so the chart
surface has real price history without ever letting the database grow without bound.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

import pandas as pd

from trading_platform.config.models import ProfileConfig
from trading_platform.core.errors import StateStoreError
from trading_platform.core.models import TradeRecord
from trading_platform.realtime.clock import Clock, SystemClock
from trading_platform.realtime.models import (
    CandleEvent,
    EquityPoint,
    Fill,
    Order,
    Position,
    ProfileState,
    ProfileStatus,
    RunMode,
    status_error,
)

__all__ = ["CANDLE_WINDOW", "SCHEMA_VERSION", "CandleRow", "SqliteStateStore", "StateStore"]

logger = logging.getLogger(__name__)

#: Schema version written into the ``schema_version`` table by this build.
#:
#: History: ``1`` was the first shipped schema (profiles, orders, fills, positions,
#: equity, trades, status, meta); ``2`` adds the bounded ``candles`` table.
SCHEMA_VERSION: int = 2

#: How many candles the store keeps **per profile** (the bounded retention window).
#:
#: The live engine appends the candle it just processed on every tick, so without a
#: bound the database would grow by one row per profile and per timeframe for ever.
#: :meth:`SqliteStateStore.append_candle` therefore prunes, inside the very same
#: transaction as the insert, everything older than the most recent
#: ``CANDLE_WINDOW`` rows of the profile.
CANDLE_WINDOW: int = 1000

#: Prefix of the ``meta`` key holding the persisted :class:`ProfileState` payload.
_PROFILE_STATE_PREFIX = "profile_state:"

#: Prefix of the ``meta`` key holding the last processed candle of a profile.
_CANDLE_WATERMARK_PREFIX = "last_candle:"

_DDL: tuple[str, ...] = (
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
    (
        "CREATE TABLE IF NOT EXISTS candles ("
        "profile_id TEXT NOT NULL, timestamp TEXT NOT NULL, open REAL NOT NULL, "
        "high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, "
        "closed INTEGER NOT NULL, PRIMARY KEY(profile_id, timestamp))"
    ),
    "CREATE INDEX IF NOT EXISTS idx_candles_profile ON candles(profile_id, timestamp)",
)

_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandleRow:
    """One persisted candle of one profile (the row behind ``GET .../candles``)."""

    profile_id: str
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "profile_id": str(self.profile_id),
            "timestamp": pd.Timestamp(self.timestamp).isoformat(),
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": float(self.volume),
            "closed": bool(self.closed),
        }


def _utc_iso(value: pd.Timestamp | str) -> str:
    """Render ``value`` as an ISO-8601 UTC string.

    A naive timestamp is interpreted as UTC, an aware one is converted to UTC, so
    the produced text is directly comparable (and sortable) across processes.
    """
    moment = pd.Timestamp(value)
    moment = moment.tz_localize("UTC") if moment.tz is None else moment.tz_convert("UTC")
    return moment.isoformat()


def _parse_timestamp(value: Any, *, operation: str, key: str) -> pd.Timestamp | None:
    """Decode an ISO-8601 text column into a timestamp, or ``None``.

    Raises
    ------
    StateStoreError
        If the stored text is not a valid timestamp.
    """
    if value is None:
        return None
    try:
        return pd.Timestamp(str(value))
    except (TypeError, ValueError) as exc:
        raise StateStoreError(
            f"state store read failed ({operation}): invalid timestamp for {key}: {exc}"
        ) from exc


def _dumps(payload: Mapping[str, Any]) -> str:
    """Serialise a ``to_dict()`` payload into a stable JSON document."""
    return json.dumps(dict(payload), sort_keys=True)


def _decode(
    loader: Callable[[Mapping[str, Any]], _T],
    raw: Any,
    *,
    operation: str,
    key: str,
) -> _T:
    """Rebuild an object from a stored JSON payload.

    Raises
    ------
    StateStoreError
        If the payload is not valid JSON or does not match the model.
    """
    try:
        return loader(json.loads(str(raw)))
    except (KeyError, TypeError, ValueError) as exc:
        raise StateStoreError(
            f"state store read failed ({operation}): corrupted payload for {key}: {exc}"
        ) from exc


def _trade_key(profile_id: str, trade: TradeRecord) -> str:
    """Return the natural key of a round-trip trade.

    The key is derived from the profile and from the identifying fields of the
    trade, so appending the same round-trip twice is a no-op (D7).  It stays
    private to the store: callers never build it themselves.
    """
    return (
        f"{profile_id}|{pd.Timestamp(trade.entry_time).isoformat()}"
        f"|{pd.Timestamp(trade.exit_time).isoformat()}|{trade.direction.value}"
        f"|{float(trade.entry_price):.8f}"
    )


def _rollback(conn: sqlite3.Connection) -> None:
    """Roll a transaction back, ignoring a transaction that is already gone."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:  # pragma: no cover - a vanished transaction needs no rollback
        logger.debug("state store rollback skipped: no active transaction")


@contextmanager
def _transaction(conn: sqlite3.Connection, operation: str) -> Iterator[sqlite3.Connection]:
    """Run a block inside ``BEGIN IMMEDIATE ... COMMIT`` on ``conn``.

    Any failure rolls the transaction back and is re-raised as
    :class:`~trading_platform.core.errors.StateStoreError`, so no half-written row
    can survive and no ``sqlite3`` exception ever escapes the store boundary.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise StateStoreError(f"state store write failed ({operation}): {exc}") from exc
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException as exc:
        _rollback(conn)
        if isinstance(exc, sqlite3.Error):
            raise StateStoreError(f"state store write failed ({operation}): {exc}") from exc
        raise


# ---------------------------------------------------------------------------
# the seam
# ---------------------------------------------------------------------------


@runtime_checkable
class StateStore(Protocol):
    """Durable state seam of the realtime engine (implemented by SQLite).

    Every method is safe to call from the thread of the caller; an implementation
    is **not** required to be safe against another *process* touching the same
    file (see :class:`SqliteStateStore` and its writer lock).
    """

    def initialize(self) -> None:
        """Open the backing store and make it usable. Idempotent."""
        ...

    def close(self) -> None:
        """Release every resource held by the store. Idempotent."""
        ...

    def save_profile(self, spec: ProfileConfig) -> None:
        """Persist (or update) the configuration of one profile."""
        ...

    def load_profiles(self) -> list[ProfileConfig]:
        """Return every persisted profile, ordered by ``profile_id``."""
        ...

    def upsert_order(self, order: Order) -> None:
        """Insert or update one order, keyed by its ``client_order_id``."""
        ...

    def get_order(self, client_order_id: str) -> Order | None:
        """Return one order, or ``None`` when it is unknown."""
        ...

    def list_orders(self, profile_id: str, *, limit: int = 100) -> list[Order]:
        """Return the orders of a profile, most recent first."""
        ...

    def append_fill(self, fill: Fill) -> bool:
        """Append one fill; return ``False`` when it was already known."""
        ...

    def upsert_position(self, position: Position) -> None:
        """Insert or update the position of ``(profile_id, symbol)``."""
        ...

    def delete_position(self, profile_id: str, symbol: str) -> None:
        """Remove the position of ``(profile_id, symbol)``; idempotent."""
        ...

    def get_position(self, profile_id: str, symbol: str) -> Position | None:
        """Return one open position, or ``None``."""
        ...

    def list_positions(self, profile_id: str) -> list[Position]:
        """Return every open position of a profile, ordered by symbol."""
        ...

    def append_equity(self, point: EquityPoint) -> bool:
        """Append one equity point; return ``False`` when it was already known."""
        ...

    def equity_curve(self, profile_id: str) -> list[EquityPoint]:
        """Return the equity curve of a profile, oldest first."""
        ...

    def append_trade(self, trade: TradeRecord, *, profile_id: str) -> bool:
        """Append one round-trip; return ``False`` when it was already known."""
        ...

    def list_trades(self, profile_id: str, *, limit: int = 1000) -> list[TradeRecord]:
        """Return the round-trips of a profile, oldest exit first."""
        ...

    def save_status(self, profile_id: str, status: ProfileStatus, detail: str = "") -> None:
        """Persist the lifecycle status (and its detail) of one profile."""
        ...

    def load_status(self, profile_id: str) -> ProfileState | None:
        """Return the persisted state of a profile, or ``None`` when never written."""
        ...

    def get_meta(self, key: str) -> str | None:
        """Return one free-form key/value pair, or ``None``."""
        ...

    def set_meta(self, key: str, value: str) -> None:
        """Insert or update one free-form key/value pair."""
        ...

    def last_processed_candle(self, profile_id: str) -> pd.Timestamp | None:
        """Return the timestamp of the last candle processed by a profile."""
        ...

    def mark_candle_processed(self, profile_id: str, timestamp: pd.Timestamp) -> None:
        """Record a processed candle, keeping the maximum timestamp seen."""
        ...

    def append_candle(self, candle: CandleEvent, *, profile_id: str) -> bool:
        """Append one processed candle; return ``False`` when it was already known."""
        ...

    def candle_series(self, profile_id: str, limit: int = CANDLE_WINDOW) -> list[CandleRow]:
        """Return the candles of a profile, oldest first, at most ``limit`` of them."""
        ...

    def profile_state(self, profile_id: str) -> ProfileState:
        """Return the state of a profile, falling back to a stopped default."""
        ...


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------


class SqliteStateStore:
    """SQLite-backed :class:`StateStore` (standard library only).

    Parameters
    ----------
    path:
        Location of the database file.  Parent directories are created on
        :meth:`initialize`.
    clock:
        Time seam used for every ``updated_at``/``created_at`` audit column.
        Defaults to :class:`~trading_platform.realtime.clock.SystemClock`.

    Raises
    ------
    StateStoreError
        From :meth:`initialize` when the parent directory cannot be created, when
        the file is not a SQLite database, when it was written by a newer schema
        version, or when another process already holds the writer lock.
    """

    def __init__(
        self, path: str | Path = Path("data/realtime/state.db"), *, clock: Clock | None = None
    ):
        self._path = Path(path)
        self._clock: Clock = SystemClock() if clock is None else clock
        self._local = threading.local()
        self._connections: dict[int, tuple[threading.Thread, sqlite3.Connection]] = {}
        self._connections_lock = threading.Lock()
        self._initialized = False
        self._generation = 0
        self._lock_fd: int | None = None

    # -- introspection ------------------------------------------------------

    @property
    def path(self) -> Path:
        """Location of the SQLite file backing this store."""
        return self._path

    @property
    def connection_count(self) -> int:
        """Number of SQLite connections currently held open by this store.

        One per *live* caller thread: the value is bounded by the number of threads
        that use the store, and it is the health signal an operator needs when the
        store is read from a thread-per-request HTTP server (see
        :meth:`_reap_dead_threads`).
        """
        with self._connections_lock:
            return len(self._connections)

    @property
    def lock_path(self) -> Path:
        """Location of the single-writer lock file (``<path>.lock``)."""
        return Path(f"{self._path}.lock")

    def is_initialized(self) -> bool:
        """Return whether :meth:`initialize` succeeded and :meth:`close` was not called."""
        return self._initialized

    def __repr__(self) -> str:
        """Return a short, secret-free representation of the store."""
        return f"SqliteStateStore(path={str(self._path)!r}, initialized={self._initialized})"

    # -- lifecycle ----------------------------------------------------------

    def initialize(self) -> None:
        """Create the schema, run the version check and take the writer lock.

        Idempotent: calling it twice in a row is a no-op, and a store can be
        initialized again after :meth:`close`.

        Raises
        ------
        StateStoreError
            If the store cannot be opened, if the database uses a newer schema
            version, or if another process already holds the writer lock.
        """
        if self._initialized:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateStoreError(
                f"state store {self._path} cannot create its parent directory: {exc}"
            ) from exc
        self._acquire_lock()
        try:
            conn = self._new_connection()
            self._apply_pragmas(conn)
            # v1 -> v2 forward migration, additive only: ``_create_schema`` runs
            # first and every statement is ``CREATE TABLE/INDEX IF NOT EXISTS``, so a
            # database deployed at version 1 simply gains the empty ``candles`` table
            # (and its index) here, while every existing table, column and row is left
            # untouched; ``_check_schema_version`` then bumps the stored version to 2.
            self._create_schema(conn)
            self._check_schema_version(conn)
        except BaseException:
            self.close()
            raise
        self._initialized = True
        logger.debug("state store %s initialized (schema version %s)", self._path, SCHEMA_VERSION)

    def close(self) -> None:
        """Close every connection and release the writer lock (deletes nothing)."""
        with self._connections_lock:
            connections = [conn for _thread, conn in self._connections.values()]
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - a broken handle is already closed
                logger.debug("state store %s: error while closing a connection", self._path)
        self._local = threading.local()
        self._generation += 1
        self._initialized = False
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _acquire_lock(self) -> None:
        """Take the exclusive, non-blocking lock on ``<path>.lock``."""
        lock_path = self.lock_path
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            raise StateStoreError(
                f"state store {self._path} cannot create its lock file {lock_path}: {exc}"
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise StateStoreError(
                f"state store {self._path} is already locked by another process"
            ) from exc
        self._lock_fd = fd

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        """Enable WAL, referential integrity and a busy timeout on ``conn``."""
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error as exc:
            raise StateStoreError(f"state store {self._path} cannot be opened: {exc}") from exc

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        """Create every table and index (idempotent)."""
        try:
            for statement in _DDL:
                conn.execute(statement)
        except sqlite3.Error as exc:
            raise StateStoreError(
                f"state store {self._path} cannot create its schema: {exc}"
            ) from exc

    def _check_schema_version(self, conn: sqlite3.Connection) -> None:
        """Compare the stored schema version with :data:`SCHEMA_VERSION`."""
        try:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError(f"state store {self._path} cannot be opened: {exc}") from exc
        versions = [int(row[0]) for row in rows]
        if not versions:
            with _transaction(conn, "initialize"):
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            return
        stored = max(versions)
        if stored > SCHEMA_VERSION:
            raise StateStoreError(
                f"state database {self._path} uses schema version {stored}, "
                f"newer than the supported {SCHEMA_VERSION}"
            )
        if stored < SCHEMA_VERSION:
            logger.warning(
                "state store %s migrates schema version %s -> %s",
                self._path,
                stored,
                SCHEMA_VERSION,
            )
            with _transaction(conn, "initialize"):
                conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            return
        logger.debug("state store %s already uses schema version %s", self._path, stored)

    # -- connections --------------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        """Open a connection for the calling thread and track it for :meth:`close`."""
        conn = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        with self._connections_lock:
            self._reap_dead_threads()
            self._connections[threading.get_ident()] = (threading.current_thread(), conn)
        self._local.connection = conn
        self._local.generation = self._generation
        return conn

    def _reap_dead_threads(self) -> None:
        """Close the connections of threads that have ended (caller holds the lock).

        The store hands one connection per *caller thread*, which is the right model
        for a process with a handful of long-lived threads.  It is the wrong model
        for the monitoring server: :class:`http.server.ThreadingHTTPServer` runs one
        thread per request, so a browser polling the dashboard every two seconds
        opened a fresh connection per poll and every one of them was retained for
        the life of the process.  The container ran out of file descriptors after
        ~500 requests (``OSError: Too many open files``) and the dashboard answered
        ``500`` to every request.

        Reaping the connections of finished threads keeps the retained set bounded
        by the number of *live* threads, which is what the docstring always
        promised, and never closes a connection another live thread is using.
        """
        if not self._connections:
            return
        current = threading.get_ident()
        for ident, (thread, conn) in list(self._connections.items()):
            if ident == current or thread.is_alive():
                continue
            self._connections.pop(ident, None)
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - a broken handle is already closed
                logger.debug(
                    "state store %s: error while closing the connection of a dead thread",
                    self._path,
                )

    def _require_initialized(self) -> None:
        """Raise :class:`StateStoreError` when the store was not initialized."""
        if not self._initialized:
            raise StateStoreError(
                f"state store {self._path} is not initialized (call initialize() first)"
            )

    def _connection(self) -> sqlite3.Connection:
        """Return the connection of the calling thread, building it lazily."""
        self._require_initialized()
        conn: sqlite3.Connection | None = getattr(self._local, "connection", None)
        generation: int = getattr(self._local, "generation", -1)
        if conn is None or generation != self._generation:
            conn = self._new_connection()
        else:
            # Reap on *every* lookup, not only when a new connection is created: a
            # thread that only ever reuses its own connection (the main thread of a
            # quiet process, or ``realtime run``'s event loop) would otherwise keep
            # the connections of every thread it outlives.  The cost is one short
            # lock and a walk over a mapping sized by the number of live threads,
            # which is exactly what ``connection_count`` reports.
            with self._connections_lock:
                self._reap_dead_threads()
        return conn

    @contextmanager
    def _write(self, operation: str) -> Iterator[sqlite3.Connection]:
        """Run a write block in an explicit transaction."""
        conn = self._connection()
        with _transaction(conn, operation):
            yield conn

    def _fetchall(self, operation: str, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Run a read query and return every row."""
        conn = self._connection()
        try:
            return list(conn.execute(sql, params).fetchall())
        except sqlite3.Error as exc:
            raise StateStoreError(f"state store read failed ({operation}): {exc}") from exc

    def _fetchone(self, operation: str, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        """Run a read query and return at most one row."""
        conn = self._connection()
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError(f"state store read failed ({operation}): {exc}") from exc
        return None if row is None else row

    def _now_iso(self) -> str:
        """Return the current instant of the injected clock as ISO-8601 UTC."""
        return self._clock.now().isoformat()

    # -- profiles -----------------------------------------------------------

    def save_profile(self, spec: ProfileConfig) -> None:
        """Persist (or update) the configuration of one profile."""
        payload = _dumps(spec.model_dump(mode="json"))
        now = self._now_iso()
        with self._write("save_profile") as conn:
            conn.execute(
                "INSERT INTO profiles (profile_id, payload, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(profile_id) DO UPDATE SET "
                "payload = excluded.payload, updated_at = excluded.updated_at",
                (spec.id, payload, now),
            )

    def load_profiles(self) -> list[ProfileConfig]:
        """Return every persisted profile, ordered by ``profile_id``."""
        rows = self._fetchall(
            "load_profiles", "SELECT profile_id, payload FROM profiles ORDER BY profile_id ASC"
        )
        profiles: list[ProfileConfig] = []
        for row in rows:
            profile_id = str(row["profile_id"])
            try:
                profiles.append(ProfileConfig.model_validate(json.loads(str(row["payload"]))))
            except (KeyError, TypeError, ValueError) as exc:
                raise StateStoreError(
                    f"state store read failed (load_profiles): corrupted payload for "
                    f"{profile_id}: {exc}"
                ) from exc
        return profiles

    # -- orders -------------------------------------------------------------

    def upsert_order(self, order: Order) -> None:
        """Insert or update one order, keyed by its ``client_order_id``."""
        payload = _dumps(order.to_dict())
        created_at = _utc_iso(order.created_at)
        updated_at = _utc_iso(order.updated_at)
        with self._write("upsert_order") as conn:
            conn.execute(
                "INSERT INTO orders (client_order_id, profile_id, symbol, state, payload, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(client_order_id) DO UPDATE SET "
                "profile_id = excluded.profile_id, symbol = excluded.symbol, "
                "state = excluded.state, payload = excluded.payload, "
                "created_at = excluded.created_at, updated_at = excluded.updated_at",
                (
                    order.client_order_id,
                    order.profile_id,
                    order.symbol,
                    order.state.value,
                    payload,
                    created_at,
                    updated_at,
                ),
            )

    def get_order(self, client_order_id: str) -> Order | None:
        """Return one order, or ``None`` when it is unknown."""
        row = self._fetchone(
            "get_order",
            "SELECT payload FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        )
        if row is None:
            return None
        return _decode(Order.from_dict, row["payload"], operation="get_order", key=client_order_id)

    def list_orders(self, profile_id: str, *, limit: int = 100) -> list[Order]:
        """Return the orders of a profile, most recent first."""
        if limit <= 0:
            return []
        rows = self._fetchall(
            "list_orders",
            "SELECT payload, client_order_id FROM orders WHERE profile_id = ? "
            "ORDER BY created_at DESC, client_order_id DESC LIMIT ?",
            (profile_id, int(limit)),
        )
        return [
            _decode(
                Order.from_dict,
                row["payload"],
                operation="list_orders",
                key=str(row["client_order_id"]),
            )
            for row in rows
        ]

    # -- fills --------------------------------------------------------------

    def append_fill(self, fill: Fill) -> bool:
        """Append one fill; return ``False`` when it was already known."""
        payload = _dumps(fill.to_dict())
        with self._write("append_fill") as conn:
            cursor = conn.execute(
                "INSERT INTO fills (fill_id, client_order_id, profile_id, payload, timestamp) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(fill_id) DO NOTHING",
                (
                    fill.fill_id,
                    fill.client_order_id,
                    fill.profile_id,
                    payload,
                    _utc_iso(fill.timestamp),
                ),
            )
            return cursor.rowcount > 0

    # -- positions ----------------------------------------------------------

    def upsert_position(self, position: Position) -> None:
        """Insert or update the position of ``(profile_id, symbol)``."""
        payload = _dumps(position.to_dict())
        with self._write("upsert_position") as conn:
            conn.execute(
                "INSERT INTO positions (profile_id, symbol, payload, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(profile_id, symbol) DO UPDATE SET "
                "payload = excluded.payload, updated_at = excluded.updated_at",
                (
                    position.profile_id,
                    position.symbol,
                    payload,
                    _utc_iso(position.updated_at),
                ),
            )

    def delete_position(self, profile_id: str, symbol: str) -> None:
        """Remove the position of ``(profile_id, symbol)``; idempotent."""
        with self._write("delete_position") as conn:
            conn.execute(
                "DELETE FROM positions WHERE profile_id = ? AND symbol = ?",
                (profile_id, symbol),
            )

    def get_position(self, profile_id: str, symbol: str) -> Position | None:
        """Return one open position, or ``None``."""
        row = self._fetchone(
            "get_position",
            "SELECT payload FROM positions WHERE profile_id = ? AND symbol = ?",
            (profile_id, symbol),
        )
        if row is None:
            return None
        return _decode(
            Position.from_dict,
            row["payload"],
            operation="get_position",
            key=f"{profile_id}/{symbol}",
        )

    def list_positions(self, profile_id: str) -> list[Position]:
        """Return every open position of a profile, ordered by symbol."""
        rows = self._fetchall(
            "list_positions",
            "SELECT payload, symbol FROM positions WHERE profile_id = ? ORDER BY symbol ASC",
            (profile_id,),
        )
        return [
            _decode(
                Position.from_dict,
                row["payload"],
                operation="list_positions",
                key=str(row["symbol"]),
            )
            for row in rows
        ]

    # -- equity -------------------------------------------------------------

    def append_equity(self, point: EquityPoint) -> bool:
        """Append one equity point; return ``False`` when it was already known."""
        payload = _dumps(point.to_dict())
        with self._write("append_equity") as conn:
            cursor = conn.execute(
                "INSERT INTO equity (profile_id, timestamp, equity, payload) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(profile_id, timestamp) DO NOTHING",
                (point.profile_id, _utc_iso(point.timestamp), float(point.equity), payload),
            )
            return cursor.rowcount > 0

    def equity_curve(self, profile_id: str) -> list[EquityPoint]:
        """Return the equity curve of a profile, oldest first."""
        rows = self._fetchall(
            "equity_curve",
            "SELECT payload, timestamp FROM equity WHERE profile_id = ? ORDER BY timestamp ASC",
            (profile_id,),
        )
        return [
            _decode(
                EquityPoint.from_dict,
                row["payload"],
                operation="equity_curve",
                key=str(row["timestamp"]),
            )
            for row in rows
        ]

    # -- trades -------------------------------------------------------------

    def append_trade(self, trade: TradeRecord, *, profile_id: str) -> bool:
        """Append one round-trip; return ``False`` when it was already known."""
        key = _trade_key(profile_id, trade)
        payload = _dumps(trade.to_dict())
        with self._write("append_trade") as conn:
            cursor = conn.execute(
                "INSERT INTO trades (profile_id, trade_id, payload, exit_time) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(profile_id, trade_id) DO NOTHING",
                (profile_id, key, payload, _utc_iso(trade.exit_time)),
            )
            return cursor.rowcount > 0

    def list_trades(self, profile_id: str, *, limit: int = 1000) -> list[TradeRecord]:
        """Return the round-trips of a profile, oldest exit first."""
        if limit <= 0:
            return []
        rows = self._fetchall(
            "list_trades",
            "SELECT payload, trade_id FROM trades WHERE profile_id = ? "
            "ORDER BY exit_time ASC, trade_id ASC LIMIT ?",
            (profile_id, int(limit)),
        )
        return [
            _decode(
                TradeRecord.from_dict,
                row["payload"],
                operation="list_trades",
                key=str(row["trade_id"]),
            )
            for row in rows
        ]

    # -- status / profile state ---------------------------------------------

    def save_status(self, profile_id: str, status: ProfileStatus, detail: str = "") -> None:
        """Persist the lifecycle status (and its detail) of one profile.

        The row of the ``status`` table carries the status, its human-readable
        detail, the last processed candle and the update instant.  The complete
        :class:`ProfileState` payload (mode, lag, last error, reconnect count) is
        kept in the ``meta`` table under ``profile_state:<profile_id>`` so that a
        restart restores the whole health block and not only its status.

        ``detail`` describes the *status* (``"running"``, ``"last candle <ts>"``)
        and never becomes the last error: only :attr:`ProfileStatus.ERROR` sets one,
        a healthy status (:attr:`ProfileStatus.STARTING` /
        :attr:`ProfileStatus.RUNNING`) clears it, and any other status keeps
        whatever is already there.  Storing the detail as the error is what made a
        perfectly healthy profile report ``last error: last candle 2024-01-05…`` --
        or, after a restart, the reason its *previous* process stopped.
        """
        status_value = ProfileStatus(status)
        text = str(detail)
        now = self._now_iso()
        watermark = self.get_meta(_CANDLE_WATERMARK_PREFIX + profile_id)
        previous = self.load_status(profile_id)
        last_candle_at = _parse_timestamp(
            watermark, operation="save_status", key=f"last_candle:{profile_id}"
        )
        if last_candle_at is None and previous is not None:
            last_candle_at = previous.last_candle_at
        state = ProfileState(
            profile_id=profile_id,
            status=status_value,
            mode=previous.mode if previous is not None else self._profile_mode(profile_id),
            last_candle_at=last_candle_at,
            lag_seconds=previous.lag_seconds if previous is not None else 0.0,
            last_error=status_error(status_value, text, previous.last_error if previous else None),
            reconnect_count=previous.reconnect_count if previous is not None else 0,
            updated_at=pd.Timestamp(now),
        )
        with self._write("save_status") as conn:
            conn.execute(
                "INSERT INTO status (profile_id, status, detail, last_candle_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(profile_id) DO UPDATE SET "
                "status = excluded.status, detail = excluded.detail, "
                "last_candle_at = excluded.last_candle_at, updated_at = excluded.updated_at",
                (profile_id, status_value.value, text, _iso_or_none(last_candle_at), now),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_PROFILE_STATE_PREFIX + profile_id, _dumps(state.to_dict())),
            )

    def load_status(self, profile_id: str) -> ProfileState | None:
        """Return the persisted state of a profile, or ``None`` when never written."""
        row = self._fetchone(
            "load_status",
            "SELECT status, last_candle_at, updated_at FROM status WHERE profile_id = ?",
            (profile_id,),
        )
        raw = self.get_meta(_PROFILE_STATE_PREFIX + profile_id)
        if row is None and raw is None:
            return None
        if raw is not None:
            state = _decode(
                ProfileState.from_dict,
                raw,
                operation="load_status",
                key=profile_id,
            )
        else:
            state = ProfileState(
                profile_id=profile_id,
                status=ProfileStatus.STOPPED,
                mode=self._profile_mode(profile_id),
                updated_at=None,
            )
        if row is None:
            return state
        try:
            status = ProfileStatus(str(row["status"]))
        except ValueError as exc:
            raise StateStoreError(
                f"state store read failed (load_status): invalid status for {profile_id}: {exc}"
            ) from exc
        last_candle_at = _parse_timestamp(
            row["last_candle_at"], operation="load_status", key=f"last_candle:{profile_id}"
        )
        updated_at = _parse_timestamp(
            row["updated_at"], operation="load_status", key=f"updated_at:{profile_id}"
        )
        return replace(
            state,
            status=status,
            last_candle_at=last_candle_at if last_candle_at is not None else state.last_candle_at,
            updated_at=updated_at if updated_at is not None else state.updated_at,
        )

    def profile_state(self, profile_id: str) -> ProfileState:
        """Return the state of a profile, falling back to a stopped default.

        An unknown profile never raises: it answers
        ``ProfileState(status=STOPPED, mode=PAPER, updated_at=None)`` so the read
        model can render a profile whose state was never persisted.
        """
        state = self.load_status(profile_id)
        if state is not None:
            return state
        return ProfileState(
            profile_id=profile_id,
            status=ProfileStatus.STOPPED,
            mode=RunMode.PAPER,
            updated_at=None,
        )

    def _profile_mode(self, profile_id: str) -> RunMode:
        """Return the run mode recorded in the saved configuration, else ``PAPER``."""
        row = self._fetchone(
            "profile_mode",
            "SELECT payload FROM profiles WHERE profile_id = ?",
            (profile_id,),
        )
        if row is None:
            return RunMode.PAPER
        try:
            spec = ProfileConfig.model_validate(json.loads(str(row["payload"])))
        except (KeyError, TypeError, ValueError):
            return RunMode.PAPER
        try:
            return RunMode(spec.mode)
        except ValueError:  # pragma: no cover - ProfileConfig constrains mode to paper/live
            return RunMode.PAPER

    # -- meta / candle watermark --------------------------------------------

    def get_meta(self, key: str) -> str | None:
        """Return one free-form key/value pair, or ``None``."""
        row = self._fetchone("get_meta", "SELECT value FROM meta WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        """Insert or update one free-form key/value pair."""
        with self._write("set_meta") as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def last_processed_candle(self, profile_id: str) -> pd.Timestamp | None:
        """Return the timestamp of the last candle processed by a profile."""
        raw = self.get_meta(_CANDLE_WATERMARK_PREFIX + profile_id)
        return _parse_timestamp(raw, operation="last_processed_candle", key=profile_id)

    def mark_candle_processed(self, profile_id: str, timestamp: pd.Timestamp) -> None:
        """Record a processed candle, keeping the maximum timestamp seen.

        The watermark is what lets a restart resume without replaying or skipping
        a candle (D7); keeping the maximum makes the call idempotent and immune to
        an out-of-order replay.
        """
        stamped = _utc_iso(timestamp)
        key = _CANDLE_WATERMARK_PREFIX + profile_id
        with self._write("mark_candle_processed") as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            if row is not None and str(row["value"]) >= stamped:
                return
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, stamped),
            )

    # -- candles ------------------------------------------------------------

    def append_candle(self, candle: CandleEvent, *, profile_id: str) -> bool:
        """Append the candle a profile just processed; ``False`` when it was known.

        One row per ``(profile_id, timestamp)``, carrying the OHLCV values and the
        ``closed`` flag.  Re-appending the same candle refreshes its values in place
        (so a forming candle replayed by a restart converges on its final values) and
        reports ``False``, exactly like the other idempotent writers of this store.

        The insert and the prune of everything older than the most recent
        :data:`CANDLE_WINDOW` rows of the profile share **one** transaction, so the
        table can never grow without bound and can never be observed un-pruned.  A
        profile is the retention unit: pruning one never touches another.
        """
        timestamp = _utc_iso(candle.timestamp)
        with self._write("append_candle") as conn:
            known = (
                conn.execute(
                    "SELECT 1 FROM candles WHERE profile_id = ? AND timestamp = ?",
                    (profile_id, timestamp),
                ).fetchone()
                is not None
            )
            conn.execute(
                "INSERT INTO candles "
                "(profile_id, timestamp, open, high, low, close, volume, closed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(profile_id, timestamp) DO UPDATE SET "
                "open = excluded.open, high = excluded.high, low = excluded.low, "
                "close = excluded.close, volume = excluded.volume, closed = excluded.closed",
                (
                    profile_id,
                    timestamp,
                    float(candle.open),
                    float(candle.high),
                    float(candle.low),
                    float(candle.close),
                    float(candle.volume),
                    int(bool(candle.closed)),
                ),
            )
            conn.execute(
                "DELETE FROM candles WHERE profile_id = ? AND timestamp NOT IN "
                "(SELECT timestamp FROM candles WHERE profile_id = ? "
                "ORDER BY timestamp DESC LIMIT ?)",
                (profile_id, profile_id, int(CANDLE_WINDOW)),
            )
            return not known

    def candle_series(self, profile_id: str, limit: int = CANDLE_WINDOW) -> list[CandleRow]:
        """Return the persisted candles of a profile, **oldest first**.

        The read queries the most recent ``limit`` rows (the cheapest plan on the
        ``(profile_id, timestamp)`` key) and reverses them, so the caller always gets
        a chronology it can plot directly.  A non-positive ``limit`` and an unknown
        profile both answer ``[]``.
        """
        if limit <= 0:
            return []
        rows = self._fetchall(
            "candle_series",
            "SELECT profile_id, timestamp, open, high, low, close, volume, closed "
            "FROM candles WHERE profile_id = ? ORDER BY timestamp DESC LIMIT ?",
            (profile_id, int(limit)),
        )
        series: list[CandleRow] = []
        for row in rows:
            timestamp = _parse_timestamp(
                row["timestamp"],
                operation="candle_series",
                key=f"{profile_id}:{row['timestamp']}",
            )
            if timestamp is None:  # pragma: no cover - the column is NOT NULL
                raise StateStoreError(
                    f"state store read failed (candle_series): missing timestamp for {profile_id}"
                )
            series.append(
                CandleRow(
                    profile_id=str(row["profile_id"]),
                    timestamp=timestamp,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    closed=bool(row["closed"]),
                )
            )
        series.reverse()
        return series


def _iso_or_none(value: pd.Timestamp | None) -> str | None:
    """Render an optional timestamp as ISO-8601 UTC text."""
    return None if value is None else _utc_iso(value)
