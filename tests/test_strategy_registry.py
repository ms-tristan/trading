"""Tests of the strategy registry.

``register_strategy`` mutates the module-level registry, so every test that
registers something monkeypatches a *copy* of it: the global state of the
test-session is never polluted.

The catalogue itself is pinned here: the ten house strategies, their sorted
name list, and the fact that every one of them builds with its defaults and
validates **every** combination of its own ``PARAM_SPACE``.  That last property
is what keeps the ``robustness`` command runnable on the whole catalogue: the
sweep hands each raw parameter combination to the registry, so a grid that
contains a cross-field contradiction would make the command fail on a strategy
nobody suspects.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.errors import StrategyError
from trading_platform.strategy import registry
from trading_platform.strategy.base import Strategy, StrategyParams, ensure_signal_frame
from trading_platform.strategy.basic import BasicStrategy, BasicStrategyParams
from trading_platform.strategy.bollinger import BollingerStrategy
from trading_platform.strategy.donchian import DonchianStrategy
from trading_platform.strategy.dual_thrust import DualThrustStrategy
from trading_platform.strategy.faber import FaberStrategy
from trading_platform.strategy.keltner import KeltnerStrategy
from trading_platform.strategy.macd import MacdStrategy
from trading_platform.strategy.momentum import MomentumStrategy, MomentumStrategyParams
from trading_platform.strategy.rsi_reversion import RsiReversionStrategy
from trading_platform.strategy.supertrend import SupertrendStrategy
from trading_platform.validation.robustness import DEFAULT_MAX_COMBINATIONS

#: The real registry, captured before any test can monkeypatch it.
_GLOBAL_REGISTRY = registry.STRATEGIES

#: Repository root and the source root a fresh interpreter must see on ``PYTHONPATH``.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter that sees ``src`` on ``PYTHONPATH``."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_DIR) if not existing else f"{SRC_DIR}{os.pathsep}{existing}"
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=120,
    )


#: The ten house strategies, in the sorted order :func:`strategy_names` answers.
#: This is the frozen catalogue of the delivery: growing it is a deliberate edit
#: of this table, never an accident.
HOUSE_STRATEGY_NAMES: list[str] = [
    "basic",
    "bollinger",
    "donchian",
    "dual_thrust",
    "faber",
    "keltner",
    "macd",
    "momentum",
    "rsi_reversion",
    "supertrend",
]

#: The class every registered name must resolve to.  ``type[Strategy]`` is not
#: precise enough to be checked with ``isinstance``, so the mapping lives here.
HOUSE_STRATEGY_CLASSES: dict[str, type[Strategy]] = {
    "basic": BasicStrategy,
    "bollinger": BollingerStrategy,
    "donchian": DonchianStrategy,
    "dual_thrust": DualThrustStrategy,
    "faber": FaberStrategy,
    "keltner": KeltnerStrategy,
    "macd": MacdStrategy,
    "momentum": MomentumStrategy,
    "rsi_reversion": RsiReversionStrategy,
    "supertrend": SupertrendStrategy,
}


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

    assert str(error.value) == (
        "unknown strategy: 'nope' (available: basic, bollinger, donchian, dual_thrust, "
        "faber, keltner, macd, momentum, rsi_reversion, supertrend)"
    )


def test_strategy_names_is_sorted_and_contains_the_ten_house_strategies() -> None:
    assert registry.strategy_names() == HOUSE_STRATEGY_NAMES
    assert registry.strategy_names() == sorted(HOUSE_STRATEGY_NAMES)


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
# the catalogue: ten house strategies, buildable and sweepable
# ---------------------------------------------------------------------------


def test_the_registry_holds_exactly_the_ten_house_strategies() -> None:
    """The catalogue, class by class: no missing name, no duplicate path.

    The eight strategies added after the reference pair register themselves
    through the ``@register_strategy`` decorator (importing the module is the
    registration), so the ``STRATEGIES`` literal only ever holds the two
    historical entries.  A second registration path would raise
    ``StrategyError`` at import time, which is what this guards.
    """
    assert set(registry.STRATEGIES) == set(HOUSE_STRATEGY_NAMES)
    for name, strategy_class in HOUSE_STRATEGY_CLASSES.items():
        assert registry.STRATEGIES[name] is strategy_class, name


def test_every_registered_strategy_builds_with_its_default_parameters() -> None:
    """``get_strategy(name)`` is total over the catalogue, with the documented types.

    Every strategy must expose a :class:`StrategyParams` subclass as its
    ``ParamsModel`` and the instance returned must carry the model's defaults --
    the CLI, the API and the dashboard all rely on "no parameters" being a valid
    request for every name.
    """
    for name in registry.strategy_names():
        strategy_class = HOUSE_STRATEGY_CLASSES[name]
        strategy = registry.get_strategy(name)

        assert type(strategy) is strategy_class, name
        assert strategy.name == name
        assert issubclass(strategy_class.ParamsModel, StrategyParams), name
        assert isinstance(strategy.params, strategy_class.ParamsModel), name
        assert strategy.params.model_dump() == strategy_class.default_params(), name
        assert isinstance(strategy_class.PARAM_SPACE, dict), name


@pytest.mark.parametrize("name", HOUSE_STRATEGY_NAMES)
def test_the_param_space_of_every_house_strategy_has_the_documented_types(name: str) -> None:
    """Keys are parameter names, values are non-empty lists of numbers.

    ``bool`` is excluded on purpose: it is a subclass of ``int`` in Python, and a
    boolean flag that leaked into a numeric grid would be swept as ``0``/``1``
    by the robustness layer.
    """
    strategy_class = HOUSE_STRATEGY_CLASSES[name]
    space = registry.strategy_param_space(name)

    assert space, f"{name} must expose a sweepable grid"
    for key, values in space.items():
        assert key in strategy_class.ParamsModel.model_fields, (name, key)
        assert isinstance(values, list), (name, key)
        assert values, (name, key)
        for value in values:
            assert isinstance(value, (int, float)), (name, key, value)
            assert not isinstance(value, bool), (name, key, value)

    # the grid handed out is a copy: mutating it never reaches the class
    first_key = next(iter(space))
    space[first_key].clear()
    assert registry.strategy_param_space(name)[first_key], name


@pytest.mark.parametrize("name", HOUSE_STRATEGY_NAMES)
def test_every_combination_of_every_param_space_validates(name: str) -> None:
    """The whole cartesian product of the grid is accepted by the params model.

    This is the property that keeps ``trading robustness`` runnable over the
    catalogue: the command expands the grid and hands each combination to the
    registry, so a grid holding a cross-field contradiction (``exit_period >=
    entry_period``, ``slow_period <= fast_period``, ...) would fail the sweep on
    a strategy nobody suspects.  Every combination is rebuilt and re-read back.
    """
    strategy_class = HOUSE_STRATEGY_CLASSES[name]
    space = registry.strategy_param_space(name)
    keys = sorted(space)

    combinations = list(itertools.product(*(space[key] for key in keys)))
    assert combinations, name
    assert len(combinations) <= DEFAULT_MAX_COMBINATIONS, name

    for values in combinations:
        params = dict(zip(keys, values, strict=True))
        strategy = registry.get_strategy(name, params)
        assert isinstance(strategy.params, strategy_class.ParamsModel), params
        dumped = strategy.params.model_dump()
        for key, value in params.items():
            assert dumped[key] == value, (name, key, value)


def test_importing_the_registry_module_alone_registers_the_whole_catalogue() -> None:
    """The bottom-of-file wiring is load-bearing, and this proves it in a fresh process.

    ``registry.py`` imports the eight new modules *after* ``register_strategy`` is
    defined; importing that module directly -- without going through the
    ``trading_platform.strategy`` package first -- must therefore already answer
    the ten names.  A top-of-file import would raise ``ImportError`` on a
    partially initialised module, so this test fails loudly if the block is ever
    moved up.
    """
    code = (
        "from trading_platform.strategy.registry import STRATEGIES, strategy_names\n"
        "names = strategy_names()\n"
        "assert names == sorted(names), names\n"
        "print(','.join(names))\n"
        "print(len(STRATEGIES))\n"
    )
    completed = run_python(code)

    assert completed.returncode == 0, completed.stderr
    printed = completed.stdout.split()
    assert printed[0] == ",".join(HOUSE_STRATEGY_NAMES)
    assert printed[1] == str(len(HOUSE_STRATEGY_NAMES))


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_register_strategy_adds_a_new_strategy(
    isolated_registry: dict[str, type[Strategy]],
) -> None:
    returned = registry.register_strategy(RegistrableStrategy)

    assert returned is RegistrableStrategy
    assert isolated_registry["registrable"] is RegistrableStrategy
    assert registry.strategy_names() == [
        "basic",
        "bollinger",
        "donchian",
        "dual_thrust",
        "faber",
        "keltner",
        "macd",
        "momentum",
        "registrable",
        "rsi_reversion",
        "supertrend",
    ]
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
    assert set(_GLOBAL_REGISTRY) == set(HOUSE_STRATEGY_NAMES)
