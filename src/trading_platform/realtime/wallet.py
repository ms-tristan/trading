"""The shared platform wallet: the **single** USDT cash ledger of the platform.

Product decision (binding): every profile funds its orders from *one* cash ledger
instead of owning an isolated simulated balance.  The wallet is therefore the only
thing that can fund an order, and it is shared by every profile of the process.

Three properties make that safe, and each of them is enforced here rather than by
the caller:

single source of truth
    :meth:`PlatformWallet.restore` reads the persisted row once at startup and
    :meth:`PlatformWallet.persist` writes it back after every accepted mutation, so
    a restart never resets the cash.  When the store holds no row yet, the wallet
    keeps the balance it was configured with (the platform initial balance, or the
    sum of the profile allocations -- the composition rule lives in the
    configuration layer, not here).

thread safety
    Profiles run in **separate threads** and their fills update the same cash
    counter concurrently, so every read and every mutation of the cash happens
    under one :class:`threading.RLock` **and** the durable write happens inside the
    same critical section: a read-modify-write (``cash = cash - amount``) is atomic,
    no update is ever lost, and the store can never observe a torn value.  An
    ``RLock`` is used on purpose: :meth:`PlatformWallet.debit` must be able to
    re-enter :meth:`PlatformWallet.persist` without deadlocking.

live mode honesty
    In **live** mode the wallet *is* the venue account: it is never locally
    debited, it mirrors whatever ``sync_from_venue`` last reported, and
    :meth:`PlatformWallet.debit`, :meth:`PlatformWallet.credit` and
    :meth:`PlatformWallet.apply_fill` refuse to run -- the venue account is the
    source of truth, and a locally invented balance would be a lie.  The mirror
    falls back to the configured initial balance while the venue has reported
    nothing (never to an invented ``0.0``).

The wallet is never resolved through a module-level global: it is **injected** by
the composition root (and privately built by
:class:`~trading_platform.realtime.broker.PaperBroker` when none is injected),
exactly like the clock and the store.  It holds no credential and no secret of any
kind.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd

from trading_platform.core.errors import StateStoreError, WalletError
from trading_platform.realtime.clock import Clock
from trading_platform.realtime.models import OrderSide, RunMode
from trading_platform.realtime.observability import LOGGER_NAME, log_event

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from trading_platform.realtime.store import StateStore, WalletRow

__all__ = ["PlatformWallet", "WalletSnapshot"]

_LOGGER = logging.getLogger(LOGGER_NAME)

#: Value of :attr:`WalletSnapshot.source` when the cash is the local ledger.
SOURCE_LOCAL = "local"

#: Value of :attr:`WalletSnapshot.source` when the cash mirrors a venue account.
SOURCE_VENUE = "venue"


# ---------------------------------------------------------------------------
# small guards shared by every amount of the ledger
# ---------------------------------------------------------------------------


def _finite_or_none(value: Any) -> float | None:
    """Return ``value`` as a finite ``float``, or ``None``.

    ``None``, ``NaN`` and the two infinities all collapse to ``None`` so that no
    ``to_dict()`` payload can ever carry a value the JSON encoder refuses.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_or_none(value: Any) -> str | None:
    """Render a timestamp as an ISO-8601 string, or ``None``."""
    return None if value is None else pd.Timestamp(value).isoformat()


def _require_amount(amount: Any) -> float:
    """Return ``amount`` as a finite, non-negative ``float``.

    Raises
    ------
    WalletError
        When ``amount`` is not a number, is ``NaN``/``Infinity``, or is negative.
        A negative amount is rejected instead of being silently treated as a
        movement of the opposite sign: the caller must say which direction it
        wants, so the ledger stays readable.
    """
    try:
        value = float(amount)
    except (TypeError, ValueError) as exc:
        raise WalletError(f"amount must be a finite, non-negative number, got {amount!r}") from exc
    if not math.isfinite(value) or value < 0.0:
        raise WalletError(f"amount must be a finite, non-negative number, got {amount!r}")
    return value


# ---------------------------------------------------------------------------
# the read model of the wallet
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalletSnapshot:
    """Immutable view of the shared wallet, as the reporting layer consumes it.

    ``cash`` is what is left to deploy, ``equity`` is ``cash`` plus the
    mark-to-market value of everything the profiles hold, ``deployed`` is the
    capital currently committed, ``realized_pnl``/``unrealized_pnl`` are the
    platform-wide P&L figures and ``total_exposure`` is the notional the profiles
    hold right now.  ``source`` says where the cash comes from: ``"local"`` for the
    simulated ledger, ``"venue"`` when the wallet mirrors a real account.
    """

    name: str
    mode: RunMode
    initial_balance: float
    cash: float
    equity: float
    deployed: float
    realized_pnl: float
    unrealized_pnl: float
    total_exposure: float
    profiles: int
    source: str
    updated_at: pd.Timestamp | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with exactly the documented keys.

        Every float is routed through a finite-or-``None`` guard: a ``NaN`` or an
        ``Infinity`` can never reach the JSON encoder, it becomes ``None`` (the
        dashboard renders it as an em dash).
        """
        return {
            "name": str(self.name),
            "mode": RunMode(self.mode).value,
            "initial_balance": _finite_or_none(self.initial_balance),
            "cash": _finite_or_none(self.cash),
            "equity": _finite_or_none(self.equity),
            "deployed": _finite_or_none(self.deployed),
            "realized_pnl": _finite_or_none(self.realized_pnl),
            "unrealized_pnl": _finite_or_none(self.unrealized_pnl),
            "total_exposure": _finite_or_none(self.total_exposure),
            "profiles": int(self.profiles),
            "source": str(self.source),
            "updated_at": _timestamp_or_none(self.updated_at),
        }


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


class PlatformWallet:
    """The one USDT cash ledger every order of the platform is funded from.

    Parameters
    ----------
    initial_balance:
        Cash the wallet starts with when the store remembers nothing.  It must be
        strictly positive: a wallet is never funded with a zero or negative
        balance, and the reporting layer uses this value as the P&L denominator.
    mode:
        :attr:`RunMode.PAPER` for the local simulated ledger, :attr:`RunMode.LIVE`
        when the wallet mirrors a real venue account (read-only, see the module
        docstring).
    store:
        Optional durable seam (``load_wallet``/``save_wallet``).  Without one the
        wallet is an in-memory ledger: it still works, it simply does not survive
        the process.
    clock:
        Optional time seam, used for the ``updated_at`` of a snapshot.  Time is
        never read from the wall clock here.
    name:
        Display name of the wallet (``"platform"`` by default); it carries no
        secret.
    """

    def __init__(
        self,
        *,
        initial_balance: float,
        mode: RunMode = RunMode.PAPER,
        store: StateStore | None = None,
        clock: Clock | None = None,
        name: str = "platform",
    ) -> None:
        balance = float(initial_balance)
        if not math.isfinite(balance) or balance <= 0:
            raise WalletError(f"initial_balance must be positive, got {initial_balance}")
        #: One lock guards *every* read and *every* mutation of ``_cash`` and of
        #: ``_initial_balance``; it is re-entrant because a mutation persists
        #: itself while it still holds it.
        self._lock = threading.RLock()
        self._name = str(name) or "platform"
        self._mode = RunMode(mode)
        self._initial_balance = balance
        self._cash = balance
        self._store = store
        self._clock = clock
        self._venue_balance: float | None = None
        self._venue_synced_at: pd.Timestamp | None = None

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the display name of the wallet."""
        return self._name

    @property
    def mode(self) -> RunMode:
        """Return whether this wallet is the local ledger or a venue mirror."""
        return self._mode

    @property
    def is_authoritative(self) -> bool:
        """Whether the wallet owns the cash (paper) instead of mirroring a venue."""
        return self._mode is RunMode.PAPER

    @property
    def initial_balance(self) -> float:
        """Return the balance the wallet started with (the P&L denominator)."""
        with self._lock:
            return self._initial_balance

    @property
    def cash(self) -> float:
        """Return the cash currently available in the ledger."""
        with self._lock:
            return self._cash

    # -- funding -----------------------------------------------------------

    def spendable(self) -> float:
        """Return the cash an order may be funded from right now.

        In paper mode this is the ledger itself.  In live mode it is the balance
        the venue last reported through :meth:`sync_from_venue`, falling back to
        the configured initial balance while the venue has reported nothing -- a
        missing venue answer is *unknown*, never an invented ``0.0``.
        """
        with self._lock:
            if self._mode is RunMode.PAPER:
                return self._cash
            if self._venue_balance is None:
                return self._initial_balance
            return self._venue_balance

    def can_fund(self, amount: float) -> bool:
        """Whether the wallet currently holds enough cash to fund ``amount``.

        ``False`` for anything that is not a finite, non-negative number (and never
        an exception: the caller is asking a question, not performing an action).
        An amount exactly equal to :meth:`spendable` is fundable.
        """
        try:
            value = _require_amount(amount)
        except (TypeError, ValueError, WalletError):
            return False
        return value <= self.spendable()

    # -- mutations ---------------------------------------------------------

    def debit(self, amount: float, *, reason: str = "") -> float:
        """Remove ``amount`` from the ledger and return the new cash.

        The whole read-check-write-persist sequence runs under the wallet lock, so
        two threads debiting at the same time can never lose an update and can
        never both spend the same unit.

        Raises
        ------
        WalletError
            When ``amount`` is not a finite, non-negative number, when the wallet
            is a read-only venue mirror (live mode), or when the ledger does not
            hold enough cash.  Nothing is written in any of those cases.
        """
        value = _require_amount(amount)
        self._require_authoritative()
        with self._lock:
            cash = self._cash
            if value > cash:
                raise WalletError(
                    f"platform wallet cannot debit {value:.2f} USDT: available {cash:.2f} USDT"
                )
            self._cash = cash - value
            remaining = self._cash
            self.persist()
        log_event(
            _LOGGER,
            "platform_wallet_debited",
            level=logging.DEBUG,
            amount=value,
            cash=remaining,
            reason=str(reason),
        )
        return remaining

    def credit(self, amount: float, *, reason: str = "") -> float:
        """Add ``amount`` to the ledger and return the new cash.

        Raises
        ------
        WalletError
            When ``amount`` is not a finite, non-negative number, or when the
            wallet is a read-only venue mirror (live mode).
        """
        value = _require_amount(amount)
        self._require_authoritative()
        with self._lock:
            self._cash = self._cash + value
            remaining = self._cash
            self.persist()
        log_event(
            _LOGGER,
            "platform_wallet_credited",
            level=logging.DEBUG,
            amount=value,
            cash=remaining,
            reason=str(reason),
        )
        return remaining

    def apply_fill(self, *, side: OrderSide, notional: float, fee: float) -> float:
        """Move the cash of one fill and return the new cash.

        This is the **single** entry point the paper broker uses: a ``BUY`` pays the
        notional plus the fee, a ``SELL`` receives the notional minus the fee.  The
        same validation, the same live-mode refusal and the same persistence apply
        as for :meth:`debit` / :meth:`credit`, because the fill goes through them.
        """
        resolved = OrderSide(side)
        notional_value = _require_amount(notional)
        fee_value = _require_amount(fee)
        if resolved is OrderSide.BUY:
            return self.debit(notional_value + fee_value, reason="fill")
        return self.credit(notional_value - fee_value, reason="fill")

    def sync_from_venue(self, balance: float, *, at: pd.Timestamp | None = None) -> None:
        """Record the balance the venue account reports (live mode only).

        The mirror is what :meth:`spendable` and :meth:`can_fund` use in live mode.
        It never touches the local ledger: in live mode the venue account *is* the
        wallet, so there is nothing local to debit.

        Raises
        ------
        WalletError
            When the wallet is a paper ledger (a simulated wallet is not a venue
            mirror), or when ``balance`` is not a finite, non-negative number.
        """
        if self._mode is not RunMode.LIVE:
            raise WalletError(f"platform wallet is not a venue mirror: mode is {self.mode.value!r}")
        value = _require_amount(balance)
        moment = None if at is None else pd.Timestamp(at)
        with self._lock:
            self._venue_balance = value
            self._venue_synced_at = moment
        log_event(
            _LOGGER,
            "platform_wallet_venue_sync",
            balance=value,
            at=None if moment is None else moment.isoformat(),
        )

    # -- the restore seam --------------------------------------------------

    def restore_cash(self, cash: float) -> None:
        """Re-seed the cash from the durable state (boot seeding).

        This mirrors :meth:`~trading_platform.realtime.broker.PaperBroker.
        restore_cash`: it is a *restore* seam, not a general balance setter -- it is
        never called by the order lifecycle and it moves no position and no fill.

        Raises
        ------
        WalletError
            When ``cash`` is not a finite number.
        """
        value = float(cash)
        if not math.isfinite(value):
            raise WalletError(f"restored cash must be finite, got {cash!r}")
        with self._lock:
            self._cash = value
            self.persist()

    def restore(self) -> float | None:
        """Adopt the persisted ledger, or initialise it when nothing is stored.

        The read and the adoption happen inside the same critical section, so a boot
        restore can never interleave with a fill of another profile.

        Returns
        -------
        float | None
            The restored cash, or ``None`` when the store held no wallet row (in
            which case the configured initial balance and cash are persisted as the
            first row, so the next restart has something to restore).

        A wallet built without a store simply keeps its configured value and
        returns ``None``: the durable write is a no-op, the behaviour is the same.
        """
        with self._lock:
            row = self._load_row()
            if row is None:
                self.persist()
                log_event(
                    _LOGGER,
                    "platform_wallet_initialized",
                    cash=self._cash,
                    initial_balance=self._initial_balance,
                )
                return None
            cash = float(row.cash)
            initial_balance = float(row.initial_balance)
            if not math.isfinite(cash):
                raise WalletError(f"restored cash must be finite, got {row.cash!r}")
            if not math.isfinite(initial_balance) or initial_balance <= 0:
                raise WalletError(f"initial_balance must be positive, got {row.initial_balance}")
            self._cash = cash
            self._initial_balance = initial_balance
            log_event(
                _LOGGER,
                "platform_wallet_restored",
                cash=cash,
                initial_balance=initial_balance,
                updated_at=_timestamp_or_none(row.updated_at),
            )
            return cash

    def persist(self) -> None:
        """Write the current ledger to the store, if there is one.

        The write happens under the wallet lock, so the persisted pair is always a
        state the ledger really went through.  A store failure
        (:class:`~trading_platform.core.errors.StateStoreError`) is logged as the
        event ``platform_wallet_persist_failed`` and **never** loses the in-memory
        value: the cash has already moved, and refusing it would be a lie about what
        the platform spent.
        """
        saver = getattr(self._store, "save_wallet", None)
        if saver is None:
            return
        with self._lock:
            cash = self._cash
            initial_balance = self._initial_balance
            try:
                saver(cash=cash, initial_balance=initial_balance)
            except StateStoreError as exc:
                log_event(
                    _LOGGER,
                    "platform_wallet_persist_failed",
                    level=logging.ERROR,
                    cash=cash,
                    initial_balance=initial_balance,
                    error=str(exc),
                )

    # -- read model --------------------------------------------------------

    def snapshot(
        self,
        *,
        positions_value: float = 0.0,
        deployed: float = 0.0,
        realized_pnl: float = 0.0,
        unrealized_pnl: float = 0.0,
        total_exposure: float = 0.0,
        profiles: int = 0,
        updated_at: pd.Timestamp | None = None,
    ) -> WalletSnapshot:
        """Return the immutable view of the wallet the reporting layer publishes.

        ``equity`` is ``cash + positions_value``: what the platform holds in cash
        plus the mark-to-market value of everything the profiles hold.  ``source``
        is ``"local"`` for the simulated ledger and ``"venue"`` for a live mirror.
        ``updated_at`` falls back to the injected clock (never to the wall clock);
        without a clock it stays ``None``.
        """
        with self._lock:
            cash = self._cash
            initial_balance = self._initial_balance
            name = self._name
            mode = self._mode
        positions = float(positions_value)
        return WalletSnapshot(
            name=name,
            mode=mode,
            initial_balance=initial_balance,
            cash=cash,
            equity=cash + positions,
            deployed=float(deployed),
            realized_pnl=float(realized_pnl),
            unrealized_pnl=float(unrealized_pnl),
            total_exposure=float(total_exposure),
            profiles=int(profiles),
            source=SOURCE_LOCAL if self.is_authoritative else SOURCE_VENUE,
            updated_at=self._resolve_updated_at(updated_at),
        )

    # -- internals ---------------------------------------------------------

    def _load_row(self) -> WalletRow | None:
        """Return the persisted wallet row, or ``None`` when there is none.

        A store without the wallet seam (an older or partial implementation) is
        treated exactly like a store without a row: the configured value is used.
        """
        loader = getattr(self._store, "load_wallet", None)
        if loader is None:
            return None
        return loader()  # type: ignore[no-any-return]

    def _require_authoritative(self) -> None:
        """Refuse a local mutation of a venue mirror (live mode).

        The message is part of the contract: it says *why* the wallet refuses, so
        an operator reading a report never believes the cash was moved locally.

        Raises
        ------
        WalletError
            Always, when the wallet is not the local ledger.
        """
        if not self.is_authoritative:
            raise WalletError(
                "platform wallet is read-only in live mode: the venue account is the "
                "source of truth"
            )

    def _resolve_updated_at(self, updated_at: pd.Timestamp | None) -> pd.Timestamp | None:
        """Return the explicit instant, else the clock's, else the venue's.

        A live mirror without a clock is stamped with the instant of its last venue
        reading: that is the moment the reported cash is really about.  Without any
        of the three the instant stays ``None`` rather than being invented.
        """
        if updated_at is not None:
            return pd.Timestamp(updated_at)
        if self._clock is not None:
            return pd.Timestamp(self._clock.now())
        with self._lock:
            return self._venue_synced_at

    def __repr__(self) -> str:
        """Return a secret-free representation of the wallet."""
        return (
            f"PlatformWallet(name={self._name!r}, mode={self._mode.value!r}, "
            f"cash={self._cash!r}, authoritative={self.is_authoritative})"
        )
