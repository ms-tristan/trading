"""Tests of the fleet supervisor: scheduling, the cap, ports, gates, restarts.

Nothing here starts a real ``freqtrade`` process and nothing opens a socket: the
:class:`ProcessLauncher` is replaced by :class:`RecordingLauncher` (which records
the command line, the environment and the pid, and never forks) and the REST
client by :class:`FakeFreqtradeApi`. The state store, on the other hand, is a
real SQLite file under ``tmp_path``, because keeping it in sync with the fleet is
exactly what the supervisor is for.

The clock is injected as well, so the restart backoff and the five-restarts-in-
fifteen-minutes escalation are driven by hand instead of by ``sleep``. That clock
is also the stagger clock: the fleet grows gradually by default
(``worker_start_stagger_seconds``), so :func:`make_harness` disables the stagger
unless a test asks for it. Every test written before the stagger existed pins the
immediate boot, and the regression tests of the gradual boot build their own
settings and move the clock by hand.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import stat
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trading_platform.config import PlatformSettings, live_trading_gate
from trading_platform.engine.client import FreqtradeClientError
from trading_platform.engine.config_builder import (
    profile_config_path,
    profile_data_dir,
    profile_db_url,
    profile_log_path,
    profile_runtime_dir,
)
from trading_platform.engine.supervisor import (
    CAP_QUEUE_REASON_TEMPLATE,
    HISTORY_TRADE_LIMIT,
    KILL_SWITCH_FILENAME,
    MAX_RESTARTS_IN_WINDOW,
    RESTART_BACKOFF_SECONDS,
    RESTART_WINDOW_SECONDS,
    SETTINGS_KEY,
    TERMINATE_GRACE_SECONDS,
    UNHEALTHY_THRESHOLD,
    SubprocessLauncher,
    Supervisor,
)
from trading_platform.models import ProfileConfig, ProfileMetrics, format_ts
from trading_platform.paths import STRATEGIES_DIR
from trading_platform.profiles.store import StateStore

#: The fake clock starts here, so every recorded timestamp is predictable.
START_TIME = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)

#: Default environment of a harness: it pins the Freqtrade binary so that the
#: recorded command line does not depend on the machine running the suite.
BASE_ENV: dict[str, str] = {"TB_FREQTRADE_BIN": sys.executable}

#: The strategy metadata document written into the temporary config directory.
STRATEGIES_DOCUMENT: dict[str, Any] = {
    "strategies": [
        {"id": "basic", "class_name": "BasicStrategy", "file": "BasicStrategy.py"},
        {"id": "momentum", "class_name": "MomentumStrategy", "file": "MomentumStrategy.py"},
    ]
}

METRICS = ProfileMetrics(
    portfolio_value=1234.5,
    cash=234.5,
    positions_value=1000.0,
    profit_abs=234.5,
    realized_profit_abs=100.0,
    unrealized_profit_abs=134.5,
    open_trades=1,
    closed_trades=4,
    win_rate=0.5,
    profit_factor=1.5,
    max_drawdown_pct=8.0,
)

#: The daily row the API double answers with, carrying the pinned
#: ``profile_daily`` keys of the monitoring read model.
DAILY_ROW: dict[str, Any] = {
    "date": "2026-09-27",
    "abs_profit": 12.5,
    "rel_profit": 0.0125,
    "starting_balance": 1000.0,
    "trade_count": 3,
}

#: A closed trade, carrying the pinned trade keys of the read model.
TRADE_ROW: dict[str, Any] = {
    "trade_id": 11,
    "pair": "BTC/USDT",
    "is_open": False,
    "open_date": "2026-09-27 10:00:00",
    "close_date": "2026-09-27 11:00:00",
    "amount": 0.01,
    "open_rate": 100.0,
    "close_rate": 110.0,
    "stake_amount": 100.0,
    "profit_abs": 1.0,
    "profit_pct": 0.01,
    "exit_reason": "roi",
}

#: The live open trade. It deliberately reuses ``trade_id`` 11: the supervisor
#: returns the open rows last so that the ``(profile_id, trade_id)`` upsert of
#: the poller lets the live row win over the closed one it just replaced.
OPEN_TRADE_ROW: dict[str, Any] = {
    "trade_id": 11,
    "pair": "BTC/USDT",
    "is_open": True,
    "open_date": "2026-09-27 12:00:00",
    "close_date": None,
    "amount": 0.02,
    "open_rate": 120.0,
    "close_rate": None,
    "stake_amount": 200.0,
    "profit_abs": 0.5,
    "profit_pct": 0.0025,
    "exit_reason": None,
}


def staggered_settings(**overrides: Any) -> PlatformSettings:
    """Return the gradual-boot settings: three slots, one new worker every 10 s."""
    values: dict[str, Any] = {"max_running_profiles": 3, "worker_start_stagger_seconds": 10}
    values.update(overrides)
    return PlatformSettings(**values)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, start: datetime = START_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        """Move the clock forward and return the new instant."""
        self.now += timedelta(seconds=seconds)
        return self.now


class FakeProcess:
    """A child handle: a pid, ``poll``, ``terminate``, ``kill`` and ``wait``."""

    def __init__(self, argv: Sequence[str], pid: int) -> None:
        self.argv = list(argv)
        self.pid = pid
        self.returncode: int | None = None
        self.signals: list[str] = []

    def poll(self) -> int | None:
        """Return the exit status, or ``None`` while the child is alive."""
        return self.returncode

    def terminate(self) -> None:
        """Record SIGTERM; the fake child exits with Freqtrade's status 130."""
        self.signals.append("SIGTERM")
        if self.returncode is None:
            self.returncode = 130

    def kill(self) -> None:
        """Record SIGKILL."""
        self.signals.append("SIGKILL")
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        """Return the exit status without ever blocking."""
        return 0 if self.returncode is None else self.returncode

    def exit(self, code: int = 1) -> None:
        """Simulate a child that exited on its own."""
        self.returncode = code


#: Every state store a test opened, so the fixture below can close them all.
_OPEN_STORES: list[StateStore] = []


@pytest.fixture(autouse=True)
def _close_state_stores() -> Iterator[None]:
    """Close every state store a test opened, keeping the suite warning-free."""
    yield
    while _OPEN_STORES:
        _OPEN_STORES.pop().close()


@dataclass
class SpawnCall:
    """One recorded ``spawn`` call."""

    argv: list[str]
    cwd: Path
    env: dict[str, str]
    log_path: Path
    process: FakeProcess


class RecordingLauncher:
    """A :class:`ProcessLauncher` double that never starts a process."""

    def __init__(self, *, fail_with: OSError | None = None) -> None:
        self.calls: list[SpawnCall] = []
        self.terminated: list[FakeProcess] = []
        self.grace_seconds: list[float] = []
        self.fail_with = fail_with
        self._pid = 5000

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
    ) -> FakeProcess:
        """Record the call and return a fake child handle."""
        if self.fail_with is not None:
            raise self.fail_with
        self._pid += 1
        process = FakeProcess(argv, self._pid)
        self.calls.append(
            SpawnCall(
                argv=list(argv),
                cwd=Path(cwd),
                env=dict(env),
                log_path=Path(log_path),
                process=process,
            )
        )
        return process

    def terminate(self, process: FakeProcess, *, grace_seconds: float = 0.0) -> None:
        """Record the stop and terminate the fake child."""
        self.terminated.append(process)
        self.grace_seconds.append(grace_seconds)
        process.terminate()

    @property
    def processes(self) -> list[FakeProcess]:
        """Return every child the launcher ever produced."""
        return [call.process for call in self.calls]

    def running(self) -> list[FakeProcess]:
        """Return the children that are still alive."""
        return [process for process in self.processes if process.poll() is None]


class FakeClient:
    """One REST client of :class:`FakeFreqtradeApi`."""

    def __init__(self, api: FakeFreqtradeApi, username: str) -> None:
        self._api = api
        self.username = username
        self.closed = False

    async def fetch_all(self) -> ProfileMetrics:
        """Answer with the configured metrics, or fail as the test asked."""
        self._api.reads[self.username] = self._api.reads.get(self.username, 0) + 1
        remaining = self._api.failures.get(self.username, 0)
        if remaining > 0:
            self._api.failures[self.username] = remaining - 1
            raise FreqtradeClientError(
                f"Freqtrade API request failed: GET balance (profile {self.username})"
            )
        if self._api.unexpected.get(self.username, 0) > 0:
            self._api.unexpected[self.username] -= 1
            raise RuntimeError("the transport exploded")
        return self._api.metrics

    async def aclose(self) -> None:
        """Record that the client was released."""
        if self._api.close_error:
            raise RuntimeError("the connection pool refuses to close")
        self.closed = True

    async def daily_records(self) -> list[dict[str, Any]]:
        """Answer with the configured daily rows, or fail as the test asked."""
        self._api.history_call(self.username)
        return [dict(row) for row in self._api.daily_rows]

    async def trade_records(self, limit: int = HISTORY_TRADE_LIMIT) -> list[dict[str, Any]]:
        """Answer with the configured closed trades, remembering ``limit``."""
        self._api.history_call(self.username)
        self._api.trade_limits.append(int(limit))
        return [dict(row) for row in self._api.trade_rows]

    async def open_trade_records(self) -> list[dict[str, Any]]:
        """Answer with the configured live trades, or fail as the test asked."""
        self._api.history_call(self.username)
        return [dict(row) for row in self._api.open_rows]


class FakeFreqtradeApi:
    """A ``client_factory`` double: one client per profile, driven by the test."""

    def __init__(self, metrics: ProfileMetrics | None = None) -> None:
        self.metrics = metrics or METRICS
        self.failures: dict[str, int] = {}
        self.unexpected: dict[str, int] = {}
        self.reads: dict[str, int] = {}
        self.created: list[dict[str, Any]] = []
        self.clients: list[FakeClient] = []
        self.close_error = False
        self.daily_rows: list[dict[str, Any]] = [dict(DAILY_ROW)]
        self.trade_rows: list[dict[str, Any]] = [dict(TRADE_ROW)]
        self.open_rows: list[dict[str, Any]] = [dict(OPEN_TRADE_ROW)]
        self.history_failures: dict[str, int] = {}
        self.history_unexpected: dict[str, int] = {}
        self.history_reads: dict[str, int] = {}
        self.trade_limits: list[int] = []

    @staticmethod
    def key(profile_id: str) -> str:
        """Return the REST user of a profile, which is how clients are keyed."""
        return f"profile-{profile_id}"

    def fail_next(self, profile_id: str, times: int = 1) -> None:
        """Make the next ``times`` reads of a profile raise a client error."""
        key = self.key(profile_id)
        self.failures[key] = self.failures.get(key, 0) + times

    def fail_unexpectedly(self, profile_id: str, times: int = 1) -> None:
        """Make the next ``times`` reads raise an unexpected (non-client) error."""
        key = self.key(profile_id)
        self.unexpected[key] = self.unexpected.get(key, 0) + times

    def fail_history_next(self, profile_id: str, times: int = 1) -> None:
        """Make the next ``times`` history calls of a profile raise a client error."""
        key = self.key(profile_id)
        self.history_failures[key] = self.history_failures.get(key, 0) + times

    def fail_history_unexpectedly(self, profile_id: str, times: int = 1) -> None:
        """Make the next ``times`` history calls raise an unexpected error."""
        key = self.key(profile_id)
        self.history_unexpected[key] = self.history_unexpected.get(key, 0) + times

    def history_call(self, username: str) -> None:
        """Count one history call and raise when the test asked for a failure.

        Every history endpoint consumes the counter, so a configured number of
        failures covers that many history *passes* -- the supervisor reads the
        daily rows first and stops at the first failure.
        """
        self.history_reads[username] = self.history_reads.get(username, 0) + 1
        remaining = self.history_failures.get(username, 0)
        if remaining > 0:
            self.history_failures[username] = remaining - 1
            raise FreqtradeClientError(
                f"Freqtrade API request failed: GET daily (profile {username})"
            )
        if self.history_unexpected.get(username, 0) > 0:
            self.history_unexpected[username] -= 1
            raise RuntimeError("the history transport exploded")

    def __call__(self, **kwargs: Any) -> FakeClient:
        """Return a client bound to this API double."""
        self.created.append(dict(kwargs))
        client = FakeClient(self, str(kwargs["username"]))
        self.clients.append(client)
        return client


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
@dataclass
class Harness:
    """One supervisor wired on ``tmp_path`` with every external effect faked."""

    supervisor: Supervisor
    store: StateStore
    state_dir: Path
    config_dir: Path
    strategies_dir: Path
    clock: FakeClock
    launcher: RecordingLauncher
    api: FakeFreqtradeApi
    env: dict[str, str] = field(default_factory=dict)

    def records(self) -> dict[str, Any]:
        """Return the stored profiles keyed by id."""
        return {record.id: record for record in self.store.list_profiles()}

    def events(self) -> list[Any]:
        """Return the engine journal, newest first."""
        return self.store.list_events(limit=500)

    def kinds(self) -> list[str]:
        """Return the kinds of the engine journal, newest first."""
        return [event.kind for event in self.events()]

    def reasons(self) -> list[str]:
        """Return the messages of the engine journal, newest first."""
        return [event.message for event in self.events()]

    def spawned_ids(self) -> list[str]:
        """Return the ids of the spawned workers, in spawn order.

        The id is read from the command line itself (``--config`` points into
        ``<state_dir>/profiles/<id>/``), so an assertion on this list pins the
        real spawn order and not just the number of children.
        """
        return [
            Path(call.argv[call.argv.index("--config") + 1]).parent.name
            for call in self.launcher.calls
        ]


def profile_config(profile_id: str, **overrides: Any) -> ProfileConfig:
    """Build a paper catalogue profile, with ``overrides`` applied."""
    document: dict[str, Any] = {
        "id": profile_id,
        "name": profile_id.replace("-", " ").title(),
        "strategy": "basic",
        "timeframe": "1h",
        "mode": "paper",
        "exchange": "binance",
        "pairs": ["BTC/USDT"],
        "initial_capital": 1000.0,
        "max_open_trades": 2,
        "priority": 100,
        "enabled": True,
    }
    document.update(overrides)
    return ProfileConfig.model_validate(document)


def write_documents(
    config_dir: Path,
    profiles: Sequence[ProfileConfig] = (),
    platform: Mapping[str, Any] | None = None,
) -> None:
    """Write the three configuration documents of a temporary deployment."""
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {"profiles": [profile.model_dump() for profile in profiles]}
    (config_dir / "profiles.json").write_text(json.dumps(payload), encoding="utf-8")
    (config_dir / "platform.json").write_text(json.dumps(dict(platform or {})), encoding="utf-8")
    (config_dir / "strategies.json").write_text(json.dumps(STRATEGIES_DOCUMENT), encoding="utf-8")


def make_harness(
    tmp_path: Path,
    *,
    profiles: Sequence[ProfileConfig] = (),
    platform: Mapping[str, Any] | None = None,
    settings: PlatformSettings | None = None,
    env: Mapping[str, str] | None = None,
    clock: FakeClock | None = None,
    launcher: RecordingLauncher | None = None,
    api: FakeFreqtradeApi | None = None,
    seed_store: bool = True,
) -> Harness:
    """Build a supervisor whose children and REST calls are entirely faked.

    The default settings carry ``worker_start_stagger_seconds=0``, so a harness
    that does not ask for the gradual boot starts the whole eligible fleet in one
    pass: that is the behaviour of every test written before the stagger gate
    existed. The regression tests of the gradual boot pass their own settings.
    """
    state_dir = tmp_path / "realtime"
    state_dir.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / "config"
    write_documents(config_dir, profiles, platform)
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)

    store = StateStore(state_dir / "state.db")
    _OPEN_STORES.append(store)
    if seed_store:
        store.bootstrap()
    resolved_env = dict(BASE_ENV if env is None else env)
    resolved_clock = clock or FakeClock()
    resolved_launcher = launcher or RecordingLauncher()
    resolved_api = api or FakeFreqtradeApi()
    supervisor = Supervisor(
        store=store,
        settings=settings or PlatformSettings(worker_start_stagger_seconds=0),
        state_dir=state_dir,
        config_dir=config_dir,
        strategies_dir=strategies_dir,
        launcher=resolved_launcher,
        client_factory=resolved_api,
        env=resolved_env,
        clock=resolved_clock,
    )
    return Harness(
        supervisor=supervisor,
        store=store,
        state_dir=state_dir,
        config_dir=config_dir,
        strategies_dir=strategies_dir,
        clock=resolved_clock,
        launcher=resolved_launcher,
        api=resolved_api,
        env=resolved_env,
    )


def live_env(**extra: str) -> dict[str, str]:
    """Return an environment that opens every live-trading gate."""
    return {
        **BASE_ENV,
        "TB_ALLOW_LIVE_TRADING": "I_UNDERSTAND_THE_RISK",
        "TB_LIVE_EXCHANGE_KEY": "live-key",
        "TB_LIVE_EXCHANGE_SECRET": "live-secret",
        **extra,
    }


async def fail_cycle(harness: Harness, profile_id: str, times: int) -> None:
    """Fail ``UNHEALTHY_THRESHOLD`` reads of a profile, once per refresh."""
    harness.api.fail_next(profile_id, UNHEALTHY_THRESHOLD * times)
    for _ in range(UNHEALTHY_THRESHOLD * times):
        await harness.supervisor.refresh_once()


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------
def test_bootstrap_seeds_a_fresh_database(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        profiles=[profile_config("beta"), profile_config("alpha")],
    )
    harness.supervisor.bootstrap()

    records = harness.records()
    assert sorted(records) == ["alpha", "beta"]
    assert {record.source for record in records.values()} == {"catalogue"}
    assert {record.state for record in records.values()} == {"running"}
    assert harness.kinds().count("catalogue_applied") == 1


def test_bootstrap_is_idempotent(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    harness.supervisor.bootstrap()
    harness.supervisor.bootstrap()

    assert len(harness.launcher.calls) == 1
    assert harness.kinds().count("catalogue_applied") == 1


def test_bootstrap_never_overwrites_an_existing_row(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    harness.store.upsert_profile(
        profile_config("alpha", name="Operator alpha", priority=1),
        source="operator",
        state="stopped",
    )

    harness.supervisor.bootstrap()

    record = harness.records()["alpha"]
    assert record.name == "Operator alpha"
    assert record.source == "operator"
    assert record.priority == 1
    assert record.state == "running"


def test_bootstrap_archives_a_legacy_database(tmp_path: Path) -> None:
    state_dir = tmp_path / "realtime"
    state_dir.mkdir(parents=True)
    config_dir = tmp_path / "config"
    write_documents(config_dir, [profile_config("alpha")])
    database = state_dir / "state.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE profiles (id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO profiles VALUES ('from-the-previous-product')")
    connection.commit()
    connection.close()

    store = StateStore(database)
    _OPEN_STORES.append(store)
    supervisor = Supervisor(
        store=store,
        settings=PlatformSettings(),
        state_dir=state_dir,
        config_dir=config_dir,
        strategies_dir=tmp_path / "strategies",
        launcher=RecordingLauncher(),
        client_factory=FakeFreqtradeApi(),
        env=dict(BASE_ENV),
        clock=FakeClock(),
    )
    supervisor.bootstrap()

    archives = list(state_dir.glob("state.db.legacy-*"))
    assert len(archives) == 1
    archived = store.list_events(limit=50)
    assert [event.level for event in archived if event.kind == "legacy_db_archived"] == ["warning"]
    assert store.get_profile("from-the-previous-product") is None
    assert [record.id for record in supervisor.profiles()] == ["alpha"]


def test_bootstrap_starts_the_fleet_without_waiting_for_it(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    harness.supervisor.bootstrap()

    assert len(harness.launcher.calls) == 1
    assert harness.supervisor.is_running("alpha")
    record = harness.records()["alpha"]
    assert record.state == "running"
    assert record.pid == harness.launcher.calls[0].process.pid
    assert record.started_at == format_ts(START_TIME)
    assert "start" in harness.kinds()


# ---------------------------------------------------------------------------
# Generated command line and configuration
# ---------------------------------------------------------------------------
def test_spawn_uses_the_documented_command_line(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    harness.supervisor.bootstrap()

    call = harness.launcher.calls[0]
    state_dir = harness.state_dir
    assert call.argv == [
        sys.executable,
        "trade",
        "--config",
        str(profile_config_path(state_dir, "alpha")),
        "--userdir",
        str(profile_runtime_dir(state_dir, "alpha")),
        "--db-url",
        profile_db_url(state_dir, "alpha"),
        "--logfile",
        str(profile_log_path(state_dir, "alpha")),
        "--strategy-path",
        str(harness.strategies_dir),
    ]
    assert call.cwd == state_dir
    assert call.env == harness.env
    assert call.log_path == profile_log_path(state_dir, "alpha")
    assert profile_data_dir(state_dir, "alpha").is_dir()


def test_generated_configuration_carries_the_credentials_with_mode_0600(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    harness.supervisor.bootstrap()

    path = profile_config_path(harness.state_dir, "alpha")
    config = json.loads(path.read_text(encoding="utf-8"))
    record = harness.records()["alpha"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config["api_server"]["username"] == "profile-alpha"
    assert config["api_server"]["password"] == record.api_password
    assert record.api_username == "profile-alpha"
    assert len(record.api_password or "") >= 30
    assert config["api_server"]["listen_port"] == record.api_port
    assert config["strategy"] == "BasicStrategy"
    assert all((record.api_password or "\0") not in message for message in harness.reasons())


def test_strategy_class_name_falls_back_to_the_profile_strategy(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        profiles=[profile_config("alpha", strategy="not-in-the-catalogue")],
    )
    harness.supervisor.bootstrap()

    config = json.loads(profile_config_path(harness.state_dir, "alpha").read_text(encoding="utf-8"))
    assert config["strategy"] == "not-in-the-catalogue"


async def test_client_is_built_from_the_generated_credentials(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    supervisor.bootstrap()
    record = harness.records()["alpha"]

    assert harness.api.created == []

    await supervisor.refresh_once()

    assert harness.api.created == [
        {
            "base_url": f"http://127.0.0.1:{record.api_port}/api/v1",
            "username": "profile-alpha",
            "password": record.api_password,
        }
    ]


# ---------------------------------------------------------------------------
# Fleet cap, ordering and ports
# ---------------------------------------------------------------------------
def test_cap_queues_the_profiles_beyond_the_limit(tmp_path: Path) -> None:
    profiles = [profile_config(f"p{index:02d}") for index in range(22)]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=12, worker_start_stagger_seconds=0),
    )
    harness.supervisor.bootstrap()

    records = harness.records()
    running = sorted(record.id for record in records.values() if record.state == "running")
    queued = [record for record in records.values() if record.state == "queued"]
    assert running == [f"p{index:02d}" for index in range(12)]
    assert len(queued) == 10
    assert {record.state_reason for record in queued} == {
        "queued: fleet cap reached (12 of 12 slots in use)"
    }
    assert len(harness.launcher.calls) == 12
    assert harness.kinds().count("cap_reached") == 1

    harness.supervisor.schedule()
    harness.supervisor.schedule()
    assert harness.kinds().count("cap_reached") == 1


def test_priority_then_id_orders_the_fleet(tmp_path: Path) -> None:
    profiles = [
        profile_config("c-low", priority=50),
        profile_config("a-low", priority=50),
        profile_config("e-high", priority=100),
        profile_config("b-low", priority=50),
        profile_config("d-high", priority=100),
    ]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=3, worker_start_stagger_seconds=0),
    )
    harness.supervisor.bootstrap()

    records = harness.records()
    running = [record.id for record in harness.supervisor.profiles() if record.state == "running"]
    assert running == ["d-high", "e-high", "a-low"]
    assert records["b-low"].state_reason == "queued: fleet cap reached (3 of 3 slots in use)"


def test_api_ports_follow_the_id_sorted_catalogue(tmp_path: Path) -> None:
    profiles = [
        profile_config("zeta"),
        profile_config("alpha"),
        profile_config("mu"),
    ]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(profile_api_port_base=9000, worker_start_stagger_seconds=0),
    )
    harness.supervisor.bootstrap()

    records = harness.records()
    assert records["alpha"].api_port == 9000
    assert records["mu"].api_port == 9001
    assert records["zeta"].api_port == 9002


def test_promotion_when_a_slot_frees(tmp_path: Path) -> None:
    profiles = [profile_config(name) for name in ("alpha", "beta", "gamma")]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=1, worker_start_stagger_seconds=0),
    )
    harness.supervisor.bootstrap()
    assert harness.supervisor.is_running("alpha")

    harness.supervisor.stop_profile("alpha")
    assert not harness.supervisor.is_running("alpha")
    assert harness.records()["alpha"].state == "stopped"

    harness.supervisor.schedule()
    assert harness.supervisor.is_running("beta")
    assert not harness.supervisor.is_running("alpha")
    assert harness.records()["gamma"].state == "queued"


def test_operator_stop_survives_the_scheduler(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta")]
    harness = make_harness(tmp_path, profiles=profiles)
    harness.supervisor.bootstrap()
    spawns = len(harness.launcher.calls)

    harness.supervisor.stop_profile("beta")

    harness.supervisor.schedule()
    harness.supervisor.schedule()
    assert not harness.supervisor.is_running("beta")
    assert harness.records()["beta"].state == "stopped"
    assert harness.records()["beta"].state_reason == "operator_stop"
    assert len(harness.launcher.calls) == spawns
    assert "stop" in harness.kinds()

    harness.supervisor.start_profile("beta")
    assert harness.supervisor.is_running("beta")
    assert harness.records()["beta"].state == "running"


def test_unknown_profiles_are_ignored(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    harness.supervisor.bootstrap()
    spawns = len(harness.launcher.calls)

    harness.supervisor.start_profile("ghost")
    harness.supervisor.stop_profile("ghost")
    harness.supervisor.restart_profile("ghost")

    assert harness.records().keys() == {"alpha"}
    assert len(harness.launcher.calls) == spawns


def test_disabled_profile_is_stopped_with_reason(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta", enabled=False)]
    harness = make_harness(tmp_path, profiles=profiles)
    harness.supervisor.bootstrap()

    assert harness.supervisor.is_running("alpha")
    assert not harness.supervisor.is_running("beta")
    assert harness.records()["beta"].state_reason == "disabled"
    assert len(harness.launcher.calls) == 1

    harness.store.update_profile_fields("alpha", {"enabled": False})
    harness.supervisor.schedule()

    assert not harness.supervisor.is_running("alpha")
    assert harness.records()["alpha"].state == "stopped"
    assert harness.records()["alpha"].state_reason == "disabled"


# ---------------------------------------------------------------------------
# Fleet slots: the cap bounds occupied slots, not candidate positions
# ---------------------------------------------------------------------------
def test_a_refused_live_profile_does_not_consume_a_slot(tmp_path: Path) -> None:
    """A live profile the gate refuses is blocked and leaves its slot to the next one.

    The refused profile sorts first, so the two paper profiles behind it must
    both run in the very same pass: a profile the gate refuses holds no worker
    and therefore occupies no slot of the fleet cap.
    """
    profiles = [
        profile_config("live-first", mode="live", priority=300),
        profile_config("paper-a", priority=200),
        profile_config("paper-b", priority=100),
    ]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=2, worker_start_stagger_seconds=0),
    )

    harness.supervisor.bootstrap()

    expected = live_trading_gate("live", harness.env)
    records = harness.records()
    assert expected[0] is False
    assert records["paper-a"].state == "running"
    assert records["paper-b"].state == "running"
    assert records["live-first"].state == "blocked"
    assert records["live-first"].state_reason == expected[1]
    assert [record.id for record in records.values() if record.state == "queued"] == []
    assert harness.spawned_ids() == ["paper-a", "paper-b"]
    assert len(harness.launcher.calls) == 2
    assert harness.kinds().count("blocked_live") == 1


def test_the_fleet_converges_to_the_cap_with_a_refused_live_profile_among_the_top_priorities(
    tmp_path: Path,
) -> None:
    """The production catalogue: the refused live profile must not waste a slot.

    ``faber-btc-1d-live`` sorts sixth, inside the six-slot budget, and the gate
    refuses it. The sixth slot therefore goes to the next candidate,
    ``keltner-sol-1h``, instead of staying empty: six workers run, the refused
    profile is blocked and every remaining candidate is queued for the cap.
    """
    profiles = [
        profile_config("basic-btc-1h"),
        profile_config("bollinger-eth-1h"),
        profile_config("donchian-eth-4h"),
        profile_config("dual-thrust-eth-15m"),
        profile_config("faber-btc-1d"),
        profile_config("faber-btc-1d-live", mode="live"),
        profile_config("keltner-sol-1h"),
        profile_config("basic-eth-4h", priority=50),
    ]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=6, worker_start_stagger_seconds=0),
    )

    harness.supervisor.bootstrap()

    records = harness.records()
    running = sorted(record.id for record in records.values() if record.state == "running")
    assert len(running) == 6
    assert "keltner-sol-1h" in running
    assert records["faber-btc-1d-live"].state == "blocked"
    assert not harness.supervisor.is_running("faber-btc-1d-live")
    queued = [record for record in records.values() if record.state == "queued"]
    assert [record.id for record in queued] == ["basic-eth-4h"]
    assert {record.state_reason for record in queued} == {
        "queued: fleet cap reached (6 of 6 slots in use)"
    }
    assert len(harness.launcher.calls) == 6
    assert harness.kinds().count("cap_reached") == 1


def test_the_cap_queue_reason_names_the_used_and_total_slots(tmp_path: Path) -> None:
    """The queued reason carries the occupied slots and the cap, not just the cap."""
    assert CAP_QUEUE_REASON_TEMPLATE == (
        "queued: fleet cap reached ({used} of {total} slots in use)"
    )
    profiles = [profile_config(f"p{index:02d}") for index in range(8)]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=6, worker_start_stagger_seconds=0),
    )

    harness.supervisor.bootstrap()

    queued = [record for record in harness.records().values() if record.state == "queued"]
    expected = CAP_QUEUE_REASON_TEMPLATE.format(used=6, total=6)
    assert expected == "queued: fleet cap reached (6 of 6 slots in use)"
    assert len(queued) == 2
    assert {record.state_reason for record in queued} == {expected}


def test_slots_used_equals_the_running_profiles_and_the_fleet_converges(tmp_path: Path) -> None:
    """The published ``slots_used`` counts the rows in state ``running``.

    Asserted inside the convergence scenario, where six workers run because the
    refused live profile costs no slot.
    """
    profiles = [
        profile_config("basic-btc-1h"),
        profile_config("bollinger-eth-1h"),
        profile_config("donchian-eth-4h"),
        profile_config("dual-thrust-eth-15m"),
        profile_config("faber-btc-1d"),
        profile_config("faber-btc-1d-live", mode="live"),
        profile_config("keltner-sol-1h"),
        profile_config("basic-eth-4h", priority=50),
    ]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=6, worker_start_stagger_seconds=0),
    )
    supervisor = harness.supervisor
    supervisor.bootstrap()

    status = supervisor.status()
    assert status["slots_used"] == status["profiles_running"] == 6
    assert status["slots_total"] == 6
    assert (
        len([record for record in harness.store.list_profiles() if record.state == "running"]) == 6
    )


def test_a_low_priority_refused_live_profile_costs_no_slot(tmp_path: Path) -> None:
    """A refused profile releases its slot to the next candidate by priority."""
    profiles = [
        profile_config("paper-a", priority=300),
        profile_config("live-low", mode="live", priority=200),
        profile_config("paper-b", priority=100),
        profile_config("paper-c", priority=50),
    ]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=2, worker_start_stagger_seconds=0),
    )

    harness.supervisor.bootstrap()

    expected = live_trading_gate("live", harness.env)
    records = harness.records()
    assert expected[0] is False
    assert records["paper-a"].state == "running"
    assert records["paper-b"].state == "running"
    assert records["live-low"].state == "blocked"
    assert records["live-low"].state_reason == expected[1]
    assert records["paper-c"].state == "queued"
    assert records["paper-c"].state_reason == ("queued: fleet cap reached (2 of 2 slots in use)")
    assert harness.spawned_ids() == ["paper-a", "paper-b"]


async def test_a_restart_backoff_reserves_its_slot_but_is_not_a_used_slot(tmp_path: Path) -> None:
    """A reserved backoff slot bounds the fleet without being a published slot.

    ``alpha`` is stopped by the health policy and waits for its restart backoff
    while the clock is frozen: it holds no worker, yet its slot stays reserved,
    so ``gamma`` is queued for the cap although only one worker (``beta``) is
    alive. ``status()`` still reports the rows in state ``running``, so the
    reserved slot is not published as a used slot.
    """
    harness = make_harness(
        tmp_path,
        profiles=[
            profile_config("alpha", priority=300),
            profile_config("beta", priority=200),
            profile_config("gamma", priority=100),
        ],
        settings=PlatformSettings(max_running_profiles=2, worker_start_stagger_seconds=0),
    )
    supervisor = harness.supervisor
    await supervisor.start()
    assert supervisor.is_running("alpha")
    assert supervisor.is_running("beta")
    assert harness.records()["gamma"].state == "queued"

    await fail_cycle(harness, "alpha", 1)

    record = harness.records()["alpha"]
    assert record.state == "stopped"
    assert record.state_reason == f"restart_backoff: retrying in {RESTART_BACKOFF_SECONDS[0]}s"
    assert not supervisor.is_running("alpha")

    supervisor.schedule()

    assert supervisor.is_running("beta")
    assert not supervisor.is_running("alpha")
    assert harness.records()["gamma"].state == "queued"
    assert harness.records()["gamma"].state_reason == (
        "queued: fleet cap reached (2 of 2 slots in use)"
    )
    assert len(harness.launcher.running()) == 1
    status = supervisor.status()
    assert status["slots_used"] == status["profiles_running"] == 1


# ---------------------------------------------------------------------------
# Live-trading gate
# ---------------------------------------------------------------------------
def test_live_profile_is_blocked_without_the_gate(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta-live", mode="live")]
    harness = make_harness(tmp_path, profiles=profiles)
    harness.supervisor.bootstrap()

    expected = live_trading_gate("live", harness.env)
    record = harness.records()["beta-live"]
    assert expected[0] is False
    assert record.state == "blocked"
    assert record.state_reason == expected[1]
    assert "TB_ALLOW_LIVE_TRADING" in (record.state_reason or "")
    assert harness.supervisor.is_running("alpha")
    assert not harness.supervisor.is_running("beta-live")
    assert len(harness.launcher.calls) == 1
    assert not profile_config_path(harness.state_dir, "beta-live").exists()
    blocked = [event for event in harness.events() if event.kind == "blocked_live"]
    assert [event.profile_id for event in blocked] == ["beta-live"]

    harness.supervisor.schedule()
    assert harness.kinds().count("blocked_live") == 1


def test_live_profile_starts_when_every_gate_is_open(tmp_path: Path) -> None:
    profiles = [profile_config("beta-live", mode="live")]
    harness = make_harness(tmp_path, profiles=profiles, env=live_env())
    harness.supervisor.bootstrap()

    assert harness.supervisor.is_running("beta-live")
    config = json.loads(
        profile_config_path(harness.state_dir, "beta-live").read_text(encoding="utf-8")
    )
    assert config["exchange"]["key"] == "live-key"
    assert config["exchange"]["secret"] == "live-secret"
    assert config["dry_run"] is False
    assert "live-key" not in " ".join(harness.reasons())
    assert "live-secret" not in " ".join(harness.reasons())


def test_live_gate_is_re_evaluated_on_every_schedule(tmp_path: Path) -> None:
    profiles = [profile_config("beta-live", mode="live")]
    harness = make_harness(tmp_path, profiles=profiles, env=live_env())
    harness.supervisor.bootstrap()
    assert harness.supervisor.is_running("beta-live")

    harness.env.clear()
    harness.env.update(BASE_ENV)
    harness.supervisor.restart_profile("beta-live")

    record = harness.records()["beta-live"]
    assert record.state == "blocked"
    assert not harness.supervisor.is_running("beta-live")


# ---------------------------------------------------------------------------
# Health policy
# ---------------------------------------------------------------------------
async def test_three_failed_reads_restart_the_worker_with_backoff(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()
    assert supervisor.is_running("alpha")

    harness.api.fail_next("alpha", UNHEALTHY_THRESHOLD)
    for _ in range(UNHEALTHY_THRESHOLD - 1):
        await supervisor.refresh_once()
        assert supervisor.is_running("alpha")
        assert supervisor.metrics_for("alpha") is None

    await supervisor.refresh_once()
    assert not supervisor.is_running("alpha")
    record = harness.records()["alpha"]
    assert record.state == "stopped"
    assert record.state_reason == f"restart_backoff: retrying in {RESTART_BACKOFF_SECONDS[0]}s"
    assert record.last_error is not None
    assert "Freqtrade API request failed" in record.last_error
    assert harness.launcher.terminated
    assert "restart" in harness.kinds()

    harness.clock.advance(RESTART_BACKOFF_SECONDS[0] - 1)
    await supervisor.refresh_once()
    assert not supervisor.is_running("alpha")

    harness.clock.advance(1)
    await supervisor.refresh_once()
    assert supervisor.is_running("alpha")
    assert harness.records()["alpha"].state == "running"
    assert len(harness.launcher.calls) == 2
    assert supervisor.metrics_for("alpha") == harness.api.metrics


async def test_restart_backoff_saturates_at_forty_five_seconds(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()

    observed: list[int] = []
    for _ in range(MAX_RESTARTS_IN_WINDOW):
        await fail_cycle(harness, "alpha", 1)
        reason = harness.records()["alpha"].state_reason or ""
        observed.append(int(reason.rsplit(" ", 1)[-1].rstrip("s")))
        harness.clock.advance(observed[-1])
        await supervisor.refresh_once()
        assert supervisor.is_running("alpha")

    assert observed == [5, 15, 45, 45, 45]
    assert harness.kinds().count("restart") == MAX_RESTARTS_IN_WINDOW


async def test_five_restarts_in_the_window_give_up_and_free_the_slot(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta")]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=1, worker_start_stagger_seconds=0),
    )
    supervisor = harness.supervisor
    await supervisor.start()
    assert supervisor.is_running("alpha")
    assert harness.records()["beta"].state == "queued"

    for _ in range(MAX_RESTARTS_IN_WINDOW):
        await fail_cycle(harness, "alpha", 1)
        backoff = int(
            (harness.records()["alpha"].state_reason or "").rsplit(" ", 1)[-1].rstrip("s")
        )
        harness.clock.advance(backoff)
        await supervisor.refresh_once()
        assert supervisor.is_running("alpha")

    await fail_cycle(harness, "alpha", 1)

    record = harness.records()["alpha"]
    assert record.state == "error"
    assert record.last_error is not None
    assert "Freqtrade API request failed" in record.last_error
    assert record.state_reason is None
    assert not supervisor.is_running("alpha")
    error_events = [event for event in harness.events() if event.kind == "error"]
    assert len(error_events) == 1
    assert error_events[0].level == "error"
    assert supervisor.is_running("beta")
    assert len(harness.launcher.calls) == MAX_RESTARTS_IN_WINDOW + 2
    assert supervisor.healthy_count() == 1


async def test_the_restart_budget_window_slides(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()

    for _ in range(MAX_RESTARTS_IN_WINDOW):
        await fail_cycle(harness, "alpha", 1)
        backoff = int(
            (harness.records()["alpha"].state_reason or "").rsplit(" ", 1)[-1].rstrip("s")
        )
        harness.clock.advance(backoff)
        await supervisor.refresh_once()

    harness.clock.advance(RESTART_WINDOW_SECONDS + 1)
    await fail_cycle(harness, "alpha", 1)

    record = harness.records()["alpha"]
    assert record.state == "stopped"
    assert record.state_reason == f"restart_backoff: retrying in {RESTART_BACKOFF_SECONDS[0]}s"
    assert harness.kinds().count("error") == 0


async def test_restart_profile_resets_the_failure_history(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()

    for _ in range(MAX_RESTARTS_IN_WINDOW + 1):
        await fail_cycle(harness, "alpha", 1)
        record = harness.records()["alpha"]
        if record.state == "error":
            break
        harness.clock.advance(int((record.state_reason or "").rsplit(" ", 1)[-1].rstrip("s")))
        await supervisor.refresh_once()
    assert harness.records()["alpha"].state == "error"

    supervisor.restart_profile("alpha")
    assert supervisor.is_running("alpha")
    assert harness.records()["alpha"].state == "running"
    assert "restart" in harness.kinds()

    await fail_cycle(harness, "alpha", 1)
    assert harness.records()["alpha"].state_reason == (
        f"restart_backoff: retrying in {RESTART_BACKOFF_SECONDS[0]}s"
    )


async def test_unexpected_client_errors_are_counted_like_read_failures(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()

    harness.api.fail_unexpectedly("alpha", UNHEALTHY_THRESHOLD)
    for _ in range(UNHEALTHY_THRESHOLD):
        await supervisor.refresh_once()

    record = harness.records()["alpha"]
    assert record.state == "stopped"
    assert "unexpected error" in (record.last_error or "")
    assert not supervisor.is_running("alpha")


async def test_a_child_that_exits_is_a_crash_and_is_restarted(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()

    harness.launcher.calls[0].process.exit(1)
    await supervisor.refresh_once()

    crash = [event for event in harness.events() if event.kind == "crash"]
    assert len(crash) == 1
    assert crash[0].level == "error"
    assert crash[0].profile_id == "alpha"
    assert not supervisor.is_running("alpha")
    assert harness.records()["alpha"].state_reason == (
        f"restart_backoff: retrying in {RESTART_BACKOFF_SECONDS[0]}s"
    )

    harness.clock.advance(RESTART_BACKOFF_SECONDS[0])
    await supervisor.refresh_once()
    assert supervisor.is_running("alpha")
    assert len(harness.launcher.calls) == 2
    assert all(process.signals == [] for process in harness.launcher.processes)


def test_a_spawn_failure_puts_the_profile_in_error(tmp_path: Path) -> None:
    launcher = RecordingLauncher(fail_with=OSError("freqtrade is missing"))
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")], launcher=launcher)
    harness.supervisor.bootstrap()

    record = harness.records()["alpha"]
    assert record.state == "error"
    assert "freqtrade is missing" in (record.last_error or "")
    assert not harness.supervisor.is_running("alpha")
    assert "error" in harness.kinds()


# ---------------------------------------------------------------------------
# Shutdown, kill switch and settings
# ---------------------------------------------------------------------------
async def test_stop_terminates_every_child_gracefully(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta")]
    harness = make_harness(tmp_path, profiles=profiles)
    supervisor = harness.supervisor
    await supervisor.start()
    await supervisor.refresh_once()

    await supervisor.stop()

    assert not supervisor.is_running("alpha")
    assert not supervisor.is_running("beta")
    assert harness.launcher.terminated == harness.launcher.processes
    assert harness.launcher.grace_seconds == [TERMINATE_GRACE_SECONDS, TERMINATE_GRACE_SECONDS]
    records = harness.records()
    assert {record.state for record in records.values()} == {"stopped"}
    assert {record.state_reason for record in records.values()} == {"shutdown"}
    assert {record.pid for record in records.values()} == {None}
    assert {record.started_at for record in records.values()} == {None}
    assert harness.kinds().count("stop") == 2
    assert all(client.closed for client in harness.api.clients)
    assert harness.kinds().count("crash") == 0


def test_engage_and_release_the_kill_switch(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta")]
    harness = make_harness(tmp_path, profiles=profiles)
    supervisor = harness.supervisor
    supervisor.bootstrap()
    assert supervisor.is_running("alpha")

    supervisor.engage_kill_switch()

    assert supervisor.kill_switch_engaged()
    assert (harness.state_dir / KILL_SWITCH_FILENAME).exists()
    records = harness.records()
    assert {record.state for record in records.values()} == {"stopped"}
    assert {record.state_reason for record in records.values()} == {"kill_switch"}
    assert not any(supervisor.is_running(record.id) for record in records.values())
    assert harness.launcher.terminated == harness.launcher.processes
    assert harness.kinds().count("kill_switch") == 1
    spawns = len(harness.launcher.calls)

    supervisor.schedule()
    assert len(harness.launcher.calls) == spawns
    assert {record.state for record in harness.records().values()} == {"stopped"}

    supervisor.release_kill_switch()

    assert not supervisor.kill_switch_engaged()
    assert not (harness.state_dir / KILL_SWITCH_FILENAME).exists()
    assert {record.state for record in harness.records().values()} == {"running"}
    assert len(harness.launcher.calls) == spawns + 2


def test_boot_with_the_kill_switch_starts_nothing(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta")]
    harness = make_harness(tmp_path, profiles=profiles)
    harness.store.upsert_profile(profile_config("alpha"), source="catalogue", state="running")
    harness.store.set_profile_runtime("alpha", pid=4242, started_at=format_ts(START_TIME))
    harness.store.upsert_profile(
        profile_config("beta"),
        source="operator",
        state="stopped",
        state_reason="operator_stop",
    )
    (harness.state_dir / KILL_SWITCH_FILENAME).touch()

    harness.supervisor.bootstrap()

    assert harness.launcher.calls == []
    records = harness.records()
    assert records["alpha"].state == "stopped"
    assert records["alpha"].state_reason == "kill_switch"
    assert records["alpha"].pid is None
    assert records["alpha"].started_at is None
    assert records["beta"].state_reason == "operator_stop"
    assert harness.kinds().count("kill_switch") == 1

    harness.supervisor.release_kill_switch()
    assert harness.supervisor.is_running("alpha")
    assert not harness.supervisor.is_running("beta")
    assert records["beta"].state_reason == "operator_stop"


def test_apply_settings_persists_and_reschedules(tmp_path: Path) -> None:
    profiles = [profile_config(name) for name in ("alpha", "beta", "gamma")]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=2, worker_start_stagger_seconds=0),
    )
    supervisor = harness.supervisor
    supervisor.bootstrap()
    assert harness.records()["gamma"].state == "queued"

    updated = supervisor.apply_settings(max_running_profiles=3, snapshot_interval_seconds=15)

    assert updated.max_running_profiles == 3
    assert updated.snapshot_interval_seconds == 15
    assert supervisor.settings is updated
    persisted = json.loads(harness.store.get_setting(SETTINGS_KEY) or "{}")
    assert persisted["max_running_profiles"] == 3
    assert persisted["snapshot_interval_seconds"] == 15
    assert {record.state for record in harness.records().values()} == {"running"}
    assert "settings_updated" in harness.kinds()

    supervisor.apply_settings(max_running_profiles=1)

    records = harness.records()
    assert records["alpha"].state == "running"
    assert records["beta"].state == "queued"
    assert records["beta"].state_reason == "queued: fleet cap reached (1 of 1 slots in use)"
    assert not supervisor.is_running("beta")
    assert harness.launcher.terminated


def test_boot_applies_the_persisted_settings_over_the_file(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        profiles=[profile_config("alpha")],
        platform={"max_running_profiles": 3, "snapshot_interval_seconds": 30},
    )
    harness.store.set_setting(SETTINGS_KEY, json.dumps({"max_running_profiles": 5}))

    harness.supervisor.bootstrap()

    assert harness.supervisor.settings.max_running_profiles == 5
    assert harness.supervisor.settings.snapshot_interval_seconds == 30


def test_boot_lets_the_environment_win_over_the_persisted_settings(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        profiles=[profile_config("alpha")],
        platform={"max_running_profiles": 3, "snapshot_interval_seconds": 30},
        env={
            **BASE_ENV,
            "TB_MAX_RUNNING_PROFILES": "2",
            "TB_PROFILE_API_PORT_BASE": "9000",
        },
    )
    harness.store.set_setting(SETTINGS_KEY, json.dumps({"max_running_profiles": 5}))

    harness.supervisor.bootstrap()

    assert harness.supervisor.settings.max_running_profiles == 2
    assert harness.supervisor.settings.profile_api_port_base == 9000
    assert harness.records()["alpha"].api_port == 9000


def test_boot_reads_the_ambient_environment_when_no_mapping_is_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``TB_*`` layer works when the supervisor reads ``os.environ`` itself.

    A supervisor built without an explicit ``env`` mapping reads the ambient
    process environment, and it must still see the overrides: comparing the
    document against a baseline loaded with ``env=None`` -- which *also* means
    "read ``os.environ``" -- would always produce an empty difference and
    silently drop ``TB_MAX_RUNNING_PROFILES``.
    """
    monkeypatch.setenv("TB_MAX_RUNNING_PROFILES", "2")
    state_dir = tmp_path / "realtime"
    state_dir.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / "config"
    write_documents(config_dir, [profile_config("alpha")], {"max_running_profiles": 12})
    store = StateStore(state_dir / "state.db")
    _OPEN_STORES.append(store)
    supervisor = Supervisor(
        store=store,
        settings=PlatformSettings(),
        state_dir=state_dir,
        config_dir=config_dir,
        launcher=RecordingLauncher(),
        client_factory=FakeFreqtradeApi(),
        clock=FakeClock(),
    )

    supervisor.bootstrap()

    assert supervisor.settings.max_running_profiles == 2


def test_persisted_settings_row_is_read_defensively(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    harness.store.set_setting(SETTINGS_KEY, "not json at all")

    harness.supervisor.bootstrap()

    assert harness.supervisor.settings.max_running_profiles == 6


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------
async def test_status_and_helpers_report_the_fleet(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta")]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=1, worker_start_stagger_seconds=0),
    )
    supervisor = harness.supervisor
    await supervisor.start()
    await supervisor.refresh_once()

    status = supervisor.status()
    assert status["running"] is True
    assert status["bootstrapped"] is True
    assert status["profiles_total"] == 2
    assert status["profiles_running"] == 1
    assert status["profiles_healthy"] == 1
    assert status["profiles_queued"] == 1
    assert status["profiles_error"] == 0
    assert status["profiles_blocked"] == 0
    assert status["slots_used"] == 1
    assert status["slots_total"] == 1
    assert status["max_running_profiles"] == 1
    assert status["snapshot_interval_seconds"] == 60
    assert status["kill_switch_engaged"] is False
    assert status["state_dir"] == str(harness.state_dir)
    assert status["state_db"] == str(harness.store.path)
    assert status["started_at"] == format_ts(START_TIME)
    assert status["last_poll_at"] == format_ts(START_TIME)
    assert status["uptime_seconds"] == 0.0

    harness.clock.advance(90)
    assert supervisor.uptime_seconds() == 90.0
    assert supervisor.healthy_count() == 1
    assert supervisor.metrics_for("alpha") == METRICS
    assert supervisor.metrics_for("beta") is None
    assert supervisor.latest_snapshots() == {}
    assert [record.id for record in supervisor.profiles()] == ["alpha", "beta"]


async def test_metrics_are_dropped_when_a_read_fails(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    supervisor.bootstrap()
    assert supervisor.metrics_for("alpha") is None

    await supervisor.refresh_once()
    assert supervisor.metrics_for("alpha") == METRICS

    harness.api.fail_next("alpha", 1)
    await supervisor.refresh_once()

    assert supervisor.metrics_for("alpha") is None


async def test_refresh_once_boots_an_unbootstrapped_supervisor(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])

    await harness.supervisor.refresh_once()

    assert harness.supervisor.is_running("alpha")
    assert harness.supervisor.status()["bootstrapped"] is True


async def test_start_profile_is_idempotent(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    supervisor.bootstrap()
    spawns = len(harness.launcher.calls)

    supervisor.start_profile("alpha")

    assert len(harness.launcher.calls) == spawns
    assert supervisor.is_running("alpha")


async def test_stop_of_a_profile_without_a_worker_still_records_the_reason(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        profiles=[profile_config("alpha")],
        settings=PlatformSettings(max_running_profiles=0),
    )
    supervisor = harness.supervisor
    supervisor.bootstrap()
    assert harness.records()["alpha"].state == "queued"

    supervisor.stop_profile("alpha", reason="maintenance")

    record = harness.records()["alpha"]
    assert record.state == "stopped"
    assert record.state_reason == "maintenance"
    assert harness.kinds().count("stop") == 0


async def test_the_kill_switch_refuses_an_explicit_start(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    supervisor.bootstrap()
    supervisor.engage_kill_switch()
    spawns = len(harness.launcher.calls)

    supervisor.start_profile("alpha")
    supervisor.restart_profile("alpha")

    assert len(harness.launcher.calls) == spawns
    record = harness.records()["alpha"]
    assert record.state == "stopped"
    assert record.state_reason == "kill_switch"


async def test_a_disabled_profile_cannot_be_started(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta", enabled=False)]
    harness = make_harness(tmp_path, profiles=profiles)
    supervisor = harness.supervisor
    supervisor.bootstrap()
    spawns = len(harness.launcher.calls)

    supervisor.start_profile("beta")

    assert len(harness.launcher.calls) == spawns
    assert not supervisor.is_running("beta")
    assert harness.records()["beta"].state_reason == "disabled"


async def test_an_explicit_start_is_not_taken_away_by_the_cap(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta")]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=PlatformSettings(max_running_profiles=1, worker_start_stagger_seconds=0),
    )
    supervisor = harness.supervisor
    supervisor.bootstrap()
    assert supervisor.is_running("alpha")
    assert harness.records()["beta"].state == "queued"

    supervisor.start_profile("beta")
    supervisor.schedule()

    assert supervisor.is_running("beta")
    assert supervisor.is_running("alpha")
    assert harness.records()["beta"].state == "running"


async def test_a_client_that_refuses_to_close_does_not_abort_the_poll(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()
    await supervisor.refresh_once()
    harness.api.close_error = True

    supervisor.stop_profile("alpha")
    await supervisor.refresh_once()

    assert not supervisor.is_running("alpha")
    assert harness.records()["alpha"].state == "stopped"
    assert all(not client.closed for client in harness.api.clients)


def test_schedule_stops_the_fleet_when_the_file_appears_out_of_band(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    supervisor.bootstrap()
    assert supervisor.is_running("alpha")

    (harness.state_dir / KILL_SWITCH_FILENAME).touch()
    supervisor.schedule()

    assert not supervisor.is_running("alpha")
    record = harness.records()["alpha"]
    assert record.state == "stopped"
    assert record.state_reason == "kill_switch"
    stop_events = [event for event in harness.events() if event.kind == "stop"]
    assert [event.level for event in stop_events] == ["warning"]


def test_an_invalid_platform_document_falls_back_to_the_caller_settings(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        profiles=[profile_config("alpha")],
        platform={"max_running_profiles": "not a number"},
        settings=PlatformSettings(max_running_profiles=4, worker_start_stagger_seconds=0),
    )

    harness.supervisor.bootstrap()

    assert harness.supervisor.settings.max_running_profiles == 4


def test_an_unreadable_platform_document_is_ignored(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    document = harness.config_dir / "platform.json"
    document.unlink()
    document.mkdir()

    harness.supervisor.bootstrap()

    assert harness.supervisor.settings.max_running_profiles == 6
    assert harness.supervisor.is_running("alpha")


def test_a_malformed_platform_document_is_ignored(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    (harness.config_dir / "platform.json").write_text("{ not json", encoding="utf-8")

    harness.supervisor.bootstrap()

    assert harness.supervisor.settings.max_running_profiles == 6


def test_the_strategy_path_falls_back_to_the_repository_directory(tmp_path: Path) -> None:
    state_dir = tmp_path / "realtime"
    state_dir.mkdir(parents=True)
    config_dir = tmp_path / "config"
    write_documents(config_dir, [profile_config("alpha")])
    launcher = RecordingLauncher()
    store = StateStore(state_dir / "state.db")
    _OPEN_STORES.append(store)
    supervisor = Supervisor(
        store=store,
        settings=PlatformSettings(),
        state_dir=state_dir,
        config_dir=config_dir,
        launcher=launcher,
        client_factory=FakeFreqtradeApi(),
        env=dict(BASE_ENV),
        clock=FakeClock(),
    )

    supervisor.bootstrap()

    argv = launcher.calls[0].argv
    assert argv[argv.index("--strategy-path") + 1] == str(STRATEGIES_DIR)


# ---------------------------------------------------------------------------
# Gentle fleet boot: the stagger gate
# ---------------------------------------------------------------------------
def test_the_stagger_boots_the_fleet_one_worker_per_interval(tmp_path: Path) -> None:
    """The regression test of the production incident: a cold start is gradual.

    Four eligible profiles and three slots. The candidates are ordered by
    priority descending then id ascending, so the fourth one is ``omega``. The
    first pass spawns exactly one worker; every other candidate is queued with the
    gradual-start reason, because a staggered profile holds no worker yet and so
    consumes no slot; and the gate then admits exactly one more worker per
    ``worker_start_stagger_seconds`` interval -- no more, no less. Only the last
    pass, once the three slots are occupied, gives ``omega`` the fleet-cap reason.
    """
    profiles = [profile_config(name) for name in ("alpha", "beta", "gamma", "omega")]
    harness = make_harness(tmp_path, profiles=profiles, settings=staggered_settings())
    supervisor = harness.supervisor

    supervisor.bootstrap()

    assert harness.spawned_ids() == ["alpha"]
    records = harness.records()
    assert records["alpha"].state == "running"
    for name in ("beta", "gamma"):
        assert records[name].state == "queued"
        assert records[name].state_reason == (
            "queued: starting workers gradually (1 of 3 slots in use)"
        )
    assert records["omega"].state == "queued"
    assert records["omega"].state_reason == (
        "queued: starting workers gradually (1 of 3 slots in use)"
    )
    assert harness.kinds().count("cap_reached") == 0
    # A staggered profile holds no worker, so the pass journals no stop.
    assert "stop" not in harness.kinds()

    harness.clock.advance(10)
    supervisor.schedule()

    assert harness.spawned_ids() == ["alpha", "beta"]
    assert harness.records()["gamma"].state_reason == (
        "queued: starting workers gradually (2 of 3 slots in use)"
    )

    harness.clock.advance(9)
    supervisor.schedule()

    assert harness.spawned_ids() == ["alpha", "beta"]
    assert not supervisor.is_running("gamma")
    assert harness.records()["gamma"].state_reason == (
        "queued: starting workers gradually (2 of 3 slots in use)"
    )

    harness.clock.advance(1)
    supervisor.schedule()

    assert harness.spawned_ids() == ["alpha", "beta", "gamma"]
    assert harness.records()["omega"].state_reason == (
        "queued: fleet cap reached (3 of 3 slots in use)"
    )
    assert harness.kinds().count("cap_reached") == 1


def test_a_zero_stagger_keeps_the_immediate_boot(tmp_path: Path) -> None:
    """``worker_start_stagger_seconds = 0`` is the historical behaviour exactly."""
    profiles = [profile_config(name) for name in ("alpha", "beta", "gamma", "omega")]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=staggered_settings(worker_start_stagger_seconds=0),
    )

    harness.supervisor.bootstrap()

    assert harness.spawned_ids() == ["alpha", "beta", "gamma"]
    records = harness.records()
    assert records["omega"].state == "queued"
    assert records["omega"].state_reason == "queued: fleet cap reached (3 of 3 slots in use)"
    assert all("gradually" not in (record.state_reason or "") for record in records.values())


def test_the_stagger_delays_promotions_without_reordering_them(tmp_path: Path) -> None:
    """The started ids are the priority-DESC/id-ASC prefix, one per pass."""
    profiles = [
        profile_config("c-low", priority=50),
        profile_config("a-low", priority=50),
        profile_config("e-high", priority=100),
        profile_config("b-low", priority=50),
        profile_config("d-high", priority=100),
    ]
    harness = make_harness(tmp_path, profiles=profiles, settings=staggered_settings())
    supervisor = harness.supervisor

    supervisor.bootstrap()
    assert harness.spawned_ids() == ["d-high"]

    harness.clock.advance(10)
    supervisor.schedule()
    assert harness.spawned_ids() == ["d-high", "e-high"]

    harness.clock.advance(10)
    supervisor.schedule()
    assert harness.spawned_ids() == ["d-high", "e-high", "a-low"]
    assert [record.id for record in supervisor.profiles() if record.state == "running"] == [
        "d-high",
        "e-high",
        "a-low",
    ]

    harness.clock.advance(10)
    supervisor.schedule()

    assert harness.spawned_ids() == ["d-high", "e-high", "a-low"]
    assert harness.records()["b-low"].state_reason == (
        "queued: fleet cap reached (3 of 3 slots in use)"
    )


def test_apply_settings_opens_and_re_arms_the_stagger_gate(tmp_path: Path) -> None:
    profiles = [profile_config(name) for name in ("alpha", "beta", "gamma", "omega")]
    harness = make_harness(tmp_path, profiles=profiles, settings=staggered_settings())
    supervisor = harness.supervisor
    supervisor.bootstrap()
    assert harness.spawned_ids() == ["alpha"]

    updated = supervisor.apply_settings(worker_start_stagger_seconds=0)

    assert updated.worker_start_stagger_seconds == 0
    assert supervisor.settings.worker_start_stagger_seconds == 0
    document = json.loads(harness.store.get_setting(SETTINGS_KEY) or "{}")
    assert document == supervisor.settings.model_dump()
    assert document["worker_start_stagger_seconds"] == 0
    assert harness.spawned_ids() == ["alpha", "beta", "gamma"]
    assert "settings_updated" in harness.kinds()

    supervisor.apply_settings(worker_start_stagger_seconds=5)

    assert supervisor.settings.worker_start_stagger_seconds == 5
    assert (
        json.loads(harness.store.get_setting(SETTINGS_KEY) or "{}")["worker_start_stagger_seconds"]
        == 5
    )

    # The gate is armed again: freeing a slot promotes one worker per interval.
    supervisor.stop_profile("gamma")
    supervisor.schedule()

    assert not supervisor.is_running("omega")
    assert harness.records()["omega"].state_reason == (
        "queued: starting workers gradually (2 of 3 slots in use)"
    )

    harness.clock.advance(4)
    supervisor.schedule()
    assert not supervisor.is_running("omega")

    harness.clock.advance(1)
    supervisor.schedule()
    assert supervisor.is_running("omega")


def test_raising_the_cap_still_starts_one_worker_per_pass(tmp_path: Path) -> None:
    """A wider cap does not open the gate: the fleet keeps growing gradually."""
    profiles = [profile_config(name) for name in ("alpha", "bravo", "charlie", "delta", "echo")]
    harness = make_harness(tmp_path, profiles=profiles, settings=staggered_settings())
    supervisor = harness.supervisor
    supervisor.bootstrap()
    assert harness.spawned_ids() == ["alpha"]

    harness.clock.advance(10)
    updated = supervisor.apply_settings(max_running_profiles=5)

    assert updated.max_running_profiles == 5
    assert harness.spawned_ids() == ["alpha", "bravo"]
    assert json.loads(harness.store.get_setting(SETTINGS_KEY) or "{}")["max_running_profiles"] == 5
    records = harness.records()
    for name in ("charlie", "delta", "echo"):
        assert records[name].state == "queued"
        assert records[name].state_reason == (
            "queued: starting workers gradually (2 of 5 slots in use)"
        )


# ---------------------------------------------------------------------------
# The history read pass (the read model of the monitoring pages)
# ---------------------------------------------------------------------------
async def test_history_for_returns_the_rows_of_the_last_successful_read(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta"), profile_config("gamma")]
    harness = make_harness(
        tmp_path,
        profiles=profiles,
        settings=staggered_settings(max_running_profiles=2, worker_start_stagger_seconds=0),
    )
    supervisor = harness.supervisor
    supervisor.bootstrap()
    assert supervisor.history_for("alpha") is None

    await supervisor.refresh_once()

    daily_rows, trade_rows = supervisor.history_for("alpha") or ([], [])
    assert daily_rows == [DAILY_ROW]
    assert set(daily_rows[0]) == set(DAILY_ROW)
    assert set(trade_rows[0]) == set(TRADE_ROW)
    assert trade_rows == [TRADE_ROW, OPEN_TRADE_ROW]
    assert [row["trade_id"] for row in trade_rows] == [11, 11]
    assert trade_rows[-1]["is_open"] is True
    assert harness.api.trade_limits == [HISTORY_TRADE_LIMIT, HISTORY_TRADE_LIMIT]
    assert supervisor.history_for("beta") == ([DAILY_ROW], [TRADE_ROW, OPEN_TRADE_ROW])
    # Beyond the cap: the worker never ran, so there is nothing to read.
    assert supervisor.history_for("gamma") is None
    assert supervisor.history_for("ghost") is None

    supervisor.stop_profile("alpha")

    assert supervisor.history_for("alpha") is None


async def test_a_failing_history_read_never_restarts_a_healthy_worker(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    supervisor.bootstrap()
    await supervisor.refresh_once()
    assert supervisor.history_for("alpha") == ([DAILY_ROW], [TRADE_ROW, OPEN_TRADE_ROW])

    harness.api.fail_history_next("alpha", UNHEALTHY_THRESHOLD)
    for _ in range(UNHEALTHY_THRESHOLD):
        await supervisor.refresh_once()
        assert supervisor.is_running("alpha")
        # ``/balance`` still answers: the worker is healthy, only ``/daily`` fails.
        assert supervisor.metrics_for("alpha") == METRICS
        assert supervisor.history_for("alpha") is None

    record = harness.records()["alpha"]
    assert record.state == "running"
    assert record.state_reason is None
    assert supervisor.healthy_count() == 1
    # The private counter is the exact pin: a history failure counts nothing.
    assert supervisor._health["alpha"].failures == 0
    assert "restart" not in harness.kinds()
    assert "crash" not in harness.kinds()
    assert len(harness.launcher.calls) == 1

    harness.api.fail_history_unexpectedly("alpha")
    await supervisor.refresh_once()

    assert supervisor.is_running("alpha")
    assert supervisor.history_for("alpha") is None
    assert supervisor._health["alpha"].failures == 0

    await supervisor.refresh_once()

    assert supervisor.history_for("alpha") == ([DAILY_ROW], [TRADE_ROW, OPEN_TRADE_ROW])


# ---------------------------------------------------------------------------
# The real launcher (still never a Freqtrade process)
# ---------------------------------------------------------------------------
def test_subprocess_launcher_appends_the_child_output_to_the_log(tmp_path: Path) -> None:
    launcher = SubprocessLauncher()
    log_path = tmp_path / "logs" / "child.log"
    first = launcher.spawn(
        [sys.executable, "-c", "print('hello from the child')"],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=log_path,
    )
    assert first.wait(timeout=30) == 0
    second = launcher.spawn(
        [sys.executable, "-c", "print('second run')"],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=log_path,
    )
    assert second.wait(timeout=30) == 0

    output = log_path.read_text(encoding="utf-8")
    assert "hello from the child" in output
    assert "second run" in output


def test_subprocess_launcher_terminates_then_kills(tmp_path: Path) -> None:
    launcher = SubprocessLauncher()

    cooperative = launcher.spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "cooperative.log",
    )
    launcher.terminate(cooperative, grace_seconds=10.0)
    assert cooperative.poll() == -signal.SIGTERM

    ready = tmp_path / "ready"
    script = (
        "import pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
        "time.sleep(30)\n"
    )
    stubborn = launcher.spawn(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "stubborn.log",
    )
    for _ in range(500):
        if ready.exists():
            break
        time.sleep(0.01)
    assert ready.exists()

    launcher.terminate(stubborn, grace_seconds=0.2)
    assert stubborn.poll() == -signal.SIGKILL

    launcher.terminate(stubborn, grace_seconds=0.2)
    assert stubborn.poll() == -signal.SIGKILL
