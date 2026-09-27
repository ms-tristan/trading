"""Tests for the catalogue documents: ``strategies.json`` and ``profiles.json``."""

from __future__ import annotations

import json
from pathlib import Path

from trading_platform import models

STRATEGY_FIELDS = (
    "id",
    "class_name",
    "file",
    "category",
    "title",
    "summary",
    "description",
    "indicators",
    "timeframes",
    "reference",
    "risk_notes",
)

PROFILE_FIELDS = (
    "id",
    "name",
    "strategy",
    "timeframe",
    "mode",
    "exchange",
    "pairs",
    "initial_capital",
    "max_open_trades",
    "priority",
    "enabled",
)

#: (id, class_name, file, category) of the ten catalogue strategies, in order.
EXPECTED_STRATEGIES = (
    ("basic", "BasicStrategy", "BasicStrategy.py", "baseline"),
    ("momentum", "MomentumStrategy", "MomentumStrategy.py", "trend"),
    ("rsi-reversion", "RsiReversionStrategy", "RsiReversionStrategy.py", "mean-reversion"),
    ("bollinger", "BollingerStrategy", "BollingerStrategy.py", "mean-reversion"),
    ("macd", "MacdStrategy", "MacdStrategy.py", "trend"),
    ("donchian", "DonchianStrategy", "DonchianStrategy.py", "breakout"),
    ("keltner", "KeltnerStrategy", "KeltnerStrategy.py", "breakout"),
    ("supertrend", "SupertrendStrategy", "SupertrendStrategy.py", "trend"),
    ("dual-thrust", "DualThrustStrategy", "DualThrustStrategy.py", "breakout"),
    ("faber", "FaberStrategy", "FaberStrategy.py", "allocation"),
)

#: (id, strategy, pair, timeframe) of the ten priority-100 paper profiles, in order.
EXPECTED_PAPER_HIGH = (
    ("basic-btc-1h", "basic", "BTC/USDT", "1h"),
    ("momentum-btc-1h", "momentum", "BTC/USDT", "1h"),
    ("rsi-reversion-btc-15m", "rsi-reversion", "BTC/USDT", "15m"),
    ("bollinger-eth-1h", "bollinger", "ETH/USDT", "1h"),
    ("macd-btc-4h", "macd", "BTC/USDT", "4h"),
    ("donchian-eth-4h", "donchian", "ETH/USDT", "4h"),
    ("keltner-sol-1h", "keltner", "SOL/USDT", "1h"),
    ("supertrend-btc-1h", "supertrend", "BTC/USDT", "1h"),
    ("dual-thrust-eth-15m", "dual-thrust", "ETH/USDT", "15m"),
    ("faber-btc-1d", "faber", "BTC/USDT", "1d"),
)

#: (id, strategy, pair, timeframe) of the ten priority-50 paper profiles, in order.
EXPECTED_PAPER_LOW = (
    ("basic-eth-4h", "basic", "ETH/USDT", "4h"),
    ("momentum-sol-15m", "momentum", "SOL/USDT", "15m"),
    ("rsi-reversion-ada-1h", "rsi-reversion", "ADA/USDT", "1h"),
    ("bollinger-xrp-5m", "bollinger", "XRP/USDT", "5m"),
    ("macd-link-1h", "macd", "LINK/USDT", "1h"),
    ("donchian-doge-1h", "donchian", "DOGE/USDT", "1h"),
    ("keltner-bnb-15m", "keltner", "BNB/USDT", "15m"),
    ("supertrend-avax-4h", "supertrend", "AVAX/USDT", "4h"),
    ("dual-thrust-dot-1h", "dual-thrust", "DOT/USDT", "1h"),
    ("faber-eth-1d", "faber", "ETH/USDT", "1d"),
)

#: (id, strategy, pair, timeframe) of the two live profiles, in order.
EXPECTED_LIVE = (
    ("momentum-eth-4h-live", "momentum", "ETH/USDT", "4h"),
    ("faber-btc-1d-live", "faber", "BTC/USDT", "1d"),
)


def read_document(repo_root: Path, name: str) -> dict:
    return json.loads((repo_root / "config" / name).read_text(encoding="utf-8"))


def build_profiles(repo_root: Path) -> list[models.ProfileConfig]:
    document = read_document(repo_root, "profiles.json")
    return [models.ProfileConfig.model_validate(entry) for entry in document["profiles"]]


def build_strategies(repo_root: Path) -> list[models.StrategyMeta]:
    document = read_document(repo_root, "strategies.json")
    return [models.StrategyMeta.model_validate(entry) for entry in document["strategies"]]


# ---------------------------------------------------------------------------
# strategies.json
# ---------------------------------------------------------------------------
def test_catalogue_lists_the_ten_documented_strategies(repo_root: Path) -> None:
    document = read_document(repo_root, "strategies.json")
    assert set(document) == {"strategies"}
    entries = document["strategies"]
    assert len(entries) == 10
    assert [
        (entry["id"], entry["class_name"], entry["file"], entry["category"]) for entry in entries
    ] == list(EXPECTED_STRATEGIES)


def test_every_strategy_entry_is_complete_and_loads(repo_root: Path) -> None:
    document = read_document(repo_root, "strategies.json")
    strategies = build_strategies(repo_root)
    assert len(strategies) == 10
    for entry, meta in zip(document["strategies"], strategies, strict=True):
        assert set(entry) == set(STRATEGY_FIELDS)
        assert meta.class_name.endswith("Strategy")
        assert meta.file == f"{meta.class_name}.py"
        assert meta.category in models.STRATEGY_CATEGORIES
        assert meta.title and "\n" not in meta.title
        assert meta.summary.endswith(".")
        assert "\n" not in meta.summary
        assert meta.description.count(". ") >= 1
        assert len(meta.description) > len(meta.summary)
        assert meta.indicators
        assert meta.timeframes
        assert set(meta.timeframes) <= set(models.SUPPORTED_TIMEFRAMES)
        assert meta.reference
        assert meta.risk_notes


def test_strategy_identifiers_are_unique(repo_root: Path) -> None:
    strategies = build_strategies(repo_root)
    assert len({meta.id for meta in strategies}) == len(strategies)
    assert len({meta.class_name for meta in strategies}) == len(strategies)
    assert len({meta.file for meta in strategies}) == len(strategies)


def test_metadata_is_not_the_platform_default(repo_root: Path) -> None:
    for meta in build_strategies(repo_root):
        assert meta.summary != ""
        assert meta.description != ""
        assert meta.reference != ""
        assert meta.risk_notes != ""


# ---------------------------------------------------------------------------
# profiles.json
# ---------------------------------------------------------------------------
def test_catalogue_lists_the_twenty_two_documented_profiles(repo_root: Path) -> None:
    document = read_document(repo_root, "profiles.json")
    assert set(document) == {"profiles"}
    assert len(document["profiles"]) == 22
    assert len(build_profiles(repo_root)) == 22


def test_every_profile_entry_carries_every_field(repo_root: Path) -> None:
    document = read_document(repo_root, "profiles.json")
    for entry in document["profiles"]:
        assert set(entry) == set(PROFILE_FIELDS)


def test_profile_identifiers_and_pairs(repo_root: Path) -> None:
    profiles = build_profiles(repo_root)
    assert len({profile.id for profile in profiles}) == len(profiles)
    for profile in profiles:
        assert profile.enabled is True
        assert profile.max_open_trades == 2
        assert profile.exchange == "binance"
        assert profile.pairs
        assert all(pair.endswith("/USDT") and pair.count("/") == 1 for pair in profile.pairs)
        assert profile.timeframe in models.SUPPORTED_TIMEFRAMES
        assert profile.name


def test_paper_profiles_are_split_by_priority(repo_root: Path) -> None:
    profiles = build_profiles(repo_root)
    high = [profile for profile in profiles if profile.mode == "paper" and profile.priority == 100]
    low = [profile for profile in profiles if profile.mode == "paper" and profile.priority == 50]
    assert [
        (profile.id, profile.strategy, profile.pairs[0], profile.timeframe) for profile in high
    ] == list(EXPECTED_PAPER_HIGH)
    assert [
        (profile.id, profile.strategy, profile.pairs[0], profile.timeframe) for profile in low
    ] == list(EXPECTED_PAPER_LOW)
    for profile in high + low:
        assert profile.initial_capital == 1000.0


def test_only_two_profiles_trade_live(repo_root: Path) -> None:
    profiles = build_profiles(repo_root)
    live = [profile for profile in profiles if profile.mode == "live"]
    assert [
        (profile.id, profile.strategy, profile.pairs[0], profile.timeframe) for profile in live
    ] == list(EXPECTED_LIVE)
    for profile in live:
        assert profile.priority == 100
        assert profile.initial_capital == 250.0
    assert len(profiles) - len(live) == 20


def test_no_profile_uses_an_unknown_mode_priority_or_capital(repo_root: Path) -> None:
    for profile in build_profiles(repo_root):
        assert profile.mode in models.PROFILE_MODES
        assert profile.priority in (50, 100)
        assert profile.initial_capital in (250.0, 1000.0)


def test_every_profile_references_a_catalogue_strategy(repo_root: Path) -> None:
    strategies = {meta.id: meta for meta in build_strategies(repo_root)}
    profiles = build_profiles(repo_root)
    for profile in profiles:
        assert profile.strategy in strategies
        assert profile.timeframe in strategies[profile.strategy].timeframes


def test_every_strategy_has_at_least_two_profiles(repo_root: Path) -> None:
    strategies = build_strategies(repo_root)
    profiles = build_profiles(repo_root)
    for meta in strategies:
        matching = [profile for profile in profiles if profile.strategy == meta.id]
        assert len(matching) >= 2, f"{meta.id} has {len(matching)} profile(s)"


def test_profiles_load_into_full_records(repo_root: Path) -> None:
    for profile in build_profiles(repo_root):
        record = models.ProfileRecord(**profile.model_dump())
        assert record.state == models.STATE_STOPPED
        assert record.source == "catalogue"
        assert record.api_port == 0
        assert record.pid is None
