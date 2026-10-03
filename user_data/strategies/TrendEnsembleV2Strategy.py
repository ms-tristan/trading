"""TrendEnsembleV2Strategy: a multi-horizon trend filter instead of one crossover.

Public source of the idea: the trend-following literature surveyed for this work,
in particular the finding that **combining several lookbacks is more robust than
tuning a single one** (Moskowitz, Ooi & Pedersen, "Time Series Momentum", Journal
of Financial Economics 105(2), 2012; and the replication caution in Huang, Li,
Wang & Zhou, "Time series momentum: Is it there?", JFE 135(3), 2020). The 200-period
trend rule itself is Mebane Faber's tactical allocation (2007), already documented
in ``docs/strategies.md``.

Why this replaces the single-crossover baseline. ``BasicStrategy`` entered on an
EMA(20)/EMA(50) cross and exited only on the opposite cross. On 2 years of cached
1h data that lost 38% against a +40% buy-and-hold: a lagging cross gives back most
of every swing, and with only the opposite cross as an exit it never protects a
gain. The diagnosis (``research/diagnose.py``) attributes the loss to the exit, not
to the entry -- the median trade was held 28 candles and the strategy was in the
market 29% of the time while still losing.

This rule votes with three horizons (50 / 100 / 200) and requires a majority plus a
rising fast average. That makes it a **regime filter** rather than a signal
generator: it holds through noise that would trigger a crossover, and it steps
aside when the multi-horizon picture breaks down. Turnover is deliberately low,
because the measured problem with the short-horizon originals is cost, and the only
durable fix for cost is trading less.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class TrendEnsembleV2Strategy(IStrategy):
    """Hold while at least two of SMA(50/100/200) agree with price and SMA(50) rises."""

    INTERFACE_VERSION = 3

    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    timeframe = "1d"
    # SMA(200) needs 200 candles, plus a margin for the slope test.
    startup_candle_count = 210

    stoploss = -0.15
    # The position is meant to be held for months; the only exit is the signal, so
    # the ROI is set unreachable on purpose (the same convention as `faber`).
    minimal_roi = {"0": 1.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma_fast"] = ta.SMA(dataframe, timeperiod=50)
        dataframe["sma_mid"] = ta.SMA(dataframe, timeperiod=100)
        dataframe["sma_slow"] = ta.SMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]
        # How many horizons agree that price is above their average.
        votes = (
            (close > dataframe["sma_fast"]).astype(int)
            + (close > dataframe["sma_mid"]).astype(int)
            + (close > dataframe["sma_slow"]).astype(int)
        )
        rising = dataframe["sma_fast"] > dataframe["sma_fast"].shift(10)
        buy_signal = (votes >= 2) & rising

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_tag"] = "trend_ensemble"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]
        votes = (
            (close > dataframe["sma_fast"]).astype(int)
            + (close > dataframe["sma_mid"]).astype(int)
            + (close > dataframe["sma_slow"]).astype(int)
        )
        rising = dataframe["sma_fast"] > dataframe["sma_fast"].shift(10)
        sell_signal = (votes <= 1) | (~rising)

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[sell_signal & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[sell_signal & (dataframe["volume"] > 0), "exit_tag"] = "trend_ensemble_off"
        return dataframe
