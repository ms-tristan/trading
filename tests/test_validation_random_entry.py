"""Unit tests for the random-entry gate (``trading_backtest.validation.random_entry``).

The gate answers the sharpest question of the whole framework -- *did the strategy
beat luck?* -- and it must answer it **before** the metrics package
(``trading_backtest.metrics``) exists: every verdict test injects a duck-typed
random-entry function through ``entry_fn`` and never imports that layer.  Only the
integration test at the end is guarded by ``pytest.importorskip`` and exercises the
real ``random_entry_benchmark``.

Nothing here is random or networked: the frames are synthetic, the injected
distributions are hand-built and the simulations use an explicit seed.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from dataclasses import FrozenInstanceError, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pytest

from trading_backtest.core.constants import DEFAULT_INITIAL_BALANCE, OHLCV_INDEX_NAME, UTC
from trading_backtest.core.errors import ValidationLayerError
from trading_backtest.core.models import BacktestResult, Direction, ExitReason, TradeRecord
from trading_backtest.validation.random_entry import (
    MIN_P_VALUE,
    RandomEntryGateResult,
    validate_random_entry,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from trading_backtest.metrics.random_entry import RandomEntryResult

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The exact key set of :meth:`RandomEntryGateResult.to_dict`, in order.
GATE_KEYS: tuple[str, ...] = (
    "n_simulations",
    "random_seed",
    "n_trades",
    "holding_periods",
    "exposure",
    "initial_balance",
    "timeframe",
    "strategy_total_return",
    "mean_return",
    "median_return",
    "std_return",
    "percentile",
    "p_value",
    "min_p_value",
    "strategy_beats_random",
    "percentiles",
    "distribution",
)

#: The percentile labels of the injected distribution (mirrors the metrics layer).
STUB_PERCENTILES: dict[str, float] = {
    "p05": -0.12,
    "p25": -0.02,
    "p50": 0.01,
    "p75": 0.05,
    "p95": 0.13,
}


# ---------------------------------------------------------------------------
# helpers: a frame, a completed run, and duck-typed stand-ins for the metrics layer
# ---------------------------------------------------------------------------


def make_frame(closes: Sequence[float], *, start: str = "2024-01-01T00:00:00Z") -> pd.DataFrame:
    """Return a minimal OHLCV frame whose closes are ``closes`` (hourly, UTC)."""
    values = np.asarray(closes, dtype="float64")
    index = pd.date_range(start, periods=len(values), freq="1h", tz=UTC, name=OHLCV_INDEX_NAME)
    return pd.DataFrame(
        {
            "open": values,
            "high": values,
            "low": values,
            "close": values,
            "volume": np.ones(len(values), dtype="float64"),
        },
        index=index,
    )


def wiggle_frame(n: int = 60) -> pd.DataFrame:
    """Return a deterministically wiggly frame (many different entry outcomes)."""
    steps = np.arange(n, dtype="float64")
    return make_frame(100.0 + 3.0 * steps + 20.0 * np.sin(2.0 * np.pi * steps / 10.0))


def make_result(
    frame: pd.DataFrame, *, n_trades: int = 4, duration_minutes: float = 60.0
) -> BacktestResult:
    """Return a completed run: a rising equity curve and ``n_trades`` long trades."""
    index = pd.DatetimeIndex(frame.index)
    initial_balance = DEFAULT_INITIAL_BALANCE
    equity = pd.Series(
        np.linspace(initial_balance, initial_balance * 1.2, len(index)),
        index=index,
        name="equity",
    )
    start = pd.Timestamp(index[0])
    trades = [
        TradeRecord(
            entry_time=start,
            exit_time=start + pd.Timedelta(minutes=duration_minutes),
            entry_price=100.0,
            exit_price=101.0,
            size=1.0,
            direction=Direction.LONG,
            pnl=10.0,
            pnl_pct=0.01,
            fees=0.0,
            exit_reason=ExitReason.SIGNAL,
            duration_minutes=float(duration_minutes),
        )
        for _ in range(n_trades)
    ]
    return BacktestResult(
        strategy_name="unit-test",
        symbol="BTC/USDT",
        timeframe="1h",
        start=start,
        end=pd.Timestamp(index[-1]),
        initial_balance=initial_balance,
        final_balance=float(equity.iloc[-1]),
        trades=trades,
        equity_curve=equity,
        params={},
    )


@dataclass
class StubDistribution:
    """Duck-typed stand-in for ``RandomEntryResult`` -- no metrics import."""

    n_simulations: int = 200
    random_seed: int = 42
    n_trades: int = 4
    holding_periods: int = 3
    exposure: float = 0.203
    initial_balance: float = 10_000.0
    timeframe: str = "1h"
    strategy_total_return: float = 0.31
    mean_return: float = 0.012
    median_return: float = 0.010
    std_return: float = 0.070
    percentile: float = 88.0
    p_value: float = 0.0
    variant: str = "random_entry"
    percentiles: dict[str, float] = field(default_factory=lambda: dict(STUB_PERCENTILES))
    returns: list[float] = field(default_factory=lambda: [-0.12, 0.01, 0.13])
    final_balances: list[float] = field(default_factory=lambda: [8_800.0, 10_100.0, 11_300.0])

    def to_dict(self) -> dict[str, Any]:
        """Return a minimal mapping, enough to check the nested ``distribution``."""
        return {
            "variant": str(self.variant),
            "n_simulations": int(self.n_simulations),
            "random_seed": int(self.random_seed),
            "strategy_total_return": float(self.strategy_total_return),
            "percentiles": {key: float(value) for key, value in self.percentiles.items()},
            "percentile": float(self.percentile),
            "p_value": float(self.p_value),
            "returns": [float(value) for value in self.returns],
            "final_balances": [float(value) for value in self.final_balances],
        }


class RecordingEntry:
    """Callable ``entry_fn`` that records how it was called."""

    def __init__(self, distribution: StubDistribution) -> None:
        self.distribution = distribution
        self.calls: list[dict[str, Any]] = []

    def __call__(self, result: object, data: object, **kwargs: Any) -> StubDistribution:
        """Record the call and return the canned distribution."""
        self.calls.append({"result": result, "data": data, **kwargs})
        return self.distribution


def stub_gate(
    distribution: StubDistribution, **overrides: Any
) -> tuple[RandomEntryGateResult, RecordingEntry]:
    """Gate ``distribution`` through an injected ``entry_fn`` (no metrics layer)."""
    recorder = RecordingEntry(distribution)
    gate = validate_random_entry(
        object(), make_frame((100.0, 101.0, 102.0)), entry_fn=recorder, **overrides
    )
    return gate, recorder


def assert_json_native(value: Any) -> None:
    """Assert ``value`` only contains JSON-native types (no numpy scalar)."""
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            assert_json_native(item)
    elif isinstance(value, list):
        for item in value:
            assert_json_native(item)
    else:
        assert value is None or isinstance(value, (str, bool, int, float))


# ---------------------------------------------------------------------------
# module surface
# ---------------------------------------------------------------------------


def test_min_p_value_is_the_documented_threshold() -> None:
    import trading_backtest.validation as validation

    assert MIN_P_VALUE == 0.05
    assert isinstance(MIN_P_VALUE, float)
    assert validation.MIN_P_VALUE is MIN_P_VALUE
    assert {"MIN_P_VALUE", "RandomEntryFn", "RandomEntryGateResult"} <= set(validation.__all__)
    assert "validate_random_entry" in validation.__all__
    assert validation.RandomEntryGateResult is RandomEntryGateResult
    assert validation.validate_random_entry is validate_random_entry


def test_validation_all_keeps_the_pre_existing_symbols() -> None:
    import trading_backtest.validation as validation

    pre_existing = {
        "MIN_ALPHA",
        "BenchmarkGateResult",
        "validate_benchmark",
        "monte_carlo",
        "MonteCarloResult",
        "parameter_sweep",
        "RobustnessResult",
        "walk_forward",
        "WalkForwardResult",
        "make_windows",
    }
    assert pre_existing <= set(validation.__all__)
    assert {"MIN_P_VALUE", "RandomEntryFn", "RandomEntryGateResult"} <= set(validation.__all__)
    assert len(validation.__all__) == len(set(validation.__all__))
    for name in validation.__all__:
        assert getattr(validation, name, None) is not None


def test_random_entry_gate_has_no_risk_free_rate_parameter() -> None:
    """The comparison is on ``total_return``: a riskless rate cannot enter it."""
    parameters = inspect.signature(validate_random_entry).parameters

    assert "risk_free_rate" not in parameters
    assert tuple(parameters) == (
        "result",
        "data",
        "n_simulations",
        "random_seed",
        "fee_rate",
        "slippage",
        "min_p_value",
        "entry_fn",
    )
    assert parameters["n_simulations"].default == 1000
    assert parameters["random_seed"].default == 42
    assert parameters["min_p_value"].default == MIN_P_VALUE
    assert parameters["entry_fn"].default is None


def test_validation_package_imports_without_the_metrics_layer(tmp_path: Path) -> None:
    """``import trading_backtest.validation`` must not need the metrics layer."""
    script = textwrap.dedent(
        """
        import sys

        class _BlockMetrics:
            def find_spec(self, name, path=None, target=None):
                if name == "trading_backtest.metrics" or name.startswith("trading_backtest.metrics."):
                    raise ModuleNotFoundError(f"blocked: {name}")
                return None

        sys.meta_path.insert(0, _BlockMetrics())
        import trading_backtest.validation as validation

        assert callable(validation.validate_random_entry)
        assert validation.MIN_P_VALUE == 0.05
        assert validation.RandomEntryFn is not None
        assert "trading_backtest.metrics" not in sys.modules
        print("ok")
        """
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout


def test_missing_metrics_layer_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics", None)
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics.random_entry", None)

    with pytest.raises(ValidationLayerError, match="not importable"):
        validate_random_entry(object(), make_frame((100.0, 110.0)))


def test_missing_metrics_layer_names_the_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics", None)
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics.random_entry", None)

    with pytest.raises(ValidationLayerError, match="'random_entry'"):
        validate_random_entry(object(), make_frame((100.0, 110.0)))


# ---------------------------------------------------------------------------
# the injectable seam
# ---------------------------------------------------------------------------


def test_injected_entry_fn_is_used_without_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics", None)
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics.random_entry", None)
    distribution = StubDistribution()
    frame = make_frame((100.0, 101.0, 102.0))
    result = object()
    recorder = RecordingEntry(distribution)

    gate = validate_random_entry(result, frame, entry_fn=recorder)

    assert gate.distribution is distribution
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["result"] is result
    assert recorder.calls[0]["data"] is frame
    assert recorder.calls[0]["n_simulations"] == 1000
    assert recorder.calls[0]["random_seed"] == 42
    assert recorder.calls[0]["fee_rate"] == 0.0
    assert recorder.calls[0]["slippage"] == 0.0


def test_scalars_are_copied_out_of_the_distribution() -> None:
    distribution = StubDistribution(
        n_simulations=500,
        random_seed=7,
        n_trades=9,
        holding_periods=5,
        exposure=0.75,
        initial_balance=2_500.0,
        timeframe="4h",
        strategy_total_return=-0.25,
        mean_return=0.004,
        median_return=0.003,
        std_return=0.101,
        percentile=12.5,
        p_value=0.87,
    )

    gate, _ = stub_gate(distribution)

    assert gate.n_simulations == 500
    assert gate.random_seed == 7
    assert gate.n_trades == 9
    assert gate.holding_periods == 5
    assert gate.exposure == 0.75
    assert gate.initial_balance == 2_500.0
    assert gate.timeframe == "4h"
    assert gate.strategy_total_return == -0.25
    assert gate.mean_return == 0.004
    assert gate.median_return == 0.003
    assert gate.std_return == 0.101
    assert gate.percentiles == STUB_PERCENTILES
    assert gate.percentile == 12.5
    assert gate.p_value == 0.87
    assert gate.min_p_value == MIN_P_VALUE


def test_simulation_parameters_are_passed_through() -> None:
    gate, recorder = stub_gate(
        StubDistribution(n_simulations=250, random_seed=123),
        n_simulations=250,
        random_seed=123,
        fee_rate=0.002,
        slippage=0.001,
        min_p_value=0.2,
    )

    assert recorder.calls[0]["n_simulations"] == 250
    assert recorder.calls[0]["random_seed"] == 123
    assert recorder.calls[0]["fee_rate"] == 0.002
    assert recorder.calls[0]["slippage"] == 0.001
    # the gate echoes what the distribution reports, never what was requested
    assert gate.n_simulations == 250
    assert gate.random_seed == 123
    assert gate.min_p_value == 0.2
    # the fee/slippage the simulations were charged are not echoed by the gate:
    # they live in the distribution, not in the verdict
    assert "fee_rate" not in gate.to_dict()
    assert "slippage" not in gate.to_dict()


def test_percentiles_are_copied_into_a_fresh_mapping() -> None:
    distribution = StubDistribution()

    gate, _ = stub_gate(distribution)

    assert gate.percentiles == distribution.percentiles
    assert gate.percentiles is not distribution.percentiles


# ---------------------------------------------------------------------------
# the verdict: strict inequality on the p-value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("p_value", "min_p_value", "expected"),
    [
        (0.0, MIN_P_VALUE, True),  # beats every simulation
        (0.01, MIN_P_VALUE, True),
        (0.049, MIN_P_VALUE, True),
        (0.05, MIN_P_VALUE, False),  # equality at the threshold is NOT a victory
        (0.06, MIN_P_VALUE, False),
        (0.5, MIN_P_VALUE, False),  # a coin flip
        (1.0, MIN_P_VALUE, False),  # the strategy never beat chance
        (0.1, 0.2, True),  # an explicit override moves the threshold
        (0.2, 0.2, False),
    ],
)
def test_verdict_is_a_strict_inequality(p_value: float, min_p_value: float, expected: bool) -> None:
    gate, _ = stub_gate(StubDistribution(p_value=p_value), min_p_value=min_p_value)

    assert gate.strategy_beats_random is expected
    assert gate.strategy_beats_random is bool(p_value < min_p_value)
    assert gate.min_p_value == min_p_value


def test_a_median_strategy_does_not_beat_randomness() -> None:
    """The documented reading: the 50th percentile is not an edge."""
    gate, _ = stub_gate(StubDistribution(percentile=50.0, p_value=0.5))

    assert gate.percentile == 50.0
    assert gate.strategy_beats_random is False


def test_a_95th_percentile_is_exactly_the_boundary() -> None:
    """The documented reading: at the 95th percentile there is a real signal."""
    gate, _ = stub_gate(StubDistribution(percentile=95.0, p_value=0.05))

    assert gate.percentile == 95.0
    # ... but only strictly below the threshold: 0.05 exactly does not pass
    assert gate.strategy_beats_random is False


# ---------------------------------------------------------------------------
# serialisation and immutability
# ---------------------------------------------------------------------------


def test_to_dict_has_exactly_the_documented_keys() -> None:
    gate, _ = stub_gate(StubDistribution(p_value=0.0), min_p_value=0.05)

    payload = gate.to_dict()

    assert tuple(payload) == GATE_KEYS
    assert payload["n_simulations"] == 200
    assert payload["random_seed"] == 42
    assert payload["n_trades"] == 4
    assert payload["holding_periods"] == 3
    assert payload["exposure"] == 0.203
    assert payload["initial_balance"] == 10_000.0
    assert payload["timeframe"] == "1h"
    assert payload["strategy_total_return"] == 0.31
    assert payload["mean_return"] == 0.012
    assert payload["median_return"] == 0.010
    assert payload["std_return"] == 0.070
    assert payload["percentile"] == 88.0
    assert payload["p_value"] == 0.0
    assert payload["min_p_value"] == 0.05
    assert payload["strategy_beats_random"] is True
    assert isinstance(payload["strategy_beats_random"], bool)
    assert isinstance(payload["n_simulations"], int)
    assert payload["percentiles"] == STUB_PERCENTILES


def test_to_dict_exposes_the_nested_distribution() -> None:
    distribution = StubDistribution()

    gate, _ = stub_gate(distribution)

    payload = gate.to_dict()
    nested = payload["distribution"]
    assert isinstance(nested, dict)
    assert nested == distribution.to_dict()
    assert nested["percentiles"] == STUB_PERCENTILES
    assert nested["returns"] == distribution.returns
    assert nested["final_balances"] == distribution.final_balances
    assert nested["p_value"] == payload["p_value"]


def test_to_dict_is_json_round_trippable() -> None:
    gate, _ = stub_gate(StubDistribution(p_value=0.02))

    payload = gate.to_dict()

    assert_json_native(payload)
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload


def test_gate_result_is_frozen() -> None:
    gate, _ = stub_gate(StubDistribution())

    with pytest.raises(FrozenInstanceError):
        gate.p_value = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# integration with the real metrics layer (guarded: optional package)
# ---------------------------------------------------------------------------


def test_default_path_runs_the_real_random_entry_benchmark() -> None:
    metrics_random_entry = pytest.importorskip("trading_backtest.metrics.random_entry")
    frame = wiggle_frame(60)
    result = make_result(frame)

    gate = validate_random_entry(
        result, frame, n_simulations=50, random_seed=42, fee_rate=0.001, slippage=0.0005
    )

    assert gate.n_simulations == 50
    assert gate.random_seed == 42
    assert gate.timeframe == "1h"
    assert gate.n_trades == result.n_trades
    assert 0.0 <= gate.percentile <= 100.0
    assert 0.0 <= gate.p_value <= 1.0
    assert gate.percentile / 100.0 + gate.p_value == pytest.approx(1.0)
    assert gate.strategy_beats_random is (gate.p_value < MIN_P_VALUE)
    assert gate.min_p_value == MIN_P_VALUE
    assert isinstance(gate.distribution, metrics_random_entry.RandomEntryResult)
    assert gate.distribution.n_simulations == 50
    assert gate.percentiles == gate.distribution.percentiles
    assert len(gate.distribution.returns) == 50
    assert gate.to_dict()["distribution"] == gate.distribution.to_dict()


def test_default_path_is_deterministic_for_a_fixed_seed() -> None:
    metrics_random_entry = pytest.importorskip("trading_backtest.metrics.random_entry")
    frame = wiggle_frame(60)
    result = make_result(frame)

    first = validate_random_entry(result, frame, n_simulations=40, random_seed=42)
    again = validate_random_entry(result, frame, n_simulations=40, random_seed=42)
    other = validate_random_entry(result, frame, n_simulations=40, random_seed=7)

    assert first.p_value == again.p_value
    assert first.percentile == again.percentile
    assert first.distribution.returns == again.distribution.returns
    assert first.strategy_beats_random == again.strategy_beats_random
    assert other.distribution.returns != first.distribution.returns
    assert isinstance(first.distribution, metrics_random_entry.RandomEntryResult)


def test_default_path_coheres_on_a_spiky_frame() -> None:
    """The gate stays coherent when a few candles carry all the return."""
    pytest.importorskip("trading_backtest.metrics.random_entry")
    steps = np.arange(60, dtype="float64")
    # a spiky frame: a few candles carry all the return, so random entries lose
    closes = 100.0 + steps + 40.0 * (np.abs(np.sin(steps)) > 0.97)
    frame = make_frame(closes)
    result = make_result(frame, n_trades=1)

    gate = validate_random_entry(result, frame, n_simulations=200, random_seed=42)

    assert gate.n_trades == 1
    assert 0.0 <= gate.p_value <= 1.0
    assert gate.strategy_beats_random is (gate.p_value < MIN_P_VALUE)


def test_default_path_propagates_metrics_errors() -> None:
    pytest.importorskip("trading_backtest.metrics.random_entry")
    from trading_backtest.core.errors import MetricsError

    frame = wiggle_frame(60)

    # errors raised by the metrics layer are never swallowed by the gate
    with pytest.raises(MetricsError, match="close"):
        validate_random_entry(make_result(frame), frame.drop(columns=["close"]), n_simulations=5)


def test_default_path_rejects_an_out_of_range_simulation_count() -> None:
    pytest.importorskip("trading_backtest.metrics.random_entry")
    from trading_backtest.core.errors import MetricsError

    frame = wiggle_frame(60)

    with pytest.raises(MetricsError, match="n_simulations"):
        validate_random_entry(make_result(frame), frame, n_simulations=0)


def test_gate_result_keeps_the_backtest_result_type() -> None:
    pytest.importorskip("trading_backtest.metrics.random_entry")
    from trading_backtest.metrics.random_entry import RandomEntryResult

    frame = wiggle_frame(60)
    result: BacktestResult = make_result(frame)

    gate: RandomEntryGateResult | None = validate_random_entry(result, frame, n_simulations=20)

    assert gate is not None
    assert isinstance(gate.distribution, RandomEntryResult)


def test_injected_distribution_only_needs_the_documented_attributes() -> None:
    """The seam is duck-typed: any object with the attributes + ``to_dict`` works."""
    distribution: RandomEntryResult = StubDistribution()  # type: ignore[assignment]

    gate = validate_random_entry(
        object(), make_frame((100.0, 101.0)), entry_fn=lambda *args, **kwargs: distribution
    )

    assert gate.p_value == 0.0
    assert gate.strategy_beats_random is True
