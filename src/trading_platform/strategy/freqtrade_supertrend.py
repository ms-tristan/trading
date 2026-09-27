"""The ``supertrend`` house strategy exposed to Freqtrade, ready to be loaded **by name**.

**This module requires the optional ``freqtrade`` extra.**  Importing it runs
:func:`~trading_platform.strategy.freqtrade_adapter.make_freqtrade_strategy` at
module level, which needs ``freqtrade.strategy.IStrategy``; without the extra it
raises :class:`~trading_platform.core.errors.StrategyError` ("install the optional
extra").  That is deliberate and is the reason why
:mod:`trading_platform.strategy` — the public namespace imported by
``import trading_platform`` — must **never** import this module: the whole suite
runs on the ``.[dev]`` extra only, where Freqtrade is absent.  Import it
explicitly::

    from trading_platform.strategy.freqtrade_supertrend import SupertrendFreqtradeStrategy

Freqtrade resolves strategies **by class name** inside ``user_data/strategies``
(``StrategyResolver._search_object``): it walks that directory, keeps the files
whose *text* contains ``class <Name>(`` and keeps the classes whose
``__module__`` equals the file stem.  Neither rule is satisfied by an alias such
as ``SupertrendStrategy = make_freqtrade_strategy("supertrend")`` — the strategy
would silently resolve to ``(None, None)``.  The loadable entry point is
therefore a **literal class statement** in its own file, which is exactly what
``user_data/strategies/SupertrendStrategy.py`` is: a two-line subclass of
:data:`SupertrendFreqtradeStrategy` whose only job is to carry the name Freqtrade
looks up (``--strategy SupertrendStrategy``).

The adapter itself duplicates **no** business logic: the recursive bands, the
direction and the entry/exit rules stay in
:mod:`trading_platform.strategy.supertrend`, and the translation between the two
contracts is the single responsibility of
:mod:`trading_platform.strategy.freqtrade_adapter`.

Why the class pins the ``4h`` grid
----------------------------------
The class-level ``timeframe`` is the literal ``"4h"``, which is the coarse grid
the strategy's own stated hypothesis is written for (``1d`` and ``4h`` — see the
docstring of :mod:`trading_platform.strategy.supertrend`).  The family has **no
recorded verdict** in this repository: it was never swept, so nothing here may be
presented as validated, and the frozen default only picks a grid on which an
ATR(10) band is a structural trailing line rather than intraday noise.  The
adapter still accepts an explicit ``timeframe`` argument, and the realtime layer
passes the *profile* timeframe (and its ``can_short``) through
:func:`~trading_platform.realtime.strategies.freqtrade_strategy_for`: this module
only freezes the safe default of a Freqtrade backtest or bot driven by the
shipped shim.  The class-level ``can_short`` stays ``False`` — the long-only
default — and is switchable per profile the same way.

Public API::

    from trading_platform.strategy.freqtrade_supertrend import (
        SUPERTREND_FREQTRADE_STRATEGY_NAME,   # "SupertrendStrategy" (the Freqtrade class name)
        SupertrendFreqtradeStrategy,          # the generated IStrategy subclass
    )
"""

from __future__ import annotations

from typing import Any

from trading_platform.strategy.freqtrade_adapter import make_freqtrade_strategy

__all__ = [
    "SUPERTREND_FREQTRADE_STRATEGY_NAME",
    "SupertrendFreqtradeStrategy",
]

#: Class name Freqtrade resolves inside ``user_data/strategies``.  It is
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name` applied
#: to the house name (``"supertrend"`` -> ``"SupertrendStrategy"``), i.e. exactly
#: ``SupertrendFreqtradeStrategy.__name__`` and the name carried by the literal
#: ``class`` statement of ``user_data/strategies/SupertrendStrategy.py``.
SUPERTREND_FREQTRADE_STRATEGY_NAME: str = "SupertrendStrategy"

#: The house ``supertrend`` strategy as a Freqtrade ``IStrategy`` subclass.  The
#: class name comes from
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name`
#: (``"supertrend"`` -> ``"SupertrendStrategy"``), and its timeframe is the
#: literal ``"4h"``: one of the two coarse grids the strategy's own hypothesis is
#: stated for (module docstring above).  ``startup_candle_count`` is derived by
#: the adapter from the parameter model — ``10``, the largest integer default
#: (``atr_period``).
SupertrendFreqtradeStrategy: type[Any] = make_freqtrade_strategy("supertrend", timeframe="4h")
