"""Sweep candidates across pairs and timeframes, with an out-of-sample split.

Two things this driver refuses to do, because they are how strategy research
usually fools itself:

* it never reports a single in-sample number as if it were a result. Every
  candidate is scored on a **train** window and a held-back **test** window, and
  the test number is the one that counts;
* it always prints the **buy-and-hold** benchmark for the same window, so a
  positive return in a bull market is not mistaken for skill.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import BacktestResult, buy_and_hold, run_backtest  # noqa: E402
from candidates import CANDIDATES  # noqa: E402
from run import load  # noqa: E402

#: The pairs the platform trades.
UNIVERSE = [
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


@dataclass
class Score:
    """One candidate measured on one pair/timeframe/window."""

    candidate: str
    pair: str
    timeframe: str
    window: str
    result: BacktestResult
    benchmark: BacktestResult


def split(frame: pd.DataFrame, train_frac: float = 0.6) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological train/test split -- never shuffled.

    A time series split has to respect order: shuffling would let the model see the
    future. 60/40 keeps enough test data to matter while leaving a long train
    window for the slow indicators (many need 200+ candles).
    """
    cut = int(len(frame) * train_frac)
    return frame.iloc[:cut], frame.iloc[cut:]


def score(
    candidate: str,
    pair: str,
    timeframe: str,
    *,
    train_frac: float = 0.6,
) -> list[Score]:
    frame = load(pair, timeframe)
    if frame is None or len(frame) < 400:
        return []
    signal_fn, stoploss, take_profit = CANDIDATES[candidate]
    out: list[Score] = []
    train, test = split(frame, train_frac)
    for label, window in (("train", train), ("test", test)):
        signals = signal_fn(window)
        result = run_backtest(
            window,
            signals,
            name=candidate,
            pair=pair,
            timeframe=timeframe,
            stoploss=stoploss,
            take_profit=take_profit,
        )
        out.append(
            Score(
                candidate=candidate,
                pair=pair,
                timeframe=timeframe,
                window=label,
                result=result,
                benchmark=buy_and_hold(window, timeframe=timeframe, pair=pair),
            )
        )
    return out


def cmd_run(args: argparse.Namespace) -> int:
    candidates = args.candidates or list(CANDIDATES)
    timeframes = args.timeframes or ["1h", "4h", "1d"]
    pairs = args.pairs or UNIVERSE

    scores: list[Score] = []
    for candidate in candidates:
        for timeframe in timeframes:
            for pair in pairs:
                scores.extend(score(candidate, pair, timeframe))

    if not scores:
        print("no scores produced (missing data?)")
        return 1

    frame = pd.DataFrame(
        [
            {
                "candidate": s.candidate,
                "pair": s.pair,
                "tf": s.timeframe,
                "window": s.window,
                "ret": s.result.total_return * 100,
                "sharpe": s.result.sharpe,
                "maxdd": s.result.max_drawdown * 100,
                "trades": s.result.n_trades,
                "win": s.result.win_rate * 100,
                "pf": s.result.profit_factor,
                "expo": s.result.exposure * 100,
                "bh": s.benchmark.total_return * 100,
                "edge": (s.result.total_return - s.benchmark.total_return) * 100,
            }
            for s in scores
        ]
    )

    # The headline: mean test-window Sharpe per candidate, with the benchmark.
    test = frame[frame["window"] == "test"]
    summary = (
        test.groupby("candidate")
        .agg(
            mean_sharpe=("sharpe", "mean"),
            med_sharpe=("sharpe", "median"),
            mean_ret=("ret", "mean"),
            mean_edge=("edge", "mean"),
            mean_dd=("maxdd", "mean"),
            mean_trades=("trades", "mean"),
            beat_bh=("edge", lambda x: (x > 0).mean() * 100),
        )
        .sort_values("mean_sharpe", ascending=False)
    )
    print("\n=== OUT-OF-SAMPLE (test window) per candidate ===")
    print(
        f"{'candidate':20} {'sharpe':>7} {'medSh':>6} {'ret%':>7} {'edge%':>7} "
        f"{'maxdd%':>7} {'trades':>7} {'beatBH%':>8}"
    )
    for name, row in summary.iterrows():
        print(
            f"{name:20} {row['mean_sharpe']:>7.2f} {row['med_sharpe']:>6.2f} "
            f"{row['mean_ret']:>7.2f} {row['mean_edge']:>7.2f} {row['mean_dd']:>7.2f} "
            f"{row['mean_trades']:>7.0f} {row['beat_bh']:>8.1f}"
        )

    if args.detail:
        print("\n=== per pair/timeframe (test) ===")
        cols = ["candidate", "pair", "tf", "ret", "sharpe", "maxdd", "trades", "win", "bh", "edge"]
        detail = test[cols].sort_values(["candidate", "sharpe"], ascending=[True, False])
        print(detail.to_string(index=False, float_format=lambda x: f"{x:.2f}"))

    if args.csv:
        frame.to_csv(args.csv, index=False)
        print(f"\nwrote {args.csv}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Candidate sweep with train/test split.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--candidates", nargs="*", default=None)
    p.add_argument("--timeframes", nargs="*", default=None)
    p.add_argument("--pairs", nargs="*", default=None)
    p.add_argument("--detail", action="store_true")
    p.add_argument("--csv", default=None)
    p.set_defaults(func=cmd_run)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
