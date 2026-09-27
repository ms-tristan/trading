"""Contract tests of the declarative profile catalogue (work package wp-profile-catalogue-cli).

Everything here is offline and pure: the catalogue is a table of frozen values,
and every invariant is re-derived from the strategy registry and from
:mod:`trading_platform.realtime.warmup` -- never read back from the module's own
helper, so a helper that drifts from the strategies is caught by these tests
instead of hiding behind itself.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from trading_platform.core.errors import ConfigError, StrategyError
from trading_platform.profiles.catalogue import (
    CATALOGUE_BY_ID,
    HISTORY_HEADROOM_CANDLES,
    LIVE_PROFILE_INITIAL_BALANCE,
    PAPER_PROFILE_INITIAL_BALANCE,
    PROFILE_CATALOGUE,
    ProfileDefinition,
    live_definitions,
    missing_definitions,
    paper_definitions,
    paper_total_initial_balance,
    prunable_profile_ids,
    required_warmup_candles,
)
from trading_platform.realtime.catalog import FALLBACK_SYMBOLS
from trading_platform.realtime.warmup import (
    DEFAULT_WARMUP_CANDLES,
    candles_per_day,
    history_request_candles,
)
from trading_platform.strategy.registry import get_strategy, strategy_names

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The identifier rule of ``ProfileConfig`` (mirrored, never imported: it is private there).
PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: The three grids the catalogue may use: the shorter ones are not validated.
ALLOWED_TIMEFRAMES = frozenset({"1d", "4h", "1h"})

#: The five momentum profiles the deployed state database already holds, verbatim.
LEGACY_PROFILE_IDS = (
    "momentumarpa",
    "momentumbome",
    "momentumbtc",
    "momentumeth",
    "momentumsol",
)

#: Symbols of the catalog's static fallback list, as ``"BTC/USDT"`` strings.
FALLBACK_PAIRS = frozenset(f"{base}/{quote}" for symbol, base, quote in FALLBACK_SYMBOLS)

#: The nine fields ``POST /api/profiles`` accepts, and the only ones it accepts.
CREATE_BODY_KEYS = frozenset(
    {
        "profile_id",
        "symbol",
        "timeframe",
        "strategy",
        "mode",
        "initial_balance",
        "params",
        "warmup_candles",
        "history_candles",
    }
)


def _required_candles(definition: ProfileDefinition) -> int:
    """Re-derive the strategy requirement of ``definition`` (independent of the catalogue)."""
    strategy = get_strategy(definition.strategy, dict(definition.params))
    return int(strategy.required_candles(candles_per_day(definition.timeframe)))


# ---------------------------------------------------------------------------
# 1. shape and identity
# ---------------------------------------------------------------------------


def test_the_catalogue_holds_26_paper_rows_and_exactly_one_live_row() -> None:
    assert len(paper_definitions()) == 26
    assert len(live_definitions()) == 1
    assert len(PROFILE_CATALOGUE) == 27
    assert [definition.mode for definition in PROFILE_CATALOGUE].count("paper") == 26


def test_every_id_matches_the_profile_id_rule_and_the_documented_form() -> None:
    for definition in PROFILE_CATALOGUE:
        assert PROFILE_ID_PATTERN.match(definition.id), definition.id
        base = definition.symbol.split("/")[0].lower()
        expected = f"{definition.strategy}-{base}-{definition.timeframe}"
        if definition.mode != "paper":
            expected = f"{expected}-live"
        assert definition.id == expected


def test_the_identifiers_are_unique_and_never_collide_with_the_legacy_ones() -> None:
    identifiers = [definition.id for definition in PROFILE_CATALOGUE]
    assert len(set(identifiers)) == len(identifiers)
    assert not set(identifiers) & set(LEGACY_PROFILE_IDS)
    assert set(CATALOGUE_BY_ID) == set(identifiers)


def test_every_symbol_is_a_fallback_major_and_every_timeframe_is_a_long_grid() -> None:
    for definition in PROFILE_CATALOGUE:
        assert definition.symbol in FALLBACK_PAIRS, definition.symbol
        assert definition.timeframe in ALLOWED_TIMEFRAMES, definition.timeframe


# ---------------------------------------------------------------------------
# 2. the comparison rules
# ---------------------------------------------------------------------------


def test_every_strategy_spreads_over_two_symbols_and_two_timeframes() -> None:
    for strategy in strategy_names():
        rows = [row for row in paper_definitions() if row.strategy == strategy]
        assert len({row.symbol for row in rows}) >= 2, strategy
        assert len({row.timeframe for row in rows}) >= 2, strategy


def test_no_strategy_symbol_timeframe_triple_repeats() -> None:
    triples = [(row.strategy, row.symbol, row.timeframe) for row in paper_definitions()]
    assert len(set(triples)) == len(triples)


def test_faber_runs_on_the_daily_grid_and_momentum_only_on_4h_and_1d() -> None:
    faber = [row for row in paper_definitions() if row.strategy == "faber"]
    assert "1d" in {row.timeframe for row in faber}
    momentum = [row for row in paper_definitions() if row.strategy == "momentum"]
    assert {row.timeframe for row in momentum} <= {"4h", "1d"}


def test_every_registered_strategy_has_at_least_two_paper_profiles() -> None:
    for strategy in strategy_names():
        rows = [row for row in paper_definitions() if row.strategy == strategy]
        assert len(rows) >= 2, strategy
    assert {row.strategy for row in paper_definitions()} == set(strategy_names())


def test_every_row_holds_the_strategy_defaults() -> None:
    """The comparison holds the rule set constant and varies symbol and timeframe."""
    for definition in PROFILE_CATALOGUE:
        assert dict(definition.params) == {}
        assert get_strategy(definition.strategy, dict(definition.params)).name == (
            definition.strategy
        )


# ---------------------------------------------------------------------------
# 3. the warm-up invariant, re-derived independently
# ---------------------------------------------------------------------------


def test_the_declared_warm_up_is_derived_from_the_strategy_and_the_grid() -> None:
    for definition in PROFILE_CATALOGUE:
        required = _required_candles(definition)
        assert definition.warmup_candles >= required, definition.id
        assert definition.warmup_candles == max(DEFAULT_WARMUP_CANDLES, required), definition.id


def test_the_history_window_can_always_serve_the_warm_up() -> None:
    for definition in PROFILE_CATALOGUE:
        assert definition.history_candles >= definition.warmup_candles + 1, definition.id
        assert definition.history_candles >= history_request_candles(definition.warmup_candles)
        assert definition.history_candles == definition.warmup_candles + HISTORY_HEADROOM_CANDLES, (
            definition.id
        )


def test_the_warm_up_helper_floors_at_the_platform_budget_and_serves_long_requirements() -> None:
    # ``basic`` declares no warm-up at all: the platform budget is served instead.
    assert required_warmup_candles("basic", "1d") == DEFAULT_WARMUP_CANDLES
    # ``faber`` and ``rsi_reversion`` need 201 candles on every grid: the floor grows.
    assert required_warmup_candles("faber", "1d") == 201
    assert required_warmup_candles("rsi_reversion", "4h") == 201
    assert required_warmup_candles("momentum", "4h") == DEFAULT_WARMUP_CANDLES


def test_the_warm_up_helper_is_loud_on_an_unknown_strategy() -> None:
    with pytest.raises(StrategyError):
        required_warmup_candles("does-not-exist", "1d")


# ---------------------------------------------------------------------------
# 4. the create contract
# ---------------------------------------------------------------------------


def test_every_row_converts_to_a_valid_profile_config() -> None:
    for definition in PROFILE_CATALOGUE:
        config = definition.to_profile_config()
        assert config.id == definition.id
        assert config.symbol == definition.symbol
        assert config.timeframe == definition.timeframe
        assert config.strategy == definition.strategy
        assert config.mode == definition.mode
        assert config.initial_balance == definition.initial_balance
        assert config.warmup_candles == definition.warmup_candles
        assert config.history_candles == definition.history_candles


def test_the_create_body_carries_exactly_the_nine_route_fields() -> None:
    for definition in PROFILE_CATALOGUE:
        body = definition.to_create_body()
        assert set(body) == CREATE_BODY_KEYS, definition.id
        assert body["profile_id"] == definition.id
        assert body["mode"] in {"paper", "live"}
        assert isinstance(body["initial_balance"], float)
        assert isinstance(body["params"], dict)
        assert isinstance(body["warmup_candles"], int)
        assert isinstance(body["history_candles"], int)


def test_the_create_body_is_a_copy_that_never_reaches_back_into_the_definition() -> None:
    definition = PROFILE_CATALOGUE[0]
    body = definition.to_create_body()
    body["params"]["ema_fast"] = 99
    body["symbol"] = "NOPE/USDT"
    assert dict(definition.params) == {}
    assert PROFILE_CATALOGUE[0].symbol == definition.symbol


def test_a_typo_in_a_row_is_rejected_by_the_profile_model() -> None:
    definition = PROFILE_CATALOGUE[0]
    with pytest.raises(ConfigError):
        replace(definition, id="not a valid id!").to_profile_config()
    with pytest.raises(ConfigError):
        replace(definition, timeframe="3m").to_profile_config()
    with pytest.raises(ConfigError):
        replace(definition, mode="shadow").to_profile_config()


def test_the_catalogue_lookup_is_read_only_and_complete() -> None:
    assert dict(CATALOGUE_BY_ID) == {row.id: row for row in PROFILE_CATALOGUE}
    assert CATALOGUE_BY_ID["donchian-btc-4h"].strategy == "donchian"
    assert CATALOGUE_BY_ID["momentum-dot-1d-live"].mode == "live"
    with pytest.raises(TypeError):
        CATALOGUE_BY_ID["new"] = PROFILE_CATALOGUE[0]  # type: ignore[index]


# ---------------------------------------------------------------------------
# 5. money and the legacy profiles
# ---------------------------------------------------------------------------


def test_the_paper_catalogue_commits_26_000_usdt() -> None:
    assert PAPER_PROFILE_INITIAL_BALANCE == 1_000.0
    assert LIVE_PROFILE_INITIAL_BALANCE == 1_000.0
    assert paper_total_initial_balance() == 26_000.0
    assert paper_total_initial_balance() == sum(row.initial_balance for row in paper_definitions())


def test_the_live_entry_is_not_part_of_the_paper_total() -> None:
    live = live_definitions()[0]
    assert live.id == "momentum-dot-1d-live"
    assert live.symbol == "DOT/USDT"
    assert live.timeframe == "1d"
    assert live.initial_balance == LIVE_PROFILE_INITIAL_BALANCE
    assert paper_total_initial_balance() < sum(row.initial_balance for row in PROFILE_CATALOGUE)


def test_missing_definitions_report_the_rows_the_platform_does_not_hold() -> None:
    existing = [row.id for row in PROFILE_CATALOGUE[:3]] + list(LEGACY_PROFILE_IDS)
    missing = missing_definitions(existing)

    assert [row.id for row in missing] == [row.id for row in PROFILE_CATALOGUE[3:]]
    assert missing_definitions([row.id for row in PROFILE_CATALOGUE]) == ()
    assert len(missing_definitions([])) == 27


def test_prunable_ids_are_the_five_legacy_profiles_and_nothing_else() -> None:
    existing = [row.id for row in PROFILE_CATALOGUE] + list(LEGACY_PROFILE_IDS)
    assert prunable_profile_ids(existing) == tuple(sorted(LEGACY_PROFILE_IDS))
    assert prunable_profile_ids(list(LEGACY_PROFILE_IDS)) == tuple(sorted(LEGACY_PROFILE_IDS))
    assert prunable_profile_ids([]) == ()


def test_the_legacy_profiles_are_prunable_and_never_to_create() -> None:
    missing = {row.id for row in missing_definitions(list(LEGACY_PROFILE_IDS))}
    assert not missing & set(LEGACY_PROFILE_IDS)
    assert prunable_profile_ids(LEGACY_PROFILE_IDS) == tuple(sorted(LEGACY_PROFILE_IDS))


# ---------------------------------------------------------------------------
# 6. import policy
# ---------------------------------------------------------------------------


def test_importing_the_package_stays_free_of_the_web_layer_and_of_the_extras() -> None:
    """``profiles`` is consumed by the CLI: it must stay import-light and layer-clean."""
    script = (
        "import sys; import trading_platform.profiles; "
        "print([name for name in ('trading_platform.web', 'urllib.request', 'ccxt', 'freqtrade') "
        "if name in sys.modules])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"
