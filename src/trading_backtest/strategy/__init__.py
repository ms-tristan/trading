"""Strategy layer.

Public API::

    from trading_backtest.strategy import (
        BasicStrategy,        # the reference EMA/RSI/ATR strategy
        Strategy,             # the abstract base class
        StrategyParams,       # the pydantic parameter base model
        get_strategy,         # registry lookup by name
        register_strategy,    # register a new strategy
        run_backtest,         # the deterministic backtest engine
        run_backtest_on_config,
        make_runner,          # RunnerFn bound to an AppConfig (validation seam)
    )

Dependency direction: this layer only depends on ``core``, ``config`` and
``data`` — never on ``validation``, ``metrics``, ``reporting`` or ``cli``.
"""

from __future__ import annotations

from trading_backtest.strategy import indicators
from trading_backtest.strategy.base import (
    BOOL_SIGNAL_COLUMNS,
    Strategy,
    StrategyParams,
    ensure_signal_frame,
    require_ohlcv_frame,
)
from trading_backtest.strategy.basic import BasicStrategy, BasicStrategyParams
from trading_backtest.strategy.engine import (
    ENGINE_VERSION,
    make_runner,
    run_backtest,
    run_backtest_on_config,
)
from trading_backtest.strategy.indicators import atr, ema, rsi, true_range
from trading_backtest.strategy.registry import (
    STRATEGIES,
    get_strategy,
    register_strategy,
    strategy_names,
    strategy_param_space,
)

__all__ = [
    "BOOL_SIGNAL_COLUMNS",
    "ENGINE_VERSION",
    "STRATEGIES",
    "BasicStrategy",
    "BasicStrategyParams",
    "Strategy",
    "StrategyParams",
    "atr",
    "ema",
    "ensure_signal_frame",
    "get_strategy",
    "indicators",
    "make_runner",
    "register_strategy",
    "require_ohlcv_frame",
    "rsi",
    "run_backtest",
    "run_backtest_on_config",
    "strategy_names",
    "strategy_param_space",
    "true_range",
]
