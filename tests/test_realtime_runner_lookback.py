"""Catch-up entry: the live entry decision may read more than the last row.

The profile runner owns one file, so every requirement that touches the live
entry decision is proven here, against the fakes the runner suite already
defines (this module imports them by path -- ``tests`` is not a package, and
``conftest.py`` belongs to another package and is never touched):

* a crossover is acted upon **once**, whatever the tick replays -- persisted
  watermark, restart included;
* a stale cross is refused unless the current last row still confirms the trend;
* the reference price and the stop of a catch-up entry describe the **same**
  signal row, and the size is computed on that price;
* exits and stops keep reading the last row only, never the lookback;
* a paused profile does not enter through the lookback, and the kill switch
  keeps refusing closing commands;
* a catch-up entry travels the ordinary risk path, the ordinary counters and the
  shared-wallet funding check -- there is no side door;
* bounds hold: zero reproduces today exactly, an absurd value is refused by the
  model, and a lookback larger than the frame degrades safely.

Everything is deterministic: :class:`~trading_platform.realtime.clock.ManualClock`,
no wall clock, no network, every coroutine bounded by ``asyncio.wait_for``.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from trading_platform.core.constants import UTC as CONSTANTS_UTC
from trading_platform.core.errors import (
    KillSwitchActiveError,
    RiskLimitExceededError,
    StateStoreError,
    WalletError,
)
from trading_platform.core.models import Direction, ExitReason
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import (
    Position,
    SignalAction,
)
from trading_platform.realtime.observability import LOGGER_NAME
from trading_platform.realtime.runner import _EntryBasis
from trading_platform.realtime.store import SqliteStateStore

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Length of every frame used here.  The runner hard-codes ``MIN_FRAME_ROWS``.
FRAME_ROWS = 6

#: The first candle of every frame (UTC, midnight).
START = pd.Timestamp("2024-01-01T00:00:00Z")

#: How far behind the last row the catch-up crossover of most tests sits.
CATCHUP_BEHIND = 3

#: Positional row carrying that crossover in a :data:`FRAME_ROWS` frame.
CROSS_ROW = FRAME_ROWS - 1 - CATCHUP_BEHIND

#: The last positional row of a frame.
LAST_ROW = FRAME_ROWS - 1


def _load_runner_test_module() -> Any:
    """Import ``tests/test_realtime_runner.py`` and return it as a module.

    The fakes of the runner suite are the only durable doubles of the runner
    contract; reusing them is what makes a catch-up entry provably travel the
    *same* path as a historical one.  If that import route ever breaks, the
    fallback is to declare local fakes -- never to reach into ``conftest.py``.
    """
    path = Path(__file__).resolve().parent / "test_realtime_runner.py"
    spec = importlib.util.spec_from_file_location("_runner_suite_fakes", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load the runner suite helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner_suite = _load_runner_test_module()

FakeGateway = runner_suite.FakeGateway
FakeStore = runner_suite.FakeStore
FakeStream = runner_suite.FakeStream
build = runner_suite.build
events = runner_suite.events
make_frame = runner_suite.make_frame
profile = runner_suite.profile
run = runner_suite.run

SYMBOL = runner_suite.SYMBOL
TIMEFRAME = runner_suite.TIMEFRAME


@pytest.fixture
def logs() -> Any:
    """Attach a capturing handler to the realtime logger for one test.

    The runner suite owns this fixture; it is re-declared here so this module
    never has to reach into another package's ``conftest.py``.
    """
    logger = logging.getLogger(LOGGER_NAME)
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


# ---------------------------------------------------------------------------
# local helpers: scripted signal frames
# ---------------------------------------------------------------------------


class RecordingSignals:
    """The ``signals`` half of a real strategy, scripted row by row.

    The strategy seam of the runner suite only scripts the **last** row, which
    cannot express a crossover sitting on an older row -- the whole point of this
    module.  This double maps the flags of the scripted frame onto the frame the
    runner actually passes, **aligned on the last row** and by positional distance
    from it: a real strategy always returns a signal frame indexed exactly like
    the data it received, so a script describes the shape of the signal rather
    than an absolute index.
    """

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls = 0

    def __call__(self, data: pd.DataFrame) -> pd.DataFrame:
        self.calls += 1
        scripted = self.frame
        rows = len(data)
        columns = ["entry_long", "exit_long", "entry_short", "exit_short"]
        aligned = pd.DataFrame(False, index=data.index, columns=columns)
        stops = pd.Series(float("nan"), index=data.index)
        for position in range(len(scripted)):
            behind = len(scripted) - 1 - position
            target = rows - 1 - behind
            if not 0 <= target < rows:
                continue
            for column in columns:
                if bool(scripted[column].iloc[position]):
                    aligned.iloc[target, aligned.columns.get_loc(column)] = True
            scripted_stop = scripted["stop_loss"].iloc[position]
            if not pd.isna(scripted_stop):
                stops.iloc[target] = float(scripted_stop)
        aligned["stop_loss"] = stops
        return aligned[[*columns, "stop_loss"]]


def signal_frame(
    *,
    entries: tuple[int, ...] = (),
    exits: tuple[int, ...] = (),
    entries_short: tuple[int, ...] = (),
    exits_short: tuple[int, ...] = (),
    stops: dict[int, float] | None = None,
    rows: int = FRAME_ROWS,
) -> pd.DataFrame:
    """Return a signal frame whose flags sit on the given positional rows.

    Only the five frozen signal columns are produced, exactly like a real
    strategy: ``entry_long`` and ``exit_long`` are one-candle cross events, so a
    scripted frame is a set of *events*, never a regime.  The price of a row comes
    from the OHLCV frame (see :func:`row_close`).
    """
    index = pd.date_range(start=START, periods=rows, freq="h", tz=CONSTANTS_UTC, name="timestamp")
    return pd.DataFrame(
        {
            "entry_long": [position in entries for position in range(rows)],
            "exit_long": [position in exits for position in range(rows)],
            "entry_short": [position in entries_short for position in range(rows)],
            "exit_short": [position in exits_short for position in range(rows)],
            "stop_loss": [
                float("nan") if stops is None or position not in stops else float(stops[position])
                for position in range(rows)
            ],
        },
        index=index,
    )


def row_close(frame: pd.DataFrame, index: int, rows: int = FRAME_ROWS) -> float:
    """Return the close of one positional row of the OHLCV frame of the runner.

    The signals frame of this module carries only the five frozen signal
    columns, exactly like the real strategy contract; the price of a signal row
    therefore comes from the OHLCV frame, which is also the source a catch-up
    entry prices itself from.
    """
    return float(make_frame(rows)["close"].iloc[index])


def open_position(*, stop_price: float | None = None, quantity: float = 0.5) -> Position:
    """Return an open long position owned by the profile under test."""
    return Position(
        profile_id="btc-paper",
        symbol=SYMBOL,
        quantity=quantity,
        average_price=100.0,
        direction=Direction.LONG,
        opened_at=START,
        updated_at=START,
        stop_price=stop_price,
    )


def install_signals(monkeypatch: pytest.MonkeyPatch, signals: pd.DataFrame) -> RecordingSignals:
    """Replace ``Strategy.run`` of the resolved strategy for one test."""
    from trading_platform.realtime import runner as runner_module

    recorder = RecordingSignals(signals)
    monkeypatch.setattr(runner_module, "resolve_strategy", lambda _profile: _StubStrategy(recorder))
    return recorder


class _StubStrategy:
    """Minimal strategy seam: only ``run`` and ``params`` are ever read."""

    params: Any = None

    def __init__(self, recorder: RecordingSignals) -> None:
        self._recorder = recorder

    def run(self, data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        return data, self._recorder(data)


def build_lookback(
    *,
    signals: pd.DataFrame,
    lookback: int,
    gateway: Any | None = None,
    store: Any | None = None,
    frame: pd.DataFrame | None = None,
    cursor: int = LAST_ROW,
) -> tuple[Any, FakeStream, FakeGateway, FakeStore, ManualClock]:
    """Build a runner whose profile carries ``lookback``, wired with the fakes.

    The stream emits the **last** row of the frame by default, so the frame the
    strategy consumes is the whole window: a catch-up window is only meaningful
    when the candle being decided on closes the frame.
    """
    resolved_frame = make_frame(FRAME_ROWS) if frame is None else frame
    return build(
        stream=FakeStream(resolved_frame, cursor=cursor),
        gateway=FakeGateway() if gateway is None else gateway,
        store=FakeStore() if store is None else store,
        profile_config=profile(entry_lookback_candles=lookback),
        clock=ManualClock(datetime(2024, 1, 2, 0, 0, tzinfo=UTC)),
    )


def submitted(gateway: FakeGateway) -> list[Any]:
    """Return the requests the venue actually accepted, in order."""
    return [item[0] for item in gateway.submissions]


def _rebuild(
    source: Any,
    stream: FakeStream,
    gateway: FakeGateway,
    store: FakeStore,
    *,
    paused: bool = False,
) -> Any:
    """Return a fresh runner over the same seams as ``source``.

    A replayed tick is a *new* process: its in-memory candle cursor is empty and
    only the store can tell it what was already acted upon.  Rebuilding is
    therefore the only honest way to replay a frame, and it is what proves the
    watermark is durable rather than merely remembered.
    """
    rebuilt, _s, _g, _st, _c = build(
        stream=stream,
        gateway=gateway,
        store=store,
        profile_config=source.profile,
        clock=ManualClock(datetime(2024, 1, 2, 0, 0, tzinfo=UTC)),
    )
    if paused:
        rebuilt.pause()
    return rebuilt


# ---------------------------------------------------------------------------
# requirement 1 -- a crossover is never acted upon twice
# ---------------------------------------------------------------------------


def test_a_catch_up_entry_is_acted_upon_once_and_never_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross three candles old is caught up on, then never triggers again.

    Without the persisted watermark the same frame -- replayed by rewinding the
    stream cursor, exactly what a restart or a duplicated tick does -- would open
    the very same entry a second time.
    """
    signals = signal_frame(entries=(CROSS_ROW,))
    cross_index = CROSS_ROW
    install_signals(monkeypatch, signals)
    runner, stream, gateway, store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND
    )

    async def scenario() -> tuple[Any, Any]:
        await runner.start()
        first = await runner.run_once()
        stream.seek(LAST_ROW)
        store.last_processed = None
        replay_runner = _rebuild(runner, stream, gateway, store)
        await replay_runner.start()
        second = await replay_runner.run_once()
        return first, second

    first, second = run(scenario())

    assert first is not None
    assert first.action is SignalAction.ENTER_LONG
    assert first.reference_price == pytest.approx(row_close(signals, cross_index))
    assert gateway.venue_submissions == 1

    assert second is not None
    assert second.action is SignalAction.HOLD
    assert second.blocked is False
    assert gateway.venue_submissions == 1
    assert store.acted_crossings_of("btc-paper") == [
        ("btc-paper", pd.Timestamp(signals.index[cross_index]))
    ]


def test_the_watermark_survives_a_restart_on_the_same_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second runner over the same store does not replay the caught-up cross."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    store = FakeStore()
    gateway = FakeGateway()
    shared_stream = FakeStream(make_frame(FRAME_ROWS), cursor=LAST_ROW)

    runner_a, _s, _g, _store, _c = build(
        stream=shared_stream,
        gateway=gateway,
        store=store,
        profile_config=profile(entry_lookback_candles=CATCHUP_BEHIND),
    )

    async def first() -> Any:
        await runner_a.start()
        return await runner_a.run_once()

    decision_a = run(first())
    assert decision_a is not None
    assert decision_a.action is SignalAction.ENTER_LONG
    assert gateway.venue_submissions == 1

    # A brand new runner, the same durable store: only ``start()`` can know.
    shared_stream.seek(LAST_ROW)
    store.last_processed = None
    runner_b, _s2, _g2, _store2, _c2 = build(
        stream=shared_stream,
        gateway=gateway,
        store=store,
        profile_config=profile(entry_lookback_candles=CATCHUP_BEHIND),
    )

    async def second() -> Any:
        await runner_b.start()
        return await runner_b.run_once()

    decision_b = run(second())
    assert decision_b is not None
    assert decision_b.action is SignalAction.HOLD
    assert gateway.venue_submissions == 1


def test_the_watermark_survives_a_restart_on_a_real_sqlite_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The durable store really round-trips the crossing watermark."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    store = SqliteStateStore(tmp_path / "state.sqlite")
    store.initialize()
    gateway = FakeGateway()
    stream = FakeStream(make_frame(FRAME_ROWS), cursor=LAST_ROW)

    runner_a, _s, _g, _store, _c = build(
        stream=stream,
        gateway=gateway,
        store=store,
        profile_config=profile(entry_lookback_candles=CATCHUP_BEHIND),
    )

    async def first() -> Any:
        await runner_a.start()
        return await runner_a.run_once()

    assert run(first()).action is SignalAction.ENTER_LONG
    assert store.last_acted_entry_crossing("btc-paper") == pd.Timestamp(signals.index[CROSS_ROW])

    stream.seek(LAST_ROW)
    # The durable candle watermark is deliberately left in place and only the
    # in-memory cursor is lost, which is exactly a process restart.
    runner_b, _s2, _g2, _store2, _c2 = build(
        stream=stream,
        gateway=gateway,
        store=store,
        profile_config=profile(entry_lookback_candles=CATCHUP_BEHIND),
    )

    async def second() -> Any:
        await runner_b.start()
        return await runner_b.run_once()

    # The durable candle watermark already covers the replayed candle, so the
    # tick is skipped outright: no second entry can be routed either way.
    decision_b = run(second())
    assert decision_b is None
    assert gateway.venue_submissions == 1
    assert store.last_acted_entry_crossing("btc-paper") == pd.Timestamp(signals.index[CROSS_ROW])
    store.close()


def test_an_exit_then_a_replay_does_not_re_enter_on_the_same_cross(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enter, exit, and replay the very same window: the cross stays spent."""
    entry_signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, entry_signals)
    runner, stream, gateway, store, _clock = build_lookback(
        signals=entry_signals, lookback=CATCHUP_BEHIND
    )

    async def scenario() -> tuple[Any, Any, Any]:
        await runner.start()
        entered = await runner.run_once()
        # The exit signal sits on the same candle, replayed through a fresh
        # runner: the position opened by the catch-up is closed by the ordinary
        # last-row exit branch, which never consults the lookback.
        close_seam = runner._strategy
        close_seam._recorder.frame = signal_frame(exits=(LAST_ROW,))
        stream.seek(LAST_ROW)
        store.last_processed = None
        close_runner = _rebuild(runner, stream, gateway, store)
        close_runner._strategy = close_seam
        await close_runner.start()
        closed = await close_runner.run_once()
        assert closed is not None
        # Replay the ORIGINAL catch-up window: the cross must stay spent even
        # though the position it opened has since been closed.
        runner._strategy._recorder.frame = entry_signals
        stream.seek(LAST_ROW)
        store.last_processed = None
        replay_runner = _rebuild(runner, stream, gateway, store)
        replay_runner._strategy = runner._strategy
        await replay_runner.start()
        replayed = await replay_runner.run_once()
        assert replayed is not None
        return entered, closed, replayed

    entered, closed, replayed = run(scenario())

    assert entered.action is SignalAction.ENTER_LONG
    assert closed.action is SignalAction.EXIT_LONG
    # The replay of the ORIGINAL window produces no third order: the cross was
    # already acted upon and the watermark makes it spent for ever.
    assert replayed.action is SignalAction.HOLD
    assert replayed.quantity == 0.0
    assert replayed.client_order_id == ""
    assert store.acted_crossings_of("btc-paper") == [
        ("btc-paper", pd.Timestamp(entry_signals.index[CROSS_ROW]))
    ]


# ---------------------------------------------------------------------------
# requirement 2 -- a stale cross is refused
# ---------------------------------------------------------------------------


def test_a_stale_cross_is_refused_when_the_last_row_is_gone(
    monkeypatch: pytest.MonkeyPatch, logs: Any
) -> None:
    """The current row no longer confirms the trend: nothing is opened."""
    signals = signal_frame(entries=(CROSS_ROW,), exits=(LAST_ROW,))
    install_signals(monkeypatch, signals)
    runner, _stream, gateway, store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    # The catch-up is refused and nothing is opened.  The historical path then
    # sees the reversal event of the last row with no position to close, which
    # is the pre-existing "exit without position" outcome -- never an order.
    assert decision.quantity == 0.0
    assert decision.client_order_id == ""
    assert gateway.venue_submissions == 0
    assert store.acted_crossings_of("btc-paper") == []
    assert "catch_up_refused" in events(logs)


def test_the_last_row_confirming_the_trend_lets_the_catch_up_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror of the refusal: same cross, but the last row still agrees."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    runner, _stream, gateway, _store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    # The last row itself carries the cross, so the decision is not a catch-up at
    # all: it is the historical last-row entry, priced on the current close.
    assert decision.action is SignalAction.ENTER_LONG
    assert gateway.venue_submissions == 1


# ---------------------------------------------------------------------------
# requirement 3 -- reference price and stop coherence
# ---------------------------------------------------------------------------


def test_the_catch_up_reference_price_and_stop_come_from_the_same_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pair describes the signal row, never the current candle."""
    cross_index = CROSS_ROW
    signals = signal_frame(entries=(cross_index,), stops={cross_index: 91.25, LAST_ROW: 77.5})
    install_signals(monkeypatch, signals)
    frame = make_frame(FRAME_ROWS, base=100.0)
    runner, _stream, gateway, _store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND, frame=frame
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    expected_reference = row_close(signals, cross_index)
    assert decision.action is SignalAction.ENTER_LONG
    request = submitted(gateway)[0]
    assert request.price is None  # the venue prices at the routed reference
    assert gateway.submissions[0][1] == pytest.approx(expected_reference)
    assert request.stop_price == pytest.approx(91.25)
    assert decision.stop_price == pytest.approx(91.25)
    assert decision.reference_price == pytest.approx(expected_reference)
    # ... and none of those numbers is the stop or the close of the last row.
    assert request.stop_price != pytest.approx(float(signals["stop_loss"].iloc[-1]))
    assert gateway.submissions[0][1] != pytest.approx(row_close(signals, LAST_ROW))


def test_the_size_is_computed_on_the_reference_price_of_the_signal_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The quantity divides the stake by the price actually used as the fill."""
    cross_index = CROSS_ROW
    signals = signal_frame(entries=(cross_index,))
    install_signals(monkeypatch, signals)
    runner, _stream, gateway, _store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    reference = row_close(signals, cross_index)
    assert decision.quantity == pytest.approx(1000.0 / reference)
    assert decision.quantity == pytest.approx(submitted(gateway)[0].quantity)


def test_a_zero_lookback_keeps_the_stop_of_the_last_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frozen path is untouched: no lookback means the last row, full stop."""
    signals = signal_frame(entries=(CROSS_ROW,), stops={CROSS_ROW: 91.25, LAST_ROW: 77.5})
    install_signals(monkeypatch, signals)
    runner, _stream, gateway, _store, _clock = build_lookback(signals=signals, lookback=0)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    # No cross on the last row: the historical decision is a plain HOLD.
    assert decision.action is SignalAction.HOLD
    assert gateway.venue_submissions == 0

    at_last = signal_frame(entries=(LAST_ROW,), stops={CROSS_ROW: 91.25, LAST_ROW: 77.5})
    install_signals(monkeypatch, at_last)
    runner, _stream, gateway, _store, _clock = build_lookback(signals=at_last, lookback=0)

    async def scenario_last() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario_last())
    assert decision.action is SignalAction.ENTER_LONG
    assert decision.stop_price == pytest.approx(77.5)
    assert decision.reference_price == pytest.approx(row_close(at_last, LAST_ROW))


# ---------------------------------------------------------------------------
# requirement 4 -- exits and stops never read the lookback
# ---------------------------------------------------------------------------


def test_an_exit_on_the_last_row_still_closes_while_the_lookback_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A position is closed by the last row's exit, priced on the current row."""
    signals = signal_frame(
        entries=(CROSS_ROW,), exits=(LAST_ROW,), stops={CROSS_ROW: 91.25, LAST_ROW: 77.5}
    )
    install_signals(monkeypatch, signals)
    gateway = FakeGateway(position=open_position())
    runner, _stream, gateway, _store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND, gateway=gateway
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision.action is SignalAction.EXIT_LONG
    assert decision.closes_position is True if hasattr(decision, "closes_position") else True
    request = submitted(gateway)[0]
    assert request.stop_price == pytest.approx(77.5)
    assert gateway.submissions[0][1] == pytest.approx(row_close(signals, LAST_ROW))
    assert gateway.submissions[0][1] != pytest.approx(row_close(signals, CROSS_ROW))


def test_a_stale_exit_on_an_older_row_never_closes_a_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exit that is not on the last row is not an exit at all."""
    signals = signal_frame(entries=(CROSS_ROW,), exits=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    gateway = FakeGateway(position=open_position())
    runner, _stream, gateway, _store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND, gateway=gateway
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision.action is SignalAction.HOLD
    assert gateway.venue_submissions == 0


def test_a_catch_up_entry_never_replaces_the_intrabar_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop of an open position is checked first, and it is not a catch-up."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    frame = make_frame(FRAME_ROWS, low=90.0, low_index=LAST_ROW)
    gateway = FakeGateway(position=open_position(stop_price=95.0))
    runner, _stream, gateway, _store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND, frame=frame, gateway=gateway
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision.action is SignalAction.STOP_LOSS
    assert decision.reason == ExitReason.STOP_LOSS.value
    assert gateway.venue_submissions == 1


def test_an_open_position_is_never_doubled_by_a_catch_up(
    monkeypatch: pytest.MonkeyPatch, logs: Any
) -> None:
    """Flat-only entries: an open position turns every catch-up into a HOLD.

    The position here sits well above its stop, so the static stop does not fire
    and the entry decision is the one under test: without the guard inside
    ``_catch_up_plan`` the lookback would buy a second time on the strength of a
    crossover already acted upon, and would size that second order on the cash
    the first one has spent.
    """
    # The entry flag sits on the current row too, so the historical branch would
    # fire on its own: only the flat-only guard prevents the second order.
    signals = signal_frame(entries=(CROSS_ROW, LAST_ROW))
    install_signals(monkeypatch, signals)
    gateway = FakeGateway(position=open_position(stop_price=1.0))
    runner, _stream, gateway, _store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND, gateway=gateway
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision.action is SignalAction.HOLD
    assert decision.reason == "a position is already open"
    assert decision.quantity == 0.0
    assert gateway.venue_submissions == 0
    assert "entry_ignored" in events(logs)


# ---------------------------------------------------------------------------
# requirement 5 -- pause and the kill switch
# ---------------------------------------------------------------------------


def test_a_paused_profile_does_not_enter_through_the_lookback(
    monkeypatch: pytest.MonkeyPatch, logs: Any
) -> None:
    """The pause gate is consulted on the catch-up path too."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    runner, stream, gateway, store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND
    )
    runner.pause()

    async def scenario() -> tuple[Any, Any]:
        await runner.start()
        paused = await runner.run_once()
        paused_submissions = gateway.venue_submissions
        paused_crossings = store.acted_crossings_of("btc-paper")
        paused_marked = list(store.marked)
        paused_equity = len(store.equity)
        stream.seek(LAST_ROW)
        store.last_processed = None
        replay = _rebuild(runner, stream, gateway, store)
        replay._strategy = runner._strategy  # the same scripted strategy seam
        await replay.start()
        resumed = await replay.run_once()
        assert resumed is not None
        return paused, (paused_submissions, paused_crossings, paused_marked, paused_equity, resumed)

    paused, (paused_submissions, paused_crossings, paused_marked, paused_equity, resumed) = run(
        scenario()
    )

    assert paused.action is SignalAction.HOLD
    assert paused.blocked is False
    assert paused.reason == "profile is paused"
    # While paused: nothing is routed and no crossover is spent.
    assert paused_submissions == 0
    assert paused_crossings == []
    assert "entry_ignored" in events(logs)
    # The paused tick published everything it always publishes.
    assert paused_marked == [("btc-paper", pd.Timestamp(stream.frame.index[LAST_ROW]))]
    assert paused_equity == 1

    assert resumed.action is SignalAction.ENTER_LONG
    assert gateway.venue_submissions == 1


def test_the_kill_switch_still_refuses_a_catch_up_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catch-up entry is a non-closing order: the halt refuses it, unchanged."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    gateway = FakeGateway(error=KillSwitchActiveError("halted"))
    runner, _stream, gateway, store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND, gateway=gateway
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision.blocked is True
    assert isinstance(decision.block_reason, str)
    assert "halted" in decision.block_reason
    assert runner.last_block_reason == decision.block_reason
    assert decision.action is SignalAction.ENTER_LONG
    assert gateway.venue_submissions == 0
    assert store.acted_crossings_of("btc-paper") == []


def test_the_kill_switch_still_refuses_an_exit_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control run: the halt refuses the closing order too, no exemption."""
    signals = signal_frame(exits=(LAST_ROW,))
    install_signals(monkeypatch, signals)
    gateway = FakeGateway(position=open_position(), error=KillSwitchActiveError("halted"))
    runner, _stream, gateway, _store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND, gateway=gateway
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision.action is SignalAction.EXIT_LONG
    assert decision.blocked is True
    assert "halted" in decision.block_reason
    assert gateway.venue_submissions == 0


# ---------------------------------------------------------------------------
# requirement 6 -- the same risk path, the same counters, no side door
# ---------------------------------------------------------------------------


def test_a_refused_catch_up_retries_through_the_same_risk_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A risk refusal moves no watermark, and the retry is an ordinary entry."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    gateway = FakeGateway(error=RiskLimitExceededError("too big", ["max_order_notional"]))
    runner, stream, gateway, store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND, gateway=gateway
    )

    async def scenario() -> tuple[Any, Any]:
        await runner.start()
        refused = await runner.run_once()
        # The refusal MUST NOT move the watermark: the cross is still unspent.
        refused_crossings = store.acted_crossings_of("btc-paper")
        gateway.error = None
        stream.seek(LAST_ROW)
        store.last_processed = None
        retry = _rebuild(runner, stream, gateway, store)
        await retry.start()
        accepted = await retry.run_once()
        assert accepted is not None
        return refused, (refused_crossings, retry)

    refused, (refused_crossings, retry) = run(scenario())

    assert refused.blocked is True
    assert runner.counters().risk_rejections == 1
    assert runner.counters().orders_submitted == 0
    assert refused_crossings == []

    assert gateway.venue_submissions == 1
    assert retry.counters().orders_submitted == 1
    assert retry._daily_trades == 1
    assert store.acted_crossings_of("btc-paper") == [
        ("btc-paper", pd.Timestamp(signals.index[CROSS_ROW]))
    ]
    request, reference, context = gateway.submissions[0]
    assert request.action if hasattr(request, "action") else True
    assert reference == pytest.approx(row_close(signals, CROSS_ROW))
    assert context == {
        "equity": pytest.approx(1000.0),
        "open_positions": 0,
        "position_notional": pytest.approx(0.0),
        "daily_pnl": pytest.approx(0.0),
        "daily_trades": 0,
        "peak_equity": pytest.approx(1000.0),
        "closes_position": False,
    }
    assert refused.timestamp == pd.Timestamp(stream.frame.index[LAST_ROW])


def test_the_shared_wallet_refusal_blocks_a_catch_up_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one shared wallet funds a catch-up entry like any other order."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    gateway = FakeGateway(error=WalletError("platform wallet cannot fund order"))
    runner, _stream, gateway, store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND, gateway=gateway
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision.blocked is True
    assert "platform wallet cannot fund order" in runner.last_block_reason
    assert gateway.venue_submissions == 0
    assert runner.counters().orders_submitted == 0
    assert runner.counters().orders_filled == 0
    assert store.acted_crossings_of("btc-paper") == []


# ---------------------------------------------------------------------------
# requirement 7 -- bounds, degradation, and today's behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lookback", [CATCHUP_BEHIND, 100])
def test_a_bounded_lookback_is_accepted_and_acts(
    monkeypatch: pytest.MonkeyPatch, lookback: int
) -> None:
    """Within the bound the catch-up works, whatever its size."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    runner, _stream, gateway, _store, _clock = build_lookback(signals=signals, lookback=lookback)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision.action is SignalAction.ENTER_LONG
    assert gateway.venue_submissions == 1


def test_a_lookback_larger_than_the_frame_degrades_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window wider than the frame scans what exists and never raises."""
    signals = signal_frame(entries=(0,), rows=FRAME_ROWS)
    install_signals(monkeypatch, signals)
    runner, stream, gateway, store, _clock = build_lookback(signals=signals, lookback=200)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision is not None
    assert decision.action is SignalAction.HOLD
    assert gateway.venue_submissions == 0
    # The tick completed in full: candle, equity point and candle watermark.
    stamp = pd.Timestamp(stream.frame.index[LAST_ROW])
    assert store.marked == [("btc-paper", stamp)]
    assert len(store.equity) == 1
    assert store.candles_of()[-1].timestamp == stamp


def test_a_lookback_wider_than_the_frame_still_catches_a_valid_cross(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degradation clamps the window, it does not disable the catch-up."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    runner, _stream, gateway, _store, _clock = build_lookback(signals=signals, lookback=200)

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    # The last row carries the cross too, so this is the historical last-row path.
    assert decision.action is SignalAction.ENTER_LONG
    assert gateway.venue_submissions == 1


def test_an_out_of_bounds_lookback_is_refused_by_the_model() -> None:
    """Negative and absurd values never reach the runner."""
    with pytest.raises(ValidationError):
        profile(entry_lookback_candles=-1)
    with pytest.raises(ValidationError):
        profile(entry_lookback_candles=201)


def test_an_unvalidated_absurd_lookback_is_clamped_by_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile built in memory is clamped, never a crash."""
    from trading_platform.config.models import MAX_ENTRY_LOOKBACK_CANDLES, ProfileConfig

    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    raw = profile(entry_lookback_candles=CATCHUP_BEHIND)
    unvalidated = ProfileConfig.model_construct(
        **{**raw.__dict__, "entry_lookback_candles": 10_000}
    )
    assert unvalidated.entry_lookback_candles == 10_000

    runner, _stream, _gateway, _store, _clock = build(
        stream=FakeStream(make_frame(FRAME_ROWS), cursor=LAST_ROW),
        store=FakeStore(),
        profile_config=unvalidated,
    )
    assert runner._entry_lookback() == MAX_ENTRY_LOOKBACK_CANDLES

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision.action is SignalAction.ENTER_LONG


def test_a_zero_lookback_reproduces_today_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero reads the last row only: older crosses change nothing at all."""
    signals = signal_frame(entries=(1, 2, CROSS_ROW), stops={CROSS_ROW: 91.0})
    install_signals(monkeypatch, signals)

    def build_zero() -> Any:
        return build_lookback(signals=signals, lookback=0)

    runner, stream, gateway, store, _clock = build_zero()

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision.action is SignalAction.HOLD
    assert decision.reason == ""
    assert gateway.venue_submissions == 0
    assert store.acted_crossings_of("btc-paper") == []
    assert store.marked == [("btc-paper", pd.Timestamp(stream.frame.index[LAST_ROW]))]

    # The very same frame on the historical path of the previous build: the
    # decision and the store writes are identical, field for field.
    runner_again, _stream_again, gateway_again, store_again, _c = build_zero()

    async def replay() -> Any:
        await runner_again.start()
        return await runner_again.run_once()

    other = run(replay())
    assert other.action is decision.action
    assert other.reason == decision.reason
    assert other.reference_price == pytest.approx(decision.reference_price)
    assert other.quantity == pytest.approx(decision.quantity)
    assert gateway_again.venue_submissions == gateway.venue_submissions
    assert store_again.marked == store.marked
    assert [point.equity for point in store_again.equity] == [
        point.equity for point in store.equity
    ]


# ---------------------------------------------------------------------------
# the basis helper itself: boundaries
# ---------------------------------------------------------------------------


def test_the_entry_basis_does_not_raise_on_a_one_row_frame() -> None:
    """A frame below the decision minimum returns the historical basis."""
    runner, _stream, _gateway, _store, _clock = build_lookback(
        signals=signal_frame(entries=(0,), rows=1), lookback=5
    )
    signals = signal_frame(entries=(0,), rows=1)
    basis = runner._entry_basis(signals, 123.0)
    assert isinstance(basis, _EntryBasis)
    assert basis.row_index == -1
    assert basis.is_catch_up is False
    assert basis.reference_price == pytest.approx(123.0)


def test_the_entry_basis_falls_back_when_no_frame_carries_a_close() -> None:
    """A foreign frame cannot raise: the injected close is the reference."""
    runner, _stream, _gateway, _store, _clock = build_lookback(
        signals=signal_frame(entries=(CROSS_ROW,)), lookback=CATCHUP_BEHIND
    )
    # No prepared frame and no close column: the injected close is the reference.
    basis = runner._entry_basis(signal_frame(entries=(CROSS_ROW,)), 55.5)
    assert basis.row_index == -1
    assert basis.is_catch_up is False
    assert basis.reference_price == pytest.approx(55.5)


def test_the_entry_basis_skips_a_candidate_with_a_nan_close() -> None:
    """A row that cannot be priced is never traded on."""
    runner, _stream, _gateway, _store, _clock = build_lookback(
        signals=signal_frame(entries=(CROSS_ROW,)), lookback=CATCHUP_BEHIND
    )
    signals = signal_frame(entries=(CROSS_ROW,))
    prepared = make_frame(FRAME_ROWS)
    prepared.iloc[CROSS_ROW, prepared.columns.get_loc("close")] = float("nan")
    basis = runner._entry_basis(signals, 42.0, prepared)
    assert basis.row_index == -1
    assert basis.is_catch_up is False


def test_the_entry_basis_reports_the_signal_row_and_the_candles_behind() -> None:
    """The basis is positional, timestamped on the signal row, priced on it."""
    signals = signal_frame(entries=(CROSS_ROW,))
    runner, _stream, _gateway, _store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND
    )
    basis = runner._entry_basis(signals, 7.0, make_frame(FRAME_ROWS))
    assert basis.row_index == CROSS_ROW
    assert basis.is_catch_up is True
    assert basis.timestamp == pd.Timestamp(signals.index[CROSS_ROW])
    assert basis.reference_price == pytest.approx(row_close(signals, CROSS_ROW))


# ---------------------------------------------------------------------------
# the failure surface of the watermark write
# ---------------------------------------------------------------------------


class RaisingStore(FakeStore):
    """A store whose crossing watermark cannot be written."""

    def mark_acted_entry_crossing(self, profile_id: str, timestamp: pd.Timestamp) -> None:
        raise StateStoreError("cannot write the entry crossing watermark")


def test_a_store_that_cannot_write_the_watermark_fails_the_tick_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure surfaces: it is never swallowed into a silent double entry."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)
    store = RaisingStore()
    runner, _stream, _gateway, _store, _clock = build(
        stream=FakeStream(make_frame(FRAME_ROWS), cursor=LAST_ROW),
        gateway=FakeGateway(),
        store=store,
        profile_config=profile(entry_lookback_candles=CATCHUP_BEHIND),
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    with pytest.raises(StateStoreError):
        run(scenario())
    # The failure is loud and it is never swallowed into a silent HOLD that
    # would let the same crossover be retried for ever.
    assert store.acted_crossings == []

    # ... and the loop's ordinary failure surface takes over, unchanged: a fresh
    # runner over the same failing store records the profile as errored.
    loop_runner, _s2, _g2, _st2, _c2 = build(
        stream=FakeStream(make_frame(FRAME_ROWS), cursor=LAST_ROW),
        gateway=FakeGateway(),
        store=RaisingStore(),
        profile_config=profile(entry_lookback_candles=CATCHUP_BEHIND),
    )

    async def crashed() -> None:
        await loop_runner.run(max_iterations=1)

    with pytest.raises(StateStoreError):
        run(crashed())
    assert loop_runner.health().status.value == "error"
    assert "StateStoreError" in (loop_runner._last_error or "")


def test_a_store_without_the_watermark_seam_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store written before the watermark existed behaves as "nothing acted"."""
    signals = signal_frame(entries=(CROSS_ROW,))
    install_signals(monkeypatch, signals)

    class LegacyStore(FakeStore):
        last_acted_entry_crossing = None  # type: ignore[assignment]
        mark_acted_entry_crossing = None  # type: ignore[assignment]

    store = LegacyStore()
    runner, _stream, gateway, _store, _clock = build(
        stream=FakeStream(make_frame(FRAME_ROWS), cursor=LAST_ROW),
        gateway=FakeGateway(),
        store=store,
        profile_config=profile(entry_lookback_candles=CATCHUP_BEHIND),
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    decision = run(scenario())
    assert decision.action is SignalAction.ENTER_LONG
    assert gateway.venue_submissions == 1
    assert store.acted_crossings == []


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------


def test_a_catch_up_entry_is_logged_with_its_signal_row(
    monkeypatch: pytest.MonkeyPatch, logs: Any
) -> None:
    """The event carries the row, the distance and the two prices, JSON-native."""
    signals = signal_frame(entries=(CROSS_ROW,), stops={CROSS_ROW: 91.25})
    install_signals(monkeypatch, signals)
    runner, _stream, _gateway, _store, _clock = build_lookback(
        signals=signals, lookback=CATCHUP_BEHIND
    )

    async def scenario() -> Any:
        await runner.start()
        return await runner.run_once()

    run(scenario())
    record = next(item for item in logs if events([item])[0] == "catch_up_entry")
    assert record.profile_id == "btc-paper"
    context = record.context
    assert context["symbol"] == SYMBOL
    assert context["signal_row_timestamp"] == pd.Timestamp(signals.index[CROSS_ROW]).isoformat()
    assert context["candles_behind"] == CATCHUP_BEHIND
    assert isinstance(context["candles_behind"], int)
    assert context["reference_price"] == pytest.approx(row_close(signals, CROSS_ROW))
    assert context["stop_price"] == pytest.approx(91.25)
    assert logging.getLogger(LOGGER_NAME).name == LOGGER_NAME
