"""Freqtrade-loadable name of the house ``bollinger`` strategy.

Freqtrade's ``StrategyResolver`` keeps a file only if its **text** contains
``class BollingerStrategy(`` and keeps the class only if its ``__module__`` equals
the file stem, so this alias cannot be a factory call: the literal ``class``
statement below is what makes ``--strategy BollingerStrategy`` resolve.

Not a single indicator or entry/exit rule lives here: everything comes from
``trading_platform.strategy.freqtrade_bollinger``, which requires the optional
``freqtrade`` extra.
"""

from trading_platform.strategy.freqtrade_bollinger import BollingerFreqtradeStrategy


class BollingerStrategy(BollingerFreqtradeStrategy):
    """Freqtrade-loadable name of the house 'bollinger' strategy."""
