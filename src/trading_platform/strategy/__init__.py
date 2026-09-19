"""Strategy layer.

Public API::

    from trading_platform.strategy import (
        BasicStrategy,        # the reference EMA/RSI/ATR strategy
        TimesFMForecastStrategy,  # the forecast-artifact driven strategy ('timesfm')
        Strategy,             # the abstract base class
        StrategyParams,       # the pydantic parameter base model
        FeatureBundle,        # the external features a strategy may consume
        resolve_features,     # configuration -> FeatureBundle (loads the artifact)
        attach_features,      # FeatureBundle -> strategy instance
        get_strategy,         # registry lookup by name
        register_strategy,    # register a new strategy
        run_backtest,         # the deterministic backtest engine
        run_backtest_on_config,
        make_runner,          # RunnerFn bound to an AppConfig (validation seam)
        make_freqtrade_strategy,           # house strategy -> Freqtrade IStrategy
        validate_freqtrade_adapter_class,  # proof that the produced class is valid
        freqtrade_available,               # is the optional extra installed?
    )

Dependency direction: this layer only depends on ``core``, ``config``, ``data``
and ``freqtrade`` (layer 2) — never on ``validation``, ``metrics``,
``reporting`` or ``cli``.

The Freqtrade **adapter** lives *here*, in layer 3
(:mod:`trading_platform.strategy.freqtrade_adapter`): ``strategy -> freqtrade``
is a downward import (layer 3 -> layer 2), whereas moving the adapter into
``trading_platform.freqtrade`` would be an upward import and would falsify that
package's "never imports anything but ``core``" contract.  So
``trading_platform.freqtrade`` stays a **freqtrade-free** layer-2 module.
Freqtrade itself stays an optional extra: the adapter imports it lazily, inside
functions, so this namespace is importable on the ``.[dev]`` extra alone — which
is why it never imports
:mod:`trading_platform.strategy.freqtrade_basic`.  That module (and the literal
``user_data/strategies/BasicStrategy.py`` shim Freqtrade resolves by class name)
builds its class at import time and is therefore **only** importable with the
extra installed.
"""

from __future__ import annotations

from trading_platform.strategy import freqtrade_adapter, indicators
from trading_platform.strategy.base import (
    BOOL_SIGNAL_COLUMNS,
    Strategy,
    StrategyParams,
    ensure_signal_frame,
    require_ohlcv_frame,
)
from trading_platform.strategy.basic import BasicStrategy, BasicStrategyParams
from trading_platform.strategy.engine import (
    ENGINE_VERSION,
    make_runner,
    run_backtest,
    run_backtest_on_config,
)
from trading_platform.strategy.features import (
    FeatureBundle,
    attach_features,
    resolve_features,
)
from trading_platform.strategy.freqtrade_adapter import (
    FREQTRADE_INTERFACE_VERSION,
    FREQTRADE_ORDER_COLUMNS,
    SIGNAL_TO_FREQTRADE_COLUMNS,
    freqtrade_available,
    make_freqtrade_strategy,
    validate_freqtrade_adapter_class,
)
from trading_platform.strategy.indicators import atr, ema, rsi, true_range
from trading_platform.strategy.registry import (
    STRATEGIES,
    get_strategy,
    register_strategy,
    strategy_names,
    strategy_param_space,
)
from trading_platform.strategy.timesfm_forecast import (
    TimesFMForecastParams,
    TimesFMForecastStrategy,
)

__all__ = [
    "BOOL_SIGNAL_COLUMNS",
    "ENGINE_VERSION",
    "FREQTRADE_INTERFACE_VERSION",
    "FREQTRADE_ORDER_COLUMNS",
    "SIGNAL_TO_FREQTRADE_COLUMNS",
    "STRATEGIES",
    "BasicStrategy",
    "BasicStrategyParams",
    "FeatureBundle",
    "Strategy",
    "StrategyParams",
    "TimesFMForecastParams",
    "TimesFMForecastStrategy",
    "atr",
    "attach_features",
    "ema",
    "ensure_signal_frame",
    "freqtrade_adapter",
    "freqtrade_available",
    "get_strategy",
    "indicators",
    "make_freqtrade_strategy",
    "make_runner",
    "register_strategy",
    "require_ohlcv_frame",
    "resolve_features",
    "rsi",
    "run_backtest",
    "run_backtest_on_config",
    "strategy_names",
    "strategy_param_space",
    "true_range",
    "validate_freqtrade_adapter_class",
]
