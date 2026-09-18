"""Venue adapters: the simulated :class:`PaperBroker` and the real :class:`CcxtBroker`.

The whole point of the :class:`Broker` seam (delivery brief D4) is that paper and
live trading share **one** code path: the gateway, the runner and the store never
learn which adapter they hold.  Paper and live therefore differ by exactly three
things -- the broker injected by the factory, the live gate (see
:mod:`trading_platform.realtime.risk`) and the configuration -- never by two
execution implementations (D5).

``PaperBroker``
    A deterministic simulated venue.  Fills happen against an explicit
    ``reference_price`` supplied by the caller (in production, the open of the
    candle that follows the signal, exactly like ``strategy/engine.py``), the
    slippage always moves the price against the trader, fees are charged on the
    filled notional, and partial fills are drawn from a generator seeded with
    ``seed ^ sha256(client_order_id)`` so the draw never depends on the order in
    which the orders happen to be submitted.

``CcxtBroker``
    A thin, idempotent adapter over ``ccxt``.  ``ccxt`` is imported **inside the
    method bodies**: ``import trading_platform.realtime.broker`` must stay valid
    with the dev extra only, and a missing extra has to surface as
    :class:`BrokerUnavailableError` naming the ``[exchange]`` extras group rather
    than as an ``ImportError`` at import time.  Every order travels with an
    explicit ``clientOrderId`` and a submission is preceded by a read of that id
    at the venue, so a restart between *submit* and *fill* can never place the
    same order twice (D7).

Nothing in this module knows about strategies, metrics or configuration: it
turns an :class:`~trading_platform.realtime.models.OrderRequest` into a venue
answer and a stream of :class:`~trading_platform.realtime.models.BrokerEvent`.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from trading_platform.core.constants import (
    DEFAULT_FEE_RATE,
    DEFAULT_INITIAL_BALANCE,
    DEFAULT_SLIPPAGE,
    DEFAULT_STAKE_CURRENCY,
    UTC,
)
from trading_platform.core.errors import BrokerError, BrokerUnavailableError, RealtimeError
from trading_platform.realtime.clock import Clock
from trading_platform.realtime.credentials import ExchangeCredentials
from trading_platform.realtime.models import (
    BrokerAck,
    BrokerEvent,
    BrokerEventType,
    Fill,
    Order,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderType,
    ReconciliationReport,
    RunMode,
)

__all__ = ["Broker", "CcxtBroker", "PaperBroker"]

#: Order states that still accept a cancellation (and that a venue still holds).
OPEN_ORDER_STATES: frozenset[OrderState] = frozenset(
    {OrderState.PENDING, OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED}
)

#: Message of the missing-extra error; the extras group is part of the contract.
CCXT_MISSING_MESSAGE = "ccxt is not installed: pip install -e '.[exchange]'"

#: Venue status (ccxt unified vocabulary) -> local :class:`OrderState`.
_VENUE_STATES: dict[str, OrderState] = {
    "open": OrderState.SUBMITTED,
    "new": OrderState.SUBMITTED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "closed": OrderState.FILLED,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELLED,
    "cancelled": OrderState.CANCELLED,
    "expired": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
}

#: Substrings that identify a "this order does not exist at the venue" error.
_NOT_FOUND_HINTS: tuple[str, ...] = (
    "notfound",
    "not found",
    "does not exist",
    "unknown order",
    "no such order",
)


# ---------------------------------------------------------------------------
# small helpers shared by both adapters
# ---------------------------------------------------------------------------


def _as_mapping(raw: Any) -> Mapping[str, Any]:
    """Return ``raw`` as a mapping, or an empty one when it is not a mapping."""
    return raw if isinstance(raw, Mapping) else {}


def _optional_float(value: Any) -> float | None:
    """Return ``value`` as a ``float`` when it is a finite number, else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _order_side(value: Any, default: OrderSide | None = None) -> OrderSide | None:
    """Decode a side, returning ``default`` when the value is unknown."""
    try:
        return OrderSide(str(value).lower())
    except ValueError:
        return default


def _order_type(value: Any, default: OrderType = OrderType.MARKET) -> OrderType:
    """Decode an order type, returning ``default`` when the value is unknown."""
    try:
        return OrderType(str(value).lower())
    except ValueError:
        return default


def _state_from_status(status: Any) -> OrderState:
    """Map a venue status onto the local order state (``SUBMITTED`` by default)."""
    return _VENUE_STATES.get(str(status or "").lower(), OrderState.SUBMITTED)


def _venue_key(payload: Mapping[str, Any]) -> str:
    """Return the local identifier of a venue payload (``clientOrderId`` first)."""
    for field in ("clientOrderId", "client_order_id", "id"):
        value = payload.get(field)
        if value:
            return str(value)
    return ""


def _is_not_found(exc: BaseException) -> bool:
    """Whether ``exc`` means "the venue does not know this order"."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(hint in text for hint in _NOT_FOUND_HINTS)


def _reconcile_orders(
    *,
    local: Sequence[Order],
    venue: Sequence[Order],
    checked_at: pd.Timestamp,
    profile_id: str = "",
) -> ReconciliationReport:
    """Compare the locally known orders against the venue's view.

    Classification
    --------------
    * ``only_at_venue`` -- the venue holds an order the local state ignores;
    * ``only_locally`` -- the local state believes in an order the venue ignores;
    * ``mismatched`` -- both sides know the id but disagree on its *state*.

    Following the frozen contract, a difference of ``filled_quantity`` with an
    identical state is reported in ``details`` only: it makes the report richer
    without turning a benign in-flight partial fill into a degraded profile.
    """
    local_states: dict[str, OrderState] = {}
    local_filled: dict[str, float] = {}
    for order in local:
        local_states.setdefault(order.client_order_id, order.state)
        local_filled.setdefault(order.client_order_id, float(order.filled_quantity))
    venue_states: dict[str, OrderState] = {}
    venue_filled: dict[str, float] = {}
    for order in venue:
        venue_states.setdefault(order.client_order_id, order.state)
        venue_filled.setdefault(order.client_order_id, float(order.filled_quantity))

    both = set(local_states) & set(venue_states)
    only_at_venue = tuple(sorted(set(venue_states) - set(local_states)))
    only_locally = tuple(sorted(set(local_states) - set(venue_states)))
    mismatched = tuple(sorted(key for key in both if local_states[key] is not venue_states[key]))
    quantity_mismatches = tuple(
        sorted(
            key
            for key in both
            if local_filled.get(key) != venue_filled.get(key) and key not in set(mismatched)
        )
    )
    resolved_profile = profile_id
    if not resolved_profile:
        resolved_profile = next((order.profile_id for order in local), "")
    if not resolved_profile:
        resolved_profile = next((order.profile_id for order in venue), "")
    details: dict[str, Any] = {
        "local_orders": len(local_states),
        "venue_orders": len(venue_states),
        "quantity_mismatches": list(quantity_mismatches),
    }
    return ReconciliationReport(
        profile_id=str(resolved_profile),
        ok=not (only_at_venue or only_locally or mismatched),
        checked_at=checked_at,
        matched=len(both) - len(mismatched),
        only_at_venue=only_at_venue,
        only_locally=only_locally,
        mismatched=mismatched,
        details=details,
    )


# ---------------------------------------------------------------------------
# the seam
# ---------------------------------------------------------------------------


@runtime_checkable
class Broker(Protocol):
    """The venue seam every execution path goes through.

    ``mode`` is the mechanical support of the foolproof paper/live separation
    (delivery brief D8d): the gateway asserts it against the profile's mode, so a
    paper profile can never be routed to a live venue.  It is a *property*
    (read-only) precisely because an adapter must not be able to become live
    after it was built.
    """

    @property
    def name(self) -> str:
        """Return the venue name (``"paper"`` or the exchange name)."""
        ...

    @property
    def mode(self) -> RunMode:
        """Return the mode this adapter can serve, immutably."""
        ...

    def submit(self, request: OrderRequest, *, reference_price: float) -> BrokerAck:
        """Send ``request`` to the venue and return its immediate answer."""
        ...

    def cancel(self, client_order_id: str) -> bool:
        """Cancel a working order; ``False`` when it is unknown or terminal."""
        ...

    def poll(self) -> list[BrokerEvent]:
        """Return the events the venue reported since the previous call."""
        ...

    def open_orders(self) -> list[Order]:
        """Return the orders the venue still holds."""
        ...

    def fetch_balance(self) -> float | None:
        """Return the quote-currency balance, or ``None`` when unavailable."""
        ...

    def reconcile(self, expected: Sequence[Order] = ()) -> ReconciliationReport:
        """Compare the venue's view against ``expected`` (the local state)."""
        ...


# ---------------------------------------------------------------------------
# simulated venue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BookPosition:
    """Net position of the simulated venue on one symbol."""

    quantity: float
    average_price: float


class PaperBroker:
    """Deterministic in-process venue used by every paper profile and by tests.

    The broker is the *venue*, not the local book: :meth:`reconcile` compares the
    orders the caller passes in (the persisted local state) against the orders
    this object holds.  :meth:`inject_venue_order` is the documented seam that
    puts an order on the venue side without a network, which is how the
    reconciliation-mismatch path is exercised offline.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        initial_balance: float = DEFAULT_INITIAL_BALANCE,
        fee_rate: float = DEFAULT_FEE_RATE,
        slippage: float = DEFAULT_SLIPPAGE,
        seed: int = 0,
        partial_fill_probability: float = 0.0,
        max_fill_fraction: float = 0.5,
        name: str = "paper",
    ) -> None:
        if initial_balance <= 0:
            raise ValueError(f"initial_balance must be positive, got {initial_balance}")
        if fee_rate < 0:
            raise ValueError(f"fee_rate must be non-negative, got {fee_rate}")
        if slippage < 0:
            raise ValueError(f"slippage must be non-negative, got {slippage}")
        if not 0.0 <= partial_fill_probability <= 1.0:
            raise ValueError(
                f"partial_fill_probability must be within [0, 1], got {partial_fill_probability}"
            )
        if not 0.0 < max_fill_fraction <= 1.0:
            raise ValueError(f"max_fill_fraction must be within (0, 1], got {max_fill_fraction}")
        self._clock = clock
        self._initial_balance = float(initial_balance)
        self._cash = float(initial_balance)
        self._fee_rate = float(fee_rate)
        self._slippage = float(slippage)
        self._seed = int(seed)
        self._partial_fill_probability = float(partial_fill_probability)
        self._max_fill_fraction = float(max_fill_fraction)
        self._name = str(name) or "paper"
        self._orders: dict[str, Order] = {}
        self._events: deque[BrokerEvent] = deque()
        self._positions: dict[str, _BookPosition] = {}
        self._last_price: dict[str, float] = {}
        self._fills_per_order: dict[str, int] = {}
        #: client_order_id -> (remaining quantity, number of polls already done).
        self._pending_completions: dict[str, tuple[float, int]] = {}
        self._poll_count = 0
        self._profile_id = ""

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the configured broker name (``"paper"`` by default)."""
        return self._name

    @property
    def mode(self) -> RunMode:
        """Always :attr:`RunMode.PAPER`; no constructor argument can change it."""
        return RunMode.PAPER

    # -- order lifecycle ---------------------------------------------------

    def submit(self, request: OrderRequest, *, reference_price: float) -> BrokerAck:
        """Accept ``request`` and fill it against ``reference_price``.

        Market orders fill immediately; a limit order fills only when the
        reference price touches it, and then *at* the limit price.  A known
        ``client_order_id`` that is not rejected is answered with the existing
        order instead of being submitted again, so a replay after a restart can
        never double an order (D7).

        Raises
        ------
        BrokerError
            When a limit order carries no price, when the quantity is not
            positive, or when the reference price is not positive.  All three
            checks happen *before* anything is recorded.
        """
        self._validate(request, reference_price)
        existing = self._orders.get(request.client_order_id)
        if existing is not None and existing.state is not OrderState.REJECTED:
            return BrokerAck(
                client_order_id=existing.client_order_id,
                accepted=True,
                state=existing.state,
                broker_order_id=existing.broker_order_id,
                reason=existing.reject_reason,
                submitted_at=existing.created_at,
            )
        now = self._now()
        self._profile_id = request.profile_id
        order = Order(
            client_order_id=request.client_order_id,
            profile_id=request.profile_id,
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            quantity=float(request.quantity),
            state=OrderState.SUBMITTED,
            mode=request.mode,
            created_at=now,
            updated_at=now,
            price=request.price,
            broker_order_id=f"paper-{request.client_order_id}",
        )
        fill_price = self._immediate_fill_price(request, reference_price)
        if fill_price is None:
            self._orders[order.client_order_id] = order
            return self._ack(order, OrderState.SUBMITTED, now)

        quantity = self._immediate_fill_quantity(request)
        fill = self._apply_fill(order, quantity, fill_price)
        complete = quantity >= float(request.quantity)
        order = replace(
            order,
            state=OrderState.FILLED if complete else OrderState.PARTIALLY_FILLED,
            updated_at=now,
            filled_quantity=quantity,
            average_fill_price=fill_price,
        )
        self._orders[order.client_order_id] = order
        if complete:
            self._events.append(
                self._event(BrokerEventType.ORDER_FILLED, order, fill, "order filled", now)
            )
        else:
            self._pending_completions[order.client_order_id] = (
                float(request.quantity) - quantity,
                self._poll_count,
            )
            self._events.append(
                self._event(
                    BrokerEventType.ORDER_PARTIALLY_FILLED,
                    order,
                    fill,
                    "order partially filled",
                    now,
                )
            )
        return self._ack(order, order.state, now)

    def cancel(self, client_order_id: str) -> bool:
        """Cancel a working order; ``False`` for an unknown or terminal one."""
        order = self._orders.get(client_order_id)
        if order is None or order.state not in OPEN_ORDER_STATES:
            return False
        now = self._now()
        updated = replace(order, state=OrderState.CANCELLED, updated_at=now)
        self._orders[client_order_id] = updated
        self._pending_completions.pop(client_order_id, None)
        self._events.append(
            self._event(BrokerEventType.ORDER_CANCELLED, updated, None, "order cancelled", now)
        )
        return True

    def poll(self) -> list[BrokerEvent]:
        """Drain the queued venue events; every event is emitted exactly once.

        An order that was partially filled is completed at the *second* call
        that follows its submission: the first ``poll()`` reports the partial
        fill, the next one reports the completion (frozen contract).
        """
        self._complete_pending()
        events = list(self._events)
        self._events.clear()
        self._poll_count += 1
        return events

    def open_orders(self) -> list[Order]:
        """Return the working orders, sorted by creation time then identifier."""
        working = [order for order in self._orders.values() if order.state in OPEN_ORDER_STATES]
        return sorted(working, key=lambda order: (order.created_at, order.client_order_id))

    def fetch_balance(self) -> float:
        """Return the simulated cash balance."""
        return self._cash

    def restore_cash(self, cash: float) -> None:
        """Re-seed the simulated cash from the durable state (D7 boot seeding).

        A simulated venue forgets everything when the process dies: its own cash
        counter is *not* the truth after a restart, the persisted state is.  The
        orchestrator therefore hands the last cash the profile's equity curve
        recorded back to the venue before the first tick, so the restored position
        is not counted twice (``cash + quantity * mark`` stays equal to the
        persisted equity instead of jumping by the position notional).

        This is deliberately a *restore* seam, not a general balance setter: it is
        never called by the order lifecycle and it changes no position, no order
        and no fill.

        Raises
        ------
        ValueError
            When ``cash`` is not a finite number.
        """
        value = float(cash)
        if not math.isfinite(value):
            raise ValueError(f"restored cash must be finite, got {cash!r}")
        self._cash = value

    def reconcile(self, expected: Sequence[Order] = ()) -> ReconciliationReport:
        """Compare ``expected`` (the local state) against the venue's book."""
        return _reconcile_orders(
            local=tuple(expected),
            venue=tuple(self._orders.values()),
            checked_at=self._now(),
            profile_id=self._profile_id,
        )

    # -- test / operations seams -------------------------------------------

    def inject_venue_order(self, order: Order) -> None:
        """Register an order the local state knows nothing about.

        This is a deliberate test/operations seam: it makes the venue hold an
        order that only exists on its side, which is the offline way to produce
        the ``only_at_venue`` reconciliation mismatch and the DEGRADED status
        that follows it.  It changes no cash and no position.
        """
        self._orders[order.client_order_id] = order
        if not self._profile_id:
            self._profile_id = order.profile_id

    def equity(self, *, reference_prices: Mapping[str, float] | None = None) -> float:
        """Return cash plus the mark-to-market value of the open positions.

        A symbol absent from ``reference_prices`` is marked at its last observed
        fill price, falling back to its average entry price -- never at an
        invented value, so an unmarked book reports the entry value.
        """
        total = self._cash
        for symbol, position in self._positions.items():
            if position.quantity == 0.0:
                continue
            mark = None if reference_prices is None else reference_prices.get(symbol)
            if mark is None:
                mark = self._last_price.get(symbol, position.average_price)
            total += position.quantity * float(mark)
        return total

    @property
    def initial_balance(self) -> float:
        """Return the balance the simulated account started with."""
        return self._initial_balance

    # -- internals ---------------------------------------------------------

    def _now(self) -> pd.Timestamp:
        """Return the current instant through the injected clock (never the wall clock)."""
        return pd.Timestamp(self._clock.now()).tz_convert(UTC)

    def _validate(self, request: OrderRequest, reference_price: float) -> None:
        """Reject an unusable request before anything is recorded."""
        if float(request.quantity) <= 0:
            raise BrokerError("order quantity must be positive")
        if float(reference_price) <= 0:
            raise BrokerError("reference price must be positive")
        if request.type is OrderType.LIMIT and request.price is None:
            raise BrokerError("limit order requires a price")

    def _immediate_fill_price(self, request: OrderRequest, reference_price: float) -> float | None:
        """Return the price an order fills at right now, or ``None`` if it rests."""
        price = float(reference_price)
        if request.type is OrderType.MARKET:
            # The slippage always works against the trader.
            if request.side is OrderSide.BUY:
                return price * (1.0 + self._slippage)
            return price * (1.0 - self._slippage)
        limit = float(request.price) if request.price is not None else 0.0
        if request.side is OrderSide.BUY and price <= limit:
            return limit
        if request.side is OrderSide.SELL and price >= limit:
            return limit
        return None

    def _immediate_fill_quantity(self, request: OrderRequest) -> float:
        """Return the quantity filled by the immediate (first) fill.

        The draw comes from a generator seeded with ``seed ^ sha256(id)``: the
        same identifier always draws the same number, whatever the order of the
        surrounding submissions.
        """
        quantity = float(request.quantity)
        if self._partial_fill_probability <= 0.0:
            return quantity
        digest = hashlib.sha256(request.client_order_id.encode("utf-8")).digest()
        rng = random.Random(self._seed ^ int.from_bytes(digest[:8], "big"))
        if rng.random() >= self._partial_fill_probability:
            return quantity
        partial = self._max_fill_fraction * quantity
        return quantity if partial >= quantity else partial

    def _complete_pending(self) -> None:
        """Fill the remainder of every order armed by an earlier poll cycle."""
        for client_order_id, (remaining, armed_epoch) in list(self._pending_completions.items()):
            if armed_epoch >= self._poll_count:
                continue
            del self._pending_completions[client_order_id]
            order = self._orders.get(client_order_id)
            if order is None or order.state is not OrderState.PARTIALLY_FILLED:
                continue
            price = order.average_fill_price
            if price is None or price <= 0.0:
                price = float(order.price or 0.0)
            if price <= 0.0:
                continue
            quantity = float(order.quantity) - float(order.filled_quantity)
            if quantity <= 0.0:
                quantity = remaining
            if quantity <= 0.0:
                continue
            fill = self._apply_fill(order, quantity, price)
            updated = replace(
                order,
                state=OrderState.FILLED,
                updated_at=self._now(),
                filled_quantity=float(order.quantity),
                average_fill_price=price,
            )
            self._orders[client_order_id] = updated
            self._events.append(
                self._event(
                    BrokerEventType.ORDER_FILLED,
                    updated,
                    fill,
                    "remaining quantity filled",
                    updated.updated_at,
                )
            )

    def _apply_fill(self, order: Order, quantity: float, price: float) -> Fill:
        """Move the cash, update the book and return the resulting fill."""
        fee = self._fee_rate * quantity * price
        notional = quantity * price
        if order.side is OrderSide.BUY:
            self._cash -= notional + fee
            self._update_book(order.symbol, quantity, price)
        else:
            self._cash += notional - fee
            self._update_book(order.symbol, -quantity, price)
        self._last_price[order.symbol] = price
        index = self._fills_per_order.get(order.client_order_id, 0) + 1
        self._fills_per_order[order.client_order_id] = index
        return Fill(
            fill_id=f"{order.client_order_id}-fill-{index:04d}",
            client_order_id=order.client_order_id,
            profile_id=order.profile_id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=fee,
            timestamp=self._now(),
            mode=order.mode,
        )

    def _update_book(self, symbol: str, signed_quantity: float, price: float) -> None:
        """Apply a signed quantity to the book of ``symbol``."""
        current = self._positions.get(symbol)
        if current is None or current.quantity == 0.0:
            self._positions[symbol] = _BookPosition(quantity=signed_quantity, average_price=price)
            return
        new_quantity = current.quantity + signed_quantity
        same_direction = (current.quantity > 0.0) == (signed_quantity > 0.0)
        if same_direction:
            total = abs(current.quantity) + abs(signed_quantity)
            average = (
                current.average_price * abs(current.quantity) + price * abs(signed_quantity)
            ) / total
        elif new_quantity == 0.0:
            average = current.average_price
        elif (new_quantity > 0.0) != (current.quantity > 0.0):
            average = price
        else:
            average = current.average_price
        self._positions[symbol] = _BookPosition(quantity=new_quantity, average_price=average)

    def _event(
        self,
        event_type: BrokerEventType,
        order: Order,
        fill: Fill | None,
        message: str,
        timestamp: pd.Timestamp,
    ) -> BrokerEvent:
        """Build a venue event attached to ``order``."""
        return BrokerEvent(
            event_type=event_type,
            client_order_id=order.client_order_id,
            profile_id=order.profile_id,
            order=order,
            fill=fill,
            message=message,
            timestamp=timestamp,
        )

    def _ack(self, order: Order, state: OrderState, submitted_at: pd.Timestamp) -> BrokerAck:
        """Build the acknowledgement of a freshly accepted order."""
        return BrokerAck(
            client_order_id=order.client_order_id,
            accepted=True,
            state=state,
            broker_order_id=order.broker_order_id,
            reason="",
            submitted_at=submitted_at,
        )


# ---------------------------------------------------------------------------
# real venue (optional extra, lazily imported)
# ---------------------------------------------------------------------------


class CcxtBroker:
    """Idempotent ``ccxt`` adapter used by live profiles.

    The adapter never caches an order locally: every call goes to the venue,
    which is the only authority on the true state of an order.  ``ccxt`` is
    imported inside the method bodies, so this module imports cleanly with the
    dev extra only and a missing extra surfaces as
    :class:`BrokerUnavailableError` at call time.
    """

    def __init__(
        self,
        credentials: ExchangeCredentials,
        *,
        clock: Clock,
        mode: RunMode = RunMode.LIVE,
        exchange: str = "binance",
        fee_rate: float = DEFAULT_FEE_RATE,
        market: str = "spot",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not credentials.configured:
            raise BrokerError("exchange credentials are not configured")
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds}")
        self._credentials = credentials
        self._clock = clock
        self._mode = RunMode(mode)
        self._exchange_name = str(exchange) or str(credentials.exchange) or "binance"
        self._fee_rate = float(fee_rate)
        self._market = str(market) or "spot"
        self._timeout_seconds = float(timeout_seconds)
        self._ccxt_client: Any = None
        self._symbols: dict[str, str] = {}
        self._profile_id = credentials.profile_id
        self._seen_trade_ids: set[str] = set()
        self._open_known: set[str] = set()

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the exchange name."""
        return self._exchange_name

    @property
    def mode(self) -> RunMode:
        """Return the injected mode (``LIVE`` by default)."""
        return self._mode

    # -- order lifecycle ---------------------------------------------------

    def submit(self, request: OrderRequest, *, reference_price: float) -> BrokerAck:
        """Submit ``request`` unless the venue already knows its identifier.

        The read-before-write (`fetch_order`) is what makes a restart safe: an
        order persisted locally but never acknowledged is found at the venue
        instead of being sent a second time (D7).

        Raises
        ------
        BrokerUnavailableError
            When the optional ``ccxt`` extra is not installed.
        BrokerError
            When the credentials are unusable, when the request is malformed, or
            when the venue refuses the order.
        """
        client = self._client()
        self._remember(request)
        existing = self._fetch_existing(client, request.client_order_id, request.symbol)
        if existing is not None:
            return self._ack_from_venue(request.client_order_id, existing)
        self._validate(request)
        try:
            raw = client.create_order(
                request.symbol,
                OrderType(request.type).value,
                OrderSide(request.side).value,
                float(request.quantity),
                request.price,
                {"clientOrderId": request.client_order_id},
            )
        except RealtimeError:
            raise
        except Exception as exc:
            raise BrokerError(f"venue rejected the order {request.client_order_id}: {exc}") from exc
        return self._ack_from_venue(request.client_order_id, _as_mapping(raw))

    def cancel(self, client_order_id: str) -> bool:
        """Ask the venue to cancel an order; ``True`` when the venue accepted."""
        client = self._client()
        self._require(client, "cancel_order")
        symbol = self._symbols.get(client_order_id)
        try:
            if symbol is None:
                client.cancel_order(client_order_id)
            else:
                client.cancel_order(client_order_id, symbol)
        except RealtimeError:
            raise
        except Exception as exc:
            raise BrokerError(f"could not cancel the order {client_order_id}: {exc}") from exc
        return True

    def poll(self) -> list[BrokerEvent]:
        """Map the open orders and the recent trades of the venue to local events.

        A *venue* failure while polling does not abort the loop: it becomes an
        ``ERROR`` event so the runner can mark the profile degraded and keep
        reconciling later.  A *missing extra* is different -- it aborts the call
        with :class:`BrokerUnavailableError`, because no amount of retrying can
        fix it.
        """
        client = self._client()
        now = self._now()
        events: list[BrokerEvent] = []
        _, open_ids, filled_ids = self._read_open_orders(client, now, events)
        self._poll_trades(client, now, events, filled_ids)
        for vanished in sorted(self._open_known - open_ids - filled_ids):
            events.append(
                BrokerEvent(
                    event_type=BrokerEventType.ORDER_CANCELLED,
                    client_order_id=vanished,
                    profile_id=self._profile_id,
                    order=None,
                    fill=None,
                    message="the order is no longer open at the venue and no fill was reported",
                    timestamp=now,
                )
            )
        self._open_known = open_ids
        return events

    def open_orders(self) -> list[Order]:
        """Return the orders the venue currently holds."""
        client = self._client()
        now = self._now()
        events: list[BrokerEvent] = []
        working, open_ids, _ = self._read_open_orders(client, now, events)
        self._open_known = open_ids
        return working

    def fetch_balance(self) -> float | None:
        """Return the quote-currency balance of the account, or ``None``."""
        client = self._client()
        self._require(client, "fetch_balance")
        try:
            raw = client.fetch_balance()
        except RealtimeError:
            raise
        except Exception as exc:
            raise BrokerError(f"could not read the balance: {exc}") from exc
        return _balance_total(_as_mapping(raw), DEFAULT_STAKE_CURRENCY)

    def reconcile(self, expected: Sequence[Order] = ()) -> ReconciliationReport:
        """Compare the venue's open orders against ``expected`` (the local state)."""
        client = self._client()
        self._require(client, "fetch_open_orders")
        now = self._now()
        try:
            raw_open = client.fetch_open_orders()
        except RealtimeError:
            raise
        except Exception as exc:
            raise BrokerError(f"could not reconcile against the venue: {exc}") from exc
        venue = tuple(
            self._order_from_venue(_as_mapping(raw), now, OrderState.SUBMITTED)
            for raw in raw_open or []
        )
        return _reconcile_orders(
            local=tuple(expected),
            venue=venue,
            checked_at=now,
            profile_id=self._profile_id,
        )

    # -- internals ---------------------------------------------------------

    def _now(self) -> pd.Timestamp:
        """Return the current instant through the injected clock."""
        return pd.Timestamp(self._clock.now()).tz_convert(UTC)

    def _client(self) -> Any:
        """Build (once) and return the venue client; the import stays lazy.

        Raises
        ------
        BrokerUnavailableError
            When ``ccxt`` is not installed -- the message names the ``[exchange]``
            extras group so the operator knows exactly what to install.
        BrokerError
            When the exchange is unknown to the installed ``ccxt``, or when the
            client cannot be built (a half-configured credential, for instance).
        """
        if self._ccxt_client is not None:
            return self._ccxt_client
        try:
            import ccxt
        except ImportError:
            raise BrokerUnavailableError(CCXT_MISSING_MESSAGE) from None
        factory = getattr(ccxt, self._exchange_name, None)
        if factory is None:
            raise BrokerError(
                f"exchange {self._exchange_name!r} is not supported by the installed ccxt"
            )
        config: dict[str, Any] = {
            "apiKey": self._credentials.api_key,
            "secret": self._credentials.api_secret,
            "enableRateLimit": True,
            "timeout": int(self._timeout_seconds * 1000),
            "options": {"defaultType": self._market},
        }
        if self._credentials.password:
            config["password"] = self._credentials.password
        try:
            client: Any = factory(config)
        except Exception as exc:
            raise BrokerError(f"could not build the {self._exchange_name} client: {exc}") from exc
        self._ccxt_client = client
        return client

    def _remember(self, request: OrderRequest) -> None:
        """Remember which symbol an order belongs to (needed to cancel it)."""
        self._symbols[request.client_order_id] = request.symbol
        if not self._profile_id:
            self._profile_id = request.profile_id

    def _require(self, client: Any, method: str) -> None:
        """Raise when the venue client does not expose ``method``.

        A missing capability is a configuration error (wrong exchange class,
        unexpected ccxt version), not a transient venue failure, so it is raised
        instead of being turned into an ``ERROR`` event.
        """
        if getattr(client, method, None) is None:
            raise BrokerError(f"the {self._exchange_name} client does not support {method}")

    def _validate(self, request: OrderRequest) -> None:
        """Reject an unusable request before it reaches the venue."""
        if float(request.quantity) <= 0:
            raise BrokerError("order quantity must be positive")
        if request.type is OrderType.LIMIT and request.price is None:
            raise BrokerError("limit order requires a price")

    def _fetch_existing(
        self, client: Any, client_order_id: str, symbol: str
    ) -> Mapping[str, Any] | None:
        """Return the venue's view of ``client_order_id``, or ``None``.

        A client without ``fetch_order`` cannot be probed and is simply
        submitted to (the caller keeps its own idempotency through the store); a
        "not found" answer is the normal case of a brand new order.
        """
        fetch = getattr(client, "fetch_order", None)
        if fetch is None:
            return None
        try:
            raw = fetch(client_order_id, symbol)
        except RealtimeError:
            raise
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise BrokerError(f"could not read the venue order {client_order_id}: {exc}") from exc
        payload = _as_mapping(raw)
        return payload or None

    def _ack_from_venue(self, client_order_id: str, payload: Mapping[str, Any]) -> BrokerAck:
        """Map a venue order payload onto a :class:`BrokerAck`."""
        state = _state_from_status(payload.get("status"))
        reason = ""
        if state is OrderState.REJECTED:
            info = _as_mapping(payload.get("info"))
            reason = str(payload.get("reason") or info.get("msg") or "venue rejected the order")
        broker_order_id = payload.get("id")
        return BrokerAck(
            client_order_id=client_order_id,
            accepted=state is not OrderState.REJECTED,
            state=state,
            broker_order_id=None if broker_order_id is None else str(broker_order_id),
            reason=reason,
            submitted_at=self._now(),
        )

    def _order_from_venue(
        self,
        payload: Mapping[str, Any],
        now: pd.Timestamp,
        default_state: OrderState,
    ) -> Order:
        """Build a local :class:`Order` from a venue payload."""
        key = _venue_key(payload)
        status = payload.get("status")
        state = default_state if status is None else _state_from_status(status)
        broker_order_id = payload.get("id")
        return Order(
            client_order_id=key,
            profile_id=str(payload.get("profile_id") or self._profile_id),
            symbol=str(payload.get("symbol") or self._symbols.get(key, "")),
            side=_order_side(payload.get("side"), OrderSide.BUY) or OrderSide.BUY,
            type=_order_type(payload.get("type")),
            quantity=_optional_float(payload.get("amount")) or 0.0,
            state=state,
            mode=self._mode,
            created_at=now,
            updated_at=now,
            filled_quantity=_optional_float(payload.get("filled")) or 0.0,
            price=_optional_float(payload.get("price")),
            average_fill_price=_optional_float(payload.get("average")),
            broker_order_id=None if broker_order_id is None else str(broker_order_id),
        )

    def _read_open_orders(
        self, client: Any, now: pd.Timestamp, events: list[BrokerEvent]
    ) -> tuple[list[Order], set[str], set[str]]:
        """Read the venue's open orders.

        An order is announced with ``ORDER_ACCEPTED`` the first time it is seen
        working (never on every poll), and every order the venue reports as
        terminal is announced once with its terminal event.  The returned triple
        is ``(working orders, still open ids, filled ids)``.
        """
        self._require(client, "fetch_open_orders")
        try:
            raw_open = client.fetch_open_orders()
        except RealtimeError:
            raise
        except Exception as exc:
            events.append(self._error_event(f"could not read the open orders: {exc}", now))
            return [], set(), set()
        working: list[Order] = []
        open_ids: set[str] = set()
        filled_ids: set[str] = set()
        for raw in raw_open or []:
            payload = _as_mapping(raw)
            key = _venue_key(payload)
            if not key:
                continue
            state = _state_from_status(payload.get("status"))
            if state in OPEN_ORDER_STATES:
                open_ids.add(key)
                order = self._order_from_venue(payload, now, state)
                working.append(order)
                if key not in self._open_known:
                    events.append(
                        self._event_for(
                            BrokerEventType.ORDER_ACCEPTED,
                            order,
                            None,
                            "the venue holds the order",
                            now,
                        )
                    )
                continue
            order = self._order_from_venue(payload, now, state)
            if state is OrderState.FILLED:
                filled_ids.add(key)
                events.append(
                    self._event_for(
                        BrokerEventType.ORDER_FILLED,
                        order,
                        None,
                        "venue reports a filled order",
                        now,
                    )
                )
                continue
            event_type = (
                BrokerEventType.ORDER_REJECTED
                if state is OrderState.REJECTED
                else BrokerEventType.ORDER_CANCELLED
            )
            events.append(
                self._event_for(
                    event_type,
                    order,
                    None,
                    f"venue reports the status {payload.get('status')!r}",
                    now,
                )
            )
        return working, open_ids, filled_ids

    def _poll_trades(
        self,
        client: Any,
        now: pd.Timestamp,
        events: list[BrokerEvent],
        filled_ids: set[str],
    ) -> None:
        """Read the recent trades and append one fill event per unseen trade."""
        self._require(client, "fetch_my_trades")
        symbols = sorted(set(self._symbols.values()))
        trades: list[Any] = []
        try:
            if symbols:
                for symbol in symbols:
                    trades.extend(client.fetch_my_trades(symbol) or [])
            else:
                trades.extend(client.fetch_my_trades() or [])
        except RealtimeError:
            raise
        except Exception as exc:
            events.append(self._error_event(f"could not read the trades: {exc}", now))
            return
        for raw in trades:
            payload = _as_mapping(raw)
            fill = self._fill_from_trade(payload, now)
            if fill is None or fill.fill_id in self._seen_trade_ids:
                continue
            self._seen_trade_ids.add(fill.fill_id)
            filled_ids.add(fill.client_order_id)
            events.append(
                BrokerEvent(
                    event_type=BrokerEventType.ORDER_FILLED,
                    client_order_id=fill.client_order_id,
                    profile_id=fill.profile_id,
                    order=None,
                    fill=fill,
                    message="venue reports a trade",
                    timestamp=fill.timestamp,
                )
            )

    def _fill_from_trade(self, payload: Mapping[str, Any], now: pd.Timestamp) -> Fill | None:
        """Map a venue trade onto a :class:`Fill`, or ``None`` when unusable."""
        side = _order_side(payload.get("side"))
        if side is None:
            return None
        client_order_id = str(payload.get("clientOrderId") or payload.get("order") or "")
        if not client_order_id:
            return None
        trade_id = payload.get("id")
        fill_id = (
            f"ccxt-{trade_id}"
            if trade_id
            else f"ccxt-{client_order_id}-{len(self._seen_trade_ids)}"
        )
        price = _optional_float(payload.get("price")) or 0.0
        quantity = _optional_float(payload.get("amount")) or 0.0
        fee = _as_mapping(payload.get("fee"))
        fee_cost = _optional_float(fee.get("cost"))
        if fee_cost is None:
            fee_cost = self._fee_rate * quantity * price
        symbol = str(payload.get("symbol") or self._symbols.get(client_order_id, ""))
        timestamp = now
        raw_timestamp = payload.get("timestamp")
        if raw_timestamp is not None:
            try:
                timestamp = pd.Timestamp(int(raw_timestamp), unit="ms", tz=UTC)
            except (TypeError, ValueError):
                timestamp = now
        return Fill(
            fill_id=fill_id,
            client_order_id=client_order_id,
            profile_id=self._profile_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee_cost,
            timestamp=timestamp,
            mode=self._mode,
        )

    def _event_for(
        self,
        event_type: BrokerEventType,
        order: Order,
        fill: Fill | None,
        message: str,
        timestamp: pd.Timestamp,
    ) -> BrokerEvent:
        """Build a venue event attached to ``order``."""
        return BrokerEvent(
            event_type=event_type,
            client_order_id=order.client_order_id,
            profile_id=order.profile_id,
            order=order,
            fill=fill,
            message=message,
            timestamp=timestamp,
        )

    def _error_event(self, message: str, now: pd.Timestamp) -> BrokerEvent:
        """Build a venue error event (no order attached)."""
        return BrokerEvent(
            event_type=BrokerEventType.ERROR,
            client_order_id="",
            profile_id=self._profile_id,
            order=None,
            fill=None,
            message=message,
            timestamp=now,
        )


def _balance_total(payload: Mapping[str, Any], quote: str) -> float | None:
    """Extract the ``quote`` total from a ccxt balance payload.

    ``None`` is returned when the payload carries no usable total, so the caller
    can tell "no balance" from "a zero balance".  The quote currency is looked up
    in the ``total`` block first (the ccxt unified shape), then among the
    per-currency blocks, and only then is a single-currency ``total`` accepted as
    a fallback.
    """
    total = payload.get("total")
    entry = payload.get(quote)
    if isinstance(total, Mapping) and total.get(quote) is not None:
        return _optional_float(total.get(quote))
    if isinstance(entry, Mapping) and entry.get("total") is not None:
        return _optional_float(entry.get("total"))
    if isinstance(total, Mapping):
        values = [_optional_float(value) for value in total.values()]
        usable = [value for value in values if value is not None]
        return usable[0] if len(usable) == 1 else None
    return None
