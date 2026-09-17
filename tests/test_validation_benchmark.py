"""Unit tests for the benchmark gate (``trading_backtest.validation.benchmark``).

The gate answers a single question -- *did the strategy beat doing nothing?* --
and it must answer it **before** the metrics package
(``trading_backtest.metrics``) exists: every verdict test injects a duck-typed
comparison through ``comparison_fn`` and never imports that layer.  Only the
integration tests at the end are guarded by ``pytest.importorskip`` and exercise
the real ``compare_benchmark``.

Nothing here is random or networked: frames are built in the test and the
expected numbers are exact closed forms.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pytest

from trading_backtest.core.constants import OHLCV_INDEX_NAME, UTC
from trading_backtest.core.errors import ValidationLayerError
from trading_backtest.validation.benchmark import (
    MIN_ALPHA,
    BenchmarkGateResult,
    validate_benchmark,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from trading_backtest.core.models import BacktestResult, RunnerFn

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 60 candles growing by +1% each: the benchmark returns ``1.01 ** 59 - 1``.
RISING_CLOSES: tuple[float, ...] = tuple(100.0 * (1.01**index) for index in range(60))
#: 60 candles shrinking by -1% each: the benchmark return is clearly negative.
FALLING_CLOSES: tuple[float, ...] = tuple(100.0 * (0.99**index) for index in range(60))

#: The exact key set of :meth:`BenchmarkGateResult.to_dict`, in order.
GATE_KEYS: tuple[str, ...] = (
    "variant",
    "strategy_beats_benchmark",
    "alpha",
    "beta",
    "correlation",
    "min_alpha",
    "n_returns",
    "initial_balance",
    "fee_rate",
    "slippage",
    "strategy_total_return",
    "benchmark_total_return",
)


# ---------------------------------------------------------------------------
# helpers: a frame, and duck-typed stand-ins for the metrics layer
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


@dataclass
class StubSide:
    """Mapping-alike side of a comparison; only ``total_return`` may be read."""

    total_return: float

    def __getitem__(self, name: str) -> float:
        """Return ``total_return``; any other metric name is a KeyError."""
        if name != "total_return":
            raise KeyError(name)
        return self.total_return


@dataclass
class StubComparison:
    """Duck-typed stand-in for ``BenchmarkComparison`` -- no metrics import."""

    alpha: float
    strategy_total_return: float
    benchmark_total_return: float
    beta: float | None = None
    correlation: float | None = None
    variant: str = "buy_and_hold"
    initial_balance: float = 10_000.0
    fee_rate: float = 0.0
    slippage: float = 0.0
    n_returns: int = 12

    @property
    def strategy(self) -> StubSide:
        """Strategy side of the comparison."""
        return StubSide(self.strategy_total_return)

    @property
    def benchmark(self) -> StubSide:
        """Benchmark side of the comparison."""
        return StubSide(self.benchmark_total_return)


class RecordingComparison:
    """Callable ``comparison_fn`` that records how it was called."""

    def __init__(self, comparison: StubComparison | None) -> None:
        self.comparison = comparison
        self.calls: list[dict[str, Any]] = []

    def __call__(self, result: object, data: object, **kwargs: Any) -> StubComparison | None:
        """Record the call and return the canned comparison."""
        self.calls.append({"result": result, "data": data, **kwargs})
        return self.comparison


def stub_gate(
    comparison: StubComparison | None, **overrides: Any
) -> tuple[BenchmarkGateResult | None, RecordingComparison]:
    """Gate ``comparison`` through an injected ``comparison_fn`` (no metrics layer)."""
    recorder = RecordingComparison(comparison)
    gate = validate_benchmark(
        object(), make_frame((100.0, 101.0, 102.0)), comparison_fn=recorder, **overrides
    )
    return gate, recorder


# ---------------------------------------------------------------------------
# module surface
# ---------------------------------------------------------------------------


def test_min_alpha_is_zero_float_and_exported() -> None:
    import trading_backtest.validation as validation

    assert MIN_ALPHA == 0.0
    assert isinstance(MIN_ALPHA, float)
    assert validation.MIN_ALPHA is MIN_ALPHA
    assert {"MIN_ALPHA", "BenchmarkGateResult", "validate_benchmark"} <= set(validation.__all__)
    assert validation.BenchmarkGateResult is BenchmarkGateResult
    assert validation.validate_benchmark is validate_benchmark


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

        assert callable(validation.validate_benchmark)
        assert validation.MIN_ALPHA == 0.0
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


# ---------------------------------------------------------------------------
# the "none" variant: no benchmark, no metrics layer at all
# ---------------------------------------------------------------------------


def test_none_variant_returns_none_without_touching_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics", None)
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics.benchmark", None)
    comparison = StubComparison(alpha=0.5, strategy_total_return=0.5, benchmark_total_return=0.0)
    recorder = RecordingComparison(comparison)

    gate = validate_benchmark(
        object(), make_frame((100.0, 110.0)), variant="none", comparison_fn=recorder
    )

    assert gate is None
    assert recorder.calls == []


def test_missing_metrics_layer_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics", None)
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics.benchmark", None)

    with pytest.raises(ValidationLayerError, match="not importable"):
        validate_benchmark(object(), make_frame((100.0, 110.0)))


# ---------------------------------------------------------------------------
# the injectable seam
# ---------------------------------------------------------------------------


def test_injected_comparison_fn_is_used_without_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics", None)
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics.benchmark", None)
    comparison = StubComparison(
        alpha=0.05,
        strategy_total_return=0.25,
        benchmark_total_return=0.20,
        beta=1.2,
        correlation=0.9,
        variant="cash",
        initial_balance=5_000.0,
        fee_rate=0.001,
        slippage=0.0005,
        n_returns=42,
    )
    frame = make_frame((100.0, 101.0, 102.0))
    result = object()
    recorder = RecordingComparison(comparison)

    gate = validate_benchmark(result, frame, comparison_fn=recorder)

    assert gate is not None
    assert gate.comparison is comparison
    assert gate.variant == "cash"
    assert gate.alpha == 0.05
    assert gate.beta == 1.2
    assert gate.correlation == 0.9
    assert gate.min_alpha == MIN_ALPHA
    assert gate.initial_balance == 5_000.0
    assert gate.fee_rate == 0.001
    assert gate.slippage == 0.0005
    assert gate.n_returns == 42
    assert gate.strategy_total_return == 0.25
    assert gate.benchmark_total_return == 0.20
    assert gate.strategy_beats_benchmark is True
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["result"] is result
    assert recorder.calls[0]["data"] is frame
    assert recorder.calls[0]["variant"] == "buy_and_hold"
    assert recorder.calls[0]["fee_rate"] == 0.0
    assert recorder.calls[0]["slippage"] == 0.0
    assert recorder.calls[0]["risk_free_rate"] == 0.0


def test_fee_rate_and_slippage_are_passed_through() -> None:
    comparison = StubComparison(
        alpha=0.01,
        strategy_total_return=0.10,
        benchmark_total_return=0.09,
        fee_rate=0.002,
        slippage=0.001,
    )

    gate, recorder = stub_gate(comparison, fee_rate=0.002, slippage=0.001, min_alpha=0.005)

    assert gate is not None
    assert recorder.calls[0]["fee_rate"] == 0.002
    assert recorder.calls[0]["slippage"] == 0.001
    assert gate.fee_rate == 0.002
    assert gate.slippage == 0.001
    assert gate.min_alpha == 0.005


def test_risk_free_rate_is_passed_through() -> None:
    """The annual risk-free rate reaches the comparison, and defaults to ``0.0``."""
    comparison = StubComparison(alpha=0.01, strategy_total_return=0.10, benchmark_total_return=0.09)

    default_gate, default_recorder = stub_gate(comparison)
    flagged_gate, flagged_recorder = stub_gate(comparison, risk_free_rate=0.05)

    assert default_gate is not None
    assert flagged_gate is not None
    assert default_recorder.calls[0]["risk_free_rate"] == 0.0
    assert flagged_recorder.calls[0]["risk_free_rate"] == 0.05
    # the rate never leaks into the frozen payload of the gate
    assert tuple(default_gate.to_dict()) == GATE_KEYS
    assert tuple(flagged_gate.to_dict()) == GATE_KEYS


def test_none_variant_needs_no_comparison_even_with_a_risk_free_rate() -> None:
    recorder = RecordingComparison(None)

    gate = validate_benchmark(
        object(),
        make_frame((100.0, 110.0)),
        variant="none",
        risk_free_rate=0.05,
        comparison_fn=recorder,
    )

    assert gate is None
    assert recorder.calls == []


def test_none_comparison_returns_none() -> None:
    recorder = RecordingComparison(None)

    gate = validate_benchmark(object(), make_frame((100.0, 101.0)), comparison_fn=recorder)

    assert gate is None
    assert len(recorder.calls) == 1


# ---------------------------------------------------------------------------
# the verdict: strict inequality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("alpha", "min_alpha", "expected"),
    [
        (0.02, MIN_ALPHA, True),  # any strictly positive margin wins
        (0.0, MIN_ALPHA, False),  # a tie is NOT a victory
        (0.0, 0.05, False),
        (0.03, 0.05, False),
        (0.05, 0.05, False),  # equality at a non-zero threshold is not a victory
        (0.06, 0.05, True),
        (-0.10, MIN_ALPHA, False),  # the strategy destroyed value
    ],
)
def test_gate_verdict_is_a_strict_inequality(
    alpha: float, min_alpha: float, expected: bool
) -> None:
    comparison = StubComparison(
        alpha=alpha, strategy_total_return=alpha, benchmark_total_return=0.0
    )

    gate, _ = stub_gate(comparison, min_alpha=min_alpha)

    assert gate is not None
    assert gate.strategy_beats_benchmark is expected
    assert gate.strategy_beats_benchmark is bool(alpha > min_alpha)


def test_negative_alpha_is_kept_as_is() -> None:
    comparison = StubComparison(
        alpha=-0.31, strategy_total_return=-0.10, benchmark_total_return=0.21
    )

    gate, _ = stub_gate(comparison)

    assert gate is not None
    assert gate.alpha == -0.31
    assert gate.strategy_total_return == -0.10
    assert gate.benchmark_total_return == 0.21
    assert gate.strategy_beats_benchmark is False


# ---------------------------------------------------------------------------
# optional beta / correlation
# ---------------------------------------------------------------------------


def test_beta_and_correlation_propagate_none() -> None:
    comparison = StubComparison(
        alpha=0.02,
        strategy_total_return=0.02,
        benchmark_total_return=0.0,
        beta=None,
        correlation=None,
    )

    gate, _ = stub_gate(comparison)

    assert gate is not None
    assert gate.beta is None
    assert gate.correlation is None
    assert gate.to_dict()["beta"] is None
    assert gate.to_dict()["correlation"] is None


def test_non_finite_beta_and_correlation_become_none() -> None:
    comparison = StubComparison(
        alpha=0.02,
        strategy_total_return=0.02,
        benchmark_total_return=0.0,
        beta=float("nan"),
        correlation=float("inf"),
    )

    gate, _ = stub_gate(comparison)

    assert gate is not None
    assert gate.beta is None
    assert gate.correlation is None
    assert json.loads(json.dumps(gate.to_dict()))["beta"] is None


# ---------------------------------------------------------------------------
# serialisation and immutability
# ---------------------------------------------------------------------------


def test_to_dict_has_exactly_the_documented_keys() -> None:
    comparison = StubComparison(
        alpha=0.07,
        strategy_total_return=0.31,
        benchmark_total_return=0.24,
        beta=0.8,
        correlation=0.65,
        variant="buy_and_hold",
        initial_balance=10_000.0,
        fee_rate=0.001,
        slippage=0.0002,
        n_returns=59,
    )

    gate, _ = stub_gate(comparison, min_alpha=0.05)

    assert gate is not None
    payload = gate.to_dict()
    assert tuple(payload) == GATE_KEYS
    assert len(payload) == 12
    assert payload == {
        "variant": "buy_and_hold",
        "strategy_beats_benchmark": True,
        "alpha": 0.07,
        "beta": 0.8,
        "correlation": 0.65,
        "min_alpha": 0.05,
        "n_returns": 59,
        "initial_balance": 10_000.0,
        "fee_rate": 0.001,
        "slippage": 0.0002,
        "strategy_total_return": 0.31,
        "benchmark_total_return": 0.24,
    }
    assert isinstance(payload["n_returns"], int)
    assert isinstance(payload["strategy_beats_benchmark"], bool)
    assert all(isinstance(value, (str, bool, int, float)) for value in payload.values())
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload


def test_to_dict_is_json_native_when_beta_is_none() -> None:
    comparison = StubComparison(alpha=0.0, strategy_total_return=0.0, benchmark_total_return=0.0)

    gate, _ = stub_gate(comparison)

    assert gate is not None
    dumped = json.dumps(gate.to_dict(), sort_keys=True)
    assert '"beta": null' in dumped
    assert '"correlation": null' in dumped
    assert '"strategy_beats_benchmark": false' in dumped


def test_gate_result_is_frozen() -> None:
    gate, _ = stub_gate(
        StubComparison(alpha=0.1, strategy_total_return=0.1, benchmark_total_return=0.0)
    )

    assert gate is not None
    with pytest.raises(FrozenInstanceError):
        gate.alpha = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# integration with the real metrics layer (guarded: optional package)
# ---------------------------------------------------------------------------


def test_integration_gate_alpha_matches_the_sides(runner_stub: RunnerFn) -> None:
    pytest.importorskip("trading_backtest.metrics")
    frame = make_frame(RISING_CLOSES)

    gate = validate_benchmark(runner_stub(frame), frame)

    assert gate is not None
    assert gate.variant == "buy_and_hold"
    assert gate.n_returns == len(frame) - 1
    # the buy & hold return is a closed form: last close / first close - 1
    assert gate.benchmark_total_return == pytest.approx(RISING_CLOSES[-1] / RISING_CLOSES[0] - 1.0)
    assert gate.strategy_total_return == pytest.approx(0.003)
    assert gate.alpha == pytest.approx(gate.strategy_total_return - gate.benchmark_total_return)
    assert gate.alpha < 0.0
    assert gate.strategy_beats_benchmark is False
    # the full comparison stays reachable for the report writers
    rows = gate.comparison.comparison_rows()
    assert [row["variant"] for row in rows] == ["strategy", "buy_and_hold", "gap"]
    assert gate.comparison.to_dict()["alpha"] == pytest.approx(gate.alpha)


def test_integration_strategy_underperforms_a_rising_benchmark(runner_stub: RunnerFn) -> None:
    pytest.importorskip("trading_backtest.metrics")
    frame = make_frame(RISING_CLOSES)

    gate = validate_benchmark(runner_stub(frame), frame)

    assert gate is not None
    assert gate.benchmark_total_return > 0.5
    assert gate.strategy_beats_benchmark is False


def test_integration_strategy_beats_a_falling_benchmark(runner_stub: RunnerFn) -> None:
    pytest.importorskip("trading_backtest.metrics")
    frame = make_frame(FALLING_CLOSES)

    gate = validate_benchmark(runner_stub(frame), frame)

    assert gate is not None
    assert gate.benchmark_total_return < 0.0
    assert gate.alpha == pytest.approx(gate.strategy_total_return - gate.benchmark_total_return)
    assert gate.alpha > 0.0
    assert gate.strategy_beats_benchmark is True
    assert gate.beta is not None
    assert gate.correlation is not None


def test_integration_costs_are_charged_to_the_benchmark(runner_stub: RunnerFn) -> None:
    pytest.importorskip("trading_backtest.metrics")
    frame = make_frame(RISING_CLOSES)
    result = runner_stub(frame)

    free = validate_benchmark(result, frame)
    charged = validate_benchmark(result, frame, fee_rate=0.001, slippage=0.0005)

    assert free is not None
    assert charged is not None
    assert charged.fee_rate == 0.001
    assert charged.slippage == 0.0005
    assert charged.benchmark_total_return < free.benchmark_total_return
    assert charged.alpha > free.alpha


def test_integration_cash_variant_has_no_beta(runner_stub: RunnerFn) -> None:
    pytest.importorskip("trading_backtest.metrics")
    frame = make_frame(RISING_CLOSES)

    gate = validate_benchmark(runner_stub(frame), frame, variant="cash")

    assert gate is not None
    assert gate.variant == "cash"
    assert gate.benchmark_total_return == pytest.approx(0.0)
    assert gate.alpha == pytest.approx(gate.strategy_total_return)
    assert gate.strategy_beats_benchmark is True
    # a flat cash curve has no variance: beta and correlation are not computable
    assert gate.beta is None
    assert gate.correlation is None
    assert gate.to_dict()["beta"] is None


def test_integration_two_candle_frame_has_no_beta(runner_stub: RunnerFn) -> None:
    pytest.importorskip("trading_backtest.metrics")
    frame = make_frame((100.0, 110.0))

    gate = validate_benchmark(runner_stub(frame), frame)

    assert gate is not None
    assert gate.n_returns == 1
    assert gate.beta is None
    assert gate.correlation is None


def test_integration_none_variant_needs_no_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("trading_backtest.metrics")
    monkeypatch.setitem(sys.modules, "trading_backtest.metrics.benchmark", None)
    frame = make_frame(RISING_CLOSES)

    assert validate_benchmark(object(), frame, variant="none") is None


def test_integration_propagates_metrics_errors(runner_stub: RunnerFn) -> None:
    pytest.importorskip("trading_backtest.metrics")
    from trading_backtest.core.errors import MetricsError

    frame = make_frame(RISING_CLOSES)

    # errors raised by the metrics layer are never swallowed by the gate
    with pytest.raises(MetricsError, match="close"):
        validate_benchmark(runner_stub(frame), frame.drop(columns=["close"]))


def test_gate_result_keeps_the_backtest_result_type(runner_stub: RunnerFn) -> None:
    pytest.importorskip("trading_backtest.metrics")
    frame = make_frame(RISING_CLOSES)
    result: BacktestResult = runner_stub(frame)

    gate = validate_benchmark(result, frame)

    assert gate is not None
    assert gate.comparison.strategy["total_return"] == pytest.approx(gate.strategy_total_return)
