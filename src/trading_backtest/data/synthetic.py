"""Deterministic, fully offline OHLCV generators.

Every layer of the test-suite (and the documentation examples) builds its data
here: the generators only need ``numpy`` and ``pandas``, never the network, and
two calls with identical arguments return identical frames.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading_backtest.core.constants import (
    DEFAULT_TIMEFRAME,
    OHLCV_INDEX_NAME,
    UTC,
    candle_delta,
)
from trading_backtest.core.errors import ConfigError
from trading_backtest.data.validation import ensure_ohlcv

__all__ = [
    "make_flat_ohlcv",
    "make_ohlcv",
    "make_trending_ohlcv",
]

_OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


def _make_index(n: int, start: str | pd.Timestamp, timeframe: str) -> pd.DatetimeIndex:
    """Build the (tz-aware UTC) index of ``n`` candles of ``timeframe``."""
    if n < 0:
        raise ConfigError(f"n must be >= 0, got {n}")
    timestamp = pd.Timestamp(start)
    timestamp = timestamp.tz_localize(UTC) if timestamp.tz is None else timestamp.tz_convert(UTC)
    return pd.date_range(
        start=timestamp,
        periods=n,
        freq=candle_delta(timeframe),
        tz=UTC,
        name=OHLCV_INDEX_NAME,
    )


def _frame(
    index: pd.DatetimeIndex,
    *,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    name: str,
) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "open": np.asarray(opens, dtype="float64"),
            "high": np.asarray(highs, dtype="float64"),
            "low": np.asarray(lows, dtype="float64"),
            "close": np.asarray(closes, dtype="float64"),
            "volume": np.asarray(volumes, dtype="float64"),
        },
        index=index,
        columns=list(_OHLCV_COLUMNS),
    )
    return ensure_ohlcv(frame, name=name)


def make_ohlcv(
    n: int = 500,
    *,
    start: str | pd.Timestamp = "2023-01-01T00:00:00Z",
    timeframe: str = DEFAULT_TIMEFRAME,
    seed: int = 42,
    initial_price: float = 100.0,
    drift: float = 0.0,
    volatility: float = 0.01,
) -> pd.DataFrame:
    """Generate a deterministic random-walk OHLCV frame of ``n`` candles.

    Returns
    -------
    pandas.DataFrame
        ``float64`` OHLCV columns indexed by an ascending tz-aware UTC
        ``DatetimeIndex`` named ``"timestamp"``.  ``open`` is the previous
        ``close`` (the first candle opens at ``initial_price``), ``high`` is at
        least ``max(open, close)`` and ``low`` at most ``min(open, close)``.
        All prices are strictly positive and the volume is strictly positive.

    Notes
    -----
    ``n=0`` yields an empty (but correctly typed) frame and ``n=1`` a single
    candle.  A fresh frame is allocated on every call.
    """
    index = _make_index(n, start, timeframe)
    rng = np.random.default_rng(seed)
    if n == 0:
        empty = np.empty(0, dtype="float64")
        return _frame(
            index, opens=empty, highs=empty, lows=empty, closes=empty, volumes=empty, name="ohlcv"
        )

    returns = rng.normal(loc=drift, scale=volatility, size=n)
    closes = initial_price * np.exp(np.cumsum(returns))
    opens = np.empty(n, dtype="float64")
    opens[0] = initial_price
    opens[1:] = closes[:-1]
    spread = np.clip(np.abs(rng.normal(loc=0.0, scale=max(volatility, 1e-6), size=n)), 0.0, 0.5)
    highs = np.maximum(opens, closes) * (1.0 + spread)
    lows = np.minimum(opens, closes) * (1.0 - spread)
    volumes = rng.uniform(1.0, 1000.0, size=n)
    return _frame(
        index, opens=opens, highs=highs, lows=lows, closes=closes, volumes=volumes, name="ohlcv"
    )


def make_trending_ohlcv(
    n: int = 600,
    *,
    timeframe: str = DEFAULT_TIMEFRAME,
    start: str | pd.Timestamp = "2023-01-01T00:00:00Z",
    period: int = 100,
    amplitude: float = 8.0,
    slope: float = 0.02,
    base_price: float = 100.0,
    seed: int = 7,
) -> pd.DataFrame:
    """Generate a deterministic oscillating, mildly trending OHLCV frame.

    The close price is ``base_price + slope * t + amplitude * sin(2*pi*t/period)``
    plus a tiny seeded noise, which guarantees **at least two EMA(9)/EMA(21)
    crossovers** for the default parameters and any ``n >= 400`` — enough for an
    EMA-cross strategy to enter and exit several times.  Prices are always
    strictly positive.
    """
    index = _make_index(n, start, timeframe)
    rng = np.random.default_rng(seed)
    if n == 0:
        empty = np.empty(0, dtype="float64")
        return _frame(
            index,
            opens=empty,
            highs=empty,
            lows=empty,
            closes=empty,
            volumes=empty,
            name="trending ohlcv",
        )

    steps = np.arange(n, dtype="float64")
    wave = np.sin(2.0 * np.pi * steps / float(period))
    noise = rng.normal(loc=0.0, scale=max(abs(amplitude) * 0.01, 1e-6), size=n)
    closes = base_price + slope * steps + amplitude * wave + noise
    closes = np.maximum(closes, base_price * 0.01)
    opens = np.empty(n, dtype="float64")
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    spread = np.clip(np.abs(rng.normal(loc=0.0, scale=0.002, size=n)), 0.0, 0.5)
    highs = np.maximum(opens, closes) * (1.0 + spread)
    lows = np.minimum(opens, closes) * (1.0 - spread)
    volumes = rng.uniform(1.0, 1000.0, size=n)
    return _frame(
        index,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=volumes,
        name="trending ohlcv",
    )


def make_flat_ohlcv(
    n: int = 100,
    *,
    price: float = 100.0,
    timeframe: str = DEFAULT_TIMEFRAME,
    start: str | pd.Timestamp = "2023-01-01T00:00:00Z",
) -> pd.DataFrame:
    """Generate a perfectly flat OHLCV frame (zero range, zero volatility)."""
    index = _make_index(n, start, timeframe)
    prices = np.full(n, float(price), dtype="float64")
    volumes = np.full(n, 1.0, dtype="float64")
    return _frame(
        index,
        opens=prices,
        highs=prices.copy(),
        lows=prices.copy(),
        closes=prices.copy(),
        volumes=volumes,
        name="flat ohlcv",
    )
