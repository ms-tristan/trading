"""MomentumStrategy: rate-of-change momentum inside a confirmed primary trend.

Public source of the idea: the cross-sectional momentum literature surveyed by
Narasimhan Jegadeesh and Sheridan Titman, "Returns to Buying Winners and
Selling Losers" (Journal of Finance, 1993), reduced to a single-asset, long-only
time-series momentum filter. The 200-period EMA carries the primary trend, the
rate of change carries the impulse and ADX(14) rejects the flat markets where
momentum signals are pure noise. The public implementation blueprint is the
freqtrade "MomentumStrategy" tutorial profile.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from market_regime import BtcRegimeGateMixin
from pandas import DataFrame


class MomentumStrategy(BtcRegimeGateMixin, IStrategy):
    """Enter while ROC(12) is positive inside an EMA(50) > EMA(200) trend with ADX(14) > 20."""

    INTERFACE_VERSION = 3

    # Long only, signals evaluated on closed candles only.
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    # Primary timeframe declared for this profile in config/strategies.json.
    timeframe = "15m"
    # EMA(200) needs 200 candles to produce its first value, plus one for the filter test.
    startup_candle_count = 210

    stoploss = -0.10
    # The single rung is unreachable, so the ROI ladder is disabled: the trade is
    # closed by the exit signal, the trailing stop or the stoploss, never by a
    # time-decaying target, which no longer truncates the right tail of the move.
    minimal_roi = {"0": 1.0}

    # A momentum trade is protected by a trailing stop once it has moved far enough.
    trailing_stop = True
    trailing_stop_positive = 0.03
    trailing_stop_positive_offset = 0.05
    trailing_only_offset_is_reached = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["roc"] = ta.ROC(dataframe, timeperiod=12)
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        momentum_up = (
            (dataframe["roc"] > 0)
            & (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["adx"] > 20)
        )

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[momentum_up & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[momentum_up & (dataframe["volume"] > 0), "enter_tag"] = "momentum_up"
        # Hand the signals to the next class of the MRO: the BTC regime gate filters them.
        return super().populate_entry_trend(dataframe, metadata)

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # The trailing stop protects the profit; the signal exits the trade when the
        # primary trend or the impulse itself is gone.
        trend_broken = dataframe["ema_fast"] < dataframe["ema_slow"]
        impulse_lost = dataframe["roc"] < 0

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[trend_broken & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[trend_broken & (dataframe["volume"] > 0), "exit_tag"] = "trend_broken"
        dataframe.loc[impulse_lost & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[impulse_lost & (dataframe["volume"] > 0), "exit_tag"] = "impulse_lost"
        return dataframe
