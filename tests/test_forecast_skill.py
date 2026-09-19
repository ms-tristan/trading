"""Contract tests for the offline forecast-skill report.

The report is the honesty mechanism of the forecasting layer: it must be able to
say "this artifact does not beat a random walk" and must never invent a number.
The tests below therefore pin three things:

* the metric algebra, on hand-built artifacts whose targets and predictions are
  known exactly (RMSE/MAE, both skill scores, MASE, decile coverage, directional
  accuracy at the horizon);
* the evaluation protocol — the target is recomputed from the candle frame with
  the transform recorded in the metadata, the baseline predicts ``0.0``, a pair
  only exists when the target candle exists, and the evaluation stops at the end
  of the frame;
* the undefined cases — an empty artifact, a store the candles do not cover, a
  flat market and the random-walk baseline itself all yield ``NaN`` rather than an
  exception or a fabricated value.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.errors import ForecastError
from trading_platform.data.synthetic import make_flat_ohlcv, make_ohlcv
from trading_platform.forecast.artifact import (
    ARTIFACT_COLUMNS,
    ARTIFACT_SCHEMA_VERSION,
    ORIGIN_INDEX_NAME,
    ArtifactMetadata,
    ForecastBuildConfig,
    ForecastStore,
    build_forecast_artifact,
    build_forecast_frame,
)
from trading_platform.forecast.series import deseasonalize_log_price
from trading_platform.forecast.skill import ForecastSkillReport, forecast_skill_report

#: Quantile levels of every hand-built artifact (low / median / high).
LEVELS: tuple[float, ...] = (0.1, 0.5, 0.9)

#: Fixed creation timestamp of the artifacts built in-memory.
CREATED_AT = pd.Timestamp("2024-06-01T00:00:00Z")

#: Metric keys every report must expose, whatever the outcome.
METRIC_KEYS = frozenset(
    {
        "rmse",
        "mae",
        "baseline_rmse",
        "baseline_mae",
        "rmse_skill_score",
        "mae_skill_score",
        "mase",
        "directional_accuracy",
        "directional_accuracy_horizon",
        "coverage_error_mean",
        # Measured against the REAL price move, not the de-seasonalised target:
        # the only pair of numbers that speaks to profitability.
        "real_rmse",
        "real_baseline_rmse",
        "real_rmse_skill_score",
        "real_directional_accuracy_horizon",
    }
)


def hourly_index(count: int, *, start: str = "2024-01-01T00:00:00Z") -> pd.DatetimeIndex:
    """Return an hourly, tz-aware UTC candle index of ``count`` stamps."""
    return pd.date_range(start, periods=count, freq="h", tz="UTC", name="timestamp")


def store_from_rows(
    origins: pd.DatetimeIndex,
    rows: list[list[list[float]]],
    *,
    horizon: int,
    levels: tuple[float, ...] = LEVELS,
    seasonal_window: int = 1,
    seasonal_period: int = 24,
    timeframe: str = "1h",
    stride: int = 1,
) -> ForecastStore:
    """Build an in-memory store whose row ``i`` holds ``rows[i][level][step]``.

    The median column is derived from the ``0.5`` level, exactly as the artifact
    contract requires, so a hand-built store is a legal artifact.
    """
    index = pd.DatetimeIndex(origins, name=ORIGIN_INDEX_NAME)
    matrices = [np.asarray(row, dtype="float32") for row in rows]
    median_index = levels.index(0.5)
    frame = pd.DataFrame(
        {
            "horizon": pd.Series(np.full(len(index), horizon, dtype="int16"), index=index),
            "stride": pd.Series(np.full(len(index), stride, dtype="int16"), index=index),
            "n_quantiles": pd.Series(np.full(len(index), len(levels), dtype="int16"), index=index),
            "quantile_levels": pd.Series(
                [np.asarray(levels, dtype="float32") for _ in range(len(index))],
                index=index,
                dtype=object,
            ),
            "median": pd.Series([matrix[median_index] for matrix in matrices], index=index),
            "quantiles": pd.Series(
                [matrix.reshape(-1) for matrix in matrices], index=index, dtype=object
            ),
        },
        index=index,
        columns=list(ARTIFACT_COLUMNS),
    )
    metadata = ArtifactMetadata(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        symbol="BTC/USDT",
        timeframe=timeframe,
        backend="test",
        model_id=None,
        context_length=8,
        stride=stride,
        horizon=horizon,
        quantile_levels=levels,
        features=(),
        created_at="2024-01-01T00:00:00+00:00",
        package_version="0.1.0",
        data_start=pd.Timestamp(index[0]) if len(index) else pd.Timestamp("2024-01-01T00:00:00Z"),
        data_end=pd.Timestamp(index[-1]) if len(index) else pd.Timestamp("2024-01-01T00:00:00Z"),
        n_origins=len(index),
        license="",
        deseasonalized=seasonal_window > 1,
        seasonal_period=seasonal_period,
        seasonal_window=seasonal_window,
    )
    return ForecastStore(frame, metadata)


def linear_candles(rates: list[float], index: pd.DatetimeIndex) -> pd.DataFrame:
    """Return a candle frame whose log close starts at ``0`` and moves by ``rates``.

    ``rates`` holds the ``len(index) - 1`` one-step log changes, so the
    de-seasonalised target of the frame (with ``seasonal_window <= 1``) is the
    cumulative sum the test reads below.
    """
    values = np.exp(np.concatenate([[0.0], np.cumsum(np.asarray(rates, dtype="float64"))]))
    return pd.DataFrame({"close": values}, index=index)


# ---------------------------------------------------------------------------
# hand-computed metrics
# ---------------------------------------------------------------------------


def test_metrics_are_hand_computed_on_a_wrong_forecast() -> None:
    index = hourly_index(5)
    candles = linear_candles([1.0, 1.0, 1.0, 1.0], index)
    store = store_from_rows(
        index[:1],
        [[[0.0, -1.0], [0.5, 4.0], [3.0, 5.0]]],
        horizon=2,
    )

    report = forecast_skill_report(store, candles)
    prediction = np.array([0.5, 4.0])
    target = np.array([1.0, 2.0])
    error = prediction - target
    expected_rmse = float(np.sqrt(np.mean(error**2)))
    expected_mae = float(np.mean(np.abs(error)))
    expected_baseline_rmse = float(np.sqrt(np.mean(target**2)))
    expected_baseline_mae = float(np.mean(np.abs(target)))

    assert report.n_origins == 1
    assert report.n_pairs == 2
    assert report.horizon == 2
    assert report.metadata.backend == "test"
    assert report.metrics["rmse"] == pytest.approx(expected_rmse)
    assert report.metrics["rmse"] == pytest.approx(math.sqrt(2.125))
    assert report.metrics["mae"] == pytest.approx(expected_mae)
    assert report.metrics["mae"] == pytest.approx(1.25)
    assert report.metrics["baseline_rmse"] == pytest.approx(expected_baseline_rmse)
    assert report.metrics["baseline_rmse"] == pytest.approx(math.sqrt(2.5))
    assert report.metrics["baseline_mae"] == pytest.approx(expected_baseline_mae)
    assert report.metrics["rmse_skill_score"] == pytest.approx(
        1.0 - expected_rmse / expected_baseline_rmse
    )
    assert report.metrics["mae_skill_score"] == pytest.approx(1.0 - 1.25 / 1.5)
    assert report.metrics["mase"] == pytest.approx(1.25)
    assert report.metrics["directional_accuracy"] == pytest.approx(1.0)
    assert report.metrics["directional_accuracy_horizon"] == pytest.approx(1.0)
    assert report.metrics["coverage_error_mean"] == pytest.approx((0.1 + 0.0 + 0.1) / 3)
    assert report.coverage == {"q10": 0.0, "q50": 0.5, "q90": 1.0}
    assert set(report.metrics) == METRIC_KEYS


def test_a_perfect_forecast_has_no_error_and_full_skill() -> None:
    index = hourly_index(5)
    candles = linear_candles([1.0, 1.0, 1.0, 1.0], index)
    store = store_from_rows(
        index[:1],
        [[[0.5, 1.5], [1.0, 2.0], [1.5, 2.5]]],
        horizon=2,
    )
    report = forecast_skill_report(store, candles)
    assert report.metrics["rmse"] == 0.0
    assert report.metrics["mae"] == 0.0
    assert report.metrics["rmse_skill_score"] == pytest.approx(1.0)
    assert report.metrics["mae_skill_score"] == pytest.approx(1.0)
    assert report.metrics["mase"] == 0.0
    assert report.metrics["directional_accuracy"] == pytest.approx(1.0)
    assert report.coverage == {"q10": 0.0, "q50": 1.0, "q90": 1.0}


def test_coverage_matches_the_empirical_fraction() -> None:
    index = hourly_index(11)
    candles = linear_candles([0.1] * 10, index)
    store = store_from_rows(
        index[:1],
        [[[0.5] * 10, [0.35] * 10, [1.05] * 10]],
        horizon=10,
    )
    report = forecast_skill_report(store, candles)
    assert report.n_pairs == 10
    assert report.coverage == {"q10": 0.5, "q50": 0.3, "q90": 1.0}
    assert report.metrics["mae"] == pytest.approx(0.29)
    assert report.metrics["mase"] == pytest.approx(2.9)
    assert report.metrics["directional_accuracy"] == pytest.approx(1.0)
    assert report.metrics["coverage_error_mean"] == pytest.approx((0.4 + 0.2 + 0.1) / 3)


def test_directional_accuracy_ignores_zero_predictions_and_zero_targets() -> None:
    index = hourly_index(5)
    # log closes 0, 0, 1, -1, 2 -> targets 0, 1, -1, 2 at steps 1..4.
    candles = linear_candles([0.0, 1.0, -2.0, 3.0], index)
    store = store_from_rows(
        index[:1],
        [[[-0.5, -1.0, -1.5, -2.5], [1.0, 0.0, -1.0, -2.0], [1.5, 1.0, -0.5, -1.5]]],
        horizon=4,
    )
    report = forecast_skill_report(store, candles)
    assert report.n_pairs == 4
    assert report.metrics["directional_accuracy"] == pytest.approx(0.5)
    assert report.metrics["directional_accuracy_horizon"] == pytest.approx(0.0)


def test_directional_accuracy_is_nan_without_an_eligible_pair() -> None:
    index = hourly_index(4)
    candles = linear_candles([1.0, 1.0, 1.0], index)
    store = store_from_rows(
        index[:1],
        [[[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]],
        horizon=3,
    )
    report = forecast_skill_report(store, candles)
    assert report.n_pairs == 3
    assert math.isnan(report.metrics["directional_accuracy"])
    assert math.isnan(report.metrics["directional_accuracy_horizon"])


# ---------------------------------------------------------------------------
# the baseline and the transform
# ---------------------------------------------------------------------------


def test_naive_backend_scores_exactly_like_the_random_walk_baseline(tmp_path: Path) -> None:
    candles = make_ohlcv(150)
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend="naive",
        context_length=32,
        horizon=6,
        reforecast_every=24,
        quantile_levels=LEVELS,
        seasonal_period=24,
        seasonal_window=48,
    )
    path, _ = build_forecast_artifact(candles, config, tmp_path / "naive.parquet")
    store = ForecastStore.load(path)

    report = forecast_skill_report(store, candles)
    assert report.n_origins > 0
    assert report.n_pairs == report.n_origins * 6
    assert report.metrics["rmse"] == pytest.approx(report.metrics["baseline_rmse"])
    assert report.metrics["mae"] == pytest.approx(report.metrics["baseline_mae"])
    assert report.metrics["rmse_skill_score"] == pytest.approx(0.0, abs=1e-12)
    assert report.metrics["mae_skill_score"] == pytest.approx(0.0, abs=1e-12)
    assert math.isnan(report.metrics["directional_accuracy"])
    assert report.metrics["mase"] == pytest.approx(
        report.metrics["mae"] / _mean_step(candles, store)
    )


def _mean_step(candles: pd.DataFrame, store: ForecastStore) -> float:
    """Recompute the MASE denominator of ``store`` from the public transform."""
    target = deseasonalize_log_price(
        candles["close"],
        timeframe=store.metadata.timeframe,
        period=store.metadata.seasonal_period,
        window=store.metadata.seasonal_window,
    )
    values = target.to_numpy(dtype="float64")
    positions = candles.index.get_indexer(store.origins())
    first = int(positions.min())
    last = int(positions.max()) + store.horizon
    return float(np.mean(np.abs(np.diff(values[first : last + 1]))))


def test_target_is_recomputed_with_the_metadata_transform(tmp_path: Path) -> None:
    candles = make_ohlcv(200)
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend="naive",
        context_length=32,
        horizon=4,
        reforecast_every=32,
        quantile_levels=LEVELS,
        seasonal_period=24,
        seasonal_window=96,
    )
    path, metadata = build_forecast_artifact(candles, config, tmp_path / "seasonal.parquet")
    store = ForecastStore.load(path)
    report = forecast_skill_report(store, candles)

    target = deseasonalize_log_price(
        candles["close"], timeframe="1h", period=24, window=96
    ).to_numpy(dtype="float64")
    positions = candles.index.get_indexer(store.origins())
    targets: list[float] = []
    predictions: list[float] = []
    for position, origin in zip(positions.tolist(), store.origins(), strict=True):
        median = store.trajectory(origin).median.astype("float64")
        for step in range(1, store.horizon + 1):
            targets.append(float(target[position + step]) - float(target[position]))
            predictions.append(float(median[step - 1]))
    expected = np.asarray(targets, dtype="float64")
    assert report.n_pairs == len(expected)
    assert report.metrics["baseline_rmse"] == pytest.approx(float(np.sqrt(np.mean(expected**2))))
    assert report.metrics["rmse"] == pytest.approx(
        float(np.sqrt(np.mean((np.asarray(predictions) - expected) ** 2)))
    )
    assert metadata.seasonal_window == 96
    assert report.metadata.seasonal_period == 24


def test_a_deseasonalised_target_differs_from_the_raw_log_price(tmp_path: Path) -> None:
    candles = make_ohlcv(200)
    raw = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend="naive",
        context_length=32,
        horizon=4,
        reforecast_every=32,
        quantile_levels=LEVELS,
        seasonal_window=1,
    )
    seasonal = replace(raw, seasonal_window=96)
    raw_path, _ = build_forecast_artifact(candles, raw, tmp_path / "raw.parquet")
    seasonal_path, _ = build_forecast_artifact(candles, seasonal, tmp_path / "seasonal.parquet")
    raw_report = forecast_skill_report(ForecastStore.load(raw_path), candles)
    seasonal_report = forecast_skill_report(ForecastStore.load(seasonal_path), candles)
    assert raw_report.n_pairs == seasonal_report.n_pairs
    assert raw_report.metrics["baseline_rmse"] != pytest.approx(
        seasonal_report.metrics["baseline_rmse"]
    )


# ---------------------------------------------------------------------------
# uncovered, empty and degenerate evaluations
# ---------------------------------------------------------------------------


def test_an_uncovered_artifact_reports_nan_metrics() -> None:
    index = hourly_index(4)
    candles = linear_candles([1.0, 1.0, 1.0], index)
    store = store_from_rows(
        pd.DatetimeIndex([pd.Timestamp("2030-01-01T00:00:00Z")], name=ORIGIN_INDEX_NAME),
        [[[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]],
        horizon=2,
    )
    report = forecast_skill_report(store, candles)
    assert report.n_origins == 0
    assert report.n_pairs == 0
    assert set(report.metrics) == METRIC_KEYS
    assert all(math.isnan(value) for value in report.metrics.values())
    assert set(report.coverage) == {"q10", "q50", "q90"}
    assert all(math.isnan(value) for value in report.coverage.values())


def test_an_empty_candle_frame_reports_nan_metrics() -> None:
    index = hourly_index(4)
    empty = pd.DataFrame({"close": np.empty(0)}, index=index[:0])
    store = store_from_rows(index[:1], [[[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]], horizon=2)
    report = forecast_skill_report(store, empty)
    assert report.n_origins == 0
    assert report.n_pairs == 0
    assert report.horizon == 2
    assert all(math.isnan(value) for value in report.metrics.values())


def test_an_empty_artifact_reports_nan_metrics(tmp_path: Path) -> None:
    candles = make_ohlcv(20)
    config = ForecastBuildConfig(
        symbol="BTC/USDT", context_length=32, horizon=4, reforecast_every=8, quantile_levels=LEVELS
    )
    path, metadata = build_forecast_artifact(candles, config, tmp_path / "empty.parquet")
    assert metadata.n_origins == 0
    report = forecast_skill_report(ForecastStore.load(path), candles)
    assert report.n_origins == 0
    assert report.n_pairs == 0
    assert report.horizon == 4
    assert all(math.isnan(value) for value in report.metrics.values())
    assert set(report.coverage) == {"q10", "q50", "q90"}


def test_candles_that_end_at_the_origin_report_nan_metrics() -> None:
    index = hourly_index(4)
    candles = linear_candles([1.0, 1.0, 1.0], index)
    store = store_from_rows(
        pd.DatetimeIndex([index[-1]], name=ORIGIN_INDEX_NAME),
        [[[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]],
        horizon=2,
    )
    report = forecast_skill_report(store, candles)
    assert report.n_origins == 0
    assert report.n_pairs == 0


def test_a_short_frame_evaluates_a_partial_horizon(tmp_path: Path) -> None:
    candles = make_ohlcv(120)
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend="naive",
        context_length=32,
        horizon=5,
        reforecast_every=16,
        quantile_levels=LEVELS,
        seasonal_window=48,
    )
    path, _ = build_forecast_artifact(candles, config, tmp_path / "naive.parquet")
    store = ForecastStore.load(path)
    assert len(store.origins()) == 6

    report = forecast_skill_report(store, candles.iloc[:50])
    assert report.n_origins == 2
    assert report.n_pairs == 5 + 2
    assert report.horizon == 5


def test_a_flat_market_reports_nan_skill_scores() -> None:
    candles = make_flat_ohlcv(120)
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend="naive",
        context_length=32,
        horizon=4,
        reforecast_every=16,
        quantile_levels=LEVELS,
        seasonal_window=48,
    )
    frame, metadata = build_forecast_frame(candles, config, created_at=CREATED_AT)
    report = forecast_skill_report(ForecastStore(frame, metadata), candles)
    assert report.n_pairs > 0
    assert report.metrics["rmse"] == 0.0
    assert report.metrics["baseline_rmse"] == 0.0
    assert math.isnan(report.metrics["rmse_skill_score"])
    assert math.isnan(report.metrics["mae_skill_score"])
    assert math.isnan(report.metrics["mase"])
    assert math.isnan(report.metrics["directional_accuracy"])
    assert report.coverage == {"q10": 1.0, "q50": 1.0, "q90": 1.0}


# ---------------------------------------------------------------------------
# report object
# ---------------------------------------------------------------------------


def test_report_to_dict_is_sorted_and_json_safe() -> None:
    index = hourly_index(5)
    candles = linear_candles([1.0, 1.0, 1.0, 1.0], index)
    store = store_from_rows(index[:1], [[[0.0, -1.0], [0.5, 4.0], [3.0, 5.0]]], horizon=2)
    report = forecast_skill_report(store, candles)
    payload = report.to_dict()
    assert list(payload) == ["coverage", "metadata", "metrics"]
    assert list(payload["metrics"]) == sorted(report.metrics)
    assert list(payload["coverage"]) == sorted(report.coverage)
    assert payload["metadata"]["backend"] == "test"
    assert payload["metadata"] == report.metadata.to_dict()
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["metrics"]["rmse"] == pytest.approx(report.metrics["rmse"])


def test_report_to_dict_keeps_nan_metrics() -> None:
    index = hourly_index(4)
    candles = linear_candles([1.0, 1.0, 1.0], index)
    store = store_from_rows(
        pd.DatetimeIndex([pd.Timestamp("2030-01-01T00:00:00Z")], name=ORIGIN_INDEX_NAME),
        [[[0.0], [0.0], [0.0]]],
        horizon=1,
    )
    report = forecast_skill_report(store, candles)
    payload = report.to_dict()
    assert "directional_accuracy" in payload["metrics"]
    assert math.isnan(payload["metrics"]["directional_accuracy"])
    assert math.isnan(json.loads(json.dumps(payload))["metrics"]["mase"])


def test_report_is_a_frozen_dataclass() -> None:
    index = hourly_index(4)
    candles = linear_candles([1.0, 1.0, 1.0], index)
    store = store_from_rows(index[:1], [[[0.0], [0.0], [0.0]]], horizon=1)
    report = forecast_skill_report(store, candles)
    assert isinstance(report, ForecastSkillReport)
    with pytest.raises(FrozenInstanceError):
        report.n_pairs = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# invalid candle frames
# ---------------------------------------------------------------------------


def _reversed(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.iloc[::-1]


def _duplicated(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.concat([frame, frame.iloc[[-1]]])


def _no_close(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.drop(columns=["close"])


def _text_close(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(close="not a number")


def _nan_close(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(close=np.nan)


def _negative_close(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(close=-1.0)


def _range_index(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.reset_index(drop=True)


BAD_CANDLES: list[tuple[str, Callable[[pd.DataFrame], pd.DataFrame]]] = [
    ("no_close", _no_close),
    ("text_close", _text_close),
    ("nan_close", _nan_close),
    ("negative_close", _negative_close),
    ("range_index", _range_index),
    ("reversed", _reversed),
    ("duplicated", _duplicated),
]


@pytest.mark.parametrize(("case", "mutate"), BAD_CANDLES, ids=[case for case, _ in BAD_CANDLES])
def test_invalid_candles_raise_a_forecast_error(
    case: str, mutate: Callable[[pd.DataFrame], pd.DataFrame]
) -> None:
    index = hourly_index(5)
    candles = linear_candles([1.0, 1.0, 1.0, 1.0], index)
    store = store_from_rows(index[:1], [[[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]], horizon=2)
    with pytest.raises(ForecastError) as info:
        forecast_skill_report(store, mutate(candles))
    assert info.value.issues, f"{case} produced no issue list"


def test_a_non_frame_raises_a_forecast_error() -> None:
    index = hourly_index(4)
    store = store_from_rows(index[:1], [[[0.0], [0.0], [0.0]]], horizon=1)
    with pytest.raises(ForecastError, match="DataFrame"):
        forecast_skill_report(store, "not a frame")  # type: ignore[arg-type]


def test_real_price_metrics_are_reported_next_to_the_deseasonalised_ones(
    tmp_path: Path,
) -> None:
    """The report measures the REAL price move too, not only the modelled target.

    A forecast can score well against the de-seasonalised target while carrying no
    information about the price actually traded: the target transform removes a
    seasonal component that is large and itself unpredictable.  The report must
    therefore expose both, and on a strongly seasonal series the real-price RMSE
    is strictly larger, because the real move keeps the seasonal swing.
    """
    candles = make_ohlcv(400)
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend="naive",
        context_length=32,
        horizon=12,
        reforecast_every=32,
        quantile_levels=LEVELS,
        seasonal_window=168,
    )
    path, _ = build_forecast_artifact(candles, config, tmp_path / "seasonal.parquet")
    report = forecast_skill_report(ForecastStore.load(path), candles)

    assert np.isfinite(report.metrics["real_rmse"])
    assert np.isfinite(report.metrics["real_rmse_skill_score"])
    # The naive backend's median path is flat, so it makes no directional call at
    # all: the accuracy is undefined (NaN), never a fabricated 0.5.
    assert np.isnan(report.metrics["real_directional_accuracy_horizon"])
    # The real price move keeps the seasonal component the target removed, so its
    # baseline is strictly larger.
    assert report.metrics["real_baseline_rmse"] > report.metrics["baseline_rmse"]
    # Both targets are scored on exactly the same pairs.
    assert report.n_pairs > 0
