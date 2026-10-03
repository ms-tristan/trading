"""DonchianAllInStrategy: Donchian breakout, risk-managed for a single all-in slot.

Public source of the idea: Richard Donchian's channel breakout (the four-week
rule), popularised by the Turtle Traders (Dennis & Eckhardt, 1983) -- the same rule
already shipped as ``DonchianStrategy`` and documented in ``docs/strategies.md``.

Why this variant exists. The shipped ``donchian`` profile runs at half size with a
``-0.08`` stop and a chandelier exit. Run at **full size** (``max_open_trades = 1``,
Freqtrade "unlimited" staking) it is a different risk proposition: the 2-year
backtest shows a -44% (ETH 4h) to -63% (DOGE 1h) peak-to-trough decline, and at
full size that is the account.

The change is confined to the risk envelope:

* the exit is a **chandelier stop in ATR units** (highest close of the lookback
  minus a multiple of ATR) rather than a fixed percentage. A percentage stop is
  meaningless across assets whose volatility differs by a factor of three -- DOGE
  and BTC do not share a sensible percentage stop. An ATR stop adapts to the
  asset, which is the correct primitive for an all-in position;
* the multiple is set to the tighter end of the robust range measured in
  ``research/``, so the trail follows closer at full size than it would at half.

The entry signal is unchanged from the shipped rule, so the two profiles stay
comparable in the dashboard.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class DonchianAllInStrategy(IStrategy):
    """Break the 20-candle high, trail out on an ATR chandelier, full-size."""

    INTERFACE_VERSION = 3

    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    timeframe = "1h"
    startup_candle_count = 60

    # The ATR trail is the real exit; this is the disaster brake at full size.
    stoploss = -0.15
    # Let the trend run: the chandelier decides, not a fixed target.
    minimal_roi = {"0": 1.0}

    #: Entry channel length, matching the shipped rule.
    entry_channel = 20
    #: Exit channel length, matching the shipped rule.
    exit_channel = 10
    #: ATR period for the chandelier.
    atr_period = 14
    #: Chandelier width in ATR units. Tighter than the shipped 2.0 because the
    #: position is full-size; chosen from the robust range, not a single optimum.
    chandelier_multiplier = 1.8

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Both channels are shifted by one candle: the breakout is measured against
        # the previous completed window, never against one containing the candle
        # being decided on.
        dataframe["donchian_high"] = dataframe["high"].rolling(self.entry_channel).max().shift(1)
        dataframe["donchian_low"] = dataframe["low"].rolling(self.exit_channel).min().shift(1)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=self.atr_period)
        # Chandelier: the highest close of the lookback, pulled down by ATR.
        dataframe["chandelier_exit"] = (
            dataframe["close"].rolling(self.entry_channel).max().shift(1)
            - self.chandelier_multiplier * dataframe["atr"]
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        breakout = dataframe["close"] > dataframe["donchian_high"]

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[breakout & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[breakout & (dataframe["volume"] > 0), "enter_tag"] = "donchian_allin_break"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        channel_exit = dataframe["close"] < dataframe["donchian_low"]
        chandelier = dataframe["close"] < dataframe["chandelier_exit"]
        exit_signal = channel_exit | chandelier

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[exit_signal & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[exit_signal & (dataframe["volume"] > 0), "exit_tag"] = "donchian_allin_trail"
        return dataframe
