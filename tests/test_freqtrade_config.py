"""Freqtrade configuration helpers and committed configuration files (wp-5).

Everything here is offline: the helper module is exercised in memory and the
committed files under ``config/`` are read from disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_platform.config import AppConfig, load_config
from trading_platform.core.constants import SUPPORTED_TIMEFRAMES
from trading_platform.core.errors import FreqtradeConfigError
from trading_platform.freqtrade import (
    FREQTRADE_REQUIRED_KEYS,
    base_freqtrade_config,
    load_freqtrade_config,
    validate_freqtrade_config,
    write_freqtrade_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"

#: Committed Freqtrade configurations and the ``dry_run`` value they must carry.
COMMITTED_FREQTRADE_CONFIGS = {
    "config/freqtrade_config.json": False,
    "config/freqtrade_dryrun.json": True,
}

#: Credential-ish keys that must never hold a value in a committed file.
SECRET_PATHS = (
    ("exchange", "key"),
    ("exchange", "secret"),
    ("telegram", "token"),
    ("telegram", "chat_id"),
    ("api_server", "jwt_secret_key"),
    ("api_server", "password"),
    ("api_server", "username"),
)


def invalid_cases() -> list[tuple[str, str]]:
    """Return ``(case, fragment)`` pairs for every documented invalidity."""
    return [
        ("missing_key", "missing required key"),
        ("max_open_trades_zero", "max_open_trades"),
        ("max_open_trades_bool", "max_open_trades"),
        ("stake_currency_empty", "stake_currency"),
        ("dry_run_not_bool", "dry_run"),
        ("pair_whitelist_empty", "pair_whitelist"),
        ("timeframe_unknown", "timeframe"),
        ("exchange_name_empty", "exchange.name"),
        ("exchange_not_object", "exchange must be a JSON object"),
    ]


def break_config(payload: dict, case: str) -> dict:
    """Apply one invalidity to ``payload`` and return it."""
    if case == "missing_key":
        del payload["stake_currency"]
    elif case == "max_open_trades_zero":
        payload["max_open_trades"] = 0
    elif case == "max_open_trades_bool":
        payload["max_open_trades"] = True
    elif case == "stake_currency_empty":
        payload["stake_currency"] = "  "
    elif case == "dry_run_not_bool":
        payload["dry_run"] = "yes"
    elif case == "pair_whitelist_empty":
        payload["exchange"]["pair_whitelist"] = []
    elif case == "timeframe_unknown":
        payload["timeframe"] = "3h"
    elif case == "exchange_name_empty":
        payload["exchange"]["name"] = ""
    elif case == "exchange_not_object":
        payload["exchange"] = "binance"
    else:  # pragma: no cover - guard against a typo in invalid_cases()
        raise AssertionError(f"unknown invalidity case: {case}")
    return payload


# ---------------------------------------------------------------------------
# base_freqtrade_config
# ---------------------------------------------------------------------------


def test_base_config_is_valid_and_carries_every_required_key() -> None:
    payload = base_freqtrade_config()

    missing = [key for key in FREQTRADE_REQUIRED_KEYS if key not in payload]
    assert not missing
    assert validate_freqtrade_config(payload) == []


def test_base_config_values_are_the_documented_defaults() -> None:
    payload = base_freqtrade_config()

    assert payload["max_open_trades"] == 1
    assert payload["stake_currency"] == "USDT"
    assert payload["stake_amount"] == 100.0
    assert payload["tradable_balance_ratio"] == 0.99
    assert payload["fiat_display_currency"] == "USD"
    assert payload["dry_run"] is False
    assert payload["dry_run_wallet"] > 0
    assert payload["timeframe"] == "1h"
    assert payload["strategy"] == "BasicStrategy"
    assert payload["exchange"] == {
        "name": "binance",
        "key": "",
        "secret": "",
        "pair_whitelist": ["BTC/USDT"],
        "pair_blacklist": [],
    }
    assert payload["pairlists"] == [{"method": "StaticPairList"}]
    assert payload["telegram"]["enabled"] is False
    assert payload["api_server"]["enabled"] is False
    assert "unfilledtimeout" in payload
    assert "entry_pricing" in payload
    assert "exit_pricing" in payload


def test_base_config_honours_every_keyword_argument() -> None:
    payload = base_freqtrade_config(
        exchange="kraken",
        stake_currency="EUR",
        stake_amount=250.0,
        timeframe="4h",
        pairs=("ETH/EUR", "BTC/EUR"),
        dry_run=True,
        max_open_trades=3,
        strategy="MyStrategy",
    )

    assert payload["exchange"]["name"] == "kraken"
    assert payload["exchange"]["pair_whitelist"] == ["ETH/EUR", "BTC/EUR"]
    assert payload["stake_currency"] == "EUR"
    assert payload["stake_amount"] == 250.0
    assert payload["timeframe"] == "4h"
    assert payload["dry_run"] is True
    assert payload["max_open_trades"] == 3
    assert payload["strategy"] == "MyStrategy"
    assert validate_freqtrade_config(payload) == []


def test_base_config_accepts_every_supported_timeframe() -> None:
    for timeframe in sorted(SUPPORTED_TIMEFRAMES):
        payload = base_freqtrade_config(timeframe=timeframe)

        assert validate_freqtrade_config(payload) == []


def test_base_config_never_contains_a_secret_value() -> None:
    payload = base_freqtrade_config()

    for section, key in SECRET_PATHS:
        assert payload[section][key] == "", f"{section}.{key} must stay empty"


# ---------------------------------------------------------------------------
# validate_freqtrade_config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("case", "fragment"), invalid_cases())
def test_validate_reports_each_invalidity(case: str, fragment: str) -> None:
    issues = validate_freqtrade_config(break_config(base_freqtrade_config(), case))

    assert issues, f"{case} produced no issue"
    assert any(fragment in issue for issue in issues), issues


def test_validate_ignores_extra_keys_and_accepts_an_untouched_config() -> None:
    payload = base_freqtrade_config()
    payload["extra_key"] = {"anything": True}
    payload["pairlists"] = [{"method": "VolumePairList"}, {"method": "StaticPairList"}]

    assert validate_freqtrade_config(payload) == []


def test_validate_rejects_a_whitelist_with_an_empty_pair() -> None:
    payload = base_freqtrade_config()
    payload["exchange"]["pair_whitelist"] = ["BTC/USDT", "  "]

    issues = validate_freqtrade_config(payload)

    assert issues == ["exchange.pair_whitelist entries must be non-empty strings"]


def test_validate_reports_every_missing_key_at_once() -> None:
    issues = validate_freqtrade_config({})

    assert len(issues) == len(FREQTRADE_REQUIRED_KEYS)
    assert all(issue.startswith("missing required key:") for issue in issues)


def test_validate_rejects_a_non_mapping_payload() -> None:
    issues = validate_freqtrade_config(["not", "a", "mapping"])

    assert issues == ["freqtrade configuration must be a JSON object, got list"]


# ---------------------------------------------------------------------------
# write_freqtrade_config / load_freqtrade_config
# ---------------------------------------------------------------------------


def test_write_and_load_round_trip(tmp_path: Path) -> None:
    payload = base_freqtrade_config(dry_run=True, pairs=("ETH/USDT",))
    target = tmp_path / "freqtrade.json"

    written = write_freqtrade_config(payload, target)

    assert written == target
    assert target.is_file()
    assert load_freqtrade_config(target) == payload
    # deterministic, sorted, two-space indented document
    assert (
        target.read_text(encoding="utf-8") == json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def test_write_creates_the_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "freqtrade.json"

    write_freqtrade_config(base_freqtrade_config(), target)

    assert target.is_file()


def test_write_rejects_a_non_serialisable_payload(tmp_path: Path) -> None:
    with pytest.raises(FreqtradeConfigError):
        write_freqtrade_config({"max_open_trades": object()}, tmp_path / "config.json")


def test_write_rejects_an_unwritable_destination(tmp_path: Path) -> None:
    with pytest.raises(FreqtradeConfigError) as error:
        write_freqtrade_config(base_freqtrade_config(), tmp_path)

    assert "cannot write freqtrade configuration" in str(error.value)


def test_load_reports_an_unreadable_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")

    def unreadable(*args: object, **kwargs: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", unreadable)

    with pytest.raises(FreqtradeConfigError) as error:
        load_freqtrade_config(path)

    assert "cannot read freqtrade configuration" in str(error.value)


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FreqtradeConfigError) as error:
        load_freqtrade_config(tmp_path / "missing.json")

    assert "not found" in str(error.value)


def test_load_corrupt_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.json"
    path.write_text("{not json,,}", encoding="utf-8")

    with pytest.raises(FreqtradeConfigError) as error:
        load_freqtrade_config(path)

    assert "invalid JSON" in str(error.value)


def test_load_rejects_a_json_document_that_is_not_an_object(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(FreqtradeConfigError) as error:
        load_freqtrade_config(path)

    assert "must contain a JSON object" in str(error.value)


# ---------------------------------------------------------------------------
# committed configuration files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("relative_path", "dry_run"), sorted(COMMITTED_FREQTRADE_CONFIGS.items()))
def test_committed_freqtrade_config(relative_path: str, dry_run: bool) -> None:
    path = REPO_ROOT / relative_path
    payload = load_freqtrade_config(path)

    assert payload["dry_run"] is dry_run
    for key in FREQTRADE_REQUIRED_KEYS:
        assert key in payload, f"{relative_path} is missing {key!r}"
    assert validate_freqtrade_config(payload) == []
    assert payload["timeframe"] in SUPPORTED_TIMEFRAMES
    for section, key in SECRET_PATHS:
        assert payload[section][key] == "", f"{relative_path}: {section}.{key} is not empty"


def test_backtest_default_config_loads_as_an_appconfig() -> None:
    cfg = load_config(REPO_ROOT / "config" / "backtest_default.json")

    assert isinstance(cfg, AppConfig)
    assert cfg.project_name == "trading-platform"
    assert cfg.data.allow_network is True
    assert cfg.data.timeframe == "1h"
    assert cfg.reporting.formats == ["markdown", "json"]
