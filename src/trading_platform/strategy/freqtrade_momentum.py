"""The ``momentum`` house strategy exposed to Freqtrade, ready to be loaded **by name**.

**This module requires the optional ``freqtrade`` extra.**  Importing it runs
:func:`~trading_platform.strategy.freqtrade_adapter.make_freqtrade_strategy` at
module level, which needs ``freqtrade.strategy.IStrategy``; without the extra it
raises :class:`~trading_platform.core.errors.StrategyError` ("install the optional
extra").  That is deliberate and is the reason why
:mod:`trading_platform.strategy` — the public namespace imported by
``import trading_platform`` — must **never** import this module: the whole suite
runs on the ``.[dev]`` extra only, where Freqtrade is absent.  Import it
explicitly::

    from trading_platform.strategy.freqtrade_momentum import MomentumFreqtradeStrategy

Freqtrade resolves strategies **by class name** inside ``user_data/strategies``
(``StrategyResolver._search_object``): it walks that directory, keeps the files
whose *text* contains ``class <Name>(`` and keeps the classes whose
``__module__`` equals the file stem.  Neither rule is satisfied by an alias such
as ``MomentumStrategy = make_freqtrade_strategy("momentum")`` — the strategy would
silently resolve to ``(None, None)``.  The loadable entry point is therefore a
**literal class statement** in its own file, which is exactly what
``user_data/strategies/MomentumStrategy.py`` is: a two-line subclass of
:data:`MomentumFreqtradeStrategy` whose only job is to carry the name Freqtrade
looks up (``--strategy MomentumStrategy``).

The adapter itself duplicates **no** business logic: indicators and entry/exit
rules stay in :mod:`trading_platform.strategy.momentum`, and the translation
between the two contracts is the single responsibility of
:mod:`trading_platform.strategy.freqtrade_adapter`.

Why the class pins the ``4h`` grid
----------------------------------
The class-level ``timeframe`` is the literal ``"4h"``, because ``4h`` and ``1d``
are the two candle grids the momentum research validated as deployable, and
``1h`` degraded the most on the untouched holdout window (see
``docs/strategies.md``).  The adapter still accepts an explicit ``timeframe``
argument, and the realtime layer passes the *profile* timeframe (and its
``can_short``) through
:func:`~trading_platform.realtime.strategies.freqtrade_strategy_for`: this module
only freezes the safe default of a Freqtrade backtest or bot driven by the
shipped shim.  The class-level ``can_short`` stays ``False`` — the long-only
default — and is switchable per profile the same way.

Public API::

    from trading_platform.strategy.freqtrade_momentum import (
        MOMENTUM_FREQTRADE_STRATEGY_NAME,   # "MomentumStrategy" (the Freqtrade class name)
        MomentumFreqtradeStrategy,          # the generated IStrategy subclass
    )
"""

from __future__ import annotations

from typing import Any

from trading_platform.strategy.freqtrade_adapter import make_freqtrade_strategy

__all__ = [
    "MOMENTUM_FREQTRADE_STRATEGY_NAME",
    "MomentumFreqtradeStrategy",
]

#: Class name Freqtrade resolves inside ``user_data/strategies``.  It is
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name` applied
#: to the house name (``"momentum"`` -> ``"MomentumStrategy"``), i.e. exactly
#: ``MomentumFreqtradeStrategy.__name__`` and the name carried by the literal
#: ``class`` statement of ``user_data/strategies/MomentumStrategy.py``.
MOMENTUM_FREQTRADE_STRATEGY_NAME: str = "MomentumStrategy"

#: The house ``momentum`` strategy as a Freqtrade ``IStrategy`` subclass.  The
#: class name comes from
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name`
#: (``"momentum"`` -> ``"MomentumStrategy"``), and its timeframe is the literal
#: ``"4h"``: the validated deployable grid, not the house default (``"1h"``),
#: which degraded the most on the holdout (module docstring above).
MomentumFreqtradeStrategy: type[Any] = make_freqtrade_strategy("momentum", timeframe="4h")
