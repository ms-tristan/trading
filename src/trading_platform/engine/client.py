"""Async REST client of the Freqtrade API server of one profile.

Each profile exposes its own loopback API server (HTTP Basic authentication on
``http://127.0.0.1:<port>/api/v1/``), and this client is the only way the
platform reads a profile: the supervisor and the poller never touch the SQLite
database of a running bot.

Two behaviours matter as much as the endpoints themselves:

* every failure -- connection refused, timeout, HTTP error status, malformed
  JSON, unexpected payload shape -- is raised as :class:`FreqtradeClientError`,
  because the caller counts a failed read and must never see a raw ``httpx``
  exception;
* every number is passed through :func:`~trading_platform.models.finite_float`,
  because Freqtrade reports ``Infinity`` for ``profit_factor`` as soon as a
  profile has never lost a trade, and ``Infinity`` is not valid JSON.

Three published values are read with a meaning of their own rather than as a bare
number:

* ``portfolio_value`` is the equity the **bot** manages (``/balance``'s
  ``total_bot``), never the whole exchange wallet (``total``): a live bot staked
  with 250 USDT on a 5,000 USDT account would otherwise report roughly +1,900 %
  profit. A dry-run wallet *is* the bot's wallet, so paper profiles are
  unaffected;
* ``max_drawdown_pct`` is stored as the **0..1 ratio** Freqtrade published
  (``max_drawdown`` is a ratio in freqtrade 2026.8), never as a percentage: every
  ``*_pct`` field of the JSON API is a ratio, and the dashboard scales it. Rows
  written by an earlier revision hold the old percentage; the next 60 s poll
  overwrites them, so no migration is needed;
* ``profit_factor`` is ``float | None``: freqtrade serialises ``Infinity`` as
  JSON ``null`` for a flawless record, and ``None`` -- never ``0.0``, the factor
  of a profile that lost every trade -- is what the platform stores.

The record readers (:meth:`FreqtradeClient.daily_records`,
:meth:`FreqtradeClient.trade_records` and
:meth:`FreqtradeClient.open_trade_records`) follow a third rule: the *request*
keeps the failure convention above -- a transport, HTTP or JSON failure still
raises :class:`FreqtradeClientError` -- while a malformed *row* is normalised
away by :func:`normalise_trade_row` / :func:`normalise_daily_row` instead of
failing the whole read, so one odd row never costs a profile its history.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import TracebackType
from typing import Any

import httpx

from ..models import ProfileMetrics, finite_float, normalise_drawdown_pct, normalise_win_rate

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FreqtradeClient",
    "FreqtradeClientError",
    "normalise_daily_row",
    "normalise_trade_row",
]

#: Default read timeout of every request, in seconds.
#:
#: It is deliberately above the worst case measured on the live deployment:
#: ``GET /api/v1/balance`` answered in 19.74 s, 19.77 s and 11.94 s while 22
#: workers shared one CoinGecko rate limit, so a worker that is merely slow must
#: answer rather than time out. A timeout is a read failure, and enough read
#: failures make the supervisor restart a worker that was only waiting.
DEFAULT_TIMEOUT_SECONDS: float = 45.0


class FreqtradeClientError(RuntimeError):
    """Raised when the Freqtrade API server of a profile cannot be read."""


class FreqtradeClient:
    """Read a single Freqtrade profile over its loopback REST API.

    ``base_url`` is the API root of the profile, that is
    ``http://127.0.0.1:<port>/api/v1``; a missing trailing slash is added, so
    ``.../api/v1`` and ``.../api/v1/`` address exactly the same endpoints.
    ``transport`` exists so that tests can inject an :class:`httpx.MockTransport`;
    production code never passes it. ``timeout`` is the per-request read timeout
    and defaults to :data:`DEFAULT_TIMEOUT_SECONDS`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=(username, password),
            timeout=timeout,
            transport=transport,
        )

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> FreqtradeClient:
        """Enter the client as an async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave the async context manager, closing the HTTP connection pool."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP connection pool (safe to call more than once)."""
        await self._client.aclose()

    # -- endpoints ----------------------------------------------------------
    async def ping(self) -> bool:
        """Return whether the API server answers ``{"status": "pong"}``."""
        body = await self._get_object("ping")
        return body.get("status") == "pong"

    async def balance(self) -> dict[str, Any]:
        """Return ``GET /balance``: the wallet of the profile."""
        return await self._get_object("balance")

    async def profit(self) -> dict[str, Any]:
        """Return ``GET /profit``: the cumulative trade statistics."""
        return await self._get_object("profit")

    async def count(self) -> dict[str, Any]:
        """Return ``GET /count``: the number of open and allowed trades."""
        return await self._get_object("count")

    async def status(self) -> list[dict[str, Any]]:
        """Return ``GET /status``: the open trades of the profile."""
        return await self._get_collection("status")

    async def show_config(self) -> dict[str, Any]:
        """Return ``GET /show_config``: the effective Freqtrade configuration."""
        return await self._get_object("show_config")

    async def daily(self) -> dict[str, Any]:
        """Return ``GET /daily``: the realised profit day by day."""
        return await self._get_object("daily")

    async def performance(self) -> list[dict[str, Any]]:
        """Return ``GET /performance``: the profit per pair."""
        return await self._get_collection("performance")

    async def trades(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return ``GET /trades``: the most recent closed trades.

        Freqtrade answers with an envelope (``{"trades": [...],
        "trades_count": n, "total_trades": n, "offset": o}``); only the trade
        list is returned. A bare list is accepted as well.
        """
        return await self._get_collection("trades", params={"limit": int(limit)})

    async def daily_records(self) -> list[dict[str, Any]]:
        """Return ``GET /daily`` as the normalised daily rows of the profile.

        Freqtrade documents an object (``{"data": [{"date", "abs_profit",
        "rel_profit", "starting_balance", "fiat_value", "trade_count"}],
        "stake_currency": "USDT"}``); a bare list is accepted too. A body of
        neither shape, or one whose ``data`` is not a list, answers ``[]``: the
        endpoint exists to feed the read model, and "no rows" is the honest
        answer of a payload that carries none. The API order is preserved
        (Freqtrade answers oldest first) and every row goes through
        :func:`normalise_daily_row`, which drops an unusable row instead of
        raising.
        """
        rows = _rows_of(await self._get("daily"), "data")
        records: list[dict[str, Any]] = []
        for row in rows:
            normalised = normalise_daily_row(row)
            if normalised is not None:
                records.append(normalised)
        return records

    async def trade_records(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return ``GET /trades?limit=N`` as the normalised trade rows.

        The documented body is an object (``{"trades": [...], "trades_count": n,
        "offset": o, "total_trades": n}``), not a bare array, but a bare list is
        tolerated as well; a body of neither shape answers ``[]``. Rows keep the
        API order and go through :func:`normalise_trade_row` with
        ``default_open=False``: this endpoint answers closed trades, and a row
        that does not say otherwise is read as closed.
        """
        body = await self._get("trades", params={"limit": int(limit)})
        return _normalise_trade_rows(_rows_of(body, "trades"), default_open=False)

    async def open_trade_records(self) -> list[dict[str, Any]]:
        """Return ``GET /status`` as the normalised open trades of the profile.

        ``/status`` is the endpoint that really answers the open positions, so
        every row is normalised with ``default_open=True`` and ``is_open`` is
        forced to ``True``: a position served here is open by definition.
        """
        records = _normalise_trade_rows(
            _rows_of(await self._get("status"), "trades"), default_open=True
        )
        for record in records:
            record["is_open"] = True
        return records

    async def fetch_all(self) -> ProfileMetrics:
        """Read every measurement of the profile in one pass.

        ``balance``, ``profit``, ``count`` and ``status`` are read in that order
        and combined into a :class:`~trading_platform.models.ProfileMetrics`:

        * ``portfolio_value`` is the equity the **bot** manages
          (``balance.total_bot``), never the whole exchange wallet
          (``balance.total``), so a live profile measures its own funds instead
          of the operator's account;
        * ``cash`` is the free amount of the stake currency, absent currency
          meaning no free balance;
        * ``positions_value`` is that bot-managed equity minus that cash;
        * ``realized_profit_abs`` is the closed-trade profit and
          ``unrealized_profit_abs`` the remainder of the total profit;
        * ``win_rate`` is normalised (Freqtrade reports a ratio),
          ``max_drawdown_pct`` keeps the magnitude Freqtrade published -- which
          is the 0..1 ratio every ``*_pct`` field of the API carries -- and
          ``profit_factor`` becomes ``None`` when Freqtrade publishes no usable
          factor, instead of the ``0.0`` that means "every trade lost";
        * ``starting_capital`` is informational only: the platform computes
          ``profit_pct`` itself from the configured initial capital.
        """
        balance = await self.balance()
        profit = await self.profit()
        count = await self.count()
        # Read in the same pass so that every number describes one instant; the
        # authoritative open-trade number stays ``count["current"]``.
        _open_positions = await self.status()

        portfolio_value = _bot_equity(balance)
        cash = _free_balance(balance, str(balance.get("stake") or ""))
        profit_abs = finite_float(profit.get("profit_all_coin"))
        realized_profit_abs = finite_float(profit.get("profit_closed_coin"))
        best_pair = profit.get("best_pair")
        return ProfileMetrics(
            portfolio_value=portfolio_value,
            cash=cash,
            positions_value=portfolio_value - cash,
            profit_abs=profit_abs,
            realized_profit_abs=realized_profit_abs,
            unrealized_profit_abs=profit_abs - realized_profit_abs,
            open_trades=int(finite_float(count.get("current"))),
            closed_trades=int(finite_float(profit.get("closed_trade_count"))),
            win_rate=normalise_win_rate(profit.get("winrate")),
            profit_factor=_profit_factor(profit.get("profit_factor")),
            # ``max_drawdown`` is a ratio in freqtrade 2026.8 and is stored as
            # one; the snapshot rows of an older revision still hold the old
            # percentage, and the next 60 s poll overwrites them in place, so no
            # migration is required.
            max_drawdown_pct=normalise_drawdown_pct(profit.get("max_drawdown")),
            best_pair=str(best_pair) if best_pair else None,
            starting_capital=finite_float(balance.get("starting_capital")),
        )

    # -- internals ----------------------------------------------------------
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Perform ``GET /api/v1/<path>`` and return the decoded JSON body."""
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise FreqtradeClientError(f"Freqtrade API request failed: GET {path}: {exc}") from exc
        except ValueError as exc:
            raise FreqtradeClientError(
                f"Freqtrade API returned malformed JSON: GET {path}: {exc}"
            ) from exc

    async def _get_object(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the JSON object served by ``path``."""
        body = await self._get(path, params)
        if not isinstance(body, dict):
            raise FreqtradeClientError(
                f"Freqtrade API returned {type(body).__name__}, expected a JSON object: GET {path}"
            )
        return body

    async def _get_collection(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Return the JSON list served by ``path``.

        A ``{"trades": [...]}`` envelope is unwrapped, so a future Freqtrade
        release that wraps a currently bare list keeps working.
        """
        body = await self._get(path, params)
        items = body.get("trades") if isinstance(body, dict) else body
        if not isinstance(items, list):
            raise FreqtradeClientError(
                f"Freqtrade API returned {type(body).__name__}, expected a JSON list: GET {path}"
            )
        return [item for item in items if isinstance(item, dict)]


def _free_balance(balance: dict[str, Any], stake_currency: str) -> float:
    """Return the free amount of ``stake_currency`` in a balance payload."""
    currencies = balance.get("currencies")
    if not isinstance(currencies, list):
        return 0.0
    for entry in currencies:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("currency") or "") == stake_currency:
            return finite_float(entry.get("free"))
    return 0.0


def _bot_equity(balance: dict[str, Any]) -> float:
    """Return the equity the worker manages, never the whole exchange wallet.

    ``/balance`` publishes two totals: ``total`` values the entire exchange
    account -- the operator's funds included -- while ``total_bot`` values what
    the worker's own stake and open positions are worth. Measuring a profile
    against the wallet would report a live bot staked with 250 USDT on a
    5,000 USDT account as roughly +1,900 % profit, so ``total_bot`` is the number
    that describes the profile. Freqtrade 2026.8 always publishes it; the
    ``total`` fallback only serves a payload that predates the key (a dry-run
    wallet *is* the bot's wallet, so both totals are the same number there and
    paper profiles are unaffected either way).
    """
    managed = _usable_number(balance.get("total_bot"))
    if managed is not None:
        return managed
    return finite_float(balance.get("total"))


def _profit_factor(value: Any) -> float | None:
    """Return the ``profit_factor`` Freqtrade published, or ``None`` when it is none.

    A JSON ``null``, an absent key and a non-finite value all mean exactly the
    same thing -- Freqtrade serialises the ``Infinity`` of a profile that has not
    lost a trade yet as ``null``, and the very first poll of a worker answers no
    key at all -- and all of them answer ``None``. ``0.0`` is deliberately never
    used as that stand-in: it is the honest factor of a profile that lost every
    trade, so collapsing the two would render a flawless record as the worst one
    and drag the fleet factor down.
    """
    return _usable_number(value)


# ---------------------------------------------------------------------------
# Row normalisation: the read model of the monitoring surface
# ---------------------------------------------------------------------------
def _rows_of(body: Any, key: str) -> list[Any]:
    """Return the row list of a Freqtrade body, or ``[]`` when it has none.

    A body is either the documented envelope (an object carrying ``key``) or the
    bare list some Freqtrade releases answer; anything else -- including an
    envelope whose ``key`` is not a list -- carries no row.
    """
    items = body.get(key) if isinstance(body, dict) else body
    return items if isinstance(items, list) else []


def _usable_number(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when it is not usable.

    ``None``, text that is not a number and the non-finite floats Freqtrade
    emits (``NaN``, ``Infinity``) are all unusable.
    """
    number = finite_float(value, math.nan)
    return None if math.isnan(number) else number


def _text(value: Any, default: str = "") -> str:
    """Return ``value`` as text, ``default`` when it is missing or null."""
    return default if value is None else str(value)


def _optional_text(value: Any) -> str | None:
    """Return ``value`` as text, or ``None`` when it is missing or null."""
    return None if value is None else str(value)


def normalise_trade_row(
    raw: Mapping[str, Any],
    *,
    default_open: bool = False,
) -> dict[str, Any] | None:
    """Normalise one ``/trades`` or ``/status`` row into the stored trade keys.

    The result always carries the same twelve keys, so the store and the API
    never have to guess: ``trade_id`` (int), ``pair`` (str), ``is_open`` (bool),
    ``open_date``/``close_date``/``exit_reason`` (str or ``None``), and the
    numbers ``amount``, ``open_rate``, ``close_rate``, ``stake_amount``,
    ``profit_abs`` and ``profit_pct`` (float, ``0.0`` when the API omits them or
    answers a non-finite value).

    ``is_open`` is the payload's own boolean when it really is one and
    ``default_open`` otherwise, which is how ``/status`` (open positions only)
    and ``/trades`` (closed trades only) each get an honest default.

    ``profit_pct`` is a percentage: the API reports ``profit_ratio`` as a
    fraction, so a usable ratio is multiplied by ``100``; a row that only
    carries ``profit_pct`` keeps it verbatim.

    ``None`` is returned for an unusable row -- not a mapping, or without a
    usable ``trade_id``. This function never raises, whatever the payload looks
    like.
    """
    if not isinstance(raw, Mapping):
        return None
    trade_id = int(finite_float(raw.get("trade_id"), 0.0))
    if trade_id == 0:
        return None
    ratio = _usable_number(raw.get("profit_ratio"))
    is_open = raw.get("is_open")
    return {
        "trade_id": trade_id,
        "pair": _text(raw.get("pair")),
        "is_open": is_open if isinstance(is_open, bool) else default_open,
        "open_date": _optional_text(raw.get("open_date")),
        "close_date": _optional_text(raw.get("close_date")),
        "amount": finite_float(raw.get("amount"), 0.0),
        "open_rate": finite_float(raw.get("open_rate"), 0.0),
        "close_rate": finite_float(raw.get("close_rate"), 0.0),
        "stake_amount": finite_float(raw.get("stake_amount"), 0.0),
        "profit_abs": finite_float(raw.get("profit_abs"), 0.0),
        "profit_pct": (
            ratio * 100.0 if ratio is not None else finite_float(raw.get("profit_pct"), 0.0)
        ),
        "exit_reason": _optional_text(raw.get("exit_reason")),
    }


def normalise_daily_row(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalise one ``/daily`` row into the stored daily keys.

    The result is ``{"date" (str), "abs_profit" (float), "rel_profit" (float),
    "starting_balance" (float), "trade_count" (int)}``. ``date`` is the only
    required key: a row without one is unusable and answers ``None``.

    ``rel_profit`` is the API's own ratio when the row carries the key; when the
    API omits it, it is derived as ``abs_profit / starting_balance`` -- the rule
    :func:`trading_platform.metrics.daily_rows` already applies -- and stays
    ``0.0`` when there is no usable starting balance.

    Every number goes through :func:`~trading_platform.models.finite_float`, and
    this function never raises, whatever the payload looks like.
    """
    if not isinstance(raw, Mapping):
        return None
    date = raw.get("date")
    if date is None or not str(date):
        return None
    abs_profit = finite_float(raw.get("abs_profit"), 0.0)
    starting_balance = finite_float(raw.get("starting_balance"), 0.0)
    if "rel_profit" in raw:
        rel_profit = finite_float(raw.get("rel_profit"), 0.0)
    else:
        rel_profit = abs_profit / starting_balance if starting_balance > 0 else 0.0
    return {
        "date": str(date),
        "abs_profit": abs_profit,
        "rel_profit": rel_profit,
        "starting_balance": starting_balance,
        "trade_count": int(finite_float(raw.get("trade_count"), 0.0)),
    }


def _normalise_trade_rows(rows: list[Any], *, default_open: bool) -> list[dict[str, Any]]:
    """Normalise every usable row of ``rows``, preserving the API order."""
    records: list[dict[str, Any]] = []
    for row in rows:
        normalised = normalise_trade_row(row, default_open=default_open)
        if normalised is not None:
            records.append(normalised)
    return records
