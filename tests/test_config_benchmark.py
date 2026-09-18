"""Contract tests for the buy and hold benchmark configuration section."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_backtest import config as config_module
from trading_backtest.config import (
    AppConfig,
    BenchmarkConfig,
    default_config,
    load_config,
)
from trading_backtest.config import models as config_models
from trading_backtest.core.errors import ConfigError
from trading_backtest.freqtrade import validate_freqtrade_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "backtest_default.json"
E2E_CONFIG_PATH = CONFIG_DIR / "backtest_btc_benchmark_e2e.json"


# ---------------------------------------------------------------------------
# BenchmarkConfig defaults
# ---------------------------------------------------------------------------


def test_benchmark_defaults_enable_buy_and_hold() -> None:
    benchmark = BenchmarkConfig()
    assert benchmark.enabled is True
    assert benchmark.variant == "buy_and_hold"


def test_benchmark_defaults_are_the_documented_ones() -> None:
    benchmark = default_config().benchmark
    assert isinstance(benchmark, BenchmarkConfig)
    assert benchmark.enabled is True
    assert benchmark.variant == "buy_and_hold"
    # ``0.0`` keeps every historical Sharpe/Sortino byte-identical: a non-zero
    # default would silently rewrite the numbers of every existing command.
    assert benchmark.risk_free_rate == 0.0
    assert benchmark.n_random_simulations == 1000
    assert benchmark.random_entry_seed == 42


@pytest.mark.parametrize(
    "variant",
    ["buy_and_hold", "cash", "risk_free", "random_entry", "none"],
)
def test_benchmark_accepts_every_variant(variant: str) -> None:
    assert BenchmarkConfig(variant=variant).variant == variant


def test_benchmark_can_be_disabled() -> None:
    assert BenchmarkConfig(enabled=False).enabled is False


def test_none_variant_is_the_explicit_opt_out() -> None:
    benchmark = BenchmarkConfig(enabled=True, variant="none")
    assert benchmark.variant == "none"


def test_risk_free_and_cash_are_distinct_variants() -> None:
    benchmark = BenchmarkConfig(variant="risk_free", risk_free_rate=0.05)
    assert benchmark.variant == "risk_free"
    assert benchmark.risk_free_rate == 0.05
    assert BenchmarkConfig(variant="cash").risk_free_rate == 0.0


# ---------------------------------------------------------------------------
# strictness
# ---------------------------------------------------------------------------


def test_benchmark_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        BenchmarkConfig(min_alpha=1)
    with pytest.raises(ValidationError):
        BenchmarkConfig(enabled=True, threshold=0.5)


def test_benchmark_rejects_an_invalid_variant() -> None:
    with pytest.raises(ValidationError):
        BenchmarkConfig(variant="hodl")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"risk_free_rate": -0.01},
        {"n_random_simulations": 0},
        {"n_random_simulations": 10_001},
        {"random_entry_seed": -1},
    ],
)
def test_benchmark_rejects_out_of_range_values(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        BenchmarkConfig(**kwargs)


def test_benchmark_accepts_the_boundary_values() -> None:
    assert BenchmarkConfig(risk_free_rate=0.0).risk_free_rate == 0.0
    assert BenchmarkConfig(n_random_simulations=1).n_random_simulations == 1
    assert BenchmarkConfig(n_random_simulations=10_000).n_random_simulations == 10_000
    assert BenchmarkConfig(random_entry_seed=0).random_entry_seed == 0


def test_app_config_rejects_unknown_benchmark_field() -> None:
    with pytest.raises(ValidationError):
        AppConfig(benchmark={"min_alpha": 1})


# ---------------------------------------------------------------------------
# AppConfig integration
# ---------------------------------------------------------------------------


def test_app_config_exposes_the_benchmark_section() -> None:
    cfg = default_config()
    assert isinstance(cfg.benchmark, BenchmarkConfig)
    assert cfg.model_dump(mode="json")["benchmark"] == {
        "enabled": True,
        "n_random_simulations": 1000,
        "random_entry_seed": 42,
        "risk_free_rate": 0.0,
        "variant": "buy_and_hold",
    }


def test_benchmark_sits_between_backtest_and_validation() -> None:
    fields = list(AppConfig.model_fields)
    assert fields.index("backtest") < fields.index("benchmark") < fields.index("validation")


def test_benchmark_does_not_disturb_the_settings_config() -> None:
    assert AppConfig.model_config["env_prefix"] == "TB_"
    assert AppConfig.model_config["env_nested_delimiter"] == "__"
    assert AppConfig.model_config["extra"] == "forbid"
    assert AppConfig.model_config["validate_default"] is True


def test_nested_overrides_reach_the_benchmark_section() -> None:
    cfg = load_config(overrides={"benchmark": {"enabled": False, "variant": "cash"}})
    assert cfg.benchmark.enabled is False
    assert cfg.benchmark.variant == "cash"
    assert load_config(overrides={"benchmark.variant": "none"}).benchmark.variant == "none"


def test_invalid_benchmark_override_names_the_field() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(overrides={"benchmark": {"variant": "hodl"}})
    assert "benchmark.variant" in str(excinfo.value)


def test_sub_config_instances_are_independent() -> None:
    first, second = AppConfig(), AppConfig()
    assert first.benchmark is not second.benchmark
    first.benchmark.enabled = False
    first.benchmark.variant = "cash"
    assert second.benchmark.enabled is True
    assert second.benchmark.variant == "buy_and_hold"
    assert AppConfig().benchmark.variant == "buy_and_hold"


# ---------------------------------------------------------------------------
# environment variables
# ---------------------------------------------------------------------------


def test_environment_overrides_the_benchmark_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_BENCHMARK__ENABLED", "false")
    monkeypatch.setenv("TB_BENCHMARK__VARIANT", "cash")
    cfg = AppConfig()
    assert cfg.benchmark.enabled is False
    assert cfg.benchmark.variant == "cash"
    assert load_config().benchmark.variant == "cash"


def test_invalid_benchmark_environment_value_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TB_BENCHMARK__VARIANT", "hodl")
    with pytest.raises(ValidationError) as excinfo:
        AppConfig()
    assert "benchmark.variant" in str(excinfo.value)


def test_environment_overrides_the_risk_free_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_BENCHMARK__RISK_FREE_RATE", "0.05")
    cfg = AppConfig()
    assert cfg.benchmark.risk_free_rate == 0.05
    assert load_config().benchmark.risk_free_rate == 0.05


def test_environment_overrides_the_risk_free_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_BENCHMARK__VARIANT", "risk_free")
    assert AppConfig().benchmark.variant == "risk_free"
    assert load_config().benchmark.variant == "risk_free"


def test_environment_overrides_the_random_entry_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TB_BENCHMARK__N_RANDOM_SIMULATIONS", "50")
    monkeypatch.setenv("TB_BENCHMARK__RANDOM_ENTRY_SEED", "7")
    cfg = AppConfig()
    assert cfg.benchmark.n_random_simulations == 50
    assert cfg.benchmark.random_entry_seed == 7


def test_invalid_risk_free_environment_value_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TB_BENCHMARK__RISK_FREE_RATE", "-0.01")
    with pytest.raises(ValidationError) as excinfo:
        AppConfig()
    assert "benchmark.risk_free_rate" in str(excinfo.value)


# ---------------------------------------------------------------------------
# committed configuration file
# ---------------------------------------------------------------------------


def test_committed_default_config_matches_the_defaults() -> None:
    from_file = load_config(DEFAULT_CONFIG_PATH)
    assert from_file.model_dump(mode="json") == default_config().model_dump(mode="json")


def test_committed_default_config_documents_the_benchmark() -> None:
    from_file = load_config(DEFAULT_CONFIG_PATH)
    assert from_file.benchmark.enabled is True
    assert from_file.benchmark.variant == "buy_and_hold"


def test_committed_default_config_is_canonical() -> None:
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload == default_config().model_dump(mode="json")
    assert text == json.dumps(payload, indent=2, sort_keys=True) + "\n"
    assert list(payload) == sorted(payload)


def test_committed_files_still_load() -> None:
    # ``backtest_default.json`` is the AppConfig-shaped document ...
    assert isinstance(load_config(DEFAULT_CONFIG_PATH), AppConfig)
    # ... while the two committed Freqtrade files use the Freqtrade schema and
    # are validated by the Freqtrade layer (see tests/test_freqtrade_config.py).
    for name in ("freqtrade_config.json", "freqtrade_dryrun.json"):
        payload = json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        assert validate_freqtrade_config(payload) == []
        assert "benchmark" not in payload


def test_e2e_config_pins_the_realistic_risk_free_rate() -> None:
    """The E2E run is the one that opts into the 2023-2025 US T-bill level."""
    cfg = load_config(E2E_CONFIG_PATH)
    assert cfg.benchmark.enabled is True
    assert cfg.benchmark.variant == "buy_and_hold"
    assert cfg.benchmark.risk_free_rate == 0.05
    assert cfg.benchmark.n_random_simulations == 1000
    assert cfg.benchmark.random_entry_seed == 42


def test_e2e_config_leaves_every_other_section_untouched() -> None:
    raw = json.loads(E2E_CONFIG_PATH.read_text(encoding="utf-8"))
    assert raw["benchmark"] == {
        "enabled": True,
        "n_random_simulations": 1000,
        "random_entry_seed": 42,
        "risk_free_rate": 0.05,
        "variant": "buy_and_hold",
    }
    dumped = load_config(E2E_CONFIG_PATH).model_dump(mode="json")
    assert {k: v for k, v in dumped.items() if k != "benchmark"} == {
        k: v for k, v in raw.items() if k != "benchmark"
    }


# ---------------------------------------------------------------------------
# public API surface
# ---------------------------------------------------------------------------


def test_benchmark_config_is_exported() -> None:
    assert "BenchmarkConfig" in config_module.__all__
    assert "BenchmarkConfig" in config_models.__all__
    assert config_module.BenchmarkConfig is BenchmarkConfig
    assert config_models.__all__ == sorted(config_models.__all__)
