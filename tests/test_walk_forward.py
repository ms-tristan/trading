"""Unit tests for the walk-forward layer (``trading_backtest.validation.walk_forward``).

The walk-forward engine is exercised with two kinds of runners:

* the shared ``runner_stub`` fixture (a deterministic runner requiring no
  engine), used for the integration-shaped tests;
* local *scripted* runners whose metric is fully controlled by the test, so that
  every expected number (per-window metrics, aggregate metrics, efficiency,
  consistency) can be asserted exactly.

The file must run **before** the metrics package (``trading_backtest.metrics``)
exists: only the single real-metrics integration test is guarded by
``pytest.importorskip``, everything else injects ``metric_fn`` or a fake metrics
module.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable, Sequence
from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from trading_backtest.core.constants import UTC
from trading_backtest.core.errors import ValidationLayerError
from trading_backtest.core.models import (
    BacktestResult,
    Direction,
    ExitReason,
    RunnerFn,
    TradeRecord,
)
from trading_backtest.data.synthetic import make_ohlcv
from trading_backtest.validation.split import Window, make_windows
from trading_backtest.validation.walk_forward import (
    CONSISTENCY_THRESHOLD,
    WalkForwardResult,
    _aggregate,
    _metric_call_order,
    _stitch_equity,
    walk_forward,
)

FRAME_SIZE = 1000
N_WINDOWS = 5

DOCUMENTED_KEYS = {
    "windows",
    "mode",
    "in_sample_ratio",
    "metric_name",
    "aggregate_oos_metric",
    "aggregate_is_metric",
    "efficiency",
    "is_consistent",
    "consistency_ratio",
    "n_windows",
    "n_oos_trades",
    "mean_oos_metric",
    "mean_is_metric",
    "stitched_oos_equity",
}


@pytest.fixture
def frame() -> pd.DataFrame:
    return make_ohlcv(FRAME_SIZE)


def _is_slice(frame: pd.DataFrame, window: Window) -> pd.DataFrame:
    return frame.loc[window.is_start : window.is_end]


def _oos_slice(frame: pd.DataFrame, window: Window) -> pd.DataFrame:
    return frame.loc[window.oos_start : window.oos_end]


# ---------------------------------------------------------------------------
# local runners
# ---------------------------------------------------------------------------


def _result_with(
    data: pd.DataFrame, *, n_trades: int, initial_balance: float = 10_000.0
) -> BacktestResult:
    """Build a deterministic result over ``data`` holding exactly ``n_trades`` trades."""
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
                params_id="scripted",
            )
        )
    equity = pd.Series(np.full(count, initial_balance, dtype="float64"), index=index, name="equity")
    return BacktestResult(
        strategy_name="scripted",
        symbol="BTC/USDT",
        timeframe="1h",
        start=index[0],
        end=index[-1],
        initial_balance=initial_balance,
        final_balance=float(initial_balance),
        trades=trades,
        equity_curve=equity,
        params={},
        metadata={"runner": "scripted"},
    )


def _scripted_runner(
    trade_counts: Sequence[int],
) -> tuple[RunnerFn, list[tuple[pd.DataFrame, object]]]:
    """Return a runner emitting the i-th trade count on the i-th call.

    ``walk_forward`` calls the runner twice per window (in-sample first), so the
    sequence is ``[is_0, oos_0, is_1, oos_1, ...]``.  Every call is recorded.
    """
    remaining = list(trade_counts)
    calls: list[tuple[pd.DataFrame, object]] = []

    def _runner(data: pd.DataFrame, params: object = None) -> BacktestResult:
        if not remaining:
            raise AssertionError("runner called more times than scripted")
        calls.append((data, params))
        return _result_with(data, n_trades=remaining.pop(0))

    return _runner, calls


def _metric_from_trades(result: BacktestResult) -> float:
    """Reference scoring function used by most tests: the trade count."""
    return float(result.n_trades)


# ---------------------------------------------------------------------------
# exact metrics with a scripted runner
# ---------------------------------------------------------------------------


def test_walk_forward_window_metrics_are_exact(frame: pd.DataFrame) -> None:
    runner, calls = _scripted_runner([5, 3, 5, 0, 5, 1])

    result = walk_forward(runner, frame, metric_fn=_metric_from_trades, n_windows=3)

    assert result.n_windows == 3
    assert [window.is_metric for window in result.windows] == [5.0, 5.0, 5.0]
    assert [window.oos_metric for window in result.windows] == [3.0, 0.0, 1.0]
    assert [window.n_oos_trades for window in result.windows] == [3, 0, 1]
    assert result.n_oos_trades == 4
    assert result.aggregate_oos_metric == 4.0
    assert result.aggregate_is_metric == 15.0
    assert result.efficiency == pytest.approx(4.0 / 15.0)
    assert result.mean_oos_metric == pytest.approx(4.0 / 3.0)
    assert result.mean_is_metric == 5.0
    assert result.consistency_ratio == pytest.approx(2.0 / 3.0)
    assert result.is_consistent is True
    assert result.metric_name == "sharpe_ratio"
    assert result.mode == "rolling"
    assert result.in_sample_ratio == 0.7
    assert len(calls) == 6
    assert all(params is None for _, params in calls)
    windows = make_windows(frame, n_windows=3)
    expected_bounds = [
        (window.is_start, window.is_end) if position == 0 else (window.oos_start, window.oos_end)
        for window in windows
        for position in (0, 1)
    ]
    assert [
        (pd.Timestamp(data.index[0]), pd.Timestamp(data.index[-1])) for data, _ in calls
    ] == expected_bounds


def test_walk_forward_is_inconsistent_when_most_windows_lose(frame: pd.DataFrame) -> None:
    runner, _ = _scripted_runner([0, 3, 0, 0, 0, 0])

    result = walk_forward(runner, frame, metric_fn=_metric_from_trades, n_windows=3)

    assert [window.oos_metric for window in result.windows] == [3.0, 0.0, 0.0]
    assert result.consistency_ratio == pytest.approx(1.0 / 3.0)
    assert result.is_consistent is False
    assert result.consistency_ratio < CONSISTENCY_THRESHOLD


def test_walk_forward_efficiency_is_zero_without_in_sample_edge(frame: pd.DataFrame) -> None:
    runner, _ = _scripted_runner([0, 3, 0, 1, 0, 2])

    result = walk_forward(runner, frame, metric_fn=_metric_from_trades, n_windows=3)

    assert result.aggregate_oos_metric == 6.0
    assert result.aggregate_is_metric == 0.0
    assert result.efficiency == 0.0


def test_walk_forward_with_shared_runner_stub(runner_stub: RunnerFn, frame: pd.DataFrame) -> None:
    result = walk_forward(runner_stub, frame, metric_fn=_metric_from_trades, n_windows=N_WINDOWS)

    assert result.n_windows == N_WINDOWS
    assert all(window.is_metric == 3.0 for window in result.windows)
    assert all(window.oos_metric == 3.0 for window in result.windows)
    assert all(window.n_oos_trades == 3 for window in result.windows)
    assert result.n_oos_trades == 15
    assert result.aggregate_oos_metric == 15.0
    assert result.aggregate_is_metric == 15.0
    assert result.efficiency == 1.0
    assert result.consistency_ratio == 1.0
    assert result.is_consistent is True
    assert all(window.is_result.n_trades == 3 for window in result.windows)


def test_window_without_oos_trades_does_not_crash(runner_stub: RunnerFn) -> None:
    frame = make_ohlcv(8)  # one window: 5 in-sample rows (3 stub trades), 3 OOS rows (none)

    result = walk_forward(
        runner_stub, frame, metric_fn=_metric_from_trades, n_windows=1, in_sample_ratio=0.7
    )

    assert result.n_windows == 1
    window = result.windows[0]
    assert window.n_oos_trades == 0
    assert window.oos_metric == 0.0
    assert window.is_metric == 3.0
    assert result.n_oos_trades == 0
    assert result.aggregate_oos_metric == 0.0
    assert result.is_consistent is False


def test_rolling_and_anchored_differ_in_in_sample_length(
    runner_stub: RunnerFn, frame: pd.DataFrame
) -> None:
    rolling = walk_forward(runner_stub, frame, metric_fn=_metric_from_trades, n_windows=3)
    anchored = walk_forward(
        runner_stub, frame, metric_fn=_metric_from_trades, n_windows=3, mode="anchored"
    )

    block = FRAME_SIZE // 3
    in_sample_length = int(block * 0.7)
    assert [len(_is_slice(frame, window.window)) for window in rolling.windows] == [
        in_sample_length
    ] * 3
    assert [len(_is_slice(frame, window.window)) for window in anchored.windows] == [
        in_sample_length,
        block + in_sample_length,
        2 * block + in_sample_length,
    ]
    assert [len(_oos_slice(frame, window.window)) for window in rolling.windows] == [
        block - in_sample_length
    ] * 3
    assert [len(_oos_slice(frame, window.window)) for window in anchored.windows] == [
        block - in_sample_length
    ] * 3
    assert anchored.mode == "anchored"
    assert all(
        previous.window.is_end < following.window.is_end
        for previous, following in pairwise(anchored.windows)
    )


# ---------------------------------------------------------------------------
# stitched out-of-sample equity
# ---------------------------------------------------------------------------


def test_stitched_oos_equity_is_rebased_and_contiguous(
    runner_stub: RunnerFn, frame: pd.DataFrame
) -> None:
    result = walk_forward(runner_stub, frame, metric_fn=lambda _result: 0.0, n_windows=N_WINDOWS)

    stitched = result.stitched_oos_equity
    oos_lengths = [len(_oos_slice(frame, window.window)) for window in result.windows]

    assert len(stitched) == sum(oos_lengths)
    assert stitched.index.is_monotonic_increasing
    assert stitched.index.tz is not None
    assert str(stitched.index.tz) == UTC
    assert stitched.index.name == "timestamp"
    assert stitched.name == "equity"
    assert stitched.iloc[0] == pytest.approx(result.windows[0].oos_result.initial_balance)
    assert stitched.iloc[0] == pytest.approx(10_000.0)
    assert pd.Timestamp(stitched.index[0]) == result.windows[0].window.oos_start
    assert pd.Timestamp(stitched.index[-1]) == result.windows[-1].window.oos_end


def test_walk_forward_accepts_an_explicit_initial_balance(
    runner_stub: RunnerFn, frame: pd.DataFrame
) -> None:
    result = walk_forward(
        runner_stub,
        frame,
        metric_fn=_metric_from_trades,
        n_windows=2,
        initial_balance=20_000.0,
    )

    assert result.stitched_oos_equity.iloc[0] == pytest.approx(10_000.0)  # the stub's own balance
    assert result.aggregate_oos_metric == 6.0


def test_stitch_equity_of_no_result_is_empty() -> None:
    stitched = _stitch_equity([])

    assert len(stitched) == 0
    assert stitched.dtype == "float64"
    assert stitched.index.tz is not None


# ---------------------------------------------------------------------------
# metric resolution
# ---------------------------------------------------------------------------


def test_unknown_metric_fails_before_any_runner_call(frame: pd.DataFrame) -> None:
    calls: list[pd.DataFrame] = []

    def _runner(data: pd.DataFrame, params: object = None) -> BacktestResult:
        calls.append(data)
        return _result_with(data, n_trades=0)

    with pytest.raises(ValidationLayerError):
        walk_forward(_runner, frame, metric="definitely_not_a_metric", n_windows=2)

    assert calls == []


def test_unknown_metric_message_lists_the_available_metrics(
    monkeypatch: pytest.MonkeyPatch, frame: pd.DataFrame
) -> None:
    module = types.ModuleType("trading_backtest.metrics")
    module.METRIC_NAMES = ("sharpe_ratio", "total_return")  # type: ignore[attr-defined]
    module.metric_value = lambda result, name: 0.0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics", module)
    calls: list[pd.DataFrame] = []

    def _runner(data: pd.DataFrame, params: object = None) -> BacktestResult:
        calls.append(data)
        return _result_with(data, n_trades=0)

    with pytest.raises(ValidationLayerError, match=r"unknown metric: 'nope'") as excinfo:
        walk_forward(_runner, frame, metric="nope", n_windows=2)

    assert "sharpe_ratio" in str(excinfo.value)
    assert calls == []


@pytest.mark.parametrize(
    "signature_style",
    ["result_first", "name_first", "factory"],
)
def test_metrics_layer_is_used_without_an_override(
    monkeypatch: pytest.MonkeyPatch,
    runner_stub: RunnerFn,
    frame: pd.DataFrame,
    signature_style: str,
) -> None:
    module = types.ModuleType("trading_backtest.metrics")
    module.METRIC_NAMES = ("custom_score",)  # type: ignore[attr-defined]
    if signature_style == "result_first":
        module.metric_value = lambda result, name: float(result.n_trades) * 10.0  # type: ignore[attr-defined]
    elif signature_style == "name_first":
        module.metric_value = lambda name, result: float(result.n_trades) * 10.0  # type: ignore[attr-defined]
    else:
        module.metric_value = lambda name: (  # type: ignore[attr-defined]
            lambda result: float(result.n_trades) * 10.0
        )
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics", module)

    result = walk_forward(runner_stub, frame, metric="custom_score", n_windows=2)

    assert result.n_windows == 2
    assert result.aggregate_oos_metric == 60.0  # 2 windows x 3 trades x 10
    assert result.aggregate_is_metric == 60.0
    assert result.efficiency == 1.0


def test_metric_call_order_handles_every_metric_value_signature() -> None:
    def result_first(result: BacktestResult, name: str) -> float:
        return 0.0

    def name_first(name: str, metric_key: str) -> float:
        return 0.0

    def factory(name: str) -> Callable[[BacktestResult], float]:
        return lambda result: 0.0

    def generic(first_argument: object, second_argument: object) -> float:
        return 0.0

    assert _metric_call_order(result_first) == "result_first"
    assert _metric_call_order(name_first) == "name_first"
    assert _metric_call_order(factory) == "factory"
    assert _metric_call_order(generic) == "result_first"
    assert _metric_call_order(min) == "result_first"  # builtins expose no signature


def test_missing_metrics_layer_is_reported(
    monkeypatch: pytest.MonkeyPatch, runner_stub: RunnerFn, frame: pd.DataFrame
) -> None:
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics", None)

    with pytest.raises(ValidationLayerError, match="not importable"):
        walk_forward(runner_stub, frame, metric="sharpe_ratio", n_windows=2)


def test_real_metrics_layer_integration(runner_stub: RunnerFn, frame: pd.DataFrame) -> None:
    pytest.importorskip("trading_backtest.metrics")

    result = walk_forward(runner_stub, frame, metric="sharpe_ratio", n_windows=2)

    assert result.n_windows == 2
    assert result.metric_name == "sharpe_ratio"
    assert isinstance(result.aggregate_oos_metric, float)
    assert isinstance(result.consistency_ratio, float)


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def test_raising_runner_is_wrapped_with_its_cause(frame: pd.DataFrame) -> None:
    def _runner(data: pd.DataFrame, params: object = None) -> BacktestResult:
        raise ZeroDivisionError("runner blew up")

    with pytest.raises(ValidationLayerError) as excinfo:
        walk_forward(_runner, frame, metric_fn=_metric_from_trades, n_windows=2)

    assert isinstance(excinfo.value.__cause__, ZeroDivisionError)
    assert "runner blew up" in str(excinfo.value)


def test_runner_returning_the_wrong_type_is_rejected(frame: pd.DataFrame) -> None:
    def _runner(data: pd.DataFrame, params: object = None) -> BacktestResult:
        return {"not": "a result"}  # type: ignore[return-value]

    with pytest.raises(ValidationLayerError, match="expected BacktestResult"):
        walk_forward(_runner, frame, metric_fn=_metric_from_trades, n_windows=1)


def test_metric_errors_propagate_for_non_empty_results(
    runner_stub: RunnerFn, frame: pd.DataFrame
) -> None:
    def _explode(result: BacktestResult) -> float:
        raise ZeroDivisionError("metric blew up")

    with pytest.raises(ZeroDivisionError):
        walk_forward(runner_stub, frame, metric_fn=_explode, n_windows=1)


def test_metric_errors_are_absorbed_for_trade_less_results(frame: pd.DataFrame) -> None:
    runner, _ = _scripted_runner([0, 0])

    def _explode(result: BacktestResult) -> float:
        raise ZeroDivisionError("no trade to score")

    result = walk_forward(runner, frame, metric_fn=_explode, n_windows=1)

    assert result.windows[0].oos_metric == 0.0
    assert result.aggregate_oos_metric == 0.0


def test_walk_forward_rejects_data_that_is_not_a_contract_frame(frame: pd.DataFrame) -> None:
    from trading_backtest.core.errors import DataValidationError

    with pytest.raises(DataValidationError):
        walk_forward(
            lambda data, params=None: _result_with(frame, n_trades=0),
            pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}),
            metric_fn=_metric_from_trades,
            n_windows=1,
        )


def test_walk_forward_rejects_an_unknown_mode(frame: pd.DataFrame) -> None:
    with pytest.raises(ValidationLayerError, match="unknown walk-forward mode"):
        walk_forward(
            lambda data, params=None: _result_with(data, n_trades=0),
            frame,
            metric_fn=_metric_from_trades,
            n_windows=2,
            mode="sliding",
        )


# ---------------------------------------------------------------------------
# serialisation, properties, aggregation helper
# ---------------------------------------------------------------------------


def test_to_dict_exposes_every_documented_key_and_is_json_serialisable(
    runner_stub: RunnerFn, frame: pd.DataFrame
) -> None:
    result = walk_forward(runner_stub, frame, metric_fn=_metric_from_trades, n_windows=2)

    payload = json.loads(json.dumps(result.to_dict(), default=str))

    assert set(payload) == DOCUMENTED_KEYS
    assert payload["n_windows"] == 2
    assert payload["metric_name"] == "sharpe_ratio"
    assert len(payload["windows"]) == 2
    assert set(payload["windows"][0]) == {
        "window",
        "is_result",
        "oos_result",
        "is_metric",
        "oos_metric",
        "n_oos_trades",
    }
    assert set(payload["windows"][0]["window"]) == {
        "index",
        "is_start",
        "is_end",
        "oos_start",
        "oos_end",
    }
    assert set(payload["stitched_oos_equity"]) == {"timestamps", "values"}
    assert len(payload["stitched_oos_equity"]["values"]) == len(result.stitched_oos_equity)
    assert payload["stitched_oos_equity"]["values"][0] == pytest.approx(10_000.0)
    assert payload["consistency_ratio"] == pytest.approx(1.0)
    assert payload["mean_oos_metric"] == pytest.approx(3.0)
    assert payload["mean_is_metric"] == pytest.approx(3.0)


def test_walk_forward_is_deterministic(runner_stub: RunnerFn, frame: pd.DataFrame) -> None:
    first = walk_forward(runner_stub, frame, metric_fn=_metric_from_trades, n_windows=3)
    second = walk_forward(runner_stub, frame, metric_fn=_metric_from_trades, n_windows=3)

    assert json.dumps(first.to_dict(), default=str) == json.dumps(second.to_dict(), default=str)


def test_empty_walk_forward_result_properties() -> None:
    empty_equity = pd.Series(
        [], index=pd.DatetimeIndex([], tz=UTC, name="timestamp"), name="equity", dtype="float64"
    )
    result = WalkForwardResult(
        windows=[],
        mode="rolling",
        in_sample_ratio=0.7,
        metric_name="sharpe_ratio",
        stitched_oos_equity=empty_equity,
        aggregate_oos_metric=0.0,
        aggregate_is_metric=0.0,
        efficiency=0.0,
        is_consistent=False,
        n_windows=0,
        n_oos_trades=0,
    )

    assert result.mean_oos_metric == 0.0
    assert result.mean_is_metric == 0.0
    assert result.consistency_ratio == 0.0
    assert set(result.to_dict()) == DOCUMENTED_KEYS
    assert json.loads(json.dumps(result.to_dict(), default=str))["windows"] == []


def test_aggregate_helper_concatenates_trades_and_equity(
    runner_stub: RunnerFn, frame: pd.DataFrame
) -> None:
    first = runner_stub(frame.iloc[:100], None)
    second = runner_stub(frame.iloc[100:200], None)

    aggregate = _aggregate([first, second])

    assert aggregate.n_trades == first.n_trades + second.n_trades == 6
    assert len(aggregate.equity_curve) == 200
    assert aggregate.initial_balance == first.initial_balance
    assert aggregate.final_balance == pytest.approx(
        first.final_balance * second.final_balance / second.initial_balance
    )
    assert aggregate.start == first.start
    assert aggregate.end == second.end
    assert aggregate.params == {}
    assert aggregate.metadata["aggregated"] is True
    assert aggregate.metadata["n_results"] == 2


def test_aggregate_helper_respects_an_explicit_initial_balance(
    runner_stub: RunnerFn, frame: pd.DataFrame
) -> None:
    result = runner_stub(frame.iloc[:100], None)

    aggregate = _aggregate([result], initial_balance=5_000.0)

    assert aggregate.initial_balance == 5_000.0
    assert len(aggregate.equity_curve) == 100
    assert aggregate.equity_curve.iloc[0] == pytest.approx(
        5_000.0 * result.equity_curve.iloc[0] / result.initial_balance
    )


def test_aggregate_helper_rejects_an_empty_sequence() -> None:
    with pytest.raises(ValidationLayerError, match="empty sequence"):
        _aggregate([])
