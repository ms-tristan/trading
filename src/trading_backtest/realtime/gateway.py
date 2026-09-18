"""The single order lifecycle, shared by paper and live profiles (brief D5).

There is **exactly one** execution path in this project: :class:`ExecutionGateway`.
Paper and live differ by three things only -- the :class:`~trading_backtest.realtime.broker.Broker`
adapter injected by the orchestrator, the
:class:`~trading_backtest.realtime.risk.LiveTradingGate` consulted before a ``live``
order is routed, and the configuration.  This module therefore carries no
``if paper`` / ``if live`` branch beyond those two: the very same steps run for
both modes, and the only thing that changes is *which venue answers*.

Order of operations of :meth:`ExecutionGateway.submit` -- frozen
---------------------------------------------------------------
1. the live gate is consulted for a ``live`` profile (it may raise
   :class:`~trading_backtest.core.errors.LiveTradingForbiddenError`);
2. the injected broker's ``mode`` is asserted against the profile's mode
   (the foolproof paper/live separation of D8d);
3. the per-profile :class:`~trading_backtest.realtime.risk.RiskManager` verdict is
   applied **before anything reaches the venue**;
4. an order the store already knows about (and that is not ``REJECTED``) is
   returned unchanged -- this is what makes a restart between *submit* and *fill*
   safe (D7);
5. the order is persisted as ``PENDING`` and only then submitted;
6. a refusal is persisted as ``REJECTED`` and raised as
   :class:`~trading_backtest.core.errors.OrderRejectedError`;
7. an acceptance is persisted with the venue's own state.

Fill accounting (:meth:`poll`)
------------------------------
The venue is polled, every event is folded into the stored order, and every *new*
fill is applied to the local position: weighted-average entry price, signed
quantity, and -- when the quantity returns to zero -- one
:class:`~trading_backtest.core.models.TradeRecord` covering the whole round trip.
A replayed fill (the store already knows it) is never counted twice, never
enters the drain buffer twice, and never moves a position twice.
The equity point is **not** written here: the runner owns that cadence, and
:meth:`snapshot_fields` hands it the numbers.

Restart semantics
-----------------
The gateway is reconstructed around the *persisted* store on every boot, so the
orders, the fills and the positions survive a restart.  The provenance of a round
trip (the stop price and the reason of the request that opened it, and the reason
of the request that closed it) lives in memory only: after a restart the stop
price of a position falls back to the persisted ``Position.stop_price`` and an
unknown exit reason falls back to ``ExitReason.SIGNAL``.  Nothing is invented from
a missing value.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import pandas as pd

from trading_backtest.config.models import ProfileConfig
from trading_backtest.core.constants import UTC
from trading_backtest.core.errors import (
    GatewayError,
    KillSwitchActiveError,
    OrderRejectedError,
    RiskLimitExceededError,
)
from trading_backtest.core.models import Direction, ExitReason, TradeRecord
from trading_backtest.realtime.clock import Clock
from trading_backtest.realtime.models import (
    BrokerAck,
    BrokerEvent,
    BrokerEventType,
    EngineCounters,
    Fill,
    Order,
    OrderRequest,
    OrderSide,
    OrderState,
    Position,
    ReconciliationReport,
    RunMode,
)
from trading_backtest.realtime.risk import LiveTradingGate, RiskDecision, RiskManager

if TYPE_CHECKING:  # the foreign seams are imported for annotations only
    from trading_backtest.realtime.broker import Broker
    from trading_backtest.realtime.store import StateStore

__all__ = ["ExecutionGateway"]

LOGGER = logging.getLogger(__name__)

#: Quantity below which a position is considered flat.  Float arithmetic on
#: prices and quantities rarely lands exactly on ``0.0``, so the round trip is
#: closed on this tolerance instead of on an exact comparison.
FLAT_EPSILON = 1e-12

#: Message raised when the injected broker cannot serve the profile's mode.
#: The two allowed branches of this module are exactly the live gate (step 1 of
#: ``submit``) and this one (step 2): a mapping keeps the rule mechanical.
_MODE_MISMATCH: dict[RunMode, str] = {
    RunMode.LIVE: "profile {profile_id!r} is live but the injected broker {broker!r} is not",
    RunMode.PAPER: "a paper profile can never be routed to a live broker: {broker!r}",
}

#: Order state applied to the stored row when a venue event carries no full order
#: object.  ``BrokerEventType.ERROR`` is deliberately absent: a venue error says
#: nothing about the state of the order, so it never overwrites one.
_EVENT_STATES: dict[BrokerEventType, OrderState] = {
    BrokerEventType.ORDER_ACCEPTED: OrderState.SUBMITTED,
    BrokerEventType.ORDER_REJECTED: OrderState.REJECTED,
    BrokerEventType.ORDER_PARTIALLY_FILLED: OrderState.PARTIALLY_FILLED,
    BrokerEventType.ORDER_FILLED: OrderState.FILLED,
    BrokerEventType.ORDER_CANCELLED: OrderState.CANCELLED,
}

#: How many persisted orders are handed to the venue for reconciliation.  The
#: comparison is keyed by ``client_order_id``; a truncated local view would make
#: the venue report a long-lived order as ``only_at_venue``, which would degrade a
#: healthy profile.  The bound is therefore generous on purpose.
RECONCILE_ORDER_LIMIT = 10_000


# ---------------------------------------------------------------------------
# round-trip accumulator (private)
# ---------------------------------------------------------------------------


@dataclass
class _RoundTrip:
    """Mutable accumulator of the round trip currently open on one symbol.

    It aggregates *every* fill between the opening fill and the fill that returns
    the position to zero, which is what a partial fill inside a round trip needs:
    the fees add up and the average entry/exit prices stay weighted by quantity.
    """

    symbol: str
    direction: Direction
    stop_price: float | None = None
    reason: str = ""
    entry_quantity: float = 0.0
    entry_notional: float = 0.0
    entry_fees: float = 0.0
    entry_time: pd.Timestamp | None = None
    exit_quantity: float = 0.0
    exit_notional: float = 0.0
    exit_fees: float = 0.0
    exit_time: pd.Timestamp | None = None

    def add_entry(self, quantity: float, price: float, fee: float, timestamp: pd.Timestamp) -> None:
        """Fold one opening (or extending) fill into the accumulator."""
        self.entry_quantity += abs(float(quantity))
        self.entry_notional += abs(float(quantity)) * float(price)
        self.entry_fees += float(fee)
        if self.entry_time is None:
            self.entry_time = pd.Timestamp(timestamp)

    def add_exit(
        self,
        quantity: float,
        price: float,
        fee: float,
        timestamp: pd.Timestamp,
        *,
        reason: str = "",
    ) -> None:
        """Fold one reducing fill into the accumulator, remembering its reason."""
        self.exit_quantity += abs(float(quantity))
        self.exit_notional += abs(float(quantity)) * float(price)
        self.exit_fees += float(fee)
        self.exit_time = pd.Timestamp(timestamp)
        if reason:
            self.reason = str(reason)

    @property
    def entry_price(self) -> float:
        """Return the quantity-weighted average entry price (``0.0`` when unknown)."""
        if self.entry_quantity <= 0.0:
            # Unreachable: to_trade() refuses an accumulator without an entry.
            return 0.0  # pragma: no cover
        return self.entry_notional / self.entry_quantity

    @property
    def exit_price(self) -> float:
        """Return the quantity-weighted average exit price (``0.0`` when unknown)."""
        if self.exit_quantity <= 0.0:
            # Unreachable: only a fill that reduces the position closes a round trip.
            return 0.0  # pragma: no cover
        return self.exit_notional / self.exit_quantity

    def gross_pnl(self) -> float:
        """Return the gross profit and loss of the aggregate (before fees)."""
        quantity = self.entry_quantity
        if quantity <= 0.0:
            # Unreachable: to_trade() refuses an accumulator without an entry.
            return 0.0  # pragma: no cover
        difference = self.exit_price - self.entry_price
        return difference * quantity if self.direction is Direction.LONG else -difference * quantity

    def realized_on(self, quantity: float, price: float) -> float:
        """Return the gross profit and loss realized by closing ``quantity`` units.

        Used when a fill only *reduces* a position: the average entry price of the
        round trip is the reference, and fees are left out (they are charged to the
        round trip, not to the open position).
        """
        difference = float(price) - self.entry_price
        sign = 1.0 if self.direction is Direction.LONG else -1.0
        return sign * difference * abs(float(quantity))

    def to_trade(self, profile_id: str) -> TradeRecord | None:
        """Build the closed round trip, or ``None`` when nothing can be proven.

        The record is only built from observed fills: an accumulator with no entry
        quantity or no timestamp yields ``None`` rather than a fabricated trade.
        """
        entry_time = self.entry_time
        if entry_time is None or self.entry_quantity <= 0.0:
            # Unreachable: an accumulator is only built from an observed fill.
            return None  # pragma: no cover
        exit_time = self.exit_time if self.exit_time is not None else entry_time
        fees = self.entry_fees + self.exit_fees
        pnl = self.gross_pnl() - fees
        notional = self.entry_price * self.entry_quantity
        return TradeRecord(
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=self.entry_price,
            exit_price=self.exit_price,
            size=self.entry_quantity,
            direction=self.direction,
            pnl=pnl,
            pnl_pct=(pnl / notional) if notional else 0.0,
            fees=fees,
            exit_reason=_exit_reason(self.reason),
            duration_minutes=(exit_time - entry_time).total_seconds() / 60.0,
            stop_price=self.stop_price,
            params_id=str(profile_id),
        )


def _exit_reason(reason: str) -> ExitReason:
    """Map a free-form closing reason onto the frozen :class:`ExitReason`.

    An unknown value -- including the strategy vocabulary (``"exit_long"``) that
    the runner may forward -- falls back to :attr:`ExitReason.SIGNAL`, which is the
    documented default of a signal-driven exit.
    """
    try:
        return ExitReason(str(reason).strip().lower())
    except ValueError:
        return ExitReason.SIGNAL


def _signed_quantity(side: OrderSide, quantity: float) -> float:
    """Return ``quantity`` signed by the side of an order or of a fill."""
    return float(quantity) if side is OrderSide.BUY else -float(quantity)


# ---------------------------------------------------------------------------
# the gateway
# ---------------------------------------------------------------------------


class ExecutionGateway:
    """Route one profile's orders to one venue, and fold the venue's answers back.

    Parameters
    ----------
    profile:
        The profile this gateway serves; its ``mode`` picks the run mode and its
        ``initial_balance`` is the fallback cash when the venue reports no balance.
    broker:
        The injected venue adapter (``PaperBroker`` or ``CcxtBroker``).  The
        gateway never learns which one it holds beyond the ``mode`` assertion.
    store:
        Durable state; every order, fill and position goes through it.
    clock:
        Time seam; the gateway never reads the wall clock directly.
    risk:
        Optional per-profile limits.  ``None`` disables them entirely (the
        orchestrator always injects one, the tests may not).
    live_gate:
        Optional live-trading gate.  ``None`` builds the real one lazily, reading
        ``TB_ALLOW_LIVE_TRADING`` from the environment.
    counters:
        Optional in-process counters; the gateway rebinds its own copy on every
        increment, so read them through :attr:`counters`.

    Raises
    ------
    GatewayError
        From :meth:`submit` when the injected broker cannot serve the profile's
        mode (the mechanical paper/live separation).
    """

    def __init__(
        self,
        *,
        profile: ProfileConfig,
        broker: Broker,
        store: StateStore,
        clock: Clock,
        risk: RiskManager | None = None,
        live_gate: LiveTradingGate | None = None,
        counters: EngineCounters | None = None,
    ) -> None:
        self._profile = profile
        self._broker = broker
        self._store = store
        self._clock = clock
        self._risk = risk
        self._live_gate = live_gate
        self._counters: EngineCounters = EngineCounters() if counters is None else counters
        self._fills: deque[Fill] = deque()
        self._closed_trade: TradeRecord | None = None
        self._round_trips: dict[str, _RoundTrip] = {}
        self._order_reasons: dict[str, str] = {}
        self._order_stops: dict[str, float | None] = {}
        self._mark_price: float | None = None

    # -- introspection ------------------------------------------------------

    @property
    def profile(self) -> ProfileConfig:
        """Return the profile this gateway serves."""
        return self._profile

    @property
    def profile_id(self) -> str:
        """Return the identifier of the profile this gateway serves."""
        return str(self._profile.id)

    @property
    def mode(self) -> RunMode:
        """Return the run mode of the profile, as a :class:`RunMode`."""
        return RunMode(self._profile.mode)

    @property
    def counters(self) -> EngineCounters:
        """Return the current in-process counters (immutable snapshot)."""
        return self._counters

    @property
    def broker(self) -> Broker:
        """Return the injected venue adapter."""
        return self._broker

    def __repr__(self) -> str:
        """Return a short, secret-free representation of the gateway."""
        return (
            f"ExecutionGateway(profile_id={self.profile_id!r}, mode={self.mode.value!r}, "
            f"broker={self._broker.name!r})"
        )

    # -- order lifecycle ----------------------------------------------------

    def submit(
        self,
        request: OrderRequest,
        *,
        reference_price: float,
        equity: float,
        open_positions: int,
        position_notional: float,
        daily_pnl: float,
        daily_trades: int,
        peak_equity: float,
        closes_position: bool = False,
    ) -> Order:
        """Run the frozen order lifecycle and return the persisted order.

        Parameters
        ----------
        request:
            The order to route; its ``client_order_id`` is the idempotency key.
        reference_price:
            Price the order would fill at (in production the open of the candle
            that follows the signal, exactly like ``strategy/engine.py``).  It is
            also the notional reference handed to the risk manager and the mark
            kept for :meth:`snapshot_fields`.
        equity, open_positions, position_notional, daily_pnl, daily_trades, peak_equity:
            The risk inputs the runner measures *before* the order (see
            :meth:`RiskManager.check_order`).  They are ignored when no risk
            manager is injected.
        closes_position:
            Forwarded to the risk manager, which relaxes the position caps for an
            order that reduces an existing position.

        Returns
        -------
        Order
            The order as persisted after the venue's answer -- or, when the store
            already knows a non-rejected order with this identifier, that stored
            order, unchanged and without any venue call.

        Raises
        ------
        LiveTradingForbiddenError
            For a ``live`` profile that is not armed through the environment.
        GatewayError
            When the injected broker's mode contradicts the profile's mode.
        RiskLimitExceededError
            When a per-profile limit blocks the order.
        KillSwitchActiveError
            When the blocking limit is the global kill switch.
        OrderRejectedError
            When the venue refuses the order.
        """
        self._arm_live_profile()
        self._assert_broker_mode()
        self._check_risk(
            request,
            reference_price=reference_price,
            equity=equity,
            open_positions=open_positions,
            position_notional=position_notional,
            daily_pnl=daily_pnl,
            daily_trades=daily_trades,
            peak_equity=peak_equity,
            closes_position=closes_position,
        )
        existing = self._store.get_order(request.client_order_id)
        if existing is not None and existing.state is not OrderState.REJECTED:
            LOGGER.info(
                "realtime.gateway.order_idempotent",
                extra={
                    "event": "order_idempotent",
                    "profile_id": self.profile_id,
                    "client_order_id": existing.client_order_id,
                    "state": existing.state.value,
                },
            )
            return existing

        now = self._now()
        pending = Order(
            client_order_id=request.client_order_id,
            profile_id=request.profile_id,
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            quantity=float(request.quantity),
            state=OrderState.PENDING,
            mode=self.mode,
            created_at=now,
            updated_at=now,
            price=request.price,
        )
        self._store.upsert_order(pending)
        self._remember_request(request)
        self._mark_price = float(reference_price)

        ack = self._broker.submit(request, reference_price=reference_price)
        if not ack.accepted:
            rejected = replace(
                pending,
                state=OrderState.REJECTED,
                broker_order_id=ack.broker_order_id,
                reject_reason=str(ack.reason),
                updated_at=self._now(),
            )
            self._store.upsert_order(rejected)
            self._counters = replace(
                self._counters, orders_rejected=self._counters.orders_rejected + 1
            )
            LOGGER.warning(
                "realtime.gateway.order_rejected",
                extra={
                    "event": "order_rejected",
                    "profile_id": self.profile_id,
                    "client_order_id": request.client_order_id,
                    "symbol": request.symbol,
                    "reason": str(ack.reason),
                },
            )
            raise OrderRejectedError(f"order {request.client_order_id} was rejected: {ack.reason}")

        accepted = replace(
            pending,
            state=_order_state(ack),
            broker_order_id=ack.broker_order_id,
            updated_at=self._now(),
        )
        self._store.upsert_order(accepted)
        self._counters = replace(
            self._counters, orders_submitted=self._counters.orders_submitted + 1
        )
        LOGGER.info(
            "realtime.gateway.order_submitted",
            extra={
                "event": "order_submitted",
                "profile_id": self.profile_id,
                "client_order_id": accepted.client_order_id,
                "symbol": accepted.symbol,
                "side": accepted.side.value,
                "type": accepted.type.value,
                "quantity": float(accepted.quantity),
                "state": accepted.state.value,
                "mode": accepted.mode.value,
            },
        )
        return accepted

    def poll(self) -> list[BrokerEvent]:
        """Fold every event the venue reported into the store and return them.

        A fill is applied once: a replayed fill (already known by the store) is
        logged and skipped, so a restart can never count the same execution twice.
        """
        events = self._broker.poll()
        for event in events:
            self._apply_event(event)
        return events

    def drain_fills(self) -> list[Fill]:
        """Return the fills observed since the previous drain, oldest first."""
        fills = list(self._fills)
        self._fills.clear()
        return fills

    def closed_trade(self) -> TradeRecord | None:
        """Return the round trip completed by the last :meth:`poll`, consumed once.

        ``None`` means no round trip was completed since the previous call.  When
        two round trips complete inside the same poll the most recent one is
        returned; both are persisted, so nothing is lost durably.
        """
        trade = self._closed_trade
        self._closed_trade = None
        return trade

    def open_orders(self) -> list[Order]:
        """Return the orders the venue still holds."""
        return list(self._broker.open_orders())

    def cancel(self, client_order_id: str) -> bool:
        """Ask the venue to cancel a working order; ``False`` when it cannot.

        The stored order is left untouched: the venue reports the resulting state
        through :meth:`poll`, which is the single writer of the local order state.
        """
        cancelled = bool(self._broker.cancel(client_order_id))
        LOGGER.info(
            "realtime.gateway.order_cancel",
            extra={
                "event": "order_cancel_requested",
                "profile_id": self.profile_id,
                "client_order_id": str(client_order_id),
                "cancelled": cancelled,
            },
        )
        return cancelled

    def reconcile(self) -> ReconciliationReport:
        """Compare the persisted orders against the venue's view, and forward it.

        The gateway reports; it never repairs.  Deciding what a mismatch means
        (typically marking the profile ``degraded``) belongs to the orchestrator,
        and silently "fixing" a divergence would hide exactly what reconciliation
        exists to surface.
        """
        expected: Sequence[Order] = self._store.list_orders(
            self.profile_id, limit=RECONCILE_ORDER_LIMIT
        )
        report = self._broker.reconcile(expected)
        if report.ok:
            LOGGER.debug(
                "realtime.gateway.reconciliation_ok",
                extra={
                    "event": "reconciliation_ok",
                    "profile_id": self.profile_id,
                    "matched": int(report.matched),
                },
            )
        else:
            LOGGER.warning(
                "realtime.gateway.reconciliation_mismatch",
                extra={
                    "event": "reconciliation_mismatch",
                    "profile_id": self.profile_id,
                    "only_at_venue": list(report.only_at_venue),
                    "only_locally": list(report.only_locally),
                    "mismatched": list(report.mismatched),
                },
            )
        return report

    # -- account reads ------------------------------------------------------

    def balance(self) -> float | None:
        """Return the venue's quote-currency balance, or ``None`` when unavailable."""
        value = self._broker.fetch_balance()
        return None if value is None else float(value)

    def cash(self) -> float:
        """Return the cash used by the equity maths (falls back to the initial balance).

        A venue that cannot report a balance (some ``ccxt`` exchanges in restricted
        modes) must not make equity unreadable, so the profile's configured initial
        balance is used instead of inventing a ``0.0``.
        """
        value = self.balance()
        return float(self._profile.initial_balance) if value is None else float(value)

    def equity(self, *, reference_price: float) -> float:
        """Return ``cash + quantity * reference_price`` for the profile's symbol."""
        position = self.position()
        quantity = 0.0 if position is None else float(position.quantity)
        return self.cash() + quantity * float(reference_price)

    def position(self, symbol: str | None = None) -> Position | None:
        """Return the open position on ``symbol`` (default: the profile's symbol)."""
        resolved = str(self._profile.symbol) if symbol is None else str(symbol)
        return self._store.get_position(self.profile_id, resolved)

    def snapshot_fields(self) -> dict[str, Any]:
        """Return the read-model inputs of this profile: equity, cash, positions.

        The mark price is the last reference price the gateway saw (the runner
        submits its orders with the candle open, so it is always fresh in
        production); before the first order -- and after a restart -- the position's
        average entry price is used instead, then ``0.0``.  No HTTP concern lives
        here: the monitor turns these numbers into a payload.
        """
        position = self.position()
        quantity = 0.0 if position is None else float(position.quantity)
        mark = self._mark_price
        if mark is None:
            mark = 0.0 if position is None else float(position.average_price)
        cash = self.cash()
        position_value = quantity * float(mark)
        return {
            "equity": cash + position_value,
            "cash": cash,
            "position_value": position_value,
            "open_positions": len(self._store.list_positions(self.profile_id)),
        }

    # -- internals: submission guards ---------------------------------------

    def _gate(self) -> LiveTradingGate:
        """Return the injected live gate, building the real one lazily."""
        if self._live_gate is None:
            self._live_gate = LiveTradingGate()
        return self._live_gate

    def _arm_live_profile(self) -> None:
        """Require the explicit live opt-in for a ``live`` profile."""
        if self._profile.mode == "live":
            self._gate().check(self._profile)

    def _assert_broker_mode(self) -> None:
        """Refuse to route a profile to a venue that cannot serve its mode.

        Raises
        ------
        GatewayError
            When the broker's mode contradicts the profile's mode, or when the
            broker reports a mode that is not a known :class:`RunMode`.
        """
        expected = self.mode
        try:
            actual = RunMode(self._broker.mode)
        except ValueError as exc:
            raise GatewayError(
                f"broker {self._broker.name!r} reports an unknown mode: {exc}"
            ) from exc
        if actual is not expected:
            raise GatewayError(
                _MODE_MISMATCH[expected].format(
                    profile_id=self.profile_id, broker=self._broker.name
                )
            )

    def _check_risk(
        self,
        request: OrderRequest,
        *,
        reference_price: float,
        equity: float,
        open_positions: int,
        position_notional: float,
        daily_pnl: float,
        daily_trades: int,
        peak_equity: float,
        closes_position: bool,
    ) -> None:
        """Apply the risk verdict before anything reaches the venue.

        Nothing is persisted and the broker is never called on a rejection.
        """
        risk = self._risk
        if risk is None:
            return
        decision = risk.check_order(
            request,
            reference_price=reference_price,
            equity=equity,
            open_positions=open_positions,
            position_notional=position_notional,
            daily_pnl=daily_pnl,
            daily_trades=daily_trades,
            peak_equity=peak_equity,
            closes_position=closes_position,
        )
        if decision.allowed:
            return
        self._counters = replace(self._counters, risk_rejections=self._counters.risk_rejections + 1)
        self._log_risk_rejection(request, decision, reference_price=reference_price)
        if decision.limit == "kill_switch":
            raise KillSwitchActiveError(decision.reason)
        raise RiskLimitExceededError(decision.reason, [decision.limit])

    def _log_risk_rejection(
        self, request: OrderRequest, decision: RiskDecision, *, reference_price: float
    ) -> None:
        """Emit the structured rejection log (limit, reason, order context)."""
        LOGGER.warning(
            "realtime.gateway.risk_rejected",
            extra={
                "event": "risk_rejected",
                "profile_id": self.profile_id,
                "client_order_id": request.client_order_id,
                "symbol": request.symbol,
                "limit": str(decision.limit),
                "reason": str(decision.reason),
                "quantity": float(request.quantity),
                "reference_price": float(reference_price),
            },
        )

    # -- internals: events and fills ----------------------------------------

    def _apply_event(self, event: BrokerEvent) -> None:
        """Fold one venue event into the stored order and, if any, the position."""
        if event.order is not None:
            self._store.upsert_order(self._normalise_order(event.order))
        else:
            self._patch_order_state(event)
        if event.fill is not None:
            self._apply_fill(event.fill)

    def _normalise_order(self, order: Order) -> Order:
        """Return the venue's order with the mode of this profile.

        The mode is part of every persisted order (D8d); a venue event that
        disagrees with the profile it was routed from would silently record a
        paper fill as live (or the opposite), so it is corrected here -- and only
        here -- before the row is stored.
        """
        if order.mode is self.mode:
            return order
        LOGGER.warning(
            "realtime.gateway.order_mode_corrected",
            extra={
                "event": "order_mode_corrected",
                "profile_id": self.profile_id,
                "client_order_id": order.client_order_id,
                "venue_mode": RunMode(order.mode).value,
                "profile_mode": self.mode.value,
            },
        )
        return replace(order, mode=self.mode)

    def _patch_order_state(self, event: BrokerEvent) -> None:
        """Apply the state implied by ``event.event_type`` to the stored row."""
        state = _EVENT_STATES.get(event.event_type)
        if state is None:
            LOGGER.warning(
                "realtime.gateway.event_without_order",
                extra={
                    "event": "broker_event_ignored",
                    "profile_id": self.profile_id,
                    "client_order_id": event.client_order_id,
                    "venue_event": event.event_type.value,
                    "venue_message": event.message,
                },
            )
            return
        stored = self._store.get_order(event.client_order_id)
        if stored is None:
            LOGGER.warning(
                "realtime.gateway.unknown_order_event",
                extra={
                    "event": "unknown_order_event",
                    "profile_id": self.profile_id,
                    "client_order_id": event.client_order_id,
                    "venue_event": event.event_type.value,
                },
            )
            return
        if stored.state is state:
            return
        patch: dict[str, Any] = {"state": state, "updated_at": self._now()}
        if state is OrderState.REJECTED and event.message:
            patch["reject_reason"] = str(event.message)
        self._store.upsert_order(replace(stored, **patch))

    def _apply_fill(self, fill: Fill) -> None:
        """Record one *new* fill: counters, drain buffer and position accounting."""
        if not self._store.append_fill(fill):
            LOGGER.info(
                "realtime.gateway.fill_replayed",
                extra={
                    "event": "fill_replayed",
                    "profile_id": self.profile_id,
                    "client_order_id": fill.client_order_id,
                    "fill_id": fill.fill_id,
                },
            )
            return
        self._counters = replace(self._counters, orders_filled=self._counters.orders_filled + 1)
        self._fills.append(fill)
        self._book_fill(fill)

    def _book_fill(self, fill: Fill) -> None:
        """Apply one new fill to the position of its symbol."""
        signed = _signed_quantity(fill.side, fill.quantity)
        position = self._store.get_position(self.profile_id, fill.symbol)
        if position is None or abs(float(position.quantity)) <= FLAT_EPSILON:
            self._open_position(fill, signed)
            return
        same_direction = (float(position.quantity) > 0.0) == (signed > 0.0)
        if same_direction:
            self._extend_position(position, fill, signed)
            return
        self._reduce_position(position, fill, signed)

    def _open_position(self, fill: Fill, signed_quantity: float) -> None:
        """Open a fresh position (and its round-trip accumulator) from one fill."""
        if abs(signed_quantity) <= FLAT_EPSILON:
            return
        direction = Direction.LONG if signed_quantity > 0.0 else Direction.SHORT
        round_trip = _RoundTrip(
            symbol=fill.symbol,
            direction=direction,
            stop_price=self._order_stops.get(fill.client_order_id),
            reason=self._order_reasons.get(fill.client_order_id, ""),
        )
        round_trip.add_entry(
            signed_quantity, float(fill.price), float(fill.fee), pd.Timestamp(fill.timestamp)
        )
        self._round_trips[fill.symbol] = round_trip
        opened_at = pd.Timestamp(fill.timestamp)
        self._store.upsert_position(
            Position(
                profile_id=self.profile_id,
                symbol=fill.symbol,
                quantity=signed_quantity,
                average_price=float(fill.price),
                direction=direction,
                opened_at=opened_at,
                updated_at=opened_at,
                stop_price=round_trip.stop_price,
            )
        )

    def _extend_position(self, position: Position, fill: Fill, signed_quantity: float) -> None:
        """Add to an existing position, keeping the weighted-average entry price."""
        added = abs(signed_quantity)
        held = abs(float(position.quantity))
        average = (float(position.average_price) * held + float(fill.price) * added) / (
            held + added
        )
        round_trip = self._round_trip_for(position)
        round_trip.add_entry(
            added, float(fill.price), float(fill.fee), pd.Timestamp(fill.timestamp)
        )
        self._store.upsert_position(
            replace(
                position,
                quantity=float(position.quantity) + signed_quantity,
                average_price=average,
                updated_at=pd.Timestamp(fill.timestamp),
            )
        )

    def _reduce_position(self, position: Position, fill: Fill, signed_quantity: float) -> None:
        """Reduce or close a position; a quantity reaching zero closes the round trip.

        A fill that crosses zero (a reversal) closes the current round trip with the
        quantity it actually offset and opens the remainder as a new position in the
        opposite direction, so neither side of the reversal is lost.
        """
        filled = abs(signed_quantity)
        closing = min(filled, abs(float(position.quantity))) if filled else 0.0
        fraction = (closing / filled) if filled else 0.0
        price = float(fill.price)
        fee = float(fill.fee)
        timestamp = pd.Timestamp(fill.timestamp)
        round_trip = self._round_trip_for(position)
        round_trip.add_exit(
            closing,
            price,
            fee * fraction,
            timestamp,
            reason=self._order_reasons.get(fill.client_order_id, ""),
        )
        remaining = float(position.quantity) + signed_quantity
        if abs(remaining) <= FLAT_EPSILON:
            self._close_round_trip(fill.symbol, round_trip)
            return
        if (remaining > 0.0) != (float(position.quantity) > 0.0):
            self._close_round_trip(fill.symbol, round_trip)
            self._open_remainder(fill, remaining, fee * (1.0 - fraction))
            return
        realized = round_trip.realized_on(closing, price)
        self._store.upsert_position(
            replace(
                position,
                quantity=remaining,
                realized_pnl=float(position.realized_pnl) + realized,
                updated_at=timestamp,
            )
        )

    def _open_remainder(self, fill: Fill, remaining: float, fee: float) -> None:
        """Open the leftover of a zero-crossing fill as a new, opposite position."""
        direction = Direction.LONG if remaining > 0.0 else Direction.SHORT
        round_trip = _RoundTrip(
            symbol=fill.symbol,
            direction=direction,
            stop_price=self._order_stops.get(fill.client_order_id),
            reason=self._order_reasons.get(fill.client_order_id, ""),
        )
        round_trip.add_entry(abs(remaining), float(fill.price), fee, pd.Timestamp(fill.timestamp))
        self._round_trips[fill.symbol] = round_trip
        opened_at = pd.Timestamp(fill.timestamp)
        self._store.upsert_position(
            Position(
                profile_id=self.profile_id,
                symbol=fill.symbol,
                quantity=remaining,
                average_price=float(fill.price),
                direction=direction,
                opened_at=opened_at,
                updated_at=opened_at,
                stop_price=round_trip.stop_price,
            )
        )

    def _close_round_trip(self, symbol: str, round_trip: _RoundTrip) -> None:
        """Drop the position, persist the round trip and expose it once."""
        self._round_trips.pop(symbol, None)
        self._store.delete_position(self.profile_id, symbol)
        trade = round_trip.to_trade(self.profile_id)
        if trade is None:
            # Unreachable: to_trade() only refuses a fabricated accumulator.
            return  # pragma: no cover
        self._store.append_trade(trade, profile_id=self.profile_id)
        self._closed_trade = trade
        LOGGER.info(
            "realtime.gateway.trade_closed",
            extra={
                "event": "trade_closed",
                "profile_id": self.profile_id,
                "symbol": symbol,
                "direction": trade.direction.value,
                "pnl": float(trade.pnl),
                "exit_reason": trade.exit_reason.value,
            },
        )

    def _round_trip_for(self, position: Position) -> _RoundTrip:
        """Return the accumulator of ``position``, rebuilding it after a restart."""
        symbol = str(position.symbol)
        existing = self._round_trips.get(symbol)
        if existing is not None:
            return existing
        rebuilt = _RoundTrip(
            symbol=symbol,
            direction=Direction(position.direction),
            stop_price=position.stop_price,
        )
        rebuilt.add_entry(
            abs(float(position.quantity)),
            float(position.average_price),
            0.0,
            pd.Timestamp(position.opened_at),
        )
        self._round_trips[symbol] = rebuilt
        return rebuilt

    # -- internals: bookkeeping ---------------------------------------------

    def _remember_request(self, request: OrderRequest) -> None:
        """Remember the provenance (reason and stop) of one submitted order.

        The persisted :class:`Order` has no room for a stop price or a free-form
        reason, so the round trip reads them back from here.  The mapping is
        in-memory on purpose: a missing entry degrades to the persisted position
        and to :attr:`ExitReason.SIGNAL`, never to a fabricated value.
        """
        self._order_reasons[request.client_order_id] = str(request.reason)
        self._order_stops[request.client_order_id] = request.stop_price

    def _now(self) -> pd.Timestamp:
        """Return the current instant through the injected clock (never the wall clock)."""
        return pd.Timestamp(self._clock.now()).tz_convert(UTC)


def _order_state(ack: BrokerAck) -> OrderState:
    """Return the order state carried by a venue acknowledgement."""
    return OrderState(ack.state)
