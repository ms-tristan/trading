"""VolTargetedTrendStrategy: trend following with a volatility-regime overlay.

Public sources of the idea:

* Mebane T. Faber, *A Quantitative Approach to Tactical Asset Allocation*, Journal
  of Wealth Management (2007) -- the 200-period trend rule that decides direction;
* Harvey, Hoyle, Korgaonkar, Rattray, Sargaison & Van Hemert, *The Impact of
  Volatility Targeting*, Journal of Portfolio Management 45(1) (2018) -- the
  finding that scaling exposure by volatility reliably reduces **tail risk**. The
  same paper is explicit that the Sharpe benefit is weaker than commonly claimed
  and is concentrated in risk assets, so this strategy is presented as a drawdown
  control, not as an alpha claim;
* the volatility-clustering literature (Mandelbrot 1963; Engle's ARCH, 1982) --
  high volatility arrives in clusters, which is what makes "stand aside when
  volatility is elevated" an implementable rule rather than hindsight.

The design. Direction comes from the 200-period trend, which is the part of the
rule set with the most published support. The overlay does two things:

1. it will not *open* a position unless realised volatility is below its own
   trailing median, so entries avoid the violent regimes where trend rules suffer
   their worst whipsaws;
2. it *closes* a position when volatility breaks above its 75th percentile, so a
   drawdown is cut early rather than ridden out.

Both thresholds are rolling and therefore causal: at every candle they use only
information available at that candle. That matters -- a fixed threshold, or one
computed over the whole sample, would smuggle the future into the signal and make
the backtest meaningless.

The honest expectation, per the literature, is a strategy that underperforms
buy-and-hold in raw return in a bull market but avoids the worst of the drawdowns.
It is measured against buy-and-hold for exactly that reason.
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class VolTargetedTrendStrategy(IStrategy):
    """Hold the 200-period trend, but only when volatility is not elevated."""

    INTERFACE_VERSION = 3

    can_short = False
    process_only_new_candles = True
    use_exit_signal = True

    timeframe = "4h"
    # SMA(200) plus the 500-candle volatility quantile window need room to warm up.
    startup_candle_count = 700

    stoploss = -0.12
    # The exit is the volatility/trend signal; the ROI must not cut a healthy trend.
    minimal_roi = {"0": 1.0}

    #: Window for the realised-volatility estimate, in candles.
    volatility_window = 72
    #: Window over which the volatility median and 75th percentile are measured.
    regime_window = 500

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        import numpy as np

        dataframe["sma_trend"] = ta.SMA(dataframe, timeperiod=200)
        log_returns = np.log(dataframe["close"] / dataframe["close"].shift(1))
        dataframe["realised_vol"] = log_returns.rolling(self.volatility_window).std()
        dataframe["vol_median"] = (
            dataframe["realised_vol"].rolling(self.regime_window, min_periods=100).median()
        )
        dataframe["vol_q75"] = (
            dataframe["realised_vol"].rolling(self.regime_window, min_periods=100).quantile(0.75)
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        above_trend = dataframe["close"] > dataframe["sma_trend"]
        calm = dataframe["realised_vol"] < dataframe["vol_median"]
        # Require the thresholds to exist: before the warm-up they are NaN and the
        # comparison is False, which is the safe default.
        ready = dataframe["vol_median"].notna() & dataframe["sma_trend"].notna()
        buy_signal = above_trend & calm & ready

        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_long"] = 1
        dataframe.loc[buy_signal & (dataframe["volume"] > 0), "enter_tag"] = "vol_targeted_trend"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        below_trend = dataframe["close"] < dataframe["sma_trend"]
        storm = dataframe["realised_vol"] > dataframe["vol_q75"]
        sell_signal = below_trend | storm

        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""
        dataframe.loc[sell_signal & (dataframe["volume"] > 0), "exit_long"] = 1
        dataframe.loc[sell_signal & (dataframe["volume"] > 0), "exit_tag"] = "vol_regime_exit"
        return dataframe
