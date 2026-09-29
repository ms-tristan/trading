"""Tests for the aggregation helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_platform import metrics, models

NOW = datetime(2026, 9, 27, 18, 0, 0, tzinfo=UTC)


def record(profile_id: str = "p1", **overrides: object) -> models.ProfileRecord:
    payload: dict[str, object] = {
        "id": profile_id,
        "name": f"Profile {profile_id}",
        "strategy": "basic",
        "timeframe": "1h",
        "mode": "paper",
        "exchange": "binance",
        "pairs": ["BTC/USDT"],
        "initial_capital": 1000.0,
        "max_open_trades": 2,
        "priority": 100,
        "enabled": True,
        "state": models.STATE_RUNNING,
    }
    payload.update(overrides)
    return models.ProfileRecord(**payload)


def snapshot(profile_id: str = "p1", ts: str = "2026-09-27T17:31:00Z", **overrides: object):
    payload: dict[str, object] = {
        "profile_id": profile_id,
        "ts": ts,
        "portfolio_value": 1100.0,
        "cash": 600.0,
        "positions_value": 500.0,
        "profit_abs": 100.0,
        "profit_pct": 0.1,
        "realized_profit_abs": 40.0,
        "unrealized_profit_abs": 60.0,
        "open_trades": 1,
        "closed_trades": 4,
        "win_rate": 0.5,
        "profit_factor": 1.5,
        "max_drawdown_pct": 8.0,
        "healthy": True,
    }
    payload.update(overrides)
    return models.ProfileSnapshot(**payload)


def strategy_meta(strategy_id: str = "basic") -> models.StrategyMeta:
    return models.StrategyMeta(
        id=strategy_id,
        class_name="BasicStrategy",
        file="BasicStrategy.py",
        title="EMA cross baseline",
        category="baseline",
        summary="Minimal EMA crossover.",
        description="A deliberately small trend strategy.",
        indicators=["EMA(12)", "EMA(26)"],
        timeframes=["1h", "4h"],
        reference="https://www.freqtrade.io/en/stable/strategy-customization/",
        risk_notes="Whipsaws in ranging markets.",
    )


def view(
    profile_id: str,
    *,
    mode: str = "paper",
    state: str = models.STATE_RUNNING,
    value: float = 1000.0,
    capital: float = 1000.0,
    profit_pct: float | None = None,
    profit_abs: float = 0.0,
    realized: float = 0.0,
    unrealized: float = 0.0,
    open_trades: int = 0,
    closed_trades: int = 0,
    win_rate: float = 0.0,
    profit_factor: float = 0.0,
    drawdown: float = 0.0,
    name: str | None = None,
) -> models.ProfileView:
    return models.ProfileView(
        id=profile_id,
        name=name if name is not None else f"Profile {profile_id}",
        strategy="basic",
        mode=mode,
        state=state,
        portfolio_value=value,
        initial_capital=capital,
        profit_pct=metrics.compute_profit_pct(value, capital) if profit_pct is None else profit_pct,
        profit_abs=profit_abs,
        realized_profit_abs=realized,
        unrealized_profit_abs=unrealized,
        open_trades=open_trades,
        closed_trades=closed_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        max_drawdown_pct=drawdown,
    )


# ---------------------------------------------------------------------------
# compute_profit_pct
# ---------------------------------------------------------------------------
def test_compute_profit_pct_is_the_platform_formula() -> None:
    assert metrics.compute_profit_pct(1100.0, 1000.0) == pytest.approx(0.1)
    assert metrics.compute_profit_pct(900.0, 1000.0) == pytest.approx(-0.1)
    assert metrics.compute_profit_pct(1000.0, 1000.0) == 0.0


def test_compute_profit_pct_without_a_usable_capital() -> None:
    assert metrics.compute_profit_pct(1000.0, 0.0) == 0.0
    assert metrics.compute_profit_pct(1000.0, -10.0) == 0.0


def test_compute_profit_pct_sanitises_broken_reads() -> None:
    assert metrics.compute_profit_pct(float("nan"), 1000.0) == 0.0
    assert metrics.compute_profit_pct(float("inf"), 1000.0) == 0.0
    assert metrics.compute_profit_pct(None, 1000.0) == 0.0


# ---------------------------------------------------------------------------
# build_profile_view
# ---------------------------------------------------------------------------
def test_build_profile_view_without_a_snapshot_uses_the_initial_capital() -> None:
    built = metrics.build_profile_view(record(), None, None)
    assert built.portfolio_value == 1000.0
    assert built.cash == 1000.0
    assert built.positions_value == 0.0
    assert built.profit_abs == 0.0
    assert built.profit_pct == 0.0
    assert built.open_trades == 0
    assert built.closed_trades == 0
    assert built.best_pair is None
    assert built.rank == 0
    assert built.strategy_title == ""
    assert built.strategy_category == ""
    assert built.last_updated.endswith("Z")


def test_build_profile_view_without_a_snapshot_uses_the_given_clock() -> None:
    built = metrics.build_profile_view(record(), None, None, now=NOW)
    assert built.last_updated == "2026-09-27T18:00:00Z"


def test_build_profile_view_carries_the_record_and_the_snapshot() -> None:
    built = metrics.build_profile_view(
        record(), snapshot(), strategy_meta(), rank=3, uptime_seconds=42
    )
    assert built.id == "p1"
    assert built.name == "Profile p1"
    assert built.strategy == "basic"
    assert built.strategy_title == "EMA cross baseline"
    assert built.strategy_category == "baseline"
    assert built.timeframe == "1h"
    assert built.mode == "paper"
    assert built.exchange == "binance"
    assert built.pairs == ["BTC/USDT"]
    assert built.initial_capital == 1000.0
    assert built.max_open_trades == 2
    assert built.priority == 100
    assert built.state == models.STATE_RUNNING
    assert built.portfolio_value == 1100.0
    assert built.cash == 600.0
    assert built.positions_value == 500.0
    assert built.profit_abs == 100.0
    assert built.realized_profit_abs == 40.0
    assert built.unrealized_profit_abs == 60.0
    assert built.open_trades == 1
    assert built.closed_trades == 4
    assert built.uptime_seconds == 42.0
    assert built.last_updated == "2026-09-27T17:31:00Z"
    assert built.rank == 3


def test_build_profile_view_never_carries_credentials() -> None:
    built = metrics.build_profile_view(
        record(api_username="user", api_password="secret"), None, None
    )
    assert "api_password" not in built.model_dump()
    assert "api_username" not in built.model_dump()


def test_build_profile_view_normalises_freqtrade_values() -> None:
    built = metrics.build_profile_view(
        record(),
        snapshot(
            win_rate=55.0,
            max_drawdown_pct=-0.25,
            profit_factor=float("inf"),
            open_trades=2.0,
            closed_trades=3.0,
        ),
        None,
    )
    assert built.win_rate == pytest.approx(0.55)
    assert built.max_drawdown_pct == pytest.approx(25.0)
    assert built.profit_factor == 0.0
    assert built.open_trades == 2
    assert built.closed_trades == 3


def test_build_profile_view_always_uses_the_platform_profit_pct() -> None:
    built = metrics.build_profile_view(
        record(), snapshot(portfolio_value=1200.0, profit_pct=99.0), None
    )
    assert built.profit_pct == pytest.approx(0.2)


def test_build_profile_view_clamps_a_negative_uptime() -> None:
    built = metrics.build_profile_view(record(), None, None, uptime_seconds=-5.0)
    assert built.uptime_seconds == 0.0


def test_build_profile_view_uses_the_snapshot_best_pair_when_present() -> None:
    class SnapshotWithBestPair(models.ProfileSnapshot):
        best_pair: str | None = None

    built = metrics.build_profile_view(
        record(), SnapshotWithBestPair(profile_id="p1", best_pair="BTC/USDT"), None
    )
    assert built.best_pair == "BTC/USDT"


# ---------------------------------------------------------------------------
# ranking, filtering, sorting
# ---------------------------------------------------------------------------
def test_rank_views_orders_across_both_modes_and_writes_the_rank_back() -> None:
    views = [
        view("paper-a", value=1000.0),
        view("live-b", mode="live", value=1500.0),
        view("paper-c", value=1200.0),
    ]
    ranked = metrics.rank_views(views)
    assert [item.id for item in ranked] == ["live-b", "paper-c", "paper-a"]
    assert [item.rank for item in ranked] == [1, 2, 3]
    assert [item.rank for item in views] == [3, 1, 2]


def test_rank_views_breaks_ties_on_the_profile_id() -> None:
    ranked = metrics.rank_views([view("b"), view("a"), view("c")])
    assert [item.id for item in ranked] == ["a", "b", "c"]


def test_rank_views_sanitises_a_broken_portfolio_value() -> None:
    ranked = metrics.rank_views([view("a", value=float("nan")), view("b", value=10.0)])
    assert [item.id for item in ranked] == ["b", "a"]


def test_rank_views_of_nothing() -> None:
    assert metrics.rank_views([]) == []


def test_the_rank_survives_filtering_and_sorting() -> None:
    views = [view("a", value=1000.0), view("b", mode="live", value=900.0), view("c", value=800.0)]
    ranked = metrics.rank_views(views)
    by_name = metrics.sort_views(ranked, "name")
    live = metrics.filter_views(ranked, mode="live")
    assert [item.rank for item in by_name] == [1, 2, 3]
    assert [item.rank for item in live] == [2]


def test_filter_views_by_mode_and_state() -> None:
    views = [
        view("a", mode="paper", state=models.STATE_RUNNING),
        view("b", mode="live", state=models.STATE_RUNNING),
        view("c", mode="live", state=models.STATE_ERROR),
        view("d", mode="paper", state=models.STATE_BLOCKED),
    ]
    assert [item.id for item in metrics.filter_views(views, mode="live")] == ["b", "c"]
    assert [item.id for item in metrics.filter_views(views, state="blocked")] == ["d"]
    assert [item.id for item in metrics.filter_views(views, mode="live", state="running")] == ["b"]
    assert [item.id for item in metrics.filter_views(views)] == ["a", "b", "c", "d"]
    assert metrics.filter_views(views, mode="live", state="stopped") == []


def test_sort_views_value_and_profit() -> None:
    views = [
        view("a", value=1000.0, profit_pct=0.5),
        view("b", value=3000.0, profit_pct=0.1),
        view("c", value=2000.0, profit_pct=0.9),
    ]
    assert [item.id for item in metrics.sort_views(views, "value")] == ["b", "c", "a"]
    assert [item.id for item in metrics.sort_views(views, "profit")] == ["c", "a", "b"]
    assert [item.id for item in metrics.sort_views(views)] == ["b", "c", "a"]


def test_sort_views_name_is_case_insensitive_and_strategy_groups_values() -> None:
    views = [
        view("a", name="beta", value=1000.0),
        view("b", name="Alpha", value=900.0),
        view("c", name="Gamma", value=800.0),
    ]
    assert [item.id for item in metrics.sort_views(views, "name")] == ["b", "a", "c"]
    views[1].strategy = "trend"
    views[2].strategy = "alpha"
    ordered = metrics.sort_views(views, "strategy")
    assert [item.id for item in ordered] == ["c", "a", "b"]


def test_sort_views_falls_back_to_portfolio_value() -> None:
    views = [view("a", value=1000.0), view("b", value=2000.0)]
    assert [item.id for item in metrics.sort_views(views, "unknown")] == ["b", "a"]


# ---------------------------------------------------------------------------
# curves and snapshots
# ---------------------------------------------------------------------------
def test_equity_curve_from_snapshots_is_ascending_and_relative_to_the_capital() -> None:
    curve = metrics.equity_curve_from_snapshots(
        [
            snapshot(ts="2026-09-27T17:32:00Z", portfolio_value=1100.0),
            snapshot(ts="2026-09-27T17:30:00Z", portfolio_value=1050.0),
        ],
        1000.0,
    )
    assert [point.t for point in curve] == ["2026-09-27T17:30:00Z", "2026-09-27T17:32:00Z"]
    assert [point.value for point in curve] == [1050.0, 1100.0]
    assert [point.profit_pct for point in curve] == pytest.approx([0.05, 0.1])


def test_equity_curve_from_snapshots_without_capital() -> None:
    curve = metrics.equity_curve_from_snapshots([snapshot(portfolio_value=1100.0)], 0.0)
    assert curve[0].value == 1100.0
    assert curve[0].profit_pct == 0.0
    assert metrics.equity_curve_from_snapshots([], 1000.0) == []


def test_latest_snapshot_map_keeps_the_newest_row_per_profile() -> None:
    latest = metrics.latest_snapshot_map(
        [
            snapshot("p1", ts="2026-09-27T17:30:00Z", portfolio_value=1000.0),
            snapshot("p1", ts="2026-09-27T17:31:00Z", portfolio_value=1010.0),
            snapshot("p2", ts="2026-09-27T17:31:00Z", portfolio_value=2000.0),
        ]
    )
    assert sorted(latest) == ["p1", "p2"]
    assert latest["p1"].portfolio_value == 1010.0
    assert latest["p2"].portfolio_value == 2000.0
    assert metrics.latest_snapshot_map([]) == {}


def test_latest_snapshot_map_accepts_the_poller_mapping() -> None:
    mapping = {
        "p1": [
            snapshot("p1", ts="2026-09-27T17:30:00Z"),
            snapshot("p1", ts="2026-09-27T17:31:00Z"),
        ],
        "p2": [snapshot("p2", ts="2026-09-27T17:31:00Z", portfolio_value=2000.0)],
    }
    latest = metrics.latest_snapshot_map(mapping)
    assert latest["p1"].ts == "2026-09-27T17:31:00Z"
    assert latest["p2"].portfolio_value == 2000.0


def test_latest_snapshot_map_accepts_a_mapping_of_single_snapshots() -> None:
    latest = metrics.latest_snapshot_map({"p1": snapshot("p1", portfolio_value=1234.0)})
    assert latest["p1"].portfolio_value == 1234.0


# ---------------------------------------------------------------------------
# daily rows
# ---------------------------------------------------------------------------
def test_daily_rows_maps_the_freqtrade_body() -> None:
    payload = {
        "data": [
            {"date": "2026-09-26", "abs_profit": 12.5, "rel_profit": 0.0125, "trade_count": 4},
            {"date": "2026-09-27", "abs_profit": -3.0, "rel_profit": -0.003, "trade_count": 1},
        ],
        "stake_currency": "USDT",
        "fiat_display_currency": "USD",
    }
    rows = metrics.daily_rows(payload)
    assert [row.date for row in rows] == ["2026-09-26", "2026-09-27"]
    assert rows[0].abs_profit == 12.5
    assert rows[0].rel_profit == pytest.approx(0.0125)
    assert rows[0].trade_count == 4
    assert rows[1].abs_profit == -3.0


def test_daily_rows_derives_the_relative_profit_without_the_field() -> None:
    payload = {"data": [{"date": "2026-09-27", "abs_profit": 25.0, "starting_balance": 1000.0}]}
    rows = metrics.daily_rows(payload)
    assert rows[0].rel_profit == pytest.approx(0.025)
    without_balance = metrics.daily_rows({"data": [{"date": "2026-09-27", "abs_profit": 25.0}]})
    assert without_balance[0].rel_profit == 0.0


def test_daily_rows_ignores_unusable_payloads() -> None:
    assert metrics.daily_rows(None) == []
    assert metrics.daily_rows({}) == []
    assert metrics.daily_rows({"data": None}) == []
    assert metrics.daily_rows({"data": "nope"}) == []
    assert metrics.daily_rows({"data": ["nope", {"trade_count": 3}]}) == []


def test_daily_rows_sanitises_numbers_and_sorts_by_date() -> None:
    payload = {
        "data": [
            {"date": "2026-09-27", "abs_profit": float("nan"), "rel_profit": float("inf")},
            {"date": "2026-09-25", "abs_profit": 1.0, "rel_profit": 0.001, "trade_count": 2.0},
        ]
    }
    rows = metrics.daily_rows(payload)
    assert [row.date for row in rows] == ["2026-09-25", "2026-09-27"]
    assert rows[0].trade_count == 2
    assert rows[1].abs_profit == 0.0
    assert rows[1].rel_profit == 0.0


# ---------------------------------------------------------------------------
# account aggregation
# ---------------------------------------------------------------------------
def test_aggregate_account_sums_capital_value_and_profit() -> None:
    views = [
        view(
            "paper-a",
            value=1100.0,
            capital=1000.0,
            profit_abs=100.0,
            realized=40.0,
            unrealized=60.0,
            open_trades=1,
            closed_trades=4,
            win_rate=0.5,
            drawdown=10.0,
        ),
        view(
            "paper-b",
            value=900.0,
            capital=1000.0,
            profit_abs=-100.0,
            realized=-100.0,
            closed_trades=6,
            win_rate=1 / 6,
            drawdown=25.0,
        ),
    ]
    account = metrics.aggregate_account(views, {}, scope="paper", now=NOW)
    assert account.scope == "paper"
    assert account.portfolio_value == 2000.0
    assert account.initial_capital == 2000.0
    assert account.profit_abs == 0.0
    assert account.profit_pct == 0.0
    assert account.realized_profit_abs == -60.0
    assert account.unrealized_profit_abs == 60.0
    assert account.open_trades == 1
    assert account.open_positions == 1
    assert account.closed_trades == 10
    assert account.win_rate == pytest.approx(0.3)
    assert account.max_drawdown_pct == 25.0
    assert account.profiles_total == 2
    assert account.profiles_running == 2
    assert account.generated_at == "2026-09-27T18:00:00Z"


def test_aggregate_account_profit_pct_uses_the_summed_capital() -> None:
    views = [
        view("a", value=1500.0, capital=1000.0),
        view("b", value=500.0, capital=1000.0),
    ]
    account = metrics.aggregate_account(views, {}, scope="combined", now=NOW)
    assert account.portfolio_value == 2000.0
    assert account.profit_pct == 0.0
    assert account.best_profile is not None and account.best_profile.id == "a"
    assert account.worst_profile is not None and account.worst_profile.id == "b"
    assert account.best_profile.profit_pct == pytest.approx(0.5)


def test_aggregate_account_win_rate_and_profit_factor_without_trades() -> None:
    views = [view("a", win_rate=0.5, profit_factor=2.0), view("b", win_rate=1.0, profit_factor=4.0)]
    account = metrics.aggregate_account(views, {}, scope="paper", now=NOW)
    assert account.win_rate == pytest.approx(0.75)
    assert account.profit_factor == pytest.approx(3.0)


def test_aggregate_account_of_nothing() -> None:
    account = metrics.aggregate_account([], {}, scope="live", now=NOW)
    assert account.portfolio_value == 0.0
    assert account.initial_capital == 0.0
    assert account.profit_pct == 0.0
    assert account.win_rate == 0.0
    assert account.profit_factor == 0.0
    assert account.sharpe == 0.0
    assert account.best_profile is None
    assert account.worst_profile is None
    assert account.equity_curve == []
    assert account.profiles_total == 0


def test_aggregate_account_combines_the_equity_curve_per_timestamp() -> None:
    views = [view("a"), view("b", mode="live")]
    snapshots = {
        "a": [
            snapshot("a", ts="2026-09-27T17:30:00Z", portfolio_value=1000.0),
            snapshot("a", ts="2026-09-27T17:31:00Z", portfolio_value=1010.0),
        ],
        "b": [
            snapshot("b", ts="2026-09-27T17:31:00Z", portfolio_value=500.0),
            snapshot("b", ts="2026-09-27T17:32:00Z", portfolio_value=505.0),
        ],
    }
    account = metrics.aggregate_account(views, snapshots, scope="combined", now=NOW)
    assert [point.t for point in account.equity_curve] == [
        "2026-09-27T17:30:00Z",
        "2026-09-27T17:31:00Z",
        "2026-09-27T17:32:00Z",
    ]
    assert [point.value for point in account.equity_curve] == [1000.0, 1510.0, 505.0]
    assert account.profiles_healthy == 2
    assert account.sharpe != 0.0


def test_aggregate_account_window_filters_the_curve_only() -> None:
    views = [view("a", value=1100.0)]
    snapshots = {
        "a": [
            snapshot("a", ts="2026-09-22T10:00:00Z", portfolio_value=900.0),
            snapshot("a", ts="2026-09-27T17:31:00Z", portfolio_value=1100.0),
        ]
    }
    day = metrics.aggregate_account(views, snapshots, scope="paper", window="24h", now=NOW)
    week = metrics.aggregate_account(views, snapshots, scope="paper", window="7d", now=NOW)
    month = metrics.aggregate_account(views, snapshots, scope="paper", window="30d", now=NOW)
    everything = metrics.aggregate_account(views, snapshots, scope="paper", window="all", now=NOW)
    assert [point.t for point in day.equity_curve] == ["2026-09-27T17:31:00Z"]
    assert len(week.equity_curve) == 2
    assert len(month.equity_curve) == 2
    assert len(everything.equity_curve) == 2
    assert day.portfolio_value == 1100.0


def test_aggregate_account_counts_healthy_profiles_from_the_latest_snapshot() -> None:
    views = [view("a"), view("b"), view("c")]
    snapshots = {
        "a": [snapshot("a", ts="2026-09-27T17:30:00Z", healthy=True)],
        "b": [snapshot("b", ts="2026-09-27T17:31:00Z", healthy=False)],
    }
    account = metrics.aggregate_account(views, snapshots, scope="combined", now=NOW)
    assert account.profiles_healthy == 1


def test_aggregate_account_sharpe_needs_a_curve() -> None:
    flat = {
        "a": [
            snapshot("a", ts="2026-09-27T17:30:00Z", portfolio_value=1000.0),
            snapshot("a", ts="2026-09-27T17:31:00Z", portfolio_value=1000.0),
            snapshot("a", ts="2026-09-27T17:32:00Z", portfolio_value=1000.0),
        ]
    }
    constant = metrics.aggregate_account([view("a")], flat, scope="paper", now=NOW)
    single = metrics.aggregate_account(
        [view("a")], {"a": [snapshot("a", portfolio_value=1000.0)]}, scope="paper", now=NOW
    )
    assert constant.sharpe == 0.0
    assert single.sharpe == 0.0


def test_aggregate_account_counts_running_profiles_by_state() -> None:
    views = [
        view("a", state=models.STATE_RUNNING),
        view("b", state=models.STATE_BLOCKED),
        view("c", state=models.STATE_ERROR),
    ]
    account = metrics.aggregate_account(views, {}, scope="combined", now=NOW)
    assert account.profiles_total == 3
    assert account.profiles_running == 1


# ---------------------------------------------------------------------------
# strategy aggregation
# ---------------------------------------------------------------------------
def test_aggregate_strategy_uses_the_metadata_and_the_profiles() -> None:
    views = [
        view("a", value=1100.0, capital=1000.0, profit_abs=100.0, closed_trades=4, win_rate=0.5),
        view(
            "b",
            value=900.0,
            capital=1000.0,
            profit_abs=-100.0,
            closed_trades=6,
            win_rate=0.0,
            state=models.STATE_STOPPED,
        ),
    ]
    aggregated = metrics.aggregate_strategy(strategy_meta(), views)
    assert aggregated.id == "basic"
    assert aggregated.class_name == "BasicStrategy"
    assert aggregated.title == "EMA cross baseline"
    assert aggregated.category == "baseline"
    assert aggregated.indicators == ["EMA(12)", "EMA(26)"]
    assert aggregated.timeframes == ["1h", "4h"]
    assert aggregated.reference.startswith("https://www.freqtrade.io/")
    assert aggregated.risk_notes
    assert aggregated.profile_count == 2
    assert aggregated.profiles_running == 1
    assert aggregated.portfolio_value == 2000.0
    assert aggregated.profit_abs == 0.0
    assert aggregated.profit_pct == 0.0
    assert aggregated.best_profile_id == "a"
    assert aggregated.win_rate == pytest.approx(0.2)


def test_aggregate_strategy_without_profiles() -> None:
    aggregated = metrics.aggregate_strategy(strategy_meta("faber"), [])
    assert aggregated.id == "faber"
    assert aggregated.profile_count == 0
    assert aggregated.profiles_running == 0
    assert aggregated.portfolio_value == 0.0
    assert aggregated.profit_pct == 0.0
    assert aggregated.best_profile_id is None


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
def test_build_health_is_ok_as_soon_as_one_profile_runs() -> None:
    views = [view("a", state=models.STATE_RUNNING), view("b", state=models.STATE_STOPPED)]
    health = metrics.build_health(
        version="1.0.0",
        uptime_seconds=90.5,
        views=views,
        profiles_healthy=1,
        kill_switch_engaged=False,
        now=NOW,
    )
    assert health.status == "ok"
    assert health.version == "1.0.0"
    assert health.uptime_seconds == 90.5
    assert health.profiles_total == 2
    assert health.profiles_running == 1
    assert health.profiles_healthy == 1
    # Kept on the wire for the dashboard, and structurally always 0.
    assert health.profiles_queued == 0
    assert health.kill_switch_engaged is False
    assert health.generated_at == "2026-09-27T18:00:00Z"


def test_build_health_is_degraded_without_a_running_profile() -> None:
    views = [view("a", state=models.STATE_ERROR), view("b", state=models.STATE_BLOCKED)]
    health = metrics.build_health(
        version="1.0.0",
        uptime_seconds=0.0,
        views=views,
        profiles_healthy=0,
        kill_switch_engaged=True,
        now=NOW,
    )
    assert health.status == "degraded"
    assert health.profiles_running == 0
    assert health.profiles_queued == 0
    assert health.kill_switch_engaged is True


def test_build_health_splits_paper_and_live() -> None:
    views = [
        view("a", mode="paper", state=models.STATE_RUNNING),
        view("b", mode="paper", state=models.STATE_BLOCKED),
        view("c", mode="live", state=models.STATE_RUNNING),
        view("d", mode="live", state=models.STATE_ERROR),
    ]
    health = metrics.build_health(
        version="1.0.0",
        uptime_seconds=-1.0,
        views=views,
        profiles_healthy=-3,
        kill_switch_engaged=False,
        now=NOW,
    )
    assert health.profiles_paper == 2
    assert health.profiles_running_paper == 1
    assert health.profiles_live == 2
    assert health.profiles_running_live == 1
    assert health.profiles_queued == 0
    assert health.uptime_seconds == 0.0
    assert health.profiles_healthy == 0
