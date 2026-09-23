"""Tests of the strategy registry.

``register_strategy`` mutates the module-level registry, so every test that
registers something monkeypatches a *copy* of it: the global state of the
test-session is never polluted.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.errors import StrategyError
from trading_platform.strategy import registry
from trading_platform.strategy.base import Strategy, StrategyParams, ensure_signal_frame
from trading_platform.strategy.basic import BasicStrategy, BasicStrategyParams
from trading_platform.strategy.momentum import MomentumStrategy, MomentumStrategyParams

#: The real registry, captured before any test can monkeypatch it.
_GLOBAL_REGISTRY = registry.STRATEGIES


class _RegistrableParams(StrategyParams):
    span: int = 2


class RegistrableStrategy(Strategy):
    """A minimal strategy used to exercise the registration path."""

    name: ClassVar[str] = "registrable"
    ParamsModel: ClassVar[type[StrategyParams]] = _RegistrableParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {"span": [2, 4]}

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        return data.copy()

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        zeros = np.zeros(len(data), dtype=bool)
        return ensure_signal_frame(
            pd.DataFrame(
                {
                    "entry_long": zeros,
                    "exit_long": zeros,
                    "entry_short": zeros,
                    "exit_short": zeros,
                    "stop_loss": np.full(len(data), np.nan),
                },
                index=data.index,
            ),
            data.index,
        )


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[Strategy]]:
    """Give the test its own copy of the registry."""
    copy: dict[str, type[Strategy]] = dict(registry.STRATEGIES)
    monkeypatch.setattr(registry, "STRATEGIES", copy)
    return copy


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------


def test_get_strategy_builds_the_basic_strategy_with_its_defaults() -> None:
    strategy = registry.get_strategy("basic")

    assert isinstance(strategy, BasicStrategy)
    assert strategy.name == "basic"
    assert isinstance(strategy.params, BasicStrategyParams)
    assert strategy.params.model_dump() == BasicStrategy.default_params()


def test_get_strategy_passes_the_parameters_through() -> None:
    strategy = registry.get_strategy("basic", {"ema_fast": 5, "ema_slow": 34})

    assert isinstance(strategy.params, BasicStrategyParams)
    assert strategy.params.ema_fast == 5
    assert strategy.params.ema_slow == 34


def test_get_strategy_propagates_parameter_errors() -> None:
    with pytest.raises(StrategyError, match="ema_slow"):
        registry.get_strategy("basic", {"ema_fast": 50})


def test_get_strategy_builds_the_momentum_strategy_with_its_defaults() -> None:
    strategy = registry.get_strategy("momentum")

    assert isinstance(strategy, MomentumStrategy)
    assert strategy.name == "momentum"
    assert isinstance(strategy.params, MomentumStrategyParams)
    assert strategy.params.model_dump() == MomentumStrategy.default_params()
    # The defaults are part of the published contract: they are the parameters
    # the research loop froze before the holdout was read.
    assert strategy.params.model_dump() == {
        "fast_days": 7,
        "mid_days": 14,
        "slow_days": 28,
        "enter_score": 0.6,
        "exit_score": 0.0,
        "atr_period": 14,
        "atr_stop_multiplier": 4.0,
        "allow_short": False,
    }


def test_momentum_param_space_is_the_documented_grid() -> None:
    space = registry.strategy_param_space("momentum")

    assert set(space) == {"fast_days", "mid_days", "slow_days", "atr_stop_multiplier"}
    assert space == {
        "fast_days": [5, 7, 10],
        "mid_days": [14, 20],
        "slow_days": [28, 40, 56],
        "atr_stop_multiplier": [0.0, 4.0],
    }
    # The robustness sweep caps a grid at 512 combinations; staying comfortably
    # below the cap is what keeps the sweep affordable.
    combinations = 1
    for values in space.values():
        combinations *= len(values)
    assert combinations == 36


def test_get_strategy_rejects_an_unknown_name() -> None:
    with pytest.raises(StrategyError) as error:
        registry.get_strategy("nope")

    assert str(error.value) == "unknown strategy: 'nope' (available: basic, momentum)"


def test_strategy_names_is_sorted_and_contains_basic() -> None:
    assert registry.strategy_names() == ["basic", "momentum"]


def test_strategy_param_space_returns_the_grid_of_the_strategy() -> None:
    space = registry.strategy_param_space("basic")

    assert set(space) == {"ema_fast", "ema_slow", "rsi_max", "atr_stop_multiplier"}
    assert space["ema_fast"] == [5, 9, 13]


def test_strategy_param_space_returns_a_copy() -> None:
    space = registry.strategy_param_space("basic")
    space["ema_fast"].append(999)

    assert registry.strategy_param_space("basic")["ema_fast"] == [5, 9, 13]


def test_strategy_param_space_rejects_an_unknown_name() -> None:
    with pytest.raises(StrategyError, match="unknown strategy"):
        registry.strategy_param_space("nope")


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_register_strategy_adds_a_new_strategy(
    isolated_registry: dict[str, type[Strategy]],
) -> None:
    returned = registry.register_strategy(RegistrableStrategy)

    assert returned is RegistrableStrategy
    assert isolated_registry["registrable"] is RegistrableStrategy
    assert registry.strategy_names() == ["basic", "momentum", "registrable"]
    assert isinstance(registry.get_strategy("registrable"), RegistrableStrategy)


def test_register_strategy_rejects_a_duplicate_name(
    isolated_registry: dict[str, type[Strategy]],
) -> None:
    class Duplicate(Strategy):
        name = "basic"
        ParamsModel = BasicStrategyParams
        PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {}

        def prepare(self, data: pd.DataFrame) -> pd.DataFrame:  # pragma: no cover - unused
            return data

        def signals(self, data: pd.DataFrame) -> pd.DataFrame:  # pragma: no cover - unused
            return RegistrableStrategy().signals(data)

    with pytest.raises(StrategyError, match="already registered"):
        registry.register_strategy(Duplicate)

    assert isolated_registry["basic"] is BasicStrategy


def test_register_strategy_rejects_a_non_strategy() -> None:
    with pytest.raises(StrategyError, match="Strategy subclass"):
        registry.register_strategy(object)  # type: ignore[arg-type]

    def factory() -> None:  # pragma: no cover - never called
        return None

    with pytest.raises(StrategyError, match="Strategy subclass"):
        registry.register_strategy(factory)  # type: ignore[arg-type]


def test_register_strategy_requires_a_name(isolated_registry: dict[str, type[Strategy]]) -> None:
    class Nameless(Strategy):
        name = ""
        ParamsModel = _RegistrableParams

        def prepare(self, data: pd.DataFrame) -> pd.DataFrame:  # pragma: no cover - unused
            return data

        def signals(self, data: pd.DataFrame) -> pd.DataFrame:  # pragma: no cover - unused
            return RegistrableStrategy().signals(data)

    with pytest.raises(StrategyError, match="non-empty 'name'"):
        registry.register_strategy(Nameless)


def test_registration_does_not_leak_into_the_global_registry(
    isolated_registry: dict[str, type[Strategy]],
) -> None:
    registry.register_strategy(RegistrableStrategy)

    assert "registrable" in isolated_registry
    assert registry.STRATEGIES is isolated_registry
    assert "registrable" not in _GLOBAL_REGISTRY
    assert set(_GLOBAL_REGISTRY) == {"basic", "momentum"}
