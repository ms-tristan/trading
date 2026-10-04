"""Causal BTC 200-day regime gate shared by the unfiltered breakout and trend profiles.

The rule is deliberately simple and public: the platform only opens a trend or
breakout trade while BTC/USDT -- the market beta every alternative pair follows --
closes above its own 200-day moving average. The same idea is the classic
"200-day moving average" market filter of Mebane Faber, "A Quantitative Approach
to Tactical Asset Allocation" (Journal of Wealth Management, 2007), applied here
as a binary risk-on/risk-off switch rather than as a position size.

This module is a *support module*, not a strategy: it is listed in
``SUPPORT_MODULES`` of :mod:`trading_platform.profiles.catalogue` so the file
discovery never advertises it as a bogus catalogue entry.

Two properties are load-bearing and pinned by ``tests/strategies/test_market_regime.py``:

* **Causality.** A daily candle is only complete at its close, and freqtrade
  stamps it with the candle's OPEN time, so the regime of day D is *not* known
  at any point of day D. The daily regime is therefore shifted by one full day
  before it is forward-filled onto the signal frame: every candle of day D
  carries the regime that was already known at the close of day D-1. Reading the
  daily frame unshifted would be lookahead bias.
* **Fail closed.** A missing or empty daily frame -- which is exactly what a
  fresh live start sees until 201 daily BTC candles exist, and what a dry-run or
  live engine sees without the informative pair declared below -- blocks every
  entry instead of opening one. The gate never guesses a regime.

``informative_pairs`` is what makes the gate reachable outside backtesting: in
DRY_RUN/LIVE ``DataProvider.get_pair_dataframe`` reads the exchange klines cache,
which only holds the whitelist pairs on the strategy timeframe plus the pairs
declared as informative. Without that declaration the daily frame is always
empty and the gate would block every entry forever.
"""

from __future__ import annotations

import pandas as pd
from pandas import DataFrame, Series

#: Pair whose daily close defines the market regime.
BTC_PAIR = "BTC/USDT"

#: Timeframe of the regime series: one candle per day.
BTC_REGIME_TIMEFRAME = "1d"

#: Length of the rolling mean the daily close is compared against (about ten months).
BTC_REGIME_WINDOW = 200

__all__ = [
    "BTC_PAIR",
    "BTC_REGIME_TIMEFRAME",
    "BTC_REGIME_WINDOW",
    "BtcRegimeGateMixin",
]


class BtcRegimeGateMixin:
    """Block every entry while BTC/USDT trades at or below its 200-day moving average.

    Mix it in *before* ``IStrategy`` -- ``class MyStrategy(BtcRegimeGateMixin,
    IStrategy)`` -- and end the host's own ``populate_entry_trend`` with

    ``return super().populate_entry_trend(dataframe, metadata)``

    so the host's signals are handed up the MRO to :meth:`populate_entry_trend`
    of this mixin, which filters them. Without that delegation the host method
    shadows the gate and no signal is ever filtered. Exit signals are never
    filtered: a risk-off regime must always be able to close what it opened.
    """

    #: Pair whose daily close defines the market regime.
    BTC_PAIR: str = BTC_PAIR
    #: Timeframe of the regime series: one candle per day.
    BTC_REGIME_TIMEFRAME: str = BTC_REGIME_TIMEFRAME
    #: Length of the rolling mean the daily close is compared against.
    BTC_REGIME_WINDOW: int = BTC_REGIME_WINDOW

    def informative_pairs(self) -> list[tuple[str, str]]:
        """Declare the daily BTC pair the gate reads, so freqtrade caches it.

        Required in DRY_RUN/LIVE, where ``get_pair_dataframe`` only serves the
        whitelist pairs on the strategy timeframe plus the pairs declared here.
        """
        return [(self.BTC_PAIR, self.BTC_REGIME_TIMEFRAME)]

    def btc_risk_on(self, dataframe: DataFrame) -> Series:
        """Return the boolean risk-on regime per row of ``dataframe``.

        The daily regime is ``close > rolling(200).mean()``, shifted by one full
        day -- a daily candle is only complete at its close -- and then forward
        filled onto the timestamps of the signal frame. Every row the daily
        series cannot answer for is ``False``: a missing or unusable daily frame
        blocks the entry rather than opening it.
        """
        blocked = Series(False, index=dataframe.index, dtype=bool)

        if getattr(self, "dp", None) is None:
            return blocked
        try:
            daily = self.dp.get_pair_dataframe(self.BTC_PAIR, self.BTC_REGIME_TIMEFRAME)
        except Exception:  # noqa: BLE001 - an unusable provider fails closed, never open
            return blocked
        if not isinstance(daily, DataFrame) or daily.empty:
            return blocked
        if "date" not in daily.columns or "close" not in daily.columns:
            return blocked
        # The rolling mean needs WINDOW candles, and the one-day shift needs one more:
        # below that the gate has no opinion and stays closed.
        if len(daily) < self.BTC_REGIME_WINDOW + 1:
            return blocked

        try:
            daily = daily.sort_values("date")
            regime = daily["close"] > daily["close"].rolling(self.BTC_REGIME_WINDOW).mean()
            # One full day: the candle of day D is only complete at its close, so day D
            # itself may only use the regime known at the close of day D-1.
            regime = regime.shift(1)
            regime.index = pd.to_datetime(daily["date"], utc=True)
            aligned = regime.reindex(pd.to_datetime(dataframe["date"], utc=True), method="ffill")
        except (KeyError, TypeError, ValueError):  # pragma: no cover - defensive
            return blocked

        return aligned.fillna(False).astype(bool).set_axis(dataframe.index)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Return the host signals, with every risk-off candle forced back to no entry.

        Reached by the host's own ``populate_entry_trend``, which calls ``super()``
        once it has written its signals; the ``super()`` call below then lands on
        ``IStrategy``, whose implementation returns the dataframe unchanged.
        """
        dataframe = super().populate_entry_trend(dataframe, metadata)

        risk_on = self.btc_risk_on(dataframe)
        dataframe.loc[~risk_on, "enter_long"] = 0
        dataframe.loc[~risk_on, "enter_tag"] = ""
        return dataframe
