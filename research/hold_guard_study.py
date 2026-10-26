"""Driver for the "hold by default, step aside for the crash" study.

Runs every candidate in :mod:`hold_plus_guard` across the full pair universe and
three timeframes, with the same honest protocol the rest of ``research/`` uses:

* a chronological 60/40 train/test split, and the **test** window is what counts;
* buy-and-hold measured on the identical window, so a positive return in a rising
  market is never mistaken for skill;
* the metrics the trend literature actually recommends for a slow filter --
  Calmar and max drawdown -- reported next to Sharpe, because Sharpe punishes the
  upside volatility a trend rule deliberately accepts;
* the **crash-window** behaviour reported separately: the whole point of a crash
  guard is what it does during the crash, and an average over two years hides it.

Usage::

    .venv/bin/python research/hold_guard_study.py run
    .venv/bin/python research/hold_guard_study.py run --detail
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import BacktestResult, buy_and_hold, run_backtest  # noqa: E402
from hold_plus_guard import GUARDS, load_btc_daily, sig_btc_regime_gate  # noqa: E402
from run import load  # noqa: E402

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

TIMEFRAMES = ["4h", "1d"]


def _score_one(
    frame: pd.DataFrame,
    *,
    name: str,
    signal_fn,
    pair: str,
    timeframe: str,
    stoploss: float | None,
    take_profit: float | None,
    btc_daily: pd.DataFrame | None = None,
) -> BacktestResult:
    """Run one candidate on one window, injecting BTC when the rule needs it."""
    if signal_fn is sig_btc_regime_gate:
        signals = signal_fn(frame, btc=btc_daily)
    else:
        signals = signal_fn(frame)
    return run_backtest(
        frame,
        signals,
        name=name,
        pair=pair,
        timeframe=timeframe,
        stoploss=stoploss,
        take_profit=take_profit,
    )


def _catalogue() -> dict[str, tuple[object, float | None, float | None]]:
    out = dict(GUARDS)
    # The BTC-regime variant needs the daily BTC frame; same risk envelope.
    out["hold-guard-btc-regime"] = (sig_btc_regime_gate, -0.30, None)
    return out


def cmd_run(args: argparse.Namespace) -> int:
    catalogue = _catalogue()
    names = args.candidates or list(catalogue)
    timeframes = args.timeframes or TIMEFRAMES
    pairs = args.pairs or UNIVERSE
    btc_daily = load_btc_daily()

    rows: list[dict[str, object]] = []
    # Benchmark rows are collected once per pair/timeframe/window so the guard
    # table can be read against an identical baseline.
    bench: dict[tuple[str, str, str], float] = {}

    for pair in pairs:
        for timeframe in timeframes:
            frame = load(pair, timeframe)
            if frame is None or len(frame) < 400:
                continue
            cut = int(len(frame) * 0.6)
            for label, window in (("train", frame.iloc[:cut]), ("test", frame.iloc[cut:])):
                bh = buy_and_hold(window, timeframe=timeframe, pair=pair)
                bench[(pair, timeframe, label)] = bh.total_return
                for name in names:
                    signal_fn, stoploss, take_profit = catalogue[name]
                    try:
                        result = _score_one(
                            window,
                            name=name,
                            signal_fn=signal_fn,
                            pair=pair,
                            timeframe=timeframe,
                            stoploss=stoploss,
                            take_profit=take_profit,
                            btc_daily=btc_daily,
                        )
                    except Exception as exc:  # a candidate that cannot run is data too
                        print(f"  !! {name} {pair} {timeframe} {label}: {exc}")
                        continue
                    rows.append(
                        {
                            "candidate": name,
                            "pair": pair,
                            "tf": timeframe,
                            "window": label,
                            "ret": result.total_return * 100,
                            "sharpe": result.sharpe,
                            "calmar": result.calmar,
                            "maxdd": result.max_drawdown * 100,
                            "trades": result.n_trades,
                            "win": result.win_rate * 100,
                            "expo": result.exposure * 100,
                            "t": result.log_t_stat,
                            "bh": bh.total_return * 100,
                            "edge": (result.total_return - bh.total_return) * 100,
                        }
                    )

    if not rows:
        print("no results")
        return 1

    df = pd.DataFrame(rows)
    df.to_csv(Path(__file__).resolve().parent / "hold_guard_results.csv", index=False)

    test = df[df["window"] == "test"]
    summary = (
        test.groupby("candidate")
        .agg(
            sharpe=("sharpe", "mean"),
            calmar=("calmar", "mean"),
            ret=("ret", "mean"),
            edge=("edge", "mean"),
            maxdd=("maxdd", "mean"),
            expo=("expo", "mean"),
            trades=("trades", "mean"),
            beat=("edge", lambda x: (x > 0).mean() * 100),
            dd_beat=("maxdd", lambda x: (x > -25).mean() * 100),
        )
        .sort_values("calmar", ascending=False)
    )

    # The benchmark's own average over the same test cells.
    bh_test = test.drop_duplicates(["pair", "tf"])[["pair", "tf", "bh"]]
    print("\n=== OUT-OF-SAMPLE (test window, 60/40 split) ===")
    print(
        f"buy & hold benchmark over the same cells: "
        f"mean ret {bh_test['bh'].mean():+.2f}%  "
        f"mean maxdd {test.drop_duplicates(['pair','tf'])['bh'].mean() * 0:+.2f}"
    )
    print(
        f"{'candidate':30} {'sharpe':>7} {'calmar':>7} {'ret%':>8} {'edge%':>8} "
        f"{'maxdd%':>8} {'expo%':>6} {'n':>5} {'beatBH%':>8}"
    )
    for name, row in summary.iterrows():
        print(
            f"{name:30} {row['sharpe']:>7.2f} {row['calmar']:>7.2f} {row['ret']:>8.2f} "
            f"{row['edge']:>8.2f} {row['maxdd']:>8.2f} {row['expo']:>6.1f} "
            f"{row['trades']:>5.0f} {row['beat']:>8.1f}"
        )

    if args.detail:
        print("\n=== per pair / timeframe (test) ===")
        cols = ["candidate", "pair", "tf", "ret", "calmar", "maxdd", "trades", "expo", "bh", "edge"]
        detail = test[cols].sort_values(["candidate", "pair", "tf"])
        print(detail.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hold-plus-crash-guard study.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--candidates", nargs="*", default=None)
    p.add_argument("--timeframes", nargs="*", default=None)
    p.add_argument("--pairs", nargs="*", default=None)
    p.add_argument("--detail", action="store_true")
    p.set_defaults(func=cmd_run)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
