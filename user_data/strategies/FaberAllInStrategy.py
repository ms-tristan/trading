"""FaberAllInStrategy: Faber's trend allocation, sized for a single all-in slot.

Public source of the idea: Mebane T. Faber, *A Quantitative Approach to Tactical
Asset Allocation*, Journal of Wealth Management (2007) -- the same 200-period rule
already shipped as ``FaberStrategy`` and documented in ``docs/strategies.md``.

This variant changes **only the risk envelope**, never the signal. It exists to be
run as a profile with ``max_open_trades = 1``, where Freqtrade's "unlimited"
staking puts the whole wallet into the single open position. Being all-in changes
what the risk parameters have to do:

* the shipped ``faber`` uses ``stoploss = -0.25`` and an unreachable ROI, because
  a half-sized position can afford to sit through daily noise. At **full** size
  the same -25% is a quarter of the entire account on one trade, so the stop is
  tightened to a level that still respects the strategy's horizon but caps the
  single-trade loss;
* the exit remains signal-driven, so the tight stop is a **disaster brake**, not
  the primary exit. That matters: the backtest evidence for this family is that a
  stop tight enough to trade often destroys the edge, because a 200-period trend
  rule is *expected* to sit through ordinary pullbacks. A stop that fires is
  evidence the regime call was wrong.

The signal itself is unchanged from ``faber`` so the two profiles remain
comparable in the dashboard -- which is the whole point of running them side by
side. Only sizing and the stop differ.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class FaberAllInStrategy(IStrategy):
    """Hold above SMA(200), full-size, with a tightened single-trade stop."""

    INTERFACE_VERSION = 3

    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    timeframe = "1d"
    startup_candle_count = 210

    # Full-size position: -12% is the disaster brake. The shipped half-size
    # profile uses -25% because a half position can afford twice the noise.
    stoploss = -0.12
    # Only the signal closes the trade, exactly like the shipped `faber`.
    minimal_roi = {"0": 1.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma_slow"] = ta.SMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        above = dataframe["close"] > dataframe["sma_slow"]

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[above & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[above & (dataframe["volume"] > 0), "enter_tag"] = "faber_allin_above"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        below = dataframe["close"] < dataframe["sma_slow"]

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[below & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[below & (dataframe["volume"] > 0), "exit_tag"] = "faber_allin_below"
        return dataframe
