"""Fleet supervision: one Freqtrade worker per profile, and its process lifecycle.

The supervisor owns three things and nothing else:

* **the fleet** -- it decides which profiles run (the enabled profiles sorted by
  ``priority`` descending then ``id`` ascending), writes their generated
  configuration, spawns one ``freqtrade trade`` child per eligible profile and
  releases the worker when a profile stops or fails for good;
* **the journal** -- every decision is written to the ``events`` table of the
  state store with one of the documented engine event kinds, so the operations
  page can explain what happened without reading a log file;
* **the safety state** -- the kill-switch file and the live-trading gate are
  evaluated on every scheduling pass, so no worker ever starts behind the
  operator's back.

Spawning is deliberately non-blocking: :meth:`Supervisor.bootstrap` writes the
per-profile configuration, starts the children and returns, so the HTTP API is
servable within seconds of process start and never waits for a worker to become
healthy. Reading the workers happens in :meth:`Supervisor.refresh_once`, which
the snapshot poller (:mod:`trading_platform.engine.poller`) drives once per
interval; that is also where the health policy is applied and where the daily
and trade history of every answering worker is read, for the monitoring read
model.

Seven rules are worth stating because they are easy to get wrong:

* a worker the supervisor stopped itself (operator stop, kill switch, restart,
  shutdown) is **never** reported as a crash -- the process handle is dropped
  before the child is signalled, so the reaper cannot see it;
* a profile stopped **by the operator** (reason ``operator_stop``, or through
  :meth:`Supervisor.stop_profile`) is not restarted by the scheduler, while
  every other stopped profile stays eligible: a database full of ``stopped``
  rows is exactly what a fresh boot looks like, and the fleet has to start from
  it;
* an explicit :meth:`Supervisor.start_profile` / :meth:`Supervisor.restart_profile`
  is honoured exactly like an automatic start: an operator decision is never
  overridden by the scheduler;
* every eligible candidate starts in the same scheduling pass -- there is no
  fleet cap, no slot accounting and no gate that holds a profile back, so no
  profile is ever left waiting for a slot;
* the restart budget is a sliding window: at most
  :data:`MAX_RESTARTS_IN_WINDOW` restarts inside
  :data:`RESTART_WINDOW_SECONDS`, with the backoff saturating at 45 s. Once the
  budget is spent the profile goes to ``error``;
* a scheduled profile is restarted **only when it is proven gone**. A read
  failure first spends the startup grace
  (:data:`WORKER_STARTUP_GRACE_SECONDS`, the fair window a freshly spawned
  Freqtrade needs to boot and answer); then it must be counted
  :data:`UNHEALTHY_THRESHOLD` consecutive times; and even then the worker is
  restarted only if ``GET /ping`` has failed
  :data:`UNHEALTHY_PING_THRESHOLD` consecutive times or its process handle no
  longer exists. A worker that is merely slow -- ``/balance`` taking 12 to 20 s
  while 22 workers share one CoinGecko rate limit -- is healthy and is never
  restarted. Restarting a live worker is not a cheap decision: it wipes its
  in-memory state, and a fleet restarted in a loop never gets the chance to
  finish its warm-up;
* nothing that reaches a log line or an event ever carries the REST password of
  a worker or an exchange credential -- the generated configuration file is the
  only place a credential is written, and it is written with mode ``0o600``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from ..config import (
    ENV_LIVE_EXCHANGE_KEY,
    ENV_LIVE_EXCHANGE_SECRET,
    PLATFORM_CONFIG_FILENAME,
    PlatformSettings,
    live_trading_gate,
)
from ..models import (
    STATE_BLOCKED,
    STATE_ERROR,
    STATE_RUNNING,
    STATE_STOPPED,
    ProfileMetrics,
    ProfileRecord,
    ProfileSnapshot,
    format_ts,
    utc_now,
)
from ..paths import STRATEGIES_DIR, resolve_config_dir
from ..profiles.catalogue import (
    PROFILES_DOCUMENT,
    STRATEGIES_DOCUMENT,
    StrategyCatalogue,
    load_profile_catalogue,
    load_strategy_catalogue,
)
from ..profiles.store import StateStore
from .client import FreqtradeClient, FreqtradeClientError
from .config_builder import (
    allocate_api_port,
    build_freqtrade_argv,
    build_freqtrade_config,
    profile_config_path,
    profile_data_dir,
    profile_db_url,
    profile_log_path,
    profile_runtime_dir,
    resolve_freqtrade_binary,
    write_freqtrade_config,
)

__all__ = [
    "API_PASSWORD_BYTES",
    "CONTAINER_STRATEGIES_DIR",
    "EVENT_BLOCKED_LIVE",
    "EVENT_CATALOGUE_APPLIED",
    "EVENT_CRASH",
    "EVENT_ERROR",
    "EVENT_KILL_SWITCH",
    "EVENT_LEGACY_DB_ARCHIVED",
    "EVENT_PROFILE_CREATED",
    "EVENT_PROFILE_DELETED",
    "EVENT_PROFILE_UPDATED",
    "EVENT_RESTART",
    "EVENT_SETTINGS_UPDATED",
    "EVENT_START",
    "EVENT_STOP",
    "HISTORY_TRADE_LIMIT",
    "KILL_SWITCH_FILENAME",
    "MAX_RESTARTS_IN_WINDOW",
    "PINNED_STOP_REASONS",
    "REASON_DISABLED",
    "REASON_KILL_SWITCH",
    "REASON_OPERATOR_STOP",
    "REASON_RESTART_BACKOFF",
    "REASON_SHUTDOWN",
    "RESTART_BACKOFF_SECONDS",
    "RESTART_WINDOW_SECONDS",
    "SETTINGS_KEY",
    "TERMINATE_GRACE_SECONDS",
    "UNHEALTHY_PING_THRESHOLD",
    "UNHEALTHY_THRESHOLD",
    "WORKER_STARTUP_GRACE_SECONDS",
    "ProcessLauncher",
    "SubprocessLauncher",
    "Supervisor",
]

#: Consecutive failed REST reads that make a worker unhealthy.
UNHEALTHY_THRESHOLD = 10

#: Consecutive ``GET /ping`` failures that make a worker safe to declare dead.
#:
#: Reaching :data:`UNHEALTHY_THRESHOLD` failed reads only *starts* the death
#: check: ``/balance`` may be slow -- 12 to 20 s measured on the live
#: deployment -- while the worker is perfectly alive, so the worker is restarted
#: only once its own liveness endpoint has failed this many consecutive times.
UNHEALTHY_PING_THRESHOLD: int = 10

#: Fair startup window after a start, in seconds.
#:
#: Freqtrade needs tens of seconds to boot, download its warm-up window and
#: answer its API server, so no failure is counted inside this window: a worker
#: that has just been spawned is not judged on reads it could not answer yet.
WORKER_STARTUP_GRACE_SECONDS: float = 90.0

#: Backoff before a restart, indexed by the restart count and saturating on 45 s.
RESTART_BACKOFF_SECONDS = (5, 15, 45)

#: Length of the sliding restart budget window, in seconds.
RESTART_WINDOW_SECONDS = 900

#: Restarts allowed inside :data:`RESTART_WINDOW_SECONDS` before giving up.
MAX_RESTARTS_IN_WINDOW = 5

#: Seconds a child may take to honour SIGTERM before it is killed.
TERMINATE_GRACE_SECONDS = 15.0

#: Kill-switch file inside the state directory; its presence stops the fleet.
KILL_SWITCH_FILENAME = "KILL_SWITCH"

#: Settings row holding the persisted platform settings.
SETTINGS_KEY = "platform_settings"

#: Entropy of a generated worker REST password, in bytes.
API_PASSWORD_BYTES = 24

#: Closed trades read per profile and per poll cycle, for the trade read model.
HISTORY_TRADE_LIMIT = 50

#: Reason recorded on a profile the operator stopped.
REASON_OPERATOR_STOP = "operator_stop"

#: Reason recorded on a profile that is not enabled.
REASON_DISABLED = "disabled"

#: Reason recorded on every profile the kill switch takes down.
REASON_KILL_SWITCH = "kill_switch"

#: Reason recorded on every worker left when the supervisor shuts down.
REASON_SHUTDOWN = "shutdown"

#: Reason prefix recorded while a worker waits for its restart backoff.
REASON_RESTART_BACKOFF = "restart_backoff"

#: Reasons of a ``stopped`` profile the scheduler must not restart by itself:
#: they record a deliberate operator decision rather than a fleet decision. A
#: profile that is merely disabled is filtered by its ``enabled`` flag, so
#: re-enabling it is enough to bring it back into the schedule.
PINNED_STOP_REASONS = frozenset({REASON_OPERATOR_STOP})

#: Engine event kinds rendered by the operations page of the dashboard.
EVENT_START = "start"
EVENT_STOP = "stop"
EVENT_CRASH = "crash"
EVENT_RESTART = "restart"
EVENT_KILL_SWITCH = "kill_switch"
EVENT_LEGACY_DB_ARCHIVED = "legacy_db_archived"
EVENT_CATALOGUE_APPLIED = "catalogue_applied"
EVENT_PROFILE_CREATED = "profile_created"
EVENT_PROFILE_UPDATED = "profile_updated"
EVENT_PROFILE_DELETED = "profile_deleted"
EVENT_SETTINGS_UPDATED = "settings_updated"
EVENT_BLOCKED_LIVE = "blocked_live"
EVENT_ERROR = "error"

#: Strategy directory of the realtime container, used whenever it exists.
CONTAINER_STRATEGIES_DIR = Path("/app/user_data/strategies")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Process launcher
# ---------------------------------------------------------------------------
class ProcessLauncher(Protocol):
    """How the supervisor starts and stops a worker process.

    This protocol is the seam the test suite replaces: production uses
    :class:`SubprocessLauncher`, tests inject a recording double, and no test
    ever starts a real ``freqtrade`` process.
    """

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
    ) -> subprocess.Popen[bytes]:
        """Start ``argv`` in ``cwd`` with ``env`` and return the child handle."""
        ...

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float = TERMINATE_GRACE_SECONDS,
    ) -> None:
        """Stop ``process``: SIGTERM first, SIGKILL after ``grace_seconds``."""
        ...


class SubprocessLauncher:
    """The default :class:`ProcessLauncher`: real children, logs appended to a file.

    ``stdout`` and ``stderr`` of the child are appended to the profile log file
    (``<state_dir>/logs/<id>.log``), which is the file the runbook reads. The
    child gets its own session so that a ``Ctrl-C`` on the supervisor is handled
    by the supervisor alone: it stops every worker gracefully instead of letting
    the terminal signal reach the fleet directly.
    """

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
    ) -> subprocess.Popen[bytes]:
        """Start ``argv`` in ``cwd``, appending its output to ``log_path``."""
        target = Path(log_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab") as handle:
            return subprocess.Popen(
                list(argv),
                cwd=str(cwd),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float = TERMINATE_GRACE_SECONDS,
    ) -> None:
        """SIGTERM ``process``, then SIGKILL it after ``grace_seconds``.

        Freqtrade exits on SIGTERM with status 130; a terminated child is
        therefore always a clean stop and never a crash, provided the caller
        dropped the handle before signalling it (which the supervisor does).
        """
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:  # pragma: no cover - the child vanished under us
            return
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            logger.warning("child %s ignored SIGTERM; killing it", process.pid)
        process.kill()
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:  # pragma: no cover - the kernel reaps SIGKILL
            logger.error("child %s is still alive after SIGKILL", process.pid)


# ---------------------------------------------------------------------------
# Health bookkeeping
# ---------------------------------------------------------------------------
@dataclass
class _WorkerHealth:
    """What the supervisor remembers between two reads of one worker."""

    #: Consecutive failed REST reads of the current run.
    failures: int = 0
    #: When the current run was started; the startup grace is measured from it.
    started_at: datetime | None = None
    #: Consecutive failed ``GET /ping`` of the current run.
    ping_failures: int = 0
    #: Timestamps of the restarts of the sliding budget window.
    restarts: list[datetime] = field(default_factory=list)
    #: When set, the worker waits for its backoff and must not be started yet.
    retry_at: datetime | None = None
    #: Last failure message, kept for the ``error`` state and the journal.
    last_error: str | None = None


# ---------------------------------------------------------------------------
# The supervisor
# ---------------------------------------------------------------------------
class Supervisor:
    """Own the fleet: schedule profiles, spawn workers, restart the unhealthy ones.

    The store is the source of truth of the fleet states; the supervisor adds
    the process handles and the health counters, which only make sense inside a
    running process. Everything here is synchronous on purpose -- ``Popen``
    never waits for a worker to become healthy -- except the poll cycle, which
    awaits the REST reads of the workers.
    """

    def __init__(
        self,
        *,
        store: StateStore,
        settings: PlatformSettings,
        state_dir: Path,
        config_dir: Path | None = None,
        strategies_dir: Path | None = None,
        launcher: ProcessLauncher | None = None,
        client_factory: Callable[..., FreqtradeClient] | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.state_dir = Path(state_dir)
        self._config_dir = Path(config_dir) if config_dir is not None else resolve_config_dir(env)
        self._strategies_dir = Path(strategies_dir) if strategies_dir is not None else None
        # The mapping is read live rather than copied, so a caller may keep it up
        # to date (the live-trading gate is evaluated on every scheduling pass).
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._launcher: ProcessLauncher = SubprocessLauncher() if launcher is None else launcher
        self._client_factory: Callable[..., FreqtradeClient] = (
            FreqtradeClient if client_factory is None else client_factory
        )
        self._clock: Callable[[], datetime] = utc_now if clock is None else clock

        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._clients: dict[str, FreqtradeClient] = {}
        self._discarded_clients: list[FreqtradeClient] = []
        self._metrics: dict[str, ProfileMetrics] = {}
        self._history: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
        self._health: dict[str, _WorkerHealth] = {}
        self._operator_stops: set[str] = set()
        self._operator_starts: set[str] = set()
        self._catalogue: StrategyCatalogue | None = None

        self._bootstrapped = False
        self._running = False
        self._started_at: datetime | None = None
        self._last_poll_at: datetime | None = None

    # -- clock -------------------------------------------------------------
    def now(self) -> datetime:
        """Return the current instant, read from the injected clock."""
        return self._clock()

    def uptime_seconds(self) -> float:
        """Return how long :meth:`start` has been running (``0.0`` before it)."""
        if self._started_at is None:
            return 0.0
        return max(0.0, (self.now() - self._started_at).total_seconds())

    # -- lifecycle ---------------------------------------------------------
    def bootstrap(self) -> None:
        """Prepare the state store, seed the catalogue and start the fleet.

        The sequence is the one the deployment relies on for a deterministic
        first boot:

        1. :meth:`StateStore.bootstrap` -- a foreign ``state.db`` is archived by
           the store, which journals ``legacy_db_archived`` itself;
        2. the effective settings: the settings the supervisor was built with,
           then the keys of ``config/platform.json``, then the persisted
           ``platform_settings`` row, then the ``TB_*`` overrides (they win);
        3. the profile catalogue -- **only the missing** profiles are inserted
           (``source`` ``catalogue``, state ``stopped``); an existing row,
           catalogue-owned or operator-owned, is never overwritten;
        4. the kill switch -- when ``<state_dir>/KILL_SWITCH`` exists nothing
           starts and every profile is reported ``stopped``/``kill_switch``;
        5. :meth:`schedule`, which spawns the workers without waiting for any of
           them to become healthy.

        Calling it twice is a no-op, so :meth:`start` and the API may both make
        sure the engine is booted.
        """
        if self._bootstrapped:
            return
        self.store.bootstrap()
        self.settings = self._load_settings()
        self._seed_catalogue()
        self._apply_boot_kill_switch()
        self._bootstrapped = True
        self.schedule()

    async def start(self) -> None:
        """Boot the engine when needed, mark it running and schedule the fleet."""
        if not self._bootstrapped:
            self.bootstrap()
        if self._started_at is None:
            self._started_at = self.now()
        self._running = True
        logger.info(
            "supervisor started with %d profile(s) in the state store", self.profile_count()
        )
        self.schedule()

    async def stop(self) -> None:
        """Stop every worker gracefully and release the HTTP clients.

        Each child receives SIGTERM and, after
        :data:`TERMINATE_GRACE_SECONDS`, SIGKILL; every stop is journaled with
        the reason ``shutdown`` so a container stop never looks like a crash.
        """
        self._running = False
        stopped = self._stop_all_workers(reason=REASON_SHUTDOWN, event_kind=EVENT_STOP)
        for profile_id in list(self._clients):
            self._discard_client(profile_id)
        await self._close_discarded_clients()
        logger.info("supervisor stopped %d worker(s)", stopped)

    # -- scheduling --------------------------------------------------------
    def schedule(self) -> None:
        """Reconcile the fleet with the store: start, block or stop.

        Candidates are the enabled profiles sorted by ``priority`` descending
        then ``id`` ascending. Every candidate starts in this very pass when it
        is not already running, is not inside its restart backoff and its live
        gate allows it. Nothing else can hold it back: there is no cap, no slot
        accounting and no stagger gate, so a candidate never waits for another
        profile to free something.

        A live profile that does not pass
        :func:`~trading_platform.config.live_trading_gate` is ``blocked`` with
        its own reason instead, so the walk goes on to the next candidate; a
        disabled profile is ``stopped`` with :data:`REASON_DISABLED`, and a
        profile waiting for its restart backoff is left alone until it is due.
        An explicit operator start is honoured exactly like an automatic one.

        While the kill switch is engaged the method only makes sure that nothing
        runs: it never rewrites a state, so the journal keeps reporting the kill
        switch as the reason the fleet is down.
        """
        profiles = self.store.list_profiles()
        if self.kill_switch_engaged():
            for record in profiles:
                if self.is_running(record.id):
                    self._stop_worker(
                        record.id,
                        reason=REASON_KILL_SWITCH,
                        event_kind=EVENT_STOP,
                        level="warning",
                    )
            return

        for record in self._candidates(profiles):
            if self.is_running(record.id):
                continue
            if not self._consume_backoff(record.id):
                continue
            if self._refuse_live(record):
                continue
            self._start(record, event_kind=EVENT_START)
        for record in profiles:
            if not record.enabled:
                self._apply_disabled(record)

    def start_profile(self, profile_id: str) -> None:
        """Start one profile now, whatever the operator did to it before.

        The explicit action clears an operator stop, an ``error`` state and the
        restart budget of the profile, then starts a worker if none is alive.
        """
        record = self.store.get_profile(profile_id)
        if record is None:
            logger.warning("ignoring start of the unknown profile %r", profile_id)
            return
        self._operator_stops.discard(profile_id)
        self._reset_health(profile_id)
        if self.is_running(profile_id):
            return
        if self._start(record, event_kind=EVENT_START):
            self._operator_starts.add(profile_id)

    def stop_profile(self, profile_id: str, reason: str = REASON_OPERATOR_STOP) -> None:
        """Stop one profile and keep it stopped across the next scheduling passes.

        The worker is terminated gracefully and the profile is pinned: the
        scheduler does not start it again by itself, so the operator decision
        survives until :meth:`start_profile` or :meth:`restart_profile`.
        """
        record = self.store.get_profile(profile_id)
        if record is None:
            logger.warning("ignoring stop of the unknown profile %r", profile_id)
            return
        self._operator_stops.add(profile_id)
        self._reset_health(profile_id)
        if self._stop_worker(profile_id, reason=reason, event_kind=EVENT_STOP):
            return
        self._set_state(record, STATE_STOPPED, reason)

    def restart_profile(self, profile_id: str) -> None:
        """Stop and start one profile again, clearing its failure history.

        This is the operator recovery path of a profile in state ``error``: the
        restart budget is reset, so a fixed worker gets its full set of retries
        back.
        """
        record = self.store.get_profile(profile_id)
        if record is None:
            logger.warning("ignoring restart of the unknown profile %r", profile_id)
            return
        self._operator_stops.discard(profile_id)
        self._reset_health(profile_id)
        self._terminate_worker(profile_id)
        if self._start(record, event_kind=EVENT_RESTART):
            self._operator_starts.add(profile_id)

    def is_running(self, profile_id: str) -> bool:
        """Whether a live worker process is held for ``profile_id``."""
        process = self._processes.get(profile_id)
        return process is not None and process.poll() is None

    def healthy_count(self) -> int:
        """Count the running profiles that are still inside the failure threshold."""
        return sum(
            1
            for record in self.store.list_profiles()
            if record.state == STATE_RUNNING and self._is_healthy(record.id)
        )

    def profile_count(self) -> int:
        """Return the number of profiles known to the state store."""
        return len(self.store.list_profiles())

    def profiles(self) -> list[ProfileRecord]:
        """Return every profile with its live state, read from the store."""
        return self.store.list_profiles()

    def latest_snapshots(self) -> dict[str, ProfileSnapshot]:
        """Return the most recent snapshot of every profile that has one."""
        return self.store.latest_snapshots()

    def metrics_for(self, profile_id: str) -> ProfileMetrics | None:
        """Return the numbers of the last successful read of the current run.

        A failed read clears them: the poller must never publish a stale
        measurement as if the worker had just answered.
        """
        return self._metrics.get(profile_id)

    def history_for(
        self,
        profile_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        """Return the ``(daily_rows, trade_rows)`` of the last successful read.

        A failed read clears them, exactly like :meth:`metrics_for`: the poller
        must never persist the rows of a worker that has not just answered.
        ``None`` means the profile is not running, has never been read, or its
        last read or history pass failed.
        """
        return self._history.get(profile_id)

    def status(self) -> dict[str, Any]:
        """Return the operational snapshot of the engine.

        The keys are stable and JSON-friendly; the API uses them for
        ``GET /api/health`` and ``GET /api/settings``:

        ``running``, ``bootstrapped``, ``uptime_seconds``, ``started_at``,
        ``last_poll_at``, ``kill_switch_engaged``, the ``profiles_*`` counters,
        the effective settings the engine acts on, and the resolved
        ``state_dir``/``state_db`` paths. No capacity is advertised: the fleet
        has no cap, so there is nothing to report about slots.
        """
        records = self.store.list_profiles()
        running = sum(1 for record in records if record.state == STATE_RUNNING)
        return {
            "running": self._running,
            "bootstrapped": self._bootstrapped,
            "uptime_seconds": self.uptime_seconds(),
            "started_at": None if self._started_at is None else format_ts(self._started_at),
            "last_poll_at": None if self._last_poll_at is None else format_ts(self._last_poll_at),
            "kill_switch_engaged": self.kill_switch_engaged(),
            "profiles_total": len(records),
            "profiles_running": running,
            "profiles_healthy": self.healthy_count(),
            # Retained for wire compatibility: with no fleet cap nothing can be
            # held back, so no profile is ever queued and this is always 0.
            "profiles_queued": 0,
            "profiles_blocked": sum(1 for r in records if r.state == STATE_BLOCKED),
            "profiles_error": sum(1 for r in records if r.state == STATE_ERROR),
            "snapshot_interval_seconds": int(self.settings.snapshot_interval_seconds),
            "profile_api_port_base": int(self.settings.profile_api_port_base),
            "state_dir": str(self.state_dir),
            "state_db": str(self.store.path),
        }

    def apply_settings(self, *, snapshot_interval_seconds: int | None = None) -> PlatformSettings:
        """Change the run-time settings, persist them and reschedule at once.

        The whole document is stored under the ``platform_settings`` key, so the
        next boot merges the operator's values over ``config/platform.json``.
        Scheduling runs immediately, so a profile that is eligible starts at
        once.
        """
        changes: dict[str, int] = {}
        if snapshot_interval_seconds is not None:
            changes["snapshot_interval_seconds"] = int(snapshot_interval_seconds)
        self.settings = self.settings.with_overrides(**changes)
        self.store.set_setting(SETTINGS_KEY, json.dumps(self.settings.model_dump()))
        if changes:
            rendered = ", ".join(f"{name}={value}" for name, value in sorted(changes.items()))
            self.store.record_event(
                "info",
                EVENT_SETTINGS_UPDATED,
                f"platform settings updated: {rendered}",
            )
        self.schedule()
        return self.settings

    # -- kill switch -------------------------------------------------------
    def kill_switch_path(self) -> Path:
        """Return the path of the kill-switch file."""
        return self.state_dir / KILL_SWITCH_FILENAME

    def kill_switch_engaged(self) -> bool:
        """Whether the kill-switch file exists, which stops the whole fleet."""
        return self.kill_switch_path().exists()

    def engage_kill_switch(self) -> None:
        """Stop every worker and write the kill-switch file.

        While the file exists nothing starts, including after a container
        restart, because the file lives in the state volume next to the
        database.
        """
        path = self.kill_switch_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        for health in self._health.values():
            health.retry_at = None
        stopped = self._stop_all_workers(
            reason=REASON_KILL_SWITCH,
            event_kind=EVENT_STOP,
            level="warning",
        )
        self.store.record_event(
            "warning",
            EVENT_KILL_SWITCH,
            f"kill switch engaged: {stopped} worker(s) stopped; "
            f"no profile starts while {KILL_SWITCH_FILENAME} exists",
        )

    def release_kill_switch(self) -> None:
        """Remove the kill-switch file and resume scheduling immediately."""
        self.kill_switch_path().unlink(missing_ok=True)
        self.store.record_event(
            "info",
            EVENT_KILL_SWITCH,
            f"kill switch released: {KILL_SWITCH_FILENAME} removed, scheduling resumed",
        )
        self.schedule()

    # -- polling -----------------------------------------------------------
    async def refresh_once(self) -> None:
        """Run one poll cycle: reap, schedule, read the workers, apply the policy.

        This is the method the snapshot poller calls once per interval, and the
        one the test suite drives by hand. It never waits for a worker: every
        read is a bounded REST call, and a worker that does not answer is
        counted, not blocked on.
        """
        if not self._bootstrapped:
            self.bootstrap()
        await self._close_discarded_clients()
        self._reap_exited_children()
        self.schedule()
        await self._poll_running_workers()
        self.schedule()
        await self._close_discarded_clients()
        self._last_poll_at = self.now()

    # -- settings ----------------------------------------------------------
    def _platform_config_path(self) -> Path:
        """Return the path of ``platform.json`` inside the config directory."""
        return self._config_dir / PLATFORM_CONFIG_FILENAME

    def _load_settings(self) -> PlatformSettings:
        """Load the effective settings, from the weakest layer to the strongest.

        The layers are the settings the supervisor was constructed with (by
        default the documented defaults), the keys actually present in
        ``config/platform.json``, the persisted ``platform_settings`` row and
        finally the ``TB_*`` overrides. Only the keys a layer really carries
        override the layer below, so a short deployment document does not reset
        a value the caller set explicitly, and the environment always wins: it
        is the only input an operator changes without writing to the state
        volume.
        """
        effective = self.settings
        document = self._document_settings()
        if document:
            try:
                effective = effective.with_overrides(**document)
            except ValidationError:
                logger.warning("ignoring the unreadable %s document", PLATFORM_CONFIG_FILENAME)
        effective = effective.with_overrides(**self._persisted_settings())
        return effective.with_overrides(**self._environment_overrides())

    def _document_settings(self) -> dict[str, Any]:
        """Return the keys of the ``platform.json`` document (``{}`` when unusable)."""
        try:
            raw = self._platform_config_path().read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _persisted_settings(self) -> dict[str, Any]:
        """Return the persisted ``platform_settings`` row as a mapping."""
        raw = self.store.get_setting(SETTINGS_KEY)
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except ValueError:
            logger.warning("ignoring the unreadable persisted %s row", SETTINGS_KEY)
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _environment_overrides(self) -> dict[str, Any]:
        """Return the fields the ``TB_*`` variables override in the document.

        ``config.py`` owns the parsing rules; the difference between the
        document loaded with the environment and without it is exactly the set
        of fields the environment decided. The baseline passes an *empty*
        mapping rather than ``None``: ``env=None`` means "read ``os.environ``"
        in :mod:`trading_platform.config`, so it would return the very overrides
        this method is looking for and the difference would always be empty.
        """
        path = self._platform_config_path()
        pristine = PlatformSettings.load(path, env={})
        with_env = PlatformSettings.load(path, env=self._env)
        return {
            name: getattr(with_env, name)
            for name in PlatformSettings.model_fields
            if getattr(with_env, name) != getattr(pristine, name)
        }

    # -- boot helpers ------------------------------------------------------
    def _seed_catalogue(self) -> None:
        """Insert the catalogue profiles that are missing, and journal the pass.

        An existing row is never touched here: it may carry the operator's own
        values, and a boot must not silently revert them to the catalogue.
        """
        catalogue = load_profile_catalogue(self._config_dir / PROFILES_DOCUMENT)
        created: list[str] = []
        for profile in catalogue:
            if self.store.get_profile(profile.id) is not None:
                continue
            self.store.upsert_profile(profile, source="catalogue", state=STATE_STOPPED)
            created.append(profile.id)
        if created:
            logger.info("seeded %d catalogue profile(s): %s", len(created), ", ".join(created))
        self.store.record_event(
            "info",
            EVENT_CATALOGUE_APPLIED,
            f"applied the profile catalogue: {len(created)} created, {len(catalogue)} declared",
        )

    def _apply_boot_kill_switch(self) -> None:
        """Report every profile as stopped by the kill switch when it is engaged.

        The process tree of a previous run is gone, so no profile may be left in
        ``running`` while the switch is on, and no pid of that run may survive
        either. A profile the operator stopped on purpose keeps its own reason.
        """
        if not self.kill_switch_engaged():
            return
        for record in self.store.list_profiles():
            if record.pid is not None or record.started_at is not None:
                self.store.set_profile_runtime(record.id, pid=None, started_at=None)
            if record.state == STATE_STOPPED and record.state_reason in PINNED_STOP_REASONS:
                continue
            self._set_state(record, STATE_STOPPED, REASON_KILL_SWITCH)
        self.store.record_event(
            "warning",
            EVENT_KILL_SWITCH,
            f"{KILL_SWITCH_FILENAME} is present: nothing starts until the kill switch is released",
        )

    # -- scheduling helpers ------------------------------------------------
    def _candidates(self, profiles: Sequence[ProfileRecord]) -> list[ProfileRecord]:
        """Return the profiles the scheduler may run, in scheduling order.

        ``profiles`` arrives sorted by ``priority`` descending then ``id``
        ascending. A disabled profile is not a candidate, and neither is one in
        the terminal ``error`` state nor one the operator stopped.
        """
        return [
            record
            for record in profiles
            if record.enabled
            and record.state != STATE_ERROR
            and record.id not in self._operator_stops
            and not (record.state == STATE_STOPPED and record.state_reason in PINNED_STOP_REASONS)
        ]

    def _apply_disabled(self, record: ProfileRecord) -> None:
        """Make sure a disabled profile holds no worker and says why."""
        if self.is_running(record.id):
            self._stop_worker(record.id, reason=REASON_DISABLED, event_kind=EVENT_STOP)
            return
        self._set_state(record, STATE_STOPPED, REASON_DISABLED)

    def _consume_backoff(self, profile_id: str) -> bool:
        """Whether ``profile_id`` may start now; consumes an elapsed backoff."""
        health = self._health.get(profile_id)
        if health is None or health.retry_at is None:
            return True
        if health.retry_at > self.now():
            return False
        health.retry_at = None
        return True

    def _set_state(self, record: ProfileRecord, state: str, reason: str | None) -> bool:
        """Write the state of a profile; return whether it actually changed.

        The last error of the profile is preserved, and an unchanged state is
        not rewritten: the scheduler runs on every tick and must not bump
        ``updated_at`` (or the journal) when nothing happened.
        """
        if record.state == state and record.state_reason == reason:
            return False
        self.store.set_profile_state(record.id, state, reason, record.last_error)
        return True

    # -- starting and stopping workers --------------------------------------
    def _refuse_live(self, record: ProfileRecord) -> bool:
        """Whether the live-trading gate refuses ``record``; block it when it does.

        This is the single live-gate decision point of the supervisor:
        :meth:`schedule` calls it to leave a refused profile alone, and
        :meth:`_start` calls it again before it spawns anything, so an explicit
        operator start is refused exactly like an automatic one.
        """
        if not record.is_live:
            return False
        allowed, gate_reason = live_trading_gate(record.mode, self._env)
        if allowed:
            return False
        reason = gate_reason or "live trading refused"
        if self._set_state(record, STATE_BLOCKED, reason):
            self.store.record_event(
                "warning",
                EVENT_BLOCKED_LIVE,
                f"refused to start live profile {record.id!r}: {reason}",
                record.id,
            )
        return True

    def _start(self, record: ProfileRecord, *, event_kind: str, level: str = "info") -> bool:
        """Generate the configuration of ``record`` and spawn its worker."""
        profile_id = record.id
        if self.kill_switch_engaged():
            self._set_state(record, STATE_STOPPED, REASON_KILL_SWITCH)
            return False
        if not record.enabled:
            self._set_state(record, STATE_STOPPED, REASON_DISABLED)
            return False
        if self._refuse_live(record):
            return False

        port = self._api_port_for(profile_id)
        username = self._api_username(profile_id)
        password = secrets.token_urlsafe(API_PASSWORD_BYTES)
        self.store.set_api_credentials(profile_id, username, password)
        self.store.set_profile_runtime(profile_id, api_port=port)

        config_path = profile_config_path(self.state_dir, profile_id)
        log_path = profile_log_path(self.state_dir, profile_id)
        try:
            config = build_freqtrade_config(
                record,
                self.settings,
                strategy_class_name=self._strategy_class_name(record),
                api_port=port,
                api_username=username,
                api_password=password,
                exchange_key=self._exchange_key(record),
                exchange_secret=self._exchange_secret(record),
            )
            write_freqtrade_config(config, config_path)
            profile_data_dir(self.state_dir, profile_id).mkdir(parents=True, exist_ok=True)
            argv = build_freqtrade_argv(
                record,
                config_path=config_path,
                user_data_dir=profile_runtime_dir(self.state_dir, profile_id),
                db_url=profile_db_url(self.state_dir, profile_id),
                logfile=log_path,
                strategy_path=self._strategy_path(),
                binary=resolve_freqtrade_binary(self._env),
            )
            process = self._launcher.spawn(
                argv,
                cwd=self.state_dir,
                env=dict(self._env),
                log_path=log_path,
            )
        except OSError as exc:
            message = f"could not start worker {profile_id!r}: {exc}"
            self.store.set_profile_state(profile_id, STATE_ERROR, None, message)
            self.store.record_event("error", EVENT_ERROR, message, profile_id)
            logger.error("%s", message)
            return False

        self._processes[profile_id] = process
        self._metrics.pop(profile_id, None)
        self._history.pop(profile_id, None)
        health = self._health_for(profile_id)
        health.failures = 0
        health.started_at = self.now()
        health.ping_failures = 0
        health.retry_at = None
        health.last_error = None
        pid = self._pid_of(process)
        self.store.set_profile_runtime(profile_id, pid=pid, started_at=format_ts(self.now()))
        self.store.set_profile_state(profile_id, STATE_RUNNING, None, None)
        self.store.record_event(
            level,
            event_kind,
            f"started worker {profile_id!r} on port {port} (pid {pid})",
            profile_id,
        )
        logger.info("started worker %s on port %d (pid %s)", profile_id, port, pid)
        return True

    def _terminate_worker(self, profile_id: str) -> subprocess.Popen[bytes] | None:
        """Terminate and forget the worker of a profile, writing no state.

        The handle is dropped *before* the child is signalled: that is what
        makes a supervisor-initiated stop impossible to mistake for a crash.
        """
        process = self._processes.pop(profile_id, None)
        self._discard_client(profile_id)
        self._metrics.pop(profile_id, None)
        self._history.pop(profile_id, None)
        self._operator_starts.discard(profile_id)
        self.store.set_profile_runtime(profile_id, pid=None, started_at=None)
        if process is not None and process.poll() is None:
            self._launcher.terminate(process, grace_seconds=TERMINATE_GRACE_SECONDS)
        return process

    def _stop_worker(
        self,
        profile_id: str,
        *,
        reason: str,
        event_kind: str | None = None,
        level: str = "info",
        state: str = STATE_STOPPED,
    ) -> bool:
        """Stop the worker of ``profile_id`` and record the resulting state."""
        process = self._terminate_worker(profile_id)
        if process is None:
            return False
        pid = self._pid_of(process)
        record = self.store.get_profile(profile_id)
        self.store.set_profile_state(
            profile_id,
            state,
            reason,
            None if record is None else record.last_error,
        )
        if event_kind is not None:
            self.store.record_event(
                level,
                event_kind,
                f"stopped worker {profile_id!r} (pid {pid}): {reason}",
                profile_id,
            )
        return True

    def _stop_all_workers(self, *, reason: str, event_kind: str | None, level: str = "info") -> int:
        """Stop every worker the supervisor holds; return how many were stopped."""
        stopped = 0
        for profile_id in list(self._processes):
            if self._stop_worker(profile_id, reason=reason, event_kind=event_kind, level=level):
                stopped += 1
        return stopped

    # -- health and restarts -----------------------------------------------
    async def _poll_running_workers(self) -> None:
        """Read every running worker and apply the health policy to the failures.

        A failed read counts a failure and, once the failure threshold is
        reached, *starts a death check* instead of restarting the worker on the
        spot: :meth:`_confirm_death` decides whether the worker is really gone.
        A worker that is merely slow -- ``/balance`` taking 12 to 20 s while 22
        workers share one CoinGecko rate limit -- answers ``/ping`` and is left
        alone.
        """
        for record in self.store.list_profiles():
            if not self.is_running(record.id):
                continue
            client = self._client_for(record)
            try:
                metrics = await client.fetch_all()
            except FreqtradeClientError as exc:
                if self._count_failure(record, str(exc)) and await self._confirm_death(record):
                    self._handle_unhealthy(record, self._health_for(record.id), str(exc))
            except Exception as exc:  # a client bug must not kill the fleet
                message = f"unexpected error while reading profile {record.id!r}: {exc}"
                if self._count_failure(record, message) and await self._confirm_death(record):
                    self._handle_unhealthy(record, self._health_for(record.id), message)
            else:
                health = self._health_for(record.id)
                health.failures = 0
                health.ping_failures = 0
                health.last_error = None
                self._metrics[record.id] = metrics
                try:
                    self._history[record.id] = await self._read_history(client)
                except FreqtradeClientError as exc:
                    self._drop_history(record.id, exc)
                except Exception as exc:  # a history bug must not restart a worker
                    self._drop_history(record.id, exc)

    async def _read_history(
        self,
        client: FreqtradeClient,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Read the daily rows and the trade rows of one worker.

        The trade list is the closed trades (the ``HISTORY_TRADE_LIMIT`` most
        recent ones) followed by the open ones: the persisted read model is
        upserted on ``(profile_id, trade_id)``, so a live open row must come
        last to win over a closed row carrying the same id.
        """
        daily_rows = await client.daily_records()
        trade_rows = await client.trade_records(HISTORY_TRADE_LIMIT)
        open_rows = await client.open_trade_records()
        return (daily_rows, [*trade_rows, *open_rows])

    def _drop_history(self, profile_id: str, exc: Exception) -> None:
        """Forget the cached history of a profile and report why.

        This is deliberately *not* a read failure: a worker that answers
        ``/balance`` but not ``/daily`` is healthy, and a history hiccup must
        neither count a failure nor restart it.
        """
        self._history.pop(profile_id, None)
        logger.warning("could not read the history of profile %r: %s", profile_id, exc)

    def _reap_exited_children(self) -> None:
        """Journal and handle every child that exited without being asked to."""
        for profile_id, process in list(self._processes.items()):
            code = process.poll()
            if code is None:
                continue
            self._terminate_worker(profile_id)
            record = self.store.get_profile(profile_id)
            if record is None:  # pragma: no cover - the row was deleted under us
                continue
            message = f"worker {profile_id!r} exited on its own with code {code}"
            self.store.record_event("error", EVENT_CRASH, message, profile_id)
            self._handle_failure(record, message, fatal=True)

    def _handle_failure(self, record: ProfileRecord, message: str, *, fatal: bool = False) -> None:
        """Count one failed read and escalate when the policy says so.

        ``fatal`` is used for a child that exited by itself: that is one failure
        no retry can absorb, so it escalates without waiting for the failure
        threshold -- and therefore without the startup grace, which protects a
        worker that could not answer yet, never one that is already gone.

        A non-fatal failure inside the startup window is not counted at all:
        the worker was just spawned, and Freqtrade needs tens of seconds before
        its API server answers.
        """
        if fatal:
            # A child that exited on its own is already gone: no threshold and
            # no startup window applies to it.
            self._book_failure(record, message)
            self._handle_unhealthy(record, self._health_for(record.id), message)
            return
        if self._count_failure(record, message):
            self._handle_unhealthy(record, self._health_for(record.id), message)

    def _book_failure(self, record: ProfileRecord, message: str) -> None:
        """Count one failure unconditionally, clearing the cached measurements."""
        health = self._health_for(record.id)
        health.failures += 1
        health.last_error = message
        self._metrics.pop(record.id, None)
        self._history.pop(record.id, None)

    def _count_failure(self, record: ProfileRecord, message: str) -> bool:
        """Count one failed read; return whether the failure threshold is reached.

        This is the counting half of the policy, and reaching the threshold only
        *starts* the death check: :meth:`_poll_running_workers` uses it and then
        asks :meth:`_confirm_death` whether the worker is really gone, while
        :meth:`_handle_failure` keeps the whole behaviour for the callers that
        have no client to ping.

        ``True`` is returned from the crossing onwards, so a worker whose reads
        keep failing is probed on every cycle until it is proven dead or answers
        again -- that is how ``GET /ping`` accumulates its consecutive failures.

        A failure inside the startup window is not counted at all -- but it
        still invalidates the cached measurements: the poller must never publish
        a measurement of a worker that has not just answered.
        """
        self._metrics.pop(record.id, None)
        self._history.pop(record.id, None)
        if self._within_startup_grace(record.id):
            return False
        self._book_failure(record, message)
        return self._health_for(record.id).failures >= UNHEALTHY_THRESHOLD

    def _within_startup_grace(self, profile_id: str) -> bool:
        """Whether a worker is still inside its fair startup window.

        A profile the supervisor has never started -- no health, or no
        ``started_at`` -- is *not* inside the grace: the window protects a fresh
        child, it never shields an unknown one.
        """
        health = self._health.get(profile_id)
        if health is None or health.started_at is None:
            return False
        grace = timedelta(seconds=WORKER_STARTUP_GRACE_SECONDS)
        return self.now() - health.started_at < grace

    async def _confirm_death(self, record: ProfileRecord) -> bool:
        """Whether an unhealthy worker is proven gone, so it may be restarted.

        Two proofs are accepted, and nothing else:

        * the process handle is gone -- ``self.is_running(record.id)`` is
          ``False``, which :meth:`_reap_exited_children` handles as a crash;
        * the worker's own liveness endpoint, ``GET /ping``, has failed
          :data:`UNHEALTHY_PING_THRESHOLD` consecutive times. ``/balance`` may
          be slow, ``/ping`` answering ``{"status": "pong"}`` means the worker is
          alive and is not restarted, however many reads failed.

        A ping that raises -- :class:`FreqtradeClientError` or anything else --
        is a failed ping and never a crash. The message of a failed ping is
        reported through the log only: ``health.last_error`` keeps the read
        failure that started the death check, which is what the ``error`` state
        and the journal of the restart must explain.
        """
        if not self.is_running(record.id):
            return True
        health = self._health_for(record.id)
        client = self._client_for(record)
        try:
            alive = await client.ping()
        except Exception as exc:  # a failed ping is a failure, never a crash
            alive = False
            logger.warning("confirmed-death ping of profile %r failed: %s", record.id, exc)
        health.ping_failures = 0 if alive else health.ping_failures + 1
        return health.ping_failures >= UNHEALTHY_PING_THRESHOLD

    def _handle_unhealthy(
        self,
        record: ProfileRecord,
        health: _WorkerHealth,
        message: str,
    ) -> None:
        """Stop an unhealthy worker and schedule its restart, or give up on it."""
        now = self.now()
        window = timedelta(seconds=RESTART_WINDOW_SECONDS)
        health.restarts = [ts for ts in health.restarts if now - ts <= window]
        self._terminate_worker(record.id)
        health.failures = 0
        if len(health.restarts) >= MAX_RESTARTS_IN_WINDOW:
            health.retry_at = None
            self.store.set_profile_state(record.id, STATE_ERROR, None, message)
            self.store.record_event(
                "error",
                EVENT_ERROR,
                f"giving up on profile {record.id!r} after {MAX_RESTARTS_IN_WINDOW} restarts "
                f"in {RESTART_WINDOW_SECONDS // 60} minutes: {message}",
                record.id,
            )
            logger.error("giving up on profile %s: %s", record.id, message)
            return
        health.restarts.append(now)
        index = min(len(health.restarts) - 1, len(RESTART_BACKOFF_SECONDS) - 1)
        backoff = RESTART_BACKOFF_SECONDS[index]
        health.retry_at = now + timedelta(seconds=backoff)
        self.store.set_profile_state(
            record.id,
            STATE_STOPPED,
            f"{REASON_RESTART_BACKOFF}: retrying in {backoff}s",
            message,
        )
        self.store.record_event(
            "warning",
            EVENT_RESTART,
            f"restarting profile {record.id!r} in {backoff}s: {message}",
            record.id,
        )
        logger.warning("restarting profile %s in %ds: %s", record.id, backoff, message)

    def _health_for(self, profile_id: str) -> _WorkerHealth:
        """Return the health bookkeeping of a profile, creating it if needed."""
        health = self._health.get(profile_id)
        if health is None:
            health = _WorkerHealth()
            self._health[profile_id] = health
        return health

    def _reset_health(self, profile_id: str) -> None:
        """Forget the failures, the ping failures and the restart budget.

        The whole entry is replaced by a fresh :class:`_WorkerHealth`, so the
        startup window is cleared as well: the next :meth:`_start` opens a new
        one.
        """
        self._health[profile_id] = _WorkerHealth()

    def _is_healthy(self, profile_id: str) -> bool:
        """Whether the reads of a worker are still inside the failure threshold."""
        if not self.is_running(profile_id):
            return False
        health = self._health.get(profile_id)
        return health is None or health.failures < UNHEALTHY_THRESHOLD

    # -- clients -----------------------------------------------------------
    def _client_for(self, record: ProfileRecord) -> FreqtradeClient:
        """Return the REST client of a worker, creating it on first use."""
        client = self._clients.get(record.id)
        if client is None:
            port = record.api_port or self._api_port_for(record.id)
            client = self._client_factory(
                base_url=f"http://127.0.0.1:{int(port)}/api/v1",
                username=record.api_username or self._api_username(record.id),
                password=record.api_password or "",
            )
            self._clients[record.id] = client
        return client

    def _discard_client(self, profile_id: str) -> None:
        """Forget the client of a worker; it is closed on the next async pass."""
        client = self._clients.pop(profile_id, None)
        if client is not None:
            self._discarded_clients.append(client)

    async def _close_discarded_clients(self) -> None:
        """Close the HTTP clients of the workers that are gone."""
        pending, self._discarded_clients = self._discarded_clients, []
        for client in pending:
            close = getattr(client, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as exc:  # closing a client must never abort a poll
                logger.warning("could not close a Freqtrade client: %s", exc)

    # -- per-profile paths and values --------------------------------------
    def _strategy_path(self) -> Path:
        """Return the directory the worker searches for strategy files.

        The container layout wins when it exists, so the same code serves a host
        checkout and the realtime image.
        """
        if self._strategies_dir is not None:
            return self._strategies_dir
        if CONTAINER_STRATEGIES_DIR.is_dir():
            return CONTAINER_STRATEGIES_DIR
        return STRATEGIES_DIR

    def _strategy_class_name(self, record: ProfileRecord) -> str:
        """Return the Freqtrade class name the profile's strategy resolves to."""
        catalogue = self._catalogue_for_strategies()
        meta = catalogue.by_class_name(record.strategy) or catalogue.get(record.strategy)
        if meta is not None and meta.class_name:
            return meta.class_name
        return record.strategy

    def _catalogue_for_strategies(self) -> StrategyCatalogue:
        """Return the strategy catalogue, loaded once per supervisor."""
        if self._catalogue is None:
            self._catalogue = load_strategy_catalogue(
                self._config_dir / STRATEGIES_DOCUMENT,
                self._strategy_path(),
            )
        return self._catalogue

    def _api_port_for(self, profile_id: str) -> int:
        """Return the deterministic port of a profile.

        The index is the position of the profile in the **id-sorted** list of
        every profile of the store, so a profile keeps its port across boots,
        promotions and deploys.
        """
        identifiers = sorted(record.id for record in self.store.list_profiles())
        try:
            index = identifiers.index(profile_id)
        except ValueError:  # pragma: no cover - the caller just read the profile
            index = 0
        return allocate_api_port(index, self.settings)

    @staticmethod
    def _api_username(profile_id: str) -> str:
        """Return the REST user of a worker, derived from the profile id."""
        return f"profile-{profile_id}"

    def _exchange_key(self, record: ProfileRecord) -> str:
        """Return the exchange key of a live profile, else an empty string."""
        if not record.is_live:
            return ""
        return str(self._env.get(ENV_LIVE_EXCHANGE_KEY) or "")

    def _exchange_secret(self, record: ProfileRecord) -> str:
        """Return the exchange secret of a live profile, else an empty string."""
        if not record.is_live:
            return ""
        return str(self._env.get(ENV_LIVE_EXCHANGE_SECRET) or "")

    @staticmethod
    def _pid_of(process: subprocess.Popen[bytes]) -> int | None:
        """Return the pid of a child handle, or ``None`` when it has none."""
        pid = getattr(process, "pid", None)
        return None if pid is None else int(pid)
