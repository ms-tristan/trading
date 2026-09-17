"""Drawdown statistics computed from an equity curve.

Everything in this module is pure ``pandas``/``numpy``: it never touches the
filesystem, the network or any other layer of the package (only
:mod:`trading_backtest.core` imports are allowed here).

Conventions (frozen by the architecture contract):

* a drawdown value is ``equity / equity.cummax() - 1``: it is ``0.0`` on a new
  high and strictly negative while the curve is under water;
* a candle is *under water* only when its equity is **strictly** below the
  running peak, so a flat curve is never under water;
* degenerate inputs (empty curve, zero or negative equity) never raise: the
  result stays finite and ``<= 0`` everywhere.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

__all__ = [
    "drawdown_duration",
    "drawdown_series",
    "drawdown_table",
    "max_drawdown",
]

#: Name of the returned series, purely cosmetic (kept deterministic for tests).
DRAWDOWN_NAME = "drawdown"


def _finite(value: float) -> float:
    """Return ``value`` when it is finite, ``0.0`` otherwise (never NaN/inf)."""
    return value if math.isfinite(value) else 0.0


def _as_float_series(equity: pd.Series) -> pd.Series:
    """Return a ``float64`` view of ``equity`` without mutating the input."""
    if isinstance(equity, pd.Series):
        return equity.astype("float64")
    return pd.Series(equity, dtype="float64")


def drawdown_series(equity: pd.Series) -> pd.Series:
    """Return the drawdown curve ``equity / equity.cummax() - 1``.

    Parameters
    ----------
    equity:
        Equity curve (any numeric series).  An empty series yields an empty
        ``float64`` series with the very same index.

    Returns
    -------
    pandas.Series
        ``float64`` series aligned on ``equity.index`` whose values are all
        ``<= 1e-12`` (in practice ``<= 0``).  A non-positive equity curve is
        accepted: the drawdown is reported as ``0.0`` there because the ratio
        against a non-positive peak carries no meaning.
    """
    series = _as_float_series(equity)
    if series.empty:
        return pd.Series(
            np.empty(0, dtype="float64"),
            index=series.index,
            name=DRAWDOWN_NAME,
            dtype="float64",
        )
    values = series.to_numpy(dtype="float64")
    peak = series.cummax().to_numpy(dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        safe_peak = np.where(peak > 0.0, peak, np.nan)
        raw = values / safe_peak - 1.0
    drawdown = np.where(np.isfinite(raw), np.minimum(raw, 0.0), 0.0)
    return pd.Series(drawdown, index=series.index, name=DRAWDOWN_NAME, dtype="float64")


def max_drawdown(equity: pd.Series) -> float:
    """Return the deepest drawdown of ``equity`` (``<= 0``), ``0.0`` when empty."""
    series = drawdown_series(equity)
    if series.empty:
        return 0.0
    return _finite(float(series.min()))


def _underwater_positions(series: pd.Series) -> np.ndarray:
    """Positions of the candles strictly below their running peak."""
    values = series.to_numpy(dtype="float64")
    peak = series.cummax().to_numpy(dtype="float64")
    return np.flatnonzero(values < peak)


def _runs(positions: np.ndarray) -> list[np.ndarray]:
    """Split ascending positions into groups of consecutive indices."""
    if positions.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(positions) != 1) + 1
    return list(np.split(positions, breaks))


def drawdown_duration(equity: pd.Series) -> int:
    """Return the longest run of consecutive candles strictly below a running peak.

    ``0`` is returned for an empty curve or a curve that never goes under water
    (including a flat curve).
    """
    series = _as_float_series(equity)
    if series.empty:
        return 0
    runs = _runs(_underwater_positions(series))
    if not runs:
        return 0
    return int(max(len(run) for run in runs))


def drawdown_table(equity: pd.Series, *, top: int = 5) -> list[dict[str, float | str]]:
    """Return the ``top`` deepest drawdown episodes of ``equity``.

    Each episode is a mapping with the keys ``start``, ``trough``, ``end``
    (ISO-8601 timestamps), ``depth`` (``<= 0``), ``duration_minutes`` (from
    ``start`` to ``end``) and ``recovery_minutes`` (from ``trough`` to ``end``).

    An episode starts on the first candle under water, reaches its lowest point
    at ``trough`` and ends on the candle that recovers the running peak.  When
    the curve never recovers, ``end`` is the last candle of the curve and
    ``recovery_minutes`` measures the time elapsed since the trough without a
    recovery.  Episodes are ordered by ``depth`` ascending (deepest first) and
    ties are broken by ``start``, which makes the output fully deterministic.
    """
    series = _as_float_series(equity)
    limit = int(top)
    if series.empty or limit <= 0:
        return []
    values = series.to_numpy(dtype="float64")
    peak = series.cummax().to_numpy(dtype="float64")
    index = pd.DatetimeIndex(series.index)
    minute = pd.Timedelta(minutes=1)
    episodes: list[dict[str, float | str]] = []
    for run in _runs(_underwater_positions(series)):
        start_position = int(run[0])
        peak_value = float(peak[start_position])
        trough_position = int(run[int(np.argmin(values[run]))])
        recovered = np.flatnonzero(values[trough_position + 1 :] >= peak_value)
        end_position = (
            trough_position + 1 + int(recovered[0]) if recovered.size else len(values) - 1
        )
        depth = 0.0
        if peak_value > 0.0:
            depth = min(float(values[trough_position]) / peak_value - 1.0, 0.0)
        episodes.append(
            {
                "start": index[start_position].isoformat(),
                "trough": index[trough_position].isoformat(),
                "end": index[end_position].isoformat(),
                "depth": _finite(depth),
                "duration_minutes": float((index[end_position] - index[start_position]) / minute),
                "recovery_minutes": float((index[end_position] - index[trough_position]) / minute),
            }
        )
    episodes.sort(key=lambda episode: (float(episode["depth"]), str(episode["start"])))
    return episodes[:limit]
