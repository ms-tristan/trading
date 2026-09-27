"""Freqtrade-loadable name of the house ``keltner`` strategy.

Freqtrade's ``StrategyResolver`` keeps a file only if its **text** contains
``class KeltnerStrategy(`` and keeps the class only if its ``__module__`` equals
the file stem, so this alias cannot be a factory call: the literal ``class``
statement below is what makes ``--strategy KeltnerStrategy`` resolve.

Not a single indicator or entry/exit rule lives here: everything comes from
``trading_platform.strategy.freqtrade_keltner``, which requires the optional
``freqtrade`` extra.
"""

from trading_platform.strategy.freqtrade_keltner import KeltnerFreqtradeStrategy


class KeltnerStrategy(KeltnerFreqtradeStrategy):
    """Freqtrade-loadable name of the house 'keltner' strategy."""
