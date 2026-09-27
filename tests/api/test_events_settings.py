"""``GET /api/events`` and ``/api/settings``: the journal and the operator settings.

The journal is the only place the engine explains itself, so its ordering
(newest first) and its ``limit``/``since`` handling are pinned here. The settings
routes are the operator's control panel: the reading route reports what the
engine acts on, and the mutating route persists a change and reschedules the
fleet through the supervisor.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.api.security import OPERATOR_TOKEN_HEADER
from trading_platform.config import (
    ENV_ALLOW_LIVE_TRADING,
    ENV_OPERATOR_TOKEN,
    LIVE_TRADING_CONFIRMATION,
    PlatformSettings,
)
from trading_platform.engine.supervisor import SETTINGS_KEY
from trading_platform.models import ProfileConfig, StrategyMeta, format_ts, utc_now
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
        # The real supervisor persists the whole document under SETTINGS_KEY.
        self.store.set_setting(SETTINGS_KEY, json.dumps(self.settings.model_dump()))
        self.calls.append(("settings", str(sorted(changes))))
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
    settings: PlatformSettings | None = None,
    kill_switch: bool = False,
) -> tuple[TestClient, _StubSupervisor, StateStore]:
    """Build a client over a real SQLite store and a stubbed process layer."""
    store = StateStore(tmp_path / "state.db")
    store.bootstrap()
    for profile in profiles:
        store.upsert_profile(profile, source=(sources or {}).get(profile.id, "catalogue"))
    resolved = PlatformSettings() if settings is None else settings
    supervisor = _StubSupervisor(store, resolved, kill_switch=kill_switch)
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
    monkeypatch.setenv(ENV_OPERATOR_TOKEN, TOKEN)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def _journal(tmp_path: Path) -> tuple[TestClient, StateStore]:
    client, _supervisor, store = _make_engine(tmp_path, profiles=[_profile("alpha")])
    for index in range(3):
        store.record_event("info", "start", f"event {index}", profile_id="alpha")
    return client, store


def test_events_are_newest_first(tmp_path: Path) -> None:
    client, store = _journal(tmp_path)

    payload = client.get("/api/events").json()

    assert [event["message"] for event in payload["events"]] == [
        "event 2",
        "event 1",
        "event 0",
    ]
    assert payload["events"][0]["profile_id"] == "alpha"
    assert payload["events"][0]["level"] == "info"
    assert payload["events"][0]["kind"] == "start"
    stored = store.list_events()
    assert payload["events"][0]["id"] == stored[0].id
    assert payload["events"][0]["ts"] == stored[0].ts


def test_events_limit_truncates_the_journal(tmp_path: Path) -> None:
    client, _store = _journal(tmp_path)

    payload = client.get("/api/events?limit=1").json()

    assert [event["message"] for event in payload["events"]] == ["event 2"]


def test_events_since_filters_the_journal(tmp_path: Path) -> None:
    client, _store = _journal(tmp_path)

    past = format_ts(utc_now() - timedelta(days=1))
    future = format_ts(utc_now() + timedelta(days=1))

    assert len(client.get(f"/api/events?since={past}").json()["events"]) == 3
    assert client.get(f"/api/events?since={future}").json()["events"] == []


def test_events_with_a_blank_since_returns_everything(tmp_path: Path) -> None:
    """``since=`` is what an empty query parameter means, not a filter."""
    client, _store = _journal(tmp_path)

    assert len(client.get("/api/events?since=").json()["events"]) == 3


def test_events_of_an_empty_journal_are_an_empty_list(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    assert client.get("/api/events").json() == {"events": []}


def test_events_need_no_operator_token(tmp_path: Path) -> None:
    client, _store = _journal(tmp_path)

    assert client.get("/api/events").status_code == 200


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def test_settings_describe_the_engine(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(
        tmp_path,
        profiles=[_profile("declared"), _profile("mine")],
        sources={"mine": "operator"},
        settings=PlatformSettings(
            max_running_profiles=7,
            snapshot_interval_seconds=45,
            worker_start_stagger_seconds=3,
        ),
        kill_switch=True,
    )

    payload = client.get("/api/settings").json()

    assert payload["max_running_profiles"] == 7
    assert payload["snapshot_interval_seconds"] == 45
    assert payload["worker_start_stagger_seconds"] == 3
    assert payload["kill_switch_engaged"] is True
    assert payload["catalogue_profile_count"] == 1
    assert payload["operator_profile_count"] == 1
    assert payload["state_db_path"] == str(store.path)
    assert payload["version"]


def test_settings_publish_the_default_worker_start_stagger(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    payload = client.get("/api/settings").json()

    assert payload["worker_start_stagger_seconds"] == 10
    assert payload["max_running_profiles"] == 6


def test_settings_report_the_live_trading_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    monkeypatch.delenv(ENV_ALLOW_LIVE_TRADING, raising=False)
    assert client.get("/api/settings").json()["allow_live_trading"] is False

    monkeypatch.setenv(ENV_ALLOW_LIVE_TRADING, LIVE_TRADING_CONFIRMATION)
    assert client.get("/api/settings").json()["allow_live_trading"] is True


def test_settings_update_is_persisted_and_returned(tmp_path: Path) -> None:
    client, supervisor, _store = _make_engine(
        tmp_path, settings=PlatformSettings(max_running_profiles=2, snapshot_interval_seconds=60)
    )

    response = client.post(
        "/api/settings",
        json={"max_running_profiles": 9, "snapshot_interval_seconds": 30},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["max_running_profiles"] == 9
    assert payload["snapshot_interval_seconds"] == 30
    assert supervisor.settings.max_running_profiles == 9
    # The engine acted on it, so the reading route reports the new value too.
    assert client.get("/api/settings").json()["max_running_profiles"] == 9
    assert client.get("/api/health").json()["engine_slots_total"] == 9


def test_settings_update_accepts_a_partial_body(tmp_path: Path) -> None:
    client, supervisor, _store = _make_engine(
        tmp_path, settings=PlatformSettings(max_running_profiles=2, snapshot_interval_seconds=60)
    )

    client.post(
        "/api/settings",
        json={"snapshot_interval_seconds": 15},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert supervisor.settings.snapshot_interval_seconds == 15
    assert supervisor.settings.max_running_profiles == 2


def test_settings_update_persists_and_republishes_the_worker_start_stagger(
    tmp_path: Path,
) -> None:
    client, supervisor, store = _make_engine(
        tmp_path,
        settings=PlatformSettings(
            max_running_profiles=4,
            snapshot_interval_seconds=45,
            worker_start_stagger_seconds=10,
        ),
    )

    response = client.post(
        "/api/settings",
        json={"worker_start_stagger_seconds": 25},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    assert response.json()["worker_start_stagger_seconds"] == 25
    assert supervisor.settings.worker_start_stagger_seconds == 25
    stored = json.loads(store.get_setting(SETTINGS_KEY) or "{}")
    assert stored["worker_start_stagger_seconds"] == 25
    # The fields the body did not mention are left alone.
    assert stored["max_running_profiles"] == 4
    assert stored["snapshot_interval_seconds"] == 45
    published = client.get("/api/settings").json()
    assert published["worker_start_stagger_seconds"] == 25
    assert published["max_running_profiles"] == 4
    assert published["snapshot_interval_seconds"] == 45


def test_settings_update_accepts_a_zero_worker_start_stagger(tmp_path: Path) -> None:
    client, supervisor, store = _make_engine(tmp_path)
    assert client.get("/api/settings").json()["worker_start_stagger_seconds"] == 10

    response = client.post(
        "/api/settings",
        json={"worker_start_stagger_seconds": 0},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    assert response.json()["worker_start_stagger_seconds"] == 0
    assert supervisor.settings.worker_start_stagger_seconds == 0
    stored = json.loads(store.get_setting(SETTINGS_KEY) or "{}")
    assert stored["worker_start_stagger_seconds"] == 0
    assert stored["max_running_profiles"] == 6
    assert stored["snapshot_interval_seconds"] == 60
    assert client.get("/api/settings").json()["worker_start_stagger_seconds"] == 0


def test_settings_update_refuses_a_bad_type(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    response = client.post(
        "/api/settings",
        json={"max_running_profiles": "many"},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 422
