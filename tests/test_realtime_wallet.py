"""Contract tests of the shared platform wallet (work package wp1).

Covers, offline and deterministically:

* the nominal ledger movements: ``debit``/``credit``/``apply_fill`` return the new
  cash and move it exactly (buys pay the notional plus the fee, sells receive the
  notional minus the fee);
* the boundaries: a zero amount is a no-op, ``can_fund`` is inclusive, an empty
  store is initialised on the first ``restore()``;
* every refused operation, pinned on its exact message (non-finite or negative
  amount, unfunded debit, local mutation of the live mirror, ``sync_from_venue``
  on a paper ledger, non-finite restored cash, non-positive initial balance);
* **thread safety**: eight threads hammering the same ledger with interleaved
  debits and credits end at the exact starting cash, no update is lost, and every
  durable write was recorded while the wallet lock was held;
* persistence: the persisted cash is adopted on restore, so a restart never resets
  the wallet, and a failing store is logged without ever losing the in-memory
  value;
* live-mode honesty: the wallet mirrors the venue, cannot be mutated locally, and
  never invents a ``0.0`` when the venue has reported nothing;
* the read model: ``equity = cash + positions_value``, ``source``, and a ``to_dict``
  payload that carries exactly the documented keys and never a ``NaN``.

Nothing here touches the network or a real database: the durable seam is a local
fake implementing ``save_wallet``/``load_wallet`` only (docs/testing-policy.md
8.4 -- a realtime package defines its own fake instead of extending the shared
``tests/conftest.py``).
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytest

from trading_platform.core.errors import RealtimeError, StateStoreError, WalletError
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import OrderSide, RunMode
from trading_platform.realtime.wallet import PlatformWallet, WalletSnapshot

START = datetime(2024, 1, 1, tzinfo=UTC)
INITIAL = 1_000.0

#: Every key ``WalletSnapshot.to_dict()`` must emit, exactly.
SNAPSHOT_KEYS = {
    "name",
    "mode",
    "initial_balance",
    "cash",
    "equity",
    "deployed",
    "realized_pnl",
    "unrealized_pnl",
    "total_exposure",
    "profiles",
    "source",
    "updated_at",
}


# ---------------------------------------------------------------------------
# local fakes -- the durable seam of the wallet, in memory only
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeWalletRow:
    """Stand-in for the store's ``WalletRow`` (owned by the persistence package)."""

    cash: float
    initial_balance: float
    updated_at: pd.Timestamp | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "cash": float(self.cash),
            "initial_balance": float(self.initial_balance),
            "updated_at": None if self.updated_at is None else self.updated_at.isoformat(),
        }


def wallet_lock_is_held(wallet: PlatformWallet | None) -> bool:
    """Whether the calling thread currently owns the wallet's re-entrant lock.

    The wallet lock is the *only* thing that serialises the ledger, so the fake
    store records this flag on every write: a write that happens outside the
    critical section means a concurrent update could be persisted torn.
    """
    if wallet is None:
        return False
    # Deliberate white box: the lock is the very thing this test asserts about.
    owned = getattr(wallet._lock, "_is_owned", None)
    return bool(owned()) if owned is not None else False


class FakeWalletStore:
    """In-memory stand-in for the two wallet methods of ``StateStore``.

    It records every write (the pair that was written, and whether the wallet lock
    was held at that instant) so a test can prove that a read-modify-write is
    atomic and that no write is lost.  ``fail=True`` simulates a store failure.
    """

    def __init__(self, row: FakeWalletRow | None = None, *, fail: bool = False) -> None:
        self.row = row
        self.fail = fail
        self.writes: list[tuple[float, float]] = []
        self.lock_held: list[bool] = []
        self.wallet: PlatformWallet | None = None
        self._guard = threading.Lock()

    def save_wallet(self, *, cash: float, initial_balance: float) -> None:
        """Record one durable write, or fail like a broken store would."""
        if self.fail:
            raise StateStoreError("state store write failed (save_wallet): boom")
        with self._guard:
            self.lock_held.append(wallet_lock_is_held(self.wallet))
            self.writes.append((float(cash), float(initial_balance)))
            self.row = FakeWalletRow(
                cash=float(cash),
                initial_balance=float(initial_balance),
                updated_at=pd.Timestamp(START),
            )

    def load_wallet(self) -> FakeWalletRow | None:
        """Return the stored row, or ``None`` when nothing was ever written."""
        return self.row


class BareStore:
    """A store that does not implement the wallet seam at all."""


def make_wallet(**kwargs: Any) -> PlatformWallet:
    """Build a paper wallet with a deterministic balance."""
    kwargs.setdefault("initial_balance", INITIAL)
    return PlatformWallet(**kwargs)


# ---------------------------------------------------------------------------
# 1. construction and identity
# ---------------------------------------------------------------------------


def test_a_wallet_starts_at_its_configured_balance() -> None:
    wallet = make_wallet()

    assert wallet.name == "platform"
    assert wallet.mode is RunMode.PAPER
    assert wallet.is_authoritative is True
    assert wallet.initial_balance == pytest.approx(INITIAL)
    assert wallet.cash == pytest.approx(INITIAL)
    assert wallet.spendable() == pytest.approx(INITIAL)


def test_a_wallet_can_be_named_and_can_mirror_a_venue() -> None:
    wallet = PlatformWallet(initial_balance=500.0, mode="live", name="binance")

    assert wallet.name == "binance"
    assert wallet.mode is RunMode.LIVE
    assert wallet.is_authoritative is False
    assert wallet.cash == pytest.approx(500.0)


def test_an_unnamed_wallet_falls_back_to_the_platform_name() -> None:
    assert PlatformWallet(initial_balance=1.0, name="").name == "platform"


@pytest.mark.parametrize("initial_balance", [0.0, -1.0, float("nan"), float("inf")])
def test_a_wallet_refuses_a_non_positive_initial_balance(initial_balance: float) -> None:
    with pytest.raises(WalletError, match="initial_balance must be positive"):
        PlatformWallet(initial_balance=initial_balance)


def test_the_initial_balance_error_names_the_rejected_value() -> None:
    with pytest.raises(WalletError) as error:
        PlatformWallet(initial_balance=0.0)
    assert str(error.value) == "initial_balance must be positive, got 0.0"


def test_the_repr_is_exact_and_carries_no_secret() -> None:
    wallet = make_wallet()

    assert repr(wallet) == (
        "PlatformWallet(name='platform', mode='paper', cash=1000.0, authoritative=True)"
    )
    assert repr(PlatformWallet(initial_balance=1.0, mode=RunMode.LIVE)) == (
        "PlatformWallet(name='platform', mode='live', cash=1.0, authoritative=False)"
    )


# ---------------------------------------------------------------------------
# 2. nominal movements
# ---------------------------------------------------------------------------


def test_debit_and_credit_return_the_new_cash_and_move_it_exactly() -> None:
    wallet = make_wallet()

    assert wallet.debit(250.0, reason="entry") == pytest.approx(750.0)
    assert wallet.cash == pytest.approx(750.0)
    assert wallet.credit(125.5, reason="exit") == pytest.approx(875.5)
    assert wallet.cash == pytest.approx(875.5)
    assert wallet.initial_balance == pytest.approx(INITIAL)


def test_a_zero_amount_is_a_no_op() -> None:
    wallet = make_wallet()

    assert wallet.debit(0.0) == pytest.approx(INITIAL)
    assert wallet.credit(0.0) == pytest.approx(INITIAL)
    assert wallet.apply_fill(side=OrderSide.BUY, notional=0.0, fee=0.0) == pytest.approx(INITIAL)
    assert wallet.cash == pytest.approx(INITIAL)


def test_apply_fill_of_a_buy_pays_the_notional_plus_the_fee() -> None:
    wallet = make_wallet()

    remaining = wallet.apply_fill(side=OrderSide.BUY, notional=200.0, fee=0.5)

    assert remaining == pytest.approx(INITIAL - 200.0 - 0.5)
    assert wallet.cash == pytest.approx(remaining)


def test_apply_fill_of_a_sell_receives_the_notional_minus_the_fee() -> None:
    wallet = make_wallet()

    remaining = wallet.apply_fill(side=OrderSide.SELL, notional=200.0, fee=0.5)

    assert remaining == pytest.approx(INITIAL + 200.0 - 0.5)
    assert wallet.cash == pytest.approx(remaining)


def test_apply_fill_accepts_the_side_as_its_wire_value() -> None:
    wallet = make_wallet()

    assert wallet.apply_fill(side="buy", notional=10.0, fee=0.0) == pytest.approx(990.0)
    assert wallet.apply_fill(side="sell", notional=10.0, fee=0.0) == pytest.approx(1_000.0)


# ---------------------------------------------------------------------------
# 3. the funding question
# ---------------------------------------------------------------------------


def test_can_fund_is_inclusive_of_the_whole_balance() -> None:
    wallet = make_wallet()

    assert wallet.can_fund(INITIAL) is True
    assert wallet.can_fund(INITIAL + 0.01) is False
    assert wallet.can_fund(0.0) is True


@pytest.mark.parametrize("amount", [-1.0, float("nan"), float("inf"), float("-inf"), "abc", None])
def test_can_fund_answers_false_for_an_unusable_amount(amount: Any) -> None:
    wallet = make_wallet()

    assert wallet.can_fund(amount) is False


def test_can_fund_follows_the_ledger() -> None:
    wallet = make_wallet()
    wallet.debit(900.0)

    assert wallet.can_fund(100.0) is True
    assert wallet.can_fund(100.01) is False


# ---------------------------------------------------------------------------
# 4. error paths (every message is pinned)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("amount", [-1.0, float("nan"), float("inf")])
def test_a_negative_or_non_finite_amount_is_refused_with_an_explicit_message(amount: float) -> None:
    wallet = make_wallet()

    with pytest.raises(WalletError) as error:
        wallet.debit(amount)
    assert str(error.value) == f"amount must be a finite, non-negative number, got {amount!r}"

    with pytest.raises(WalletError, match="amount must be a finite, non-negative number"):
        wallet.credit(amount)
    with pytest.raises(WalletError, match="amount must be a finite, non-negative number"):
        wallet.apply_fill(side=OrderSide.BUY, notional=amount, fee=0.0)
    with pytest.raises(WalletError, match="amount must be a finite, non-negative number"):
        wallet.apply_fill(side=OrderSide.BUY, notional=10.0, fee=amount)
    assert wallet.cash == pytest.approx(INITIAL)


def test_an_unparsable_amount_is_refused_as_well() -> None:
    wallet = make_wallet()

    with pytest.raises(WalletError, match="amount must be a finite, non-negative number"):
        wallet.debit("not-a-number")  # type: ignore[arg-type]
    assert wallet.cash == pytest.approx(INITIAL)


def test_a_debit_below_zero_is_refused_and_moves_nothing() -> None:
    store = FakeWalletStore()
    wallet = make_wallet(store=store)
    store.wallet = wallet

    with pytest.raises(WalletError) as error:
        wallet.debit(1_000.01)

    assert str(error.value) == ("platform wallet cannot debit 1000.01 USDT: available 1000.00 USDT")
    assert wallet.cash == pytest.approx(INITIAL)
    assert store.writes == []


def test_a_debit_of_the_whole_balance_is_accepted() -> None:
    wallet = make_wallet()

    assert wallet.debit(INITIAL) == pytest.approx(0.0)
    assert wallet.can_fund(0.01) is False


def test_a_paper_wallet_cannot_be_synced_from_a_venue() -> None:
    wallet = make_wallet()

    with pytest.raises(WalletError) as error:
        wallet.sync_from_venue(1_000.0)

    assert str(error.value) == "platform wallet is not a venue mirror: mode is 'paper'"
    assert wallet.spendable() == pytest.approx(INITIAL)


@pytest.mark.parametrize("operation", ["debit", "credit"])
def test_a_live_wallet_refuses_every_local_mutation(operation: str) -> None:
    wallet = PlatformWallet(initial_balance=INITIAL, mode=RunMode.LIVE)

    with pytest.raises(WalletError) as error:
        getattr(wallet, operation)(10.0)

    assert str(error.value) == (
        "platform wallet is read-only in live mode: the venue account is the source of truth"
    )
    assert wallet.cash == pytest.approx(INITIAL)


def test_a_live_wallet_refuses_a_local_fill() -> None:
    wallet = PlatformWallet(initial_balance=INITIAL, mode=RunMode.LIVE)

    with pytest.raises(WalletError, match="read-only in live mode"):
        wallet.apply_fill(side=OrderSide.BUY, notional=10.0, fee=0.0)


def test_restore_cash_refuses_a_non_finite_value() -> None:
    wallet = make_wallet()

    with pytest.raises(WalletError) as error:
        wallet.restore_cash(float("nan"))

    assert str(error.value) == "restored cash must be finite, got nan"
    assert wallet.cash == pytest.approx(INITIAL)


def test_every_wallet_failure_is_catchable_as_a_realtime_error() -> None:
    wallet = make_wallet()

    with pytest.raises(RealtimeError):
        wallet.debit(INITIAL + 1.0)
    assert issubclass(WalletError, RealtimeError)


# ---------------------------------------------------------------------------
# 5. persistence and restart
# ---------------------------------------------------------------------------


def test_a_wallet_without_a_store_keeps_its_configured_value() -> None:
    wallet = make_wallet()

    assert wallet.restore() is None
    assert wallet.cash == pytest.approx(INITIAL)
    wallet.persist()
    assert wallet.cash == pytest.approx(INITIAL)


def test_an_empty_store_has_no_row_and_restore_persists_the_configured_value() -> None:
    store = FakeWalletStore()
    wallet = make_wallet(store=store)

    assert store.load_wallet() is None
    assert wallet.restore() is None
    assert wallet.cash == pytest.approx(INITIAL)
    assert store.writes == [(INITIAL, INITIAL)]
    assert store.row is not None
    assert store.row.cash == pytest.approx(INITIAL)
    assert store.row.initial_balance == pytest.approx(INITIAL)


def test_restore_adopts_the_persisted_cash_and_initial_balance() -> None:
    store = FakeWalletStore(
        row=FakeWalletRow(cash=250.0, initial_balance=1_000.0, updated_at=pd.Timestamp(START))
    )
    wallet = make_wallet(store=store)

    assert store.load_wallet() is not None
    assert wallet.restore() == pytest.approx(250.0)
    assert wallet.cash == pytest.approx(250.0)
    assert wallet.initial_balance == pytest.approx(1_000.0)
    # Adopting a row is not a write: the store is left exactly as it was.
    assert store.writes == []


def test_every_accepted_movement_is_persisted() -> None:
    store = FakeWalletStore()
    wallet = make_wallet(store=store)
    store.wallet = wallet
    wallet.restore()

    wallet.debit(100.0)
    wallet.credit(40.0)

    assert store.writes == [(INITIAL, INITIAL), (900.0, INITIAL), (940.0, INITIAL)]
    assert store.lock_held == [True, True, True]


def test_a_restart_over_the_same_store_does_not_reset_the_wallet() -> None:
    store = FakeWalletStore()
    first = make_wallet(store=store, name="first")
    store.wallet = first
    first.restore()
    first.debit(300.0)
    first.credit(50.0)

    # A brand new process builds a brand new wallet over the same durable row.
    second = make_wallet(store=store, name="second")
    restored = second.restore()

    assert restored == pytest.approx(750.0)
    assert second.cash == pytest.approx(750.0)
    assert second.initial_balance == pytest.approx(INITIAL)
    assert second.spendable() == pytest.approx(750.0)


def test_restore_cash_is_persisted_too() -> None:
    store = FakeWalletStore()
    wallet = make_wallet(store=store)
    store.wallet = wallet

    wallet.restore_cash(1_234.0)

    assert wallet.cash == pytest.approx(1_234.0)
    assert store.writes == [(1_234.0, INITIAL)]
    assert store.lock_held == [True]


def test_a_corrupted_persisted_cash_is_refused() -> None:
    store = FakeWalletStore(row=FakeWalletRow(cash=float("nan"), initial_balance=INITIAL))
    wallet = make_wallet(store=store)

    with pytest.raises(WalletError) as error:
        wallet.restore()

    assert str(error.value) == "restored cash must be finite, got nan"
    assert wallet.cash == pytest.approx(INITIAL)


def test_a_corrupted_persisted_initial_balance_is_refused() -> None:
    store = FakeWalletStore(row=FakeWalletRow(cash=10.0, initial_balance=0.0))
    wallet = make_wallet(store=store)

    with pytest.raises(WalletError, match="initial_balance must be positive"):
        wallet.restore()
    assert wallet.cash == pytest.approx(INITIAL)


def test_a_store_without_the_wallet_seam_behaves_like_a_store_without_a_row() -> None:
    wallet = make_wallet(store=BareStore())  # type: ignore[arg-type]

    assert wallet.restore() is None
    wallet.persist()
    assert wallet.debit(10.0) == pytest.approx(990.0)


def test_a_failing_store_is_logged_and_never_loses_the_in_memory_cash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeWalletStore(fail=True)
    wallet = make_wallet(store=store)

    with caplog.at_level(logging.ERROR, logger="trading_platform.realtime"):
        assert wallet.debit(100.0) == pytest.approx(900.0)

    assert wallet.cash == pytest.approx(900.0)
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "platform_wallet_persist_failed"
    ]
    assert len(events) == 1
    assert events[0].levelno == logging.ERROR
    assert events[0].context["cash"] == pytest.approx(900.0)
    assert "boom" in events[0].context["error"]


# ---------------------------------------------------------------------------
# 6. thread safety -- one shared ledger, many profile threads
# ---------------------------------------------------------------------------

THREADS = 8
OPERATIONS = 200


def test_concurrent_debits_and_credits_lose_no_update() -> None:
    store = FakeWalletStore()
    wallet = make_wallet(initial_balance=100_000.0, store=store)
    store.wallet = wallet
    wallet.restore()
    barrier = threading.Barrier(THREADS)
    failures: list[BaseException] = []
    failures_guard = threading.Lock()

    def hammer() -> None:
        try:
            barrier.wait(timeout=30)
            for _ in range(OPERATIONS // 2):
                wallet.debit(1.0, reason="worker")
                wallet.credit(1.0, reason="worker")
        except BaseException as exc:  # reported to the main thread, never swallowed
            with failures_guard:
                failures.append(exc)

    workers = [threading.Thread(target=hammer) for _ in range(THREADS)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)

    assert failures == []
    assert all(not worker.is_alive() for worker in workers)
    # Every equal debit has its matching credit: the ledger is exactly back where
    # it started, which is only true when no update was lost.
    assert wallet.cash == pytest.approx(100_000.0)
    # Every single mutation reached the store, and every write happened while the
    # wallet lock was held: the read-modify-write is atomic, the row is never torn.
    assert len(store.writes) == 1 + THREADS * OPERATIONS
    assert len(store.lock_held) == len(store.writes)
    assert all(store.lock_held)
    # A serialised ledger changes by exactly one unit per write.  An interleaved
    # (unlocked) pair of updates would show a repeated value or a jump of two.
    deltas = [
        after[0] - before[0] for before, after in zip(store.writes, store.writes[1:], strict=False)
    ]
    assert set(deltas) == {1.0, -1.0}


def test_concurrent_reads_never_observe_a_partial_balance() -> None:
    wallet = make_wallet(initial_balance=10_000.0)
    barrier = threading.Barrier(4)
    observed: list[float] = []
    observed_guard = threading.Lock()

    def reader() -> None:
        barrier.wait(timeout=30)
        for _ in range(200):
            with observed_guard:
                observed.append(wallet.cash)

    def writer() -> None:
        barrier.wait(timeout=30)
        for _ in range(200):
            wallet.debit(1.0)
            wallet.credit(1.0)

    workers = [threading.Thread(target=reader) for _ in range(3)] + [
        threading.Thread(target=writer)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)

    assert len(observed) == 600
    assert set(observed) <= {9_999.0, 10_000.0}


# ---------------------------------------------------------------------------
# 7. live mode: the wallet mirrors the venue and says so
# ---------------------------------------------------------------------------


def test_a_live_wallet_mirrors_the_balance_the_venue_reports() -> None:
    wallet = PlatformWallet(initial_balance=INITIAL, mode=RunMode.LIVE)

    assert wallet.spendable() == pytest.approx(INITIAL)  # no invented 0.0
    assert wallet.can_fund(INITIAL) is True

    wallet.sync_from_venue(2_500.0, at=pd.Timestamp(START))

    assert wallet.spendable() == pytest.approx(2_500.0)
    assert wallet.cash == pytest.approx(INITIAL)  # the local ledger never moved
    assert wallet.can_fund(2_500.0) is True
    assert wallet.can_fund(2_500.01) is False
    # Without a clock, a live mirror is stamped with its last venue reading.
    assert wallet.snapshot().updated_at == pd.Timestamp(START)


def test_a_live_wallet_without_a_venue_reading_has_no_instant() -> None:
    wallet = PlatformWallet(initial_balance=INITIAL, mode=RunMode.LIVE)

    assert wallet.snapshot().updated_at is None


def test_a_live_wallet_never_falls_back_to_zero() -> None:
    wallet = PlatformWallet(initial_balance=50.0, mode=RunMode.LIVE)

    wallet.sync_from_venue(0.0)
    assert wallet.spendable() == pytest.approx(0.0)
    assert wallet.can_fund(0.01) is False


def test_a_live_wallet_refuses_a_non_finite_venue_balance() -> None:
    wallet = PlatformWallet(initial_balance=INITIAL, mode=RunMode.LIVE)

    with pytest.raises(WalletError, match="amount must be a finite, non-negative number"):
        wallet.sync_from_venue(float("nan"))
    assert wallet.spendable() == pytest.approx(INITIAL)


# ---------------------------------------------------------------------------
# 8. the read model
# ---------------------------------------------------------------------------


def test_a_snapshot_describes_the_shared_ledger() -> None:
    wallet = make_wallet()

    snapshot = wallet.snapshot(
        positions_value=300.0,
        deployed=200.0,
        realized_pnl=12.0,
        unrealized_pnl=-4.0,
        total_exposure=250.0,
        profiles=3,
        updated_at=pd.Timestamp(START),
    )

    assert isinstance(snapshot, WalletSnapshot)
    assert snapshot.name == "platform"
    assert snapshot.mode is RunMode.PAPER
    assert snapshot.initial_balance == pytest.approx(INITIAL)
    assert snapshot.cash == pytest.approx(INITIAL)
    assert snapshot.equity == pytest.approx(INITIAL + 300.0)
    assert snapshot.deployed == pytest.approx(200.0)
    assert snapshot.realized_pnl == pytest.approx(12.0)
    assert snapshot.unrealized_pnl == pytest.approx(-4.0)
    assert snapshot.total_exposure == pytest.approx(250.0)
    assert snapshot.profiles == 3
    assert snapshot.source == "local"
    assert snapshot.updated_at == pd.Timestamp(START)


def test_a_snapshot_of_a_live_wallet_is_sourced_from_the_venue() -> None:
    wallet = PlatformWallet(initial_balance=INITIAL, mode=RunMode.LIVE)

    snapshot = wallet.snapshot()

    assert snapshot.source == "venue"
    assert snapshot.mode is RunMode.LIVE
    assert snapshot.equity == pytest.approx(INITIAL)


def test_a_snapshot_takes_its_instant_from_the_injected_clock() -> None:
    clock = ManualClock(start=START)
    wallet = make_wallet(clock=clock)

    assert wallet.snapshot().updated_at == pd.Timestamp(START)
    assert wallet.snapshot(updated_at=pd.Timestamp(START)).updated_at == pd.Timestamp(START)


def test_a_snapshot_without_a_clock_has_no_instant() -> None:
    assert make_wallet().snapshot().updated_at is None


def test_the_snapshot_payload_carries_exactly_the_documented_keys() -> None:
    wallet = make_wallet()

    payload = wallet.snapshot(updated_at=pd.Timestamp(START)).to_dict()

    assert set(payload) == SNAPSHOT_KEYS
    assert payload["name"] == "platform"
    assert payload["mode"] == "paper"
    assert payload["initial_balance"] == pytest.approx(INITIAL)
    assert payload["cash"] == pytest.approx(INITIAL)
    assert payload["equity"] == pytest.approx(INITIAL)
    assert payload["deployed"] == 0.0
    assert payload["realized_pnl"] == 0.0
    assert payload["unrealized_pnl"] == 0.0
    assert payload["total_exposure"] == 0.0
    assert payload["profiles"] == 0
    assert payload["source"] == "local"
    assert payload["updated_at"] == pd.Timestamp(START).isoformat()


def test_the_snapshot_payload_never_carries_a_non_finite_number() -> None:
    snapshot = WalletSnapshot(
        name="platform",
        mode=RunMode.PAPER,
        initial_balance=float("nan"),
        cash=float("inf"),
        equity=float("-inf"),
        deployed=float("nan"),
        realized_pnl=float("nan"),
        unrealized_pnl=float("inf"),
        total_exposure=float("nan"),
        profiles=2,
        source="local",
        updated_at=None,
    )

    payload = snapshot.to_dict()

    assert set(payload) == SNAPSHOT_KEYS
    for key in (
        "initial_balance",
        "cash",
        "equity",
        "deployed",
        "realized_pnl",
        "unrealized_pnl",
        "total_exposure",
    ):
        assert payload[key] is None, key
    assert payload["updated_at"] is None
    assert payload["profiles"] == 2
    assert payload["source"] == "local"


def test_a_non_finite_mark_to_market_cannot_poison_the_payload() -> None:
    wallet = make_wallet()

    payload = wallet.snapshot(positions_value=float("nan")).to_dict()

    assert payload["cash"] == pytest.approx(INITIAL)
    assert payload["equity"] is None


def test_a_non_numeric_snapshot_field_collapses_to_none() -> None:
    polluted = dataclasses.replace(make_wallet().snapshot(), cash="not-a-number")  # type: ignore[arg-type]

    assert polluted.to_dict()["cash"] is None


def test_an_absent_snapshot_field_collapses_to_none() -> None:
    absent = dataclasses.replace(
        make_wallet().snapshot(),
        deployed=None,  # type: ignore[arg-type]
        equity=None,  # type: ignore[arg-type]
    )

    payload = absent.to_dict()

    assert payload["deployed"] is None
    assert payload["equity"] is None


def test_the_snapshot_is_json_serialisable() -> None:
    import json

    wallet = make_wallet()
    wallet.snapshot(updated_at=pd.Timestamp(START))

    assert json.loads(json.dumps(wallet.snapshot(updated_at=pd.Timestamp(START)).to_dict()))
