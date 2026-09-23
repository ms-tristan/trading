"""Supervision of N concurrent profiles: the realtime platform itself.

:class:`RealtimeOrchestrator` owns the whole picture -- the profiles, the shared
state store, the global kill switch and one :class:`~trading_platform.realtime.runner.ProfileRunner`
task per enabled profile -- and answers the read model the monitoring layer
consumes (:meth:`RealtimeOrchestrator.snapshot`, :meth:`RealtimeOrchestrator.health`).

Wiring, and what is deliberately injectable
-------------------------------------------
The two seams that would otherwise reach the outside world are **factories**, not
classes:

``stream_factory``
    builds the :class:`~trading_platform.realtime.stream.MarketStream` of a profile.
    It has **no default**: a market-data source is a decision only the caller can
    make (replay, polling provider, ``ccxt.pro``), so a missing factory raises
    :class:`~trading_platform.core.errors.MarketStreamError` instead of silently
    inventing one.
``broker_factory``
    builds the venue adapter; it defaults to :func:`default_broker_factory`, which
    picks :class:`~trading_platform.realtime.broker.PaperBroker` for a paper profile
    and :class:`~trading_platform.realtime.broker.CcxtBroker` for a live one.  The
    optional ``ccxt`` import therefore happens *inside a function body*, never at
    package import time.

Restart safety
--------------
:meth:`RealtimeOrchestrator.start` reconciles **every** profile against its venue
*before* any order can be sent.  A mismatch does not stop the platform and does not
"repair" anything: the profile is persisted as ``DEGRADED`` with the report as its
detail, the event ``reconciliation_mismatch`` is logged, and the profile stays
degraded across its subsequent ticks until an operator looks at it.  Nothing is
double-submitted either: the deterministic client order id of the runner plus the
idempotency check of the gateway make a replayed decision a no-op.

`paper` and `live` share the single lifecycle
---------------------------------------------
The orchestrator never branches on the mode beyond asking the factory for a broker
and handing the profile to the one
:class:`~trading_platform.realtime.gateway.ExecutionGateway`.  The mechanical
guarantee of that separation lives in the gateway (the broker-mode assertion) and in
the live gate; here there is only wiring.

Runtime profile control
-----------------------
The platform is not frozen once started: :meth:`RealtimeOrchestrator.pause_profile`,
:meth:`RealtimeOrchestrator.resume_profile`,
:meth:`RealtimeOrchestrator.delete_profile` and
:meth:`RealtimeOrchestrator.add_profile` mutate it while it runs.  Every one of them
is a coroutine, so they all execute **inside the engine loop**: they can never
interleave with a candle being processed, and the registry dictionaries are only
ever written from that loop (the threaded HTTP layer reaches them through
:class:`~trading_platform.realtime.control.RuntimeProfileController`, which marshals
the coroutine onto the loop).  Two semantics are worth restating because they are the
product rules, not implementation details: *pausing* only closes the entry gate --
the static stop of an open position and every exit signal keep being evaluated, so
nothing is left unmanaged -- and *deleting* flattens the open position through the
gateway before anything is removed, so a profile is never removed while it is still
exposed.  Both are persisted (the pause flag in the ``meta`` table, the profile
list in the SQLite ``profiles`` table) and survive a restart.

An empty platform is a legal platform
-------------------------------------
The SQLite state store is the single source of truth for the profile set, so the
platform may legitimately hold **zero** profiles: a freshly deployed host starts
empty and the operator creates profiles from the dashboard.  Constructing the
orchestrator with an empty ``profiles`` sequence therefore succeeds,
:meth:`RealtimeOrchestrator.start` and
:meth:`RealtimeOrchestrator.run_once` complete immediately (``run_once`` answers
``[]``), :meth:`RealtimeOrchestrator.run_forever` returns instead of blocking, and
``POST /api/profiles`` still works because the wiring (the stream factory and the
store) is prepared regardless of how many profiles there are.  The duplicated-id
check stays: a platform that silently drops a profile is still worse than one that
refuses to start.

One broken profile is quarantined, never fatal
----------------------------------------------
A profile can be unstartable for reasons that have nothing to do with the rest of
the platform -- the live outage was a persisted ``strategy`` name whose strategy had
been removed by a release, and the same shape appears when a venue refuses a
credential or a stream factory rejects a symbol.  The boot therefore treats each
profile **independently**: a profile whose runner cannot be built (see
:meth:`RealtimeOrchestrator._prepare_runners`) or cannot be started (see
:meth:`RealtimeOrchestrator._start_runner`) is *quarantined* -- the reason is
sanitised and recorded through :meth:`RealtimeOrchestrator._record_profile_failure`,
the profile is persisted as ``ERROR`` and published by
:meth:`RealtimeOrchestrator.profile_failures` -- while every other profile is built
and supervised exactly as before, and the monitoring API still binds.  The whole
platform is never taken down by one profile, and the failure is never invisible
either: it is logged as ``profile_boot_failed``, it degrades
:meth:`RealtimeOrchestrator.health` and it is carried by the additive
``profile_failures`` key of the monitoring payload.

Two failure classes are deliberately **not** quarantined: :class:`asyncio.CancelledError`
(a shutdown is not a profile fault) and an unexpected :class:`Exception`, which is a
programming bug and stays loud.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import pandas as pd

from trading_platform.config.models import (
    MonitoringConfig,
    ProfileConfig,
    RealtimeConfig,
    resolve_platform_initial_balance,
)
from trading_platform.core.errors import (
    BrokerError,
    GatewayError,
    KillSwitchActiveError,
    MarketStreamError,
    OrderRejectedError,
    ProfileError,
    RealtimeError,
    RiskLimitExceededError,
    StrategyError,
    WalletError,
)
from trading_platform.core.models import Direction, TradeRecord
from trading_platform.realtime.clock import Clock
from trading_platform.realtime.models import (
    EngineCounters,
    EquityPoint,
    OrderRequest,
    OrderSide,
    OrderType,
    PlatformSnapshot,
    Position,
    ProfileHealth,
    ProfileSnapshot,
    ProfileState,
    ProfileStatus,
    RunMode,
    TradeSignalDecision,
    new_client_order_id,
)
from trading_platform.realtime.observability import LOGGER_NAME, log_event

# --- ORPHAN SWEEP WIRING (WP3) ---------------------------------------------
# Everything about the sweep itself lives in ``realtime.orphans``; this module
# only plugs it into the boot order and publishes its report.
from trading_platform.realtime.orphans import (
    OrphanSweepReport,
    load_orphan_report,
    sweep_orphaned_positions,
)

# --- END ORPHAN SWEEP WIRING (WP3) -----------------------------------------
# --- PROFILE QUARANTINE WIRING ---------------------------------------------
# The one profile-related helper the orchestrator borrows from the store: the
# sanitiser that strips the value a validation error echoes.  A profile row is
# exactly where an operator might have hand-written a credential, and the reason
# recorded below reaches the log stream *and* the ``profile_failures`` block of the
# monitoring payload, so it is never the raw exception text.  The private alias is
# intentional: the store publishes the public ``redact_profile_error`` and keeps
# this name for its existing callers.
from trading_platform.realtime.store import _redact_profile_error

# --- END PROFILE QUARANTINE WIRING -----------------------------------------

if TYPE_CHECKING:
    from trading_platform.realtime.broker import Broker
    from trading_platform.realtime.risk import (
        KillSwitch,
        KillSwitchState,
        LiveTradingGate,
        PlatformRiskLimits,
        PlatformRiskState,
        RiskManager,
    )
    from trading_platform.realtime.runner import ProfileRunner
    from trading_platform.realtime.settings import PlatformSettings
    from trading_platform.realtime.store import CandleRow, StateStore
    from trading_platform.realtime.stream import MarketStream
    from trading_platform.realtime.wallet import PlatformWallet

__all__ = [
    "BrokerFactory",
    "RealtimeOrchestrator",
    "StreamFactory",
    "default_broker_factory",
    "make_live_gate",
    "make_risk_manager",
]

_LOGGER = logging.getLogger(LOGGER_NAME)

#: Builds the market stream of one profile.
StreamFactory = Callable[[ProfileConfig], "MarketStream"]

#: Builds the venue adapter of one profile.
BrokerFactory = Callable[[ProfileConfig], "Broker"]

#: Statuses that make the platform health ``degraded``.
_UNHEALTHY_STATUSES: frozenset[ProfileStatus] = frozenset(
    {ProfileStatus.DEGRADED, ProfileStatus.ERROR, ProfileStatus.HALTED}
)

#: Errors that quarantine **one** profile instead of aborting the boot.
#:
#: :class:`~trading_platform.core.errors.RealtimeError` covers the engine's own
#: failures (a missing credential, an unreachable venue, a rejected symbol).
#: :class:`~trading_platform.core.errors.StrategyError` is *not* a
#: ``RealtimeError`` -- it belongs to the backtest hierarchy -- yet an unknown
#: strategy name or a rejected parameter set is exactly the configuration mistake
#: the platform must survive: it is what kept the live ``zaaaa`` profile (whose
#: ``timesfm`` strategy had been removed by a release) from taking the whole
#: platform down.  Both are caught; anything else stays loud.
_PROFILE_BOOT_ERRORS: tuple[type[BaseException], ...] = (RealtimeError, StrategyError)


def _sanitised_failure(exc: BaseException, reason: str) -> BaseException:
    """Return ``exc`` when its rendering is already sanitised, else a safe stand-in.

    The original exception is returned untouched in the common case, which keeps its
    type and its traceback in the hands of the runner.  An exception whose rendering
    still carries a value (pydantic echoes it as ``input_value=...``) is replaced by
    a :class:`~trading_platform.core.errors.RealtimeError` built from the sanitised
    reason alone, so nothing that renders or logs it can leak the value.
    """
    if _redact_profile_error(exc) == str(exc):
        return exc
    return RealtimeError(reason)


#: Prefix of the ``meta`` key holding the persisted pause flag of one profile.
#:
#: The flag lives in the existing free-form ``meta`` table (values ``"1"`` for
#: paused and ``"0"`` for running), so pausing a profile needs **no schema change**
#: and survives a process restart: :meth:`RealtimeOrchestrator._build_runner` reads
#: it back and re-pauses the runner it just built.
_PAUSED_META_PREFIX = "paused:"

#: Smallest positive balance the shared ledger accepts.
#:
#: An empty platform resolves its starting cash to ``0.0`` and the ledger refuses a
#: non-positive balance, but the platform must still exist (it serves its API and
#: accepts a profile created from the dashboard).  This is the placeholder it is
#: built with; the operator's configured ``realtime.platform_initial_balance`` --
#: or the first created profile's allocation -- is what actually funds it.
_MINIMUM_WALLET_BALANCE = 1e-9

#: Sequence number of the market order that flattens a profile being deleted.
#:
#: The client order id of that order is built from the deletion instant and this
#: sequence, so a delete replayed after a restart produces the same identifier and
#: the gateway answers it idempotently instead of sending a second order.
_DELETE_ORDER_SEQUENCE = 0


def make_risk_manager(
    profile: ProfileConfig,
    *,
    clock: Clock,
    kill_switch: KillSwitch | None = None,
    platform: PlatformRiskLimits | None = None,
    wallet: PlatformWallet | None = None,
    state: PlatformRiskState | None = None,
) -> RiskManager:
    """Build the per-profile risk manager enforcing ``profile.risk``.

    Parameters
    ----------
    profile:
        Profile whose ``risk`` block is turned into limits.
    clock:
        Time seam used by the daily counters of the manager.
    kill_switch:
        Optional global halt; when injected it is the first limit evaluated.
    platform:
        Optional platform-wide caps, enforced in addition to the per-profile ones.
    wallet:
        Optional shared wallet; when injected every entry is checked against the
        cash it can really fund.
    state:
        Optional aggregation of the figures every profile published, read by the
        platform-wide caps.
    """
    from trading_platform.realtime.risk import RiskLimits, RiskManager

    return RiskManager(
        RiskLimits.from_config(profile.risk),
        clock=clock,
        kill_switch=kill_switch,
        platform=platform,
        wallet=wallet,
        state=state,
    )


def make_live_gate(environ: Mapping[str, str] | None = None) -> LiveTradingGate:
    """Build the live-trading gate reading ``TB_ALLOW_LIVE_TRADING``.

    Parameters
    ----------
    environ:
        Environment mapping to read; defaults to the process environment.
    """
    from trading_platform.realtime.risk import LiveTradingGate

    return LiveTradingGate(environ)


def default_broker_factory(
    profile: ProfileConfig,
    *,
    clock: Clock | None = None,
    environ: Mapping[str, str] | None = None,
    wallet: PlatformWallet | None = None,
) -> Broker:
    """Build the venue adapter of ``profile`` -- paper simulated, live real.

    A ``paper`` profile always gets a :class:`~trading_platform.realtime.broker.PaperBroker`
    seeded **deterministically from the profile id**, so two runs of the same profile
    produce byte-identical fills.  A ``live`` profile gets a
    :class:`~trading_platform.realtime.broker.CcxtBroker` built from the credentials
    the environment carries; missing credentials are a configuration error, raised
    before any order could be attempted.

    Parameters
    ----------
    profile:
        Profile to serve.
    clock:
        Time seam handed to the adapter; the system clock is used when omitted.
    environ:
        Environment mapping the credentials are read from.
    wallet:
        Optional shared wallet of the platform.  A paper venue funds its fills from
        it -- that is what makes every simulated profile spend the *same* USDT
        ledger -- and a private simulated wallet is built when none is injected, so
        every existing construction keeps its exact previous behaviour.  A live
        venue ignores it: there the wallet *mirrors* the account the venue reports,
        and the venue itself is the source of truth.

    Raises
    ------
    ProfileError
        When a ``live`` profile has no usable credentials in the environment.
    """
    from trading_platform.realtime.clock import SystemClock

    resolved_clock = SystemClock() if clock is None else clock
    if str(profile.mode) == RunMode.LIVE.value:
        from trading_platform.realtime.broker import CcxtBroker
        from trading_platform.realtime.credentials import credentials_from_env

        credentials = credentials_from_env(
            str(profile.id), exchange=str(profile.exchange), environ=environ
        )
        if credentials is None or not credentials.configured:
            raise ProfileError(
                f"profile {profile.id!r} is live but has no credentials in the environment: "
                f"set TB_LIVE_API_KEY/TB_LIVE_API_SECRET or "
                f"TB_PROFILE_{str(profile.id).upper()}_API_KEY/_API_SECRET"
            )
        return CcxtBroker(
            credentials,
            clock=resolved_clock,
            mode=RunMode.LIVE,
            exchange=str(profile.exchange),
        )
    from trading_platform.realtime.broker import PaperBroker

    seed = int.from_bytes(hashlib.sha256(str(profile.id).encode("utf-8")).digest()[:8], "big")
    return PaperBroker(
        clock=resolved_clock,
        initial_balance=float(profile.initial_balance),
        seed=seed,
        name="paper",
        wallet=wallet,
    )


class RealtimeOrchestrator:
    """Run N independently configured profiles over one shared state store.

    Parameters
    ----------
    profiles:
        The profiles to run.  An **empty** sequence is legal -- the platform boots,
        serves its API and accepts a new profile -- while a duplicated id still
        raises :class:`~trading_platform.core.errors.ProfileError`: a platform that
        silently drops a profile is worse than one that refuses to start.
    store:
        Durable state, shared by every profile.  It is also the single source of
        truth for the profile set **and** for the engine settings.
    clock:
        Time seam; no module of the layer reads the wall clock directly.
    realtime:
        Engine settings shared by the run (poll intervals, timeouts, state paths).
        Only ``state_db``/``logs_dir`` are authoritative at construction; every
        other field is replaced from the store at boot (see
        :meth:`_load_settings_into_configs`).
    monitoring:
        Optional HTTP surface settings; kept here so a caller has a single wiring
        object even though the server itself lives in the web layer.
    settings:
        Optional settings already resolved from the store.  When it is ``None`` the
        orchestrator builds its own bootstrap from the injected ``realtime``/
        ``monitoring`` and lets :meth:`_load_settings_into_configs` replace them
        from SQLite; when it is given, the boot reads the store once more anyway, so
        the resolved objects always describe what the store holds.
    stream_factory:
        Builds the market stream of a profile; **required** to start.
    broker_factory:
        Builds the venue adapter of a profile; defaults to
        :func:`default_broker_factory`.
    environ:
        Environment mapping used for the live gate, the credentials and the kill
        switch; defaults to the process environment.
    version:
        Version string reported by :meth:`health`.
    wallet:
        Optional shared platform wallet.  When it is omitted the orchestrator builds
        its own, lazily and exactly once, from the configuration -- the platform
        initial balance (or the sum of the profiles' allocations) -- and hands it to
        every paper venue and to every risk manager, so every profile funds its
        orders from the *same* USDT ledger.
    """

    def __init__(
        self,
        *,
        profiles: Sequence[ProfileConfig],
        store: StateStore,
        clock: Clock,
        realtime: RealtimeConfig,
        monitoring: MonitoringConfig | None = None,
        stream_factory: StreamFactory | None = None,
        broker_factory: BrokerFactory | None = None,
        environ: Mapping[str, str] | None = None,
        version: str = "",
        wallet: PlatformWallet | None = None,
        settings: PlatformSettings | None = None,
    ) -> None:
        self._profiles: tuple[ProfileConfig, ...] = tuple(profiles)
        identifiers = [str(profile.id) for profile in self._profiles]
        duplicates = sorted(
            identifier for identifier, count in Counter(identifiers).items() if count > 1
        )
        if duplicates:
            raise ProfileError(f"duplicate profile id(s): {', '.join(duplicates)}")
        self._store = store
        self._clock = clock
        self._realtime = realtime
        self._monitoring = monitoring
        self._settings = settings
        #: Guards the one-shot adoption of the persisted settings: the boot calls
        #: :meth:`_load_settings_into_configs` exactly once, and the flag makes that
        #: "once" a contract rather than an approximation.
        self._settings_loaded = False
        self._stream_factory = stream_factory
        self._broker_factory = broker_factory
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._version = str(version)
        self._by_id: dict[str, ProfileConfig] = {
            str(profile.id): profile for profile in self._profiles
        }
        self._runners: dict[str, ProfileRunner] = {}
        self._streams: dict[str, MarketStream] = {}
        self._reports: dict[str, bool] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started_ids: set[str] = set()
        #: Profiles this process could not build or start, mapped to the sanitised
        #: reason.  A profile that fails here is quarantined -- every other profile
        #: keeps running and the platform boots -- and the mapping is published by
        #: :meth:`profile_failures` and by :meth:`health`.
        self._boot_failures: dict[str, str] = {}
        self._kill_switch: KillSwitch | None = None
        self._wallet = wallet
        #: Guards the one-shot restore of the shared wallet: the boot runs on the
        #: engine thread while the monitoring API answers on its own thread, and
        #: "restored once" is a contract, not an approximation.
        self._wallet_lock = threading.Lock()
        self._wallet_restored = False
        self._restored_cash: float | None = None
        self._platform_state: PlatformRiskState | None = None
        self._monotonic_start: float | None = None
        self._started_at: pd.Timestamp | None = None
        self._prepared = False
        # --- ORPHAN SWEEP WIRING (WP3) -------------------------------------
        #: Report of the last startup orphan sweep, or ``None`` before this
        #: process ever booted.  The engine sets it at boot; a read-only process
        #: reads the last boot's report back from the store (see
        #: :meth:`orphan_report`).
        self._orphan_report: OrphanSweepReport | None = None
        # --- END ORPHAN SWEEP WIRING (WP3) ---------------------------------

    # -- introspection ------------------------------------------------------

    @property
    def profiles(self) -> tuple[ProfileConfig, ...]:
        """Return every profile of the platform, in the order it was given."""
        return self._profiles

    @property
    def wallet(self) -> PlatformWallet:
        """Return the one shared wallet every profile funds its orders from.

        The wallet is built **once** per orchestrator instance, lazily, and never
        resolved through a module-level global: it is the same object the paper
        venues spend from, the same one the risk managers check before an order and
        the same one the snapshot reports.  Its mode follows the profiles -- a
        platform that runs any enabled ``live`` profile has a wallet that *mirrors*
        the venue account (read-only, never locally debited), every other platform
        has the local simulated ledger.

        The starting cash is :func:`resolve_platform_initial_balance`: the
        configured ``realtime.platform_initial_balance`` when there is one, else the
        sum of the profiles' effective allocations -- which is what keeps a
        configuration written before the shared wallet existed unchanged.

        An **empty** platform resolves that sum to ``0.0``, and the ledger refuses a
        non-positive starting balance.  The platform still has to exist -- it serves
        its API and accepts a profile created from the dashboard -- so the wallet is
        built over the smallest positive balance the model allows and left at zero
        cash.  As soon as a profile is created, :meth:`add_profile` re-reads it
        through the very same object.
        """
        if self._wallet is None:
            from trading_platform.realtime.wallet import PlatformWallet

            resolved = float(resolve_platform_initial_balance(self._realtime, self._profiles))
            self._wallet = PlatformWallet(
                initial_balance=resolved if resolved > 0.0 else _MINIMUM_WALLET_BALANCE,
                mode=RunMode.LIVE if self._has_enabled_live_profile() else RunMode.PAPER,
                store=self._store,
                clock=self._clock,
                name="platform",
            )
        return self._wallet

    @property
    def monitoring(self) -> MonitoringConfig | None:
        """Return the monitoring settings, read from the store at boot."""
        return self._monitoring

    @property
    def realtime(self) -> RealtimeConfig:
        """Return the engine settings shared by the run, read from the store at boot."""
        return self._realtime

    @property
    def settings(self) -> PlatformSettings | None:
        """Return the settings resolved from the store, or ``None`` before boot.

        A reader that wants the *pair* of models -- the realtime and monitoring
        sections that the state database currently declares -- reads them here
        instead of reassembling them from :attr:`realtime` and :attr:`monitoring`.
        """
        return self._settings

    # --- ORPHAN SWEEP WIRING (WP3) -----------------------------------------

    @property
    def orphan_report(self) -> OrphanSweepReport | None:
        """Return the last orphan-position sweep report, or ``None``.

        The report of the boot that ran in **this** process wins.  A process that
        never booted the engine -- the monitoring server of ``realtime serve`` --
        reads the report of the last boot back from the store, so an operator
        always sees whether the platform was swept and what the sweep did.

        ``None`` means "never swept": the API renders it as an explicit ``null``
        rather than a missing key, so a consumer never has to guess.

        The read path is deliberately **not** gated on the engine having booted:
        a read-only composition that never opens the engine still has to answer
        the report the store holds, exactly like :meth:`_ensure_wallet_restored`
        answers the durable cash.
        """
        if self._orphan_report is not None:
            return self._orphan_report
        try:
            return load_orphan_report(self._store)
        except RealtimeError:
            return None

    # --- END ORPHAN SWEEP WIRING (WP3) -------------------------------------

    def __repr__(self) -> str:
        """Return a short, secret-free representation of the orchestrator."""
        return (
            f"RealtimeOrchestrator(profiles={len(self._profiles)}, "
            f"runners={len(self._runners)}, version={self._version!r})"
        )

    def profile_ids(self) -> list[str]:
        """Return every profile id of the platform, sorted."""
        return sorted(self._by_id)

    def runner(self, profile_id: str) -> ProfileRunner | None:
        """Return the runner of ``profile_id``, or ``None`` when it has none."""
        return self._runners.get(str(profile_id))

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Persist the profiles, reconcile them, and supervise one task each.

        A profile whose start fails is quarantined (see
        :meth:`_record_profile_failure`) and simply gets no supervised task: the
        platform still starts, every other profile still runs, and the failure is
        published by :meth:`profile_failures`.
        """
        runner_ids = self._prepare_runners()
        if self._monotonic_start is None:
            self._monotonic_start = float(self._clock.monotonic())
            self._started_at = pd.Timestamp(self._clock.now())
        for profile_id in runner_ids:
            if not await self._start_runner(profile_id):
                continue
            self._tasks[profile_id] = asyncio.create_task(
                self._supervise(self._runners[profile_id])
            )
        log_event(
            _LOGGER,
            "platform_started",
            profiles=len(self._profiles),
            running=len(runner_ids),
            version=self._version,
        )

    async def stop(self) -> None:
        """Cancel every profile, await them, persist ``STOPPED`` and close the store."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=float(self._realtime.stream_poll_timeout_seconds),
                )
            except TimeoutError:  # pragma: no cover - a task refusing to stop
                log_event(
                    _LOGGER,
                    "shutdown_timeout",
                    level=logging.ERROR,
                    timeout=float(self._realtime.stream_poll_timeout_seconds),
                )
        await self._stop_streams()
        for profile_id in sorted(self._started_ids):
            runner = self._runners.get(profile_id)
            if runner is not None:
                await runner.stop()
        self._started_ids.clear()
        self._store.close()
        log_event(_LOGGER, "platform_stopped", profiles=len(self._profiles))

    async def run_once(self) -> list[TradeSignalDecision]:
        """Run exactly one tick for every enabled profile, in profile-id order.

        Returns
        -------
        list[TradeSignalDecision]
            The decisions the tick produced, in profile-id order.  A profile whose
            stream had nothing new contributes no entry.

        A profile that cannot be started is quarantined (see
        :meth:`_record_profile_failure`) and contributes no decision; the tick still
        runs every other profile.
        """
        runner_ids = self._prepare_runners()
        if self._monotonic_start is None:
            self._monotonic_start = float(self._clock.monotonic())
            self._started_at = pd.Timestamp(self._clock.now())
        decisions = []
        for profile_id in runner_ids:
            if not await self._start_runner(profile_id):
                continue
            decision = await self._runners[profile_id].run_once()
            if decision is not None:
                decisions.append(decision)
        return decisions

    async def run_forever(self) -> None:
        """Start every profile and wait until they all stop."""
        await self.start()
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks)

    # -- read model ---------------------------------------------------------

    def profile_snapshot(self, profile_id: str) -> ProfileSnapshot | None:
        """Return the snapshot of one profile, or ``None`` when it is unknown."""
        profile = self._by_id.get(str(profile_id))
        if profile is None:
            return None
        runner = self._runners.get(str(profile_id))
        if runner is not None:
            return runner.snapshot()
        return self._fallback_snapshot(profile)

    def snapshot(self) -> PlatformSnapshot:
        """Return the whole platform as the monitoring layer sees it.

        The shared wallet view is aggregated from the profile snapshots collected
        here -- their position values, their deployed capital and their P&L -- so
        the platform-wide cash, equity and exposure are always the exact sum of the
        attributed per-profile figures the same payload carries.  The ledger is
        restored first, so the reported cash is the durable one even when this
        process never booted the engine (see :meth:`_ensure_wallet_restored`).
        """
        self._ensure_wallet_restored()
        collected = tuple(self.profile_snapshot(profile_id) for profile_id in self.profile_ids())
        state = self.kill_switch_state()
        profiles = tuple(item for item in collected if item is not None)
        return PlatformSnapshot(
            profiles=profiles,
            generated_at=pd.Timestamp(self._clock.now()),
            kill_switch=bool(state.engaged),
            kill_switch_reason=str(state.reason),
            kill_switch_changed_at=state.changed_at,
            version=self._version,
            started_at=self._started_at,
            uptime_seconds=self._uptime(),
            wallet=self.wallet.snapshot(
                positions_value=sum(item.position_value for item in profiles),
                deployed=sum(item.deployed for item in profiles),
                realized_pnl=sum(item.realized_pnl for item in profiles),
                unrealized_pnl=sum(item.unrealized_pnl for item in profiles),
                total_exposure=sum(abs(item.position_value) for item in profiles),
                profiles=len(collected),
            ),
        )

    def profile_failures(self) -> dict[str, str]:
        """Return the profiles that could not be loaded, built or started, sorted.

        The mapping is ``profile_id -> sanitised reason``, and it merges two
        sources:

        1. what the **store** could not read, as reported by its
           ``load_profile_failures()`` accessor (a persisted row whose payload is
           no longer a valid profile, e.g. one carrying a field a previous release
           removed).  The accessor is read through ``getattr`` -- a store that does
           not implement it (an in-memory double, an older adapter) simply
           contributes nothing -- and a store that raises while answering is logged
           as ``profile_failures_unavailable`` and contributes nothing either;
        2. what **this process** could not build or start (:attr:`_boot_failures`),
           which wins for a profile id both sources know about.

        The method **never raises** and answers ``{}`` when nothing is wrong, so a
        read-only caller (the monitoring API) can always ask.  That is the whole
        point: the outage this contract comes from was invisible because the reason
        only ever reached a crash-looping container's log.

        The result is sorted by profile id, so the payload is stable across calls
        and diffable in a monitoring system.
        """
        merged: dict[str, str] = {}
        reader = getattr(self._store, "load_profile_failures", None)
        if callable(reader):
            try:
                reported = reader()
                if isinstance(reported, Mapping):
                    merged.update({str(key): str(value) for key, value in reported.items()})
            except Exception as exc:  # a broken store must never hide the rest
                log_event(
                    _LOGGER,
                    "profile_failures_unavailable",
                    level=logging.WARNING,
                    error=str(exc),
                )
        merged.update(self._boot_failures)
        return dict(sorted(merged.items()))

    def health(self) -> dict[str, Any]:
        """Return the ``/api/health`` body of the platform (no HTTP concern here).

        ``profile_failures`` is the one additive key: the profiles that could not be
        loaded, built or started, mapped to their sanitised reason (see
        :meth:`profile_failures`).  It is **always present** and ``{}`` when every
        profile is fine, so an operator (or a script) reads what the platform could
        not start without parsing the log stream.  It never changes the meaning of
        the keys beside it: ``status`` is still degraded by a profile status and by
        the kill switch, and a failed profile is persisted as ``ERROR``, which is
        what makes it degrade.
        """
        snapshot = self.snapshot()
        profiles = snapshot.profiles
        engaged = self.kill_switch_state().engaged
        degraded = engaged or any(item.status in _UNHEALTHY_STATUSES for item in profiles)
        wallet = snapshot.wallet
        return {
            "status": "degraded" if degraded else "ok",
            "version": self._version,
            "uptime_seconds": self._uptime(),
            "profiles_total": len(profiles),
            "profiles_running": sum(1 for item in profiles if item.status is ProfileStatus.RUNNING),
            "kill_switch": bool(engaged),
            "checked_at": self._clock.now().isoformat(),
            "wallet": None if wallet is None else wallet.to_dict(),
            # --- ORPHAN SWEEP WIRING (WP3) ---------------------------------
            # The last orphan sweep, always present: ``None`` means "never
            # swept", an explicit ``null`` a consumer can tell apart from "swept
            # and found nothing" (which carries a ``swept_at`` timestamp).
            "orphaned_positions": self._orphan_payload(),
            # --- END ORPHAN SWEEP WIRING (WP3) -----------------------------
            # The one additive key of this delivery: what could not be loaded,
            # built or started, sanitised.  Always present, ``{}`` when clean.
            "profile_failures": self.profile_failures(),
        }

    # --- ORPHAN SWEEP WIRING (WP3) -----------------------------------------

    def _orphan_payload(self) -> dict[str, Any] | None:
        """Return the last orphan sweep as the monitoring payload, or ``None``."""
        report = self.orphan_report
        return None if report is None else report.to_dict()

    # --- END ORPHAN SWEEP WIRING (WP3) -------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return the in-process counters of every profile of the platform."""
        payload: dict[str, Any] = {}
        for profile_id in self.profile_ids():
            runner = self._runners.get(profile_id)
            counters = EngineCounters() if runner is None else runner.counters()
            payload[profile_id] = counters.to_dict()
        return payload

    # -- kill switch --------------------------------------------------------

    def kill_switch_state(self) -> KillSwitchState:
        """Return the current state of the global halt."""
        return self._kill_switch_instance().state()

    def engage_kill_switch(self, reason: str) -> KillSwitchState:
        """Halt every profile: no order is accepted from now on.

        The switch is persisted in the store, so it survives a restart; nothing is
        cancelled silently, the running profiles simply get a blocked decision on
        their next order.
        """
        state = self._kill_switch_instance().engage(str(reason), source="api")
        log_event(
            _LOGGER,
            "kill_switch_engaged",
            level=logging.WARNING,
            reason=str(reason),
            source=state.source,
            engaged=bool(state.engaged),
        )
        return state

    def release_kill_switch(self) -> KillSwitchState:
        """Clear the stored halt (a file or environment force is not clearable)."""
        state = self._kill_switch_instance().release(source="api")
        log_event(
            _LOGGER,
            "kill_switch_released",
            level=logging.WARNING,
            source=state.source,
            engaged=bool(state.engaged),
        )
        return state

    # -- runtime profile control --------------------------------------------

    def profile_config(self, profile_id: str) -> ProfileConfig | None:
        """Return the definition of one profile, or ``None`` when it is unknown."""
        return self._by_id.get(str(profile_id))

    def control_state(self) -> dict[str, Any]:
        """Return the pause/run state of every profile of the platform.

        The payload is deliberately tiny and synchronous: it is the answer the
        dashboard refreshes after a lifecycle command, so it can never be stale in a
        way the operator would notice.  ``running`` means "the orchestrator started
        this profile", ``paused`` means "its entry gate is closed" -- the two are
        independent, because a paused profile is still supervised and still manages
        its open position.
        """
        return {
            "profiles": [
                {
                    "profile_id": profile_id,
                    "paused": self._paused(profile_id),
                    "running": profile_id in self._started_ids,
                }
                for profile_id in self.profile_ids()
            ]
        }

    def candle_series(self, profile_id: str, limit: int) -> list[CandleRow]:
        """Return the persisted candles of a profile, oldest first.

        Part of the read model the monitoring layer consumes: the orchestrator is
        therefore a valid :class:`~trading_platform.web.routes.SnapshotProvider` for
        the candles route in both an engine run and a read-only ``realtime serve``.
        """
        self._ensure_store()
        return self._store.candle_series(profile_id, limit)

    async def pause_profile(self, profile_id: str) -> ProfileSnapshot:
        """Close the entry gate of one running profile, durably.

        The profile keeps its stream, its runner and its supervision: it stops
        opening **new** positions, while the static stop of an open position and
        every exit signal keep being evaluated, so nothing is ever left unmanaged.
        The flag is persisted before returning, so a process restart resumes paused.

        Raises
        ------
        ProfileError
            The profile is unknown, or it is not running right now.
        """
        resolved = str(profile_id)
        if resolved not in self._by_id:
            raise ProfileError(f"unknown profile: {profile_id!r}")
        runner = self._runners.get(resolved)
        if runner is None:
            raise ProfileError(f"profile {profile_id!r} is not running")
        self._ensure_store()
        runner.pause()
        self._store.set_meta(_PAUSED_META_PREFIX + resolved, "1")
        log_event(_LOGGER, "profile_pause_requested", profile_id=resolved)
        return self._require_snapshot(profile_id)

    async def resume_profile(self, profile_id: str) -> ProfileSnapshot:
        """Re-open the entry gate of one running profile, durably.

        The mirror of :meth:`pause_profile`, including the persisted flag.

        Raises
        ------
        ProfileError
            The profile is unknown, or it is not running right now.
        """
        resolved = str(profile_id)
        if resolved not in self._by_id:
            raise ProfileError(f"unknown profile: {profile_id!r}")
        runner = self._runners.get(resolved)
        if runner is None:
            raise ProfileError(f"profile {profile_id!r} is not running")
        self._ensure_store()
        runner.resume()
        self._store.set_meta(_PAUSED_META_PREFIX + resolved, "0")
        log_event(_LOGGER, "profile_resume_requested", profile_id=resolved)
        return self._require_snapshot(profile_id)

    async def delete_profile(self, profile_id: str) -> str:
        """Flatten, stop and forget one profile; return the removed identifier.

        The order of the operations is the safety property of this method, not an
        implementation detail:

        1. the profile must exist;
        2. the open exposure is closed **through the execution gateway**, before
           anything is removed: every working order is cancelled and the open
           position is flattened with one market order.  A failure at this step
           aborts the deletion and changes nothing at all -- a profile is never
           removed while it is still exposed;
        3. the profile row is deleted from the state store, so a write failure aborts
           with the store and the running engine still consistent;
        4. only then is the profile stopped (supervise task, runner and stream) and
           forgotten by the registry, the store flag included.

        Deleting the **last** profile is allowed.  The profile set lives in the
        SQLite state store, which is the single source of truth, so an empty
        platform is a legal platform: the API keeps serving and
        ``POST /api/profiles`` re-creates a profile without a restart.  The
        flatten-before-remove guarantee is unchanged -- it is still step 2, and a
        position that cannot be flattened still aborts the whole call and changes
        nothing.  What replaces the old "cannot delete the last profile" guard is
        the startup orphan sweep: a profile that disappears **without** going
        through this method (a manual row removal, a restored older database)
        leaves an orphaned position, and the sweep closes it at the venue on the
        next boot.

        Parameters
        ----------
        profile_id:
            Profile to delete.

        Raises
        ------
        ProfileError
            Unknown profile, or a position that could not be flattened.  In both
            cases nothing else was modified.
        StateStoreError
            The profile row could not be removed; nothing was forgotten then.
        """
        resolved = str(profile_id)
        profile = self._by_id.get(resolved)
        if profile is None:
            raise ProfileError(f"unknown profile: {profile_id!r}")
        runner = self._runners.get(resolved)
        if runner is not None:
            self._flatten_profile(profile, runner)
        self._ensure_store()
        self._store.delete_profile(resolved)
        await self._stop_profile(resolved)
        self._runners.pop(resolved, None)
        self._streams.pop(resolved, None)
        self._reports.pop(resolved, None)
        self._started_ids.discard(resolved)
        self._profiles = tuple(item for item in self._profiles if str(item.id) != resolved)
        self._by_id.pop(resolved, None)
        platform_state = self._platform_state
        if platform_state is not None:
            # A deleted profile must stop contributing to the platform-wide caps:
            # its last exposure would otherwise cap the platform for ever.
            platform_state.forget(resolved)
        self._store.set_meta(_PAUSED_META_PREFIX + resolved, "0")
        log_event(_LOGGER, "profile_deleted", profile_id=resolved, profiles=len(self._profiles))
        return resolved

    async def add_profile(self, profile: ProfileConfig) -> ProfileSnapshot:
        """Persist, build and start one new profile; return its snapshot.

        The store write happens **before** the in-memory registry is touched, so a
        failed write leaves the platform exactly as it was.  The new profile is then
        built by the regular wiring (:meth:`_build_runner`, reconciliation included)
        and started, which is what makes it appear on the dashboard without a
        restart.

        Parameters
        ----------
        profile:
            The definition to add; its identifier must be free.

        Raises
        ------
        ProfileError
            The platform is not running (no prepared wiring, no stream factory), or
            the identifier is already registered.
        StateStoreError
            The profile row could not be written; nothing was registered then.
        """
        resolved = str(profile.id)
        factory = self._stream_factory
        if not self._prepared or factory is None:
            raise ProfileError("the engine is not running")
        if resolved in self._by_id:
            raise ProfileError(f"profile already exists: {profile.id!r}")
        self._ensure_store()
        self._store.save_profile(profile)
        self._by_id[resolved] = profile
        self._profiles = (*self._profiles, profile)
        self._build_runner(profile, factory)
        # A profile that cannot be started is still added, persisted and reported by
        # ``profile_failures()``: it simply gets no supervised task, because there is
        # nothing to supervise.  Building the task unconditionally would index a
        # runner that ran but never started, and hide the failure behind a KeyError.
        if await self._start_runner(resolved):
            self._tasks[resolved] = asyncio.create_task(self._supervise(self._runners[resolved]))
        log_event(_LOGGER, "profile_added", profile_id=resolved, profiles=len(self._profiles))
        return self._require_snapshot(profile.id)

    # -- wiring -------------------------------------------------------------

    def _record_profile_failure(self, profile_id: str, exc: BaseException) -> str:
        """Quarantine one profile and return the sanitised reason.

        This is the single place a profile is declared unstartable, and it is
        deliberately *total*: the failure is remembered for
        :meth:`profile_failures`, the profile is marked ``ERROR`` so
        :meth:`health` degrades, and the event ``profile_boot_failed`` is logged --
        none of which may itself take the platform down.  Persisting the status is
        best-effort: a store that refuses the write is logged as
        ``profile_boot_failure_not_persisted`` and the platform keeps booting,
        because a monitoring write must never be the reason an engine stays down.

        The reason is rendered by the store's :func:`_redact_profile_error`, so a
        profile row an operator hand-wrote a credential into can never echo it into
        the log stream or into the monitoring payload.
        """
        resolved = str(profile_id)
        reason = _redact_profile_error(exc)
        self._boot_failures[resolved] = reason
        # A runner that was already built owns the profile's read model: without
        # this the dashboard would keep reporting whatever status the runner had
        # before its start failed (``STOPPED``), and `health()` would answer "ok"
        # about a profile that is not trading.  ``mark_crashed`` is the existing
        # "this profile ended on an error" path of the runner: it sets ERROR in
        # memory *and* persists it.  It renders and logs what it is given, so it is
        # handed the sanitised failure -- see :func:`_sanitised_failure`.
        runner = self._runners.get(resolved)
        if runner is not None:
            try:
                runner.mark_crashed(_sanitised_failure(exc, reason))
            except RealtimeError as persist_exc:
                log_event(
                    _LOGGER,
                    "profile_boot_failure_not_persisted",
                    level=logging.ERROR,
                    profile_id=resolved,
                    error=_redact_profile_error(persist_exc),
                )
        try:
            self._store.save_status(resolved, ProfileStatus.ERROR, detail=reason)
        except RealtimeError as persist_exc:
            log_event(
                _LOGGER,
                "profile_boot_failure_not_persisted",
                level=logging.ERROR,
                profile_id=resolved,
                error=_redact_profile_error(persist_exc),
            )
        log_event(
            _LOGGER,
            "profile_boot_failed",
            level=logging.WARNING,
            profile_id=resolved,
            error=reason,
        )
        return reason

    def _prepare_runners(self) -> list[str]:
        """Initialize the store, adopt the settings, sweep orphans, build the runners.

        The boot order is a safety property, not an implementation detail:

        1. the store is opened (it takes the single-writer lock), because it is the
           source of truth for both the profiles and the settings;
        2. the engine settings are read from the store **once** (see
           :meth:`_load_settings_into_configs`), so nothing downstream ever reads
           the bootstrap placeholder;
        3. every loaded profile is persisted, which is what makes the registry that
           the monitor and the API answer from durable;
        4. the shared wallet is restored **exactly once** from the durable state, so
           a restart never resets the platform's cash;
        5. the orphan sweep runs (see :meth:`_sweep_orphaned_positions`): at this
           point the full durable position set and the full loaded profile set are
           both visible, and **no runner exists yet**, so nothing can be trading
           while the sweep closes an orphaned position at the venue;
        6. only then is every enabled runner built and reconciled, and a live
           platform mirrors the venue's balance once, best-effort (see
           :meth:`_sync_live_wallet`).

        Zero profiles is a legal platform: every step above still runs and the
        method answers ``[]``, which leaves ``_prepared`` set so that
        :meth:`add_profile` can wire a profile created through the API without a
        restart.

        A profile that cannot be **built** (a venue refusing a credential, a stream
        factory rejecting a symbol) is quarantined by
        :meth:`_record_profile_failure` instead of aborting the loop: the remaining
        profiles are still built, the orphan sweep and the wallet steps are
        untouched, and ``_prepared`` is still set, so the platform serves its API
        with the profiles it *can* run.  The quarantined id is not in the returned
        list, so no runner is started for it.
        """
        if self._prepared:
            return self._runnable_ids()
        factory = self._stream_factory
        if factory is None:
            raise MarketStreamError("no market stream factory: inject one")
        self._ensure_store()
        self._load_settings_into_configs()
        for profile in self._profiles:
            self._store.save_profile(profile)
            # --- ORPHAN SWEEP WIRING (WP3) -----------------------------
            # The exchange of every loaded profile is durable *before* any
            # sweep can need it: an orphan has no ProfileConfig left, so this
            # ``meta`` entry is the only way to learn which venue it was
            # trading on.
            self._store.set_meta("profile_exchange:" + str(profile.id), str(profile.exchange))
            # --- END ORPHAN SWEEP WIRING (WP3) -------------------------
        self._ensure_wallet_restored()
        self._sweep_orphaned_positions()
        for profile_id in self._enabled_ids():
            profile = self._by_id[profile_id]
            try:
                self._build_runner(profile, factory)
            except _PROFILE_BOOT_ERRORS as exc:
                # One profile is not the platform: quarantine it and keep booting.
                self._record_profile_failure(profile_id, exc)
        self._sync_live_wallet()
        self._prepared = True
        return self._runnable_ids()

    def _runnable_ids(self) -> list[str]:
        """Return the enabled profiles that actually got a runner built.

        A quarantined profile (see :meth:`_record_profile_failure`) has no runner
        and therefore no entry in :attr:`_runners`; leaving it out of the boot list
        is what keeps :meth:`start` and :meth:`run_once` from indexing a runner that
        does not exist, while the profile stays visible in the read model as
        ``ERROR``.
        """
        return [profile_id for profile_id in self._enabled_ids() if profile_id in self._runners]

    def _load_settings_into_configs(self) -> None:
        """Adopt the settings the state store holds, exactly once per orchestrator.

        The SQLite state store is the single source of truth for the engine
        settings: whatever the caller injected is a **bootstrap** -- the minimal
        surface (``state_db``, ``logs_dir``) that had to be known before the store
        could be opened -- and this method replaces it with what the store
        remembers, seeding it from the injected values on the very first boot.

        The flag makes "once" exact: the settings are read before the first profile
        is persisted and before any runner exists, so no component ever observes a
        half-adopted configuration.
        """
        if self._settings_loaded:
            return
        self._settings_loaded = True
        from trading_platform.realtime.settings import (
            PlatformSettings,
            settings_from_store,
        )

        bootstrap = self._settings
        if bootstrap is None:
            bootstrap = PlatformSettings(
                realtime=self._realtime,
                monitoring=(
                    self._monitoring if self._monitoring is not None else MonitoringConfig()
                ),
            )
        resolved = settings_from_store(self._store, bootstrap=bootstrap)
        self._settings = resolved
        self._realtime = resolved.realtime
        self._monitoring = resolved.monitoring
        log_event(
            _LOGGER,
            "platform_settings_loaded",
            state_db=str(self._realtime.state_db),
            source="store",
        )

    # --- ORPHAN SWEEP WIRING (WP3) -----------------------------------------

    def _sweep_orphaned_positions(self) -> None:
        """Flatten every durable position whose profile is no longer loaded.

        This is the boot step that closes the gap left by a profile that
        disappears **without** going through :meth:`delete_profile`: such a
        position is tracked by nobody -- no stop loss, no exit management, no
        reconciliation -- so it is closed at the venue here, before any runner
        exists.

        The sweep is called exactly once, at the point where the full durable
        position set and the full loaded profile set are both visible, and it is
        given the same factory and the same environment the engine uses, so a
        ``paper`` orphan closes on the simulated venue and a ``live`` orphan
        closes through the real one.

        A sweep that cannot even read the store must never stop the boot:
        :class:`~trading_platform.core.errors.RealtimeError` is caught, logged as
        ``orphan_sweep_failed`` at ``ERROR`` and turned into an empty report.  The
        sweep itself never raises for an individual position either -- that is
        carried by the entries of the report.
        """
        try:
            report = sweep_orphaned_positions(
                store=self._store,
                profiles=self._profiles,
                clock=self._clock,
                broker_factory=self._broker_factory,
                environ=self._environ,
            )
        except RealtimeError as exc:
            log_event(
                _LOGGER,
                "orphan_sweep_failed",
                level=logging.ERROR,
                error=str(exc),
            )
            report = OrphanSweepReport.empty(swept_at=pd.Timestamp(self._clock.now()).isoformat())
        self._orphan_report = report

    # --- END ORPHAN SWEEP WIRING (WP3) -------------------------------------

    def _ensure_wallet_restored(self) -> None:
        """Adopt the persisted ledger, exactly once, before anything reports it.

        :meth:`_prepare_runners` calls this at boot; the read model calls it too,
        because a process that only *reads* the platform -- the monitoring API of
        ``realtime run`` answers before the engine loop has booted, and a read-only
        composition never boots at all -- must report the **durable** cash.  Serving
        the configured initial balance while the store holds a different row would
        be a lie about the platform's money.

        The call is idempotent (a lock and a flag make "once" exact across the
        engine thread and the API thread) and best-effort: a store that cannot be
        read logs ``platform_wallet_restore_failed`` and leaves the configured
        balance in place, and the restore is retried on the next boot.  A store that
        has not been initialized yet is left to its owner: initializing it here
        would race the boot that opens it.
        """
        with self._wallet_lock:
            if self._wallet_restored or not self._store_ready():
                return
            wallet = self.wallet
            try:
                restored_cash = wallet.restore()
            except RealtimeError as exc:
                log_event(
                    _LOGGER,
                    "platform_wallet_restore_failed",
                    level=logging.WARNING,
                    error=str(exc),
                )
                return
            self._wallet_restored = True
            self._restored_cash = restored_cash
        log_event(
            _LOGGER,
            "platform_wallet_restored",
            cash=float(wallet.cash),
            restored=restored_cash is not None,
            mode=wallet.mode.value,
        )

    def _build_runner(self, profile: ProfileConfig, factory: StreamFactory) -> None:
        """Build the gateway and the runner of one profile, then reconcile it."""
        from trading_platform.realtime.gateway import ExecutionGateway
        from trading_platform.realtime.risk import PlatformRiskLimits
        from trading_platform.realtime.runner import ProfileRunner

        profile_id = str(profile.id)
        broker_factory = self._broker_factory
        broker = (
            default_broker_factory(
                profile, clock=self._clock, environ=self._environ, wallet=self.wallet
            )
            if broker_factory is None
            else broker_factory(profile)
        )
        gateway = ExecutionGateway(
            profile=profile,
            broker=broker,
            store=self._store,
            clock=self._clock,
            risk=make_risk_manager(
                profile,
                clock=self._clock,
                kill_switch=self._kill_switch_instance(),
                platform=PlatformRiskLimits.from_config(self._realtime),
                wallet=self.wallet,
                state=self._risk_state(),
            ),
            live_gate=make_live_gate(self._environ),
        )
        runner = ProfileRunner(
            profile=profile,
            stream=factory(profile),
            gateway=gateway,
            store=self._store,
            clock=self._clock,
            timeout_seconds=float(self._realtime.stream_poll_timeout_seconds),
        )
        report = gateway.reconcile()
        self._reports[profile_id] = bool(report.ok)
        if not report.ok:
            detail = json.dumps(report.to_dict(), sort_keys=True, default=str)
            runner.mark_degraded(detail)
            log_event(
                _LOGGER,
                "reconciliation_mismatch",
                level=logging.WARNING,
                profile_id=profile_id,
                symbol=str(profile.symbol),
                matched=int(report.matched),
                only_at_venue=list(report.only_at_venue),
                only_locally=list(report.only_locally),
                mismatched=list(report.mismatched),
            )
        self._streams[profile_id] = runner.stream
        self._runners[profile_id] = runner
        if self._store.get_meta(_PAUSED_META_PREFIX + profile_id) == "1":
            # The operator paused this profile before the restart: the entry gate is
            # closed again here, before the first tick, so a restart can never open a
            # position the operator asked not to open.
            runner.pause()

    def _sync_live_wallet(self) -> None:
        """Mirror the venue account into the shared wallet once, best-effort.

        In live mode the wallet **is** the venue's account: the local ledger is
        never debited, and the only honest thing the boot can do is ask the venue
        what it holds.  The balance of the first enabled live profile answers for
        the whole platform, exactly like the mode itself does.  The call is
        best-effort by contract: any :class:`~trading_platform.core.errors.RealtimeError`
        (an unreachable venue, a missing credential) is logged and the platform
        starts anyway -- the wallet then falls back to its configured initial
        balance rather than inventing a ``0.0``.

        A paper platform has nothing to mirror and returns immediately, so no test
        and no simulated run ever reaches a venue here.
        """
        wallet = self.wallet
        if wallet.is_authoritative:
            return
        for profile_id in self._enabled_ids():
            profile = self._by_id[profile_id]
            runner = self._runners.get(profile_id)
            if runner is None or RunMode(profile.mode) is not RunMode.LIVE:
                continue
            try:
                balance = runner.gateway.balance()
                if balance is not None:
                    wallet.sync_from_venue(balance, at=pd.Timestamp(self._clock.now()))
            except RealtimeError as exc:
                log_event(
                    _LOGGER,
                    "platform_wallet_venue_sync_failed",
                    level=logging.WARNING,
                    profile_id=profile_id,
                    error=str(exc),
                )
            return

    def _has_enabled_live_profile(self) -> bool:
        """Return whether any enabled profile runs against a real venue."""
        return any(
            str(profile.mode) == RunMode.LIVE.value for profile in self._profiles if profile.enabled
        )

    def _risk_state(self) -> PlatformRiskState:
        """Return the platform-wide aggregation of the profiles' figures (one per run).

        It is built once per orchestrator and injected into every risk manager of
        the platform, so the platform caps read the *same* aggregate whatever
        profile is asking.
        """
        if self._platform_state is None:
            from trading_platform.realtime.risk import PlatformRiskState

            self._platform_state = PlatformRiskState(clock=self._clock)
        return self._platform_state

    async def _start_runner(self, profile_id: str) -> bool:
        """Start the stream and the runner of one profile, exactly once.

        The orchestrator owns the streams (it built them through the factory), so it
        is the one that starts and stops them; the runner only reads them.

        Returns
        -------
        bool
            ``True`` when the profile is started (or was already started) and
            ``False`` when it could not be: the failure is then quarantined by
            :meth:`_record_profile_failure` and the caller simply skips the profile.
            Starting a profile is the point where the strategy is resolved (see
            :meth:`~trading_platform.realtime.runner.ProfileRunner.start`), so this is
            where a profile whose ``strategy`` left the registry used to take the
            whole platform down with it.

        Only :data:`_PROFILE_BOOT_ERRORS` -- a realtime failure or a broken strategy
        configuration -- is quarantined.  :class:`asyncio.CancelledError` propagates
        (a shutdown is not a profile fault) and so does any other exception, because
        a programming bug must stay loud rather than become a silently degraded
        profile.
        """
        if profile_id in self._started_ids:
            return True
        try:
            await asyncio.wait_for(
                self._streams[profile_id].start(),
                timeout=float(self._realtime.stream_poll_timeout_seconds),
            )
            await self._runners[profile_id].start()
        except _PROFILE_BOOT_ERRORS as exc:
            self._record_profile_failure(profile_id, exc)
            return False
        self._started_ids.add(profile_id)
        return True

    async def _supervise(self, runner: ProfileRunner) -> None:
        """Keep one profile's failure from taking the platform down with it."""
        failure: BaseException | None = None
        try:
            await runner.run()
        except asyncio.CancelledError:
            raise
        except RealtimeError as exc:
            failure = exc
            log_event(
                _LOGGER,
                "profile_stopped_on_error",
                level=logging.ERROR,
                profile_id=runner.profile_id,
                error=str(exc),
            )
        except Exception as exc:
            failure = exc
            log_event(
                _LOGGER,
                "profile_crashed",
                level=logging.ERROR,
                profile_id=runner.profile_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        if failure is not None:
            # A profile that ended on an error must say so on the dashboard, not
            # only in the log stream: the persisted status is what survives the
            # process.  ``ProfileRunner.run`` records its own tick failures, so this
            # is the last-resort path (a failure outside the tick loop).
            runner.mark_crashed(failure)

    async def _stop_streams(self) -> None:
        """Best-effort shutdown of the injected streams (bounded, never fatal)."""
        streams = list(self._streams.values())
        if not streams:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*(stream.stop() for stream in streams), return_exceptions=True),
                timeout=float(self._realtime.stream_poll_timeout_seconds),
            )
        except TimeoutError:  # pragma: no cover - a stream refusing to stop
            log_event(_LOGGER, "stream_shutdown_timeout", level=logging.ERROR)

    # -- internals ----------------------------------------------------------

    def _require_snapshot(self, profile_id: str) -> ProfileSnapshot:
        """Return the snapshot of a profile the caller already resolved."""
        snapshot = self.profile_snapshot(profile_id)
        if snapshot is None:  # pragma: no cover - defensive: the id was resolved
            raise ProfileError(f"unknown profile: {profile_id!r}")
        return snapshot

    def _paused(self, profile_id: str) -> bool:
        """Return whether the entry gate of a profile is closed.

        A running profile answers from its runner (the live truth); a profile with
        no runner answers from the persisted flag, so the dashboard shows the same
        state before and after a restart.
        """
        runner = self._runners.get(profile_id)
        if runner is not None:
            return bool(runner.paused)
        try:
            return self._store.get_meta(_PAUSED_META_PREFIX + profile_id) == "1"
        except RealtimeError:  # a store that was never opened cannot answer
            return False

    @staticmethod
    def _declared_ids(profile_ids: Sequence[str]) -> set[str]:
        """Return the identifiers a sequence of profile ids declares, as a set."""
        return {str(item) for item in profile_ids}

    def _flatten_profile(self, profile: ProfileConfig, runner: ProfileRunner) -> None:
        """Cancel the working orders and flatten the open position, before any removal.

        The flattening goes through the *existing* execution gateway, so it obeys the
        same risk manager, the same live gate and the same idempotency keys as every
        other order of the profile.  A profile whose exposure could not be closed is
        never deleted: the caller gets a
        :class:`~trading_platform.core.errors.ProfileError` and nothing was changed.
        """
        from trading_platform.realtime.gateway import FLAT_EPSILON

        profile_id = str(profile.id)
        gateway = runner.gateway
        for order in gateway.open_orders():
            gateway.cancel(order.client_order_id)
        position = gateway.position()
        if position is not None and abs(float(position.quantity)) > FLAT_EPSILON:
            self._submit_flatten(profile, runner, position)
            gateway.poll()
            trade = gateway.closed_trade()
            if trade is not None:
                self._store.append_trade(trade, profile_id=profile_id)
        remaining = gateway.position()
        if remaining is not None and abs(float(remaining.quantity)) > FLAT_EPSILON:
            raise ProfileError(
                f"cannot delete profile {profile_id!r}: the open position could not be flattened"
            )

    def _submit_flatten(
        self, profile: ProfileConfig, runner: ProfileRunner, position: Position
    ) -> None:
        """Route the single market order that closes the whole open position."""
        profile_id = str(profile.id)
        gateway = runner.gateway
        fields = gateway.snapshot_fields()
        stamp = pd.Timestamp(self._clock.now())
        equity = float(fields.get("equity", 0.0))
        signed = float(position.quantity)
        mark = 0.0 if not signed else float(fields.get("position_value", 0.0)) / signed
        reference_price = float(position.average_price) if mark <= 0.0 else mark
        request = OrderRequest(
            profile_id=profile_id,
            client_order_id=new_client_order_id(
                profile_id, str(profile.symbol), stamp, _DELETE_ORDER_SEQUENCE
            ),
            symbol=str(profile.symbol),
            side=(
                OrderSide.SELL if Direction(position.direction) is Direction.LONG else OrderSide.BUY
            ),
            type=OrderType.MARKET,
            quantity=abs(signed),
            price=None,
            stop_price=None,
            mode=RunMode(profile.mode),
            reason="profile deleted",
            created_at=stamp,
        )
        try:
            gateway.submit(
                request,
                reference_price=reference_price,
                equity=equity,
                open_positions=int(fields.get("open_positions", 0)),
                position_notional=abs(float(fields.get("position_value", 0.0))),
                daily_pnl=float(fields.get("daily_pnl", 0.0)),
                daily_trades=int(fields.get("daily_trades", 0)),
                peak_equity=float(fields.get("peak_equity", equity)),
                closes_position=True,
            )
        except (
            BrokerError,
            RiskLimitExceededError,
            KillSwitchActiveError,
            OrderRejectedError,
            GatewayError,
            WalletError,
        ) as exc:
            raise ProfileError(
                f"cannot delete profile {profile_id!r}: the open position could not be flattened"
            ) from exc

    async def _stop_profile(self, profile_id: str) -> None:
        """Cancel the supervise task of one profile, then stop its runner and stream.

        The shutdown of the stream is best-effort and bounded, exactly like the
        platform-wide :meth:`_stop_streams`: a stream that refuses to stop must not
        abort a deletion whose configuration file was already rewritten.
        """
        timeout = float(self._realtime.stream_poll_timeout_seconds)
        task = self._tasks.pop(profile_id, None)
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True), timeout=timeout
                )
            except TimeoutError:  # pragma: no cover - a task refusing to stop
                log_event(
                    _LOGGER,
                    "profile_shutdown_timeout",
                    level=logging.ERROR,
                    profile_id=profile_id,
                    timeout=timeout,
                )
        runner = self._runners.get(profile_id)
        if runner is not None:
            await runner.stop()
        stream = self._streams.get(profile_id)
        if stream is not None:
            try:
                await asyncio.wait_for(stream.stop(), timeout=timeout)
            except TimeoutError:  # pragma: no cover - a stream refusing to stop
                log_event(
                    _LOGGER,
                    "profile_stream_shutdown_timeout",
                    level=logging.ERROR,
                    profile_id=profile_id,
                    timeout=timeout,
                )

    def _enabled_ids(self) -> list[str]:
        """Return the ids of the enabled profiles, sorted."""
        return sorted(str(profile.id) for profile in self._profiles if profile.enabled)

    def _ensure_store(self) -> None:
        """Initialize the store when it can report that it is not initialized yet.

        The probe accepts both shapes of the attribute a store may expose (a
        property and a method), because the protocol itself only requires
        ``initialize``/``close``: a store that cannot answer is assumed to be ready.
        """
        probe = getattr(self._store, "is_initialized", None)
        if probe is None:
            return
        initialized = probe() if callable(probe) else probe
        if not initialized:
            self._store.initialize()

    def _store_ready(self) -> bool:
        """Return whether the store is usable, **without** opening it.

        It accepts both shapes of the attribute a store may expose (a property and
        a method) and assumes a store that cannot answer is ready, exactly like
        :meth:`_ensure_store`.  A closed store reports ``False``: it must never be
        reopened behind its owner's back by a read.
        """
        probe = getattr(self._store, "is_initialized", None)
        if probe is None:
            return True
        return bool(probe() if callable(probe) else probe)

    def _kill_switch_instance(self) -> KillSwitch:
        """Build the global kill switch lazily, over the shared store."""
        if self._kill_switch is None:
            from trading_platform.realtime.risk import KillSwitch

            self._ensure_store()
            self._kill_switch = KillSwitch(
                self._store,
                clock=self._clock,
                flag_path=self._realtime.kill_switch_file,
                environ=self._environ,
            )
        return self._kill_switch

    def _uptime(self) -> float:
        """Return the seconds elapsed since the platform started (never negative)."""
        if self._monotonic_start is None:
            return 0.0
        return max(0.0, float(self._clock.monotonic()) - self._monotonic_start)

    def _persisted(
        self, profile_id: str
    ) -> tuple[ProfileState, list[EquityPoint], list[TradeRecord], list[Position]]:
        """Read the persisted state of a profile that has no live runner.

        A store that is not readable (never opened, closed, or damaged) must not
        make the dashboard fail: the profile is then reported as stopped, without
        inventing a position or a trade.
        """
        fallback = ProfileState(
            profile_id=profile_id,
            status=ProfileStatus.STOPPED,
            mode=RunMode(self._by_id[profile_id].mode),
            updated_at=None,
        )
        try:
            state = self._store.profile_state(profile_id)
            curve = self._store.equity_curve(profile_id)
            trades = self._store.list_trades(profile_id)
            positions = self._store.list_positions(profile_id)
        except RealtimeError:
            log_event(
                _LOGGER,
                "profile_read_model_unavailable",
                level=logging.WARNING,
                profile_id=profile_id,
            )
            return fallback, [], [], []
        return state, curve, trades, positions

    def _fallback_snapshot(self, profile: ProfileConfig) -> ProfileSnapshot:
        """Build the attributed snapshot of a profile that is not running.

        Every figure is rebuilt from what the store proves -- the position's entry
        value, the closed round trips, the last equity point -- so the payload is
        the *attributed* view of the shared wallet, exactly like a running profile
        reports it.  Nothing is invented: without a curve the position value is
        ``0.0`` and without a position nothing is deployed.
        """
        profile_id = str(profile.id)
        state, curve, trades, positions = self._persisted(profile_id)
        allocation = float(profile.effective_allocation)
        initial = float(profile.initial_balance)
        point = curve[-1] if curve else None
        position_value = 0.0 if point is None else float(point.position_value)
        deployed = sum(
            abs(float(position.quantity) * float(position.average_price)) for position in positions
        )
        realized_pnl = sum(float(trade.pnl) for trade in trades)
        unrealized_pnl = position_value - deployed
        cash = allocation - deployed + realized_pnl
        equity = allocation + realized_pnl + unrealized_pnl
        return ProfileSnapshot(
            profile_id=profile_id,
            symbol=str(profile.symbol),
            timeframe=str(profile.timeframe),
            strategy=str(profile.strategy),
            mode=RunMode(profile.mode),
            status=state.status,
            initial_balance=initial,
            equity=equity,
            cash=cash,
            position_value=position_value,
            total_return=(equity - allocation) / allocation if allocation else 0.0,
            n_trades=len(trades),
            open_positions=len(positions),
            health=self._fallback_health(profile_id, state),
            started_at=self._started_at,
            updated_at=state.updated_at,
            allocation=allocation,
            deployed=deployed,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            last_block_reason=None,
        )

    def _fallback_health(self, profile_id: str, state: ProfileState) -> ProfileHealth:
        """Build the health block of a profile that has no live runner."""
        runner = self._runners.get(profile_id)
        return ProfileHealth(
            profile_id=profile_id,
            status=state.status,
            last_candle_at=state.last_candle_at,
            lag_seconds=float(state.lag_seconds),
            last_error=state.last_error,
            reconnect_count=int(state.reconnect_count),
            counters=EngineCounters() if runner is None else runner.counters(),
        )
