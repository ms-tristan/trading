"""Contract tests for the deterministic series helpers of the forecast layer.

The two properties that matter and are pinned here:

* **causality** — ``deseasonalize_log_price`` at candle ``t`` depends on candles
  ``<= t`` only, so an artifact built from a truncated candle frame is identical
  on the origins both frames share (no look-ahead, ever);
* **unit safety** — the phase arithmetic works on :class:`pandas.Timedelta`
  differences, because pandas 3 stores timestamps at microsecond resolution and
  ``DatetimeIndex.asi8`` is therefore not a nanosecond count.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.errors import ForecastError
from trading_platform.forecast.series import (
    EPOCH,
    TIMEFRAME_SECONDS,
    candle_phase,
    deseasonalize_log_price,
    timeframe_delta,
)

HOURS_PER_DAY = 24


def hourly_index(periods: int, start: str = "2024-01-01T00:00:00Z") -> pd.DatetimeIndex:
    """A deterministic, tz-aware hourly index of ``periods`` candles."""
    return pd.date_range(start, periods=periods, freq="h", tz="UTC")


# ---------------------------------------------------------------------------
# timeframe_delta
# ---------------------------------------------------------------------------


def test_timeframe_seconds_covers_the_supported_calendar() -> None:
    assert TIMEFRAME_SECONDS["1m"] == 60
    assert TIMEFRAME_SECONDS["1h"] == 3600
    assert TIMEFRAME_SECONDS["4h"] == 14400
    assert TIMEFRAME_SECONDS["1d"] == 86400
    assert TIMEFRAME_SECONDS["1w"] == 604800
    assert all(seconds > 0 for seconds in TIMEFRAME_SECONDS.values())


@pytest.mark.parametrize(
    ("timeframe", "seconds"),
    [("1m", 60), ("15m", 900), ("1h", 3600), ("1d", 86400), ("1w", 604800)],
)
def test_timeframe_delta_returns_the_candle_duration(timeframe: str, seconds: int) -> None:
    assert timeframe_delta(timeframe) == pd.Timedelta(seconds=seconds)


@pytest.mark.parametrize("timeframe", ["", "3h", "2d", "1H", "hourly"])
def test_timeframe_delta_rejects_an_unsupported_timeframe(timeframe: str) -> None:
    with pytest.raises(ForecastError) as error:
        timeframe_delta(timeframe)

    assert "1h" in str(error.value)
    assert "supported" in str(error.value)


# ---------------------------------------------------------------------------
# candle_phase
# ---------------------------------------------------------------------------


def test_epoch_is_the_unix_epoch() -> None:
    assert pd.Timestamp("1970-01-01T00:00:00Z") == EPOCH
    assert EPOCH.tz is not None


def test_candle_phase_cycles_over_the_period() -> None:
    index = hourly_index(HOURS_PER_DAY + 6)
    phases = candle_phase(index, timeframe="1h", period=HOURS_PER_DAY)

    assert phases.dtype == np.int64
    assert phases.shape == (HOURS_PER_DAY + 6,)
    assert phases.tolist() == [*range(HOURS_PER_DAY), 0, 1, 2, 3, 4, 5]


def test_candle_phase_localises_a_naive_index_to_utc() -> None:
    naive = pd.date_range("2024-01-01", periods=3, freq="h")
    aware = pd.date_range("2024-01-01T00:00:00Z", periods=3, freq="h")

    np.testing.assert_array_equal(
        candle_phase(naive, timeframe="1h", period=HOURS_PER_DAY),
        candle_phase(aware, timeframe="1h", period=HOURS_PER_DAY),
    )


def test_candle_phase_converts_an_aware_index_to_utc() -> None:
    eastern = pd.date_range("2024-01-01", periods=2, freq="h", tz="US/Eastern")
    # 2024-01-01T00:00-05:00 is 05:00 UTC, i.e. phase 5 of the daily cycle.
    assert candle_phase(eastern, timeframe="1h", period=HOURS_PER_DAY).tolist() == [5, 6]


def test_candle_phase_of_a_period_of_one_is_always_zero() -> None:
    phases = candle_phase(hourly_index(10), timeframe="1h", period=1)
    np.testing.assert_array_equal(phases, np.zeros(10, dtype="int64"))


def test_candle_phase_uses_the_timeframe() -> None:
    index = pd.date_range("2024-01-01T00:00:00Z", periods=13, freq="4h")
    phases = candle_phase(index, timeframe="4h", period=6)

    assert phases.tolist() == [0, 1, 2, 3, 4, 5, 0, 1, 2, 3, 4, 5, 0]


def test_candle_phase_of_an_empty_index_is_empty() -> None:
    phases = candle_phase(pd.DatetimeIndex([]), timeframe="1h", period=HOURS_PER_DAY)

    assert phases.dtype == np.int64
    assert phases.shape == (0,)


@pytest.mark.parametrize("period", [0, -1])
def test_candle_phase_rejects_a_non_positive_period(period: int) -> None:
    with pytest.raises(ForecastError, match="period must be >= 1"):
        candle_phase(hourly_index(3), timeframe="1h", period=period)


def test_candle_phase_rejects_an_unsupported_timeframe() -> None:
    with pytest.raises(ForecastError):
        candle_phase(hourly_index(3), timeframe="3h", period=HOURS_PER_DAY)


def test_candle_phase_does_not_change_the_index() -> None:
    index = hourly_index(5)
    snapshot = index.copy()

    candle_phase(index, timeframe="1h", period=HOURS_PER_DAY)

    pd.testing.assert_index_equal(index, snapshot)


# ---------------------------------------------------------------------------
# deseasonalize_log_price
# ---------------------------------------------------------------------------


def test_deseasonalize_disabled_window_returns_the_log_price() -> None:
    close = pd.Series([1.0, 2.0, 4.0, 8.0], index=hourly_index(4), name="close")

    result = deseasonalize_log_price(close, timeframe="1h", period=HOURS_PER_DAY, window=1)

    np.testing.assert_allclose(result.to_numpy(), np.log(close.to_numpy()), rtol=1e-12)
    assert result.name == "deseasonalized_log_price"
    assert result.dtype == np.float64
    pd.testing.assert_index_equal(result.index, close.index)


def test_deseasonalize_is_causal_on_a_small_hand_computed_case() -> None:
    """With ``period == 3`` and ``window == 6`` the seasonal term averages two same-phase candles."""
    close = pd.Series([1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0], index=hourly_index(7), name="close")

    result = deseasonalize_log_price(close, timeframe="1h", period=3, window=6)

    values = result.to_numpy()
    # The first occurrence of every phase has a single observation: z == 0 exactly.
    np.testing.assert_allclose(values[:3], [0.0, 0.0, 0.0], atol=1e-12)
    # Afterwards the seasonal term is the mean of the candle and its 3-candle-old twin,
    # so z is half of the three-candle log return.
    np.testing.assert_allclose(
        values[3:],
        [
            (np.log(8.0) - np.log(1.0)) / 2.0,
            (np.log(16.0) - np.log(2.0)) / 2.0,
            (np.log(32.0) - np.log(4.0)) / 2.0,
            (np.log(64.0) - np.log(8.0)) / 2.0,
        ],
        rtol=1e-12,
    )
    assert result.name == "deseasonalized_log_price"
    assert result.dtype == np.float64


def test_deseasonalize_disabled_by_a_window_equal_to_one_period() -> None:
    """``window <= period`` means one same-phase observation, hence a constant seasonal."""
    close = pd.Series([1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0], index=hourly_index(7), name="close")

    result = deseasonalize_log_price(close, timeframe="1h", period=3, window=3)

    np.testing.assert_allclose(result.to_numpy(), np.zeros(7), atol=1e-12)


def test_deseasonalize_never_looks_at_a_later_candle() -> None:
    index = hourly_index(72)
    values = 100.0 * np.exp(0.01 * np.arange(72, dtype="float64"))
    close = pd.Series(values, index=index, name="close")

    full = deseasonalize_log_price(close, timeframe="1h", period=HOURS_PER_DAY, window=48)
    truncated = deseasonalize_log_price(
        close.iloc[:-1], timeframe="1h", period=HOURS_PER_DAY, window=48
    )

    np.testing.assert_array_equal(full.to_numpy()[:-1], truncated.to_numpy())

    # Changing the last candle may only change the last de-seasonalised value.
    changed = close.copy()
    changed.iloc[-1] = changed.iloc[-1] * 1.5
    after = deseasonalize_log_price(changed, timeframe="1h", period=HOURS_PER_DAY, window=48)

    np.testing.assert_array_equal(full.to_numpy()[:-1], after.to_numpy()[:-1])
    assert after.to_numpy()[-1] != full.to_numpy()[-1]


def test_deseasonalize_removes_a_repeating_daily_pattern() -> None:
    index = hourly_index(24 * 14)
    steps = np.arange(len(index), dtype="float64")
    seasonal = 0.05 * np.sin(2.0 * np.pi * (steps % HOURS_PER_DAY) / HOURS_PER_DAY)
    trend = 0.0005 * steps
    close = pd.Series(np.exp(trend + seasonal), index=index, name="close")

    result = deseasonalize_log_price(close, timeframe="1h", period=HOURS_PER_DAY, window=24 * 7)

    log_price = np.log(close.to_numpy())
    assert float(np.std(result.to_numpy()[24 * 7 :])) < 0.2 * float(np.std(log_price[24 * 7 :]))


def test_deseasonalize_handles_a_naive_index_like_a_utc_one() -> None:
    values = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0]
    aware = pd.Series(values, index=hourly_index(len(values)), name="close")
    naive = pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values), freq="h"))

    first = deseasonalize_log_price(naive, timeframe="1h", period=3, window=6)
    second = deseasonalize_log_price(aware, timeframe="1h", period=3, window=6)

    np.testing.assert_array_equal(first.to_numpy(), second.to_numpy())


def test_deseasonalize_never_mutates_the_input() -> None:
    close = pd.Series([1.0, 2.0, 4.0, 8.0], index=hourly_index(4), name="close")
    snapshot = close.copy()

    deseasonalize_log_price(close, timeframe="1h", period=HOURS_PER_DAY, window=48)

    pd.testing.assert_series_equal(close, snapshot)


def test_deseasonalize_of_an_empty_series_is_empty() -> None:
    close = pd.Series([], index=pd.DatetimeIndex([]), name="close", dtype="float64")

    result = deseasonalize_log_price(close, timeframe="1h", period=HOURS_PER_DAY, window=48)

    assert result.empty
    assert result.name == "deseasonalized_log_price"
    assert result.dtype == np.float64


@pytest.mark.parametrize("bad", [0.0, -1.0, -0.5])
def test_deseasonalize_rejects_a_non_positive_price(bad: float) -> None:
    close = pd.Series([1.0, 2.0, bad], index=hourly_index(3), name="close")

    with pytest.raises(ForecastError, match="strictly positive"):
        deseasonalize_log_price(close, timeframe="1h", period=HOURS_PER_DAY, window=48)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_deseasonalize_rejects_a_non_finite_price(bad: float) -> None:
    close = pd.Series([1.0, bad, 4.0], index=hourly_index(3), name="close")

    with pytest.raises(ForecastError, match="finite"):
        deseasonalize_log_price(close, timeframe="1h", period=HOURS_PER_DAY, window=48)


def test_deseasonalize_rejects_a_non_series_input() -> None:
    with pytest.raises(ForecastError, match="pandas Series"):
        deseasonalize_log_price(  # type: ignore[arg-type]
            [1.0, 2.0, 3.0], timeframe="1h", period=HOURS_PER_DAY, window=48
        )


def test_deseasonalize_rejects_a_non_numeric_series() -> None:
    close = pd.Series(["1.0", "oops", "4.0"], index=hourly_index(3), name="close")

    with pytest.raises(ForecastError, match="numeric"):
        deseasonalize_log_price(close, timeframe="1h", period=HOURS_PER_DAY, window=48)


def test_deseasonalize_rejects_a_non_integer_window() -> None:
    close = pd.Series([1.0, 2.0, 4.0], index=hourly_index(3), name="close")

    with pytest.raises(ForecastError, match="window must be an integer"):
        deseasonalize_log_price(close, timeframe="1h", period=HOURS_PER_DAY, window="many")


def test_deseasonalize_rejects_an_unsupported_timeframe() -> None:
    close = pd.Series([1.0, 2.0, 4.0], index=hourly_index(3), name="close")

    with pytest.raises(ForecastError, match="unsupported timeframe"):
        deseasonalize_log_price(close, timeframe="3h", period=HOURS_PER_DAY, window=48)


@pytest.mark.parametrize("period", [0, -3])
def test_deseasonalize_rejects_a_non_positive_period(period: int) -> None:
    close = pd.Series([1.0, 2.0, 4.0], index=hourly_index(3), name="close")

    with pytest.raises(ForecastError, match="period must be >= 1"):
        deseasonalize_log_price(close, timeframe="1h", period=period, window=48)


def test_deseasonalize_validates_the_period_even_when_disabled() -> None:
    close = pd.Series([1.0, 2.0, 4.0], index=hourly_index(3), name="close")

    with pytest.raises(ForecastError, match="period must be >= 1"):
        deseasonalize_log_price(close, timeframe="1h", period=0, window=1)
