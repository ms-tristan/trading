"""Startup safety sweep: flatten every durable position nobody tracks any more.

A **position** is durable SQLite state completely independent of the ``profiles``
table.  Removing a profile through the monitoring API is safe -- it flattens the
open exposure at the venue *before* anything is removed, and aborts if it cannot
-- but nothing else is: a profile row that disappears without going through
``Orchestrator.delete_profile`` (a manual row removal, a restored older database,
a state file edited by hand) leaves a position that **no component ever looks at
again**.  No stop loss, no exit management, no reconciliation: a real exposure
sitting at a real venue, forgotten for ever.

This module is the safety net.  :func:`sweep_orphaned_positions` runs once at
engine boot -- at the exact point where the full set of durable positions and the
full set of loaded profiles are both visible, and before any runner exists -- and
**closes every orphaned position at the venue**.

What "orphaned" means
---------------------
A position is orphaned when its ``profile_id`` matches **no** loaded profile.
The sweep enumerates the candidate identifiers from the ``positions`` table itself
-- through the single dedicated read
:meth:`~trading_platform.realtime.store.StateStore.position_profile_ids`, because
a profile id that appears **only** there is precisely the hazard being fixed --
and, when that read is unavailable, falls back to two durable sources:

* the identifiers of the profiles that were loaded for this boot, and
* the identifiers the previous sweep already recorded (the persisted report, see
  :data:`ORPHAN_REPORT_META_KEY`) plus the identifiers the store's own
  ``profiles`` table history still remembers (``StateStore.load_profiles``).

Closing an orphan
-----------------
The closing order goes through the **existing** :class:`ExecutionGateway`, never
through a raw broker call: it obeys the same lifecycle, the same deterministic
idempotency key and the same persistence as every other order of the platform.
Because an orphan has no :class:`ProfileConfig` any more, a **synthetic unmanaged
profile** is built for it (see :func:`_unmanaged_profile`):

* its ``mode`` comes from the persisted ``profile_state:<id>`` ``meta`` payload
  -- an unknown profile falls back to the documented ``STOPPED``/``PAPER``
  default -- so a ``paper`` orphan closes on the simulated venue and a ``live``
  orphan closes through ``CcxtBroker`` at the real venue.  **Both modes are
  swept**: there is never a lingering exposure.
* its ``exchange`` comes from :func:`recorded_exchange`, the durable
  ``profile_exchange:<id>`` ``meta`` value the orchestrator writes for every
  loaded profile before any sweep can run.

No risk manager and no live gate are injected: there is no profile left to limit,
and the gateway already asserts that the broker and the profile agree on the run
mode.  An orphan is closed, not traded.

Never lose an unclosable position
---------------------------------
A closure that fails -- the venue refuses, the wallet is broken, the gateway
raises -- is **caught per position**, recorded in the report, logged at ``ERROR``
as ``orphan_position_close_failed``, and **nothing is deleted**: the ``positions``
row stays exactly as it was, so an exposure nobody could close stays visible
instead of being silently forgotten.  The sweep itself never raises: the platform
must boot, and the warning is carried by the report.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from trading_platform.config.models import ProfileConfig
from trading_platform.core.errors import (
    BrokerError,
    GatewayError,
    KillSwitchActiveError,
    OrderRejectedError,
    ProfileError,
    RealtimeError,
    RiskLimitExceededError,
    WalletError,
)
from trading_platform.core.models import Direction
from trading_platform.realtime.clock import Clock
from trading_platform.realtime.models import (
    OrderRequest,
    OrderSide,
    OrderType,
    Position,
    RunMode,
    new_client_order_id,
)
from trading_platform.realtime.observability import LOGGER_NAME, log_event
from trading_platform.realtime.store import StateStore

__all__ = [
    "ORPHAN_CLOSE_REASON",
    "ORPHAN_EXCHANGE_PREFIX",
    "ORPHAN_ORDER_SEQUENCE",
    "ORPHAN_REPORT_META_KEY",
    "ORPHAN_SWEPT_AT_META_KEY",
    "OrphanClosure",
    "OrphanFailure",
    "OrphanSweepReport",
    "recorded_exchange",
    "sweep_orphaned_positions",
]

_LOGGER = logging.getLogger(LOGGER_NAME)

#: ``meta`` key holding the JSON payload of the last orphan sweep.
#:
#: The report is persisted **on every sweep**, a clean one included, so a reader
#: can always tell "swept, nothing found" (``orphaned == 0``, a ``swept_at``
#: timestamp) from "never swept" (no key at all, which the API renders ``null``).
ORPHAN_REPORT_META_KEY: str = "orphan_sweep_report"

#: ``meta`` key mirroring :attr:`OrphanSweepReport.swept_at` on its own.
ORPHAN_SWEPT_AT_META_KEY: str = "orphan_sweep_swept_at"

#: Prefix of the ``meta`` key holding the exchange recorded for one profile.
#:
#: The orchestrator writes it for every loaded profile **before** the sweep runs
#: (see ``RealtimeOrchestrator._prepare_runners``), which is what lets a ``live``
#: orphan be closed through the venue it was really trading on.
ORPHAN_EXCHANGE_PREFIX: str = "profile_exchange:"

#: Reason carried by the market order that closes an orphaned position.
ORPHAN_CLOSE_REASON: str = "orphaned position: profile no longer loaded"

#: Sequence number of the closing order inside its deterministic identifier.
ORPHAN_ORDER_SEQUENCE: int = 0

#: Mode a profile of unknown state is closed with.
#:
#: ``PAPER`` is the documented default of
#: :meth:`~trading_platform.realtime.store.StateStore.profile_state` for a profile
#: whose state was never persisted, and the safe one: a ``live`` closure is only
#: ever attempted for an orphan whose persisted state *says* it was live.
_DEFAULT_EXCHANGE = "binance"


def _default_broker_factory() -> Callable[..., Any]:
    """Return the engine's venue factory, imported lazily.

    :mod:`trading_platform.realtime.orchestrator` imports this module to wire the
    sweep into the boot, so importing it back at module scope would be a circular
    import.  The lookup happens on the first sweep only.
    """
    from trading_platform.realtime.orchestrator import default_broker_factory

    return default_broker_factory


@dataclass(frozen=True)
class OrphanClosure:
    """One orphaned position that was successfully closed at the venue."""

    profile_id: str
    symbol: str
    quantity: float
    side: str
    price: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field.

        ``side`` is the side of the **closing** order (``"buy"`` or ``"sell"``)
        and ``price`` is the reference price that order used.
        """
        return {
            "profile_id": str(self.profile_id),
            "symbol": str(self.symbol),
            "quantity": float(self.quantity),
            "side": str(self.side),
            "price": float(self.price),
        }


@dataclass(frozen=True)
class OrphanFailure:
    """One orphaned position the platform could **not** close."""

    profile_id: str
    symbol: str
    quantity: float
    error: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "profile_id": str(self.profile_id),
            "symbol": str(self.symbol),
            "quantity": float(self.quantity),
            "error": str(self.error),
        }


@dataclass(frozen=True)
class OrphanSweepReport:
    """Outcome of one startup orphan sweep.

    ``found`` counts every durable position that belonged to no loaded profile --
    a position that was already flat is *found* but neither closed nor failed --
    while :attr:`orphaned` counts the ones the sweep had to act upon.
    """

    found: int
    closed: tuple[OrphanClosure, ...]
    failed: tuple[OrphanFailure, ...]
    swept_at: str

    @property
    def orphaned(self) -> int:
        """Return how many orphaned positions the sweep acted upon."""
        return len(self.closed) + len(self.failed)

    @property
    def ok(self) -> bool:
        """Return whether every orphaned position was closed."""
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        """Return the machine-readable payload persisted in the store."""
        return {
            "found": int(self.found),
            "orphaned": int(self.orphaned),
            "closed_count": len(self.closed),
            "failed_count": len(self.failed),
            "closed": [item.to_dict() for item in self.closed],
            "failed": [item.to_dict() for item in self.failed],
            "swept_at": str(self.swept_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> OrphanSweepReport:
        """Rebuild a report from :meth:`to_dict` output.

        The counts are **derived** from the entries rather than trusted: a stored
        payload whose counters disagree with its lists can therefore never make
        the API claim a closure that did not happen.
        """
        closed = tuple(
            OrphanClosure(
                profile_id=str(item["profile_id"]),
                symbol=str(item["symbol"]),
                quantity=float(item["quantity"]),
                side=str(item["side"]),
                price=float(item["price"]),
            )
            for item in payload.get("closed") or ()
        )
        failed = tuple(
            OrphanFailure(
                profile_id=str(item["profile_id"]),
                symbol=str(item["symbol"]),
                quantity=float(item["quantity"]),
                error=str(item["error"]),
            )
            for item in payload.get("failed") or ()
        )
        found = payload.get("found")
        return cls(
            found=len(closed) + len(failed) if found is None else int(found),
            closed=closed,
            failed=failed,
            swept_at=str(payload.get("swept_at") or ""),
        )

    @classmethod
    def empty(cls, *, swept_at: str) -> OrphanSweepReport:
        """Return a clean report: the sweep ran and found no orphan."""
        return cls(found=0, closed=(), failed=(), swept_at=str(swept_at))


# ---------------------------------------------------------------------------
# durable reads an orphan depends on
# ---------------------------------------------------------------------------


def recorded_exchange(store: StateStore, profile_id: str) -> str:
    """Return the exchange durably recorded for ``profile_id``.

    The value is the ``profile_exchange:<id>`` ``meta`` entry the orchestrator
    writes for every loaded profile; an absent key answers ``"binance"``, the
    :class:`~trading_platform.config.models.ProfileConfig` default, so an orphan of
    a profile that never recorded one is still closed on the documented venue.
    """
    recorded = store.get_meta(ORPHAN_EXCHANGE_PREFIX + str(profile_id))
    if recorded is None:
        return _DEFAULT_EXCHANGE
    text = str(recorded).strip()
    return text or _DEFAULT_EXCHANGE


def _known_profile_ids(store: StateStore, known: set[str]) -> list[str]:
    """Return every profile identifier the sweep has to inspect, sorted.

    The **primary** source is the ``positions`` table itself
    (:meth:`~trading_platform.realtime.store.StateStore.position_profile_ids`):
    the whole point of the sweep is a profile id nobody tracks any more, so a
    candidate list rebuilt from the loaded profiles alone would miss exactly the
    hazard it exists for.

    Two fallbacks keep a store that cannot answer that read usable, and they are
    what the sweep degrades to: the identifiers of the profiles loaded for this
    boot, the ones the store's ``profiles`` table still remembers, and the ones
    the previous sweep recorded.
    """
    candidates: set[str] = {str(item) for item in known}
    scan = getattr(store, "position_profile_ids", None)
    if callable(scan):
        try:
            candidates.update(str(item) for item in scan())
            return sorted(candidates)
        except RealtimeError as exc:
            log_event(
                _LOGGER,
                "orphan_sweep_scan_unavailable",
                level=logging.WARNING,
                error=str(exc),
            )
    try:
        candidates.update(str(profile.id) for profile in store.load_profiles())
    except RealtimeError:
        log_event(_LOGGER, "orphan_sweep_profiles_unavailable", level=logging.WARNING)
    previous = _previous_report(store)
    if previous is not None:
        for closure in previous.closed:
            candidates.add(closure.profile_id)
        for failure in previous.failed:
            candidates.add(failure.profile_id)
    return sorted(candidates)


def _previous_report(store: StateStore) -> OrphanSweepReport | None:
    """Return the report the previous sweep persisted, or ``None``.

    A payload that cannot be decoded is treated as "never swept": a corrupt
    warning must never stop a boot, and the sweep that follows rewrites it.
    """
    raw = store.get_meta(ORPHAN_REPORT_META_KEY)
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        log_event(_LOGGER, "orphan_sweep_report_undecodable", level=logging.WARNING)
        return None
    if not isinstance(decoded, Mapping):
        return None
    try:
        return OrphanSweepReport.from_dict(decoded)
    except (KeyError, TypeError, ValueError):
        log_event(_LOGGER, "orphan_sweep_report_undecodable", level=logging.WARNING)
        return None


def _unmanaged_profile(store: StateStore, position: Position) -> ProfileConfig:
    """Build the synthetic profile that lets an orphan reach a venue.

    The profile is never persisted and never run: it exists only so the very same
    :func:`default_broker_factory` the engine uses -- and the very same
    :class:`ExecutionGateway` -- can be handed a ``ProfileConfig``.  Its ``mode`` is
    the one durably recorded for the profile that used to own the position, and its
    ``exchange`` the one the orchestrator recorded for it.
    """
    profile_id = str(position.profile_id)
    state = store.profile_state(profile_id)
    return ProfileConfig(
        id=profile_id,
        symbol=str(position.symbol),
        mode=RunMode(state.mode).value,
        exchange=recorded_exchange(store, profile_id),
    )


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------


def sweep_orphaned_positions(
    *,
    store: StateStore,
    profiles: Sequence[ProfileConfig],
    clock: Clock,
    broker_factory: Callable[..., Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> OrphanSweepReport:
    """Close every durable position whose profile is not loaded, at the venue.

    The sweep runs at engine boot, before any runner exists, so nothing can be
    trading while an orphan is being closed.  For each orphaned
    ``(profile_id, symbol)`` pair, in ascending order:

    1. a position already flat (``abs(quantity) <= FLAT_EPSILON``) is counted in
       ``found`` and skipped -- there is nothing to close;
    2. the closing market order is built on the **opposite** side of the position
       and routed through :class:`ExecutionGateway`, which persists it and sends it
       to the venue;
    3. the venue's answer is folded back with ``gateway.poll()``, the completed
       round trip (when there is one) is appended to the store, and the position is
       re-read: a position that is still open after the closure is a **failure**,
       not a success;
    4. whatever fails is caught per position, recorded as an :class:`OrphanFailure`
       and logged at ``ERROR`` -- **nothing is deleted**, so an unclosable position
       stays visible.

    Idempotency: the identifier of the closing order is built from the position's
    own ``updated_at`` and :data:`ORPHAN_ORDER_SEQUENCE`, never from the boot
    instant, and the store is checked **first**.  A restart therefore re-derives
    the very same identifier: the order is already known (no venue call at all) or
    the position is already gone, so a second boot never double-closes.

    The report is persisted under :data:`ORPHAN_REPORT_META_KEY` before this
    function returns, a clean sweep included.

    Parameters
    ----------
    store:
        Durable state; it holds the positions, the profile states and the report.
    profiles:
        Every profile loaded for this boot -- the ones that are *not* orphaned.
    clock:
        Time seam; the sweep never reads the wall clock directly.
    broker_factory:
        Seam building the venue adapter of the synthetic unmanaged profile.  It is
        :func:`~trading_platform.realtime.orchestrator.default_broker_factory` when
        omitted, so a ``paper`` orphan closes on the simulated venue and a ``live``
        orphan closes through ``CcxtBroker`` at the real one.
    environ:
        Environment mapping the factory (and the live credentials it resolves)
        reads.

    Returns
    -------
    OrphanSweepReport
        What was found, what was closed and what could not be closed.
    """
    from trading_platform.realtime.gateway import FLAT_EPSILON, ExecutionGateway

    resolved_factory: Callable[..., Any] = (
        _default_broker_factory() if broker_factory is None else broker_factory
    )
    started = pd.Timestamp(clock.now())
    examined = 0
    closed: list[OrphanClosure] = []
    failed: list[OrphanFailure] = []
    known = {str(profile.id) for profile in profiles}
    for profile_id in _known_profile_ids(store, known):
        if profile_id in known:
            continue
        for position in _positions_of(store, profile_id):
            examined += 1
            quantity = float(position.quantity)
            symbol = str(position.symbol)
            if abs(quantity) <= FLAT_EPSILON:
                continue
            try:
                closure = _close_orphan(
                    store=store,
                    position=position,
                    clock=clock,
                    broker_factory=resolved_factory,
                    environ=environ,
                    gateway_type=ExecutionGateway,
                    flat_epsilon=FLAT_EPSILON,
                )
            except (
                BrokerError,
                RiskLimitExceededError,
                KillSwitchActiveError,
                OrderRejectedError,
                GatewayError,
                WalletError,
                ProfileError,
                RealtimeError,
            ) as exc:
                failed.append(
                    OrphanFailure(
                        profile_id=profile_id,
                        symbol=symbol,
                        quantity=abs(quantity),
                        error=str(exc),
                    )
                )
                log_event(
                    _LOGGER,
                    "orphan_position_close_failed",
                    level=logging.ERROR,
                    profile_id=profile_id,
                    symbol=symbol,
                    quantity=abs(quantity),
                    error=str(exc),
                )
                continue
            if closure is None:
                continue
            closed.append(closure)
            log_event(
                _LOGGER,
                "orphan_position_closed",
                profile_id=profile_id,
                symbol=symbol,
                quantity=closure.quantity,
                side=closure.side,
                price=closure.price,
            )
    report = OrphanSweepReport(
        found=examined,
        closed=tuple(closed),
        failed=tuple(failed),
        swept_at=started.isoformat(),
    )
    _persist_report(store, report)
    log_event(
        _LOGGER,
        "orphan_sweep_completed",
        found=int(report.found),
        orphaned=int(report.orphaned),
        closed=len(report.closed),
        failed=len(report.failed),
    )
    return report


def _positions_of(store: StateStore, profile_id: str) -> list[Position]:
    """Return the durable positions of ``profile_id``, or ``[]`` when unreadable."""
    try:
        return list(store.list_positions(profile_id))
    except RealtimeError as exc:
        log_event(
            _LOGGER,
            "orphan_sweep_positions_unavailable",
            level=logging.WARNING,
            profile_id=profile_id,
            error=str(exc),
        )
        return []


def _close_orphan(
    *,
    store: StateStore,
    position: Position,
    clock: Clock,
    broker_factory: Callable[..., Any],
    environ: Mapping[str, str] | None,
    gateway_type: Any,
    flat_epsilon: float,
) -> OrphanClosure | None:
    """Route the single market order that closes one orphaned position.

    Returns ``None`` when there was nothing left to do -- the position vanished
    between the scan and the closure, or an identical order is already known to the
    store, which is exactly the restart case.
    """
    profile_id = str(position.profile_id)
    symbol = str(position.symbol)
    quantity = float(position.quantity)
    if abs(quantity) <= flat_epsilon:
        return None

    untracked = _unmanaged_profile(store, position)
    stamp = pd.Timestamp(position.updated_at)
    client_order_id = new_client_order_id(profile_id, symbol, stamp, ORPHAN_ORDER_SEQUENCE)
    side = OrderSide.SELL if Direction(position.direction) is Direction.LONG else OrderSide.BUY
    reference_price = float(position.average_price)

    from trading_platform.realtime.models import OrderState

    existing = store.get_order(client_order_id)
    if existing is not None and existing.state is not OrderState.REJECTED:
        # The closure of this very position already ran: a restart re-derives the
        # same identifier, so the durable row proves nothing is left to send.
        log_event(
            _LOGGER,
            "orphan_position_already_closed",
            profile_id=profile_id,
            symbol=symbol,
            client_order_id=client_order_id,
        )
        return None

    broker = broker_factory(untracked, clock=clock, environ=environ, wallet=None)
    gateway = gateway_type(profile=untracked, broker=broker, store=store, clock=clock)
    request = OrderRequest(
        profile_id=profile_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        type=OrderType.MARKET,
        quantity=abs(quantity),
        price=None,
        stop_price=None,
        mode=RunMode(untracked.mode),
        reason=ORPHAN_CLOSE_REASON,
        created_at=stamp,
    )
    gateway.submit(
        request,
        reference_price=reference_price,
        equity=0.0,
        open_positions=1,
        position_notional=abs(quantity) * reference_price,
        daily_pnl=0.0,
        daily_trades=0,
        peak_equity=0.0,
        closes_position=True,
    )
    gateway.poll()
    trade = gateway.closed_trade()
    if trade is not None:
        store.append_trade(trade, profile_id=profile_id)
    remaining = store.get_position(profile_id, symbol)
    if remaining is not None and abs(float(remaining.quantity)) > flat_epsilon:
        raise OrderRejectedError(
            f"orphaned position {profile_id}/{symbol} is still open after the closing order"
        )
    return OrphanClosure(
        profile_id=profile_id,
        symbol=symbol,
        quantity=abs(quantity),
        side=side.value,
        price=reference_price,
    )


def _persist_report(store: StateStore, report: OrphanSweepReport) -> None:
    """Write ``report`` into the store, best-effort.

    A store that cannot persist the report must not turn a successful closure into
    a boot failure; the report is still returned to the caller, which publishes it
    on the snapshot of this process.
    """
    payload = json.dumps(report.to_dict(), sort_keys=True)
    try:
        store.set_meta(ORPHAN_REPORT_META_KEY, payload)
        store.set_meta(ORPHAN_SWEPT_AT_META_KEY, str(report.swept_at))
    except RealtimeError as exc:
        log_event(
            _LOGGER,
            "orphan_sweep_report_not_persisted",
            level=logging.ERROR,
            error=str(exc),
        )


def load_orphan_report(store: StateStore) -> OrphanSweepReport | None:
    """Return the report the last sweep persisted, or ``None`` when never swept.

    This is the read path of a process that did **not** boot an engine: a
    ``realtime serve`` process renders the warning of the last boot from here.
    """
    try:
        return _previous_report(store)
    except RealtimeError as exc:
        log_event(
            _LOGGER,
            "orphan_sweep_report_unavailable",
            level=logging.WARNING,
            error=str(exc),
        )
        return None
