"""``POST /api/kill-switch``: stop everything, and start nothing.

Engaging the kill switch is the operator's emergency stop: it must stop every
worker, be visible in the health and settings payloads, and survive being called
twice. Releasing it resumes scheduling without starting anything by itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.api.security import OPERATOR_TOKEN_HEADER
from trading_platform.config import ENV_OPERATOR_TOKEN, PlatformSettings
from trading_platform.models import ProfileConfig, StrategyMeta
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

    def apply_settings(
        self,
        *,
        max_running_profiles: int | None = None,
        snapshot_interval_seconds: int | None = None,
        worker_start_stagger_seconds: int | None = None,
    ) -> PlatformSettings:
        changes: dict[str, int] = {}
        if max_running_profiles is not None:
            changes["max_running_profiles"] = int(max_running_profiles)
        if snapshot_interval_seconds is not None:
            changes["snapshot_interval_seconds"] = int(snapshot_interval_seconds)
        if worker_start_stagger_seconds is not None:
            changes["worker_start_stagger_seconds"] = int(worker_start_stagger_seconds)
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
