"""Contract tests of the ``realtime`` CLI group (work package wp10).

Everything here is **offline, deterministic and bounded**:

* the profiles documents live in ``tmp_path`` and drive the engine over a local
  CSV cache (:class:`~trading_platform.data.loader.CsvDataProvider`), so no test
  touches the network and ``TB_ALLOW_LIVE_TRADING`` is never needed;
* ``realtime.start_at`` anchors a ``ManualClock``, which is what makes
  ``realtime run --once`` reproducible: the same command run twice produces the
  same candle, the same decision and the same deterministic ``client_order_id``;
* the monitoring surface is either never started (``--once``) or patched at its
  one blocking seam, so no test waits on a socket it does not own;
* only ephemeral ports are ever requested (``--port 0``) and the chosen port is
  read back from the announced URL.

The tests never import ``tests/conftest.py`` helpers they do not own, and they
define their scenario locally instead of reaching into another work package.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from trading_platform.cli import app, main
from trading_platform.config import MonitoringConfig, RealtimeConfig
from trading_platform.data.synthetic import make_ohlcv
from trading_platform.realtime.catalog import default_catalog_body
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.store import SqliteStateStore

REPO_ROOT = Path(__file__).resolve().parents[1]

RUNNER = CliRunner()

#: The two profiles of every scenario: distinct symbols and a distinct timeframe.
BTC = "BTC/USDT"
ETH = "ETH/USDT"

#: Anchor of the deterministic tick: ``start_at - 1h`` is the exact candle the
#: BTC profile decides on, and ``start_at - 4h`` is a candle of the ETH grid.
ANCHOR = "2024-01-06T00:00:00+00:00"

#: Candles a profile may look back at, and how many it is given as warm-up.
WARMUP = 40
HISTORY_CANDLES = 1

#: Keys documented for the ``realtime-check`` payload.
CHECK_KEYS = frozenset(
    {
        "command",
        "ok",
        "config_path",
        "state_db",
        "state_db_writable",
        "kill_switch",
        "profiles",
        "issues",
    }
)

#: Keys documented for one ``realtime-check`` profile entry.
CHECK_PROFILE_KEYS = frozenset(
    {
        "id",
        "symbol",
        "timeframe",
        "strategy",
        "mode",
        "ok",
        "issues",
        "credentials_present",
        "live_gate_allowed",
        "risk",
    }
)

#: Keys documented for the ``realtime-run``/``realtime-serve`` payloads.
RUN_KEYS = frozenset({"command", "ok", "config_path", "state_db", "profiles", "decisions", "url"})

SERVE_KEYS = frozenset({"command", "ok", "config_path", "state_db", "profiles", "url"})


@pytest.fixture(autouse=True)
def quiet_logs() -> Iterator[None]:
    """Silence the structured logs so stdout carries exactly one JSON object.

    The engine logs (a warm-up warning, a reconciliation mismatch...) travel
    through the standard ``logging`` module on stderr, and ``CliRunner`` merges
    both streams.  Silencing the logger keeps the payload parseable; the logging
    behaviour itself is covered by the observability suite.
    """
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


# ---------------------------------------------------------------------------
# local scenario (no shared fixture, no other work package)
# ---------------------------------------------------------------------------


def write_csv_cache(directory: Path, *, rows: int = 2000) -> Path:
    """Write the synthetic CSV cache of the two scenario symbols."""
    cache = directory / "csv"
    cache.mkdir(parents=True, exist_ok=True)
    for symbol, timeframe, seed in ((BTC, "1h", 7), (ETH, "4h", 11)):
        frame = make_ohlcv(rows, start="2023-12-01T00:00:00Z", timeframe=timeframe, seed=seed)
        name = f"{symbol.replace('/', '_').upper()}-{timeframe}.csv"
        frame.to_csv(cache / name, index_label="timestamp")
    return cache


def profile(
    identifier: str, symbol: str, timeframe: str, balance: float, stake: float
) -> dict[str, Any]:
    """Return one valid paper profile of the scenario."""
    return {
        "id": identifier,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": "basic",
        "mode": "paper",
        "initial_balance": balance,
        "stake_amount": stake,
        "warmup_candles": WARMUP,
        "poll_interval_seconds": 30.0,
        "risk": {
            "max_position_notional": stake * 5,
            "max_order_notional": stake * 2,
            "max_open_positions": 1,
            "max_daily_loss": balance * 0.05,
            "max_drawdown_pct": 0.5,
            "max_daily_trades": 10,
        },
    }


def write_profiles(
    directory: Path,
    *,
    profiles: list[dict[str, Any]] | None = None,
    start_at: str | None = ANCHOR,
    **overrides: Any,
) -> Path:
    """Write a complete profiles document into ``directory`` and return its path."""
    cache = write_csv_cache(directory)
    document: dict[str, Any] = {
        "profiles": [
            profile("btc-paper", BTC, "1h", 10000.0, 1000.0),
            profile("eth-paper", ETH, "4h", 5000.0, 500.0),
        ]
        if profiles is None
        else profiles,
        "realtime": {
            "state_db": str(directory / "state.db"),
            "logs_dir": str(directory / "logs"),
            "data_dir": str(directory),
            "cache_dir": str(directory / "cache"),
            "format": "csv",
            "allow_network": False,
            "csv_dir": str(cache),
            "start_at": start_at,
            "history_candles": HISTORY_CANDLES,
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
    document["realtime"].update(overrides.pop("realtime", {}))
    document.update(overrides)
    path = directory / "profiles.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def payload_of(result: Any) -> dict[str, Any]:
    """Decode the single JSON object of a CLI result (logs are silenced)."""
    text = stdout_of(result)
    start = text.index("{")
    end = text.rindex("}")
    return json.loads(text[start : end + 1])


def stdout_of(result: Any) -> str:
    """Return the stdout of a click result, whatever the click version calls it."""
    return getattr(result, "stdout", None) or result.output


def combined_output(result: Any) -> str:
    """Return stdout and stderr concatenated (click separates them since 8.2)."""
    return stdout_of(result) + getattr(result, "stderr", "") or ""


def table_rows(database: Path, table: str) -> list[tuple[Any, ...]]:
    """Return every row of ``table`` in the state database."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return connection.execute(f"select * from {table}").fetchall()
    finally:
        connection.close()


def meta_value(database: Path, key: str) -> str | None:
    """Return one ``meta`` value of the state database, or ``None``."""
    rows = table_rows(database, "meta")
    for row in rows:
        if row[0] == key:
            return str(row[1])
    return None


def invoke(*args: str) -> Any:
    """Run the CLI in-process and return the click result."""
    return RUNNER.invoke(app, list(args))


# ---------------------------------------------------------------------------
# 1. the group itself, and the frozen import policy
# ---------------------------------------------------------------------------


def test_realtime_help_lists_the_three_commands() -> None:
    result = invoke("realtime", "--help")

    assert result.exit_code == 0
    for command in ("run", "serve", "check"):
        assert command in result.output


def test_missing_profiles_option_is_a_usage_error() -> None:
    result = invoke("realtime", "check")

    assert result.exit_code == 2


def test_a_missing_profiles_file_exits_one_without_a_traceback(tmp_path: Path) -> None:
    result = invoke("realtime", "check", "--profiles", str(tmp_path / "nope.json"))

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "error:" in result.output


def test_importing_the_cli_does_not_import_the_new_layers() -> None:
    """``trading_platform.cli`` must stay free of layers 6 and 7 at import time."""
    script = (
        "import sys; import trading_platform.cli; "
        "print([name for name in ('trading_platform.realtime', 'trading_platform.web') "
        "if name in sys.modules])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"


def test_the_realtime_clock_is_yielding_and_keeps_virtual_time() -> None:
    """The anchored replay clock must hand the loop back without losing time.

    ``ManualClock.sleep`` never suspends, and the engine paces itself *only*
    through ``Clock.sleep``: with a non-yielding clock the whole profile loop runs
    inside one task, the event loop is starved and a ``SIGINT`` is never
    delivered.  The helper injected by the CLI therefore advances the virtual
    time **and** yields once -- this test pins both halves of that contract.
    """
    from trading_platform.cli import _parse_moment, _realtime_clock

    anchor = _parse_moment(ANCHOR, field="start_at")
    clock = _realtime_clock(RealtimeConfig(start_at=anchor.to_pydatetime()))
    assert hasattr(clock, "advance"), "an anchored run must use a manual clock"
    progress = 0

    async def heartbeat() -> None:
        nonlocal progress
        for _ in range(5):
            progress += 1
            await asyncio.sleep(0)

    async def scenario() -> int:
        task = asyncio.ensure_future(heartbeat())
        for _ in range(100):
            await clock.sleep(1.0)
        await task
        return progress

    assert asyncio.run(scenario()) == 5, "the event loop must not be starved"
    assert clock.now().isoformat() == "2024-01-06T00:01:40+00:00"


def test_an_unanchored_run_uses_the_system_clock() -> None:
    from trading_platform.cli import _realtime_clock

    clock = _realtime_clock(RealtimeConfig(start_at=None))

    assert not hasattr(clock, "advance")
    assert clock.now().tzinfo is not None


# ---------------------------------------------------------------------------
# 2. realtime check -- the static pre-flight
# ---------------------------------------------------------------------------


def test_check_accepts_a_valid_paper_document(tmp_path: Path) -> None:
    path = write_profiles(tmp_path)
    database = tmp_path / "state.db"

    result = invoke("realtime", "check", "--profiles", str(path), "--json")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert set(payload) == CHECK_KEYS
    assert payload["command"] == "realtime-check"
    assert payload["ok"] is True
    assert payload["state_db"] == str(database)
    assert payload["state_db_writable"] is True
    assert payload["kill_switch"] is False
    assert payload["issues"] == []
    assert [entry["id"] for entry in payload["profiles"]] == ["btc-paper", "eth-paper"]
    for entry in payload["profiles"]:
        assert set(entry) == CHECK_PROFILE_KEYS
        assert entry["ok"] is True
        assert entry["issues"] == []
        assert entry["credentials_present"] is False
        assert entry["live_gate_allowed"] is True
        assert entry["mode"] == "paper"
        assert entry["risk"]["max_open_positions"] == 1


def test_check_never_creates_the_state_database(tmp_path: Path) -> None:
    path = write_profiles(tmp_path)
    database = tmp_path / "state.db"

    assert invoke("realtime", "check", "--profiles", str(path)).exit_code == 0

    assert not database.exists()
    assert not (tmp_path / "state.db.lock").exists()


def test_check_refuses_a_profile_carrying_a_credential_field(tmp_path: Path) -> None:
    profiles = [profile("btc-paper", BTC, "1h", 10000.0, 1000.0)]
    profiles[0]["api_key"] = "super-secret-value"
    path = write_profiles(tmp_path, profiles=profiles)

    result = invoke("realtime", "check", "--profiles", str(path), "--json")
    payload = payload_of(result)

    assert result.exit_code == 1
    assert payload["ok"] is False
    assert payload["profiles"] == []
    assert any("api_key" in issue for issue in payload["issues"])
    # ... and the value itself never travels through the payload
    assert "super-secret-value" not in result.output


def test_check_reports_a_live_profile_that_is_not_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TB_ALLOW_LIVE_TRADING", raising=False)
    monkeypatch.delenv("TB_LIVE_API_KEY", raising=False)
    monkeypatch.delenv("TB_LIVE_API_SECRET", raising=False)
    profiles = [profile("btc-live", BTC, "1h", 10000.0, 1000.0)]
    profiles[0]["mode"] = "live"
    path = write_profiles(tmp_path, profiles=profiles)

    result = invoke("realtime", "check", "--profiles", str(path), "--json")
    payload = payload_of(result)

    assert result.exit_code == 1
    entry = payload["profiles"][0]
    assert entry["ok"] is False
    assert entry["live_gate_allowed"] is False
    assert entry["credentials_present"] is False
    joined = " ".join(entry["issues"])
    assert "TB_ALLOW_LIVE_TRADING" in joined
    assert "I_UNDERSTAND_THE_RISK" in joined


def test_check_accepts_an_armed_live_profile_with_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TB_ALLOW_LIVE_TRADING", "I_UNDERSTAND_THE_RISK")
    monkeypatch.setenv("TB_PROFILE_BTC_LIVE_API_KEY", "key")
    monkeypatch.setenv("TB_PROFILE_BTC_LIVE_API_SECRET", "secret")
    profiles = [profile("btc-live", BTC, "1h", 10000.0, 1000.0)]
    profiles[0]["mode"] = "live"
    path = write_profiles(tmp_path, profiles=profiles)

    result = invoke("realtime", "check", "--profiles", str(path), "--json")
    payload = payload_of(result)

    assert result.exit_code == 0
    entry = payload["profiles"][0]
    assert entry["live_gate_allowed"] is True
    assert entry["credentials_present"] is True
    assert entry["issues"] == []


def test_check_reports_a_malformed_document(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text('{"profiles": [', encoding="utf-8")

    result = invoke("realtime", "check", "--profiles", str(path), "--json")
    payload = payload_of(result)

    assert result.exit_code == 1
    assert payload["ok"] is False
    assert payload["profiles"] == []
    assert payload["issues"], "the reason must travel in the payload"
    assert any(str(path) in issue for issue in payload["issues"])


def test_check_reports_an_unsupported_timeframe(tmp_path: Path) -> None:
    profiles = [profile("btc-paper", BTC, "7m", 10000.0, 1000.0)]
    path = write_profiles(tmp_path, profiles=profiles)

    result = invoke("realtime", "check", "--profiles", str(path), "--json")
    payload = payload_of(result)

    assert result.exit_code == 1
    assert payload["ok"] is False
    assert any("7m" in issue for issue in payload["issues"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the file mode")
def test_check_reports_an_unwritable_state_directory(tmp_path: Path) -> None:
    directory = tmp_path / "readonly"
    directory.mkdir()
    path = write_profiles(directory)
    directory.chmod(0o500)
    try:
        result = invoke("realtime", "check", "--profiles", str(path), "--json")
        payload = payload_of(result)
    finally:
        directory.chmod(0o700)

    assert result.exit_code == 1
    assert payload["state_db_writable"] is False
    assert payload["ok"] is False
    assert any("not writable" in issue for issue in payload["issues"])


def test_check_prints_a_human_summary_and_exits_one_on_a_finding(tmp_path: Path) -> None:
    profiles = [profile("btc-paper", BTC, "1h", 10000.0, 1000.0)]
    profiles[0]["api_secret"] = "leaked"
    path = write_profiles(tmp_path, profiles=profiles)

    result = invoke("realtime", "check", "--profiles", str(path))

    assert result.exit_code == 1
    assert "realtime-check" in result.output
    assert "issues" in result.output
    assert "leaked" not in result.output


# ---------------------------------------------------------------------------
# 3. realtime run --once -- one deterministic tick
# ---------------------------------------------------------------------------


def test_run_installs_the_structured_logs_of_the_layer(tmp_path: Path) -> None:
    """``realtime run`` must wire the JSON logs, console *and* durable file.

    Regression test for the deployed container: nothing called
    ``configure_logging``, so the layer's events fell through to the interpreter's
    last-resort handler -- bare event names at WARNING and above, every INFO event
    dropped, and a ``profile_crashed`` line that did not carry its error.
    """
    path = write_profiles(tmp_path)
    logs_dir = tmp_path / "logs"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["realtime"]["logs_dir"] = str(logs_dir)
    path.write_text(json.dumps(document), encoding="utf-8")

    result = None
    logging.disable(logging.NOTSET)  # this test asserts the log stream itself
    try:
        result = invoke("realtime", "run", "--profiles", str(path), "--once", "--json")
    finally:
        logging.disable(logging.CRITICAL)

    assert result is not None
    assert result.exit_code == 0
    files = sorted(logs_dir.glob("realtime-*.log"))
    assert len(files) == 1, "the durable JSON log was not created"
    lines = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    events = [entry["event"] for entry in lines]
    assert "profile_starting" in events
    assert "candle_processed" in events, "INFO events must reach the log stream"
    processed = next(entry for entry in lines if entry["event"] == "candle_processed")
    assert processed["profile_id"] == "btc-paper"
    assert "equity" in processed


def test_run_once_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    """The heart of the contract: one tick writes durable state, twice does not double it."""
    path = write_profiles(tmp_path)
    database = tmp_path / "state.db"

    first = invoke("realtime", "run", "--profiles", str(path), "--once", "--json")
    first_payload = payload_of(first)
    assert first.exit_code == 0
    assert set(first_payload) == RUN_KEYS
    assert first_payload["command"] == "realtime-run"
    assert first_payload["ok"] is True
    assert first_payload["url"] is None, "--once never starts a monitoring server"
    assert [entry["profile_id"] for entry in first_payload["profiles"]] == [
        "btc-paper",
        "eth-paper",
    ]
    decisions = first_payload["decisions"]
    assert [entry["profile_id"] for entry in decisions] == ["btc-paper", "eth-paper"]
    entry_decisions = [item for item in decisions if item["action"] == "enter_long"]
    assert len(entry_decisions) == 1, "the scenario plants exactly one entry signal"
    client_order_id = entry_decisions[0]["client_order_id"]
    assert client_order_id == "btc-paper-BTC_USDT-20240105T230000Z-0000"

    # the durable state the tick published
    assert database.is_file()
    assert len(table_rows(database, "profiles")) == 2
    equity = table_rows(database, "equity")
    assert len(equity) == 2
    assert len(table_rows(database, "orders")) == 1
    assert len(table_rows(database, "fills")) == 1
    assert len(table_rows(database, "positions")) == 1
    assert meta_value(database, "last_candle:btc-paper") == "2024-01-05T23:00:00+00:00"
    assert meta_value(database, "last_candle:eth-paper") == "2024-01-05T20:00:00+00:00"
    orders_before = table_rows(database, "orders")
    fills_before = table_rows(database, "fills")
    positions_before = table_rows(database, "positions")
    equity_before = table_rows(database, "equity")

    second = invoke("realtime", "run", "--profiles", str(path), "--once", "--json")
    second_payload = payload_of(second)

    assert second.exit_code == 0
    assert second_payload["ok"] is True
    assert second_payload["decisions"] == [], "a restart replays no candle"
    # the very same client_order_id is still the one and only order of the store,
    # once: the restart replayed nothing and resubmitted nothing.  A profile whose
    # durable history is a terminal order reconciles healthy against the fresh
    # venue of the restart, so the identifier is read from the durable state --
    # which is its truth -- and not from a reconciliation error the profile no
    # longer has (see docs/realtime.md, "reconciliation compares like with like").
    assert [order[0] for order in table_rows(database, "orders")] == [client_order_id]
    assert table_rows(database, "orders") == orders_before
    assert table_rows(database, "fills") == fills_before
    assert table_rows(database, "positions") == positions_before
    assert table_rows(database, "equity") == equity_before
    assert meta_value(database, "last_candle:btc-paper") == "2024-01-05T23:00:00+00:00"


def test_run_once_prints_a_human_summary(tmp_path: Path) -> None:
    path = write_profiles(tmp_path)

    result = invoke("realtime", "run", "--profiles", str(path), "--once")

    assert result.exit_code == 0
    assert "realtime-run" in result.output
    assert "decisions: 2" in result.output


def test_run_once_turns_a_hanging_stream_into_a_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every await is bounded: a stream that never answers fails loudly, it never hangs."""
    from trading_platform.realtime import stream as stream_module

    async def never(self: Any, symbol: str, timeframe: str) -> None:
        await asyncio.sleep(30.0)

    monkeypatch.setattr(stream_module.PollingMarketStream, "next_candle", never)
    profiles = [profile("btc-paper", BTC, "1h", 10000.0, 1000.0)]
    path = write_profiles(
        tmp_path,
        profiles=profiles,
        realtime={"stream_poll_timeout_seconds": 0.05},
    )

    result = invoke("realtime", "run", "--profiles", str(path), "--once", "--json")
    payload = payload_of(result)

    assert result.exit_code == 1
    assert payload["ok"] is False
    assert "error:" in combined_output(result)


# ---------------------------------------------------------------------------
# 4. realtime serve and realtime run in server mode
# ---------------------------------------------------------------------------


def test_serve_is_read_only_over_the_persisted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve`` reads the SQLite file, binds an ephemeral port and stops cleanly."""
    from trading_platform.web import server as server_module

    path = write_profiles(tmp_path)
    assert invoke("realtime", "run", "--profiles", str(path), "--once").exit_code == 0

    served: dict[str, Any] = {}

    def bounded(server: Any, *, block: bool = True) -> None:
        """Run the real serving loop, bounded by an immediate shutdown."""
        served["read_only"] = server.router.read_only
        served["port"] = server.port
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        server.shutdown()
        thread.join(timeout=5.0)
        assert not thread.is_alive()

    monkeypatch.setattr(server_module, "serve", bounded)
    result = invoke("realtime", "serve", "--profiles", str(path), "--json", "--port", "0")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert set(payload) == SERVE_KEYS
    assert payload["command"] == "realtime-serve"
    assert payload["ok"] is True
    assert served["read_only"] is True
    assert served["port"] != 0
    assert payload["url"] == f"http://127.0.0.1:{served['port']}/"
    assert [entry["profile_id"] for entry in payload["profiles"]] == [
        "btc-paper",
        "eth-paper",
    ]
    snapshot = {entry["profile_id"]: entry for entry in payload["profiles"]}
    assert snapshot["btc-paper"]["open_positions"] == 1
    assert snapshot["btc-paper"]["health"]["last_candle_at"] == "2024-01-05T23:00:00+00:00"


def test_serve_publishes_the_persisted_shared_wallet(tmp_path: Path) -> None:
    """The read-only surface reports the durable ledger, never a configured guess.

    ``realtime serve`` runs no engine, but the shared wallet **is** persisted: the
    snapshot it rebuilds therefore carries the stored cash of the one ledger, and
    every profile carries the share *attributed* to it (its allocation, its
    deployed capital and its own P&L) instead of defaulting to zero.
    """
    from trading_platform.cli import _PersistedSnapshotProvider
    from trading_platform.config import load_realtime_config
    from trading_platform.realtime.models import RunMode

    path = write_profiles(tmp_path)
    database = tmp_path / "state.db"
    assert invoke("realtime", "run", "--profiles", str(path), "--once").exit_code == 0

    realtime = load_realtime_config(path)
    clock = ManualClock(datetime.fromisoformat(ANCHOR))
    store = SqliteStateStore(database, clock=clock)
    store.initialize()
    try:
        provider = _PersistedSnapshotProvider(store, clock=clock, realtime=realtime)
        snapshot = provider.snapshot()
        wallet = snapshot.wallet
        assert wallet is not None
        assert wallet.source == "local"
        assert wallet.mode == RunMode.PAPER
        assert wallet.initial_balance == pytest.approx(15_000.0), "the durable initial balance"
        assert wallet.cash == pytest.approx(13_999.0), "15000 - 1000 notional - 1.00 fee"
        assert wallet.profiles == 2
        assert wallet.equity == pytest.approx(
            wallet.cash + sum(float(item.position_value) for item in snapshot.profiles)
        )
        assert provider.health()["wallet"] == wallet.to_dict()

        attributed = {item.profile_id: item.to_dict() for item in snapshot.profiles}
        assert attributed["btc-paper"]["allocation"] == pytest.approx(10_000.0)
        assert attributed["btc-paper"]["deployed"] == pytest.approx(1_000.0)
        assert attributed["btc-paper"]["realized_pnl"] == pytest.approx(0.0)
        assert attributed["eth-paper"]["allocation"] == pytest.approx(5_000.0)
        assert attributed["eth-paper"]["deployed"] == pytest.approx(0.0)
        # the cash of a profile is its attributed share, never the whole wallet
        assert attributed["eth-paper"]["cash"] == pytest.approx(5_000.0)
    finally:
        store.close()

    # a store that never saw a wallet answers None, never an invented ledger
    empty = SqliteStateStore(tmp_path / "empty.db", clock=clock)
    empty.initialize()
    try:
        provider = _PersistedSnapshotProvider(empty, clock=clock, realtime=realtime)
        assert provider.snapshot().wallet is None
        assert provider.health()["wallet"] is None
    finally:
        empty.close()


def test_run_starts_the_monitoring_server_and_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server mode wires the engine and the dashboard, then shuts both down."""
    from trading_platform.realtime.orchestrator import RealtimeOrchestrator

    path = write_profiles(tmp_path)
    client_order_ids: list[str] = []

    async def run_forever(self: RealtimeOrchestrator) -> None:
        """One real tick, then return as a platform that stopped by itself."""
        for decision in await self.run_once():
            client_order_ids.append(decision.client_order_id)

    monkeypatch.setattr(RealtimeOrchestrator, "run_forever", run_forever)

    result = invoke("realtime", "run", "--profiles", str(path), "--json", "--port", "0")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert set(payload) == RUN_KEYS
    assert payload["decisions"] == [], "server mode publishes no decision list"
    assert payload["ok"] is True
    assert payload["url"].startswith("http://127.0.0.1:")
    assert payload["url"].endswith("/")
    port = int(payload["url"].rsplit(":", 1)[1].rstrip("/"))
    assert port != 0
    assert len(payload["profiles"]) == 2
    assert any(client_order_ids), "the tick really ran before the server was closed"
    assert "monitoring: " in combined_output(result)


def test_serve_announces_the_url_on_stderr_in_json_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` keeps stdout to exactly one JSON object."""
    from trading_platform.web import server as server_module

    path = write_profiles(tmp_path)
    assert invoke("realtime", "run", "--profiles", str(path), "--once").exit_code == 0

    def bounded(server: Any, *, block: bool = True) -> None:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        server.shutdown()
        thread.join(timeout=5.0)

    monkeypatch.setattr(server_module, "serve", bounded)
    result = invoke("realtime", "serve", "--profiles", str(path), "--json", "--port", "0")

    assert result.exit_code == 0
    assert stdout_of(result).strip().startswith("{")
    assert stdout_of(result).strip().endswith("}")
    assert payload_of(result)["command"] == "realtime-serve"


def test_main_returns_zero_for_a_successful_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_profiles(tmp_path)

    assert main(["realtime", "check", "--profiles", str(path)]) == 0
    capsys.readouterr()


def test_main_returns_one_for_a_missing_file(tmp_path: Path) -> None:
    assert main(["realtime", "check", "--profiles", str(tmp_path / "absent.json")]) == 1


def test_monitoring_config_defaults_are_documented_values() -> None:
    """The scenario relies on the documented monitoring defaults staying stable."""
    monitoring = MonitoringConfig()

    assert monitoring.host == "127.0.0.1"
    assert monitoring.refresh_seconds == 2.0


# ---------------------------------------------------------------------------
# 5. the profile surface of the monitoring server (catalog, candles, lifecycle)
# ---------------------------------------------------------------------------


def seed_state_store(database: Path, profiles_path: Path) -> None:
    """Persist the profiles of ``profiles_path`` into a **fresh** state database.

    The store then knows both profiles and holds **no** candle: it is the exact
    state ``realtime serve`` reads after a deployment that never ran a tick.
    """
    from trading_platform.config import load_profiles

    store = SqliteStateStore(database, clock=ManualClock(datetime.fromisoformat(ANCHOR)))
    store.initialize()
    try:
        for definition in load_profiles(profiles_path):
            store.save_profile(definition)
    finally:
        store.close()


def http_call(
    server: Any,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Perform one loopback request against ``server`` with an explicit timeout."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5.0)
    try:
        connection.request(method, path, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw.decode("utf-8"))
    finally:
        connection.close()


def test_serve_answers_the_catalog_candles_and_control_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``realtime serve`` exposes the whole read surface over persisted state."""
    from trading_platform.web import server as server_module

    path = write_profiles(tmp_path)
    seed_state_store(tmp_path / "state.db", path)
    observed: dict[str, Any] = {}

    def bounded(server: Any, *, block: bool = True) -> None:
        """Serve for real, run the calls, then stop deterministically."""
        observed["read_only"] = server.router.read_only
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            observed["catalog"] = http_call(server, "GET", "/api/catalog")
            observed["candles"] = http_call(server, "GET", "/api/profiles/btc-paper/candles")
            observed["control"] = http_call(server, "GET", "/api/control")
            observed["pause"] = http_call(
                server, "POST", "/api/profiles/btc-paper/pause", body=b"{}"
            )
            observed["delete"] = http_call(server, "DELETE", "/api/profiles/btc-paper")
        finally:
            server.shutdown()
            thread.join(timeout=5.0)
            assert not thread.is_alive()

    monkeypatch.setattr(server_module, "serve", bounded)
    result = invoke("realtime", "serve", "--profiles", str(path), "--json", "--port", "0")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert set(payload) == SERVE_KEYS
    assert observed["read_only"] is True

    status, catalog = observed["catalog"]
    assert status == 200
    expected = default_catalog_body("USDT")
    assert catalog == expected
    assert catalog["symbols"], "the offline fallback must offer the picker some symbols"
    for entry in catalog["symbols"]:
        assert sorted(entry) == ["base", "quote", "symbol"]

    status, candles = observed["candles"]
    assert status == 200
    assert candles == {"candles": [], "count": 0}

    status, control = observed["control"]
    assert status == 200
    assert control == {
        "engine_running": False,
        "read_only": True,
        "mutable": False,
        "profiles": [],
    }

    for name in ("pause", "delete"):
        status, body = observed[name]
        assert status == 403, name
        assert body == {"error": "mutations are disabled on this server"}, name


def test_run_wires_the_controller_and_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``realtime run`` hands both lifecycle seams to the monitoring server."""
    from trading_platform.realtime.catalog import MarketCatalog
    from trading_platform.realtime.control import RuntimeProfileController
    from trading_platform.realtime.orchestrator import RealtimeOrchestrator
    from trading_platform.web import server as server_module

    path = write_profiles(tmp_path)
    recorded: dict[str, Any] = {}
    real_create_server = server_module.create_server

    def recording_create_server(provider: Any, **kwargs: Any) -> Any:
        recorded["provider"] = provider
        recorded.update(kwargs)
        return real_create_server(provider, **kwargs)

    async def run_forever(self: RealtimeOrchestrator) -> None:
        """One real tick, then return: no live engine is ever started."""
        await self.run_once()

    monkeypatch.setattr(server_module, "create_server", recording_create_server)
    monkeypatch.setattr(RealtimeOrchestrator, "run_forever", run_forever)

    result = invoke("realtime", "run", "--profiles", str(path), "--json", "--port", "0")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert set(payload) == RUN_KEYS
    assert recorded["read_only"] is False

    controller = recorded["controller"]
    catalog = recorded["catalog"]
    assert isinstance(controller, RuntimeProfileController)
    assert isinstance(catalog, MarketCatalog)
    assert controller.profiles_path == path
    assert controller.bound is True, "the controller must be bound to the engine loop"
    assert catalog.exchange == "binance"
    assert catalog.quote == "USDT"
    assert catalog.catalog()["symbols"] == default_catalog_body("USDT")["symbols"]
    assert hasattr(recorded["provider"], "candle_series")


def test_the_catalog_venue_comes_from_the_profiles() -> None:
    """The quote of a profile is what the picker is asked to list."""
    from trading_platform.cli import _realtime_catalog_venue
    from trading_platform.config import ProfileConfig

    assert _realtime_catalog_venue(()) == ("binance", "USDT")
    eth = ProfileConfig(id="eth-eur", symbol="ETH/EUR", timeframe="1h")
    assert _realtime_catalog_venue([eth]) == ("binance", "EUR")
