"""Tests of the house-parameters -> Freqtrade-parameters translation seam.

Everything here is **offline** and **deterministic**: no network, no market data
and no Freqtrade needed.  The module under test imports ``freqtrade`` lazily, so
the only tests touching the real library are guarded by
``pytest.importorskip("freqtrade")`` — the CI installs ``.[dev]`` only, where
Freqtrade is absent, and must stay green (``docs/testing-policy.md`` §1.2).

The module is imported through its **own path**
(``trading_backtest.strategy.freqtrade_parameters``) rather than through the
``trading_backtest.strategy`` public namespace, which is owned by another work
package.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

import trading_backtest.strategy.freqtrade_parameters as freqtrade_parameters
from trading_backtest.core.errors import StrategyError
from trading_backtest.strategy.base import Strategy, StrategyParams
from trading_backtest.strategy.basic import BasicStrategy, BasicStrategyParams
from trading_backtest.strategy.freqtrade_parameters import (
    FREQTRADE_DECIMALS,
    FREQTRADE_PARAM_SPACE,
    FREQTRADE_UNBOUNDED_HIGH_FACTOR,
    FreqtradeParamSpec,
    freqtrade_param_specs,
    freqtrade_parameter_attributes,
    startup_candle_count_for,
)
from trading_backtest.strategy.registry import STRATEGIES

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The house grid of the reference strategy (``Strategy.param_space()`` is an
#: instance method, so the copy is taken from a default-configured instance).
BASIC_GRID = BasicStrategy().param_space()


# ---------------------------------------------------------------------------
# local throwaway models -- they exercise the mapping rules in isolation
# ---------------------------------------------------------------------------


class EmptyParams(StrategyParams):
    """A parameter model with no field at all."""


class UnsupportedParams(StrategyParams):
    """Fields whose annotation has no Freqtrade scalar counterpart."""

    label: str = "reference"
    modes: tuple[str, ...] = ("fast", "slow")
    ema_fast: int = Field(default=9, ge=1)


class GuardParams(StrategyParams):
    """Defaults falling *outside* their own constraints (pydantic skips defaults).

    ``forced_high`` is below its own ``ge``, ``forced_low`` above its own ``le``,
    ``pinned`` has a zero-width integer range and ``tiny`` a zero-width float
    range at a magnitude where ``low + 10**-decimals == low``.
    """

    forced_high: int = Field(default=5, ge=20)
    forced_low: int = Field(default=99, le=10)
    pinned: int = Field(default=7, ge=7, le=7)
    tiny: float = Field(default=1e300, ge=1e300, le=1e300)


class NegativeParams(StrategyParams):
    """A float default below the implicit ``low = 0.0`` bound."""

    floor: float = Field(default=-5.0)


class ExclusiveParams(StrategyParams):
    """Exclusive bounds only (``gt`` / ``lt``), for both numeric kinds."""

    ratio: float = Field(default=0.5, gt=0.0, lt=1.0)
    size: int = Field(default=10, gt=0, lt=20)


class OnlyBoolParams(StrategyParams):
    """A model whose only field is a boolean (never counted as a look-back)."""

    allow_short: bool = False


#: The exact translation of :class:`BasicStrategyParams` (no grid).
EXPECTED_BASIC_SPECS = (
    FreqtradeParamSpec(name="ema_fast", kind="int", default=9, low=1, high=18),
    FreqtradeParamSpec(name="ema_slow", kind="int", default=21, low=2, high=42),
    FreqtradeParamSpec(name="rsi_period", kind="int", default=14, low=2, high=28),
    FreqtradeParamSpec(
        name="rsi_min", kind="float", default=30.0, low=0.0, high=99.999, decimals=3
    ),
    FreqtradeParamSpec(
        name="rsi_max", kind="float", default=70.0, low=0.001, high=100.0, decimals=3
    ),
    FreqtradeParamSpec(name="atr_period", kind="int", default=14, low=2, high=28),
    FreqtradeParamSpec(
        name="atr_stop_multiplier", kind="float", default=2.0, low=0.001, high=4.0, decimals=3
    ),
    FreqtradeParamSpec(name="allow_short", kind="bool", default=False),
)


def _spec(specs: tuple[FreqtradeParamSpec, ...], name: str) -> FreqtradeParamSpec:
    """Return the spec called ``name`` (fails loudly when it is missing)."""
    for spec in specs:
        if spec.name == name:
            return spec
    raise AssertionError(f"no spec named {name!r} in {[item.name for item in specs]}")


# ---------------------------------------------------------------------------
# module level contract
# ---------------------------------------------------------------------------


def test_public_constants_are_the_documented_ones() -> None:
    assert FREQTRADE_PARAM_SPACE == "buy"
    assert FREQTRADE_DECIMALS == 3
    assert FREQTRADE_UNBOUNDED_HIGH_FACTOR == 2.0
    assert freqtrade_parameters.__all__ == sorted(freqtrade_parameters.__all__)


def test_spec_dataclass_is_frozen_and_defaults_to_the_house_space() -> None:
    spec = FreqtradeParamSpec(name="ema_fast", kind="int", default=9, low=1, high=18)
    assert spec.space == FREQTRADE_PARAM_SPACE
    assert spec.decimals is None
    assert spec.categories is None
    with pytest.raises(FrozenInstanceError):
        spec.default = 10  # type: ignore[misc]


def test_module_has_no_top_level_freqtrade_import() -> None:
    """``freqtrade`` must only be imported lazily, inside the attribute builder."""
    source = Path(str(freqtrade_parameters.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_level.append(node.module or "")
    assert all(module.split(".")[0] != "freqtrade" for module in top_level), top_level

    imported_inside = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("freqtrade")
    ]
    assert imported_inside, "the real Freqtrade classes must be imported somewhere"


def test_the_strategy_layer_imports_without_freqtrade_in_a_real_subprocess() -> None:
    """Prove -- by really running python -- that the optional extra is optional."""
    script = (
        "import sys\n"
        "sys.modules['freqtrade'] = None\n"
        "from trading_backtest.core.errors import StrategyError\n"
        "from trading_backtest.strategy.basic import BasicStrategyParams\n"
        "from trading_backtest.strategy.freqtrade_parameters import (\n"
        "    freqtrade_param_specs,\n"
        "    freqtrade_parameter_attributes,\n"
        ")\n"
        "specs = freqtrade_param_specs(BasicStrategyParams)\n"
        "assert len(specs) == 8, specs\n"
        "try:\n"
        "    freqtrade_parameter_attributes(specs)\n"
        "except StrategyError as error:\n"
        "    assert 'freqtrade is not installed' in str(error), error\n"
        "else:\n"
        "    raise AssertionError('the attribute builder must fail without freqtrade')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


# ---------------------------------------------------------------------------
# freqtrade_param_specs -- nominal paths
# ---------------------------------------------------------------------------


def test_basic_params_without_grid_are_numeric() -> None:
    assert freqtrade_param_specs(BasicStrategyParams) == EXPECTED_BASIC_SPECS


def test_specs_keep_the_declaration_order_of_the_model() -> None:
    specs = freqtrade_param_specs(BasicStrategyParams)
    assert [spec.name for spec in specs] == list(BasicStrategyParams.model_fields)


def test_strategy_class_and_model_class_give_the_same_specs() -> None:
    """The grid is never inferred: a strategy class alone maps like its model."""
    assert freqtrade_param_specs(BasicStrategy) == EXPECTED_BASIC_SPECS


def test_strategy_instance_defaults_follow_the_validated_values() -> None:
    strategy = BasicStrategy({"ema_fast": 5, "ema_slow": 13, "allow_short": True})
    specs = freqtrade_param_specs(strategy)
    assert _spec(specs, "ema_fast").default == 5
    assert _spec(specs, "ema_fast").high == 10
    assert _spec(specs, "ema_slow").default == 13
    assert _spec(specs, "allow_short").default is True
    assert _spec(specs, "rsi_min").default == 30.0


def test_bool_fields_are_not_int_fields() -> None:
    spec = _spec(freqtrade_param_specs(BasicStrategyParams), "allow_short")
    assert spec.kind == "bool"
    assert spec.default is False
    assert spec.low is None
    assert spec.high is None
    assert spec.decimals is None
    assert spec.categories is None


def test_exclusive_bounds_are_approximated_at_three_decimals() -> None:
    specs = freqtrade_param_specs(ExclusiveParams)
    ratio = _spec(specs, "ratio")
    assert ratio.kind == "float"
    assert ratio.low == 10**-FREQTRADE_DECIMALS
    assert ratio.high == 1.0 - 10**-FREQTRADE_DECIMALS
    size = _spec(specs, "size")
    assert size.kind == "int"
    assert (size.low, size.high) == (1, 19)


def test_unbounded_float_uses_the_documented_factor() -> None:
    class ScaleParams(StrategyParams):
        scale: float = Field(default=3.25)

    spec = _spec(freqtrade_param_specs(ScaleParams), "scale")
    assert (spec.low, spec.high) == (0.0, round(3.25 * FREQTRADE_UNBOUNDED_HIGH_FACTOR, 3))


def test_space_is_always_the_single_house_space() -> None:
    assert {spec.space for spec in freqtrade_param_specs(BasicStrategyParams)} == {
        FREQTRADE_PARAM_SPACE
    }


# ---------------------------------------------------------------------------
# freqtrade_param_specs -- grid (categorical) branch
# ---------------------------------------------------------------------------


def test_grid_turns_the_declared_parameters_into_categoricals() -> None:
    specs = freqtrade_param_specs(BasicStrategyParams, grid=BASIC_GRID)
    expected = {
        "ema_fast": (5, 9, 13),
        "ema_slow": (21, 34, 55),
        "rsi_max": (65.0, 70.0, 75.0),
        "atr_stop_multiplier": (1.5, 2.0, 3.0),
    }
    for name, categories in expected.items():
        spec = _spec(specs, name)
        assert spec.kind == "categorical", name
        assert spec.categories == categories
        assert spec.default in categories
        assert spec.low is None
        assert spec.high is None
        assert spec.decimals is None
    untouched = {
        "rsi_period": "int",
        "rsi_min": "float",
        "atr_period": "int",
        "allow_short": "bool",
    }
    for name, kind in untouched.items():
        assert _spec(specs, name).kind == kind, name


def test_grid_with_a_single_candidate_is_ignored() -> None:
    """Freqtrade raises below two categories, so a one-value grid must not win."""
    specs = freqtrade_param_specs(BasicStrategyParams, grid={"ema_fast": [9]})
    assert _spec(specs, "ema_fast").kind == "int"


def test_grid_ignores_a_field_whose_default_is_absent() -> None:
    specs = freqtrade_param_specs(BasicStrategyParams, grid={"ema_fast": [5, 13]})
    spec = _spec(specs, "ema_fast")
    assert spec.kind == "int"
    assert (spec.low, spec.high) == (1, 18)


def test_grid_never_turns_a_bool_field_into_a_categorical() -> None:
    specs = freqtrade_param_specs(OnlyBoolParams, grid={"allow_short": [0, 1]})
    assert _spec(specs, "allow_short").kind == "bool"


def test_grid_is_never_inferred_from_the_strategy_class() -> None:
    assert freqtrade_param_specs(BasicStrategy) == freqtrade_param_specs(BasicStrategyParams)
    assert BasicStrategy.PARAM_SPACE, "the reference strategy does declare a grid"


# ---------------------------------------------------------------------------
# freqtrade_param_specs -- guard, unsupported annotations, overrides, errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", [GuardParams, NegativeParams, ExclusiveParams])
def test_every_spec_satisfies_the_guard(target: type[StrategyParams]) -> None:
    for spec in freqtrade_param_specs(target):
        if spec.kind in {"int", "float"}:
            assert spec.low < spec.high
            assert spec.low <= spec.default <= spec.high


def test_guard_repairs_a_default_below_its_lower_bound() -> None:
    spec = _spec(freqtrade_param_specs(GuardParams), "forced_high")
    assert (spec.low, spec.high) == (5, 21)
    assert spec.low <= spec.default <= spec.high


def test_guard_repairs_a_default_above_its_upper_bound() -> None:
    spec = _spec(freqtrade_param_specs(GuardParams), "forced_low")
    assert (spec.low, spec.high) == (0, 99)
    assert spec.low <= spec.default <= spec.high


def test_guard_never_emits_a_zero_width_integer_range() -> None:
    spec = _spec(freqtrade_param_specs(GuardParams), "pinned")
    assert (spec.low, spec.high) == (7, 8)


def test_guard_widens_a_float_range_that_cannot_be_represented() -> None:
    spec = _spec(freqtrade_param_specs(GuardParams), "tiny")
    assert spec.low == 1e300
    assert spec.high > spec.low


def test_guard_handles_a_negative_float_default() -> None:
    spec = _spec(freqtrade_param_specs(NegativeParams), "floor")
    assert (spec.low, spec.high) == (-5.0, -4.999)


@pytest.mark.parametrize(
    "manual",
    [
        FreqtradeParamSpec(name="custom", kind="int", default=9, low=30, high=40),
        FreqtradeParamSpec(name="custom", kind="int", default=9),
        FreqtradeParamSpec(name="custom", kind="float", default=2.5, low=8.0, high=8.0, decimals=3),
        FreqtradeParamSpec(name="custom", kind="float", default=2.5),
    ],
)
def test_a_hand_built_numeric_spec_is_guarded_before_being_emitted(
    manual: FreqtradeParamSpec,
) -> None:
    specs = freqtrade_param_specs(BasicStrategyParams, overrides={"custom": manual})
    spec = _spec(specs, "custom")
    assert spec.name == "custom"
    assert spec.low <= spec.default <= spec.high
    assert spec.low < spec.high


def test_unsupported_annotations_are_skipped() -> None:
    specs = freqtrade_param_specs(UnsupportedParams)
    assert [spec.name for spec in specs] == ["ema_fast"]
    assert _spec(specs, "ema_fast").kind == "int"


def test_unsupported_annotations_are_skipped_even_when_the_grid_has_candidates() -> None:
    """Limit (iv) is a hard limit: an unsupported annotation is never mapped.

    The grid below is deliberately a 2-value grid whose default is present —
    without the annotation check first, ``label`` would become categorical.
    """
    grid = {"label": ["reference", "other"], "modes": ("fast", "slow")}  # type: ignore[dict-item]
    specs = freqtrade_param_specs(UnsupportedParams, grid=grid)  # type: ignore[arg-type]
    assert [spec.name for spec in specs] == ["ema_fast"]


def test_overrides_replace_by_name_and_keep_the_position() -> None:
    manual = FreqtradeParamSpec(name="ema_fast", kind="categorical", default=9, categories=(9, 21))
    specs = freqtrade_param_specs(BasicStrategyParams, overrides={"ema_fast": manual})
    assert specs[0] == manual
    assert [spec.name for spec in specs] == [spec.name for spec in EXPECTED_BASIC_SPECS]


def test_overrides_can_add_a_brand_new_spec() -> None:
    added = FreqtradeParamSpec(name="custom_flag", kind="bool", default=True)
    specs = freqtrade_param_specs(BasicStrategyParams, overrides={"custom_flag": added})
    assert specs[-1] == added
    assert [spec.name for spec in specs[:-1]] == [spec.name for spec in EXPECTED_BASIC_SPECS]


def test_a_well_formed_override_is_returned_unchanged() -> None:
    manual = FreqtradeParamSpec(name="ema_fast", kind="int", default=9, low=1, high=18)
    specs = freqtrade_param_specs(BasicStrategyParams, overrides={"ema_fast": manual})
    assert specs[0] == manual


def test_a_malformed_override_is_repaired_not_emitted() -> None:
    manual = FreqtradeParamSpec(name="ema_fast", kind="int", default=9, low=30, high=40)
    specs = freqtrade_param_specs(BasicStrategyParams, overrides={"ema_fast": manual})
    spec = _spec(specs, "ema_fast")
    assert spec.default == 9
    assert spec.low <= spec.default <= spec.high
    assert spec.low < spec.high


@pytest.mark.parametrize("target", ["basic", 42, BasicStrategyParams(), object()])
def test_unsupported_targets_raise_strategy_error(target: Any) -> None:
    with pytest.raises(StrategyError) as excinfo:
        freqtrade_param_specs(target)
    assert "expects a StrategyParams subclass" in str(excinfo.value)


# ---------------------------------------------------------------------------
# freqtrade_param_specs -- invariant over every registered strategy
# ---------------------------------------------------------------------------


def test_the_invariant_holds_for_every_registered_strategy() -> None:
    assert STRATEGIES, "at least the reference strategy must be registered"
    for name, strategy_class in STRATEGIES.items():
        assert issubclass(strategy_class, Strategy)
        for grid in (None, strategy_class().param_space()):
            specs = freqtrade_param_specs(strategy_class.ParamsModel, grid=grid)
            for spec in specs:
                assert spec.name in strategy_class.ParamsModel.model_fields
                if spec.kind == "int":
                    assert spec.low < spec.high, (name, spec)
                    assert spec.low <= spec.default <= spec.high, (name, spec)
                elif spec.kind == "float":
                    assert spec.low < spec.high, (name, spec)
                    assert spec.low <= spec.default <= spec.high, (name, spec)
                    assert spec.decimals == FREQTRADE_DECIMALS
                elif spec.kind == "categorical":
                    assert spec.categories is not None
                    assert len(spec.categories) >= 2, (name, spec)
                    assert spec.default in spec.categories, (name, spec)
                    assert spec.low is None
                    assert spec.high is None
                    assert spec.decimals is None
                else:
                    assert spec.kind == "bool"
                    assert isinstance(spec.default, bool)


# ---------------------------------------------------------------------------
# startup_candle_count_for
# ---------------------------------------------------------------------------


def test_startup_candle_count_is_the_longest_integer_parameter() -> None:
    assert startup_candle_count_for(BasicStrategyParams) == 21  # ema_slow
    assert startup_candle_count_for(BasicStrategy) == 21
    assert startup_candle_count_for(BasicStrategy()) == 21
    assert startup_candle_count_for(BasicStrategy({"ema_slow": 55})) == 55


def test_startup_candle_count_is_zero_without_any_integer_field() -> None:
    assert startup_candle_count_for(EmptyParams) == 0
    assert startup_candle_count_for(OnlyBoolParams) == 0


def test_startup_candle_count_rejects_an_unsupported_target() -> None:
    with pytest.raises(StrategyError):
        startup_candle_count_for("basic")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# freqtrade_parameter_attributes -- always offline
# ---------------------------------------------------------------------------


def _simulate_missing_freqtrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``freqtrade`` unimportable, exactly as on a ``.[dev]``-only checkout.

    ``sys.modules["freqtrade"] = None`` alone is **not** enough once the real
    library has been imported somewhere in the session: ``from freqtrade.strategy
    import ...`` is then served straight from ``sys.modules`` without walking
    through the parent, so the simulated absence silently stops biting and the
    test would pass or fail depending on what an earlier module imported.  The
    cached submodules are therefore dropped too, through ``monkeypatch`` so every
    one of them is put back — the very same objects, hence a single
    ``IStrategy`` class — at teardown.
    """
    cached = [name for name in sys.modules if name == "freqtrade" or name.startswith("freqtrade.")]
    for name in cached:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "freqtrade", None)


def test_parameter_attributes_without_freqtrade_raises_strategy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the ``ccxt`` failure-path test of ``tests/test_data_loader.py``."""
    specs = freqtrade_param_specs(BasicStrategyParams)
    _simulate_missing_freqtrade(monkeypatch)
    with pytest.raises(StrategyError) as excinfo:
        freqtrade_parameter_attributes(specs)
    assert "freqtrade is not installed" in str(excinfo.value)
    assert "trading-backtest[freqtrade]" in str(excinfo.value)
    assert not isinstance(excinfo.value, ImportError)


def test_parameter_attributes_raises_before_touching_the_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _simulate_missing_freqtrade(monkeypatch)
    with pytest.raises(StrategyError) as excinfo:
        freqtrade_parameter_attributes(())
    assert "pip install 'trading-backtest[freqtrade]'" in str(excinfo.value)


# ---------------------------------------------------------------------------
# freqtrade_parameter_attributes -- against the real library when available
# ---------------------------------------------------------------------------


def test_real_freqtrade_parameters_are_built() -> None:
    pytest.importorskip("freqtrade")
    from freqtrade.strategy import (
        BooleanParameter,
        CategoricalParameter,
        DecimalParameter,
        IntParameter,
    )

    specs = freqtrade_param_specs(BasicStrategyParams, grid=BASIC_GRID)
    attributes = freqtrade_parameter_attributes(specs)

    assert list(attributes) == [spec.name for spec in specs]
    for spec in specs:
        parameter = attributes[spec.name]
        assert parameter.space == FREQTRADE_PARAM_SPACE
        assert parameter.value == spec.default
        assert list(parameter.range) == [spec.default]
        if spec.kind == "int":
            assert isinstance(parameter, IntParameter)
            assert type(parameter) is IntParameter
            assert (parameter.low, parameter.high) == (spec.low, spec.high)
            rebuilt = IntParameter(low=spec.low, high=spec.high, default=spec.default)
        elif spec.kind == "float":
            assert isinstance(parameter, DecimalParameter)
            assert type(parameter) is DecimalParameter
            assert (parameter.low, parameter.high) == (spec.low, spec.high)
            assert parameter.decimals == FREQTRADE_DECIMALS
            rebuilt = DecimalParameter(
                low=spec.low, high=spec.high, default=spec.default, decimals=spec.decimals
            )
        elif spec.kind == "bool":
            assert isinstance(parameter, BooleanParameter)
            rebuilt = BooleanParameter(default=spec.default)
        else:
            assert isinstance(parameter, CategoricalParameter)
            assert type(parameter) is CategoricalParameter
            assert tuple(parameter.opt_range) == spec.categories
            rebuilt = CategoricalParameter(categories=spec.categories, default=spec.default)
        assert rebuilt.value == spec.default


def test_real_freqtrade_parameters_accept_every_spec_of_every_registered_strategy() -> None:
    pytest.importorskip("freqtrade")
    from freqtrade.strategy import DecimalParameter, IntParameter

    assert STRATEGIES
    for name, strategy_class in STRATEGIES.items():
        for grid in (None, strategy_class().param_space()):
            specs = freqtrade_param_specs(strategy_class.ParamsModel, grid=grid)
            attributes = freqtrade_parameter_attributes(specs)
            assert list(attributes) == [spec.name for spec in specs]
            for spec in specs:
                parameter = attributes[spec.name]
                if isinstance(parameter, (IntParameter, DecimalParameter)):
                    assert (parameter.low, parameter.high) == (spec.low, spec.high), name
                    assert parameter.low <= parameter.value <= parameter.high, name
