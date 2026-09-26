"""Contract tests of the ``realtime`` CLI group (work package wp10).

Everything here is **offline, deterministic and bounded**:

* the profiles live in the SQLite state store under ``tmp_path`` -- the single
  source of truth -- and drive the engine over a local CSV cache
  (:class:`~trading_platform.data.loader.CsvDataProvider`), so no test touches the
  network and ``TB_ALLOW_LIVE_TRADING`` is never needed;
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
import math
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
from trading_platform.config import MonitoringConfig, ProfileConfig, RealtimeConfig
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
        # additive: the warm-up contract of the profile (see WARMUP_KEYS)
        "warmup",
    }
)

#: Keys documented for the additive ``warmup`` block of a profile entry.
WARMUP_KEYS = frozenset(
    {
        "candles_per_day",
        "required_candles",
        "warmup_candles",
        "history_candles",
        "findings",
    }
)

#: Keys documented for one warm-up finding.
WARMUP_FINDING_KEYS = frozenset({"code", "severity", "message"})

#: Keys documented for the ``realtime-run``/``realtime-serve`` payloads.
RUN_KEYS = frozenset({"command", "ok", "state_db", "profiles", "decisions", "url"})

SERVE_KEYS = frozenset({"command", "ok", "state_db", "profiles", "url"})


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


def momentum_profile(
    identifier: str, timeframe: str, warmup_candles: int, **overrides: Any
) -> dict[str, Any]:
    """Return one momentum profile of the incident's shape.

    The delivered strategy reads its three lookbacks in **days**, converting them
    with the candle grid of the timeframe, so the same parameters need 40 321
    candles on a 1m grid and 169 on a 4h one: this helper is how a scenario asks
    ``realtime check`` about that arithmetic.
    """
    document = profile(identifier, BTC, timeframe, 10000.0, 1000.0)
    document["strategy"] = "momentum"
    document["warmup_candles"] = int(warmup_candles)
    document.update(overrides)
    return document


def store_raw_profile(database: Path, definition: Mapping[str, Any]) -> None:
    """Write one profile row **verbatim**, bypassing the model validation.

    The store validates a profile on write -- that is the point of it being the
    source of truth -- so this helper exists only for the pre-flight findings that
    describe a row a *previous, laxer* build could have written (a timeframe the
    engine no longer supports).  It is the SQLite equivalent of the hand-edited
    JSON document those tests used to build.
    """
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO profiles (profile_id, payload, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(profile_id) DO UPDATE SET payload = excluded.payload",
            (str(definition["id"]), json.dumps(definition), "2024-01-06T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()


def seed_profiles(
    directory: Path,
    *,
    profiles: list[dict[str, Any]] | None = None,
    start_at: str | None = ANCHOR,
    raw_profiles: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> Path:
    """Seed a **fresh** state database with the scenario profiles and settings.

    The SQLite state store is the single source of truth for both, so this helper
    is the whole "configuration" of a scenario: it writes the profiles into the
    ``profiles`` table and the engine settings into the ``meta`` table, exactly
    like a deployment whose settings were customised once through the store.  It
    returns the database path, which is what every command is pointed at.
    """
    from trading_platform.realtime.settings import PlatformSettings, save_settings

    cache = write_csv_cache(directory)
    realtime: dict[str, Any] = {
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
    }
    realtime.update(overrides.pop("realtime", {}))
    monitoring: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 0,
        "refresh_seconds": 2.0,
        "request_timeout_seconds": 5.0,
        "max_request_bytes": 65536,
    }
    monitoring.update(overrides.pop("monitoring", {}))
    definitions = (
        [
            profile("btc-paper", BTC, "1h", 10000.0, 1000.0),
            profile("eth-paper", ETH, "4h", 5000.0, 500.0),
        ]
        if profiles is None
        else profiles
    )

    database = directory / "state.db"
    store = SqliteStateStore(database, clock=ManualClock(datetime.fromisoformat(ANCHOR)))
    store.initialize()
    try:
        for definition in definitions:
            store.save_profile(ProfileConfig.model_validate(definition))
        save_settings(
            store,
            PlatformSettings(
                realtime=RealtimeConfig(**realtime),
                monitoring=MonitoringConfig(**monitoring),
            ),
        )
    finally:
        store.close()
    for definition in raw_profiles or []:
        store_raw_profile(database, definition)
    return database


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


def test_the_state_db_option_falls_back_to_the_environment_then_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--state-db`` is the bootstrap key, and it is optional.

    The state database is the single source of truth, but *its own path* cannot
    live inside it: it is resolved as *option > environment > model default*, so a
    host that customises nothing launches with no argument at all and still gets a
    real pre-flight.
    """
    # 1. no option, no environment: the model default is used, and it still runs.
    monkeypatch.delenv("TB_REALTIME_STATE_DB", raising=False)
    default = invoke("realtime", "check", "--json")
    assert default.exit_code == 0, default.output
    assert str(RealtimeConfig().state_db) in default.output

    # 2. the environment variable is honoured when the option is omitted.
    target = tmp_path / "from-env.db"
    monkeypatch.setenv("TB_REALTIME_STATE_DB", str(target))
    from_env = invoke("realtime", "check", "--json")
    assert from_env.exit_code == 0, from_env.output
    assert str(target) in from_env.output

    # 3. an explicit option always wins over the environment.
    explicit = tmp_path / "explicit.db"
    chosen = invoke("realtime", "check", "--state-db", str(explicit), "--json")
    assert chosen.exit_code == 0, chosen.output
    assert str(explicit) in chosen.output
    assert str(target) not in chosen.output


def test_a_missing_state_db_directory_exits_without_a_traceback(tmp_path: Path) -> None:
    """The state database is created on demand: what fails is an unusable path.

    A *missing directory* is no longer a finding -- the store creates its parent --
    so the failure surface is now an unwritable location, which is exactly what an
    operator hits when a volume is not mounted.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("this is a file, not a directory", encoding="utf-8")
    target = blocker / "state.db"

    result = invoke("realtime", "check", "--state-db", str(target), "--json")

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
    database = seed_profiles(tmp_path)

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
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
        # The additive warm-up block reports the numbers and the findings alike.
        warmup = entry["warmup"]
        assert set(warmup) == WARMUP_KEYS
        assert warmup["candles_per_day"] == pytest.approx(
            24.0 if entry["id"] == "btc-paper" else 6.0
        )
        # ``basic`` sizes its lookbacks in candles: it declares no warm-up at all.
        assert warmup["required_candles"] == 0
        assert warmup["warmup_candles"] == WARMUP
        assert warmup["history_candles"] == HISTORY_CANDLES
        # The scenario's stream window is 1 candle while the profiles ask for 40:
        # a real misconfiguration, reported as a warning -- and never in ``issues``,
        # because a warning must not turn a working platform into a failure.
        assert [finding["severity"] for finding in warmup["findings"]] == ["warning"]
        for finding in warmup["findings"]:
            assert set(finding) == WARMUP_FINDING_KEYS
            assert finding["code"] == "warmup-exceeds-history"
            assert str(WARMUP) in finding["message"]
            assert str(HISTORY_CANDLES) in finding["message"]


def test_check_reports_an_impossible_warm_up_as_a_finding(tmp_path: Path) -> None:
    """The incident profile is a finding: ``ok`` is ``False`` and the exit code is 1.

    A ``momentum`` profile on 1m asking for 200 candles needs 40 321: the frame it
    builds can never warm up, so the pre-flight must say so instead of reporting a
    profile that will run for ever with zero signals as healthy.
    """
    database = seed_profiles(
        tmp_path, profiles=[momentum_profile("momentum-1m", "1m", warmup_candles=200)]
    )

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
    payload = payload_of(result)

    assert result.exit_code == 1
    assert payload["ok"] is False
    # The finding belongs to the profile entry, not to the platform-level issues.
    assert payload["issues"] == []
    entry = payload["profiles"][0]
    assert set(entry) == CHECK_PROFILE_KEYS
    assert entry["ok"] is False
    assert len(entry["issues"]) == 1
    assert "can never warm up" in entry["issues"][0]

    warmup = entry["warmup"]
    assert set(warmup) == WARMUP_KEYS
    assert warmup["candles_per_day"] == pytest.approx(1440.0)
    assert warmup["required_candles"] == 40321
    assert warmup["warmup_candles"] == 200
    assert warmup["history_candles"] == HISTORY_CANDLES
    assert [finding["code"] for finding in warmup["findings"]] == [
        "strategy-warmup-impossible",
        "warmup-exceeds-history",
    ]
    assert [finding["severity"] for finding in warmup["findings"]] == ["error", "warning"]
    # The error finding is repeated verbatim in ``issues``; the warning is not.
    assert entry["issues"] == [
        finding["message"] for finding in warmup["findings"] if finding["severity"] == "error"
    ]


def test_check_reports_a_coherence_warning_without_failing(tmp_path: Path) -> None:
    """A 4h momentum profile inside a 1-candle window: warning only, exit code 0."""
    database = seed_profiles(
        tmp_path, profiles=[momentum_profile("momentum-4h", "4h", warmup_candles=200)]
    )

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert payload["ok"] is True
    entry = payload["profiles"][0]
    assert entry["ok"] is True
    assert entry["issues"] == []
    warmup = entry["warmup"]
    # 169 candles are all this profile needs on a 4h grid.
    assert warmup["candles_per_day"] == pytest.approx(6.0)
    assert warmup["required_candles"] == 169
    assert [finding["severity"] for finding in warmup["findings"]] == ["warning"]
    assert entry["warmup"]["findings"][0]["code"] == "warmup-exceeds-history"


def test_check_is_green_for_a_momentum_profile_that_can_be_fed(tmp_path: Path) -> None:
    """The fixed configuration: 40 321 candles asked for, 50 000 served, no finding."""
    database = seed_profiles(
        tmp_path,
        profiles=[momentum_profile("momentum-1m", "1m", warmup_candles=40321)],
        realtime={"history_candles": 50000},
    )

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert payload["ok"] is True
    entry = payload["profiles"][0]
    assert entry["ok"] is True
    assert entry["issues"] == []
    warmup = entry["warmup"]
    assert warmup["required_candles"] == 40321
    assert warmup["warmup_candles"] == 40321
    assert warmup["history_candles"] == 50000
    assert warmup["findings"] == []


def test_check_never_refuses_a_strategy_without_a_warm_up(tmp_path: Path) -> None:
    """A 1m ``basic`` profile at the default warm-up stays exactly as green as before."""
    database = seed_profiles(
        tmp_path,
        profiles=[profile("basic-1m", BTC, "1m", 10000.0, 1000.0)],
        realtime={"history_candles": 300},
    )

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert payload["ok"] is True
    entry = payload["profiles"][0]
    assert entry["strategy"] == "basic"
    assert entry["ok"] is True
    assert entry["issues"] == []
    assert entry["warmup"]["required_candles"] == 0
    assert entry["warmup"]["findings"] == []


def test_check_on_a_fresh_database_is_ok_with_zero_profiles(tmp_path: Path) -> None:
    """An empty platform is a legal platform: ``ok`` is ``True`` with no profile.

    The state database is the single source of truth, so a host that never
    declared anything boots, serves and accepts ``POST /api/profiles``.  The
    static pre-flight therefore finds nothing to report.
    """
    database = tmp_path / "state.db"

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert set(payload) == CHECK_KEYS
    assert payload["ok"] is True
    assert payload["profiles"] == []
    assert payload["issues"] == []
    assert payload["state_db"] == str(database)
    assert "config_path" not in payload


def test_check_refuses_a_database_holding_a_profile_with_a_credential_field(
    tmp_path: Path,
) -> None:
    """A credential can never be stored: the model refuses the row, loudly."""
    from pydantic import ValidationError

    leaked = profile("btc-paper", BTC, "1h", 10000.0, 1000.0)
    leaked["api_key"] = "super-secret-value"

    with pytest.raises(ValidationError) as excinfo:
        seed_profiles(tmp_path, profiles=[leaked])
    assert "api_key" in str(excinfo.value)
    # the value itself never travelled anywhere
    stored = tmp_path / "state.db"
    if stored.exists():
        assert "super-secret-value" not in stored.read_bytes().decode("utf-8", errors="ignore")


def test_check_reports_a_live_profile_that_is_not_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TB_ALLOW_LIVE_TRADING", raising=False)
    monkeypatch.delenv("TB_LIVE_API_KEY", raising=False)
    monkeypatch.delenv("TB_LIVE_API_SECRET", raising=False)
    profiles = [profile("btc-live", BTC, "1h", 10000.0, 1000.0)]
    profiles[0]["mode"] = "live"
    database = seed_profiles(tmp_path, profiles=profiles)

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
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
    database = seed_profiles(tmp_path, profiles=profiles)

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
    payload = payload_of(result)

    assert result.exit_code == 0
    entry = payload["profiles"][0]
    assert entry["live_gate_allowed"] is True
    assert entry["credentials_present"] is True
    assert entry["issues"] == []


def test_check_reports_unusable_state_storage(tmp_path: Path) -> None:
    """An unusable state database is a pre-flight *finding*, never a traceback."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("this is a file", encoding="utf-8")
    target = blocker / "state.db"

    result = invoke("realtime", "check", "--state-db", str(target), "--json")
    payload = payload_of(result)

    assert result.exit_code == 1
    assert payload["ok"] is False
    assert payload["profiles"] == []
    assert payload["issues"], "the reason must travel in the payload"
    assert "Traceback" not in result.output


def test_check_reports_an_unsupported_timeframe(tmp_path: Path) -> None:
    database = seed_profiles(
        tmp_path, raw_profiles=[profile("btc-paper", BTC, "7m", 10000.0, 1000.0)]
    )

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
    payload = payload_of(result)

    assert result.exit_code == 1
    assert payload["ok"] is False
    assert any("7m" in issue for issue in payload["issues"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the file mode")
def test_check_reports_an_unwritable_state_directory(tmp_path: Path) -> None:
    directory = tmp_path / "readonly"
    directory.mkdir()
    database = directory / "state.db"
    directory.chmod(0o500)
    try:
        result = invoke("realtime", "check", "--state-db", str(database), "--json")
        payload = payload_of(result)
    finally:
        directory.chmod(0o700)

    assert result.exit_code == 1
    assert payload["state_db_writable"] is False
    assert payload["ok"] is False
    assert any("not writable" in issue for issue in payload["issues"])


def test_check_prints_a_human_summary_and_exits_one_on_a_finding(tmp_path: Path) -> None:
    leaked = profile("btc-paper", BTC, "1h", 10000.0, 1000.0)
    leaked["api_secret"] = "leaked-secret-value"
    database = seed_profiles(tmp_path, raw_profiles=[leaked])

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
    payload = payload_of(result)

    assert result.exit_code == 1
    assert payload["ok"] is False
    assert payload["profiles"] == []
    # The JSON payload never echoes the offending value.
    assert "leaked-secret-value" not in stdout_of(result)


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
    logs_dir = tmp_path / "logs"
    database = seed_profiles(tmp_path)

    result = None
    logging.disable(logging.NOTSET)  # this test asserts the log stream itself
    try:
        result = invoke(
            "realtime",
            "run",
            "--state-db",
            str(database),
            "--logs-dir",
            str(logs_dir),
            "--once",
            "--json",
        )
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
    database = seed_profiles(tmp_path)

    first = invoke("realtime", "run", "--state-db", str(database), "--once", "--json")
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

    second = invoke("realtime", "run", "--state-db", str(database), "--once", "--json")
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
    database = seed_profiles(tmp_path)

    result = invoke("realtime", "run", "--state-db", str(database), "--once")

    assert result.exit_code == 0
    assert "realtime-run" in result.output
    assert "decisions: 2" in result.output


def test_run_once_turns_a_hanging_stream_into_a_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every await is bounded: a stream that never answers fails loudly, it never hangs.

    The tick budget is now derived from the profile's own legitimate wait
    (``max(poll_interval_seconds, stream_poll_timeout_seconds)`` plus its margin),
    not from ``stream_poll_timeout_seconds`` alone -- the old arithmetic
    (``timeout * (profiles + 1)``) cut a healthy idle poll of a profile pacing
    slower than the stream timeout, which is exactly the defect this package
    fixes.  The scenario therefore keeps the poll interval *inside* the stream
    timeout (0.05 s against 0.05 s) so that the 30 s hang really is an unbounded
    await, and the command still fails fast instead of hanging.
    """
    from trading_platform.realtime import stream as stream_module

    async def never(self: Any, symbol: str, timeframe: str) -> None:
        await asyncio.sleep(30.0)

    monkeypatch.setattr(stream_module.PollingMarketStream, "next_candle", never)
    definition = profile("btc-paper", BTC, "1h", 10000.0, 1000.0)
    definition["poll_interval_seconds"] = 0.05
    database = seed_profiles(
        tmp_path,
        profiles=[definition],
        realtime={"stream_poll_timeout_seconds": 0.05},
    )

    result = invoke("realtime", "run", "--state-db", str(database), "--once", "--json")
    payload = payload_of(result)

    assert result.exit_code == 1
    assert payload["ok"] is False
    assert "error:" in combined_output(result)


@pytest.mark.parametrize(
    ("poll_interval_seconds", "stream_timeout_seconds", "profile_count"),
    [
        (30.0, 10.0, 2),
        (5.0, 5.0, 1),
        (0.05, 0.05, 1),
        (1.0, 10.0, 2),
    ],
)
def test_the_tick_budget_covers_every_legitimate_per_profile_wait(
    poll_interval_seconds: float, stream_timeout_seconds: float, profile_count: int
) -> None:
    """INVARIANT: one tick budgets every profile's legitimate wait, with margin.

    The bound the runner applies around a call that may legitimately idle must be
    STRICTLY GREATER than the longest wait that call can take, for ANY
    (``poll_interval_seconds``, ``stream_poll_timeout_seconds``) pair, including
    equal ones and the deployment's 30 s / 10 s.  ``realtime run --once`` wraps the
    whole sequential tick in one outer ``asyncio.wait_for``, so that budget has to
    dominate the per-profile bounds it contains -- otherwise the outer deadline
    becomes the crash: with the old ``timeout * (profiles + 1)`` formula the
    deployment's two profiles got a 30 s budget that cut the second profile's own
    legitimate 30 s idle poll.

    Pure arithmetic, no clock and no duration: the check is on the invariant and on
    the fact that a timeout-dominated tick is never budgeted below the previous
    formula (``timeout * (profiles + 1)``), which is now only a floor -- the
    per-profile margin makes the new budget strictly larger for any positive wait.
    """
    from trading_platform.cli import _realtime_tick_budget
    from trading_platform.realtime.runner import stream_wait_bound

    documents = []
    for position in range(profile_count):
        document = profile(f"profile-{position}", BTC, "1h", 10000.0, 1000.0)
        document["poll_interval_seconds"] = poll_interval_seconds
        documents.append(document)
    profiles = [ProfileConfig.model_validate(document) for document in documents]
    realtime = RealtimeConfig(stream_poll_timeout_seconds=stream_timeout_seconds)

    budget = _realtime_tick_budget(profiles, realtime)

    per_profile_wait = max(float(poll_interval_seconds), float(stream_timeout_seconds))
    per_profile_bound = stream_wait_bound(per_profile_wait)
    # Strictly greater than the sum of the per-profile waits plus their margin.
    assert budget >= profile_count * per_profile_bound * math.nextafter(1.0, math.inf)
    # The previous, timeout-dominated formula stays a lower bound of the new one.
    assert budget >= float(stream_timeout_seconds) * (profile_count + 1)

    if poll_interval_seconds <= stream_timeout_seconds:
        # Timeout-dominated tick: the budget is the per-profile bound plus the one
        # bare stream timeout that pays for the shutdown, so it never shrank.
        assert budget >= profile_count * per_profile_bound


def test_the_tick_budget_covers_the_declared_wait_of_the_stream_it_builds() -> None:
    """The budget must dominate the bound of the stream the CLI really builds.

    The stream this command builds is a ``PollingMarketStream``, which declares
    ``max(poll_interval_seconds, the whole retry backoff series)``: with the shipped
    ``reconnect_backoff_seconds = 1.0`` and ``max_stream_reconnects = 5`` that series
    is 15 s, so a profile pacing at 5 s under a 10 s stream timeout still gets a
    runner bound of ``stream_wait_bound(10, 15) = 15.8 s`` -- larger than the stream
    timeout, and larger than any budget derived from the poll interval and the
    timeout alone.  The tick budget therefore has to be the sum of the per-profile
    bounds the runners apply, not one shared wait multiplied by the profile count:
    a bound narrower than the waits it wraps is the very defect this package fixes.

    Pure arithmetic on the real seam members: no clock, no tick, no duration.
    """
    from trading_platform.cli import _realtime_tick_budget
    from trading_platform.realtime.runner import stream_wait_bound
    from trading_platform.realtime.stream import PollingMarketStream

    class IdleProvider:
        """Minimal provider: this test only reads the stream's declared wait."""

        def fetch_ohlcv(self, symbol: str, timeframe: str, since: Any, until: Any) -> Any:
            return make_ohlcv(rows=2)

    realtime = RealtimeConfig(
        stream_poll_timeout_seconds=10.0,
        reconnect_backoff_seconds=1.0,
        max_stream_reconnects=5,
    )
    documents = []
    streams = []
    for position in range(2):
        document = profile(f"profile-{position}", BTC, "1h", 10000.0, 1000.0)
        document["poll_interval_seconds"] = 5.0
        documents.append(document)
        streams.append(
            PollingMarketStream(
                IdleProvider(),
                clock=ManualClock(),
                poll_interval_seconds=5.0,
                timeout_seconds=float(realtime.stream_poll_timeout_seconds),
                max_reconnects=int(realtime.max_stream_reconnects),
                reconnect_backoff_seconds=float(realtime.reconnect_backoff_seconds),
            )
        )
    profiles = [ProfileConfig.model_validate(document) for document in documents]

    # The retry series, not the cadence, is what these streams declare.
    declared = [stream.max_wait_seconds for stream in streams]
    assert declared == [15.0, 15.0]

    # The bound each of those profiles' runners applies, from the public seam.
    per_profile_bounds = [
        stream_wait_bound(float(realtime.stream_poll_timeout_seconds), wait) for wait in declared
    ]
    assert per_profile_bounds == [stream_wait_bound(10.0, 15.0)] * 2

    budget = _realtime_tick_budget(profiles, realtime)

    # Strictly greater than the sum of the bounds it wraps (the extra stream timeout
    # pays for the shutdown), so no legitimate wait of the second profile is cut.
    assert budget > sum(per_profile_bounds)
    assert budget >= sum(per_profile_bounds) + float(realtime.stream_poll_timeout_seconds)
    # And the poll-interval/timeout-only shape really was narrower than that sum:
    # it would have cut the second profile's declared wait.  This is the assertion
    # that fails on the previous budget formula.
    timeout_only = 2 * stream_wait_bound(max(5.0, float(realtime.stream_poll_timeout_seconds)))
    assert timeout_only + float(realtime.stream_poll_timeout_seconds) < sum(per_profile_bounds)
    assert budget > timeout_only


# ---------------------------------------------------------------------------
# 4. realtime serve and realtime run in server mode
# ---------------------------------------------------------------------------


def test_serve_is_read_only_over_the_persisted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve`` reads the SQLite file, binds an ephemeral port and stops cleanly."""
    from trading_platform.web import server as server_module

    database = seed_profiles(tmp_path)
    assert invoke("realtime", "run", "--state-db", str(database), "--once").exit_code == 0

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
    result = invoke("realtime", "serve", "--state-db", str(database), "--json", "--port", "0")
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


def test_serve_publishes_the_persisted_paper_ledger(tmp_path: Path) -> None:
    """The read-only surface reports the durable ledger, never a configured guess.

    ``realtime serve`` runs no engine, but the ledgers **are** persisted: the
    snapshot it rebuilds therefore carries the stored cash of the **paper** ledger,
    and every profile carries the share *attributed* to it (its allocation, its
    deployed capital and its own P&L) instead of defaulting to zero.
    """
    from trading_platform.cli import _PersistedSnapshotProvider
    from trading_platform.realtime.models import RunMode
    from trading_platform.realtime.settings import PlatformSettings, settings_from_store

    database = seed_profiles(tmp_path)
    assert invoke("realtime", "run", "--state-db", str(database), "--once").exit_code == 0

    clock = ManualClock(datetime.fromisoformat(ANCHOR))
    store = SqliteStateStore(database, clock=clock)
    store.initialize()
    try:
        realtime = settings_from_store(
            store,
            bootstrap=PlatformSettings(
                realtime=RealtimeConfig(state_db=database),
                monitoring=MonitoringConfig(),
            ),
        ).realtime
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

        # --- the three totals of the DEFECT: the platform total is the sum of the
        #     attributed per-profile figures, no longer smaller than them -----------
        per_profile_cash = sum(float(item.cash or 0.0) for item in snapshot.profiles)
        per_profile_positions = sum(float(item.position_value or 0.0) for item in snapshot.profiles)
        per_profile_equity = sum(float(item.equity or 0.0) for item in snapshot.profiles)
        assert wallet.total_cash == pytest.approx(per_profile_cash)
        assert wallet.positions_value == pytest.approx(per_profile_positions)
        assert wallet.total_portfolio_value == pytest.approx(per_profile_equity)
        assert wallet.total_portfolio_value == pytest.approx(
            wallet.total_cash + wallet.positions_value
        )
        # ... and the durable ledger cash is a DIFFERENT, equally-correct number:
        #     the entry fee of the still-open position is attributed to no profile
        fees = sum(float(item.deployed) for item in snapshot.profiles) * 0.001
        assert fees > 0.0
        assert wallet.cash == pytest.approx(wallet.total_cash - fees)

        # --- one ledger per mode, with the live one an EXPLICIT None --------------
        wallets = provider.wallets()
        assert set(wallets) == {"paper", "live"}
        assert wallets["paper"] is not None
        assert wallets["paper"].mode == RunMode.PAPER
        assert wallets["paper"].cash == pytest.approx(wallet.cash)
        assert wallets["live"] is None, "no live row exists, so there is no live ledger"
        health = provider.health()
        assert health["wallet"] == wallet.to_dict(), "the existing key is byte-identical"
        assert health["wallets"] == {"paper": wallets["paper"].to_dict(), "live": None}
    finally:
        store.close()

    # a store that never saw a wallet answers None, never an invented ledger
    empty = SqliteStateStore(tmp_path / "empty.db", clock=clock)
    empty.initialize()
    try:
        provider = _PersistedSnapshotProvider(empty, clock=clock, realtime=realtime)
        assert provider.snapshot().wallet is None
        assert provider.health()["wallet"] is None
        # both modes are present and explicitly absent: the read-only surface and the
        # engine publish IDENTICAL semantics for the same empty state
        assert provider.wallets() == {"paper": None, "live": None}
        assert provider.health()["wallets"] == {"paper": None, "live": None}
    finally:
        empty.close()


def test_run_starts_the_monitoring_server_and_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server mode wires the engine and the dashboard, then shuts both down."""
    from trading_platform.realtime.orchestrator import RealtimeOrchestrator

    database = seed_profiles(tmp_path)
    client_order_ids: list[str] = []

    async def run_forever(self: RealtimeOrchestrator) -> None:
        """One real tick, then return as a platform that stopped by itself."""
        for decision in await self.run_once():
            client_order_ids.append(decision.client_order_id)

    monkeypatch.setattr(RealtimeOrchestrator, "run_forever", run_forever)

    result = invoke("realtime", "run", "--state-db", str(database), "--json", "--port", "0")
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

    database = seed_profiles(tmp_path)
    assert invoke("realtime", "run", "--state-db", str(database), "--once").exit_code == 0

    def bounded(server: Any, *, block: bool = True) -> None:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        server.shutdown()
        thread.join(timeout=5.0)

    monkeypatch.setattr(server_module, "serve", bounded)
    result = invoke("realtime", "serve", "--state-db", str(database), "--json", "--port", "0")

    assert result.exit_code == 0
    assert stdout_of(result).strip().startswith("{")
    assert stdout_of(result).strip().endswith("}")
    assert payload_of(result)["command"] == "realtime-serve"


def test_main_returns_zero_for_a_successful_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = seed_profiles(tmp_path)

    assert main(["realtime", "check", "--state-db", str(database)]) == 0
    capsys.readouterr()


def test_main_returns_one_for_an_unusable_state_database(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("this is a file", encoding="utf-8")

    assert main(["realtime", "check", "--state-db", str(blocker / "state.db")]) == 1


def test_monitoring_config_defaults_are_documented_values() -> None:
    """The scenario relies on the documented monitoring defaults staying stable."""
    monitoring = MonitoringConfig()

    assert monitoring.host == "127.0.0.1"
    assert monitoring.refresh_seconds == 2.0


# ---------------------------------------------------------------------------
# 5. the profile surface of the monitoring server (catalog, candles, lifecycle)
# ---------------------------------------------------------------------------


def seed_state_store(database: Path, source: Path) -> None:
    """Copy the profiles of a seeded store into a **fresh** state database.

    The new store then knows both profiles **and the settings** and holds **no**
    candle: it is the exact state ``realtime serve`` reads after a deployment that
    never ran a tick.
    """
    from trading_platform.realtime.settings import SETTINGS_META_KEY, PlatformSettings

    source_store = SqliteStateStore(source, clock=ManualClock(datetime.fromisoformat(ANCHOR)))
    source_store.initialize()
    store = SqliteStateStore(database, clock=ManualClock(datetime.fromisoformat(ANCHOR)))
    store.initialize()
    try:
        for definition in source_store.load_profiles():
            store.save_profile(definition)
        stored = source_store.get_meta(SETTINGS_META_KEY)
        if stored is not None:
            store.set_meta(SETTINGS_META_KEY, stored)
        else:  # pragma: no cover - defensive: seed_profiles always writes them
            assert PlatformSettings.from_defaults() is not None
    finally:
        store.close()
        source_store.close()


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

    seeded = seed_profiles(tmp_path / "seeded")
    seed_state_store(tmp_path / "state.db", seeded)
    database = tmp_path / "state.db"
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
    result = invoke("realtime", "serve", "--state-db", str(database), "--json", "--port", "0")
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

    database = seed_profiles(tmp_path)
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

    result = invoke("realtime", "run", "--state-db", str(database), "--json", "--port", "0")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert set(payload) == RUN_KEYS
    assert recorded["read_only"] is False

    controller = recorded["controller"]
    catalog = recorded["catalog"]
    assert isinstance(controller, RuntimeProfileController)
    assert isinstance(catalog, MarketCatalog)
    assert not hasattr(controller, "profiles_path")
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


# ---------------------------------------------------------------------------
# 6. SQLite is the source of truth: zero-profile boot and the settings round trip
# ---------------------------------------------------------------------------


def test_run_once_on_an_empty_platform_exits_zero(tmp_path: Path) -> None:
    """A brand new host runs: no profile is a legal platform, not a failure."""
    database = tmp_path / "state.db"

    result = invoke("realtime", "run", "--state-db", str(database), "--once", "--json")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert set(payload) == RUN_KEYS
    assert payload["ok"] is True
    assert payload["profiles"] == []
    assert payload["decisions"] == []
    assert payload["url"] is None
    assert "Traceback" not in result.output


def test_the_historical_profiles_flag_is_an_alias_of_state_db(tmp_path: Path) -> None:
    """The shipped image CMD and every operator habit keep working."""
    database = seed_profiles(tmp_path)

    checked = invoke("realtime", "check", "--profiles", str(database), "--json")
    assert checked.exit_code == 0
    assert payload_of(checked)["state_db"] == str(database)

    ran = invoke("realtime", "run", "--profiles", str(database), "--once", "--json")
    assert ran.exit_code == 0
    assert payload_of(ran)["state_db"] == str(database)

    short = invoke("realtime", "run", "-p", str(database), "--once", "--json")
    assert short.exit_code == 0
    assert payload_of(short)["state_db"] == str(database)


def test_a_setting_changed_in_the_store_is_seen_by_the_next_run(tmp_path: Path) -> None:
    """The store is read at every boot, so a runtime update needs no file edit."""
    from trading_platform.realtime.settings import (
        PlatformSettings,
        settings_from_store,
        update_settings,
    )

    database = seed_profiles(tmp_path, realtime={"history_candles": 1})
    assert invoke("realtime", "run", "--state-db", str(database), "--once").exit_code == 0

    clock = ManualClock(datetime.fromisoformat(ANCHOR))
    store = SqliteStateStore(database, clock=clock)
    store.initialize()
    try:
        current = settings_from_store(
            store,
            bootstrap=PlatformSettings(
                realtime=RealtimeConfig(state_db=database),
                monitoring=MonitoringConfig(),
            ),
        )
        updated = update_settings(store, current=current, realtime={"history_candles": 500})
        assert updated.realtime.history_candles == 500
    finally:
        store.close()

    printed = invoke("realtime", "check", "--state-db", str(database), "--json")
    assert printed.exit_code == 0
    assert payload_of(printed)["state_db"] == str(database)

    # and the very next engine boot reads the new value out of the store
    from trading_platform.cli import _realtime_settings

    resolved = _realtime_settings(
        database, None, csv_dir=None, allow_network=True, host=None, port=None
    )
    assert resolved.realtime.history_candles == 500


def test_the_settings_seeded_on_a_fresh_database_are_the_model_defaults(tmp_path: Path) -> None:
    """A host that never customises anything behaves exactly as it did before."""
    from trading_platform.realtime.settings import PlatformSettings, settings_from_store

    database = tmp_path / "state.db"
    clock = ManualClock(datetime.fromisoformat(ANCHOR))
    store = SqliteStateStore(database, clock=clock)
    store.initialize()
    try:
        resolved = settings_from_store(
            store,
            bootstrap=PlatformSettings(
                realtime=RealtimeConfig(state_db=database),
                monitoring=MonitoringConfig(),
            ),
        )
        defaults = PlatformSettings.from_defaults()
        assert resolved.realtime.poll_interval_seconds == defaults.realtime.poll_interval_seconds
        assert resolved.monitoring.refresh_seconds == defaults.monitoring.refresh_seconds
        assert resolved.realtime.state_db == database
    finally:
        store.close()


def test_host_and_port_override_the_invocation_only(tmp_path: Path) -> None:
    """``--host``/``--port`` are a per-invocation decision, never persisted."""
    from trading_platform.cli import _realtime_settings
    from trading_platform.realtime.settings import SETTINGS_META_KEY

    database = seed_profiles(tmp_path)
    before = table_rows(database, "meta")

    resolved = _realtime_settings(
        database, None, csv_dir=None, allow_network=True, host="0.0.0.0", port=9999
    )
    assert resolved.monitoring.host == "0.0.0.0"
    assert resolved.monitoring.port == 9999

    assert table_rows(database, "meta") == before
    assert SETTINGS_META_KEY in {row[0] for row in before}


# ---------------------------------------------------------------------------
# the warm-up contract of ``realtime check``: the RESOLVED value, and the
# per-mode ledgers of the read-only ``realtime serve`` surface
# ---------------------------------------------------------------------------


def test_check_reports_the_resolved_warm_up_when_no_override_is_declared(
    tmp_path: Path,
) -> None:
    """An omitted ``warmup_candles`` reports the strategy's own requirement.

    The field used to report the model default whatever the strategy needed, so a
    profile that declared nothing could be reported as impossible while it was in
    fact served exactly what it needs.  It now reports the **resolved** warm-up:
    ``required_candles`` and ``warmup_candles`` are the same number, and neither the
    error finding nor an entry in ``issues`` can be produced by an omission.
    """
    database = seed_profiles(
        tmp_path,
        profiles=[momentum_profile("momentum-1m", "1m", warmup_candles=40321)],
        realtime={"history_candles": 50000},
    )
    # the same profile, this time declaring nothing at all
    document = momentum_profile("momentum-omitted", "1m", 40321)
    document.pop("warmup_candles")
    store_raw_profile(database, document)

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
    payload = payload_of(result)

    assert result.exit_code == 0
    assert payload["ok"] is True
    by_id = {entry["id"]: entry for entry in payload["profiles"]}
    omitted = by_id["momentum-omitted"]
    assert set(omitted["warmup"]) == WARMUP_KEYS, "the block keeps its exact five keys"
    assert omitted["ok"] is True
    assert omitted["issues"] == [], "an omission is never an issue"
    assert omitted["warmup"]["warmup_candles"] == omitted["warmup"]["required_candles"]
    assert omitted["warmup"]["warmup_candles"] == 40321, "the RESOLVED requirement"
    assert omitted["warmup"]["findings"] == []
    # ... and the explicit override of the very same profile is unchanged
    declared = by_id["momentum-1m"]
    assert declared["warmup"]["warmup_candles"] == 40321
    assert declared["warmup"]["findings"] == []


def test_check_reports_an_explicit_under_requirement_override_as_impossible(
    tmp_path: Path,
) -> None:
    """The incident is now reachable ONLY through an explicit, too-low override.

    ``strategy-warmup-impossible`` is a real finding -- it is what stops a profile
    that can never warm up -- but with the resolved semantics it can no longer be
    produced by *omitting* the field.  This is that same finding, re-pointed onto the
    configuration that really triggers it: an operator overriding the warm-up below
    what the strategy needs.
    """
    database = seed_profiles(
        tmp_path, profiles=[momentum_profile("momentum-1m", "1m", warmup_candles=200)]
    )

    result = invoke("realtime", "check", "--state-db", str(database), "--json")
    payload = payload_of(result)

    assert result.exit_code == 1
    assert payload["ok"] is False
    entry = payload["profiles"][0]
    assert entry["ok"] is False
    assert entry["warmup"]["warmup_candles"] == 200, "the explicit override is the resolved value"
    assert entry["warmup"]["required_candles"] == 40321
    assert [finding["code"] for finding in entry["warmup"]["findings"]] == [
        "strategy-warmup-impossible",
        "warmup-exceeds-history",
    ]
    # the error finding is repeated verbatim in ``issues``, the warning is not
    assert entry["issues"] == [
        finding["message"]
        for finding in entry["warmup"]["findings"]
        if finding["severity"] == "error"
    ]


def test_serve_reads_each_mode_ledger_independently(tmp_path: Path) -> None:
    """The read-only adapter publishes both ledgers, each from its own store row.

    ``realtime serve`` runs no engine, so the only honest source of a ledger is the
    durable row: the adapter reads the paper one through the defaulted ``load_wallet()``
    and the live one through ``load_wallet(mode='live')``, and a mode with no row is an
    explicit ``None``.  The two surfaces must agree, so the shapes are compared with
    the orchestrator's own contract (``{"paper": ..., "live": ...}``, both keys always
    present).
    """
    from trading_platform.cli import _PersistedSnapshotProvider
    from trading_platform.realtime.models import RunMode
    from trading_platform.realtime.settings import PlatformSettings, settings_from_store

    database = seed_profiles(tmp_path)
    assert invoke("realtime", "run", "--state-db", str(database), "--once").exit_code == 0

    clock = ManualClock(datetime.fromisoformat(ANCHOR))
    store = SqliteStateStore(database, clock=clock)
    store.initialize()
    try:
        realtime = settings_from_store(
            store,
            bootstrap=PlatformSettings(
                realtime=RealtimeConfig(state_db=database),
                monitoring=MonitoringConfig(),
            ),
        ).realtime
        provider = _PersistedSnapshotProvider(store, clock=clock, realtime=realtime)

        # (a) the engine has left the paper ledger on disk and nothing for live
        assert store.load_wallet() is not None
        assert store.load_wallet(mode="live") is None
        paper_only = provider.wallets()
        assert paper_only["live"] is None

        # (b) a live row of its own is published, and never merged into the paper one
        store.save_wallet(cash=640.0, initial_balance=500.0, mode=RunMode.LIVE)
        both = provider.wallets()
        assert set(both) == {"paper", "live"}
        assert both["paper"] is not None and both["live"] is not None
        assert both["paper"].mode == RunMode.PAPER
        assert both["live"].mode == RunMode.LIVE
        assert both["live"].source == "venue"
        assert both["live"].cash == pytest.approx(640.0)
        assert both["live"].initial_balance == pytest.approx(500.0)
        # the paper ledger is untouched by the live write
        assert both["paper"].cash == pytest.approx(paper_only["paper"].cash)  # type: ignore[union-attr]
        # no profile of this platform belongs to the live mode, so its totals are zero
        assert both["live"].profiles == 0
        assert both["live"].total_portfolio_value == pytest.approx(0.0)

        # (c) the health body carries the same mapping, normalised the same way
        health = provider.health()
        assert health["wallets"] == {
            "paper": both["paper"].to_dict(),
            "live": both["live"].to_dict(),
        }
        # ... and the snapshot's ``wallet`` key stays the paper ledger
        assert provider.snapshot().wallet is not None
        assert provider.snapshot().wallet.mode == RunMode.PAPER  # type: ignore[union-attr]
    finally:
        store.close()
