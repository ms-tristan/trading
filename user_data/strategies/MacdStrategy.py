"""MacdStrategy: MACD histogram crossing above zero inside an uptrend.

Public source of the idea: Gerald Appel's Moving Average Convergence Divergence
indicator (1979), traded here on the histogram zero-line cross described in the
freqtrade "MacdStrategy" documentation example. The EMA(200) filter restricts
the entries to the bullish regime; the histogram cross itself times the entry.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class MacdStrategy(IStrategy):
    """Enter when the MACD(12, 26, 9) histogram crosses above zero while close > EMA(200)."""

    INTERFACE_VERSION = 3

    # Long only, signals evaluated on closed candles only.
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    # Primary timeframe declared for this profile in config/strategies.json.
    timeframe = "1h"
    # EMA(200) needs 200 candles to produce its first value, plus one for the regime test.
    startup_candle_count = 210

    stoploss = -0.10
    minimal_roi = {"0": 0.10, "480": 0.05, "1440": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        histogram = dataframe["macdhist"]
        histogram_cross_up = (histogram > 0) & (histogram.shift(1) <= 0)
        uptrend = dataframe["close"] > dataframe["ema_slow"]
        buy_signal = histogram_cross_up & uptrend

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_tag"] = "macd_hist_cross_up"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        histogram = dataframe["macdhist"]
        histogram_cross_down = (histogram < 0) & (histogram.shift(1) >= 0)

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[histogram_cross_down & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[histogram_cross_down & (dataframe["volume"] > 0), "exit_tag"] = (
            "macd_hist_cross_down"
        )
        return dataframe
