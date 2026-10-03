"""Faithful re-implementations of the shipped strategies, plus shared indictors.

The point of this module is reproducibility: each ``sig_*`` function reproduces the
signal logic of the corresponding file in ``user_data/strategies/`` so that the
research harness can measure the *original* rules on long history, not only on the
few days the live paper ledger has been running.

These functions return a frame with the same index as the input and two boolean
columns, ``enter_long`` / ``exit_long``. They are pure signal generators: sizing,
stops and ROI are applied by the research driver so that every candidate is
compared under one cost model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import talib.abstract as ta


def _ohlcv(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    """talib.abstract wants plain arrays keyed by field name."""
    return {
        "open": frame["open"].to_numpy(dtype=float),
        "high": frame["high"].to_numpy(dtype=float),
        "low": frame["low"].to_numpy(dtype=float),
        "close": frame["close"].to_numpy(dtype=float),
        "volume": frame["volume"].to_numpy(dtype=float),
    }


def _empty_like(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"enter_long": False, "exit_long": False}, index=frame.index)


def true_range(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR as a pandas Series aligned to ``frame``."""
    return pd.Series(ta.ATR(_ohlcv(frame), timeperiod=period), index=frame.index)


def realised_vol(frame: pd.DataFrame, period: int = 24) -> pd.Series:
    """Rolling standard deviation of log returns, in return units per candle."""
    logret = np.log(frame["close"] / frame["close"].shift(1))
    return logret.rolling(period).std()


# ---------------------------------------------------------------------------
# Original shipped strategies
# ---------------------------------------------------------------------------
def sig_basic(frame: pd.DataFrame) -> pd.DataFrame:
    """EMA(20)/EMA(50) cross up with RSI(14) < 70; exit on cross down or RSI > 78."""
    out = _empty_like(frame)
    data = _ohlcv(frame)
    ema_fast = pd.Series(ta.EMA(data, timeperiod=20), index=frame.index)
    ema_slow = pd.Series(ta.EMA(data, timeperiod=50), index=frame.index)
    rsi = pd.Series(ta.RSI(data, timeperiod=14), index=frame.index)

    cross_up = (ema_fast > ema_slow) & (ema_fast.shift(1) <= ema_slow.shift(1)) & (rsi < 70)
    cross_down = (ema_fast < ema_slow) & (ema_fast.shift(1) >= ema_slow.shift(1))
    out["enter_long"] = cross_up & (frame["volume"] > 0)
    out["exit_long"] = (cross_down | (rsi > 78)) & (frame["volume"] > 0)
    return out


def sig_donchian(frame: pd.DataFrame) -> pd.DataFrame:
    """Break the previous 20-candle high; exit below the 10-candle low or chandelier."""
    out = _empty_like(frame)
    data = _ohlcv(frame)
    high = pd.Series(data["high"], index=frame.index)
    low = pd.Series(data["low"], index=frame.index)
    donchian_high = high.rolling(20).max().shift(1)
    donchian_low = low.rolling(10).min().shift(1)
    atr = true_range(frame, 14)
    chandelier = frame["close"].rolling(20).max().shift(1) - 2.0 * atr

    out["enter_long"] = (frame["close"] > donchian_high) & (frame["volume"] > 0)
    out["exit_long"] = ((frame["close"] < donchian_low) | (frame["close"] < chandelier)) & (
        frame["volume"] > 0
    )
    return out


def sig_dual_thrust(frame: pd.DataFrame, window: int = 4, k: float = 0.5) -> pd.DataFrame:
    """Break above open + k * previous-range; exit below open - k * range."""
    out = _empty_like(frame)
    data = _ohlcv(frame)
    high = pd.Series(data["high"], index=frame.index)
    low = pd.Series(data["low"], index=frame.index)
    close = pd.Series(data["close"], index=frame.index)

    hh = high.rolling(window).max().shift(1)
    ll = low.rolling(window).min().shift(1)
    hc = close.rolling(window).max().shift(1)
    lc = close.rolling(window).min().shift(1)
    rng = np.maximum(hh - lc, hc - ll)
    buy_line = frame["open"] + k * rng
    sell_line = frame["open"] - k * rng

    out["enter_long"] = (frame["close"] > buy_line) & (frame["volume"] > 0)
    out["exit_long"] = (frame["close"] < sell_line) & (frame["volume"] > 0)
    return out


def sig_keltner(frame: pd.DataFrame) -> pd.DataFrame:
    """Close above EMA(20)+2*ATR(10) while EMA(50) > EMA(200); exit below the middle."""
    out = _empty_like(frame)
    data = _ohlcv(frame)
    close = frame["close"]
    middle = pd.Series(ta.EMA(data, timeperiod=20), index=frame.index)
    atr = true_range(frame, 10)
    upper = middle + 2.0 * atr
    ema_fast = pd.Series(ta.EMA(data, timeperiod=50), index=frame.index)
    ema_slow = pd.Series(ta.EMA(data, timeperiod=200), index=frame.index)

    out["enter_long"] = (close > upper) & (ema_fast > ema_slow) & (frame["volume"] > 0)
    out["exit_long"] = (close < middle) & (frame["volume"] > 0)
    return out


def sig_macd(frame: pd.DataFrame) -> pd.DataFrame:
    """MACD histogram cross above zero while close > EMA(200); exit on cross down."""
    out = _empty_like(frame)
    data = _ohlcv(frame)
    macd = ta.MACD(data, fastperiod=12, slowperiod=26, signalperiod=9)
    # talib.abstract returns a positional tuple: (macd, signal, histogram).
    hist = pd.Series(macd[2], index=frame.index)
    ema_slow = pd.Series(ta.EMA(data, timeperiod=200), index=frame.index)

    cross_up = (hist > 0) & (hist.shift(1) <= 0)
    cross_down = (hist < 0) & (hist.shift(1) >= 0)
    out["enter_long"] = cross_up & (frame["close"] > ema_slow) & (frame["volume"] > 0)
    out["exit_long"] = cross_down & (frame["volume"] > 0)
    return out


def sig_rsi_reversion(frame: pd.DataFrame) -> pd.DataFrame:
    """RSI crosses back above 30 while close > SMA(200); exit RSI > 65."""
    out = _empty_like(frame)
    data = _ohlcv(frame)
    rsi = pd.Series(ta.RSI(data, timeperiod=14), index=frame.index)
    sma_slow = pd.Series(ta.SMA(data, timeperiod=200), index=frame.index)

    recovery = (rsi > 30) & (rsi.shift(1) <= 30)
    out["enter_long"] = recovery & (frame["close"] > sma_slow) & (frame["volume"] > 0)
    out["exit_long"] = (rsi > 65) & (frame["volume"] > 0)
    return out


def sig_bollinger(frame: pd.DataFrame) -> pd.DataFrame:
    """Lower band touch with RSI < 40 above SMA(200); exit at the middle band or RSI > 65."""
    out = _empty_like(frame)
    data = _ohlcv(frame)
    bb = ta.BBANDS(data, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
    # talib.abstract returns a positional tuple: (upper, middle, lower).
    lower = pd.Series(bb[2], index=frame.index)
    middle = pd.Series(bb[1], index=frame.index)
    rsi = pd.Series(ta.RSI(data, timeperiod=14), index=frame.index)
    sma_slow = pd.Series(ta.SMA(data, timeperiod=200), index=frame.index)

    touch = (frame["low"] <= lower) & (rsi < 40) & (frame["close"] > sma_slow)
    out["enter_long"] = touch & (frame["volume"] > 0)
    out["exit_long"] = ((frame["close"] >= middle) | (rsi > 65)) & (frame["volume"] > 0)
    return out


def sig_momentum(frame: pd.DataFrame) -> pd.DataFrame:
    """ROC(12) > 0 and EMA(50) > EMA(200) and ADX > 20; exit on trend or impulse loss."""
    out = _empty_like(frame)
    data = _ohlcv(frame)
    roc = pd.Series(ta.ROC(data, timeperiod=12), index=frame.index)
    ema_fast = pd.Series(ta.EMA(data, timeperiod=50), index=frame.index)
    ema_slow = pd.Series(ta.EMA(data, timeperiod=200), index=frame.index)
    adx = pd.Series(ta.ADX(data, timeperiod=14), index=frame.index)

    out["enter_long"] = (roc > 0) & (ema_fast > ema_slow) & (adx > 20) & (frame["volume"] > 0)
    out["exit_long"] = ((ema_fast < ema_slow) | (roc < 0)) & (frame["volume"] > 0)
    return out


def supertrend_direction(frame: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    """Return +1 / -1 per candle for the Supertrend indicator (recursive bands)."""
    hl2 = ((frame["high"] + frame["low"]) / 2.0).to_numpy(dtype=float)
    atr = true_range(frame, period).to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)
    n = len(frame)

    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    direction = np.ones(n)

    for i in range(1, n):
        if np.isnan(atr[i]) or np.isnan(atr[i - 1]):
            continue
        prev_upper = final_upper[i - 1]
        prev_lower = final_lower[i - 1]
        if np.isnan(prev_upper):
            prev_upper = upper[i - 1]
            prev_lower = lower[i - 1]
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


def sig_supertrend(frame: pd.DataFrame) -> pd.DataFrame:
    """Enter on a flip to +1, exit on a flip to -1."""
    out = _empty_like(frame)
    direction = supertrend_direction(frame)
    flip_up = (direction > 0) & (direction.shift(1) < 0)
    flip_down = (direction < 0) & (direction.shift(1) > 0)
    out["enter_long"] = flip_up & (frame["volume"] > 0)
    out["exit_long"] = flip_down & (frame["volume"] > 0)
    return out


def sig_faber(frame: pd.DataFrame) -> pd.DataFrame:
    """Hold while close > SMA(200); exit when it closes back below."""
    out = _empty_like(frame)
    data = _ohlcv(frame)
    sma_slow = pd.Series(ta.SMA(data, timeperiod=200), index=frame.index)
    above = frame["close"] > sma_slow
    out["enter_long"] = above & (frame["volume"] > 0)
    out["exit_long"] = (~above) & (frame["volume"] > 0)
    return out


#: The catalogue: id -> signal function, mirroring ``config/strategies.json``.
ORIGINALS: dict[str, object] = {
    "basic": sig_basic,
    "momentum": sig_momentum,
    "rsi-reversion": sig_rsi_reversion,
    "bollinger": sig_bollinger,
    "macd": sig_macd,
    "donchian": sig_donchian,
    "keltner": sig_keltner,
    "supertrend": sig_supertrend,
    "dual-thrust": sig_dual_thrust,
    "faber": sig_faber,
}

#: stoploss / minimal_roi of each original, as shipped in the strategy files.
ORIGINAL_RISK: dict[str, tuple[float, float | None]] = {
    "basic": (-0.10, 0.08),
    "momentum": (-0.10, 0.12),
    "rsi-reversion": (-0.10, 0.06),
    "bollinger": (-0.10, 0.05),
    "macd": (-0.10, 0.10),
    "donchian": (-0.08, 0.20),
    "keltner": (-0.10, 0.15),
    "supertrend": (-0.10, 0.10),
    "dual-thrust": (-0.06, 0.03),
    "faber": (-0.25, None),
}
