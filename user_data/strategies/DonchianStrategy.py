"""DonchianStrategy: 20-candle channel breakout with an ATR(14) chandelier exit.

Public source of the idea: Richard Donchian's four-week rule / channel breakout
(see "Donchian's 5- and 20-day moving average method", Futures magazine, 1970s),
generalised to the 20-candle high channel popularised by the Turtle Traders
(William Eckhardt and Richard Dennis, 1983). The exit uses the 10-candle low
channel and a chandelier level (highest close minus 2 ATR(14)) so that a
volatility expansion takes the trade out before the fixed stoploss is reached.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class DonchianStrategy(IStrategy):
    """Enter on a close above the previous 20-candle high; exit on the 10-candle low or ATR."""

    INTERFACE_VERSION = 3

    # Long only, signals evaluated on closed candles only.
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    # Primary timeframe declared for this profile in config/strategies.json.
    timeframe = "1h"
    # The 20-candle channel needs 20 closed candles, plus one for the shifted breakout test.
    startup_candle_count = 25

    stoploss = -0.08
    minimal_roi = {"0": 0.20, "1440": 0.10, "2880": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        # The channels are built on the *previous* candles only, never on the current one.
        dataframe["donchian_high"] = ta.MAX(dataframe, timeperiod=20, price="high").shift(1)
        dataframe["donchian_low"] = ta.MIN(dataframe, timeperiod=10, price="low").shift(1)
        dataframe["chandelier_exit"] = (
            ta.MAX(dataframe, timeperiod=20, price="close").shift(1) - 2.0 * dataframe["atr"]
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        breakout = dataframe["close"] > dataframe["donchian_high"]

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[breakout & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[breakout & (dataframe["volume"] > 0), "enter_tag"] = "donchian_breakout"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # The 10-candle low ends the breakout; the ATR chandelier ends it when
        # volatility expands against the trade before the stoploss is hit.
        channel_exit = dataframe["close"] < dataframe["donchian_low"]
        atr_exit = dataframe["close"] < dataframe["chandelier_exit"]

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[channel_exit & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[channel_exit & (dataframe["volume"] > 0), "exit_tag"] = "donchian_low_break"
        dataframe.loc[atr_exit & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[atr_exit & (dataframe["volume"] > 0), "exit_tag"] = "atr_chandelier_exit"
        return dataframe
