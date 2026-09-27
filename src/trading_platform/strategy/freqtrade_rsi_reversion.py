"""The ``rsi_reversion`` house strategy exposed to Freqtrade, ready to be loaded **by name**.

**This module requires the optional ``freqtrade`` extra.**  Importing it runs
:func:`~trading_platform.strategy.freqtrade_adapter.make_freqtrade_strategy` at
module level, which needs ``freqtrade.strategy.IStrategy``; without the extra it
raises :class:`~trading_platform.core.errors.StrategyError` ("install the optional
extra").  That is deliberate and is the reason why
:mod:`trading_platform.strategy` — the public namespace imported by
``import trading_platform`` — must **never** import this module: the whole suite
runs on the ``.[dev]`` extra only, where Freqtrade is absent.  Import it
explicitly::

    from trading_platform.strategy.freqtrade_rsi_reversion import RsiReversionFreqtradeStrategy

Freqtrade resolves strategies **by class name** inside ``user_data/strategies``
(``StrategyResolver._search_object``): it walks that directory, keeps the files
whose *text* contains ``class <Name>(`` and keeps the classes whose
``__module__`` equals the file stem.  Neither rule is satisfied by an alias such
as ``RsiReversionStrategy = make_freqtrade_strategy("rsi_reversion")`` — the
strategy would silently resolve to ``(None, None)``.  The loadable entry point is
therefore a **literal class statement** in its own file, which is exactly what
``user_data/strategies/RsiReversionStrategy.py`` is: a two-line subclass of
:data:`RsiReversionFreqtradeStrategy` whose only job is to carry the name
Freqtrade looks up (``--strategy RsiReversionStrategy``).

The adapter itself duplicates **no** business logic: indicators and entry/exit
rules stay in :mod:`trading_platform.strategy.rsi_reversion`, and the translation
between the two contracts is the single responsibility of
:mod:`trading_platform.strategy.freqtrade_adapter`.

Why the class pins the ``1d`` grid
----------------------------------
The class-level ``timeframe`` is the literal ``"1d"``, because the published
Connors RSI(2) pullback is a **daily** rule: it was measured on daily bars, and
its whole premise — a temporary liquidity-driven dislocation inside a long-term
uptrend — is a statement about daily closes.  ``1d`` is therefore the grid on
which this implementation is the published rule rather than a transposition of
it, and it is also the grid on which the 200-period trend EMA keeps its
published meaning (200 days).  The family has **no recorded verdict** in this
repository — it was never swept, so nothing here may be presented as validated.
The adapter still accepts an explicit ``timeframe`` argument, and the realtime
layer passes the *profile* timeframe (and its ``can_short``) through
:func:`~trading_platform.realtime.strategies.freqtrade_strategy_for`: this module
only freezes the safe default of a Freqtrade backtest or bot driven by the
shipped shim.  The class-level ``can_short`` stays ``False`` — the long-only
default of the published rule — and is switchable per profile the same way.

Public API::

    from trading_platform.strategy.freqtrade_rsi_reversion import (
        RSI_REVERSION_FREQTRADE_STRATEGY_NAME,   # "RsiReversionStrategy" (the Freqtrade class name)
        RsiReversionFreqtradeStrategy,           # the generated IStrategy subclass
    )
"""

from __future__ import annotations

from typing import Any

from trading_platform.strategy.freqtrade_adapter import make_freqtrade_strategy

__all__ = [
    "RSI_REVERSION_FREQTRADE_STRATEGY_NAME",
    "RsiReversionFreqtradeStrategy",
]

#: Class name Freqtrade resolves inside ``user_data/strategies``.  It is
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name` applied
#: to the house name (``"rsi_reversion"`` -> ``"RsiReversionStrategy"``), i.e.
#: exactly ``RsiReversionFreqtradeStrategy.__name__`` and the name carried by the
#: literal ``class`` statement of ``user_data/strategies/RsiReversionStrategy.py``.
RSI_REVERSION_FREQTRADE_STRATEGY_NAME: str = "RsiReversionStrategy"

#: The house ``rsi_reversion`` strategy as a Freqtrade ``IStrategy`` subclass.
#: The class name comes from
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name`
#: (``"rsi_reversion"`` -> ``"RsiReversionStrategy"``), and its timeframe is the
#: literal ``"1d"``: the daily grid the published Connors rule is defined on
#: (module docstring above).  ``startup_candle_count`` is derived by the adapter
#: from the parameter model — ``200``, the largest integer default
#: (``trend_ema_period``).
RsiReversionFreqtradeStrategy: type[Any] = make_freqtrade_strategy("rsi_reversion", timeframe="1d")
