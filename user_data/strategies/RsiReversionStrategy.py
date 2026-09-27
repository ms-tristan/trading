"""RsiReversionStrategy: buy the RSI(14) recovery out of oversold inside an uptrend.

Public source of the idea: J. Welles Wilder's Relative Strength Index, published
in "New Concepts in Technical Trading Systems" (1978), used in its classic
mean-reversion reading -- the exit from the oversold area is the entry trigger.
The SMA(200) regime filter is the standard addition of the freqtrade community
"RsiReversion" profile: only reversion trades that lean with the long-term trend
are taken.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class RsiReversionStrategy(IStrategy):
    """Enter when RSI(14) crosses back above 30 while the close holds above SMA(200)."""

    INTERFACE_VERSION = 3

    # Long only, signals evaluated on closed candles only.
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    # Primary timeframe declared for this profile in config/strategies.json.
    timeframe = "15m"
    # SMA(200) needs 200 candles to produce its first value, plus one for the regime test.
    startup_candle_count = 210

    stoploss = -0.10
    minimal_roi = {"0": 0.06, "240": 0.03, "720": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["sma_slow"] = ta.SMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        oversold_recovery = (dataframe["rsi"] > 30) & (dataframe["rsi"].shift(1) <= 30)
        uptrend = dataframe["close"] > dataframe["sma_slow"]
        buy_signal = oversold_recovery & uptrend

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_tag"] = "rsi_oversold_recovery"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        reverted = dataframe["rsi"] > 65

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[reverted & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[reverted & (dataframe["volume"] > 0), "exit_tag"] = "rsi_reverted"
        return dataframe
