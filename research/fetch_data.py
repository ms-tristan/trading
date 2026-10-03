"""Download and cache OHLCV history for research and backtesting.

Data is cached as parquet under ``research/data/<PAIR>_<TIMEFRAME>.parquet`` so a
re-run of a study never re-hits the exchange. The pagination walks backwards from
``now`` until ``days`` of history are covered, honouring the exchange rate limit.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import ccxt
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"

#: Pairs the platform actually trades, plus the majors used as market context.
DEFAULT_PAIRS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
    "ADA/USDT",
    "DOGE/USDT",
    "DOT/USDT",
    "LINK/USDT",
    "AVAX/USDT",
]

#: Timeframes the shipped profiles use.
DEFAULT_TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]


def _ms_per_candle(timeframe: str) -> int:
    unit = timeframe[-1]
    value = int(timeframe[:-1])
    table = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    return value * table[unit]


def fetch_pair(
    exchange: ccxt.Exchange,
    pair: str,
    timeframe: str,
    *,
    days: int,
    batch: int = 1000,
) -> pd.DataFrame:
    """Return up to ``days`` of OHLCV for ``pair`` on ``timeframe``."""
    step = _ms_per_candle(timeframe)
    now = exchange.milliseconds()
    start = now - days * 86_400_000
    since = start
    rows: list[list[float]] = []

    while since < now:
        try:
            chunk = exchange.fetch_ohlcv(pair, timeframe, since=since, limit=batch)
        except Exception as exc:  # transient network / rate-limit
            print(f"    retry after {type(exc).__name__}: {str(exc)[:80]}", flush=True)
            time.sleep(3)
            continue
        if not chunk:
            break
        rows.extend(chunk)
        last = chunk[-1][0]
        if last <= since:
            break
        since = last + step
        if len(chunk) < batch:
            break

    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = frame.drop_duplicates(subset="timestamp").sort_values("timestamp")
    frame = frame[frame["timestamp"] >= start].reset_index(drop=True)
    frame["date"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    return frame[["date", "open", "high", "low", "close", "volume"]]


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch OHLCV history for research.")
    parser.add_argument("--days", type=int, default=730, help="days of history to fetch")
    parser.add_argument("--pairs", nargs="*", default=DEFAULT_PAIRS)
    parser.add_argument("--timeframes", nargs="*", default=DEFAULT_TIMEFRAMES)
    parser.add_argument("--force", action="store_true", help="refetch an existing cache")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    exchange = ccxt.binance({"enableRateLimit": True})

    for timeframe in args.timeframes:
        for pair in args.pairs:
            stem = pair.replace("/", "")
            target = DATA_DIR / f"{stem}_{timeframe}.parquet"
            if target.exists() and not args.force:
                print(f"skip  {target.name} (cached)")
                continue
            print(f"fetch {pair} {timeframe} ({args.days}d) ...", flush=True)
            frame = fetch_pair(exchange, pair, timeframe, days=args.days)
            if frame.empty:
                print(f"  !! no data for {pair} {timeframe}")
                continue
            frame.to_parquet(target, index=False)
            span = f"{frame['date'].iloc[0]:%Y-%m-%d} -> {frame['date'].iloc[-1]:%Y-%m-%d}"
            print(f"  ok  {len(frame):>7} candles  {span}  -> {target.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
