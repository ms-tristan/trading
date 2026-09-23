"""The warm-up contract at profile start and on every tick.

This is the half of the contract the operator sees: a profile that can **never**
warm up must go to ``ERROR`` with an actionable message instead of polling for
ever with zero signals, while a profile that is merely **not warm yet** must keep
running, log a warning and start trading as soon as the candles have accumulated.
Both sides are tested here, through the real :class:`ProfileRunner` and the real
``momentum`` strategy -- the strategy the incident was reported on.

Everything is offline and deterministic: the four foreign seams (stream, gateway,
store, clock) are local fakes, the frames are hand-built from an explicit
arithmetic series, and time is a :class:`ManualClock`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_platform.config.models import ProfileConfig, RiskLimitsConfig
from trading_platform.core.errors import RealtimeError
from trading_platform.core.models import TradeRecord
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import (
    CandleEvent,
    EquityPoint,
    Order,
    ProfileState,
    ProfileStatus,
    RunMode,
    SignalAction,
)
from trading_platform.realtime.observability import LOGGER_NAME
from trading_platform.realtime.runner import ProfileRunner

TIMEOUT = 5.0
SYMBOL = "BTC/USDT"

#: The frozen event names of the two start-time verdicts.
IMPOSSIBLE_EVENT = "warmup_impossible"
COHERENCE_EVENT = "warmup_coherence"

#: The existing, "not warm YET" event of the tick.
INCOMPLETE_EVENT = "warmup_incomplete"

#: Candles a default ``momentum`` needs on a 1h grid / on a 1m grid / on a 1d grid.
REQUIRED_1H = 673
REQUIRED_1M = 40321
REQUIRED_1D = 29


def run(coro: Any) -> Any:
    """Run one coroutine under an explicit bound so nothing can hang the suite."""
    return asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


def rising_frame(rows: int, *, freq: str = "h") -> pd.DataFrame:
    """Return a deterministic, strictly rising OHLCV frame of ``rows`` candles.

    Every close is higher than the previous one by the same amount, so the
    momentum score is exactly ``1.0`` on every row where it is defined: the frame
    isolates the warm-up from the signal rule.
    """
    index = pd.date_range(
        start="2024-01-01T00:00:00Z", periods=rows, freq=freq, tz="UTC", name="timestamp"
    )
    close = 100.0 + 0.5 * np.arange(rows, dtype="float64")
    opens = np.concatenate(([close[0]], close[:-1])) if rows else close
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, close) + 1.0,
            "low": np.minimum(opens, close) - 1.0,
            "close": close,
            "volume": np.full(rows, 10.0, dtype="float64"),
        },
        index=index,
    )


def candle_at(frame: pd.DataFrame, position: int) -> CandleEvent:
    """Return the :class:`CandleEvent` of one row of ``frame``."""
    row = frame.iloc[position]
    return CandleEvent(
        symbol=SYMBOL,
        timeframe=str(frame.attrs.get("timeframe", "1h")),
        timestamp=pd.Timestamp(frame.index[position]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
    )


def profile(**overrides: Any) -> ProfileConfig:
    """Return the incident's momentum profile, overridable field by field."""
    payload: dict[str, Any] = {
        "id": "momentum-1m",
        "symbol": SYMBOL,
        "timeframe": "1m",
        "strategy": "momentum",
        "mode": "paper",
        "initial_balance": 1000.0,
        "warmup_candles": 200,
        "poll_interval_seconds": 5.0,
        "risk": RiskLimitsConfig(max_open_positions=1),
    }
    payload.update(overrides)
    return ProfileConfig(**payload)


# ---------------------------------------------------------------------------
# local fakes (stream, gateway, store) -- offline and deterministic
# ---------------------------------------------------------------------------


class FrameStream:
    """A deterministic stream emitting the rows of one prepared frame.

    ``history`` answers the rows already emitted *before* the cursor, so the
    window really grows as candles accumulate -- which is exactly the "not warm
    yet" situation the contract must not kill.
    """

    def __init__(self, frame: pd.DataFrame, *, cursor: int = 1, max_wait_seconds: float = 0.0):
        self.frame = frame
        self.cursor = int(cursor)
        self.started = 0
        self.stopped = 0
        self.history_calls: list[int] = []
        self._max_wait_seconds = float(max_wait_seconds)

    def seek(self, cursor: int) -> None:
        """Move the emission cursor, like a long-running poll would."""
        self.cursor = int(cursor)

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
        if self.cursor >= len(self.frame):
            return None
        position = self.cursor
        self.cursor += 1
        return candle_at(self.frame, position)

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        self.history_calls.append(int(count))
        emitted = self.frame.iloc[: self.cursor]
        return emitted.iloc[-int(count) :].copy()

    @property
    def max_wait_seconds(self) -> float:
        """Longest wait a ``next_candle`` call of this fake may legitimately take."""
        return self._max_wait_seconds


class RecordingGateway:
    """Minimal in-memory execution gateway: fills everything it is given."""

    def __init__(self) -> None:
        self.submissions: list[Any] = []
        self.polls = 0

    def submit(self, request: Any, **context: Any) -> Order:
        self.submissions.append((request, context))
        return Order(
            client_order_id=str(request.client_order_id),
            profile_id=str(request.profile_id),
            symbol=str(request.symbol),
            side=request.side,
            type=request.type,
            quantity=float(request.quantity),
            state=request.state if hasattr(request, "state") else None,
            mode=request.mode,
            created_at=pd.Timestamp(request.created_at),
            updated_at=pd.Timestamp(request.created_at),
        )

    def poll(self) -> list[Any]:
        self.polls += 1
        return []

    def closed_trade(self) -> TradeRecord | None:
        return None

    def position(self, symbol: str | None = None) -> None:
        return None

    def snapshot_fields(self) -> dict[str, float]:
        """Return the attributed read model of an all-cash profile."""
        return {
            "equity": 1000.0,
            "cash": 1000.0,
            "position_value": 0.0,
            "open_positions": 0.0,
            "allocation": 1000.0,
            "deployed": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
        }


class RecordingStore:
    """In-memory state store recording every write, enough for one profile."""

    def __init__(self, *, last_processed: pd.Timestamp | None = None) -> None:
        self.last_processed = last_processed
        self.statuses: list[tuple[str, ProfileStatus, str]] = []
        self.candles: list[tuple[CandleEvent, str]] = []
        self.equity: list[EquityPoint] = []
        self.last_status: ProfileStatus = ProfileStatus.STOPPED
        self.last_detail = ""

    # -- status ----------------------------------------------------------

    def save_status(self, profile_id: str, status: ProfileStatus, detail: str = "") -> None:
        self.statuses.append((profile_id, status, detail))
        self.last_status = status
        self.last_detail = detail

    def profile_state(self, profile_id: str) -> ProfileState:
        return ProfileState(
            profile_id=profile_id,
            status=self.last_status,
            mode=RunMode.PAPER,
            last_error=self.last_detail or None,
        )

    def status_values(self) -> list[str]:
        """Return the recorded statuses as their string values."""
        return [status.value for _identifier, status, _detail in self.statuses]

    # -- candles and equity ----------------------------------------------

    def last_processed_candle(self, profile_id: str) -> pd.Timestamp | None:
        return self.last_processed

    def mark_candle_processed(self, profile_id: str, timestamp: pd.Timestamp) -> None:
        self.last_processed = pd.Timestamp(timestamp)

    def append_candle(self, candle: CandleEvent, *, profile_id: str) -> bool:
        self.candles.append((candle, profile_id))
        return True

    def candle_series(self, profile_id: str, limit: int = 1000) -> list[CandleEvent]:
        return [candle for candle, owner in self.candles if owner == profile_id][: int(limit)]

    def append_equity(self, point: EquityPoint) -> bool:
        self.equity.append(point)
        return True

    def equity_curve(self, profile_id: str) -> list[EquityPoint]:
        return list(self.equity)

    # -- watermark and read models ---------------------------------------

    def last_acted_entry_crossing(self, profile_id: str) -> pd.Timestamp | None:
        return None

    def mark_acted_entry_crossing(self, profile_id: str, timestamp: pd.Timestamp) -> None:
        return None

    def list_trades(self, profile_id: str, *, limit: int = 1000) -> list[TradeRecord]:
        return []

    def list_positions(self, profile_id: str) -> list[Any]:
        return []

    def list_orders(self, profile_id: str, *, limit: int = 1000) -> list[Order]:
        return []


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def logs() -> Any:
    """Attach a capturing handler to the realtime logger for one test."""
    logger = logging.getLogger(LOGGER_NAME)
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler()
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def records_of(records: list[logging.LogRecord], event: str) -> list[logging.LogRecord]:
    """Return the captured records carrying one structured event."""
    return [record for record in records if getattr(record, "event", None) == event]


def context_of(record: logging.LogRecord) -> dict[str, Any]:
    """Return the structured context of a realtime log record."""
    return dict(getattr(record, "context", {}) or {})


def build(
    *,
    stream: FrameStream,
    profile_config: ProfileConfig,
    gateway: RecordingGateway | None = None,
    store: RecordingStore | None = None,
    history_candles: int | None = None,
    warmup_candles: int | None = None,
) -> tuple[ProfileRunner, RecordingGateway, RecordingStore]:
    """Build a runner wired with the local fakes."""
    resolved_gateway = RecordingGateway() if gateway is None else gateway
    resolved_store = RecordingStore() if store is None else store
    runner = ProfileRunner(
        profile=profile_config,
        stream=stream,  # type: ignore[arg-type]
        gateway=resolved_gateway,  # type: ignore[arg-type]
        store=resolved_store,  # type: ignore[arg-type]
        clock=ManualClock(datetime(2024, 1, 2, tzinfo=UTC)),
        warmup_candles=warmup_candles,
        history_candles=history_candles,
        timeout_seconds=1.0,
    )
    return runner, resolved_gateway, resolved_store


# ---------------------------------------------------------------------------
# 1. the profile that can NEVER warm up
# ---------------------------------------------------------------------------


def test_a_profile_that_can_never_warm_up_ends_in_error_with_an_actionable_message(
    logs: Any,
) -> None:
    """1m + 200 warm-up candles + 40321 needed: ``ERROR``, never an endless poll.

    The message must name the strategy, the timeframe, the candles required and
    the candles available, and tell the operator which timeframes would work.
    """
    target = profile()
    stream = FrameStream(rising_frame(10, freq="min"))
    runner, gateway, store = build(stream=stream, profile_config=target, history_candles=300)

    with pytest.raises(RealtimeError) as excinfo:
        run(runner.start())

    message = str(excinfo.value)
    assert "can never warm up" in message
    assert "'momentum'" in message
    assert "'1m'" in message
    assert str(REQUIRED_1M) in message
    assert "200" in message
    assert "4h" in message and "1d" in message

    # The verdict is loud and structured.
    verdicts = records_of(logs, IMPOSSIBLE_EVENT)
    assert len(verdicts) == 1
    assert verdicts[0].levelno == logging.ERROR
    assert context_of(verdicts[0])["required_candles"] == REQUIRED_1M
    assert context_of(verdicts[0])["warmup_candles"] == 200

    # It is persisted: the dashboard shows ERROR, with the reason, not "running".
    assert store.status_values() == [ProfileStatus.ERROR.value]
    assert store.last_status is ProfileStatus.ERROR
    assert "can never warm up" in store.last_detail
    assert runner.health().status is ProfileStatus.ERROR

    # Nothing was ever polled or ordered: the profile never started.
    assert stream.started == 0
    assert stream.history_calls == []
    assert gateway.submissions == []
    assert runner.counters().candles_processed == 0


def test_the_refusal_is_idempotent_and_never_turns_into_a_silent_start(logs: Any) -> None:
    """A runner that refused once refuses again -- through ``run_once`` too."""
    stream = FrameStream(rising_frame(10, freq="min"))
    runner, _gateway, store = build(stream=stream, profile_config=profile(), history_candles=300)

    for _attempt in range(2):
        with pytest.raises(RealtimeError):
            run(runner.run_once())

    assert len(records_of(logs, IMPOSSIBLE_EVENT)) == 2
    assert store.last_status is ProfileStatus.ERROR
    assert stream.history_calls == []


def test_a_profile_that_can_never_warm_up_is_refused_even_at_the_exact_boundary(
    logs: Any,
) -> None:
    """One candle short of the requirement is still impossible -- not "almost"."""
    target = profile(timeframe="1h", warmup_candles=REQUIRED_1H - 1)
    runner, _gateway, store = build(
        stream=FrameStream(rising_frame(10)), profile_config=target, history_candles=300
    )
    with pytest.raises(RealtimeError):
        run(runner.start())
    assert store.last_status is ProfileStatus.ERROR
    assert len(records_of(logs, IMPOSSIBLE_EVENT)) == 1


# ---------------------------------------------------------------------------
# 2. the profile that is merely NOT WARM YET
# ---------------------------------------------------------------------------


def test_a_profile_that_is_not_warm_yet_keeps_running_and_then_warms_up(logs: Any) -> None:
    """Both sides: a short window warns and survives, a long one starts trading.

    The profile is the same one that was silently dead in the incident -- 1h with
    a warm-up of 700 candles (673 required) -- only this time it is fed enough
    history, and the tick that cannot warm up says so instead of doing nothing.
    """
    target = profile(timeframe="1h", warmup_candles=700)
    frame = rising_frame(800)
    stream = FrameStream(frame, cursor=1)
    runner, gateway, store = build(stream=stream, profile_config=target, history_candles=700)

    # -- side 1: not warm yet -------------------------------------------------
    decision = run(runner.run_once())
    assert decision is None
    assert store.last_status is not ProfileStatus.ERROR
    assert store.status_values() == []
    warnings = records_of(logs, INCOMPLETE_EVENT)
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert context_of(warnings[0])["required_candles"] == REQUIRED_1H
    assert context_of(warnings[0])["rows"] == 2
    assert context_of(warnings[0])["candles_per_day"] == pytest.approx(24.0)
    assert runner.counters().candles_processed == 0
    assert gateway.submissions == []
    assert records_of(logs, IMPOSSIBLE_EVENT) == []

    # -- side 2: the candles have accumulated ---------------------------------
    stream.seek(REQUIRED_1H - 1)
    decision = run(runner.run_once())
    assert decision is not None
    assert decision.action is SignalAction.ENTER_LONG
    assert decision.blocked is False
    assert len(gateway.submissions) == 1
    assert runner.counters().candles_processed == 1
    assert store.last_status is ProfileStatus.RUNNING
    assert records_of(logs, INCOMPLETE_EVENT) == [warnings[0]]
    assert records_of(logs, IMPOSSIBLE_EVENT) == []


def test_the_same_profile_fed_enough_history_from_the_first_tick_never_warns(logs: Any) -> None:
    """The mirror case: a warm window emits on the very first tick, silently."""
    target = profile(timeframe="1h", warmup_candles=700)
    stream = FrameStream(rising_frame(800), cursor=REQUIRED_1H - 1)
    runner, _gateway, store = build(stream=stream, profile_config=target, history_candles=700)

    decision = run(runner.run_once())
    assert decision is not None
    assert decision.action is SignalAction.ENTER_LONG
    assert records_of(logs, INCOMPLETE_EVENT) == []
    assert records_of(logs, IMPOSSIBLE_EVENT) == []
    assert store.last_status is ProfileStatus.RUNNING


def test_the_window_really_grows_with_the_candles(logs: Any) -> None:
    """A short requirement on the same grid warms up after a couple of ticks.

    The test keeps the requirement small (a 4h profile still needs 169 candles,
    so the 1d grid is the cheap one here) and proves the profile is *not* killed:
    the tick count it takes to warm up is exactly the number of candles missing.
    """
    target = profile(timeframe="1d", warmup_candles=REQUIRED_1D)
    stream = FrameStream(rising_frame(40, freq="D"), cursor=1)
    runner, _gateway, _store = build(stream=stream, profile_config=target, history_candles=100)

    # One candle is emitted per tick and the frame ends on it, so the frame holds
    # ``tick + 1`` rows: the profile is warm on the ``required - 1``-th tick.
    decisions = [run(runner.run_once()) for _ in range(REQUIRED_1D - 2)]
    assert decisions == [None] * (REQUIRED_1D - 2)
    assert len(records_of(logs, INCOMPLETE_EVENT)) == REQUIRED_1D - 2
    assert records_of(logs, IMPOSSIBLE_EVENT) == []

    decision = run(runner.run_once())
    assert decision is not None
    assert decision.action is SignalAction.ENTER_LONG
    assert runner.counters().candles_processed == 1


# ---------------------------------------------------------------------------
# 3. the coherence rule at start time: reported, never fatal
# ---------------------------------------------------------------------------


def test_a_warmup_wider_than_the_stream_window_only_warns(logs: Any) -> None:
    """``warmup_candles`` 1000 inside a 300-candle window: reported, not refused."""
    target = profile(timeframe="1h", warmup_candles=1000)
    runner, _gateway, store = build(
        stream=FrameStream(rising_frame(800), cursor=REQUIRED_1H - 1),
        profile_config=target,
        history_candles=300,
    )

    run(runner.start())

    reported = records_of(logs, COHERENCE_EVENT)
    assert len(reported) == 1
    assert reported[0].levelno == logging.WARNING
    assert context_of(reported[0])["warmup_candles"] == 1000
    assert context_of(reported[0])["history_candles"] == 300
    assert runner.health().status is ProfileStatus.RUNNING
    assert ProfileStatus.ERROR.value not in store.status_values()


def test_an_absent_stream_window_falls_back_to_the_profile_warmup(logs: Any) -> None:
    """``history_candles=None`` means "no window resolved": the old behaviour.

    Without a resolved window the coherence rule cannot fire -- ``warmup_candles``
    is compared against itself -- and the runner behaves exactly as it did before
    the override existed.
    """
    target = profile(timeframe="1h", warmup_candles=1000)
    runner, _gateway, _store = build(
        stream=FrameStream(rising_frame(800), cursor=REQUIRED_1H - 1), profile_config=target
    )

    run(runner.start())

    assert records_of(logs, COHERENCE_EVENT) == []
    assert runner.health().status is ProfileStatus.RUNNING


def test_the_window_the_stream_is_asked_for_is_the_profile_warmup() -> None:
    """The tick asks the stream for ``warmup_candles`` candles, unchanged."""
    target = profile(timeframe="1h", warmup_candles=700)
    stream = FrameStream(rising_frame(800), cursor=REQUIRED_1H - 1)
    runner, _gateway, _store = build(stream=stream, profile_config=target, history_candles=42000)

    run(runner.run_once())

    assert stream.history_calls == [700]
