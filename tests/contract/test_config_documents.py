"""Contract test: the three declarative documents of ``config/``.

The configuration documents are the frozen interface between the engine, the
dashboard and the operator: this module pins their keys, their documented
defaults and the exact profile catalogue the specification froze. It reads the
JSON files only -- no import of ``trading_platform``, no process, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: ``config/platform.json``: the ten documented keys and their documented defaults.
PLATFORM_DEFAULTS: dict[str, object] = {
    "max_running_profiles": 12,
    "snapshot_interval_seconds": 60,
    "profile_api_port_base": 8101,
    "default_exchange": "binance",
    "default_timeframe": "1h",
    "default_stake_currency": "USDT",
    "default_initial_capital": 1000.0,
    "default_max_open_trades": 2,
    "dashboard_refresh_seconds": 15,
    "equity_retention_days": 90,
}

#: ``config/strategies.json``: id, class name, file name.
STRATEGY_ENTRIES: tuple[tuple[str, str, str], ...] = (
    ("basic", "BasicStrategy", "BasicStrategy.py"),
    ("momentum", "MomentumStrategy", "MomentumStrategy.py"),
    ("rsi-reversion", "RsiReversionStrategy", "RsiReversionStrategy.py"),
    ("bollinger", "BollingerStrategy", "BollingerStrategy.py"),
    ("macd", "MacdStrategy", "MacdStrategy.py"),
    ("donchian", "DonchianStrategy", "DonchianStrategy.py"),
    ("keltner", "KeltnerStrategy", "KeltnerStrategy.py"),
    ("supertrend", "SupertrendStrategy", "SupertrendStrategy.py"),
    ("dual-thrust", "DualThrustStrategy", "DualThrustStrategy.py"),
    ("faber", "FaberStrategy", "FaberStrategy.py"),
)

#: The only categories the strategy catalogue may use.
VALID_CATEGORIES = frozenset({"baseline", "trend", "mean-reversion", "breakout", "allocation"})

#: ``config/profiles.json``: id, strategy, pair, timeframe, priority, mode.
PROFILE_ENTRIES: tuple[tuple[str, str, str, str, int, str], ...] = (
    ("basic-btc-1h", "basic", "BTC/USDT", "1h", 100, "paper"),
    ("momentum-btc-1h", "momentum", "BTC/USDT", "1h", 100, "paper"),
    ("rsi-reversion-btc-15m", "rsi-reversion", "BTC/USDT", "15m", 100, "paper"),
    ("bollinger-eth-1h", "bollinger", "ETH/USDT", "1h", 100, "paper"),
    ("macd-btc-4h", "macd", "BTC/USDT", "4h", 100, "paper"),
    ("donchian-eth-4h", "donchian", "ETH/USDT", "4h", 100, "paper"),
    ("keltner-sol-1h", "keltner", "SOL/USDT", "1h", 100, "paper"),
    ("supertrend-btc-1h", "supertrend", "BTC/USDT", "1h", 100, "paper"),
    ("dual-thrust-eth-15m", "dual-thrust", "ETH/USDT", "15m", 100, "paper"),
    ("faber-btc-1d", "faber", "BTC/USDT", "1d", 100, "paper"),
    ("basic-eth-4h", "basic", "ETH/USDT", "4h", 50, "paper"),
    ("momentum-sol-15m", "momentum", "SOL/USDT", "15m", 50, "paper"),
    ("rsi-reversion-ada-1h", "rsi-reversion", "ADA/USDT", "1h", 50, "paper"),
    ("bollinger-xrp-5m", "bollinger", "XRP/USDT", "5m", 50, "paper"),
    ("macd-link-1h", "macd", "LINK/USDT", "1h", 50, "paper"),
    ("donchian-doge-1h", "donchian", "DOGE/USDT", "1h", 50, "paper"),
    ("keltner-bnb-15m", "keltner", "BNB/USDT", "15m", 50, "paper"),
    ("supertrend-avax-4h", "supertrend", "AVAX/USDT", "4h", 50, "paper"),
    ("dual-thrust-dot-1h", "dual-thrust", "DOT/USDT", "1h", 50, "paper"),
    ("faber-eth-1d", "faber", "ETH/USDT", "1d", 50, "paper"),
    ("momentum-eth-4h-live", "momentum", "ETH/USDT", "4h", 100, "live"),
    ("faber-btc-1d-live", "faber", "BTC/USDT", "1d", 100, "live"),
)

#: The only two profiles the specification allows to run against a live account.
LIVE_PROFILE_IDS = frozenset({"momentum-eth-4h-live", "faber-btc-1d-live"})

PAPER_CAPITAL = 1000.0
LIVE_CAPITAL = 250.0
MAX_OPEN_TRADES = 2
EXCHANGE = "binance"

PROFILE_IDS = tuple(entry[0] for entry in PROFILE_ENTRIES)
STRATEGY_IDS = tuple(entry[0] for entry in STRATEGY_ENTRIES)


def _document(name: str) -> dict:
    """Load one ``config/`` document as decoded JSON."""
    return json.loads((REPO_ROOT / "config" / name).read_text(encoding="utf-8"))


def _strategies_by_id() -> dict[str, dict]:
    entries = _document("strategies.json")["strategies"]
    return {entry["id"]: entry for entry in entries}


def _profiles_by_id() -> dict[str, dict]:
    entries = _document("profiles.json")["profiles"]
    return {entry["id"]: entry for entry in entries}


# ---------------------------------------------------------------------------
# config/platform.json
# ---------------------------------------------------------------------------
def test_platform_document_carries_the_ten_documented_keys() -> None:
    document = _document("platform.json")
    assert set(document) == set(PLATFORM_DEFAULTS), (
        "config/platform.json must carry exactly the ten documented keys; "
        f"unexpected: {sorted(set(document) - set(PLATFORM_DEFAULTS))}, "
        f"missing: {sorted(set(PLATFORM_DEFAULTS) - set(document))}"
    )


@pytest.mark.parametrize("key", sorted(PLATFORM_DEFAULTS))
def test_platform_key_keeps_its_documented_default(key: str) -> None:
    document = _document("platform.json")
    expected = PLATFORM_DEFAULTS[key]
    value = document[key]
    assert value == expected and isinstance(value, type(expected)), (
        f"config/platform.json: {key} must default to {expected!r} "
        f"({type(expected).__name__}), found {value!r} ({type(value).__name__})"
    )


# ---------------------------------------------------------------------------
# config/strategies.json
# ---------------------------------------------------------------------------
def test_strategy_catalogue_has_exactly_ten_entries() -> None:
    entries = _document("strategies.json")["strategies"]
    assert len(entries) == 10, f"config/strategies.json must hold ten entries, found {len(entries)}"
    ids = [entry["id"] for entry in entries]
    assert sorted(ids) == sorted(STRATEGY_IDS), (
        f"config/strategies.json ids drifted: {sorted(ids)} != {sorted(STRATEGY_IDS)}"
    )
    assert len(set(ids)) == len(ids), f"config/strategies.json repeats an id: {ids}"


@pytest.mark.parametrize(("strategy_id", "class_name", "file_name"), STRATEGY_ENTRIES)
def test_strategy_entry_keeps_its_documented_triple(
    strategy_id: str, class_name: str, file_name: str
) -> None:
    entry = _strategies_by_id()[strategy_id]
    assert entry["class_name"] == class_name, (
        f"config/strategies.json: {strategy_id}.class_name must be {class_name!r}"
    )
    assert entry["file"] == file_name, (
        f"config/strategies.json: {strategy_id}.file must be {file_name!r}"
    )


def test_every_strategy_entry_declares_a_valid_category() -> None:
    for entry in _document("strategies.json")["strategies"]:
        assert entry["category"] in VALID_CATEGORIES, (
            f"config/strategies.json: {entry['id']} carries the unknown category "
            f"{entry['category']!r}; allowed: {sorted(VALID_CATEGORIES)}"
        )


# ---------------------------------------------------------------------------
# config/profiles.json
# ---------------------------------------------------------------------------
def test_profile_catalogue_has_exactly_22_entries() -> None:
    entries = _document("profiles.json")["profiles"]
    assert len(entries) == 22, f"config/profiles.json must hold 22 entries, found {len(entries)}"
    ids = [entry["id"] for entry in entries]
    assert sorted(ids) == sorted(PROFILE_IDS), (
        f"config/profiles.json ids drifted: {sorted(ids)} != {sorted(PROFILE_IDS)}"
    )
    assert len(set(ids)) == len(ids), f"config/profiles.json repeats an id: {ids}"


@pytest.mark.parametrize(
    ("profile_id", "strategy_id", "pair", "timeframe", "priority", "mode"), PROFILE_ENTRIES
)
def test_profile_keeps_its_documented_contract(
    profile_id: str,
    strategy_id: str,
    pair: str,
    timeframe: str,
    priority: int,
    mode: str,
) -> None:
    profile = _profiles_by_id()[profile_id]

    assert profile["strategy"] == strategy_id, (
        f"config/profiles.json: {profile_id}.strategy must be {strategy_id!r}"
    )
    assert profile["pairs"] == [pair], (
        f"config/profiles.json: {profile_id}.pairs must be exactly [{pair!r}]"
    )
    assert profile["timeframe"] == timeframe, (
        f"config/profiles.json: {profile_id}.timeframe must be {timeframe!r}"
    )
    assert profile["mode"] == mode, f"config/profiles.json: {profile_id}.mode must be {mode!r}"
    assert profile["priority"] == priority, (
        f"config/profiles.json: {profile_id}.priority must be {priority}"
    )
    assert profile["max_open_trades"] == MAX_OPEN_TRADES, (
        f"config/profiles.json: {profile_id}.max_open_trades must be {MAX_OPEN_TRADES}"
    )
    assert profile["enabled"] is True, f"config/profiles.json: {profile_id} must be enabled"
    assert profile["exchange"] == EXCHANGE, (
        f"config/profiles.json: {profile_id}.exchange must be {EXCHANGE!r}"
    )

    expected_capital = LIVE_CAPITAL if mode == "live" else PAPER_CAPITAL
    assert profile["initial_capital"] == expected_capital, (
        f"config/profiles.json: {profile_id} is a {mode} profile, so its initial_capital must be "
        f"{expected_capital}"
    )


def test_exactly_two_profiles_are_live_and_they_are_the_documented_ones() -> None:
    live = {
        entry["id"] for entry in _document("profiles.json")["profiles"] if entry["mode"] == "live"
    }
    assert live == LIVE_PROFILE_IDS, (
        f"exactly two live profiles are allowed, {sorted(LIVE_PROFILE_IDS)}; found {sorted(live)}"
    )


def test_every_profile_runs_a_strategy_of_the_catalogue() -> None:
    known = set(_strategies_by_id())
    unknown = sorted(
        {entry["strategy"] for entry in _document("profiles.json")["profiles"]} - known
    )
    assert not unknown, (
        f"config/profiles.json references strategies absent from config/strategies.json: {unknown}"
    )


def test_every_strategy_carries_at_least_two_profiles() -> None:
    usage: dict[str, int] = dict.fromkeys(STRATEGY_IDS, 0)
    for entry in _document("profiles.json")["profiles"]:
        usage[entry["strategy"]] = usage.get(entry["strategy"], 0) + 1

    under_used = sorted(name for name, count in usage.items() if count < 2)
    assert not under_used, (
        f"every strategy of the catalogue must be used by at least two profiles; {under_used} "
        f"are used {[usage[name] for name in under_used]} time(s) -- counts: {usage}"
    )
