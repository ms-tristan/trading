"""FaberStrategy: the 200-period trend filter of Mebane Faber's tactical model.

Public source of the idea: Mebane T. Faber, "A Quantitative Approach to Tactical
Asset Allocation" (Journal of Wealth Management, 2007) -- the "10-month simple
moving average" timing rule. On a daily timeframe the 200-candle SMA is that
10-month average: the asset is held while the close stays above it and the
position is moved back to cash as soon as it closes below. The minimal_roi is
deliberately unreachable so that only the trend signal can close the trade.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class FaberStrategy(IStrategy):
    """Hold while the close stays above SMA(200); exit on the close below it."""

    INTERFACE_VERSION = 3

    # Long only, signals evaluated on closed candles only.
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    # Primary timeframe declared for this profile in config/strategies.json.
    timeframe = "1d"
    # SMA(200) needs 200 candles to produce its first value, plus one for the crossover test.
    startup_candle_count = 210

    stoploss = -0.25
    # Unreachable target: the trend signal is the only exit of this strategy.
    minimal_roi = {"0": 1.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma_slow"] = ta.SMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        above_trend = dataframe["close"] > dataframe["sma_slow"]

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[above_trend & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[above_trend & (dataframe["volume"] > 0), "enter_tag"] = "above_sma200"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        below_trend = dataframe["close"] < dataframe["sma_slow"]

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[below_trend & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[below_trend & (dataframe["volume"] > 0), "exit_tag"] = "below_sma200"
        return dataframe
