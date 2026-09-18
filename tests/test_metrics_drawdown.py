"""Tests for the drawdown metrics (``trading_platform.metrics.drawdown``).

Everything here is offline and deterministic: the curves are hand-built so the
expected values are exact and can be checked by reading the test.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from trading_platform.core.constants import OHLCV_INDEX_NAME, UTC
from trading_platform.metrics import (
    drawdown_duration,
    drawdown_series,
    drawdown_table,
    max_drawdown,
)

EPISODE_KEYS = {
    "start",
    "trough",
    "end",
    "depth",
    "duration_minutes",
    "recovery_minutes",
}


def make_equity(values: list[float]) -> pd.Series:
    """Hourly equity curve starting 2024-01-01T00:00:00Z, one point per value."""
    index = pd.date_range(
        "2024-01-01T00:00:00Z",
        periods=len(values),
        freq="h",
        tz=UTC,
        name=OHLCV_INDEX_NAME,
    )
    return pd.Series(
        [float(value) for value in values], index=index, name="equity", dtype="float64"
    )


def empty_equity() -> pd.Series:
    """Empty equity curve carrying a proper UTC ``DatetimeIndex``."""
    index = pd.DatetimeIndex([], tz=UTC, name=OHLCV_INDEX_NAME)
    return pd.Series([], index=index, name="equity", dtype="float64")


# --- drawdown_series --------------------------------------------------------


def test_drawdown_series_exact_values_on_known_curve() -> None:
    equity = make_equity([100.0, 110.0, 105.0, 90.0, 95.0, 120.0, 118.0])

    drawdown = drawdown_series(equity)

    expected = [
        0.0,
        0.0,
        105.0 / 110.0 - 1.0,
        90.0 / 110.0 - 1.0,
        95.0 / 110.0 - 1.0,
        0.0,
        118.0 / 120.0 - 1.0,
    ]
    assert list(drawdown) == pytest.approx(expected, rel=1e-12, abs=1e-15)
    assert drawdown.dtype == "float64"
    assert list(drawdown.index) == list(equity.index)
    assert drawdown.name == "drawdown"


def test_drawdown_series_is_never_positive_on_rising_falling_and_crashing_curves() -> None:
    curves = {
        "rising": [100.0, 101.0, 102.5, 110.0],
        "falling": [110.0, 100.0, 95.0, 80.0],
        "crashing": [100.0, 50.0, 10.0, 1.0, 0.5],
    }

    for name, values in curves.items():
        drawdown = drawdown_series(make_equity(values))
        assert (drawdown <= 1e-12).all(), name
        assert all(math.isfinite(value) for value in drawdown), name


def test_drawdown_series_empty_input_returns_empty_float_series() -> None:
    drawdown = drawdown_series(empty_equity())

    assert drawdown.empty
    assert drawdown.dtype == "float64"
    assert list(drawdown.index) == []


def test_drawdown_series_flat_curve_is_all_zero() -> None:
    drawdown = drawdown_series(make_equity([1000.0] * 6))

    assert list(drawdown) == [0.0] * 6


def test_drawdown_series_accepts_non_positive_equity_without_raising() -> None:
    drawdown = drawdown_series(make_equity([-100.0, -50.0, 0.0, 10.0, -20.0]))

    assert len(drawdown) == 5
    assert (drawdown <= 1e-12).all()
    assert all(math.isfinite(value) for value in drawdown)


def test_drawdown_helpers_accept_a_plain_sequence() -> None:
    drawdown = drawdown_series([100.0, 110.0, 100.0])

    assert len(drawdown) == 3
    assert drawdown.iloc[-1] == pytest.approx(100.0 / 110.0 - 1.0, rel=1e-12)
    assert max_drawdown([100.0, 110.0, 100.0]) == pytest.approx(100.0 / 110.0 - 1.0, rel=1e-12)


# --- max_drawdown -----------------------------------------------------------


def test_max_drawdown_exact_on_known_curve() -> None:
    equity = make_equity([100.0, 110.0, 105.0, 90.0, 95.0, 120.0, 118.0])

    assert max_drawdown(equity) == pytest.approx(90.0 / 110.0 - 1.0, rel=1e-12)


def test_max_drawdown_is_zero_on_monotonic_and_flat_curves() -> None:
    assert max_drawdown(make_equity([100.0, 105.0, 120.0, 130.0])) == 0.0
    assert max_drawdown(make_equity([50.0, 50.0, 50.0])) == 0.0


def test_max_drawdown_is_zero_on_empty_curve() -> None:
    assert max_drawdown(empty_equity()) == 0.0


def test_max_drawdown_never_positive() -> None:
    assert max_drawdown(make_equity([100.0, 1.0, 200.0])) <= 0.0


# --- drawdown_duration ------------------------------------------------------


def test_drawdown_duration_counts_four_underwater_candles() -> None:
    # index:            0     1    2    3    4    5     6
    equity = make_equity([100.0, 100.0, 90.0, 80.0, 70.0, 60.0, 100.0])

    assert drawdown_duration(equity) == 4


def test_drawdown_duration_resets_after_recovery() -> None:
    # Two separate underwater runs: 2 candles then 5 candles.
    equity = make_equity([100.0, 90.0, 80.0, 100.0, 95.0, 90.0, 85.0, 80.0, 75.0, 100.0])

    assert drawdown_duration(equity) == 5


def test_drawdown_duration_is_zero_when_monotonic_or_flat() -> None:
    assert drawdown_duration(make_equity([100.0, 101.0, 102.0, 103.0])) == 0
    assert drawdown_duration(make_equity([42.0, 42.0, 42.0])) == 0


def test_drawdown_duration_is_zero_on_empty_curve() -> None:
    assert drawdown_duration(empty_equity()) == 0


def test_drawdown_duration_counts_the_trailing_open_drawdown() -> None:
    equity = make_equity([100.0, 90.0, 80.0])

    assert drawdown_duration(equity) == 2


# --- drawdown_table ---------------------------------------------------------


def test_drawdown_table_episodes_are_ordered_by_depth_and_carry_iso_timestamps() -> None:
    equity = make_equity([100.0, 110.0, 105.0, 90.0, 95.0, 120.0, 118.0])

    table = drawdown_table(equity)

    assert len(table) == 2
    for episode in table:
        assert set(episode) == EPISODE_KEYS
        for key in ("start", "trough", "end"):
            assert isinstance(episode[key], str)
            pd.Timestamp(episode[key])

    deepest, shallowest = table
    assert deepest["depth"] == pytest.approx(90.0 / 110.0 - 1.0, rel=1e-12)
    assert deepest["start"] == "2024-01-01T02:00:00+00:00"
    assert deepest["trough"] == "2024-01-01T03:00:00+00:00"
    assert deepest["end"] == "2024-01-01T05:00:00+00:00"
    assert deepest["duration_minutes"] == 180.0
    assert deepest["recovery_minutes"] == 120.0

    assert shallowest["depth"] == pytest.approx(118.0 / 120.0 - 1.0, rel=1e-12)
    assert shallowest["start"] == "2024-01-01T06:00:00+00:00"
    assert shallowest["trough"] == "2024-01-01T06:00:00+00:00"
    assert shallowest["end"] == "2024-01-01T06:00:00+00:00"
    assert shallowest["duration_minutes"] == 0.0
    assert shallowest["recovery_minutes"] == 0.0

    assert float(deepest["depth"]) < float(shallowest["depth"])


def test_drawdown_table_top_keeps_the_deepest_episode() -> None:
    equity = make_equity([100.0, 80.0, 100.0, 95.0, 100.0, 98.0, 100.0])

    everything = drawdown_table(equity)
    limited = drawdown_table(equity, top=1)

    assert len(everything) == 3
    assert len(limited) == 1
    assert limited[0] == everything[0]
    assert limited[0]["depth"] == pytest.approx(80.0 / 100.0 - 1.0, rel=1e-12)
    assert limited[0]["start"] == "2024-01-01T01:00:00+00:00"
    assert limited[0]["duration_minutes"] == 60.0
    assert limited[0]["recovery_minutes"] == 60.0


def test_drawdown_table_on_monotonic_curve_and_zero_top_are_empty() -> None:
    monotonic = make_equity([100.0, 110.0, 120.0])

    assert drawdown_table(monotonic) == []
    assert drawdown_table(monotonic, top=0) == []
    assert drawdown_table(empty_equity()) == []


def test_drawdown_table_handles_an_unrecovered_drawdown() -> None:
    equity = make_equity([100.0, 70.0, 60.0, 65.0])

    table = drawdown_table(equity)

    assert len(table) == 1
    episode = table[0]
    assert episode["trough"] == "2024-01-01T02:00:00+00:00"
    assert episode["end"] == "2024-01-01T03:00:00+00:00"
    assert episode["duration_minutes"] == 120.0
    assert episode["recovery_minutes"] == 60.0
