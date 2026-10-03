"""Regime analysis: how each candidate behaves in bull, bear and chop.

A single number hides the thing that matters for a trading decision -- *when* a
strategy makes and loses money. This script buckets the history by the market's
own state (measured on a 200-period trend and the realised trend slope) and reports
each candidate's return inside each bucket, next to buy-and-hold over the same
candles.

That separation is what turns "this strategy lost 10%" into "this strategy loses
in chop, which is 60% of the sample, and that is why the headline is negative".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import run_backtest  # noqa: E402
from candidates import CANDIDATES  # noqa: E402
from run import load  # noqa: E402
from sweep import UNIVERSE  # noqa: E402


def classify(frame: pd.DataFrame, trend_period: int = 200) -> pd.Series:
    """Label each candle ``bull`` / ``bear`` / ``chop`` from price structure alone.

    * ``bear`` -- close below the trend line *and* the trend line falling;
    * ``bull`` -- close above the trend line *and* the trend line rising;
    * ``chop`` -- everything else (price crossing the line, or a flat line).

    Using a causal definition (only past data) is essential: a regime label built
    with hindsight would make every strategy look prescient.
    """
    trend = frame["close"].rolling(trend_period).mean()
    rising = trend > trend.shift(20)
    falling = trend < trend.shift(20)
    above = frame["close"] > trend
    regime = pd.Series("chop", index=frame.index)
    regime[above & rising] = "bull"
    regime[~above & falling] = "bear"
    return regime


def segment_returns(frame: pd.DataFrame, equity: pd.Series, regime: pd.Series) -> dict[str, float]:
    """Return per-regime total return of an equity curve.

    The equity is rescaled inside each regime segment, so the number answers "if I
    had only been exposed during this regime, what would have happened", which is
    what an operator switching the strategy on and off would experience.
    """
    out: dict[str, float] = {}
    for label in ("bull", "bear", "chop"):
        mask = regime == label
        if mask.sum() < 2:
            out[label] = 0.0
            continue
        segment = equity[mask]
        out[label] = float(segment.iloc[-1] / segment.iloc[0] - 1.0)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Per-regime behaviour of candidates.")
    parser.add_argument("--timeframes", nargs="*", default=["1h", "4h", "1d"])
    parser.add_argument("--pairs", nargs="*", default=UNIVERSE)
    parser.add_argument("--candidates", nargs="*", default=list(CANDIDATES))
    args = parser.parse_args()

    rows = []
    for candidate in args.candidates:
        signal_fn, stoploss, take_profit = CANDIDATES[candidate]
        for timeframe in args.timeframes:
            for pair in args.pairs:
                frame = load(pair, timeframe)
                if frame is None or len(frame) < 600:
                    continue
                # Warm up the indicators, then evaluate on the usable part.
                regime = classify(frame)
                result = run_backtest(
                    frame,
                    signal_fn(frame),
                    name=candidate,
                    pair=pair,
                    timeframe=timeframe,
                    stoploss=stoploss,
                    take_profit=take_profit,
                )
                seg = segment_returns(frame, result.equity, regime)
                rows.append(
                    {
                        "candidate": candidate,
                        "pair": pair,
                        "tf": timeframe,
                        "bull": seg["bull"] * 100,
                        "bear": seg["bear"] * 100,
                        "chop": seg["chop"] * 100,
                        "ret": result.total_return * 100,
                        "sharpe": result.sharpe,
                        "expo": result.exposure * 100,
                    }
                )

    if not rows:
        print("no data")
        return 1
    frame_out = pd.DataFrame(rows)

    print("\n=== mean return (%) per regime, by candidate and timeframe ===")
    for timeframe in args.timeframes:
        subset = frame_out[frame_out["tf"] == timeframe]
        if subset.empty:
            continue
        agg = (
            subset.groupby("candidate")
            .agg(
                bull=("bull", "mean"),
                bear=("bear", "mean"),
                chop=("chop", "mean"),
                ret=("ret", "mean"),
                sharpe=("sharpe", "mean"),
                expo=("expo", "mean"),
            )
            .sort_values("sharpe", ascending=False)
        )
        print(f"\n--- {timeframe} ---")
        print(
            f"{'candidate':20} {'bull%':>8} {'bear%':>8} {'chop%':>8} "
            f"{'total%':>8} {'sharpe':>7} {'expo%':>6}"
        )
        for name, row in agg.iterrows():
            print(
                f"{name:20} {row['bull']:>8.1f} {row['bear']:>8.1f} {row['chop']:>8.1f} "
                f"{row['ret']:>8.1f} {row['sharpe']:>7.2f} {row['expo']:>6.1f}"
            )

    # How much of the sample is each regime?
    print("\n=== regime shares of the sample (BTC 1h) ===")
    frame = load("BTC/USDT", "1h")
    if frame is not None:
        regime = classify(frame)
        shares = regime.value_counts(normalize=True) * 100
        for label, share in shares.items():
            print(f"  {label:6} {share:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
