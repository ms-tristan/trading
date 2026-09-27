"""Freqtrade-loadable name of the house ``dual_thrust`` strategy.

Freqtrade's ``StrategyResolver`` keeps a file only if its **text** contains
``class DualThrustStrategy(`` and keeps the class only if its ``__module__``
equals the file stem, so this alias cannot be a factory call: the literal
``class`` statement below is what makes ``--strategy DualThrustStrategy``
resolve.

Not a single indicator or entry/exit rule lives here: everything comes from
``trading_platform.strategy.freqtrade_dual_thrust``, which requires the optional
``freqtrade`` extra.
"""

from trading_platform.strategy.freqtrade_dual_thrust import DualThrustFreqtradeStrategy


class DualThrustStrategy(DualThrustFreqtradeStrategy):
    """Freqtrade-loadable name of the house 'dual_thrust' strategy."""
