"""Tests for the domain models and the shared value helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from trading_platform import models


def test_taxonomy_constants() -> None:
    assert models.SUPPORTED_TIMEFRAMES == (
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "6h",
        "8h",
        "12h",
        "1d",
        "3d",
        "1w",
        "1M",
    )
    assert models.PROFILE_MODES == ("paper", "live")
    assert models.PROFILE_STATES == ("running", "stopped", "error", "blocked")
    assert models.PROFILE_SOURCES == ("catalogue", "operator")
    assert models.STRATEGY_CATEGORIES == (
        "baseline",
        "trend",
        "mean-reversion",
        "breakout",
        "allocation",
    )
    assert models.WINDOWS == ("24h", "7d", "30d", "all")


def test_state_constants_match_the_state_taxonomy() -> None:
    assert (
        models.STATE_RUNNING,
        models.STATE_STOPPED,
        models.STATE_ERROR,
        models.STATE_BLOCKED,
    ) == models.PROFILE_STATES
    # The ``queued`` state is gone: with no fleet cap nothing can be held back.
    assert not hasattr(models, "STATE_QUEUED")
    assert "queued" not in models.PROFILE_STATES


def test_utc_now_is_timezone_aware_utc() -> None:
    moment = models.utc_now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)


def test_format_ts_uses_the_canonical_format() -> None:
    moment = datetime(2026, 9, 27, 17, 31, 30, 123456, tzinfo=UTC)
    assert models.format_ts(moment) == "2026-09-27T17:31:30Z"


def test_format_ts_defaults_to_now_and_normalises_the_timezone() -> None:
    rendered = models.format_ts()
    assert rendered.endswith("Z")
    assert len(rendered) == len("2026-09-27T17:31:30Z")
    naive = datetime(2026, 9, 27, 17, 31, 30)
    assert models.format_ts(naive) == "2026-09-27T17:31:30Z"
    offset = datetime(2026, 9, 27, 19, 31, 30, tzinfo=timezone(timedelta(hours=2)))
    assert models.format_ts(offset) == "2026-09-27T17:31:30Z"


def test_parse_ts_round_trips_the_canonical_format() -> None:
    parsed = models.parse_ts("2026-09-27T17:31:30Z")
    assert parsed == datetime(2026, 9, 27, 17, 31, 30, tzinfo=UTC)
    assert parsed.tzinfo is not None
    assert models.format_ts(parsed) == "2026-09-27T17:31:30Z"


def test_parse_ts_accepts_naive_and_offset_strings() -> None:
    assert models.parse_ts("2026-09-27 17:31:30") == datetime(2026, 9, 27, 17, 31, 30, tzinfo=UTC)
    assert models.parse_ts("2026-09-27T19:31:30+02:00") == datetime(
        2026, 9, 27, 17, 31, 30, tzinfo=UTC
    )


@pytest.mark.parametrize("value", ["", "   ", "yesterday", "2026-13-45T99:99:99Z"])
def test_parse_ts_rejects_unusable_input(value: str) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        models.parse_ts(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.5, 1.5),
        (2, 2.0),
        ("3.25", 3.25),
        (None, 0.0),
        ("not-a-number", 0.0),
        (float("nan"), 0.0),
        (float("inf"), 0.0),
        (float("-inf"), 0.0),
    ],
)
def test_finite_float(value: object, expected: float) -> None:
    assert models.finite_float(value) == expected


def test_finite_float_honours_the_default() -> None:
    assert models.finite_float(float("nan"), 12.0) == 12.0
    assert models.finite_float(None, 12.0) == 12.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.57, 0.57),
        (57.0, 0.57),
        (1, 1.0),
        (100, 1.0),
        (-0.2, 0.0),
        (250.0, 1.0),
        (float("nan"), 0.0),
        (None, 0.0),
    ],
)
def test_normalise_win_rate(value: object, expected: float) -> None:
    assert models.normalise_win_rate(value) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The magnitude Freqtrade published, never rescaled: a drawdown is a
        # 0..1 ratio on the wire, exactly like every other ``*_pct`` field.
        (-0.25, 0.25),
        (0.25, 0.25),
        (-12.0, 12.0),
        (12.0, 12.0),
        (0.7793, 0.7793),
        (1.0, 1.0),
        (0.0, 0.0),
        (float("nan"), 0.0),
        (float("inf"), 0.0),
        (None, 0.0),
    ],
)
def test_normalise_drawdown_pct(value: object, expected: float) -> None:
    assert models.normalise_drawdown_pct(value) == pytest.approx(expected)


def test_window_start_covers_every_documented_window() -> None:
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    assert models.window_start("24h", now) == now - timedelta(days=1)
    assert models.window_start("7d", now) == now - timedelta(days=7)
    assert models.window_start("30d", now) == now - timedelta(days=30)
    assert models.window_start("all", now) is None


def test_window_start_defaults_to_now_and_normalises_the_value() -> None:
    before = models.utc_now() - timedelta(days=1)
    computed = models.window_start("24H")
    assert computed is not None
    assert computed >= before


def test_window_start_rejects_an_unknown_window() -> None:
    with pytest.raises(ValueError, match="unsupported window"):
        models.window_start("3h")


def test_profile_config_defaults() -> None:
    profile = models.ProfileConfig(id="p1", strategy="basic")
    assert profile.name == ""
    assert profile.timeframe == "1h"
    assert profile.mode == "paper"
    assert profile.exchange == "binance"
    assert profile.pairs == []
    assert profile.initial_capital == 1000.0
    assert profile.max_open_trades == 2
    assert profile.priority == 100
    assert profile.enabled is True
    assert profile.is_live is False
    live = models.ProfileConfig(id="p2", strategy="basic", mode="live")
    assert live.is_live is True


def test_profile_config_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        models.ProfileConfig(id="p1", strategy="basic", mode="real")


def test_profile_record_defaults_and_credentials() -> None:
    record = models.ProfileRecord(id="p1", strategy="basic", api_username="u", api_password="s")
    assert record.source == "catalogue"
    assert record.state == models.STATE_STOPPED
    assert record.state_reason is None
    assert record.pid is None
    assert record.started_at is None
    assert record.last_error is None
    assert record.created_at.endswith("Z")
    assert record.updated_at.endswith("Z")


def test_profile_view_carries_no_credentials() -> None:
    view = models.ProfileView(id="p1")
    dumped = view.model_dump()
    assert "api_username" not in dumped
    assert "api_password" not in dumped
    assert view.rank == 0
    assert view.state == models.STATE_STOPPED


def test_models_ignore_unknown_document_keys() -> None:
    view = models.ProfileView(id="p1", removed_in_v2="x")
    assert not hasattr(view, "removed_in_v2")


def test_models_sanitise_non_finite_floats() -> None:
    snapshot = models.ProfileSnapshot(
        profile_id="p1",
        portfolio_value=float("nan"),
        cash=float("inf"),
        positions_value=float("-inf"),
        profit_factor=float("inf"),
        max_drawdown_pct=float("nan"),
    )
    assert snapshot.portfolio_value == 0.0
    assert snapshot.cash == 0.0
    assert snapshot.positions_value == 0.0
    assert snapshot.profit_factor == 0.0
    assert snapshot.max_drawdown_pct == 0.0


def test_profile_snapshot_profit_factor_is_nullable() -> None:
    """A flawless record stays ``None`` through a round trip, never ``0.0``."""
    snapshot = models.ProfileSnapshot(profile_id="p1")
    assert snapshot.profit_factor is None
    assert snapshot.model_dump()["profit_factor"] is None
    restored = models.ProfileSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored.profit_factor is None
    # ``0.0`` still means "every trade lost" and is preserved as such.
    assert models.ProfileSnapshot(profile_id="p1", profit_factor=0.0).profit_factor == 0.0


def test_every_shape_carrying_a_profit_factor_is_nullable() -> None:
    """The three API shapes default to "not measurable", not to "all losses"."""
    assert models.ProfileMetrics().profit_factor is None
    assert models.ProfileView(id="p1").profit_factor is None
    assert models.AccountPerformance(scope="paper").profit_factor is None


def test_every_document_shape_serialises_to_strict_json() -> None:
    documents = _sample_documents()
    assert len(documents) == 22
    for document in documents:
        payload = document.model_dump()
        assert json.dumps(payload, allow_nan=False)
        assert json.loads(document.model_dump_json()) == json.loads(json.dumps(payload))


def _sample_documents() -> list[models._Model]:
    """One instance of every documented JSON shape, with hostile float values."""
    profile = models.ProfileView(id="p1", name="Basic BTC 1h", portfolio_value=1099.5, rank=1)
    strategy = models.StrategyView(id="basic", title="EMA cross baseline", profile_count=2)
    account = models.AccountPerformance(scope="combined", portfolio_value=1099.5)
    event = models.Event(id=1, message="profile started")
    detail = models.ProfileDetail(profile=profile, strategy=strategy)
    return [
        models.ProfileConfig(id="p1", strategy="basic"),
        models.ProfileRecord(id="p1", strategy="basic"),
        profile,
        models.ProfileSnapshot(profile_id="p1", profit_factor=float("inf")),
        models.ProfileMetrics(win_rate=0.5),
        models.StrategyMeta(id="basic", class_name="BasicStrategy", file="BasicStrategy.py"),
        strategy,
        event,
        models.EquityPoint(t="2026-09-27T17:31:30Z", value=1000.0, profit_pct=0.0),
        models.ProfileSummary(id="p1", profit_pct=0.1),
        models.DailyRow(date="2026-09-27", abs_profit=1.5, rel_profit=0.0015, trade_count=2),
        account,
        models.HealthStatus(status="ok", version="1.0.0"),
        models.DashboardSettings(state_db_path="/tmp/state.db"),
        models.CatalogueApplyResult(created=["p1"]),
        detail,
        models.ProfilesResponse(profiles=[profile], total=1),
        models.AccountResponse(paper=account, live=account, combined=account),
        models.StrategiesResponse(strategies=[strategy]),
        models.EventsResponse(events=[event]),
    ] + [
        models.ProfileResponse(profile=profile),
        models.KillSwitchResponse(kill_switch_engaged=True),
    ]


def test_health_status_only_accepts_the_two_documented_values() -> None:
    assert models.HealthStatus(status="degraded").status == "degraded"
    with pytest.raises(ValueError, match="status"):
        models.HealthStatus(status="down")


def test_timestamps_default_to_the_canonical_format() -> None:
    for ts in (
        models.ProfileRecord(id="p1", strategy="basic").created_at,
        models.ProfileSnapshot(profile_id="p1").ts,
        models.Event().ts,
        models.ProfileView(id="p1").last_updated,
        models.AccountPerformance(scope="paper").generated_at,
        models.HealthStatus(status="ok").generated_at,
        models.ProfilesResponse().generated_at,
        models.AccountResponse(
            paper=models.AccountPerformance(scope="paper"),
            live=models.AccountPerformance(scope="live"),
            combined=models.AccountPerformance(scope="combined"),
        ).generated_at,
    ):
        assert ts.endswith("Z")
        assert models.format_ts(models.parse_ts(ts)) == ts
