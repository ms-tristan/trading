"""``GET /api/account``: the paper, live and combined performance of the fleet.

The three scopes are all computed from the same ranked views, so the paper plus
the live scope always add up to the combined one -- that invariant is pinned
here, together with the window handling of the equity curve and the ``422`` an
unknown window gets.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.config import PlatformSettings
from trading_platform.models import ProfileConfig, ProfileSnapshot, StrategyMeta, format_ts, utc_now
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


def _snapshot(
    profile_id: str,
    value: float,
    *,
    days_ago: float = 0.0,
    **overrides: Any,
) -> ProfileSnapshot:
    fields: dict[str, Any] = {
        "profile_id": profile_id,
        "ts": format_ts(utc_now() - timedelta(days=days_ago)),
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


def _mixed_fleet(tmp_path: Path) -> tuple[TestClient, _StubSupervisor, StateStore]:
    """Two paper profiles and one live profile, with a recent and an old snapshot."""
    return _make_engine(
        tmp_path,
        profiles=[
            _profile("paper-one"),
            _profile("paper-two"),
            _profile("live-one", mode="live", initial_capital=200.0),
        ],
        states={"paper-one": "running", "paper-two": "stopped", "live-one": "running"},
        alive=["paper-one", "live-one"],
        snapshots=[
            _snapshot(
                "paper-one", 1200.0, days_ago=0.5, open_trades=1, closed_trades=4, win_rate=0.5
            ),
            _snapshot("paper-one", 1100.0, days_ago=3.0),
            _snapshot("paper-two", 800.0, days_ago=0.5, closed_trades=2),
            _snapshot("live-one", 250.0, days_ago=0.5, open_trades=1),
        ],
    )


def test_account_defaults_to_the_last_24_hours(tmp_path: Path) -> None:
    client, _supervisor, _store = _mixed_fleet(tmp_path)

    payload = client.get("/api/account").json()

    assert payload["combined"]["equity_curve"]
    assert [point["value"] for point in payload["combined"]["equity_curve"]] == [2250.0]
    assert payload["generated_at"]


def test_account_window_restricts_the_equity_curve(tmp_path: Path) -> None:
    client, _supervisor, _store = _mixed_fleet(tmp_path)

    last_week = client.get("/api/account?window=7d").json()["combined"]["equity_curve"]
    everything = client.get("/api/account?window=all").json()["combined"]["equity_curve"]

    assert [point["value"] for point in last_week] == [1100.0, 2250.0]
    assert everything == last_week
    assert everything[0]["t"] < everything[1]["t"]


def test_account_refuses_an_unknown_window(tmp_path: Path) -> None:
    client, _supervisor, _store = _mixed_fleet(tmp_path)

    assert client.get("/api/account?window=99h").status_code == 422


def test_account_separates_paper_and_live(tmp_path: Path) -> None:
    client, _supervisor, _store = _mixed_fleet(tmp_path)

    payload = client.get("/api/account").json()
    paper = payload["paper"]
    live = payload["live"]

    assert (paper["scope"], live["scope"]) == ("paper", "live")
    assert paper["profiles_total"] == 2
    assert paper["profiles_running"] == 1
    assert paper["portfolio_value"] == 2000.0
    assert paper["initial_capital"] == 2000.0
    assert paper["profit_abs"] == 0.0
    assert paper["profit_pct"] == 0.0
    assert paper["open_trades"] == 1
    assert paper["closed_trades"] == 6
    assert paper["open_positions"] == 1
    assert live["profiles_total"] == 1
    assert live["portfolio_value"] == 250.0
    assert live["initial_capital"] == 200.0
    assert live["profit_pct"] == pytest.approx(0.25)
    assert live["open_trades"] == 1


def test_account_combined_is_the_sum_of_both_scopes(tmp_path: Path) -> None:
    client, _supervisor, _store = _mixed_fleet(tmp_path)

    payload = client.get("/api/account").json()
    paper, live, combined = payload["paper"], payload["live"], payload["combined"]

    assert combined["scope"] == "combined"
    assert combined["portfolio_value"] == paper["portfolio_value"] + live["portfolio_value"]
    assert combined["initial_capital"] == paper["initial_capital"] + live["initial_capital"]
    assert combined["profit_abs"] == paper["profit_abs"] + live["profit_abs"]
    assert combined["open_trades"] == paper["open_trades"] + live["open_trades"]
    assert combined["closed_trades"] == paper["closed_trades"] + live["closed_trades"]
    assert combined["profit_pct"] == pytest.approx(50.0 / 2200.0)


def test_account_reports_best_and_worst_profiles(tmp_path: Path) -> None:
    client, _supervisor, _store = _mixed_fleet(tmp_path)

    combined = client.get("/api/account").json()["combined"]

    assert combined["best_profile"]["id"] == "live-one"
    assert combined["best_profile"]["profit_pct"] == pytest.approx(0.25)
    assert combined["worst_profile"]["id"] == "paper-two"
    assert combined["profiles_healthy"] == 3
    assert combined["max_drawdown_pct"] == 0.0
    # Pooled over the closed trades: (0.5 * 4 + 0.0 * 2) / 6.
    assert combined["win_rate"] == pytest.approx(1.0 / 3.0)


def test_account_of_an_empty_scope_is_zeroed(tmp_path: Path) -> None:
    """A fleet without live profiles answers a well-formed zero payload."""
    client, _supervisor, _store = _make_engine(
        tmp_path, profiles=[_profile("paper-one")], snapshots=[_snapshot("paper-one", 1000.0)]
    )

    payload = client.get("/api/account").json()

    assert payload["live"]["profiles_total"] == 0
    assert payload["live"]["portfolio_value"] == 0.0
    assert payload["live"]["profit_pct"] == 0.0
    assert payload["live"]["equity_curve"] == []
    assert payload["live"]["best_profile"] is None
    assert payload["combined"]["profiles_total"] == 1


def test_account_needs_no_operator_token(tmp_path: Path) -> None:
    client, _supervisor, _store = _mixed_fleet(tmp_path)

    assert client.get("/api/account").status_code == 200
