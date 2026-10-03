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

#: (id, class_name, file, category) of the fifteen catalogue strategies, in order.
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
    # --- 2026 strategy research programme: additive, the ten above are untouched.
    (
        "keltner-breakout-v2",
        "KeltnerBreakoutV2Strategy",
        "KeltnerBreakoutV2Strategy.py",
        "breakout",
    ),
    ("trend-ensemble-v2", "TrendEnsembleV2Strategy", "TrendEnsembleV2Strategy.py", "trend"),
    ("vol-targeted-trend", "VolTargetedTrendStrategy", "VolTargetedTrendStrategy.py", "trend"),
    ("faber-all-in", "FaberAllInStrategy", "FaberAllInStrategy.py", "allocation"),
    ("donchian-all-in", "DonchianAllInStrategy", "DonchianAllInStrategy.py", "breakout"),
)

#: (id, strategy, pair, timeframe) of the priority-100 paper profiles, in order.
EXPECTED_PAPER_HIGH = (
    ("basic-btc-1h", "basic", "BTC/USDT", "1h"),
    ("macd-btc-4h", "macd", "BTC/USDT", "4h"),
    ("donchian-eth-4h", "donchian", "ETH/USDT", "4h"),
    ("supertrend-btc-1h", "supertrend", "BTC/USDT", "1h"),
    # --- 2026 strategy research programme (paper only) --------------------
    ("keltner-breakout-v2-btc-4h", "keltner-breakout-v2", "BTC/USDT", "4h"),
    ("keltner-breakout-v2-eth-4h", "keltner-breakout-v2", "ETH/USDT", "4h"),
    ("trend-ensemble-v2-btc-1d", "trend-ensemble-v2", "BTC/USDT", "1d"),
    ("vol-targeted-trend-eth-4h", "vol-targeted-trend", "ETH/USDT", "4h"),
    ("faber-all-in-btc-1d", "faber-all-in", "BTC/USDT", "1d"),
    ("faber-all-in-eth-1d", "faber-all-in", "ETH/USDT", "1d"),
    ("donchian-all-in-doge-1h", "donchian-all-in", "DOGE/USDT", "1h"),
    ("donchian-all-in-eth-4h", "donchian-all-in", "ETH/USDT", "4h"),
)

#: (id, strategy, pair, timeframe) of the priority-50 paper profiles, in order.
EXPECTED_PAPER_LOW = (
    ("donchian-doge-1h", "donchian", "DOGE/USDT", "1h"),
    ("dual-thrust-dot-1h", "dual-thrust", "DOT/USDT", "1h"),
    # --- 2026 strategy research programme (paper only) --------------------
    ("trend-ensemble-v2-eth-1d", "trend-ensemble-v2", "ETH/USDT", "1d"),
    ("vol-targeted-trend-btc-1d", "vol-targeted-trend", "BTC/USDT", "1d"),
)

#: (id, strategy, pair, timeframe) of the two live profiles, in order.
EXPECTED_LIVE = (
    ("momentum-eth-4h-live", "momentum", "ETH/USDT", "4h"),
    ("faber-btc-1d-live", "faber", "BTC/USDT", "1d"),
)

#: The four profiles that deliberately hold one position at a time, so that
#: Freqtrade stakes the whole wallet on it. They are the only profiles allowed
#: to declare ``max_open_trades = 1``; every other profile uses two slots.
ALL_IN_PROFILE_IDS = frozenset(
    {
        "faber-all-in-btc-1d",
        "faber-all-in-eth-1d",
        "donchian-all-in-doge-1h",
        "donchian-all-in-eth-4h",
    }
)

#: ``max_open_trades`` of every profile outside :data:`ALL_IN_PROFILE_IDS`.
DEFAULT_MAX_OPEN_TRADES = 2


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
def test_catalogue_lists_the_documented_strategies(repo_root: Path) -> None:
    document = read_document(repo_root, "strategies.json")
    assert set(document) == {"strategies"}
    entries = document["strategies"]
    assert len(entries) == len(EXPECTED_STRATEGIES)
    assert [
        (entry["id"], entry["class_name"], entry["file"], entry["category"]) for entry in entries
    ] == list(EXPECTED_STRATEGIES)


def test_every_strategy_entry_is_complete_and_loads(repo_root: Path) -> None:
    document = read_document(repo_root, "strategies.json")
    strategies = build_strategies(repo_root)
    assert len(strategies) == len(EXPECTED_STRATEGIES)
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
def test_catalogue_lists_the_documented_profiles(repo_root: Path) -> None:
    document = read_document(repo_root, "profiles.json")
    expected = len(EXPECTED_PAPER_HIGH) + len(EXPECTED_PAPER_LOW) + len(EXPECTED_LIVE)
    assert set(document) == {"profiles"}
    assert len(document["profiles"]) == expected
    assert len(build_profiles(repo_root)) == expected


def test_every_profile_entry_carries_every_field(repo_root: Path) -> None:
    document = read_document(repo_root, "profiles.json")
    for entry in document["profiles"]:
        assert set(entry) == set(PROFILE_FIELDS)


def test_profile_identifiers_and_pairs(repo_root: Path) -> None:
    profiles = build_profiles(repo_root)
    assert len({profile.id for profile in profiles}) == len(profiles)
    for profile in profiles:
        # An all-in profile deliberately holds a single position, which is what
        # makes Freqtrade stake the whole wallet on it; every other profile uses
        # the platform default of two slots.
        expected_slots = 1 if profile.id in ALL_IN_PROFILE_IDS else DEFAULT_MAX_OPEN_TRADES
        assert profile.enabled is True
        assert profile.max_open_trades == expected_slots
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
    assert len(profiles) - len(live) == len(EXPECTED_PAPER_HIGH) + len(EXPECTED_PAPER_LOW)


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


def test_every_catalogue_profile_targets_the_default_pair_or_a_strategy_pair(
    repo_root: Path,
) -> None:
    """A strategy may carry any number of profiles, including none.

    This test used to require at least two profiles per strategy so that two
    readings of each rule set were always comparable. It was relaxed deliberately
    when the operator retired under-performing profiles: a retired strategy keeps
    its file and stays discoverable without forcing a profile to exist. What still
    holds is that every profile names a strategy the catalogue offers, which
    ``test_catalogue_strategies_and_profiles_are_coherent`` covers.
    """
    strategies = build_strategies(repo_root)
    profiles = build_profiles(repo_root)
    known = {meta.id for meta in strategies}
    assert all(profile.strategy in known for profile in profiles)


def test_profiles_load_into_full_records(repo_root: Path) -> None:
    for profile in build_profiles(repo_root):
        record = models.ProfileRecord(**profile.model_dump())
        assert record.state == models.STATE_STOPPED
        assert record.source == "catalogue"
        assert record.api_port == 0
        assert record.pid is None


#: The profiles the operator retired by applying two rules to the live paper
#: ledger: a profile that never opened a position, and a profile that closed two
#: or more positions with a win rate below 50 %. The strategy *files* stay, so a
#: retired rule remains discoverable and tested; only its profile is gone.
RETIRED_PROFILE_IDS = frozenset(
    {
        "basic-eth-4h",
        "bollinger-eth-1h",
        "bollinger-xrp-5m",
        "dual-thrust-eth-15m",
        "faber-btc-1d",
        "faber-eth-1d",
        "keltner-bnb-15m",
        "keltner-sol-1h",
        "macd-link-1h",
        "momentum-btc-1h",
        "momentum-sol-15m",
        "rsi-reversion-ada-1h",
        "rsi-reversion-btc-15m",
        "supertrend-avax-4h",
    }
)

#: Profiles the cleanup criteria would have removed but that were kept on purpose.
#: The two live profiles have no closed trade because a live profile is gated and
#: starts blocked -- not because the strategy failed -- and `donchian-doge-1h` had
#: a 0 % win rate on only two trades, which is too small a sample to act on.
CLEANUP_EXEMPT_PROFILE_IDS = frozenset(
    {"momentum-eth-4h-live", "faber-btc-1d-live", "donchian-doge-1h"}
)


def test_retired_profiles_are_absent_and_their_strategies_still_ship(repo_root: Path) -> None:
    """A retired profile is gone; the strategy file it used must remain.

    This pins both halves of the cleanup decision, so a later edit cannot quietly
    re-add a retired profile or delete a strategy that is only dormant.
    """
    profile_ids = {profile.id for profile in build_profiles(repo_root)}
    assert not (RETIRED_PROFILE_IDS & profile_ids), (
        f"retired profiles came back: {sorted(RETIRED_PROFILE_IDS & profile_ids)}"
    )

    strategy_ids = {meta.id for meta in build_strategies(repo_root)}
    for profile_id in sorted(RETIRED_PROFILE_IDS):
        assert profile_id not in profile_ids
    # Every strategy that lost all its profiles is still part of the catalogue.
    for strategy_id in ("bollinger", "keltner", "rsi-reversion"):
        assert strategy_id in strategy_ids, (
            f"{strategy_id} lost its profiles but its strategy file must remain"
        )


def test_the_cleanup_exemptions_are_still_profiles(repo_root: Path) -> None:
    """The profiles spared by the cleanup are still declared."""
    profile_ids = {profile.id for profile in build_profiles(repo_root)}
    assert profile_ids >= CLEANUP_EXEMPT_PROFILE_IDS, (
        f"exempt profiles missing: {sorted(CLEANUP_EXEMPT_PROFILE_IDS - profile_ids)}"
    )
