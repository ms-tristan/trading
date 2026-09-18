"""Tests of the realtime orchestrator: N profiles, one platform.

The orchestrator is the assembly point of the layer, so these tests use the **real**
:class:`~trading_platform.realtime.store.SqliteStateStore`,
:class:`~trading_platform.realtime.gateway.ExecutionGateway` and
:class:`~trading_platform.realtime.broker.PaperBroker` (all offline and
deterministic) and inject only the two seams that would otherwise reach the outside
world: the market stream and the venue adapter factory.  Nothing here binds a port,
touches the network or reads the wall clock.

The profile *strategy* is replaced by a local scripted strategy on the one test that
needs an order to be attempted (the kill-switch path), so the assertion does not
depend on the shape of a real indicator series.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_platform.config.models import (
    MonitoringConfig,
    ProfileConfig,
    RealtimeConfig,
    RiskLimitsConfig,
)
from trading_platform.core.errors import MarketStreamError, ProfileError
from trading_platform.realtime import runner as runner_module
from trading_platform.realtime.broker import PaperBroker
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import (
    CandleEvent,
    PlatformSnapshot,
    ProfileSnapshot,
    ProfileStatus,
    ReconciliationReport,
    RunMode,
    SignalAction,
    TradeSignalDecision,
)
from trading_platform.realtime.orchestrator import (
    RealtimeOrchestrator,
    default_broker_factory,
    make_live_gate,
    make_risk_manager,
)
from trading_platform.realtime.store import SqliteStateStore
from trading_platform.strategy.base import Strategy, StrategyParams, ensure_signal_frame

TIMEOUT = 30.0

#: Duration of the fake stream's own awaits: long enough that a cancellation never
#: races with the completion of an instantly-finished coroutine (a CPython
#: ``asyncio.wait_for`` hazard), short enough to keep the suite fast.
_STREAM_TICK = 0.001

#: Time the supervised loops are given to reach their pacing sleep before they are
#: cancelled.
_SETTLE = 0.05


class ParkingClock(ManualClock):
    """A manual clock whose ``sleep`` parks the caller until it is cancelled.

    The supervised loop then waits in a *genuinely pending* await, which is what a
    ``SystemClock`` does between two candles and the only situation in which CPython
    delivers a task cancellation reliably (a ``wait_for`` whose inner coroutine
    completes in the same loop cycle as the cancellation loses it).  The virtual
    time never advances, so the test stays fully deterministic.
    """

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(3600.0)


START = pd.Timestamp("2024-01-01T00:00:00Z")
SYMBOL_BTC = "BTC/USDT"
SYMBOL_ETH = "ETH/USDT"

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


def run(coro: Any) -> Any:
    """Run one coroutine under an explicit bound so nothing can hang the suite."""
    return asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


# ---------------------------------------------------------------------------
# local fakes and helpers
# ---------------------------------------------------------------------------


def make_frame(symbol: str = SYMBOL_BTC, rows: int = 6, *, base: float = 100.0) -> pd.DataFrame:
    """Return a deterministic hourly OHLCV frame."""
    index = pd.date_range(start=START, periods=rows, freq="h", tz=UTC, name="timestamp")
    close = [base + float(position) for position in range(rows)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [value + 1.0 for value in close],
            "low": [value - 1.0 for value in close],
            "close": close,
            "volume": [10.0] * rows,
        },
        index=index,
    )


class FakeStream:
    """Deterministic :class:`MarketStream` owned by one profile."""

    def __init__(self, symbol: str, *, cursor: int = 1, rows: int = 6) -> None:
        self.symbol = symbol
        self.frame = make_frame(symbol, rows=rows)
        self.cursor = int(cursor)
        self.started = 0
        self.stopped = 0
        self.calls = 0
        self.connected = True
        self.last_error: str | None = None
        self.reconnect_count = 0

    def seek(self, cursor: int) -> None:
        """Move the emission cursor."""
        self.cursor = int(cursor)

    async def start(self) -> None:
        self.started += 1
        await asyncio.sleep(_STREAM_TICK)

    async def stop(self) -> None:
        self.stopped += 1
        await asyncio.sleep(_STREAM_TICK)

    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
        self.calls += 1
        await asyncio.sleep(_STREAM_TICK)
        if self.cursor >= len(self.frame):
            return None
        row = self.frame.iloc[self.cursor]
        event = CandleEvent(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=pd.Timestamp(self.frame.index[self.cursor]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        self.cursor += 1
        return event

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        await asyncio.sleep(_STREAM_TICK)
        return self.frame.iloc[: self.cursor].iloc[-int(count) :].copy()


class MismatchBroker(PaperBroker):
    """A paper venue that reports a reconciliation mismatch."""

    def reconcile(self, expected: Any = ()) -> ReconciliationReport:
        """Return a report that disagrees with the persisted orders."""
        return ReconciliationReport(
            profile_id="",
            ok=False,
            checked_at=START,
            matched=0,
            only_at_venue=("venue-only-order",),
            only_locally=tuple(order.client_order_id for order in expected),
            mismatched=(),
            details={"reason": "offline test venue"},
        )


class _ScriptedParams(StrategyParams):
    """Parameters of :class:`_ScriptedStrategy`."""

    mode: str = "hold"
    allow_short: bool = False


class _ScriptedStrategy(Strategy):
    """A real strategy whose only signal is dictated by a mutable flag."""

    name = "orchestrator-scripted"
    ParamsModel = _ScriptedParams

    def __init__(self, params: Any = None, **kwargs: Any) -> None:
        super().__init__(params)
        shared = kwargs.get("flag")
        self.flag: dict[str, Any] = {} if shared is None else shared

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the frame untouched (no indicator is needed here)."""
        return data.copy()

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return a signal frame opening a long on the last candle."""
        params = self._params
        assert isinstance(params, _ScriptedParams)
        columns = ["entry_long", "exit_long", "entry_short", "exit_short", "stop_loss"]
        frame = pd.DataFrame(False, index=data.index, columns=columns)
        frame["stop_loss"] = float("nan")
        if str(self.flag.get("mode") or params.mode) == "entry_long":
            frame.loc[data.index[-1], "entry_long"] = True
        return ensure_signal_frame(frame, data.index)


def profile(identifier: str, symbol: str, **overrides: Any) -> ProfileConfig:
    """Return a valid paper profile."""
    payload: dict[str, Any] = {
        "id": identifier,
        "symbol": symbol,
        "timeframe": "1h",
        "strategy": "basic",
        "mode": "paper",
        "initial_balance": 1000.0,
        "warmup_candles": 5,
        "poll_interval_seconds": 0.01,
        "risk": RiskLimitsConfig(max_open_positions=1),
    }
    payload.update(overrides)
    return ProfileConfig(**payload)


def realtime_config(tmp_path: Path, **overrides: Any) -> RealtimeConfig:
    """Return a realtime configuration rooted in the test's temporary directory."""
    payload: dict[str, Any] = {
        "state_db": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
        "stream_poll_timeout_seconds": 30.0,
    }
    payload.update(overrides)
    return RealtimeConfig(**payload)


def build_orchestrator(
    tmp_path: Path,
    profiles: list[ProfileConfig],
    *,
    clock: ManualClock | None = None,
    store: SqliteStateStore | None = None,
    stream_factory: Any = None,
    broker_factory: Any = None,
    realtime: RealtimeConfig | None = None,
    environ: dict[str, str] | None = None,
    version: str = "0.1.0-test",
) -> tuple[RealtimeOrchestrator, SqliteStateStore, ManualClock, list[FakeStream]]:
    """Build an orchestrator wired with fake streams and paper venues."""
    resolved_clock = (
        ParkingClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC)) if clock is None else clock
    )
    resolved_store = (
        SqliteStateStore(tmp_path / "state.db", clock=resolved_clock) if store is None else store
    )
    streams: list[FakeStream] = []

    def factory(target: ProfileConfig) -> FakeStream:
        stream = FakeStream(str(target.symbol))
        streams.append(stream)
        return stream

    def venues(target: ProfileConfig) -> PaperBroker:
        return PaperBroker(
            clock=resolved_clock, initial_balance=float(target.initial_balance), seed=7
        )

    orchestrator = RealtimeOrchestrator(
        profiles=profiles,
        store=resolved_store,
        clock=resolved_clock,
        realtime=realtime_config(tmp_path) if realtime is None else realtime,
        monitoring=MonitoringConfig(port=0),
        stream_factory=factory if stream_factory is None else stream_factory,
        broker_factory=venues if broker_factory is None else broker_factory,
        environ={} if environ is None else environ,
        version=version,
    )
    return orchestrator, resolved_store, resolved_clock, streams


@pytest.fixture
def logs() -> Any:
    """Attach a capturing handler to the realtime logger for one test."""
    logger = logging.getLogger("trading_platform.realtime")
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def events(records: list[logging.LogRecord]) -> list[str]:
    """Return the event names of the captured records."""
    return [str(getattr(record, "event", record.getMessage())) for record in records]


# ---------------------------------------------------------------------------
# 9. N profiles concurrently
# ---------------------------------------------------------------------------


def test_two_profiles_run_one_tick_each_in_id_order(tmp_path: Path) -> None:
    """``run_once`` ticks every enabled profile, sequentially by profile id."""
    slow = profile("aaa-paper", SYMBOL_BTC)
    fast = profile("zzz-paper", SYMBOL_ETH)
    orchestrator, store, _clock, streams = build_orchestrator(tmp_path, [fast, slow])

    decisions = run(orchestrator.run_once())
    assert [decision.profile_id for decision in decisions] == ["aaa-paper", "zzz-paper"]
    assert all(isinstance(decision, TradeSignalDecision) for decision in decisions)
    assert [stream.calls for stream in streams] == [1, 1]

    for identifier in ("aaa-paper", "zzz-paper"):
        assert len(store.equity_curve(identifier)) == 1
        state = store.load_status(identifier)
        assert state is not None
        assert state.status is ProfileStatus.RUNNING
    assert [saved.id for saved in store.load_profiles()] == ["aaa-paper", "zzz-paper"]
    assert store.last_processed_candle("aaa-paper") == pd.Timestamp(make_frame().index[1])
    assert orchestrator.profile_ids() == ["aaa-paper", "zzz-paper"]
    assert orchestrator.runner("aaa-paper") is not None
    assert orchestrator.runner("unknown") is None
    store.close()


def test_start_and_stop_leave_every_profile_stopped(tmp_path: Path) -> None:
    """``start`` supervises one task per profile; ``stop`` persists ``STOPPED``."""
    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]
    orchestrator, _store, _clock, streams = build_orchestrator(tmp_path, profiles)

    async def scenario() -> None:
        await orchestrator.start()
        await asyncio.sleep(_SETTLE)
        await orchestrator.stop()

    run(scenario())
    assert [stream.stopped for stream in streams] == [1, 1]
    assert all(stream.started == 1 for stream in streams)

    reopened = SqliteStateStore(tmp_path / "state.db")
    reopened.initialize()
    try:
        for identifier in ("btc-paper", "eth-paper"):
            state = reopened.load_status(identifier)
            assert state is not None
            assert state.status is ProfileStatus.STOPPED
    finally:
        reopened.close()


def test_run_forever_returns_when_nothing_is_enabled(tmp_path: Path) -> None:
    """A platform with no enabled profile has no task to await."""
    profiles = [profile("btc-paper", SYMBOL_BTC, enabled=False)]
    orchestrator, store, _clock, _streams = build_orchestrator(tmp_path, profiles)
    run(orchestrator.run_forever())
    assert orchestrator.runner("btc-paper") is None
    assert store.load_profiles()[0].enabled is False
    store.close()


# ---------------------------------------------------------------------------
# 10. reconciliation mismatch -> DEGRADED
# ---------------------------------------------------------------------------


def test_a_reconciliation_mismatch_marks_the_profile_degraded(tmp_path: Path, logs: Any) -> None:
    """A mismatching venue degrades the profile, with the report, and sends nothing."""
    profiles = [profile("btc-paper", SYMBOL_BTC)]
    venues: list[PaperBroker] = []

    def mismatching(target: ProfileConfig) -> PaperBroker:
        broker = MismatchBroker(
            clock=ManualClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC)),
            initial_balance=float(target.initial_balance),
            seed=3,
        )
        venues.append(broker)
        return broker

    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, profiles, broker_factory=mismatching
    )

    async def scenario() -> Any:
        await orchestrator.start()
        await asyncio.sleep(_SETTLE)
        state = store.load_status("btc-paper")
        snapshot = orchestrator.profile_snapshot("btc-paper")
        orders = store.list_orders("btc-paper")
        await orchestrator.stop()
        return state, snapshot, orders

    state, snapshot, orders = run(scenario())
    assert state is not None
    assert state.status is ProfileStatus.DEGRADED
    assert "venue-only-order" in (state.last_error or "")
    assert snapshot is not None
    assert snapshot.status is ProfileStatus.DEGRADED
    assert "reconciliation_mismatch" in events(logs)
    assert orders == []
    assert venues[0].open_orders() == []


def test_the_reconciliation_detail_is_persisted(tmp_path: Path) -> None:
    """The stored detail is the JSON report, so an operator sees *what* diverged."""

    def mismatching(target: ProfileConfig) -> MismatchBroker:
        return MismatchBroker(
            clock=ManualClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC)),
            initial_balance=float(target.initial_balance),
            seed=3,
        )

    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, [profile("btc-paper", SYMBOL_BTC)], broker_factory=mismatching
    )
    run(orchestrator.run_once())
    state = store.load_status("btc-paper")
    assert state is not None
    assert state.status is ProfileStatus.DEGRADED
    assert "venue-only-order" in (state.last_error or "")
    store.close()


def test_a_successful_reconciliation_leaves_the_profile_running(tmp_path: Path) -> None:
    """The nominal case is silent and keeps the profile healthy."""
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, [profile("btc-paper", SYMBOL_BTC)]
    )
    run(orchestrator.run_once())
    state = store.load_status("btc-paper")
    assert state is not None
    assert state.status is ProfileStatus.RUNNING
    store.close()


# ---------------------------------------------------------------------------
# 11. the global kill switch
# ---------------------------------------------------------------------------


def test_the_kill_switch_blocks_every_profile_and_survives_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engaging the halt blocks every enabled profile, durably."""
    flag: dict[str, Any] = {"mode": "hold"}
    scripted = _ScriptedStrategy({}, flag=flag)
    monkeypatch.setattr(runner_module, "resolve_strategy", lambda _profile: scripted)

    profiles = [profile("aaa-paper", SYMBOL_BTC), profile("zzz-paper", SYMBOL_ETH)]
    orchestrator, store, _clock, _streams = build_orchestrator(tmp_path, profiles)

    assert orchestrator.kill_switch_state().engaged is False
    first = run(orchestrator.run_once())
    assert [decision.blocked for decision in first] == [False, False]
    assert [decision.action for decision in first] == [SignalAction.HOLD, SignalAction.HOLD]
    assert store.list_orders("aaa-paper") == []

    flag["mode"] = "entry_long"
    state = orchestrator.engage_kill_switch("operator halted the desk")
    assert state.engaged is True
    blocked = run(orchestrator.run_once())
    assert [decision.profile_id for decision in blocked] == ["aaa-paper", "zzz-paper"]
    assert all(decision.blocked for decision in blocked)
    assert all("kill switch" in decision.block_reason for decision in blocked)
    assert all(decision.client_order_id for decision in blocked)
    assert store.list_orders("aaa-paper") == []
    assert orchestrator.health()["kill_switch"] is True
    assert orchestrator.health()["status"] == "degraded"

    run(orchestrator.stop())
    reopened = SqliteStateStore(tmp_path / "state.db")
    second = RealtimeOrchestrator(
        profiles=profiles,
        store=reopened,
        clock=ParkingClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC)),
        realtime=realtime_config(tmp_path),
        stream_factory=lambda target: FakeStream(str(target.symbol)),
        broker_factory=lambda target: PaperBroker(
            clock=ManualClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC)),
            initial_balance=float(target.initial_balance),
        ),
        environ={},
    )
    assert second.kill_switch_state().engaged is True
    assert second.kill_switch_state().reason == "operator halted the desk"
    released = second.release_kill_switch()
    assert released.engaged is False
    reopened.close()


def test_the_kill_switch_can_be_forced_by_a_file(tmp_path: Path) -> None:
    """The file force wins and survives a release attempt."""
    from trading_platform.core.errors import KillSwitchActiveError

    flag = tmp_path / "KILL"
    flag.write_text("operator\n", encoding="utf-8")
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path,
        [profile("btc-paper", SYMBOL_BTC)],
        realtime=realtime_config(tmp_path, kill_switch_file=flag),
    )
    state = orchestrator.kill_switch_state()
    assert state.engaged is True
    assert state.source == "file"
    with pytest.raises(KillSwitchActiveError):
        orchestrator.release_kill_switch()
    store.close()


def test_a_failing_profile_does_not_take_the_platform_down(tmp_path: Path, logs: Any) -> None:
    """One profile's stream failure stops that profile, and only that one."""

    class FailingStream(FakeStream):
        async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
            raise MarketStreamError("the venue closed the stream")

    def factory(target: ProfileConfig) -> FakeStream:
        if target.id == "eth-paper":
            return FailingStream(str(target.symbol))
        return FakeStream(str(target.symbol))

    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]
    orchestrator, _store, _clock, _streams = build_orchestrator(
        tmp_path, profiles, stream_factory=factory
    )

    async def scenario() -> Any:
        await orchestrator.start()
        await asyncio.sleep(_SETTLE)
        broken = orchestrator.profile_snapshot("eth-paper")
        await orchestrator.stop()
        return broken

    broken = run(scenario())
    assert "profile_stopped_on_error" in events(logs)
    assert "profile_error" in events(logs)
    assert broken is not None
    assert broken.status is ProfileStatus.ERROR

    reopened = SqliteStateStore(tmp_path / "state.db")
    reopened.initialize()
    try:
        stored = reopened.load_status("eth-paper")
        assert stored is not None
        assert stored.status is ProfileStatus.STOPPED
        assert "the venue closed the stream" in (stored.last_error or "")
    finally:
        reopened.close()


def test_health_before_any_start_is_zeroed(tmp_path: Path) -> None:
    """A platform that never ran reports a zero uptime and stopped profiles."""
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, [profile("btc-paper", SYMBOL_BTC)]
    )
    health = orchestrator.health()
    assert set(health) == HEALTH_KEYS
    assert health["uptime_seconds"] == 0.0
    assert health["profiles_running"] == 0
    assert health["profiles_total"] == 1
    snapshot = orchestrator.snapshot()
    assert snapshot.uptime_seconds == 0.0
    assert snapshot.started_at is None
    assert snapshot.profiles[0].status is ProfileStatus.STOPPED
    assert orchestrator.stats()["btc-paper"]["candles_processed"] == 0
    store.close()


# ---------------------------------------------------------------------------
# 12. snapshot and health
# ---------------------------------------------------------------------------


def test_snapshot_and_health_expose_the_documented_payload(tmp_path: Path) -> None:
    """One entry per profile, the documented health keys, and no NaN anywhere."""
    profiles = [
        profile("btc-paper", SYMBOL_BTC),
        profile("eth-paper", SYMBOL_ETH),
        profile("sol-paper", "SOL/USDT", enabled=False),
    ]
    orchestrator, store, _clock, _streams = build_orchestrator(tmp_path, profiles)
    decisions = run(orchestrator.run_once())
    assert [decision.profile_id for decision in decisions] == ["btc-paper", "eth-paper"]

    snapshot = orchestrator.snapshot()
    assert isinstance(snapshot, PlatformSnapshot)
    assert [item.profile_id for item in snapshot.profiles] == [
        "btc-paper",
        "eth-paper",
        "sol-paper",
    ]
    assert all(isinstance(item, ProfileSnapshot) for item in snapshot.profiles)
    assert snapshot.version == "0.1.0-test"
    disabled = orchestrator.profile_snapshot("sol-paper")
    assert disabled is not None
    assert disabled.status is ProfileStatus.STOPPED
    assert disabled.equity == pytest.approx(1000.0)
    assert orchestrator.profile_snapshot("nope") is None

    payload = snapshot.to_dict()
    assert json.dumps(payload)  # no NaN/Inf can survive the encoder
    assert "NaN" not in json.dumps(payload)
    assert payload["kill_switch"] is False
    assert payload["profiles"][0]["health"]["counters"]["candles_processed"] == 1
    assert snapshot.uptime_seconds >= 0.0

    health = orchestrator.health()
    assert set(health) == HEALTH_KEYS
    assert health["status"] == "ok"
    assert health["profiles_total"] == 3
    assert health["profiles_running"] == 2
    assert health["kill_switch"] is False
    assert health["version"] == "0.1.0-test"
    assert isinstance(health["uptime_seconds"], float)
    assert health["uptime_seconds"] >= 0.0
    assert isinstance(health["checked_at"], str)
    stats = orchestrator.stats()
    assert sorted(stats) == ["btc-paper", "eth-paper", "sol-paper"]
    assert stats["btc-paper"]["candles_processed"] == 1
    assert stats["sol-paper"] == {
        "candles_processed": 0,
        "orders_submitted": 0,
        "orders_filled": 0,
        "orders_rejected": 0,
        "stream_reconnects": 0,
        "risk_rejections": 0,
        "errors": 0,
    }
    store.close()


def test_a_read_model_that_cannot_read_reports_a_stopped_profile(tmp_path: Path) -> None:
    """A closed store never breaks the dashboard: the profile is reported stopped."""
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, [profile("btc-paper", SYMBOL_BTC)]
    )
    store.close()
    snapshot = orchestrator.profile_snapshot("btc-paper")
    assert snapshot is not None
    assert snapshot.status is ProfileStatus.STOPPED
    assert snapshot.equity == pytest.approx(1000.0)
    assert snapshot.n_trades == 0


# ---------------------------------------------------------------------------
# 13. the constructor and the wiring refuse the impossible
# ---------------------------------------------------------------------------


def test_an_empty_profile_list_is_refused(tmp_path: Path) -> None:
    """A platform with no profile is a configuration mistake."""
    with pytest.raises(ProfileError, match="empty"):
        RealtimeOrchestrator(
            profiles=[],
            store=SqliteStateStore(tmp_path / "state.db"),
            clock=ManualClock(),
            realtime=RealtimeConfig(),
        )


def test_a_duplicate_profile_id_is_refused(tmp_path: Path) -> None:
    """Two profiles sharing an id would share a state: refused loudly."""
    duplicated = [
        profile("btc-paper", SYMBOL_BTC),
        profile("btc-paper", SYMBOL_ETH),
    ]
    with pytest.raises(ProfileError, match="duplicate profile id"):
        RealtimeOrchestrator(
            profiles=duplicated,
            store=SqliteStateStore(tmp_path / "state.db"),
            clock=ManualClock(),
            realtime=RealtimeConfig(),
        )


def test_a_missing_stream_factory_is_refused(tmp_path: Path) -> None:
    """A market-data source is a decision the caller must make explicitly."""
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, [profile("btc-paper", SYMBOL_BTC)], stream_factory=None
    )
    orchestrator._stream_factory = None  # the seam under test
    with pytest.raises(MarketStreamError, match="no market stream factory"):
        run(orchestrator.run_once())
    store.close()


# ---------------------------------------------------------------------------
# the wiring helpers
# ---------------------------------------------------------------------------


def test_default_broker_factory_builds_a_paper_venue(tmp_path: Path) -> None:
    """A paper profile gets a deterministic simulated venue."""
    clock = ManualClock()
    target = profile("btc-paper", SYMBOL_BTC)
    first = default_broker_factory(target, clock=clock, environ={})
    second = default_broker_factory(target, clock=clock, environ={})
    assert isinstance(first, PaperBroker)
    assert first.name == "paper"
    assert first.mode is RunMode.PAPER
    assert first.initial_balance == pytest.approx(1000.0)
    assert type(first) is type(second)
    assert first.name == second.name
    assert "PaperBroker" in repr(first)


def test_default_broker_factory_refuses_a_live_profile_without_credentials() -> None:
    """Missing credentials are a configuration error, raised before any order."""
    target = profile("btc-live", SYMBOL_BTC, mode="live")
    with pytest.raises(ProfileError, match="no credentials"):
        default_broker_factory(target, clock=ManualClock(), environ={})


def test_default_broker_factory_builds_a_live_venue_from_the_environment() -> None:
    """With credentials in the environment the live adapter is built, lazily."""
    target = profile("btc-live", SYMBOL_BTC, mode="live")
    environ = {"TB_LIVE_API_KEY": "key-1234", "TB_LIVE_API_SECRET": "secret-5678"}
    broker = default_broker_factory(target, clock=ManualClock(), environ=environ)
    assert type(broker).__name__ == "CcxtBroker"
    assert broker.mode is RunMode.LIVE
    assert broker.name == "binance"


def test_make_live_gate_follows_the_environment() -> None:
    """Only the exact opt-in value arms a live profile."""
    gate = make_live_gate({})
    assert gate.allowed(profile("btc-paper", SYMBOL_BTC)) is True
    live = profile("btc-live", SYMBOL_BTC, mode="live")
    assert gate.allowed(live) is False
    armed = make_live_gate({"TB_ALLOW_LIVE_TRADING": "I_UNDERSTAND_THE_RISK"})
    assert armed.allowed(live) is True


def test_make_risk_manager_carries_the_profile_limits() -> None:
    """The per-profile limits and the kill switch reach the manager."""
    from trading_platform.realtime.risk import KillSwitch

    clock = ManualClock()
    target = profile(
        "btc-paper",
        SYMBOL_BTC,
        risk=RiskLimitsConfig(max_order_notional=100.0, max_open_positions=2),
    )
    switch = KillSwitch(clock=clock, environ={})
    manager = make_risk_manager(target, clock=clock, kill_switch=switch)
    assert manager.limits.max_order_notional == pytest.approx(100.0)
    assert manager.limits.max_open_positions == 2
    assert manager.to_dict()["kill_switch"] is False


def test_the_orchestrator_repr_is_short_and_secret_free(tmp_path: Path) -> None:
    """The representation names the platform, never a value."""
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, [profile("btc-paper", SYMBOL_BTC)], environ={"TB_LIVE_API_KEY": "key-1234"}
    )
    text = repr(orchestrator)
    assert "key-1234" not in text
    assert "RealtimeOrchestrator" in text
    assert orchestrator.monitoring is not None
    assert orchestrator.realtime.stream_poll_timeout_seconds == pytest.approx(30.0)
    assert [item.id for item in orchestrator.profiles] == ["btc-paper"]
    store.close()


def test_the_scripted_strategy_used_by_the_kill_switch_test_is_a_real_strategy() -> None:
    """The local fake honours the same ``run`` entry point as the registry."""
    strategy = _ScriptedStrategy({"mode": "entry_long"})
    _prepared, signals = strategy.run(make_frame())
    assert bool(signals.iloc[-1]["entry_long"]) is True
    assert signals.iloc[0]["entry_long"] == False  # noqa: E712 - a numpy-free check
    assert list(signals.columns) == [
        "entry_long",
        "exit_long",
        "entry_short",
        "exit_short",
        "stop_loss",
    ]


def test_the_hold_mode_of_the_scripted_strategy_never_opens(tmp_path: Path) -> None:
    """A hold decision still writes the equity point of the tick."""
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, [profile("btc-paper", SYMBOL_BTC)]
    )
    decisions = run(orchestrator.run_once())
    assert decisions[0].action is SignalAction.HOLD
    assert decisions[0].blocked is False
    assert len(store.equity_curve("btc-paper")) == 1
    store.close()
