"""The ``macd`` house strategy exposed to Freqtrade, ready to be loaded **by name**.

**This module requires the optional ``freqtrade`` extra.**  Importing it runs
:func:`~trading_platform.strategy.freqtrade_adapter.make_freqtrade_strategy` at
module level, which needs ``freqtrade.strategy.IStrategy``; without the extra it
raises :class:`~trading_platform.core.errors.StrategyError` ("install the optional
extra").  That is deliberate and is the reason why
:mod:`trading_platform.strategy` — the public namespace imported by
``import trading_platform`` — must **never** import this module: the whole suite
runs on the ``.[dev]`` extra only, where Freqtrade is absent.  Import it
explicitly::

    from trading_platform.strategy.freqtrade_macd import MacdFreqtradeStrategy

Freqtrade resolves strategies **by class name** inside ``user_data/strategies``
(``StrategyResolver._search_object``): it walks that directory, keeps the files
whose *text* contains ``class <Name>(`` and keeps the classes whose
``__module__`` equals the file stem.  Neither rule is satisfied by an alias such
as ``MacdStrategy = make_freqtrade_strategy("macd")`` — the strategy would
silently resolve to ``(None, None)``.  The loadable entry point is therefore a
**literal class statement** in its own file, which is exactly what
``user_data/strategies/MacdStrategy.py`` is: a two-line subclass of
:data:`MacdFreqtradeStrategy` whose only job is to carry the name Freqtrade looks
up (``--strategy MacdStrategy``).

The adapter itself duplicates **no** business logic: indicators and entry/exit
rules stay in :mod:`trading_platform.strategy.macd`, and the translation between
the two contracts is the single responsibility of
:mod:`trading_platform.strategy.freqtrade_adapter`.

Why the class pins the ``4h`` grid
----------------------------------
The class-level ``timeframe`` is the literal ``"4h"``, which is the coarse grid
the strategy's own stated hypothesis is written for (``1d`` and ``4h`` — see the
docstring of :mod:`trading_platform.strategy.macd`).  The MACD family has **no
recorded verdict** in this repository: it was never swept, so nothing here may be
presented as validated, and the frozen default only picks the grid the
platform's own trend evidence survived on (``4h``/``1d`` cleared for
``momentum``; ``1h`` degraded the most on the holdout).  The adapter still
accepts an explicit ``timeframe`` argument, and the realtime layer passes the
*profile* timeframe (and its ``can_short``) through
:func:`~trading_platform.realtime.strategies.freqtrade_strategy_for`: this module
only freezes the safe default of a Freqtrade backtest or bot driven by the
shipped shim.  The class-level ``can_short`` stays ``False`` — the long-only
default — and is switchable per profile the same way.

Public API::

    from trading_platform.strategy.freqtrade_macd import (
        MACD_FREQTRADE_STRATEGY_NAME,   # "MacdStrategy" (the Freqtrade class name)
        MacdFreqtradeStrategy,          # the generated IStrategy subclass
    )
"""

from __future__ import annotations

from typing import Any

from trading_platform.strategy.freqtrade_adapter import make_freqtrade_strategy

__all__ = [
    "MACD_FREQTRADE_STRATEGY_NAME",
    "MacdFreqtradeStrategy",
]

#: Class name Freqtrade resolves inside ``user_data/strategies``.  It is
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name` applied
#: to the house name (``"macd"`` -> ``"MacdStrategy"``), i.e. exactly
#: ``MacdFreqtradeStrategy.__name__`` and the name carried by the literal
#: ``class`` statement of ``user_data/strategies/MacdStrategy.py``.
MACD_FREQTRADE_STRATEGY_NAME: str = "MacdStrategy"

#: The house ``macd`` strategy as a Freqtrade ``IStrategy`` subclass.  The class
#: name comes from
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name`
#: (``"macd"`` -> ``"MacdStrategy"``), and its timeframe is the literal ``"4h"``:
#: one of the two coarse grids the strategy's own hypothesis is stated for
#: (module docstring above).  ``startup_candle_count`` is derived by the adapter
#: from the parameter model — ``26``, the largest integer default
#: (``slow_period``).
MacdFreqtradeStrategy: type[Any] = make_freqtrade_strategy("macd", timeframe="4h")
