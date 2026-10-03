"""Research driver: reproduce a strategy's signals on cached history and score them.

Typical use from the command line::

    .venv/bin/python research/run.py originals --timeframe 1h

The module exposes :func:`load` (cached parquet), :func:`evaluate` (signals ->
backtest result) and a ``main`` with sub-commands for the studies run during this
work.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import BacktestResult, buy_and_hold, format_table, run_backtest  # noqa: E402
from strategies_original import ORIGINAL_RISK, ORIGINALS  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"

#: Each profile as it is actually deployed: strategy -> (pair, timeframe).
DEPLOYED = {
    "basic-btc-1h": ("basic", "BTC/USDT", "1h"),
    "basic-eth-4h": ("basic", "ETH/USDT", "4h"),
    "momentum-btc-1h": ("momentum", "BTC/USDT", "1h"),
    "momentum-sol-15m": ("momentum", "SOL/USDT", "15m"),
    "rsi-reversion-btc-15m": ("rsi-reversion", "BTC/USDT", "15m"),
    "rsi-reversion-ada-1h": ("rsi-reversion", "ADA/USDT", "1h"),
    "bollinger-eth-1h": ("bollinger", "ETH/USDT", "1h"),
    "bollinger-xrp-5m": ("bollinger", "XRP/USDT", "5m"),
    "macd-btc-4h": ("macd", "BTC/USDT", "4h"),
    "macd-link-1h": ("macd", "LINK/USDT", "1h"),
    "donchian-eth-4h": ("donchian", "ETH/USDT", "4h"),
    "donchian-doge-1h": ("donchian", "DOGE/USDT", "1h"),
    "keltner-sol-1h": ("keltner", "SOL/USDT", "1h"),
    "keltner-bnb-15m": ("keltner", "BNB/USDT", "15m"),
    "supertrend-btc-1h": ("supertrend", "BTC/USDT", "1h"),
    "supertrend-avax-4h": ("supertrend", "AVAX/USDT", "4h"),
    "dual-thrust-eth-15m": ("dual-thrust", "ETH/USDT", "15m"),
    "dual-thrust-dot-1h": ("dual-thrust", "DOT/USDT", "1h"),
    "faber-btc-1d": ("faber", "BTC/USDT", "1d"),
    "faber-eth-1d": ("faber", "ETH/USDT", "1d"),
}


def load(pair: str, timeframe: str) -> pd.DataFrame | None:
    """Return the cached OHLCV frame for ``pair``/``timeframe``, indexed by date."""
    stem = pair.replace("/", "")
    path = DATA_DIR / f"{stem}_{timeframe}.parquet"
    if not path.exists():
        return None
    frame = pd.read_parquet(path)
    frame = frame.set_index("date").sort_index()
    frame = frame[~frame.index.duplicated(keep="first")]
    return frame


def evaluate(
    frame: pd.DataFrame,
    signal_fn,
    *,
    name: str,
    pair: str,
    timeframe: str,
    stoploss: float | None = None,
    take_profit: float | None = None,
    max_bars: int | None = None,
) -> BacktestResult:
    """Run ``signal_fn`` over ``frame`` and backtest the result."""
    signals = signal_fn(frame)
    return run_backtest(
        frame,
        signals,
        name=name,
        pair=pair,
        timeframe=timeframe,
        stoploss=stoploss,
        take_profit=take_profit,
        max_bars=max_bars,
    )


def cmd_originals(args: argparse.Namespace) -> int:
    """Score every shipped strategy on history, and buy & hold alongside it."""
    rows: list[BacktestResult] = []
    for _profile_id, (strategy_id, pair, timeframe) in sorted(DEPLOYED.items()):
        if args.timeframe and timeframe != args.timeframe:
            continue
        frame = load(pair, timeframe)
        if frame is None:
            print(f"  (no data for {pair} {timeframe})")
            continue
        sliced = frame.iloc[-args.bars :] if args.bars else frame
        stoploss, take_profit = ORIGINAL_RISK[strategy_id]
        rows.append(
            evaluate(
                sliced,
                ORIGINALS[strategy_id],
                name=strategy_id,
                pair=pair,
                timeframe=timeframe,
                stoploss=stoploss,
                take_profit=take_profit,
            )
        )
        benchmark = buy_and_hold(sliced, timeframe=timeframe, pair=pair)
        benchmark.name = "^ buy&hold"
        rows.append(benchmark)
    print(format_table(rows))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Strategy research driver.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_orig = sub.add_parser("originals", help="score the shipped strategies")
    p_orig.add_argument("--timeframe", default=None)
    p_orig.add_argument("--bars", type=int, default=0, help="limit to the most recent N candles")
    p_orig.set_defaults(func=cmd_originals)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
