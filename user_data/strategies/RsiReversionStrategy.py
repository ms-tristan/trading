"""Freqtrade-loadable name of the house ``rsi_reversion`` strategy.

Freqtrade's ``StrategyResolver`` keeps a file only if its **text** contains
``class RsiReversionStrategy(`` and keeps the class only if its ``__module__``
equals the file stem, so this alias cannot be a factory call: the literal
``class`` statement below is what makes ``--strategy RsiReversionStrategy``
resolve.

Not a single indicator or entry/exit rule lives here: everything comes from
``trading_platform.strategy.freqtrade_rsi_reversion``, which requires the optional
``freqtrade`` extra.
"""

from trading_platform.strategy.freqtrade_rsi_reversion import RsiReversionFreqtradeStrategy


class RsiReversionStrategy(RsiReversionFreqtradeStrategy):
    """Freqtrade-loadable name of the house 'rsi_reversion' strategy."""
