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

from trading_platform.engine.client import (
    DEFAULT_TIMEOUT_SECONDS,
    FreqtradeClient,
    FreqtradeClientError,
    normalise_daily_row,
    normalise_trade_row,
)

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
# Record readers: the read model the poller persists
# ---------------------------------------------------------------------------
DAILY_PAYLOAD: dict[str, Any] = {
    "data": [
        {
            "date": "2026-09-26",
            "abs_profit": 4.0,
            "rel_profit": 0.004,
            "starting_balance": 1000.0,
            "fiat_value": 4.0,
            "trade_count": 1,
        },
        {"date": "2026-09-27", "abs_profit": -1.5, "starting_balance": 1004.0, "trade_count": 2},
    ],
    "stake_currency": "USDT",
}

TRADES_PAYLOAD: dict[str, Any] = {
    "trades": [
        {
            "trade_id": 9,
            "pair": "BTC/USDT",
            "is_open": False,
            "open_date": "2026-09-26T08:00:00Z",
            "close_date": "2026-09-26T09:00:00Z",
            "amount": 0.5,
            "open_rate": 100.0,
            "close_rate": 110.0,
            "stake_amount": 50.0,
            "profit_abs": 5.0,
            "profit_ratio": 0.0123,
            "exit_reason": "roi",
        },
        {"trade_id": 10, "pair": "ETH/USDT", "profit_pct": 2.5},
    ],
    "trades_count": 2,
    "offset": 0,
    "total_trades": 2,
}

STATUS_ROWS: list[dict[str, Any]] = [
    {
        "trade_id": 21,
        "pair": "SOL/USDT",
        "open_date": "2026-09-27T11:30:00Z",
        "amount": 2.0,
        "open_rate": 25.0,
        "stake_amount": 50.0,
        "profit_abs": 1.5,
        "profit_ratio": 0.0123,
    },
    {"trade_id": 22, "pair": "ADA/USDT", "is_open": False},
]

#: The exact keys of one normalised trade row.
TRADE_ROW_KEYS = {
    "trade_id",
    "pair",
    "is_open",
    "open_date",
    "close_date",
    "amount",
    "open_rate",
    "close_rate",
    "stake_amount",
    "profit_abs",
    "profit_pct",
    "exit_reason",
}

#: The exact keys of one normalised daily row.
DAILY_ROW_KEYS = {"date", "abs_profit", "rel_profit", "starting_balance", "trade_count"}


async def test_daily_records_map_the_documented_envelope() -> None:
    records = await make_client({"/api/v1/daily": DAILY_PAYLOAD}).daily_records()

    assert len(records) == 2
    first, second = records
    # An unknown extra key of the API (fiat_value) is ignored.
    assert set(first) == DAILY_ROW_KEYS
    assert first == {
        "date": "2026-09-26",
        "abs_profit": 4.0,
        "rel_profit": 0.004,
        "starting_balance": 1000.0,
        "trade_count": 1,
    }
    # The API omitted rel_profit: it is derived from the starting balance.
    assert second["date"] == "2026-09-27"
    assert second["abs_profit"] == -1.5
    assert second["rel_profit"] == pytest.approx(-1.5 / 1004.0)
    assert second["starting_balance"] == 1004.0
    assert second["trade_count"] == 2


async def test_daily_records_tolerate_a_bare_list() -> None:
    client = make_client({"/api/v1/daily": DAILY_PAYLOAD["data"]})

    records = await client.daily_records()

    assert [row["date"] for row in records] == ["2026-09-26", "2026-09-27"]


async def test_daily_records_answer_empty_without_a_usable_data_list() -> None:
    missing = await make_client({"/api/v1/daily": {"stake_currency": "USDT"}}).daily_records()
    assert missing == []

    not_a_list = await make_client(
        {"/api/v1/daily": {"data": {"date": "2026-09-27"}}}
    ).daily_records()
    assert not_a_list == []

    neither_shape = await make_client({"/api/v1/daily": "not-a-payload"}).daily_records()
    assert neither_shape == []


async def test_daily_records_drop_an_unusable_row() -> None:
    payload = {
        "data": [
            "not-an-object",
            {"abs_profit": 1.0},
            {"date": "", "abs_profit": 1.0},
            {"date": "2026-09-27", "abs_profit": 1.0},
        ]
    }

    records = await make_client({"/api/v1/daily": payload}).daily_records()

    assert records == [
        {
            "date": "2026-09-27",
            "abs_profit": 1.0,
            "rel_profit": 0.0,
            "starting_balance": 0.0,
            "trade_count": 0,
        }
    ]


async def test_trade_records_map_the_documented_envelope() -> None:
    records = await make_client({"/api/v1/trades": TRADES_PAYLOAD}).trade_records()

    assert len(records) == 2
    first, second = records
    assert set(first) == TRADE_ROW_KEYS
    assert first["trade_id"] == 9
    assert first["pair"] == "BTC/USDT"
    assert first["is_open"] is False
    assert first["open_date"] == "2026-09-26T08:00:00Z"
    assert first["close_date"] == "2026-09-26T09:00:00Z"
    assert first["amount"] == 0.5
    assert first["open_rate"] == 100.0
    assert first["close_rate"] == 110.0
    assert first["stake_amount"] == 50.0
    assert first["profit_abs"] == 5.0
    # profit_ratio is a fraction; the row carries the percentage.
    assert first["profit_pct"] == pytest.approx(1.23)
    assert first["exit_reason"] == "roi"
    # A row carrying only profit_pct keeps it, and every missing key defaults.
    assert set(second) == TRADE_ROW_KEYS
    assert second == {
        "trade_id": 10,
        "pair": "ETH/USDT",
        "is_open": False,
        "open_date": None,
        "close_date": None,
        "amount": 0.0,
        "open_rate": 0.0,
        "close_rate": 0.0,
        "stake_amount": 0.0,
        "profit_abs": 0.0,
        "profit_pct": 2.5,
        "exit_reason": None,
    }


async def test_trade_records_tolerate_a_bare_list() -> None:
    client = make_client({"/api/v1/trades": TRADES_PAYLOAD["trades"]})

    records = await client.trade_records()

    assert [row["trade_id"] for row in records] == [9, 10]


async def test_trade_records_drop_a_row_without_a_usable_trade_id() -> None:
    payload = {
        "trades": [
            {"pair": "BTC/USDT"},
            {"trade_id": 0, "pair": "BTC/USDT"},
            {"trade_id": None},
            "not-an-object",
            {"trade_id": 7},
        ]
    }

    records = await make_client({"/api/v1/trades": payload}).trade_records()

    assert records == [
        {
            "trade_id": 7,
            "pair": "",
            "is_open": False,
            "open_date": None,
            "close_date": None,
            "amount": 0.0,
            "open_rate": 0.0,
            "close_rate": 0.0,
            "stake_amount": 0.0,
            "profit_abs": 0.0,
            "profit_pct": 0.0,
            "exit_reason": None,
        }
    ]


async def test_trade_records_collapse_non_finite_numbers() -> None:
    # Freqtrade emits bare NaN/Infinity tokens, so the body is built by hand.
    body = json.dumps(
        {
            "trades": [
                {
                    "trade_id": 9,
                    "profit_ratio": float("inf"),
                    "profit_abs": float("nan"),
                    "amount": float("-inf"),
                    "open_rate": float("nan"),
                }
            ]
        }
    )
    response = httpx.Response(
        200, content=body.encode(), headers={"content-type": "application/json"}
    )

    records = await make_client({"/api/v1/trades": response}).trade_records()

    assert records[0]["profit_pct"] == 0.0
    assert records[0]["profit_abs"] == 0.0
    assert records[0]["amount"] == 0.0
    assert records[0]["open_rate"] == 0.0


async def test_trade_records_pass_the_history_limit() -> None:
    requests: list[httpx.Request] = []
    client = make_client({"/api/v1/trades": TRADES_PAYLOAD}, requests=requests)

    await client.trade_records()
    await client.trade_records(limit=7)

    assert requests[0].url.params["limit"] == "50"
    assert requests[1].url.params["limit"] == "7"


async def test_open_trade_records_force_is_open_on_the_status_rows() -> None:
    requests: list[httpx.Request] = []
    client = make_client({"/api/v1/status": STATUS_ROWS}, requests=requests)

    records = await client.open_trade_records()

    assert requests[0].url.path == "/api/v1/status"
    assert [row["trade_id"] for row in records] == [21, 22]
    # /status only ever answers open positions, whatever the row claims.
    assert [row["is_open"] for row in records] == [True, True]
    assert records[0]["pair"] == "SOL/USDT"
    assert records[0]["profit_pct"] == pytest.approx(1.23)
    assert set(records[0]) == TRADE_ROW_KEYS


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("daily_records", "/api/v1/daily"),
        ("trade_records", "/api/v1/trades"),
        ("open_trade_records", "/api/v1/status"),
    ],
)
async def test_record_readers_still_raise_on_a_failed_read(method: str, path: str) -> None:
    http_error = make_client({path: httpx.Response(500, json={"error": "boom"})})
    with pytest.raises(FreqtradeClientError):
        await getattr(http_error, method)()

    connection_error = make_client({path: httpx.ConnectError("connection refused")})
    with pytest.raises(FreqtradeClientError):
        await getattr(connection_error, method)()

    timeout = make_client({path: httpx.ReadTimeout("timed out")})
    with pytest.raises(FreqtradeClientError):
        await getattr(timeout, method)()

    malformed = make_client(
        {
            path: httpx.Response(
                200, content=b"<html>not json</html>", headers={"content-type": "application/json"}
            )
        }
    )
    with pytest.raises(FreqtradeClientError):
        await getattr(malformed, method)()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"trade_id": "not-a-number"},
        {"trade_id": None},
        {"trade_id": float("nan")},
        {"trade_id": float("inf")},
        {"trade_id": 0},
        "not-a-mapping",
        [1, 2, 3],
        5,
    ],
)
def test_normalise_trade_row_never_raises(raw: Any) -> None:
    assert normalise_trade_row(raw) is None


def test_normalise_trade_row_pins_every_key() -> None:
    row = normalise_trade_row({"trade_id": 3, "is_open": "yes", "profit_ratio": "bad"})

    assert row is not None
    assert set(row) == TRADE_ROW_KEYS
    assert row["trade_id"] == 3
    # ``is_open`` is only honoured when it really is a boolean.
    assert row["is_open"] is False
    assert row["profit_pct"] == 0.0


def test_normalise_trade_row_honours_the_open_default() -> None:
    row = normalise_trade_row({"trade_id": 3}, default_open=True)

    assert row is not None
    assert row["is_open"] is True


@pytest.mark.parametrize(
    "raw",
    [None, {}, {"date": ""}, {"date": None}, {"abs_profit": 1.0}, "not-a-mapping", [1], 7],
)
def test_normalise_daily_row_never_raises(raw: Any) -> None:
    assert normalise_daily_row(raw) is None


def test_normalise_daily_row_keeps_a_zero_relative_profit() -> None:
    row = normalise_daily_row({"date": "2026-09-27", "abs_profit": 5.0, "rel_profit": 0.0})

    assert row is not None
    assert set(row) == DAILY_ROW_KEYS
    assert row["rel_profit"] == 0.0
    assert row["starting_balance"] == 0.0
    assert row["trade_count"] == 0


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
    # ``max_drawdown`` is a ratio in freqtrade 2026.8 and is stored as one: the
    # API publishes every ``*_pct`` field as a 0..1 ratio.
    assert metrics.max_drawdown_pct == pytest.approx(0.1234)
    assert metrics.best_pair == "BTC/USDT"
    assert metrics.starting_capital == 1000.0


async def test_fetch_all_measures_the_bot_equity_not_the_exchange_wallet() -> None:
    """A live bot is measured on its own funds, not on the operator's account.

    ``/balance`` answers two totals: ``total`` is the whole exchange wallet,
    ``total_bot`` is what the worker manages. A 250 USDT bot on a 5,000 USDT
    account would report roughly +1,900 % profit against a 250 USDT profile if
    the wallet total were used, so ``total_bot`` is the number that is kept.
    """
    balance = {
        "currencies": [{"currency": "USDT", "free": 250.0, "balance": 250.0}],
        "total": 5000.0,
        "total_bot": 250.0,
        "stake": "USDT",
    }
    metrics = await make_client({**METRICS_ROUTES, "/api/v1/balance": balance}).fetch_all()
    assert metrics.portfolio_value == 250.0
    assert metrics.portfolio_value != 5000.0
    assert metrics.cash == 250.0
    assert metrics.positions_value == 0.0


async def test_fetch_all_keeps_the_bot_equity_of_a_dry_run_wallet() -> None:
    """A dry-run wallet is the bot's wallet: both totals carry the same number."""
    balance = {**BALANCE, "total_bot": BALANCE["total"]}
    metrics = await make_client({**METRICS_ROUTES, "/api/v1/balance": balance}).fetch_all()
    assert metrics.portfolio_value == 630.0


async def test_fetch_all_falls_back_to_the_wallet_total_without_a_bot_total() -> None:
    """A payload that predates ``total_bot`` keeps its documented reading."""
    metrics = await make_client(METRICS_ROUTES).fetch_all()
    assert "total_bot" not in BALANCE
    assert metrics.portfolio_value == 630.0

    null_bot = await make_client(
        {**METRICS_ROUTES, "/api/v1/balance": {**BALANCE, "total_bot": None}}
    ).fetch_all()
    assert null_bot.portfolio_value == 630.0


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
    # No ``profit_factor`` key at all: "not measurable", never the 0.0 that
    # means every trade lost.
    assert metrics.profit_factor is None
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
    # ``Infinity`` is not JSON: it reaches the client as a non-finite number and
    # becomes "not measurable", while the drawdown magnitude becomes 0.0.
    assert metrics.profit_factor is None
    assert metrics.win_rate == 0.0
    assert metrics.max_drawdown_pct == 0.0
    # The payload must stay valid JSON for the dashboard.
    assert "Infinity" not in json.dumps(metrics.model_dump(), allow_nan=False)
    assert "NaN" not in json.dumps(metrics.model_dump(), allow_nan=False)


async def test_fetch_all_keeps_the_drawdown_ratio_freqtrade_published() -> None:
    """freqtrade 2026.8 answers ``max_drawdown`` as a ratio; nothing rescales it."""
    routes = {**METRICS_ROUTES, "/api/v1/profit": {**PROFIT, "max_drawdown": -0.0078}}
    metrics = await make_client(routes).fetch_all()
    assert metrics.max_drawdown_pct == pytest.approx(0.0078)


async def test_fetch_all_stores_zero_drawdown_without_the_key() -> None:
    profit = {key: value for key, value in PROFIT.items() if key != "max_drawdown"}
    metrics = await make_client({**METRICS_ROUTES, "/api/v1/profit": profit}).fetch_all()
    assert metrics.max_drawdown_pct == 0.0


async def test_fetch_all_reports_a_null_profit_factor_as_none() -> None:
    """Freqtrade serialises the ``Infinity`` of a flawless record as JSON ``null``."""
    routes = {**METRICS_ROUTES, "/api/v1/profit": {**PROFIT, "profit_factor": None}}
    metrics = await make_client(routes).fetch_all()
    assert metrics.profit_factor is None


async def test_fetch_all_reports_a_missing_profit_factor_as_none() -> None:
    profit = {key: value for key, value in PROFIT.items() if key != "profit_factor"}
    metrics = await make_client({**METRICS_ROUTES, "/api/v1/profit": profit}).fetch_all()
    assert metrics.profit_factor is None


async def test_fetch_all_keeps_a_measured_profit_factor() -> None:
    """A real number -- including the honest ``0.0`` of an all-losses record."""
    measured = await make_client(
        {**METRICS_ROUTES, "/api/v1/profit": {**PROFIT, "profit_factor": 1.6839}}
    ).fetch_all()
    zero = await make_client(
        {**METRICS_ROUTES, "/api/v1/profit": {**PROFIT, "profit_factor": 0.0}}
    ).fetch_all()
    assert measured.profit_factor == pytest.approx(1.6839)
    assert zero.profit_factor == 0.0


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


def test_client_default_timeout_is_above_the_measured_worst_case() -> None:
    """The default timeout must outlast the slowest read measured in production.

    ``GET /api/v1/balance`` answered in 19.74 s, 19.77 s and 11.94 s while 22
    workers shared one CoinGecko rate limit. A timeout is a read failure, and
    enough read failures make the supervisor restart a worker that was only
    waiting, so the default stays well above 30 s.
    """
    assert DEFAULT_TIMEOUT_SECONDS >= 30.0
    client = FreqtradeClient(base_url=BASE_URL, username=USERNAME, password=PASSWORD)
    assert client._client.timeout == httpx.Timeout(DEFAULT_TIMEOUT_SECONDS)


def test_make_client_helper_uses_a_mock_transport_only() -> None:
    # Guards the suite itself: every client built here talks to a MockTransport.
    transport: Callable[..., Any] = make_client({})._client._transport
    assert isinstance(transport, httpx.MockTransport)
