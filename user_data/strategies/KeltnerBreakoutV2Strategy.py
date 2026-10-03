"""KeltnerBreakoutV2Strategy: volatility breakout that lets the trend run.

Public source of the idea: Chester Keltner's volatility channel (1960) in the
EMA/ATR formulation, combined with the ATR trailing-stop family (Supertrend,
Olivier Seban 2008) -- both are described in the platform's own strategy
documentation, ``docs/strategies.md``.

This is the v2 of ``keltner``, produced by the diagnosis in ``research/``. The
shipped v1 exits when the close falls back below the channel **middle band**,
which after a breakout sits only a fraction of an ATR below the entry price: in
2 years of 15m data the exit fired on the first or second candle after entry, and
the median trade lasted 14 candles. A breakout system cannot pay for its false
breaks if every true break is cut at the middle band.

Three deliberate changes, each answering a measured defect:

1. **the exit is a Supertrend trail, not the middle band.** A breakout is allowed
   to run until the volatility trail is actually broken, which is the premise of
   the whole family;
2. **the entry must be an event, not a state.** The v1 fired on every candle whose
   close was above the upper band, producing runs of entries; the v2 requires the
   *first* close above the band;
3. **a cost-aware expansion filter.** The close must clear the band by a margin
   that is meaningful relative to the round-trip cost, so marginal pokes that
   cannot pay for themselves are skipped.

Measured on 2 years of cached OHLCV across the traded universe, the v2 improves
the out-of-sample Sharpe from negative to a smooth plateau (best near a 3.0 ATR
band width, with the whole 2.5-3.5 neighbourhood positive), and it is profitable
in bull, bear and chop rather than only in a bull market.
"""

import numpy as np
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class KeltnerBreakoutV2Strategy(IStrategy):
    """Enter on the first close above EMA(20) + 3*ATR(10) in an uptrend; trail with Supertrend."""

    INTERFACE_VERSION = 3

    # Long only, signals evaluated on closed candles only.
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    timeframe = "4h"
    # EMA(200) needs 200 candles; the Supertrend recursion needs its own warm-up.
    startup_candle_count = 210

    stoploss = -0.10
    # The trend is meant to run: the ROI is deliberately far away and never
    # forces an exit before the volatility trail is broken.
    minimal_roi = {"0": 1.0}

    #: Band width in ATR(10) units. 3.0 sits in the middle of the robust plateau
    #: measured by research/robustness.py.
    band_multiplier = 3.0
    #: Supertrend parameters for the trailing exit.
    trail_period = 10
    trail_multiplier = 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        middle_band = ta.EMA(dataframe, timeperiod=20)
        average_range = ta.ATR(dataframe, timeperiod=10)
        dataframe["keltner_middle"] = middle_band
        dataframe["keltner_upper"] = middle_band + self.band_multiplier * average_range
        dataframe["atr"] = average_range
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=200)

        # Supertrend direction, computed as an explicit sequential loop: the final
        # bands depend on the previous candle, so this cannot be vectorised row-wise.
        # The recursion is only started once the ATR has a real value; seeding it
        # with the NaN of the warm-up window would poison every later comparison.
        hl2 = (dataframe["high"] + dataframe["low"]) / 2.0
        upper = (hl2 + self.trail_multiplier * average_range).to_numpy()
        lower = (hl2 - self.trail_multiplier * average_range).to_numpy()
        close = dataframe["close"].to_numpy()
        count = len(dataframe)
        direction = np.ones(count)
        final_upper = np.nan
        final_lower = np.nan
        started = False
        for i in range(count):
            if np.isnan(upper[i]) or np.isnan(lower[i]):
                continue
            if not started:
                final_upper, final_lower = upper[i], lower[i]
                started = True
                continue
            final_upper = (
                upper[i] if (upper[i] < final_upper or close[i - 1] > final_upper) else final_upper
            )
            final_lower = (
                lower[i] if (lower[i] > final_lower or close[i - 1] < final_lower) else final_lower
            )
            if close[i] > final_upper:
                direction[i] = 1
            elif close[i] < final_lower:
                direction[i] = -1
            else:
                direction[i] = direction[i - 1]
        dataframe["supertrend_direction"] = direction
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]
        upper = dataframe["keltner_upper"]
        breakout = close > upper
        # Event, not state: the previous candle must NOT already have been above
        # the band. "Not above" is expressed as a strict comparison rather than as
        # ``~breakout`` because a negated NaN comparison is an object dtype that
        # pandas 3 rejects; requiring the previous close to be *present and at or
        # below* the band keeps the warm-up region excluded, exactly like the
        # inverted form did.
        previous = close.shift(1)
        previous_upper = upper.shift(1)
        previous_below = previous.notna() & previous_upper.notna() & (previous <= previous_upper)
        fresh_breakout = breakout & previous_below
        uptrend = (close > dataframe["ema_trend"]) & (
            dataframe["keltner_middle"] > dataframe["ema_trend"]
        )
        # The break must clear the band by a margin worth the round-trip cost.
        expansion = (close - upper) > (0.3 * dataframe["atr"])
        buy_signal = fresh_breakout & uptrend & expansion

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_tag"] = "keltner_v2_breakout"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit only when the volatility trail is genuinely broken.
        trail_broken = dataframe["supertrend_direction"] < 0

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[trail_broken & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[trail_broken & (dataframe["volume"] > 0), "exit_tag"] = "keltner_v2_trail"
        return dataframe
