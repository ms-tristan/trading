"""BasicStrategy: EMA(20) / EMA(50) crossover baseline.

Public source of the idea: the classic dual moving-average crossover system,
the same baseline published as ``SampleStrategy`` in the freqtrade strategy
documentation (https://www.freqtrade.io/en/stable/strategy-customization/).
It is the reference profile the other nine strategies of this platform are
compared against: it trades the trend, never against it, and only when the
momentum filter leaves room for the move to continue.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from market_regime import BtcRegimeGateMixin
from pandas import DataFrame


class BasicStrategy(BtcRegimeGateMixin, IStrategy):
    """Enter on an EMA(20) cross above EMA(50) while RSI(14) is below 70."""

    INTERFACE_VERSION = 3

    # Long only, signals evaluated on closed candles only.
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    # Primary timeframe declared for this profile in config/strategies.json.
    timeframe = "1h"
    # EMA(50) needs 50 candles to produce its first value, plus one for the cross test.
    startup_candle_count = 60

    stoploss = -0.10
    # The single rung is unreachable, so the ROI ladder is disabled: the trade is
    # closed by the exit signal or by the stoploss, never by a time-decaying target,
    # which no longer truncates the right tail of a trend move.
    minimal_roi = {"0": 1.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        previous_fast = dataframe["ema_fast"].shift(1)
        previous_slow = dataframe["ema_slow"].shift(1)
        cross_up = (
            (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (previous_fast <= previous_slow)
            & (dataframe["rsi"] < 70)
        )

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[cross_up & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[cross_up & (dataframe["volume"] > 0), "enter_tag"] = "ema_cross_up"
        # Hand the signals to the next class of the MRO: the BTC regime gate filters them.
        return super().populate_entry_trend(dataframe, metadata)

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        previous_fast = dataframe["ema_fast"].shift(1)
        previous_slow = dataframe["ema_slow"].shift(1)
        cross_down = (dataframe["ema_fast"] < dataframe["ema_slow"]) & (
            previous_fast >= previous_slow
        )
        overbought = dataframe["rsi"] > 78

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[cross_down & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[cross_down & (dataframe["volume"] > 0), "exit_tag"] = "ema_cross_down"
        dataframe.loc[overbought & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[overbought & (dataframe["volume"] > 0), "exit_tag"] = "rsi_overbought"
        return dataframe
