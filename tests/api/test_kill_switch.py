"""``POST /api/kill-switch``: stop everything, and start nothing.

Engaging the kill switch is the operator's emergency stop: it must stop every
worker, be visible in the health and settings payloads, and survive being called
twice. Releasing it resumes scheduling without starting anything by itself.

The emergency stop itself is asserted against the real engine and a recording
launcher: the claim is not only that the route reaches the supervisor, but that
the supervisor is then holding no worker at all -- an untracked child would keep
trading where the kill switch could never reach it.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.api.security import OPERATOR_TOKEN_HEADER
from trading_platform.config import ENV_OPERATOR_TOKEN, PlatformSettings
from trading_platform.engine.supervisor import Supervisor
from trading_platform.models import ProfileConfig, StrategyMeta, utc_now
from trading_platform.profiles.catalogue import StrategyCatalogue
from trading_platform.profiles.store import StateStore

TOKEN = "operator-token-of-the-test"


class _StubSupervisor:
    """Engine double: the state store is real, the process layer is not.

    ``engage_kill_switch``/``release_kill_switch`` mirror the real engine: engaging
    drops every live worker, releasing does not start one.
    """

    def __init__(
        self,
        store: StateStore,
        settings: PlatformSettings,
        *,
        alive: Iterable[str] = (),
    ) -> None:
        self.store = store
        self.settings = settings
        self.alive = set(alive)
        self.kill_switch = False
        self.calls: list[tuple[str, str]] = []

    def is_running(self, profile_id: str) -> bool:
        return profile_id in self.alive

    def healthy_count(self) -> int:
        return len(self.alive)

    def kill_switch_engaged(self) -> bool:
        return self.kill_switch

    def engage_kill_switch(self) -> None:
        self.kill_switch = True
        self.alive.clear()
        self.calls.append(("engage", ""))

    def release_kill_switch(self) -> None:
        self.kill_switch = False
        self.calls.append(("release", ""))

    def start_profile(self, profile_id: str, *_args: Any) -> None:
        self.alive.add(profile_id)
        self.calls.append(("start", profile_id))

    def stop_profile(self, profile_id: str, *_args: Any) -> None:
        self.alive.discard(profile_id)
        self.calls.append(("stop", profile_id))

    def restart_profile(self, profile_id: str) -> None:
        self.alive.add(profile_id)
        self.calls.append(("restart", profile_id))

    def apply_settings(self, *, snapshot_interval_seconds: int | None = None) -> PlatformSettings:
        changes: dict[str, int] = {}
        if snapshot_interval_seconds is not None:
            changes["snapshot_interval_seconds"] = int(snapshot_interval_seconds)
        self.settings = self.settings.with_overrides(**changes)
        return self.settings

    def schedule(self) -> None:
        self.calls.append(("schedule", ""))


def _catalogue() -> StrategyCatalogue:
    return StrategyCatalogue(
        [
            StrategyMeta(
                id="basic",
                class_name="BasicStrategy",
                title="EMA cross baseline",
                category="baseline",
            )
        ]
    )


def _profile(profile_id: str, **overrides: Any) -> ProfileConfig:
    fields: dict[str, Any] = {
        "id": profile_id,
        "name": profile_id.title(),
        "strategy": "basic",
        "timeframe": "1h",
        "pairs": ["BTC/USDT"],
        "initial_capital": 1000.0,
        "priority": 100,
    }
    fields.update(overrides)
    return ProfileConfig(**fields)


def _make_engine(
    tmp_path: Path,
    *,
    profiles: Sequence[ProfileConfig] = (),
    states: Mapping[str, str] | None = None,
    alive: Iterable[str] = (),
) -> tuple[TestClient, _StubSupervisor, StateStore]:
    """Build a client over a real SQLite store and a stubbed process layer."""
    store = StateStore(tmp_path / "state.db")
    store.bootstrap()
    for profile in profiles:
        store.upsert_profile(profile, source="catalogue")
    for profile_id, state in (states or {}).items():
        store.set_profile_state(profile_id, state)
    settings = PlatformSettings()
    supervisor = _StubSupervisor(store, settings, alive=alive)
    app = create_app(
        supervisor=supervisor,
        settings=settings,
        state_dir=tmp_path,
        start_engine=False,
    )
    app.state.strategy_catalogue = _catalogue()
    return TestClient(app), supervisor, store


def _running_fleet(tmp_path: Path) -> tuple[TestClient, _StubSupervisor, StateStore]:
    return _make_engine(
        tmp_path,
        profiles=[_profile("alpha"), _profile("beta")],
        states={"alpha": "running", "beta": "running"},
        alive=["alpha", "beta"],
    )


# ---------------------------------------------------------------------------
# A real engine, for the emergency stop that is supposed to kill every child
# ---------------------------------------------------------------------------
#: The strategy metadata document of the temporary deployments built below.
LIVE_STRATEGIES_DOCUMENT: dict[str, Any] = {
    "strategies": [
        {"id": "basic", "class_name": "BasicStrategy", "file": "BasicStrategy.py"},
    ]
}

#: Environment of those deployments: the Freqtrade binary is pinned, so the
#: recorded command line does not depend on the machine running the suite.
LIVE_ENV: dict[str, str] = {"TB_FREQTRADE_BIN": sys.executable}


class _FakeProcess:
    """A child handle: a pid, ``poll``, ``terminate``, ``kill`` and ``wait``."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        """Return the exit status, or ``None`` while the child is alive."""
        return self.returncode

    def terminate(self) -> None:
        """Record SIGTERM; the fake child exits with Freqtrade's status 130."""
        if self.returncode is None:
            self.returncode = 130

    def kill(self) -> None:
        """Record SIGKILL."""
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        """Return the exit status without ever blocking."""
        return 0 if self.returncode is None else self.returncode


class _RecordingLauncher:
    """A ``ProcessLauncher`` double that records the children it would start."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.processes: list[_FakeProcess] = []
        self.terminated: list[_FakeProcess] = []
        self._pid = 8000

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
    ) -> _FakeProcess:
        """Record the command line and return a fresh fake child."""
        self._pid += 1
        process = _FakeProcess(self._pid)
        self.calls.append(list(argv))
        self.processes.append(process)
        return process

    def terminate(self, process: _FakeProcess, *, grace_seconds: float = 0.0) -> None:
        """Record the stop and terminate the fake child."""
        self.terminated.append(process)
        process.terminate()

    def running(self) -> list[_FakeProcess]:
        """Return the children that are still alive."""
        return [process for process in self.processes if process.poll() is None]


def _make_live_engine(
    tmp_path: Path,
    *,
    profiles: Sequence[ProfileConfig] = (),
) -> tuple[TestClient, Supervisor, StateStore, _RecordingLauncher]:
    """Build a client over a real engine whose children are only recorded.

    Nothing is started for real: the launcher records, and the port probe answers
    "free" instead of binding a socket. The supervisor is the production one, so
    the emergency stop really walks the process table it holds.
    """
    state_dir = tmp_path / "realtime"
    state_dir.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "platform.json").write_text("{}", encoding="utf-8")
    (config_dir / "profiles.json").write_text(json.dumps({"profiles": []}), encoding="utf-8")
    (config_dir / "strategies.json").write_text(
        json.dumps(LIVE_STRATEGIES_DOCUMENT), encoding="utf-8"
    )
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)

    store = StateStore(state_dir / "state.db")
    store.bootstrap()
    for profile in profiles:
        store.upsert_profile(profile, source="catalogue", state="stopped")
    settings = PlatformSettings()
    launcher = _RecordingLauncher()
    supervisor = Supervisor(
        store=store,
        settings=settings,
        state_dir=state_dir,
        config_dir=config_dir,
        strategies_dir=strategies_dir,
        launcher=launcher,
        env=dict(LIVE_ENV),
        clock=utc_now,
        port_probe=lambda port: True,
    )
    app = create_app(
        supervisor=supervisor,
        settings=settings,
        state_dir=state_dir,
        start_engine=False,
    )
    app.state.strategy_catalogue = _catalogue()
    supervisor.bootstrap()
    return TestClient(app), supervisor, store, launcher


@pytest.fixture(autouse=True)
def _configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OPERATOR_TOKEN, TOKEN)


def test_engaging_the_kill_switch_stops_the_fleet(tmp_path: Path) -> None:
    client, supervisor, _store = _running_fleet(tmp_path)

    response = client.post(
        "/api/kill-switch",
        json={"engaged": True},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"kill_switch_engaged": True}
    assert supervisor.calls == [("engage", "")]
    assert supervisor.alive == set()


def test_engaging_the_kill_switch_stops_every_child_of_the_real_engine(tmp_path: Path) -> None:
    """The emergency stop leaves no worker behind, tracked or not.

    Every child the fleet started is terminated and the supervisor holds no
    process any more, so a later scheduling pass cannot be walked past a worker
    the switch was supposed to stop -- and no child is left trading off the
    books.
    """
    client, supervisor, store, launcher = _make_live_engine(
        tmp_path,
        profiles=[_profile("alpha"), _profile("beta")],
    )
    assert len(launcher.calls) == 2
    assert len(supervisor._processes) == 2

    response = client.post(
        "/api/kill-switch",
        json={"engaged": True},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"kill_switch_engaged": True}
    assert launcher.terminated == launcher.processes
    assert launcher.running() == []
    assert supervisor._processes == {}
    records = {record.id: record for record in store.list_profiles()}
    assert {record.state for record in records.values()} == {"stopped"}
    assert {record.state_reason for record in records.values()} == {"kill_switch"}

    # Scheduling stays a no-op while the switch is engaged.
    supervisor.schedule()
    assert len(launcher.calls) == 2
    assert launcher.running() == []


def test_an_engaged_kill_switch_is_visible_in_the_other_payloads(tmp_path: Path) -> None:
    client, _supervisor, _store = _running_fleet(tmp_path)

    client.post(
        "/api/kill-switch",
        json={"engaged": True},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert client.get("/api/health").json()["kill_switch_engaged"] is True
    assert client.get("/api/health").json()["status"] == "degraded"
    assert client.get("/api/settings").json()["kill_switch_engaged"] is True


def test_releasing_the_kill_switch_resumes_scheduling(tmp_path: Path) -> None:
    client, supervisor, _store = _running_fleet(tmp_path)
    client.post(
        "/api/kill-switch",
        json={"engaged": True},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    response = client.post(
        "/api/kill-switch",
        json={"engaged": False},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"kill_switch_engaged": False}
    assert ("release", "") in supervisor.calls
    assert client.get("/api/settings").json()["kill_switch_engaged"] is False


def test_engaging_twice_is_idempotent(tmp_path: Path) -> None:
    client, supervisor, _store = _running_fleet(tmp_path)

    first = client.post(
        "/api/kill-switch", json={"engaged": True}, headers={OPERATOR_TOKEN_HEADER: TOKEN}
    )
    second = client.post(
        "/api/kill-switch", json={"engaged": True}, headers={OPERATOR_TOKEN_HEADER: TOKEN}
    )

    assert first.json() == second.json() == {"kill_switch_engaged": True}
    assert supervisor.calls.count(("engage", "")) == 2


def test_kill_switch_requires_the_operator_token(tmp_path: Path) -> None:
    client, supervisor, _store = _running_fleet(tmp_path)

    refused = client.post("/api/kill-switch", json={"engaged": True})
    wrong = client.post(
        "/api/kill-switch",
        json={"engaged": True},
        headers={OPERATOR_TOKEN_HEADER: "not-the-token"},
    )

    assert refused.status_code == 401
    assert wrong.status_code == 403
    assert supervisor.calls == []


def test_kill_switch_requires_a_boolean_flag(tmp_path: Path) -> None:
    client, supervisor, _store = _running_fleet(tmp_path)

    missing = client.post("/api/kill-switch", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN})
    unparsable = client.post(
        "/api/kill-switch",
        json={"engaged": "perhaps"},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert missing.status_code == 422
    assert unparsable.status_code == 422
    assert supervisor.calls == []
