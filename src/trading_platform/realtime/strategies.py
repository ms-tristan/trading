"""Strategy resolution of the realtime layer, and the optional Freqtrade bridge.

The realtime engine owns **no** trading rule: it resolves the profile's strategy
through :func:`trading_platform.strategy.registry.get_strategy` and hands it the
prepared frame, exactly like ``strategy.engine`` does for a backtest (delivery
brief D6).  A profile is therefore the same artefact in the backtest, in the
realtime engine and in Freqtrade.

A profile's **external features** are resolved here too: the forecast artifact a
``timesfm`` profile declares is loaded once, at construction, through
:func:`trading_platform.realtime.features.resolve_profile_features` (which is a
thin adapter over the project's single loader,
:func:`trading_platform.strategy.features.resolve_features`) and handed to the
strategy through :func:`trading_platform.strategy.features.attach_features` --
the same seam ``strategy.engine`` uses.  Realtime therefore injects exactly what
the backtest injects, and a profile that declares no forecast is untouched: its
bundle is empty and its behaviour is unchanged.

:func:`freqtrade_strategy_for` is the single exposure path towards the optional
``freqtrade`` extra.  It imports ``strategy.freqtrade_adapter`` **inside its own
body** so that neither this module nor the package import an optional dependency;
when the extra is missing it returns ``None`` instead of raising, because "Freqtrade
is not installed" is a normal configuration of this project, not an error.
"""

from __future__ import annotations

from typing import Any

from trading_platform.config.models import ProfileConfig
from trading_platform.realtime.features import (
    resolve_profile_features,
    strategy_needs_forecast,
)
from trading_platform.strategy import registry
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.features import attach_features

__all__ = ["freqtrade_strategy_for", "resolve_strategy", "strategy_names"]


def _strategy_parameters(profile: ProfileConfig) -> dict[str, Any]:
    """Return the parameters to hand to ``profile``'s strategy class.

    :meth:`~trading_platform.config.models.ProfileConfig.strategy_params` merges
    the profile's ``params`` with an ``artifact`` key whenever the profile
    declares a forecast path.  Only a strategy whose parameter model *has* that
    field may receive it: every other strategy validates its parameters with
    ``extra="forbid"`` and would reject the key with a ``StrategyError``, so a
    ``basic`` profile that merely declares a ``forecast`` path (a legitimate,
    additive configuration) would stop starting.  The key is therefore filtered
    on the strategy's own declared contract -- see
    :func:`trading_platform.realtime.features.strategy_needs_forecast` -- and
    dropped for a strategy that has no forecast input, which keeps such a profile
    byte-identical to the historical behaviour.
    """
    resolved = profile.strategy_params()
    if resolved.get("artifact") and not strategy_needs_forecast(profile):
        resolved = {key: value for key, value in resolved.items() if key != "artifact"}
    return resolved


def resolve_strategy(profile: ProfileConfig) -> Strategy:
    """Build the strategy of ``profile`` -- the only construction path.

    The strategy is instantiated from the profile's ``strategy`` name and merged
    parameters, and its **external features are injected** in the same call, so a
    profile that declares a forecast artifact receives it exactly as it would in a
    backtest.  The artifact is loaded **once** here, at construction, never per
    candle, and the profile's own callers (:meth:`ProfileRunner.start`,
    :meth:`ProfileRunner.run_once` and the orchestrator's ``_build_runner``) all go
    through :meth:`ProfileRunner._prepare`, so ``realtime run``,
    ``realtime run --once`` and the orchestrator all fail at startup -- before the
    first candle -- rather than running inert forever.

    Parameters
    ----------
    profile:
        Profile whose ``strategy`` name, ``params`` and ``forecast`` path are
        used.

    Returns
    -------
    Strategy
        A ready instance, already validated by the strategy's parameter model and
        already holding its feature bundle.

    Raises
    ------
    StrategyError
        If the name is unknown or the parameters are rejected; the error is
        propagated untouched so a configuration mistake stays loud.
    ForecastArtifactError
        If the profile's strategy needs a forecast artifact and the profile
        declares no path, if the declared artifact is missing, corrupt or
        schema-incompatible, or if the startup guard rejects it (wrong symbol,
        wrong timeframe, coverage too stale to reach the decision horizon).  The
        error is never wrapped: it is a ``TradingBacktestError``, so the CLI error
        surface already renders it.
    """
    strategy = registry.get_strategy(profile.strategy, _strategy_parameters(profile))
    attach_features(strategy, resolve_profile_features(profile, required=True))
    return strategy


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
    from trading_platform.strategy.freqtrade_adapter import (
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
