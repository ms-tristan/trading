"""Tests of the async Freqtrade REST client.

Every test drives an :class:`httpx.MockTransport`: no socket is opened and no
Freqtrade process is ever started, so the suite stays fast and deterministic.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from trading_platform.engine.client import FreqtradeClient, FreqtradeClientError

BASE_URL = "http://127.0.0.1:8101/api/v1/"
API_PORT_BASE_URL = "http://127.0.0.1:8101"
USERNAME = "alpha"
PASSWORD = "generated-password"

BALANCE = {
    "currencies": [
        {
            "currency": "BTC",
            "free": 0.01,
            "balance": 0.01,
            "used": 0.0,
            "est_stake": 500.0,
            "stake": "USDT",
        },
        {
            "currency": "USDT",
            "free": 120.0,
            "balance": 130.0,
            "used": 10.0,
            "est_stake": 130.0,
            "stake": "USDT",
        },
    ],
    "total": 630.0,
    "symbol": "USDT",
    "value": 630.0,
    "stake": "USDT",
    "note": "",
    "starting_capital": 1000.0,
    "starting_capital_ratio": 0.63,
}

PROFIT = {
    "profit_closed_coin": 25.0,
    "profit_all_coin": 42.5,
    "closed_trade_count": 10,
    "trade_count": 12,
    "winning_trades": 6,
    "losing_trades": 4,
    "winrate": 0.6,
    "profit_factor": 1.8,
    "max_drawdown": -0.1234,
    "best_pair": "BTC/USDT",
    "best_rate": 3.2,
}

COUNT = {"current": 3, "max": 2, "total_stake": 500.0}

OPEN_TRADES = [{"trade_id": 1, "pair": "BTC/USDT"}, {"trade_id": 2, "pair": "ETH/USDT"}]

TRADES_ENVELOPE = {
    "trades": [{"trade_id": 9, "pair": "BTC/USDT", "close_profit_abs": 4.2}],
    "trades_count": 1,
    "total_trades": 1,
    "offset": 0,
}

METRICS_ROUTES: dict[str, Any] = {
    "/api/v1/balance": BALANCE,
    "/api/v1/profit": PROFIT,
    "/api/v1/count": COUNT,
    "/api/v1/status": OPEN_TRADES,
}


def make_client(
    routes: dict[str, Any],
    *,
    requests: list[httpx.Request] | None = None,
    base_url: str = BASE_URL,
    **kwargs: Any,
) -> FreqtradeClient:
    """Return a client whose transport answers ``routes`` by request path.

    A route value is a JSON payload, an :class:`httpx.Response` or an exception
    to raise. Every request is appended to ``requests`` when it is given.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        route = routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"error": "not found"})
        if isinstance(route, Exception):
            raise route
        if isinstance(route, httpx.Response):
            return route
        if callable(route):
            return route(request)
        return httpx.Response(200, json=route)

    return FreqtradeClient(
        base_url=base_url,
        username=USERNAME,
        password=PASSWORD,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------
async def test_client_uses_basic_auth_on_the_api_root() -> None:
    requests: list[httpx.Request] = []
    client = make_client({"/api/v1/ping": {"status": "pong"}}, requests=requests)
    assert await client.ping() is True
    assert str(requests[0].url) == f"{BASE_URL}ping"
    expected = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    assert requests[0].headers["authorization"] == f"Basic {expected}"


async def test_client_accepts_a_base_url_without_trailing_slash() -> None:
    requests: list[httpx.Request] = []
    client = make_client(
        {"/api/v1/ping": {"status": "pong"}},
        requests=requests,
        base_url=f"{API_PORT_BASE_URL}/api/v1",
    )
    assert await client.ping() is True
    assert requests[0].url.path == "/api/v1/ping"


async def test_client_is_an_async_context_manager_that_closes_its_pool() -> None:
    client = make_client({"/api/v1/ping": {"status": "pong"}})
    async with client as entered:
        assert entered is client
        assert await client.ping() is True
        assert client._client.is_closed is False
    assert client._client.is_closed is True


async def test_aclose_is_idempotent() -> None:
    client = make_client({})
    await client.aclose()
    await client.aclose()
    assert client._client.is_closed is True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
async def test_ping_is_false_when_the_status_is_not_pong() -> None:
    client = make_client({"/api/v1/ping": {"status": "weird"}})
    assert await client.ping() is False


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("balance", BALANCE),
        ("profit", PROFIT),
        ("count", COUNT),
        ("show_config", {"stake_currency": "USDT"}),
        ("daily", {"data": [{"date": "2026-09-27", "abs_profit": 1.5}]}),
    ],
)
async def test_object_endpoints_return_the_payload(method: str, payload: dict[str, Any]) -> None:
    client = make_client({f"/api/v1/{method}": payload})
    assert await getattr(client, method)() == payload


async def test_status_returns_the_open_trades() -> None:
    client = make_client({"/api/v1/status": OPEN_TRADES})
    assert await client.status() == OPEN_TRADES


async def test_status_tolerates_an_envelope() -> None:
    client = make_client({"/api/v1/status": TRADES_ENVELOPE})
    assert await client.status() == TRADES_ENVELOPE["trades"]


async def test_performance_returns_its_rows() -> None:
    rows = [{"pair": "BTC/USDT", "profit_abs": 12.0}]
    client = make_client({"/api/v1/performance": rows})
    assert await client.performance() == rows


async def test_trades_unwraps_the_envelope() -> None:
    client = make_client({"/api/v1/trades": TRADES_ENVELOPE})
    assert await client.trades() == TRADES_ENVELOPE["trades"]


async def test_trades_tolerates_a_bare_list() -> None:
    client = make_client({"/api/v1/trades": TRADES_ENVELOPE["trades"]})
    assert await client.trades() == TRADES_ENVELOPE["trades"]


async def test_trades_passes_its_limit() -> None:
    requests: list[httpx.Request] = []
    client = make_client({"/api/v1/trades": TRADES_ENVELOPE}, requests=requests)
    await client.trades()
    await client.trades(limit=5)
    assert requests[0].url.params["limit"] == "50"
    assert requests[1].url.params["limit"] == "5"


# ---------------------------------------------------------------------------
# fetch_all
# ---------------------------------------------------------------------------
async def test_fetch_all_derives_every_metric() -> None:
    requests: list[httpx.Request] = []
    client = make_client(METRICS_ROUTES, requests=requests)
    metrics = await client.fetch_all()
    assert [request.url.path for request in requests] == [
        "/api/v1/balance",
        "/api/v1/profit",
        "/api/v1/count",
        "/api/v1/status",
    ]
    assert metrics.portfolio_value == 630.0
    assert metrics.cash == 120.0
    assert metrics.positions_value == 510.0
    assert metrics.profit_abs == 42.5
    assert metrics.realized_profit_abs == 25.0
    assert metrics.unrealized_profit_abs == 17.5
    # count["current"] is authoritative, not the length of the open-trade list.
    assert metrics.open_trades == 3
    assert metrics.closed_trades == 10
    assert metrics.win_rate == 0.6
    assert metrics.profit_factor == 1.8
    assert metrics.max_drawdown_pct == pytest.approx(12.34)
    assert metrics.best_pair == "BTC/USDT"
    assert metrics.starting_capital == 1000.0


async def test_fetch_all_survives_an_empty_payload() -> None:
    routes = {
        "/api/v1/balance": {},
        "/api/v1/profit": {},
        "/api/v1/count": {},
        "/api/v1/status": [],
    }
    metrics = await make_client(routes).fetch_all()
    assert metrics.portfolio_value == 0.0
    assert metrics.cash == 0.0
    assert metrics.positions_value == 0.0
    assert metrics.profit_abs == 0.0
    assert metrics.unrealized_profit_abs == 0.0
    assert metrics.open_trades == 0
    assert metrics.closed_trades == 0
    assert metrics.win_rate == 0.0
    assert metrics.profit_factor == 0.0
    assert metrics.max_drawdown_pct == 0.0
    assert metrics.best_pair is None
    assert metrics.starting_capital == 0.0


async def test_fetch_all_never_returns_a_non_finite_number() -> None:
    # Freqtrade itself answers with bare NaN/Infinity tokens through FastAPI, so
    # the payload is built with a raw body instead of httpx's strict ``json=``.
    body = json.dumps(
        {
            **PROFIT,
            "profit_factor": float("inf"),
            "winrate": float("nan"),
            "max_drawdown": float("-inf"),
        }
    )
    routes = {
        "/api/v1/balance": BALANCE,
        "/api/v1/profit": httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/json"}
        ),
        "/api/v1/count": COUNT,
        "/api/v1/status": OPEN_TRADES,
    }
    metrics = await make_client(routes).fetch_all()
    assert metrics.profit_factor == 0.0
    assert metrics.win_rate == 0.0
    assert metrics.max_drawdown_pct == 0.0
    # The payload must stay valid JSON for the dashboard.
    assert "Infinity" not in json.dumps(metrics.model_dump(), allow_nan=False)
    assert "NaN" not in json.dumps(metrics.model_dump(), allow_nan=False)


async def test_fetch_all_reports_cash_of_the_stake_currency_only() -> None:
    balance = {
        "currencies": [
            {"currency": "ETH", "free": 3.0},
            {"currency": "USDT", "free": 55.5},
        ],
        "total": 400.0,
        "stake": "USDT",
    }
    metrics = await make_client({**METRICS_ROUTES, "/api/v1/balance": balance}).fetch_all()
    assert metrics.cash == 55.5
    assert metrics.positions_value == 344.5


async def test_fetch_all_reports_no_cash_when_the_stake_currency_is_absent() -> None:
    balance = {"currencies": [{"currency": "BTC", "free": 1.0}], "total": 400.0, "stake": "USDT"}
    metrics = await make_client({**METRICS_ROUTES, "/api/v1/balance": balance}).fetch_all()
    assert metrics.cash == 0.0
    assert metrics.positions_value == 400.0


async def test_fetch_all_ignores_a_malformed_currency_row() -> None:
    balance = {
        "currencies": ["not-an-object", {"currency": "USDT", "free": 10.0}],
        "total": 100.0,
        "stake": "USDT",
    }
    metrics = await make_client({**METRICS_ROUTES, "/api/v1/balance": balance}).fetch_all()
    assert metrics.cash == 10.0
    assert metrics.positions_value == 90.0


async def test_fetch_all_falls_back_to_the_ratio_win_rate_scale() -> None:
    routes = {
        **METRICS_ROUTES,
        "/api/v1/profit": {**PROFIT, "winrate": 62.5, "max_drawdown": -12.5},
    }
    metrics = await make_client(routes).fetch_all()
    assert metrics.win_rate == pytest.approx(0.625)
    assert metrics.max_drawdown_pct == pytest.approx(12.5)


async def test_fetch_all_ignores_a_missing_best_pair() -> None:
    routes = {**METRICS_ROUTES, "/api/v1/profit": {**PROFIT, "best_pair": ""}}
    metrics = await make_client(routes).fetch_all()
    assert metrics.best_pair is None


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status_code", [401, 404, 500, 502])
async def test_http_error_status_raises_a_client_error(status_code: int) -> None:
    client = make_client({"/api/v1/balance": httpx.Response(status_code, json={"error": "nope"})})
    with pytest.raises(FreqtradeClientError) as caught:
        await client.balance()
    assert str(status_code) in str(caught.value)


async def test_connection_failure_raises_a_client_error() -> None:
    client = make_client({"/api/v1/balance": httpx.ConnectError("connection refused")})
    with pytest.raises(FreqtradeClientError):
        await client.balance()


async def test_timeout_raises_a_client_error() -> None:
    client = make_client({"/api/v1/balance": httpx.ReadTimeout("timed out")})
    with pytest.raises(FreqtradeClientError):
        await client.balance()


async def test_malformed_json_raises_a_client_error() -> None:
    response = httpx.Response(
        200, content=b"<html>not json</html>", headers={"content-type": "application/json"}
    )
    client = make_client({"/api/v1/balance": response})
    with pytest.raises(FreqtradeClientError):
        await client.balance()


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("balance", [1, 2, 3]),
        ("profit", "not-an-object"),
        ("status", {"detail": "unexpected"}),
        ("trades", 5),
        ("performance", {"detail": "unexpected"}),
    ],
)
async def test_unexpected_payload_shape_raises_a_client_error(method: str, payload: Any) -> None:
    path = f"/api/v1/{method}"
    client = make_client({path: payload})
    with pytest.raises(FreqtradeClientError):
        await getattr(client, method)()


async def test_client_error_is_a_runtime_error() -> None:
    assert issubclass(FreqtradeClientError, RuntimeError)


async def test_fetch_all_propagates_a_failed_read() -> None:
    routes = {**METRICS_ROUTES, "/api/v1/profit": httpx.Response(500, json={"error": "boom"})}
    with pytest.raises(FreqtradeClientError):
        await make_client(routes).fetch_all()


def test_make_client_helper_uses_a_mock_transport_only() -> None:
    # Guards the suite itself: every client built here talks to a MockTransport.
    transport: Callable[..., Any] = make_client({})._client._transport
    assert isinstance(transport, httpx.MockTransport)
