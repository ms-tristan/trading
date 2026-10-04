"""KeltnerStrategy: Keltner channel breakout in the direction of the primary trend.

Public source of the idea: Chester Keltner's "How To Make Money in Commodities"
(1960) volatility channel, in the modern EMA/ATR formulation popularised by
Linda Raschke ("Street Smarts", 1996): an EMA(20) middle band surrounded by two
ATR(10) widths. A close above the upper band marks a volatility breakout, and
the EMA(50) > EMA(200) filter only accepts breakouts that happen with the trend.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class KeltnerStrategy(IStrategy):
    """Enter on a close above EMA(20) + 2 * ATR(10) while EMA(50) > EMA(200)."""

    INTERFACE_VERSION = 3

    # Long only, signals evaluated on closed candles only.
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    # Primary timeframe declared for this profile in config/strategies.json.
    timeframe = "15m"
    # EMA(200) needs 200 candles to produce its first value, plus one for the trend filter.
    startup_candle_count = 210

    stoploss = -0.10
    # Mean reversion needs a target, so the ladder stays -- but the terminal rung now
    # carries the previous tier's value instead of decaying to zero, which used to
    # close the trade at any non-negative profit once its timestamp had passed.
    minimal_roi = {"0": 0.15, "720": 0.07, "2880": 0.07}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        middle_band = ta.EMA(dataframe, timeperiod=20)
        average_range = ta.ATR(dataframe, timeperiod=10)
        dataframe["keltner_middle"] = middle_band
        dataframe["keltner_upper"] = middle_band + 2.0 * average_range
        dataframe["keltner_lower"] = middle_band - 2.0 * average_range
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        breakout = dataframe["close"] > dataframe["keltner_upper"]
        uptrend = dataframe["ema_fast"] > dataframe["ema_slow"]
        buy_signal = breakout & uptrend

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_tag"] = "keltner_breakout"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        below_middle_band = dataframe["close"] < dataframe["keltner_middle"]

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[below_middle_band & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[below_middle_band & (dataframe["volume"] > 0), "exit_tag"] = (
            "keltner_middle_band"
        )
        return dataframe
