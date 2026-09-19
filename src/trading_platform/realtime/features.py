"""Forecast-artifact injection and its startup guard, for the realtime layer.

The realtime engine used to resolve a profile's strategy and nothing else, so a
profile that legitimately declared ``"strategy": "timesfm"`` instantiated the
strategy **without** its external forecast: every diagnostic column stayed
``NaN``, the profile emitted no signal at all, and it did so silently and
forever.  The strategy was therefore only half-integrated -- it traded in the
backtest and in the validation layers (where ``strategy.engine`` calls
``strategy.features.resolve_features``) and it was inert in realtime.

This module removes that limit.  It is the realtime counterpart of the hand-over
seam, and it deliberately **reuses** it instead of inventing a second mechanism:

``resolve_profile_features(profile, required=...)``
    The one and only construction path of a profile's :class:`FeatureBundle`.  It
    reads ``profile.forecast_artifact``, adapts it into an
    :class:`~trading_platform.config.models.AppConfig` shape and hands it to
    :func:`trading_platform.strategy.features.resolve_features`, which stays the
    single artifact loader of the project.  The bundle is built **once per
    profile**, at runner construction, never per candle.

``check_profile_forecast(profile, store, now=...)``
    The mandatory startup guard.  It refuses a profile whose artifact was built
    for another symbol or another timeframe, and -- the reason this module
    exists -- a profile whose artifact can no longer cover the profile's decision
    horizon.  A profile that starts and then never trades is exactly the failure
    being fixed, so it must fail **loudly at startup** instead.

``ForecastCoverage``
    The frozen answer to *"is this artifact still usable right now?"*: the
    artifact identity, the seasonal period actually in force and the covered
    window.  ``trading forecast-info`` renders it, which is what makes the guard
    operable instead of merely an error message.

Dependency direction: ``realtime`` -> ``strategy`` -> ``forecast`` -> ``core``.
``ForecastStore`` is imported **inside** the function bodies, exactly like
:func:`~trading_platform.strategy.features.resolve_features` does, so importing
the realtime layer never pulls the parquet reader into a process that does not
consume a forecast.  This module never imports :mod:`trading_platform.realtime.runner`
(that would be a cycle) and never imports ``torch``, ``timesfm``, ``freqtrade``
or ``ccxt``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from trading_platform.config.models import ForecastConfig, ProfileConfig
from trading_platform.core.errors import ForecastArtifactError
from trading_platform.forecast.series import resolve_seasonal_period, timeframe_delta
from trading_platform.strategy.features import FeatureBundle, resolve_features

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from trading_platform.config import AppConfig
    from trading_platform.forecast.artifact import ForecastStore

__all__ = [
    "ForecastCoverage",
    "check_profile_forecast",
    "resolve_profile_features",
    "strategy_needs_forecast",
]


@dataclass(frozen=True)
class ForecastCoverage:
    """The answer to *"is this artifact still usable right now?"*.

    One profile, one artifact, one verdict: the instance is returned by
    :func:`check_profile_forecast` **only** when the artifact passed every guard,
    so holding one is the proof that the profile may trade on it.  It is frozen
    and positional, and it is JSON-safe through :func:`dataclasses.asdict` on
    purpose: ``trading forecast-info`` renders exactly these fields.

    Attributes
    ----------
    path:
        Artifact the profile declares.
    symbol:
        Symbol the artifact was built for; equals the profile's symbol.
    timeframe:
        Timeframe the artifact was built on; equals the profile's timeframe.
    seasonal_period:
        Number of phases the de-seasonalisation actually folded on, resolved
        through :func:`trading_platform.forecast.series.resolve_seasonal_period`
        so an artifact built before the timeframe-aware default still reports the
        period in force for its own timeframe.
    horizon:
        Forecast steps stored per origin, read from the artifact metadata.
    stride:
        Candles between two stored origins, read from the artifact metadata.
    first_origin:
        Oldest stored origin, as a timezone-aware UTC ``datetime``.
    last_origin:
        Most recent stored origin, as a timezone-aware UTC ``datetime``.  The
        covered window ends ``stride`` candles after it, for as long as the
        profile's decision horizon allows.
    """

    path: Path
    symbol: str
    timeframe: str
    seasonal_period: int
    horizon: int
    stride: int
    first_origin: datetime
    last_origin: datetime


def _feature_config(profile: ProfileConfig) -> AppConfig:
    """Return the one-field configuration adapter of ``profile``.

    This is a **configuration adapter, not an artifact loader**: it only reshapes
    the single path a profile declares into the ``AppConfig`` that
    :func:`~trading_platform.strategy.features.resolve_features` expects, so the
    realtime layer never duplicates the artifact-loading logic.  The profile
    stays the source of truth for the path: that same path is also present as
    ``params['artifact']`` (see
    :meth:`~trading_platform.config.models.ProfileConfig.strategy_params`), which
    ``resolve_features`` consults **first**, and this adapter therefore only
    covers a caller that passes bare parameters.
    """
    from trading_platform.config import AppConfig

    return AppConfig(forecast=ForecastConfig(artifact=profile.forecast_artifact))


def strategy_needs_forecast(profile: ProfileConfig) -> bool:
    """Return whether ``profile``'s strategy genuinely consumes a forecast.

    The marker is the ``artifact`` field of the strategy's own parameter model:
    a forecast-driven strategy declares it (see
    :class:`~trading_platform.strategy.timesfm_forecast.TimesFMForecastParams`,
    whose ``artifact`` is an optional *path override* that the strategy itself
    never loads), while a strategy with no external input has no such field.  The
    check is therefore driven by the strategy's declared contract rather than by
    a hardcoded name, and it never guesses: a strategy the registry does not know
    is left to :func:`~trading_platform.strategy.registry.get_strategy`, which
    raises the loud ``StrategyError`` it always did.
    """
    from trading_platform.strategy import registry

    strategy_class = registry.STRATEGIES.get(profile.strategy)
    if strategy_class is None:
        # An unknown name is the registry's own loud failure, raised by
        # ``registry.get_strategy`` when the strategy is actually built.
        return False
    return "artifact" in getattr(strategy_class.ParamsModel, "model_fields", {})


def resolve_profile_features(profile: ProfileConfig, *, required: bool) -> FeatureBundle:
    """Resolve, load and validate the external features of ``profile``.

    The **only** construction path of a profile's :class:`FeatureBundle`.  It
    loads the artifact exactly once -- through
    :func:`trading_platform.strategy.features.resolve_features`, the project's
    single loader -- and then runs the startup guard of
    :func:`check_profile_forecast` whenever the profile actually declares a path.

    Parameters
    ----------
    profile:
        Profile whose ``forecast`` path, ``strategy``, ``symbol``, ``timeframe``
        and parameters decide what is loaded and whether it is usable.
    required:
        ``True`` when the caller wants a strategy that genuinely predicts.  A
        profile whose strategy carries an ``artifact`` parameter -- the
        forecast-driven contract of
        :class:`~trading_platform.strategy.timesfm_forecast.TimesFMForecastParams`
        -- always needs an artifact and is refused when it declares none; a
        profile whose strategy has no such parameter (``basic``) is never
        rejected by this seam.

    Returns
    -------
    FeatureBundle
        The loaded bundle, or an empty one when the profile declares no forecast
        and ``required`` is ``False``.

    Raises
    ------
    ForecastArtifactError
        If ``required`` is ``True`` and the profile declares no path; if the
        configured artifact is missing, corrupt or schema-incompatible (raised
        unchanged by ``ForecastStore.load``); or if the guard rejects it (wrong
        symbol, wrong timeframe, stale coverage).  The error is never wrapped and
        never swallowed: it derives from ``ForecastError`` ->
        ``TradingBacktestError``, so the CLI error surface already renders it.

    Examples
    --------
    >>> from trading_platform.config.models import ProfileConfig
    >>> resolve_profile_features(ProfileConfig(id="p", symbol="BTC/USDT"), required=False)
    FeatureBundle(forecast=None)
    """
    if profile.forecast_artifact is None:
        if required and strategy_needs_forecast(profile):
            raise ForecastArtifactError(
                f"profile {profile.id!r} uses strategy {profile.strategy!r}, which needs a "
                "forecast artifact, but declares no 'forecast' path; build one with "
                "'trading forecast-build' and add '\"forecast\": \"<artifact>.parquet\"' "
                "to the profile"
            )
        return FeatureBundle.empty()

    params = profile.strategy_params()
    bundle = resolve_features(_feature_config(profile), params)
    if params.get("artifact"):
        check_profile_forecast(profile, bundle.forecast)
    return bundle


def check_profile_forecast(
    profile: ProfileConfig,
    store: ForecastStore | None,
    *,
    now: pd.Timestamp | None = None,
) -> ForecastCoverage:
    """Validate that ``store`` may feed ``profile``, and describe its coverage.

    Four checks run in this order, and every one of them raises
    :class:`~trading_platform.core.errors.ForecastArtifactError` with an
    actionable message: a missing bundle, a symbol mismatch, a timeframe mismatch
    and -- the mandatory guard -- a coverage that can no longer reach the
    profile's decision horizon.

    The coverage check is the reason this function exists.  A profile whose
    artifact stops before the horizon it decides on would start and then never
    trade, silently; it is refused here instead, at startup, with the exact
    command that rebuilds the artifact.  The bound is derived from the strategy's
    own parameters (``min_lead``, ``forecast_age``, and the artifact's ``horizon``
    and ``stride``), so it tracks the live decision path rather than a fixed
    constant: a decision is only taken where the active path still has room, which
    holds until ``age_bound - 1`` strides after the last origin.

    Parameters
    ----------
    profile:
        Profile the artifact must feed.  Its ``symbol``, ``timeframe`` and
        merged strategy parameters decide the verdict.
    store:
        Loaded artifact, or ``None`` when the feature bundle carried none.
    now:
        Instant the staleness is measured against.  ``None`` means "right now",
        read from the wall clock **by this function** (the artifact guard is a
        startup check, not a per-candle decision, so it has no clock seam to go
        through); a caller that supplies a value makes the check deterministic,
        which is how the tests pin the boundary.

    Returns
    -------
    ForecastCoverage
        The validated coverage of the artifact.

    Raises
    ------
    ForecastArtifactError
        On any of the four checks; the message names the profile, the artifact
        and the ``trading forecast-build`` command that fixes it.

    Examples
    --------
    >>> from trading_platform.config.models import ProfileConfig
    >>> profile = ProfileConfig(id="p", symbol="BTC/USDT")
    >>> check_profile_forecast(profile, None)  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    ForecastArtifactError: profile 'p' declares the forecast artifact None ...
    """
    path = profile.forecast_artifact
    params: dict[str, Any] = profile.strategy_params()

    # 1. a bundle without a forecast behind a declared path is a caller bug; it
    #    still surfaces loudly instead of degrading into "no signal".
    if store is None:
        raise ForecastArtifactError(
            f"profile {profile.id!r} declares the forecast artifact {path} but the feature "
            "bundle carries no forecast"
        )

    metadata = store.metadata

    # 2. the artifact must have been built for the symbol the profile trades.
    if str(metadata.symbol) != str(profile.symbol):
        raise ForecastArtifactError(
            f"forecast artifact {path} was built for symbol {metadata.symbol!r} but profile "
            f"{profile.id!r} trades {profile.symbol!r}; rebuild it with "
            f"'trading forecast-build --symbol {profile.symbol}'"
        )

    # 3. ... and on the timeframe the profile runs.
    if str(metadata.timeframe) != str(profile.timeframe):
        raise ForecastArtifactError(
            f"forecast artifact {path} was built on the {metadata.timeframe} timeframe but "
            f"profile {profile.id!r} runs {profile.timeframe}; rebuild it with "
            f"'trading forecast-build --timeframe {profile.timeframe}'"
        )

    # 4. the mandatory coverage guard: the decision horizon must still be covered.
    stamp = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    stamp = stamp.tz_localize("UTC") if stamp.tz is None else stamp.tz_convert("UTC")
    horizon = int(metadata.horizon)
    stride = int(metadata.stride)
    min_lead = max(0, int(params.get("min_lead", 2)))
    forecast_age = int(params.get("forecast_age", 24))
    cadence = stride if stride >= 1 else 1
    decision_age_bound = max(0, horizon - 1 - min_lead)
    age_bound = min(forecast_age, decision_age_bound)
    usable_span = max(0, age_bound - 1)

    origins = store.origins()
    if len(origins) > 0:
        last_origin = pd.Timestamp(origins[-1])
        usable_until = last_origin + cadence * usable_span * timeframe_delta(metadata.timeframe)
    else:  # pragma: no cover - a n_origins == 0 artifact never loads, but the guard owns it
        last_origin = pd.Timestamp(stamp)
        usable_until = last_origin

    if int(metadata.n_origins) < 1 or not params.get("artifact") or stamp > usable_until:
        raise ForecastArtifactError(
            f"the forecast artifact {path} is stale for profile {profile.id!r}: its last origin "
            f"is {last_origin.isoformat()} and the profile decision horizon only stays covered "
            f"until {usable_until.isoformat()} (now is {stamp.isoformat()}); coverage must reach "
            f"the {profile.timeframe} decision horizon, so rebuild it with "
            f"'trading forecast-build --symbol {profile.symbol} --timeframe {profile.timeframe}' "
            "and point the profile at the new file"
        )

    return ForecastCoverage(
        path=path if path is not None else Path(str(store.path)),
        symbol=str(metadata.symbol),
        timeframe=str(metadata.timeframe),
        seasonal_period=resolve_seasonal_period(
            str(metadata.timeframe), int(metadata.seasonal_period)
        ),
        horizon=horizon,
        stride=stride,
        first_origin=pd.Timestamp(origins[0]).to_pydatetime(),
        last_origin=last_origin.to_pydatetime(),
    )
