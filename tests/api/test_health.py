"""``GET /api/health``: the liveness contract the deployment asserts on.

The deploy pipeline (``.github/workflows/deploy.yml``) greps exactly two things
out of this payload: ``status == "ok"`` and ``profiles_running > 0``. Both are
pinned here on a seeded state store, so a change that turns the counter into a
boolean or counts stored rows instead of living workers fails before it reaches
a deployment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_platform.api import app as app_module
from trading_platform.api.app import create_app
from trading_platform.config import PlatformSettings
from trading_platform.engine.supervisor import Supervisor
from trading_platform.models import (
    ProfileConfig,
    ProfileSnapshot,
    StrategyMeta,
    format_ts,
)
from trading_platform.profiles.catalogue import StrategyCatalogue
from trading_platform.profiles.store import StateStore


class _StubSupervisor:
    """Engine double: the state store is real, the process layer is not.

    ``alive`` is the ground truth of "a worker process exists right now", which
    is the only thing ``GET /api/health`` counts.
    """

    def __init__(
        self,
        store: StateStore,
        settings: PlatformSettings,
        *,
        alive: Iterable[str] = (),
        kill_switch: bool = False,
    ) -> None:
        self.store = store
        self.settings = settings
        self.alive = set(alive)
        self.kill_switch = kill_switch
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

    def apply_settings(
        self,
        *,
        max_running_profiles: int | None = None,
        snapshot_interval_seconds: int | None = None,
    ) -> PlatformSettings:
        changes: dict[str, int] = {}
        if max_running_profiles is not None:
            changes["max_running_profiles"] = int(max_running_profiles)
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
            ),
            StrategyMeta(
                id="momentum",
                class_name="MomentumStrategy",
                title="Momentum breakout",
                category="trend",
            ),
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
    snapshots: Sequence[ProfileSnapshot] = (),
    settings: PlatformSettings | None = None,
) -> tuple[TestClient, _StubSupervisor, StateStore]:
    """Build a client over a real SQLite store and a stubbed process layer."""
    store = StateStore(tmp_path / "state.db")
    store.bootstrap()
    for profile in profiles:
        store.upsert_profile(profile, source="catalogue")
    for profile_id, state in (states or {}).items():
        store.set_profile_state(profile_id, state)
    for snapshot in snapshots:
        store.record_snapshot(snapshot)
    resolved = PlatformSettings() if settings is None else settings
    supervisor = _StubSupervisor(store, resolved, alive=alive)
    app = create_app(
        supervisor=supervisor,
        settings=resolved,
        state_dir=tmp_path,
        start_engine=False,
    )
    app.state.strategy_catalogue = _catalogue()
    return TestClient(app), supervisor, store


def test_health_payload_satisfies_the_deploy_smoke_contract(tmp_path: Path) -> None:
    """The exact assertions of the deploy pipeline, on a seeded store."""
    client, _supervisor, _store = _make_engine(
        tmp_path,
        profiles=[_profile("running-one"), _profile("idle-one")],
        states={"running-one": "running", "idle-one": "stopped"},
        alive=["running-one"],
    )

    response = client.get("/api/health")

    assert response.status_code == 200
    health = response.json()
    assert health["status"] == "ok"
    assert isinstance(health["profiles_running"], int)
    assert not isinstance(health["profiles_running"], bool)
    assert health["profiles_running"] > 0


def test_health_is_degraded_when_no_worker_is_alive(tmp_path: Path) -> None:
    """A stored ``running`` state is not evidence: the worker process is."""
    client, _supervisor, _store = _make_engine(
        tmp_path,
        profiles=[_profile("crashed")],
        states={"crashed": "running"},
        alive=[],
    )

    health = client.get("/api/health").json()

    assert health["status"] == "degraded"
    assert health["profiles_running"] == 0
    assert health["profiles_total"] == 1


def test_health_counts_modes_slots_and_the_kill_switch(tmp_path: Path) -> None:
    """Every documented counter is filled from the store and the settings."""
    settings = PlatformSettings(max_running_profiles=5, snapshot_interval_seconds=30)
    client, _supervisor, _store = _make_engine(
        tmp_path,
        profiles=[
            _profile("paper-running"),
            _profile("paper-queued"),
            _profile("live-running", mode="live"),
        ],
        states={
            "paper-running": "running",
            "paper-queued": "queued",
            "live-running": "running",
        },
        alive=["paper-running", "live-running"],
        settings=settings,
    )

    health = client.get("/api/health").json()

    assert health["status"] == "ok"
    assert health["profiles_running"] == 2
    assert health["profiles_paper"] == 2
    assert health["profiles_running_paper"] == 1
    assert health["profiles_live"] == 1
    assert health["profiles_running_live"] == 1
    assert health["profiles_queued"] == 1
    assert health["profiles_healthy"] == 2
    assert health["engine_slots_used"] == 2
    assert health["engine_slots_total"] == 5
    assert health["kill_switch_engaged"] is False
    assert health["version"]
    assert health["uptime_seconds"] >= 0.0
    assert health["generated_at"]


def test_health_reports_the_kill_switch_and_no_running_profile(tmp_path: Path) -> None:
    """An engaged kill switch stops every worker, so the API reports degraded."""
    client, supervisor, _store = _make_engine(
        tmp_path,
        profiles=[_profile("only")],
        states={"only": "stopped"},
        alive=["only"],
    )
    supervisor.engage_kill_switch()

    health = client.get("/api/health").json()

    assert health["kill_switch_engaged"] is True
    assert health["status"] == "degraded"
    assert health["profiles_running"] == 0


def test_health_on_an_empty_engine_is_degraded(tmp_path: Path) -> None:
    """A fresh deployment has nothing running yet: the API must say so."""
    client, _supervisor, _store = _make_engine(tmp_path)

    health = client.get("/api/health").json()

    assert health["status"] == "degraded"
    assert health["profiles_running"] == 0
    assert health["profiles_total"] == 0
    assert health["engine_slots_total"] == PlatformSettings().max_running_profiles


def test_health_needs_no_operator_token(tmp_path: Path) -> None:
    """The container healthcheck and the deploy smoke test are unauthenticated."""
    client, _supervisor, _store = _make_engine(
        tmp_path, profiles=[_profile("only")], alive=["only"]
    )

    assert client.get("/api/health").status_code == 200


def test_health_generated_at_is_a_canonical_timestamp(tmp_path: Path) -> None:
    """Every timestamp of the API uses the platform format."""
    client, _supervisor, _store = _make_engine(tmp_path)

    health = client.get("/api/health").json()

    assert health["generated_at"] == format_ts()


@pytest.mark.parametrize("alive", [[], ["one"]])
def test_health_status_matches_the_alive_count(tmp_path: Path, alive: list[str]) -> None:
    """``ok`` if and only if at least one worker is alive."""
    client, _supervisor, _store = _make_engine(
        tmp_path, profiles=[_profile("one"), _profile("two")], alive=alive
    )

    health = client.get("/api/health").json()

    assert (health["status"] == "ok") is bool(alive)
    assert health["profiles_running"] == len(alive)


# ---------------------------------------------------------------------------
# The application lifecycle
# ---------------------------------------------------------------------------
def test_the_lifespan_boots_and_stops_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start_engine=True`` makes the API process own the engine lifecycle."""
    events: list[str] = []

    class _Poller:
        def __init__(self, supervisor: Any) -> None:
            self.supervisor = supervisor
            events.append("poller")

        async def run_forever(self) -> None:
            events.append("polling")
            await asyncio.sleep(30)

        def stop(self) -> None:
            events.append("poller-stopped")

    monkeypatch.setattr(app_module, "SnapshotPoller", _Poller)
    store = StateStore(tmp_path / "state.db")
    store.bootstrap()
    settings = PlatformSettings(max_running_profiles=3)

    class _LifecycleSupervisor(_StubSupervisor):
        def bootstrap(self) -> None:
            events.append("bootstrap")

        async def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

    supervisor = _LifecycleSupervisor(store, settings, alive=["only"])
    store.upsert_profile(_profile("only"), source="catalogue")
    app = create_app(
        supervisor=supervisor,
        settings=settings,
        state_dir=tmp_path,
        start_engine=True,
    )

    with TestClient(app) as client:
        assert client.get("/api/health").json()["engine_slots_total"] == 3

    assert events[:2] == ["bootstrap", "start"]
    assert "poller" in events
    assert events[-2:] == ["poller-stopped", "stop"]


def test_a_disabled_lifespan_leaves_the_engine_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start_engine=False`` is what the CLI and every test use."""
    events: list[str] = []

    class _LifecycleSupervisor(_StubSupervisor):
        def bootstrap(self) -> None:
            events.append("bootstrap")

        async def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

    store = StateStore(tmp_path / "state.db")
    store.bootstrap()
    settings = PlatformSettings()
    app = create_app(
        supervisor=_LifecycleSupervisor(store, settings),
        settings=settings,
        state_dir=tmp_path,
        start_engine=False,
    )

    with TestClient(app):
        assert events == []

    assert events == []


def test_create_app_builds_an_engine_over_the_resolved_state_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a supervisor, the application builds a real one -- without booting it."""
    state_db = tmp_path / "state.db"
    monkeypatch.setattr(app_module, "resolve_state_db_path", lambda: state_db)
    settings = PlatformSettings()

    app = create_app(settings=settings, state_dir=tmp_path, start_engine=False)

    assert isinstance(app.state.supervisor, Supervisor)
    assert app.state.supervisor.store.path == state_db
    assert app.state.supervisor.settings is settings
    assert app.state.settings is settings
    assert app.state.state_dir == tmp_path
    assert app.state.start_engine is False
    assert not state_db.exists()


def test_health_survives_a_missing_start_timestamp(tmp_path: Path) -> None:
    """A process without a usable start time still answers a health payload."""
    client, _supervisor, _store = _make_engine(tmp_path)
    client.app.state.started_at = "not-a-timestamp"

    health = client.get("/api/health").json()

    assert health["uptime_seconds"] == 0.0
