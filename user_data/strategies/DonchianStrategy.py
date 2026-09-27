"""Freqtrade-loadable name of the house ``donchian`` strategy.

Freqtrade's ``StrategyResolver`` keeps a file only if its **text** contains
``class DonchianStrategy(`` and keeps the class only if its ``__module__`` equals
the file stem, so this alias cannot be a factory call: the literal ``class``
statement below is what makes ``--strategy DonchianStrategy`` resolve.

Not a single indicator or entry/exit rule lives here: everything comes from
``trading_platform.strategy.freqtrade_donchian``, which requires the optional
``freqtrade`` extra.
"""

from trading_platform.strategy.freqtrade_donchian import DonchianFreqtradeStrategy


class DonchianStrategy(DonchianFreqtradeStrategy):
    """Freqtrade-loadable name of the house 'donchian' strategy."""
