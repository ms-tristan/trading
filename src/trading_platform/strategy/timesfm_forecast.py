"""The ``timesfm`` strategy: forecast-driven entries and a priority-ordered exit tree.

The strategy consumes a **pre-computed** forecast artifact — a parquet file built
offline by ``trading forecast-build`` — and never runs a model itself.  Its two
public methods stay pure and I/O-free, exactly like every other house strategy:

``prepare(data)``
    Adds the house indicators (RSI, ATR, EMAs, realised volatility, ATR
    percentile) **and** the forecast diagnostic columns read from the artifact
    attached through :meth:`~trading_platform.strategy.base.Strategy.set_feature_bundle`.
    Without an artifact every forecast column is ``NaN``, ``exit_code`` is ``0``
    and ``signals`` then produces no order at all: a missing forecast is never a
    guess and never an exception.

``signals(prepared)``
    Turns those diagnostics into the AND-composed entry gates and the
    **priority-ordered exit decision tree** of the design note, exposed through
    the ``exit_code`` diagnostic column so every decision is auditable.

Decision algorithm
------------------
Availability.  An artifact stores one trajectory per *origin* (the last context
candle), and origins are spaced by the ``reforecast_every`` stride.  At candle
``t`` the active origin is the most recent stored origin at or before ``t`` and

    ``forecast_age(t) = t - active_origin_row``

is how many candles old the prediction is.  A row carries a usable forecast when
an origin exists, ``forecast_age <= store.horizon - 1 - min_lead`` (the
``min_lead`` cushion keeps every decision inside the covered window) and
``forecast_age <= params.forecast_age`` (the staleness bound).

Entries (all of them must hold; each is a parameter):

1. freshness: a usable forecast that is already at least one candle old;
2. sign and size: ``forecast_alpha`` on the traded side and ``>= min_alpha``;
3. significance: ``|forecast_alpha| >= alpha_vs_atr * atr_pct``;
4. reliability: ``forecast_reliability >= min_reliability``;
5. agreement: ``forecast_agreement >= min_agreement``;
6. shape: ``forecast_path_eff >= min_path_efficiency``;
7. volatility regime: ``vol_ratio <= max_vol_ratio`` and the ATR percentile
   inside ``[min_atr_percentile, max_atr_percentile]``;
8. optional RSI context filter (only when ``rsi_min > 0`` or ``rsi_max < 100``);
9. cooldown: no entry within ``cooldown`` candles of a *losing* exit.

Exits, evaluated at every close, **first match wins**, and only on rows carrying
a usable forecast:

===== ==================== =====================================================
Order Code                 Fires when
===== ==================== =====================================================
1     ``FORECAST_FLIP``    the predicted edge reverses against the position
2     ``EDGE_DECAY``       ``|forecast_alpha| < exit_alpha`` for ``exit_confirm``
                           consecutive candles
3     ``TARGET_REACHED``   the realised move captured ``target_capture`` of the
                           predicted favourable excursion and the forecast no
                           longer supports more
4     ``PATH_DEGRADED``    ``forecast_path_eff`` or ``forecast_reliability``
                           collapsed below its exit floor
5     ``TIME_STOP``        ``max_hold`` candles (capped by the covered window)
                           without ``min_progress`` of the expected move
6     ``VOL_REGIME``       ``vol_ratio > exit_vol_ratio``
7     ``STALE_FORECAST``   the forecast vanished or aged past ``forecast_age``
===== ==================== =====================================================

``EXIT_CODES['ATR_STOP']`` is **reserved**: the static protective stop travels in
the ``stop_loss`` column (``close - atr_stop_multiplier * atr``, mirrored by the
engine for a short) and is therefore never written by ``signals``.

``max_hold`` is a *soft* bound.  The time stop is skipped while the position is
progressing (``min_progress`` of the predicted excursion already realised), so
the hard backstop of every open position is the end of the coverage window: a
position entered on an available row is exited at the latest when the window
closes, which is at most ``horizon - min_lead`` candles after its origin — and
therefore within ``min(max_hold, horizon - min_lead)`` candles of its entry
whenever ``max_hold`` is at least ``horizon - min_lead``, which the shipped
defaults satisfy.

Honesty
-------
Positive backtest PnL is **not** evidence of a forecasting edge.  Off-the-shelf
and fine-tuned time-series foundation models show no reliable directional skill
on equity-style returns in the 2025-26 literature, and the same question is
open for hourly crypto.  The forecast layer ships its own skill report
(``trading forecast-skill``: RMSE/MAE/MASE against a random walk, quantile
coverage and directional accuracy) precisely so that this strategy can be shown
to be no better than its baseline.  See ``docs/forecasting.md``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from trading_platform.core.constants import UTC
from trading_platform.core.errors import ForecastError, StrategyError
from trading_platform.strategy.base import (
    Strategy,
    StrategyParams,
    ensure_signal_frame,
    require_ohlcv_frame,
)
from trading_platform.strategy.features import FeatureBundle
from trading_platform.strategy.indicators import atr, ema, rsi

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from trading_platform.forecast.artifact import ForecastStore
    from trading_platform.forecast.types import ForecastTrajectory

__all__ = [
    "ALPHA_LOOKAHEADS",
    "DECISION_COLUMN",
    "DIAGNOSTIC_COLUMNS",
    "EXIT_CODES",
    "FORECAST_COLUMNS",
    "INDICATOR_COLUMNS",
    "LOSS_EXIT_CODES",
    "TimesFMForecastParams",
    "TimesFMForecastStrategy",
]

#: Look-ahead windows, in candles, of the ``forecast_alpha_<k>`` diagnostics.
ALPHA_LOOKAHEADS: tuple[int, ...] = (1, 2, 4, 8)

#: House indicator columns added by :meth:`TimesFMForecastStrategy.prepare`.
#:
#: ``ema_fast`` / ``ema_slow`` are **context** columns: they document the trend
#: the forecast is compared against and are never used as a gate.
INDICATOR_COLUMNS: tuple[str, ...] = (
    "rsi",
    "atr",
    "atr_pct",
    "atr_percentile",
    "realized_vol",
    "ema_fast",
    "ema_slow",
)

#: Forecast diagnostic columns added by :meth:`TimesFMForecastStrategy.prepare`.
FORECAST_COLUMNS: tuple[str, ...] = (
    "forecast_alpha_1",
    "forecast_alpha_2",
    "forecast_alpha_4",
    "forecast_alpha_8",
    "forecast_alpha",
    "forecast_slope",
    "forecast_mfe",
    "forecast_mae",
    "forecast_path_eff",
    "forecast_iqr_term",
    "forecast_iqr_per_bar",
    "forecast_reliability",
    "forecast_agreement",
    "vol_ratio",
    "forecast_age",
    "exit_code",
)

#: Every column :meth:`TimesFMForecastStrategy.prepare` adds to the input frame.
DIAGNOSTIC_COLUMNS: tuple[str, ...] = INDICATOR_COLUMNS + FORECAST_COLUMNS

#: Ordered exit reasons.  ``NO_EXIT`` never fires; ``ATR_STOP`` is **reserved**
#: for the engine-side static stop and is never written by ``signals``.
EXIT_CODES: dict[str, int] = {
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

#: Exit codes counted as a *losing* exit by the entry cooldown.
#:
#: The strategy cannot read the engine's realised P&L (it never sees a position),
#: so "came out at a loss" is approximated by "left through any reason other than
#: ``TARGET_REACHED``".  ``ATR_STOP`` is absent on purpose: it is produced by the
#: engine, not by ``signals``, so it can never appear in the ``exit_code`` column.
LOSS_EXIT_CODES: frozenset[int] = frozenset({1, 2, 4, 5, 6, 7})

#: Diagnostic column carrying the decided edge (the entry look-ahead alpha).
DECISION_COLUMN: str = "forecast_alpha"

#: Ratio ``IQR / (q90 - q10)`` of a Gaussian envelope, used to rebuild an
#: interquartile dispersion when an artifact stores deciles but no quartiles.
_IQR_RANGE_SCALE: float = 0.526


class TimesFMForecastParams(StrategyParams):
    """Validated parameters of :class:`TimesFMForecastStrategy`.

    The defaults describe the shipped configuration: a 512-candle context, a
    24-candle horizon re-anchored every 24 candles, a 2-candle decision cushion
    and a one-day staleness bound (all four expressed in candles, so the same
    numbers mean the same thing on any timeframe).

    ``context_length``, ``horizon`` and ``reforecast_every`` document the artifact
    the strategy expects (they are the very parameters ``trading forecast-build``
    was given with); ``horizon`` additionally bounds the coherence of
    ``entry_lookahead`` and ``min_lead``.  The *decision* always uses the horizon
    and the stride actually stored in the artifact, never the parameter: a
    mismatch can then never silently shift a decision window.
    """

    #: Optional artifact path override.  The strategy **never** loads it itself:
    #: the path is resolved by
    #: :func:`~trading_platform.strategy.features.resolve_features` and the loaded
    #: artifact is injected into the instance.
    artifact: str = ""
    context_length: int = Field(default=512, ge=32)
    horizon: int = Field(default=24, ge=1)
    reforecast_every: int = Field(default=24, ge=1)
    min_lead: int = Field(default=2, ge=0)
    forecast_age: int = Field(default=24, ge=0)
    entry_lookahead: int = Field(default=4, ge=1)
    rsi_period: int = Field(default=14, ge=2)
    atr_period: int = Field(default=14, ge=2)
    ema_fast_period: int = Field(default=9, ge=1)
    ema_slow_period: int = Field(default=21, ge=2)
    vol_window: int = Field(default=24, ge=2)
    atr_percentile_window: int = Field(default=100, ge=2)
    min_alpha: float = Field(default=0.001, ge=0)
    alpha_vs_atr: float = Field(default=0.5, gt=0)
    min_reliability: float = Field(default=0.5)
    min_agreement: float = Field(default=0.6, ge=0, le=1)
    min_path_efficiency: float = Field(default=0.2, ge=0, le=1)
    max_vol_ratio: float = Field(default=3.0, gt=0)
    min_atr_percentile: float = Field(default=0.1, ge=0, le=1)
    max_atr_percentile: float = Field(default=0.95, ge=0, le=1)
    rsi_min: float = Field(default=0.0, ge=0, lt=100)
    rsi_max: float = Field(default=100.0, gt=0, le=100)
    exit_alpha: float = Field(default=0.0005, ge=0)
    exit_confirm: int = Field(default=2, ge=1)
    target_capture: float = Field(default=0.8, gt=0, le=1)
    min_path_efficiency_exit: float = Field(default=0.05, ge=0, le=1)
    min_reliability_exit: float = Field(default=0.1)
    max_hold: int = Field(default=24, ge=1)
    min_progress: float = Field(default=0.3, ge=0, le=1)
    exit_vol_ratio: float = Field(default=6.0, gt=0)
    atr_stop_multiplier: float = Field(default=2.0, gt=0)
    cooldown: int = Field(default=4, ge=0)
    allow_short: bool = False

    @model_validator(mode="after")
    def _check_coherence(self) -> TimesFMForecastParams:
        """Enforce the cross-field invariants of the decision algorithm."""
        issues: list[str] = []
        if self.entry_lookahead > self.horizon:
            issues.append(
                f"entry_lookahead ({self.entry_lookahead}) must not exceed horizon ({self.horizon})"
            )
        if self.horizon - self.min_lead < 1:
            issues.append(f"horizon - min_lead ({self.horizon - self.min_lead}) must be at least 1")
        if self.min_alpha > 0.0 and self.exit_alpha > self.min_alpha:
            # The exit threshold must not sit *above* the entry threshold, or a
            # position would be dropped the candle after it was opened.  With
            # ``min_alpha == 0`` the entry gate is "any positive edge", so the
            # ordering constraint is vacuous and the documented sweep grid stays
            # sweepable (``exit_alpha`` then simply decides how fast the position
            # is dropped).
            issues.append(
                f"exit_alpha ({self.exit_alpha}) must not exceed min_alpha ({self.min_alpha})"
            )
        if self.exit_vol_ratio < self.max_vol_ratio:
            issues.append(
                f"exit_vol_ratio ({self.exit_vol_ratio}) must be at least "
                f"max_vol_ratio ({self.max_vol_ratio})"
            )
        if self.rsi_min >= self.rsi_max:
            issues.append(f"rsi_min ({self.rsi_min}) must be lower than rsi_max ({self.rsi_max})")
        if self.min_atr_percentile >= self.max_atr_percentile:
            issues.append(
                f"min_atr_percentile ({self.min_atr_percentile}) must be lower than "
                f"max_atr_percentile ({self.max_atr_percentile})"
            )
        if self.ema_slow_period <= self.ema_fast_period:
            issues.append(
                f"ema_slow_period ({self.ema_slow_period}) must be greater than "
                f"ema_fast_period ({self.ema_fast_period})"
            )
        if issues:
            raise ValueError("; ".join(issues))
        return self


class TimesFMForecastStrategy(Strategy):
    """Forecast-driven strategy: AND-composed entries, priority-ordered exits."""

    name: ClassVar[str] = "timesfm"
    ParamsModel: ClassVar[type[StrategyParams]] = TimesFMForecastParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {
        "entry_lookahead": [2, 4, 8],
        "min_alpha": [0.0, 0.001, 0.002],
        "min_reliability": [0.25, 0.5, 1.0],
        "min_agreement": [0.5, 0.625, 0.75],
        "max_vol_ratio": [2.0, 3.0, 5.0],
        "atr_stop_multiplier": [1.5, 2.0, 3.0],
        "max_hold": [12, 24],
        "cooldown": [0, 4, 12],
    }

    # -- parameters and features ------------------------------------------

    @property
    def _forecast_params(self) -> TimesFMForecastParams:
        """The parameters, typed as :class:`TimesFMForecastParams`."""
        return cast(TimesFMForecastParams, self.params)

    @property
    def _store(self) -> ForecastStore | None:
        """The forecast artifact attached to this instance, when there is one."""
        bundle = self.feature_bundle
        if isinstance(bundle, FeatureBundle):
            return bundle.forecast
        return None

    def set_feature_bundle(self, features: object | None = None) -> None:
        """Attach the resolved :class:`FeatureBundle` of this run.

        This strategy consumes an external forecast artifact, so the base hook is
        narrowed by validation: anything that is not a
        :class:`~trading_platform.strategy.features.FeatureBundle` (or ``None``)
        is rejected loudly instead of silently producing no signal.

        Raises
        ------
        StrategyError
            If ``features`` is neither ``None`` nor a ``FeatureBundle``.
        """
        if features is not None and not isinstance(features, FeatureBundle):
            raise StrategyError(
                f"strategy {self.name!r} expects a FeatureBundle, got {type(features).__name__}"
            )
        self._feature_bundle = features

    # -- prepare -----------------------------------------------------------

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add the house indicators and the forecast diagnostics to a copy of ``data``.

        The returned frame carries the OHLCV columns, the
        :data:`INDICATOR_COLUMNS`, the :data:`FORECAST_COLUMNS` and the
        ``stop_loss`` column (``close - atr_stop_multiplier * atr``, the *long*
        formula, which the engine mirrors for a short).  The index is preserved
        exactly and ``data`` itself is never mutated.

        Without an attached artifact — or when the frame shares no origin with it
        — every forecast column is ``NaN`` and ``exit_code`` is ``0``: a missing
        forecast means "no decision", never an exception and never a guess.

        Raises
        ------
        StrategyError
            If ``data`` does not satisfy the OHLCV contract.
        """
        params = self._forecast_params
        frame = require_ohlcv_frame(data, name="data")
        index = pd.DatetimeIndex(frame.index)
        close_series = frame["close"]
        close = close_series.to_numpy(dtype="float64")

        frame["rsi"] = rsi(close_series, params.rsi_period).to_numpy(dtype="float64")
        frame["atr"] = atr(frame["high"], frame["low"], close_series, params.atr_period).to_numpy(
            dtype="float64"
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            atr_pct = frame["atr"].to_numpy(dtype="float64") / close
        frame["atr_pct"] = _finite(atr_pct)
        frame["atr_percentile"] = (
            frame["atr"]
            .rolling(params.atr_percentile_window, min_periods=params.atr_percentile_window)
            .rank(pct=True)
            .to_numpy(dtype="float64")
        )
        frame["realized_vol"] = (
            _log_returns(close, index)
            .rolling(params.vol_window, min_periods=params.vol_window)
            .std(ddof=0)
            .to_numpy(dtype="float64")
        )
        frame["ema_fast"] = ema(close_series, params.ema_fast_period).to_numpy(dtype="float64")
        frame["ema_slow"] = ema(close_series, params.ema_slow_period).to_numpy(dtype="float64")

        store = self._store
        columns = _forecast_columns(
            index,
            close,
            frame["realized_vol"].to_numpy(dtype="float64"),
            store,
            params,
        )
        state = _decision_state(
            columns,
            close,
            params,
            horizon=0 if store is None else int(store.horizon),
        )
        for name in FORECAST_COLUMNS:
            if name == "exit_code":
                frame[name] = _combined_codes(state)
            else:
                frame[name] = columns[name]

        with np.errstate(invalid="ignore"):
            stop = close - params.atr_stop_multiplier * frame["atr"].to_numpy(dtype="float64")
        frame["stop_loss"] = _finite(stop)
        return frame

    # -- signals -----------------------------------------------------------

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the signal frame of a frame produced by :meth:`prepare`.

        The computation is pure and repeatable — the input is never mutated — and
        every decision is exposed through the ``exit_code`` column of the
        *prepared* frame, so an exit can always be audited back to its reason.

        Raises
        ------
        StrategyError
            If ``data`` is not a prepared frame (missing OHLCV, indicator or
            diagnostic column, or a non-numeric diagnostic column).
        """
        params = self._forecast_params
        if not isinstance(data, pd.DataFrame):
            raise StrategyError(f"data must be a pandas DataFrame, got {type(data).__name__}")
        required = ("close", "stop_loss", *DIAGNOSTIC_COLUMNS)
        missing = [column for column in required if column not in data.columns]
        if missing:
            raise StrategyError(
                "signals() must be called on a frame returned by prepare()",
                [f"missing column(s): {', '.join(missing)}"],
            )
        try:
            close = data["close"].to_numpy(dtype="float64")
            stop_loss = data["stop_loss"].to_numpy(dtype="float64")
            columns: dict[str, np.ndarray] = {
                name: data[name].to_numpy(dtype="float64") for name in DIAGNOSTIC_COLUMNS
            }
        except (TypeError, ValueError):
            raise StrategyError(
                "signals() must be called on a frame returned by prepare()",
                ["every OHLCV and diagnostic column must be numeric"],
            ) from None

        store = self._store
        state = _decision_state(
            columns,
            close,
            params,
            horizon=0 if store is None else int(store.horizon),
        )
        entries = _entry_gates(columns, state, params)
        exit_long = state.long_codes != 0
        exit_short = state.short_codes != 0

        signals = pd.DataFrame(
            {
                "entry_long": entries.long,
                "exit_long": exit_long,
                "entry_short": entries.short,
                "exit_short": exit_short,
                "stop_loss": stop_loss,
            },
            index=data.index,
        )
        return ensure_signal_frame(signals, data.index)


# ---------------------------------------------------------------------------
# internal helpers -- per-row forecast state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DecisionState:
    """Per-row forecast state shared by :meth:`prepare` and :meth:`signals`.

    Attributes
    ----------
    age:
        ``forecast_age`` per row (``NaN`` when no origin covers the row).
    available:
        ``True`` where the row carries a usable forecast.
    realised:
        Normalised move from the active origin close to this candle, long
        convention (``NaN`` when undefined).
    long_codes, short_codes:
        Ordered exit code of the long / short decision tree (``0`` = no exit).
        The short codes are all ``0`` unless ``allow_short`` is set.
    """

    age: np.ndarray
    available: np.ndarray
    realised: np.ndarray
    long_codes: np.ndarray
    short_codes: np.ndarray


@dataclass(frozen=True)
class _EntryGates:
    """The AND-composed entry gates, one boolean array per direction."""

    long: np.ndarray
    short: np.ndarray


def _finite(values: np.ndarray) -> np.ndarray:
    """Return ``values`` as ``float64`` with every non-finite entry mapped to ``NaN``."""
    numeric = np.asarray(values, dtype="float64")
    return np.where(np.isfinite(numeric), numeric, np.nan)


def _log_returns(close: np.ndarray, index: pd.DatetimeIndex) -> pd.Series:
    """Return the ``float64`` log-return series of ``close`` (``NaN`` on the first row)."""
    positive = close > 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        log_close = np.where(positive, np.log(np.where(positive, close, 1.0)), np.nan)
    return pd.Series(log_close, index=index, dtype="float64").diff()


def _origin_positions(
    index: pd.DatetimeIndex,
    store: ForecastStore,
) -> tuple[np.ndarray, pd.DatetimeIndex]:
    """Map every artifact origin to its position in ``index``.

    The mapping is **positional and causal**: it only uses the frame's own index,
    so a frame that merely slices the artifact's history (a walk-forward window,
    for example) works unchanged.

    Returns
    -------
    tuple[numpy.ndarray, pandas.DatetimeIndex]
        The frame positions of the origins present in ``index``, ascending, and
        the matching origins.  Both are empty when nothing can be mapped (empty
        artifact, empty frame, duplicated frame index or an index that cannot be
        compared with the artifact's UTC index).
    """
    origins = store.origins()
    empty = np.empty(0, dtype="int64")
    if len(origins) == 0 or len(index) == 0:
        return empty, origins[:0]
    lookup = index.tz_localize(UTC) if index.tz is None else index.tz_convert(UTC)
    try:
        positions = np.asarray(lookup.get_indexer(origins), dtype="int64")
    except (TypeError, ValueError, KeyError, IndexError):
        return empty, origins[:0]
    present = positions >= 0
    if not bool(present.any()):
        return empty, origins[:0]
    order = np.argsort(positions[present], kind="stable")
    return positions[present][order], origins[present][order]


def _alpha_window(median: np.ndarray, ages: np.ndarray, lookahead: int) -> np.ndarray:
    """Return ``median[age + lookahead - 1]``, ``NaN`` where the path ends first."""
    values = np.full(ages.shape[0], np.nan, dtype="float64")
    steps = ages + float(lookahead)
    inside = steps <= float(median.shape[0])
    if bool(inside.any()):
        values[inside] = median[steps[inside].astype("int64") - 1]
    return values


def _dispersion_path(trajectory: ForecastTrajectory) -> np.ndarray | None:
    """Return the widest stored dispersion path as an interquartile range.

    The exact quartiles (``0.25`` / ``0.75``) are used when the artifact stores
    them.  A decile-only artifact (the TimesFM default) is calibrated on the
    ``q10 - q90`` range instead, scaled by :data:`_IQR_RANGE_SCALE` — the
    Gaussian ratio between an interquartile range and that range — so the
    dispersion stays on the same scale.  ``None`` means "no dispersion stored":
    the caller then reports ``NaN`` rather than inventing a value.
    """
    candidates: tuple[tuple[float, tuple[float, float]], ...] = (
        (1.0, (0.25, 0.75)),
        (_IQR_RANGE_SCALE, (0.1, 0.9)),
        (_IQR_RANGE_SCALE, (0.2, 0.8)),
    )
    for scale, (low, high) in candidates:
        try:
            spread = trajectory.quantile(high) - trajectory.quantile(low)
        except ForecastError:
            continue
        return np.asarray(spread, dtype="float64") * scale
    return None


def _reliability(terminal_move: float, per_bar_dispersion: float) -> float:
    """Return ``terminal_move / per_bar_dispersion``, never ``inf``.

    An unknown dispersion (``NaN``) yields ``NaN`` ("unknown"), a zero dispersion
    yields ``0.0`` ("no edge"), which is the convention of
    :meth:`trading_platform.forecast.types.ForecastTrajectory.reliability`.
    """
    if not math.isfinite(per_bar_dispersion):
        return float("nan")
    if per_bar_dispersion == 0.0:
        return 0.0
    return terminal_move / per_bar_dispersion


def _forecast_columns(
    index: pd.DatetimeIndex,
    close: np.ndarray,
    realized_vol: np.ndarray,
    store: ForecastStore | None,
    params: TimesFMForecastParams,
) -> dict[str, np.ndarray]:
    """Return every :data:`FORECAST_COLUMNS` value but ``exit_code``.

    The trajectory of an origin is decoded **once** and reused for every row of
    its window; rows no origin covers stay ``NaN``.
    """
    row_count = len(index)
    columns: dict[str, np.ndarray] = {
        name: np.full(row_count, np.nan, dtype="float64")
        for name in FORECAST_COLUMNS
        if name != "exit_code"
    }
    if store is None or row_count == 0:
        return columns

    frame_positions, origins = _origin_positions(index, store)
    for window in range(frame_positions.shape[0]):
        start = int(frame_positions[window])
        stop = (
            int(frame_positions[window + 1]) if window + 1 < frame_positions.shape[0] else row_count
        )
        ages = np.arange(start, stop, dtype="float64") - float(start)
        trajectory = store.trajectory(origins[window])
        median = trajectory.median.astype("float64")
        horizon = float(trajectory.horizon)

        columns["forecast_age"][start:stop] = ages
        for lookahead in ALPHA_LOOKAHEADS:
            columns[f"forecast_alpha_{lookahead}"][start:stop] = _alpha_window(
                median, ages, lookahead
            )
        columns[DECISION_COLUMN][start:stop] = _alpha_window(median, ages, params.entry_lookahead)

        terminal_move = float(median[-1])
        dispersion = _dispersion_path(trajectory)
        terminal_iqr = float("nan") if dispersion is None else float(dispersion[-1])
        per_bar = terminal_iqr / math.sqrt(horizon)
        path_length = float(np.sum(np.abs(np.diff(median, prepend=0.0)), dtype="float64"))
        columns["forecast_slope"][start:stop] = terminal_move / horizon
        columns["forecast_mfe"][start:stop] = float(np.max(median))
        columns["forecast_mae"][start:stop] = float(np.min(median))
        columns["forecast_path_eff"][start:stop] = (
            0.0 if path_length == 0.0 else terminal_move / path_length
        )
        columns["forecast_iqr_term"][start:stop] = terminal_iqr
        columns["forecast_iqr_per_bar"][start:stop] = per_bar
        columns["forecast_reliability"][start:stop] = _reliability(terminal_move, per_bar)
        columns["forecast_agreement"][start:stop] = trajectory.agreement()

    with np.errstate(divide="ignore", invalid="ignore"):
        columns["vol_ratio"] = _finite(columns["forecast_iqr_per_bar"] / realized_vol)
    return columns


def _edge_decay(alpha: np.ndarray, *, threshold: float, confirm: int) -> np.ndarray:
    """Return the rows where ``|alpha| < threshold`` for ``confirm`` rows in a row.

    A ``NaN`` alpha resets the streak: "unknown" is not "decayed".
    """
    result = np.zeros(alpha.shape[0], dtype=bool)
    streak = 0
    for position in range(alpha.shape[0]):
        value = float(alpha[position])
        if not math.isfinite(value):
            streak = 0
            continue
        streak = streak + 1 if abs(value) < threshold else 0
        result[position] = streak >= confirm
    return result


def _stale_mask(available: np.ndarray, age: np.ndarray, forecast_age: int) -> np.ndarray:
    """Return the rows where the forecast vanished or aged past its bound."""
    expired = np.asarray(np.isfinite(age) & (age > float(forecast_age)), dtype=bool)
    previous = np.zeros(age.shape[0], dtype=bool)
    if age.shape[0] > 1:
        previous[1:] = available[:-1]
    return np.asarray((previous & ~available) | expired, dtype=bool)


def _directional_exit_codes(
    columns: Mapping[str, np.ndarray],
    *,
    sign: float,
    params: TimesFMForecastParams,
    age: np.ndarray,
    available: np.ndarray,
    realised: np.ndarray,
    holding_limit: int,
) -> np.ndarray:
    """Return the ordered exit code of one direction (first match wins).

    ``sign`` is ``+1`` for a long and ``-1`` for a short: every *signed*
    diagnostic is read through it, so the short tree is the exact mirror of the
    long one (``forecast_alpha``, ``forecast_path_eff``, ``forecast_reliability``
    and the realised move are negated, and the favourable excursion becomes
    ``-forecast_mae``).
    """
    alpha = sign * columns[DECISION_COLUMN]
    path_efficiency = sign * columns["forecast_path_eff"]
    reliability = sign * columns["forecast_reliability"]
    excursion = columns["forecast_mfe"] if sign > 0.0 else -columns["forecast_mae"]
    direction_realised = sign * realised
    vol_ratio = columns["vol_ratio"]

    decay = _edge_decay(
        columns[DECISION_COLUMN], threshold=params.exit_alpha, confirm=params.exit_confirm
    )
    stale = _stale_mask(available, age, params.forecast_age)
    known = np.isfinite(alpha)

    rules: tuple[tuple[int, np.ndarray], ...] = (
        (
            EXIT_CODES["FORECAST_FLIP"],
            available & known & (alpha < 0.0) & (-alpha >= params.exit_alpha),
        ),
        (EXIT_CODES["EDGE_DECAY"], available & decay),
        (
            EXIT_CODES["TARGET_REACHED"],
            available
            & np.isfinite(excursion)
            & (excursion > 0.0)
            & np.isfinite(direction_realised)
            & (direction_realised >= params.target_capture * excursion)
            & known
            & (alpha <= params.exit_alpha),
        ),
        (
            EXIT_CODES["PATH_DEGRADED"],
            available
            & (
                (path_efficiency < params.min_path_efficiency_exit)
                | (reliability < params.min_reliability_exit)
            ),
        ),
        (
            EXIT_CODES["TIME_STOP"],
            available
            & (age - 1.0 >= float(holding_limit))
            & (direction_realised < params.min_progress * np.maximum(excursion, 0.0)),
        ),
        (
            EXIT_CODES["VOL_REGIME"],
            available & np.isfinite(vol_ratio) & (vol_ratio > params.exit_vol_ratio),
        ),
        (EXIT_CODES["STALE_FORECAST"], stale),
    )

    codes = np.zeros(alpha.shape[0], dtype="int8")
    assigned = np.zeros(alpha.shape[0], dtype=bool)
    # ``rules`` is ordered by priority: the first rule that matches a row owns it,
    # so the codes are written in that order and every later rule only fills the
    # rows no higher-priority rule has claimed.
    for code, condition in rules:
        take = condition & ~assigned
        codes[take] = np.int8(code)
        assigned |= take
    return codes


def _exit_codes(
    columns: Mapping[str, np.ndarray],
    *,
    params: TimesFMForecastParams,
    age: np.ndarray,
    available: np.ndarray,
    realised: np.ndarray,
    holding_limit: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Return the ordered exit codes of the long tree and of the short tree."""
    long_codes = _directional_exit_codes(
        columns,
        sign=1.0,
        params=params,
        age=age,
        available=available,
        realised=realised,
        holding_limit=holding_limit,
    )
    short_codes = _directional_exit_codes(
        columns,
        sign=-1.0,
        params=params,
        age=age,
        available=available,
        realised=realised,
        holding_limit=holding_limit,
    )
    return long_codes, short_codes


def _decision_state(
    columns: Mapping[str, np.ndarray],
    close: np.ndarray,
    params: TimesFMForecastParams,
    *,
    horizon: int,
) -> _DecisionState:
    """Compute the whole per-row decision state shared by ``prepare`` and ``signals``.

    ``columns`` must carry every diagnostic column of :data:`DIAGNOSTIC_COLUMNS`.
    ``horizon`` is the horizon of the active artifact (``0`` when the run has no
    artifact, which makes every row unavailable).
    """
    row_count = close.shape[0]
    age = np.asarray(columns["forecast_age"], dtype="float64")
    available = (
        np.isfinite(age)
        & (age <= float(horizon - 1 - params.min_lead))
        & (age <= float(params.forecast_age))
    )

    # The active origin row is recovered from the frame itself: prepare() wrote
    # ``forecast_age = t - origin_row``, so ``origin_row = t - forecast_age``.
    origin_rows = np.rint(np.arange(row_count, dtype="float64") - age)
    usable = np.isfinite(age) & (origin_rows >= 0.0) & (origin_rows < float(row_count))
    positions = np.where(usable, origin_rows, 0.0).astype("int64")
    with np.errstate(divide="ignore", invalid="ignore"):
        realised = np.log(close / close[positions])
    realised = _finite(np.where(usable, realised, np.nan))

    holding_limit = max(1, min(int(params.max_hold), int(horizon) - int(params.min_lead)))
    long_codes, short_codes = _exit_codes(
        columns,
        params=params,
        age=age,
        available=available,
        realised=realised,
        holding_limit=holding_limit,
    )
    if not params.allow_short:
        short_codes = np.zeros(row_count, dtype="int8")
    return _DecisionState(
        age=age,
        available=available,
        realised=realised,
        long_codes=long_codes,
        short_codes=short_codes,
    )


def _combined_codes(state: _DecisionState) -> np.ndarray:
    """Return the ``exit_code`` column: the long reason, else the short reason, else ``0``."""
    codes = state.long_codes.astype("int8", copy=True)
    take = (codes == 0) & (state.short_codes != 0)
    codes[take] = state.short_codes[take]
    return codes


def _cooldown_block(exit_code: np.ndarray, cooldown: int) -> np.ndarray:
    """Return the rows blocked by the cooldown that follows a losing exit.

    ``cooldown_block[t]`` is ``True`` when ``exit_code`` at any row in
    ``[t - cooldown, t]`` belongs to :data:`LOSS_EXIT_CODES`.  ``cooldown == 0``
    disables the mask entirely — it never degrades into a one-row mask that would
    block the entry candle itself.
    """
    losses = np.isin(exit_code.astype("int64", copy=False), np.fromiter(LOSS_EXIT_CODES, "int64"))
    if cooldown <= 0:
        return np.zeros(exit_code.shape[0], dtype=bool)
    block = losses.copy()
    for shift in range(1, cooldown + 1):
        block[shift:] |= losses[:-shift]
    return block


def _entry_gates(
    columns: Mapping[str, np.ndarray],
    state: _DecisionState,
    params: TimesFMForecastParams,
) -> _EntryGates:
    """Return the AND-composed entry gates of both directions.

    Every gate is ``False`` on a row carrying no usable forecast, and ``False``
    wherever the diagnostic it reads is ``NaN`` — a missing input is never a
    reason to trade.  The short gates read the same expressions on the
    *favourable* sign convention (``-forecast_alpha``, ``-forecast_path_eff``,
    ``-forecast_reliability``), so they are the exact mirror of the long ones.
    """
    alpha = columns[DECISION_COLUMN]
    atr_pct = columns["atr_pct"]
    atr_percentile = columns["atr_percentile"]
    agreement = columns["forecast_agreement"]
    path_efficiency = columns["forecast_path_eff"]
    reliability = columns["forecast_reliability"]
    vol_ratio = columns["vol_ratio"]
    oscillator = columns["rsi"]
    age = state.age

    fresh = state.available & np.isfinite(age) & (age >= 1.0)
    known = np.isfinite(alpha)
    significance = np.isfinite(atr_pct) & (np.abs(alpha) >= params.alpha_vs_atr * atr_pct)
    agreement_ok = np.isfinite(agreement) & (agreement >= params.min_agreement)
    volatility_ok = np.isfinite(vol_ratio) & (vol_ratio <= params.max_vol_ratio)
    atr_window_ok = (
        np.isfinite(atr_percentile)
        & (atr_percentile >= params.min_atr_percentile)
        & (atr_percentile <= params.max_atr_percentile)
    )
    if params.rsi_min > 0.0 or params.rsi_max < 100.0:
        oscillator_ok = (
            np.isfinite(oscillator) & (oscillator > params.rsi_min) & (oscillator < params.rsi_max)
        )
    else:
        oscillator_ok = np.ones(alpha.shape[0], dtype=bool)
    cooldown_ok = ~_cooldown_block(columns["exit_code"], params.cooldown)

    def _side(sign: float) -> np.ndarray:
        """AND-compose the gates of one direction (exact mirror through ``sign``)."""
        edge = sign * alpha
        gates = (
            fresh
            & known
            & (edge > 0.0)
            & (edge >= params.min_alpha)
            & np.isfinite(path_efficiency)
            & (sign * path_efficiency >= params.min_path_efficiency)
            & np.isfinite(reliability)
            & (sign * reliability >= params.min_reliability)
            & significance
            & agreement_ok
            & volatility_ok
            & atr_window_ok
            & oscillator_ok
            & cooldown_ok
        )
        return np.asarray(gates, dtype=bool)

    long_entries = _side(1.0)
    short_entries = _side(-1.0) if params.allow_short else np.zeros(alpha.shape[0], dtype=bool)
    if alpha.shape[0] > 0:
        long_entries[0] = False
        short_entries[0] = False
    return _EntryGates(long=long_entries, short=short_entries)
