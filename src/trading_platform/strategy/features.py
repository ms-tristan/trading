"""External feature inputs of a strategy, and the seam that injects them.

The house ``Strategy.prepare`` / ``signals`` contract is **pure, deterministic
and I/O-free**, and the whole test suite must stay green with the ``.[dev]``
extra alone — no ``torch``, no ``timesfm``, no network.  A forecast-driven
strategy therefore never loads anything by itself: prediction is pre-computed
offline into a versioned parquet artifact (``trading forecast-build``) and the
artifact is handed to the strategy as a deterministic external input.

This module is that hand-over point — the single place where a configuration (or
an explicit parameter override) becomes a :class:`FeatureBundle`:

``resolve_features(cfg, params)``
    Resolve the configured artifact path and load it.  Called once per run by
    :func:`trading_platform.strategy.engine.run_backtest_on_config`.

``attach_features(strategy, bundle)``
    Hand the bundle to a strategy through the
    :meth:`~trading_platform.strategy.base.Strategy.set_feature_bundle` hook.

``FeatureBundle``
    The frozen value object carrying the external inputs.  It is intentionally a
    bundle (not a bare store) so that a later external input is additive instead
    of a breaking signature change.

Dependency direction: ``strategy`` → ``forecast`` (a downward import: the
forecast layer sits next to ``core`` and knows nothing about strategies).
:class:`~trading_platform.forecast.artifact.ForecastStore` is imported *inside*
:func:`resolve_features` so that importing :mod:`trading_platform.strategy` stays
cheap and never pulls the artifact reader into a process that does not need it.

A missing artifact path is **not** an error: it means "this run has no external
features", which every consumer must handle by producing no signal at all.  A
configured but unusable path *is* an error and is reported unchanged as
:class:`~trading_platform.core.errors.ForecastArtifactError` (a
``TradingBacktestError``, so the CLI error surface already renders it).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from trading_platform.core.errors import StrategyError
from trading_platform.strategy.base import Strategy

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from trading_platform.config import AppConfig
    from trading_platform.forecast.artifact import ForecastStore

__all__ = [
    "FeatureBundle",
    "attach_features",
    "resolve_features",
]


@dataclass(frozen=True)
class FeatureBundle:
    """The external, deterministic inputs a strategy may consume.

    Attributes
    ----------
    forecast:
        Reader over one offline forecast artifact, or ``None`` when the run has
        no forecast at all (the default).  A strategy that finds ``None`` must
        produce no signal: there is nothing to guess from.

    Examples
    --------
    >>> FeatureBundle.empty().has_forecast
    False
    """

    forecast: ForecastStore | None = None

    @property
    def has_forecast(self) -> bool:
        """``True`` when this bundle carries a forecast artifact."""
        return self.forecast is not None

    @classmethod
    def empty(cls) -> FeatureBundle:
        """Return the bundle of a run that has no external feature."""
        return cls()


def resolve_features(
    cfg: AppConfig,
    params: Mapping[str, Any] | None = None,
) -> FeatureBundle:
    """Resolve and load the external features declared by ``cfg`` / ``params``.

    The artifact path is read from ``params['artifact']`` first (the strategy
    parameter, which is where a CLI override lands) and from
    ``cfg.forecast.artifact`` second.  A falsy value — ``None`` or the empty
    string of the default parameter — means "no external feature" and yields
    :meth:`FeatureBundle.empty`.

    Parameters
    ----------
    cfg:
        Application configuration holding the ``forecast`` section.
    params:
        Optional merged strategy parameters; ``artifact`` there wins over the
        configuration, exactly like every other parameter override.

    Returns
    -------
    FeatureBundle
        The loaded bundle, or an empty one when no path is configured.

    Raises
    ------
    ForecastArtifactError
        If a path *is* configured but the artifact or its sidecar metadata is
        missing, corrupt or incompatible.  The error is propagated unchanged:
        it is a :class:`~trading_platform.core.errors.TradingBacktestError`, so
        wrapping or swallowing it would only hide a real configuration mistake.

    Examples
    --------
    >>> from trading_platform.config import AppConfig
    >>> resolve_features(AppConfig()).has_forecast
    False
    """
    artifact: Any = (params or {}).get("artifact")
    if not artifact:
        forecast_config = getattr(cfg, "forecast", None)
        artifact = getattr(forecast_config, "artifact", None)
    if not artifact:
        return FeatureBundle.empty()

    # Imported here on purpose: the artifact reader (pandas + pyarrow parquet)
    # is only needed by a run that actually consumes a forecast.
    from trading_platform.forecast.artifact import ForecastStore

    return FeatureBundle(forecast=ForecastStore.load(artifact))


def attach_features(strategy: Strategy, bundle: FeatureBundle) -> None:
    """Hand ``bundle`` to ``strategy`` through its feature hook.

    Parameters
    ----------
    strategy:
        Any :class:`~trading_platform.strategy.base.Strategy` instance.  The base
        class accepts and ignores the bundle, so a strategy that has no external
        input is unaffected by the injection.
    bundle:
        The resolved external features.

    Raises
    ------
    StrategyError
        If ``strategy`` exposes no ``set_feature_bundle`` hook at all.

    Examples
    --------
    >>> from trading_platform.strategy.basic import BasicStrategy
    >>> attach_features(BasicStrategy(), FeatureBundle.empty())  # accepted, ignored
    """
    hook = getattr(strategy, "set_feature_bundle", None)
    if not callable(hook):
        raise StrategyError(
            f"strategy {getattr(strategy, 'name', type(strategy).__name__)!r} "
            "does not accept a feature bundle"
        )
    hook(bundle)
