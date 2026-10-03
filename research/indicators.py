"""Shared indicator helpers for the research strategies.

Everything here is deliberately pandas/NumPy + ``talib.abstract`` and operates on
a frame indexed by candle open time, which is what both the freqtrade strategy
files and the research harness expect.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import talib.abstract as ta


def as_talib(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    """talib.abstract expects plain arrays keyed by field name."""
    return {
        "open": frame["open"].to_numpy(dtype=float),
        "high": frame["high"].to_numpy(dtype=float),
        "low": frame["low"].to_numpy(dtype=float),
        "close": frame["close"].to_numpy(dtype=float),
        "volume": frame["volume"].to_numpy(dtype=float),
    }


def series(values, index: pd.Index) -> pd.Series:
    return pd.Series(np.asarray(values, dtype=float), index=index)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average true range, in price units."""
    return series(ta.ATR(as_talib(frame), timeperiod=period), frame.index)


def atr_pct(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR as a fraction of the close -- comparable across price levels."""
    return atr(frame, period) / frame["close"]


def ema(frame: pd.DataFrame, period: int) -> pd.Series:
    return series(ta.EMA(as_talib(frame), timeperiod=period), frame.index)


def sma(frame: pd.DataFrame, period: int) -> pd.Series:
    return series(ta.SMA(as_talib(frame), timeperiod=period), frame.index)


def rsi(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    return series(ta.RSI(as_talib(frame), timeperiod=period), frame.index)


def adx(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    return series(ta.ADX(as_talib(frame), timeperiod=period), frame.index)


def roc(frame: pd.DataFrame, period: int = 12) -> pd.Series:
    return series(ta.ROC(as_talib(frame), timeperiod=period), frame.index)


def realised_vol(frame: pd.DataFrame, period: int = 24) -> pd.Series:
    """Rolling standard deviation of log returns, per candle."""
    logret = np.log(frame["close"] / frame["close"].shift(1))
    return logret.rolling(period).std()


def donchian(frame: pd.DataFrame, period: int) -> tuple[pd.Series, pd.Series]:
    """Previous-``period`` highest high and lowest low, shifted to avoid look-ahead."""
    high = frame["high"].rolling(period).max().shift(1)
    low = frame["low"].rolling(period).min().shift(1)
    return high, low


def kama(frame: pd.DataFrame, period: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    """Kaufman adaptive moving average.

    The smoothing constant adapts to the efficiency ratio (net move / path
    length), so the average tracks quickly in a trend and slowly in noise. This is
    the property the classic EMA filter lacks, and it is what makes KAMA a better
    trend gate for a noisy crypto series.
    """
    close = frame["close"]
    change = (close - close.shift(period)).abs()
    volatility = close.diff().abs().rolling(period).sum()
    er = (change / volatility).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    fast_sc = 2.0 / (fast + 1.0)
    slow_sc = 2.0 / (slow + 1.0)
    sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
    out = np.full(len(close), np.nan)
    values = close.to_numpy(dtype=float)
    sc_values = sc.to_numpy(dtype=float)
    start = period
    if start < len(values):
        out[start] = values[start]
        for i in range(start + 1, len(values)):
            out[i] = out[i - 1] + sc_values[i] * (values[i] - out[i - 1])
    return pd.Series(out, index=frame.index)


def supertrend(frame: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    """Return the Supertrend direction (+1 / -1) per candle."""
    hl2 = ((frame["high"] + frame["low"]) / 2.0).to_numpy(dtype=float)
    atr_values = atr(frame, period).to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)
    n = len(frame)
    upper = hl2 + mult * atr_values
    lower = hl2 - mult * atr_values
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    direction = np.ones(n)
    for i in range(1, n):
        if np.isnan(atr_values[i]) or np.isnan(atr_values[i - 1]):
            continue
        prev_upper = final_upper[i - 1]
        prev_lower = final_lower[i - 1]
        if np.isnan(prev_upper):
            prev_upper, prev_lower = upper[i - 1], lower[i - 1]
        final_upper[i] = (
            upper[i] if (upper[i] < prev_upper or close[i - 1] > prev_upper) else prev_upper
        )
        final_lower[i] = (
            lower[i] if (lower[i] > prev_lower or close[i - 1] < prev_lower) else prev_lower
        )
        if close[i] > final_upper[i]:
            direction[i] = 1
        elif close[i] < final_lower[i]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
    return pd.Series(direction, index=frame.index)


def rolling_percentile(series_in: pd.Series, window: int) -> pd.Series:
    """Percentile rank (0-1) of the latest value within the trailing ``window``."""
    return series_in.rolling(window).apply(
        lambda window_values: (window_values[-1] > window_values[:-1]).mean(), raw=True
    )
