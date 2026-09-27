"""SupertrendStrategy: ATR band flip traded long only.

Public source of the idea: the Supertrend indicator, the ATR band system
popularised by Olivier Seban in 2008 and standardised by the TradingView Pine
script reference implementation, where the bands are built from hl2 and ATR(10)
with a 3.0 multiplier. The direction flips to +1 when the close takes out the
final upper band and back to -1 when it loses the final lower band.
"""

import numpy as np
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame

SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = 3.0
UPTREND = 1
DOWNTREND = -1


def supertrend_direction(
    close: np.ndarray, hl2: np.ndarray, average_range: np.ndarray
) -> np.ndarray:
    """Return the Supertrend direction per candle: +1 for an uptrend, -1 for a downtrend.

    The bands are recursive, so the final upper/lower band of a candle depends on
    the previous candle; the loop is the reference Pine implementation transcribed.
    """
    upper_band = hl2 + SUPERTREND_MULTIPLIER * average_range
    lower_band = hl2 - SUPERTREND_MULTIPLIER * average_range
    length = close.shape[0]
    direction = np.full(length, UPTREND, dtype=int)
    final_upper = np.full(length, np.nan)
    final_lower = np.full(length, np.nan)

    for index in range(length):
        if np.isnan(upper_band[index]):
            # ATR(10) warm-up: no band yet, stay in the neutral uptrend state.
            continue
        if index == 0 or np.isnan(final_upper[index - 1]):
            final_upper[index] = upper_band[index]
            final_lower[index] = lower_band[index]
            continue

        previous_upper = final_upper[index - 1]
        previous_lower = final_lower[index - 1]
        final_upper[index] = (
            upper_band[index]
            if upper_band[index] < previous_upper or close[index - 1] > previous_upper
            else previous_upper
        )
        final_lower[index] = (
            lower_band[index]
            if lower_band[index] > previous_lower or close[index - 1] < previous_lower
            else previous_lower
        )

        if close[index] > previous_upper:
            direction[index] = UPTREND
        elif close[index] < previous_lower:
            direction[index] = DOWNTREND
        else:
            direction[index] = direction[index - 1]

    return direction


class SupertrendStrategy(IStrategy):
    """Enter on the Supertrend(10, 3.0) flip to uptrend; exit on the flip to downtrend."""

    INTERFACE_VERSION = 3

    # Long only, signals evaluated on closed candles only.
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    # Primary timeframe declared for this profile in config/strategies.json.
    timeframe = "1h"
    # ATR(10) and the 10-candle period need 10 candles, plus the recursive band warm-up.
    startup_candle_count = 30

    stoploss = -0.10
    minimal_roi = {"0": 0.10, "720": 0.05, "2880": 0.0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        average_range = ta.ATR(dataframe, timeperiod=SUPERTREND_PERIOD)
        hl2 = (dataframe["high"] + dataframe["low"]) / 2.0
        dataframe["atr"] = average_range
        dataframe["supertrend_direction"] = supertrend_direction(
            dataframe["close"].to_numpy(),
            hl2.to_numpy(),
            average_range.to_numpy(),
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        previous_direction = dataframe["supertrend_direction"].shift(1)
        flip_up = (dataframe["supertrend_direction"] == UPTREND) & (previous_direction == DOWNTREND)

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[flip_up & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[flip_up & (dataframe["volume"] > 0), "enter_tag"] = "supertrend_flip_up"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        previous_direction = dataframe["supertrend_direction"].shift(1)
        flip_down = (dataframe["supertrend_direction"] == DOWNTREND) & (
            previous_direction == UPTREND
        )

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[flip_down & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[flip_down & (dataframe["volume"] > 0), "exit_tag"] = "supertrend_flip_down"
        return dataframe
