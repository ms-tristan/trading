"""Contract tests of the strategy base layer.

The strategy used here is defined *inside* the test module on purpose: it must
never import :mod:`trading_backtest.strategy.basic`, so the ABC contract is
pinned independently of any concrete strategy.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from trading_backtest.core.constants import SIGNAL_COLUMNS
from trading_backtest.core.errors import StrategyError
from trading_backtest.strategy.base import (
    BOOL_SIGNAL_COLUMNS,
    Strategy,
    StrategyParams,
    ensure_signal_frame,
    require_ohlcv_frame,
)

START = "2024-01-01T00:00:00Z"


def _ohlcv_frame(n: int = 24) -> pd.DataFrame:
    """Deterministic OHLCV frame (no network, no shared fixture)."""
    index = pd.date_range(START, periods=n, freq="h", tz="UTC", name="timestamp")
    close = np.linspace(100.0, 120.0, n)
    open_ = np.concatenate(([close[0]], close[:-1])) if n else close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 1.0,
            "low": np.minimum(open_, close) - 1.0,
            "close": close,
            "volume": np.full(n, 5.0),
        },
        index=index,
    )


def _signal_frame(index: pd.Index, *, n_true: int = 1) -> pd.DataFrame:
    entry_long = np.zeros(len(index), dtype=bool)
    entry_long[:n_true] = True
    return pd.DataFrame(
        {
            "entry_long": entry_long,
            "exit_long": np.zeros(len(index), dtype=bool),
            "entry_short": np.zeros(len(index), dtype=bool),
            "exit_short": np.zeros(len(index), dtype=bool),
            "stop_loss": np.full(len(index), np.nan),
        },
        index=index,
    )


class DummyParams(StrategyParams):
    """Parameters of the in-test strategy."""

    period: int = 3
    threshold: float = 0.5


class DummyStrategy(Strategy):
    """Minimal concrete strategy implementing the ABC contract."""

    name: ClassVar[str] = "dummy"
    ParamsModel: ClassVar[type[StrategyParams]] = DummyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {"period": [2, 3, 5]}

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        params = self.params
        assert isinstance(params, DummyParams)
        frame = require_ohlcv_frame(data, name="data")
        frame["sma"] = frame["close"].rolling(params.period, min_periods=1).mean().to_numpy()
        frame["above"] = (frame["close"] > params.threshold).to_numpy(dtype=bool)
        return frame

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        rising = data["sma"].diff().fillna(0.0) > 0.0
        return ensure_signal_frame(
            pd.DataFrame(
                {
                    "entry_long": (rising & data["above"]).to_numpy(dtype=bool),
                    "exit_long": (~rising).to_numpy(dtype=bool),
                    "entry_short": np.zeros(len(data), dtype=bool),
                    "exit_short": np.zeros(len(data), dtype=bool),
                    "stop_loss": np.full(len(data), np.nan),
                },
                index=data.index,
            ),
            data.index,
        )


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


def test_the_abc_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        Strategy()  # type: ignore[abstract]


def test_a_subclass_without_the_abstract_methods_cannot_be_instantiated() -> None:
    class Incomplete(Strategy):
        name = "incomplete"

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_run_returns_the_prepared_frame_and_the_signals() -> None:
    strategy = DummyStrategy()

    prepared, signals = strategy.run(_ohlcv_frame())

    assert "sma" in prepared.columns and "above" in prepared.columns
    assert list(signals.columns) == list(SIGNAL_COLUMNS)


def test_prepare_does_not_mutate_its_input() -> None:
    data = _ohlcv_frame()
    before = data.copy(deep=True)

    prepared = DummyStrategy().prepare(data)

    pd.testing.assert_frame_equal(data, before)
    assert prepared is not data
    assert "sma" not in data.columns
    assert prepared.index.equals(data.index)


def test_signals_returns_exactly_the_signal_columns() -> None:
    strategy = DummyStrategy()
    prepared = strategy.prepare(_ohlcv_frame())

    signals = strategy.signals(prepared)

    assert list(signals.columns) == list(SIGNAL_COLUMNS)
    for column in BOOL_SIGNAL_COLUMNS:
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].isna().any()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    assert signals.index.equals(prepared.index)


def test_signals_is_repeatable_and_side_effect_free() -> None:
    strategy = DummyStrategy()
    prepared = strategy.prepare(_ohlcv_frame())
    snapshot = prepared.copy(deep=True)

    first = strategy.signals(prepared)
    second = strategy.signals(prepared)

    pd.testing.assert_frame_equal(first, second)
    pd.testing.assert_frame_equal(prepared, snapshot)


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def test_mapping_params_are_validated_through_the_model() -> None:
    strategy = DummyStrategy({"period": 5, "threshold": 1.5})

    assert isinstance(strategy.params, DummyParams)
    assert strategy.params.period == 5
    assert strategy.params.threshold == 1.5
    assert strategy.params.model_dump() == {"period": 5, "threshold": 1.5}


def test_params_model_instances_are_accepted() -> None:
    params = DummyParams(period=7)

    strategy = DummyStrategy(params)

    assert strategy.params is params


def test_no_params_means_the_defaults() -> None:
    strategy = DummyStrategy()

    assert strategy.params.model_dump() == {"period": 3, "threshold": 0.5}


def test_unknown_param_key_raises_with_the_field_name() -> None:
    with pytest.raises(StrategyError) as error:
        DummyStrategy({"period": 3, "nope": 1})

    assert "nope" in str(error.value)
    assert "dummy" in str(error.value)


def test_wrong_typed_param_raises_with_the_field_name() -> None:
    with pytest.raises(StrategyError) as error:
        DummyStrategy({"period": "many"})

    assert "period" in str(error.value)


def test_wrong_params_model_type_raises() -> None:
    class OtherParams(StrategyParams):
        period: int = 4

    with pytest.raises(StrategyError, match="DummyParams"):
        DummyStrategy(OtherParams())


@pytest.mark.parametrize("params", [42, "period=3", [("period", 3)]])
def test_non_mapping_params_raise(params: object) -> None:
    with pytest.raises(StrategyError, match="params must be a mapping"):
        DummyStrategy(params)  # type: ignore[arg-type]


def test_validate_params_classmethod() -> None:
    assert DummyStrategy.validate_params(None).model_dump() == {"period": 3, "threshold": 0.5}
    assert DummyStrategy.validate_params({"period": 2}).period == 2
    with pytest.raises(StrategyError, match="mapping"):
        DummyStrategy.validate_params(3)  # type: ignore[arg-type]


def test_parameters_are_frozen() -> None:
    params = DummyStrategy().params

    with pytest.raises(ValidationError):
        params.period = 9  # type: ignore[misc]


def test_default_params_returns_a_copy() -> None:
    first = DummyStrategy.default_params()
    first["period"] = 99

    assert DummyStrategy.default_params()["period"] == 3
    assert DummyStrategy().params.period == 3


def test_param_space_returns_a_copy() -> None:
    space = DummyStrategy().param_space()
    space["period"].append(1234)
    space["injected"] = [1]

    assert DummyStrategy().param_space() == {"period": [2, 3, 5]}
    assert DummyStrategy.PARAM_SPACE == {"period": [2, 3, 5]}


def test_param_space_defaults_to_empty() -> None:
    class Bare(Strategy):
        name = "bare"
        ParamsModel = DummyParams

        def prepare(self, data: pd.DataFrame) -> pd.DataFrame:  # pragma: no cover - unused
            return data

        def signals(self, data: pd.DataFrame) -> pd.DataFrame:  # pragma: no cover - unused
            return ensure_signal_frame(_signal_frame(data.index), data.index)

    assert Bare().param_space() == {}


# ---------------------------------------------------------------------------
# require_ohlcv_frame
# ---------------------------------------------------------------------------


def test_require_ohlcv_frame_returns_a_copy_of_the_frame() -> None:
    data = _ohlcv_frame()

    frame = require_ohlcv_frame(data, name="data")
    frame.loc[frame.index[0], "close"] = -1.0

    assert data.loc[data.index[0], "close"] == 100.0
    assert frame["open"].dtype == np.dtype("float64")
    assert frame.index.equals(data.index)


def test_require_ohlcv_frame_rejects_a_missing_column() -> None:
    data = _ohlcv_frame().drop(columns=["volume"])

    with pytest.raises(StrategyError, match="missing required column") as error:
        require_ohlcv_frame(data)

    assert "volume" in str(error.value)


def test_require_ohlcv_frame_rejects_an_empty_frame() -> None:
    with pytest.raises(StrategyError, match="empty frame"):
        require_ohlcv_frame(_ohlcv_frame(0))


def test_require_ohlcv_frame_rejects_a_non_datetime_index() -> None:
    data = _ohlcv_frame().reset_index(drop=True)

    with pytest.raises(StrategyError, match="DatetimeIndex"):
        require_ohlcv_frame(data)


def test_require_ohlcv_frame_rejects_a_non_numeric_column() -> None:
    data = _ohlcv_frame()
    data["close"] = data["close"].astype(object)
    data.loc[data.index[0], "close"] = "not-a-price"

    with pytest.raises(StrategyError, match="not convertible to float64"):
        require_ohlcv_frame(data)


def test_require_ohlcv_frame_rejects_a_non_dataframe() -> None:
    with pytest.raises(StrategyError, match="must be a pandas DataFrame"):
        require_ohlcv_frame([1, 2, 3])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ensure_signal_frame
# ---------------------------------------------------------------------------


def test_ensure_signal_frame_accepts_the_exact_contract() -> None:
    data = _ohlcv_frame()
    signals = _signal_frame(data.index)

    checked = ensure_signal_frame(signals, data.index)

    assert list(checked.columns) == list(SIGNAL_COLUMNS)
    assert checked.index.equals(data.index)
    assert checked["entry_long"].dtype == np.dtype("bool")


def test_ensure_signal_frame_does_not_mutate_the_input() -> None:
    data = _ohlcv_frame()
    signals = _signal_frame(data.index, n_true=2)
    signals["entry_long"] = signals["entry_long"].astype(object)
    signals.loc[signals.index[0], "entry_long"] = np.nan
    before = signals.copy(deep=True)

    checked = ensure_signal_frame(signals, data.index)

    pd.testing.assert_frame_equal(signals, before)
    assert bool(checked["entry_long"].iloc[0]) is False
    assert bool(checked["entry_long"].iloc[1]) is True


@pytest.mark.parametrize(
    "column", ["entry_long", "exit_long", "entry_short", "exit_short", "stop_loss"]
)
def test_ensure_signal_frame_rejects_a_missing_column(column: str) -> None:
    data = _ohlcv_frame()
    signals = _signal_frame(data.index).drop(columns=[column])

    with pytest.raises(StrategyError, match="missing signal column"):
        ensure_signal_frame(signals, data.index)


def test_ensure_signal_frame_rejects_an_unexpected_column() -> None:
    data = _ohlcv_frame()
    signals = _signal_frame(data.index)
    signals["take_profit"] = 1.0

    with pytest.raises(StrategyError, match="unexpected signal column"):
        ensure_signal_frame(signals, data.index)


def test_ensure_signal_frame_rejects_a_duplicated_column() -> None:
    data = _ohlcv_frame()
    signals = pd.concat([_signal_frame(data.index), _signal_frame(data.index)], axis=1)

    with pytest.raises(StrategyError, match="duplicated column"):
        ensure_signal_frame(signals, data.index)


def test_ensure_signal_frame_rejects_a_shifted_index() -> None:
    data = _ohlcv_frame()
    other = pd.date_range("2030-01-01T00:00:00Z", periods=len(data), freq="h", tz="UTC")

    with pytest.raises(StrategyError, match="indexed exactly like the input"):
        ensure_signal_frame(_signal_frame(other), data.index)


def test_ensure_signal_frame_rejects_a_non_numeric_stop_loss() -> None:
    data = _ohlcv_frame()
    signals = _signal_frame(data.index)
    signals["stop_loss"] = "wide"

    with pytest.raises(StrategyError, match="stop_loss"):
        ensure_signal_frame(signals, data.index)


def test_ensure_signal_frame_rejects_a_non_dataframe() -> None:
    with pytest.raises(StrategyError, match="must be a pandas DataFrame"):
        ensure_signal_frame({"entry_long": [True]}, pd.RangeIndex(1))  # type: ignore[arg-type]
