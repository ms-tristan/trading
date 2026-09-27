"""The per-strategy warm-up matrix of the catalog (work-package wp-warmup-matrix).

``GET /api/catalog`` answers one more vocabulary than it used to: for every
strategy and every supported timeframe, the candles that frame must hold before
the strategy can emit any signal, and whether the default warm-up can feed it.
The numbers are **not** invented here -- they are the frozen output of the single
arithmetic authority, :mod:`trading_platform.realtime.warmup`, projected onto the
catalog, and this file pins that projection:

1. the exact matrix, key by key, for the ten registered strategies;
2. the shape of both catalog bodies (the I/O-free one and
   :meth:`MarketCatalog.catalog`) and the fact that they carry the *same* block;
3. the invariants that hold for **any** registry -- seven timeframes, shortest
   first, and ``feedable`` exactly when the requirement fits the default budget;
4. the totality that keeps the route at ``GET``-never-500: an unbuildable
   strategy answers all-zero, all-``False``, and nothing raises;
5. the registry indirection: a strategy registered after import is part of the
   very next block.

The catalogue grew from two to ten strategies, and the matrix is the place where
that growth becomes visible to the picker: the eight new rows are pinned below,
one test each.  They are all **candle-based**, so their requirement is flat across
the seven grids, unlike ``momentum`` whose day-based lookbacks grow as the frame
shortens.  Two of them -- ``faber`` (201 candles) and ``rsi_reversion`` (201) --
do not fit the 200-candle default budget on any grid, which is an honest verdict
rather than a defect: a profile that overrides nothing cannot warm them up, and
the profile catalogue therefore declares an explicit ``warmup_candles`` for them.

Offline and deterministic: the matrix is a pure function of the registry, so no
test here touches a clock, a file or the network.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from trading_platform.core.constants import SUPPORTED_TIMEFRAMES, timeframe_minutes
from trading_platform.core.errors import ConfigError, StrategyError
from trading_platform.realtime import catalog
from trading_platform.realtime.catalog import (
    TIMEFRAME_ORDER,
    MarketCatalog,
    default_catalog_body,
    warmup_matrix,
    warmup_payload,
)
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.warmup import DEFAULT_WARMUP_CANDLES
from trading_platform.strategy import registry
from trading_platform.strategy.base import Strategy, StrategyParams

#: The timeframes of the picker, shortest first (the requirement, not a derivation).
EXPECTED_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

#: The documented keys of ``GET /api/catalog``: four vocabularies plus the matrix.
CATALOG_KEYS = {"symbols", "strategies", "timeframes", "modes", "warmup"}

#: The keys of one matrix entry, and of one cell of it.
MATRIX_ENTRY_KEYS = {"default_warmup_candles", "timeframes"}
CELL_KEYS = {"required_candles", "feedable"}

#: The requirement of every supported strategy, timeframe by timeframe.
#:
#: ``basic`` declares no warm-up at all, so it needs nothing on any grid;
#: ``momentum`` converts its three day-based lookbacks into candles, so the
#: requirement grows as the frame shortens.  These are the very numbers
#: ``tests/test_realtime_warmup.py`` pins for the same strategy through a profile,
#: read here through the catalog instead.
#:
#: The eight strategies of the expanded catalogue express **every** lookback in
#: candles already, so their requirement is the same number on all seven grids --
#: which is exactly why most of them are feedable by the default budget and why
#: the two that need 201 candles are not.
REQUIRED_BY_STRATEGY: dict[str, dict[str, int]] = {
    "basic": {
        "1m": 0,
        "5m": 0,
        "15m": 0,
        "30m": 0,
        "1h": 0,
        "4h": 0,
        "1d": 0,
    },
    "momentum": {
        "1m": 40321,
        "5m": 8065,
        "15m": 2689,
        "30m": 1345,
        "1h": 673,
        "4h": 169,
        "1d": 29,
    },
    "bollinger": dict.fromkeys(EXPECTED_TIMEFRAMES, 21),
    "donchian": dict.fromkeys(EXPECTED_TIMEFRAMES, 22),
    "dual_thrust": dict.fromkeys(EXPECTED_TIMEFRAMES, 16),
    "faber": dict.fromkeys(EXPECTED_TIMEFRAMES, 201),
    "keltner": dict.fromkeys(EXPECTED_TIMEFRAMES, 101),
    "macd": dict.fromkeys(EXPECTED_TIMEFRAMES, 47),
    "rsi_reversion": dict.fromkeys(EXPECTED_TIMEFRAMES, 201),
    "supertrend": dict.fromkeys(EXPECTED_TIMEFRAMES, 12),
}

#: The strategies of the expanded catalogue, in the sorted order the matrix keys
#: follow.  Frozen here so the exact-payload test below can never silently lose a
#: row when a strategy is removed from the registry.
EXPECTED_STRATEGY_NAMES = [
    "basic",
    "bollinger",
    "donchian",
    "dual_thrust",
    "faber",
    "keltner",
    "macd",
    "momentum",
    "rsi_reversion",
    "supertrend",
]

#: The strategies the 200-candle default budget can feed on every grid, and the
#: two it cannot -- the honest verdict the catalogue has to work around.
FEEDABLE_ON_EVERY_GRID = [
    "basic",
    "bollinger",
    "donchian",
    "dual_thrust",
    "keltner",
    "macd",
    "supertrend",
]
NOT_FEEDABLE_ON_ANY_GRID = ["faber", "rsi_reversion"]

#: The eight strategies added by the expanded catalogue.  Unlike ``momentum``
#: their lookbacks are expressed in candles, so their requirement is flat across
#: the grids -- which is what makes them feedable almost everywhere.
CANDLE_BASED_STRATEGY_NAMES = [
    "bollinger",
    "donchian",
    "dual_thrust",
    "faber",
    "keltner",
    "macd",
    "rsi_reversion",
    "supertrend",
]


def expected_entry(
    strategy: str, *, warmup_candles: int = DEFAULT_WARMUP_CANDLES
) -> dict[str, Any]:
    """Return the whole frozen matrix entry of ``strategy``.

    Built from the table above rather than copied, so the payload shape and the
    ``feedable`` rule stay visible in one place.
    """
    return {
        "default_warmup_candles": warmup_candles,
        "timeframes": {
            timeframe: {
                "required_candles": required,
                "feedable": required <= warmup_candles,
            }
            for timeframe, required in REQUIRED_BY_STRATEGY[strategy].items()
        },
    }


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[Strategy]]:
    """Give the test its own copy of the registry.

    Registering a strategy mutates module-level state, so the copy is
    monkeypatched into place and the global registry of the session stays
    pristine (the convention of ``tests/test_strategy_registry.py``).
    """
    copy: dict[str, type[Strategy]] = dict(registry.STRATEGIES)
    monkeypatch.setattr(registry, "STRATEGIES", copy)
    return copy


class MatrixStrategy(Strategy):
    """A minimal strategy registering ``200`` candles on every grid.

    It exists to prove the registry indirection of the matrix: the catalog never
    freezes the strategy list, so a name that appears after import appears in the
    very next ``warmup`` block, on all seven timeframes.
    """

    name: ClassVar[str] = "matrix"
    ParamsModel: ClassVar[type[StrategyParams]] = StrategyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {}

    def prepare(self, data: Any) -> Any:  # pragma: no cover - never called here
        return data

    def signals(self, data: Any) -> Any:  # pragma: no cover - never called here
        return data

    def required_candles(self, candles_per_day: float = 1.0) -> int:  # pragma: no cover - pure
        return 200


# ---------------------------------------------------------------------------
# 1. the frozen matrix of the registered strategies
# ---------------------------------------------------------------------------


def test_the_matrix_is_exactly_the_frozen_payload() -> None:
    """The whole block, byte for byte: names, order, numbers and booleans."""
    assert warmup_matrix() == {name: expected_entry(name) for name in EXPECTED_STRATEGY_NAMES}
    # the order of the keys is the registry order, and the table above covers it
    assert list(warmup_matrix()) == EXPECTED_STRATEGY_NAMES
    assert set(REQUIRED_BY_STRATEGY) == set(EXPECTED_STRATEGY_NAMES)
    # the short horizon of ``momentum`` is the point of the guard: 40321 candles
    # of ``1m`` can never fit the 200-candle default, while ``4h`` and ``1d`` can
    assert expected_entry("basic")["timeframes"]["1m"] == {
        "required_candles": 0,
        "feedable": True,
    }


def test_the_momentum_row_is_the_documented_one() -> None:
    """The strategy the incident was about: five timeframes it cannot warm up on."""
    entry = warmup_matrix()["momentum"]
    assert entry["default_warmup_candles"] == 200
    assert entry["timeframes"]["1m"] == {"required_candles": 40321, "feedable": False}
    assert entry["timeframes"]["5m"] == {"required_candles": 8065, "feedable": False}
    assert entry["timeframes"]["15m"] == {"required_candles": 2689, "feedable": False}
    assert entry["timeframes"]["30m"] == {"required_candles": 1345, "feedable": False}
    assert entry["timeframes"]["1h"] == {"required_candles": 673, "feedable": False}
    assert entry["timeframes"]["4h"] == {"required_candles": 169, "feedable": True}
    assert entry["timeframes"]["1d"] == {"required_candles": 29, "feedable": True}


def test_the_basic_row_is_every_cell_zero_and_feedable() -> None:
    """``basic`` declares no warm-up: every grid is feedable, including ``1m``."""
    entry = warmup_matrix()["basic"]
    assert entry["default_warmup_candles"] == 200
    assert set(entry["timeframes"]) == set(EXPECTED_TIMEFRAMES)
    for timeframe, cell in entry["timeframes"].items():
        assert cell == {"required_candles": 0, "feedable": True}, timeframe


# ---------------------------------------------------------------------------
# 1b. one row per strategy of the expanded catalogue
# ---------------------------------------------------------------------------

#: The grids each new row is pinned on: the shortest, the two intraday ones a
#: profile may actually use and the daily one.
PINNED_GRIDS = ["1m", "1h", "4h", "1d"]


@pytest.mark.parametrize("name", CANDLE_BASED_STRATEGY_NAMES)
def test_every_new_row_matches_the_frozen_requirement_on_the_pinned_grids(name: str) -> None:
    """Every new strategy, pinned cell by cell on ``1m``/``1h``/``4h``/``1d``.

    One test per name on purpose: a failure names the strategy whose warm-up
    drifted, and the registry order is checked separately so a row can never be
    dropped silently.  The two historical rows keep their own tests below --
    ``momentum`` is the only row whose requirement is *not* flat, since its
    lookbacks are day-based.
    """
    required = REQUIRED_BY_STRATEGY[name][PINNED_GRIDS[0]]
    assert required == REQUIRED_BY_STRATEGY[name][PINNED_GRIDS[-1]], (
        f"{name} expresses every lookback in candles: its requirement is flat"
    )

    entry = warmup_matrix()[name]
    assert entry["default_warmup_candles"] == 200
    for timeframe in PINNED_GRIDS:
        assert entry["timeframes"][timeframe] == {
            "required_candles": required,
            "feedable": required <= 200,
        }, (name, timeframe)


@pytest.mark.parametrize("name", FEEDABLE_ON_EVERY_GRID)
def test_the_candle_based_rows_are_feedable_on_every_grid(name: str) -> None:
    """A requirement expressed in candles fits the default budget on all seven grids."""
    entry = warmup_matrix()[name]
    for timeframe, cell in entry["timeframes"].items():
        assert cell["feedable"] is True, (name, timeframe)
        assert cell["required_candles"] <= DEFAULT_WARMUP_CANDLES, (name, timeframe)


@pytest.mark.parametrize("name", NOT_FEEDABLE_ON_ANY_GRID)
def test_the_two_two_hundred_candle_rows_are_not_feedable_on_any_grid(name: str) -> None:
    """``faber`` and ``rsi_reversion`` need 201 candles: the default budget is 200.

    This is the honest verdict of the catalogue rather than a defect: a profile
    that overrides nothing can never warm those two up, so the profile catalogue
    declares an explicit ``warmup_candles`` for them.  The cells stay ``False`` on
    every grid -- one candle short is one candle short, on every timeframe.
    """
    entry = warmup_matrix()[name]
    assert entry["timeframes"]["1d"]["required_candles"] == 201
    for timeframe, cell in entry["timeframes"].items():
        assert cell == {"required_candles": 201, "feedable": False}, (name, timeframe)
    # one more candle of budget is all it takes: the flag is a real comparison
    assert warmup_matrix(default_warmup_candles=201)[name]["timeframes"]["1d"]["feedable"] is True


def test_the_timeframe_order_of_a_row_is_the_picker_order() -> None:
    """Shortest first, never re-sorted: the order the payload is rendered in."""
    for entry in warmup_matrix().values():
        assert list(entry["timeframes"]) == EXPECTED_TIMEFRAMES


def test_the_matrix_follows_the_registry_order() -> None:
    """The keys are ``STRATEGY_NAMES()`` as returned, not a sorted copy of them."""
    assert list(warmup_matrix()) == catalog.STRATEGY_NAMES()


def test_the_matrix_defaults_to_the_timeframe_order_of_the_picker() -> None:
    """``timeframes=None`` is :data:`TIMEFRAME_ORDER`, the shortest grid first."""
    assert list(TIMEFRAME_ORDER) == EXPECTED_TIMEFRAMES
    assert warmup_matrix() == warmup_matrix(timeframes=list(TIMEFRAME_ORDER))


def test_the_matrix_honours_an_explicit_timeframe_selection() -> None:
    """A caller may ask for a slice, and the slice keeps the order it was given."""
    matrix = warmup_matrix(timeframes=["1d", "1m"])
    for entry in matrix.values():
        assert list(entry["timeframes"]) == ["1d", "1m"]
    assert matrix["momentum"]["timeframes"]["1d"] == {"required_candles": 29, "feedable": True}
    assert matrix["momentum"]["timeframes"]["1m"] == {"required_candles": 40321, "feedable": False}


@pytest.mark.parametrize("budget", [1, 200, 10_000])
def test_the_feedable_flag_is_the_comparison_with_the_budget(budget: int) -> None:
    """``feedable`` is exactly ``required_candles <= default_warmup_candles``."""
    matrix = warmup_matrix(default_warmup_candles=budget)
    for entry in matrix.values():
        assert entry["default_warmup_candles"] == budget
        for cell in entry["timeframes"].values():
            assert (cell["feedable"] is True) == (cell["required_candles"] <= budget)


def test_a_wider_budget_feeds_more_timeframes() -> None:
    """The guard is a real comparison, not a constant: raising it flips cells."""
    narrow = warmup_matrix(default_warmup_candles=200)["momentum"]["timeframes"]
    wide = warmup_matrix(default_warmup_candles=1000)["momentum"]["timeframes"]
    assert [tf for tf, cell in narrow.items() if cell["feedable"]] == ["4h", "1d"]
    assert [tf for tf, cell in wide.items() if cell["feedable"]] == ["1h", "4h", "1d"]


def test_the_payload_wraps_the_matrix_under_the_warmup_key() -> None:
    """The frozen JSON key is spelled once, and the wrapper is its only owner."""
    assert warmup_payload() == {"warmup": warmup_matrix()}
    assert list(warmup_payload()) == ["warmup"]


def test_every_number_of_the_matrix_is_a_plain_int_or_bool() -> None:
    """``_json_response`` serialises with ``allow_nan=False``: no float may leak."""
    matrix = warmup_matrix()
    assert matrix, "the registry must expose at least one strategy"
    for entry in matrix.values():
        assert type(entry["default_warmup_candles"]) is int
        assert entry["default_warmup_candles"] >= 1
        for cell in entry["timeframes"].values():
            assert type(cell["required_candles"]) is int
            assert cell["required_candles"] >= 0
            assert type(cell["feedable"]) is bool


def test_a_matrix_entry_has_exactly_the_documented_shape() -> None:
    """No key may be added or dropped: the dashboard reads this shape directly."""
    for name, entry in warmup_matrix().items():
        assert set(entry) == MATRIX_ENTRY_KEYS, name
        for timeframe, cell in entry["timeframes"].items():
            assert set(cell) == CELL_KEYS, (name, timeframe)


# ---------------------------------------------------------------------------
# 2. both catalog bodies carry the same block
# ---------------------------------------------------------------------------


def test_the_default_body_carries_the_five_documented_keys() -> None:
    """The I/O-free body -- the fallback of the route -- carries the matrix too."""
    body = default_catalog_body()
    assert set(body) == CATALOG_KEYS
    assert body["warmup"] == warmup_matrix()


def test_the_market_catalog_body_carries_the_five_documented_keys() -> None:
    """The served body has the same shape, without going through the network."""
    market_catalog = MarketCatalog(allow_network=False, clock=ManualClock())
    body = market_catalog.catalog()
    assert set(body) == CATALOG_KEYS
    assert body["warmup"] == warmup_matrix()


def test_both_bodies_agree_on_the_warmup_block() -> None:
    """One expression, two producers: the block can never drift between them."""
    market_catalog = MarketCatalog(allow_network=False, clock=ManualClock())
    assert default_catalog_body()["warmup"] == market_catalog.catalog()["warmup"]


def test_the_four_historical_keys_are_untouched() -> None:
    """The matrix is **additive**: the existing keys keep name, order and value."""
    body = default_catalog_body()
    assert list(body) == ["symbols", "strategies", "timeframes", "modes", "warmup"]
    assert body["strategies"] == catalog.STRATEGY_NAMES()
    assert body["timeframes"] == EXPECTED_TIMEFRAMES
    assert body["modes"] == ["paper", "live"]


def test_the_matrix_lines_up_with_the_warmup_contract_of_a_profile() -> None:
    """The catalog and the warm-up contract agree on the very same grid.

    A profile created on a catalog cell's strategy and timeframe, with **no**
    warm-up override, is served the strategy's own requirement -- so the cell and
    the contract publish the same number for the same grid, and the block is a
    projection of the contract rather than a second opinion about it.  The cell's
    own ``feedable`` is the comparison with the **default budget** of the matrix,
    which is the warm-up a profile overriding nothing resolved to before
    ``warmup_candles`` became optional.
    """
    from trading_platform.realtime.warmup import effective_warmup_candles, required_candles_for

    for name, entry in warmup_matrix().items():
        budget = entry["default_warmup_candles"]
        for timeframe, cell in entry["timeframes"].items():
            profile = _profile(strategy=name, timeframe=timeframe)
            required = required_candles_for(profile)
            # the matrix and the contract read the requirement off the same strategy
            assert cell["required_candles"] == required, (name, timeframe)
            assert effective_warmup_candles(profile) == max(1, required), (name, timeframe)
            # the guard: what the default budget could feed, and what the profile gets
            assert (cell["feedable"] is True) == (required <= budget), (name, timeframe)
            assert (cell["feedable"] and effective_warmup_candles(profile) <= budget) or (
                not cell["feedable"]
            ), (name, timeframe)


def _profile(*, strategy: str, timeframe: str) -> Any:
    """Return a minimal, valid profile on ``strategy`` and ``timeframe``."""
    from trading_platform.config.models import ProfileConfig

    return ProfileConfig.model_validate(
        {
            "id": "catalog-cell",
            "symbol": "BTC/USDT",
            "timeframe": timeframe,
            "strategy": strategy,
            "mode": "paper",
        }
    )


# ---------------------------------------------------------------------------
# 3. invariants over any registry
# ---------------------------------------------------------------------------


def test_every_strategy_has_exactly_the_seven_timeframes_shortest_first() -> None:
    """The client contract is "no missing key", for every name in ``strategies``."""
    matrix = warmup_matrix()
    assert set(matrix) == set(catalog.STRATEGY_NAMES())
    assert set(EXPECTED_TIMEFRAMES) == set(SUPPORTED_TIMEFRAMES)
    for name, entry in matrix.items():
        assert set(entry["timeframes"]) == set(SUPPORTED_TIMEFRAMES), name
        assert list(entry["timeframes"]) == sorted(SUPPORTED_TIMEFRAMES, key=timeframe_minutes), (
            name
        )


def test_the_matrix_is_the_projection_of_the_strategy_requirement() -> None:
    """Each cell is the very number the strategy declares, on that grid."""
    from trading_platform.realtime.warmup import candles_per_day
    from trading_platform.strategy.registry import get_strategy

    for name, entry in warmup_matrix().items():
        built = get_strategy(name, {})
        for timeframe, cell in entry["timeframes"].items():
            assert cell["required_candles"] == int(
                built.required_candles(candles_per_day(timeframe))
            ), (name, timeframe)


def test_the_whole_matrix_is_feedable_exactly_when_the_rule_says_so() -> None:
    """One assertion over the whole block: the rule holds cell by cell."""
    for name, entry in warmup_matrix().items():
        budget = entry["default_warmup_candles"]
        for timeframe, cell in entry["timeframes"].items():
            assert (cell["feedable"] is True) == (cell["required_candles"] <= budget), (
                name,
                timeframe,
            )


# ---------------------------------------------------------------------------
# 4. totality: an unbuildable strategy never breaks the route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error", [StrategyError("unknown strategy: 'ghost'"), ConfigError("bad parameter")]
)
def test_a_strategy_that_cannot_be_built_is_reported_as_all_zero(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """The matrix never raises: an unbuildable name is "nothing required, unusable".

    Reporting zeros rather than dropping the key keeps the shape unconditional,
    which is what lets the dashboard grey out a row instead of crashing on a
    missing one.  Both errors of the contract are exercised -- the registry really
    holds a name it cannot build (an unknown name is a :class:`StrategyError`, a
    strategy refusing the empty parameter set a :class:`ConfigError`) -- and the
    catch sits around the **whole** row, so a strategy that raises on a narrow
    grid cannot lose the cells of the wider ones.
    """
    monkeypatch.setattr(catalog, "get_strategy", _raiser(error), raising=True)
    monkeypatch.setattr(registry, "STRATEGIES", {"ghost": _UnbuildableStrategy})
    matrix = warmup_matrix()

    assert list(matrix) == ["ghost"]
    entry = matrix["ghost"]
    assert entry["default_warmup_candles"] == DEFAULT_WARMUP_CANDLES
    assert set(entry["timeframes"]) == set(SUPPORTED_TIMEFRAMES)
    assert list(entry["timeframes"]) == EXPECTED_TIMEFRAMES
    for timeframe, cell in entry["timeframes"].items():
        assert cell == {"required_candles": 0, "feedable": False}, timeframe


def test_an_unbuildable_strategy_leaves_the_catalog_bodies_answerable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The degraded answer still carries the fifth key -- a venue outage is not a 500."""
    monkeypatch.setattr(catalog, "get_strategy", _raiser(StrategyError("boom")), raising=True)
    body = default_catalog_body()
    assert set(body) == CATALOG_KEYS
    assert body["warmup"]
    assert body["symbols"], "the static table is unaffected"

    market_catalog = MarketCatalog(allow_network=False, clock=ManualClock())
    assert market_catalog.catalog()["warmup"] == body["warmup"]


def test_the_arithmetic_authority_is_not_re_implemented_here() -> None:
    """``catalog`` borrows the grid and the default budget: it owns no copy of them.

    The module-level names are the very objects of
    :mod:`trading_platform.realtime.warmup`, which is what keeps the matrix a
    projection of the warm-up contract instead of a second implementation of it.
    """
    from trading_platform.realtime import warmup

    assert catalog.candles_per_day is warmup.candles_per_day
    assert catalog.DEFAULT_WARMUP_CANDLES is warmup.DEFAULT_WARMUP_CANDLES
    assert catalog.candles_per_day("1h") == 24.0


def _raiser(error: Exception) -> Any:
    """Return a ``get_strategy`` stand-in that always raises ``error``."""

    def _get_strategy(name: str, params: Any = None) -> Any:
        raise error

    return _get_strategy


class _UnbuildableStrategy:
    """Stand-in registered under a name whose build always raises.

    The registry itself is replaced by a one-entry copy, so the *name* the matrix
    iterates comes from the real ``STRATEGY_NAMES()`` path while the build fails:
    the test exercises the contract's own totality rather than leaning on a name
    that happens to be missing from the registry.
    """

    name: ClassVar[str] = "ghost"


# ---------------------------------------------------------------------------
# 5. the registry indirection
# ---------------------------------------------------------------------------


def test_a_strategy_registered_after_import_appears_in_the_next_matrix(
    isolated_registry: dict[str, type[Strategy]],
) -> None:
    """The block is computed at call time, never frozen at import time."""
    before = warmup_matrix()
    assert "matrix" not in before

    registry.register_strategy(MatrixStrategy)

    after = warmup_matrix()
    assert set(registry.STRATEGIES) >= {"basic", "momentum", "matrix"}
    assert "matrix" in after
    assert after["matrix"]["timeframes"] == {
        timeframe: {"required_candles": 200, "feedable": True} for timeframe in EXPECTED_TIMEFRAMES
    }
    # the new name is part of the served catalog too, with its own row
    assert default_catalog_body()["warmup"]["matrix"] == after["matrix"]


def test_the_matrix_lines_up_with_the_strategies_key_of_the_body(
    isolated_registry: dict[str, type[Strategy]],
) -> None:
    """``body['strategies']`` and ``set(body['warmup'])`` are the same vocabulary."""
    registry.register_strategy(MatrixStrategy)
    body = default_catalog_body()
    assert set(body["warmup"]) == set(body["strategies"])
