"""The operator token: who may mutate, who may only read.

Three refusals are pinned here -- missing header (``401``), wrong header
(``403``) and unconfigured server token (``403``) -- plus the fact that every
route of the documented surface is either public on purpose or guarded, with no
third possibility.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.api.security import (
    INVALID_TOKEN_DETAIL,
    MISSING_TOKEN_DETAIL,
    OPERATOR_TOKEN_HEADER,
    UNCONFIGURED_TOKEN_DETAIL,
)
from trading_platform.config import ENV_OPERATOR_TOKEN, PlatformSettings
from trading_platform.models import ProfileConfig, ProfileSnapshot, StrategyMeta
from trading_platform.profiles.catalogue import StrategyCatalogue
from trading_platform.profiles.store import StateStore

TOKEN = "operator-token-of-the-test"


class _StubSupervisor:
    """Engine double: the state store is real, the process layer is not."""

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
    sources: Mapping[str, str] | None = None,
    snapshots: Sequence[ProfileSnapshot] = (),
    settings: PlatformSettings | None = None,
) -> tuple[TestClient, _StubSupervisor, StateStore]:
    """Build a client over a real SQLite store and a stubbed process layer."""
    store = StateStore(tmp_path / "state.db")
    store.bootstrap()
    for profile in profiles:
        store.upsert_profile(profile, source=(sources or {}).get(profile.id, "catalogue"))
    for snapshot in snapshots:
        store.record_snapshot(snapshot)
    resolved = PlatformSettings() if settings is None else settings
    supervisor = _StubSupervisor(store, resolved, alive=[profile.id for profile in profiles])
    app = create_app(
        supervisor=supervisor,
        settings=resolved,
        state_dir=tmp_path,
        start_engine=False,
    )
    app.state.strategy_catalogue = _catalogue()
    return TestClient(app), supervisor, store


@pytest.fixture(autouse=True)
def _configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from a configured operator token."""
    monkeypatch.setenv(ENV_OPERATOR_TOKEN, TOKEN)


# ---------------------------------------------------------------------------
# The three refusals
# ---------------------------------------------------------------------------
def test_missing_header_is_refused_with_401(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    response = client.post("/api/kill-switch", json={"engaged": True})

    assert response.status_code == 401
    assert response.json() == {"detail": MISSING_TOKEN_DETAIL}


def test_wrong_token_is_refused_with_403(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    response = client.post(
        "/api/kill-switch",
        json={"engaged": True},
        headers={OPERATOR_TOKEN_HEADER: "not-the-token"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": INVALID_TOKEN_DETAIL}


def test_unconfigured_server_token_refuses_every_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset ``TB_OPERATOR_TOKEN`` closes the door, header or not."""
    monkeypatch.delenv(ENV_OPERATOR_TOKEN, raising=False)
    client, _supervisor, _store = _make_engine(tmp_path)

    with_header = client.post(
        "/api/kill-switch",
        json={"engaged": True},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )
    without_header = client.post("/api/kill-switch", json={"engaged": True})

    assert with_header.status_code == 403
    assert with_header.json() == {"detail": UNCONFIGURED_TOKEN_DETAIL}
    assert without_header.status_code == 403
    assert without_header.json() == {"detail": UNCONFIGURED_TOKEN_DETAIL}


def test_empty_server_token_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OPERATOR_TOKEN, "")
    client, _supervisor, _store = _make_engine(tmp_path)

    response = client.post(
        "/api/settings",
        json={"max_running_profiles": 3},
        headers={OPERATOR_TOKEN_HEADER: ""},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": UNCONFIGURED_TOKEN_DETAIL}


def test_the_valid_token_is_accepted(tmp_path: Path) -> None:
    client, supervisor, _store = _make_engine(tmp_path)

    response = client.post(
        "/api/kill-switch",
        json={"engaged": True},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    assert ("engage", "") in supervisor.calls


# ---------------------------------------------------------------------------
# The guarded surface and the public surface
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "post",
            "/api/profiles",
            {
                "name": "n",
                "strategy": "basic",
                "timeframe": "1h",
                "mode": "paper",
                "pairs": ["BTC/USDT"],
                "initial_capital": 10.0,
            },
        ),
        ("patch", "/api/profiles/known", {"name": "renamed"}),
        ("delete", "/api/profiles/known", None),
        ("post", "/api/profiles/known/actions", {"action": "start"}),
        ("post", "/api/catalogue/apply", {"prune": False}),
        ("post", "/api/kill-switch", {"engaged": False}),
        ("post", "/api/settings", {"max_running_profiles": 2}),
    ],
)
def test_every_mutating_route_requires_the_token(
    tmp_path: Path, method: str, path: str, body: dict[str, Any] | None
) -> None:
    client, _supervisor, _store = _make_engine(tmp_path, profiles=[_profile("known")])

    response = client.request(method.upper(), path, json=body)

    assert response.status_code == 401
    assert response.json() == {"detail": MISSING_TOKEN_DETAIL}


@pytest.mark.parametrize(
    "path",
    [
        "/api/health",
        "/api/account",
        "/api/profiles",
        "/api/profiles/known",
        "/api/strategies",
        "/api/events",
        "/api/settings",
    ],
)
def test_reading_routes_need_no_token(tmp_path: Path, path: str) -> None:
    """The dashboard reads through its same-origin rewrite, with no credential."""
    client, _supervisor, _store = _make_engine(tmp_path, profiles=[_profile("known")])

    assert client.get(path).status_code == 200


def test_security_constants_match_the_documented_contract() -> None:
    assert OPERATOR_TOKEN_HEADER == "X-Operator-Token"
    assert MISSING_TOKEN_DETAIL == "missing operator token"
    assert INVALID_TOKEN_DETAIL == "invalid operator token"
    assert UNCONFIGURED_TOKEN_DETAIL == "operator token is not configured"
