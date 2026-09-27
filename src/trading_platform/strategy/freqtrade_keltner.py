"""The ``keltner`` house strategy exposed to Freqtrade, ready to be loaded **by name**.

**This module requires the optional ``freqtrade`` extra.**  Importing it runs
:func:`~trading_platform.strategy.freqtrade_adapter.make_freqtrade_strategy` at
module level, which needs ``freqtrade.strategy.IStrategy``; without the extra it
raises :class:`~trading_platform.core.errors.StrategyError` ("install the optional
extra").  That is deliberate and is the reason why
:mod:`trading_platform.strategy` — the public namespace imported by
``import trading_platform`` — must **never** import this module: the whole suite
runs on the ``.[dev]`` extra only, where Freqtrade is absent.  Import it
explicitly::

    from trading_platform.strategy.freqtrade_keltner import KeltnerFreqtradeStrategy

Freqtrade resolves strategies **by class name** inside ``user_data/strategies``
(``StrategyResolver._search_object``): it walks that directory, keeps the files
whose *text* contains ``class <Name>(`` and keeps the classes whose
``__module__`` equals the file stem.  Neither rule is satisfied by an alias such
as ``KeltnerStrategy = make_freqtrade_strategy("keltner")`` — the strategy would
silently resolve to ``(None, None)``.  The loadable entry point is therefore a
**literal class statement** in its own file, which is exactly what
``user_data/strategies/KeltnerStrategy.py`` is: a two-line subclass of
:data:`KeltnerFreqtradeStrategy` whose only job is to carry the name Freqtrade
looks up (``--strategy KeltnerStrategy``).

The adapter itself duplicates **no** business logic: indicators and entry/exit
rules stay in :mod:`trading_platform.strategy.keltner`, and the translation
between the two contracts is the single responsibility of
:mod:`trading_platform.strategy.freqtrade_adapter`.

Why the class pins the ``4h`` grid
----------------------------------
The class-level ``timeframe`` is the literal ``"4h"``.  The Keltner family has
**no recorded verdict** in this repository — it was never swept, so nothing here
may be presented as validated — and the frozen grid therefore follows the only
grid evidence the platform actually owns: its strict protocol cleared the
``momentum`` trend family on ``4h`` and ``1d``, while ``1h`` degraded the most on
the untouched holdout (``docs/strategies.md``).  ``4h`` also keeps the
100-candle trend EMA a genuine long-horizon filter (about 17 days) instead of an
intraday average.  This is a choice of default, **not** a validation of Keltner.
The adapter still accepts an explicit ``timeframe`` argument, and the realtime
layer passes the *profile* timeframe (and its ``can_short``) through
:func:`~trading_platform.realtime.strategies.freqtrade_strategy_for`: this module
only freezes the safe default of a Freqtrade backtest or bot driven by the
shipped shim.  The class-level ``can_short`` stays ``False`` — the long-only
default — and is switchable per profile the same way.

Public API::

    from trading_platform.strategy.freqtrade_keltner import (
        KELTNER_FREQTRADE_STRATEGY_NAME,   # "KeltnerStrategy" (the Freqtrade class name)
        KeltnerFreqtradeStrategy,          # the generated IStrategy subclass
    )
"""

from __future__ import annotations

from typing import Any

from trading_platform.strategy.freqtrade_adapter import make_freqtrade_strategy

__all__ = [
    "KELTNER_FREQTRADE_STRATEGY_NAME",
    "KeltnerFreqtradeStrategy",
]

#: Class name Freqtrade resolves inside ``user_data/strategies``.  It is
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name` applied
#: to the house name (``"keltner"`` -> ``"KeltnerStrategy"``), i.e. exactly
#: ``KeltnerFreqtradeStrategy.__name__`` and the name carried by the literal
#: ``class`` statement of ``user_data/strategies/KeltnerStrategy.py``.
KELTNER_FREQTRADE_STRATEGY_NAME: str = "KeltnerStrategy"

#: The house ``keltner`` strategy as a Freqtrade ``IStrategy`` subclass.  The
#: class name comes from
#: :func:`~trading_platform.strategy.freqtrade_adapter.default_class_name`
#: (``"keltner"`` -> ``"KeltnerStrategy"``), and its timeframe is the literal
#: ``"4h"``: the coarse grid the repository's only validated trend family
#: survived on (module docstring above).  ``startup_candle_count`` is derived by
#: the adapter from the parameter model — ``100``, the largest integer default
#: (``trend_ema_period``).
KeltnerFreqtradeStrategy: type[Any] = make_freqtrade_strategy("keltner", timeframe="4h")
