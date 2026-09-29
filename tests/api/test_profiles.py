"""``/api/profiles``: the ranked fleet, one profile, and the operator mutations.

The ranking rule is the one thing every widget of the dashboard depends on, so it
is pinned from both sides here: the rank is assigned once over both modes and
``?mode=``/``?sort=``/``?limit=`` only reorder or truncate without ever changing
it. The mutations cover the documented refusals (``404``, ``409``, ``422``) as
well as the defaults an operator profile is created with.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.api.security import OPERATOR_TOKEN_HEADER
from trading_platform.config import ENV_OPERATOR_TOKEN, PlatformSettings
from trading_platform.models import (
    ProfileConfig,
    ProfileSnapshot,
    StrategyMeta,
    format_ts,
    utc_now,
)
from trading_platform.profiles.catalogue import StrategyCatalogue, load_profile_catalogue
from trading_platform.profiles.store import (
    ProfileDailyRecord,
    ProfileTradeRecord,
    StateStore,
)

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


def _snapshot(
    profile_id: str, value: float, *, days_ago: float = 0.0, **overrides: Any
) -> ProfileSnapshot:
    fields: dict[str, Any] = {
        "profile_id": profile_id,
        "ts": format_ts(utc_now() - timedelta(days=days_ago)),
        "portfolio_value": value,
        "cash": value,
    }
    fields.update(overrides)
    return ProfileSnapshot(**fields)


def _daily_date(day: int) -> str:
    """Return the ISO day ``day`` days after 2026-08-01."""
    return (datetime(2026, 8, 1) + timedelta(days=day)).date().isoformat()


def _daily_row(profile_id: str, day: int, **overrides: Any) -> ProfileDailyRecord:
    """Build one stored daily row for the day ``day`` days after 2026-08-01."""
    fields: dict[str, Any] = {
        "profile_id": profile_id,
        "date": _daily_date(day),
        "abs_profit": 1.0 + day,
        "rel_profit": 0.001 * day,
        "starting_balance": 1000.0,
        "trade_count": day,
    }
    fields.update(overrides)
    return ProfileDailyRecord(**fields)


def _trade_row(profile_id: str, trade_id: int, **overrides: Any) -> ProfileTradeRecord:
    """Build one stored trade row; ``trade_id`` also orders the read model."""
    fields: dict[str, Any] = {
        "profile_id": profile_id,
        "trade_id": trade_id,
        "pair": "BTC/USDT",
        "is_open": False,
        "open_date": f"2026-09-{trade_id:02d}T08:00:00Z",
        "close_date": f"2026-09-{trade_id:02d}T09:00:00Z",
        "amount": 0.5,
        "open_rate": 100.0,
        "close_rate": 110.0,
        "stake_amount": 50.0,
        "profit_abs": 5.0,
        "profit_pct": 1.25,
        "exit_reason": "roi",
    }
    fields.update(overrides)
    return ProfileTradeRecord(**fields)


#: Keys of one ``daily`` row of ``GET /api/profiles/{id}``.
DAILY_KEYS = {"date", "abs_profit", "rel_profit", "starting_balance", "trade_count"}

#: Keys of one ``open_trades`` row.
OPEN_TRADE_KEYS = {
    "trade_id",
    "pair",
    "open_date",
    "amount",
    "open_rate",
    "current_rate",
    "stake_amount",
    "profit_abs",
    "profit_pct",
}

#: Keys of one ``recent_trades`` row.
RECENT_TRADE_KEYS = {
    "trade_id",
    "pair",
    "open_date",
    "close_date",
    "amount",
    "open_rate",
    "close_rate",
    "stake_amount",
    "profit_abs",
    "profit_pct",
    "exit_reason",
}


def _seed_read_model(store: StateStore, profile_id: str) -> None:
    """Seed 35 days, 21 closed trades and two open ones for one profile."""
    store.upsert_daily_records([_daily_row(profile_id, day) for day in range(35)])
    store.upsert_trade_records(
        [_trade_row(profile_id, trade_id) for trade_id in range(1, 22)]
        + [
            _trade_row(
                profile_id,
                90,
                is_open=True,
                open_date="2026-09-27T11:00:00Z",
                close_date=None,
                close_rate=None,
                exit_reason=None,
            ),
            _trade_row(
                profile_id,
                91,
                is_open=True,
                open_date="2026-09-27T12:00:00Z",
                close_date=None,
                close_rate=None,
                exit_reason=None,
                profit_pct=2.5,
            ),
        ]
    )


def _make_engine(
    tmp_path: Path,
    *,
    profiles: Sequence[ProfileConfig] = (),
    sources: Mapping[str, str] | None = None,
    states: Mapping[str, str] | None = None,
    alive: Iterable[str] = (),
    snapshots: Sequence[ProfileSnapshot] = (),
    settings: PlatformSettings | None = None,
) -> tuple[TestClient, _StubSupervisor, StateStore]:
    """Build a client over a real SQLite store and a stubbed process layer."""
    store = StateStore(tmp_path / "state.db")
    store.bootstrap()
    for profile in profiles:
        store.upsert_profile(profile, source=(sources or {}).get(profile.id, "catalogue"))
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


def _create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "New Profile",
        "strategy": "basic",
        "timeframe": "1h",
        "mode": "paper",
        "pairs": ["ETH/USDT"],
        "initial_capital": 500.0,
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def _configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OPERATOR_TOKEN, TOKEN)


def _ranked_engine(tmp_path: Path) -> tuple[TestClient, _StubSupervisor, StateStore]:
    """Four profiles whose portfolio values order them beta, alpha, gamma, delta."""
    return _make_engine(
        tmp_path,
        profiles=[
            _profile("alpha", name="Alpha", strategy="basic"),
            _profile("beta", name="beta", strategy="momentum"),
            _profile("gamma", name="Gamma", strategy="basic"),
            _profile("delta", name="Delta", strategy="momentum", mode="live"),
        ],
        states={"alpha": "running", "beta": "running", "gamma": "stopped", "delta": "stopped"},
        alive=["alpha", "beta"],
        snapshots=[
            _snapshot("alpha", 1500.0),
            _snapshot("beta", 2000.0),
            _snapshot("gamma", 1000.0),
            _snapshot("delta", 800.0, mode="live"),
        ],
    )


# ---------------------------------------------------------------------------
# The ranked fleet
# ---------------------------------------------------------------------------
def test_profiles_are_ranked_by_portfolio_value_across_both_modes(tmp_path: Path) -> None:
    client, _supervisor, _store = _ranked_engine(tmp_path)

    payload = client.get("/api/profiles").json()

    assert [profile["id"] for profile in payload["profiles"]] == ["beta", "alpha", "gamma", "delta"]
    assert [profile["rank"] for profile in payload["profiles"]] == [1, 2, 3, 4]
    assert payload["total"] == 4
    assert payload["generated_at"]


def test_mode_filter_keeps_the_global_ranks(tmp_path: Path) -> None:
    """``?mode=paper`` selects, it does not re-rank."""
    client, _supervisor, _store = _ranked_engine(tmp_path)

    payload = client.get("/api/profiles?mode=paper").json()

    assert [profile["id"] for profile in payload["profiles"]] == ["beta", "alpha", "gamma"]
    assert [profile["rank"] for profile in payload["profiles"]] == [1, 2, 3]


def test_live_filter_returns_the_live_profile_only(tmp_path: Path) -> None:
    client, _supervisor, _store = _ranked_engine(tmp_path)

    payload = client.get("/api/profiles?mode=live").json()

    assert [profile["id"] for profile in payload["profiles"]] == ["delta"]
    assert payload["profiles"][0]["rank"] == 4


def test_state_filter_selects_one_state(tmp_path: Path) -> None:
    client, _supervisor, _store = _ranked_engine(tmp_path)

    payload = client.get("/api/profiles?state=running").json()

    assert [profile["id"] for profile in payload["profiles"]] == ["beta", "alpha"]


def test_unknown_filter_value_selects_nothing_instead_of_failing(tmp_path: Path) -> None:
    client, _supervisor, _store = _ranked_engine(tmp_path)

    response = client.get("/api/profiles?mode=both&state=unknown")

    assert response.status_code == 200
    payload = response.json()
    assert payload["profiles"] == []
    assert payload["total"] == 0
    assert payload["generated_at"]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("value", ["beta", "alpha", "gamma", "delta"]),
        ("profit", ["beta", "alpha", "gamma", "delta"]),
        ("name", ["alpha", "beta", "delta", "gamma"]),
        ("strategy", ["alpha", "gamma", "beta", "delta"]),
    ],
)
def test_sort_orders(tmp_path: Path, sort: str, expected: list[str]) -> None:
    """``name`` sorts case-insensitively; ``strategy`` groups without re-ranking."""
    client, _supervisor, _store = _ranked_engine(tmp_path)

    payload = client.get(f"/api/profiles?sort={sort}").json()

    assert [profile["id"] for profile in payload["profiles"]] == expected


def test_unknown_sort_falls_back_to_portfolio_value(tmp_path: Path) -> None:
    client, _supervisor, _store = _ranked_engine(tmp_path)

    payload = client.get("/api/profiles?sort=whatever").json()

    assert [profile["id"] for profile in payload["profiles"]] == ["beta", "alpha", "gamma", "delta"]


def test_limit_truncates_the_answer(tmp_path: Path) -> None:
    client, _supervisor, _store = _ranked_engine(tmp_path)

    payload = client.get("/api/profiles?limit=2").json()

    assert [profile["id"] for profile in payload["profiles"]] == ["beta", "alpha"]
    assert payload["total"] == 2


def test_profiles_without_a_snapshot_are_shown_at_their_initial_capital(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(
        tmp_path, profiles=[_profile("fresh", initial_capital=750.0)]
    )

    profile = client.get("/api/profiles").json()["profiles"][0]

    assert profile["portfolio_value"] == 750.0
    assert profile["profit_pct"] == 0.0
    assert profile["state"] == "stopped"


# ---------------------------------------------------------------------------
# One profile
# ---------------------------------------------------------------------------
def test_detail_returns_the_documented_payload(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(
        tmp_path,
        profiles=[_profile("alpha", strategy="basic")],
        states={"alpha": "running"},
        alive=["alpha"],
        snapshots=[
            _snapshot("alpha", 1100.0, days_ago=0.02),
            _snapshot("alpha", 1200.0, open_trades=1, closed_trades=3),
        ],
    )
    _seed_read_model(store, "alpha")

    payload = client.get("/api/profiles/alpha").json()

    assert set(payload) == {
        "profile",
        "strategy",
        "equity_curve",
        "daily",
        "open_trades",
        "recent_trades",
    }
    assert payload["profile"]["id"] == "alpha"
    assert payload["profile"]["rank"] == 1
    assert payload["strategy"]["id"] == "basic"
    assert payload["strategy"]["title"] == "EMA cross baseline"
    assert payload["strategy"]["profile_count"] == 1
    assert payload["strategy"]["profiles_running"] == 1
    assert [point["value"] for point in payload["equity_curve"]] == [1100.0, 1200.0]

    # ``daily``: the 30 most recent days, chronological with the newest last.
    daily = payload["daily"]
    assert len(daily) == 30
    assert [row["date"] for row in daily] == [_daily_date(day) for day in range(5, 35)]
    assert set(daily[0]) == DAILY_KEYS
    assert daily[-1] == {
        "date": _daily_date(34),
        "abs_profit": 35.0,
        "rel_profit": pytest.approx(0.034),
        "starting_balance": 1000.0,
        "trade_count": 34,
    }

    # ``open_trades``: every open row, newest first, and exactly nine keys.
    open_trades = payload["open_trades"]
    assert [trade["trade_id"] for trade in open_trades] == [91, 90]
    assert set(open_trades[0]) == OPEN_TRADE_KEYS
    assert open_trades[0]["pair"] == "BTC/USDT"
    assert open_trades[0]["open_date"] == "2026-09-27T12:00:00Z"
    assert open_trades[0]["amount"] == 0.5
    assert open_trades[0]["open_rate"] == 100.0
    # The stored row carries no live rate, so current_rate is the entry price.
    assert open_trades[0]["current_rate"] == open_trades[0]["open_rate"]
    assert open_trades[0]["stake_amount"] == 50.0
    assert open_trades[0]["profit_abs"] == 5.0
    assert open_trades[0]["profit_pct"] == 2.5

    # ``recent_trades``: the 20 most recent closed rows, newest first.
    recent = payload["recent_trades"]
    assert len(recent) == 20
    assert [trade["trade_id"] for trade in recent] == list(range(21, 1, -1))
    assert set(recent[0]) == RECENT_TRADE_KEYS
    assert recent[0]["open_date"] == "2026-09-21T08:00:00Z"
    assert recent[0]["close_date"] == "2026-09-21T09:00:00Z"
    assert recent[0]["close_rate"] == 110.0
    assert recent[0]["stake_amount"] == 50.0
    assert recent[0]["profit_abs"] == 5.0
    assert recent[0]["profit_pct"] == 1.25
    assert recent[0]["exit_reason"] == "roi"


def test_detail_serves_the_read_model_of_a_stopped_profile(tmp_path: Path) -> None:
    client, supervisor, store = _make_engine(
        tmp_path,
        profiles=[_profile("alpha")],
        states={"alpha": "stopped"},
    )
    store.upsert_daily_records([_daily_row("alpha", 0), _daily_row("alpha", 1)])
    store.upsert_trade_records(
        [
            _trade_row(
                "alpha",
                5,
                is_open=True,
                open_date="2026-09-27T10:00:00Z",
                close_date=None,
                close_rate=None,
                exit_reason=None,
            ),
            _trade_row("alpha", 6),
        ]
    )

    payload = client.get("/api/profiles/alpha").json()

    # Nothing runs, yet the route answers: the rows come from the state DB.
    assert supervisor.is_running("alpha") is False
    assert payload["profile"]["slot"] is None
    assert payload["profile"]["worker_port"] is None
    assert [row["date"] for row in payload["daily"]] == [_daily_date(0), _daily_date(1)]
    assert [trade["trade_id"] for trade in payload["open_trades"]] == [5]
    assert [trade["trade_id"] for trade in payload["recent_trades"]] == [6]


def test_profiles_publish_the_sparkline_the_slot_and_the_worker_port(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(
        tmp_path,
        profiles=[
            _profile("alpha", priority=10),
            _profile("beta", priority=30),
            _profile("gamma", priority=20),
        ],
        states={"alpha": "running", "beta": "running"},
        alive=["alpha", "beta"],
        snapshots=[
            _snapshot("alpha", 1000.0 + index, days_ago=(61 - index) / 1440.0)
            for index in range(61)
        ],
    )
    # The runtime columns keep the port of a worker that stopped as well.
    store.set_profile_runtime("alpha", api_port=8101)
    store.set_profile_runtime("beta", api_port=8102)
    store.set_profile_runtime("gamma", api_port=8103)

    profiles = {row["id"]: row for row in client.get("/api/profiles").json()["profiles"]}

    # ``sparkline``: the 60 most recent snapshots, oldest first.
    sparkline = profiles["alpha"]["sparkline"]
    assert len(sparkline) == 60
    assert set(sparkline[0]) == {"t", "value", "profit_pct"}
    assert [point["value"] for point in sparkline] == [1000.0 + index for index in range(1, 61)]
    assert sparkline[0]["t"] < sparkline[-1]["t"]
    assert sparkline[0]["profit_pct"] == pytest.approx(0.001)
    # A profile without a snapshot publishes no point at all.
    assert profiles["beta"]["sparkline"] == []

    # ``slot``: the running profiles in scheduler order (priority DESC, id ASC).
    assert profiles["beta"]["slot"] == 1
    assert profiles["alpha"]["slot"] == 2
    assert profiles["gamma"]["slot"] is None

    # ``worker_port``: only while the worker is alive.
    assert profiles["alpha"]["worker_port"] == 8101
    assert profiles["beta"]["worker_port"] == 8102
    assert profiles["gamma"]["worker_port"] is None

    # The detail route publishes the very same profile view.
    detail = client.get("/api/profiles/alpha").json()["profile"]
    assert detail["slot"] == 2
    assert detail["worker_port"] == 8101
    assert len(detail["sparkline"]) == 60


def test_detail_equity_curve_follows_the_window(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(
        tmp_path,
        profiles=[_profile("alpha")],
        snapshots=[
            _snapshot("alpha", 1400.0, days_ago=2.0),
            _snapshot("alpha", 1300.0, days_ago=0.5),
            _snapshot("alpha", 1200.0),
        ],
    )

    last_day = client.get("/api/profiles/alpha?window=24h").json()["equity_curve"]
    last_week = client.get("/api/profiles/alpha?window=7d").json()["equity_curve"]
    everything = client.get("/api/profiles/alpha?window=all").json()["equity_curve"]

    assert [point["value"] for point in last_day] == [1300.0, 1200.0]
    assert [point["value"] for point in last_week] == [1400.0, 1300.0, 1200.0]
    assert everything == last_week


def test_detail_refuses_an_unknown_window(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path, profiles=[_profile("alpha")])

    assert client.get("/api/profiles/alpha?window=42h").status_code == 422


def test_detail_of_an_unknown_profile_is_404(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path, profiles=[_profile("alpha")])

    response = client.get("/api/profiles/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "profile not found"}


def test_detail_of_a_strategy_without_metadata_still_answers(tmp_path: Path) -> None:
    """A profile may point at a strategy the catalogue does not describe yet."""
    client, _supervisor, _store = _make_engine(
        tmp_path, profiles=[_profile("alpha", strategy="not-in-the-catalogue")]
    )

    payload = client.get("/api/profiles/alpha").json()

    assert payload["profile"]["strategy"] == "not-in-the-catalogue"
    assert payload["profile"]["strategy_title"] == ""
    assert payload["strategy"]["id"] == "not-in-the-catalogue"
    assert payload["strategy"]["profile_count"] == 0


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
def test_create_uses_the_platform_defaults_and_slugs_the_name(tmp_path: Path) -> None:
    settings = PlatformSettings(default_exchange="kraken", default_max_open_trades=4)
    client, supervisor, store = _make_engine(tmp_path, settings=settings)

    response = client.post(
        "/api/profiles",
        json=_create_body(name="My ETH Momentum!"),
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 201
    profile = response.json()["profile"]
    assert profile["id"] == "my-eth-momentum"
    assert profile["exchange"] == "kraken"
    assert profile["max_open_trades"] == 4
    assert profile["priority"] == 0
    assert profile["state"] == "stopped"
    assert profile["portfolio_value"] == 500.0
    stored = store.get_profile("my-eth-momentum")
    assert stored is not None
    assert stored.source == "operator"
    assert ("schedule", "") in supervisor.calls
    assert [event.kind for event in store.list_events()] == ["profile_created"]


def test_create_honours_every_explicit_field(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(tmp_path)

    response = client.post(
        "/api/profiles",
        json=_create_body(
            id="explicit-id",
            name="Explicit",
            strategy="momentum",
            timeframe="4h",
            mode="paper",
            exchange="bybit",
            pairs=["SOL/USDT", "AVAX/USDT"],
            initial_capital=250.0,
            max_open_trades=1,
            priority=7,
        ),
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 201
    stored = store.get_profile("explicit-id")
    assert stored is not None
    assert (stored.strategy, stored.timeframe, stored.exchange) == ("momentum", "4h", "bybit")
    assert stored.pairs == ["SOL/USDT", "AVAX/USDT"]
    assert (stored.max_open_trades, stored.priority) == (1, 7)


def test_create_refuses_a_duplicate_id_with_409(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path, profiles=[_profile("taken")])

    response = client.post(
        "/api/profiles",
        json=_create_body(id="taken"),
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "profile already exists"}


def test_create_refuses_an_unknown_strategy_with_422(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(tmp_path)

    response = client.post(
        "/api/profiles",
        json=_create_body(strategy="does-not-exist"),
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "unknown strategy: does-not-exist"}
    assert store.list_profiles() == []


def test_create_refuses_an_unsupported_timeframe_with_422(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(tmp_path)

    response = client.post(
        "/api/profiles",
        json=_create_body(timeframe="7m"),
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "unsupported timeframe: 7m"}
    assert store.list_profiles() == []


def test_create_refuses_a_name_without_a_usable_slug(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    response = client.post(
        "/api/profiles",
        json=_create_body(name="!!!"),
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "profile id must not be empty"}


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
def test_patch_writes_only_the_fields_of_the_body(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(tmp_path, profiles=[_profile("alpha")])

    response = client.patch(
        "/api/profiles/alpha",
        json={"name": "Renamed", "pairs": ["SOL/USDT"], "priority": 3},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    profile = response.json()["profile"]
    assert profile["name"] == "Renamed"
    assert profile["pairs"] == ["SOL/USDT"]
    assert profile["priority"] == 3
    assert profile["strategy"] == "basic"
    assert profile["timeframe"] == "1h"
    assert profile["initial_capital"] == 1000.0
    assert store.get_profile("alpha") is not None


def test_patch_can_disable_a_profile(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(tmp_path, profiles=[_profile("alpha")])

    response = client.patch(
        "/api/profiles/alpha",
        json={"enabled": False},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    stored = store.get_profile("alpha")
    assert stored is not None
    assert stored.enabled is False


def test_patch_of_an_unknown_profile_is_404(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    response = client.patch(
        "/api/profiles/missing",
        json={"name": "Renamed"},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "profile not found"}


def test_patch_with_an_unknown_field_is_ignored(tmp_path: Path) -> None:
    """The body schema ignores what it does not know, and writes nothing else."""
    client, _supervisor, store = _make_engine(tmp_path, profiles=[_profile("alpha")])

    response = client.patch(
        "/api/profiles/alpha",
        json={"strategy": "momentum", "mode": "live"},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    stored = store.get_profile("alpha")
    assert stored is not None
    assert (stored.strategy, stored.mode) == ("basic", "paper")


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
def test_delete_of_an_operator_profile_is_204(tmp_path: Path) -> None:
    client, supervisor, store = _make_engine(
        tmp_path, profiles=[_profile("mine")], sources={"mine": "operator"}
    )

    response = client.delete("/api/profiles/mine", headers={OPERATOR_TOKEN_HEADER: TOKEN})

    assert response.status_code == 204
    assert response.content == b""
    assert store.get_profile("mine") is None
    assert ("stop", "mine") in supervisor.calls


def test_delete_of_a_catalogue_profile_needs_force(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(tmp_path, profiles=[_profile("declared")])

    refused = client.delete("/api/profiles/declared", headers={OPERATOR_TOKEN_HEADER: TOKEN})
    forced = client.delete(
        "/api/profiles/declared?force=true", headers={OPERATOR_TOKEN_HEADER: TOKEN}
    )

    assert refused.status_code == 409
    assert refused.json() == {"detail": "catalogue profile cannot be deleted without force=true"}
    assert forced.status_code == 204
    assert store.get_profile("declared") is None


def test_force_does_not_accept_a_non_boolean_value(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path, profiles=[_profile("declared")])

    response = client.delete(
        "/api/profiles/declared?force=maybe", headers={OPERATOR_TOKEN_HEADER: TOKEN}
    )

    assert response.status_code == 422


def test_delete_of_an_unknown_profile_is_404(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    response = client.delete("/api/profiles/missing", headers={OPERATOR_TOKEN_HEADER: TOKEN})

    assert response.status_code == 404
    assert response.json() == {"detail": "profile not found"}


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_actions_reach_the_supervisor(tmp_path: Path, action: str) -> None:
    client, supervisor, _store = _make_engine(tmp_path, profiles=[_profile("alpha")])

    response = client.post(
        "/api/profiles/alpha/actions",
        json={"action": action},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    assert response.json()["profile"]["id"] == "alpha"
    assert (action, "alpha") in supervisor.calls


def test_the_actions_alias_accepts_the_action_in_the_path(tmp_path: Path) -> None:
    """The dashboard client posts the action in the path; both forms are served."""
    client, supervisor, _store = _make_engine(tmp_path, profiles=[_profile("alpha")])

    response = client.post(
        "/api/profiles/alpha/actions/restart", headers={OPERATOR_TOKEN_HEADER: TOKEN}
    )

    assert response.status_code == 200
    assert ("restart", "alpha") in supervisor.calls


def test_an_unknown_action_is_refused(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path, profiles=[_profile("alpha")])

    response = client.post(
        "/api/profiles/alpha/actions",
        json={"action": "explode"},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 422


def test_an_action_on_an_unknown_profile_is_404(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    response = client.post(
        "/api/profiles/missing/actions",
        json={"action": "start"},
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "profile not found"}


# ---------------------------------------------------------------------------
# Catalogue apply
# ---------------------------------------------------------------------------
def _with_catalogue(client: TestClient, profiles: Sequence[ProfileConfig]) -> None:
    """Install the declarative catalogue the apply route works from."""
    client.app.state.profile_catalogue = list(profiles)  # type: ignore[attr-defined]


def test_apply_creates_the_declared_profiles(tmp_path: Path) -> None:
    client, supervisor, store = _make_engine(tmp_path)
    _with_catalogue(
        client,
        [
            _profile("basic-btc-1h", name="Basic BTC 1h"),
            _profile("momentum-btc-1h", name="Momentum BTC", strategy="momentum"),
        ],
    )

    response = client.post(
        "/api/catalogue/apply", json={"prune": False}, headers={OPERATOR_TOKEN_HEADER: TOKEN}
    )

    assert response.status_code == 200
    assert response.json() == {
        "created": ["basic-btc-1h", "momentum-btc-1h"],
        "updated": [],
        "skipped": [],
        "pruned": [],
        "refused_live": [],
    }
    stored = store.get_profile("basic-btc-1h")
    assert stored is not None
    assert stored.source == "catalogue"
    assert ("schedule", "") in supervisor.calls
    assert [event.kind for event in store.list_events()] == ["catalogue_applied"]


def test_apply_is_idempotent(tmp_path: Path) -> None:
    """A second apply reports every row as skipped, and rewrites nothing."""
    client, _supervisor, store = _make_engine(tmp_path)
    _with_catalogue(client, [_profile("basic-btc-1h", name="Basic BTC 1h")])

    first = client.post("/api/catalogue/apply", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN})
    before = store.get_profile("basic-btc-1h")
    second = client.post("/api/catalogue/apply", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN})

    assert first.json()["created"] == ["basic-btc-1h"]
    assert second.json()["created"] == []
    assert second.json()["updated"] == []
    assert second.json()["skipped"] == ["basic-btc-1h"]
    assert store.get_profile("basic-btc-1h") == before


def test_apply_refreshes_a_changed_catalogue_profile(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(
        tmp_path, profiles=[_profile("basic-btc-1h", name="Old name", priority=1)]
    )
    _with_catalogue(client, [_profile("basic-btc-1h", name="New name", priority=9)])

    response = client.post("/api/catalogue/apply", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN})

    assert response.json()["updated"] == ["basic-btc-1h"]
    stored = store.get_profile("basic-btc-1h")
    assert stored is not None
    assert (stored.name, stored.priority) == ("New name", 9)


def test_apply_never_touches_an_operator_profile(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(
        tmp_path,
        profiles=[_profile("mine", name="Operator name", priority=42)],
        sources={"mine": "operator"},
    )
    _with_catalogue(client, [_profile("mine", name="Catalogue name", priority=1)])

    response = client.post("/api/catalogue/apply", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN})

    assert response.json() == {
        "created": [],
        "updated": [],
        "skipped": ["mine"],
        "pruned": [],
        "refused_live": [],
    }
    stored = store.get_profile("mine")
    assert stored is not None
    assert stored.name == "Operator name"
    assert stored.source == "operator"


def test_apply_refuses_to_create_a_live_profile_whose_gate_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TB_ALLOW_LIVE_TRADING", raising=False)
    monkeypatch.delenv("TB_LIVE_EXCHANGE_KEY", raising=False)
    monkeypatch.delenv("TB_LIVE_EXCHANGE_SECRET", raising=False)
    client, _supervisor, store = _make_engine(tmp_path)
    _with_catalogue(
        client,
        [
            _profile("paper-one"),
            _profile("live-one", mode="live", initial_capital=250.0),
        ],
    )

    payload = client.post(
        "/api/catalogue/apply", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN}
    ).json()

    assert payload["created"] == ["paper-one"]
    assert payload["refused_live"] == ["live-one"]
    assert store.get_profile("live-one") is None


def test_apply_creates_a_live_profile_when_the_gate_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TB_ALLOW_LIVE_TRADING", "I_UNDERSTAND_THE_RISK")
    monkeypatch.setenv("TB_LIVE_EXCHANGE_KEY", "key")
    monkeypatch.setenv("TB_LIVE_EXCHANGE_SECRET", "secret")
    client, _supervisor, store = _make_engine(tmp_path)
    _with_catalogue(client, [_profile("live-one", mode="live", initial_capital=250.0)])

    payload = client.post(
        "/api/catalogue/apply", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN}
    ).json()

    assert payload["created"] == ["live-one"]
    assert payload["refused_live"] == []
    assert store.get_profile("live-one") is not None


def test_apply_only_prunes_when_asked(tmp_path: Path) -> None:
    """A catalogue row the document dropped survives a plain apply."""
    client, supervisor, store = _make_engine(
        tmp_path, profiles=[_profile("dropped"), _profile("kept")]
    )
    _with_catalogue(client, [_profile("kept")])

    plain = client.post("/api/catalogue/apply", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN})

    assert plain.json()["pruned"] == []
    assert store.get_profile("dropped") is not None

    pruned = client.post(
        "/api/catalogue/apply", json={"prune": True}, headers={OPERATOR_TOKEN_HEADER: TOKEN}
    )

    assert pruned.json()["pruned"] == ["dropped"]
    assert store.get_profile("dropped") is None
    assert ("stop", "dropped") in supervisor.calls


def test_prune_never_deletes_an_operator_profile(tmp_path: Path) -> None:
    client, _supervisor, store = _make_engine(
        tmp_path,
        profiles=[_profile("mine"), _profile("declared")],
        sources={"mine": "operator"},
    )
    _with_catalogue(client, [_profile("declared")])

    response = client.post(
        "/api/catalogue/apply", json={"prune": True}, headers={OPERATOR_TOKEN_HEADER: TOKEN}
    )

    assert response.json()["pruned"] == []
    assert store.get_profile("mine") is not None


def test_apply_refuses_a_body_that_is_not_an_object(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    response = client.post(
        "/api/catalogue/apply",
        json=["prune"],
        headers={OPERATOR_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 422


def test_apply_uses_the_catalogue_of_the_checkout_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an application override, the route applies ``config/profiles.json``."""
    monkeypatch.delenv("TB_ALLOW_LIVE_TRADING", raising=False)
    monkeypatch.delenv("TB_LIVE_EXCHANGE_KEY", raising=False)
    monkeypatch.delenv("TB_LIVE_EXCHANGE_SECRET", raising=False)
    declared = load_profile_catalogue()
    client, _supervisor, store = _make_engine(tmp_path)

    payload = client.post(
        "/api/catalogue/apply", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN}
    ).json()

    refused = [profile.id for profile in declared if profile.mode == "live"]
    assert payload["refused_live"] == refused
    assert payload["created"] == [profile.id for profile in declared if profile.mode != "live"]
    assert payload["updated"] == []
    assert payload["skipped"] == []
    for profile in declared:
        assert (store.get_profile(profile.id) is not None) is (profile.mode != "live")
