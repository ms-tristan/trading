"""Statistical validation layer.

Public API::

    from trading_backtest.validation import (
        Window, split_is_oos, make_windows,
        walk_forward, WalkForwardResult, WalkForwardWindowResult,
        parameter_sweep, RobustnessResult, RobustnessPoint, expand_grid, grid_size,
        monte_carlo, MonteCarloResult,
        validate_benchmark, BenchmarkGateResult, MIN_ALPHA,
        validate_random_entry, RandomEntryGateResult, MIN_P_VALUE,
    )

The layer is purely mechanical and fully injectable:

* every backtest is performed by a caller-provided
  :data:`~trading_backtest.core.models.RunnerFn` (this package never imports the
  engine or the strategy);
* every scoring function accepts an explicit ``metric_fn`` override and the
  metrics layer (``trading_backtest.metrics``) is imported **lazily** inside the
  function bodies, so this package stays importable and testable on its own;
* the benchmark gate accepts an explicit ``comparison_fn`` override and follows
  the same lazy rule: ``validate_benchmark`` answers "did the strategy beat buy &
  hold?" (``strategy_beats_benchmark``, with ``MIN_ALPHA = 0.0`` so a tie is not
  a victory), next to ``is_robust`` and ``is_consistent``;
* the random-entry gate accepts an explicit ``entry_fn`` override and follows it
  too: ``validate_random_entry`` answers "did the strategy beat **luck**?"
  (``strategy_beats_random``, with ``MIN_P_VALUE = 0.05`` so a strategy must beat
  at least 95 % of the random entries -- a strict inequality, and ties count
  against it).

Dependency direction: ``core`` / ``config`` / ``data`` -> ``validation``.
"""

from __future__ import annotations

from trading_backtest.validation.benchmark import (
    MIN_ALPHA,
    BenchmarkGateResult,
    validate_benchmark,
)
from trading_backtest.validation.monte_carlo import (
    METHODS,
    PERCENTILE_LABELS,
    SERIALISED_SIMULATIONS,
    MonteCarloResult,
    monte_carlo,
)
from trading_backtest.validation.random_entry import (
    MIN_P_VALUE,
    RandomEntryFn,
    RandomEntryGateResult,
    validate_random_entry,
)
from trading_backtest.validation.robustness import (
    DEFAULT_MAX_COMBINATIONS,
    POSITIVE_RATIO_THRESHOLD,
    ROBUST_RATIO_THRESHOLD,
    RobustnessPoint,
    RobustnessResult,
    expand_grid,
    grid_size,
    parameter_sweep,
)
from trading_backtest.validation.split import (
    MIN_ROWS_PER_SLICE,
    WINDOW_MODES,
    Window,
    make_windows,
    split_is_oos,
)
from trading_backtest.validation.walk_forward import (
    CONSISTENCY_THRESHOLD,
    WalkForwardResult,
    WalkForwardWindowResult,
    walk_forward,
)

__all__ = [
    "CONSISTENCY_THRESHOLD",
    "DEFAULT_MAX_COMBINATIONS",
    "METHODS",
    "MIN_ALPHA",
    "MIN_P_VALUE",
    "MIN_ROWS_PER_SLICE",
    "PERCENTILE_LABELS",
    "POSITIVE_RATIO_THRESHOLD",
    "ROBUST_RATIO_THRESHOLD",
    "SERIALISED_SIMULATIONS",
    "WINDOW_MODES",
    "BenchmarkGateResult",
    "MonteCarloResult",
    "RandomEntryFn",
    "RandomEntryGateResult",
    "RobustnessPoint",
    "RobustnessResult",
    "WalkForwardResult",
    "WalkForwardWindowResult",
    "Window",
    "expand_grid",
    "grid_size",
    "make_windows",
    "monte_carlo",
    "parameter_sweep",
    "split_is_oos",
    "validate_benchmark",
    "validate_random_entry",
    "walk_forward",
]
