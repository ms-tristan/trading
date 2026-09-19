"""Frozen vocabulary of the realtime layer (the single home of the domain types).

Every dataclass exchanged between the stream, the store, the broker, the gateway,
the runner, the monitor and the web layer is declared **here and nowhere else**
(delivery brief D3).  The module is deliberately dependency-light: it imports the
standard library, ``pandas`` and the frozen cross-package models only, so it can
be imported by any layer of the realtime stack without creating a cycle.

Conventions
-----------
* every dataclass is ``@dataclass(frozen=True)``;
* every public dataclass exposes :meth:`to_dict`, whose payload contains
  JSON-native values only -- enums through ``.value``, timestamps through
  :meth:`pandas.Timestamp.isoformat`, floats through ``float(...)`` with every
  non-finite value mapped to ``None`` (never ``NaN``/``Inf`` on the wire);
* the dataclasses marked ``from_dict`` implement it as the exact inverse of
  :meth:`to_dict`, so a payload can travel through the SQLite store, the JSON
  API or a restart without loss;
* ``trading_platform.realtime.models`` never imports another realtime submodule
  and never imports config / data / strategy / metrics / reporting.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Self

import pandas as pd

from trading_platform.core.constants import UTC
from trading_platform.core.models import Direction

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    # Annotation only: the wallet imports this module, so the reference stays
    # inside ``TYPE_CHECKING`` and the "models imports no sibling" rule holds.
    from trading_platform.realtime.wallet import WalletSnapshot

__all__ = [
    "BrokerAck",
    "BrokerEvent",
    "BrokerEventType",
    "CandleEvent",
    "EngineCounters",
    "EquityPoint",
    "Fill",
    "Order",
    "OrderRequest",
    "OrderSide",
    "OrderState",
    "OrderType",
    "PlatformSnapshot",
    "Position",
    "ProfileHealth",
    "ProfileSnapshot",
    "ProfileState",
    "ProfileStatus",
    "ReconciliationReport",
    "RunMode",
    "SignalAction",
    "TradeSignalDecision",
    "new_client_order_id",
    "status_error",
]


# ---------------------------------------------------------------------------
# private helpers
# ---------------------------------------------------------------------------


def _finite_or_none(value: float | None) -> float | None:
    """Return ``value`` as a finite ``float``, or ``None``.

    ``None``, ``NaN`` and the two infinities all collapse to ``None`` so that no
    ``to_dict()`` payload can ever carry a value the JSON encoder refuses.
    """
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _float_from(value: Any, default: float = 0.0) -> float:
    """Inverse of :func:`_finite_or_none` for a required field.

    A payload produced by ``to_dict()`` turns a non-finite value into ``None``;
    decoding it back to ``default`` keeps ``from_dict`` total instead of raising
    on a value the encoder had to drop.
    """
    return default if value is None else float(value)


def _optional_float_from(value: Any) -> float | None:
    """Inverse of :func:`_finite_or_none` for an optional field."""
    return None if value is None else float(value)


def _timestamp_or_none(value: Any) -> str | None:
    """Render a timestamp as an ISO-8601 string, or ``None``."""
    return None if value is None else pd.Timestamp(value).isoformat()


def _timestamp_from(value: Any) -> pd.Timestamp:
    """Decode a required timestamp from a :meth:`to_dict` payload."""
    return pd.Timestamp(value)


def _optional_timestamp_from(value: Any) -> pd.Timestamp | None:
    """Decode an optional timestamp from a :meth:`to_dict` payload."""
    return None if value is None else pd.Timestamp(value)


# ---------------------------------------------------------------------------
# enumerations
# ---------------------------------------------------------------------------


class RunMode(str, Enum):  # noqa: UP042 - the frozen contract mandates (str, Enum)
    """Whether a profile trades on a simulated or on a real venue."""

    PAPER = "paper"
    LIVE = "live"


class ProfileStatus(str, Enum):  # noqa: UP042 - the frozen contract mandates (str, Enum)
    """Lifecycle status of a single profile."""

    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    HALTED = "halted"
    STOPPED = "stopped"
    ERROR = "error"


class OrderSide(str, Enum):  # noqa: UP042 - the frozen contract mandates (str, Enum)
    """Side of an order request."""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):  # noqa: UP042 - the frozen contract mandates (str, Enum)
    """Order type supported by the gateway."""

    MARKET = "market"
    LIMIT = "limit"


class OrderState(str, Enum):  # noqa: UP042 - the frozen contract mandates (str, Enum)
    """Lifecycle state of an order."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class BrokerEventType(str, Enum):  # noqa: UP042 - the frozen contract mandates (str, Enum)
    """Kind of event a venue reports when it is polled."""

    ORDER_ACCEPTED = "order_accepted"
    ORDER_REJECTED = "order_rejected"
    ORDER_PARTIALLY_FILLED = "order_partially_filled"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ERROR = "error"


class SignalAction(str, Enum):  # noqa: UP042 - the frozen contract mandates (str, Enum)
    """Action decided by a strategy for one closed candle."""

    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"
    ENTER_SHORT = "enter_short"
    EXIT_SHORT = "exit_short"
    STOP_LOSS = "stop_loss"
    HOLD = "hold"


# ---------------------------------------------------------------------------
# market data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandleEvent:
    """One closed (or still forming) candle emitted by a market stream."""

    symbol: str
    timeframe: str
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "symbol": str(self.symbol),
            "timeframe": str(self.timeframe),
            "timestamp": pd.Timestamp(self.timestamp).isoformat(),
            "open": _finite_or_none(self.open),
            "high": _finite_or_none(self.high),
            "low": _finite_or_none(self.low),
            "close": _finite_or_none(self.close),
            "volume": _finite_or_none(self.volume),
            "closed": bool(self.closed),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild a :class:`CandleEvent` from :meth:`to_dict` output."""
        return cls(
            symbol=str(payload["symbol"]),
            timeframe=str(payload["timeframe"]),
            timestamp=_timestamp_from(payload["timestamp"]),
            open=_float_from(payload["open"]),
            high=_float_from(payload["high"]),
            low=_float_from(payload["low"]),
            close=_float_from(payload["close"]),
            volume=_float_from(payload["volume"]),
            closed=bool(payload.get("closed", True)),
        )


# ---------------------------------------------------------------------------
# orders, fills and venue events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderRequest:
    """An order the gateway is about to route to the injected broker."""

    profile_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    type: OrderType
    quantity: float
    price: float | None = None
    stop_price: float | None = None
    mode: RunMode = RunMode.PAPER
    reason: str = ""
    created_at: pd.Timestamp | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "profile_id": str(self.profile_id),
            "client_order_id": str(self.client_order_id),
            "symbol": str(self.symbol),
            "side": OrderSide(self.side).value,
            "type": OrderType(self.type).value,
            "quantity": _finite_or_none(self.quantity),
            "price": _finite_or_none(self.price),
            "stop_price": _finite_or_none(self.stop_price),
            "mode": RunMode(self.mode).value,
            "reason": str(self.reason),
            "created_at": _timestamp_or_none(self.created_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild an :class:`OrderRequest` from :meth:`to_dict` output."""
        return cls(
            profile_id=str(payload["profile_id"]),
            client_order_id=str(payload["client_order_id"]),
            symbol=str(payload["symbol"]),
            side=OrderSide(payload["side"]),
            type=OrderType(payload["type"]),
            quantity=_float_from(payload["quantity"]),
            price=_optional_float_from(payload.get("price")),
            stop_price=_optional_float_from(payload.get("stop_price")),
            mode=RunMode(payload.get("mode", RunMode.PAPER.value)),
            reason=str(payload.get("reason") or ""),
            created_at=_optional_timestamp_from(payload.get("created_at")),
        )


@dataclass(frozen=True)
class Order:
    """The persisted state of one order, whatever the venue behind it."""

    client_order_id: str
    profile_id: str
    symbol: str
    side: OrderSide
    type: OrderType
    quantity: float
    state: OrderState
    mode: RunMode
    created_at: pd.Timestamp
    updated_at: pd.Timestamp
    filled_quantity: float = 0.0
    price: float | None = None
    average_fill_price: float | None = None
    broker_order_id: str | None = None
    reject_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "client_order_id": str(self.client_order_id),
            "profile_id": str(self.profile_id),
            "symbol": str(self.symbol),
            "side": OrderSide(self.side).value,
            "type": OrderType(self.type).value,
            "quantity": _finite_or_none(self.quantity),
            "state": OrderState(self.state).value,
            "mode": RunMode(self.mode).value,
            "created_at": pd.Timestamp(self.created_at).isoformat(),
            "updated_at": pd.Timestamp(self.updated_at).isoformat(),
            "filled_quantity": _finite_or_none(self.filled_quantity),
            "price": _finite_or_none(self.price),
            "average_fill_price": _finite_or_none(self.average_fill_price),
            "broker_order_id": None if self.broker_order_id is None else str(self.broker_order_id),
            "reject_reason": str(self.reject_reason),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild an :class:`Order` from :meth:`to_dict` output."""
        broker_order_id = payload.get("broker_order_id")
        return cls(
            client_order_id=str(payload["client_order_id"]),
            profile_id=str(payload["profile_id"]),
            symbol=str(payload["symbol"]),
            side=OrderSide(payload["side"]),
            type=OrderType(payload["type"]),
            quantity=_float_from(payload["quantity"]),
            state=OrderState(payload["state"]),
            mode=RunMode(payload["mode"]),
            created_at=_timestamp_from(payload["created_at"]),
            updated_at=_timestamp_from(payload["updated_at"]),
            filled_quantity=_float_from(payload.get("filled_quantity")),
            price=_optional_float_from(payload.get("price")),
            average_fill_price=_optional_float_from(payload.get("average_fill_price")),
            broker_order_id=None if broker_order_id is None else str(broker_order_id),
            reject_reason=str(payload.get("reject_reason") or ""),
        )


@dataclass(frozen=True)
class Fill:
    """One execution reported by the venue (possibly a partial fill)."""

    fill_id: str
    client_order_id: str
    profile_id: str
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    fee: float
    timestamp: pd.Timestamp
    mode: RunMode

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "fill_id": str(self.fill_id),
            "client_order_id": str(self.client_order_id),
            "profile_id": str(self.profile_id),
            "symbol": str(self.symbol),
            "side": OrderSide(self.side).value,
            "quantity": _finite_or_none(self.quantity),
            "price": _finite_or_none(self.price),
            "fee": _finite_or_none(self.fee),
            "timestamp": pd.Timestamp(self.timestamp).isoformat(),
            "mode": RunMode(self.mode).value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild a :class:`Fill` from :meth:`to_dict` output."""
        return cls(
            fill_id=str(payload["fill_id"]),
            client_order_id=str(payload["client_order_id"]),
            profile_id=str(payload["profile_id"]),
            symbol=str(payload["symbol"]),
            side=OrderSide(payload["side"]),
            quantity=_float_from(payload["quantity"]),
            price=_float_from(payload["price"]),
            fee=_float_from(payload["fee"]),
            timestamp=_timestamp_from(payload["timestamp"]),
            mode=RunMode(payload["mode"]),
        )


@dataclass(frozen=True)
class BrokerAck:
    """The venue's immediate answer to :meth:`Broker.submit`."""

    client_order_id: str
    accepted: bool
    state: OrderState
    broker_order_id: str | None = None
    reason: str = ""
    submitted_at: pd.Timestamp | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "client_order_id": str(self.client_order_id),
            "accepted": bool(self.accepted),
            "state": OrderState(self.state).value,
            "broker_order_id": None if self.broker_order_id is None else str(self.broker_order_id),
            "reason": str(self.reason),
            "submitted_at": _timestamp_or_none(self.submitted_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild a :class:`BrokerAck` from :meth:`to_dict` output."""
        broker_order_id = payload.get("broker_order_id")
        return cls(
            client_order_id=str(payload["client_order_id"]),
            accepted=bool(payload["accepted"]),
            state=OrderState(payload["state"]),
            broker_order_id=None if broker_order_id is None else str(broker_order_id),
            reason=str(payload.get("reason") or ""),
            submitted_at=_optional_timestamp_from(payload.get("submitted_at")),
        )


@dataclass(frozen=True)
class BrokerEvent:
    """One event polled from the venue, already mapped to the local vocabulary."""

    event_type: BrokerEventType
    client_order_id: str
    profile_id: str
    order: Order | None = None
    fill: Fill | None = None
    message: str = ""
    timestamp: pd.Timestamp | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "event_type": BrokerEventType(self.event_type).value,
            "client_order_id": str(self.client_order_id),
            "profile_id": str(self.profile_id),
            "order": None if self.order is None else self.order.to_dict(),
            "fill": None if self.fill is None else self.fill.to_dict(),
            "message": str(self.message),
            "timestamp": _timestamp_or_none(self.timestamp),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild a :class:`BrokerEvent` from :meth:`to_dict` output."""
        raw_order = payload.get("order")
        raw_fill = payload.get("fill")
        return cls(
            event_type=BrokerEventType(payload["event_type"]),
            client_order_id=str(payload["client_order_id"]),
            profile_id=str(payload["profile_id"]),
            order=None if raw_order is None else Order.from_dict(raw_order),
            fill=None if raw_fill is None else Fill.from_dict(raw_fill),
            message=str(payload.get("message") or ""),
            timestamp=_optional_timestamp_from(payload.get("timestamp")),
        )


@dataclass(frozen=True)
class ReconciliationReport:
    """Outcome of comparing the persisted orders against the venue's view."""

    profile_id: str
    ok: bool
    checked_at: pd.Timestamp
    matched: int = 0
    only_at_venue: tuple[str, ...] = ()
    only_locally: tuple[str, ...] = ()
    mismatched: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field.

        The three identifier collections are rendered as JSON lists so the
        payload survives a round-trip through the store or the HTTP API.
        """
        return {
            "profile_id": str(self.profile_id),
            "ok": bool(self.ok),
            "checked_at": pd.Timestamp(self.checked_at).isoformat(),
            "matched": int(self.matched),
            "only_at_venue": [str(value) for value in self.only_at_venue],
            "only_locally": [str(value) for value in self.only_locally],
            "mismatched": [str(value) for value in self.mismatched],
            "details": {str(key): value for key, value in self.details.items()},
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild a :class:`ReconciliationReport` from :meth:`to_dict` output."""
        return cls(
            profile_id=str(payload["profile_id"]),
            ok=bool(payload["ok"]),
            checked_at=_timestamp_from(payload["checked_at"]),
            matched=int(payload.get("matched", 0)),
            only_at_venue=tuple(str(value) for value in payload.get("only_at_venue") or ()),
            only_locally=tuple(str(value) for value in payload.get("only_locally") or ()),
            mismatched=tuple(str(value) for value in payload.get("mismatched") or ()),
            details=dict(payload.get("details") or {}),
        )


# ---------------------------------------------------------------------------
# positions, equity and profile state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    """One open position of one profile on one symbol."""

    profile_id: str
    symbol: str
    quantity: float
    average_price: float
    direction: Direction
    opened_at: pd.Timestamp
    updated_at: pd.Timestamp
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    stop_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "profile_id": str(self.profile_id),
            "symbol": str(self.symbol),
            "quantity": _finite_or_none(self.quantity),
            "average_price": _finite_or_none(self.average_price),
            "direction": Direction(self.direction).value,
            "opened_at": pd.Timestamp(self.opened_at).isoformat(),
            "updated_at": pd.Timestamp(self.updated_at).isoformat(),
            "realized_pnl": _finite_or_none(self.realized_pnl),
            "unrealized_pnl": _finite_or_none(self.unrealized_pnl),
            "stop_price": _finite_or_none(self.stop_price),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild a :class:`Position` from :meth:`to_dict` output."""
        return cls(
            profile_id=str(payload["profile_id"]),
            symbol=str(payload["symbol"]),
            quantity=_float_from(payload["quantity"]),
            average_price=_float_from(payload["average_price"]),
            direction=Direction(payload["direction"]),
            opened_at=_timestamp_from(payload["opened_at"]),
            updated_at=_timestamp_from(payload["updated_at"]),
            realized_pnl=_float_from(payload.get("realized_pnl")),
            unrealized_pnl=_float_from(payload.get("unrealized_pnl")),
            stop_price=_optional_float_from(payload.get("stop_price")),
        )


@dataclass(frozen=True)
class EquityPoint:
    """One point of a profile's equity curve."""

    profile_id: str
    timestamp: pd.Timestamp
    equity: float
    cash: float
    position_value: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "profile_id": str(self.profile_id),
            "timestamp": pd.Timestamp(self.timestamp).isoformat(),
            "equity": _finite_or_none(self.equity),
            "cash": _finite_or_none(self.cash),
            "position_value": _finite_or_none(self.position_value),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild an :class:`EquityPoint` from :meth:`to_dict` output."""
        return cls(
            profile_id=str(payload["profile_id"]),
            timestamp=_timestamp_from(payload["timestamp"]),
            equity=_float_from(payload["equity"]),
            cash=_float_from(payload["cash"]),
            position_value=_float_from(payload["position_value"]),
        )


@dataclass(frozen=True)
class EngineCounters:
    """In-process counters exposed through the health read model (brief D10)."""

    candles_processed: int = 0
    orders_submitted: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    stream_reconnects: int = 0
    risk_rejections: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "candles_processed": int(self.candles_processed),
            "orders_submitted": int(self.orders_submitted),
            "orders_filled": int(self.orders_filled),
            "orders_rejected": int(self.orders_rejected),
            "stream_reconnects": int(self.stream_reconnects),
            "risk_rejections": int(self.risk_rejections),
            "errors": int(self.errors),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild :class:`EngineCounters` from :meth:`to_dict` output."""
        return cls(
            candles_processed=int(payload.get("candles_processed", 0)),
            orders_submitted=int(payload.get("orders_submitted", 0)),
            orders_filled=int(payload.get("orders_filled", 0)),
            orders_rejected=int(payload.get("orders_rejected", 0)),
            stream_reconnects=int(payload.get("stream_reconnects", 0)),
            risk_rejections=int(payload.get("risk_rejections", 0)),
            errors=int(payload.get("errors", 0)),
        )


#: Statuses that describe a *healthy* profile and therefore clear the last error.
_HEALTHY_STATUSES = frozenset({ProfileStatus.STARTING, ProfileStatus.RUNNING})

#: Statuses that describe a *fault*: their detail is the reason and becomes the error.
_FAULT_STATUSES = frozenset({ProfileStatus.ERROR, ProfileStatus.DEGRADED, ProfileStatus.HALTED})


def status_error(status: ProfileStatus, detail: str, previous: str | None = None) -> str | None:
    """Return the ``last_error`` a status write must carry (shared rule).

    The same rule drives the in-memory health block of a running profile and the
    persisted :class:`ProfileState`, because a dashboard that reads one while the
    engine writes the other must not be told two different stories:

    * a **fault** status (:attr:`ProfileStatus.ERROR`, :attr:`ProfileStatus.DEGRADED`,
      :attr:`ProfileStatus.HALTED`) carries its reason in the detail, which becomes
      the error;
    * a **healthy** status (:attr:`ProfileStatus.STARTING`,
      :attr:`ProfileStatus.RUNNING`) clears it, because a profile that just started,
      or just processed a candle, is by definition not in the state its previous
      process died in;
    * :attr:`ProfileStatus.STOPPED` keeps whatever is there, which is why the
      profile stopped.

    The ``detail`` of a healthy status is a *description* (``"running"``,
    ``"last candle <ts>"``) and never an error -- storing it as one is what made the
    dashboard report ``last error: last candle 2024-01-05T23:00:00+00:00`` for a
    perfectly healthy profile.
    """
    if status in _FAULT_STATUSES:
        return detail or None
    if status in _HEALTHY_STATUSES:
        return None
    return previous


@dataclass(frozen=True)
class ProfileState:
    """The persisted health of one profile, restored on every restart."""

    profile_id: str
    status: ProfileStatus
    mode: RunMode
    last_candle_at: pd.Timestamp | None = None
    lag_seconds: float = 0.0
    last_error: str | None = None
    reconnect_count: int = 0
    updated_at: pd.Timestamp | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "profile_id": str(self.profile_id),
            "status": ProfileStatus(self.status).value,
            "mode": RunMode(self.mode).value,
            "last_candle_at": _timestamp_or_none(self.last_candle_at),
            "lag_seconds": _finite_or_none(self.lag_seconds),
            "last_error": None if self.last_error is None else str(self.last_error),
            "reconnect_count": int(self.reconnect_count),
            "updated_at": _timestamp_or_none(self.updated_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild a :class:`ProfileState` from :meth:`to_dict` output."""
        last_error = payload.get("last_error")
        return cls(
            profile_id=str(payload["profile_id"]),
            status=ProfileStatus(payload["status"]),
            mode=RunMode(payload["mode"]),
            last_candle_at=_optional_timestamp_from(payload.get("last_candle_at")),
            lag_seconds=_float_from(payload.get("lag_seconds")),
            last_error=None if last_error is None else str(last_error),
            reconnect_count=int(payload.get("reconnect_count", 0)),
            updated_at=_optional_timestamp_from(payload.get("updated_at")),
        )


# ---------------------------------------------------------------------------
# read model (health / snapshots) -- consumed by the monitor and the web layer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileHealth:
    """Health block of a profile snapshot: status, lag and counters."""

    profile_id: str
    status: ProfileStatus
    last_candle_at: pd.Timestamp | None = None
    lag_seconds: float = 0.0
    last_error: str | None = None
    reconnect_count: int = 0
    counters: EngineCounters = field(default_factory=EngineCounters)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "profile_id": str(self.profile_id),
            "status": ProfileStatus(self.status).value,
            "last_candle_at": _timestamp_or_none(self.last_candle_at),
            "lag_seconds": _finite_or_none(self.lag_seconds),
            "last_error": None if self.last_error is None else str(self.last_error),
            "reconnect_count": int(self.reconnect_count),
            "counters": self.counters.to_dict(),
        }


@dataclass(frozen=True)
class ProfileSnapshot:
    """Everything the dashboard shows for one profile at one instant.

    Every cash figure of a profile is **attributed**, never the venue's: the
    platform funds every order from one shared wallet, so ``cash`` is
    ``allocation - deployed + realized_pnl`` -- the profile's own share of the
    ledger -- and ``equity`` is ``allocation + realized_pnl + unrealized_pnl``.
    The five appended fields are additive: the existing keys keep their names and
    their types, only their meaning is refined, and a snapshot built without them
    (as every caller did before the shared wallet existed) reports zeros.
    """

    profile_id: str
    symbol: str
    timeframe: str
    strategy: str
    mode: RunMode
    status: ProfileStatus
    initial_balance: float
    equity: float
    cash: float
    position_value: float
    total_return: float
    n_trades: int
    open_positions: int
    health: ProfileHealth
    started_at: pd.Timestamp | None = None
    updated_at: pd.Timestamp | None = None
    allocation: float = 0.0
    deployed: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    last_block_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "profile_id": str(self.profile_id),
            "symbol": str(self.symbol),
            "timeframe": str(self.timeframe),
            "strategy": str(self.strategy),
            "mode": RunMode(self.mode).value,
            "status": ProfileStatus(self.status).value,
            "initial_balance": _finite_or_none(self.initial_balance),
            "equity": _finite_or_none(self.equity),
            "cash": _finite_or_none(self.cash),
            "position_value": _finite_or_none(self.position_value),
            "total_return": _finite_or_none(self.total_return),
            "n_trades": int(self.n_trades),
            "open_positions": int(self.open_positions),
            "health": self.health.to_dict(),
            "started_at": _timestamp_or_none(self.started_at),
            "updated_at": _timestamp_or_none(self.updated_at),
            "allocation": _finite_or_none(self.allocation),
            "deployed": _finite_or_none(self.deployed),
            "realized_pnl": _finite_or_none(self.realized_pnl),
            "unrealized_pnl": _finite_or_none(self.unrealized_pnl),
            "last_block_reason": (
                None if self.last_block_reason is None else str(self.last_block_reason)
            ),
        }


@dataclass(frozen=True)
class PlatformSnapshot:
    """The whole platform as seen by the monitoring layer.

    ``wallet`` is the shared USDT wallet every profile funds its orders from: the
    platform-wide view of the cash, the equity and the exposure.  It is the last
    field and it is optional, so every construction written before the shared
    wallet existed keeps working and serialises ``"wallet": null``.
    """

    profiles: tuple[ProfileSnapshot, ...]
    generated_at: pd.Timestamp
    kill_switch: bool
    kill_switch_reason: str = ""
    kill_switch_changed_at: pd.Timestamp | None = None
    version: str = ""
    started_at: pd.Timestamp | None = None
    uptime_seconds: float = 0.0
    wallet: WalletSnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "profiles": [profile.to_dict() for profile in self.profiles],
            "generated_at": pd.Timestamp(self.generated_at).isoformat(),
            "kill_switch": bool(self.kill_switch),
            "kill_switch_reason": str(self.kill_switch_reason),
            "kill_switch_changed_at": _timestamp_or_none(self.kill_switch_changed_at),
            "version": str(self.version),
            "started_at": _timestamp_or_none(self.started_at),
            "uptime_seconds": _finite_or_none(self.uptime_seconds),
            "wallet": None if self.wallet is None else self.wallet.to_dict(),
        }


@dataclass(frozen=True)
class TradeSignalDecision:
    """A strategy decision for one closed candle, before risk and routing.

    ``client_order_id`` is empty when the decision does not produce an order
    (``HOLD`` or a blocked decision).
    """

    profile_id: str
    timestamp: pd.Timestamp
    action: SignalAction
    direction: Direction | None = None
    stop_price: float | None = None
    quantity: float = 0.0
    reference_price: float = 0.0
    client_order_id: str = ""
    blocked: bool = False
    block_reason: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "profile_id": str(self.profile_id),
            "timestamp": pd.Timestamp(self.timestamp).isoformat(),
            "action": SignalAction(self.action).value,
            "direction": None if self.direction is None else Direction(self.direction).value,
            "stop_price": _finite_or_none(self.stop_price),
            "quantity": _finite_or_none(self.quantity),
            "reference_price": _finite_or_none(self.reference_price),
            "client_order_id": str(self.client_order_id),
            "blocked": bool(self.blocked),
            "block_reason": str(self.block_reason),
            "reason": str(self.reason),
        }


# ---------------------------------------------------------------------------
# identifier factory
# ---------------------------------------------------------------------------


def new_client_order_id(
    profile_id: str,
    symbol: str,
    candle_timestamp: pd.Timestamp | datetime | str,
    sequence: int,
) -> str:
    """Build the deterministic client order id of one order.

    The identifier is derived from the profile, the symbol, the timestamp of the
    candle that triggered the decision and a per-candle sequence number, so the
    very same decision always produces the very same id.  That is what makes the
    store and the venue idempotent across a restart (delivery brief D7).

    Parameters
    ----------
    profile_id:
        Owning profile.
    symbol:
        Exchange symbol, e.g. ``"BTC/USDT"`` (``/`` becomes ``_`` and the whole
        symbol is upper-cased).
    candle_timestamp:
        Timestamp of the triggering candle.  A naive timestamp is interpreted as
        UTC, an aware one is converted to UTC.
    sequence:
        Order index inside the same candle (``0`` for the first one).

    Returns
    -------
    str
        ``"<profile_id>-<SYMBOL>-<YYYYMMDDTHHMMSSZ>-<NNNN>"``.

    Raises
    ------
    ValueError
        If ``sequence`` is negative.

    Examples
    --------
    >>> new_client_order_id("btc-paper", "BTC/USDT", "2024-01-01T00:00:00Z", 0)
    'btc-paper-BTC_USDT-20240101T000000Z-0000'
    """
    if sequence < 0:
        raise ValueError(f"sequence must be >= 0, got {sequence}")
    stamp = pd.Timestamp(candle_timestamp)
    stamp_utc = stamp.tz_localize(UTC) if stamp.tz is None else stamp.tz_convert(UTC)
    token = str(symbol).replace("/", "_").upper()
    return f"{profile_id}-{token}-{stamp_utc.strftime('%Y%m%dT%H%M%SZ')}-{sequence:04d}"
