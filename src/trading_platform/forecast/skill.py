"""Offline forecast-skill report: is the artifact better than a random walk?

A backtest PnL is **not** evidence that a forecast has an edge: fees, position
sizing and a favourable market window can produce a profit out of noise.  The
2025-26 literature on time-series foundation models is unambiguous on this
point — on equity returns, off-the-shelf and fine-tuned models show no reliable
directional skill, and on realised volatility TimesFM-2.5 loses to a Log-HAR
baseline at every horizon.  The mitigation is mechanical: measure the forecast
itself, against the honest baseline, before looking at any PnL.

This module is that measurement, and it is deliberately cheap and offline:

* it recomputes the evaluation target from the **candle frame** with the same
  causal de-seasonalisation the builder used
  (:func:`trading_platform.forecast.series.deseasonalize_log_price`), so the
  comparison never depends on the artifact being self-consistent;
* it compares the stored median path with the **random-walk baseline** (predict
  ``0.0``, i.e. "the origin close is held");
* it reports error metrics (RMSE/MAE/MASE and their skill scores against the
  baseline), the empirical coverage of the stored quantiles and the directional
  accuracy at the horizon;
* it never raises on an empty or uncovered evaluation: an absent evaluation is
  reported as ``NaN`` metrics, never as a guess.

The report is a *diagnostic*.  Its usual outcome on hourly crypto is "the model
does not beat the random walk", and that is exactly what it exists to show.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from trading_platform.core.constants import UTC
from trading_platform.core.errors import ForecastError
from trading_platform.forecast.artifact import ArtifactMetadata, ForecastStore
from trading_platform.forecast.series import deseasonalize_log_price

__all__ = ["ForecastSkillReport", "forecast_skill_report"]

#: Metric keys of a report, in the order they are documented (all ``float``).
_METRIC_NAMES: tuple[str, ...] = (
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
    "real_rmse",
    "real_baseline_rmse",
    "real_rmse_skill_score",
    "real_directional_accuracy_horizon",
)

#: Predictions of the random-walk baseline (the origin close is held).
_BASELINE_PREDICTION: float = 0.0


@dataclass(frozen=True)
class ForecastSkillReport:
    """Skill of one artifact against the random-walk baseline.

    Attributes
    ----------
    metadata:
        Metadata of the evaluated artifact.
    n_origins:
        Number of origins with at least one evaluated step.
    n_pairs:
        Number of evaluated ``(origin, step)`` pairs.
    horizon:
        Horizon of the artifact (the evaluation stops earlier when the candle
        frame ends first).
    metrics:
        Scalar metrics, one key per reported quantity: ``rmse``, ``mae``,
        ``baseline_rmse``, ``baseline_mae``, ``rmse_skill_score``,
        ``mae_skill_score``, ``mase``, ``directional_accuracy``,
        ``directional_accuracy_horizon`` and ``coverage_error_mean``.  A metric
        that is undefined for the evaluation is ``NaN``, never an exception:
        ``mase`` needs a non-zero mean absolute one-step change, the skill
        scores need a non-zero baseline error and the directional accuracies need
        at least one pair with a non-zero prediction *and* a non-zero target.
    coverage:
        Empirical coverage keyed ``"q10"`` … ``"q90"`` (one key per stored
        level): the fraction of evaluated targets at or below the predicted
        level quantile.  A well-calibrated ``q10`` sits near ``0.1``.

    Examples
    --------
    >>> report = forecast_skill_report(store, candles)  # doctest: +SKIP
    >>> round(report.metrics["mae_skill_score"], 3)     # doctest: +SKIP
    -0.014
    """

    metadata: ArtifactMetadata
    n_origins: int
    n_pairs: int
    horizon: int
    metrics: dict[str, float]
    coverage: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe, key-sorted view of the report.

        ``NaN`` metrics are kept as ``NaN`` floats: a report must be able to say
        "this could not be measured" rather than silently dropping a key.
        """
        return {
            "coverage": {key: float(value) for key, value in sorted(self.coverage.items())},
            "metadata": self.metadata.to_dict(),
            "metrics": {key: float(value) for key, value in sorted(self.metrics.items())},
        }


def forecast_skill_report(store: ForecastStore, candles: pd.DataFrame) -> ForecastSkillReport:
    """Measure the skill of ``store`` on the candles it was built from.

    A pair is one ``(origin, step)`` with ``step`` in ``1 … horizon`` such that
    the candle ``origin + step`` exists in ``candles``.  Its target is the
    de-seasonalised move ``z[origin + step] - z[origin]`` and its prediction is
    the stored median at that step.  Because the de-seasonalisation is causal,
    evaluating on the whole candle frame is not leakage — the transform never
    looks ahead.

    Parameters
    ----------
    store:
        Artifact to evaluate.
    candles:
        Candle frame the artifact was built from (or any frame with a ``close``
        column).  A tz-naive index is read as UTC.

    Returns
    -------
    ForecastSkillReport
        The metrics, the empirical coverage and the evaluated counts.  When no
        pair can be evaluated (empty artifact, candles that do not cover it, a
        frame that ends at the first origin) the counts are ``0`` and every
        metric and coverage entry is ``NaN``.

    Raises
    ------
    ForecastError
        If ``candles`` is not a non-empty ``DataFrame`` with an ascending,
        unique ``DatetimeIndex`` and a numeric, strictly positive ``close``
        column.
    """
    metadata = store.metadata
    horizon = int(store.horizon)
    levels = store.quantile_levels
    stamps, close = _validated_candles(candles)
    if len(stamps) == 0 or len(store.origins()) == 0:
        return _empty_report(metadata, horizon, levels)

    target = deseasonalize_log_price(
        close,
        timeframe=str(metadata.timeframe),
        period=int(metadata.seasonal_period),
        window=int(metadata.seasonal_window),
    )
    values = target.to_numpy(dtype="float64")

    origins = store.origins()
    positions = stamps.get_indexer(origins)
    predictions: list[float] = []
    targets: list[float] = []
    real_targets: list[float] = []
    steps: list[int] = []
    hits = np.zeros(len(levels), dtype="float64")
    covered = 0
    first_position: int | None = None
    last_position: int | None = None
    candle_count = len(stamps)
    closing = close.to_numpy(dtype="float64")

    for position, origin in zip(positions.tolist(), origins, strict=True):
        if position < 0:
            continue
        available = candle_count - 1 - position
        if available < 1:
            continue
        limit = min(horizon, available)
        trajectory = store.trajectory(origin)
        median = np.asarray(trajectory.median, dtype="float64")
        quantiles = np.asarray(trajectory.quantiles, dtype="float64")
        base = float(values[position])
        real_base = float(np.log(closing[position]))
        covered += 1
        first_position = position if first_position is None else min(first_position, position)
        for step in range(1, limit + 1):
            predictions.append(float(median[step - 1]))
            targets.append(float(values[position + step]) - base)
            # What a trader is actually paid: the move in the PRICE, which still
            # carries the seasonal component the target transform removed.
            real_targets.append(float(np.log(closing[position + step])) - real_base)
            steps.append(step)
            hits += (targets[-1] <= quantiles[:, step - 1]).astype("float64")
            last_position = position + step

    n_pairs = len(predictions)
    if n_pairs == 0:
        return _empty_report(metadata, horizon, levels)

    prediction_array = np.asarray(predictions, dtype="float64")
    target_array = np.asarray(targets, dtype="float64")
    real_array = np.asarray(real_targets, dtype="float64")
    step_array = np.asarray(steps, dtype="int64")
    error = prediction_array - target_array
    real_error = prediction_array - real_array

    rmse = float(np.sqrt(np.mean(error**2)))
    mae = float(np.mean(np.abs(error)))
    baseline_rmse = float(np.sqrt(np.mean(target_array**2)))
    baseline_mae = float(np.mean(np.abs(target_array)))
    real_rmse = float(np.sqrt(np.mean(real_error**2)))
    real_baseline_rmse = float(np.sqrt(np.mean(real_array**2)))

    coverage = {
        _coverage_key(level): float(hits[index]) / float(n_pairs)
        for index, level in enumerate(levels)
    }
    differences = [abs(coverage[_coverage_key(level)] - float(level)) for level in levels]
    mase_denominator = _mean_absolute_step(values, first_position, last_position)
    metrics = {
        "rmse": rmse,
        "mae": mae,
        "baseline_rmse": baseline_rmse,
        "baseline_mae": baseline_mae,
        "rmse_skill_score": _skill_score(rmse, baseline_rmse),
        "mae_skill_score": _skill_score(mae, baseline_mae),
        "mase": mae / mase_denominator if mase_denominator > 0.0 else float("nan"),
        "directional_accuracy": _directional_accuracy(prediction_array, target_array),
        "directional_accuracy_horizon": _directional_accuracy(
            prediction_array[step_array == horizon], target_array[step_array == horizon]
        ),
        "coverage_error_mean": float(np.mean(differences)) if differences else float("nan"),
        # The same measurements against the REAL price move, which is what a
        # strategy is paid on.  The de-seasonalised target is a modelling
        # convenience: it has the seasonal component removed, so a model can score
        # well here while earning nothing on the actual price.  These two keys are
        # the ones to read before claiming an edge.
        "real_rmse": real_rmse,
        "real_baseline_rmse": real_baseline_rmse,
        "real_rmse_skill_score": _skill_score(real_rmse, real_baseline_rmse),
        "real_directional_accuracy_horizon": _directional_accuracy(
            prediction_array[step_array == horizon], real_array[step_array == horizon]
        ),
    }
    return ForecastSkillReport(
        metadata=metadata,
        n_origins=covered,
        n_pairs=n_pairs,
        horizon=horizon,
        metrics=metrics,
        coverage=coverage,
    )


def _validated_candles(candles: pd.DataFrame) -> tuple[pd.DatetimeIndex, pd.Series]:
    """Return the UTC index and the ``float64`` close of ``candles``.

    Raises
    ------
    ForecastError
        If the frame is not a ``DataFrame`` with a ``DatetimeIndex``, if its
        timestamps are duplicated or unsorted, or if ``close`` is missing,
        non-numeric, non-finite or not strictly positive.  An **empty** frame is
        accepted: it carries no evaluation, which the report expresses as
        ``NaN`` metrics.
    """
    if not isinstance(candles, pd.DataFrame):
        raise ForecastError(f"candles must be a pandas DataFrame, got {type(candles).__name__}")
    raw_index = candles.index
    if not isinstance(raw_index, pd.DatetimeIndex):
        raise ForecastError(
            "invalid candle frame",
            [f"the index must be a DatetimeIndex, got {type(raw_index).__name__}"],
        )
    if "close" not in candles.columns:
        raise ForecastError("invalid candle frame", ["a 'close' column is required"])

    issues: list[str] = []
    stamps = raw_index.tz_localize(UTC) if raw_index.tz is None else raw_index.tz_convert(UTC)
    if stamps.has_duplicates:
        issues.append("candle timestamps must be unique")
    if not stamps.is_monotonic_increasing:
        issues.append("candle timestamps must be sorted ascending")
    try:
        values = candles["close"].to_numpy(dtype="float64")
    except (TypeError, ValueError):
        raise ForecastError(
            "invalid candle frame", ["the 'close' column must hold numbers"]
        ) from None
    if values.size and not bool(np.all(np.isfinite(values))):
        issues.append("the 'close' column must not contain NaN or infinite values")
    elif values.size and bool(np.any(values <= 0.0)):
        issues.append("the 'close' column must be strictly positive to define a log price")
    if issues:
        raise ForecastError("invalid candle frame", issues)
    if values.size == 0:
        return pd.DatetimeIndex(stamps), pd.Series(
            np.empty(0, dtype="float64"), index=stamps, name="close", dtype="float64"
        )
    return (
        pd.DatetimeIndex(stamps),
        pd.Series(values, index=stamps, name="close", dtype="float64"),
    )


def _empty_report(
    metadata: ArtifactMetadata, horizon: int, levels: tuple[float, ...]
) -> ForecastSkillReport:
    """Return the report of an evaluation that could not measure anything."""
    return ForecastSkillReport(
        metadata=metadata,
        n_origins=0,
        n_pairs=0,
        horizon=int(horizon),
        metrics={name: float("nan") for name in _METRIC_NAMES},
        coverage={_coverage_key(level): float("nan") for level in levels},
    )


def _coverage_key(level: float) -> str:
    """Return the coverage key of a level (``0.1`` → ``"q10"``, ``0.05`` → ``"q05"``)."""
    return f"q{round(float(level) * 100):02d}"


def _skill_score(candidate: float, baseline: float) -> float:
    """Return ``1 - candidate / baseline``, or ``NaN`` when the baseline is zero."""
    if baseline == 0.0 or not np.isfinite(baseline):
        return float("nan")
    return 1.0 - candidate / baseline


def _directional_accuracy(predictions: np.ndarray, targets: np.ndarray) -> float:
    """Return the fraction of pairs whose sign agrees, ignoring zero on either side.

    A zero prediction carries no direction (it is the random-walk baseline) and a
    zero target has no direction either, so those pairs are excluded rather than
    counted as a success.  ``NaN`` means "no eligible pair".
    """
    mask = (predictions != _BASELINE_PREDICTION) & (targets != _BASELINE_PREDICTION)
    if not bool(np.any(mask)):
        return float("nan")
    agreement = np.sign(predictions[mask]) == np.sign(targets[mask])
    return float(np.count_nonzero(agreement)) / float(np.count_nonzero(mask))


def _mean_absolute_step(
    values: np.ndarray, first_position: int | None, last_position: int | None
) -> float:
    """Return the mean absolute one-step change over the evaluated candle span.

    The span runs from the earliest evaluated origin to the latest evaluated
    target candle, which is what makes ``MASE`` the error of the forecast
    relative to the error of a one-step random walk *on the evaluated sample*.
    """
    if first_position is None or last_position is None or last_position <= first_position:
        return 0.0
    changes = np.abs(np.diff(values[first_position : last_position + 1]))
    if changes.size == 0:
        return 0.0
    return float(np.mean(changes))
