"""The ``basic`` house strategy exposed to Freqtrade, ready to be loaded **by name**.

**This module requires the optional ``freqtrade`` extra.**  Importing it runs
:func:`~trading_backtest.strategy.freqtrade_adapter.make_freqtrade_strategy` at
module level, which needs ``freqtrade.strategy.IStrategy``; without the extra it
raises :class:`~trading_backtest.core.errors.StrategyError` ("install the optional
extra").  That is deliberate and is the reason why
:mod:`trading_backtest.strategy` — the public namespace imported by
``import trading_backtest`` — must **never** import this module: the whole suite
runs on the ``.[dev]`` extra only, where Freqtrade is absent.  Import it
explicitly::

    from trading_backtest.strategy.freqtrade_basic import BasicFreqtradeStrategy

Freqtrade resolves strategies **by class name** inside ``user_data/strategies``
(``StrategyResolver._search_object``): it walks that directory, keeps the files
whose *text* contains ``class <Name>(`` and keeps the classes whose
``__module__`` equals the file stem.  Neither rule is satisfied by an alias such
as ``BasicStrategy = make_freqtrade_strategy("basic")`` — the strategy would
silently resolve to ``(None, None)``.  The loadable entry point is therefore a
**literal class statement** in its own file, which is exactly what
``user_data/strategies/BasicStrategy.py`` is: a two-line subclass of
:data:`BasicFreqtradeStrategy` whose only job is to carry the name Freqtrade
looks up (``config/freqtrade_config.json`` and ``config/freqtrade_dryrun.json``
both declare ``"strategy": "BasicStrategy"``).

The adapter itself duplicates **no** business logic: indicators and entry/exit
rules stay in :mod:`trading_backtest.strategy.basic`, and the translation between
the two contracts is the single responsibility of
:mod:`trading_backtest.strategy.freqtrade_adapter`.

Public API::

    from trading_backtest.strategy.freqtrade_basic import (
        BASIC_FREQTRADE_STRATEGY_NAME,   # "BasicStrategy" (the Freqtrade class name)
        BasicFreqtradeStrategy,          # the generated IStrategy subclass
    )
"""

from __future__ import annotations

from typing import Any

from trading_backtest.core.constants import DEFAULT_TIMEFRAME
from trading_backtest.freqtrade import DEFAULT_STRATEGY_NAME
from trading_backtest.strategy.freqtrade_adapter import make_freqtrade_strategy

__all__ = [
    "BASIC_FREQTRADE_STRATEGY_NAME",
    "BasicFreqtradeStrategy",
]

#: Class name Freqtrade resolves inside ``user_data/strategies``.  It is
#: :data:`~trading_backtest.freqtrade.DEFAULT_STRATEGY_NAME` (``"BasicStrategy"``),
#: the very value declared by ``config/freqtrade_config.json`` and
#: ``config/freqtrade_dryrun.json``: a single source of truth for the name.
BASIC_FREQTRADE_STRATEGY_NAME: str = DEFAULT_STRATEGY_NAME

#: The house ``basic`` strategy as a Freqtrade ``IStrategy`` subclass.  The class
#: name comes from
#: :func:`~trading_backtest.strategy.freqtrade_adapter.default_class_name`
#: (``"basic"`` -> ``"BasicStrategy"``), and its timeframe is the house
#: default (:data:`~trading_backtest.core.constants.DEFAULT_TIMEFRAME`, ``"1h"``).
BasicFreqtradeStrategy: type[Any] = make_freqtrade_strategy("basic", timeframe=DEFAULT_TIMEFRAME)
