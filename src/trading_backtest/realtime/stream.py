"""Market-stream seam of the realtime layer (layer 6).

A :class:`MarketStream` is the *only* way candles enter the realtime engine.  The
seam is deliberately narrow -- ``start``/``stop``/``next_candle``/``history`` plus
three health properties -- so the engine can be driven, offline and
deterministically, by a replay of prepared frames while production runs against a
polling provider or a live WebSocket feed.

Four implementations are provided:

* :class:`ReplayMarketStream` -- a time-free, deterministic replay of prepared
  frames; the primary engine of the offline tests and of ``--once``;
* :class:`PollingMarketStream` -- polls a
  :class:`~trading_backtest.data.loader.MarketDataProvider` through the injected
  :class:`~trading_backtest.realtime.clock.Clock`;
* :class:`CcxtProMarketStream` -- a live ``ccxt.pro`` stream, imported lazily
  inside :meth:`CcxtProMarketStream.start` so ``ccxt`` stays an optional extra;
* :class:`CompositeMarketStream` -- multiplexes several symbol/timeframe pairs.

Contract shared by every implementation
---------------------------------------
* ``next_candle`` returns the **newest closed candle strictly newer** than the
  last candle already emitted for that ``(symbol, timeframe)`` pair, or ``None``
  when there is nothing new.  A candle is *closed* when its timestamp is at most
  ``now - candle_delta(timeframe)``: the candle that is still forming is never
  emitted, so the strategy always decides on data whose close is known.
* ``history`` returns the most recent candles available to a decision taken
  **now**, so the runner can warm a strategy up.  It never raises: an unknown
  key or an unavailable window yields a well-formed, empty OHLCV frame.
* the two rules above are **one contract, not two**: the runner warms a strategy
  up on ``history`` (a window ending *now*) and appends the candle returned by
  ``next_candle`` to it, so a stream that handed out an old candle would feed the
  strategy a truncated window -- and, on a live venue, would trade a past signal
  at the present price.  A deliberate replay of a past window is
  :class:`ReplayMarketStream`'s job, never the live streams'.
* every ``await`` on a wait of our own is bounded by ``asyncio.wait_for`` with an
  explicit timeout; a retry loop is bounded by ``max_reconnects`` and ends in a
  :class:`~trading_backtest.core.errors.MarketStreamError`.

Time-free replay vs wall-clock time
-----------------------------------
:class:`ReplayMarketStream` takes no :class:`Clock`: it emits the timestamps
carried by its frames and never sleeps, which makes a run byte-identical
whatever the machine does.  The polling and ``ccxt.pro`` streams take a
:class:`Clock` so that a test can advance virtual time instead of waiting, and
so that no module of the realtime layer ever calls ``datetime.now()`` or
``time.time()`` directly.

The synchronous provider call of :class:`PollingMarketStream` is *not* wrapped in
``asyncio.wait_for``: it is a blocking call, and it is the provider's own HTTP
timeout (``requests``/``ccxt``) that bounds it.  Every wait *we* own is bounded.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from trading_backtest.core.constants import (
    OHLCV_INDEX_NAME,
    REQUIRED_OHLCV_COLUMNS,
    UTC,
    candle_delta,
)
from trading_backtest.core.errors import DataValidationError, MarketStreamError
from trading_backtest.data.loader import MarketDataProvider
from trading_backtest.data.validation import ensure_ohlcv
from trading_backtest.realtime.clock import Clock
from trading_backtest.realtime.models import CandleEvent

__all__ = [
    "CcxtProMarketStream",
    "CompositeMarketStream",
    "MarketStream",
    "PollingMarketStream",
    "ReplayMarketStream",
]

#: ``__name__`` resolves to ``trading_backtest.realtime.stream``.
LOGGER = logging.getLogger(__name__)

#: Timeout that bounds the fan-out of :class:`CompositeMarketStream`.
#:
#: A composite has no timeout parameter of its own (it is a pure delegation
#: object), but its ``start``/``stop`` fan-outs must never hang the event loop on
#: a misbehaving child, so they are bounded by this explicit constant.
_FAN_OUT_TIMEOUT_SECONDS = 10.0

#: Message raised when the optional ``exchange`` extra (ccxt/ccxt.pro) is missing.
_MISSING_CCXT_PRO = "ccxt.pro is not installed: pip install -e '.[exchange]'"


# ---------------------------------------------------------------------------
# private helpers
# ---------------------------------------------------------------------------


def _positive_int(value: Any, *, name: str) -> int:
    """Return ``value`` as a strictly positive ``int``.

    Raises
    ------
    ValueError
        If ``value`` is not an integer-like number, or is not strictly positive.
    """
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc
    if number < 1:
        raise ValueError(f"{name} must be >= 1, got {number}")
    return number


def _positive_float(value: Any, *, name: str, allow_zero: bool = False) -> float:
    """Return ``value`` as a ``float``, strictly positive unless ``allow_zero``.

    Raises
    ------
    ValueError
        If ``value`` is not a number, is negative, or is zero when ``allow_zero``
        is ``False``.
    """
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc
    if number < 0 or (number == 0 and not allow_zero):
        limit = ">= 0" if allow_zero else "> 0"
        raise ValueError(f"{name} must be {limit}, got {number}")
    return number


def _failure_message(cause: BaseException | None) -> str:
    """Return a non-empty human message for a stream failure.

    An exception raised without arguments (``RuntimeError()``) stringifies to the
    empty string: the health surface must never carry an empty "last error", so a
    placeholder takes its place.
    """
    text = str(cause).strip() if cause is not None else ""
    return text or "unknown market-data failure"


def _empty_frame() -> pd.DataFrame:
    """Return a well-formed, empty OHLCV frame (UTC index, ``float64`` columns).

    The frame satisfies :func:`~trading_backtest.data.validation.ensure_ohlcv`,
    so a caller can concatenate it or validate it again without a special case.
    """
    index = pd.DatetimeIndex([], tz=UTC, name=OHLCV_INDEX_NAME)
    return pd.DataFrame(index=index, columns=list(REQUIRED_OHLCV_COLUMNS), dtype="float64")


def _candle_from_row(
    row: pd.Series,
    *,
    symbol: str,
    timeframe: str,
    timestamp: Any,
    closed: bool = True,
) -> CandleEvent:
    """Build a :class:`CandleEvent` from one OHLCV row."""
    return CandleEvent(
        symbol=str(symbol),
        timeframe=str(timeframe),
        timestamp=pd.Timestamp(timestamp),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        closed=closed,
    )


def _rows_to_frame(rows: Any, *, name: str) -> pd.DataFrame:
    """Map a ccxt-style ``[[ts_ms, o, h, l, c, v], ...]`` payload to an OHLCV frame.

    Raises
    ------
    MarketStreamError
        If the payload is ``None`` or carries no candle at all.
    """
    if rows is None:
        raise MarketStreamError(f"the venue returned no candle for {name}")
    records = [list(row)[:6] for row in rows]
    if not records:
        raise MarketStreamError(f"the venue returned an empty candle list for {name}")
    frame = pd.DataFrame(records, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame = frame.set_index("timestamp")
    return ensure_ohlcv(frame, name=name)


# ---------------------------------------------------------------------------
# the seam
# ---------------------------------------------------------------------------


@runtime_checkable
class MarketStream(Protocol):
    """The only way candles enter the realtime engine.

    Implementations expose exactly these members, so a local fake in a test --
    or another layer's own stream -- only has to implement this list.
    """

    async def start(self) -> None:
        """Acquire whatever the stream needs (connection, exchange handle)."""
        ...  # pragma: no cover - protocol definition

    async def stop(self) -> None:
        """Release everything acquired by :meth:`start` (never raises)."""
        ...  # pragma: no cover - protocol definition

    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
        """Return the next closed candle strictly newer than the last emitted one.

        ``None`` means "nothing new within the poll interval"; the caller decides
        when to ask again.
        """
        ...  # pragma: no cover - protocol definition

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Return at most ``count`` recent candles, for the strategy warm-up.

        Never raises: an unknown key or an unavailable window yields an empty
        OHLCV frame.
        """
        ...  # pragma: no cover - protocol definition

    @property
    def connected(self) -> bool:
        """Whether the stream is currently able to deliver candles."""
        ...  # pragma: no cover - protocol definition

    @property
    def last_error(self) -> str | None:
        """The last failure message, or ``None`` when the stream is healthy."""
        ...  # pragma: no cover - protocol definition

    @property
    def reconnect_count(self) -> int:
        """Monotonic number of failed delivery attempts (never decreases)."""
        ...  # pragma: no cover - protocol definition


# ---------------------------------------------------------------------------
# deterministic replay
# ---------------------------------------------------------------------------


class ReplayMarketStream:
    """Deterministic, offline, time-free stream over prepared candle frames.

    The stream owns a cursor per ``(symbol, timeframe)`` key.  ``next_candle``
    emits the frame rows in order, so two runs of the same frames produce the
    exact same :class:`CandleEvent` sequence.  It never reads a clock and never
    sleeps: the runner owns the real-time pace, the replay owns the data.

    Parameters
    ----------
    frames:
        Mapping of ``(symbol, timeframe)`` to an OHLCV frame.  Every frame is
        copied and validated by :func:`~trading_backtest.data.validation.ensure_ohlcv`.
    loop:
        When ``True``, the cursor wraps back to index ``0`` once the frame is
        exhausted instead of yielding ``None``.

    Raises
    ------
    MarketStreamError
        If no frame is given, if a frame is not valid OHLCV, or if a frame holds
        fewer than two candles.
    """

    def __init__(
        self,
        frames: Mapping[tuple[str, str], pd.DataFrame],
        *,
        loop: bool = False,
    ) -> None:
        if not frames:
            raise MarketStreamError(
                "a replay market stream needs at least one (symbol, timeframe) frame"
            )
        prepared: dict[tuple[str, str], pd.DataFrame] = {}
        for key, frame in frames.items():
            symbol, timeframe = key
            try:
                validated = ensure_ohlcv(frame, name=f"replay {symbol} {timeframe}")
            except DataValidationError as exc:
                raise MarketStreamError(
                    f"replay frame for {symbol} {timeframe} is not valid OHLCV: {exc}"
                ) from exc
            if len(validated) < 2:
                raise MarketStreamError(
                    f"replay frame for {symbol} {timeframe} needs at least 2 candles"
                )
            prepared[(str(symbol), str(timeframe))] = validated
        self._frames = prepared
        self._loop = bool(loop)
        self._cursors: dict[tuple[str, str], int] = dict.fromkeys(prepared, 0)
        self._started = False
        self._connected = False
        self._last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Rewind every cursor to ``0`` and mark the stream as connected."""
        self._cursors = dict.fromkeys(self._frames, 0)
        self._started = True
        self._connected = True
        self._last_error = None

    async def stop(self) -> None:
        """Mark the stream as disconnected (the cursors are kept)."""
        self._started = False
        self._connected = False

    # -- data --------------------------------------------------------------

    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
        """Return the next row of the frame as a closed :class:`CandleEvent`.

        Raises
        ------
        MarketStreamError
            If the stream was not started, or if the key has no registered frame.
        """
        if not self._started:
            raise MarketStreamError("the replay stream is not started: call start() first")
        key = (str(symbol), str(timeframe))
        frame = self._frames.get(key)
        if frame is None:
            raise MarketStreamError(f"no replay frame registered for {symbol} {timeframe}")
        cursor = self._cursors[key]
        if cursor >= len(frame):
            if not self._loop:
                return None
            cursor = 0
        self._cursors[key] = cursor + 1
        return _candle_from_row(
            frame.iloc[cursor],
            symbol=key[0],
            timeframe=key[1],
            timestamp=frame.index[cursor],
        )

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Return at most ``count`` candles ending at the last emitted one.

        The window is built from the candles the stream has **already** emitted,
        so a caller can never read a candle it has not been handed yet through
        :meth:`next_candle` -- no look-ahead.  Before the first emission (and for
        an unknown key) the result is an empty OHLCV frame.
        """
        key = (str(symbol), str(timeframe))
        frame = self._frames.get(key)
        if frame is None or count <= 0:
            return _empty_frame()
        cursor = self._cursors.get(key, 0)
        if cursor <= 0:
            return _empty_frame()
        start = max(0, cursor - int(count))
        return frame.iloc[start:cursor].copy()

    # -- deterministic control --------------------------------------------

    def seek(self, index: int) -> None:
        """Move every cursor to ``index`` (the next candle to emit).

        Used by restart tests and by a runner that has to resume mid-frame.

        Raises
        ------
        MarketStreamError
            If ``index`` is negative or beyond the end of any registered frame.
        """
        position = int(index)
        for key, frame in self._frames.items():
            if position < 0 or position > len(frame):
                raise MarketStreamError(
                    f"cannot seek the replay stream of {key[0]} {key[1]} to {position}: "
                    f"the frame holds {len(frame)} candles"
                )
        self._cursors = dict.fromkeys(self._frames, position)

    # -- health ------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the replay is started."""
        return self._connected

    @property
    def last_error(self) -> str | None:
        """Always ``None``: a replay of prepared frames cannot fail."""
        return self._last_error

    @property
    def reconnect_count(self) -> int:
        """Always ``0``: a replay never reconnects."""
        return 0


# ---------------------------------------------------------------------------
# polling provider
# ---------------------------------------------------------------------------


class PollingMarketStream:
    """Realtime stream polling a :class:`MarketDataProvider` through a :class:`Clock`.

    The stream reuses the backtesting data layer unchanged: the injected
    provider (:class:`~trading_backtest.data.loader.CsvDataProvider`,
    ``CcxtDataProvider`` or any structural equivalent) answers a
    ``[since, until]`` window, and every frame is validated by
    :func:`~trading_backtest.data.validation.ensure_ohlcv` before a candle is
    built.  Only closed candles are emitted, which is what makes a live profile
    behave exactly like the backtest engine: the signal is evaluated at the close
    of candle ``t`` and filled at the open of ``t + 1``.

    Parameters
    ----------
    provider:
        The synchronous market-data provider.
    clock:
        The time seam; all windows and every wait go through it.
    exchange:
        Venue name, carried for observability and for the providers that need it.
    history_candles:
        Size of the polling window, in candles.
    poll_interval_seconds:
        Cadence of the stream: after a poll that brought nothing new it waits one
        poll interval before returning ``None``, so a runner looping on
        ``next_candle`` is paced instead of busy-waiting on the provider.
    timeout_seconds:
        Explicit bound of every wait this class owns.
    max_reconnects:
        Number of consecutive failed polls tolerated before giving up with
        :class:`MarketStreamError`.
    reconnect_backoff_seconds:
        Base of the bounded exponential backoff between two failed polls.

    Raises
    ------
    ValueError
        If a numeric argument is invalid.
    """

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        clock: Clock,
        exchange: str = "binance",
        history_candles: int = 300,
        poll_interval_seconds: float = 5.0,
        timeout_seconds: float = 10.0,
        max_reconnects: int = 5,
        reconnect_backoff_seconds: float = 1.0,
    ) -> None:
        if not isinstance(exchange, str) or not exchange.strip():
            raise ValueError(f"exchange must be a non-empty string, got {exchange!r}")
        self._provider = provider
        self._clock = clock
        self._exchange = exchange.strip()
        self._history_candles = _positive_int(history_candles, name="history_candles")
        self._poll_interval_seconds = _positive_float(
            poll_interval_seconds, name="poll_interval_seconds"
        )
        self._timeout_seconds = _positive_float(timeout_seconds, name="timeout_seconds")
        self._max_reconnects = _positive_int(max_reconnects, name="max_reconnects")
        self._reconnect_backoff_seconds = _positive_float(
            reconnect_backoff_seconds, name="reconnect_backoff_seconds", allow_zero=True
        )
        self._started = False
        self._connected = False
        self._last_error: str | None = None
        self._reconnect_count = 0
        self._consecutive_failures = 0
        self._last_emitted: dict[tuple[str, str], pd.Timestamp] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Mark the stream as running.

        No network call happens here: ``connected`` only turns ``False`` once a
        poll actually fails.
        """
        self._started = True
        self._connected = True
        self._last_error = None

    async def stop(self) -> None:
        """Mark the stream as stopped."""
        self._started = False
        self._connected = False

    # -- data --------------------------------------------------------------

    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
        """Poll the provider and return the newest closed candle, or ``None``.

        Only candles satisfying ``timestamp <= now - candle_delta(timeframe)`` are
        candidates; the **newest** candidate strictly newer than the last emitted
        one wins.  Candles whose close was already known when the engine started
        (or that closed while the process was down) are deliberately **skipped**,
        not replayed: this stream is the live seam, and a decision taken on a
        stale candle would be filled at today's price.  Skipped candles are
        reported through ``realtime.market_data.candles_skipped``; a deterministic
        replay of a past window is what :class:`ReplayMarketStream` is for.

        Choosing the newest candidate is also what keeps this stream consistent
        with :meth:`history`, which returns the window ending *now*: the runner
        builds its warm-up frame as ``history[index < stamp]`` plus the candle
        ``stamp``, which is only a complete window when ``stamp`` is the latest
        closed candle.

        When the provider fails (or answers an empty/invalid frame) the stream
        records the error, backs off for a bounded delay and retries, up to
        ``max_reconnects`` consecutive attempts.

        Raises
        ------
        MarketStreamError
            If the stream was not started, or if the retry budget is exhausted.
        """
        if not self._started:
            raise MarketStreamError("the polling stream is not started: call start() first")
        key = (str(symbol), str(timeframe))
        delta = candle_delta(timeframe)
        until = pd.Timestamp(self._clock.now())
        since = until - self._history_candles * delta
        attempt = 0
        while True:
            attempt += 1
            frame: pd.DataFrame | None = None
            cause: BaseException | None = None
            try:
                # The provider call is synchronous by contract: it is bounded by
                # the provider's own HTTP timeout, not by asyncio.wait_for.
                raw = self._provider.fetch_ohlcv(symbol, timeframe, since, until)
                frame = ensure_ohlcv(raw, name=f"{symbol} {timeframe}")
            except Exception as exc:  # any provider failure is a stream failure
                cause = exc
            if cause is None and (frame is None or frame.empty):
                cause = MarketStreamError(
                    f"the provider returned no candle for {symbol} {timeframe}"
                )
            if cause is None and frame is not None:
                self._consecutive_failures = 0
                self._connected = True
                self._last_error = None
                closed = frame.loc[frame.index <= until - delta]
                watermark = self._last_emitted.get(key)
                if watermark is not None:
                    closed = closed.loc[closed.index > watermark]
                if closed.empty:
                    await self._idle()
                    return None
                timestamp = closed.index[-1]
                self._last_emitted[key] = pd.Timestamp(timestamp)
                if len(closed) > 1:
                    # Only surface the gap once per emission: a fresh engine and a
                    # restart after a downtime both land here, and the operator has
                    # to know that the candles in between were not traded.
                    LOGGER.warning(
                        "realtime.market_data.candles_skipped",
                        extra={
                            "event": "market_data.candles_skipped",
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "skipped": len(closed) - 1,
                            "skipped_from": closed.index[0].isoformat(),
                            "skipped_to": closed.index[-2].isoformat(),
                            "emitting": pd.Timestamp(timestamp).isoformat(),
                        },
                    )
                return _candle_from_row(
                    closed.iloc[-1],
                    symbol=key[0],
                    timeframe=key[1],
                    timestamp=timestamp,
                )
            message = _failure_message(cause)
            self._record_failure(message)
            if attempt >= self._max_reconnects:
                raise MarketStreamError(
                    f"polling stream for {symbol} {timeframe} gave up after "
                    f"{attempt} attempts: {message}"
                ) from cause
            await self._backoff(attempt)

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Return the provider window of ``count`` candles ending now.

        The window is the raw provider answer (closed *and* still-forming
        candles); filtering to closed candles is :meth:`next_candle`'s job.  A
        failure, an unknown symbol or an invalid frame yields an empty OHLCV
        frame -- this method never raises and never changes the health state.

        See Also
        --------
        MarketStream.history
        """
        if count <= 0:
            return _empty_frame()
        try:
            delta = candle_delta(timeframe)
            until = pd.Timestamp(self._clock.now())
            since = until - int(count) * delta
            raw = self._provider.fetch_ohlcv(symbol, timeframe, since, until)
            return ensure_ohlcv(raw, name=f"{symbol} {timeframe}")
        except Exception as exc:  # history is a best-effort read model
            LOGGER.warning(
                "realtime.market_data.history_failed",
                extra={
                    "event": "market_data.history_failed",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "error": str(exc),
                },
            )
            return _empty_frame()

    # -- internals ---------------------------------------------------------

    async def _idle(self) -> None:
        """Wait one poll interval (bounded) before reporting "nothing new"."""
        await asyncio.wait_for(
            self._clock.sleep(self._poll_interval_seconds), timeout=self._timeout_seconds
        )

    async def _backoff(self, attempt: int) -> None:
        """Sleep the bounded exponential backoff of failed attempt ``attempt``."""
        delay = self._reconnect_backoff_seconds * 2 ** (attempt - 1)
        await asyncio.wait_for(self._clock.sleep(delay), timeout=self._timeout_seconds)

    def _record_failure(self, message: str) -> None:
        """Record a failed poll: health, counter and one structured warning."""
        self._last_error = message
        self._connected = False
        self._consecutive_failures += 1
        self._reconnect_count += 1
        LOGGER.warning(
            "realtime.market_data.poll_failed",
            extra={
                "event": "market_data.poll_failed",
                "exchange": self._exchange,
                "error": message,
                "consecutive_failures": self._consecutive_failures,
                "reconnects": self._reconnect_count,
            },
        )

    # -- health ------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the last poll succeeded (``True`` right after :meth:`start`)."""
        return self._connected

    @property
    def last_error(self) -> str | None:
        """The message of the last failed poll, or ``None``."""
        return self._last_error

    @property
    def reconnect_count(self) -> int:
        """Monotonic number of failed polls since the stream was created."""
        return self._reconnect_count


# ---------------------------------------------------------------------------
# live ccxt.pro stream (optional extra, imported lazily)
# ---------------------------------------------------------------------------


class CcxtProMarketStream:
    """Live stream over ``ccxt.pro``'s ``watch_ohlcv``, imported lazily.

    ``ccxt.pro`` is an optional extra and is imported **inside** :meth:`start`,
    never at module import time, so the layer keeps working with the dev extra
    only.  Credentials are not handled here: this class only reads public market
    data (authenticated trading belongs to the broker adapter).

    Parameters
    ----------
    clock:
        The time seam; the closed-candle rule and every wait go through it.
    exchange:
        ``ccxt.pro`` exchange id (``binance``, ``kraken``, ...).
    timeout_seconds:
        Explicit bound of every wait this class owns.
    history_candles:
        Default depth of :meth:`history`.
    max_reconnects:
        Number of consecutive failed reads tolerated before giving up.
    reconnect_backoff_seconds:
        Base of the bounded exponential backoff between two failed reads.

    Raises
    ------
    ValueError
        If a numeric argument is invalid.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        exchange: str = "binance",
        timeout_seconds: float = 10.0,
        history_candles: int = 300,
        max_reconnects: int = 5,
        reconnect_backoff_seconds: float = 1.0,
    ) -> None:
        if not isinstance(exchange, str) or not exchange.strip():
            raise ValueError(f"exchange must be a non-empty string, got {exchange!r}")
        self._clock = clock
        self._exchange_name = exchange.strip()
        self._timeout_seconds = _positive_float(timeout_seconds, name="timeout_seconds")
        self._history_candles = _positive_int(history_candles, name="history_candles")
        self._max_reconnects = _positive_int(max_reconnects, name="max_reconnects")
        self._reconnect_backoff_seconds = _positive_float(
            reconnect_backoff_seconds, name="reconnect_backoff_seconds", allow_zero=True
        )
        self._exchange: Any | None = None
        self._connected = False
        self._last_error: str | None = None
        self._reconnect_count = 0
        self._consecutive_failures = 0
        self._last_emitted: dict[tuple[str, str], pd.Timestamp] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Import ``ccxt.pro`` (lazily) and build the exchange handle.

        Raises
        ------
        MarketStreamError
            If the ``exchange`` extra is missing or does not expose the venue.
        """
        try:
            import ccxt.pro as ccxtpro
        except ImportError:
            raise MarketStreamError(_MISSING_CCXT_PRO) from None
        factory = getattr(ccxtpro, self._exchange_name, None)
        if factory is None:
            raise MarketStreamError(
                f"ccxt.pro does not provide the exchange {self._exchange_name!r}"
            )
        self._exchange = factory({"enableRateLimit": True})
        self._connected = True
        self._last_error = None

    async def stop(self) -> None:
        """Close the exchange handle; never raises."""
        exchange = self._exchange
        self._exchange = None
        self._connected = False
        if exchange is None:
            return
        close = getattr(exchange, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=self._timeout_seconds)
        except Exception as exc:  # a failing close must not break the shutdown
            LOGGER.warning(
                "realtime.market_data.close_failed",
                extra={"event": "market_data.close_failed", "error": str(exc)},
            )

    # -- data --------------------------------------------------------------

    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
        """Read the venue stream and return the next closed candle, or ``None``.

        Raises
        ------
        MarketStreamError
            If the stream was not started, or if the retry budget is exhausted
            (the underlying timeout/venue error is chained as ``__cause__``).
        """
        if self._exchange is None:
            raise MarketStreamError("the ccxt.pro stream is not started: call start() first")
        key = (str(symbol), str(timeframe))
        delta = candle_delta(timeframe)
        attempt = 0
        while True:
            attempt += 1
            frame: pd.DataFrame | None = None
            cause: BaseException | None = None
            try:
                rows = await asyncio.wait_for(
                    self._exchange.watch_ohlcv(symbol, timeframe),
                    timeout=self._timeout_seconds,
                )
                frame = _rows_to_frame(rows, name=f"{symbol} {timeframe}")
            except Exception as exc:  # venue error, bounded timeout or invalid payload
                cause = exc
            if cause is None and frame is not None:
                self._consecutive_failures = 0
                self._connected = True
                self._last_error = None
                closed = frame.loc[frame.index <= pd.Timestamp(self._clock.now()) - delta]
                watermark = self._last_emitted.get(key)
                if watermark is not None:
                    closed = closed.loc[closed.index > watermark]
                if closed.empty:
                    return None
                # Same rule as the polling stream: a live seam emits the NEWEST
                # closed candle, so ``history`` (a window ending now) is a complete
                # warm-up for it and the engine never trades a stale signal.
                timestamp = closed.index[-1]
                self._last_emitted[key] = pd.Timestamp(timestamp)
                return _candle_from_row(
                    closed.iloc[-1],
                    symbol=key[0],
                    timeframe=key[1],
                    timestamp=timestamp,
                )
            message = _failure_message(cause)
            self._record_failure(message)
            if attempt >= self._max_reconnects:
                raise MarketStreamError(
                    f"ccxt.pro stream for {symbol} {timeframe} gave up after "
                    f"{attempt} attempts: {message}"
                ) from cause
            await self._backoff(attempt)

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Fetch up to ``count`` recent candles; empty frame on failure.

        Never raises: a venue failure, an unknown symbol or an invalid payload
        yields an empty OHLCV frame.
        """
        if count <= 0:
            return _empty_frame()
        exchange = self._exchange
        if exchange is None:
            return _empty_frame()
        try:
            rows = await asyncio.wait_for(
                exchange.fetch_ohlcv(symbol, timeframe, None, int(count)),
                timeout=self._timeout_seconds,
            )
            return _rows_to_frame(rows, name=f"{symbol} {timeframe}")
        except Exception as exc:  # history is a best-effort read model
            LOGGER.warning(
                "realtime.market_data.history_failed",
                extra={
                    "event": "market_data.history_failed",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "error": str(exc),
                },
            )
            return _empty_frame()

    # -- internals ---------------------------------------------------------

    async def _backoff(self, attempt: int) -> None:
        """Sleep the bounded exponential backoff of failed attempt ``attempt``."""
        delay = self._reconnect_backoff_seconds * 2 ** (attempt - 1)
        await asyncio.wait_for(self._clock.sleep(delay), timeout=self._timeout_seconds)

    def _record_failure(self, message: str) -> None:
        """Record a failed read: health, counter and one structured warning."""
        self._last_error = message
        self._connected = False
        self._consecutive_failures += 1
        self._reconnect_count += 1
        LOGGER.warning(
            "realtime.market_data.read_failed",
            extra={
                "event": "market_data.read_failed",
                "exchange": self._exchange_name,
                "error": message,
                "consecutive_failures": self._consecutive_failures,
                "reconnects": self._reconnect_count,
            },
        )

    # -- health ------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the last read succeeded."""
        return self._connected

    @property
    def last_error(self) -> str | None:
        """The message of the last failed read, or ``None``."""
        return self._last_error

    @property
    def reconnect_count(self) -> int:
        """Monotonic number of failed reads since the stream was created."""
        return self._reconnect_count


# ---------------------------------------------------------------------------
# multiplexer
# ---------------------------------------------------------------------------


class CompositeMarketStream:
    """Multiplex several ``(symbol, timeframe)`` streams behind one seam.

    Typical use: one profile follows ``BTC/USDT`` while another follows
    ``ETH/USDT``, and both are served by a single injected object.  A failing
    child never takes the composite down: its error is recorded and reported
    through :attr:`last_error`, the other children keep running.

    Parameters
    ----------
    streams:
        Mapping of ``(symbol, timeframe)`` to a child :class:`MarketStream`.
    clock:
        Optional time seam, accepted for wiring symmetry with the other streams;
        delegation itself needs no time.

    Raises
    ------
    ValueError
        If ``streams`` is empty.
    """

    def __init__(
        self,
        streams: Mapping[tuple[str, str], MarketStream],
        *,
        clock: Clock | None = None,
    ) -> None:
        if not streams:
            raise ValueError("a composite market stream needs at least one child stream")
        self._streams: dict[tuple[str, str], MarketStream] = {
            (str(symbol), str(timeframe)): stream for (symbol, timeframe), stream in streams.items()
        }
        self._clock = clock
        self._errors: dict[tuple[str, str], str] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start every child; a failing child only records its error."""
        await self._fan_out("start")

    async def stop(self) -> None:
        """Stop every child, ignoring every failure."""
        await self._fan_out("stop", record=False)

    # -- data --------------------------------------------------------------

    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
        """Delegate to the child registered for ``(symbol, timeframe)``.

        Raises
        ------
        MarketStreamError
            If no child is registered for the key.
        """
        return await self._child(symbol, timeframe).next_candle(symbol, timeframe)

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Delegate to the child; an unknown key yields an empty frame."""
        child = self._streams.get((str(symbol), str(timeframe)))
        if child is None:
            return _empty_frame()
        return await child.history(symbol, timeframe, count)

    def keys(self) -> list[tuple[str, str]]:
        """Return the registered keys, sorted, for a deterministic iteration."""
        return sorted(self._streams)

    # -- internals ---------------------------------------------------------

    def _child(self, symbol: str, timeframe: str) -> MarketStream:
        """Return the child of a key.

        Raises
        ------
        MarketStreamError
            If no child is registered for the key.
        """
        child = self._streams.get((str(symbol), str(timeframe)))
        if child is None:
            raise MarketStreamError(f"no stream registered for {symbol} {timeframe}")
        return child

    async def _fan_out(self, method: str, *, record: bool = True) -> None:
        """Call ``method`` on every child concurrently, bounded by a timeout.

        The fan-out is bounded by :data:`_FAN_OUT_TIMEOUT_SECONDS`, so a hung
        child can never hang the composite (and therefore never the event loop).
        """
        keys = self.keys()
        coroutines = [getattr(self._streams[key], method)() for key in keys]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*coroutines, return_exceptions=True),
                timeout=_FAN_OUT_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            if record:
                for key in keys:
                    self._errors.setdefault(key, f"{method} timed out: {exc}")
            LOGGER.warning(
                "realtime.market_data.composite_timeout",
                extra={
                    "event": "market_data.composite_timeout",
                    "method": method,
                    "error": str(exc),
                },
            )
            return
        for key, result in zip(keys, results, strict=True):
            if isinstance(result, BaseException):
                if record:
                    self._errors[key] = str(result)
                    LOGGER.warning(
                        "realtime.market_data.composite_child_failed",
                        extra={
                            "event": "market_data.composite_child_failed",
                            "method": method,
                            "symbol": key[0],
                            "timeframe": key[1],
                            "error": str(result),
                        },
                    )
                continue
            self._errors.pop(key, None)

    # -- health ------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """``True`` as soon as one child is connected."""
        return any(self._streams[key].connected for key in self.keys())

    @property
    def last_error(self) -> str | None:
        """First non-``None`` child error, in sorted key order."""
        for key in self.keys():
            error = self._errors.get(key) or self._streams[key].last_error
            if error is not None:
                return error
        return None

    @property
    def reconnect_count(self) -> int:
        """Sum of the children's reconnect counters."""
        return sum(self._streams[key].reconnect_count for key in self.keys())
