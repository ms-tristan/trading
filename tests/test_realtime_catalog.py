"""Tests of the market catalog (work-package wp3).

Everything here is offline and deterministic: no test opens a socket, no test
imports a real exchange client for its own sake, and every networked path runs
against a local fake injected through the ``fetch_markets`` seam -- or against a
fake ``ccxt`` module installed in ``sys.modules`` for the duration of one test
(docs/testing-policy.md 8.3.1 and 9.3).

The group numbering follows the module under test:

1. the static table and the I/O-free default body;
2. the vocabularies: timeframes (shortest first), modes, strategies (read from
   the registry at call time, never hard-coded);
3. :class:`MarketCatalog` offline -- the fetcher is never called;
4. normalisation of a mixed ``ccxt`` payload (spot/futures/inactive/foreign
   quote/non-mapping entries);
5. degradation of every networked failure to the static table;
6. the TTL, driven by a :class:`ManualClock`, and ``invalidate``;
7. the lazily imported ``ccxt`` seam itself;
8. import safety without the optional ``[exchange]`` extra.
"""

from __future__ import annotations

import importlib
import logging
import sys
import types
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from trading_platform.core.constants import SUPPORTED_TIMEFRAMES, timeframe_minutes
from trading_platform.realtime import catalog
from trading_platform.realtime.catalog import (
    CATALOG_MODES,
    CATALOG_TTL_SECONDS,
    FALLBACK_SYMBOLS,
    STRATEGY_NAMES,
    TIMEFRAME_ORDER,
    MarketCatalog,
    default_catalog_body,
    fallback_symbols,
)
from trading_platform.realtime.clock import ManualClock
from trading_platform.strategy.registry import strategy_names

#: The timeframes of the picker, byte for byte (the requirement, not a derivation).
EXPECTED_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

#: Bases the static table must always carry.
REQUIRED_BASES = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX"}

#: The four documented keys of ``GET /api/catalog``.
CATALOG_KEYS = {"symbols", "strategies", "timeframes", "modes"}


class FakeFetcher:
    """Offline stand-in of the exchange seam; counts how often it is called."""

    def __init__(
        self,
        payload: Sequence[Mapping[str, Any]] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.payload: Sequence[Mapping[str, Any]] = [] if payload is None else payload
        self.error = error
        self.calls = 0

    def __call__(self) -> Sequence[Mapping[str, Any]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


class FakeExchangeClient:
    """Minimal ``ccxt`` exchange handle: only ``load_markets`` is used."""

    def __init__(self, markets: Mapping[str, Mapping[str, Any]]) -> None:
        self.markets = dict(markets)

    def load_markets(self) -> Mapping[str, Mapping[str, Any]]:
        return self.markets


def mixed_markets() -> list[Any]:
    """Return a payload mixing everything the normalisation must sort out."""
    return [
        # kept: plain spot USDT pair
        {"symbol": "XRP/USDT", "base": "XRP", "quote": "USDT", "type": "spot", "active": True},
        {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "type": "spot", "active": True},
        # dropped: a perpetual contract on the same base asset
        {"symbol": "ETH/USDT:USDT", "base": "ETH", "quote": "USDT", "type": "swap", "active": True},
        # dropped: a delisted spot market
        {"symbol": "SOL/USDT", "base": "SOL", "quote": "USDT", "type": "spot", "active": False},
        # dropped: quoted in another currency
        {"symbol": "ADA/BTC", "base": "ADA", "quote": "BTC", "type": "spot", "active": True},
        # dropped: the "spot" boolean encoding, not a spot market
        {"symbol": "AVAX/USDT", "base": "AVAX", "quote": "USDT", "spot": False, "active": True},
        # kept: base derived from the symbol
        {"symbol": "DOGE/USDT", "quote": "USDT", "type": "spot", "active": True},
        # kept: the "spot" boolean encoding of a live market
        {"symbol": "LTC/USDT", "base": "LTC", "quote": "USDT", "spot": True, "active": True},
        # kept: no quote in the payload, so it is derived from the symbol
        {"symbol": "LINK/USDT", "base": "LINK", "type": "spot", "active": True},
        # kept: a duplicate symbol, whose first occurrence wins
        {"symbol": "XRP/USDT", "base": "XRP", "quote": "USDT", "type": "spot", "active": True},
        # dropped: not a mapping at all
        "not-a-market",
        # dropped: no symbol
        {"base": "NOPE", "quote": "USDT", "type": "spot", "active": True},
        # dropped: not a pair
        {"symbol": "BARE", "quote": "USDT", "type": "spot", "active": True},
    ]


# ---------------------------------------------------------------------------
# 1. the static table and the I/O-free default body
# ---------------------------------------------------------------------------


def test_fallback_table_is_a_well_formed_usdt_table() -> None:
    assert len(FALLBACK_SYMBOLS) >= 8
    bases = {base for _, base, _ in FALLBACK_SYMBOLS}
    assert bases >= REQUIRED_BASES
    assert all(quote == "USDT" for _, _, quote in FALLBACK_SYMBOLS)
    assert all(symbol == f"{base}/USDT" for symbol, base, _ in FALLBACK_SYMBOLS)
    assert len({symbol for symbol, _, _ in FALLBACK_SYMBOLS}) == len(FALLBACK_SYMBOLS)


def test_fallback_symbols_shape_order_and_quote_filter() -> None:
    entries = fallback_symbols()
    assert entries[0] == {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"}
    assert all(set(entry) == {"symbol", "base", "quote"} for entry in entries)
    assert all(isinstance(value, str) for entry in entries for value in entry.values())
    # the filter ignores the case of the quote, so "usdt" is the same catalog
    assert fallback_symbols("usdt") == entries
    assert fallback_symbols("  USDT ") == entries


@pytest.mark.parametrize("quote", ["EUR", "", "USDT/USDT", "XXXX"])
def test_fallback_symbols_answers_an_empty_list_for_an_unknown_quote(quote: str) -> None:
    assert fallback_symbols(quote) == []


def test_default_catalog_body_has_exactly_the_four_documented_keys() -> None:
    body = default_catalog_body()
    assert set(body) == CATALOG_KEYS
    assert body["timeframes"] == EXPECTED_TIMEFRAMES
    assert body["modes"] == ["paper", "live"]
    # the strategy list comes from the registry, never from a literal here
    assert body["strategies"] == strategy_names()
    assert body["symbols"] == fallback_symbols("USDT")


def test_default_catalog_body_filters_the_symbols_on_the_quote() -> None:
    assert default_catalog_body("EUR")["symbols"] == []
    assert default_catalog_body("usdt")["symbols"] == fallback_symbols("USDT")


# ---------------------------------------------------------------------------
# 2. the vocabularies
# ---------------------------------------------------------------------------


def test_timeframes_modes_and_ttl_are_the_documented_ones() -> None:
    assert tuple(EXPECTED_TIMEFRAMES) == TIMEFRAME_ORDER
    assert list(TIMEFRAME_ORDER) == sorted(SUPPORTED_TIMEFRAMES, key=timeframe_minutes)
    assert CATALOG_MODES == ("paper", "live")
    assert CATALOG_TTL_SECONDS == 300.0


def test_strategy_names_is_resolved_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert STRATEGY_NAMES() == strategy_names()
    monkeypatch.setattr(catalog, "strategy_names", lambda: ["zeta", "alpha"])
    assert STRATEGY_NAMES() == ["zeta", "alpha"]
    assert default_catalog_body()["strategies"] == ["zeta", "alpha"]


# ---------------------------------------------------------------------------
# 3. MarketCatalog offline
# ---------------------------------------------------------------------------


def test_market_catalog_exposes_its_configuration() -> None:
    default = MarketCatalog()
    assert (default.exchange, default.market, default.quote) == ("binance", "spot", "USDT")
    configured = MarketCatalog(exchange="kraken", market="spot", quote="usdt")
    assert (configured.exchange, configured.market, configured.quote) == (
        "kraken",
        "spot",
        "usdt",
    )


def test_offline_catalog_answers_the_static_table_without_calling_the_fetcher() -> None:
    fetcher = FakeFetcher(error=AssertionError("the fetcher must never be called offline"))
    market_catalog = MarketCatalog(allow_network=False, fetch_markets=fetcher, clock=ManualClock())
    assert market_catalog.symbols() == fallback_symbols("USDT")
    assert market_catalog.symbols() == fallback_symbols("USDT")
    assert fetcher.calls == 0


def test_catalog_returns_the_four_keys_and_a_symbol_copy() -> None:
    market_catalog = MarketCatalog(allow_network=False, clock=ManualClock())
    body = market_catalog.catalog()
    assert set(body) == CATALOG_KEYS
    assert body["symbols"] == fallback_symbols("USDT")
    assert body["strategies"] == strategy_names()
    assert body["timeframes"] == EXPECTED_TIMEFRAMES
    assert body["modes"] == ["paper", "live"]
    returned = market_catalog.symbols()
    returned.clear()
    assert market_catalog.symbols() == fallback_symbols("USDT")


def test_an_offline_catalog_answers_an_empty_symbol_list_for_a_foreign_quote() -> None:
    market_catalog = MarketCatalog(quote="EUR", allow_network=False, clock=ManualClock())
    body = market_catalog.catalog()
    assert body["symbols"] == []
    assert body["timeframes"] == EXPECTED_TIMEFRAMES
    assert body["modes"] == ["paper", "live"]


# ---------------------------------------------------------------------------
# 4. normalisation of a mixed payload
# ---------------------------------------------------------------------------


def test_normalisation_keeps_only_the_active_spot_pairs_of_the_quote() -> None:
    fetcher = FakeFetcher(payload=mixed_markets())
    market_catalog = MarketCatalog(allow_network=True, fetch_markets=fetcher, clock=ManualClock())
    assert market_catalog.symbols() == [
        {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"},
        {"symbol": "DOGE/USDT", "base": "DOGE", "quote": "USDT"},
        {"symbol": "LINK/USDT", "base": "LINK", "quote": "USDT"},
        {"symbol": "LTC/USDT", "base": "LTC", "quote": "USDT"},
        {"symbol": "XRP/USDT", "base": "XRP", "quote": "USDT"},
    ]
    assert fetcher.calls == 1


def test_normalisation_accepts_the_markets_mapping_of_ccxt() -> None:
    markets = {
        "BTC/USDT": {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "type": "spot"},
        "ETH/USDT:USDT": {
            "symbol": "ETH/USDT:USDT",
            "base": "ETH",
            "quote": "USDT",
            "type": "swap",
        },
    }
    market_catalog = MarketCatalog(
        allow_network=True,
        fetch_markets=lambda: markets,
        clock=ManualClock(),
    )
    assert market_catalog.symbols() == [
        {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"},
    ]


def test_a_venue_with_no_matching_market_answers_empty_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fetcher = FakeFetcher(
        payload=[
            {"symbol": "BTC/EUR", "base": "BTC", "quote": "EUR", "type": "spot", "active": True}
        ]
    )
    market_catalog = MarketCatalog(
        quote="USDT", allow_network=True, fetch_markets=fetcher, clock=ManualClock()
    )
    with caplog.at_level(logging.WARNING, logger=catalog.LOGGER.name):
        assert market_catalog.symbols() == []
    assert "no tradable" in caplog.text


# ---------------------------------------------------------------------------
# 5. every networked failure degrades to the static table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("the venue answered 503"),
        ImportError("ccxt is not installed: pip install -e '.[exchange]'"),
        TimeoutError("the venue did not answer within 5s"),
    ],
)
def test_networked_failures_degrade_to_the_static_table(
    error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    fetcher = FakeFetcher(error=error)
    market_catalog = MarketCatalog(allow_network=True, fetch_markets=fetcher, clock=ManualClock())
    with caplog.at_level(logging.WARNING, logger=catalog.LOGGER.name):
        assert market_catalog.symbols() == fallback_symbols("USDT")
    assert fetcher.calls == 1
    assert "using the static symbol list" in caplog.text
    # the degraded answer is cached like any other one: no hammering of a dead venue
    assert market_catalog.symbols() == fallback_symbols("USDT")
    assert fetcher.calls == 1


def test_the_catalog_never_raises_when_the_fetcher_explodes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fetcher = FakeFetcher(error=RuntimeError("boom"))
    market_catalog = MarketCatalog(allow_network=True, fetch_markets=fetcher)
    with caplog.at_level(logging.WARNING, logger=catalog.LOGGER.name):
        body = market_catalog.catalog()
    assert set(body) == CATALOG_KEYS
    assert body["symbols"] == fallback_symbols("USDT")


# ---------------------------------------------------------------------------
# 6. the TTL and invalidation
# ---------------------------------------------------------------------------


def test_symbols_are_fetched_once_per_ttl_window() -> None:
    clock = ManualClock()
    fetcher = FakeFetcher(
        payload=[
            {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "type": "spot", "active": True}
        ]
    )
    market_catalog = MarketCatalog(
        allow_network=True, fetch_markets=fetcher, clock=clock, ttl_seconds=60.0
    )
    expected = [{"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"}]
    assert market_catalog.symbols() == expected
    assert market_catalog.symbols() == expected
    assert fetcher.calls == 1
    clock.advance(59.0)
    assert market_catalog.symbols() == expected
    assert fetcher.calls == 1
    clock.advance(1.0)
    assert market_catalog.symbols() == expected
    assert fetcher.calls == 2


def test_invalidate_forces_a_refresh() -> None:
    clock = ManualClock()
    fetcher = FakeFetcher(payload=[])
    market_catalog = MarketCatalog(
        allow_network=True, fetch_markets=fetcher, clock=clock, ttl_seconds=600.0
    )
    assert market_catalog.symbols() == []
    assert market_catalog.symbols() == []
    assert fetcher.calls == 1
    market_catalog.invalidate()
    assert market_catalog.symbols() == []
    assert fetcher.calls == 2


def test_an_offline_catalog_also_honours_invalidate() -> None:
    market_catalog = MarketCatalog(allow_network=False, clock=ManualClock())
    assert market_catalog.symbols() == fallback_symbols("USDT")
    market_catalog.invalidate()
    assert market_catalog.symbols() == fallback_symbols("USDT")


# ---------------------------------------------------------------------------
# 7. the lazily imported ccxt seam
# ---------------------------------------------------------------------------


def install_fake_ccxt(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exchanges: Mapping[str, Any],
) -> None:
    """Install a fake ``ccxt`` module exposing ``exchanges`` as factories."""
    module = types.ModuleType("ccxt")
    for name, factory in exchanges.items():
        setattr(module, name, factory)
    monkeypatch.setitem(sys.modules, "ccxt", module)


def block_ccxt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the missing optional extra *inside the test body*.

    ``None`` in ``sys.modules`` makes ``import ccxt`` raise ``ImportError``, and
    the cached ``ccxt.*`` submodules are dropped through ``monkeypatch`` -- a
    cached submodule would be served straight out of ``sys.modules`` and the
    simulation would silently stop biting (docs/testing-policy.md 1.2 and 8.3.2).
    """
    monkeypatch.setitem(sys.modules, "ccxt", None)
    for name in [name for name in sys.modules if name.startswith("ccxt.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def test_default_fetcher_builds_the_exchange_with_the_documented_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeExchangeClient(
        {"BTC/USDT": {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "type": "spot"}}
    )
    configs: list[Mapping[str, Any]] = []

    def factory(config: Mapping[str, Any]) -> FakeExchangeClient:
        configs.append(config)
        return client

    install_fake_ccxt(monkeypatch, exchanges={"binance": factory})
    fetched = catalog._default_fetch_markets(exchange="binance", market="spot")
    assert configs == [
        {"enableRateLimit": True, "timeout": 5000, "options": {"defaultType": "spot"}}
    ]
    assert list(fetched) == list(client.markets.values())


def test_the_networked_catalog_uses_the_ccxt_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeExchangeClient(
        {
            "BTC/USDT": {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "type": "spot"},
            "ETH/BTC": {"symbol": "ETH/BTC", "base": "ETH", "quote": "BTC", "type": "spot"},
        }
    )
    install_fake_ccxt(monkeypatch, exchanges={"binance": lambda config: client})
    market_catalog = MarketCatalog(allow_network=True, clock=ManualClock())
    assert market_catalog.symbols() == [
        {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"},
    ]


def test_an_exchange_unknown_to_ccxt_degrades_to_the_static_table(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_fake_ccxt(monkeypatch, exchanges={})
    market_catalog = MarketCatalog(exchange="nowhere", allow_network=True, clock=ManualClock())
    with caplog.at_level(logging.WARNING, logger=catalog.LOGGER.name):
        assert market_catalog.symbols() == fallback_symbols("USDT")
    assert "does not provide the exchange" in caplog.text


def test_the_default_fetcher_rejects_an_exchange_unknown_to_ccxt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ccxt(monkeypatch, exchanges={})
    with pytest.raises(ValueError, match="does not provide the exchange"):
        catalog._default_fetch_markets(exchange="nowhere", market="spot")


# ---------------------------------------------------------------------------
# 8. import safety without the optional extra
# ---------------------------------------------------------------------------


def test_the_module_imports_and_answers_without_ccxt(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    block_ccxt(monkeypatch)
    # reloading executes the module body again: it must not need the extra
    reloaded = importlib.reload(catalog)
    assert reloaded.default_catalog_body()["timeframes"] == EXPECTED_TIMEFRAMES
    with caplog.at_level(logging.WARNING, logger=reloaded.LOGGER.name):
        online = reloaded.MarketCatalog(allow_network=True, clock=ManualClock())
        assert online.symbols() == fallback_symbols("USDT")
    assert "using the static symbol list" in caplog.text
