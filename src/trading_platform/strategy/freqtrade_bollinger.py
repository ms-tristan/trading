"""The ``bollinger`` house strategy exposed to Freqtrade, ready to be loaded **by name**.

**This module requires the optional ``freqtrade`` extra.**  Importing it runs
:func:`~trading_platform.strategy.freqtrade_adapter.make_freqtrade_strategy` at
module level, which needs ``freqtrade.strategy.IStrategy``; without the extra it
raises :class:`~trading_platform.core.errors.StrategyError` ("install the optional
extra").  That is deliberate and is the reason why
:mod:`trading_platform.strategy` — the public namespace imported by
``import trading_platform`` — must **never** import this module: the whole suite
runs on the ``.[dev]`` extra only, where Freqtrade is absent.  Import it
explicitly::

    from trading_platform.strategy.freqtrade_bollinger import BollingerFreqtradeStrategy

Freqtrade resolves strategies **by class name** inside ``user_data/strategies``
(``StrategyResolver._search_object``): it walks that directory, keeps the files
whose *text* contains ``class <Name>(`` and keeps the classes whose
``__module__`` equals the file stem.  Neither rule is satisfied by an alias such
as ``BollingerStrategy = make_freqtrade_strategy("bollinger")`` — the strategy
would silently resolve to ``(None, None)``.  The loadable entry point is
therefore a **literal class statement** in its own file, which is exactly what
``user_data/strategies/BollingerStrategy.py`` is: a two-line subclass of
:data:`BollingerFreqtradeStrategy` whose only job is to carry the name Freqtrade
looks up (``--strategy BollingerStrategy``).

The adapter itself duplicates **no** business logic: indicators and entry/exit
rules stay in :mod:`trading_platform.strategy.bollinger`, and the translation
between the two contracts is the single responsibility of
:mod:`trading_platform.strategy.freqtrade_adapter`.

Why the class pins the ``1h`` grid
----------------------------------
The class-level ``timeframe`` is the literal ``"1h"``, because that is the grid
the repository's own Bollinger evidence is measured on: the reversion variant V1
(1h, long-only) reached a DEV Sharpe of ``0.706`` with a maximum drawdown of
``-0.32`` and **failed the holdout** (``docs/strategies.md``,
``.scratch/research/FINDINGS2.md``).  The recorded verdict is negative, so the
frozen default repeats the grid the measurement was taken on; reading the same
rule on another grid is a new hypothesis, not a second reading of that result.
The adapter still accepts an explicit ``timeframe`` argument, and the realtime
layer passes the *profile* timeframe (and its ``can_short``) through
:func:`~trading_platform.realtime.strategies.freqtrade_strategy_for`: this module
only freezes the safe default of a Freqtrade backtest or bot driven by the
shipped shim.  The class-level ``can_short`` stays ``False`` — the long-only
default of a spot reversion rule — and is switchable per profile the same way.

Public API::

    from trading_platform.strategy.freqtrade_bollinger import (
        BOLLINGER_FREQTRADE_STRATEGY_NAME,   # "BollingerStrategy" (the Freqtrade class name)
        BollingerFreqtradeStrategy,          # the generated IStrategy subclass
    )
"""

from __future__ import annotations

from typing import Any

from trading_platform.strategy.freqtrade_adapter import make_freqtrade_strategy

__all__ = [
    "BOLLINGER_FREQTRADE_STRATEGY_NAME",
    "BollingerFreqtradeStrategy",
]

#: Class name Freqtrade resolves inside ``user_data/strategies``.  It is
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name` applied
#: to the house name (``"bollinger"`` -> ``"BollingerStrategy"``), i.e. exactly
#: ``BollingerFreqtradeStrategy.__name__`` and the name carried by the literal
#: ``class`` statement of ``user_data/strategies/BollingerStrategy.py``.
BOLLINGER_FREQTRADE_STRATEGY_NAME: str = "BollingerStrategy"

#: The house ``bollinger`` strategy as a Freqtrade ``IStrategy`` subclass.  The
#: class name comes from
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name`
#: (``"bollinger"`` -> ``"BollingerStrategy"``), and its timeframe is the literal
#: ``"1h"``: the grid the recorded Bollinger verdict was measured on (module
#: docstring above).  ``startup_candle_count`` is derived by the adapter from the
#: parameter model — ``20``, the largest integer default (``period``).
BollingerFreqtradeStrategy: type[Any] = make_freqtrade_strategy("bollinger", timeframe="1h")
