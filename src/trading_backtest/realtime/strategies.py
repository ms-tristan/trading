"""Strategy resolution of the realtime layer, and the optional Freqtrade bridge.

The realtime engine owns **no** trading rule: it resolves the profile's strategy
through :func:`trading_backtest.strategy.registry.get_strategy` and hands it the
prepared frame, exactly like ``strategy.engine`` does for a backtest (delivery
brief D6).  A profile is therefore the same artefact in the backtest, in the
realtime engine and in Freqtrade.

:func:`freqtrade_strategy_for` is the single exposure path towards the optional
``freqtrade`` extra.  It imports ``strategy.freqtrade_adapter`` **inside its own
body** so that neither this module nor the package import an optional dependency;
when the extra is missing it returns ``None`` instead of raising, because "Freqtrade
is not installed" is a normal configuration of this project, not an error.
"""

from __future__ import annotations

from typing import Any

from trading_backtest.config.models import ProfileConfig
from trading_backtest.strategy import registry
from trading_backtest.strategy.base import Strategy

__all__ = ["freqtrade_strategy_for", "resolve_strategy", "strategy_names"]


def resolve_strategy(profile: ProfileConfig) -> Strategy:
    """Build the strategy of ``profile`` -- the only construction path.

    Parameters
    ----------
    profile:
        Profile whose ``strategy`` name and ``params`` are used.

    Returns
    -------
    Strategy
        A ready instance, already validated by the strategy's parameter model.

    Raises
    ------
    StrategyError
        If the name is unknown or the parameters are rejected; the error is
        propagated untouched so a configuration mistake stays loud.
    """
    return registry.get_strategy(profile.strategy, profile.params)


def strategy_names() -> list[str]:
    """Return the sorted names of every strategy the registry exposes."""
    return registry.strategy_names()


def freqtrade_strategy_for(profile: ProfileConfig) -> type[Any] | None:
    """Return the Freqtrade ``IStrategy`` class of ``profile``, or ``None``.

    ``None`` means "the optional ``freqtrade`` extra is not installed": the very
    same profile can then be handed to Freqtrade unchanged on a machine that has
    it, without the realtime layer ever duplicating the bridge.

    Parameters
    ----------
    profile:
        Profile to expose.  Its ``timeframe`` and ``params`` are forwarded, and
        ``can_short`` follows the strategy's own ``allow_short`` parameter, exactly
        like ``strategy.engine._resolve_allow_short``.

    Raises
    ------
    StrategyError
        A genuine failure of the bridge (unknown strategy, invalid parameters) is
        never swallowed.
    """
    from trading_backtest.strategy.freqtrade_adapter import (
        freqtrade_available,
        make_freqtrade_strategy,
    )

    if not freqtrade_available():
        return None
    strategy = resolve_strategy(profile)
    return make_freqtrade_strategy(
        profile.strategy,
        timeframe=profile.timeframe,
        params=profile.params,
        can_short=bool(getattr(strategy.params, "allow_short", False)),
    )
