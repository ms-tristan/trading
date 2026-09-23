"""Contract tests of the market-stream seam (work package wp2).

Covers, offline and deterministically:

* :class:`ReplayMarketStream` -- ordering, UTC timestamps, exhaustion, looping,
  ``seek``, the documented error paths and the warm-up window of ``history``;
* byte-identical determinism of two identical replay runs;
* :class:`PollingMarketStream` over a local fake provider and a
  :class:`ManualClock` -- closed candles only, one candle per time advance, no
  duplicate emission, bounded retry/backoff, health state, ``history``;
* :class:`CcxtProMarketStream` -- the lazily imported optional extra, the mapped
  candle, the bounded timeout and the shutdown path;
* :class:`CompositeMarketStream` -- routing, isolation of a failing child,
  aggregate health and the timeout of its fan-out;
* structural conformance of the four classes to the documented
  :class:`MarketStream` member set.

Every coroutine of this module is awaited through :func:`run`, which wraps it in
``asyncio.wait_for(..., timeout=1)``: a hung implementation fails the suite
instead of hanging it.  No socket, no network, no wall-clock dependency, no
fixed TCP port, no import of any other realtime work package (local fakes only).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest

from trading_platform.core.constants import REQUIRED_OHLCV_COLUMNS, candle_delta
from trading_platform.core.errors import MarketStreamError
from trading_platform.realtime import stream as stream_module
from trading_platform.realtime.clock import ManualClock, SystemClock
from trading_platform.realtime.models import CandleEvent
from trading_platform.realtime.stream import (
    CcxtProMarketStream,
    CompositeMarketStream,
    MarketStream,
    PollingMarketStream,
    ReplayMarketStream,
)

#: Every await of this module is bounded by this explicit timeout.
TIMEOUT = 1.0

#: Anchor of the deterministic price grid.
EPOCH = pd.Timestamp("2024-01-01", tz="UTC")

#: The exact member set every implementation must expose (the seam contract).
DOCUMENTED_MEMBERS = frozenset(
    {
        "start",
        "stop",
        "next_candle",
        "history",
        "connected",
        "last_error",
        "reconnect_count",
        "max_wait_seconds",
    }
)

BTC = "BTC/USDT"
ETH = "ETH/USDT"
HOUR = "1h"


def run(coro: Any) -> Any:
    """Run one coroutine under an explicit bound so nothing can hang the suite."""
    return asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


# ---------------------------------------------------------------------------
# local helpers and fakes
# ---------------------------------------------------------------------------


def price_for(moment: Any) -> float:
    """Return a deterministic, positive price for one candle timestamp."""
    hours = int((pd.Timestamp(moment) - EPOCH).total_seconds() // 3600)
    return 100.0 + 0.01 * hours


def frame_for(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Build a valid OHLCV frame whose prices are derived from the timestamps."""
    close = [price_for(moment) for moment in index]
    return pd.DataFrame(
        {
            "open": close,
            "high": [value + 1.0 for value in close],
            "low": [value - 1.0 for value in close],
            "close": [value + 0.5 for value in close],
            "volume": [10.0] * len(index),
        },
        index=index,
    )


def replay_frame(periods: int = 3, *, start: str = "2024-01-01 00:00") -> pd.DataFrame:
    """Build a replay frame of hourly candles starting at ``start``."""
    index = pd.date_range(start=start, periods=periods, freq="1h", tz="UTC", name="timestamp")
    return frame_for(index)


def empty_frame() -> pd.DataFrame:
    """Return a well-formed, empty OHLCV frame."""
    index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    return pd.DataFrame(index=index, columns=list(REQUIRED_OHLCV_COLUMNS), dtype="float64")


def ms(moment: str) -> int:
    """Return the epoch milliseconds of an ISO instant (ccxt payload shape)."""
    return int(pd.Timestamp(moment, tz="UTC").timestamp() * 1000)


class GridProvider:
    """Offline ``MarketDataProvider`` answering candles on a fixed hourly grid.

    The window ends at ``floor(until)`` and holds ``count`` candles, so the
    newest row is the candle still forming at ``until``: exactly the shape a real
    provider returns.
    """

    def __init__(
        self,
        *,
        count: int = 2,
        fail_times: int = 0,
        fail_forever: bool = False,
        error: BaseException | None = None,
        empty: bool = False,
        bad_frame: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str, pd.Timestamp, pd.Timestamp]] = []
        self.count = count
        self.fail_times = fail_times
        self.fail_forever = fail_forever
        self.error = error or RuntimeError("provider exploded")
        self.empty = empty
        self.bad_frame = bad_frame

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: pd.Timestamp,
        until: pd.Timestamp,
    ) -> pd.DataFrame:
        self.calls.append((symbol, timeframe, pd.Timestamp(since), pd.Timestamp(until)))
        if self.fail_forever or self.fail_times > 0:
            if self.fail_times > 0:
                self.fail_times -= 1
            raise self.error
        if self.empty:
            return empty_frame()
        if self.bad_frame:
            return pd.DataFrame({"nope": [1.0]})
        delta = candle_delta(timeframe)
        last = pd.Timestamp(until).floor(delta)
        index = pd.date_range(end=last, periods=self.count, freq=delta, tz="UTC", name="timestamp")
        return frame_for(index)


def make_polling(
    *,
    clock: ManualClock,
    count: int = 2,
    history_candles: int = 2,
    poll_interval_seconds: float = 0.5,
    timeout_seconds: float = 0.5,
    max_reconnects: int = 3,
    reconnect_backoff_seconds: float = 0.25,
    **provider_kwargs: Any,
) -> tuple[PollingMarketStream, GridProvider]:
    """Build a polling stream wired to a :class:`GridProvider`."""
    provider = GridProvider(count=count, **provider_kwargs)
    stream = PollingMarketStream(
        provider,
        clock=clock,
        history_candles=history_candles,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
        max_reconnects=max_reconnects,
        reconnect_backoff_seconds=reconnect_backoff_seconds,
    )
    return stream, provider


class FakeChild:
    """Minimal local :class:`MarketStream` implementation (no other package)."""

    def __init__(
        self,
        label: str,
        events: list[CandleEvent] | None = None,
        *,
        frame: pd.DataFrame | None = None,
        fail_start: bool = False,
        fail_stop: bool = False,
        hang_start: bool = False,
        connected: bool = False,
        last_error: str | None = None,
        reconnect_count: int = 0,
        max_wait_seconds: float = 0.0,
    ) -> None:
        self.label = label
        self.events = list(events or [])
        self.frame = frame if frame is not None else empty_frame()
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.hang_start = hang_start
        self.started = False
        self.stopped = False
        self._connected = connected
        self._last_error = last_error
        self._reconnect_count = reconnect_count
        self._max_wait_seconds = float(max_wait_seconds)
        self._index = 0

    async def start(self) -> None:
        if self.hang_start:
            await asyncio.sleep(3600)
        if self.fail_start:
            raise MarketStreamError(f"{self.label} cannot start")
        self.started = True
        self._connected = True

    async def stop(self) -> None:
        self.stopped = True
        self._connected = False
        if self.fail_stop:
            raise MarketStreamError(f"{self.label} cannot stop")

    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
        if self._index >= len(self.events):
            return None
        event = self.events[self._index]
        self._index += 1
        return event

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        return self.frame.tail(count)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def max_wait_seconds(self) -> float:
        """Longest wait a ``next_candle`` call of this fake may legitimately take."""
        return self._max_wait_seconds


class LegacyFakeChild:
    """A duck-typed child written against the seam **before** ``max_wait_seconds``.

    External streams are structural, so a composite must keep accepting a child
    that declares no wait at all and contribute ``0.0`` in its place instead of
    raising ``AttributeError`` at boot.
    """

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
        return None

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        return empty_frame()

    @property
    def connected(self) -> bool:
        return True

    @property
    def last_error(self) -> str | None:
        return None

    @property
    def reconnect_count(self) -> int:
        return 0


def candle(timestamp: str, *, symbol: str = BTC, close: float = 100.0) -> CandleEvent:
    """Build a closed candle event for a fake child."""
    return CandleEvent(
        symbol=symbol,
        timeframe=HOUR,
        timestamp=pd.Timestamp(timestamp, tz="UTC"),
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1.0,
        closed=True,
    )


class FakeCcxtProExchange:
    """Fake ``ccxt.pro`` exchange handle: async ``watch_ohlcv``/``fetch_ohlcv``."""

    def __init__(
        self,
        rows: Any = None,
        *,
        hang: bool = False,
        bad_close: bool = False,
    ) -> None:
        self.rows = rows
        self.hang = hang
        self.bad_close = bad_close
        self.watch_calls: list[tuple[str, str]] = []
        self.fetch_calls: list[tuple[str, str, Any, Any]] = []
        self.closed = False
        self.config: Any = None

    async def watch_ohlcv(self, symbol: str, timeframe: str, *args: Any) -> Any:
        self.watch_calls.append((symbol, timeframe))
        if self.hang:
            await asyncio.sleep(3600)
        return self.rows

    async def fetch_ohlcv(self, symbol: str, timeframe: str, *args: Any) -> Any:
        self.fetch_calls.append((symbol, timeframe, *args))
        if self.hang:
            await asyncio.sleep(3600)
        return self.rows

    async def close(self) -> None:
        self.closed = True
        if self.bad_close:
            raise RuntimeError("close exploded")


def install_fake_ccxt_pro(monkeypatch: pytest.MonkeyPatch, exchange: FakeCcxtProExchange) -> Any:
    """Install a fake ``ccxt.pro`` module exposing a ``binance`` factory.

    Two seams are pinned, not one.  The production code resolves the extra with
    ``import ccxt.pro as ccxtpro`` inside a method body; CPython compiles that to
    ``IMPORT_NAME ccxt.pro`` (which returns the **top-level** ``ccxt`` package)
    followed by an attribute lookup ``ccxt.pro`` that only falls back to
    ``sys.modules['ccxt.pro']`` when the attribute is *absent*.

    That fallback is not enough in the whole-suite run: ``tests/test_freqtrade_integration.py``
    imports the real ``freqtrade`` at collection time (``importorskip`` succeeds in an
    environment where the extras are installed), which imports the real ``ccxt.pro``
    and therefore sets the ``ccxt.pro`` attribute on the real parent package.  Without
    the second seam below the fake would be silently bypassed, the real exchange would
    be built and the test would try to reach the network.  Both seams are reverted by
    ``monkeypatch`` at the end of the test, and neither exists when ``ccxt`` itself is
    not imported (the fresh-interpreter / dev-extra-only case).
    """

    def factory(config: Any = None) -> FakeCcxtProExchange:
        exchange.config = config
        return exchange

    module = types.SimpleNamespace(binance=factory)
    monkeypatch.setitem(sys.modules, "ccxt.pro", module)
    parent = sys.modules.get("ccxt")
    if parent is None:
        # ``ccxt`` is an OPTIONAL extra: with ``.[dev]`` only (the documented CI
        # environment) the parent package does not exist at all, and
        # ``import ccxt.pro`` fails on the *parent* before it can ever reach the
        # injected submodule.  A minimal stub parent completes the seam in an
        # extras-free environment while changing nothing when ccxt is installed.
        parent = types.ModuleType("ccxt")
        monkeypatch.setitem(sys.modules, "ccxt", parent)
    monkeypatch.setattr(parent, "pro", module, raising=False)
    return module


def make_ccxt(
    exchange: FakeCcxtProExchange,
    *,
    clock: ManualClock,
    timeout_seconds: float = 0.5,
    max_reconnects: int = 2,
    reconnect_backoff_seconds: float = 0.25,
) -> CcxtProMarketStream:
    """Build a ccxt.pro stream with a bounded configuration."""
    return CcxtProMarketStream(
        clock=clock,
        timeout_seconds=timeout_seconds,
        max_reconnects=max_reconnects,
        reconnect_backoff_seconds=reconnect_backoff_seconds,
        history_candles=4,
    )


# ---------------------------------------------------------------------------
# 1. ReplayMarketStream
# ---------------------------------------------------------------------------


def test_replay_emits_the_frame_rows_in_order_with_utc_timestamps() -> None:
    frame = replay_frame(3)
    stream = ReplayMarketStream({(BTC, HOUR): frame})

    async def scenario() -> list[CandleEvent]:
        await stream.start()
        out: list[CandleEvent] = []
        for _ in range(3):
            event = await stream.next_candle(BTC, HOUR)
            assert event is not None
            out.append(event)
        return out

    events = run(scenario())
    assert [event.timestamp for event in events] == list(frame.index)
    assert [event.timestamp.tz is not None for event in events] == [True, True, True]
    assert [str(event.timestamp.tz) for event in events] == ["UTC"] * 3
    assert [event.close for event in events] == list(frame["close"])
    assert all(event.closed for event in events)
    assert all(event.symbol == BTC and event.timeframe == HOUR for event in events)


def test_replay_exhausted_yields_none_and_stays_exhausted() -> None:
    stream = ReplayMarketStream({(BTC, HOUR): replay_frame(2)})

    async def scenario() -> tuple[Any, Any]:
        await stream.start()
        assert await stream.next_candle(BTC, HOUR) is not None
        assert await stream.next_candle(BTC, HOUR) is not None
        return await stream.next_candle(BTC, HOUR), await stream.next_candle(BTC, HOUR)

    assert run(scenario()) == (None, None)


def test_replay_loop_wraps_back_to_the_first_candle() -> None:
    frame = replay_frame(2)
    stream = ReplayMarketStream({(BTC, HOUR): frame}, loop=True)

    async def scenario() -> list[Any]:
        await stream.start()
        return [await stream.next_candle(BTC, HOUR) for _ in range(5)]

    timestamps = [event.timestamp for event in run(scenario()) if event is not None]
    assert timestamps == [
        frame.index[0],
        frame.index[1],
        frame.index[0],
        frame.index[1],
        frame.index[0],
    ]


def test_replay_unknown_key_raises_market_stream_error() -> None:
    stream = ReplayMarketStream({(BTC, HOUR): replay_frame(2)})

    async def scenario() -> None:
        await stream.start()
        await stream.next_candle(ETH, HOUR)

    with pytest.raises(MarketStreamError, match="no replay frame registered for ETH/USDT 1h"):
        run(scenario())


def test_replay_next_candle_before_start_raises_market_stream_error() -> None:
    stream = ReplayMarketStream({(BTC, HOUR): replay_frame(2)})

    with pytest.raises(MarketStreamError, match="not started"):
        run(stream.next_candle(BTC, HOUR))
    assert stream.connected is False


def test_replay_rejects_a_single_candle_frame() -> None:
    with pytest.raises(MarketStreamError, match="needs at least 2 candles"):
        ReplayMarketStream({(BTC, HOUR): replay_frame(1)})


def test_replay_rejects_an_empty_mapping() -> None:
    with pytest.raises(MarketStreamError, match="at least one"):
        ReplayMarketStream({})


def test_replay_rejects_an_invalid_frame() -> None:
    with pytest.raises(MarketStreamError, match="is not valid OHLCV"):
        ReplayMarketStream({(BTC, HOUR): pd.DataFrame({"nope": [1.0, 2.0]})})


def test_replay_seek_resumes_at_an_explicit_index() -> None:
    frame = replay_frame(4)
    stream = ReplayMarketStream({(BTC, HOUR): frame})

    async def scenario() -> list[Any]:
        await stream.start()
        assert await stream.next_candle(BTC, HOUR) is not None
        stream.seek(3)
        rest = [await stream.next_candle(BTC, HOUR) for _ in range(2)]
        stream.seek(len(frame))
        exhausted = await stream.next_candle(BTC, HOUR)
        return [*rest, exhausted]

    first, second, exhausted = run(scenario())
    assert first.timestamp == frame.index[3]
    assert second is None
    assert exhausted is None


@pytest.mark.parametrize("index", [-1, 5, 99])
def test_replay_seek_out_of_range_raises_market_stream_error(index: int) -> None:
    stream = ReplayMarketStream({(BTC, HOUR): replay_frame(4)})
    with pytest.raises(MarketStreamError, match="cannot seek"):
        stream.seek(index)


def test_replay_history_returns_at_most_count_candles_ending_at_the_last_emitted_one() -> None:
    frame = replay_frame(5)
    stream = ReplayMarketStream({(BTC, HOUR): frame})

    async def scenario() -> tuple[pd.DataFrame, pd.DataFrame]:
        await stream.start()
        assert await stream.next_candle(BTC, HOUR) is not None
        assert await stream.next_candle(BTC, HOUR) is not None
        assert await stream.next_candle(BTC, HOUR) is not None
        return await stream.history(BTC, HOUR, 2), await stream.history(BTC, HOUR, 10)

    window, wider = run(scenario())
    assert list(window.index) == [frame.index[1], frame.index[2]]
    assert window.index[-1] == frame.index[2]
    assert len(wider) == 3
    assert list(wider.index) == list(frame.index[:3])


def test_replay_history_before_the_first_emission_is_empty() -> None:
    stream = ReplayMarketStream({(BTC, HOUR): replay_frame(3)})

    async def scenario() -> tuple[pd.DataFrame, pd.DataFrame]:
        await stream.start()
        return await stream.history(BTC, HOUR, 5), await stream.history(ETH, HOUR, 5)

    before_start, unknown_key = run(scenario())
    assert before_start.empty
    assert unknown_key.empty
    assert list(before_start.columns) == list(REQUIRED_OHLCV_COLUMNS)
    assert before_start.index.tz is not None


def test_replay_history_after_stop_keeps_the_emitted_window() -> None:
    stream = ReplayMarketStream({(BTC, HOUR): replay_frame(3)})

    async def scenario() -> pd.DataFrame:
        await stream.start()
        assert await stream.next_candle(BTC, HOUR) is not None
        await stream.stop()
        return await stream.history(BTC, HOUR, 5)

    assert len(run(scenario())) == 1


def test_replay_history_ignores_a_non_positive_count() -> None:
    stream = ReplayMarketStream({(BTC, HOUR): replay_frame(3)})

    async def scenario() -> pd.DataFrame:
        await stream.start()
        assert await stream.next_candle(BTC, HOUR) is not None
        return await stream.history(BTC, HOUR, 0)

    assert run(scenario()).empty


def test_replay_health_properties_are_stable() -> None:
    stream = ReplayMarketStream({(BTC, HOUR): replay_frame(2)})
    assert stream.connected is False
    assert stream.last_error is None
    assert stream.reconnect_count == 0

    async def scenario() -> None:
        await stream.start()
        await stream.stop()

    run(scenario())
    assert stream.connected is False
    assert stream.last_error is None
    assert stream.reconnect_count == 0


def test_replay_keeps_the_two_keys_independent() -> None:
    btc = replay_frame(2, start="2024-01-01 00:00")
    eth = replay_frame(2, start="2024-01-01 06:00")
    stream = ReplayMarketStream({(BTC, HOUR): btc, (ETH, HOUR): eth})

    async def scenario() -> tuple[Any, Any]:
        await stream.start()
        return await stream.next_candle(ETH, HOUR), await stream.next_candle(BTC, HOUR)

    eth_event, btc_event = run(scenario())
    assert eth_event.timestamp == eth.index[0]
    assert btc_event.timestamp == btc.index[0]


# ---------------------------------------------------------------------------
# 2. determinism
# ---------------------------------------------------------------------------


def test_two_identical_replay_runs_are_byte_identical() -> None:
    frame = replay_frame(6)

    def scenario() -> str:
        stream = ReplayMarketStream({(BTC, HOUR): frame})

        async def go() -> list[dict[str, Any]]:
            await stream.start()
            out: list[dict[str, Any]] = []
            for _ in range(8):
                event = await stream.next_candle(BTC, HOUR)
                if event is not None:
                    out.append(event.to_dict())
            return out

        return json.dumps(run(go()), sort_keys=True)

    assert scenario() == scenario()


def test_two_identical_polling_runs_are_byte_identical() -> None:
    def scenario() -> str:
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        stream, _ = make_polling(clock=clock)

        async def go() -> list[dict[str, Any]]:
            await stream.start()
            out: list[dict[str, Any]] = []
            for _ in range(4):
                event = await stream.next_candle(BTC, HOUR)
                clock.advance(3600)
                if event is not None:
                    out.append(event.to_dict())
            return out

        return json.dumps(run(go()), sort_keys=True)

    assert scenario() == scenario()


# ---------------------------------------------------------------------------
# 3. PollingMarketStream -- happy path
# ---------------------------------------------------------------------------


def test_polling_emits_only_closed_candles() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, provider = make_polling(clock=clock)

    async def scenario() -> Any:
        await stream.start()
        return await stream.next_candle(BTC, HOUR)

    event = run(scenario())
    assert event is not None
    # The candle starting at `until` is still forming: only the previous one is closed.
    assert event.timestamp == pd.Timestamp("2024-01-01 11:00", tz="UTC")
    assert event.closed is True
    symbol, timeframe, since, until = provider.calls[0]
    assert (symbol, timeframe) == (BTC, HOUR)
    assert until == pd.Timestamp("2024-01-01 12:00", tz="UTC")
    assert since == until - 2 * candle_delta(HOUR)


def test_polling_idle_wait_survives_when_the_interval_equals_the_timeout() -> None:
    """``poll_interval_seconds == timeout_seconds`` must not raise ``TimeoutError``.

    Regression test for the defect that crash-looped the Docker deployment: the
    idle wait slept the poll interval under a bound of exactly the same duration,
    so the two deadlines collided on the first idle poll and every profile died.
    A real clock is required -- ``ManualClock.sleep`` returns immediately, so the
    collision only exists when the sleep really awaits.
    """
    stream, _provider = make_polling(
        clock=SystemClock(),
        count=2,
        poll_interval_seconds=0.05,
        timeout_seconds=0.05,
    )

    async def scenario() -> list[Any]:
        await stream.start()
        return [await stream.next_candle(BTC, HOUR) for _ in range(3)]

    events = run(scenario())
    assert events[0] is not None, "the first poll must deliver the closed candle"
    assert events[1:] == [None, None], "an idle poll reports 'nothing new'"
    assert stream.last_error is None


def test_polling_backoff_may_pass_the_timeout_without_being_cut_short() -> None:
    """An exponential retry delay larger than the stream timeout still completes.

    A pacing wait is not a read: bounding the backoff by ``timeout_seconds`` cut the
    third retry (0.04 s under a 0.02 s bound) short with a ``TimeoutError``, which
    killed a profile over a failure the stream is designed to ride out.  A real
    clock is used so the collision actually schedules.
    """
    stream, provider = make_polling(
        clock=SystemClock(),
        timeout_seconds=0.02,
        max_reconnects=4,
        reconnect_backoff_seconds=0.01,
        fail_forever=True,
    )

    async def scenario() -> None:
        await stream.start()
        await stream.next_candle(BTC, HOUR)

    with pytest.raises(MarketStreamError) as excinfo:
        run(scenario())

    assert "gave up after 4 attempts" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert len(provider.calls) == 4
    assert stream.reconnect_count == 4


def test_polling_returns_none_when_nothing_is_new_and_waits_one_poll_interval() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, _ = make_polling(clock=clock, poll_interval_seconds=0.5)

    async def scenario() -> tuple[Any, Any]:
        await stream.start()
        first = await stream.next_candle(BTC, HOUR)
        before = clock.monotonic()
        second = await stream.next_candle(BTC, HOUR)
        return first, (second, clock.monotonic() - before)

    first, (second, waited) = run(scenario())
    assert first is not None
    assert second is None
    assert waited == pytest.approx(0.5)
    assert stream.connected is True
    assert stream.last_error is None


def test_polling_advancing_one_candle_releases_exactly_one_more_candle() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, _ = make_polling(clock=clock)

    async def scenario() -> tuple[Any, Any, Any]:
        await stream.start()
        first = await stream.next_candle(BTC, HOUR)
        clock.advance(3600)
        second = await stream.next_candle(BTC, HOUR)
        third = await stream.next_candle(BTC, HOUR)
        return first, second, third

    first, second, third = run(scenario())
    assert first.timestamp == pd.Timestamp("2024-01-01 11:00", tz="UTC")
    assert second.timestamp == pd.Timestamp("2024-01-01 12:00", tz="UTC")
    assert third is None


def test_polling_never_emits_the_same_candle_twice() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, _ = make_polling(clock=clock)

    async def scenario() -> list[pd.Timestamp]:
        await stream.start()
        seen: list[pd.Timestamp] = []
        for _ in range(6):
            clock.advance(3600)
            for _ in range(2):
                event = await stream.next_candle(BTC, HOUR)
                if event is not None:
                    seen.append(event.timestamp)
        return seen

    timestamps = run(scenario())
    assert len(timestamps) == len(set(timestamps)) == 6
    assert timestamps == sorted(timestamps)


def test_polling_skips_stale_candles_and_emits_the_newest_closed_one() -> None:
    """A live stream trades the present: past candles are skipped, never replayed.

    Regression test for the ``warmup_incomplete`` dead-lock: the runner warms the
    strategy up on ``history`` (a window ending *now*) and appends the emitted
    candle to it, so emitting the **oldest** candle of the look-back window fed the
    strategy a one-row frame -- and, once the frame grew, made the engine decide on
    a days-old candle while the venue would fill at today's price.
    """
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, _ = make_polling(clock=clock, count=4, history_candles=4)

    async def scenario() -> list[pd.Timestamp]:
        await stream.start()
        out: list[pd.Timestamp] = []
        for _ in range(6):
            event = await stream.next_candle(BTC, HOUR)
            if event is not None:
                out.append(event.timestamp)
        return out

    # The window holds 09:00, 10:00, 11:00 closed; only 11:00 is ever emitted, and
    # the calls after it report "nothing new" instead of walking backwards.
    assert run(scenario()) == [pd.Timestamp("2024-01-01 11:00", tz="UTC")]


def test_polling_first_emission_agrees_with_the_warmup_window() -> None:
    """The invariant the runner depends on: ``history[index < stamp]`` is never empty."""
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, _ = make_polling(clock=clock, count=6, history_candles=6)

    async def scenario() -> tuple[pd.Timestamp, int]:
        await stream.start()
        event = await stream.next_candle(BTC, HOUR)
        assert event is not None
        window = await stream.history(BTC, HOUR, 4)
        return event.timestamp, int((window.index < event.timestamp).sum())

    stamp, rows = run(scenario())
    assert stamp == pd.Timestamp("2024-01-01 11:00", tz="UTC")
    assert rows > 0


def test_polling_history_returns_the_provider_window() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, provider = make_polling(clock=clock, count=3, history_candles=3)

    async def scenario() -> pd.DataFrame:
        await stream.start()
        return await stream.history(BTC, HOUR, 3)

    frame = run(scenario())
    assert len(frame) == 3
    assert frame.index[-1] == pd.Timestamp("2024-01-01 12:00", tz="UTC")
    assert frame.index.tz is not None
    assert provider.calls[0][2] == pd.Timestamp("2024-01-01 09:00", tz="UTC")


def test_polling_history_is_empty_for_a_non_positive_count_and_never_calls_the_provider() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, provider = make_polling(clock=clock)

    async def scenario() -> pd.DataFrame:
        await stream.start()
        return await stream.history(BTC, HOUR, 0)

    assert run(scenario()).empty
    assert provider.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"history_candles": 0},
        {"history_candles": "many"},
        {"poll_interval_seconds": 0},
        {"poll_interval_seconds": -1.0},
        {"timeout_seconds": 0.0},
        {"timeout_seconds": "fast"},
        {"max_reconnects": 0},
        {"reconnect_backoff_seconds": -0.5},
        {"reconnect_backoff_seconds": "slow"},
    ],
)
def test_polling_rejects_invalid_numeric_arguments(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        PollingMarketStream(GridProvider(), clock=ManualClock(), **kwargs)


def test_polling_rejects_an_empty_exchange_name() -> None:
    with pytest.raises(ValueError, match="exchange"):
        PollingMarketStream(GridProvider(), clock=ManualClock(), exchange="  ")


def test_polling_next_candle_before_start_raises_market_stream_error() -> None:
    stream, _ = make_polling(clock=ManualClock())
    with pytest.raises(MarketStreamError, match="not started"):
        run(stream.next_candle(BTC, HOUR))


def test_polling_stop_marks_the_stream_as_disconnected() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, _ = make_polling(clock=clock)

    async def scenario() -> None:
        await stream.start()
        await stream.stop()

    run(scenario())
    assert stream.connected is False
    assert stream.reconnect_count == 0


# ---------------------------------------------------------------------------
# 4. PollingMarketStream -- failure paths
# ---------------------------------------------------------------------------


def test_polling_retries_with_virtual_backoff_then_gives_up() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, provider = make_polling(
        clock=clock, fail_forever=True, max_reconnects=3, reconnect_backoff_seconds=1.0
    )

    async def scenario() -> None:
        await stream.start()
        await stream.next_candle(BTC, HOUR)

    started = time.monotonic()
    with pytest.raises(MarketStreamError) as excinfo:
        run(scenario())
    elapsed = time.monotonic() - started

    assert "gave up after 3 attempts" in str(excinfo.value)
    assert "provider exploded" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert len(provider.calls) == 3
    assert stream.reconnect_count == 3
    assert stream.last_error == "provider exploded"
    assert stream.connected is False
    # 1 s then 2 s of *virtual* time: the ManualClock never blocks.
    assert clock.monotonic() == pytest.approx(3.0)
    assert elapsed < TIMEOUT


def test_polling_success_after_failures_resets_the_health_and_keeps_the_counter() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, provider = make_polling(
        clock=clock, fail_times=2, max_reconnects=4, reconnect_backoff_seconds=1.0
    )

    async def scenario() -> Any:
        await stream.start()
        return await stream.next_candle(BTC, HOUR)

    event = run(scenario())
    assert event is not None
    assert event.timestamp == pd.Timestamp("2024-01-01 11:00", tz="UTC")
    assert len(provider.calls) == 3
    assert stream.connected is True
    assert stream.last_error is None
    assert stream.reconnect_count == 2
    assert clock.monotonic() == pytest.approx(1.0 + 2.0)


def test_polling_treats_an_empty_frame_as_a_failure() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, _ = make_polling(clock=clock, empty=True, max_reconnects=2)

    async def scenario() -> None:
        await stream.start()
        await stream.next_candle(BTC, HOUR)

    with pytest.raises(MarketStreamError, match="returned no candle"):
        run(scenario())
    assert stream.reconnect_count == 2
    assert stream.connected is False


def test_polling_treats_an_invalid_frame_as_a_failure() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, _ = make_polling(clock=clock, bad_frame=True, max_reconnects=2)

    async def scenario() -> None:
        await stream.start()
        await stream.next_candle(BTC, HOUR)

    with pytest.raises(MarketStreamError, match="gave up after 2 attempts"):
        run(scenario())
    assert stream.reconnect_count == 2


def test_polling_records_a_placeholder_for_a_message_less_failure() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, _ = make_polling(
        clock=clock,
        error=RuntimeError(),
        fail_forever=True,
        max_reconnects=1,
        reconnect_backoff_seconds=0.0,
    )

    async def scenario() -> None:
        await stream.start()
        await stream.next_candle(BTC, HOUR)

    with pytest.raises(MarketStreamError, match="unknown market-data failure"):
        run(scenario())
    assert stream.last_error == "unknown market-data failure"
    assert stream.reconnect_count == 1


def test_polling_history_returns_an_empty_frame_instead_of_raising() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, _ = make_polling(clock=clock, fail_forever=True)

    async def scenario() -> pd.DataFrame:
        await stream.start()
        return await stream.history(BTC, HOUR, 5)

    frame = run(scenario())
    assert frame.empty
    assert list(frame.columns) == list(REQUIRED_OHLCV_COLUMNS)
    # history is a best-effort read model: it never degrades the health state.
    assert stream.last_error is None
    assert stream.reconnect_count == 0


def test_polling_history_swallows_an_unsupported_timeframe() -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    stream, _ = make_polling(clock=clock)

    async def scenario() -> pd.DataFrame:
        await stream.start()
        return await stream.history(BTC, "7h", 5)

    assert run(scenario()).empty


# ---------------------------------------------------------------------------
# 5. CcxtProMarketStream
# ---------------------------------------------------------------------------


def test_ccxt_pro_start_raises_when_the_optional_extra_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "ccxt.pro", None)
    stream = make_ccxt(FakeCcxtProExchange(), clock=ManualClock())

    with pytest.raises(MarketStreamError) as excinfo:
        run(stream.start())
    assert "ccxt.pro is not installed" in str(excinfo.value)
    assert "[exchange]" in str(excinfo.value)
    assert stream.connected is False


def test_ccxt_pro_maps_the_watch_payload_to_a_closed_candle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    rows = [
        [ms("2024-01-01 11:00"), 100.0, 101.0, 99.0, 100.5, 5.0],
        [ms("2024-01-01 12:00"), 101.0, 102.0, 100.0, 101.5, 6.0],
    ]
    exchange = FakeCcxtProExchange(rows)
    install_fake_ccxt_pro(monkeypatch, exchange)
    stream = make_ccxt(exchange, clock=clock)

    async def scenario() -> Any:
        await stream.start()
        return await stream.next_candle(BTC, HOUR)

    event = run(scenario())
    assert event is not None
    assert event.timestamp == pd.Timestamp("2024-01-01 11:00", tz="UTC")
    assert (event.open, event.high, event.low, event.close, event.volume) == (
        100.0,
        101.0,
        99.0,
        100.5,
        5.0,
    )
    assert event.closed is True
    assert exchange.watch_calls == [(BTC, HOUR)]
    assert exchange.config == {"enableRateLimit": True}
    assert stream.connected is True

    async def second() -> Any:
        return await stream.next_candle(BTC, HOUR)

    assert run(second()) is None


def test_ccxt_pro_emits_the_newest_closed_candle_of_the_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnected buffer holds several closed candles: only the last one is traded.

    The same rule as the polling stream (regression test for the deployment defect):
    a live seam must not replay its buffer, otherwise the engine decides on a
    candle that is older than the warm-up window ``history`` returns.
    """
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    rows = [
        [ms("2024-01-01 09:00"), 90.0, 91.0, 89.0, 90.5, 1.0],
        [ms("2024-01-01 10:00"), 92.0, 93.0, 91.0, 92.5, 2.0],
        [ms("2024-01-01 11:00"), 100.0, 101.0, 99.0, 100.5, 5.0],
        [ms("2024-01-01 12:00"), 101.0, 102.0, 100.0, 101.5, 6.0],
    ]
    exchange = FakeCcxtProExchange(rows)
    install_fake_ccxt_pro(monkeypatch, exchange)
    stream = make_ccxt(exchange, clock=clock)

    async def scenario() -> list[pd.Timestamp]:
        await stream.start()
        out: list[pd.Timestamp] = []
        for _ in range(4):
            event = await stream.next_candle(BTC, HOUR)
            if event is not None:
                out.append(event.timestamp)
        return out

    assert run(scenario()) == [pd.Timestamp("2024-01-01 11:00", tz="UTC")]


def test_ccxt_pro_unknown_exchange_raises_market_stream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ccxt_pro(monkeypatch, FakeCcxtProExchange())
    stream = CcxtProMarketStream(clock=ManualClock(), exchange="nonexistent-venue")

    with pytest.raises(MarketStreamError, match="does not provide the exchange"):
        run(stream.start())


def test_ccxt_pro_timeout_is_bounded_and_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    exchange = FakeCcxtProExchange(hang=True)
    install_fake_ccxt_pro(monkeypatch, exchange)
    stream = make_ccxt(
        exchange, clock=clock, timeout_seconds=0.01, max_reconnects=1, reconnect_backoff_seconds=0.0
    )

    async def scenario() -> None:
        await stream.start()
        await stream.next_candle(BTC, HOUR)

    started = time.monotonic()
    with pytest.raises(MarketStreamError) as excinfo:
        run(scenario())
    assert time.monotonic() - started < TIMEOUT
    assert isinstance(excinfo.value.__cause__, TimeoutError)
    assert stream.reconnect_count == 1
    assert stream.last_error is not None
    assert stream.connected is False


def test_ccxt_pro_retries_with_a_bounded_backoff_before_giving_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    exchange = FakeCcxtProExchange(hang=True)
    install_fake_ccxt_pro(monkeypatch, exchange)
    stream = make_ccxt(
        exchange,
        clock=clock,
        timeout_seconds=0.01,
        max_reconnects=2,
        reconnect_backoff_seconds=1.0,
    )

    async def scenario() -> None:
        await stream.start()
        await stream.next_candle(BTC, HOUR)

    started = time.monotonic()
    with pytest.raises(MarketStreamError, match="gave up after 2 attempts"):
        run(scenario())
    assert time.monotonic() - started < TIMEOUT
    assert stream.reconnect_count == 2
    # 1 s of *virtual* time: the ManualClock never blocks.
    assert clock.monotonic() == pytest.approx(1.0)


def test_ccxt_pro_rejects_an_empty_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    exchange = FakeCcxtProExchange(None)
    install_fake_ccxt_pro(monkeypatch, exchange)
    stream = make_ccxt(exchange, clock=clock, max_reconnects=1, reconnect_backoff_seconds=0.0)

    async def scenario() -> None:
        await stream.start()
        await stream.next_candle(BTC, HOUR)

    with pytest.raises(MarketStreamError, match="no candle"):
        run(scenario())


@pytest.mark.parametrize("payload", [[], ()])
def test_ccxt_pro_rejects_an_empty_row_list(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    exchange = FakeCcxtProExchange(payload)
    install_fake_ccxt_pro(monkeypatch, exchange)
    stream = make_ccxt(exchange, clock=clock, max_reconnects=1, reconnect_backoff_seconds=0.0)

    async def scenario() -> None:
        await stream.start()
        await stream.next_candle(BTC, HOUR)

    with pytest.raises(MarketStreamError, match="empty candle list"):
        run(scenario())


def test_ccxt_pro_stop_without_a_close_method_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange = types.SimpleNamespace(
        watch_ohlcv=FakeCcxtProExchange().watch_ohlcv,
        fetch_ohlcv=FakeCcxtProExchange().fetch_ohlcv,
        config=None,
    )
    install_fake_ccxt_pro(monkeypatch, exchange)  # type: ignore[arg-type]  # duck-typed handle
    stream = make_ccxt(FakeCcxtProExchange(), clock=ManualClock())

    async def scenario() -> None:
        await stream.start()
        await stream.stop()

    run(scenario())
    assert stream.connected is False


def test_ccxt_pro_rejects_an_empty_exchange_name() -> None:
    with pytest.raises(ValueError, match="exchange"):
        CcxtProMarketStream(clock=ManualClock(), exchange=" ")


def test_ccxt_pro_history_ignores_a_non_positive_count() -> None:
    stream = CcxtProMarketStream(clock=ManualClock())

    async def scenario() -> pd.DataFrame:
        return await stream.history(BTC, HOUR, 0)

    assert run(scenario()).empty


def test_ccxt_pro_history_maps_the_fetch_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    rows = [
        [ms("2024-01-01 10:00"), 99.0, 100.0, 98.0, 99.5, 4.0],
        [ms("2024-01-01 11:00"), 100.0, 101.0, 99.0, 100.5, 5.0],
    ]
    exchange = FakeCcxtProExchange(rows)
    install_fake_ccxt_pro(monkeypatch, exchange)
    stream = make_ccxt(exchange, clock=clock)

    async def scenario() -> pd.DataFrame:
        await stream.start()
        return await stream.history(BTC, HOUR, 2)

    frame = run(scenario())
    assert len(frame) == 2
    assert frame.index[-1] == pd.Timestamp("2024-01-01 11:00", tz="UTC")
    assert exchange.fetch_calls[0][:2] == (BTC, HOUR)
    assert exchange.fetch_calls[0][3] == 2


def test_ccxt_pro_history_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
    exchange = FakeCcxtProExchange(hang=True)
    install_fake_ccxt_pro(monkeypatch, exchange)
    stream = make_ccxt(exchange, clock=clock, timeout_seconds=0.01)

    async def scenario() -> tuple[pd.DataFrame, pd.DataFrame]:
        before_start = await stream.history(BTC, HOUR, 2)
        await stream.start()
        return before_start, await stream.history(BTC, HOUR, 2)

    before_start, hanging = run(scenario())
    assert before_start.empty
    assert hanging.empty
    assert list(hanging.columns) == list(REQUIRED_OHLCV_COLUMNS)


def test_ccxt_pro_next_candle_before_start_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_ccxt_pro(monkeypatch, FakeCcxtProExchange())
    stream = make_ccxt(FakeCcxtProExchange(), clock=ManualClock())

    with pytest.raises(MarketStreamError, match="not started"):
        run(stream.next_candle(BTC, HOUR))


def test_ccxt_pro_stop_closes_the_exchange_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange = FakeCcxtProExchange([[ms("2024-01-01 11:00"), 1.0, 1.0, 1.0, 1.0, 1.0]])
    install_fake_ccxt_pro(monkeypatch, exchange)
    stream = make_ccxt(exchange, clock=ManualClock())

    async def scenario() -> None:
        await stream.stop()  # stop before start is a no-op
        await stream.start()
        await stream.stop()
        await stream.stop()

    run(scenario())
    assert exchange.closed is True
    assert stream.connected is False

    broken = FakeCcxtProExchange(bad_close=True)
    install_fake_ccxt_pro(monkeypatch, broken)
    other = make_ccxt(broken, clock=ManualClock())

    async def scenario_2() -> None:
        await other.start()
        await other.stop()

    run(scenario_2())
    assert other.connected is False


# ---------------------------------------------------------------------------
# 6. CompositeMarketStream
# ---------------------------------------------------------------------------


def test_composite_routes_each_key_to_its_own_child() -> None:
    btc_child = FakeChild("btc", [candle("2024-01-01 11:00", symbol=BTC)])
    eth_child = FakeChild("eth", [candle("2024-01-01 11:00", symbol=ETH, close=200.0)])
    composite = CompositeMarketStream({(BTC, HOUR): btc_child, (ETH, HOUR): eth_child})

    async def scenario() -> tuple[Any, Any]:
        await composite.start()
        return await composite.next_candle(ETH, HOUR), await composite.next_candle(BTC, HOUR)

    eth_event, btc_event = run(scenario())
    assert eth_event.symbol == ETH
    assert btc_event.symbol == BTC
    assert btc_child.started and eth_child.started
    assert composite.keys() == [
        (BTC, HOUR),
        (ETH, HOUR),
    ]  # hmm ETH < BTC? no: "BTC/USDT" < "ETH/USDT"
    assert composite.connected is True


def test_composite_start_survives_a_failing_child() -> None:
    healthy = FakeChild("healthy", [candle("2024-01-01 11:00")])
    broken = FakeChild("broken", fail_start=True)
    composite = CompositeMarketStream({(BTC, HOUR): broken, (ETH, HOUR): healthy})

    async def scenario() -> Any:
        await composite.start()
        return await composite.next_candle(ETH, HOUR)

    event = run(scenario())
    assert event is not None
    assert healthy.started is True
    assert broken.started is False
    assert composite.connected is True
    assert composite.last_error == "broken cannot start"


def test_composite_unknown_key_raises_and_history_is_empty() -> None:
    composite = CompositeMarketStream({(BTC, HOUR): FakeChild("btc")})

    async def next_unknown() -> None:
        await composite.next_candle(ETH, HOUR)

    with pytest.raises(MarketStreamError, match="no stream registered for ETH/USDT 1h"):
        run(next_unknown())

    async def history_unknown() -> pd.DataFrame:
        return await composite.history(ETH, HOUR, 4)

    assert run(history_unknown()).empty


def test_composite_health_is_aggregated_over_the_children() -> None:
    left = FakeChild("left", connected=True, last_error=None, reconnect_count=3)
    right = FakeChild("right", connected=False, last_error="right is down", reconnect_count=4)
    composite = CompositeMarketStream({(BTC, HOUR): left, (ETH, HOUR): right})

    async def scenario() -> pd.DataFrame:
        await composite.start()
        return await composite.history(BTC, HOUR, 2)

    run(scenario())
    assert composite.connected is True
    assert composite.reconnect_count == 7
    # "BTC/USDT" sorts before "ETH/USDT": the first non-None error in key order wins.
    assert composite.last_error == "right is down"

    silent = CompositeMarketStream({(BTC, HOUR): FakeChild("silent")})
    assert silent.connected is False
    assert silent.last_error is None
    assert silent.reconnect_count == 0


def test_composite_stop_ignores_a_failing_child() -> None:
    good = FakeChild("good")
    bad = FakeChild("bad", fail_stop=True)
    composite = CompositeMarketStream({(BTC, HOUR): bad, (ETH, HOUR): good})

    async def scenario() -> None:
        await composite.start()
        await composite.stop()

    run(scenario())
    assert good.stopped is True
    assert bad.stopped is True
    assert composite.connected is False


def test_composite_rejects_an_empty_mapping() -> None:
    with pytest.raises(ValueError, match="at least one child"):
        CompositeMarketStream({})


def test_composite_fan_out_is_bounded() -> None:
    hanging = FakeChild("hanging", hang_start=True)
    composite = CompositeMarketStream({(BTC, HOUR): hanging})

    async def scenario() -> None:
        with patch.object(stream_module, "_FAN_OUT_TIMEOUT_SECONDS", 0.01):
            started = time.monotonic()
            await composite.start()
            assert time.monotonic() - started < TIMEOUT
            assert composite.last_error is not None
            assert "timed out" in (composite.last_error or "")

    run(scenario())
    assert hanging.started is False


# ---------------------------------------------------------------------------
# 7. structural conformance of the seam
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "build_instance"),
    [
        (ReplayMarketStream, lambda: ReplayMarketStream({(BTC, HOUR): replay_frame(2)})),
        (PollingMarketStream, lambda: PollingMarketStream(GridProvider(), clock=ManualClock())),
        (CcxtProMarketStream, lambda: CcxtProMarketStream(clock=ManualClock())),
        (
            CompositeMarketStream,
            lambda: CompositeMarketStream({(BTC, HOUR): FakeChild("child")}),
        ),
    ],
)
def test_every_class_exposes_the_documented_member_set(cls: type, build_instance: Any) -> None:
    """Every stream exposes the documented members, the declared wait included.

    ``max_wait_seconds`` is read-only on the class **and** answers a non-negative
    float on a built instance: the runner derives its bound from that value, so a
    missing or negative member would silently disarm the idle-poll invariant.
    """
    for name in ("start", "stop", "next_candle", "history"):
        assert asyncio.iscoroutinefunction(getattr(cls, name)), name
    for name in ("connected", "last_error", "reconnect_count", "max_wait_seconds"):
        assert isinstance(getattr(cls, name), property), name
    assert set(dir(cls)) >= DOCUMENTED_MEMBERS

    instance = build_instance()
    declared_wait = instance.max_wait_seconds
    assert isinstance(declared_wait, float), cls.__name__
    assert declared_wait >= 0.0, cls.__name__


def test_max_wait_seconds_declares_the_longest_legitimate_wait() -> None:
    """Each implementation answers the longest wait one ``next_candle`` may take.

    This is the value the runner's bound is derived from, so each shape matters:

    * a replay reads prepared rows and never waits -- ``0.0``;
    * the deployment's polling pair (``poll_interval_seconds = 30``,
      ``timeout_seconds = 10``) declares the **30 s** idle poll, not the 10 s
      timeout: deriving the bound from the timeout alone cut that healthy idle poll
      into a ``TimeoutError`` and crash-looped every profile;
    * a retry may instead sleep the whole bounded backoff series, which dominates a
      tiny poll interval (``1 + 2 + 4 + 8 = 15 s`` for a base of 1 s and
      ``max_reconnects = 5``), so the declared wait is the longer of the two;
    * a composite delegates to exactly one child per call, so its declared wait is
      the longest declared wait of the children it may route to.
    """
    assert ReplayMarketStream({(BTC, HOUR): replay_frame(2)}).max_wait_seconds == 0.0

    deployment = PollingMarketStream(
        GridProvider(), clock=ManualClock(), poll_interval_seconds=30.0, timeout_seconds=10.0
    )
    assert deployment.max_wait_seconds == 30.0
    # The declared wait is the longer of the two inputs the stream was built with,
    # and the cadence itself is readable back for a caller deriving its own bound.
    assert deployment.poll_interval_seconds == 30.0

    retrying = PollingMarketStream(
        GridProvider(),
        clock=ManualClock(),
        poll_interval_seconds=0.01,
        reconnect_backoff_seconds=1.0,
        max_reconnects=5,
    )
    assert retrying.max_wait_seconds == 15.0

    live = CcxtProMarketStream(
        clock=ManualClock(),
        timeout_seconds=10.0,
        reconnect_backoff_seconds=1.0,
        max_reconnects=5,
    )
    assert live.max_wait_seconds == 15.0
    assert live.timeout_seconds == 10.0

    composite = CompositeMarketStream(
        {
            (BTC, HOUR): FakeChild("idle", max_wait_seconds=0.0),
            (ETH, HOUR): FakeChild("paced", max_wait_seconds=30.0),
        }
    )
    assert composite.max_wait_seconds == 30.0

    # A duck-typed child that predates the member declares no wait at all.
    legacy = CompositeMarketStream({(BTC, HOUR): LegacyFakeChild()})
    assert legacy.max_wait_seconds == 0.0


def test_a_local_fake_satisfies_the_market_stream_protocol() -> None:
    fake = FakeChild("fake")
    assert isinstance(fake, MarketStream)
    assert run(fake.next_candle(BTC, HOUR)) is None


@pytest.mark.parametrize(
    ("stream_factory", "expected"),
    [
        (lambda: ReplayMarketStream({(BTC, HOUR): replay_frame(2)}), 0),
        (lambda: PollingMarketStream(GridProvider(), clock=ManualClock()), 0),
        (lambda: CcxtProMarketStream(clock=ManualClock()), 0),
        (lambda: CompositeMarketStream({(BTC, HOUR): FakeChild("child", reconnect_count=2)}), 2),
    ],
)
def test_reconnect_counter_is_exposed_by_every_implementation(
    stream_factory: Any, expected: int
) -> None:
    stream = stream_factory()
    assert isinstance(stream, MarketStream)
    assert stream.reconnect_count == expected


def test_the_harness_bounds_a_hung_implementation() -> None:
    async def hangs() -> None:
        await asyncio.sleep(3600)

    with pytest.raises(TimeoutError):
        run(hangs())
