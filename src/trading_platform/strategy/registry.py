"""Strategy registry: names in, configured :class:`Strategy` instances out.

The registry is the only place that knows the available strategies, which keeps
the CLI, the configuration layer and the validation layer free of hard-coded
strategy imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trading_platform.core.errors import StrategyError
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.basic import BasicStrategy

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
