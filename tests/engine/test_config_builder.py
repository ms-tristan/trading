"""Tests of the generated Freqtrade configuration and process contract.

The key set of a generated document is pinned exactly: Freqtrade validates its
configuration at startup and refuses to boot when a mandatory key is missing, so
a silent removal here would only surface as a profile that never starts.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from trading_platform.config import PlatformSettings
from trading_platform.engine import config_builder
from trading_platform.engine.config_builder import (
    DEFAULT_THROTTLE_SECONDS,
    GENERATED_CONFIG_MODE,
    TIMEFRAME_THROTTLE_SECONDS,
    allocate_api_port,
    build_freqtrade_argv,
    build_freqtrade_config,
    profile_config_path,
    profile_data_dir,
    profile_db_url,
    profile_log_path,
    profile_runtime_dir,
    resolve_freqtrade_binary,
    throttle_for_timeframe,
    write_freqtrade_config,
)
from trading_platform.models import ProfileRecord

PAPER_CONFIG_KEYS = frozenset(
    {
        "max_open_trades",
        "stake_currency",
        "stake_amount",
        "tradable_balance_ratio",
        "fiat_display_currency",
        "dry_run",
        "dry_run_wallet",
        "trading_mode",
        "margin_mode",
        "timeframe",
        "strategy",
        "unfilledtimeout",
        "entry_pricing",
        "exit_pricing",
        "exchange",
        "pairlists",
        "api_server",
        "initial_state",
        "internals",
        "bot_name",
    }
)

LIVE_CONFIG_KEYS = PAPER_CONFIG_KEYS - {"dry_run_wallet"}

#: Values the builder generates fresh on every call.
GENERATED_API_KEYS = ("jwt_secret_key", "ws_token")


def make_profile(**overrides: Any) -> ProfileRecord:
    """Return a profile record, optionally overriding its documented fields."""
    fields: dict[str, Any] = {
        "id": "alpha",
        "name": "Alpha Momentum",
        "strategy": "AlphaMomentumStrategy",
        "timeframe": "1h",
        "mode": "paper",
        "exchange": "binance",
        "pairs": ["BTC/USDT", "ETH/USDT"],
        "initial_capital": 1000.0,
        "max_open_trades": 2,
    }
    fields.update(overrides)
    return ProfileRecord(**fields)


def make_config(profile: ProfileRecord, **overrides: Any) -> dict[str, Any]:
    """Return the generated configuration of ``profile``."""
    arguments: dict[str, Any] = {
        "strategy_class_name": profile.strategy,
        "api_port": 8101,
        "api_username": profile.id,
        "api_password": "generated-password",
    }
    arguments.update(overrides)
    return build_freqtrade_config(profile, PlatformSettings(), **arguments)


# ---------------------------------------------------------------------------
# Key set and paper/live differences
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mode", "expected_keys"),
    [("paper", PAPER_CONFIG_KEYS), ("live", LIVE_CONFIG_KEYS)],
)
def test_generated_config_has_exactly_the_mandatory_keys(
    mode: str, expected_keys: frozenset[str]
) -> None:
    config = make_config(make_profile(mode=mode))
    assert set(config) == expected_keys


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_generated_config_never_carries_telegram_or_user_data_dir(mode: str) -> None:
    config = make_config(make_profile(mode=mode))
    assert "telegram" not in config
    assert "user_data_dir" not in config


def test_generated_config_is_json_serialisable() -> None:
    config = make_config(make_profile())
    assert json.loads(json.dumps(config))["strategy"] == "AlphaMomentumStrategy"


def test_paper_config_matches_the_documented_shape() -> None:
    profile = make_profile(mode="paper", pairs=["BTC/USDT", "ETH/USDT"], initial_capital=1000.0)
    config = make_config(profile)
    for key in GENERATED_API_KEYS:
        assert config["api_server"].pop(key)
    assert config == {
        "max_open_trades": 2,
        "stake_currency": "USDT",
        "stake_amount": "unlimited",
        "tradable_balance_ratio": 0.99,
        "fiat_display_currency": "",
        "dry_run": True,
        "dry_run_wallet": 1000.0,
        "trading_mode": "spot",
        "margin_mode": "",
        "timeframe": "1h",
        "strategy": "AlphaMomentumStrategy",
        "unfilledtimeout": {
            "entry": 10,
            "exit": 10,
            "exit_timeout_count": 0,
            "unit": "minutes",
        },
        "entry_pricing": {
            "price_side": "same",
            "use_order_book": True,
            "order_book_top": 1,
            "price_last_balance": 0.0,
            "check_depth_of_market": {"enabled": False, "bids_to_ask_delta": 1},
        },
        "exit_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "exchange": {
            "name": "binance",
            "key": "",
            "secret": "",
            "ccxt_config": {"enableRateLimit": True, "timeout": 30000},
            "ccxt_async_config": {"enableRateLimit": True, "timeout": 30000},
            "pair_whitelist": ["BTC/USDT", "ETH/USDT"],
            "pair_blacklist": [],
        },
        "pairlists": [{"method": "StaticPairList"}],
        "api_server": {
            "enabled": True,
            "listen_ip_address": "127.0.0.1",
            "listen_port": 8101,
            "verbosity": "error",
            "enable_openapi": False,
            "CORS_origins": [],
            "username": "alpha",
            "password": "generated-password",
        },
        "initial_state": "running",
        "internals": {"process_throttle_secs": 30},
        "bot_name": "alpha",
    }


def test_generated_config_does_not_trigger_a_fiat_conversion() -> None:
    """No generated worker may ask Freqtrade for a fiat conversion.

    An empty ``fiat_display_currency`` means "no fiat conversion requested", so
    the worker never calls the CoinGecko price API. ``"USD"`` made every
    ``/balance`` refresh that conversion through the anonymous rate limit the
    whole fleet shares, which is what stretched the read to 12-20 s. The key
    itself stays: it is part of the validated key set and only its value is
    neutralised.
    """
    rendered = json.dumps(make_config(make_profile()))

    assert make_config(make_profile())["fiat_display_currency"] == ""
    # The stake currency is USDT, which merely contains the letters: what must
    # be gone is the fiat conversion value itself.
    assert '"USD"' not in rendered


def test_paper_profile_uses_its_initial_capital_as_dry_run_wallet() -> None:
    config = make_config(make_profile(mode="paper", initial_capital=2500.5))
    assert config["dry_run"] is True
    assert config["dry_run_wallet"] == 2500.5
    assert config["stake_amount"] == "unlimited"
    assert config["exchange"]["key"] == ""
    assert config["exchange"]["secret"] == ""


def test_live_profile_has_no_dry_run_wallet_and_stakes_its_capital() -> None:
    config = make_config(
        make_profile(mode="live", initial_capital=1000.0, max_open_trades=4),
        exchange_key="live-key",
        exchange_secret="live-secret",
    )
    assert config["dry_run"] is False
    assert "dry_run_wallet" not in config
    assert config["stake_amount"] == 250.0
    assert config["exchange"]["key"] == "live-key"
    assert config["exchange"]["secret"] == "live-secret"


def test_live_stake_amount_never_divides_by_zero() -> None:
    config = make_config(
        make_profile(mode="live", initial_capital=1000.0, max_open_trades=0),
        exchange_key="live-key",
        exchange_secret="live-secret",
    )
    assert config["stake_amount"] == 1000.0


def test_live_stake_amount_is_rounded_to_eight_decimals() -> None:
    config = make_config(
        make_profile(mode="live", initial_capital=1000.0, max_open_trades=3),
        exchange_key="live-key",
        exchange_secret="live-secret",
    )
    assert config["stake_amount"] == round(1000.0 / 3, 8)


def test_max_open_trades_and_pairs_come_from_the_profile() -> None:
    profile = make_profile(max_open_trades=7, pairs=["SOL/USDT"])
    config = make_config(profile)
    assert config["max_open_trades"] == 7
    assert config["exchange"]["pair_whitelist"] == ["SOL/USDT"]
    # A copy, never the profile's own list.
    assert config["exchange"]["pair_whitelist"] is not profile.pairs


def test_stake_currency_comes_from_the_platform_settings() -> None:
    config = build_freqtrade_config(
        make_profile(),
        PlatformSettings(default_stake_currency="EUR"),
        strategy_class_name="AlphaMomentumStrategy",
        api_port=8101,
        api_username="alpha",
        api_password="generated-password",
    )
    assert config["stake_currency"] == "EUR"


# ---------------------------------------------------------------------------
# API server block
# ---------------------------------------------------------------------------
def test_api_server_is_loopback_only_and_uses_the_profile_credentials() -> None:
    config = make_config(make_profile(), api_port=8123, api_username="alpha", api_password="pw-1")
    api_server = config["api_server"]
    assert api_server["enabled"] is True
    assert api_server["listen_ip_address"] == "127.0.0.1"
    assert api_server["listen_port"] == 8123
    assert api_server["username"] == "alpha"
    assert api_server["password"] == "pw-1"
    assert api_server["CORS_origins"] == []
    assert api_server["enable_openapi"] is False


def test_api_secrets_are_fresh_on_every_call() -> None:
    first = make_config(make_profile())
    second = make_config(make_profile())
    assert first["api_server"]["jwt_secret_key"] != second["api_server"]["jwt_secret_key"]
    assert first["api_server"]["ws_token"] != second["api_server"]["ws_token"]
    assert len(first["api_server"]["jwt_secret_key"]) == 64
    assert len(first["api_server"]["ws_token"]) >= 32


# ---------------------------------------------------------------------------
# Timeframe throttling
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [("5m", 5), ("15m", 15), ("1h", 30), ("4h", 60), ("1d", 60), ("1M", 30), ("3m", 30), ("", 30)],
)
def test_throttle_for_timeframe(timeframe: str, expected: int) -> None:
    assert throttle_for_timeframe(timeframe) == expected


def test_throttle_table_is_the_documented_one() -> None:
    assert TIMEFRAME_THROTTLE_SECONDS == {"5m": 5, "15m": 15, "1h": 30, "4h": 60, "1d": 60}
    assert DEFAULT_THROTTLE_SECONDS == 30


def test_throttle_lookup_ignores_case_and_surrounding_spaces() -> None:
    assert throttle_for_timeframe(" 1H ") == TIMEFRAME_THROTTLE_SECONDS["1h"]


@pytest.mark.parametrize("timeframe", ["5m", "15m", "1h", "4h", "1d", "3m"])
def test_process_throttle_follows_the_profile_timeframe(timeframe: str) -> None:
    config = make_config(make_profile(timeframe=timeframe))
    assert config["timeframe"] == timeframe
    assert config["internals"] == {"process_throttle_secs": throttle_for_timeframe(timeframe)}


# ---------------------------------------------------------------------------
# Freqtrade's own validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["paper", "live"])
def test_freqtrade_accepts_the_generated_config(mode: str) -> None:
    validation = pytest.importorskip("freqtrade.configuration.config_validation")
    profile = make_profile(mode=mode)
    config = make_config(
        profile,
        exchange_key="live-key" if mode == "live" else "",
        exchange_secret="live-secret" if mode == "live" else "",
    )
    validated = validation.validate_config_schema(copy.deepcopy(config))
    assert validated["strategy"] == profile.strategy
    assert validated["dry_run"] is (mode == "paper")


# ---------------------------------------------------------------------------
# Writing the document
# ---------------------------------------------------------------------------
def test_write_freqtrade_config_creates_parents_and_returns_the_target(tmp_path: Path) -> None:
    target = tmp_path / "profiles" / "alpha" / "config.json"
    config = make_config(make_profile())
    written = write_freqtrade_config(config, target)
    assert written == target
    assert json.loads(target.read_text(encoding="utf-8")) == config


def test_write_freqtrade_config_is_owner_readable_only(tmp_path: Path) -> None:
    target = tmp_path / "profiles" / "alpha" / "config.json"
    write_freqtrade_config(make_config(make_profile()), target)
    assert os.stat(target).st_mode & 0o777 == GENERATED_CONFIG_MODE
    assert GENERATED_CONFIG_MODE == 0o600


def test_write_freqtrade_config_restricts_mode_of_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, 0o644)
    write_freqtrade_config(make_config(make_profile()), target)
    assert os.stat(target).st_mode & 0o777 == GENERATED_CONFIG_MODE


# ---------------------------------------------------------------------------
# Per-profile runtime layout
# ---------------------------------------------------------------------------
def test_profile_runtime_layout(tmp_state_dir: Path) -> None:
    assert profile_runtime_dir(tmp_state_dir, "alpha") == tmp_state_dir / "profiles" / "alpha"
    assert (
        profile_config_path(tmp_state_dir, "alpha")
        == tmp_state_dir / "profiles" / "alpha" / "config.json"
    )
    assert profile_db_url(tmp_state_dir, "alpha") == (
        f"sqlite:///{tmp_state_dir / 'profiles' / 'alpha' / 'tradesv3.sqlite'}"
    )
    assert profile_log_path(tmp_state_dir, "alpha") == tmp_state_dir / "logs" / "alpha.log"
    assert profile_data_dir(tmp_state_dir, "alpha") == (
        tmp_state_dir / "profiles" / "alpha" / "data"
    )


def test_profile_paths_accept_a_string_state_dir() -> None:
    assert profile_runtime_dir("/tmp/state", "alpha") == Path("/tmp/state/profiles/alpha")


def test_allocate_api_port_offsets_the_configured_base() -> None:
    settings = PlatformSettings(profile_api_port_base=8101)
    assert allocate_api_port(0, settings) == 8101
    assert allocate_api_port(3, settings) == 8104
    assert allocate_api_port(3, PlatformSettings(profile_api_port_base=9000)) == 9003


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------
def test_resolve_freqtrade_binary_uses_the_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_builder.shutil, "which", lambda name: f"/opt/bin/{name}")
    assert resolve_freqtrade_binary({"TB_FREQTRADE_BIN": "/opt/freqtrade/freqtrade"}) == [
        "/opt/freqtrade/freqtrade"
    ]


def test_resolve_freqtrade_binary_defaults_to_the_console_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_which(name: str) -> str:
        seen.append(name)
        return f"/usr/local/bin/{name}"

    monkeypatch.setattr(config_builder.shutil, "which", fake_which)
    assert resolve_freqtrade_binary({}) == ["freqtrade"]
    assert seen == ["freqtrade"]


def test_resolve_freqtrade_binary_ignores_a_blank_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_builder.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert resolve_freqtrade_binary({"TB_FREQTRADE_BIN": "   "}) == ["freqtrade"]


def test_resolve_freqtrade_binary_falls_back_to_the_running_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_builder.shutil, "which", lambda name: None)
    assert resolve_freqtrade_binary({"TB_FREQTRADE_BIN": "freqtrade"}) == [
        sys.executable,
        "-m",
        "freqtrade",
    ]


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def test_build_freqtrade_argv_matches_the_executed_command_line() -> None:
    profile = make_profile()
    argv = build_freqtrade_argv(
        profile,
        config_path=Path("/state/profiles/alpha/config.json"),
        user_data_dir=Path("/state/profiles/alpha"),
        db_url="sqlite:////state/profiles/alpha/tradesv3.sqlite",
        logfile=Path("/state/logs/alpha.log"),
        strategy_path=Path("/app/user_data/strategies"),
        binary=["freqtrade"],
    )
    assert argv == [
        "freqtrade",
        "trade",
        "--config",
        "/state/profiles/alpha/config.json",
        "--userdir",
        "/state/profiles/alpha",
        "--db-url",
        "sqlite:////state/profiles/alpha/tradesv3.sqlite",
        "--logfile",
        "/state/logs/alpha.log",
        "--strategy-path",
        "/app/user_data/strategies",
    ]


def test_build_freqtrade_argv_omits_an_absent_strategy_path() -> None:
    argv = build_freqtrade_argv(
        make_profile(),
        config_path=Path("/state/profiles/alpha/config.json"),
        user_data_dir=Path("/state/profiles/alpha"),
        db_url="sqlite:////state/profiles/alpha/tradesv3.sqlite",
        logfile=Path("/state/logs/alpha.log"),
        strategy_path=None,
        binary=["freqtrade"],
    )
    assert "--strategy-path" not in argv
    assert argv[:2] == ["freqtrade", "trade"]


def test_build_freqtrade_argv_resolves_the_binary_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_builder,
        "resolve_freqtrade_binary",
        lambda env=None: [sys.executable, "-m", "freqtrade"],
    )
    argv = build_freqtrade_argv(
        make_profile(),
        config_path=Path("/state/config.json"),
        user_data_dir=Path("/state"),
        db_url="sqlite:////state/tradesv3.sqlite",
        logfile=Path("/state/alpha.log"),
        strategy_path=None,
    )
    assert argv[:4] == [sys.executable, "-m", "freqtrade", "trade"]
