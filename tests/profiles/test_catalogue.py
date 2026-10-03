"""Tests of the catalogue loaders: strategy metadata, file discovery and profiles.

The tests pin the two contracts of the module: a strategy file is discovered
even when ``config/strategies.json`` does not describe it (id and title derived
from the class name), and a broken profile entry fails loudly instead of being
silently dropped. The repository documents themselves are also pinned -- the
full strategy list and the profile count, derived from the document rather than
hardcoded -- so a renamed strategy or profile id breaks the suite rather than the
dashboard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_platform.models import SUPPORTED_TIMEFRAMES, ProfileConfig, StrategyMeta
from trading_platform.profiles.catalogue import (
    StrategyCatalogue,
    discover_strategy_files,
    load_profile_catalogue,
    load_strategy_catalogue,
)

# The fifteen strategies shipped in user_data/strategies, sorted by file stem.
SHIPPED_STRATEGY_STEMS = [
    "BasicStrategy",
    "BollingerStrategy",
    "DonchianAllInStrategy",
    "DonchianStrategy",
    "DualThrustStrategy",
    "FaberAllInStrategy",
    "FaberStrategy",
    "KeltnerBreakoutV2Strategy",
    "KeltnerStrategy",
    "MacdStrategy",
    "MomentumStrategy",
    "RsiReversionStrategy",
    "SupertrendStrategy",
    "TrendEnsembleV2Strategy",
    "VolTargetedTrendStrategy",
]

# The catalogue ids of config/strategies.json, in document order.
SHIPPED_STRATEGY_IDS = [
    "basic",
    "momentum",
    "rsi-reversion",
    "bollinger",
    "macd",
    "donchian",
    "keltner",
    "supertrend",
    "dual-thrust",
    "faber",
    # --- 2026 strategy research programme: additive, the ten above are untouched.
    "keltner-breakout-v2",
    "trend-ensemble-v2",
    "vol-targeted-trend",
    "faber-all-in",
    "donchian-all-in",
]


def write_json(path: Path, document: object) -> Path:
    """Write ``document`` as UTF-8 JSON and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def shipped_profile_document(repo_root: Path) -> dict:
    """Return ``config/profiles.json`` of the repository, as a dict."""
    return json.loads((repo_root / "config" / "profiles.json").read_text(encoding="utf-8"))


def shipped_profile_count(repo_root: Path) -> int:
    """Return how many profiles the repository document ships.

    The count is derived from the document itself, which has two consequences
    worth stating: the test below breaks when the file and the loader disagree,
    not when the catalogue legitimately grows, and the placeholder here mirrors
    the loader's own rule (a non-list ``profiles`` key means zero entries).
    """
    entries = shipped_profile_document(repo_root).get("profiles")
    return len(entries) if isinstance(entries, list) else 0


def write_strategies(directory: Path, *stems: str) -> Path:
    """Create one empty ``<stem>.py`` file per stem and return the directory."""
    directory.mkdir(parents=True, exist_ok=True)
    for stem in stems:
        (directory / f"{stem}.py").write_text("", encoding="utf-8")
    return directory


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------
def test_discover_strategy_files_returns_sorted_stems(tmp_path: Path) -> None:
    directory = write_strategies(tmp_path, "ZetaStrategy", "alpha", "BetaStrategy")
    (directory / "__init__.py").write_text("", encoding="utf-8")
    (directory / "_helpers.py").write_text("", encoding="utf-8")
    (directory / "notes.txt").write_text("", encoding="utf-8")
    (directory / "DirectoryStrategy.py").mkdir()

    assert discover_strategy_files(directory) == ["BetaStrategy", "ZetaStrategy", "alpha"]


def test_discover_strategy_files_ignores_a_missing_directory(tmp_path: Path) -> None:
    assert discover_strategy_files(tmp_path / "absent") == []


def test_discover_strategy_files_finds_the_shipped_strategies(repo_root: Path) -> None:
    directory = repo_root / "user_data" / "strategies"
    assert discover_strategy_files(directory) == SHIPPED_STRATEGY_STEMS


def test_discover_strategy_files_defaults_to_the_resolved_directory() -> None:
    assert discover_strategy_files() == SHIPPED_STRATEGY_STEMS


# ---------------------------------------------------------------------------
# StrategyCatalogue
# ---------------------------------------------------------------------------
def make_meta(strategy_id: str, class_name: str, **overrides: object) -> StrategyMeta:
    """Build a strategy metadata entry."""
    payload: dict[str, object] = {"id": strategy_id, "class_name": class_name}
    payload.update(overrides)
    return StrategyMeta.model_validate(payload)


def test_catalogue_exposes_lookup_and_iteration() -> None:
    basic = make_meta("basic", "BasicStrategy", title="EMA cross baseline")
    macd = make_meta("macd", "MacdStrategy", title="MACD trend follow")
    catalogue = StrategyCatalogue([basic, macd])

    assert len(catalogue) == 2
    assert catalogue.get("basic") == basic
    assert catalogue.get("absent") is None
    assert catalogue.by_class_name("MacdStrategy") == macd
    assert catalogue.by_class_name("AbsentStrategy") is None
    assert catalogue.all() == [basic, macd]
    assert [meta.id for meta in catalogue] == ["basic", "macd"]
    assert "basic" in catalogue
    assert "absent" not in catalogue
    assert StrategyCatalogue([]).all() == []


def test_catalogue_indexes_the_first_entry_of_a_class_name() -> None:
    first = make_meta("first", "SharedStrategy")
    second = make_meta("second", "SharedStrategy")
    catalogue = StrategyCatalogue([first, second])

    assert catalogue.by_class_name("SharedStrategy") == first
    assert len(catalogue) == 2


# ---------------------------------------------------------------------------
# load_strategy_catalogue
# ---------------------------------------------------------------------------
def test_load_strategy_catalogue_reads_the_repository_documents(repo_root: Path) -> None:
    strategies_dir = repo_root / "user_data" / "strategies"
    catalogue = load_strategy_catalogue(
        config_path=repo_root / "config" / "strategies.json",
        strategies_dir=strategies_dir,
    )

    assert [meta.id for meta in catalogue] == SHIPPED_STRATEGY_IDS
    assert len(catalogue) == len(SHIPPED_STRATEGY_IDS)
    for meta in catalogue:
        assert meta.file == f"{meta.class_name}.py"
        assert meta.title
        assert meta.summary
        assert (strategies_dir / meta.file).is_file()

    macd = catalogue.get("macd")
    assert macd is not None
    assert macd.class_name == "MacdStrategy"
    assert macd.category == "trend"
    assert macd.indicators == ["MACD(12, 26, 9)", "EMA(200)"]
    assert macd.timeframes == ["1h", "4h"]
    assert macd.reference
    assert macd.risk_notes
    assert catalogue.get("absent") is None
    assert catalogue.by_class_name("AbsentStrategy") is None


def test_load_strategy_catalogue_defaults_to_the_resolved_documents() -> None:
    catalogue = load_strategy_catalogue()
    assert [meta.id for meta in catalogue] == SHIPPED_STRATEGY_IDS
    basic = catalogue.get("basic")
    assert basic is not None
    assert basic.title == "EMA cross baseline"


def test_load_strategy_catalogue_adds_a_file_without_metadata(tmp_path: Path) -> None:
    config_path = write_json(
        tmp_path / "config" / "strategies.json",
        {
            "strategies": [
                {
                    "id": "known",
                    "class_name": "KnownStrategy",
                    "file": "KnownStrategy.py",
                    "title": "Known strategy",
                    "category": "trend",
                }
            ]
        },
    )
    strategies_dir = write_strategies(
        tmp_path / "strategies",
        "KnownStrategy",
        "MyThingStrategy",
        "MACDStrategy",
        "_private",
        "__init__",
    )

    catalogue = load_strategy_catalogue(config_path=config_path, strategies_dir=strategies_dir)

    assert [meta.id for meta in catalogue] == ["known", "macd", "my-thing"]
    discovered = catalogue.get("my-thing")
    assert discovered is not None
    assert discovered.class_name == "MyThingStrategy"
    assert discovered.title == "My Thing Strategy"
    assert discovered.file == "MyThingStrategy.py"
    assert discovered.category == "baseline"
    assert discovered.summary == ""
    assert discovered.description == ""
    assert discovered.reference == ""
    assert discovered.risk_notes == ""
    assert discovered.indicators == []
    assert discovered.timeframes == []
    assert catalogue.by_class_name("MyThingStrategy") == discovered

    acronym = catalogue.get("macd")
    assert acronym is not None
    assert acronym.class_name == "MACDStrategy"
    assert acronym.title == "MACD Strategy"


def test_load_strategy_catalogue_keeps_an_entry_whose_file_is_missing(tmp_path: Path) -> None:
    config_path = write_json(
        tmp_path / "config" / "strategies.json",
        {
            "strategies": [
                {"id": "ghost", "class_name": "GhostStrategy", "file": "GhostStrategy.py"},
                {"id": "other", "class_name": "OtherStrategy", "file": "OtherStrategy.py"},
            ]
        },
    )
    strategies_dir = write_strategies(tmp_path / "strategies", "OtherStrategy")

    catalogue = load_strategy_catalogue(config_path=config_path, strategies_dir=strategies_dir)

    assert [meta.id for meta in catalogue] == ["ghost", "other"]
    ghost = catalogue.get("ghost")
    assert ghost is not None
    assert ghost.class_name == "GhostStrategy"


def test_load_strategy_catalogue_tolerates_a_missing_document(tmp_path: Path) -> None:
    catalogue = load_strategy_catalogue(
        config_path=tmp_path / "config" / "absent.json",
        strategies_dir=tmp_path / "strategies",
    )
    assert len(catalogue) == 0
    assert catalogue.all() == []


def test_load_strategy_catalogue_tolerates_a_broken_document(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "strategies.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{ not json", encoding="utf-8")
    strategies_dir = write_strategies(tmp_path / "strategies", "OnlyStrategy")

    catalogue = load_strategy_catalogue(config_path=config_path, strategies_dir=strategies_dir)

    assert [meta.id for meta in catalogue] == ["only"]


def test_load_strategy_catalogue_skips_malformed_entries(tmp_path: Path) -> None:
    config_path = write_json(
        tmp_path / "config" / "strategies.json",
        {"strategies": [{"class_name": "NoIdStrategy"}, "not an object"]},
    )

    catalogue = load_strategy_catalogue(config_path=config_path, strategies_dir=tmp_path / "none")

    assert len(catalogue) == 0


# ---------------------------------------------------------------------------
# load_profile_catalogue
# ---------------------------------------------------------------------------
def test_load_profile_catalogue_reads_the_repository_document(repo_root: Path) -> None:
    profiles = load_profile_catalogue(config_path=repo_root / "config" / "profiles.json")

    expected = shipped_profile_count(repo_root)
    assert len(profiles) == expected
    assert len({profile.id for profile in profiles}) == expected
    assert profiles[0].id == "basic-btc-1h"
    assert profiles[-1].id == "vol-targeted-trend-btc-1d"
    assert all(isinstance(profile, ProfileConfig) for profile in profiles)

    modes = [profile.mode for profile in profiles]
    assert modes.count("paper") + modes.count("live") == expected
    assert modes.count("live") == 2
    assert all(profile.timeframe in SUPPORTED_TIMEFRAMES for profile in profiles)

    catalogue = load_strategy_catalogue(
        config_path=repo_root / "config" / "strategies.json",
        strategies_dir=repo_root / "user_data" / "strategies",
    )
    assert all(catalogue.get(profile.strategy) is not None for profile in profiles)

    first = profiles[0]
    assert first.name == "Basic BTC 1h"
    assert first.strategy == "basic"
    assert first.pairs == ["BTC/USDT"]
    assert first.initial_capital == 1000.0
    assert first.priority == 100
    assert first.enabled is True


def test_load_profile_catalogue_defaults_to_the_resolved_document(repo_root: Path) -> None:
    profiles = load_profile_catalogue()
    assert len(profiles) == shipped_profile_count(repo_root)


def test_load_profile_catalogue_is_empty_without_a_document(tmp_path: Path) -> None:
    assert load_profile_catalogue(config_path=tmp_path / "absent.json") == []


def test_load_profile_catalogue_applies_the_model_defaults(tmp_path: Path) -> None:
    config_path = write_json(
        tmp_path / "profiles.json",
        {"profiles": [{"id": "operator-1", "strategy": "basic", "unexpected": "ignored"}]},
    )

    profiles = load_profile_catalogue(config_path=config_path)

    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.id == "operator-1"
    assert profile.timeframe == "1h"
    assert profile.mode == "paper"
    assert profile.exchange == "binance"
    assert profile.pairs == []
    assert profile.max_open_trades == 2
    assert profile.priority == 100
    assert profile.enabled is True


def test_load_profile_catalogue_rejects_an_invalid_entry(tmp_path: Path) -> None:
    config_path = write_json(
        tmp_path / "profiles.json",
        {
            "profiles": [
                {"id": "good-profile", "strategy": "basic"},
                {"id": "broken-profile", "timeframe": "1h"},
            ]
        },
    )

    with pytest.raises(ValueError, match="broken-profile") as excinfo:
        load_profile_catalogue(config_path=config_path)

    message = str(excinfo.value)
    assert "broken-profile" in message
    assert "strategy" in message


def test_load_profile_catalogue_rejects_a_non_object_entry(tmp_path: Path) -> None:
    config_path = write_json(tmp_path / "profiles.json", {"profiles": ["not an object"]})

    with pytest.raises(ValueError, match="index 0"):
        load_profile_catalogue(config_path=config_path)


def test_load_profile_catalogue_ignores_a_non_list_document_key(tmp_path: Path) -> None:
    config_path = write_json(tmp_path / "profiles.json", {"profiles": {"id": "not-a-list"}})
    assert load_profile_catalogue(config_path=config_path) == []
