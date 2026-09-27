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
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from ..models import ProfileMetrics, finite_float, normalise_drawdown_pct, normalise_win_rate

__all__ = ["FreqtradeClient", "FreqtradeClientError"]


class FreqtradeClientError(RuntimeError):
    """Raised when the Freqtrade API server of a profile cannot be read."""


class FreqtradeClient:
    """Read a single Freqtrade profile over its loopback REST API.

    ``base_url`` is the API root of the profile, that is
    ``http://127.0.0.1:<port>/api/v1``; a missing trailing slash is added, so
    ``.../api/v1`` and ``.../api/v1/`` address exactly the same endpoints.
    ``transport`` exists so that tests can inject an :class:`httpx.MockTransport`;
    production code never passes it.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 10.0,
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

    async def fetch_all(self) -> ProfileMetrics:
        """Read every measurement of the profile in one pass.

        ``balance``, ``profit``, ``count`` and ``status`` are read in that order
        and combined into a :class:`~trading_platform.models.ProfileMetrics`:

        * ``portfolio_value`` is the wallet total;
        * ``cash`` is the free amount of the stake currency, absent currency
          meaning no free balance;
        * ``positions_value`` is the wallet total minus that cash;
        * ``realized_profit_abs`` is the closed-trade profit and
          ``unrealized_profit_abs`` the remainder of the total profit;
        * ``win_rate`` and ``max_drawdown_pct`` are normalised (Freqtrade
          reports both as ratios) and ``profit_factor`` becomes ``0.0`` when
          Freqtrade reports ``Infinity``;
        * ``starting_capital`` is informational only: the platform computes
          ``profit_pct`` itself from the configured initial capital.
        """
        balance = await self.balance()
        profit = await self.profit()
        count = await self.count()
        # Read in the same pass so that every number describes one instant; the
        # authoritative open-trade number stays ``count["current"]``.
        _open_positions = await self.status()

        portfolio_value = finite_float(balance.get("total"))
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
            profit_factor=finite_float(profit.get("profit_factor")),
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
