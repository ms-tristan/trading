"""Behavioural tests of the ``momentum`` strategy and of the ``sma`` / ``roc`` indicators.

Everything here is offline and deterministic: the frames are hand-built (or come
from the shared synthetic fixtures), the expected indicator values are
hand-computed or checked against the pandas expression the contract mandates, and
no test touches the network or a file outside the repository.

Three groups live in this file:

* the two new pure indicators (:func:`sma`, :func:`roc`) — nominal values,
  boundaries, ``NaN`` handling, dtype / index / name guarantees, error paths,
  purity and determinism;
* :class:`MomentumStrategy` — its parameters, the day-to-candle grid inference,
  the ``NaN``-propagating score, the entry / exit / short semantics and the ATR
  stop;
* one end-to-end run through :func:`trading_platform.strategy.engine.run_backtest`,
  proving the strategy really opens **and** closes trades.

The momentum symbols are imported from :mod:`trading_platform.strategy.momentum`
on purpose: the strategy is deliberately **not** re-exported by the package
namespace.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.constants import REQUIRED_OHLCV_COLUMNS, SIGNAL_COLUMNS
from trading_platform.core.errors import StrategyError
from trading_platform.core.models import ExitReason
from trading_platform.strategy.base import BOOL_SIGNAL_COLUMNS
from trading_platform.strategy.engine import run_backtest
from trading_platform.strategy.indicators import atr, roc, sma
from trading_platform.strategy.momentum import (
    INDICATOR_COLUMNS,
    MomentumParams,
    MomentumStrategy,
    MomentumStrategyParams,
)

START = "2024-01-01T00:00:00Z"

#: The horizon (in candles) of each lookback on each supported candle grid.
GRID_HORIZONS: dict[str, tuple[float, int, int, int]] = {
    # freq: (candles per day, fast 7 d, mid 14 d, slow 28 d)
    "h": (24.0, 168, 336, 672),
    "4h": (6.0, 42, 84, 168),
    "D": (1.0, 7, 14, 28),
}

#: Small lookbacks that stay defined on the shared 600-candle hourly fixture.
SHORT_FRAME_PARAMS: dict[str, object] = {
    "fast_days": 1,
    "mid_days": 2,
    "slow_days": 3,
    "atr_stop_multiplier": 4.0,
}


def _series(values: object, *, name: str = "close") -> pd.Series:
    """Build a tz-aware ``float64`` Series from ``values``."""
    array = np.asarray(values, dtype="float64")
    index = pd.date_range(START, periods=array.size, freq="h", tz="UTC", name="timestamp")
    return pd.Series(array, index=index, name=name, dtype="float64")


def _frame_from_closes(closes: object, *, freq: str = "h") -> pd.DataFrame:
    """Build an OHLCV frame whose opens chain the closes (``open[t] = close[t-1]``)."""
    values = np.asarray(closes, dtype="float64")
    index = pd.date_range(START, periods=values.size, freq=freq, tz="UTC", name="timestamp")
    opens = np.concatenate(([values[0]], values[:-1])) if values.size else values
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, values) + 0.5,
            "low": np.minimum(opens, values) - 0.5,
            "close": values,
            "volume": np.full(values.size, 1.0),
        },
        index=index,
    )


def _frame(count: int, *, freq: str = "h") -> pd.DataFrame:
    """Build a deterministic, NaN-free OHLCV frame of ``count`` candles."""
    steps = np.arange(count, dtype="float64")
    closes = 100.0 + 0.05 * steps + 10.0 * np.sin(steps / 17.0)
    return _frame_from_closes(closes, freq=freq)


def _relabelled(frame: pd.DataFrame, *, freq: str) -> pd.DataFrame:
    """Return ``frame`` with the same values on a fresh regularly-spaced grid."""
    moved = frame.copy(deep=True)
    moved.index = pd.date_range(START, periods=len(frame), freq=freq, tz="UTC", name="timestamp")
    return moved


def _rows(column: pd.Series) -> list[int]:
    """Return the integer positions where ``column`` is ``True``."""
    return [int(position) for position in np.flatnonzero(column.to_numpy(dtype=bool))]


def _assert_same_index(result: pd.Series, source: pd.Series) -> None:
    """Assert the shared guarantees of the pure indicator layer."""
    assert isinstance(result, pd.Series)
    assert result.dtype == np.dtype("float64")
    assert result.index.equals(source.index)
    assert result.index.name == "timestamp"
    assert not np.isinf(result.to_numpy(dtype="float64")).any()


# ---------------------------------------------------------------------------
# sma
# ---------------------------------------------------------------------------


def test_sma_hand_computed_values() -> None:
    close = _series([1.0, 2.0, 3.0, 4.0, 5.0])

    result = sma(close, 3)

    # the first period - 1 = 2 values have no full window
    np.testing.assert_allclose(
        result.to_numpy(dtype="float64"), [np.nan, np.nan, 2.0, 3.0, 4.0], rtol=0.0, atol=1e-12
    )
    assert result.name == "sma"
    _assert_same_index(result, close)


def test_sma_matches_the_mandated_pandas_expression() -> None:
    close = _series(np.linspace(100.0, 130.0, 40))

    expected = close.rolling(7).mean()

    pd.testing.assert_series_equal(sma(close, 7), expected, check_names=False)


def test_sma_nan_prefix_and_index_are_preserved() -> None:
    close = _series(np.arange(1.0, 31.0), name="price")

    result = sma(close, 14)

    assert int(result.isna().sum()) == 13
    assert np.isfinite(result.to_numpy(dtype="float64")[13:]).all()
    assert result.index.equals(close.index)
    assert result.index.name == "timestamp"
    assert result.name == "sma"


def test_sma_period_one_is_the_identity() -> None:
    close = _series([3.0, 1.0, 4.0, 1.0, 5.0])

    result = sma(close, 1)

    assert result.isna().sum() == 0
    pd.testing.assert_series_equal(result, close, check_names=False)


def test_sma_single_row_is_nan() -> None:
    close = _series([5.0])

    result = sma(close, 3)

    assert result.size == 1
    assert result.isna().all()
    _assert_same_index(result, close)


def test_sma_empty_series_is_empty() -> None:
    close = _series([])

    result = sma(close, 14)

    assert result.empty
    assert result.dtype == np.dtype("float64")
    assert result.name == "sma"


def test_sma_series_shorter_than_the_period_is_all_nan() -> None:
    close = _series([1.0, 2.0, 3.0])

    result = sma(close, 5)

    assert result.isna().all()
    assert result.size == 3


def test_sma_propagates_a_nan_of_the_input() -> None:
    close = _series([1.0, np.nan, 3.0, 4.0, 5.0])

    result = sma(close, 2)

    # every window holding the NaN is NaN; the last one is a full window
    np.testing.assert_allclose(
        result.to_numpy(dtype="float64"),
        [np.nan, np.nan, np.nan, 3.5, 4.5],
        rtol=0.0,
        atol=1e-12,
    )


def test_sma_never_leaks_an_infinite_input() -> None:
    close = _series([1.0, 2.0, np.inf, 4.0, 5.0])

    result = sma(close, 2)

    assert not np.isinf(result.to_numpy(dtype="float64")).any()
    np.testing.assert_allclose(
        result.to_numpy(dtype="float64"),
        [np.nan, 1.5, np.nan, np.nan, 4.5],
        rtol=0.0,
        atol=1e-12,
    )


@pytest.mark.parametrize("period", [0, -1, -14])
def test_sma_invalid_period_raises(period: int) -> None:
    with pytest.raises(StrategyError, match="period must be >= 1"):
        sma(_series([1.0, 2.0, 3.0]), period)


def test_sma_rejects_a_fractional_period() -> None:
    with pytest.raises(StrategyError, match=r"must be an integer, got 2\.5"):
        sma(_series([1.0, 2.0, 3.0]), 2.5)


@pytest.mark.parametrize("period", [None, "3"])
def test_sma_rejects_a_non_integer_period(period: object) -> None:
    with pytest.raises(StrategyError, match="integer"):
        sma(_series([1.0, 2.0, 3.0]), period)  # type: ignore[arg-type]


def test_sma_rejects_non_series_and_non_numeric_input() -> None:
    with pytest.raises(StrategyError, match="must be a pandas Series"):
        sma([1.0, 2.0, 3.0], 2)  # type: ignore[arg-type]
    with pytest.raises(StrategyError, match="numeric"):
        sma(pd.Series(["a", "b", "c"]), 2)


def test_sma_never_mutates_its_input_and_is_deterministic() -> None:
    close = _series(np.linspace(10.0, 20.0, 25))
    before = close.copy(deep=True)

    first = sma(close, 5)
    second = sma(close, 5)

    pd.testing.assert_series_equal(close, before)
    pd.testing.assert_series_equal(first, second)


# ---------------------------------------------------------------------------
# roc
# ---------------------------------------------------------------------------


def test_roc_hand_computed_values() -> None:
    close = _series([100.0, 110.0, 121.0, 121.0, 133.1])

    result = roc(close, 2)

    # 121 / 100 - 1 = +21 %, 121 / 110 - 1 = +10 %, 133.1 / 121 - 1 = +10 %
    np.testing.assert_allclose(
        result.to_numpy(dtype="float64"),
        [np.nan, np.nan, 0.21, 0.1, 0.1],
        rtol=1e-12,
        atol=1e-12,
    )
    assert result.name == "roc"
    _assert_same_index(result, close)


def test_roc_matches_the_mandated_pandas_expression() -> None:
    close = _series(np.linspace(100.0, 200.0, 40))

    expected = close / close.shift(5) - 1.0

    pd.testing.assert_series_equal(roc(close, 5), expected, check_names=False)


def test_roc_nan_prefix_and_index_are_preserved() -> None:
    close = _series(np.arange(1.0, 31.0), name="price")

    result = roc(close, 14)

    assert int(result.isna().sum()) == 14
    assert np.isfinite(result.to_numpy(dtype="float64")[14:]).all()
    assert result.index.equals(close.index)
    assert result.index.name == "timestamp"
    assert result.name == "roc"


def test_roc_period_one_only_loses_its_first_value() -> None:
    close = _series([100.0, 110.0, 121.0])

    result = roc(close, 1)

    assert np.isnan(result.iloc[0])
    np.testing.assert_allclose(
        result.to_numpy(dtype="float64")[1:], [0.1, 0.1], rtol=1e-12, atol=1e-12
    )
    pd.testing.assert_series_equal(result, close / close.shift(1) - 1.0, check_names=False)


def test_roc_zero_denominator_is_nan_never_infinite() -> None:
    close = _series([1.0, 0.0, 1.0, 2.0])
    before = close.copy(deep=True)

    result = roc(close, 1)

    # 1 / 0 - 1 = +inf, mapped to NaN; the other rows are plain rates
    np.testing.assert_allclose(
        result.to_numpy(dtype="float64"),
        [np.nan, -1.0, np.nan, 1.0],
        rtol=0.0,
        atol=1e-12,
    )
    assert not np.isinf(result.to_numpy(dtype="float64")).any()
    pd.testing.assert_series_equal(close, before)


def test_roc_single_row_is_nan() -> None:
    close = _series([5.0])

    result = roc(close, 1)

    assert result.size == 1
    assert result.isna().all()
    _assert_same_index(result, close)


def test_roc_empty_series_is_empty() -> None:
    close = _series([])

    result = roc(close, 14)

    assert result.empty
    assert result.dtype == np.dtype("float64")
    assert result.name == "roc"


def test_roc_series_shorter_than_the_period_is_all_nan() -> None:
    close = _series([1.0, 2.0, 3.0])

    result = roc(close, 5)

    assert result.isna().all()
    assert result.size == 3


def test_roc_propagates_a_nan_of_the_input() -> None:
    close = _series([1.0, 2.0, np.nan, 4.0, 5.0])

    result = roc(close, 1)

    undefined = [0, 2, 3]  # no predecessor, NaN numerator, NaN denominator
    assert result.isna().to_numpy().nonzero()[0].tolist() == undefined
    assert result.iloc[1] == pytest.approx(1.0)
    assert result.iloc[4] == pytest.approx(0.25)


def test_roc_never_leaks_an_infinite_input() -> None:
    close = _series([1.0, 2.0, np.inf, 4.0, 5.0])

    result = roc(close, 1)

    assert not np.isinf(result.to_numpy(dtype="float64")).any()
    assert np.isfinite(result.to_numpy(dtype="float64")[[1, 4]]).all()


@pytest.mark.parametrize("period", [0, -1, -14])
def test_roc_invalid_period_raises(period: int) -> None:
    with pytest.raises(StrategyError, match="period must be >= 1"):
        roc(_series([1.0, 2.0, 3.0]), period)


def test_roc_rejects_a_fractional_period() -> None:
    with pytest.raises(StrategyError, match=r"must be an integer, got 2\.5"):
        roc(_series([1.0, 2.0, 3.0]), 2.5)


@pytest.mark.parametrize("period", [None, "3"])
def test_roc_rejects_a_non_integer_period(period: object) -> None:
    with pytest.raises(StrategyError, match="integer"):
        roc(_series([1.0, 2.0, 3.0]), period)  # type: ignore[arg-type]


def test_roc_rejects_non_series_and_non_numeric_input() -> None:
    with pytest.raises(StrategyError, match="must be a pandas Series"):
        roc([1.0, 2.0, 3.0], 2)  # type: ignore[arg-type]
    with pytest.raises(StrategyError, match="numeric"):
        roc(pd.Series(["a", "b", "c"]), 2)


def test_roc_never_mutates_its_input_and_is_deterministic() -> None:
    close = _series(np.linspace(10.0, 20.0, 25))
    before = close.copy(deep=True)

    first = roc(close, 5)
    second = roc(close, 5)

    pd.testing.assert_series_equal(close, before)
    pd.testing.assert_series_equal(first, second)


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def test_default_parameters_are_the_documented_ones() -> None:
    strategy = MomentumStrategy()
    params = strategy.params

    assert strategy.name == "momentum"
    assert isinstance(params, MomentumStrategyParams)
    # The parameter model has exactly one class behind its two public spellings.
    assert MomentumParams is MomentumStrategyParams
    assert isinstance(params, MomentumParams)
    assert params.model_dump() == {
        "fast_days": 7,
        "mid_days": 14,
        "slow_days": 28,
        "enter_score": 0.6,
        "exit_score": 0.0,
        "atr_period": 14,
        "atr_stop_multiplier": 4.0,
        "allow_short": False,
    }
    assert MomentumStrategy.default_params() == params.model_dump()


def test_param_space_matches_the_documented_grid() -> None:
    space = MomentumStrategy().param_space()

    assert space == {
        "fast_days": [5, 7, 10],
        "mid_days": [14, 20],
        "slow_days": [28, 40, 56],
        "atr_stop_multiplier": [0.0, 4.0],
    }
    # the robustness sweep is bounded: the grid must stay <= 512 combinations
    assert math.prod(len(values) for values in space.values()) == 36


@pytest.mark.parametrize(
    ("params", "fields"),
    [
        ({"fast_days": 14, "mid_days": 14}, ("fast_days", "mid_days")),
        ({"fast_days": 21, "mid_days": 14}, ("fast_days", "mid_days")),
        ({"mid_days": 28, "slow_days": 28}, ("mid_days", "slow_days")),
        ({"mid_days": 40, "slow_days": 28}, ("mid_days", "slow_days")),
        ({"enter_score": 0.6, "exit_score": 0.6}, ("enter_score", "exit_score")),
        ({"exit_score": 0.6}, ("enter_score", "exit_score")),
    ],
)
def test_incoherent_parameters_raise(params: dict[str, object], fields: tuple[str, str]) -> None:
    with pytest.raises(StrategyError) as error:
        MomentumStrategy(params)

    message = str(error.value)
    for field in fields:
        assert field in message, f"the error does not name {field!r}: {message}"


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"fast_days": 0}, "fast_days"),
        ({"fast_days": -3}, "fast_days"),
        ({"mid_days": 1}, "mid_days"),
        ({"slow_days": 2}, "slow_days"),
        ({"enter_score": 0.0}, "enter_score"),
        ({"enter_score": -0.5}, "enter_score"),
        ({"enter_score": 1.5}, "enter_score"),
        ({"exit_score": -1.5}, "exit_score"),
        ({"exit_score": 1.5}, "exit_score"),
        ({"atr_period": 1}, "atr_period"),
        ({"atr_period": 0}, "atr_period"),
        ({"atr_stop_multiplier": -0.1}, "atr_stop_multiplier"),
    ],
)
def test_out_of_range_parameters_raise(params: dict[str, object], field: str) -> None:
    with pytest.raises(StrategyError) as error:
        MomentumStrategy(params)

    assert field in str(error.value)


def test_unknown_parameter_raises() -> None:
    with pytest.raises(StrategyError, match="unknown_field"):
        MomentumStrategy({"unknown_field": 1})


def test_allow_short_defaults_to_off_and_can_be_enabled() -> None:
    assert MomentumStrategy().params.model_dump()["allow_short"] is False
    assert MomentumStrategy({"allow_short": True}).params.model_dump()["allow_short"] is True


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_adds_exactly_the_indicator_columns_and_leaves_the_input_untouched() -> None:
    data = _frame(400)
    before = data.copy(deep=True)

    prepared = MomentumStrategy().prepare(data)

    assert list(prepared.columns) == [*REQUIRED_OHLCV_COLUMNS, *INDICATOR_COLUMNS]
    assert list(INDICATOR_COLUMNS) == [
        "momentum_fast",
        "momentum_mid",
        "momentum_slow",
        "momentum_score",
        "atr",
        "candles_per_day",
    ]
    pd.testing.assert_frame_equal(prepared[list(before.columns)], before)
    pd.testing.assert_frame_equal(data, before)
    assert prepared.index.equals(data.index)
    assert prepared.index.name == "timestamp"
    for column in INDICATOR_COLUMNS:
        assert prepared[column].dtype == np.dtype("float64")


@pytest.mark.parametrize("freq", sorted(GRID_HORIZONS))
def test_prepare_converts_the_day_lookbacks_into_candles_on_every_grid(freq: str) -> None:
    per_day, fast, mid, slow = GRID_HORIZONS[freq]
    # enough candles for the slow horizon to become defined
    data = _frame(slow + 20, freq=freq)

    prepared = MomentumStrategy().prepare(data)

    assert prepared["candles_per_day"].eq(per_day).all()
    for column, horizon in (
        ("momentum_fast", fast),
        ("momentum_mid", mid),
        ("momentum_slow", slow),
    ):
        # roc() loses its first `horizon` values: they have nothing to compare with
        assert int(prepared[column].isna().sum()) == horizon
        assert np.isnan(prepared[column].iloc[horizon - 1])
        assert np.isfinite(prepared[column].iloc[horizon])


def test_prepare_uses_the_median_spacing_of_the_whole_index() -> None:
    data = _frame(500, freq="h")

    prepared = MomentumStrategy().prepare(data)

    assert prepared["candles_per_day"].iloc[0] == 24.0
    assert prepared["momentum_fast"].iloc[167:169].isna().tolist() == [True, False]
    assert np.isfinite(prepared["momentum_fast"].iloc[-1])


def test_prepare_infers_one_candle_per_day_on_degenerate_grids() -> None:
    single = MomentumStrategy().prepare(_frame(1))
    assert single["candles_per_day"].eq(1.0).all()
    assert single["momentum_score"].isna().all()

    duplicated = _frame(12)
    duplicated.index = pd.DatetimeIndex([pd.Timestamp(START)] * 12, name="timestamp")
    prepared = MomentumStrategy().prepare(duplicated)
    assert prepared["candles_per_day"].eq(1.0).all()
    assert prepared["momentum_score"].isna().all()


def test_prepare_rejects_a_frame_that_violates_the_ohlcv_contract() -> None:
    with pytest.raises(StrategyError, match="missing required column"):
        MomentumStrategy().prepare(_frame(30).drop(columns=["volume"]))
    with pytest.raises(StrategyError, match="empty frame"):
        MomentumStrategy().prepare(_frame_from_closes([]))


def test_the_score_is_nan_while_any_horizon_is_undefined() -> None:
    data = _frame(100, freq="D")
    strategy = MomentumStrategy()

    prepared = strategy.prepare(data)

    score = prepared["momentum_score"]
    horizons = prepared[["momentum_fast", "momentum_mid", "momentum_slow"]]
    # the slow horizon (28 daily candles) is the last to become defined
    assert int(score.isna().sum()) == GRID_HORIZONS["D"][3]
    assert score.isna().to_numpy().nonzero()[0].tolist() == list(range(GRID_HORIZONS["D"][3]))
    assert np.isfinite(score.to_numpy(dtype="float64")[GRID_HORIZONS["D"][3] :]).all()
    # NaN propagates: the score is undefined exactly where any horizon is
    pd.testing.assert_series_equal(score.isna(), horizons.isna().any(axis=1), check_names=False)


def test_the_score_is_the_mean_of_the_three_signs() -> None:
    prepared = MomentumStrategy().prepare(_frame(100, freq="D"))

    expected = (
        np.sign(prepared["momentum_fast"])
        + np.sign(prepared["momentum_mid"])
        + np.sign(prepared["momentum_slow"])
    ) / 3.0

    pd.testing.assert_series_equal(prepared["momentum_score"], expected, check_names=False)


def test_nan_rows_never_fire_a_signal() -> None:
    strategy = MomentumStrategy()
    prepared, signals = strategy.run(_frame(100, freq="D"))

    undefined = prepared["momentum_score"].isna()
    assert int(undefined.sum()) >= GRID_HORIZONS["D"][3]
    # the trap: a score filled with 0 would satisfy `exit_long` (score <= 0.0) on
    # every early candle, so NaN rows must never carry any signal at all
    assert not signals.loc[undefined, list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def _rising_then_falling() -> pd.DataFrame:
    """120 daily candles rising from 100 to 159 then falling by 2 a day."""
    return _frame_from_closes(
        [100.0 + index for index in range(60)] + [159.0 - 2.0 * index for index in range(60)],
        freq="D",
    )


def _hand_params(**overrides: object) -> dict[str, object]:
    """Short daily lookbacks that are fully defined on :func:`_rising_then_falling`."""
    return {"fast_days": 1, "mid_days": 2, "slow_days": 3, "atr_stop_multiplier": 4.0, **overrides}


def test_signal_frame_has_exactly_the_five_contract_columns() -> None:
    strategy = MomentumStrategy(_hand_params())
    prepared = strategy.prepare(_rising_then_falling())

    signals = strategy.signals(prepared)

    assert list(signals.columns) == list(SIGNAL_COLUMNS)
    for column in BOOL_SIGNAL_COLUMNS:
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].isna().any()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    assert signals.index.equals(prepared.index)


def test_entry_long_is_the_state_of_a_strong_positive_score() -> None:
    strategy = MomentumStrategy(_hand_params())
    prepared, signals = strategy.run(_rising_then_falling())

    score = prepared["momentum_score"]
    assert score.iloc[10] == pytest.approx(1.0)
    assert _rows(signals["entry_long"]) == _rows(score >= 0.6)

    # enter_score = 0.6 is exactly "at least two horizons agree, none disagrees"
    # (and all three must be defined: an undefined horizon makes the score NaN)
    horizons = prepared[["momentum_fast", "momentum_mid", "momentum_slow"]]
    signs = np.sign(horizons)
    agreement = (
        signs.gt(0.0).sum(axis=1).ge(2)
        & signs.lt(0.0).sum(axis=1).eq(0)
        & horizons.notna().all(axis=1)
    )
    pd.testing.assert_series_equal(signals["entry_long"], agreement, check_names=False)


def test_exit_long_fires_on_a_negative_score() -> None:
    strategy = MomentumStrategy(_hand_params())
    prepared, signals = strategy.run(_rising_then_falling())

    assert prepared["momentum_score"].iloc[-1] == pytest.approx(-1.0)
    assert _rows(signals["exit_long"]) == _rows(prepared["momentum_score"] <= 0.0)
    assert bool(signals["exit_long"].iloc[-1])
    assert not bool(signals["exit_long"].iloc[10])


def test_without_allow_short_the_four_short_side_columns_stay_false() -> None:
    strategy = MomentumStrategy(_hand_params())
    _prepared, signals = strategy.run(_rising_then_falling())

    assert _rows(signals["entry_short"]) == []
    assert _rows(signals["exit_short"]) == []


def test_allow_short_mirrors_the_signals() -> None:
    strategy = MomentumStrategy(_hand_params(allow_short=True))
    prepared, signals = strategy.run(_rising_then_falling())

    score = prepared["momentum_score"]
    assert _rows(signals["entry_short"]) == _rows(score <= -0.6)
    assert _rows(signals["exit_short"]) == _rows(score >= 0.0)
    assert bool(signals["entry_short"].iloc[-1])
    assert bool(signals["exit_short"].iloc[10])
    assert not bool(signals["entry_short"].iloc[10])
    # the stop column is shared by both directions (row 20: the ATR is defined)
    assert signals["stop_loss"].iloc[20] == pytest.approx(
        prepared["close"].iloc[20] - 4.0 * prepared["atr"].iloc[20]
    )


def test_stop_loss_is_all_nan_when_the_multiplier_is_zero() -> None:
    strategy = MomentumStrategy(_hand_params(atr_stop_multiplier=0.0))
    prepared, signals = strategy.run(_rising_then_falling())

    assert signals["stop_loss"].isna().all()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    # 0.0 means "no stop", not "a stop at the close"
    assert not prepared["atr"].isna().all()


def test_stop_loss_is_close_minus_the_atr_distance() -> None:
    strategy = MomentumStrategy(_hand_params())
    prepared, signals = strategy.run(_rising_then_falling())

    expected = prepared["close"] - 4.0 * atr(
        prepared["high"], prepared["low"], prepared["close"], 14
    )

    pd.testing.assert_series_equal(signals["stop_loss"], expected, check_names=False)
    # NaN wherever the ATR is not defined yet (the first atr_period candles)
    pd.testing.assert_series_equal(
        signals["stop_loss"].isna(), prepared["atr"].isna(), check_names=False
    )
    assert int(signals["stop_loss"].isna().sum()) == 14


def test_signals_requires_a_prepared_frame() -> None:
    with pytest.raises(StrategyError, match="prepare"):
        MomentumStrategy().signals(_frame(40, freq="D"))
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        MomentumStrategy().signals([1, 2, 3])  # type: ignore[arg-type]


def test_pipeline_is_deterministic_and_leaves_both_inputs_untouched() -> None:
    data = _frame(200, freq="D")
    snapshot = data.copy(deep=True)
    strategy = MomentumStrategy()

    prepared_first = strategy.prepare(data)
    prepared_snapshot = prepared_first.copy(deep=True)
    signals_first = strategy.signals(prepared_first)
    signals_second = strategy.signals(prepared_first)
    prepared_second = strategy.prepare(data)

    pd.testing.assert_frame_equal(prepared_first, prepared_second)
    pd.testing.assert_frame_equal(signals_first, signals_second)
    pd.testing.assert_frame_equal(prepared_first, prepared_snapshot)
    pd.testing.assert_frame_equal(data, snapshot)


# ---------------------------------------------------------------------------
# end-to-end through the engine
# ---------------------------------------------------------------------------


def test_run_backtest_opens_and_closes_trades(trending_frame: pd.DataFrame) -> None:
    # the shared fixture is relabelled to a daily grid, where the default
    # 7 / 14 / 28-day lookbacks are 7 / 14 / 28 candles
    frame = _relabelled(trending_frame, freq="D")
    strategy = MomentumStrategy()

    first = run_backtest(strategy, frame)
    second = run_backtest(strategy, frame)

    assert first.strategy_name == "momentum"
    assert len(first.equity_curve) == len(frame)
    assert len(first.trades) >= 1
    assert all(trade.exit_time > trade.entry_time for trade in first.trades)
    assert {trade.exit_reason for trade in first.trades} <= set(ExitReason)
    assert any(trade.exit_reason is ExitReason.SIGNAL for trade in first.trades)

    # the run is fully deterministic and never mutates the input frame
    assert first.trades == second.trades
    assert first.final_balance == second.final_balance
    np.testing.assert_allclose(
        first.equity_curve.to_numpy(dtype="float64"),
        second.equity_curve.to_numpy(dtype="float64"),
    )
    assert len(strategy.prepare(frame)) == len(frame)


def test_run_backtest_on_the_hourly_grid_with_short_lookbacks(trending_frame: pd.DataFrame) -> None:
    strategy = MomentumStrategy(SHORT_FRAME_PARAMS)

    result = run_backtest(strategy, trending_frame)

    assert result.strategy_name == "momentum"
    assert len(result.equity_curve) == len(trending_frame)
    assert len(result.trades) >= 1
    for trade in result.trades:
        assert trade.exit_reason in set(ExitReason)
        assert trade.entry_price > 0.0
        assert trade.exit_price > 0.0
