"""The ``dual_thrust`` house strategy exposed to Freqtrade, ready to be loaded **by name**.

**This module requires the optional ``freqtrade`` extra.**  Importing it runs
:func:`~trading_platform.strategy.freqtrade_adapter.make_freqtrade_strategy` at
module level, which needs ``freqtrade.strategy.IStrategy``; without the extra it
raises :class:`~trading_platform.core.errors.StrategyError` ("install the optional
extra").  That is deliberate and is the reason why
:mod:`trading_platform.strategy` — the public namespace imported by
``import trading_platform`` — must **never** import this module: the whole suite
runs on the ``.[dev]`` extra only, where Freqtrade is absent.  Import it
explicitly::

    from trading_platform.strategy.freqtrade_dual_thrust import DualThrustFreqtradeStrategy

Freqtrade resolves strategies **by class name** inside ``user_data/strategies``
(``StrategyResolver._search_object``): it walks that directory, keeps the files
whose *text* contains ``class <Name>(`` and keeps the classes whose
``__module__`` equals the file stem.  Neither rule is satisfied by an alias such
as ``DualThrustStrategy = make_freqtrade_strategy("dual_thrust")`` — the strategy
would silently resolve to ``(None, None)``.  The loadable entry point is
therefore a **literal class statement** in its own file, which is exactly what
``user_data/strategies/DualThrustStrategy.py`` is: a two-line subclass of
:data:`DualThrustFreqtradeStrategy` whose only job is to carry the name Freqtrade
looks up (``--strategy DualThrustStrategy``).

The adapter itself duplicates **no** business logic: indicators and entry/exit
rules stay in :mod:`trading_platform.strategy.dual_thrust`, and the translation
between the two contracts is the single responsibility of
:mod:`trading_platform.strategy.freqtrade_adapter`.

Why the class pins the ``1h`` grid
----------------------------------
The class-level ``timeframe`` is the literal ``"1h"``, because the published
Dual Thrust rule is an **intraday** one: its bars are usually minutes, and
deploying it on a daily grid is an extrapolation of the rule rather than the
rule itself (see the docstring of
:mod:`trading_platform.strategy.dual_thrust`).  ``1h`` is the coarsest grid that
stays inside that intraday reading, which keeps the default of a Freqtrade
backtest faithful to the reference while avoiding the enormous warm-ups of the
sub-hourly grids.  There is **no** recorded verdict for this family in the
repository's research ledger — it was never put through the validation protocol —
so this module claims nothing about it.  The adapter still accepts an explicit
``timeframe`` argument, and the realtime layer passes the *profile* timeframe
(and its ``can_short``) through
:func:`~trading_platform.realtime.strategies.freqtrade_strategy_for`: this module
only freezes the safe default of a Freqtrade backtest or bot driven by the
shipped shim.  The class-level ``can_short`` stays ``False`` — the long-only
default — and is switchable per profile the same way.

Public API::

    from trading_platform.strategy.freqtrade_dual_thrust import (
        DUAL_THRUST_FREQTRADE_STRATEGY_NAME,   # "DualThrustStrategy" (the Freqtrade class name)
        DualThrustFreqtradeStrategy,           # the generated IStrategy subclass
    )
"""

from __future__ import annotations

from typing import Any

from trading_platform.strategy.freqtrade_adapter import make_freqtrade_strategy

__all__ = [
    "DUAL_THRUST_FREQTRADE_STRATEGY_NAME",
    "DualThrustFreqtradeStrategy",
]

#: Class name Freqtrade resolves inside ``user_data/strategies``.  It is
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name` applied
#: to the house name (``"dual_thrust"`` -> ``"DualThrustStrategy"``), i.e. exactly
#: ``DualThrustFreqtradeStrategy.__name__`` and the name carried by the literal
#: ``class`` statement of ``user_data/strategies/DualThrustStrategy.py``.
DUAL_THRUST_FREQTRADE_STRATEGY_NAME: str = "DualThrustStrategy"

#: The house ``dual_thrust`` strategy as a Freqtrade ``IStrategy`` subclass.  The
#: class name comes from
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name`
#: (``"dual_thrust"`` -> ``"DualThrustStrategy"``), and its timeframe is the
#: literal ``"1h"``: the coarsest grid that is still intraday, which is what the
#: published rule assumes (module docstring above).  ``startup_candle_count`` is
#: derived by the adapter from the parameter model — ``14``, the largest integer
#: default (``atr_period``).
DualThrustFreqtradeStrategy: type[Any] = make_freqtrade_strategy("dual_thrust", timeframe="1h")
