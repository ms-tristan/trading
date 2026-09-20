"""Contract tests for the typed configuration layer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from trading_platform.config import (
    AppConfig,
    BacktestConfig,
    DataConfig,
    ReportingConfig,
    StrategyConfig,
    ValidationConfig,
    default_config,
    dump_config,
    load_config,
    override_params,
)
from trading_platform.config.models import (
    ProfileConfig,
    RealtimeConfig,
    resolve_platform_initial_balance,
)
from trading_platform.core.constants import DEFAULT_TIMEFRAME
from trading_platform.core.errors import ConfigError

# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------


def test_root_defaults_are_exactly_the_documented_ones() -> None:
    cfg = default_config()
    assert cfg.project_name == "trading-platform"
    assert cfg.log_level == "INFO"
    assert AppConfig.model_config["env_prefix"] == "TB_"
    assert AppConfig.model_config["env_nested_delimiter"] == "__"
    assert AppConfig.model_config["extra"] == "forbid"
    assert AppConfig.model_config["validate_default"] is True


def test_exchange_defaults() -> None:
    exchange = default_config().exchange
    assert exchange.name == "binance"
    assert exchange.market == "spot"
    assert exchange.quote_currency == "USDT"
    assert exchange.fee_rate == 0.001
    assert exchange.slippage == 0.0
    assert exchange.rate_limit_ms == 200


def test_data_defaults() -> None:
    data = default_config().data
    assert data.data_dir == Path("data")
    assert data.cache_dir == Path("data/cache")
    assert data.format == "parquet"
    assert data.timeframe == DEFAULT_TIMEFRAME == "1h"
    assert data.start is None
    assert data.end is None
    assert data.allow_network is True
    assert data.validate is True
    assert data.max_gap_factor == 3.0
    assert data.max_missing_ratio == 0.0


def test_strategy_defaults() -> None:
    strategy = default_config().strategy
    assert strategy.name == "basic"
    assert strategy.timeframe == "1h"
    assert strategy.params == {}


def test_backtest_defaults() -> None:
    backtest = default_config().backtest
    assert backtest.initial_balance == 10_000.0
    assert backtest.stake_amount is None
    assert backtest.max_open_trades == 1
    assert backtest.fee_rate == 0.001
    assert backtest.slippage == 0.0
    assert backtest.allow_short is False
    assert backtest.compute_metrics is True


def test_validation_defaults() -> None:
    validation = default_config().validation
    assert validation.n_windows == 5
    assert validation.in_sample_ratio == 0.7
    assert validation.mode == "rolling"
    assert validation.purge_candles == 0
    assert validation.n_monte_carlo == 1000
    assert validation.monte_carlo_method == "trade_resample"
    assert validation.random_seed == 42
    assert validation.robustness_metric == "sharpe_ratio"
    assert validation.robustness_max_combinations == 512
    assert validation.robustness_grid == {}


def test_reporting_defaults_do_not_include_html() -> None:
    reporting = default_config().reporting
    assert reporting.output_dir == Path("reports")
    assert reporting.basename == "report"
    assert reporting.formats == ["markdown", "json"]
    assert reporting.title == "Backtest Report"
    assert reporting.include_trades is True
    assert reporting.trade_limit == 50
    with pytest.raises(ValidationError):
        ReportingConfig(formats=["html"])


def test_sub_config_defaults_are_independent_instances() -> None:
    first, second = default_config(), default_config()
    assert first.exchange is not second.exchange
    first.strategy.params["fast"] = 9
    assert second.strategy.params == {}
    assert default_config().strategy.params == {}


def test_models_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        StrategyConfig(nope=1)
    with pytest.raises(ValidationError):
        ValidationConfig(nope=1)
    with pytest.raises(ValidationError):
        BacktestConfig(nope=1)
    with pytest.raises(ValidationError):
        DataConfig(nope=1)
    with pytest.raises(ValidationError):
        AppConfig(nope=1)


def test_data_timeframe_is_restricted_to_supported_timeframes() -> None:
    assert DataConfig(timeframe="4h").timeframe == "4h"
    with pytest.raises(ValidationError):
        DataConfig(timeframe="2h")


# ---------------------------------------------------------------------------
# environment variables
# ---------------------------------------------------------------------------


def test_environment_overrides_nested_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_DATA__TIMEFRAME", "4h")
    monkeypatch.setenv("TB_BACKTEST__INITIAL_BALANCE", "2500")
    cfg = AppConfig()
    assert cfg.data.timeframe == "4h"
    assert cfg.backtest.initial_balance == 2500.0
    assert load_config().backtest.initial_balance == 2500.0


def test_invalid_environment_value_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_DATA__TIMEFRAME", "2h")
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    assert "data.timeframe" in str(excinfo.value)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_without_path_equals_the_defaults() -> None:
    assert load_config() == default_config()
    assert load_config(None, None) == default_config()
    assert load_config(overrides={}) == default_config()


def test_load_config_from_a_json_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "project_name": "from-file",
                "log_level": "DEBUG",
                "data": {"timeframe": "4h", "cache_dir": str(tmp_path / "cache")},
                "backtest": {"initial_balance": 5000},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.project_name == "from-file"
    assert cfg.log_level == "DEBUG"
    assert cfg.data.timeframe == "4h"
    assert cfg.data.cache_dir == tmp_path / "cache"
    assert cfg.backtest.initial_balance == 5000.0
    assert cfg.exchange.fee_rate == 0.001  # untouched default


def test_load_config_accepts_str_path(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"project_name": "str-path"}), encoding="utf-8")
    assert load_config(str(path)).project_name == "str-path"


def test_nested_overrides_win_over_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"data": {"timeframe": "4h"}, "log_level": "DEBUG"}), encoding="utf-8"
    )
    cfg = load_config(path, {"data": {"timeframe": "1d"}, "backtest": {"initial_balance": 100}})
    assert cfg.data.timeframe == "1d"
    assert cfg.backtest.initial_balance == 100.0
    assert cfg.log_level == "DEBUG"


def test_dotted_overrides_win_over_nested_and_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"data": {"timeframe": "4h"}}), encoding="utf-8")
    cfg = load_config(
        path,
        {
            "data": {"timeframe": "1d"},
            "data.timeframe": "1h",
            "backtest.initial_balance": 5000,
            "strategy.params.fast": 9,
        },
    )
    assert cfg.data.timeframe == "1h"
    assert cfg.backtest.initial_balance == 5000.0
    assert cfg.strategy.params == {"fast": 9}


def test_load_config_rejects_non_json_suffix(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("data: {}", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "format" in str(excinfo.value)


def test_load_config_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "missing.json")
    assert "not found" in str(excinfo.value)


def test_load_config_rejects_a_path_that_is_not_a_file(tmp_path: Path) -> None:
    directory = tmp_path / "config.json"
    directory.mkdir()
    with pytest.raises(ConfigError) as excinfo:
        load_config(directory)
    assert "not a file" in str(excinfo.value)


def test_overrides_with_an_empty_dotted_key_raise_config_error() -> None:
    with pytest.raises(ConfigError):
        load_config(overrides={".": 1})


def test_app_config_timezone_property_is_utc() -> None:
    assert default_config().timezone == "UTC"


def test_load_config_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not: json", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "invalid JSON" in str(excinfo.value)


def test_load_config_rejects_a_non_object_payload(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "JSON object" in str(excinfo.value)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"nope": 1}, "nope"),
        ({"data": {"timefrmae": "1h"}}, "timefrmae"),
        ({"validation": {"in_sample_ratio": 1.5}}, "in_sample_ratio"),
        ({"validation": {"in_sample_ratio": 0.0}}, "in_sample_ratio"),
        ({"backtest": {"initial_balance": -1}}, "initial_balance"),
        ({"backtest": {"initial_balance": 0}}, "initial_balance"),
        ({"backtest": {"stake_amount": 0}}, "stake_amount"),
        ({"backtest": {"max_open_trades": 0}}, "max_open_trades"),
        ({"data": {"timeframe": "2h"}}, "data.timeframe"),
        ({"data": {"max_missing_ratio": 1.5}}, "max_missing_ratio"),
        ({"data": {"max_gap_factor": 0.5}}, "max_gap_factor"),
        ({"validation": {"mode": "nope"}}, "mode"),
        ({"reporting": {"formats": ["html"]}}, "formats"),
        ({"exchange": {"market": "margin"}}, "market"),
        ({"log_level": "TRACE"}, "log_level"),
    ],
)
def test_invalid_overrides_raise_config_error_naming_the_field(
    overrides: dict[str, object], field: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(overrides=overrides)
    assert field in str(excinfo.value)


def test_max_open_trades_above_one_is_accepted() -> None:
    assert load_config(overrides={"backtest.max_open_trades": 3}).backtest.max_open_trades == 3


# ---------------------------------------------------------------------------
# dump_config
# ---------------------------------------------------------------------------


def test_dump_config_writes_a_reloadable_document(tmp_path: Path) -> None:
    cfg = load_config(
        overrides={"data": {"timeframe": "4h"}, "backtest": {"initial_balance": 5000}}
    )
    path = dump_config(cfg, tmp_path / "nested" / "config.json")
    assert path == tmp_path / "nested" / "config.json"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{\n")
    assert text.endswith("\n")
    assert load_config(path) == cfg


def test_dump_config_round_trip_of_the_defaults(tmp_path: Path) -> None:
    cfg = default_config()
    path = dump_config(cfg, tmp_path / "config.json")
    reloaded = load_config(path)
    assert reloaded == cfg
    assert reloaded.model_dump() == cfg.model_dump()


def test_dump_config_is_sorted_and_indented(tmp_path: Path) -> None:
    path = dump_config(default_config(), tmp_path / "config.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload) == sorted(payload)
    assert payload["project_name"] == "trading-platform"
    assert payload["data"]["cache_dir"] == "data/cache"


def test_dump_config_accepts_a_str_path(tmp_path: Path) -> None:
    path = dump_config(default_config(), str(tmp_path / "config.json"))
    assert isinstance(path, Path)
    assert path.is_file()


# ---------------------------------------------------------------------------
# override_params
# ---------------------------------------------------------------------------


def test_override_params_merges_and_does_not_mutate_the_source() -> None:
    cfg = default_config()
    cfg.strategy.params["fast"] = 9
    updated = override_params(cfg, {"slow": 21, "fast": 12})
    assert updated.strategy.params == {"fast": 12, "slow": 21}
    assert cfg.strategy.params == {"fast": 9}
    assert updated is not cfg
    assert updated.strategy is not cfg.strategy
    assert updated != cfg
    assert updated.data == cfg.data


def test_override_params_on_an_empty_mapping_returns_an_equal_copy() -> None:
    cfg = default_config()
    updated = override_params(cfg, {})
    assert updated == cfg
    assert updated is not cfg


# ---------------------------------------------------------------------------
# profiles live in the SQLite state store, not in a JSON document
# ---------------------------------------------------------------------------


def seed_profiles(tmp_path: Path, identifiers: list[str]) -> Any:
    """Persist one profile per identifier in a fresh state store, and return it."""
    from trading_platform.realtime.store import SqliteStateStore

    store = SqliteStateStore(tmp_path / "state.db")
    store.initialize()
    for identifier in identifiers:
        store.save_profile(
            ProfileConfig(
                id=identifier,
                symbol="BTC/USDT",
                timeframe="1h",
                strategy="basic",
                mode="paper",
                initial_balance=1000.0,
            )
        )
    return store


def test_profiles_round_trip_through_the_state_store(tmp_path: Path) -> None:
    """Two profiles out, the very same two models in -- no document involved."""
    store = seed_profiles(tmp_path, ["aaa-paper", "bbb-paper"])
    try:
        rewritten = store.load_profiles()
        assert [item.id for item in rewritten] == ["aaa-paper", "bbb-paper"]
        assert all(isinstance(item, ProfileConfig) for item in rewritten)
    finally:
        store.close()


def test_a_saved_profile_is_updated_in_place(tmp_path: Path) -> None:
    """The identifier is the natural key: re-saving never duplicates a row."""
    store = seed_profiles(tmp_path, ["aaa-paper"])
    try:
        store.save_profile(ProfileConfig(id="aaa-paper", symbol="ETH/USDT"))
        profiles = store.load_profiles()
        assert len(profiles) == 1
        assert str(profiles[0].symbol) == "ETH/USDT"
    finally:
        store.close()


def test_a_deleted_profile_disappears_from_the_store(tmp_path: Path) -> None:
    """Deleting a profile is a store write, and an empty set is legal."""
    store = seed_profiles(tmp_path, ["aaa-paper", "bbb-paper"])
    try:
        assert store.load_profiles()
        assert store.delete_profile("aaa-paper") is True
        assert [item.id for item in store.load_profiles()] == ["bbb-paper"]
        # deleting what is gone is an idempotent no-op
        assert store.delete_profile("aaa-paper") is False
        store.delete_profile("bbb-paper")
        assert store.load_profiles() == []
    finally:
        store.close()


def test_the_store_refuses_a_profile_carrying_a_credential(tmp_path: Path) -> None:
    """The model is the write-side validation: a credential never reaches the row."""
    store = seed_profiles(tmp_path, ["aaa-paper"])
    try:
        with pytest.raises(ValidationError) as excinfo:
            ProfileConfig.model_validate(
                {"id": "bad", "symbol": "BTC/USDT", "api_key": "super-secret"}
            )
        assert "api_key" in str(excinfo.value)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# the shared platform wallet: per-profile allocation
# ---------------------------------------------------------------------------


def test_profile_allocation_defaults_to_none_and_falls_back_to_initial_balance() -> None:
    """``allocation`` is optional: absent, the profile's ``initial_balance`` is it."""
    profile = ProfileConfig(id="btc-paper", symbol="BTC/USDT", initial_balance=8000.0)
    assert profile.allocation is None
    assert profile.effective_allocation == 8000.0
    assert profile.model_dump()["allocation"] is None


def test_profile_allocation_wins_over_the_initial_balance() -> None:
    """A configured allocation is the profile's share of the wallet."""
    profile = ProfileConfig(
        id="btc-paper", symbol="BTC/USDT", initial_balance=8000.0, allocation=2500.0
    )
    assert profile.allocation == 2500.0
    assert profile.effective_allocation == 2500.0
    # The legacy field keeps its own value: only the meaning of the attributed
    # figures is refined, no existing field changes name or type.
    assert profile.initial_balance == 8000.0
    assert isinstance(profile.effective_allocation, float)


def test_effective_allocation_is_float_even_for_an_int_allocation() -> None:
    profile = ProfileConfig(id="btc-paper", symbol="BTC/USDT", allocation=2500)
    assert profile.effective_allocation == 2500.0
    assert isinstance(profile.effective_allocation, float)


@pytest.mark.parametrize("allocation", [0.0, 0, -1.0, -2500.0])
def test_profile_allocation_must_be_positive(tmp_path: Path, allocation: float) -> None:
    """Zero and negative allocations are invalid, and the error names the field."""
    with pytest.raises(ValidationError):
        ProfileConfig(id="btc-paper", symbol="BTC/USDT", allocation=allocation)

    with pytest.raises(ValidationError) as excinfo:
        ProfileConfig(id="aaa-paper", symbol="BTC/USDT", allocation=allocation)
    assert "allocation" in str(excinfo.value)


def test_a_profile_stored_before_the_wallet_still_loads(tmp_path: Path) -> None:
    """Backward compatibility: no ``allocation`` key, no platform key, no change."""
    store = seed_profiles(tmp_path, ["aaa-paper", "bbb-paper"])
    try:
        profiles = store.load_profiles()
        assert [profile.allocation for profile in profiles] == [None, None]
        assert [profile.effective_allocation for profile in profiles] == [1000.0, 1000.0]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# the shared platform wallet: platform-wide configuration
# ---------------------------------------------------------------------------


def test_platform_configuration_fields_default_to_none() -> None:
    """An absent platform field means "no platform cap" / "no initial balance"."""
    realtime = default_config().realtime
    assert realtime.platform_initial_balance is None
    assert realtime.platform_max_total_notional is None
    assert realtime.platform_max_daily_loss is None


def test_platform_configuration_fields_are_read_back() -> None:
    realtime = RealtimeConfig(
        platform_initial_balance=15_000.0,
        platform_max_total_notional=12_000.0,
        platform_max_daily_loss=1_000.0,
    )
    assert realtime.platform_initial_balance == 15_000.0
    assert realtime.platform_max_total_notional == 12_000.0
    assert realtime.platform_max_daily_loss == 1_000.0
    assert RealtimeConfig.model_validate(realtime.model_dump(mode="json")) == realtime


@pytest.mark.parametrize(
    "field",
    ["platform_initial_balance", "platform_max_total_notional", "platform_max_daily_loss"],
)
@pytest.mark.parametrize("value", [0.0, 0, -1.0])
def test_platform_configuration_fields_reject_zero_and_negative(field: str, value: float) -> None:
    """Each platform field must be strictly positive, and the error names it."""
    with pytest.raises(ValidationError):
        RealtimeConfig(**{field: value})

    with pytest.raises(ConfigError) as excinfo:
        load_config(overrides={"realtime": {field: value}})
    assert field in str(excinfo.value)


def test_an_unknown_platform_key_is_still_refused() -> None:
    """``extra='forbid'`` stays: a typo in a platform key is a loud ConfigError."""
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RealtimeConfig(platform_max_leverage=3.0)
    with pytest.raises(ConfigError) as excinfo:
        load_config(overrides={"realtime": {"platform_max_leverage": 3.0}})
    assert "platform_max_leverage" in str(excinfo.value)


# ---------------------------------------------------------------------------
# the shared platform wallet: the starting balance
# ---------------------------------------------------------------------------


def _allocation_profiles() -> list[ProfileConfig]:
    """Return one profile falling back to ``initial_balance`` and one allocated."""
    return [
        ProfileConfig(id="btc-paper", symbol="BTC/USDT", initial_balance=10_000.0),
        ProfileConfig(
            id="eth-paper", symbol="ETH/USDT", initial_balance=5_000.0, allocation=2_500.0
        ),
    ]


def test_resolve_platform_initial_balance_prefers_the_configured_value() -> None:
    realtime = RealtimeConfig(platform_initial_balance=15_000.0)
    assert resolve_platform_initial_balance(realtime, _allocation_profiles()) == 15_000.0


def test_resolve_platform_initial_balance_falls_back_to_the_effective_allocations() -> None:
    """Without a platform balance the wallet starts at the sum of the shares."""
    profiles = _allocation_profiles()
    assert resolve_platform_initial_balance(RealtimeConfig(), profiles) == 12_500.0
    assert resolve_platform_initial_balance(RealtimeConfig(), profiles) == sum(
        profile.effective_allocation for profile in profiles
    )
    # Any sequence is accepted, a tuple included.
    assert resolve_platform_initial_balance(RealtimeConfig(), tuple(profiles)) == 12_500.0


def test_resolve_platform_initial_balance_of_a_legacy_document_is_the_sum_of_balances() -> None:
    """A configuration predating the wallet funds it with exactly its balances."""
    profiles = [
        ProfileConfig(id="btc-paper", symbol="BTC/USDT", initial_balance=10_000.0),
        ProfileConfig(id="eth-paper", symbol="ETH/USDT", initial_balance=5_000.0),
    ]
    assert resolve_platform_initial_balance(RealtimeConfig(), profiles) == 15_000.0


def test_resolve_platform_initial_balance_is_zero_without_profiles() -> None:
    assert resolve_platform_initial_balance(RealtimeConfig(), []) == 0.0
    assert isinstance(resolve_platform_initial_balance(RealtimeConfig(), []), float)
