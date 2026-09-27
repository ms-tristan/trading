"""DualThrustStrategy: intraday range breakout around the open.

Public source of the idea: the Dual Thrust day-trading system attributed to
Michael Chalek and widely republished in the futures and index-futures
literature. It is the only short-horizon breakout profile of this platform: the
trigger range is the largest of the last N=4 candle spans, and the buy and sell
lines are projected from the current open with the K1/K2 multipliers. The
strategy is long only, so the buy line opens the trade and the sell line is its
exit.
"""

import numpy as np
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame

RANGE_WINDOW = 4
BUY_MULTIPLIER = 0.5
SELL_MULTIPLIER = 0.5


class DualThrustStrategy(IStrategy):
    """Enter above open + 0.5 * range, exit below open - 0.5 * range over the last 4 candles."""

    INTERFACE_VERSION = 3

    # Long only, signals evaluated on closed candles only.
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    # Primary timeframe declared for this profile in config/strategies.json.
    timeframe = "15m"
    # The 4-candle range needs 4 closed candles, plus one for the shifted range test.
    startup_candle_count = 10

    stoploss = -0.06
    # Short horizon: an intraday range breakout is expected to work within hours.
    minimal_roi = {"0": 0.03, "60": 0.015, "180": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Every component of the range is measured on the previous N candles only.
        highest_high = ta.MAX(dataframe, timeperiod=RANGE_WINDOW, price="high").shift(1)
        lowest_low = ta.MIN(dataframe, timeperiod=RANGE_WINDOW, price="low").shift(1)
        highest_close = ta.MAX(dataframe, timeperiod=RANGE_WINDOW, price="close").shift(1)
        lowest_close = ta.MIN(dataframe, timeperiod=RANGE_WINDOW, price="close").shift(1)
        dataframe["dual_thrust_range"] = np.maximum(
            highest_high - lowest_close, highest_close - lowest_low
        )
        dataframe["buy_line"] = dataframe["open"] + BUY_MULTIPLIER * dataframe["dual_thrust_range"]
        dataframe["sell_line"] = (
            dataframe["open"] - SELL_MULTIPLIER * dataframe["dual_thrust_range"]
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        breakout_up = dataframe["close"] > dataframe["buy_line"]

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[breakout_up & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[breakout_up & (dataframe["volume"] > 0), "enter_tag"] = "dual_thrust_up"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        breakout_down = dataframe["close"] < dataframe["sell_line"]

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[breakout_down & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[breakout_down & (dataframe["volume"] > 0), "exit_tag"] = "dual_thrust_down"
        return dataframe
