"""Unit tests of the pure indicator layer.

Everything here is offline and deterministic: the expected values are
hand-computed (or checked against the pandas expression the contract mandates),
never taken from an external library.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.errors import StrategyError
from trading_platform.strategy.indicators import (
    BollingerBands,
    atr,
    bollinger_bands,
    ema,
    rolling_max,
    rolling_min,
    rolling_std,
    rsi,
    sma,
    true_range,
)

START = "2024-01-01T00:00:00Z"


def _series(values: object, *, name: str = "close") -> pd.Series:
    """Build a tz-aware float64 Series from ``values``."""
    array = np.asarray(values, dtype="float64")
    index = pd.date_range(START, periods=array.size, freq="h", tz="UTC", name="timestamp")
    return pd.Series(array, index=index, name=name, dtype="float64")


def _assert_same_index(result: pd.Series, source: pd.Series) -> None:
    assert isinstance(result, pd.Series)
    assert result.dtype == np.dtype("float64")
    assert result.index.equals(source.index)
    assert not np.isinf(result.to_numpy(dtype="float64")).any()


# ---------------------------------------------------------------------------
# ema
# ---------------------------------------------------------------------------


def test_ema_hand_computed_values() -> None:
    close = _series([1.0, 2.0, 3.0, 4.0, 5.0])

    result = ema(close, 3)

    # alpha = 2 / (3 + 1) = 0.5, first value at position 2 (min_periods = 3)
    assert result.isna().to_numpy().tolist() == [True, True, False, False, False]
    np.testing.assert_allclose(
        result.to_numpy(dtype="float64")[2:], [2.25, 3.125, 4.0625], rtol=0.0, atol=1e-12
    )
    _assert_same_index(result, close)


def test_ema_matches_the_mandated_pandas_expression() -> None:
    close = _series(np.linspace(100.0, 130.0, 40))

    expected = close.ewm(span=7, adjust=False, min_periods=7).mean()

    pd.testing.assert_series_equal(ema(close, 7), expected)


def test_ema_nan_prefix_and_index_are_preserved() -> None:
    close = _series(np.arange(1.0, 31.0), name="price")

    result = ema(close, 14)

    assert int(result.isna().sum()) == 13
    assert result.index.equals(close.index)
    assert result.index.name == "timestamp"
    assert result.name == "price"


def test_ema_period_one_is_the_identity() -> None:
    close = _series([3.0, 1.0, 4.0, 1.0, 5.0])

    pd.testing.assert_series_equal(ema(close, 1), close)


@pytest.mark.parametrize("period", [0, -1, -14])
def test_ema_invalid_period_raises(period: int) -> None:
    with pytest.raises(StrategyError, match="period must be >= 1"):
        ema(_series([1.0, 2.0, 3.0]), period)


def test_ema_rejects_non_series_and_non_numeric_input() -> None:
    with pytest.raises(StrategyError, match="must be a pandas Series"):
        ema([1.0, 2.0, 3.0], 2)  # type: ignore[arg-type]
    with pytest.raises(StrategyError, match="numeric"):
        ema(pd.Series(["a", "b", "c"]), 2)


@pytest.mark.parametrize("period", [None, "3"])
def test_ema_rejects_a_non_integer_period(period: object) -> None:
    with pytest.raises(StrategyError, match="integer"):
        ema(_series([1.0, 2.0, 3.0]), period)  # type: ignore[arg-type]


def test_ema_rejects_a_fractional_period() -> None:
    with pytest.raises(StrategyError, match=r"must be an integer, got 2\.5"):
        ema(_series([1.0, 2.0, 3.0]), 2.5)


def test_ema_accepts_numeric_strings() -> None:
    text = pd.Series(["1", "2", "3", "4", "5"], dtype=object)

    result = ema(text, 3)

    np.testing.assert_allclose(
        result.to_numpy(dtype="float64")[2:], [2.25, 3.125, 4.0625], rtol=0.0, atol=1e-12
    )


def test_ema_never_mutates_its_input() -> None:
    close = _series([1.0, 2.0, 3.0, 4.0])
    before = close.copy(deep=True)

    ema(close, 2)

    pd.testing.assert_series_equal(close, before)


# ---------------------------------------------------------------------------
# rsi
# ---------------------------------------------------------------------------


def test_rsi_strictly_rising_is_100() -> None:
    close = _series(np.arange(1.0, 31.0))

    result = rsi(close, 14)

    assert int(result.isna().sum()) == 14
    np.testing.assert_allclose(result.to_numpy(dtype="float64")[14:], 100.0)
    assert float(result.max()) <= 100.0


def test_rsi_strictly_falling_is_0() -> None:
    close = _series(np.arange(30.0, 0.0, -1.0))

    result = rsi(close, 14)

    np.testing.assert_allclose(result.to_numpy(dtype="float64")[14:], 0.0)


def test_rsi_flat_series_is_the_neutral_50() -> None:
    close = _series([42.0] * 30)

    result = rsi(close, 14)

    np.testing.assert_allclose(result.to_numpy(dtype="float64")[14:], 50.0)


def test_rsi_hand_computed_wilder_values() -> None:
    close = _series([100.0, 99, 98, 97, 96, 95, 96, 97, 98, 99, 100, 101])

    result = rsi(close, 2)

    expected = [np.nan, np.nan, 0.0, 0.0, 0.0, 0.0, 50.0, 75.0, 87.5, 93.75, 96.875, 98.4375]
    np.testing.assert_allclose(result.to_numpy(dtype="float64"), expected, rtol=0.0, atol=1e-12)
    _assert_same_index(result, close)
    assert result.name == "rsi"


def test_rsi_bounds_and_nan_prefix() -> None:
    close = _series(np.sin(np.linspace(0.0, 12.0, 120)) * 10.0 + 100.0)

    result = rsi(close, 14)

    values = result.to_numpy(dtype="float64")
    assert np.isnan(values[:14]).all()
    assert np.isfinite(values[14:]).all()
    assert float(values[14:].min()) >= 0.0
    assert float(values[14:].max()) <= 100.0
    assert result.index.equals(close.index)


def test_rsi_short_series_has_no_value() -> None:
    close = _series([1.0, 2.0, 3.0])

    result = rsi(close, 14)

    assert result.isna().all()
    assert result.dtype == np.dtype("float64")


def test_rsi_empty_series_is_empty() -> None:
    close = _series([])

    result = rsi(close, 14)

    assert result.empty
    assert result.dtype == np.dtype("float64")


@pytest.mark.parametrize("period", [0, -3])
def test_rsi_invalid_period_raises(period: int) -> None:
    with pytest.raises(StrategyError, match="period must be >= 1"):
        rsi(_series([1.0, 2.0, 3.0]), period)


def test_rsi_never_mutates_its_input() -> None:
    close = _series(np.linspace(10.0, 20.0, 25))
    before = close.copy(deep=True)

    rsi(close, 5)

    pd.testing.assert_series_equal(close, before)


# ---------------------------------------------------------------------------
# true_range
# ---------------------------------------------------------------------------


def test_true_range_first_candle_falls_back_to_high_minus_low() -> None:
    close = _series([10.0, 11.0, 12.0, 11.0, 13.0])
    high = close + 1.0
    low = close - 1.0

    result = true_range(high, low, close)

    np.testing.assert_allclose(result.to_numpy(dtype="float64"), [2.0, 2.0, 2.0, 2.0, 3.0])
    assert result.name == "true_range"
    _assert_same_index(result, close)


def test_true_range_honours_an_explicit_previous_close() -> None:
    close = _series([10.0, 11.0, 12.0])
    high = _series([11.0, 12.0, 13.0], name="high")
    low = _series([9.0, 10.0, 11.0], name="low")
    previous = _series([np.nan, 10.0, 11.0], name="previous_close")

    explicit = true_range(high, low, close, previous_close=previous)
    implicit = true_range(high, low, close)

    pd.testing.assert_series_equal(explicit, implicit)


def test_true_range_uses_the_widest_leg() -> None:
    close = _series([10.0, 11.5, 7.0, 7.5])
    high = _series([10.5, 12.0, 11.0, 8.0])
    low = _series([9.5, 11.0, 6.0, 7.0])

    result = true_range(high, low, close)

    # row 0: high - low fallback; row 1: gap leg (|12 - 10|); row 2: low leg
    # (|6 - 11.5|); row 3: high - low
    np.testing.assert_allclose(result.to_numpy(dtype="float64"), [1.0, 2.0, 5.5, 1.0])


def test_true_range_rejects_mismatched_lengths_and_bad_types() -> None:
    with pytest.raises(StrategyError, match="same length"):
        true_range(_series([1.0, 2.0]), _series([1.0]), _series([1.0, 2.0]))
    with pytest.raises(StrategyError, match="must be a pandas Series"):
        true_range([1.0, 2.0], [1.0, 2.0], [1.0, 2.0])  # type: ignore[arg-type]
    with pytest.raises(StrategyError, match="same length"):
        true_range(
            _series([1.0, 2.0]),
            _series([1.0, 2.0]),
            _series([1.0, 2.0]),
            previous_close=_series([1.0]),
        )


def test_true_range_never_mutates_its_inputs() -> None:
    close = _series([10.0, 11.0, 12.0])
    high = close + 1.0
    low = close - 1.0
    snapshots = (high.copy(deep=True), low.copy(deep=True), close.copy(deep=True))

    true_range(high, low, close)

    for original, snapshot in zip((high, low, close), snapshots, strict=True):
        pd.testing.assert_series_equal(original, snapshot)


# ---------------------------------------------------------------------------
# atr
# ---------------------------------------------------------------------------


def test_atr_hand_computed_wilder_values() -> None:
    close = _series([10.0, 11.0, 12.0, 11.0, 13.0])
    high = close + 1.0
    low = close - 1.0

    result = atr(high, low, close, 2)

    # TR = [2, 2, 2, 2, 3]; seed = SMA(TR[1:3]) = 2 at position 2, then Wilder:
    # 0.5 * 2 + 0.5 * TR[3] = 2, 0.5 * 2 + 0.5 * TR[4] = 2.5
    np.testing.assert_allclose(
        result.to_numpy(dtype="float64"), [np.nan, np.nan, 2.0, 2.0, 2.5], rtol=0.0, atol=1e-12
    )
    assert result.name == "atr"


def test_atr_nan_prefix_positive_and_index_preserved() -> None:
    close = _series(np.linspace(100.0, 140.0, 60) + np.sin(np.linspace(0.0, 9.0, 60)))
    high = close * 1.002
    low = close * 0.998

    result = atr(high, low, close, 14)

    values = result.to_numpy(dtype="float64")
    assert np.isnan(values[:14]).all()
    assert (values[14:] > 0.0).all()
    assert result.index.equals(close.index)
    assert result.index.name == "timestamp"
    _assert_same_index(result, close)


def test_atr_short_and_empty_inputs_are_all_nan() -> None:
    close = _series([10.0, 11.0])
    short = atr(close + 1.0, close - 1.0, close, 14)
    assert short.isna().all()
    assert short.size == 2

    empty_close = _series([])
    empty = atr(empty_close, empty_close, empty_close, 14)
    assert empty.empty


@pytest.mark.parametrize("period", [0, -2])
def test_atr_invalid_period_raises(period: int) -> None:
    close = _series([10.0, 11.0, 12.0])
    with pytest.raises(StrategyError, match="period must be >= 1"):
        atr(close, close, close, period)


def test_atr_never_mutates_its_inputs() -> None:
    close = _series([10.0, 11.0, 12.0, 11.5])
    high = close + 1.0
    low = close - 1.0
    snapshots = (high.copy(deep=True), low.copy(deep=True), close.copy(deep=True))

    atr(high, low, close, 2)

    for original, snapshot in zip((high, low, close), snapshots, strict=True):
        pd.testing.assert_series_equal(original, snapshot)


# ---------------------------------------------------------------------------
# rolling_max / rolling_min
# ---------------------------------------------------------------------------

ROLLING_EXTREMES: list[Callable[[pd.Series, int], pd.Series]] = [rolling_max, rolling_min]


def test_rolling_max_hand_computed_values() -> None:
    close = _series([1.0, 3.0, 2.0, 5.0, 4.0])

    result = rolling_max(close, 3)

    np.testing.assert_allclose(
        result.to_numpy(dtype="float64"), [np.nan, np.nan, 3.0, 5.0, 5.0], rtol=0.0, atol=1e-12
    )
    _assert_same_index(result, close)


def test_rolling_min_hand_computed_values() -> None:
    close = _series([1.0, 3.0, 2.0, 5.0, 4.0])

    result = rolling_min(close, 3)

    np.testing.assert_allclose(
        result.to_numpy(dtype="float64"), [np.nan, np.nan, 1.0, 2.0, 2.0], rtol=0.0, atol=1e-12
    )
    _assert_same_index(result, close)


def test_rolling_max_matches_the_mandated_pandas_expression() -> None:
    close = _series(np.linspace(100.0, 130.0, 40))

    expected = close.rolling(window=7, min_periods=7).max().rename("rolling_max")

    pd.testing.assert_series_equal(rolling_max(close, 7), expected)


def test_rolling_min_matches_the_mandated_pandas_expression() -> None:
    close = _series(np.linspace(100.0, 130.0, 40))

    expected = close.rolling(window=7, min_periods=7).min().rename("rolling_min")

    pd.testing.assert_series_equal(rolling_min(close, 7), expected)


@pytest.mark.parametrize("func", ROLLING_EXTREMES)
def test_rolling_extremes_nan_prefix_name_and_index(
    func: Callable[[pd.Series, int], pd.Series],
) -> None:
    close = _series(np.arange(1.0, 31.0), name="price")

    result = func(close, 14)

    assert result.isna().to_numpy().tolist() == [True] * 13 + [False] * 17
    assert result.index.equals(close.index)
    assert result.index.name == "timestamp"
    assert result.name == func.__name__


@pytest.mark.parametrize("func", ROLLING_EXTREMES)
def test_rolling_extremes_period_one_is_the_identity(
    func: Callable[[pd.Series, int], pd.Series],
) -> None:
    close = _series([3.0, 1.0, 4.0, 1.0, 5.0])

    result = func(close, 1)

    pd.testing.assert_series_equal(result, close.rename(func.__name__))


@pytest.mark.parametrize("func", ROLLING_EXTREMES)
@pytest.mark.parametrize("period", [0, -1, -14])
def test_rolling_extremes_invalid_period_raises(
    func: Callable[[pd.Series, int], pd.Series], period: int
) -> None:
    with pytest.raises(StrategyError, match="period must be >= 1"):
        func(_series([1.0, 2.0, 3.0]), period)


@pytest.mark.parametrize("func", ROLLING_EXTREMES)
def test_rolling_extremes_reject_non_series_and_non_numeric_input(
    func: Callable[[pd.Series, int], pd.Series],
) -> None:
    with pytest.raises(StrategyError, match="must be a pandas Series"):
        func([1.0, 2.0, 3.0], 2)  # type: ignore[arg-type]
    with pytest.raises(StrategyError, match="numeric"):
        func(pd.Series(["a", "b", "c"]), 2)


@pytest.mark.parametrize("func", ROLLING_EXTREMES)
@pytest.mark.parametrize("period", [None, "3"])
def test_rolling_extremes_reject_a_non_integer_period(
    func: Callable[[pd.Series, int], pd.Series], period: object
) -> None:
    with pytest.raises(StrategyError, match="integer"):
        func(_series([1.0, 2.0, 3.0]), period)  # type: ignore[arg-type]


@pytest.mark.parametrize("func", ROLLING_EXTREMES)
def test_rolling_extremes_reject_a_fractional_period(
    func: Callable[[pd.Series, int], pd.Series],
) -> None:
    with pytest.raises(StrategyError, match=r"must be an integer, got 2\.5"):
        func(_series([1.0, 2.0, 3.0]), 2.5)


@pytest.mark.parametrize("func", ROLLING_EXTREMES)
def test_rolling_extremes_short_and_empty_inputs_are_all_nan(
    func: Callable[[pd.Series, int], pd.Series],
) -> None:
    short = func(_series([1.0, 2.0, 3.0]), 10)

    assert short.isna().all()
    assert short.size == 3
    assert short.dtype == np.dtype("float64")

    empty = func(_series([]), 3)

    assert empty.empty
    assert empty.dtype == np.dtype("float64")


@pytest.mark.parametrize("func", ROLLING_EXTREMES)
def test_rolling_extremes_accept_numeric_strings(
    func: Callable[[pd.Series, int], pd.Series],
) -> None:
    text = pd.Series(["1", "3", "2", "5", "4"], dtype=object)

    result = func(text, 3)

    expected = {
        "rolling_max": [np.nan, np.nan, 3.0, 5.0, 5.0],
        "rolling_min": [np.nan] * 2 + [1.0, 2.0, 2.0],
    }
    np.testing.assert_allclose(
        result.to_numpy(dtype="float64"), expected[func.__name__], rtol=0.0, atol=1e-12
    )


@pytest.mark.parametrize("func", ROLLING_EXTREMES)
def test_rolling_extremes_infinite_input_never_leaks(
    func: Callable[[pd.Series, int], pd.Series],
) -> None:
    close = _series([1.0, 2.0, np.inf, 4.0, 5.0, 6.0])

    result = func(close, 2)

    _assert_same_index(result, close)
    # The two windows holding the infinite observation are undefined (NaN), the
    # untouched windows keep their finite extreme.
    assert np.isnan(result.to_numpy(dtype="float64")[2:4]).all()
    expected_last = {"rolling_max": 5.0, "rolling_min": 4.0}[func.__name__]
    np.testing.assert_allclose(
        result.to_numpy(dtype="float64")[4:],
        [expected_last, expected_last + 1.0],
        rtol=0.0,
        atol=1e-12,
    )


@pytest.mark.parametrize("func", ROLLING_EXTREMES)
def test_rolling_extremes_never_mutate_and_are_deterministic(
    func: Callable[[pd.Series, int], pd.Series],
) -> None:
    close = _series([1.0, 3.0, 2.0, 5.0, 4.0])
    before = close.copy(deep=True)

    first = func(close, 3)
    second = func(close, 3)

    pd.testing.assert_series_equal(first, second)
    pd.testing.assert_series_equal(close, before)


# ---------------------------------------------------------------------------
# rolling_std
# ---------------------------------------------------------------------------


def test_rolling_std_default_is_the_population_estimator() -> None:
    close = _series([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])

    result = rolling_std(close, 8)

    # mean 5, population variance 32 / 8 = 4 -> population std 2.0
    assert result.isna().to_numpy().tolist() == [True] * 7 + [False]
    np.testing.assert_allclose(result.to_numpy(dtype="float64")[7:], [2.0], rtol=0.0, atol=1e-12)
    assert result.name == "rolling_std"


def test_rolling_std_sample_estimator_differs_on_the_same_window() -> None:
    close = _series([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])

    population = rolling_std(close, 8)
    sample = rolling_std(close, 8, ddof=1)

    # sample variance 32 / 7 -> sqrt(32 / 7)
    np.testing.assert_allclose(
        sample.to_numpy(dtype="float64")[7:], [2.138089935299395], rtol=0.0, atol=1e-12
    )
    assert not np.isclose(float(population.iloc[7]), float(sample.iloc[7]))
    np.testing.assert_allclose(
        float(sample.iloc[7]), float(population.iloc[7]) * np.sqrt(8.0 / 7.0), rtol=1e-12
    )


@pytest.mark.parametrize("ddof", [0, 1])
def test_rolling_std_matches_the_mandated_pandas_expression(ddof: int) -> None:
    close = _series(np.linspace(100.0, 130.0, 40))

    expected = close.rolling(window=7, min_periods=7).std(ddof=ddof).rename("rolling_std")

    pd.testing.assert_series_equal(rolling_std(close, 7, ddof=ddof), expected)


def test_rolling_std_nan_prefix_name_and_index() -> None:
    close = _series(np.arange(1.0, 31.0), name="price")

    result = rolling_std(close, 14)

    assert result.isna().to_numpy().tolist() == [True] * 13 + [False] * 17
    assert result.index.equals(close.index)
    assert result.index.name == "timestamp"
    assert result.name == "rolling_std"


def test_rolling_std_period_one_is_all_zero() -> None:
    close = _series([3.0, 1.0, 4.0, 1.0, 5.0])

    result = rolling_std(close, 1)

    np.testing.assert_allclose(result.to_numpy(dtype="float64"), np.zeros(5), rtol=0.0, atol=1e-12)


@pytest.mark.parametrize("period", [0, -1, -14])
def test_rolling_std_invalid_period_raises(period: int) -> None:
    with pytest.raises(StrategyError, match="period must be >= 1"):
        rolling_std(_series([1.0, 2.0, 3.0]), period)


@pytest.mark.parametrize("ddof", [-1, -3])
def test_rolling_std_negative_ddof_raises(ddof: int) -> None:
    with pytest.raises(StrategyError, match="ddof must be >= 0"):
        rolling_std(_series([1.0, 2.0, 3.0]), 2, ddof=ddof)


@pytest.mark.parametrize("ddof", [1.5, None, "1"])
def test_rolling_std_rejects_a_non_integer_ddof(ddof: object) -> None:
    with pytest.raises(StrategyError, match="ddof must be an integer"):
        rolling_std(_series([1.0, 2.0, 3.0]), 2, ddof=ddof)  # type: ignore[arg-type]


def test_rolling_std_rejects_non_series_and_non_numeric_input() -> None:
    with pytest.raises(StrategyError, match="must be a pandas Series"):
        rolling_std([1.0, 2.0, 3.0], 2)  # type: ignore[arg-type]
    with pytest.raises(StrategyError, match="numeric"):
        rolling_std(pd.Series(["a", "b", "c"]), 2)


@pytest.mark.parametrize("period", [None, "3"])
def test_rolling_std_rejects_a_non_integer_period(period: object) -> None:
    with pytest.raises(StrategyError, match="integer"):
        rolling_std(_series([1.0, 2.0, 3.0]), period)  # type: ignore[arg-type]


def test_rolling_std_rejects_a_fractional_period() -> None:
    with pytest.raises(StrategyError, match=r"must be an integer, got 2\.5"):
        rolling_std(_series([1.0, 2.0, 3.0]), 2.5)


def test_rolling_std_short_and_empty_inputs_are_all_nan() -> None:
    short = rolling_std(_series([1.0, 2.0, 3.0]), 10)

    assert short.isna().all()
    assert short.size == 3
    assert short.dtype == np.dtype("float64")

    empty = rolling_std(_series([]), 3)

    assert empty.empty
    assert empty.dtype == np.dtype("float64")


def test_rolling_std_infinite_input_never_leaks() -> None:
    close = _series([1.0, 2.0, np.inf, 4.0, 5.0, 6.0])

    result = rolling_std(close, 2)

    _assert_same_index(result, close)
    np.testing.assert_allclose(
        result.to_numpy(dtype="float64")[4:], [0.5, 0.5], rtol=0.0, atol=1e-12
    )


def test_rolling_std_never_mutates_and_is_deterministic() -> None:
    close = _series([1.0, 3.0, 2.0, 5.0, 4.0])
    before = close.copy(deep=True)

    first = rolling_std(close, 3)
    second = rolling_std(close, 3)

    pd.testing.assert_series_equal(first, second)
    pd.testing.assert_series_equal(close, before)


# ---------------------------------------------------------------------------
# bollinger_bands
# ---------------------------------------------------------------------------


def test_bollinger_bands_hand_computed_values() -> None:
    close = _series([1.0, 3.0, 2.0, 5.0, 4.0])

    bands = bollinger_bands(close, 3)

    assert isinstance(bands, BollingerBands)
    assert bands._fields == ("middle", "upper", "lower")
    # window [1, 3, 2]: mean 2, population std sqrt(2 / 3)
    # window [3, 2, 5] and [2, 5, 4]: mean 10 / 3 and 11 / 3, std sqrt(14 / 9)
    np.testing.assert_allclose(
        bands.middle.to_numpy(dtype="float64"),
        [np.nan, np.nan, 2.0, 10.0 / 3.0, 11.0 / 3.0],
        rtol=0.0,
        atol=1e-12,
    )
    np.testing.assert_allclose(
        bands.upper.to_numpy(dtype="float64"),
        [np.nan, np.nan, 3.632993161855452, 5.827771591182628, 6.161104924515962],
        rtol=0.0,
        atol=1e-12,
    )
    np.testing.assert_allclose(
        bands.lower.to_numpy(dtype="float64"),
        [np.nan, np.nan, 0.367006838144548, 0.838895075484039, 1.172228408817372],
        rtol=0.0,
        atol=1e-12,
    )
    assert bands.middle.name == "bollinger_mid"
    assert bands.upper.name == "bollinger_upper"
    assert bands.lower.name == "bollinger_lower"
    for band in bands:
        _assert_same_index(band, close)


def test_bollinger_bands_middle_is_exactly_sma() -> None:
    close = _series(np.linspace(100.0, 140.0, 60) + np.sin(np.linspace(0.0, 9.0, 60)))

    bands = bollinger_bands(close, 20)

    # Same values and index as ``sma``; the name is the band column name.
    pd.testing.assert_series_equal(bands.middle, sma(close, 20).rename("bollinger_mid"))


@pytest.mark.parametrize("num_std", [1.0, 1.5, 2.0, 3.0])
def test_bollinger_bands_width_is_num_std_times_the_population_deviation(num_std: float) -> None:
    close = _series(np.linspace(100.0, 140.0, 60) + np.sin(np.linspace(0.0, 9.0, 60)))

    bands = bollinger_bands(close, 20, num_std)
    deviation = rolling_std(close, 20)

    np.testing.assert_allclose(
        (bands.upper - bands.middle).to_numpy(dtype="float64"),
        (num_std * deviation).to_numpy(dtype="float64"),
        rtol=0.0,
        atol=1e-12,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        (bands.middle - bands.lower).to_numpy(dtype="float64"),
        (num_std * deviation).to_numpy(dtype="float64"),
        rtol=0.0,
        atol=1e-12,
        equal_nan=True,
    )


def test_bollinger_bands_use_the_population_estimator() -> None:
    close = _series([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])

    bands = bollinger_bands(close, 8, 1.0)

    half_width = float(bands.upper.iloc[7] - bands.middle.iloc[7])

    np.testing.assert_allclose(half_width, 2.0, rtol=0.0, atol=1e-12)
    assert not np.isclose(half_width, float(rolling_std(close, 8, ddof=1).iloc[7]))


def test_bollinger_bands_are_ordered_and_share_the_sma_nan_prefix() -> None:
    close = _series(np.linspace(100.0, 140.0, 120) + np.sin(np.linspace(0.0, 18.0, 120)) * 3.0)

    bands = bollinger_bands(close, 20)
    prefix = sma(close, 20).isna().to_numpy().tolist()

    assert prefix == [True] * 19 + [False] * 101
    assert bands.upper.isna().to_numpy().tolist() == prefix
    assert bands.lower.isna().to_numpy().tolist() == prefix
    defined = slice(19, None)
    assert bool(bands.upper.iloc[defined].ge(bands.middle.iloc[defined]).all())
    assert bool(bands.middle.iloc[defined].ge(bands.lower.iloc[defined]).all())


@pytest.mark.parametrize("num_std", [0.0, -1.0, -2.5, float("nan"), float("inf"), float("-inf")])
def test_bollinger_bands_reject_a_non_positive_or_non_finite_num_std(num_std: float) -> None:
    with pytest.raises(StrategyError, match="num_std must be a finite number > 0"):
        bollinger_bands(_series([1.0, 2.0, 3.0]), 2, num_std)


def test_bollinger_bands_reject_a_non_numeric_num_std() -> None:
    with pytest.raises(StrategyError, match="num_std must be a finite number > 0"):
        bollinger_bands(_series([1.0, 2.0, 3.0]), 2, "wide")  # type: ignore[arg-type]


@pytest.mark.parametrize("period", [0, -1, -14])
def test_bollinger_bands_invalid_period_raises(period: int) -> None:
    with pytest.raises(StrategyError, match="period must be >= 1"):
        bollinger_bands(_series([1.0, 2.0, 3.0]), period)


def test_bollinger_bands_reject_non_series_and_non_numeric_input() -> None:
    with pytest.raises(StrategyError, match="must be a pandas Series"):
        bollinger_bands([1.0, 2.0, 3.0], 2)  # type: ignore[arg-type]
    with pytest.raises(StrategyError, match="numeric"):
        bollinger_bands(pd.Series(["a", "b", "c"]), 2)


def test_bollinger_bands_short_and_empty_inputs_are_all_nan() -> None:
    short = bollinger_bands(_series([1.0, 2.0, 3.0]), 10)

    for band in short:
        assert band.isna().all()
        assert band.size == 3
        assert band.dtype == np.dtype("float64")

    empty = bollinger_bands(_series([]), 3)

    for band in empty:
        assert band.empty
        assert band.dtype == np.dtype("float64")


def test_bollinger_bands_infinite_input_never_leaks() -> None:
    close = _series([1.0, 2.0, np.inf, 4.0, 5.0, 6.0])

    bands = bollinger_bands(close, 2)

    for band in bands:
        _assert_same_index(band, close)


def test_bollinger_bands_never_mutate_their_input() -> None:
    close = _series([1.0, 3.0, 2.0, 5.0, 4.0])
    before = close.copy(deep=True)

    first = bollinger_bands(close, 3)
    second = bollinger_bands(close, 3)

    for left, right in zip(first, second, strict=True):
        pd.testing.assert_series_equal(left, right)
    pd.testing.assert_series_equal(close, before)


# ---------------------------------------------------------------------------
# finiteness guarantee
# ---------------------------------------------------------------------------


def test_infinite_inputs_never_leak_into_the_results() -> None:
    close = _series([1.0, 2.0, np.inf, 4.0, 5.0, 6.0])
    high = close + 1.0
    low = close - 1.0

    results = [
        ema(close, 2),
        rsi(close, 2),
        atr(high, low, close, 2),
        true_range(high, low, close),
        rolling_max(close, 2),
        rolling_min(close, 2),
        rolling_std(close, 2),
    ]
    bands = bollinger_bands(close, 2)
    results.extend(bands)

    for result in results:
        values = result.to_numpy(dtype="float64")
        assert not np.isinf(values).any()
        assert result.index.equals(close.index)
