"""``GET /api/strategies``: the discovered catalogue and its live performance.

The strategy payload is the join of two sources -- the catalogue metadata of
``config/strategies.json`` (plus the strategy files on disk) and the profiles that
use each strategy -- so both halves are pinned here, including a strategy nobody
runs yet.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.config import PlatformSettings
from trading_platform.models import ProfileConfig, ProfileSnapshot, StrategyMeta, format_ts
from trading_platform.profiles.catalogue import StrategyCatalogue
from trading_platform.profiles.store import StateStore


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
    ) -> PlatformSettings:
        return self.settings

    def schedule(self) -> None:
        self.calls.append(("schedule", ""))


def _catalogue() -> StrategyCatalogue:
    """Two documented strategies and one discovered file without metadata."""
    return StrategyCatalogue(
        [
            StrategyMeta(
                id="basic",
                class_name="BasicStrategy",
                file="BasicStrategy.py",
                title="EMA cross baseline",
                category="baseline",
                summary="Minimal EMA crossover.",
                indicators=["EMA(12)", "EMA(26)"],
            ),
            StrategyMeta(
                id="momentum",
                class_name="MomentumStrategy",
                file="MomentumStrategy.py",
                title="Momentum breakout",
                category="trend",
            ),
            StrategyMeta(id="faber", class_name="FaberStrategy", file="FaberStrategy.py"),
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


def _snapshot(profile_id: str, value: float, **overrides: Any) -> ProfileSnapshot:
    fields: dict[str, Any] = {
        "profile_id": profile_id,
        "ts": format_ts(),
        "portfolio_value": value,
        "cash": value,
    }
    fields.update(overrides)
    return ProfileSnapshot(**fields)


def _make_engine(
    tmp_path: Path,
    *,
    profiles: Sequence[ProfileConfig] = (),
    states: Mapping[str, str] | None = None,
    alive: Iterable[str] = (),
    snapshots: Sequence[ProfileSnapshot] = (),
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


def test_strategies_keep_the_catalogue_order_and_metadata(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    payload = client.get("/api/strategies").json()

    assert [strategy["id"] for strategy in payload["strategies"]] == [
        "basic",
        "momentum",
        "faber",
    ]
    basic = payload["strategies"][0]
    assert basic["class_name"] == "BasicStrategy"
    assert basic["title"] == "EMA cross baseline"
    assert basic["category"] == "baseline"
    assert basic["summary"] == "Minimal EMA crossover."
    assert basic["indicators"] == ["EMA(12)", "EMA(26)"]


def test_strategies_aggregate_the_profiles_that_use_them(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(
        tmp_path,
        profiles=[
            _profile("basic-one", strategy="basic"),
            _profile("basic-two", strategy="basic", initial_capital=500.0),
            _profile("momentum-one", strategy="momentum"),
        ],
        states={"basic-one": "running", "basic-two": "stopped", "momentum-one": "running"},
        alive=["basic-one", "momentum-one"],
        snapshots=[
            _snapshot("basic-one", 1200.0, profit_abs=200.0),
            _snapshot("basic-two", 400.0, profit_abs=-100.0),
            _snapshot("momentum-one", 900.0, profit_abs=-100.0),
        ],
    )

    strategies = {item["id"]: item for item in client.get("/api/strategies").json()["strategies"]}

    assert strategies["basic"]["profile_count"] == 2
    assert strategies["basic"]["profiles_running"] == 1
    assert strategies["basic"]["portfolio_value"] == 1600.0
    assert strategies["basic"]["profit_abs"] == 100.0
    assert strategies["basic"]["profit_pct"] == pytest.approx(100.0 / 1500.0)
    assert strategies["basic"]["best_profile_id"] == "basic-one"
    assert strategies["momentum"]["profile_count"] == 1
    assert strategies["momentum"]["profiles_running"] == 1
    assert strategies["momentum"]["portfolio_value"] == 900.0


def test_a_strategy_nobody_runs_is_still_reported(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path, profiles=[_profile("basic-one")])

    faber = next(
        item for item in client.get("/api/strategies").json()["strategies"] if item["id"] == "faber"
    )

    assert faber["profile_count"] == 0
    assert faber["profiles_running"] == 0
    assert faber["portfolio_value"] == 0.0
    assert faber["profit_pct"] == 0.0
    assert faber["best_profile_id"] is None


def test_strategies_ignore_profiles_of_unknown_strategies(tmp_path: Path) -> None:
    """A profile pointing outside the catalogue contributes to no strategy."""
    client, _supervisor, _store = _make_engine(
        tmp_path, profiles=[_profile("orphan", strategy="not-declared")]
    )

    payload = client.get("/api/strategies").json()

    assert [strategy["profile_count"] for strategy in payload["strategies"]] == [0, 0, 0]


def test_strategies_need_no_operator_token(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    assert client.get("/api/strategies").status_code == 200


def test_strategies_fall_back_to_the_catalogue_of_the_checkout(tmp_path: Path) -> None:
    """Without an application override, the API discovers the real catalogue."""
    store = StateStore(tmp_path / "state.db")
    store.bootstrap()
    settings = PlatformSettings()
    app = create_app(
        supervisor=_StubSupervisor(store, settings),
        settings=settings,
        state_dir=tmp_path,
        start_engine=False,
    )

    strategies = TestClient(app).get("/api/strategies").json()["strategies"]

    ids = [strategy["id"] for strategy in strategies]
    assert "basic" in ids
    assert len(ids) == len(set(ids)) > 0
    assert all(strategy["title"] for strategy in strategies)
