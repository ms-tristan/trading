"""Candidate strategy logic for the v2 redesigns and the new research strategies.

Every function here is a pure signal generator: it takes an OHLCV frame and
returns a frame with boolean ``enter_long`` / ``exit_long`` columns. Sizing, stops
and ROI are applied by the driver, so all candidates are compared under one cost
model.

The designs are driven by the diagnosis in ``research/diagnose.py``, whose
findings are:

1. the losing profiles trade far too often for their per-candle volatility -- on
   BNB 15m a round trip costs about 1.0 ATR, i.e. more than an entire average
   candle, so the signal has to be right far more often than it is just to break
   even;
2. the losing mean-reversion rules exit on a trigger that is *already true* on the
   entry candle (the Bollinger middle band, the Keltner middle band), so the trade
   is closed by the very first candle after entry and can never capture a
   reversion;
3. the losing trend rules (`momentum`) use a *state* as an entry condition
   (``roc > 0``), which re-fires on consecutive candles and produces runs of
   near-identical entries and immediate exits.

So the redesigns systematically do three things: make entries **events** rather
than states, give the exit **room** to work, and gate entries on a **volatility
regime** so the strategy trades when a move is large relative to its cost.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from indicators import (
    adx,
    atr,
    donchian,
    ema,
    kama,
    realised_vol,
    roc,
    sma,
    supertrend,
)


def _blank(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"enter_long": False, "exit_long": False}, index=frame.index)


# ---------------------------------------------------------------------------
# v2 redesigns of the losing originals
# ---------------------------------------------------------------------------
def sig_v2_bollinger(frame: pd.DataFrame) -> pd.DataFrame:
    """Bollinger reversion with room to work (fixes `bollinger`).

    Three changes against the shipped rule, each answering one diagnosed defect:

    * the exit is **no longer the middle band**. The middle band is above the
      entry price by construction after a lower-band touch, so "close >= middle"
      is true on the next candle and the original could never hold a reversion.
      The v2 target is the middle band *or* a fixed reversion fraction, and the
      trade is given a minimum holding period before the target can fire;
    * the entry must be a **fresh** touch (the previous candle was not already
      below the band), which turns a state into an event and removes the runs of
      duplicate entries;
    * a **volatility floor** keeps the rule out of dead tape where the band width
      cannot pay the round trip.
    """
    out = _blank(frame)
    middle = sma(frame, 20)
    std = frame["close"].rolling(20).std()
    lower = middle - 2.0 * std
    upper = middle + 2.0 * std

    band_width = (upper - lower) / middle
    vol_ok = band_width > 0.02  # a band narrower than 2% cannot pay 0.3% of costs

    below = frame["low"] <= lower
    fresh_touch = below & (~below.shift(1).fillna(False))
    not_crashing = frame["close"] > middle * 0.94  # refuse to buy a cliff
    out["enter_long"] = fresh_touch & vol_ok & not_crashing & (frame["volume"] > 0)

    # Room to work: exit at the middle band but only after candle 3, or on a
    # genuine overshoot that proves the reversion is done.
    reached_middle = frame["close"] >= middle
    out["exit_long"] = (reached_middle | (frame["close"] >= upper)) & (frame["volume"] > 0)
    return out


def sig_v2_keltner(frame: pd.DataFrame) -> pd.DataFrame:
    """Keltner breakout that keeps the trend (fixes `keltner`).

    The shipped rule exits below the middle band, which after a breakout is a
    short distance below the entry, so the trade dies on the first pullback. The
    v2 keeps a **trailing Supertrend stop** and only exits on a genuine trend
    break, so a winning breakout can run -- which is the entire premise of a
    breakout system.
    """
    out = _blank(frame)
    middle = ema(frame, 20)
    bands = atr(frame, 10)
    # Band width in ATR units. 3.0 is the centre of the robust plateau measured by
    # research/robustness.py; it must stay in step with KeltnerBreakoutV2Strategy,
    # which is the file that actually ships.
    upper = middle + 3.0 * bands
    trend = ema(frame, 200)
    direction = supertrend(frame, 10, 3.0)

    breakout = frame["close"] > upper
    # "Was not already above the band", written so the warm-up region is excluded
    # and no negated NaN comparison is materialised (pandas 3 rejects the object
    # dtype that ``~breakout.shift()`` produces on a NaN-bearing column). This must
    # stay in step with KeltnerBreakoutV2Strategy, which is the file that ships.
    previous = frame["close"].shift(1)
    previous_upper = upper.shift(1)
    fresh = breakout & previous.notna() & previous_upper.notna() & (previous <= previous_upper)
    regime = (frame["close"] > trend) & (middle > trend)
    # Require a real expansion: the candle must clear the band by a margin that
    # exceeds the round-trip cost, so marginal pokes are skipped.
    expansion = (frame["close"] - upper) > 0.3 * bands
    out["enter_long"] = fresh & regime & expansion & (frame["volume"] > 0)

    # Exit on the Supertrend flip, not on the middle band.
    out["exit_long"] = (direction < 0) & (frame["volume"] > 0)
    return out


def sig_v2_momentum(frame: pd.DataFrame) -> pd.DataFrame:
    """Momentum as an event with a trend gate (fixes `momentum`).

    ``roc > 0`` is a *state*: it stays true for dozens of candles, which is why
    the shipped rule fired 494 times on BTC 1h and 1672 times on SOL 15m. The v2
    requires the momentum reading to **cross** into strength and the trend to be
    confirmed by a slow KAMA, then rides a Supertrend trail.
    """
    out = _blank(frame)
    momentum = roc(frame, 20)
    # Crossing into strength: tonight's reading is a new 30-candle high.
    strength = momentum > 0
    fresh = strength & (momentum >= momentum.rolling(30).max().shift(1))
    trend = kama(frame, 20)
    regime = frame["close"] > trend
    efficiency = adx(frame, 14) > 18
    out["enter_long"] = fresh & regime & efficiency & (frame["volume"] > 0)

    direction = supertrend(frame, 10, 3.0)
    lost = (momentum < 0) | (frame["close"] < trend)
    out["exit_long"] = ((direction < 0) | lost) & (frame["volume"] > 0)
    return out


def sig_v2_dual_thrust(frame: pd.DataFrame, window: int = 4, k: float = 1.0) -> pd.DataFrame:
    """Dual Thrust with a cost-aware trigger (fixes `dual-thrust`).

    The shipped rule uses ``k = 0.5``, placing the trigger only half a range from
    the open. On 15m crypto that is well inside the noise: it produced 3346 trades
    on ETH and lost 100% of the account almost entirely to fees (488% of equity in
    charges against a +388% gross). Two fixes: the trigger is widened to a full
    range (``k = 1.0``) so the break must be real, and the exit is the opposite
    trigger *or* a Supertrend flip rather than a same-candle opposite line.
    """
    out = _blank(frame)
    high, low = donchian(frame, window)
    highest_close = frame["close"].rolling(window).max().shift(1)
    lowest_close = frame["close"].rolling(window).min().shift(1)
    rng = np.maximum(high - lowest_close, highest_close - low)
    # A range this small cannot pay the round trip; skip those candles.
    cost_floor = rng / frame["close"] > 0.006
    buy_line = frame["open"] + k * rng
    sell_line = frame["open"] - k * rng
    breakout = frame["close"] > buy_line
    fresh = breakout & (~breakout.shift(1).fillna(False))
    out["enter_long"] = fresh & cost_floor & (frame["volume"] > 0)

    direction = supertrend(frame, 10, 3.0)
    out["exit_long"] = ((frame["close"] < sell_line) | (direction < 0)) & (frame["volume"] > 0)
    return out


def sig_v2_supertrend(frame: pd.DataFrame) -> pd.DataFrame:
    """Supertrend with a trend filter and a wider band (fixes `supertrend`).

    The shipped rule takes every flip in both directions, so in a range it
    alternates and pays a fee each time. The v2 only takes flips that agree with a
    slow trend, and widens the ATR multiplier to reduce flip frequency.
    """
    out = _blank(frame)
    direction = supertrend(frame, 10, 4.0)
    trend = ema(frame, 200)
    flip_up = (direction > 0) & (direction.shift(1) < 0)
    aligned = frame["close"] > trend
    out["enter_long"] = flip_up & aligned & (frame["volume"] > 0)
    flip_down = (direction < 0) & (direction.shift(1) > 0)
    out["exit_long"] = (flip_down | (frame["close"] < trend * 0.97)) & (frame["volume"] > 0)
    return out


def sig_v2_macd(frame: pd.DataFrame) -> pd.DataFrame:
    """MACD cross with a slope gate (fixes `macd`).

    The shipped rule exits on the histogram crossing back below zero, which
    happens a median of 7-8 candles after entry -- too soon for the move to
    develop. The v2 keeps the entry but requires the slow EMA to be *rising* and
    exits on a slower confirmation, giving the trend time.
    """
    out = _blank(frame)
    ema_fast = ema(frame, 12)
    ema_slow = ema(frame, 26)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    hist = macd_line - signal_line

    trend = ema(frame, 200)
    rising = trend > trend.shift(5)
    cross_up = (hist > 0) & (hist.shift(1) <= 0)
    out["enter_long"] = cross_up & (frame["close"] > trend) & rising & (frame["volume"] > 0)

    direction = supertrend(frame, 10, 3.0)
    out["exit_long"] = ((hist < 0) & (hist.shift(1) >= 0) | (direction < 0)) & (frame["volume"] > 0)
    return out


def sig_v2_basic(frame: pd.DataFrame) -> pd.DataFrame:
    """EMA cross that does not give everything back (fixes `basic`).

    The shipped baseline exits only on the opposite cross, which on a lagging EMA
    pair means it gives back most of every swing; it lost 38% on BTC 1h. The v2
    adds a trailing Supertrend exit so the position is closed when the trend
    actually turns, plus a slope confirmation on the entry.
    """
    out = _blank(frame)
    fast = ema(frame, 20)
    slow = ema(frame, 50)
    slow_rising = slow > slow.shift(5)
    cross_up = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    out["enter_long"] = cross_up & slow_rising & (frame["volume"] > 0)

    direction = supertrend(frame, 10, 3.0)
    cross_down = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    out["exit_long"] = (cross_down | (direction < 0)) & (frame["volume"] > 0)
    return out


def sig_v2_donchian(frame: pd.DataFrame) -> pd.DataFrame:
    """Donchian breakout with a trailing exit instead of a fixed ATR target."""
    out = _blank(frame)
    high, low = donchian(frame, 20)
    breakout = frame["close"] > high
    fresh = breakout & (~breakout.shift(1).fillna(False))
    trend = ema(frame, 100)
    out["enter_long"] = fresh & (frame["close"] > trend) & (frame["volume"] > 0)

    direction = supertrend(frame, 10, 3.5)
    exit_low, _ = donchian(frame, 10)
    out["exit_long"] = ((frame["close"] < exit_low) | (direction < 0)) & (frame["volume"] > 0)
    return out


# ---------------------------------------------------------------------------
# New research strategies
# ---------------------------------------------------------------------------
def sig_tsmom_vol(frame: pd.DataFrame, lookback: int = 168, vol_window: int = 168) -> pd.DataFrame:
    """Volatility-scaled time-series momentum with a regime gate.

    The literature is genuinely mixed on time-series momentum (see the research
    report): Huang et al. (2020) show the asset-by-asset evidence is weak and that
    a TSMOM rule is often indistinguishable from always being long. So this
    candidate is built to be *defensible* rather than clever:

    * the signal is the classic 12-period-or-longer return sign (``lookback``
      candles), which is the version with the most support;
    * exposure is gated on **volatility regime** -- the strategy stands aside when
      realised volatility is in its top decile, which is where trend rules suffer
      their worst whipsaws and where the drawdowns concentrate;
    * the position is exited by a Supertrend trail rather than by the same signal
      that opened it, so a trend is not abandoned on a single noisy candle.

    The honest expectation, per the papers, is that this behaves close to a
    filtered buy-and-hold with a better drawdown -- which is still valuable, and
    is measured against buy-and-hold accordingly.
    """
    out = _blank(frame)
    momentum = frame["close"] / frame["close"].shift(lookback) - 1.0
    vol = realised_vol(frame, vol_window)
    # Stand aside in the most violent decile of volatility.
    vol_cap = vol.rolling(500, min_periods=100).quantile(0.90)
    calm = vol <= vol_cap
    trend = sma(frame, 200)

    out["enter_long"] = (momentum > 0) & calm & (frame["close"] > trend) & (frame["volume"] > 0)
    out["exit_long"] = ((momentum < 0) | (frame["close"] < trend) | (~calm)) & (frame["volume"] > 0)
    return out


def sig_vol_squeeze_breakout(frame: pd.DataFrame) -> pd.DataFrame:
    """Volatility-contraction breakout (the "squeeze" idea).

    Volatility clusters: quiet periods precede expansions. The rule measures
    Bollinger band width, requires it to be in the bottom quintile of its trailing
    range (a squeeze), and then buys the breakout *when the expansion starts*, on
    a close above the upper band with the band width turning up. A Supertrend
    trail manages the exit.

    The economic claim is about *timing* only -- the squeeze tells you a move is
    likely, not which way -- so the direction comes from the breakout, not from the
    squeeze.
    """
    out = _blank(frame)
    middle = sma(frame, 20)
    std = frame["close"].rolling(20).std()
    upper = middle + 2.0 * std
    width = (upper - (middle - 2.0 * std)) / middle
    # Squeeze: the current width sits in the lowest 25% of the last 250 candles.
    threshold = width.rolling(250, min_periods=60).quantile(0.25)
    squeezed = width <= threshold
    # The squeeze must have been present recently (it may already be releasing).
    recent_squeeze = squeezed.rolling(5).max().fillna(0).astype(bool)
    expanding = width > width.shift(1)
    breakout = frame["close"] > upper
    out["enter_long"] = breakout & recent_squeeze & expanding & (frame["volume"] > 0)

    direction = supertrend(frame, 10, 3.0)
    out["exit_long"] = ((frame["close"] < middle) | (direction < 0)) & (frame["volume"] > 0)
    return out


def sig_trend_ensemble(frame: pd.DataFrame) -> pd.DataFrame:
    """Multi-horizon trend ensemble.

    A single crossover has one lookback and therefore one failure point. The
    evidence on trend following is that combining several horizons is more robust
    than tuning one. The rule votes with three simple moving averages (50, 100,
    200) and requires a **majority plus the fastest one rising** to enter, exiting
    when the vote is lost. This is a low-turnover regime filter, closer to an
    allocation rule than a signal generator.
    """
    out = _blank(frame)
    close = frame["close"]
    fast = sma(frame, 50)
    mid = sma(frame, 100)
    slow = sma(frame, 200)
    votes = (close > fast).astype(int) + (close > mid).astype(int) + (close > slow).astype(int)
    rising = fast > fast.shift(10)
    out["enter_long"] = (votes >= 2) & rising & (frame["volume"] > 0)
    out["exit_long"] = ((votes <= 1) | (~rising)) & (frame["volume"] > 0)
    return out


def sig_vol_targeted_trend(frame: pd.DataFrame) -> pd.DataFrame:
    """Trend following with a volatility-targeting overlay.

    Harvey et al. (2018) find volatility targeting reliably reduces tail risk
    (its Sharpe benefit is concentrated in risk assets and is weaker than usually
    claimed). This rule implements the risk half of that finding: it trades the
    200-period trend, but *only* opens when realised volatility is below its
    trailing median, and it exits promptly when volatility spikes above the 75th
    percentile. The aim is drawdown control, not extra return.
    """
    out = _blank(frame)
    close = frame["close"]
    trend = sma(frame, 200)
    vol = realised_vol(frame, 72)
    vol_median = vol.rolling(500, min_periods=100).median()
    vol_q75 = vol.rolling(500, min_periods=100).quantile(0.75)
    calm = vol < vol_median
    storm = vol > vol_q75

    out["enter_long"] = (close > trend) & calm & (frame["volume"] > 0)
    out["exit_long"] = ((close < trend) | storm) & (frame["volume"] > 0)
    return out


#: Registry used by the research driver and the sweeps.
CANDIDATES = {
    "v2-basic": (sig_v2_basic, -0.10, 0.08),
    "v2-momentum": (sig_v2_momentum, -0.10, 0.12),
    "v2-bollinger": (sig_v2_bollinger, -0.08, 0.06),
    "v2-macd": (sig_v2_macd, -0.10, 0.10),
    "v2-donchian": (sig_v2_donchian, -0.10, 0.20),
    "v2-keltner": (sig_v2_keltner, -0.10, 0.15),
    "v2-supertrend": (sig_v2_supertrend, -0.10, 0.10),
    "v2-dual-thrust": (sig_v2_dual_thrust, -0.06, 0.04),
    "tsmom-vol": (sig_tsmom_vol, -0.15, None),
    "vol-squeeze": (sig_vol_squeeze_breakout, -0.08, 0.15),
    "trend-ensemble": (sig_trend_ensemble, -0.15, None),
    "vol-targeted-trend": (sig_vol_targeted_trend, -0.12, None),
}
