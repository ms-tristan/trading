"""The declarative profile catalogue: 26 paper profiles, one live entry, zero I/O.

A profile is a strategy + a timeframe + a mode + a symbol + a starting amount of
cash, and this module is the **declaration** of the set the platform should hold.
Adding a profile is a one-line data edit; nothing else in the platform has to
change, which is the whole point of the module.

The shape of the catalogue
--------------------------
* **26 paper profiles** -- two or three per house strategy, over the ten
  registered strategies, so the operator can compare the strategies *and* the
  grids side by side;
* **one live entry** -- ``momentum-dot-1d-live`` -- carried by the table so the
  apply path's refusal is reachable and testable.  It is **never** created while
  the venue credentials are absent: a live profile the broker cannot authenticate
  is quarantined as a boot failure, and fabricating one would be a lie.

Holding the strategy constant
-----------------------------
Every entry uses the strategy's **default parameters** (``params={}``): the
comparison holds the rule set constant and varies the symbol and the timeframe,
exactly like the repository's research protocol.  A profile that wants different
parameters is another catalogue row, and the row carries them explicitly.

Identifiers
-----------
``<strategy>-<base-lower>-<timeframe>`` for a paper profile
(``donchian-btc-4h``) and ``<strategy>-<base-lower>-<timeframe>-live`` for a live
one (``momentum-dot-1d-live``).  The form is mechanical, testable, matches
:data:`~trading_platform.config.models.PROFILE_ID_PATTERN`
(``^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$``) and never collides with the five legacy
momentum profiles of the deployed state database (``momentumarpa``,
``momentumbome``, ``momentumbtc``, ``momentumeth``, ``momentumsol``), which carry
no hyphen at all.

Symbols and timeframes
----------------------
Only the twelve liquid USDT majors of
:data:`~trading_platform.realtime.catalog.FALLBACK_SYMBOLS` are used, so the
venue read can never fail on an unknown pair, and only the **1d**, **4h** and
**1h** grids: the shorter ones are not validated by the repository and their
warm-ups are enormous.  No ``(strategy, symbol, timeframe)`` triple repeats, every
strategy appears on at least two distinct symbols and on at least two distinct
timeframes, ``faber`` appears on ``1d`` (its 200-candle average *is* a 200-day
rule there) and ``momentum`` stays on ``4h``/``1d``, the only grids its own
documentation validates.

Derived, never hand-typed
-------------------------
``warmup_candles`` and ``history_candles`` are **computed** through the strategy
registry and :mod:`trading_platform.realtime.warmup` -- the platform's single
arithmetic authority -- by :func:`required_warmup_candles`:

* ``required = strategy.required_candles(candles_per_day(timeframe))`` reads the
  very class the runner will build, with the very parameters of the row;
* ``warmup_candles = max(DEFAULT_WARMUP_CANDLES, required)`` -- the floor is the
  platform's 200-candle budget, so ``basic``'s declared ``0`` becomes the budget
  instead of ``ProfileConfig``'s minimum of ``1``.  ``rsi_reversion`` and
  ``faber`` need ``201`` candles, which the default budget cannot feed; the
  catalogue declares the honest ``201`` (the create route accepts it) while
  ``GET /api/catalog`` keeps reporting the combination as not feedable on the
  default budget;
* ``history_candles = warmup_candles + HISTORY_HEADROOM_CANDLES``, which is
  ``history_request_candles(warmup)`` plus nine spare rows: the window always
  serves the CLOSED candles the warm-up needs, so the create route raises no
  coherence warning.

Money
-----
One shared USDT ledger funds every order of a mode, so the sum of the paper
``initial_balance`` values is real capital the ledger must be able to cover:
``26 x 1 000.00 = 26 000.00 USDT``.  The amount is uniform on purpose -- the
comparison across profiles is only meaningful when they start from the same
capital.  :func:`paper_total_initial_balance` publishes the total and
:func:`trading_platform.profiles.apply.apply_catalogue` compares it to the
ledger's own capacity before creating anything.

Nothing here is deleted
-----------------------
The profiles already living in the deployed state database are **never** touched
by a default run: they are reported as prunable by
:func:`prunable_profile_ids` and removed only by the explicit ``--prune`` flag of
``trading provision``.

Layer direction (frozen)
------------------------
This module imports ``config``, ``strategy`` and the pure ``realtime.warmup``
seam only: no I/O, no network, no clock, no environment.  It must never be
imported by ``trading_platform.strategy`` nor ``trading_platform.realtime``, and
``trading_platform.cli`` imports it inside the command body.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, cast

from pydantic import ValidationError

from trading_platform.config import ProfileConfig
from trading_platform.core.errors import ConfigError
from trading_platform.realtime.warmup import DEFAULT_WARMUP_CANDLES, candles_per_day
from trading_platform.strategy.registry import get_strategy

__all__ = [
    "CATALOGUE_BY_ID",
    "HISTORY_HEADROOM_CANDLES",
    "LIVE_PROFILE_INITIAL_BALANCE",
    "PAPER_PROFILE_INITIAL_BALANCE",
    "PROFILE_CATALOGUE",
    "ProfileDefinition",
    "live_definitions",
    "missing_definitions",
    "paper_definitions",
    "paper_total_initial_balance",
    "prunable_profile_ids",
    "required_warmup_candles",
]

#: Starting cash of **every** paper profile of the catalogue, in USDT.
#:
#: Uniform on purpose: comparing strategies is only meaningful when they start
#: from the same capital, and a uniform amount makes the committed total a single
#: multiplication (``26 000.00`` USDT for the 26 paper rows).
PAPER_PROFILE_INITIAL_BALANCE: float = 1_000.0

#: Starting cash of the (single) live entry, in USDT.
#:
#: It is the venue's real money, not the platform's simulated ledger: the value is
#: what the operator is willing to commit per live profile, and the apply path
#: refuses to create the entry while the venue credentials are absent.
LIVE_PROFILE_INITIAL_BALANCE: float = 1_000.0

#: Spare rows added on top of the warm-up when the history window is declared.
#:
#: ``history_candles = warmup_candles + HISTORY_HEADROOM_CANDLES`` is strictly
#: greater than ``warmup_candles + 1``, the documented coherence rule: the window
#: always serves the CLOSED candles the warm-up needs even after the venue's
#: still-forming candle is discounted, and it keeps enough room for the engine to
#: read a candle it has just seen.
HISTORY_HEADROOM_CANDLES: int = 10

#: The only two run modes a definition may carry.
_MODES: tuple[str, ...] = ("paper", "live")


@dataclass(frozen=True)
class ProfileDefinition:
    """One row of the catalogue: everything the create route needs, and nothing else.

    The row is a pure value: it holds no client, no store and no clock, and every
    method below derives its answer from the fields alone.  ``params`` is the
    strategy parameter mapping (the strategy's defaults when empty), ``warmup_candles``
    and ``history_candles`` are the **derived** candles described in the module
    docstring, and ``mode`` is ``'paper'`` or ``'live'``.

    Attributes
    ----------
    id:
        Profile identifier, ``<strategy>-<base-lower>-<timeframe>`` (plus
        ``-live`` for a live entry).
    symbol:
        Trading pair, always a USDT major of the catalog's static fallback list.
    timeframe:
        One of ``1d``, ``4h``, ``1h``.
    strategy:
        Registered strategy name.
    mode:
        ``'paper'`` or ``'live'``.
    initial_balance:
        Starting cash of the profile, in USDT.
    params:
        Strategy parameters; empty means "the strategy's defaults".
    warmup_candles:
        CLOSED candles the strategy needs on that grid, floored at the platform's
        :data:`~trading_platform.realtime.warmup.DEFAULT_WARMUP_CANDLES` budget.
    history_candles:
        Rows the engine's window holds: ``warmup_candles + HISTORY_HEADROOM_CANDLES``.
    """

    id: str
    symbol: str
    timeframe: str
    strategy: str
    mode: str
    initial_balance: float
    params: Mapping[str, Any]
    warmup_candles: int
    history_candles: int

    def to_create_body(self) -> dict[str, Any]:
        """Return the exact body of ``POST /api/profiles`` for this row.

        The nine keys are the ones the route accepts, and the route rejects any
        other key (``unexpected field``), so this method is the single place the
        catalogue touches that contract:

        ``profile_id``, ``symbol``, ``timeframe``, ``strategy``, ``mode``,
        ``initial_balance``, ``params``, ``warmup_candles``, ``history_candles``.

        The returned mapping is a fresh copy: mutating it never reaches back into
        the frozen definition.
        """
        return {
            "profile_id": self.id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.strategy,
            "mode": self.mode,
            "initial_balance": float(self.initial_balance),
            "params": dict(self.params),
            "warmup_candles": int(self.warmup_candles),
            "history_candles": int(self.history_candles),
        }

    def to_profile_config(self) -> ProfileConfig:
        """Return this row as a validated :class:`~trading_platform.config.ProfileConfig`.

        The conversion is the loudness of the table: the profile identifier is
        checked against ``ProfileConfig``'s own pattern, the timeframe against the
        supported grids and the parameters against the model's scalar rule, so a
        typo in a row fails **at import time** instead of at apply time.

        Raises
        ------
        ConfigError
            When the model rejects the row; the message names the identifier.
        """
        if self.mode not in _MODES:
            raise ConfigError(
                f"profile definition {self.id!r} carries an unsupported mode: {self.mode!r} "
                f"(supported: {', '.join(_MODES)})"
            )
        mode = cast("Literal['paper', 'live']", self.mode)
        try:
            return ProfileConfig(
                id=self.id,
                symbol=self.symbol,
                timeframe=self.timeframe,
                strategy=self.strategy,
                params=dict(self.params),
                mode=mode,
                initial_balance=float(self.initial_balance),
                warmup_candles=int(self.warmup_candles),
                history_candles=int(self.history_candles),
            )
        except ValidationError as exc:
            raise ConfigError(f"profile definition {self.id!r} is invalid: {exc}") from exc


def required_warmup_candles(
    strategy: str, timeframe: str, params: Mapping[str, Any] | None = None
) -> int:
    """Return the warm-up the platform must serve a profile of ``strategy`` on ``timeframe``.

    The number is ``max(DEFAULT_WARMUP_CANDLES, required)`` where ``required`` is
    the strategy's **own** requirement, read from the class the registry builds
    with ``params`` on that candle grid
    (:func:`~trading_platform.realtime.warmup.candles_per_day`).  The floor is the
    platform's 200-candle budget: a strategy that declares no warm-up at all
    (``basic`` answers ``0``) is still served that budget, which is what the
    engine's default window holds, and a strategy that needs more than the budget
    (``faber``, ``rsi_reversion``: 201) is served its own requirement.

    Parameters
    ----------
    strategy:
        Registered strategy name.
    timeframe:
        Supported candle grid, e.g. ``'4h'``.
    params:
        Strategy parameters; ``None`` means "the strategy's defaults".

    Returns
    -------
    int
        The warm-up in CLOSED candles, always ``>= DEFAULT_WARMUP_CANDLES``.

    Raises
    ------
    StrategyError
        If the strategy is unknown or rejects ``params``.
    ConfigError
        If the timeframe is not supported.
    """
    required = int(get_strategy(strategy, params).required_candles(candles_per_day(timeframe)))
    return max(DEFAULT_WARMUP_CANDLES, required)


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------
# Every row goes through ``_entry``, which derives the warm-up and the history
# window from the registry: a row can therefore never declare a warm-up the
# strategy does not need, nor one the create route would refuse.


def _entry(
    strategy: str,
    base: str,
    timeframe: str,
    *,
    mode: str = "paper",
    initial_balance: float = PAPER_PROFILE_INITIAL_BALANCE,
    params: Mapping[str, Any] | None = None,
) -> ProfileDefinition:
    """Materialise one catalogue row through the registry and validate it.

    The identifier is ``<strategy>-<base-lower>-<timeframe>`` (plus ``-live`` for
    a live row) and the symbol is always ``<BASE>/USDT``.  The warm-up and the
    history window come from :func:`required_warmup_candles` and
    :data:`HISTORY_HEADROOM_CANDLES`, so a row declares numbers only for its
    identity and its money.

    The returned definition is validated on the spot through
    :meth:`ProfileDefinition.to_profile_config`, which is what makes a typo in the
    table fail at import time.
    """
    parameters = dict(params or {})
    warmup = required_warmup_candles(strategy, timeframe, parameters)
    definition = ProfileDefinition(
        id=f"{strategy}-{base.lower()}-{timeframe}{'' if mode == 'paper' else f'-{mode}'}",
        symbol=f"{base.upper()}/USDT",
        timeframe=timeframe,
        strategy=strategy,
        mode=mode,
        initial_balance=float(initial_balance),
        params=parameters,
        warmup_candles=warmup,
        history_candles=warmup + HISTORY_HEADROOM_CANDLES,
    )
    definition.to_profile_config()
    return definition


#: Every profile the platform should hold: 26 paper rows plus one live row.
#:
#: The order is the order the apply path creates them in and the order the CLI
#: reports them in: grouped by strategy, most established first.
PROFILE_CATALOGUE: tuple[ProfileDefinition, ...] = (
    # -- momentum: its own documentation validates the 4h and 1d grids only --
    _entry("momentum", "BTC", "4h"),
    _entry("momentum", "ETH", "1d"),
    _entry("momentum", "SOL", "4h"),
    # -- donchian: the Turtle channel breakout, on three grids --
    _entry("donchian", "BTC", "4h"),
    _entry("donchian", "ETH", "1d"),
    _entry("donchian", "SOL", "1h"),
    # -- macd: the signal-line crossover, slow trend following --
    _entry("macd", "BTC", "4h"),
    _entry("macd", "AVAX", "1h"),
    _entry("macd", "LINK", "1d"),
    # -- rsi_reversion: the Connors pullback, needs 201 candles on every grid --
    _entry("rsi_reversion", "BTC", "1h"),
    _entry("rsi_reversion", "DOGE", "4h"),
    _entry("rsi_reversion", "XRP", "1d"),
    # -- bollinger: band mean reversion --
    _entry("bollinger", "BTC", "4h"),
    _entry("bollinger", "ADA", "1h"),
    _entry("bollinger", "LTC", "1d"),
    # -- supertrend: the ATR trailing-flip follower --
    _entry("supertrend", "BNB", "4h"),
    _entry("supertrend", "DOT", "1h"),
    _entry("supertrend", "TRX", "1d"),
    # -- keltner: the volatility breakout filtered by a long EMA --
    _entry("keltner", "ETH", "4h"),
    _entry("keltner", "AVAX", "1d"),
    # -- faber: a 200-candle average, which is a 200-DAY rule on the 1d grid --
    _entry("faber", "BTC", "1d"),
    _entry("faber", "LTC", "4h"),
    # -- dual_thrust: the intraday range breakout --
    _entry("dual_thrust", "SOL", "1h"),
    _entry("dual_thrust", "BTC", "4h"),
    # -- basic: the reference rule, kept in the comparison --
    _entry("basic", "BTC", "1h"),
    _entry("basic", "ETH", "4h"),
    # -- the single live entry: carried so the refusal is reachable and testable,
    #    never created while the venue credentials are absent --
    _entry(
        "momentum",
        "DOT",
        "1d",
        mode="live",
        initial_balance=LIVE_PROFILE_INITIAL_BALANCE,
    ),
)

#: Identifiers of every catalogue row (the live entry included).
_CATALOGUE_IDS: frozenset[str] = frozenset(definition.id for definition in PROFILE_CATALOGUE)

#: The catalogue keyed by identifier, for O(1) lookups (read-only).
CATALOGUE_BY_ID: Mapping[str, ProfileDefinition] = MappingProxyType(
    {definition.id: definition for definition in PROFILE_CATALOGUE}
)


def paper_definitions() -> tuple[ProfileDefinition, ...]:
    """Return the paper rows of the catalogue, in catalogue order."""
    return tuple(definition for definition in PROFILE_CATALOGUE if definition.mode == "paper")


def live_definitions() -> tuple[ProfileDefinition, ...]:
    """Return the live rows of the catalogue, in catalogue order.

    There is exactly one today, and the apply path treats every row this function
    returns as "refused unless the venue credentials and the live gate are
    present".
    """
    return tuple(definition for definition in PROFILE_CATALOGUE if definition.mode != "paper")


def paper_total_initial_balance() -> float:
    """Return the paper capital the whole catalogue commits, in USDT.

    This is the number the apply path compares to the shared paper ledger's
    capacity: one ledger funds every order of the mode, so the catalogue is only
    affordable when that ledger can cover this total.
    """
    return float(sum(definition.initial_balance for definition in paper_definitions()))


def missing_definitions(existing_ids: Iterable[str]) -> tuple[ProfileDefinition, ...]:
    """Return the catalogue rows whose identifier is **not** in ``existing_ids``.

    Catalogue order is preserved, and both modes are considered: an absent live
    row is "missing" like any other -- whether it may be created is the apply
    path's credential gate, not this function's business.
    """
    known = {str(identifier) for identifier in existing_ids}
    return tuple(definition for definition in PROFILE_CATALOGUE if definition.id not in known)


def prunable_profile_ids(existing_ids: Iterable[str]) -> tuple[str, ...]:
    """Return the identifiers that exist but are **absent from the catalogue**.

    Sorted ascending so the answer is total and deterministic: a set has no order
    of its own, and the caller (``--prune``) needs a reproducible list.  The five
    legacy momentum profiles of the deployed state database are exactly what this
    function reports, which is why the default apply run deletes nothing.
    """
    known = {str(identifier) for identifier in existing_ids}
    return tuple(sorted(known - _CATALOGUE_IDS))
