"""BollingerStrategy: lower-band touch bought back into an uptrend.

Public source of the idea: John Bollinger's Bollinger Bands (Bollinger on
Bollinger Bands, McGraw-Hill, 2001). The classic "band touch" reading is kept,
but the raw touch is filtered twice: RSI(14) below 40 proves the touch is a real
pullback and not the start of a downtrend, and the close above SMA(200) keeps
the trade aligned with the long-term direction. This is the standard freqtrade
Bollinger mean-reversion profile.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class BollingerStrategy(IStrategy):
    """Enter on a lower Bollinger(20, 2.0) touch with RSI(14) < 40 above SMA(200)."""

    INTERFACE_VERSION = 3

    # Long only, signals evaluated on closed candles only.
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    # Primary timeframe declared for this profile in config/strategies.json.
    timeframe = "5m"
    # SMA(200) needs 200 candles to produce its first value, plus one for the regime test.
    startup_candle_count = 210

    stoploss = -0.10
    # Mean reversion needs a target, so the ladder stays -- but the terminal rung now
    # carries the previous tier's value instead of decaying to zero, which used to
    # close the trade at any non-negative profit once its timestamp had passed.
    minimal_roi = {"0": 0.05, "240": 0.025, "720": 0.025}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bollinger = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_upper"] = bollinger["upperband"]
        dataframe["bb_middle"] = bollinger["middleband"]
        dataframe["bb_lower"] = bollinger["lowerband"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["sma_slow"] = ta.SMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        band_touch = dataframe["low"] <= dataframe["bb_lower"]
        pullback = dataframe["rsi"] < 40
        uptrend = dataframe["close"] > dataframe["sma_slow"]
        buy_signal = band_touch & pullback & uptrend

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_tag"] = "bb_lower_touch"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Mean reversion is complete at the middle band; a stretched RSI exits early.
        at_middle_band = dataframe["close"] >= dataframe["bb_middle"]
        overbought = dataframe["rsi"] > 65

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[at_middle_band & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[at_middle_band & (dataframe["volume"] > 0), "exit_tag"] = "bb_middle_band"
        dataframe.loc[overbought & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[overbought & (dataframe["volume"] > 0), "exit_tag"] = "rsi_overbought"
        return dataframe
