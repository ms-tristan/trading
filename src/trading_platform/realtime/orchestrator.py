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
exposed.  Both are persisted (the pause flag in the ``meta`` table, the profile list
in the profiles document) and survive a restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from trading_platform.config.loader import load_profiles, save_profiles
from trading_platform.config.models import MonitoringConfig, ProfileConfig, RealtimeConfig
from trading_platform.core.errors import (
    BrokerError,
    ConfigError,
    GatewayError,
    KillSwitchActiveError,
    MarketStreamError,
    OrderRejectedError,
    ProfileError,
    RealtimeError,
    RiskLimitExceededError,
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

if TYPE_CHECKING:
    from trading_platform.realtime.broker import Broker
    from trading_platform.realtime.risk import (
        KillSwitch,
        KillSwitchState,
        LiveTradingGate,
        RiskManager,
    )
    from trading_platform.realtime.runner import ProfileRunner
    from trading_platform.realtime.store import CandleRow, StateStore
    from trading_platform.realtime.stream import MarketStream

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

#: Prefix of the ``meta`` key holding the persisted pause flag of one profile.
#:
#: The flag lives in the existing free-form ``meta`` table (values ``"1"`` for
#: paused and ``"0"`` for running), so pausing a profile needs **no schema change**
#: and survives a process restart: :meth:`RealtimeOrchestrator._build_runner` reads
#: it back and re-pauses the runner it just built.
_PAUSED_META_PREFIX = "paused:"

#: Sequence number of the market order that flattens a profile being deleted.
#:
#: The client order id of that order is built from the deletion instant and this
#: sequence, so a delete replayed after a restart produces the same identifier and
#: the gateway answers it idempotently instead of sending a second order.
_DELETE_ORDER_SEQUENCE = 0


def make_risk_manager(
    profile: ProfileConfig, *, clock: Clock, kill_switch: KillSwitch | None = None
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
    """
    from trading_platform.realtime.risk import RiskLimits, RiskManager

    return RiskManager(RiskLimits.from_config(profile.risk), clock=clock, kill_switch=kill_switch)


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
    )


class RealtimeOrchestrator:
    """Run N independently configured profiles over one shared state store.

    Parameters
    ----------
    profiles:
        The profiles to run.  An empty sequence or a duplicated id raises
        :class:`~trading_platform.core.errors.ProfileError`: a platform that
        silently drops a profile is worse than one that refuses to start.
    store:
        Durable state, shared by every profile.
    clock:
        Time seam; no module of the layer reads the wall clock directly.
    realtime:
        Engine settings shared by the run (poll intervals, timeouts, state paths).
    monitoring:
        Optional HTTP surface settings; kept here so a caller has a single wiring
        object even though the server itself lives in the web layer.
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
    ) -> None:
        self._profiles: tuple[ProfileConfig, ...] = tuple(profiles)
        if not self._profiles:
            raise ProfileError("no profile to run: the profile list is empty")
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
        self._kill_switch: KillSwitch | None = None
        self._monotonic_start: float | None = None
        self._started_at: pd.Timestamp | None = None
        self._prepared = False

    # -- introspection ------------------------------------------------------

    @property
    def profiles(self) -> tuple[ProfileConfig, ...]:
        """Return every profile of the platform, in the order it was given."""
        return self._profiles

    @property
    def monitoring(self) -> MonitoringConfig | None:
        """Return the monitoring settings, when one was configured."""
        return self._monitoring

    @property
    def realtime(self) -> RealtimeConfig:
        """Return the engine settings shared by the run."""
        return self._realtime

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
        """Persist the profiles, reconcile them, and supervise one task each."""
        runner_ids = self._prepare_runners()
        if self._monotonic_start is None:
            self._monotonic_start = float(self._clock.monotonic())
            self._started_at = pd.Timestamp(self._clock.now())
        for profile_id in runner_ids:
            await self._start_runner(profile_id)
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
        """
        runner_ids = self._prepare_runners()
        if self._monotonic_start is None:
            self._monotonic_start = float(self._clock.monotonic())
            self._started_at = pd.Timestamp(self._clock.now())
        decisions = []
        for profile_id in runner_ids:
            await self._start_runner(profile_id)
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
        """Return the whole platform as the monitoring layer sees it."""
        profiles = tuple(self.profile_snapshot(profile_id) for profile_id in self.profile_ids())
        state = self.kill_switch_state()
        return PlatformSnapshot(
            profiles=tuple(item for item in profiles if item is not None),
            generated_at=pd.Timestamp(self._clock.now()),
            kill_switch=bool(state.engaged),
            kill_switch_reason=str(state.reason),
            kill_switch_changed_at=state.changed_at,
            version=self._version,
            started_at=self._started_at,
            uptime_seconds=self._uptime(),
        )

    def health(self) -> dict[str, Any]:
        """Return the ``/api/health`` body of the platform (no HTTP concern here)."""
        profiles = self.snapshot().profiles
        engaged = self.kill_switch_state().engaged
        degraded = engaged or any(item.status in _UNHEALTHY_STATUSES for item in profiles)
        return {
            "status": "degraded" if degraded else "ok",
            "version": self._version,
            "uptime_seconds": self._uptime(),
            "profiles_total": len(profiles),
            "profiles_running": sum(1 for item in profiles if item.status is ProfileStatus.RUNNING),
            "kill_switch": bool(engaged),
            "checked_at": self._clock.now().isoformat(),
        }

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

    async def delete_profile(self, profile_id: str, *, profiles_path: str | Path) -> str:
        """Flatten, stop and forget one profile; return the removed identifier.

        The order of the operations is the safety property of this method, not an
        implementation detail:

        1. the profile must exist, and it must not be the last one of the platform
           (an engine with no profile cannot run);
        2. the open exposure is closed **through the execution gateway**, before
           anything is removed: every working order is cancelled and the open
           position is flattened with one market order.  A failure at this step
           aborts the deletion and changes nothing at all -- a profile is never
           removed while it is still exposed;
        3. the profiles document is rewritten, so a write failure aborts with the
           file and the running engine still consistent;
        4. only then is the profile stopped (supervise task, runner and stream) and
           forgotten by the registry, the store flag included.

        Parameters
        ----------
        profile_id:
            Profile to delete.
        profiles_path:
            The profiles document -- the on-disk source of truth -- rewritten
            atomically without the deleted profile.

        Raises
        ------
        ProfileError
            Unknown profile, last profile of the platform, or a position that could
            not be flattened.  In every case nothing else was modified.
        ConfigError
            The profiles document could not be rewritten; nothing was removed then
            either.
        """
        resolved = str(profile_id)
        profile = self._by_id.get(resolved)
        if profile is None:
            raise ProfileError(f"unknown profile: {profile_id!r}")
        remaining = [item for item in self._profiles if str(item.id) != resolved]
        if not remaining:
            raise ProfileError(
                "cannot delete the last profile: the engine requires at least one profile"
            )
        runner = self._runners.get(resolved)
        if runner is not None:
            self._flatten_profile(profile, runner)
        await asyncio.to_thread(save_profiles, profiles_path, remaining)
        await self._stop_profile(resolved)
        self._runners.pop(resolved, None)
        self._streams.pop(resolved, None)
        self._reports.pop(resolved, None)
        self._started_ids.discard(resolved)
        self._profiles = tuple(item for item in self._profiles if str(item.id) != resolved)
        self._by_id.pop(resolved, None)
        self._store.set_meta(_PAUSED_META_PREFIX + resolved, "0")
        log_event(_LOGGER, "profile_deleted", profile_id=resolved, profiles=len(self._profiles))
        return resolved

    async def add_profile(
        self, profile: ProfileConfig, *, profiles_path: str | Path
    ) -> ProfileSnapshot:
        """Persist, build and start one new profile; return its snapshot.

        The profiles document is written **before** the in-memory registry is
        touched, so a failed write leaves the platform exactly as it was.  The new
        profile is then saved in the store, built by the regular wiring
        (:meth:`_build_runner`, reconciliation included) and started, which is what
        makes it appear on the dashboard without a restart.

        Parameters
        ----------
        profile:
            The definition to add; its identifier must be free.
        profiles_path:
            The profiles document rewritten atomically with the new profile.

        Raises
        ------
        ProfileError
            The platform is not running (no prepared wiring, no stream factory), or
            the identifier is already declared.
        """
        resolved = str(profile.id)
        factory = self._stream_factory
        if not self._prepared or factory is None:
            raise ProfileError("the engine is not running")
        if resolved in self._by_id or resolved in self._declared_ids(profiles_path):
            raise ProfileError(f"profile already exists: {profile.id!r}")
        self._ensure_store()
        await asyncio.to_thread(save_profiles, profiles_path, [*self._profiles, profile])
        self._store.save_profile(profile)
        self._by_id[resolved] = profile
        self._profiles = (*self._profiles, profile)
        self._build_runner(profile, factory)
        await self._start_runner(resolved)
        self._tasks[resolved] = asyncio.create_task(self._supervise(self._runners[resolved]))
        log_event(_LOGGER, "profile_added", profile_id=resolved, profiles=len(self._profiles))
        return self._require_snapshot(profile.id)

    # -- wiring -------------------------------------------------------------

    def _prepare_runners(self) -> list[str]:
        """Initialize the store, build every runner and reconcile it, once."""
        if self._prepared:
            return self._enabled_ids()
        factory = self._stream_factory
        if factory is None:
            raise MarketStreamError("no market stream factory: inject one")
        self._ensure_store()
        for profile in self._profiles:
            self._store.save_profile(profile)
        for profile_id in self._enabled_ids():
            profile = self._by_id[profile_id]
            self._build_runner(profile, factory)
        self._prepared = True
        return self._enabled_ids()

    def _build_runner(self, profile: ProfileConfig, factory: StreamFactory) -> None:
        """Build the gateway and the runner of one profile, then reconcile it."""
        from trading_platform.realtime.gateway import ExecutionGateway
        from trading_platform.realtime.runner import ProfileRunner

        profile_id = str(profile.id)
        broker_factory = self._broker_factory
        broker = (
            default_broker_factory(profile, clock=self._clock, environ=self._environ)
            if broker_factory is None
            else broker_factory(profile)
        )
        self._seed_paper_cash(profile, broker)
        gateway = ExecutionGateway(
            profile=profile,
            broker=broker,
            store=self._store,
            clock=self._clock,
            risk=make_risk_manager(
                profile, clock=self._clock, kill_switch=self._kill_switch_instance()
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

    def _seed_paper_cash(self, profile: ProfileConfig, broker: Broker) -> None:
        """Re-seed a simulated venue with the cash the durable state remembers (D7).

        A **simulated** venue has no memory of its own: after a restart its cash
        starts again at the configured initial balance while the position is
        restored from the store, so equity would jump by the position notional and
        the dashboard would display a number the persisted curve contradicts.  The
        last equity point of the profile *is* the durable truth (``cash`` column),
        so it is handed back to the venue here, before the first tick.

        Only a paper profile is seeded: a real venue reports its own balance
        (``CcxtBroker.fetch_balance``) and overwriting it would be a lie.  The seam
        is optional by contract, so an injected broker that does not expose
        ``restore_cash`` is left untouched.
        """
        if str(profile.mode) != RunMode.PAPER.value:
            return
        restore = getattr(broker, "restore_cash", None)
        if restore is None:
            return
        points = self._store.equity_curve(str(profile.id))
        if not points:
            return
        cash = float(points[-1].cash)
        restore(cash)
        log_event(
            _LOGGER,
            "paper_cash_restored",
            profile_id=str(profile.id),
            cash=cash,
            at=points[-1].timestamp.isoformat(),
        )

    async def _start_runner(self, profile_id: str) -> None:
        """Start the stream and the runner of one profile, exactly once.

        The orchestrator owns the streams (it built them through the factory), so it
        is the one that starts and stops them; the runner only reads them.
        """
        if profile_id in self._started_ids:
            return
        await asyncio.wait_for(
            self._streams[profile_id].start(),
            timeout=float(self._realtime.stream_poll_timeout_seconds),
        )
        await self._runners[profile_id].start()
        self._started_ids.add(profile_id)

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
    def _declared_ids(profiles_path: str | Path) -> set[str]:
        """Return the identifiers the profiles document declares (empty when unreadable).

        The file is the source of truth, so it is consulted before adding a profile:
        an identifier that a concurrent editor added on disk must be refused too.  An
        unreadable document is not reported here -- the write that follows owns that
        failure and reports it with the document in hand.
        """
        try:
            return {str(item.id) for item in load_profiles(profiles_path)}
        except ConfigError:
            return set()

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
    ) -> tuple[ProfileState, list[EquityPoint], list[TradeRecord], int]:
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
            open_positions = len(self._store.list_positions(profile_id))
        except RealtimeError:
            log_event(
                _LOGGER,
                "profile_read_model_unavailable",
                level=logging.WARNING,
                profile_id=profile_id,
            )
            return fallback, [], [], 0
        return state, curve, trades, open_positions

    def _fallback_snapshot(self, profile: ProfileConfig) -> ProfileSnapshot:
        """Build the snapshot of a profile that is not running (disabled, or late)."""
        profile_id = str(profile.id)
        state, curve, trades, open_positions = self._persisted(profile_id)
        initial = float(profile.initial_balance)
        point = curve[-1] if curve else None
        cash = initial if point is None else float(point.cash)
        position_value = 0.0 if point is None else float(point.position_value)
        equity = cash + position_value
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
            total_return=(equity - initial) / initial if initial else 0.0,
            n_trades=len(trades),
            open_positions=open_positions,
            health=self._fallback_health(profile_id, state),
            started_at=self._started_at,
            updated_at=state.updated_at,
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
