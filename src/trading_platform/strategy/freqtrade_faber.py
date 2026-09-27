"""The ``faber`` house strategy exposed to Freqtrade, ready to be loaded **by name**.

**This module requires the optional ``freqtrade`` extra.**  Importing it runs
:func:`~trading_platform.strategy.freqtrade_adapter.make_freqtrade_strategy` at
module level, which needs ``freqtrade.strategy.IStrategy``; without the extra it
raises :class:`~trading_platform.core.errors.StrategyError` ("install the optional
extra").  That is deliberate and is the reason why
:mod:`trading_platform.strategy` — the public namespace imported by
``import trading_platform`` — must **never** import this module: the whole suite
runs on the ``.[dev]`` extra only, where Freqtrade is absent.  Import it
explicitly::

    from trading_platform.strategy.freqtrade_faber import FaberFreqtradeStrategy

Freqtrade resolves strategies **by class name** inside ``user_data/strategies``
(``StrategyResolver._search_object``): it walks that directory, keeps the files
whose *text* contains ``class <Name>(`` and keeps the classes whose
``__module__`` equals the file stem.  Neither rule is satisfied by an alias such
as ``FaberStrategy = make_freqtrade_strategy("faber")`` — the strategy would
silently resolve to ``(None, None)``.  The loadable entry point is therefore a
**literal class statement** in its own file, which is exactly what
``user_data/strategies/FaberStrategy.py`` is: a two-line subclass of
:data:`FaberFreqtradeStrategy` whose only job is to carry the name Freqtrade
looks up (``--strategy FaberStrategy``).

The adapter itself duplicates **no** business logic: indicators and entry/exit
rules stay in :mod:`trading_platform.strategy.faber`, and the translation between
the two contracts is the single responsibility of
:mod:`trading_platform.strategy.freqtrade_adapter`.

Why the class pins the ``1d`` grid
----------------------------------
The class-level ``timeframe`` is the literal ``"1d"``, because the published
Faber rule is a **200-day** moving average and 200 candles *are* 200 days on the
daily grid: ``1d`` is the only grid where this implementation is the published
rule rather than an approximation of it (on ``4h`` the same 200 candles span
about 33 days).  The adapter still accepts an explicit ``timeframe`` argument,
and the realtime layer passes the *profile* timeframe through
:func:`~trading_platform.realtime.strategies.freqtrade_strategy_for`: this module
only freezes the safe default of a Freqtrade backtest or bot driven by the
shipped shim.  ``can_short`` is not passed and stays ``False``: the strategy is
long-only **by construction** — its parameter model declares no ``allow_short``
field at all — so there is nothing to switch per profile.

Public API::

    from trading_platform.strategy.freqtrade_faber import (
        FABER_FREQTRADE_STRATEGY_NAME,   # "FaberStrategy" (the Freqtrade class name)
        FaberFreqtradeStrategy,          # the generated IStrategy subclass
    )
"""

from __future__ import annotations

from typing import Any

from trading_platform.strategy.freqtrade_adapter import make_freqtrade_strategy

__all__ = [
    "FABER_FREQTRADE_STRATEGY_NAME",
    "FaberFreqtradeStrategy",
]

#: Class name Freqtrade resolves inside ``user_data/strategies``.  It is
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name` applied
#: to the house name (``"faber"`` -> ``"FaberStrategy"``), i.e. exactly
#: ``FaberFreqtradeStrategy.__name__`` and the name carried by the literal
#: ``class`` statement of ``user_data/strategies/FaberStrategy.py``.
FABER_FREQTRADE_STRATEGY_NAME: str = "FaberStrategy"

#: The house ``faber`` strategy as a Freqtrade ``IStrategy`` subclass.  The class
#: name comes from
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name`
#: (``"faber"`` -> ``"FaberStrategy"``), and its timeframe is the literal
#: ``"1d"``: the grid on which the 200-candle average *is* the published 200-day
#: rule (module docstring above).  ``startup_candle_count`` is derived by the
#: adapter from the parameter model — ``200``, the largest integer default
#: (``sma_period``).
FaberFreqtradeStrategy: type[Any] = make_freqtrade_strategy("faber", timeframe="1d")
