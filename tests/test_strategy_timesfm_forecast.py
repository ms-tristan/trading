"""Contract tests of the forecast-driven ``timesfm`` strategy.

Everything here is offline and dependency-free: the artifacts are built with the
pure-``numpy`` house backends (``naive`` / ``seasonal``) or with a local stub
backend, and the decision rules are exercised on hand-built prepared frames.  No
``torch``, no ``timesfm``, no network.

What this file pins, in order:

* the strategy surface (registry name, parameter grid, diagnostic vocabulary);
* ``prepare``: the exact diagnostic column set, its dtypes, purity and index
  preservation, and the "no artifact means no forecast" contract;
* every entry gate (a-j) individually, with the other gates satisfied;
* every exit rule (1-7) individually, the strict priority order on overlapping
  conditions, the ``exit_code`` values and the ``int8`` dtype;
* the "no signal at all" cases — no bundle, unknown origin, stale origin,
  decision outside the covered window — none of which may ever raise;
* the cross-cutting guarantee that an open position reaches an exit inside
  ``min(max_hold, horizon - min_lead)`` candles;
* the exact long/short mirroring of the whole decision tree;
* one end-to-end offline backtest driven by a real artifact.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from trading_platform.config import AppConfig
from trading_platform.core.constants import REQUIRED_OHLCV_COLUMNS
from trading_platform.core.errors import ForecastArtifactError, StrategyError
from trading_platform.data.synthetic import make_flat_ohlcv, make_ohlcv
from trading_platform.forecast.artifact import (
    ForecastBuildConfig,
    ForecastStore,
    build_forecast_artifact,
)
from trading_platform.forecast.types import ForecastRequest, ForecastTrajectory
from trading_platform.strategy import registry
from trading_platform.strategy.engine import run_backtest_on_config
from trading_platform.strategy.features import FeatureBundle
from trading_platform.strategy.timesfm_forecast import (
    ALPHA_LOOKAHEADS,
    DECISION_COLUMN,
    DIAGNOSTIC_COLUMNS,
    EXIT_CODES,
    FORECAST_COLUMNS,
    INDICATOR_COLUMNS,
    LOSS_EXIT_CODES,
    TimesFMForecastParams,
    TimesFMForecastStrategy,
)
from trading_platform.strategy.timesfm_forecast import _combined_codes as combined_codes
from trading_platform.strategy.timesfm_forecast import _decision_state as decision_state
from trading_platform.strategy.timesfm_forecast import _origin_positions as origin_positions

#: Horizon of every artifact used below (candles).
HORIZON = 24

#: Re-anchoring stride of every artifact used below (candles).
STRIDE = 24

#: The decision cushion of the shipped defaults.
MIN_LEAD = 2

#: Quantile levels of the local artifacts (low / median / high).
LEVELS: tuple[float, ...] = (0.1, 0.5, 0.9)

#: Columns whose value is a signed quantity, i.e. exactly negated by a mirror.
SIGNED_COLUMNS: tuple[str, ...] = (
    "forecast_slope",
    "forecast_path_eff",
    "forecast_reliability",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def build_store(
    directory: Path,
    *,
    backend: str = "naive",
    candles: pd.DataFrame | None = None,
    horizon: int = HORIZON,
    stride: int = STRIDE,
    context: int = 64,
    name: str = "artifact.parquet",
) -> ForecastStore:
    """Build a small offline artifact and return its store."""
    frame = make_ohlcv(220, seed=11) if candles is None else candles
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend=backend,
        context_length=context,
        horizon=horizon,
        reforecast_every=stride,
        quantile_levels=LEVELS,
        seasonal_period=24,
        seasonal_window=48,
    )
    path, _metadata = build_forecast_artifact(frame, config, directory / name)
    return ForecastStore.load(path)


def strategy(store: ForecastStore | None = None, **params: Any) -> TimesFMForecastStrategy:
    """Return a configured strategy, optionally carrying ``store``."""
    instance = TimesFMForecastStrategy(params or None)
    instance.set_feature_bundle(FeatureBundle(forecast=store))
    return instance


def _column(values: Any, rows: int) -> np.ndarray:
    """Return ``values`` as a ``float64`` array of ``rows`` points."""
    if np.isscalar(values):
        return np.full(rows, float(values), dtype="float64")
    array = np.asarray(values, dtype="float64").reshape(-1)
    assert array.shape[0] == rows, f"expected {rows} values, got {array.shape[0]}"
    return array


#: Gate-passing diagnostics of a hand-built prepared frame.
NEUTRAL: dict[str, Any] = {
    "rsi": 50.0,
    "atr": 1.0,
    "atr_pct": 0.01,
    "atr_percentile": 0.5,
    "realized_vol": 0.01,
    "ema_fast": 101.0,
    "ema_slow": 100.0,
    "forecast_alpha_1": 0.02,
    "forecast_alpha_2": 0.02,
    "forecast_alpha_4": 0.02,
    "forecast_alpha_8": 0.02,
    DECISION_COLUMN: 0.02,
    "forecast_slope": 0.001,
    "forecast_mfe": 0.05,
    "forecast_mae": -0.01,
    "forecast_path_eff": 0.5,
    "forecast_iqr_term": 0.02,
    "forecast_iqr_per_bar": 0.004,
    "forecast_reliability": 5.0,
    "forecast_agreement": 1.0,
    "vol_ratio": 0.4,
    "forecast_age": 1.0,
    "exit_code": 0.0,
}


def prepared_frame(
    rows: int = 8,
    *,
    close: Any = 100.0,
    index: pd.DatetimeIndex | None = None,
    **overrides: Any,
) -> pd.DataFrame:
    """Return a frame that satisfies the prepared-frame contract of the strategy.

    Every diagnostic column is gate-passing by default, so a single override
    isolates the rule under test.  ``close`` drives the realised move of the exit
    rules through ``log(close[t] / close[origin_row])`` with
    ``origin_row = t - forecast_age``.
    """
    stamps = (
        pd.date_range("2024-01-01", periods=rows, freq="1h", tz="UTC") if index is None else index
    )
    prices = _column(close, rows)
    frame = pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.full(rows, 10.0, dtype="float64"),
        },
        index=stamps,
    )
    values = dict(NEUTRAL)
    values.update(overrides)
    for column in DIAGNOSTIC_COLUMNS:
        if column in values:
            frame[column] = _column(values[column], rows)
    frame["atr"] = _column(values["atr"], rows)
    frame["stop_loss"] = prices - 2.0 * frame["atr"].to_numpy(dtype="float64")
    return frame


def decision_codes(
    frame: pd.DataFrame,
    params: TimesFMForecastParams,
    *,
    horizon: int = HORIZON,
) -> np.ndarray:
    """Return the ``exit_code`` column the strategy would compute for ``frame``.

    This calls the private decision helper on purpose: ``signals`` exposes the
    decision tree only through its boolean columns, while the auditable code
    lives in the *prepared* frame — which is exactly what is pinned here.
    """
    return combined_codes(decision_state_state(frame, params, horizon=horizon))


def decision_state_state(
    frame: pd.DataFrame,
    params: TimesFMForecastParams,
    *,
    horizon: int = HORIZON,
) -> Any:
    """Return the private per-row decision state of ``frame``."""
    columns = {name: frame[name].to_numpy(dtype="float64") for name in DIAGNOSTIC_COLUMNS}
    close = frame["close"].to_numpy(dtype="float64")
    return decision_state(columns, close, params, horizon=horizon)


def mirror(frame: pd.DataFrame, origin_close: float) -> pd.DataFrame:
    """Return the exact mirror of a prepared frame.

    Prices are reflected around the origin close in log space
    (``close' = origin_close ** 2 / close``), so a realised move changes sign, and
    every signed diagnostic is negated (``mfe`` / ``mae`` are swapped and
    negated).  Both trees must therefore agree: the short tree of the mirror is
    the long tree of the original.
    """
    mirrored = frame.copy(deep=True)
    prices = frame["close"].to_numpy(dtype="float64")
    prices = origin_close * origin_close / prices
    for column in REQUIRED_OHLCV_COLUMNS:
        if column != "volume":
            mirrored[column] = prices
    mirrored["close"] = prices
    mirrored["stop_loss"] = prices - 2.0 * frame["atr"].to_numpy(dtype="float64")
    for column in SIGNED_COLUMNS:
        mirrored[column] = -frame[column].to_numpy(dtype="float64")
    for column in (DECISION_COLUMN, *(f"forecast_alpha_{k}" for k in ALPHA_LOOKAHEADS)):
        mirrored[column] = -frame[column].to_numpy(dtype="float64")
    mirrored["forecast_mfe"] = -frame["forecast_mae"].to_numpy(dtype="float64")
    mirrored["forecast_mae"] = -frame["forecast_mfe"].to_numpy(dtype="float64")
    return mirrored


@pytest.fixture(scope="module")
def horizon_store(tmp_path_factory: pytest.TempPathFactory) -> ForecastStore:
    """A tiny ``naive`` artifact, used as the horizon carrier of hand-built frames."""
    return build_store(tmp_path_factory.mktemp("horizon"), name="horizon.parquet")


@pytest.fixture(scope="module")
def market() -> pd.DataFrame:
    """A deterministic 600-candle market used by the end-to-end tests."""
    return make_ohlcv(600, seed=5)


@pytest.fixture(scope="module")
def seasonal_store(tmp_path_factory: pytest.TempPathFactory, market: pd.DataFrame) -> ForecastStore:
    """A ``seasonal`` artifact with a directional (non-flat) median path."""
    return build_store(
        tmp_path_factory.mktemp("seasonal"),
        backend="seasonal",
        candles=market,
        name="seasonal.parquet",
    )


@pytest.fixture(scope="module")
def naive_store(tmp_path_factory: pytest.TempPathFactory, market: pd.DataFrame) -> ForecastStore:
    """A ``naive`` artifact: the honest random-walk baseline (flat median)."""
    return build_store(
        tmp_path_factory.mktemp("naive"),
        backend="naive",
        candles=market,
        name="naive.parquet",
    )


# ---------------------------------------------------------------------------
# surface
# ---------------------------------------------------------------------------


def test_the_strategy_is_registered_under_its_name() -> None:
    assert registry.get_strategy("timesfm").name == "timesfm"
    assert registry.STRATEGIES["timesfm"] is TimesFMForecastStrategy
    assert TimesFMForecastStrategy.ParamsModel is TimesFMForecastParams


def test_the_diagnostic_vocabulary_is_the_documented_one() -> None:
    assert INDICATOR_COLUMNS == (
        "rsi",
        "atr",
        "atr_pct",
        "atr_percentile",
        "realized_vol",
        "ema_fast",
        "ema_slow",
    )
    assert DIAGNOSTIC_COLUMNS == INDICATOR_COLUMNS + FORECAST_COLUMNS
    assert DECISION_COLUMN in FORECAST_COLUMNS
    for lookahead in ALPHA_LOOKAHEADS:
        assert f"forecast_alpha_{lookahead}" in FORECAST_COLUMNS
    assert len(set(DIAGNOSTIC_COLUMNS)) == len(DIAGNOSTIC_COLUMNS)


def test_the_exit_codes_are_the_documented_ordered_ones() -> None:
    assert EXIT_CODES == {
        "NO_EXIT": 0,
        "FORECAST_FLIP": 1,
        "EDGE_DECAY": 2,
        "TARGET_REACHED": 3,
        "PATH_DEGRADED": 4,
        "TIME_STOP": 5,
        "VOL_REGIME": 6,
        "STALE_FORECAST": 7,
        "ATR_STOP": 8,
    }
    assert sorted(EXIT_CODES.values()) == list(range(9))
    assert frozenset({1, 2, 4, 5, 6, 7}) == LOSS_EXIT_CODES
    assert EXIT_CODES["TARGET_REACHED"] not in LOSS_EXIT_CODES
    assert EXIT_CODES["ATR_STOP"] not in LOSS_EXIT_CODES


def test_the_parameter_grid_is_the_documented_one() -> None:
    assert TimesFMForecastStrategy.PARAM_SPACE == {
        "entry_lookahead": [2, 4, 8],
        "min_alpha": [0.0, 0.001, 0.002],
        "min_reliability": [0.25, 0.5, 1.0],
        "min_agreement": [0.5, 0.625, 0.75],
        "max_vol_ratio": [2.0, 3.0, 5.0],
        "atr_stop_multiplier": [1.5, 2.0, 3.0],
        "max_hold": [12, 24],
        "cooldown": [0, 4, 12],
    }
    assert registry.strategy_param_space("timesfm") == TimesFMForecastStrategy.PARAM_SPACE
    assert registry.strategy_param_space("timesfm") is not TimesFMForecastStrategy.PARAM_SPACE


def test_every_grid_point_is_a_legal_parameter_set() -> None:
    """Every combination of the shipped sweep grid validates."""
    grid = TimesFMForecastStrategy.PARAM_SPACE
    tested = 0
    for values in itertools.product(*grid.values()):
        combination = dict(zip(grid, values, strict=True))
        TimesFMForecastParams(**combination)
        tested += 1
    assert tested == 4374


def test_the_parameter_model_forbids_unknown_fields_and_is_frozen() -> None:
    with pytest.raises(ValidationError):
        TimesFMForecastParams(nope=1)  # type: ignore[call-arg]
    params = TimesFMForecastParams()
    with pytest.raises(ValidationError):
        params.horizon = 3  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides, needle",
    [
        ({"entry_lookahead": 30}, "entry_lookahead"),
        ({"horizon": 4, "min_lead": 4}, "horizon - min_lead"),
        ({"exit_alpha": 0.005}, "exit_alpha"),
        ({"max_vol_ratio": 7.0}, "exit_vol_ratio"),
        ({"rsi_min": 60.0, "rsi_max": 40.0}, "rsi_min"),
        ({"min_atr_percentile": 0.9, "max_atr_percentile": 0.5}, "min_atr_percentile"),
        ({"ema_fast_period": 30}, "ema_slow_period"),
    ],
)
def test_the_coherence_rules_reject_inconsistent_parameters(
    overrides: dict[str, Any], needle: str
) -> None:
    with pytest.raises(ValidationError, match=needle):
        TimesFMForecastParams(**overrides)


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_adds_exactly_the_documented_columns(horizon_store: ForecastStore) -> None:
    frame = make_ohlcv(120, seed=2)
    prepared = strategy(horizon_store).prepare(frame)

    assert list(prepared.columns) == [
        *REQUIRED_OHLCV_COLUMNS,
        *INDICATOR_COLUMNS,
        *FORECAST_COLUMNS,
        "stop_loss",
    ]
    for column in DIAGNOSTIC_COLUMNS:
        expected = "int8" if column == "exit_code" else "float64"
        assert str(prepared[column].dtype) == expected, column
    assert str(prepared["stop_loss"].dtype) == "float64"


def test_prepare_is_pure_and_preserves_the_index(horizon_store: ForecastStore) -> None:
    frame = make_ohlcv(120, seed=2)
    original = frame.copy(deep=True)
    instance = strategy(horizon_store)

    first = instance.prepare(frame)
    second = instance.prepare(frame)

    pd.testing.assert_frame_equal(frame, original)
    pd.testing.assert_frame_equal(first, second)
    assert first.index.equals(frame.index)
    assert first.index.name == frame.index.name
    assert first is not frame


def test_prepare_never_produces_an_infinite_value(horizon_store: ForecastStore) -> None:
    frame = make_ohlcv(120, seed=2)
    frame.loc[frame.index[10], "close"] = 0.0
    prepared = strategy(horizon_store).prepare(frame)

    for column in (*DIAGNOSTIC_COLUMNS, "stop_loss"):
        values = prepared[column].to_numpy(dtype="float64")
        assert not np.isinf(values).any(), column


def test_prepare_without_a_bundle_has_no_forecast_at_all() -> None:
    frame = make_ohlcv(120, seed=2)
    prepared = strategy().prepare(frame)

    forecast = [name for name in FORECAST_COLUMNS if name != "exit_code"]
    assert prepared[forecast].isna().all().all()
    assert (prepared["exit_code"] == 0).all()
    assert prepared["stop_loss"].notna().any()


def test_prepare_does_not_raise_on_an_unknown_origin(horizon_store: ForecastStore) -> None:
    """A frame that shares no timestamp with the artifact simply has no forecast."""
    frame = make_ohlcv(60, start="2019-01-01T00:00:00Z", seed=2)
    prepared = strategy(horizon_store).prepare(frame)

    assert prepared["forecast_age"].isna().all()
    assert (prepared["exit_code"] == 0).all()


def test_prepare_covers_only_the_artifact_window(
    tmp_path: Path, horizon_store: ForecastStore
) -> None:
    """The rows before the first origin carry no forecast; the covered window does."""
    store = build_store(tmp_path, candles=make_ohlcv(120, seed=11), name="short.parquet")
    frame = make_ohlcv(220, seed=11)
    prepared = strategy(store).prepare(frame)
    ages = prepared["forecast_age"]
    first_row = int(frame.index.get_indexer([store.origins()[0]])[0])

    assert first_row > 0
    assert ages.iloc[:first_row].isna().all()
    assert ages.iloc[first_row : first_row + 3].notna().all()
    assert ages.iloc[first_row] == 0.0
    assert horizon_store.horizon == HORIZON


def test_the_stop_loss_column_is_the_long_atr_formula(horizon_store: ForecastStore) -> None:
    frame = make_ohlcv(120, seed=2)
    prepared = strategy(horizon_store, atr_stop_multiplier=3.0).prepare(frame)

    expected = prepared["close"].to_numpy(dtype="float64") - 3.0 * prepared["atr"].to_numpy(
        dtype="float64"
    )
    np.testing.assert_allclose(
        prepared["stop_loss"].to_numpy(dtype="float64"), expected, equal_nan=True
    )
    assert prepared["stop_loss"].notna().any()


# ---------------------------------------------------------------------------
# signals -- contract
# ---------------------------------------------------------------------------


def test_signals_returns_exactly_the_signal_columns(horizon_store: ForecastStore) -> None:
    prepared = strategy(horizon_store).prepare(make_ohlcv(120, seed=2))
    signals = strategy(horizon_store).signals(prepared)

    assert list(signals.columns) == [
        "entry_long",
        "exit_long",
        "entry_short",
        "exit_short",
        "stop_loss",
    ]
    assert signals.index.equals(prepared.index)


@pytest.mark.parametrize("dropped", ["close", "rsi", "forecast_age", "stop_loss", "exit_code"])
def test_signals_rejects_a_frame_that_prepare_did_not_produce(dropped: str) -> None:
    frame = prepared_frame()
    broken = frame.drop(columns=[dropped])

    with pytest.raises(StrategyError, match="prepare"):
        strategy().signals(broken)


def test_signals_rejects_a_raw_ohlcv_frame() -> None:
    with pytest.raises(StrategyError, match="prepare"):
        strategy().signals(make_ohlcv(40, seed=2))


def test_signals_is_pure(horizon_store: ForecastStore) -> None:
    prepared = strategy(horizon_store).prepare(make_ohlcv(200, seed=4))
    original = prepared.copy(deep=True)
    instance = strategy(horizon_store)

    first = instance.signals(prepared)
    second = instance.signals(prepared)

    pd.testing.assert_frame_equal(prepared, original)
    pd.testing.assert_frame_equal(first, second)


# ---------------------------------------------------------------------------
# entry gates
# ---------------------------------------------------------------------------


def test_the_neutral_frame_produces_a_long_entry(horizon_store: ForecastStore) -> None:
    """The reference frame of the gate tests: every gate holds from row 1 on."""
    signals = strategy(horizon_store).signals(prepared_frame())

    assert not signals["entry_long"].iloc[0]
    assert signals["entry_long"].iloc[1:].all()
    assert not signals["entry_short"].any()


def test_gate_a_requires_a_fresh_forecast(horizon_store: ForecastStore) -> None:
    fresh = prepared_frame(forecast_age=[1.0, 1.0, 2.0, 1.0, 0.0, 1.0, 1.0, 1.0])
    stale = prepared_frame(forecast_age=[1.0, 1.0, 22.0, 1.0, 1.0, 1.0, 1.0, 1.0])

    fresh_signals = strategy(horizon_store).signals(fresh)
    stale_signals = strategy(horizon_store).signals(stale)

    assert fresh_signals["entry_long"].iloc[2]
    assert not stale_signals["entry_long"].iloc[2]
    # age = 0 means "the origin candle itself": never an entry.
    assert not fresh_signals["entry_long"].iloc[4]


def test_gate_a_requires_a_forecast_at_all(horizon_store: ForecastStore) -> None:
    signals = strategy(horizon_store).signals(prepared_frame(forecast_age=np.nan))

    assert not signals["entry_long"].any()
    assert not signals["exit_long"].any()


@pytest.mark.parametrize("alpha", [-0.02, 0.0, 0.0005])
def test_gate_b_requires_a_positive_alpha_above_min_alpha(
    horizon_store: ForecastStore, alpha: float
) -> None:
    signals = strategy(horizon_store).signals(prepared_frame(**{DECISION_COLUMN: alpha}))

    assert not signals["entry_long"].any()


def test_gate_b_accepts_an_alpha_at_the_minimum(horizon_store: ForecastStore) -> None:
    instance = strategy(horizon_store, min_alpha=0.02)
    signals = instance.signals(prepared_frame(**{DECISION_COLUMN: 0.02}))

    assert signals["entry_long"].iloc[1]


def test_gate_c_requires_the_edge_to_dominate_the_atr(horizon_store: ForecastStore) -> None:
    decided = prepared_frame(**{DECISION_COLUMN: 0.02})
    too_small = prepared_frame(**{DECISION_COLUMN: 0.02}, atr_pct=0.5)

    assert strategy(horizon_store).signals(decided)["entry_long"].iloc[1]
    assert not strategy(horizon_store).signals(too_small)["entry_long"].any()


def test_gate_c_is_false_on_a_missing_atr_pct(horizon_store: ForecastStore) -> None:
    signals = strategy(horizon_store).signals(prepared_frame(atr_pct=np.nan))

    assert not signals["entry_long"].any()


def test_gate_d_requires_reliability(horizon_store: ForecastStore) -> None:
    instance = strategy(horizon_store, min_reliability=0.5)

    assert instance.signals(prepared_frame(forecast_reliability=0.5))["entry_long"].iloc[1]
    assert not instance.signals(prepared_frame(forecast_reliability=0.4))["entry_long"].any()
    assert (
        not strategy(horizon_store)
        .signals(prepared_frame(forecast_reliability=np.nan))["entry_long"]
        .any()
    )


def test_gate_e_requires_decile_agreement(horizon_store: ForecastStore) -> None:
    instance = strategy(horizon_store, min_agreement=0.6)

    assert instance.signals(prepared_frame(forecast_agreement=0.6))["entry_long"].iloc[1]
    assert not instance.signals(prepared_frame(forecast_agreement=0.5))["entry_long"].any()
    assert (
        not strategy(horizon_store)
        .signals(prepared_frame(forecast_agreement=np.nan))["entry_long"]
        .any()
    )


def test_gate_f_requires_a_directional_path(horizon_store: ForecastStore) -> None:
    instance = strategy(horizon_store, min_path_efficiency=0.2)

    assert instance.signals(prepared_frame(forecast_path_eff=0.2))["entry_long"].iloc[1]
    assert not instance.signals(prepared_frame(forecast_path_eff=0.1))["entry_long"].any()
    assert (
        not strategy(horizon_store)
        .signals(prepared_frame(forecast_path_eff=np.nan))["entry_long"]
        .any()
    )


def test_gate_g_requires_a_known_volatility_regime(horizon_store: ForecastStore) -> None:
    instance = strategy(horizon_store, max_vol_ratio=3.0)

    assert instance.signals(prepared_frame(vol_ratio=3.0))["entry_long"].iloc[1]
    assert not instance.signals(prepared_frame(vol_ratio=3.1))["entry_long"].any()
    assert not strategy(horizon_store).signals(prepared_frame(vol_ratio=np.nan))["entry_long"].any()


def test_gate_h_requires_the_atr_percentile_window(horizon_store: ForecastStore) -> None:
    instance = strategy(horizon_store, min_atr_percentile=0.1, max_atr_percentile=0.95)

    assert instance.signals(prepared_frame(atr_percentile=0.1))["entry_long"].iloc[1]
    assert instance.signals(prepared_frame(atr_percentile=0.95))["entry_long"].iloc[1]
    assert not instance.signals(prepared_frame(atr_percentile=0.05))["entry_long"].any()
    assert not instance.signals(prepared_frame(atr_percentile=0.99))["entry_long"].any()
    assert (
        not strategy(horizon_store)
        .signals(prepared_frame(atr_percentile=np.nan))["entry_long"]
        .any()
    )


def test_gate_i_is_disabled_by_default(horizon_store: ForecastStore) -> None:
    """With ``rsi_min == 0`` and ``rsi_max == 100`` the RSI is not a gate at all."""
    signals = strategy(horizon_store).signals(
        prepared_frame(rsi=[10.0, 10.0, 90.0, 10.0, 10.0, 10.0, 10.0, 10.0])
    )

    assert signals["entry_long"].iloc[2]


def test_gate_i_filters_the_rsi_when_it_is_enabled(horizon_store: ForecastStore) -> None:
    instance = strategy(horizon_store, rsi_min=30.0, rsi_max=70.0)
    inside = prepared_frame(rsi=[50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0])
    outside = prepared_frame(rsi=[50.0, 50.0, 80.0, 50.0, 50.0, 50.0, 50.0, 50.0])
    missing = prepared_frame(rsi=[50.0, 50.0, np.nan, 50.0, 50.0, 50.0, 50.0, 50.0])

    assert instance.signals(inside)["entry_long"].iloc[2]
    assert not instance.signals(outside)["entry_long"].iloc[2]
    assert not instance.signals(missing)["entry_long"].iloc[2]


def test_gate_j_blocks_the_entries_that_follow_a_losing_exit(
    horizon_store: ForecastStore,
) -> None:
    frame = prepared_frame(exit_code=[0.0, 0.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    blocked = strategy(horizon_store, cooldown=4).signals(frame)
    unblocked = strategy(horizon_store, cooldown=0).signals(frame)

    assert blocked["entry_long"].to_numpy().tolist() == [
        False,
        True,
        False,
        False,
        False,
        False,
        False,
        True,
    ]
    assert unblocked["entry_long"].iloc[1:].all()


def test_gate_j_ignores_a_winning_exit(horizon_store: ForecastStore) -> None:
    frame = prepared_frame(
        exit_code=[0.0, 0.0, float(EXIT_CODES["TARGET_REACHED"]), 0.0, 0.0, 0.0, 0.0, 0.0]
    )
    signals = strategy(horizon_store, cooldown=4).signals(frame)

    assert signals["entry_long"].iloc[1:].all()


def test_the_first_row_is_never_an_entry(horizon_store: ForecastStore) -> None:
    signals = strategy(horizon_store, allow_short=True).signals(prepared_frame())

    assert not signals.iloc[0][["entry_long", "entry_short"]].any()


# ---------------------------------------------------------------------------
# exits -- one rule at a time
# ---------------------------------------------------------------------------


def test_rule_one_forecast_flip(horizon_store: ForecastStore) -> None:
    frame = prepared_frame(**{DECISION_COLUMN: -0.002})
    instance = strategy(horizon_store)

    assert instance.signals(frame)["exit_long"].iloc[1]
    assert not instance.signals(frame)["entry_long"].any()


def test_rule_one_needs_the_exit_alpha(horizon_store: ForecastStore) -> None:
    """A small negative alpha is not a flip: only the confirmed decay can fire."""
    frame = prepared_frame(**{DECISION_COLUMN: -0.0001})
    signals = strategy(horizon_store, exit_confirm=3).signals(frame)

    assert not signals["exit_long"].iloc[1]
    assert signals["exit_long"].iloc[2]


def test_rule_two_edge_decay_needs_consecutive_rows(horizon_store: ForecastStore) -> None:
    instant = prepared_frame(**{DECISION_COLUMN: 0.0001})
    confirmed = strategy(horizon_store, exit_confirm=2).signals(instant)
    patient = strategy(horizon_store, exit_confirm=3).signals(instant)

    assert confirmed["exit_long"].to_numpy().tolist() == [
        False,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
    ]
    assert patient["exit_long"].to_numpy().tolist() == [
        False,
        False,
        True,
        True,
        True,
        True,
        True,
        True,
    ]


def test_rule_two_is_reset_by_a_missing_alpha(horizon_store: ForecastStore) -> None:
    frame = prepared_frame(
        forecast_alpha=[0.0001, 0.0001, np.nan, 0.0001, 0.0001, 0.0001, 0.0001, 0.0001]
    )
    signals = strategy(horizon_store, exit_confirm=2).signals(frame)

    assert not signals["exit_long"].iloc[2]
    assert signals["exit_long"].iloc[4]


def test_rule_three_target_reached(horizon_store: ForecastStore) -> None:
    closes = [100.0, 100.0, 100.0, 105.0, 100.0, 100.0, 100.0, 100.0]
    frame = prepared_frame(
        close=closes,
        forecast_alpha=0.0005,
        forecast_mfe=0.05,
        forecast_path_eff=0.5,
        forecast_reliability=5.0,
    )
    instance = strategy(horizon_store, exit_confirm=1)

    assert instance.signals(frame)["exit_long"].iloc[3]
    assert not instance.signals(frame)["exit_long"].iloc[2]


def test_rule_three_needs_a_predicted_excursion(horizon_store: ForecastStore) -> None:
    closes = [100.0, 100.0, 100.0, 105.0, 100.0, 100.0, 100.0, 100.0]
    frame = prepared_frame(
        close=closes,
        forecast_alpha=0.0005,
        forecast_mfe=0.0,
        forecast_mae=0.0,
        exit_confirm=1,
    )
    instance = strategy(horizon_store, exit_confirm=1)

    assert not instance.signals(frame)["exit_long"].any()


def test_rule_four_path_degraded(horizon_store: ForecastStore) -> None:
    degraded = prepared_frame(forecast_path_eff=0.0, forecast_reliability=5.0)
    unreliable = prepared_frame(forecast_path_eff=0.5, forecast_reliability=0.0)
    healthy = prepared_frame(forecast_path_eff=0.5, forecast_reliability=5.0)
    instance = strategy(horizon_store)

    assert instance.signals(degraded)["exit_long"].iloc[1]
    assert instance.signals(unreliable)["exit_long"].iloc[1]
    assert not instance.signals(healthy)["exit_long"].any()


def test_rule_five_time_stop(horizon_store: ForecastStore) -> None:
    ages = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 6.0, 1.0]
    frame = prepared_frame(forecast_age=ages)
    instance = strategy(horizon_store, max_hold=5)

    assert instance.signals(frame)["exit_long"].iloc[6]
    assert not instance.signals(frame)["exit_long"].iloc[1]


def test_rule_five_spares_a_position_that_is_progressing(
    horizon_store: ForecastStore,
) -> None:
    ages = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 6.0, 1.0]
    progressing = prepared_frame(close=[100.0] * 6 + [102.0, 100.0], forecast_age=ages)
    flat = prepared_frame(forecast_age=ages)
    instance = strategy(horizon_store, max_hold=5)

    # Row 6 is 2 % up on the origin close: above min_progress * forecast_mfe.
    assert not instance.signals(progressing)["exit_long"].iloc[6]
    assert instance.signals(flat)["exit_long"].iloc[6]


def test_rule_six_vol_regime(horizon_store: ForecastStore) -> None:
    spike = prepared_frame(vol_ratio=[0.4, 0.4, 9.0, 0.4, 0.4, 0.4, 0.4, 0.4])
    instance = strategy(horizon_store, max_vol_ratio=3.0, exit_vol_ratio=6.0)

    assert instance.signals(spike)["exit_long"].iloc[2]
    assert not instance.signals(spike)["exit_long"].iloc[1]


def test_rule_seven_stale_forecast(horizon_store: ForecastStore) -> None:
    frame = prepared_frame(forecast_age=[1.0, 1.0, 1.0, np.nan, 1.0, 1.0, 1.0, 1.0])
    signals = strategy(horizon_store).signals(frame)

    assert signals["exit_long"].iloc[3]
    assert not signals["exit_long"].iloc[2]


def test_rule_seven_fires_on_an_expired_forecast(horizon_store: ForecastStore) -> None:
    frame = prepared_frame(forecast_age=[1.0, 1.0, 4.0, 1.0, 1.0, 1.0, 1.0, 1.0])
    instance = strategy(horizon_store, forecast_age=3)

    assert instance.signals(frame)["exit_long"].iloc[2]
    assert not instance.signals(frame)["exit_long"].iloc[1]


# ---------------------------------------------------------------------------
# exits -- priority order and codes
# ---------------------------------------------------------------------------


def ordering_frame() -> pd.DataFrame:
    """One row per exit rule, with every higher-priority condition also true.

    Row 1 flips, row 2 decays, row 3 reaches its target, row 4 degrades, row 5
    times out, row 6 hits the volatility cap and row 7 loses its forecast — while
    rows 1-6 *all* carry the conditions of the lower-priority rules.
    """
    return prepared_frame(
        close=[100.0, 100.0, 100.0, 105.0, 105.0, 100.0, 100.0, 100.0],
        forecast_age=[1.0, 1.0, 1.0, 1.0, 1.0, 5.0, 1.0, np.nan],
        forecast_alpha=[0.02, -0.002, 0.0001, 0.0005, 0.002, 0.002, 0.002, 0.002],
        forecast_path_eff=[0.5, 0.0, 0.0, 0.0, 0.0, 0.5, 0.5, 0.5],
        forecast_reliability=[5.0, 0.0, 0.0, 0.0, 0.0, 5.0, 5.0, 5.0],
        vol_ratio=[0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 9.0, 0.4],
        forecast_mfe=0.05,
        forecast_mae=-0.01,
    )


def test_the_exit_codes_follow_their_priority_order() -> None:
    params = TimesFMForecastParams(exit_confirm=1, max_hold=4)

    codes = decision_codes(ordering_frame(), params)

    assert codes.dtype == np.int8
    assert codes.tolist() == [0, 1, 2, 3, 4, 5, 6, 7]


def test_the_exit_codes_are_zero_where_no_rule_fires() -> None:
    params = TimesFMForecastParams()

    codes = decision_codes(prepared_frame(), params)

    assert codes.tolist() == [0] * 8


def test_the_lower_priority_rules_still_fire_alone() -> None:
    """Removing a higher-priority condition promotes the next rule, not the same one."""
    params = TimesFMForecastParams(exit_confirm=1, max_hold=4)
    frame = ordering_frame()
    frame["forecast_path_eff"] = [0.5] * 8

    codes = decision_codes(frame, params)

    # Row 4 still degrades, through the *reliability* floor rather than the path.
    assert codes[4] == EXIT_CODES["PATH_DEGRADED"]
    assert codes[5] == EXIT_CODES["TIME_STOP"]
    assert codes[6] == EXIT_CODES["VOL_REGIME"]


def test_the_atr_stop_code_is_reserved_and_never_written(
    horizon_store: ForecastStore, seasonal_store: ForecastStore, market: pd.DataFrame
) -> None:
    for store in (horizon_store, seasonal_store):
        prepared = strategy(store).prepare(market)
        codes = set(prepared["exit_code"].unique().tolist())
        assert codes <= set(EXIT_CODES.values())
        assert EXIT_CODES["ATR_STOP"] not in codes


def test_the_prepared_exit_code_agrees_with_the_signal_columns(
    seasonal_store: ForecastStore,
) -> None:
    instance = strategy(seasonal_store)
    prepared = instance.prepare(make_ohlcv(600, seed=5))
    signals = instance.signals(prepared)

    fired = (signals["exit_long"] | signals["exit_short"]).to_numpy()
    np.testing.assert_array_equal(fired, (prepared["exit_code"] != 0).to_numpy())
    assert fired.any(), "the seasonal artifact must exercise at least one exit"


def test_a_sliced_frame_maps_the_same_origins(
    seasonal_store: ForecastStore, market: pd.DataFrame
) -> None:
    """A walk-forward window sees the same forecast values as the full frame.

    The origin mapping is positional over the frame it is given, so slicing the
    input -- exactly what ``walk-forward`` does -- changes nothing but the rows
    that are present.  A row that precedes the first origin *of the slice* has no
    forecast at all: an origin outside the frame is never used, so a window can
    never inherit a prediction that was anchored before it started.
    """
    instance = strategy(seasonal_store)
    full = instance.prepare(market)
    window = market.iloc[200:320]
    sliced = instance.prepare(window)

    assert sliced.index.equals(window.index)
    inside = window.index[window.index.isin(seasonal_store.origins())]
    assert len(inside) > 0
    start = int(window.index.get_loc(inside[0]))
    assert start > 0, "the fixture must start inside an origin window"
    assert sliced["forecast_age"].iloc[:start].isna().all()
    assert full["forecast_age"].iloc[start:].notna().any()
    for column in (DECISION_COLUMN, "forecast_age", "forecast_path_eff", "forecast_reliability"):
        np.testing.assert_allclose(
            sliced[column].to_numpy(dtype="float64")[start:],
            full.loc[window.index[start:], column].to_numpy(dtype="float64"),
            equal_nan=True,
        )


# ---------------------------------------------------------------------------
# long / short mirroring
# ---------------------------------------------------------------------------


def test_the_short_entries_are_the_exact_mirror_of_the_long_ones(
    horizon_store: ForecastStore,
) -> None:
    frame = prepared_frame(
        close=[100.0, 100.0, 100.0, 105.0, 100.0, 100.0, 100.0, 100.0],
        **{DECISION_COLUMN: 0.02},
    )
    reflected = mirror(frame, origin_close=100.0)
    instance = strategy(horizon_store, allow_short=True, cooldown=0)

    long_signals = instance.signals(frame)
    short_signals = instance.signals(reflected)

    np.testing.assert_array_equal(
        short_signals["entry_long"].to_numpy(), long_signals["entry_short"].to_numpy()
    )
    np.testing.assert_array_equal(
        short_signals["entry_short"].to_numpy(), long_signals["entry_long"].to_numpy()
    )


def test_the_short_exit_tree_is_the_exact_mirror_of_the_long_one(
    horizon_store: ForecastStore,
) -> None:
    frame = ordering_frame()
    reflected = mirror(frame, origin_close=100.0)
    instance = strategy(horizon_store, allow_short=True, exit_confirm=1, max_hold=4)

    long_signals = instance.signals(frame)
    short_signals = instance.signals(reflected)

    assert long_signals["exit_long"].any()
    np.testing.assert_array_equal(
        short_signals["exit_short"].to_numpy(), long_signals["exit_long"].to_numpy()
    )
    np.testing.assert_array_equal(
        short_signals["exit_long"].to_numpy(), long_signals["exit_short"].to_numpy()
    )


def test_the_short_tree_produces_no_signal_unless_allowed(horizon_store: ForecastStore) -> None:
    reflected = mirror(ordering_frame(), origin_close=100.0)
    signals = strategy(horizon_store, exit_confirm=1, max_hold=4).signals(reflected)

    assert not signals["entry_short"].any()
    assert not signals["exit_short"].any()


def test_the_short_codes_are_the_mirror_of_the_long_codes() -> None:
    params = TimesFMForecastParams(exit_confirm=1, max_hold=4, allow_short=True)
    frame = ordering_frame()
    reflected = mirror(frame, origin_close=100.0)

    original = decision_state_state(frame, params)
    mirrored = decision_state_state(reflected, params)

    assert original.long_codes.tolist() == [0, 1, 2, 3, 4, 5, 6, 7]
    np.testing.assert_array_equal(mirrored.short_codes, original.long_codes)
    np.testing.assert_array_equal(mirrored.long_codes, original.short_codes)


# ---------------------------------------------------------------------------
# no signal at all
# ---------------------------------------------------------------------------


def test_no_bundle_produces_no_signal() -> None:
    instance = strategy()
    prepared = instance.prepare(make_ohlcv(200, seed=7))
    signals = instance.signals(prepared)

    assert not signals[["entry_long", "exit_long", "entry_short", "exit_short"]].any().any()


def test_a_frame_outside_the_artifact_window_produces_no_signal(
    horizon_store: ForecastStore,
) -> None:
    instance = strategy(horizon_store)
    frame = make_ohlcv(80, start="2030-01-01T00:00:00Z", seed=7)
    prepared = instance.prepare(frame)
    signals = instance.signals(prepared)

    assert not signals[["entry_long", "exit_long"]].any().any()


def test_an_unknown_origin_produces_no_signal(horizon_store: ForecastStore) -> None:
    """Timestamps that never match an artifact origin: no forecast, never an error."""
    frame = make_ohlcv(120, seed=2)
    shifted = frame.copy(deep=True)
    shifted.index = shifted.index + pd.Timedelta(minutes=30)
    instance = strategy(horizon_store)

    prepared = instance.prepare(shifted)
    signals = instance.signals(prepared)

    assert prepared["forecast_age"].isna().all()
    assert not signals[["entry_long", "exit_long"]].any().any()


def test_a_stale_origin_produces_no_entry(horizon_store: ForecastStore) -> None:
    frame = make_ohlcv(220, seed=11)
    instance = strategy(horizon_store, forecast_age=0)
    prepared = instance.prepare(frame)
    signals = instance.signals(prepared)

    assert not signals["entry_long"].any()
    assert signals["exit_long"].any(), "the stale rule must still fire"


def test_the_uncovered_tail_produces_no_signal(tmp_path: Path) -> None:
    store = build_store(tmp_path, candles=make_ohlcv(120, seed=11), name="short.parquet")
    frame = make_ohlcv(220, seed=11)
    instance = strategy(store)
    prepared = instance.prepare(frame)
    signals = instance.signals(prepared)

    last_origin_row = int(frame.index.get_indexer([store.origins()[-1]])[0])
    first_uncovered = last_origin_row + store.horizon - MIN_LEAD
    assert first_uncovered < len(frame), "the fixture must have an uncovered tail"
    assert not signals["entry_long"].iloc[first_uncovered:].any()
    tail = prepared["forecast_age"].iloc[first_uncovered:]
    assert tail.gt(float(HORIZON - 1 - MIN_LEAD)).all()


def test_signals_never_raise_on_a_frame_without_forecast_columns() -> None:
    """A prepared frame from a bundle-less instance is still a valid signal frame."""
    instance = strategy()
    prepared = instance.prepare(make_ohlcv(60, seed=1))

    signals = instance.signals(prepared)

    assert signals["stop_loss"].notna().any()
    assert not signals["entry_long"].any()


# ---------------------------------------------------------------------------
# incomplete artifacts and defensive inputs: NaN, never an exception
# ---------------------------------------------------------------------------


def test_a_median_only_artifact_reports_nan_dispersion_columns(tmp_path: Path) -> None:
    """The TimesFM default stores deciles, so the dispersion columns are optional.

    An artifact that stores **only** the median is legal; the strategy then
    reports ``NaN`` for every dispersion-derived diagnostic (never an exception,
    never an invented value) and simply has no entry to offer.
    """
    candles = make_ohlcv(220, seed=11)
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend="naive",
        context_length=64,
        horizon=HORIZON,
        reforecast_every=STRIDE,
        quantile_levels=(0.5,),
        seasonal_period=24,
        seasonal_window=48,
    )
    path, metadata = build_forecast_artifact(candles, config, tmp_path / "median-only.parquet")
    assert metadata.quantile_levels == (0.5,)

    instance = strategy(ForecastStore.load(path))
    prepared = instance.prepare(candles)
    covered = prepared["forecast_age"].notna()

    assert covered.any()
    for column in (
        "forecast_iqr_term",
        "forecast_iqr_per_bar",
        "forecast_reliability",
        "vol_ratio",
    ):
        assert prepared.loc[covered, column].isna().all(), column
    # the median-derived diagnostics stay defined: only the dispersion is absent
    assert prepared.loc[covered, "forecast_slope"].notna().all()
    assert not instance.signals(prepared)["entry_long"].any()


def test_a_zero_dispersion_artifact_scores_a_zero_reliability(tmp_path: Path) -> None:
    """A zero-width envelope gives ``reliability == 0.0``, never ``inf`` or ``NaN``."""
    candles = make_flat_ohlcv(200)
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend="naive",
        context_length=64,
        horizon=HORIZON,
        reforecast_every=STRIDE,
        quantile_levels=LEVELS,
        seasonal_period=24,
        seasonal_window=48,
    )
    path, _metadata = build_forecast_artifact(candles, config, tmp_path / "flat.parquet")
    prepared = strategy(ForecastStore.load(path)).prepare(candles)
    covered = prepared["forecast_age"].notna()

    assert covered.any()
    assert (prepared.loc[covered, "forecast_iqr_per_bar"] == 0.0).all()
    assert (prepared.loc[covered, "forecast_reliability"] == 0.0).all()
    assert np.isfinite(prepared.loc[covered, "forecast_reliability"].to_numpy()).all()


def test_an_empty_frame_maps_no_origin(horizon_store: ForecastStore) -> None:
    """An empty frame is not an error: it simply shares no origin with the artifact."""
    positions, origins = origin_positions(pd.DatetimeIndex([]), horizon_store)

    assert positions.shape == (0,)
    assert len(origins) == 0


def test_signals_rejects_a_non_dataframe() -> None:
    with pytest.raises(StrategyError, match="DataFrame"):
        strategy().signals([1, 2, 3])  # type: ignore[arg-type]


def test_signals_rejects_a_non_numeric_diagnostic_column(horizon_store: ForecastStore) -> None:
    frame = prepared_frame()
    frame["forecast_alpha"] = "not-a-number"

    with pytest.raises(StrategyError, match="numeric"):
        strategy(horizon_store).signals(frame)


def test_a_foreign_bundle_attribute_reads_no_forecast(horizon_store: ForecastStore) -> None:
    """The validated setter is the public door; a foreign value still never raises."""
    instance = strategy()
    instance._feature_bundle = {"forecast": horizon_store}  # type: ignore[assignment]

    prepared = instance.prepare(make_ohlcv(60, seed=1))

    assert prepared["forecast_age"].isna().all()


# ---------------------------------------------------------------------------
# the exit guarantee
# ---------------------------------------------------------------------------


def holding_spans(signals: pd.DataFrame) -> list[tuple[int, int]]:
    """Pair every long entry row with the first exit row that follows it."""
    entries = signals["entry_long"].to_numpy(dtype=bool)
    exits = signals["exit_long"].to_numpy(dtype=bool)
    spans: list[tuple[int, int]] = []
    position = 0
    while position < len(signals):
        if not entries[position]:
            position += 1
            continue
        closing = position + 1
        while closing < len(signals) and not exits[closing]:
            closing += 1
        spans.append((position, closing))
        position = closing + 1
    return spans


@pytest.mark.parametrize("max_hold", [24, 48])
def test_every_position_reaches_an_exit_inside_the_covered_window(
    seasonal_store: ForecastStore, market: pd.DataFrame, max_hold: int
) -> None:
    """An open position never survives past ``min(max_hold, horizon - min_lead)``.

    The guarantee is carried by the end of the availability window (the stale
    rule) whenever ``max_hold`` is at least ``horizon - min_lead`` — the shipped
    default — because a position can never be entered outside it.
    """
    instance = strategy(seasonal_store, max_hold=max_hold)
    signals = instance.signals(instance.prepare(market))
    limit = max(1, min(max_hold, HORIZON - MIN_LEAD))
    spans = holding_spans(signals)

    assert spans, "the fixture must trade"
    for entry_row, exit_row in spans:
        assert exit_row < len(signals), f"position opened at {entry_row} never exited"
        assert exit_row - entry_row <= limit, (entry_row, exit_row)


def test_a_capped_hold_is_soft_but_the_window_is_not(
    seasonal_store: ForecastStore, market: pd.DataFrame
) -> None:
    """``max_hold`` is skipped for a progressing trade; the covered window is not.

    The time stop spares a position that already realised ``min_progress`` of its
    predicted excursion, so the hard backstop of every position remains the end of
    the coverage window — at most ``horizon - min_lead`` candles after its origin.
    """
    instance = strategy(seasonal_store, max_hold=6)
    prepared = instance.prepare(market)
    signals = instance.signals(prepared)

    assert EXIT_CODES["TIME_STOP"] in set(prepared["exit_code"].unique().tolist())
    spans = holding_spans(signals)
    assert spans, "the fixture must trade"
    for entry_row, exit_row in spans:
        assert exit_row - entry_row <= HORIZON - MIN_LEAD, (entry_row, exit_row)


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


def test_end_to_end_backtest_with_the_seasonal_artifact(
    seasonal_store: ForecastStore, market: pd.DataFrame, tmp_path: Path
) -> None:
    """A real artifact, a real configuration, the real engine: it must trade."""
    artifact = seasonal_store.path
    assert artifact is not None
    config = AppConfig(
        strategy={"name": "timesfm"},
        forecast={"artifact": str(artifact)},
    )

    result = run_backtest_on_config(config, market)

    assert result.strategy_name == "timesfm"
    assert result.n_trades > 0
    assert len(result.equity_curve) == len(market)
    assert all(
        trade.exit_reason.value in {"signal", "stop_loss", "end_of_data"} for trade in result.trades
    )


def test_end_to_end_backtest_with_the_naive_baseline(
    naive_store: ForecastStore, market: pd.DataFrame
) -> None:
    """The random-walk baseline has no thesis: it must never open a position."""
    artifact = naive_store.path
    assert artifact is not None
    config = AppConfig(strategy={"name": "timesfm"}, forecast={"artifact": str(artifact)})

    result = run_backtest_on_config(config, market)

    assert result.strategy_name == "timesfm"
    assert result.n_trades == 0
    assert result.final_balance == result.initial_balance


def test_end_to_end_with_the_short_side(
    seasonal_store: ForecastStore, market: pd.DataFrame
) -> None:
    artifact = seasonal_store.path
    assert artifact is not None
    config = AppConfig(
        strategy={"name": "timesfm"},
        backtest={"allow_short": True},
        forecast={"artifact": str(artifact)},
    )

    result = run_backtest_on_config(config, market)

    assert result.strategy_name == "timesfm"
    assert all(trade.direction.value in {"long", "short"} for trade in result.trades)


def test_a_configured_artifact_that_cannot_be_read_is_reported(
    market: pd.DataFrame, tmp_path: Path
) -> None:
    missing = tmp_path / "nope.parquet"
    config = AppConfig(strategy={"name": "timesfm"}, forecast={"artifact": str(missing)})

    with pytest.raises(ForecastArtifactError):
        run_backtest_on_config(config, market)


def test_the_strategy_rejects_a_foreign_feature_bundle() -> None:
    instance = strategy()

    with pytest.raises(StrategyError, match="FeatureBundle"):
        instance.set_feature_bundle({"forecast": None})


def test_a_stub_backend_drives_the_full_pipeline_offline(
    tmp_path: Path, market: pd.DataFrame
) -> None:
    """A local backend proves the whole chain without any ML dependency."""

    class RecordingBackend:
        """A deterministic offline backend producing a persistent upward path."""

        name = "recording"

        def __init__(self) -> None:
            self.calls = 0

        def is_available(self) -> bool:
            return True

        def predict(
            self, requests: Sequence[ForecastRequest], *, horizon: int
        ) -> list[ForecastTrajectory]:
            self.calls += 1
            steps = np.arange(1, horizon + 1, dtype="float64")
            paths = []
            for request in requests:
                median = 0.004 * steps
                spread = 0.002 * np.sqrt(steps)
                quantiles = np.vstack([median - spread, median, median + spread])
                paths.append(
                    ForecastTrajectory(
                        origin=request.origin,
                        timeframe="1h",
                        horizon=horizon,
                        quantile_levels=LEVELS,
                        quantiles=np.asarray(quantiles, dtype="float32"),
                    )
                )
            return paths

    backend = RecordingBackend()
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend=backend.name,
        context_length=64,
        horizon=HORIZON,
        reforecast_every=STRIDE,
        quantile_levels=LEVELS,
        seasonal_period=24,
        seasonal_window=48,
    )
    path, metadata = build_forecast_artifact(
        market, config, tmp_path / "stub.parquet", backend=backend
    )
    assert metadata.backend == "recording"

    app = AppConfig(strategy={"name": "timesfm"}, forecast={"artifact": str(path)})
    prepared = strategy(ForecastStore.load(path)).prepare(market)

    assert prepared[DECISION_COLUMN].notna().any()
    assert (prepared["exit_code"] != 0).any()
    assert run_backtest_on_config(app, market).n_trades > 0


# ---------------------------------------------------------------------------
# the documented reproduction path
# ---------------------------------------------------------------------------

DOCS = Path(__file__).resolve().parents[1] / "docs" / "forecasting.md"

_PYTHON_FENCE = re.compile(r"```python\n(.*?)```", re.DOTALL)


def documented_reproduction_snippet() -> str:
    """Return the standalone reproduction snippet of ``docs/forecasting.md``.

    The page holds a second, shorter fragment inside the ``naive``-baseline note
    (it reuses the ``frame`` and the configuration built above); the standalone
    snippet is the one that builds the frame, the artifact and the configuration
    on its own, so it is the copy-pasteable one and the one executed here.
    """
    fences = _PYTHON_FENCE.findall(DOCS.read_text(encoding="utf-8"))
    matching = [
        fence for fence in fences if "build_forecast_artifact" in fence and "make_ohlcv(" in fence
    ]
    assert len(matching) == 1, (
        "docs/forecasting.md must hold exactly one standalone build_forecast_artifact snippet"
    )
    return matching[0]


def test_the_documented_offline_reproduction_snippet_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snippet a reader copies from the docs must actually execute.

    The documentation is the delivered surface: a renamed import, a reordered
    positional argument or a stale keyword in the reproduction section silently
    breaks every reader.  The snippet is therefore *executed* here, verbatim,
    from a scratch working directory -- it must build a real artifact, resolve
    the ``forecast`` configuration section and run a real backtest offline.

    The ``naive`` backend is the documented default and it deliberately opens no
    position (a random walk has no thesis, see
    :func:`test_end_to_end_backtest_with_the_naive_baseline`), so the assertion
    is on the pipeline, not on a trade count.
    """
    snippet = documented_reproduction_snippet()
    assert "trading_platform.forecast.artifact" in snippet, (
        "the documented import must name the module that actually exports the builder"
    )

    monkeypatch.chdir(tmp_path)
    namespace: dict[str, Any] = {"__name__": "__main__"}
    exec(compile(snippet, str(DOCS), "exec"), namespace)

    assert (tmp_path / "forecast_config.json").is_file()
    assert (tmp_path / "data" / "forecast" / "forecast.parquet").is_file()
    assert namespace["result"].n_trades == 0
    assert namespace["result"].final_balance == namespace["result"].initial_balance
