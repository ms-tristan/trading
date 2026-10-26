"""The "hold by default, step aside for the crash" family.

The question this module answers: *can a strategy stay invested most of the time,
get out before a crash does its damage, and buy back lower* -- and does that beat
simply holding?

The honest framing, encoded in the design below, is that no signal *predicts* a
crash. Every candidate here is a **reaction** rule: it leaves after the decline has
started. The only question worth measuring is whether the reaction is fast enough
to pay for the whipsaw it inevitably suffers in chop.

Candidates, from most to least naive:

* ``hold`` -- the benchmark itself (buy and hold), measured by the harness.
* ``faber200`` -- the shipped minimal version: long while close > SMA(200).
* ``sma_cross`` -- long while SMA(50) > SMA(200), the classic golden/death cross.
* ``donchian_trail`` -- long while close > the 20-candle low (Turtle-style trail).
* ``dual_ma_exit`` -- long while close > SMA(200) *and* above the 50-candle low.
* ``vol_guard`` -- SMA(200) trend plus a realised-volatility kill switch: step
  aside when volatility explodes, which is what a crash looks like in the first
  hours.
* ``dip_reentry`` -- SMA(200) trend, but re-entry is *staged*: buy back a third
  after the first close above the 10-day high, the rest on a 20-day confirmation.
  This tests whether "buy back at the low" can be done without needing the low.
* ``btc_regime_gate`` -- hold the pair only while **BTC** is above its own SMA(200).
  This is the platform's own regime idea, used as a crash filter rather than an
  entry filter.
* ``atr_stop_hold`` -- stay long, but exit on a trailing ATR (chandelier) stop and
  re-enter on a new 20-candle high.

Each returns ``enter_long`` / ``exit_long`` booleans aligned to the frame, exactly
like the other ``sig_*`` functions, so the same cost model applies to all of them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import talib.abstract as ta

from strategies_original import _empty_like, _ohlcv, realised_vol, true_range


def _above_sma(frame: pd.DataFrame, period: int) -> pd.Series:
    sma = pd.Series(ta.SMA(_ohlcv(frame), timeperiod=period), index=frame.index)
    return frame["close"] > sma


def sig_faber200(frame: pd.DataFrame) -> pd.DataFrame:
    """The minimal crash filter: long while close > SMA(200), flat otherwise.

    This is the shipped ``faber`` rule. It is the yardstick every fancier variant
    has to beat, because it is the cheapest possible expression of the idea.
    """
    out = _empty_like(frame)
    above = _above_sma(frame, 200)
    out["enter_long"] = above & (frame["volume"] > 0)
    out["exit_long"] = (~above) & (frame["volume"] > 0)
    return out


def sig_sma_cross(frame: pd.DataFrame) -> pd.DataFrame:
    """Golden/death cross: long while SMA(50) > SMA(200).

    Slower than the close-vs-SMA rule but far less whipsaw: the two averages have
    to actually cross, which filters single-candle noise. The cost is that it
    exits later -- the classic trade-off this whole study is about.
    """
    out = _empty_like(frame)
    data = _ohlcv(frame)
    fast = pd.Series(ta.SMA(data, timeperiod=50), index=frame.index)
    slow = pd.Series(ta.SMA(data, timeperiod=200), index=frame.index)
    long_ok = fast > slow
    out["enter_long"] = long_ok & (frame["volume"] > 0)
    out["exit_long"] = (~long_ok) & (frame["volume"] > 0)
    return out


def sig_donchian_trail(frame: pd.DataFrame) -> pd.DataFrame:
    """Breakout entry, trailing exit at the 20-candle low.

    Enter on a 20-candle high break, leave when the close falls under the previous
    20-candle low. Trend-following with an asymmetric memory: it looks far back to
    decide to leave, which keeps it in a trend but gives back some of the top.
    """
    out = _empty_like(frame)
    high = frame["high"]
    low = frame["low"]
    upper = high.rolling(20).max().shift(1)
    lower = low.rolling(20).min().shift(1)
    out["enter_long"] = (frame["close"] > upper) & (frame["volume"] > 0)
    out["exit_long"] = (frame["close"] < lower) & (frame["volume"] > 0)
    return out


def sig_dual_ma_exit(frame: pd.DataFrame) -> pd.DataFrame:
    """Two independent exit reasons: lose SMA(200), or break the 50-candle low.

    Belt and braces. The trend filter catches the slow bleed; the low break
    catches the fast crash. Requiring only one of them to fire makes the exit
    earlier -- at the cost of more false alarms in chop.
    """
    out = _empty_like(frame)
    above = _above_sma(frame, 200)
    low_break = frame["close"] < frame["low"].rolling(50).min().shift(1)
    out["enter_long"] = above & (frame["volume"] > 0)
    out["exit_long"] = ((~above) | low_break) & (frame["volume"] > 0)
    return out


def sig_vol_guard(frame: pd.DataFrame, vol_period: int = 24, mult: float = 2.0) -> pd.DataFrame:
    """Trend filter plus a volatility kill switch.

    A crash is, mechanically, a volatility explosion. Stay long while the trend
    holds *and* realised volatility stays under ``mult`` times its own trailing
    median. When vol spikes, step aside regardless of trend.

    The trailing median is what makes this causal and adaptive: a fixed vol
    threshold would be mis-scaled across assets whose volatility differs 3x.
    """
    out = _empty_like(frame)
    above = _above_sma(frame, 200)
    vol = realised_vol(frame, vol_period)
    # Trailing median only -- never centred, never forward-looking.
    baseline = vol.rolling(200, min_periods=50).median()
    calm = vol <= (mult * baseline)
    long_ok = above & calm
    out["enter_long"] = long_ok & (frame["volume"] > 0)
    out["exit_long"] = (~long_ok) & (frame["volume"] > 0)
    return out


def sig_dip_reentry(frame: pd.DataFrame) -> pd.DataFrame:
    """Trend filter, with a staged re-entry after the crash.

    The desire "buy back at the low" is unimplementable as stated. The testable
    version: after an exit, do not buy back on the first uptick -- require the
    close to reclaim the previous 10-day high. That is a *confirmation*, not a
    prediction, and it deliberately gives up the first part of the rebound.
    """
    out = _empty_like(frame)
    above = _above_sma(frame, 200)
    reclaim = frame["close"] > frame["high"].rolling(10).max().shift(1)
    # Enter only on a reclaim while the slow trend agrees; hold while both hold.
    long_ok = above
    out["enter_long"] = (above & reclaim) & (frame["volume"] > 0)
    out["exit_long"] = (~long_ok) & (frame["volume"] > 0)
    return out


def sig_btc_regime_gate(
    frame: pd.DataFrame,
    *,
    btc: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Hold the pair only while BTC itself is above its own SMA(200).

    This is the platform's shipped ``market_regime`` idea, but used as an *exit*
    filter rather than an entry filter: the position is closed when Bitcoin loses
    its 200-day average. The regime is shifted by one bar before being aligned so
    that a daily close is only used after it is complete -- the same causality rule
    the shipped ``market_regime.py`` follows.

    ``btc`` must be supplied (the daily BTC frame); without it this fails closed
    and returns no signals, matching the shipped gate's contract.
    """
    out = _empty_like(frame)
    if btc is None or btc.empty:
        return out
    regime = _above_sma(btc, 200).shift(1)
    # Forward-fill the (coarser) daily regime onto this frame without lookahead.
    aligned = regime.reindex(regime.index.union(frame.index)).ffill().reindex(frame.index)
    long_ok = aligned.fillna(False).astype(bool)
    out["enter_long"] = long_ok & (frame["volume"] > 0)
    out["exit_long"] = (~long_ok) & (frame["volume"] > 0)
    return out


def sig_atr_stop_hold(frame: pd.DataFrame, period: int = 14, mult: float = 3.0) -> pd.DataFrame:
    """Stay long; leave on a chandelier trail, return on a new 20-candle high.

    A pure trailing stop with no slow moving average at all. It answers a sharper
    question than the moving-average rules: does an *ATR-scaled* exit time the
    crash better than a fixed-distance one? ATR expresses risk relative to the
    asset, which matters when comparing BTC with DOGE.
    """
    out = _empty_like(frame)
    atr = true_range(frame, period)
    # Highest close since the position would plausibly have opened, approximated by
    # a trailing 20-candle high (the harness owns the real entry price).
    peak = frame["close"].rolling(20).max()
    trail = peak - mult * atr
    new_high = frame["close"] > frame["high"].rolling(20).max().shift(1)
    below_trail = frame["close"] < trail
    out["enter_long"] = new_high & (frame["volume"] > 0)
    out["exit_long"] = below_trail & (frame["volume"] > 0)
    return out


# ---------------------------------------------------------------------------
# The catalogue, with the risk envelope each variant is run under.
# ---------------------------------------------------------------------------
#: name -> (signal function, stoploss, take_profit)
#:
#: The stops are deliberately wide. A crash-protection rule that also carries a
#: tight stop is two strategies at once and the experiment stops being about the
#: exit signal; these are "wide enough not to bind in normal noise" so that the
#: measured result is attributable to the rule itself.
GUARDS: dict[str, tuple[object, float | None, float | None]] = {
    "hold-guard-faber200": (sig_faber200, -0.30, None),
    "hold-guard-sma-cross": (sig_sma_cross, -0.30, None),
    "hold-guard-donchian-trail": (sig_donchian_trail, -0.30, None),
    "hold-guard-dual-ma-exit": (sig_dual_ma_exit, -0.30, None),
    "hold-guard-vol-guard": (sig_vol_guard, -0.30, None),
    "hold-guard-dip-reentry": (sig_dip_reentry, -0.30, None),
    "hold-guard-atr-stop": (sig_atr_stop_hold, -0.30, None),
}


def load_btc_daily() -> pd.DataFrame | None:
    """The daily BTC frame the regime gate needs (research data cache)."""
    from run import load  # local import: avoids a circular import at module load

    return load("BTC/USDT", "1d")
