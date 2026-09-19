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

from trading_platform.config.loader import load_profiles
from trading_platform.config.models import (
    MonitoringConfig,
    ProfileConfig,
    RealtimeConfig,
    RiskLimitsConfig,
    resolve_platform_initial_balance,
)
from trading_platform.core.errors import ConfigError, MarketStreamError, ProfileError
from trading_platform.core.models import Direction
from trading_platform.realtime import runner as runner_module
from trading_platform.realtime.broker import PaperBroker
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import (
    BrokerAck,
    CandleEvent,
    OrderSide,
    OrderState,
    OrderType,
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
from trading_platform.realtime.wallet import PlatformWallet
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

#: The exact key set of the ``/api/health`` body (the shared wallet view included).
HEALTH_KEYS = frozenset(
    {
        "status",
        "version",
        "uptime_seconds",
        "profiles_total",
        "profiles_running",
        "kill_switch",
        "checked_at",
        "wallet",
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
        mode = str(self.flag.get("mode") or params.mode)
        if mode == "entry_long":
            frame.loc[data.index[-1], "entry_long"] = True
            stop = self.flag.get("stop_loss")
            if stop is not None:
                frame.loc[data.index[-1], "stop_loss"] = float(stop)
        elif mode == "exit_long":
            frame.loc[data.index[-1], "exit_long"] = True
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
    wallet: PlatformWallet | None = None,
) -> tuple[RealtimeOrchestrator, SqliteStateStore, ManualClock, list[FakeStream]]:
    """Build an orchestrator wired with fake streams and paper venues.

    The venues share **one** :class:`PlatformWallet` built exactly like the
    orchestrator builds its own -- the platform initial balance, or the sum of the
    profiles' allocations -- because that is what the production wiring does: every
    simulated profile spends the same USDT ledger.  An explicit ``wallet`` is used
    as-is, which is how a test pins one identity across the whole platform.
    """
    resolved_clock = (
        ParkingClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC)) if clock is None else clock
    )
    resolved_store = (
        SqliteStateStore(tmp_path / "state.db", clock=resolved_clock) if store is None else store
    )
    resolved_realtime = realtime_config(tmp_path) if realtime is None else realtime
    shared = wallet
    if shared is None:
        live = any(str(item.mode) == RunMode.LIVE.value for item in profiles if item.enabled)
        shared = PlatformWallet(
            initial_balance=resolve_platform_initial_balance(resolved_realtime, profiles),
            mode=RunMode.LIVE if live else RunMode.PAPER,
            store=resolved_store,
            clock=resolved_clock,
            name="platform",
        )
    streams: list[FakeStream] = []

    def factory(target: ProfileConfig) -> FakeStream:
        stream = FakeStream(str(target.symbol))
        streams.append(stream)
        return stream

    def venues(target: ProfileConfig) -> PaperBroker:
        return PaperBroker(
            clock=resolved_clock,
            initial_balance=float(target.initial_balance),
            seed=7,
            wallet=shared if shared.is_authoritative else None,
        )

    orchestrator = RealtimeOrchestrator(
        profiles=profiles,
        store=resolved_store,
        clock=resolved_clock,
        realtime=resolved_realtime,
        monitoring=MonitoringConfig(port=0),
        stream_factory=factory if stream_factory is None else stream_factory,
        broker_factory=venues if broker_factory is None else broker_factory,
        environ={} if environ is None else environ,
        version=version,
        wallet=shared,
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
# 12b. the one shared platform wallet
# ---------------------------------------------------------------------------


class CountingWallet(PlatformWallet):
    """A shared wallet that counts how many times the boot restored it."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.restores: list[float | None] = []

    def restore(self) -> float | None:
        """Restore as usual, remembering what the durable state answered."""
        result = super().restore()
        self.restores.append(result)
        return result


class LiveVenueBroker:
    """Offline stand-in for a live venue: it reports a balance and nothing else.

    It exists so the live half of the wallet wiring is exercised without a network
    call, a credential or a fixed port: no order is ever accepted here.
    """

    def __init__(
        self, *, balance: float | None = 640.0, error: BaseException | None = None
    ) -> None:
        self.balance_value = balance
        self.error = error
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake-live"

    @property
    def mode(self) -> RunMode:
        return RunMode.LIVE

    def fetch_balance(self) -> float | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.balance_value

    def submit(self, request: Any, *, reference_price: float) -> Any:
        raise AssertionError("no order may reach the venue in these tests")

    def cancel(self, client_order_id: str) -> bool:
        return False

    def poll(self) -> list[Any]:
        return []

    def open_orders(self) -> list[Any]:
        return []

    def reconcile(self, expected: Any = ()) -> ReconciliationReport:
        return ReconciliationReport(
            profile_id="", ok=True, checked_at=START, matched=len(list(expected))
        )


def test_every_profile_funds_its_orders_from_the_one_wallet(tmp_path: Path) -> None:
    """The production venue factory hands *the same* wallet to every paper profile."""
    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]
    clock = ManualClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC))
    streams: list[FakeStream] = []

    def factory(target: ProfileConfig) -> FakeStream:
        stream = FakeStream(str(target.symbol))
        streams.append(stream)
        return stream

    store = SqliteStateStore(tmp_path / "state.db", clock=clock)
    orchestrator = RealtimeOrchestrator(
        profiles=profiles,
        store=store,
        clock=clock,
        realtime=realtime_config(tmp_path),
        monitoring=MonitoringConfig(port=0),
        stream_factory=factory,
        version="wallet-test",
    )
    wallet = orchestrator.wallet
    assert orchestrator.wallet is wallet, "the wallet is built once per orchestrator"
    assert wallet.name == "platform"
    assert wallet.mode is RunMode.PAPER
    assert wallet.initial_balance == pytest.approx(2000.0), "the sum of the allocations"

    run(orchestrator.run_once())
    first = orchestrator.runner("btc-paper").gateway.broker  # type: ignore[union-attr]
    second = orchestrator.runner("eth-paper").gateway.broker  # type: ignore[union-attr]
    assert first is not second, "one venue per profile"
    assert first.wallet is wallet
    assert second.wallet is wallet
    store.close()


def test_the_shared_wallet_is_restored_once_from_the_durable_state(
    tmp_path: Path, logs: Any
) -> None:
    """A restart adopts the persisted ledger; the boot restores it exactly once."""
    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]
    clock = ManualClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC))
    store = SqliteStateStore(tmp_path / "state.db", clock=clock)
    store.initialize()
    store.save_wallet(cash=777.5, initial_balance=2000.0)
    wallet = CountingWallet(store=store, clock=clock, initial_balance=2000.0, name="platform")
    orchestrator, _store, _clock, _streams = build_orchestrator(
        tmp_path, profiles, clock=clock, store=store, wallet=wallet
    )

    async def scenario() -> PlatformWallet:
        await orchestrator.run_once()
        await orchestrator.run_once()
        return orchestrator.wallet

    assert run(scenario()) is wallet
    assert wallet.restores == [777.5], "the persisted cash, adopted exactly once"
    assert wallet.initial_balance == pytest.approx(2000.0)
    restored_events = [
        record
        for record in logs
        if getattr(record, "event", "") == "platform_wallet_restored"
        and record.context.get("restored") is True
    ]
    assert len(restored_events) == 1
    assert restored_events[0].context["cash"] == pytest.approx(777.5)
    assert restored_events[0].context["mode"] == "paper"
    store.close()


def test_a_platform_without_a_persisted_wallet_starts_from_the_configuration(
    tmp_path: Path, logs: Any
) -> None:
    """No persisted row: the wallet starts at the sum of the allocations and says so."""
    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]
    clock = ManualClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC))
    store = SqliteStateStore(tmp_path / "state.db", clock=clock)
    wallet = CountingWallet(store=store, clock=clock, initial_balance=2000.0, name="platform")
    orchestrator, _store, _clock, _streams = build_orchestrator(
        tmp_path, profiles, clock=clock, store=store, wallet=wallet
    )

    run(orchestrator.run_once())

    assert wallet.restores == [None]
    assert store.load_wallet() is not None, "the first row is written for the next boot"
    assert store.load_wallet().cash == pytest.approx(wallet.cash)  # type: ignore[union-attr]
    restored_events = [
        record
        for record in logs
        if getattr(record, "event", "") == "platform_wallet_restored"
        and record.context.get("restored") is False
    ]
    assert len(restored_events) == 1
    store.close()


def test_the_orchestrator_builds_its_wallet_from_the_platform_configuration(
    tmp_path: Path,
) -> None:
    """``realtime.platform_initial_balance`` wins over the sum of the allocations."""
    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path,
        profiles,
        realtime=realtime_config(tmp_path, platform_initial_balance=2500.0),
    )
    assert orchestrator.wallet.initial_balance == pytest.approx(2500.0)
    assert orchestrator.wallet.cash == pytest.approx(2500.0)
    store.close()


def test_the_snapshot_and_health_expose_the_shared_wallet_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform payload carries the wallet, aggregated from the profile figures."""
    flag: dict[str, Any] = {"mode": "entry_long"}
    scripted(monkeypatch, flag)
    profiles = [
        profile("btc-paper", SYMBOL_BTC, stake_amount=250.0),
        profile("eth-paper", SYMBOL_ETH, stake_amount=250.0),
    ]
    orchestrator, store, _clock, _streams = build_orchestrator(tmp_path, profiles)

    async def scenario() -> PlatformSnapshot:
        await orchestrator.run_once()
        return orchestrator.snapshot()

    snapshot = run(scenario())
    wallet = snapshot.wallet
    assert wallet is not None
    assert wallet.name == "platform"
    assert wallet.source == "local"
    assert wallet.profiles == 2
    assert wallet.initial_balance == pytest.approx(2000.0)
    assert [item.open_positions for item in snapshot.profiles] == [1, 1]
    assert wallet.deployed == pytest.approx(sum(item.deployed for item in snapshot.profiles))
    assert wallet.realized_pnl == pytest.approx(
        sum(item.realized_pnl for item in snapshot.profiles)
    )
    assert wallet.unrealized_pnl == pytest.approx(
        sum(item.unrealized_pnl for item in snapshot.profiles)
    )
    assert wallet.total_exposure == pytest.approx(
        sum(abs(item.position_value) for item in snapshot.profiles)
    )
    assert wallet.equity == pytest.approx(
        wallet.cash + sum(item.position_value for item in snapshot.profiles)
    )
    # the documented deviation: the entry fees of the still-open positions are not
    # attributed to any profile yet, so the ledger holds exactly that much less
    fees = sum(item.deployed for item in snapshot.profiles) * 0.001
    assert wallet.cash == pytest.approx(
        2000.0 - sum(item.deployed for item in snapshot.profiles) - fees
    )
    assert sum(item.cash for item in snapshot.profiles) - wallet.cash == pytest.approx(fees)

    payload = snapshot.to_dict()
    assert payload["wallet"] == wallet.to_dict()
    assert payload["wallet"]["cash"] == pytest.approx(wallet.cash)
    health = orchestrator.health()
    assert set(health) == HEALTH_KEYS
    assert health["wallet"] == payload["wallet"]
    assert json.dumps(health, allow_nan=False)
    store.close()


def test_the_platform_risk_state_is_fed_by_every_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform cap reads what the *other* profile published, through one state."""
    flag: dict[str, Any] = {"mode": "entry_long"}
    scripted(monkeypatch, flag)
    profiles = [
        profile("btc-paper", SYMBOL_BTC, stake_amount=250.0),
        profile("eth-paper", SYMBOL_ETH, stake_amount=250.0),
    ]
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, profiles, realtime=realtime_config(tmp_path, platform_max_total_notional=300.0)
    )

    async def scenario() -> list[Any]:
        return await orchestrator.run_once()

    decisions = run(scenario())
    by_profile = {decision.profile_id: decision for decision in decisions}
    assert by_profile["btc-paper"].blocked is False
    # eth-paper sees btc-paper's published exposure and is refused by the platform cap
    assert by_profile["eth-paper"].blocked is True
    assert "platform_max_total_notional" in by_profile["eth-paper"].block_reason
    assert orchestrator.runner("eth-paper").last_block_reason == (  # type: ignore[union-attr]
        by_profile["eth-paper"].block_reason
    )
    assert len(store.list_positions("eth-paper")) == 0
    store.close()


def test_a_deleted_profile_is_forgotten_by_the_platform_risk_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed profile must stop contributing to the platform-wide exposure."""
    flag: dict[str, Any] = {"mode": "entry_long"}
    scripted(monkeypatch, flag)
    profiles = [
        profile("btc-paper", SYMBOL_BTC, stake_amount=250.0),
        profile("eth-paper", SYMBOL_ETH, stake_amount=250.0),
    ]
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, profiles, realtime=realtime_config(tmp_path, platform_max_total_notional=300.0)
    )
    path = write_profiles(tmp_path / "profiles.json", profiles)

    async def scenario() -> Any:
        first = await orchestrator.run_once()
        removed = await orchestrator.delete_profile("btc-paper", profiles_path=path)
        after = await orchestrator.run_once()
        return first, removed, after

    first, removed, after = run(scenario())
    by_profile = {decision.profile_id: decision for decision in first}
    assert by_profile["eth-paper"].blocked is True, "the platform cap is shared"
    assert removed == "btc-paper"
    # the deleted profile's exposure is gone from the aggregation: the same order
    # that was refused a moment ago now fits under the cap
    assert [decision.profile_id for decision in after] == ["eth-paper"]
    assert after[0].blocked is False
    assert len(store.list_positions("eth-paper")) == 1
    store.close()


def test_a_live_platform_mirrors_the_venue_balance_into_a_read_only_wallet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In live mode the wallet *is* the venue account: mirrored, never debited."""
    flag: dict[str, Any] = {"mode": "hold"}
    scripted(monkeypatch, flag)
    venues = [LiveVenueBroker(balance=640.0)]
    profiles = [profile("btc-live", SYMBOL_BTC, mode="live")]
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path,
        profiles,
        broker_factory=lambda target: venues[0],
        environ={"TB_ALLOW_LIVE_TRADING": "I_UNDERSTAND_THE_RISK"},
    )

    run(orchestrator.run_once())

    wallet = orchestrator.wallet
    assert wallet.mode is RunMode.LIVE
    assert wallet.is_authoritative is False
    assert wallet.spendable() == pytest.approx(640.0), "the venue account is the truth"
    assert wallet.cash == pytest.approx(1000.0), "the local ledger is never moved"
    assert venues[0].calls == 1, "exactly one best-effort reading at boot"
    snapshot = orchestrator.snapshot()
    assert snapshot.wallet is not None
    assert snapshot.wallet.source == "venue"
    assert snapshot.wallet.cash == pytest.approx(1000.0)
    store.close()


def test_a_live_wallet_sync_failure_is_logged_and_never_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: Any
) -> None:
    """An unreachable venue is reported, and the platform still starts."""
    from trading_platform.core.errors import BrokerUnavailableError

    flag: dict[str, Any] = {"mode": "hold"}
    scripted(monkeypatch, flag)
    venues = [LiveVenueBroker(error=BrokerUnavailableError("venue unreachable"))]
    profiles = [profile("btc-live", SYMBOL_BTC, mode="live")]
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path,
        profiles,
        broker_factory=lambda target: venues[0],
        environ={"TB_ALLOW_LIVE_TRADING": "I_UNDERSTAND_THE_RISK"},
    )

    run(orchestrator.run_once())

    assert "platform_wallet_venue_sync_failed" in events(logs)
    # the fallback is the configured initial balance, never an invented 0.0
    assert orchestrator.wallet.spendable() == pytest.approx(1000.0)
    store.close()


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


def test_default_broker_factory_passes_the_shared_wallet_to_a_paper_venue() -> None:
    """A paper venue spends the injected ledger; without one it keeps its own."""
    clock = ManualClock()
    target = profile("btc-paper", SYMBOL_BTC)
    wallet = PlatformWallet(initial_balance=2000.0, name="platform")
    shared = default_broker_factory(target, clock=clock, environ={}, wallet=wallet)
    private = default_broker_factory(target, clock=clock, environ={})
    assert shared.wallet is wallet
    assert private.wallet is not wallet
    assert private.wallet.name == "paper-wallet"
    assert private.wallet.cash == pytest.approx(1000.0), "unchanged behaviour without a wallet"


def test_default_broker_factory_ignores_the_wallet_for_a_live_profile() -> None:
    """A live venue reports its own account: it is never handed a simulated ledger."""
    target = profile("btc-live", SYMBOL_BTC, mode="live")
    environ = {"TB_LIVE_API_KEY": "key-1234", "TB_LIVE_API_SECRET": "secret-5678"}
    broker = default_broker_factory(
        target,
        clock=ManualClock(),
        environ=environ,
        wallet=PlatformWallet(initial_balance=2000.0),
    )
    assert type(broker).__name__ == "CcxtBroker"
    assert not hasattr(broker, "wallet")


def test_make_risk_manager_carries_the_platform_limits_wallet_and_state() -> None:
    """The three platform seams reach the manager, and stay optional."""
    from trading_platform.realtime.risk import PlatformRiskLimits, PlatformRiskState

    clock = ManualClock()
    wallet = PlatformWallet(initial_balance=2000.0)
    aggregated = PlatformRiskState(clock=clock)
    limits = PlatformRiskLimits(max_total_notional=500.0, max_daily_loss=100.0)
    manager = make_risk_manager(
        profile("btc-paper", SYMBOL_BTC),
        clock=clock,
        platform=limits,
        wallet=wallet,
        state=aggregated,
    )

    assert manager.platform_limits is limits
    manager.publish_platform_state("btc-paper", exposure=120.0, daily_pnl=-5.0)
    assert aggregated.total_exposure == pytest.approx(120.0)
    assert aggregated.total_daily_pnl == pytest.approx(-5.0)
    # the historical construction is untouched: no platform seam, no platform check
    bare = make_risk_manager(profile("btc-paper", SYMBOL_BTC), clock=clock)
    assert bare.platform_limits is None
    bare.publish_platform_state("btc-paper", exposure=1.0, daily_pnl=1.0)


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


# ---------------------------------------------------------------------------
# 14. runtime profile control: pause, resume, delete and create
# ---------------------------------------------------------------------------


def write_profiles(path: Path, profiles: list[ProfileConfig]) -> Path:
    """Write a minimal profiles document (``profiles`` + the two section keys)."""
    path.write_text(
        json.dumps(
            {
                "profiles": [item.model_dump(mode="json") for item in profiles],
                "realtime": {"state_db": str(path.parent / "state.db")},
                "monitoring": {"port": 0},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class RefusingFlattenBroker(PaperBroker):
    """A venue that refuses the market order flattening a deleted profile."""

    def submit(self, request: Any, *, reference_price: float) -> BrokerAck:
        if request.reason == "profile deleted":
            return BrokerAck(
                client_order_id=request.client_order_id,
                accepted=False,
                state=OrderState.REJECTED,
                reason="venue closed",
            )
        return super().submit(request, reference_price=reference_price)


class SilentFlattenBroker(PaperBroker):
    """A venue that acknowledges the flattening order without ever filling it."""

    def submit(self, request: Any, *, reference_price: float) -> BrokerAck:
        if request.reason == "profile deleted":
            return BrokerAck(
                client_order_id=request.client_order_id,
                accepted=True,
                state=OrderState.SUBMITTED,
                broker_order_id="silent-order",
            )
        return super().submit(request, reference_price=reference_price)


def scripted(monkeypatch: pytest.MonkeyPatch, flag: dict[str, Any]) -> None:
    """Replace the strategy resolution of the runner by the scripted one."""
    strategy = _ScriptedStrategy({}, flag=flag)
    monkeypatch.setattr(runner_module, "resolve_strategy", lambda _profile: strategy)


def test_pause_profile_closes_the_entry_gate_and_keeps_the_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paused profile stops opening, keeps exiting, and resumes on demand."""
    flag: dict[str, Any] = {"mode": "entry_long"}
    scripted(monkeypatch, flag)
    orchestrator, store, _clock, _streams = build_orchestrator(
        # An explicit stake, like every deployed profile: under the shared-wallet
        # model a profile funds its orders from ONE ledger, so an order that spent
        # the whole allocation could not pay the venue fee on top of it.
        tmp_path,
        [profile("btc-paper", SYMBOL_BTC, stake_amount=500.0)],
    )

    async def scenario() -> dict[str, Any]:
        opening = await orchestrator.run_once()
        paused = await orchestrator.pause_profile("btc-paper")
        measured: dict[str, Any] = {
            "opening": opening,
            "paused": paused,
            "gate": (
                orchestrator.runner("btc-paper").paused,  # type: ignore[union-attr]
                store.get_meta("paused:btc-paper"),
            ),
        }
        flag["mode"] = "exit_long"
        measured["closing"] = await orchestrator.run_once()
        measured["flat"] = store.list_positions("btc-paper")
        flag["mode"] = "entry_long"
        measured["blocked"] = await orchestrator.run_once()
        measured["orders_while_paused"] = len(store.list_orders("btc-paper"))
        measured["resumed"] = await orchestrator.resume_profile("btc-paper")
        measured["reopened"] = await orchestrator.run_once()
        measured["open_again"] = store.list_positions("btc-paper")
        measured["orders_after_resume"] = len(store.list_orders("btc-paper"))
        return measured

    measured = run(scenario())
    assert measured["opening"][0].action is SignalAction.ENTER_LONG
    assert measured["paused"].profile_id == "btc-paper"
    assert measured["paused"].status is ProfileStatus.RUNNING
    assert measured["gate"] == (True, "1")
    # the exit path is *not* gated: the open position stays managed while paused
    assert measured["closing"][0].action is SignalAction.EXIT_LONG
    assert measured["flat"] == []
    # the entry path is gated, and the tick still publishes its decision
    assert measured["blocked"][0].action is SignalAction.HOLD
    assert measured["blocked"][0].reason == "profile is paused"
    assert measured["orders_while_paused"] == 2
    assert measured["resumed"].status is ProfileStatus.RUNNING
    assert measured["reopened"][0].action is SignalAction.ENTER_LONG
    assert len(measured["open_again"]) == 1
    assert measured["orders_after_resume"] == 3
    assert store.get_meta("paused:btc-paper") == "0"
    assert orchestrator.runner("btc-paper").paused is False  # type: ignore[union-attr]
    store.close()


def test_a_paused_profile_still_honours_the_stop_of_its_open_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The static stop is evaluated before the entry gate, so it fires while paused."""
    flag: dict[str, Any] = {"mode": "entry_long", "stop_loss": 101.5}
    scripted(monkeypatch, flag)
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, [profile("btc-paper", SYMBOL_BTC, stake_amount=500.0)]
    )

    async def scenario() -> Any:
        await orchestrator.run_once()
        positions = store.list_positions("btc-paper")
        await orchestrator.pause_profile("btc-paper")
        flag["mode"] = "hold"
        decision = await orchestrator.run_once()
        return positions, decision

    positions, decision = run(scenario())
    assert len(positions) == 1
    assert positions[0].stop_price == pytest.approx(101.5)
    assert decision is not None
    assert decision[0].action is SignalAction.STOP_LOSS
    assert store.list_positions("btc-paper") == []
    orders = store.list_orders("btc-paper")
    assert len(orders) == 2
    assert {order.side for order in orders} == {OrderSide.BUY, OrderSide.SELL}
    assert orchestrator.runner("btc-paper").paused is True  # type: ignore[union-attr]
    store.close()


def test_pausing_an_unknown_or_stopped_profile_is_refused(tmp_path: Path) -> None:
    """An unknown id and a profile without a runner are two distinct failures."""
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path,
        [profile("btc-paper", SYMBOL_BTC), profile("sol-paper", "SOL/USDT", enabled=False)],
    )
    run(orchestrator.run_once())
    with pytest.raises(ProfileError) as unknown:
        run(orchestrator.pause_profile("nope"))
    assert str(unknown.value) == "unknown profile: 'nope'"
    with pytest.raises(ProfileError) as stopped:
        run(orchestrator.pause_profile("sol-paper"))
    assert str(stopped.value) == "profile 'sol-paper' is not running"
    with pytest.raises(ProfileError) as unknown_resume:
        run(orchestrator.resume_profile("nope"))
    assert str(unknown_resume.value) == "unknown profile: 'nope'"
    with pytest.raises(ProfileError) as stopped_resume:
        run(orchestrator.resume_profile("sol-paper"))
    assert str(stopped_resume.value) == "profile 'sol-paper' is not running"
    store.close()


def test_the_pause_flag_survives_a_restart(tmp_path: Path) -> None:
    """A pause is persisted, and the restart re-pauses the runner it rebuilds."""
    profiles = [profile("btc-paper", SYMBOL_BTC)]
    orchestrator, store, _clock, _streams = build_orchestrator(tmp_path, profiles)
    run(orchestrator.run_once())
    run(orchestrator.pause_profile("btc-paper"))
    store.close()

    restarted, reopened, _clock, _streams = build_orchestrator(tmp_path, profiles)
    run(restarted.run_once())
    assert restarted.runner("btc-paper").paused is True  # type: ignore[union-attr]
    assert restarted.control_state()["profiles"] == [
        {"profile_id": "btc-paper", "paused": True, "running": True}
    ]
    reopened.close()


def test_control_state_reports_paused_and_running_per_profile(tmp_path: Path) -> None:
    """The two flags are independent: a paused profile is still supervised."""
    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]
    orchestrator, store, _clock, _streams = build_orchestrator(tmp_path, profiles)

    async def scenario() -> Any:
        await orchestrator.start()
        running = orchestrator.control_state()
        paused = await orchestrator.pause_profile("btc-paper")
        after = orchestrator.control_state()
        return running, paused, after

    running, paused, after = run(scenario())
    assert running["profiles"] == [
        {"profile_id": "btc-paper", "paused": False, "running": True},
        {"profile_id": "eth-paper", "paused": False, "running": True},
    ]
    assert paused.status is ProfileStatus.RUNNING
    assert after["profiles"] == [
        {"profile_id": "btc-paper", "paused": True, "running": True},
        {"profile_id": "eth-paper", "paused": False, "running": True},
    ]
    store.close()


def test_candle_series_reads_the_persisted_window(tmp_path: Path) -> None:
    """The engine persists the window it consumed; the orchestrator reads it back."""
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, [profile("btc-paper", SYMBOL_BTC)]
    )
    run(orchestrator.run_once())
    candles = orchestrator.candle_series("btc-paper", 10)
    # The first tick seeds the warm-up window it fed the strategy, so the chart
    # is usable immediately, and it ends on the candle just processed.
    assert len(candles) == 2
    assert [row.timestamp for row in candles] == sorted(row.timestamp for row in candles)
    assert candles[-1].profile_id == "btc-paper"
    assert candles[-1].close == pytest.approx(101.0)
    assert candles[-1].closed is True

    store.append_candle(
        CandleEvent(
            symbol=SYMBOL_BTC,
            timeframe="1h",
            timestamp=pd.Timestamp("2024-01-01T07:00:00Z"),
            open=110.0,
            high=160.0,
            low=109.0,
            close=150.0,
            volume=3.0,
        ),
        profile_id="btc-paper",
    )
    series = orchestrator.candle_series("btc-paper", 10)
    assert [row.close for row in series] == [100.0, 101.0, 150.0]
    assert len(orchestrator.candle_series("btc-paper", 1)) == 1
    assert orchestrator.candle_series("nope", 10) == []
    store.close()


def test_delete_profile_flattens_before_it_removes_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open position is closed at market, persisted, and only then forgotten."""
    flag: dict[str, Any] = {"mode": "entry_long"}
    scripted(monkeypatch, flag)
    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]
    orchestrator, store, _clock, _streams = build_orchestrator(tmp_path, profiles)
    path = write_profiles(tmp_path / "profiles.json", profiles)

    async def scenario() -> Any:
        await orchestrator.run_once()
        positions = store.list_positions("btc-paper")
        removed = await orchestrator.delete_profile("btc-paper", profiles_path=path)
        return positions, removed

    positions, removed = run(scenario())
    assert len(positions) == 1
    quantity = float(positions[0].quantity)
    assert quantity > 0.0
    assert removed == "btc-paper"

    orders = store.list_orders("btc-paper")
    flatten = [order for order in orders if order.side is OrderSide.SELL]
    assert len(orders) == 2
    assert len(flatten) == 1
    assert flatten[0].type is OrderType.MARKET
    assert flatten[0].quantity == pytest.approx(quantity)
    assert flatten[0].price is None
    assert flatten[0].mode is RunMode.PAPER
    assert flatten[0].state is OrderState.FILLED
    assert store.list_positions("btc-paper") == []
    trades = store.list_trades("btc-paper")
    assert len(trades) == 1
    assert trades[0].direction is Direction.LONG

    assert "btc-paper" not in orchestrator.profile_ids()
    assert orchestrator.profile_snapshot("btc-paper") is None
    assert orchestrator.profile_config("btc-paper") is None
    assert [item.id for item in orchestrator.profiles] == ["eth-paper"]
    assert [item.id for item in load_profiles(path)] == ["eth-paper"]
    assert orchestrator.control_state()["profiles"] == [
        {"profile_id": "eth-paper", "paused": False, "running": True}
    ]
    store.close()


def test_delete_profile_refuses_an_unknown_profile(tmp_path: Path) -> None:
    """Deleting what does not exist changes nothing."""
    profiles = [profile("btc-paper", SYMBOL_BTC)]
    orchestrator, store, _clock, _streams = build_orchestrator(tmp_path, profiles)
    path = write_profiles(tmp_path / "profiles.json", profiles)
    before = path.read_bytes()
    run(orchestrator.run_once())
    with pytest.raises(ProfileError) as excinfo:
        run(orchestrator.delete_profile("nope", profiles_path=path))
    assert str(excinfo.value) == "unknown profile: 'nope'"
    assert path.read_bytes() == before
    assert orchestrator.profile_ids() == ["btc-paper"]
    store.close()


def test_delete_profile_refuses_the_last_profile(tmp_path: Path) -> None:
    """An engine requires at least one profile, so the last one is not deletable."""
    profiles = [profile("btc-paper", SYMBOL_BTC)]
    orchestrator, store, _clock, _streams = build_orchestrator(tmp_path, profiles)
    path = write_profiles(tmp_path / "profiles.json", profiles)
    before = path.read_bytes()
    run(orchestrator.run_once())
    with pytest.raises(ProfileError) as excinfo:
        run(orchestrator.delete_profile("btc-paper", profiles_path=path))
    assert str(excinfo.value) == (
        "cannot delete the last profile: the engine requires at least one profile"
    )
    assert path.read_bytes() == before
    assert orchestrator.profile_ids() == ["btc-paper"]
    assert orchestrator.profile_config("btc-paper") is not None
    store.close()


def test_delete_profile_keeps_everything_when_the_venue_refuses_the_flatten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused flattening order aborts the deletion: a profile is never orphaned."""
    flag: dict[str, Any] = {"mode": "entry_long"}
    scripted(monkeypatch, flag)
    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]
    wallets: dict[str, PlatformWallet] = {}

    def venues(target: ProfileConfig) -> PaperBroker:
        return RefusingFlattenBroker(
            clock=ManualClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC)),
            initial_balance=float(target.initial_balance),
            seed=7,
            wallet=wallets["platform"],
        )

    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, profiles, broker_factory=venues
    )
    wallets["platform"] = orchestrator.wallet
    path = write_profiles(tmp_path / "profiles.json", profiles)
    run(orchestrator.run_once())
    before = path.read_bytes()
    with pytest.raises(ProfileError, match="could not be flattened"):
        run(orchestrator.delete_profile("btc-paper", profiles_path=path))
    assert path.read_bytes() == before
    assert orchestrator.profile_ids() == ["btc-paper", "eth-paper"]
    assert [item.id for item in load_profiles(path)] == ["btc-paper", "eth-paper"]
    assert len(store.list_positions("btc-paper")) == 1
    store.close()


def test_delete_profile_keeps_everything_when_the_position_stays_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An acknowledged but unfilled flattening order is detected on the re-read."""
    flag: dict[str, Any] = {"mode": "entry_long"}
    scripted(monkeypatch, flag)
    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]
    wallets: dict[str, PlatformWallet] = {}

    def venues(target: ProfileConfig) -> PaperBroker:
        return SilentFlattenBroker(
            clock=ManualClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC)),
            initial_balance=float(target.initial_balance),
            seed=7,
            wallet=wallets["platform"],
        )

    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, profiles, broker_factory=venues
    )
    wallets["platform"] = orchestrator.wallet
    path = write_profiles(tmp_path / "profiles.json", profiles)
    run(orchestrator.run_once())
    before = path.read_bytes()
    with pytest.raises(ProfileError) as excinfo:
        run(orchestrator.delete_profile("btc-paper", profiles_path=path))
    assert str(excinfo.value) == (
        "cannot delete profile 'btc-paper': the open position could not be flattened"
    )
    assert path.read_bytes() == before
    assert orchestrator.profile_ids() == ["btc-paper", "eth-paper"]
    assert len(store.list_positions("btc-paper")) == 1
    store.close()


def test_add_profile_starts_it_and_rewrites_the_document(tmp_path: Path) -> None:
    """A created profile is persisted, built, started and visible immediately."""
    base = profile("btc-paper", SYMBOL_BTC)
    orchestrator, store, _clock, streams = build_orchestrator(tmp_path, [base])
    path = write_profiles(tmp_path / "profiles.json", [base])
    created = ProfileConfig(
        id="sol-paper",
        symbol="SOL/USDT",
        timeframe="15m",
        strategy="basic",
        mode="paper",
        initial_balance=500.0,
    )

    async def scenario() -> Any:
        await orchestrator.run_once()
        return await orchestrator.add_profile(created, profiles_path=path)

    snapshot = run(scenario())
    assert snapshot.profile_id == "sol-paper"
    assert snapshot.symbol == "SOL/USDT"
    assert snapshot.timeframe == "15m"
    assert snapshot.status is ProfileStatus.RUNNING
    assert [stream.symbol for stream in streams] == [SYMBOL_BTC, "SOL/USDT"]
    assert streams[1].started == 1
    assert "sol-paper" in orchestrator._started_ids  # the documented running flag
    assert orchestrator.runner("sol-paper") is not None
    assert orchestrator.profile_config("sol-paper") is created
    assert orchestrator.profile_ids() == ["btc-paper", "sol-paper"]
    assert [item.id for item in load_profiles(path)] == ["btc-paper", "sol-paper"]
    assert [item.id for item in store.load_profiles()] == ["btc-paper", "sol-paper"]
    assert orchestrator.control_state()["profiles"] == [
        {"profile_id": "btc-paper", "paused": False, "running": True},
        {"profile_id": "sol-paper", "paused": False, "running": True},
    ]
    with pytest.raises(ProfileError) as excinfo:
        run(orchestrator.add_profile(created, profiles_path=path))
    assert str(excinfo.value) == "profile already exists: 'sol-paper'"
    assert [item.id for item in load_profiles(path)] == ["btc-paper", "sol-paper"]
    store.close()


def test_add_profile_reports_a_document_it_cannot_write(tmp_path: Path) -> None:
    """A write failure aborts the creation with the registry exactly as it was."""
    base = profile("btc-paper", SYMBOL_BTC)
    orchestrator, store, _clock, _streams = build_orchestrator(tmp_path, [base])
    run(orchestrator.run_once())
    with pytest.raises(ConfigError, match="not found"):
        run(
            orchestrator.add_profile(
                ProfileConfig(id="sol-paper", symbol="SOL/USDT"),
                profiles_path=tmp_path / "missing.json",
            )
        )
    assert orchestrator.profile_ids() == ["btc-paper"]
    assert orchestrator.profile_config("sol-paper") is None
    assert [item.id for item in store.load_profiles()] == ["btc-paper"]
    store.close()


def test_add_profile_requires_a_running_engine(tmp_path: Path) -> None:
    """A profile cannot be appended to a platform that never prepared its wiring."""
    base = profile("btc-paper", SYMBOL_BTC)
    orchestrator, store, _clock, _streams = build_orchestrator(tmp_path, [base])
    path = write_profiles(tmp_path / "profiles.json", [base])
    with pytest.raises(ProfileError) as excinfo:
        run(
            orchestrator.add_profile(
                ProfileConfig(id="sol-paper", symbol="SOL/USDT"), profiles_path=path
            )
        )
    assert str(excinfo.value) == "the engine is not running"
    assert [item.id for item in load_profiles(path)] == ["btc-paper"]
    store.close()


def test_delete_profile_stops_only_the_deleted_profile(tmp_path: Path) -> None:
    """One profile leaves the platform; its neighbours keep running and ticking."""
    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]
    orchestrator, store, _clock, streams = build_orchestrator(tmp_path, profiles)
    path = write_profiles(tmp_path / "profiles.json", profiles)

    async def scenario() -> Any:
        await orchestrator.start()
        await asyncio.sleep(_SETTLE)
        removed = await orchestrator.delete_profile("btc-paper", profiles_path=path)
        state = orchestrator.control_state()
        surviving = orchestrator._tasks.get("eth-paper")
        assert surviving is not None
        alive = not surviving.done()
        survivor_ticks = streams[1].calls
        statuses = (
            store.load_status("eth-paper").status,  # type: ignore[union-attr]
            store.load_status("btc-paper").status,  # type: ignore[union-attr]
        )
        await asyncio.sleep(_SETTLE)
        return removed, state, alive, survivor_ticks, streams[1].calls, statuses

    removed, state, alive, before, after, statuses = run(scenario())
    assert removed == "btc-paper"
    assert [stream.stopped for stream in streams] == [1, 0]
    assert all(stream.started == 1 for stream in streams)
    assert state["profiles"] == [{"profile_id": "eth-paper", "paused": False, "running": True}]
    assert alive is True
    assert after >= before >= 1  # the survivor kept polling its stream
    assert statuses == (ProfileStatus.RUNNING, ProfileStatus.STOPPED)
    assert [item.id for item in load_profiles(path)] == ["eth-paper"]
    store.close()


def test_control_state_survives_a_store_that_cannot_answer(tmp_path: Path) -> None:
    """The pause/run read model never breaks the dashboard, closed store included."""
    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, [profile("btc-paper", SYMBOL_BTC)]
    )
    store.close()
    assert orchestrator.control_state()["profiles"] == [
        {"profile_id": "btc-paper", "paused": False, "running": False}
    ]


class HalfFilledEntryBroker(PaperBroker):
    """A venue that fills an entry only halfway, and every exit completely.

    The remainder of the entry stays *working* at the venue and would be completed
    by a later poll -- which is exactly the exposure the deletion must cancel before
    it flattens anything.
    """

    def _immediate_fill_quantity(self, request: Any) -> float:
        if request.side is OrderSide.BUY:
            return float(request.quantity) / 2.0
        return float(request.quantity)


def test_delete_profile_cancels_the_working_orders_before_it_flattens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resting venue order is cancelled first, so it can never re-open exposure."""
    flag: dict[str, Any] = {"mode": "entry_long"}
    scripted(monkeypatch, flag)
    profiles = [profile("btc-paper", SYMBOL_BTC), profile("eth-paper", SYMBOL_ETH)]

    def venues(target: ProfileConfig) -> PaperBroker:
        return HalfFilledEntryBroker(
            clock=ManualClock(datetime(2024, 1, 1, 6, 0, tzinfo=UTC)),
            initial_balance=float(target.initial_balance),
            seed=7,
        )

    orchestrator, store, _clock, _streams = build_orchestrator(
        tmp_path, profiles, broker_factory=venues
    )
    path = write_profiles(tmp_path / "profiles.json", profiles)

    async def scenario() -> Any:
        await orchestrator.run_once()
        runner = orchestrator.runner("btc-paper")
        assert runner is not None
        working = runner.gateway.open_orders()
        removed = await orchestrator.delete_profile("btc-paper", profiles_path=path)
        return working, runner.gateway.open_orders(), removed

    working, still_working, removed = run(scenario())
    assert removed == "btc-paper"
    # the entry was only half filled: the venue was holding the rest of the order
    assert len(working) == 1
    assert working[0].state is OrderState.PARTIALLY_FILLED
    assert still_working == []
    assert store.list_positions("btc-paper") == []
    assert [item.id for item in load_profiles(path)] == ["eth-paper"]
    store.close()
