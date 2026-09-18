"""Contract tests for the typed configuration layer."""

from __future__ import annotations

import json
import os
from pathlib import Path

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
from trading_platform.config.loader import load_profiles, save_profiles
from trading_platform.config.models import ProfileConfig
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
# save_profiles: the on-disk source of truth of a running platform
# ---------------------------------------------------------------------------


def profiles_document(tmp_path: Path, identifiers: list[str]) -> Path:
    """Write a profiles document carrying profiles, realtime and monitoring keys."""
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "id": identifier,
                        "symbol": "BTC/USDT",
                        "timeframe": "1h",
                        "strategy": "basic",
                        "mode": "paper",
                        "initial_balance": 1000.0,
                    }
                    for identifier in identifiers
                ],
                "realtime": {"state_db": str(tmp_path / "state.db"), "history_candles": 42},
                "monitoring": {"host": "127.0.0.1", "port": 8099},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_save_profiles_round_trips_the_rewritten_list(tmp_path: Path) -> None:
    """Two profiles out, the very same two models in."""
    path = profiles_document(tmp_path, ["aaa-paper", "bbb-paper"])
    rewritten = [
        ProfileConfig(id="ccc-paper", symbol="ETH/USDT", mode="paper"),
        ProfileConfig(id="ddd-paper", symbol="SOL/USDT", timeframe="4h", initial_balance=250.0),
    ]
    assert save_profiles(path, rewritten) == path
    assert save_profiles(str(path), rewritten) == path
    assert load_profiles(path) == rewritten


def test_save_profiles_preserves_every_other_root_key_verbatim(tmp_path: Path) -> None:
    """The engine and the server must not lose their settings to a profile edit."""
    path = profiles_document(tmp_path, ["aaa-paper"])
    before = json.loads(path.read_text(encoding="utf-8"))
    save_profiles(path, [ProfileConfig(id="aaa-paper", symbol="BTC/USDT")])
    text = path.read_text(encoding="utf-8")
    after = json.loads(text)
    assert after["realtime"] == before["realtime"]
    assert after["monitoring"] == before["monitoring"]
    assert sorted(after) == ["monitoring", "profiles", "realtime"]
    assert text.startswith("{\n")
    assert text.endswith("\n")
    assert list(after) == sorted(after)  # pretty, sorted keys, like dump_config


def test_save_profiles_adds_then_removes_a_profile(tmp_path: Path) -> None:
    """The exact sequence the create and delete routes perform."""
    path = profiles_document(tmp_path, ["aaa-paper"])
    first = load_profiles(path)
    created = ProfileConfig(id="bbb-paper", symbol="ETH/USDT")
    save_profiles(path, [*first, created])
    assert [item.id for item in load_profiles(path)] == ["aaa-paper", "bbb-paper"]
    save_profiles(path, load_profiles(path)[:1])
    assert [item.id for item in load_profiles(path)] == ["aaa-paper"]


def test_save_profiles_never_validates_the_empty_list(tmp_path: Path) -> None:
    """Writing an empty list is allowed; reading it back is still refused."""
    path = profiles_document(tmp_path, ["aaa-paper"])
    save_profiles(path, [])
    assert json.loads(path.read_text(encoding="utf-8"))["profiles"] == []
    with pytest.raises(ConfigError, match="declares no profile"):
        load_profiles(path)


def test_save_profiles_is_atomic_when_the_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing rename leaves the original file byte-identical, and no temporary."""
    path = profiles_document(tmp_path, ["aaa-paper", "bbb-paper"])
    original = path.read_bytes()
    real_replace = os.replace

    def failing_replace(source: object, target: object, *args: object, **kwargs: object) -> None:
        if Path(str(target)) == path:
            raise OSError("read-only file system")
        real_replace(source, target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(ConfigError) as excinfo:
        save_profiles(path, [ProfileConfig(id="aaa-paper", symbol="BTC/USDT")])
    assert f"cannot write profiles file {path}" in str(excinfo.value)
    assert path.read_bytes() == original
    assert sorted(item.name for item in tmp_path.iterdir()) == ["profiles.json"]


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="a root process ignores the directory permissions",
)
def test_save_profiles_reports_an_unwritable_directory(tmp_path: Path) -> None:
    """A directory that refuses the temporary file aborts without touching the file."""
    directory = tmp_path / "readonly"
    directory.mkdir()
    path = directory / "profiles.json"
    path.write_text(json.dumps({"profiles": []}), encoding="utf-8")
    original = path.read_bytes()
    directory.chmod(0o500)
    try:
        with pytest.raises(ConfigError) as excinfo:
            save_profiles(path, [ProfileConfig(id="aaa-paper", symbol="BTC/USDT")])
        assert f"cannot write profiles file {path}" in str(excinfo.value)
        assert path.read_bytes() == original
        assert sorted(item.name for item in directory.iterdir()) == ["profiles.json"]
    finally:
        directory.chmod(0o700)


def test_save_profiles_refuses_a_document_it_cannot_read(tmp_path: Path) -> None:
    """Saving updates a document; it never invents one."""
    with pytest.raises(ConfigError, match="not found"):
        save_profiles(tmp_path / "missing.json", [ProfileConfig(id="aaa", symbol="BTC/USDT")])
