"""Freqtrade-loadable name of the house ``basic`` strategy.

Freqtrade's ``StrategyResolver`` keeps a file only if its **text** contains
``class BasicStrategy(`` and keeps the class only if its ``__module__`` equals
the file stem, so this alias cannot be a factory call: the literal ``class``
statement below is what makes ``--strategy BasicStrategy`` resolve.

Not a single indicator or entry/exit rule lives here: everything comes from
``trading_backtest.strategy.freqtrade_basic``, which requires the optional
``freqtrade`` extra.
"""

from trading_backtest.strategy.freqtrade_basic import BasicFreqtradeStrategy


class BasicStrategy(BasicFreqtradeStrategy):
    """Freqtrade-loadable name of the house 'basic' strategy."""
