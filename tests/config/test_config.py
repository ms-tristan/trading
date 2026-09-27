"""Tests for the platform settings, the environment overrides and the live gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_platform import config


def test_environment_variable_names() -> None:
    assert config.ENV_MAX_RUNNING_PROFILES == "TB_MAX_RUNNING_PROFILES"
    assert config.ENV_SNAPSHOT_INTERVAL_SECONDS == "TB_SNAPSHOT_INTERVAL_SECONDS"
    assert config.ENV_PROFILE_API_PORT_BASE == "TB_PROFILE_API_PORT_BASE"
    assert config.ENV_WORKER_START_STAGGER_SECONDS == "TB_WORKER_START_STAGGER_SECONDS"
    assert config.ENV_OPERATOR_TOKEN == "TB_OPERATOR_TOKEN"
    assert config.ENV_ALLOW_LIVE_TRADING == "TB_ALLOW_LIVE_TRADING"
    assert config.ENV_LIVE_EXCHANGE_KEY == "TB_LIVE_EXCHANGE_KEY"
    assert config.ENV_LIVE_EXCHANGE_SECRET == "TB_LIVE_EXCHANGE_SECRET"
    assert config.ENV_LOG_LEVEL == "TB_LOG_LEVEL"
    assert config.ENV_FREETRADE_BIN == "TB_FREETRADE_BIN"
    assert config.LIVE_TRADING_CONFIRMATION == "I_UNDERSTAND_THE_RISK"
    assert config.PLATFORM_CONFIG_FILENAME == "platform.json"


def test_default_settings_are_the_documented_ones() -> None:
    settings = config.PlatformSettings()
    assert settings.max_running_profiles == 6
    assert settings.snapshot_interval_seconds == 60
    assert settings.worker_start_stagger_seconds == 10
    assert settings.profile_api_port_base == 8101
    assert settings.default_exchange == "binance"
    assert settings.default_timeframe == "1h"
    assert settings.default_stake_currency == "USDT"
    assert settings.default_initial_capital == 1000.0
    assert settings.default_max_open_trades == 2
    assert settings.dashboard_refresh_seconds == 15
    assert settings.equity_retention_days == 90


def test_platform_document_carries_exactly_the_eleven_settings(repo_root: Path) -> None:
    document = json.loads((repo_root / "config" / "platform.json").read_text(encoding="utf-8"))
    assert set(document) == set(config.PlatformSettings.model_fields)
    assert config.PlatformSettings.load(env={}).model_dump() == document


def test_load_reads_an_explicit_document(tmp_path: Path) -> None:
    path = tmp_path / "platform.json"
    path.write_text(
        json.dumps({"max_running_profiles": 4, "default_timeframe": "4h"}),
        encoding="utf-8",
    )
    settings = config.PlatformSettings.load(path, env={})
    assert settings.max_running_profiles == 4
    assert settings.default_timeframe == "4h"
    assert settings.snapshot_interval_seconds == 60
    assert settings.profile_api_port_base == 8101


def test_load_ignores_unknown_document_keys(tmp_path: Path) -> None:
    path = tmp_path / "platform.json"
    path.write_text(json.dumps({"max_running_profiles": 4, "removed_key": True}), encoding="utf-8")
    settings = config.PlatformSettings.load(path, env={})
    assert settings.max_running_profiles == 4
    assert not hasattr(settings, "removed_key")


def test_load_falls_back_to_defaults_when_the_file_is_missing(tmp_path: Path) -> None:
    settings = config.PlatformSettings.load(tmp_path / "absent.json", env={})
    assert settings == config.PlatformSettings()


def test_load_falls_back_to_defaults_when_the_path_is_not_a_file(tmp_path: Path) -> None:
    settings = config.PlatformSettings.load(tmp_path, env={})
    assert settings == config.PlatformSettings()


def test_load_falls_back_to_defaults_on_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "platform.json"
    path.write_text("{not json", encoding="utf-8")
    assert config.PlatformSettings.load(path, env={}) == config.PlatformSettings()


def test_load_falls_back_to_defaults_when_the_document_is_not_an_object(tmp_path: Path) -> None:
    path = tmp_path / "platform.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert config.PlatformSettings.load(path, env={}) == config.PlatformSettings()


def test_load_falls_back_to_defaults_when_a_value_has_the_wrong_type(tmp_path: Path) -> None:
    path = tmp_path / "platform.json"
    path.write_text(json.dumps({"max_running_profiles": "many"}), encoding="utf-8")
    assert config.PlatformSettings.load(path, env={}) == config.PlatformSettings()


def test_load_resolves_the_document_from_the_environment(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / config.PLATFORM_CONFIG_FILENAME).write_text(
        json.dumps({"max_running_profiles": 7}), encoding="utf-8"
    )
    monkeypatch.setenv("TB_CONFIG_DIR", str(config_dir))
    assert config.PlatformSettings.load().max_running_profiles == 7
    assert config.PlatformSettings.load(env={}).max_running_profiles == 6


def test_load_applies_the_documented_environment_overrides() -> None:
    env = {
        config.ENV_MAX_RUNNING_PROFILES: "3",
        config.ENV_SNAPSHOT_INTERVAL_SECONDS: "5",
        config.ENV_PROFILE_API_PORT_BASE: "9000",
        config.ENV_WORKER_START_STAGGER_SECONDS: "25",
    }
    settings = config.PlatformSettings.load(env=env)
    assert settings.max_running_profiles == 3
    assert settings.snapshot_interval_seconds == 5
    assert settings.profile_api_port_base == 9000
    assert settings.worker_start_stagger_seconds == 25


def test_load_honours_a_zero_worker_stagger() -> None:
    """``0`` is a documented value: it keeps the immediate boot, not an error."""
    settings = config.PlatformSettings.load(env={config.ENV_WORKER_START_STAGGER_SECONDS: "0"})
    assert settings.worker_start_stagger_seconds == 0


@pytest.mark.parametrize("raw", ["many", "-1"])
def test_load_ignores_an_unusable_worker_stagger(raw: str, tmp_path: Path) -> None:
    """A non-integer or negative stagger is ignored: the document value wins."""
    path = tmp_path / "platform.json"
    path.write_text(json.dumps({"worker_start_stagger_seconds": 30}), encoding="utf-8")
    env = {config.ENV_WORKER_START_STAGGER_SECONDS: raw}
    assert config.PlatformSettings.load(path, env=env).worker_start_stagger_seconds == 30


def test_load_ignores_unusable_environment_values() -> None:
    env = {
        config.ENV_MAX_RUNNING_PROFILES: "many",
        config.ENV_SNAPSHOT_INTERVAL_SECONDS: "0",
        config.ENV_PROFILE_API_PORT_BASE: "70000",
    }
    settings = config.PlatformSettings.load(env=env)
    assert settings == config.PlatformSettings()


def test_load_ignores_blank_environment_values() -> None:
    env = {
        config.ENV_MAX_RUNNING_PROFILES: "  ",
        config.ENV_SNAPSHOT_INTERVAL_SECONDS: "",
        config.ENV_PROFILE_API_PORT_BASE: " ",
        config.ENV_WORKER_START_STAGGER_SECONDS: " ",
    }
    assert config.PlatformSettings.load(env=env) == config.PlatformSettings()


def test_load_ignores_environment_variables_it_does_not_own() -> None:
    env = {
        config.ENV_LOG_LEVEL: "DEBUG",
        config.ENV_OPERATOR_TOKEN: "secret",
        config.ENV_ALLOW_LIVE_TRADING: config.LIVE_TRADING_CONFIRMATION,
        "TB_DEFAULT_TIMEFRAME": "4h",
    }
    assert config.PlatformSettings.load(env=env) == config.PlatformSettings()


def test_load_reads_the_process_environment(monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_PROFILE_API_PORT_BASE, "8500")
    monkeypatch.setenv(config.ENV_SNAPSHOT_INTERVAL_SECONDS, "30")
    settings = config.PlatformSettings.load()
    assert settings.profile_api_port_base == 8500
    assert settings.snapshot_interval_seconds == 30


def test_with_overrides_returns_a_new_validated_settings_object() -> None:
    base = config.PlatformSettings()
    updated = base.with_overrides(max_running_profiles=2, snapshot_interval_seconds=10)
    assert updated is not base
    assert updated.max_running_profiles == 2
    assert updated.snapshot_interval_seconds == 10
    assert base.max_running_profiles == 6


def test_with_overrides_round_trips_a_zero_worker_stagger() -> None:
    base = config.PlatformSettings()
    immediate = base.with_overrides(worker_start_stagger_seconds=0)
    assert immediate is not base
    assert immediate.worker_start_stagger_seconds == 0
    assert base.worker_start_stagger_seconds == 10
    assert immediate.with_overrides().worker_start_stagger_seconds == 0


def test_with_overrides_ignores_unknown_keys() -> None:
    updated = config.PlatformSettings().with_overrides(
        max_running_profiles=4, kill_switch_engaged=True, default_exchange="kraken"
    )
    assert updated.max_running_profiles == 4
    assert updated.default_exchange == "kraken"
    assert not hasattr(updated, "kill_switch_engaged")


def test_with_overrides_rejects_an_invalid_value() -> None:
    with pytest.raises(ValueError, match="max_running_profiles"):
        config.PlatformSettings().with_overrides(max_running_profiles="many")


def test_live_gate_allows_every_non_live_mode() -> None:
    assert config.live_trading_gate("paper", {}) == (True, None)
    assert config.live_trading_gate("dry_run", {}) == (True, None)


def test_live_gate_refuses_without_the_acknowledgement() -> None:
    allowed, reason = config.live_trading_gate("live", {})
    assert allowed is False
    assert reason == (
        "live trading refused: TB_ALLOW_LIVE_TRADING must equal I_UNDERSTAND_THE_RISK"
    )


def test_live_gate_refuses_a_wrong_acknowledgement() -> None:
    env = {config.ENV_ALLOW_LIVE_TRADING: "yes"}
    allowed, reason = config.live_trading_gate("live", env)
    assert allowed is False
    assert reason == (
        "live trading refused: TB_ALLOW_LIVE_TRADING must equal I_UNDERSTAND_THE_RISK"
    )


def test_live_gate_lists_both_missing_credentials() -> None:
    env = {config.ENV_ALLOW_LIVE_TRADING: config.LIVE_TRADING_CONFIRMATION}
    allowed, reason = config.live_trading_gate("live", env)
    assert allowed is False
    assert reason == (
        "live trading refused: missing environment variable(s): "
        "TB_LIVE_EXCHANGE_KEY, TB_LIVE_EXCHANGE_SECRET"
    )


def test_live_gate_lists_only_the_missing_credential() -> None:
    env = {
        config.ENV_ALLOW_LIVE_TRADING: config.LIVE_TRADING_CONFIRMATION,
        config.ENV_LIVE_EXCHANGE_KEY: "key",
    }
    allowed, reason = config.live_trading_gate("live", env)
    assert allowed is False
    assert reason == (
        "live trading refused: missing environment variable(s): TB_LIVE_EXCHANGE_SECRET"
    )


def test_live_gate_treats_blank_credentials_as_missing() -> None:
    env = {
        config.ENV_ALLOW_LIVE_TRADING: config.LIVE_TRADING_CONFIRMATION,
        config.ENV_LIVE_EXCHANGE_KEY: "   ",
        config.ENV_LIVE_EXCHANGE_SECRET: "secret",
    }
    assert config.live_trading_gate("live", env) == (
        False,
        "live trading refused: missing environment variable(s): TB_LIVE_EXCHANGE_KEY",
    )


def test_live_gate_allows_a_fully_configured_live_profile() -> None:
    env = {
        config.ENV_ALLOW_LIVE_TRADING: config.LIVE_TRADING_CONFIRMATION,
        config.ENV_LIVE_EXCHANGE_KEY: "key",
        config.ENV_LIVE_EXCHANGE_SECRET: "secret",
    }
    assert config.live_trading_gate("live", env) == (True, None)


def test_live_gate_fails_safe_on_an_unexpected_casing() -> None:
    env = {config.ENV_ALLOW_LIVE_TRADING: config.LIVE_TRADING_CONFIRMATION}
    allowed, reason = config.live_trading_gate("LIVE", env)
    assert allowed is False
    assert reason is not None and "missing environment variable" in reason


def test_live_gate_reads_the_process_environment(monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_ALLOW_LIVE_TRADING, config.LIVE_TRADING_CONFIRMATION)
    monkeypatch.setenv(config.ENV_LIVE_EXCHANGE_KEY, "key")
    monkeypatch.setenv(config.ENV_LIVE_EXCHANGE_SECRET, "secret")
    assert config.live_trading_gate("live") == (True, None)


def test_operator_token_defaults_to_an_empty_string() -> None:
    assert config.operator_token({}) == ""


def test_operator_token_reads_the_environment(monkeypatch) -> None:
    assert config.operator_token({config.ENV_OPERATOR_TOKEN: "s3cret"}) == "s3cret"
    monkeypatch.setenv(config.ENV_OPERATOR_TOKEN, "from-process")
    assert config.operator_token() == "from-process"


def test_allow_live_trading_requires_the_acknowledgement() -> None:
    assert config.allow_live_trading({}) is False
    assert config.allow_live_trading({config.ENV_ALLOW_LIVE_TRADING: "true"}) is False
    assert (
        config.allow_live_trading({config.ENV_ALLOW_LIVE_TRADING: config.LIVE_TRADING_CONFIRMATION})
        is True
    )


def test_allow_live_trading_reads_the_process_environment(monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_ALLOW_LIVE_TRADING, config.LIVE_TRADING_CONFIRMATION)
    assert config.allow_live_trading() is True
