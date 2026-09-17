"""Unit tests of the pure indicator layer.

Everything here is offline and deterministic: the expected values are
hand-computed (or checked against the pandas expression the contract mandates),
never taken from an external library.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_backtest.core.errors import StrategyError
from trading_backtest.strategy.indicators import atr, ema, rsi, true_range

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
# finiteness guarantee
# ---------------------------------------------------------------------------


def test_infinite_inputs_never_leak_into_the_results() -> None:
    close = _series([1.0, 2.0, np.inf, 4.0, 5.0, 6.0])
    high = close + 1.0
    low = close - 1.0

    results = [ema(close, 2), rsi(close, 2), atr(high, low, close, 2), true_range(high, low, close)]

    for result in results:
        values = result.to_numpy(dtype="float64")
        assert not np.isinf(values).any()
        assert result.index.equals(close.index)
