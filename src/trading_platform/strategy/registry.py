"""Strategy registry: names in, configured :class:`Strategy` instances out.

The registry is the only place that knows the available strategies, which keeps
the CLI, the configuration layer and the validation layer free of hard-coded
strategy imports.

Every strategy module decorates its class with :func:`register_strategy`, so
importing the module is what registers the strategy.  The ten house strategies
are imported at the **bottom** of this file, once :func:`register_strategy`
exists: each of those modules imports that decorator *from this module*, so a
top-of-file import would import a partially initialised module and fail with an
``ImportError``.  The bottom placement is therefore load-bearing, and the
resulting ``E402`` is silenced file-wide below; the unused-import finding
(``F401``) goes with it, because the imported names are deliberately imported
for their registration side effect and for nothing else.
"""

# ruff: noqa: E402, F401

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trading_platform.core.errors import StrategyError
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.basic import BasicStrategy
from trading_platform.strategy.momentum import MomentumStrategy

__all__ = [
    "STRATEGIES",
    "get_strategy",
    "register_strategy",
    "strategy_names",
    "strategy_param_space",
]

#: Every strategy known to the package, keyed by :attr:`Strategy.name`.
STRATEGIES: dict[str, type[Strategy]] = {
    "basic": BasicStrategy,
    "momentum": MomentumStrategy,
}


def strategy_names() -> list[str]:
    """Return the sorted names of every registered strategy."""
    return sorted(STRATEGIES)


def get_strategy(name: str, params: Mapping[str, Any] | None = None) -> Strategy:
    """Build the strategy registered under ``name`` with ``params``.

    Parameters
    ----------
    name:
        A key of :data:`STRATEGIES`.
    params:
        Optional parameter overrides, validated by the strategy's ``ParamsModel``
        (``None`` means "use the defaults").

    Raises
    ------
    StrategyError
        If ``name`` is unknown, or if ``params`` is rejected by the strategy
        (the message names the failing field).
    """
    try:
        strategy_class = STRATEGIES[name]
    except (KeyError, TypeError):
        available = ", ".join(strategy_names())
        raise StrategyError(f"unknown strategy: {name!r} (available: {available})") from None
    return strategy_class(params)


def strategy_param_space(name: str) -> dict[str, list[float | int]]:
    """Return a copy of the parameter grid of the strategy called ``name``.

    Raises
    ------
    StrategyError
        If ``name`` is unknown.
    """
    try:
        strategy_class = STRATEGIES[name]
    except (KeyError, TypeError):
        available = ", ".join(strategy_names())
        raise StrategyError(f"unknown strategy: {name!r} (available: {available})") from None
    return {key: list(values) for key, values in strategy_class.PARAM_SPACE.items()}


def register_strategy(cls: type[Strategy]) -> type[Strategy]:
    """Class decorator adding ``cls`` to :data:`STRATEGIES`.

    Raises
    ------
    StrategyError
        If ``cls`` is not a :class:`~trading_platform.strategy.base.Strategy`
        subclass, if it has no ``name``, or if that name is already taken.
    """
    if not (isinstance(cls, type) and issubclass(cls, Strategy)):
        raise StrategyError(f"register_strategy expects a Strategy subclass, got {cls!r}")
    name = getattr(cls, "name", "")
    if not name or not isinstance(name, str):
        raise StrategyError(f"strategy {cls.__name__} must define a non-empty 'name'")
    if name in STRATEGIES and STRATEGIES[name] is not cls:
        raise StrategyError(f"strategy {name!r} is already registered")
    STRATEGIES[name] = cls
    return cls


# ---------------------------------------------------------------------------
# the house strategies
# ---------------------------------------------------------------------------
# These imports must stay at the BOTTOM of the module.  Each strategy module
# starts with ``from trading_platform.strategy.registry import register_strategy``,
# so importing one of them from the top of this file would hand it a partially
# initialised module -- ``register_strategy`` would not exist yet -- and every
# ``import trading_platform.strategy`` would raise ImportError.  Placing them
# after the definition is what makes the registration side effect safe.
#
# They are deliberately absent from the STRATEGIES literal above: the
# ``@register_strategy`` decorator is the single registration path, and a second
# one would raise "already registered".  The imported names are unused on
# purpose -- importing the module is the whole point.  Adding an eleventh house
# strategy is therefore exactly two edits: the new module, and one line here.
from trading_platform.strategy.bollinger import BollingerStrategy
from trading_platform.strategy.donchian import DonchianStrategy
from trading_platform.strategy.dual_thrust import DualThrustStrategy
from trading_platform.strategy.faber import FaberStrategy
from trading_platform.strategy.keltner import KeltnerStrategy
from trading_platform.strategy.macd import MacdStrategy
from trading_platform.strategy.rsi_reversion import RsiReversionStrategy
from trading_platform.strategy.supertrend import SupertrendStrategy
