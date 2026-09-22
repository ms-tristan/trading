"""Market catalog of the realtime layer: symbols, strategies, timeframes, modes.

The dashboard pickers need four vocabularies, and this module is their single
source of truth:

symbols
    the tradable **spot** pairs of the exchange, quoted in the configured quote
    currency.  They are read through the existing exchange/``ccxt`` seam, cached
    server-side for :data:`CATALOG_TTL_SECONDS` and backed by a static, offline
    table (:data:`FALLBACK_SYMBOLS`) so the catalog route answers even when the
    venue -- or ``ccxt`` itself -- is unreachable;
strategies
    whatever :func:`trading_platform.strategy.registry.strategy_names` exposes
    **at call time**.  The list is never duplicated (and never hard-coded) here:
    a strategy registered after import shows up in the very next answer;
timeframes
    the keys of :data:`~trading_platform.core.constants.SUPPORTED_TIMEFRAMES`,
    ordered shortest to longest through
    :func:`~trading_platform.core.constants.timeframe_minutes`;
modes
    ``"paper"`` and ``"live"``.

Offline by default, networked only when asked
---------------------------------------------
``symbols()`` never raises.  ``MarketCatalog(allow_network=False)`` -- the
default -- answers :func:`fallback_symbols` without touching the network at all;
``allow_network=True`` calls the injected ``fetch_markets`` callable, or the
module-level default fetcher, which imports ``ccxt`` **inside its own body** and
builds the exchange with ``{"enableRateLimit": True, "timeout": 5000}``.  Every
failure of the networked path (a timeout, a venue error, an unknown exchange,
``ImportError`` when the optional ``[exchange]`` extra is missing) is logged at
``WARNING`` and degrades to the static table: a missing network is a normal
configuration of this project, never a 500 in the monitoring API.

Time flows through the injected :class:`~trading_platform.realtime.clock.Clock`
(``SystemClock`` by default), so a test drives the TTL of the cache with a
:class:`~trading_platform.realtime.clock.ManualClock` instead of sleeping, and
this module never reads the wall clock directly.

Import safety
-------------
Importing this module only pulls the standard library plus pure first-party
modules (``core.constants``, ``realtime.clock``, ``strategy.registry``); no
optional extra is imported at module scope and no socket is ever opened at import
time.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any

from trading_platform.core.constants import SUPPORTED_TIMEFRAMES, timeframe_minutes
from trading_platform.realtime.clock import Clock, SystemClock
from trading_platform.strategy.registry import strategy_names

__all__ = [
    "CATALOG_MODES",
    "CATALOG_TTL_SECONDS",
    "FALLBACK_SYMBOLS",
    "STRATEGIES_REQUIRING_FORECAST",
    "STRATEGY_NAMES",
    "TIMEFRAME_ORDER",
    "MarketCatalog",
    "default_catalog_body",
    "fallback_symbols",
]

#: Module logger: every degraded (networked -> static) answer is reported here.
LOGGER = logging.getLogger(__name__)

#: How long the fetched symbol list stays cached, in seconds.
CATALOG_TTL_SECONDS: float = 300.0

#: The only two run modes a profile may be created with.
CATALOG_MODES: tuple[str, ...] = ("paper", "live")

#: Supported timeframes, shortest first (the order the picker displays them in).
TIMEFRAME_ORDER: tuple[str, ...] = tuple(sorted(SUPPORTED_TIMEFRAMES, key=timeframe_minutes))

#: Static, offline table of the most liquid ``(symbol, base, quote)`` pairs used
#: whenever the venue (or ``ccxt``) is unavailable.  It is curated in rough
#: liquidity order and returned as-is by :func:`fallback_symbols`.
FALLBACK_SYMBOLS: tuple[tuple[str, str, str], ...] = (
    ("BTC/USDT", "BTC", "USDT"),
    ("ETH/USDT", "ETH", "USDT"),
    ("SOL/USDT", "SOL", "USDT"),
    ("BNB/USDT", "BNB", "USDT"),
    ("XRP/USDT", "XRP", "USDT"),
    ("ADA/USDT", "ADA", "USDT"),
    ("DOGE/USDT", "DOGE", "USDT"),
    ("AVAX/USDT", "AVAX", "USDT"),
    ("LTC/USDT", "LTC", "USDT"),
    ("LINK/USDT", "LINK", "USDT"),
    ("DOT/USDT", "DOT", "USDT"),
    ("TRX/USDT", "TRX", "USDT"),
)

#: Timeout of one default (``ccxt``) market request, in milliseconds.
_DEFAULT_TIMEOUT_MS = 5000


def _registered_strategy_names() -> list[str]:
    """Return the strategy names the registry knows *now*.

    The catalog follows the registry instead of freezing a copy of it: a strategy
    registered by a plugin -- or by a test -- is part of the next answer, and the
    picker can never offer a name the engine would reject.
    """
    return strategy_names()


#: Zero-argument callable returning the registered strategy names, resolved at
#: call time (a *callable* on purpose: a module-level tuple would freeze the list
#: at import time and silently ignore every later registration).
STRATEGY_NAMES: Callable[[], list[str]] = _registered_strategy_names


def _strategies_requiring_forecast() -> list[str]:
    """Return the registered strategies that cannot be built without an artifact.

    The marker is the strategy's own parameter model (see
    :func:`~trading_platform.realtime.features.strategy_needs_forecast`), never a
    hardcoded name, so a strategy that gains or loses its ``artifact`` field is
    reported correctly without touching this function.

    The creation form reads this list to ask for the artifact path *before*
    submitting: a ``timesfm`` profile created without one is refused by the
    engine, and making the operator discover that from a failed request is
    exactly the friction this avoids.
    """
    from trading_platform.config.models import ProfileConfig
    from trading_platform.realtime.features import strategy_needs_forecast

    required: list[str] = []
    for name in strategy_names():
        try:
            probe = ProfileConfig(id="probe", symbol="BTC/USDT", timeframe="1h", strategy=name)
        except (ValueError, TypeError):
            # A strategy that refuses the probe is left to the registry's own
            # failure when it is actually built; it is not reported here.
            continue
        if strategy_needs_forecast(probe):
            required.append(name)
    return required


#: Callable returning the strategy names that need a forecast artifact.
STRATEGIES_REQUIRING_FORECAST: Callable[[], list[str]] = _strategies_requiring_forecast


def fallback_symbols(quote: str = "USDT") -> list[dict[str, str]]:
    """Return the static symbol table, filtered on ``quote``.

    Parameters
    ----------
    quote:
        Quote currency to keep.  Matching ignores case, so ``"usdt"`` and
        ``"USDT"`` give the same answer.

    Returns
    -------
    list[dict[str, str]]
        One ``{"symbol", "base", "quote"}`` mapping per matching entry, in the
        curated order of :data:`FALLBACK_SYMBOLS`.  An unknown quote yields an
        empty list: this function never raises, whatever it is handed.
    """
    wanted = str(quote).strip().upper()
    return [
        {"symbol": symbol, "base": base, "quote": entry_quote}
        for symbol, base, entry_quote in FALLBACK_SYMBOLS
        if entry_quote == wanted
    ]


def default_catalog_body(quote: str = "USDT") -> dict[str, Any]:
    """Return the whole catalog with **no** I/O at all.

    The result has exactly the four documented keys of ``GET /api/catalog`` and
    is the guaranteed answer of the route when the venue cannot be reached.

    Parameters
    ----------
    quote:
        Quote currency of the returned symbols.
    """
    return {
        "symbols": fallback_symbols(quote),
        "strategies": STRATEGY_NAMES(),
        "timeframes": list(TIMEFRAME_ORDER),
        "modes": list(CATALOG_MODES),
        "forecast_strategies": STRATEGIES_REQUIRING_FORECAST(),
    }


def _normalise_markets(
    markets: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    *,
    quote: str,
    market: str,
) -> list[dict[str, str]]:
    """Normalise a raw ``ccxt`` market payload into the catalog symbol shape.

    A market is kept when it is *tradable* (``active`` is not ``False``), of the
    requested market type -- read from either the ``type`` string or the ``spot``
    boolean when the payload carries one -- and quoted in ``quote`` (the quote is
    read from the payload, or derived from the symbol itself).  Anything else --
    a futures contract, a delisted pair, another quote currency, an entry that is
    not even a mapping -- is dropped.

    Parameters
    ----------
    markets:
        The raw payload, either a sequence of market mappings or a mapping of
        ``symbol -> market`` (``ccxt``'s own ``load_markets()`` shape).
    quote:
        Quote currency to keep (case-insensitive).
    market:
        Market type to keep (``"spot"`` by default).

    Returns
    -------
    list[dict[str, str]]
        ``{"symbol", "base", "quote"}`` mappings, sorted by symbol and free of
        duplicates (the first occurrence of a symbol wins).
    """
    wanted_quote = str(quote).strip().upper()
    wanted_type = str(market).strip().lower()
    payload: Sequence[Mapping[str, Any]] = (
        list(markets.values()) if isinstance(markets, Mapping) else markets
    )
    entries: dict[str, dict[str, str]] = {}
    for raw in payload:
        if not isinstance(raw, Mapping):
            continue
        symbol = raw.get("symbol")
        if not symbol:
            continue
        symbol = str(symbol)
        if "/" not in symbol:
            continue
        declared_type = raw.get("type")
        if declared_type is not None and str(declared_type).strip().lower() != wanted_type:
            continue
        spot_flag = raw.get("spot")
        if spot_flag is not None and bool(spot_flag) != (wanted_type == "spot"):
            continue
        if raw.get("active") is False:
            continue
        declared_quote = raw.get("quote")
        if declared_quote is None:
            declared_quote = symbol.split("/", 1)[1]
        if str(declared_quote).strip().upper() != wanted_quote:
            continue
        base = raw.get("base") or symbol.split("/", 1)[0]
        entries.setdefault(
            symbol,
            {"symbol": symbol, "base": str(base), "quote": str(declared_quote)},
        )
    return [entries[symbol] for symbol in sorted(entries)]


def _default_fetch_markets(*, exchange: str, market: str) -> Sequence[Mapping[str, Any]]:
    """Fetch the markets of ``exchange`` through the optional ``ccxt`` extra.

    ``ccxt`` is imported **inside this body** so that importing the catalog never
    requires the optional extra and never opens a socket.

    Parameters
    ----------
    exchange:
        ``ccxt`` exchange id (``binance``, ``kraken``, ...).
    market:
        Market type handed to the client through ``options.defaultType``.

    Raises
    ------
    ImportError
        When the optional ``[exchange]`` extra is not installed.
    ValueError
        When the installed ``ccxt`` does not provide ``exchange``.
    """
    import ccxt

    factory = getattr(ccxt, exchange, None)
    if factory is None:
        raise ValueError(f"the installed ccxt does not provide the exchange {exchange!r}")
    client: Any = factory(
        {
            "enableRateLimit": True,
            "timeout": _DEFAULT_TIMEOUT_MS,
            "options": {"defaultType": market},
        }
    )
    return list(client.load_markets().values())


class MarketCatalog:
    """TTL-cached market catalog served to the pickers of the dashboard.

    Parameters
    ----------
    exchange:
        ``ccxt`` exchange id used by the default fetcher.
    market:
        Market type kept by the normalisation (``"spot"``).
    quote:
        Quote currency kept by the normalisation and reported in every entry.
    ttl_seconds:
        How long a computed answer stays cached.
    clock:
        Time seam used to measure the TTL; :class:`SystemClock` by default, so a
        test injects a :class:`ManualClock` and never sleeps.
    allow_network:
        ``False`` (the default) keeps the catalog strictly offline and always
        answers the static table.
    fetch_markets:
        Zero-argument callable returning the raw market payload; when ``None``
        the module-level ``ccxt`` fetcher is used.  Every test injects one, so no
        test ever reaches the network.

    Notes
    -----
    * :meth:`symbols` **never raises**: any failure of the networked path is
      logged at ``WARNING`` and degrades to :func:`fallback_symbols`;
    * the degraded answer is cached like any other one, so a venue that is down
      is not hammered on every poll of the dashboard -- :meth:`invalidate` forces
      a refresh;
    * the cache is guarded by a lock because the monitoring server answers from
      several threads.
    """

    def __init__(
        self,
        *,
        exchange: str = "binance",
        market: str = "spot",
        quote: str = "USDT",
        ttl_seconds: float = CATALOG_TTL_SECONDS,
        clock: Clock | None = None,
        allow_network: bool = False,
        fetch_markets: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        self._exchange = str(exchange)
        self._market = str(market)
        self._quote = str(quote)
        self._ttl_seconds = float(ttl_seconds)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._allow_network = bool(allow_network)
        self._fetch_markets: Callable[[], Sequence[Mapping[str, Any]]] = (
            fetch_markets
            if fetch_markets is not None
            else partial(_default_fetch_markets, exchange=self._exchange, market=self._market)
        )
        self._lock = threading.Lock()
        self._symbols: list[dict[str, str]] | None = None
        self._expires_at = 0.0

    @property
    def exchange(self) -> str:
        """Return the ``ccxt`` exchange id of the default fetcher."""
        return self._exchange

    @property
    def market(self) -> str:
        """Return the market type kept by the normalisation."""
        return self._market

    @property
    def quote(self) -> str:
        """Return the quote currency reported in every entry."""
        return self._quote

    def symbols(self) -> list[dict[str, str]]:
        """Return the tradable symbols of the configured quote currency.

        The answer is cached for ``ttl_seconds`` measured through the injected
        clock.  This method never raises: an offline catalog, an unavailable
        venue and a missing ``ccxt`` all answer the static table.
        """
        with self._lock:
            now = self._clock.monotonic()
            if self._symbols is None or now >= self._expires_at:
                self._symbols = self._load_symbols()
                self._expires_at = self._clock.monotonic() + self._ttl_seconds
            return [dict(entry) for entry in self._symbols]

    def catalog(self) -> dict[str, Any]:
        """Return the whole catalog: symbols, strategies, timeframes and modes."""
        return {
            "symbols": self.symbols(),
            "strategies": STRATEGY_NAMES(),
            "timeframes": list(TIMEFRAME_ORDER),
            "modes": list(CATALOG_MODES),
            "forecast_strategies": STRATEGIES_REQUIRING_FORECAST(),
        }

    def invalidate(self) -> None:
        """Drop the cached symbols so the next call recomputes them."""
        with self._lock:
            self._symbols = None
            self._expires_at = 0.0

    def _load_symbols(self) -> list[dict[str, str]]:
        """Compute the symbol list; the cache lock is already held."""
        if not self._allow_network:
            return fallback_symbols(self._quote)
        try:
            markets = self._fetch_markets()
        except Exception as exc:  # a venue, timeout, config or import failure: degrade
            LOGGER.warning(
                "market catalog: %s markets are unavailable (%s): using the static symbol list",
                self._exchange,
                exc,
            )
            return fallback_symbols(self._quote)
        normalised = _normalise_markets(markets, quote=self._quote, market=self._market)
        if not normalised:
            LOGGER.warning(
                "market catalog: %s returned no tradable %s market for quote %s",
                self._exchange,
                self._market,
                self._quote,
            )
        return normalised
