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

The shared platform wallet is the second exception.  It is a row of the ``wallet``
table holding the USDT cash every profile of one **run mode** funds its orders from,
together with the initial balance that ledger was started with.  Schema version ``5``
makes the table **one ledger per mode**, and the mapping from a mode to its row id is
written down here and nowhere else:

* :data:`WALLET_ID_PAPER` = ``1`` -- the **paper** ledger
  (:attr:`~trading_platform.realtime.models.RunMode.PAPER`), the local simulated cash
  the paper broker mutates;
* :data:`WALLET_ID_LIVE` = ``2`` -- the **live** ledger
  (:attr:`~trading_platform.realtime.models.RunMode.LIVE`), the read-only mirror of a
  real venue account.

The ids are *stable*: ``1`` is the id the single-row table of schema version ``3``
already used, so the ``4 -> 5`` migration keeps a legacy row **as the paper ledger**
instead of losing it.  Both ledgers are written through
:meth:`SqliteStateStore.save_wallet` -- an ``INSERT ... ON CONFLICT(wallet_id) DO
UPDATE`` on the resolved id, so re-saving a ledger can never create a duplicate row --
and read back through :meth:`SqliteStateStore.load_wallet`, which answers ``None`` for
a mode whose ledger holds no row.  Schema version ``3`` added that table; the
migration is additive, so a database deployed at version ``1`` or ``2`` simply gains
the empty ``wallet`` table and the ledgers are initialised from the configuration at
the next boot.

Schema version ``4`` is the **first non-additive step** of the store: it rewrites the
``profiles`` payloads and drops exactly the top-level keys the *current*
:class:`~trading_platform.config.models.ProfileConfig` does not declare (see
:meth:`SqliteStateStore._migrate_profiles_payload`).  The step is driven by
``ProfileConfig.model_fields`` and deliberately **not** by a hard-coded list of
removed names, because the declared field set of the current model *is* the contract
a payload has to satisfy: the next release that removes a field prunes the persisted
rows carrying it during its own boot, with no migration ever written against the
name of the field it removes (the release that removed ``forecast`` shipped none, and
a stale row then bricked the platform).  The rewrite touches a row only when that row
actually carries an unknown key, so a clean database -- the live one was repaired by
hand -- migrates without a single row write, and a payload that is not valid JSON is
left byte for byte untouched for the read path to report.

Schema version ``5`` is the **second non-additive step**: the ``wallet`` table stops
being a single-row table and becomes **one ledger per mode**, keyed by the stable row
ids :data:`WALLET_ID_PAPER` (``1``) and :data:`WALLET_ID_LIVE` (``2``).
``CREATE TABLE IF NOT EXISTS`` cannot widen the ``CHECK (wallet_id = 1)`` constraint
of an already-deployed file, so the table is rebuilt -- ``wallet_new`` ->
``INSERT INTO wallet_new SELECT ...`` -> ``DROP TABLE wallet`` -> ``RENAME`` -- by
:meth:`SqliteStateStore._migrate_wallet_modes`.  The rebuild is a **migration, never a
reset**: the legacy row is ``wallet_id = 1``, which *is* the paper id, so the ledger
the previous build held survives as the **paper ledger** with its cash and its initial
balance intact, and the live ledger simply has no row until a venue balance is
mirrored into it.

Reading follows from the same principle: **one bad row must never take the platform
down again**.  :meth:`SqliteStateStore.load_profiles` is tolerant by default -- it
skips a row it cannot decode or validate, logs the *sanitised* reason at ``WARNING``
and keeps loading every other profile -- and the failures it saw are readable through
:meth:`SqliteStateStore.load_profile_failures`, which the monitoring read model
publishes so an unreadable profile can never again be invisible.  A caller that
genuinely wants validation to fail loudly asks for the strict read
(``load_profiles(strict=True)``), which restores the previous raising behaviour.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
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

__all__ = [
    "CANDLE_WINDOW",
    "SCHEMA_VERSION",
    "WALLET_ID_LIVE",
    "WALLET_ID_PAPER",
    "CandleRow",
    "SqliteStateStore",
    "StateStore",
    "WalletRow",
    "redact_profile_error",
]

logger = logging.getLogger(__name__)

#: Stable row id of the **paper** ledger in the ``wallet`` table.
#:
#: ``1`` is the id the single-row ``wallet`` table of schema version ``3`` already
#: used, so the ``4 -> 5`` migration keeps the legacy row as this very ledger rather
#: than losing it.  Together with :data:`WALLET_ID_LIVE` this pair is the **only**
#: place the mode -> row id mapping is written down; every reader and writer of the
#: table resolves its id through :meth:`SqliteStateStore._wallet_id`.
WALLET_ID_PAPER: int = 1

#: Stable row id of the **live** ledger in the ``wallet`` table.
#:
#: The live ledger is the read-only mirror of a real venue account; it holds no row
#: until a venue balance has been mirrored into it, and that absence is what makes
#: ``load_wallet(mode="live")`` answer ``None`` instead of an invented ``0.0``.
WALLET_ID_LIVE: int = 2

#: Schema version written into the ``schema_version`` table by this build.
#:
#: History: ``1`` was the first shipped schema (profiles, orders, fills, positions,
#: equity, trades, status, meta); ``2`` adds the bounded ``candles`` table; ``3``
#: adds the single-row ``wallet`` table (the shared platform wallet every profile
#: funds its orders from); ``4`` removes from every ``profiles`` payload the
#: top-level keys the current :class:`~trading_platform.config.models.ProfileConfig`
#: no longer declares, which makes the ``3 -> 4`` step the **first non-additive**
#: migration of this store; ``5`` rebuilds the ``wallet`` table into **one ledger per
#: mode** (row ``1`` paper, row ``2`` live -- see :data:`WALLET_ID_PAPER` and
#: :data:`WALLET_ID_LIVE`), the second non-additive step, which keeps a legacy
#: ``wallet_id = 1`` row as the paper ledger.
SCHEMA_VERSION: int = 5

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

#: Prefix of the ``meta`` key holding the last entry crossing acted upon by a profile.
#:
#: Deliberately a **separate** key from :data:`_CANDLE_WATERMARK_PREFIX`, and a
#: separate method pair, so the two watermarks can never be collapsed into one by
#: accident: the candle watermark answers "which candle was processed", this one
#: answers "which signal row was traded on".  It reuses the existing free-form
#: ``meta`` table, so it needs no DDL change and no schema version bump -- the
#: ``schema_version`` row stays at :data:`SCHEMA_VERSION` and a database written by
#: the previous build opens with no migration at all.
_ENTRY_WATERMARK_PREFIX = "entry_crossing:"

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
    (
        # One ledger **per mode**: ``wallet_id`` 1 is the paper ledger and 2 the live
        # one (see ``WALLET_ID_PAPER``/``WALLET_ID_LIVE``, the only place the mapping
        # is written down).  A database created before schema version 5 carries the
        # narrower ``CHECK (wallet_id = 1)``; ``CREATE TABLE IF NOT EXISTS`` cannot
        # widen it, which is why ``_migrate_wallet_modes`` rebuilds the table.
        "CREATE TABLE IF NOT EXISTS wallet ("
        "wallet_id INTEGER PRIMARY KEY CHECK (wallet_id IN (1, 2)), cash REAL NOT NULL, "
        "initial_balance REAL NOT NULL, updated_at TEXT NOT NULL)"
    ),
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


@dataclass(frozen=True)
class WalletRow:
    """The persisted state of **one** ledger of the platform (one row per mode).

    A ledger is the single source of truth for the USDT cash of every profile that
    runs in one mode: the paper ledger is spent by the paper venues, the live ledger
    mirrors a real venue account.  ``cash`` is what is left to deploy right now,
    ``initial_balance`` is what that ledger started with (it is the denominator of
    the platform-wide P&L) and ``updated_at`` is the instant of the last accepted
    write.

    The mode is the **key** (the ``wallet_id`` of the row), never a column: this
    dataclass therefore carries exactly the three columns of the table it was read
    from and is unchanged by the per-mode change.
    """

    cash: float
    initial_balance: float
    updated_at: pd.Timestamp | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "cash": float(self.cash),
            "initial_balance": float(self.initial_balance),
            "updated_at": _iso_or_none(self.updated_at),
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


#: Matches the ``input_value=...`` fragment of a pydantic error message.
_INPUT_VALUE_PATTERN = re.compile(r"input_value=.*?(?=,\s*input_type=|\]|$)", re.DOTALL)


def redact_profile_error(exc: BaseException) -> str:
    """Return ``exc`` rendered with every echoed *value* removed.

    A profile row is the one place a credential could have been hand-written, and
    pydantic echoes the offending value in its message (``input_value='s3cr3t'``).
    The rule and the field path are what an operator needs; the value is not, and a
    message carrying it ends up in the monitoring payload and in the logs.

    This is the only sanctioned way to render a profile failure: the tolerant read
    (:meth:`SqliteStateStore.load_profiles`), the reason it publishes through
    :meth:`SqliteStateStore.load_profile_failures` and the strict mode all go
    through it.
    """
    return _INPUT_VALUE_PATTERN.sub("input_value=<redacted>", str(exc))


#: Backwards-compatible private alias of :func:`redact_profile_error`.
#:
#: The implementation was private before the quarantine accessor made the sanitised
#: reason part of the store's public surface; the alias keeps every internal call
#: site -- and every importer written against the old private name -- working.
_redact_profile_error = redact_profile_error


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

    def state_path(self) -> Path | None:
        """Return the path of the backing store's database file, or ``None``.

        The path of the file is what the bootstrap surface of the platform
        settings is resolved against (see :mod:`trading_platform.realtime.settings`),
        so a caller never has to know which concrete store it was handed.  A store
        with no file of its own -- an in-memory double, a test fake -- answers
        ``None``.
        """
        ...

    def save_profile(self, spec: ProfileConfig) -> None:
        """Persist (or update) the configuration of one profile."""
        ...

    def load_profiles(self, *, strict: bool = False) -> list[ProfileConfig]:
        """Return every readable persisted profile, ordered by ``profile_id``.

        Tolerant by default: a row whose payload cannot be decoded or validated is
        skipped and reported through :meth:`load_profile_failures`, never fatal.
        ``strict=True`` restores the raising behaviour, for a caller that wants
        validation to fail loudly.
        """
        ...

    def load_profile_failures(self) -> dict[str, str]:
        """Return ``profile_id -> sanitised reason`` of the most recent profile read.

        The reasons are the rows :meth:`load_profiles` could not decode, rendered so
        that no stored value is ever echoed.  The method never raises and answers
        ``{}`` when there is nothing to report.
        """
        ...

    def delete_profile(self, profile_id: str) -> bool:
        """Remove one persisted profile; answer whether a row was actually removed.

        The profile set lives in the store, so removing one is a store write and
        not a document rewrite.  Deleting an unknown identifier is a no-op that
        answers ``False`` rather than an error: the caller has already resolved
        the profile it passes, and a concurrent deletion must not turn a
        successful call into a failure.
        """
        ...

    def save_wallet(
        self,
        *,
        cash: float,
        initial_balance: float,
        mode: str | RunMode = RunMode.PAPER,
    ) -> None:
        """Persist one mode's ledger (one row, upserted in place).

        ``mode`` is an **optional keyword defaulting to paper**, so every caller
        written before the per-mode ledgers existed keeps addressing the ledger it
        always addressed.  The row id is resolved from ``mode`` (see
        :data:`WALLET_ID_PAPER`/:data:`WALLET_ID_LIVE`); an unknown mode raises
        :class:`~trading_platform.core.errors.StateStoreError`.
        """
        ...

    def load_wallet(self, *, mode: str | RunMode = RunMode.PAPER) -> WalletRow | None:
        """Return one mode's persisted ledger, or ``None`` when that mode has no row.

        The optional ``mode`` keyword defaults to paper, exactly like
        :meth:`save_wallet`; ``None`` means "this ledger holds no row yet" and is
        the signal the engine initialises from the configuration -- it is never a
        ledger of ``0.0``.
        """
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

    def position_profile_ids(self) -> list[str]:
        """Return, sorted, every profile id the ``positions`` table mentions.

        The read exists for the startup orphan sweep: a position is durable state
        completely independent of the ``profiles`` table, so the only way to find
        one whose profile was never loaded is to ask the table itself.  An empty
        table answers ``[]``; no ordering, no filtering, no decoding -- the
        identifiers are exactly the ``profile_id`` column, distinct and sorted.
        """
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

    def last_acted_entry_crossing(self, profile_id: str) -> pd.Timestamp | None:
        """Return the timestamp of the signal row an entry was last acted upon, or None."""
        ...

    def mark_acted_entry_crossing(self, profile_id: str, timestamp: pd.Timestamp) -> None:
        """Record a signal row an entry was acted upon; keep the MAXIMUM timestamp seen."""
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
        # ``profile_id -> sanitised reason`` of the rows the most recent
        # ``load_profiles`` could not read.  Declared here, and not only set by the
        # read, so ``load_profile_failures`` answers on a store that was never
        # initialized.
        self._profile_failures: dict[str, str] = {}

    # -- introspection ------------------------------------------------------

    @property
    def path(self) -> Path:
        """Location of the SQLite file backing this store."""
        return self._path

    def state_path(self) -> Path | None:
        """Return the location of the SQLite file backing this store.

        The file *is* the store, so this never answers ``None`` -- it is the
        member of the :class:`StateStore` seam that lets a caller resolve the
        bootstrap surface of the platform settings without knowing which
        implementation it was handed.
        """
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
            # v1/v2/v3 -> v4 forward migration.  ``_create_schema`` runs first and
            # every statement is ``CREATE TABLE/INDEX IF NOT EXISTS``, so a database
            # deployed at version 1 or 2 simply gains the empty ``candles`` and/or
            # ``wallet`` table (and its index) here, while every existing table,
            # column and row is left untouched.  ``_check_schema_version`` then runs
            # the first **non-additive** step of the store -- the ``3 -> 4`` prune of
            # the ``profiles`` payload keys the current ``ProfileConfig`` no longer
            # declares, see :meth:`_migrate_profiles_payload` -- inside the very same
            # migration transaction as the version bump, and a database already at
            # version 4 touches neither a row nor the version.
            self._create_schema(conn)
            self._check_schema_version(conn)
        except BaseException:
            self.close()
            raise
        self._initialized = True
        logger.debug("state store %s initialized (schema version %s)", self._path, SCHEMA_VERSION)

    def close(self) -> None:
        """Close every connection and release the writer lock (deletes nothing).

        The quarantine report of the last read is dropped with the connections: a
        closed store has no live read to answer for, so
        :meth:`load_profile_failures` answers ``{}`` again.
        """
        self._profile_failures = {}
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
        """Compare the stored schema version with :data:`SCHEMA_VERSION`.

        An older database is migrated in place: the ``3 -> 4`` payload prune, the
        ``4 -> 5`` wallet rebuild and the version bump all run in **one**
        transaction, so the rewrites either commit together with the new version or
        are rolled back whole.
        """
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
                # The prune runs for every older version, not only for 3: it is a
                # no-op on a payload that already matches the current model, and
                # running it unconditionally means no later version step has to
                # remember which payload change it introduced.
                self._migrate_profiles_payload(conn)
                # Likewise the wallet rebuild runs for every older version: it is a
                # no-op on a table that already carries the per-mode CHECK, so the
                # step belongs to the *table shape* rather than to a version number.
                self._migrate_wallet_modes(conn)
                conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            return
        logger.debug("state store %s already uses schema version %s", self._path, stored)

    def _migrate_profiles_payload(self, conn: sqlite3.Connection) -> None:
        """Drop the ``profiles`` payload keys the current ``ProfileConfig`` rejects.

        This is the ``3 -> 4`` step, the first **non-additive** migration of the
        store.  Schema version ``3`` predates the release that removed the ``forecast``
        field from :class:`~trading_platform.config.models.ProfileConfig` -- which is
        ``extra="forbid"`` -- and removed the ``timesfm`` strategy from the registry,
        without migrating the rows the previous builds had persisted.  A row written
        before it therefore carries a key the model no longer declares, and nothing but
        this method stood between such a row and a store read that aborted.

        The keys a row may keep are ``ProfileConfig.model_fields`` -- the declared
        field set of the model **of this build** -- and never a hard-coded list of
        removed names: the model declaration is the contract the payload has to
        satisfy, so the field a later release removes is pruned by that release's own
        boot, automatically.  The comparison is strictly top-level (a nested object is
        the model's business, never pruned), and the rewritten document is serialised
        by :func:`_dumps`, the very helper :meth:`save_profile` uses, so every
        surviving key keeps its exact JSON content.  ``updated_at`` is never written.

        The method is idempotent and a **no-op on a clean database**: a row that
        carries no unknown key is not written at all, so an already-conforming row
        keeps its bytes and the migrated file differs from the original only by the
        version row.  A payload that is not valid JSON, or is not a JSON object, is
        left **byte for byte** untouched and logged instead: mangling it here would
        destroy the only copy of what the operator wrote, and the tolerant read
        reports it as a failed profile anyway.

        The caller owns the transaction (see :meth:`_check_schema_version`) and the
        connection is used directly: a ``sqlite3.Error`` is deliberately not caught
        here, because the enclosing :func:`_transaction` converts it into a
        :class:`~trading_platform.core.errors.StateStoreError` and rolls the whole
        migration back.
        """
        declared = set(ProfileConfig.model_fields)
        rows = conn.execute("SELECT profile_id, payload FROM profiles").fetchall()
        for row in rows:
            profile_id = str(row["profile_id"])
            try:
                data = json.loads(str(row["payload"]))
            except ValueError as exc:
                logger.warning(
                    "state store %s: profile %s payload is not valid JSON, left untouched "
                    "by the v3 -> v4 migration: %s",
                    self._path,
                    profile_id,
                    redact_profile_error(exc),
                )
                continue
            if not isinstance(data, dict):
                logger.warning(
                    "state store %s: profile %s payload is not a JSON object, left untouched "
                    "by the v3 -> v4 migration",
                    self._path,
                    profile_id,
                )
                continue
            unknown = [key for key in data if key not in declared]
            if not unknown:
                # Nothing to prune, so the row is not written back at all: an UPDATE
                # would rewrite identical content and touch ``updated_at`` for nothing.
                continue
            pruned = {key: value for key, value in data.items() if key in declared}
            conn.execute(
                "UPDATE profiles SET payload = ? WHERE profile_id = ?",
                (_dumps(pruned), profile_id),
            )
            logger.warning(
                "state store %s: profile %s migrated, dropped %d field(s) removed from "
                "ProfileConfig: %s",
                self._path,
                profile_id,
                len(unknown),
                sorted(unknown),
            )

    def _migrate_wallet_modes(self, conn: sqlite3.Connection) -> None:
        """Rebuild the ``wallet`` table so it accepts one ledger per mode.

        This is the ``4 -> 5`` step, the second **non-additive** migration of the
        store.  Schema version ``4`` shipped a single-row ``wallet`` table whose
        ``CHECK (wallet_id = 1)`` refuses a second ledger, and ``CREATE TABLE IF NOT
        EXISTS`` -- the only DDL this store applies at boot -- leaves an existing
        table exactly as it is.  A database written by the previous build would
        therefore reject the live ledger's ``wallet_id = 2`` for ever, so the table
        has to be **rebuilt**: ``wallet_new`` carries the new
        ``CHECK (wallet_id IN (1, 2))``, every row of the old table is copied into
        it, the old table is dropped and the new one is renamed into its place.

        The rebuild preserves the legacy row instead of dropping it, and that is the
        whole point: the previous build wrote its single ledger as ``wallet_id = 1``,
        which *is* :data:`WALLET_ID_PAPER`, so the deployed ledger survives **as the
        paper ledger** -- same cash, same initial balance, same ``updated_at``.  The
        live ledger simply has no row until a venue balance is mirrored into it.

        The method is idempotent and a **no-op on a clean database**: the table is
        read back first and left untouched when it already accepts both ids, so an
        already-migrated file (and a file this build created) reopens without a
        single write.  A missing ``wallet`` table -- a version ``1`` or ``2``
        database -- never reaches here with nothing to do: ``_create_schema`` runs
        before this method and creates the per-mode table, and this rewrite then
        finds it already correct.

        The caller owns the transaction (see :meth:`_check_schema_version`) and the
        connection is used directly: a ``sqlite3.Error`` is deliberately not caught
        here, because the enclosing :func:`_transaction` converts it into a
        :class:`~trading_platform.core.errors.StateStoreError` and rolls the whole
        migration back -- a half-rebuilt wallet table would be far worse than a
        failed boot.
        """
        if self._wallet_accepts_both_modes(conn):
            # Nothing to rebuild, so the table is not touched at all: a DROP/RENAME
            # cycle on an already-correct table would rewrite the file for nothing
            # and would make the boot non-idempotent on disk.
            return
        conn.execute(
            "CREATE TABLE wallet_new ("
            "wallet_id INTEGER PRIMARY KEY CHECK (wallet_id IN (1, 2)), cash REAL NOT NULL, "
            "initial_balance REAL NOT NULL, updated_at TEXT NOT NULL)"
        )
        # ``SELECT *`` copies the four columns positionally, which is the whole row:
        # the legacy ``wallet_id = 1`` row therefore becomes the paper ledger.
        conn.execute(
            "INSERT INTO wallet_new (wallet_id, cash, initial_balance, updated_at) "
            "SELECT wallet_id, cash, initial_balance, updated_at FROM wallet"
        )
        conn.execute("DROP TABLE wallet")
        conn.execute("ALTER TABLE wallet_new RENAME TO wallet")
        logger.warning(
            "state store %s: wallet table migrated to one ledger per mode "
            "(wallet_id 1 = paper, 2 = live); the legacy row is now the paper ledger",
            self._path,
        )

    def _wallet_accepts_both_modes(self, conn: sqlite3.Connection) -> bool:
        """Whether the ``wallet`` table already carries the per-mode ``CHECK``.

        The answer is read from ``sqlite_master`` -- the DDL SQLite actually stores --
        rather than from the stored schema version, so the rebuild is driven by the
        real shape of the table on disk.  A file whose version row was hand-edited
        (or bumped by a build that failed half-way) is therefore still repaired
        instead of bricking on the next live write.

        A ``wallet`` table that does not exist at all is *not* "already correct": this
        helper answers ``False`` in that case, because the caller must then be able to
        tell.  In practice it cannot happen -- ``_create_schema`` runs first -- and
        the rebuild's own failure is reported as a :class:`StateStoreError` by the
        enclosing transaction, which is exactly what a missing table deserves.
        """
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'wallet'"
        ).fetchone()
        if row is None:
            return False
        return "wallet_id IN (1, 2)" in str(row[0])

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

    def load_profiles(self, *, strict: bool = False) -> list[ProfileConfig]:
        """Return every readable persisted profile, ordered by ``profile_id``.

        Tolerant by default: a row whose payload cannot be decoded or validated is
        **skipped**, not fatal.  One unreadable row used to abort the whole read, and
        because that read is the orchestrator's only source of profiles, a single
        stale row kept the engine from starting, the monitoring API from binding and
        the dashboard from showing anything but an unreachable API.  A bad row is now
        a quarantined profile: the reason is logged at ``WARNING`` **sanitised** (see
        :func:`redact_profile_error` -- a profile row is exactly where an operator
        might have hand-written a credential, so the value is never echoed) and
        published through :meth:`load_profile_failures`, and every other profile
        still loads.

        Parameters
        ----------
        strict:
            Keyword-only, ``False`` by default.  ``True`` restores the previous
            raising behaviour: the first row that cannot be decoded or validated
            raises :class:`~trading_platform.core.errors.StateStoreError`, which the
            boot path of the engine -- and every other production caller -- does not
            ask for.

        Raises
        ------
        StateStoreError
            In **both** modes when the ``profiles`` table itself cannot be read (an
            infrastructure failure, never a bad row), and in strict mode for the
            first unreadable row.
        """
        # The report answers for the most recent read, so it starts empty on every
        # call -- including a call that fails on the table read itself.
        self._profile_failures = {}
        rows = self._fetchall(
            "load_profiles", "SELECT profile_id, payload FROM profiles ORDER BY profile_id ASC"
        )
        profiles: list[ProfileConfig] = []
        for row in rows:
            profile_id = str(row["profile_id"])
            try:
                profiles.append(ProfileConfig.model_validate(json.loads(str(row["payload"]))))
            except (KeyError, TypeError, ValueError) as exc:
                # The payload is echoed *sanitised*: pydantic reports the offending
                # value in its message, and a profile row is exactly where an
                # operator might have hand-written a credential.  The reason is kept
                # (field path and rule), the value never is.
                reason = redact_profile_error(exc)
                self._profile_failures[profile_id] = reason
                if strict:
                    raise StateStoreError(
                        f"state store read failed (load_profiles): corrupted payload for "
                        f"{profile_id}: {reason}"
                    ) from exc
                logger.warning(
                    "state store %s: skipping unreadable profile %s (load_profiles): %s",
                    self._path,
                    profile_id,
                    reason,
                )
        return profiles

    def load_profile_failures(self) -> dict[str, str]:
        """Return the profiles the most recent :meth:`load_profiles` could not read.

        The mapping is ``profile_id -> sanitised reason`` (see
        :func:`redact_profile_error`), and it is a **copy**: the caller may keep or
        mutate it without touching the store.  Every reason is the one logged by the
        tolerant read, so the monitoring payload and the log stream tell the same
        story about what could not be loaded.

        It answers ``{}`` before the first read, after a read in which every row
        decoded, and after :meth:`close` -- in all three cases there is nothing to
        report, which the read model renders as "every profile loaded".

        The accessor is deliberately the one profile read that can never fail: it
        reads no SQL, needs no initialized store and holds no connection, so a
        dashboard asking a broken store what it could not load still gets an answer.
        """
        try:
            return dict(self._profile_failures)
        except Exception:  # pragma: no cover - defensive read of a plain dict attribute
            logger.warning("state store %s: cannot report the failed profiles", self._path)
            return {}

    def delete_profile(self, profile_id: str) -> bool:
        """Remove the row of ``profile_id``; answer whether one was removed.

        The delete is idempotent: replaying it deletes nothing the second time and
        answers ``False``.  Only the ``profiles`` table is touched -- the durable
        positions of a profile are deliberately left alone, because a position is
        state of its own and is closed (or swept) through the execution path, never
        by deleting a row.
        """
        with self._write("delete_profile") as conn:
            cursor = conn.execute("DELETE FROM profiles WHERE profile_id = ?", (str(profile_id),))
            return bool(cursor.rowcount)

    # -- platform wallets, one ledger per mode ------------------------------

    def save_wallet(
        self,
        *,
        cash: float,
        initial_balance: float,
        mode: str | RunMode = RunMode.PAPER,
    ) -> None:
        """Persist one mode's ledger, upserting the row of that mode.

        The row id is resolved from ``mode`` through :meth:`_wallet_id` -- never a
        hard-coded ``1`` -- and the ``ON CONFLICT(wallet_id) DO UPDATE`` clause is
        what makes a ledger a *single* row for ever: the first save of a mode inserts
        it, every later save overwrites the cash and the initial balance of that same
        row, inside one ``BEGIN IMMEDIATE ... COMMIT``.  Writing the live ledger can
        therefore never touch the paper one.

        ``mode`` is an optional keyword defaulting to
        :attr:`~trading_platform.realtime.models.RunMode.PAPER`, so every caller
        written before the per-mode ledgers existed keeps writing the paper ledger.

        A failed save (a ``NaN`` cash, which SQLite stores as ``NULL``, violates
        ``cash REAL NOT NULL``) rolls back and leaves the previous ledger exactly as
        it was, reported as :class:`StateStoreError`.

        Raises
        ------
        StateStoreError
            If ``mode`` is not a known run mode, or if the write fails.
        """
        wallet_id = self._wallet_id(mode)
        now = self._now_iso()
        with self._write("save_wallet") as conn:
            conn.execute(
                "INSERT INTO wallet (wallet_id, cash, initial_balance, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(wallet_id) DO UPDATE SET "
                "cash = excluded.cash, initial_balance = excluded.initial_balance, "
                "updated_at = excluded.updated_at",
                (wallet_id, float(cash), float(initial_balance), now),
            )

    def load_wallet(self, *, mode: str | RunMode = RunMode.PAPER) -> WalletRow | None:
        """Return one mode's persisted ledger, or ``None`` when that mode has no row.

        Each mode is **independent**: the paper ledger and the live ledger are two
        rows of one table, so reading one never observes the other.  ``None`` is the
        signal the engine uses to initialise a ledger from the configuration (the
        configured platform initial balance, or the sum of the profile allocations)
        -- it is never confused with a ledger of ``0.0``, which is a value.

        ``mode`` is an optional keyword defaulting to
        :attr:`~trading_platform.realtime.models.RunMode.PAPER`, exactly like
        :meth:`save_wallet`.

        Raises
        ------
        StateStoreError
            If ``mode`` is not a known run mode, or if the stored ``updated_at`` is
            not a valid timestamp.
        """
        wallet_id = self._wallet_id(mode)
        row = self._fetchone(
            "load_wallet",
            "SELECT cash, initial_balance, updated_at FROM wallet WHERE wallet_id = ?",
            (wallet_id,),
        )
        if row is None:
            return None
        return WalletRow(
            cash=float(row["cash"]),
            initial_balance=float(row["initial_balance"]),
            updated_at=_parse_timestamp(row["updated_at"], operation="load_wallet", key="wallet"),
        )

    def _wallet_id(self, mode: str | RunMode) -> int:
        """Return the stable row id of one ledger, or refuse an unknown mode.

        This is the **single** resolution point of the mode -> row id mapping: the
        ids themselves live in :data:`WALLET_ID_PAPER`/:data:`WALLET_ID_LIVE`, and
        every read and write of the ``wallet`` table goes through here, so a mode can
        never be silently mapped to the wrong ledger.

        An unknown mode is refused rather than defaulted: writing the paper ledger
        because a caller passed a typo'd mode would publish one mode's cash as
        another's, and that is a money bug, not a convenience.

        Raises
        ------
        StateStoreError
            If ``mode`` is not ``"paper"``/``"live"`` (nor the matching
            :class:`~trading_platform.realtime.models.RunMode`).
        """
        try:
            resolved = RunMode(mode)
        except ValueError as exc:
            raise StateStoreError(
                f"state store wallet: unknown run mode {mode!r}; expected 'paper' or 'live'"
            ) from exc
        return WALLET_ID_PAPER if resolved is RunMode.PAPER else WALLET_ID_LIVE

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

    def position_profile_ids(self) -> list[str]:
        """Return, sorted, every profile id the ``positions`` table mentions.

        See the protocol member: this is the orphan sweep's raw scan of the
        durable positions, which is the only way to find a row whose profile is
        not loaded any more.
        """
        rows = self._fetchall(
            "position_profile_ids",
            "SELECT DISTINCT profile_id FROM positions ORDER BY profile_id ASC",
        )
        return [str(row["profile_id"]) for row in rows]

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

    def last_acted_entry_crossing(self, profile_id: str) -> pd.Timestamp | None:
        """Return the timestamp of the signal row an entry was last acted upon.

        ``None`` -- never a silent default instant -- means "no crossover of this
        profile has ever been acted upon", which is the exact state of every profile
        written before this watermark existed.  It is stored as an ISO-8601 UTC text
        in the ``meta`` table under ``entry_crossing:<profile_id>``, so one profile
        never gates another.

        A **corrupted** stored value also answers ``None`` rather than raising: this
        read sits on the live entry path, and one bad byte in one key must never take
        a tick down.  The tick then behaves as if nothing had ever been acted upon,
        which at worst re-arms a single entry -- a bounded loss, unlike a profile that
        stops trading.  Every other read error still surfaces as
        :class:`~trading_platform.core.errors.StateStoreError` (the ``get_meta`` call
        below is *not* wrapped, so a broken ``meta`` table is still reported).
        """
        raw = self.get_meta(_ENTRY_WATERMARK_PREFIX + profile_id)
        try:
            return _parse_timestamp(raw, operation="last_acted_entry_crossing", key=profile_id)
        except StateStoreError:
            logger.warning(
                "state store %s: corrupted entry crossing watermark for profile %s, "
                "treating it as absent",
                self._path,
                profile_id,
            )
            return None

    def mark_acted_entry_crossing(self, profile_id: str, timestamp: pd.Timestamp) -> None:
        """Record the signal row an entry was acted upon, keeping the maximum seen.

        The watermark is what makes requirement 1 hold: once a crossover has been
        acted upon, no replay of that same row -- a replayed tick, an out-of-order
        replay, or a restart reading the same frame again -- can ever act on it a
        second time.  The value is the timestamp of the **signal row** the entry came
        from, never the processed candle and never the frame's last stamp, and
        :func:`_utc_iso` normalises it to UTC so a naive input and the aware input of
        the same instant can never produce two distinct watermarks.

        Monotonic exactly like :meth:`mark_candle_processed`: writing an older or
        equal value is a no-op, so the watermark can only ever move forward.

        The comparison is made on *parsed instants*, not on the raw text: an
        unparseable stored value (a corrupted row) is treated as absent and simply
        overwritten.  A raw string comparison would be the opposite of safe here --
        ``"not-a-timestamp" >= "2024-01-01T00:04:00+00:00"`` is ``True``, so one bad
        byte in the key would silently swallow every later legitimate write and the
        profile would never act on a crossover again.
        """
        stamped = _utc_iso(timestamp)
        key = _ENTRY_WATERMARK_PREFIX + profile_id
        with self._write("mark_acted_entry_crossing") as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            if row is not None:
                try:
                    current = _parse_timestamp(
                        row["value"], operation="mark_acted_entry_crossing", key=profile_id
                    )
                except StateStoreError:
                    current = None
                if current is not None and _utc_iso(current) >= stamped:
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
