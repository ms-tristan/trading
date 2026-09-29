"""Tests of the snapshot poller: minute rounding, idempotence, honest gaps.

The poller is driven through its real supervisor, whose REST client is the real
:class:`~trading_platform.engine.client.FreqtradeClient` sitting on an
:class:`httpx.MockTransport`: the payloads are the documented Freqtrade ones, no
socket is opened, and no ``freqtrade`` process is ever started (the launcher is a
recording double). The state store is a real SQLite file under ``tmp_path``,
because what the poller writes there -- one row per profile and minute, plus the
monitoring read model of the trades and the days of every running worker -- is
exactly what these tests pin down.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from trading_platform.config import PlatformSettings
from trading_platform.engine.client import FreqtradeClient
from trading_platform.engine.poller import (
    MINIMUM_INTERVAL_SECONDS,
    SnapshotPoller,
    round_to_minute,
)
from trading_platform.engine.supervisor import (
    RESTART_BACKOFF_SECONDS,
    UNHEALTHY_PING_THRESHOLD,
    UNHEALTHY_THRESHOLD,
    WORKER_STARTUP_GRACE_SECONDS,
    Supervisor,
)
from trading_platform.models import ProfileConfig, format_ts
from trading_platform.profiles.store import StateStore

#: The fake clock starts here; the minute boundary of the snapshots follows.
START_TIME = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)

#: Initial capital of the profiles under test.
INITIAL_CAPITAL = 1000.0

BALANCE = {
    "currencies": [
        {"currency": "BTC", "free": 0.01, "balance": 0.01, "used": 0.0, "stake": "USDT"},
        {"currency": "USDT", "free": 120.0, "balance": 130.0, "used": 10.0, "stake": "USDT"},
    ],
    "total": 630.0,
    "symbol": "USDT",
    "value": 630.0,
    "stake": "USDT",
    "note": "",
    "starting_capital": 1000.0,
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
}

COUNT = {"current": 3, "max": 2, "total_stake": 500.0}

#: ``GET /trades``: a closed trade and the open trade the worker just reported as
#: closed as well (Freqtrade answers both lists newest first).
CLOSED_TRADES: list[dict[str, Any]] = [
    {
        "trade_id": 11,
        "pair": "BTC/USDT",
        "is_open": False,
        "open_date": "2026-09-26T08:00:00Z",
        "close_date": "2026-09-26T09:30:00Z",
        "amount": 0.5,
        "open_rate": 100.0,
        "close_rate": 110.0,
        "stake_amount": 50.0,
        "profit_abs": 5.0,
        "profit_ratio": 0.1,
        "exit_reason": "roi",
    },
    {
        "trade_id": 12,
        "pair": "ETH/USDT",
        "is_open": False,
        "open_date": "2026-09-27T10:00:00Z",
        "close_date": "2026-09-27T11:00:00Z",
        "amount": 1.0,
        "open_rate": 50.0,
        "close_rate": 55.0,
        "stake_amount": 50.0,
        "profit_abs": 5.0,
        "profit_ratio": 0.1,
        "exit_reason": "roi",
    },
]

#: ``GET /status``: the position that is still open (trade 12 is *not* in it; it
#: appears there only in the "flipped in place" test).
OPEN_TRADES: list[dict[str, Any]] = [
    {
        "trade_id": 13,
        "pair": "SOL/USDT",
        "is_open": True,
        "open_date": "2026-09-27T11:30:00Z",
        "amount": 2.0,
        "open_rate": 25.0,
        "stake_amount": 50.0,
        "profit_abs": 1.5,
        "profit_ratio": 0.03,
    }
]

#: ``GET /daily`` rows; the second one carries no ``rel_profit`` on purpose, so
#: the derived ratio is exercised as well.
DAILY_ROWS: list[dict[str, Any]] = [
    {
        "date": "2026-09-26",
        "abs_profit": 4.0,
        "rel_profit": 0.004,
        "starting_balance": 1000.0,
        "fiat_value": 4.0,
        "trade_count": 1,
    },
    {
        "date": "2026-09-27",
        "abs_profit": 3.0,
        "starting_balance": 1000.0,
        "fiat_value": 3.0,
        "trade_count": 2,
    },
]

DAILY_ENVELOPE = {"data": DAILY_ROWS, "stake_currency": "USDT"}

#: ``profit_pct`` of one successful read, computed against the initial capital.
EXPECTED_PROFIT_PCT = (630.0 - INITIAL_CAPITAL) / INITIAL_CAPITAL

#: Every state store a test opened, so the fixture below can close them all.
_OPEN_STORES: list[StateStore] = []


@pytest.fixture(autouse=True)
def _close_state_stores() -> Iterator[None]:
    """Close every state store a test opened, keeping the suite warning-free."""
    yield
    while _OPEN_STORES:
        _OPEN_STORES.pop().close()


# ---------------------------------------------------------------------------
# Doubles and harness
# ---------------------------------------------------------------------------
class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, start: datetime = START_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        """Move the clock forward and return the new instant."""
        self.now += timedelta(seconds=seconds)
        return self.now


class FakeProcess:
    """A child handle that never exists for real."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        """Return the exit status, or ``None`` while the fake child is alive."""
        return self.returncode

    def terminate(self) -> None:
        """Exit with Freqtrade's SIGTERM status."""
        self.returncode = 130

    def kill(self) -> None:
        """Exit as if killed."""
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        """Return the exit status without blocking."""
        return 0 if self.returncode is None else self.returncode


class RecordingLauncher:
    """A ``ProcessLauncher`` double that records the command lines it is given."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self._pid = 7000

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
    ) -> FakeProcess:
        """Record the command line and return a fake child."""
        self.commands.append(list(argv))
        self._pid += 1
        return FakeProcess(self._pid)

    def terminate(self, process: FakeProcess, *, grace_seconds: float = 0.0) -> None:
        """Terminate the fake child."""
        process.terminate()


class ApiStub:
    """The Freqtrade REST payloads of one profile, served without a socket.

    Every endpoint the supervisor reads is answered in its real shape: the
    metric endpoints (``/balance``, ``/profit``, ``/count``, ``/status``) in the
    order ``fetch_all`` reads them, then the history endpoints ``/daily`` and
    ``/trades``. ``trades``, ``open_trades`` and ``daily`` are attributes, so a
    test can move a trade from ``/status`` to ``/trades`` (what Freqtrade does
    when a position closes) or change a day between two ticks.
    """

    def __init__(self) -> None:
        self.failures = 0
        self.daily_failures = 0
        #: Whether ``GET /ping`` answers ``pong``. The supervisor restarts a
        #: worker only once its reads *and* its pings have failed enough times.
        self.ping_answers = True
        self.requests: list[str] = []
        self.trades: list[dict[str, Any]] = [dict(row) for row in CLOSED_TRADES]
        self.open_trades: list[dict[str, Any]] = [dict(row) for row in OPEN_TRADES]
        self.daily: list[dict[str, Any]] = [dict(row) for row in DAILY_ROWS]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer every endpoint of one poll cycle, or fail as told to."""
        path = request.url.path
        self.requests.append(path)
        if path.endswith("/ping"):
            if not self.ping_answers:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"status": "pong"})
        if self.failures > 0:
            self.failures -= 1
            raise httpx.ConnectError("connection refused", request=request)
        if path.endswith("/balance"):
            return httpx.Response(200, json=BALANCE)
        if path.endswith("/profit"):
            return httpx.Response(200, json=PROFIT)
        if path.endswith("/count"):
            return httpx.Response(200, json=COUNT)
        if path.endswith("/status"):
            return httpx.Response(200, json=self.open_trades)
        if path.endswith("/daily"):
            if self.daily_failures > 0:
                self.daily_failures -= 1
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"data": self.daily, "stake_currency": "USDT"})
        if path.endswith("/trades"):
            return httpx.Response(
                200,
                json={
                    "trades": self.trades,
                    "trades_count": len(self.trades),
                    "offset": 0,
                    "total_trades": len(self.trades),
                },
            )
        return httpx.Response(404, json={"error": "not found"})


def client_factory(stub: ApiStub) -> Any:
    """Return a client factory that builds real clients on the stub transport."""

    def factory(*, base_url: str, username: str, password: str) -> FreqtradeClient:
        return FreqtradeClient(
            base_url=base_url,
            username=username,
            password=password,
            transport=httpx.MockTransport(stub),
        )

    return factory


@dataclass
class Harness:
    """One poller wired on ``tmp_path`` with every external effect faked."""

    poller: SnapshotPoller
    supervisor: Supervisor
    store: StateStore
    state_dir: Path
    clock: FakeClock
    stub: ApiStub
    launcher: RecordingLauncher = field(default_factory=RecordingLauncher)

    def snapshots(self, profile_id: str) -> list[Any]:
        """Return the stored snapshots of a profile, oldest first."""
        return self.store.list_snapshots(profile_id)

    def trades(self, profile_id: str) -> list[Any]:
        """Return the stored trades of a profile, newest ``open_date`` first."""
        return self.store.trade_records(profile_id)

    def daily(self, profile_id: str) -> list[Any]:
        """Return the stored days of a profile, newest first."""
        return self.store.daily_records(profile_id)

    def raw_trade_rows(self, profile_id: str) -> list[tuple[Any, ...]]:
        """Return the raw ``(trade_id, is_open)`` columns, ordered by trade id."""
        with sqlite3.connect(self.store.path) as connection:
            return list(
                connection.execute(
                    "SELECT trade_id, is_open FROM profile_trades"
                    " WHERE profile_id = ? ORDER BY trade_id",
                    (profile_id,),
                )
            )


def profile_config(profile_id: str, **overrides: Any) -> ProfileConfig:
    """Build a paper catalogue profile with ``overrides`` applied."""
    document: dict[str, Any] = {
        "id": profile_id,
        "name": profile_id.title(),
        "strategy": "basic",
        "timeframe": "1h",
        "mode": "paper",
        "exchange": "binance",
        "pairs": ["BTC/USDT"],
        "initial_capital": INITIAL_CAPITAL,
        "max_open_trades": 2,
        "priority": 100,
        "enabled": True,
    }
    document.update(overrides)
    return ProfileConfig.model_validate(document)


def write_documents(config_dir: Path, profiles: Sequence[ProfileConfig]) -> None:
    """Write the three configuration documents of a temporary deployment."""
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {"profiles": [profile.model_dump() for profile in profiles]}
    (config_dir / "profiles.json").write_text(json.dumps(payload), encoding="utf-8")
    (config_dir / "platform.json").write_text("{}", encoding="utf-8")
    strategies = {"strategies": [{"id": "basic", "class_name": "BasicStrategy"}]}
    (config_dir / "strategies.json").write_text(json.dumps(strategies), encoding="utf-8")


def make_harness(
    tmp_path: Path,
    *,
    profiles: Sequence[ProfileConfig] = (),
    settings: PlatformSettings | None = None,
) -> Harness:
    """Build a poller whose supervisor never spawns and never opens a socket."""
    state_dir = tmp_path / "realtime"
    state_dir.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / "config"
    write_documents(config_dir, profiles)
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)

    store = StateStore(state_dir / "state.db")
    _OPEN_STORES.append(store)
    store.bootstrap()
    clock = FakeClock()
    stub = ApiStub()
    launcher = RecordingLauncher()
    supervisor = Supervisor(
        store=store,
        settings=settings or PlatformSettings(),
        state_dir=state_dir,
        config_dir=config_dir,
        strategies_dir=strategies_dir,
        launcher=launcher,
        client_factory=client_factory(stub),
        env={"TB_FREQTRADE_BIN": "freqtrade"},
        clock=clock,
    )
    return Harness(
        poller=SnapshotPoller(supervisor),
        supervisor=supervisor,
        store=store,
        state_dir=state_dir,
        clock=clock,
        stub=stub,
        launcher=launcher,
    )


# ---------------------------------------------------------------------------
# Minute rounding
# ---------------------------------------------------------------------------
def test_round_to_minute_zeroes_seconds_and_microseconds() -> None:
    moment = datetime(2026, 9, 27, 17, 31, 45, 123456, tzinfo=UTC)
    assert round_to_minute(moment) == datetime(2026, 9, 27, 17, 31, tzinfo=UTC)
    assert round_to_minute(moment).second == 0
    assert round_to_minute(moment).microsecond == 0


def test_round_to_minute_reads_a_naive_timestamp_as_utc() -> None:
    naive = datetime(2026, 9, 27, 17, 31, 45)
    assert round_to_minute(naive) == datetime(2026, 9, 27, 17, 31, tzinfo=UTC)


def test_round_to_minute_converts_to_utc_before_rounding() -> None:
    madrid = timezone(timedelta(hours=2))
    local = datetime(2026, 9, 27, 19, 31, 45, tzinfo=madrid)
    assert round_to_minute(local) == datetime(2026, 9, 27, 17, 31, tzinfo=UTC)


# ---------------------------------------------------------------------------
# One snapshot per running profile and minute
# ---------------------------------------------------------------------------
async def test_tick_writes_one_snapshot_per_running_profile(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    await harness.supervisor.start()

    await harness.poller.tick()

    snapshots = harness.snapshots("alpha")
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.ts == format_ts(round_to_minute(START_TIME))
    assert snapshot.portfolio_value == 630.0
    assert snapshot.cash == 120.0
    assert snapshot.positions_value == 510.0
    assert snapshot.profit_abs == 42.5
    assert snapshot.profit_pct == EXPECTED_PROFIT_PCT
    assert snapshot.realized_profit_abs == 25.0
    assert snapshot.unrealized_profit_abs == 17.5
    assert snapshot.open_trades == 3
    assert snapshot.closed_trades == 10
    assert snapshot.win_rate == 0.6
    assert snapshot.profit_factor == 1.8
    assert snapshot.max_drawdown_pct == 12.34
    assert snapshot.healthy is True
    assert harness.poller.snapshots_written == 1


async def test_tick_reads_the_documented_endpoints(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    await harness.supervisor.start()

    await harness.poller.tick()

    # One poll cycle: the metrics first, then the history of the worker.
    assert harness.stub.requests == [
        "/api/v1/balance",
        "/api/v1/profit",
        "/api/v1/count",
        "/api/v1/status",
        "/api/v1/daily",
        "/api/v1/trades",
        "/api/v1/status",
    ]


async def test_the_minute_is_idempotent(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    await harness.supervisor.start()

    await harness.poller.tick()
    harness.clock.advance(20)
    await harness.poller.tick()

    assert len(harness.snapshots("alpha")) == 1

    harness.clock.advance(40)
    await harness.poller.tick()

    snapshots = harness.snapshots("alpha")
    assert len(snapshots) == 2
    assert [snapshot.ts for snapshot in snapshots] == [
        "2026-09-27T12:00:00Z",
        "2026-09-27T12:01:00Z",
    ]


async def test_two_running_profiles_get_one_snapshot_each(tmp_path: Path) -> None:
    profiles = [profile_config("alpha"), profile_config("beta")]
    harness = make_harness(tmp_path, profiles=profiles)
    await harness.supervisor.start()

    await harness.poller.tick()

    assert len(harness.snapshots("alpha")) == 1
    assert len(harness.snapshots("beta")) == 1
    assert harness.poller.snapshots_written == 2


# ---------------------------------------------------------------------------
# The monitoring read model: the trades and the days of a running worker
# ---------------------------------------------------------------------------
async def test_tick_persists_the_trades_and_the_days_of_a_running_worker(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    await harness.supervisor.start()

    await harness.poller.tick()

    trades = harness.trades("alpha")
    assert [trade.trade_id for trade in trades] == [13, 12, 11]
    open_trade = trades[0]
    assert open_trade.profile_id == "alpha"
    assert open_trade.pair == "SOL/USDT"
    assert open_trade.is_open is True
    assert open_trade.open_date == "2026-09-27T11:30:00Z"
    assert open_trade.close_date is None
    assert open_trade.amount == 2.0
    assert open_trade.open_rate == 25.0
    assert open_trade.close_rate == 0.0
    assert open_trade.stake_amount == 50.0
    assert open_trade.profit_abs == 1.5
    # The API reports profit_ratio as a fraction; the store keeps the percentage.
    assert open_trade.profit_pct == pytest.approx(3.0)
    assert open_trade.exit_reason is None
    # ``updated_at`` is empty in the payload and stamped by the store.
    assert open_trade.updated_at

    closed = trades[1]
    assert closed.trade_id == 12
    assert closed.is_open is False
    assert closed.close_date == "2026-09-27T11:00:00Z"
    assert closed.close_rate == 55.0
    assert closed.exit_reason == "roi"
    assert closed.profit_pct == pytest.approx(10.0)

    days = harness.daily("alpha")
    assert [day.date for day in days] == ["2026-09-27", "2026-09-26"]
    assert days[0].abs_profit == 3.0
    # The API omitted rel_profit: it is derived from the starting balance.
    assert days[0].rel_profit == pytest.approx(0.003)
    assert days[0].starting_balance == 1000.0
    assert days[0].trade_count == 2
    assert days[1].rel_profit == pytest.approx(0.004)

    assert harness.poller.trade_rows_written == 3
    assert harness.poller.daily_rows_written == 2


async def test_a_trade_that_closes_is_flipped_in_place(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    await harness.supervisor.start()
    await harness.poller.tick()
    assert harness.raw_trade_rows("alpha") == [(11, 0), (12, 0), (13, 1)]

    # Freqtrade answers the trade in /trades once it closed and drops it from
    # /status, which is exactly how a position moves from one list to the other.
    harness.stub.trades = [
        {
            **OPEN_TRADES[0],
            "is_open": False,
            "close_date": "2026-09-27T12:30:00Z",
            "close_rate": 26.0,
            "profit_ratio": 0.04,
            "exit_reason": "roi",
        },
        *harness.stub.trades,
    ]
    harness.stub.open_trades = []
    harness.clock.advance(60)
    await harness.poller.tick()

    # One row per trade id, and the open row became closed in place.
    assert harness.raw_trade_rows("alpha") == [(11, 0), (12, 0), (13, 0)]
    flipped = next(trade for trade in harness.trades("alpha") if trade.trade_id == 13)
    assert flipped.is_open is False
    assert flipped.close_date == "2026-09-27T12:30:00Z"
    assert flipped.close_rate == 26.0
    assert flipped.exit_reason == "roi"
    assert flipped.profit_pct == pytest.approx(4.0)
    assert harness.poller.trade_rows_written == 6


async def test_the_daily_rows_are_upserted_on_the_date(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    await harness.supervisor.start()
    await harness.poller.tick()
    assert [day.date for day in harness.daily("alpha")] == ["2026-09-27", "2026-09-26"]

    # The day is still unfolding: the same date comes back with new numbers.
    harness.stub.daily = [
        DAILY_ROWS[0],
        {**DAILY_ROWS[1], "abs_profit": 9.5, "rel_profit": 0.0095, "trade_count": 4},
    ]
    harness.clock.advance(60)
    await harness.poller.tick()

    days = {day.date: day for day in harness.daily("alpha")}
    assert sorted(days) == ["2026-09-26", "2026-09-27"]
    assert days["2026-09-27"].abs_profit == 9.5
    assert days["2026-09-27"].rel_profit == pytest.approx(0.0095)
    assert days["2026-09-27"].trade_count == 4
    assert days["2026-09-26"].abs_profit == 4.0
    assert harness.poller.daily_rows_written == 4


async def test_a_stopped_profile_keeps_its_persisted_rows(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    await harness.supervisor.start()
    await harness.poller.tick()
    assert len(harness.trades("alpha")) == 3

    harness.supervisor.stop_profile("alpha")
    # Even an empty answer from a worker nobody reads must not clear the rows.
    harness.stub.trades = []
    harness.stub.open_trades = []
    harness.stub.daily = []
    harness.clock.advance(60)
    await harness.poller.tick()

    assert not harness.supervisor.is_running("alpha")
    assert [trade.trade_id for trade in harness.trades("alpha")] == [13, 12, 11]
    assert [day.date for day in harness.daily("alpha")] == ["2026-09-27", "2026-09-26"]
    # A profile that is not running is skipped entirely: no read, no delete.
    assert harness.stub.requests.count("/api/v1/daily") == 1
    assert harness.poller.trade_rows_written == 3
    assert harness.poller.daily_rows_written == 2


async def test_a_failing_daily_read_writes_no_row_and_leaves_the_worker_healthy(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()
    harness.stub.daily_failures = 1

    await harness.poller.tick()

    assert harness.trades("alpha") == []
    assert harness.daily("alpha") == []
    assert harness.poller.trade_rows_written == 0
    assert harness.poller.daily_rows_written == 0
    # A history hiccup is not a read failure: the metrics were read, so the
    # snapshot is written, and the worker is neither restarted nor counted.
    assert len(harness.snapshots("alpha")) == 1
    assert supervisor.is_running("alpha")
    assert supervisor.healthy_count() == 1
    assert supervisor.status()["profiles_healthy"] == 1
    assert supervisor._health_for("alpha").failures == 0
    assert len(harness.launcher.commands) == 1


async def test_a_tick_with_an_empty_fleet_writes_nothing(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)

    await harness.poller.tick()

    assert harness.stub.requests == []
    assert harness.poller.snapshots_written == 0
    assert harness.poller.trade_rows_written == 0
    assert harness.poller.daily_rows_written == 0


# ---------------------------------------------------------------------------
# Failures and stopped profiles
# ---------------------------------------------------------------------------
async def test_a_failed_read_writes_no_snapshot(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()
    await harness.poller.tick()
    assert len(harness.snapshots("alpha")) == 1

    harness.stub.failures = 2
    for _ in range(2):
        harness.clock.advance(60)
        await harness.poller.tick()

    assert len(harness.snapshots("alpha")) == 1
    assert supervisor.is_running("alpha")
    assert supervisor.metrics_for("alpha") is None


async def test_a_proven_dead_worker_stops_and_leaves_a_gap(tmp_path: Path) -> None:
    """A worker whose reads and pings both fail long enough is restarted.

    Failing reads alone no longer stops a worker -- that was the crash loop --
    so the whole budget is spent here: past the startup grace, the failure
    threshold and the ping threshold.
    """
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()
    await harness.poller.tick()

    harness.clock.advance(WORKER_STARTUP_GRACE_SECONDS)
    harness.stub.ping_answers = False
    harness.stub.failures = UNHEALTHY_THRESHOLD + UNHEALTHY_PING_THRESHOLD
    stopped_at = None
    for _ in range(UNHEALTHY_THRESHOLD + UNHEALTHY_PING_THRESHOLD):
        harness.clock.advance(60)
        await harness.poller.tick()
        if not supervisor.is_running("alpha"):
            # The snapshot of the transition is stamped with the top of the
            # minute the poller ran in, not with the sub-minute clock.
            stopped_at = format_ts(harness.clock.now.replace(second=0, microsecond=0))
            break
    else:
        raise AssertionError("the worker survived its whole failure budget")

    work = harness.store.get_profile("alpha")
    assert work is not None
    assert work.state_reason == f"restart_backoff: retrying in {RESTART_BACKOFF_SECONDS[0]}s"

    snapshots = harness.snapshots("alpha")
    # The failing minutes add no measurement; only the change of state carries
    # the last known values into the minute the worker stopped.
    assert [snapshot.ts for snapshot in snapshots] == [
        "2026-09-27T12:00:00Z",
        stopped_at,
    ]
    assert snapshots[0].healthy is True
    assert snapshots[1].healthy is False
    assert snapshots[1].portfolio_value == snapshots[0].portfolio_value
    assert snapshots[1].profit_pct == snapshots[0].profit_pct


async def test_a_disabled_profile_gets_no_snapshot(tmp_path: Path) -> None:
    profiles = [profile_config("alpha", enabled=False)]
    harness = make_harness(tmp_path, profiles=profiles)
    supervisor = harness.supervisor
    supervisor.bootstrap()

    assert not supervisor.is_running("alpha")
    assert harness.launcher.commands == []

    await harness.poller.tick()
    harness.clock.advance(60)
    await harness.poller.tick()

    assert harness.snapshots("alpha") == []
    assert harness.poller.snapshots_written == 0


async def test_a_stopped_profile_writes_a_snapshot_only_on_the_transition(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()
    await harness.poller.tick()
    assert len(harness.snapshots("alpha")) == 1

    supervisor.stop_profile("alpha")
    harness.clock.advance(60)
    await harness.poller.tick()

    snapshots = harness.snapshots("alpha")
    assert len(snapshots) == 2
    assert snapshots[1].ts == "2026-09-27T12:01:00Z"
    assert snapshots[1].healthy is False
    assert snapshots[1].portfolio_value == snapshots[0].portfolio_value

    for _ in range(3):
        harness.clock.advance(60)
        await harness.poller.tick()

    assert len(harness.snapshots("alpha")) == 2
    assert harness.poller.snapshots_written == 2


async def test_a_transition_without_history_writes_nothing(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()
    harness.clock.advance(WORKER_STARTUP_GRACE_SECONDS)
    harness.stub.ping_answers = False
    harness.stub.failures = 99

    await harness.poller.tick()
    for _ in range(UNHEALTHY_THRESHOLD + UNHEALTHY_PING_THRESHOLD):
        harness.clock.advance(60)
        await harness.poller.tick()
        if not supervisor.is_running("alpha"):
            break

    assert not supervisor.is_running("alpha")
    assert supervisor.metrics_for("alpha") is None
    # The worker never answered, so there is no value to carry into the minute
    # its state changed: the poller writes nothing rather than inventing one.
    assert harness.snapshots("alpha") == []
    assert harness.poller.snapshots_written == 0


async def test_a_profile_the_health_policy_stopped_is_recorded_once(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    supervisor = harness.supervisor
    await supervisor.start()
    await harness.poller.tick()
    assert len(harness.snapshots("alpha")) == 1

    harness.clock.advance(WORKER_STARTUP_GRACE_SECONDS)
    harness.stub.ping_answers = False
    harness.stub.failures = 99
    for _ in range(UNHEALTHY_THRESHOLD + UNHEALTHY_PING_THRESHOLD):
        harness.clock.advance(60)
        await harness.poller.tick()
        if not supervisor.is_running("alpha"):
            break

    assert not supervisor.is_running("alpha")
    snapshots = harness.snapshots("alpha")
    assert len(snapshots) == 2
    assert snapshots[1].healthy is False

    harness.clock.advance(60)
    await harness.poller.tick()
    assert len(harness.snapshots("alpha")) == 2


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
async def test_run_forever_ticks_and_stops(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        profiles=[profile_config("alpha")],
        settings=PlatformSettings(snapshot_interval_seconds=0),
    )
    supervisor = harness.supervisor
    await supervisor.start()
    assert harness.poller.interval() == MINIMUM_INTERVAL_SECONDS

    task = asyncio.create_task(harness.poller.run_forever())
    await asyncio.sleep(0.12)
    assert harness.poller.running is True
    harness.poller.stop()
    await asyncio.wait_for(task, timeout=2)

    assert harness.poller.running is False
    assert len(harness.snapshots("alpha")) == 1


async def test_stop_before_run_forever_returns_at_once(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    harness.poller.stop()

    await asyncio.wait_for(harness.poller.run_forever(), timeout=1)

    assert harness.poller.running is False
    assert harness.snapshots("alpha") == []
    assert harness.supervisor.status()["last_poll_at"] is None


async def test_interval_follows_the_settings(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, profiles=[profile_config("alpha")])
    assert harness.poller.interval() == 60.0

    harness.supervisor.apply_settings(snapshot_interval_seconds=15)
    assert harness.poller.interval() == 15.0
    assert harness.poller.supervisor is harness.supervisor


async def test_a_failing_tick_does_not_end_the_loop(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        profiles=[profile_config("alpha")],
        settings=PlatformSettings(snapshot_interval_seconds=0),
    )
    supervisor = harness.supervisor
    await supervisor.start()
    original = supervisor.refresh_once
    calls: list[int] = []

    async def flaky_refresh() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the state store hiccupped")
        await original()

    supervisor.refresh_once = flaky_refresh  # type: ignore[method-assign]

    task = asyncio.create_task(harness.poller.run_forever())
    await asyncio.sleep(0.15)
    harness.poller.stop()
    await asyncio.wait_for(task, timeout=2)

    assert len(calls) >= 2
    assert harness.snapshots("alpha")
