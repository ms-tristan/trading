"""Unit tests for the parametric robustness layer (``trading_backtest.validation.robustness``).

The sweep is driven by local fake runners whose metric is a pure function of the
parameters, so every summary statistic (mean, population std, min/max, stability,
the two ratios, ``is_robust``) is asserted against a hand-computed value.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence

import numpy as np
import pandas as pd
import pytest

from trading_backtest.core.errors import ValidationLayerError
from trading_backtest.core.models import (
    BacktestResult,
    Direction,
    ExitReason,
    RunnerFn,
    TradeRecord,
)
from trading_backtest.validation.robustness import (
    DEFAULT_BEST_COUNT,
    RobustnessPoint,
    RobustnessResult,
    expand_grid,
    grid_size,
    parameter_sweep,
)

INITIAL_BALANCE = 10_000.0

DOCUMENTED_POINT_KEYS = {"params", "metric", "n_trades", "total_return"}
DOCUMENTED_RESULT_KEYS = {
    "metric_name",
    "base_params",
    "base_metric",
    "points",
    "n_points",
    "metric_mean",
    "metric_std",
    "metric_min",
    "metric_max",
    "stability",
    "positive_ratio",
    "robust_ratio",
    "worst_case_return",
    "best_params",
    "is_robust",
}


def _metadata_score(result: BacktestResult) -> float:
    """Scoring function reading the score the fake runners stored in metadata."""
    return float(result.metadata["score"])


def _result_with_score(
    data: pd.DataFrame,
    score: float,
    *,
    n_trades: int = 0,
    initial_balance: float = INITIAL_BALANCE,
) -> BacktestResult:
    index = pd.DatetimeIndex(data.index)
    count = len(index)
    trades: list[TradeRecord] = []
    for position in range(n_trades):
        entry = index[min(position, count - 1)]
        exit_ = index[min(position + 1, count - 1)]
        trades.append(
            TradeRecord(
                entry_time=entry,
                exit_time=exit_,
                entry_price=100.0,
                exit_price=101.0,
                size=1.0,
                direction=Direction.LONG,
                pnl=1.0,
                pnl_pct=0.0001,
                fees=0.0,
                exit_reason=ExitReason.SIGNAL,
                duration_minutes=60.0,
                params_id="grid",
            )
        )
    equity = pd.Series(
        np.full(count, initial_balance + score, dtype="float64"), index=index, name="equity"
    )
    return BacktestResult(
        strategy_name="grid",
        symbol="BTC/USDT",
        timeframe="1h",
        start=index[0],
        end=index[-1],
        initial_balance=initial_balance,
        final_balance=float(initial_balance + score),
        trades=trades,
        equity_curve=equity,
        params={},
        metadata={"score": float(score)},
    )


def _score_runner(
    score_fn: Callable[[Mapping[str, float | int]], float],
) -> tuple[RunnerFn, list[dict[str, object] | None]]:
    """Runner whose metric is ``score_fn(params)``; records every call's params."""
    calls: list[dict[str, object] | None] = []

    def _runner(data: pd.DataFrame, params: Mapping[str, object] | None = None) -> BacktestResult:
        snapshot = dict(params) if params else None
        calls.append(snapshot)
        score = float(score_fn(snapshot or {}))
        return _result_with_score(data, score)

    return _runner, calls


# ---------------------------------------------------------------------------
# grid helpers
# ---------------------------------------------------------------------------


def test_expand_grid_order_is_alphabetical_and_deterministic() -> None:
    assert expand_grid({"a": [1, 2], "b": [3]}) == [{"a": 1, "b": 3}, {"a": 2, "b": 3}]
    assert expand_grid({"b": [3, 4], "a": [1, 2]}) == [
        {"a": 1, "b": 3},
        {"a": 1, "b": 4},
        {"a": 2, "b": 3},
        {"a": 2, "b": 4},
    ]
    assert expand_grid({"b": [3], "a": [1, 2]}) == expand_grid({"a": [1, 2], "b": [3]})


def test_expand_grid_of_an_empty_grid_is_empty() -> None:
    assert expand_grid({}) == []
    assert expand_grid({"a": []}) == []


def test_grid_size_counts_combinations() -> None:
    assert grid_size({}) == 0
    assert grid_size({"a": []}) == 0
    assert grid_size({"a": [1, 2], "b": [3]}) == 2
    assert grid_size({"a": [1, 2], "b": [3, 4], "c": [5, 6, 7]}) == 12


def test_robustness_point_key_is_canonical_and_sorted() -> None:
    point = RobustnessPoint(params={"b": 2.5, "a": 1}, metric=1.5, n_trades=3, total_return=0.2)

    assert point.key == "a=1,b=2.5"
    assert point.to_dict() == {
        "params": {"b": 2.5, "a": 1},
        "metric": 1.5,
        "n_trades": 3,
        "total_return": 0.2,
    }
    assert set(point.to_dict()) == DOCUMENTED_POINT_KEYS


# ---------------------------------------------------------------------------
# statistics of a 2x2x2 sweep
# ---------------------------------------------------------------------------


def test_parameter_sweep_2x2x2_grid_statistics(ohlcv_frame: pd.DataFrame) -> None:
    runner, calls = _score_runner(lambda params: float(params["a"] + params["b"] + params["c"]))
    grid = {"a": [1, 2], "b": [10, 20], "c": [100, 200]}
    base_params = {"a": 1, "b": 10, "c": 100}

    result = parameter_sweep(
        runner,
        ohlcv_frame,
        grid,
        base_params=base_params,
        metric_fn=_metadata_score,
        max_combinations=8,
    )

    assert result.n_points == 8
    assert len(calls) == 9  # one base call + 8 grid points
    assert [point.metric for point in result.points] == [
        111.0,
        211.0,
        121.0,
        221.0,
        112.0,
        212.0,
        122.0,
        222.0,
    ]
    assert result.base_metric == 111.0
    assert result.base_params == base_params
    assert result.metric_name == "sharpe_ratio"
    assert result.metric_mean == 166.5  # 1332 / 8
    assert result.metric_std == pytest.approx(math.sqrt(2525.25))  # population std
    assert result.metric_min == 111.0
    assert result.metric_max == 222.0
    assert result.metric_range == 111.0
    assert result.stability == pytest.approx(166.5 / math.sqrt(2525.25))
    assert result.positive_ratio == 1.0
    assert result.robust_ratio == 1.0  # every point is above 0.5 * 111
    assert result.is_robust is True
    assert result.worst_case_return == pytest.approx(111.0 / INITIAL_BALANCE)
    assert result.best_params == {"a": 2, "b": 20, "c": 200}
    assert [point.n_trades for point in result.points] == [0] * 8


def test_parameter_sweep_robust_ratio_uses_the_base_metric(ohlcv_frame: pd.DataFrame) -> None:
    runner, _ = _score_runner(lambda params: float(params["a"]))

    result = parameter_sweep(
        runner,
        ohlcv_frame,
        {"a": [0, 2, 4]},
        base_params={"a": 2},
        metric_fn=_metadata_score,
    )

    # threshold = 0.5 * 2 = 1 -> only a=2 and a=4 qualify
    assert [point.metric for point in result.points] == [0.0, 2.0, 4.0]
    assert result.robust_ratio == pytest.approx(2.0 / 3.0)
    assert result.positive_ratio == pytest.approx(2.0 / 3.0)
    assert result.is_robust is False


def test_parameter_sweep_without_base_params_compares_to_zero(ohlcv_frame: pd.DataFrame) -> None:
    runner, calls = _score_runner(lambda params: float(params.get("a", 2)) - 2.0)

    result = parameter_sweep(runner, ohlcv_frame, {"a": [1, 2, 3]}, metric_fn=_metadata_score)

    assert calls[0] is None  # base call receives None, not an empty mapping
    assert calls[1:] == [{"a": 1}, {"a": 2}, {"a": 3}]
    assert result.base_params == {}
    assert result.base_metric == 0.0  # score of the empty parameter set
    assert [point.metric for point in result.points] == [-1.0, 0.0, 1.0]
    assert result.positive_ratio == pytest.approx(1.0 / 3.0)
    assert result.robust_ratio == pytest.approx(2.0 / 3.0)  # metric >= base_metric == 0
    assert result.is_robust is False


def test_parameter_sweep_is_robust_with_seventy_percent_positive(ohlcv_frame: pd.DataFrame) -> None:
    runner, _ = _score_runner(lambda params: float(params.get("a", 0)))

    result = parameter_sweep(
        runner, ohlcv_frame, {"a": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}, metric_fn=_metadata_score
    )

    assert result.positive_ratio == 1.0
    assert result.is_robust is True
    assert result.n_points == 10


def test_single_point_grid_has_zero_std_and_no_stability(ohlcv_frame: pd.DataFrame) -> None:
    runner, _ = _score_runner(lambda params: 3.0)

    result = parameter_sweep(runner, ohlcv_frame, {"a": [1]}, metric_fn=_metadata_score)

    assert result.n_points == 1
    assert result.metric_std == 0.0
    assert result.stability == 0.0
    assert result.metric_mean == 3.0
    assert result.metric_min == result.metric_max == 3.0


# ---------------------------------------------------------------------------
# runner contract
# ---------------------------------------------------------------------------


def test_base_params_are_merged_into_every_runner_call(ohlcv_frame: pd.DataFrame) -> None:
    runner, calls = _score_runner(lambda params: float(params["a"]))
    base_params = {"a": 9, "b": 3, "strategy": "basic"}

    parameter_sweep(
        runner,
        ohlcv_frame,
        {"a": [1, 2]},
        base_params=base_params,
        metric_fn=_metadata_score,
    )

    assert calls[0] == base_params
    assert calls[1] == {"a": 1, "b": 3, "strategy": "basic"}
    assert calls[2] == {"a": 2, "b": 3, "strategy": "basic"}


def test_points_carry_the_full_merged_mapping(ohlcv_frame: pd.DataFrame) -> None:
    runner, _ = _score_runner(lambda params: float(params["a"]))

    result = parameter_sweep(
        runner,
        ohlcv_frame,
        {"b": [1, 2]},
        base_params={"a": 7},
        metric_fn=_metadata_score,
    )

    assert [point.params for point in result.points] == [{"a": 7, "b": 1}, {"a": 7, "b": 2}]
    assert [point.key for point in result.points] == ["a=7,b=1", "a=7,b=2"]


def test_points_record_the_trade_count_of_their_result(ohlcv_frame: pd.DataFrame) -> None:
    def _runner(data: pd.DataFrame, params: Mapping[str, object] | None = None) -> BacktestResult:
        count = int((params or {}).get("a", 0))
        return _result_with_score(data, float(count), n_trades=count)

    result = parameter_sweep(
        runner=_runner, data=ohlcv_frame, grid={"a": [1, 2, 3]}, metric_fn=_metadata_score
    )

    assert [point.metric for point in result.points] == [1.0, 2.0, 3.0]
    assert [point.n_trades for point in result.points] == [1, 2, 3]
    assert result.best_params == {"a": 3}


def test_total_return_falls_back_on_the_sweep_initial_balance(ohlcv_frame: pd.DataFrame) -> None:
    def _runner(data: pd.DataFrame, params: Mapping[str, object] | None = None) -> BacktestResult:
        return _result_with_score(data, 0.0, initial_balance=0.0)

    result = parameter_sweep(
        _runner,
        ohlcv_frame,
        {"a": [1]},
        metric_fn=_metadata_score,
        initial_balance=200.0,
    )

    assert result.points[0].total_return == pytest.approx(0.0 / 200.0 - 1.0)


# ---------------------------------------------------------------------------
# ordering helpers
# ---------------------------------------------------------------------------


def test_best_orders_by_metric_descending(ohlcv_frame: pd.DataFrame) -> None:
    runner, _ = _score_runner(lambda params: float(params.get("a", 0) + params.get("b", 0)))

    result = parameter_sweep(
        runner, ohlcv_frame, {"a": [1, 2], "b": [3, 4]}, metric_fn=_metadata_score
    )

    # metrics: a=1,b=3 -> 4 | a=1,b=4 -> 5 | a=2,b=3 -> 5 | a=2,b=4 -> 6
    assert [point.key for point in result.best(2)] == ["a=2,b=4", "a=1,b=4"]
    assert [point.metric for point in result.best(2)] == [6.0, 5.0]
    assert [point.key for point in result.best(DEFAULT_BEST_COUNT)] == [
        "a=2,b=4",
        "a=1,b=4",  # tie on 5.0, broken by key ascending
        "a=2,b=3",
        "a=1,b=3",
    ]


def test_best_breaks_ties_deterministically(ohlcv_frame: pd.DataFrame) -> None:
    runner, _ = _score_runner(lambda params: 0.0)

    result = parameter_sweep(
        runner, ohlcv_frame, {"a": [1, 2], "b": [3, 4]}, metric_fn=_metadata_score
    )

    assert [point.key for point in result.best(2)] == ["a=1,b=3", "a=1,b=4"]
    assert [point.key for point in result.best()] == ["a=1,b=3", "a=1,b=4", "a=2,b=3", "a=2,b=4"]
    assert result.best(0) == []
    assert result.best(-1) == []


def test_robustness_result_can_be_built_directly() -> None:
    point = RobustnessPoint(params={"a": 1}, metric=2.0, n_trades=1, total_return=0.01)

    result = RobustnessResult(metric_name="m", base_params={}, base_metric=1.0, points=[point])

    assert result.n_points == 0  # derived fields are filled by parameter_sweep
    assert result.metric_range == 0.0
    assert result.best(1) == [point]
    assert set(result.to_dict()) == DOCUMENTED_RESULT_KEYS
    assert json.loads(json.dumps(result.to_dict()))["points"][0]["params"] == {"a": 1}


# ---------------------------------------------------------------------------
# validation and error handling
# ---------------------------------------------------------------------------


def test_parameter_sweep_rejects_an_empty_grid(ohlcv_frame: pd.DataFrame) -> None:
    runner, calls = _score_runner(lambda params: 0.0)

    with pytest.raises(ValidationLayerError, match="empty parameter grid"):
        parameter_sweep(runner, ohlcv_frame, {}, metric_fn=_metadata_score)

    assert calls == []


def test_parameter_sweep_rejects_a_grid_expanding_to_zero_combinations(
    ohlcv_frame: pd.DataFrame,
) -> None:
    runner, calls = _score_runner(lambda params: 0.0)

    with pytest.raises(ValidationLayerError, match="0 combinations"):
        parameter_sweep(runner, ohlcv_frame, {"a": []}, metric_fn=_metadata_score)

    assert calls == []


def test_parameter_sweep_rejects_a_grid_larger_than_max_combinations(
    ohlcv_frame: pd.DataFrame,
) -> None:
    runner, calls = _score_runner(lambda params: 0.0)

    with pytest.raises(ValidationLayerError) as excinfo:
        parameter_sweep(
            runner,
            ohlcv_frame,
            {"a": [1, 2], "b": [3, 4]},
            metric_fn=_metadata_score,
            max_combinations=1,
        )

    message = str(excinfo.value)
    assert "4" in message and "1" in message
    assert calls == []


def test_parameter_sweep_wraps_runner_failures(ohlcv_frame: pd.DataFrame) -> None:
    def _runner(data: pd.DataFrame, params: Mapping[str, object] | None = None) -> BacktestResult:
        raise RuntimeError("runner exploded")

    with pytest.raises(ValidationLayerError) as excinfo:
        parameter_sweep(_runner, ohlcv_frame, {"a": [1]}, metric_fn=_metadata_score)

    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_parameter_sweep_requires_a_contract_frame() -> None:
    runner, _ = _score_runner(lambda params: 0.0)

    from trading_backtest.core.errors import DataValidationError

    with pytest.raises(DataValidationError):
        parameter_sweep(
            runner, pd.DataFrame({"close": [1.0, 2.0]}), {"a": [1]}, metric_fn=_metadata_score
        )


def test_parameter_sweep_accepts_an_unknown_metric_without_an_override(
    ohlcv_frame: pd.DataFrame,
) -> None:
    runner, _ = _score_runner(lambda params: 0.0)

    with pytest.raises(ValidationLayerError):
        parameter_sweep(runner, ohlcv_frame, {"a": [1]}, metric="definitely_not_a_metric")


# ---------------------------------------------------------------------------
# serialisation and determinism
# ---------------------------------------------------------------------------


def test_to_dict_is_json_serialisable_and_exhaustive(ohlcv_frame: pd.DataFrame) -> None:
    runner, _ = _score_runner(lambda params: float(params["a"]))

    result = parameter_sweep(
        runner, ohlcv_frame, {"a": [1, 2]}, base_params={"a": 1}, metric_fn=_metadata_score
    )
    payload = json.loads(json.dumps(result.to_dict(), default=str))

    assert set(payload) == DOCUMENTED_RESULT_KEYS
    assert set(payload["points"][0]) == DOCUMENTED_POINT_KEYS
    assert payload["n_points"] == 2
    assert payload["best_params"] == {"a": 2}
    assert payload["metric_name"] == "sharpe_ratio"
    assert payload["is_robust"] in (True, False)


def test_parameter_sweep_is_deterministic(ohlcv_frame: pd.DataFrame) -> None:
    grid = {"a": [1, 2, 3], "b": [0.1, 0.2]}
    first_runner, _ = _score_runner(lambda params: float(params.get("a", 0) + params.get("b", 0)))
    second_runner, _ = _score_runner(lambda params: float(params.get("a", 0) + params.get("b", 0)))

    first = parameter_sweep(first_runner, ohlcv_frame, grid, metric_fn=_metadata_score)
    second = parameter_sweep(second_runner, ohlcv_frame, grid, metric_fn=_metadata_score)

    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )
    assert [point.key for point in first.points] == [point.key for point in second.points]


def test_parameter_sweep_accepts_a_sequence_grid_without_mutating_it(
    ohlcv_frame: pd.DataFrame,
) -> None:
    values: Sequence[float] = [1.0, 2.0]
    grid: dict[str, Sequence[float]] = {"a": values}
    runner, _ = _score_runner(lambda params: float(params.get("a", 0)))

    parameter_sweep(runner, ohlcv_frame, grid, metric_fn=_metadata_score)

    assert list(grid["a"]) == [1.0, 2.0]
