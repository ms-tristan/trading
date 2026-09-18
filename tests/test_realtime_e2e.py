"""End-to-end tests of the realtime platform (work package wp10).

The two flows the delivery brief pins down are exercised here over a **local CSV
cache** and a **single SQLite file**:

1. two paper profiles are ticked once (``realtime run --once``), the tick is
   replayed a second time and must not double anything, then the *same* persisted
   state is served read-only by the standard-library monitoring server -- every
   documented route, the two documented failures (unknown profile, unknown route)
   and the read-only refusal of the mutating route over ``urllib`` with explicit
   timeouts and an ephemeral port (``0``);
2. a restart in the middle of a position: a second tick and a second orchestrator
   over the *same* database still see the order, the fills, the position and the
   last processed candle, and re-submit nothing.

Everything is offline, deterministic, seeded and bounded: no wall-clock
dependency (the run is anchored by ``realtime.start_at``), no fixed TCP port, no
signal, and every server is stopped and joined inside the test that started it.
"""

from __future__ import annotations

import ast
import json
import logging
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from typer.testing import CliRunner

from trading_backtest.cli import app
from trading_backtest.config import (
    MonitoringConfig,
    RealtimeConfig,
    load_profiles,
    load_realtime_config,
)
from trading_backtest.data.synthetic import make_ohlcv
from trading_backtest.realtime.clock import SystemClock
from trading_backtest.realtime.monitor import Monitor
from trading_backtest.realtime.orchestrator import RealtimeOrchestrator
from trading_backtest.realtime.store import SqliteStateStore
from trading_backtest.web.server import create_server, start_in_thread

RUNNER = CliRunner()

BTC = "BTC/USDT"
ETH = "ETH/USDT"
ANCHOR = "2024-01-06T00:00:00+00:00"
WARMUP = 40
LAST_CANDLE = "2024-01-05T23:00:00+00:00"

#: The exact key set of the ``/api/health`` body.
HEALTH_KEYS = frozenset(
    {
        "status",
        "version",
        "uptime_seconds",
        "profiles_total",
        "profiles_running",
        "kill_switch",
        "checked_at",
    }
)

CLIENT_TIMEOUT = 5.0
JOIN_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def quiet_logs() -> Iterator[None]:
    """Keep the structured logs out of the captured CLI output."""
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


# ---------------------------------------------------------------------------
# local scenario
# ---------------------------------------------------------------------------


def write_scenario(directory: Path) -> Path:
    """Write the CSV cache and the profiles document of the scenario."""
    cache = directory / "csv"
    cache.mkdir(parents=True, exist_ok=True)
    for symbol, timeframe, seed in ((BTC, "1h", 7), (ETH, "4h", 11)):
        frame = make_ohlcv(2000, start="2023-12-01T00:00:00Z", timeframe=timeframe, seed=seed)
        frame.to_csv(
            cache / f"{symbol.replace('/', '_').upper()}-{timeframe}.csv", index_label="timestamp"
        )
    document = {
        "profiles": [
            {
                "id": "btc-paper",
                "symbol": BTC,
                "timeframe": "1h",
                "strategy": "basic",
                "mode": "paper",
                "initial_balance": 10000.0,
                "stake_amount": 1000.0,
                "warmup_candles": WARMUP,
                "poll_interval_seconds": 30.0,
                "risk": {
                    "max_position_notional": 5000.0,
                    "max_order_notional": 2000.0,
                    "max_open_positions": 1,
                    "max_daily_loss": 500.0,
                    "max_drawdown_pct": 0.5,
                    "max_daily_trades": 10,
                },
            },
            {
                "id": "eth-paper",
                "symbol": ETH,
                "timeframe": "4h",
                "strategy": "basic",
                "mode": "paper",
                "initial_balance": 5000.0,
                "stake_amount": 500.0,
                "warmup_candles": WARMUP,
                "poll_interval_seconds": 30.0,
                "risk": {
                    "max_position_notional": 2500.0,
                    "max_order_notional": 1000.0,
                    "max_open_positions": 1,
                    "max_daily_loss": 250.0,
                    "max_drawdown_pct": 0.5,
                    "max_daily_trades": 10,
                },
            },
        ],
        "realtime": {
            "state_db": str(directory / "state.db"),
            "logs_dir": str(directory / "logs"),
            "data_dir": str(directory),
            "cache_dir": str(directory / "cache"),
            "format": "csv",
            "allow_network": False,
            "csv_dir": str(cache),
            "start_at": ANCHOR,
            "history_candles": 1,
            "poll_interval_seconds": 1.0,
            "stream_poll_timeout_seconds": 5.0,
            "max_stream_reconnects": 1,
            "reconnect_backoff_seconds": 0.01,
            "reconcile_interval_seconds": 60.0,
            "risk_free_rate": 0.0,
            "benchmark_variant": "buy_and_hold",
            "kill_switch_file": str(directory / "KILL_SWITCH"),
        },
        "monitoring": {
            "host": "127.0.0.1",
            "port": 0,
            "refresh_seconds": 2.0,
            "request_timeout_seconds": 5.0,
            "max_request_bytes": 65536,
        },
    }
    path = directory / "profiles.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def tick(path: Path) -> dict[str, Any]:
    """Run one deterministic engine tick through the CLI and return its payload."""
    result = RUNNER.invoke(app, ["realtime", "run", "--profiles", str(path), "--once", "--json"])
    assert result.exit_code == 0, result.output
    text = getattr(result, "stdout", None) or result.output
    start = text.index("{")
    end = text.rindex("}")
    return json.loads(text[start : end + 1])


def rows(database: Path, table: str) -> list[tuple[Any, ...]]:
    """Return every row of ``table`` (read-only, so the test never locks the file)."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return connection.execute(f"select * from {table}").fetchall()
    finally:
        connection.close()


def get(port: int, path: str, *, body: bytes | None = None) -> tuple[int, dict[str, Any]]:
    """Perform one HTTP call against the local server, with an explicit timeout."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method="POST" if body else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=CLIENT_TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # a documented 4xx still carries JSON
        return exc.code, json.loads(exc.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 5. the deterministic tick, twice, then the monitoring API
# ---------------------------------------------------------------------------


def test_deterministic_tick_twice_then_the_read_only_monitoring_api(tmp_path: Path) -> None:
    path = write_scenario(tmp_path)
    database = tmp_path / "state.db"

    first = tick(path)
    assert [entry["profile_id"] for entry in first["profiles"]] == ["btc-paper", "eth-paper"]
    assert first["url"] is None
    entry_orders = [item for item in first["decisions"] if item["action"] == "enter_long"]
    assert len(entry_orders) == 1
    client_order_id = entry_orders[0]["client_order_id"]
    assert client_order_id
    orders_after_first = rows(database, "orders")
    fills_after_first = rows(database, "fills")
    equity_after_first = rows(database, "equity")
    positions_after_first = rows(database, "positions")
    assert len(orders_after_first) == 1
    assert len(positions_after_first) == 1

    second = tick(path)
    assert second["decisions"] == [], "the restart replays no candle"
    assert rows(database, "orders") == orders_after_first, "no order is ever double-submitted"
    assert rows(database, "fills") == fills_after_first
    assert rows(database, "equity") == equity_after_first
    assert rows(database, "positions") == positions_after_first
    assert client_order_id in json.dumps(second)

    # ---- the read-only monitoring surface over the persisted state ----------
    clock = SystemClock()
    store = SqliteStateStore(database, clock=clock)
    store.initialize()
    orchestrator = RealtimeOrchestrator(
        profiles=load_profiles(path),
        store=store,
        clock=clock,
        realtime=load_realtime_config(path),
        monitoring=MonitoringConfig(port=0),
        stream_factory=lambda profile: None,  # never started: the API only reads
        version="e2e",
    )
    server = create_server(
        orchestrator,
        monitor=Monitor(store, clock=clock, realtime=load_realtime_config(path)),
        config=MonitoringConfig(port=0),
        read_only=True,
        version="e2e",
    )
    thread = start_in_thread(server)
    port = server.port
    assert port != 0
    try:
        status, health = get(port, "/api/health")
        assert status == 200
        assert set(health) == HEALTH_KEYS
        assert health["profiles_total"] == 2
        assert health["kill_switch"] is False
        assert health["checked_at"]

        status, payload = get(port, "/api/profiles")
        assert status == 200
        assert set(payload) == {"profiles", "generated_at"}
        assert [item["profile_id"] for item in payload["profiles"]] == [
            "btc-paper",
            "eth-paper",
        ]
        snapshot = {item["profile_id"]: item for item in payload["profiles"]}
        assert snapshot["btc-paper"]["open_positions"] == 1
        assert snapshot["btc-paper"]["health"]["last_candle_at"] == LAST_CANDLE
        # the read model rebuilds the curve from the persisted rows, not from an
        # in-process balance: the equity of the dashboard is the stored one
        assert snapshot["btc-paper"]["equity"] == pytest.approx(9999.0)

        status, detail = get(port, "/api/profiles/btc-paper")
        assert status == 200
        assert detail["profile_id"] == "btc-paper"
        assert detail["mode"] == "paper"

        status, metrics = get(port, "/api/profiles/btc-paper/metrics")
        assert status == 200
        assert set(metrics) == {"metrics", "benchmark", "generated_at"}
        assert isinstance(metrics["metrics"], dict)

        status, equity = get(port, "/api/profiles/btc-paper/equity")
        assert status == 200
        assert [point["timestamp"] for point in equity["points"]] == [LAST_CANDLE]

        status, trades = get(port, "/api/profiles/btc-paper/trades")
        assert status == 200
        assert set(trades) == {"trades", "count"}

        status, orders = get(port, "/api/profiles/btc-paper/orders")
        assert status == 200
        assert [order["client_order_id"] for order in orders["orders"]] == [client_order_id]

        status, positions = get(port, "/api/profiles/btc-paper/positions")
        assert status == 200
        assert [item["symbol"] for item in positions["positions"]] == [BTC]

        # the mutating route is refused on a read-only server
        status, refusal = get(
            port,
            "/api/kill-switch",
            body=json.dumps({"engage": True, "reason": "e2e"}).encode("utf-8"),
        )
        assert status == 403
        assert "error" in refusal

        status, missing = get(port, "/api/profiles/unknown-profile")
        assert status == 404
        assert "error" in missing

        status, unknown = get(port, "/api/no-such-route")
        assert status == 404
        assert "error" in unknown
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=JOIN_TIMEOUT)
        assert not thread.is_alive()
        store.close()


# ---------------------------------------------------------------------------
# 6. a restart in the middle of a position
# ---------------------------------------------------------------------------


def test_a_restart_in_the_middle_of_a_position_resubmits_nothing(tmp_path: Path) -> None:
    path = write_scenario(tmp_path)
    database = tmp_path / "state.db"

    tick(path)
    orders = rows(database, "orders")
    fills = rows(database, "fills")
    positions = rows(database, "positions")
    equity = rows(database, "equity")
    assert len(orders) == 1
    assert len(fills) == 1
    assert len(positions) == 1
    assert len(equity) == 2

    # ---- a second tick over the same file -----------------------------------
    payload = tick(path)
    assert payload["decisions"] == []
    assert rows(database, "orders") == orders
    assert rows(database, "fills") == fills
    assert rows(database, "positions") == positions
    assert rows(database, "equity") == equity

    # ---- a brand-new orchestrator over the same SQLite file -----------------
    clock = SystemClock()
    store = SqliteStateStore(database, clock=clock)
    store.initialize()
    try:
        assert store.last_processed_candle("btc-paper") == pd.Timestamp(LAST_CANDLE)
        restored = store.list_positions("btc-paper")
        assert len(restored) == 1
        assert restored[0].symbol == BTC
        assert len(store.list_orders("btc-paper")) == 1

        orchestrator = RealtimeOrchestrator(
            profiles=load_profiles(path),
            store=store,
            clock=clock,
            realtime=load_realtime_config(path),
            monitoring=MonitoringConfig(port=0),
            stream_factory=lambda profile: None,
            version="e2e",
        )
        snapshot = orchestrator.profile_snapshot("btc-paper")
        assert snapshot is not None
        assert snapshot.open_positions == 1
        assert snapshot.profile_id == "btc-paper"
        assert orchestrator.profile_snapshot("unknown") is None
    finally:
        store.close()


def test_a_restart_restores_the_simulated_cash_from_the_durable_state(tmp_path: Path) -> None:
    """D7: after a restart the durable state -- not the venue's amnesia -- is the truth.

    A simulated venue starts again from its configured initial balance while the
    open position is restored from the store, so without an explicit re-seeding the
    restarted platform counts that position *twice*: the dashboard would display an
    equity the persisted curve contradicts.  The last equity point of the profile is
    the durable cash, and the boot wiring hands it back to the venue.
    """
    path = write_scenario(tmp_path)
    database = tmp_path / "state.db"

    first = tick(path)
    opened = {item["profile_id"]: item for item in first["profiles"]}
    assert opened["btc-paper"]["open_positions"] == 1
    cash = opened["btc-paper"]["cash"]
    equity = opened["btc-paper"]["equity"]
    assert cash < opened["btc-paper"]["initial_balance"], "the entry spent the cash"
    assert equity == pytest.approx(cash + opened["btc-paper"]["position_value"])

    # a brand-new process over the same file: fresh PaperBroker, restored position
    second = tick(path)
    restarted = {item["profile_id"]: item for item in second["profiles"]}
    assert restarted["btc-paper"]["open_positions"] == 1
    assert restarted["btc-paper"]["cash"] == pytest.approx(cash)
    assert restarted["btc-paper"]["equity"] == pytest.approx(equity)
    assert restarted["btc-paper"]["equity"] == pytest.approx(
        restarted["btc-paper"]["cash"] + restarted["btc-paper"]["position_value"]
    )
    # the profile without a position is untouched by the re-seeding
    assert restarted["eth-paper"]["cash"] == pytest.approx(
        restarted["eth-paper"]["initial_balance"]
    )

    # ... and the durable curve says exactly the same thing
    clock = SystemClock()
    store = SqliteStateStore(database, clock=clock)
    store.initialize()
    try:
        points = store.equity_curve("btc-paper")
        assert len(points) == 1, "the restart replays no candle, so it appends no point"
        assert points[-1].cash == pytest.approx(cash)
        assert points[-1].equity == pytest.approx(restarted["btc-paper"]["equity"])
    finally:
        store.close()


def test_the_global_kill_switch_halts_the_whole_platform_end_to_end(tmp_path: Path) -> None:
    """D8c: a forced halt blocks every profile, persists nothing, and is reversible.

    The scenario's ``realtime.kill_switch_file`` is the file force.  While it
    exists, the two profiles keep running (their streams are read, their decisions
    are published) but **no** order reaches a venue, no equity point and no
    watermark is written -- so the halted candle is retried after the halt is
    lifted instead of being silently skipped.  Removing the file releases the
    platform without touching the store: the halt was never persisted, because it
    is a *file* force, not an API engagement.
    """
    path = write_scenario(tmp_path)
    database = tmp_path / "state.db"
    flag = tmp_path / "KILL_SWITCH"
    flag.write_text("operator halted the desk\n", encoding="utf-8")

    halted = tick(path)
    assert [item["profile_id"] for item in halted["profiles"]] == ["btc-paper", "eth-paper"]
    assert len(halted["decisions"]) == 2, "every profile still reports its candle"
    orders_wanted = [item for item in halted["decisions"] if item["action"] != "hold"]
    assert len(orders_wanted) == 1, "one profile wants to enter, the other has nothing to do"
    blocked = orders_wanted[0]
    assert blocked["profile_id"] == "btc-paper"
    assert blocked["blocked"] is True
    assert "kill switch" in blocked["block_reason"]
    assert blocked["client_order_id"], "the deterministic id is computed before the halt bites"
    assert blocked["quantity"] > 0.0
    assert rows(database, "orders") == []
    assert rows(database, "fills") == []
    assert rows(database, "positions") == []
    # a profile whose order was refused writes no equity point and advances no
    # watermark (its candle is retried); a profile that simply had nothing to do
    # still publishes its equity, exactly like the backtest engine's flat candle
    published_equity = {row[0] for row in rows(database, "equity")}
    assert "btc-paper" not in published_equity
    assert published_equity == {"eth-paper"}

    # the halt is durable while the file is present: a second tick changes nothing
    again = tick(path)
    still_blocked = [item for item in again["decisions"] if item["blocked"]]
    assert len(still_blocked) == 1
    assert still_blocked[0]["client_order_id"] == blocked["client_order_id"], (
        "the same deterministic order is retried, never a second one"
    )
    assert still_blocked[0]["block_reason"] == blocked["block_reason"]
    assert rows(database, "orders") == []

    # lifting the file force releases the platform, and the halted candle is retried
    flag.unlink()
    released = tick(path)
    entry = [item for item in released["decisions"] if item["action"] == "enter_long"]
    assert len(entry) == 1, "the candle the halt blocked is processed again, not skipped"
    assert entry[0]["blocked"] is False
    assert len(rows(database, "orders")) == 1
    assert len(rows(database, "positions")) == 1


def test_a_second_writer_on_the_same_state_file_is_refused(tmp_path: Path) -> None:
    """The documented single-writer rule: one process owns the state database."""
    from trading_backtest.core.errors import StateStoreError

    path = write_scenario(tmp_path)
    database = tmp_path / "state.db"
    tick(path)

    clock = SystemClock()
    first = SqliteStateStore(database, clock=clock)
    first.initialize()
    try:
        assert [profile.id for profile in first.load_profiles()] == ["btc-paper", "eth-paper"]
        second = SqliteStateStore(database, clock=clock)
        with pytest.raises(StateStoreError):
            second.initialize()
    finally:
        first.close()

    # once the owner is gone the file is usable again
    third = SqliteStateStore(database, clock=clock)
    third.initialize()
    try:
        assert len(third.list_orders("btc-paper")) == 1
    finally:
        third.close()


def test_the_platform_snapshot_is_json_safe(tmp_path: Path) -> None:
    """No NaN and no Infinity ever reaches the wire (the dashboard contract)."""
    path = write_scenario(tmp_path)
    tick(path)

    clock = SystemClock()
    store = SqliteStateStore(tmp_path / "state.db", clock=clock)
    store.initialize()
    try:
        orchestrator = RealtimeOrchestrator(
            profiles=load_profiles(path),
            store=store,
            clock=clock,
            realtime=RealtimeConfig(state_db=tmp_path / "state.db", csv_dir=tmp_path / "csv"),
            monitoring=MonitoringConfig(port=0),
            stream_factory=lambda profile: None,
            version="e2e",
        )
        encoded = json.dumps(orchestrator.snapshot().to_dict(), allow_nan=False)
        assert "Infinity" not in encoded
        assert "NaN" not in encoded
    finally:
        store.close()


def wall_clock_calls(source: Path) -> list[str]:
    """Return every real ``datetime.now()``/``time.time()`` call of one module.

    The check runs on the syntax tree, not on the raw text: the docstrings of the
    layer legitimately *mention* ``datetime.now()`` when they explain that it is
    forbidden, and a plain grep would flag that prose as a violation.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    forbidden = {"datetime.now", "datetime.utcnow", "datetime.today", "time.time"}
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and f"{owner.id}.{node.func.attr}" in forbidden:
            found.append(f"line {node.lineno}: {owner.id}.{node.func.attr}()")
    return found


def test_no_realtime_module_reads_the_wall_clock_directly() -> None:
    """The mechanical time test: only ``clock.py`` may read the system time."""
    root = Path(__file__).resolve().parents[1] / "src" / "trading_backtest"
    offenders: list[str] = []
    for package in ("realtime", "web"):
        for source in sorted((root / package).rglob("*.py")):
            if source.name == "clock.py":
                continue
            for call in wall_clock_calls(source):
                offenders.append(f"{source.relative_to(root)} -> {call}")
    assert not offenders, f"the realtime layer must read time through Clock: {offenders}"
    # the one documented exception still owns the wall clock of the layer
    assert wall_clock_calls(root / "realtime" / "clock.py")


def test_the_dashboard_static_assets_are_shipped_and_offline(tmp_path: Path) -> None:
    """The dashboard renders with no CDN, no build step and no external asset."""
    static = Path(__file__).resolve().parents[1] / "src" / "trading_backtest" / "web" / "static"
    for name in ("index.html", "app.js", "styles.css"):
        assert (static / name).is_file()
    html = (static / "index.html").read_text(encoding="utf-8")
    javascript = (static / "app.js").read_text(encoding="utf-8")
    assert "http://" not in html.replace("http://127.0.0.1", "")
    assert "cdn" not in html.lower()
    assert "cdn" not in javascript.lower()
    assert "/api/profiles" in javascript
