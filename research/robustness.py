"""Parameter robustness: does a candidate survive its own neighbourhood?

A single parameter set that performs well is not evidence -- with enough tries,
something always looks good. What distinguishes a real effect from a fitted one is
that performance is **smooth in the parameters**: if ``k = 1.0`` works and ``k =
0.8`` and ``k = 1.2`` also work, the rule captures something; if only ``k = 1.0``
works and its neighbours collapse, it is noise that happened to fit.

This script sweeps a parameter for a candidate across the universe and reports the
mean and dispersion of the out-of-sample Sharpe, so the neighbourhood can be read
at a glance.
"""

from __future__ import annotations

import argparse
import sys
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import run_backtest  # noqa: E402
from candidates import sig_v2_dual_thrust  # noqa: E402
from run import load  # noqa: E402
from sweep import UNIVERSE, split  # noqa: E402

#: Sweeps to run: candidate -> list of (label, signal_fn factory).
SWEEPS: dict[str, dict] = {
    "v2-dual-thrust": {
        "param": "k (trigger width in ranges)",
        "values": [0.5, 0.75, 1.0, 1.25, 1.5, 2.0],
        "make": lambda v: partial(sig_v2_dual_thrust, k=v),
    },
    "v2-keltner": {
        "param": "atr_mult (band width)",
        "values": [1.5, 2.0, 2.5, 3.0, 3.5],
        "make": lambda v: partial(_keltner_with_mult, mult=v),
    },
}


def _keltner_with_mult(frame, mult: float = 2.0):
    """Keltner v2 with a configurable band-width multiplier."""
    from indicators import atr, ema, supertrend

    middle = ema(frame, 20)
    bands = atr(frame, 10)
    upper = middle + mult * bands
    trend = ema(frame, 200)
    direction = supertrend(frame, 10, 3.0)
    breakout = frame["close"] > upper
    fresh = breakout & (~breakout.shift(1).fillna(False))
    regime = (frame["close"] > trend) & (middle > trend)
    expansion = (frame["close"] - upper) > 0.3 * bands
    out = pd.DataFrame({"enter_long": False, "exit_long": False}, index=frame.index)
    out["enter_long"] = fresh & regime & expansion & (frame["volume"] > 0)
    out["exit_long"] = (direction < 0) & (frame["volume"] > 0)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Parameter robustness sweep.")
    parser.add_argument("--candidate", default="v2-dual-thrust")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--pairs", nargs="*", default=UNIVERSE)
    args = parser.parse_args()

    spec = SWEEPS[args.candidate]
    print(f"\n=== {args.candidate}: {spec['param']} -- out-of-sample Sharpe ===")
    print(
        f"{'value':>8} {'meanSh':>8} {'medSh':>8} {'stdSh':>7} "
        f"{'meanRet%':>9} {'trades':>8} {'pos%':>6}"
    )

    for value in spec["values"]:
        signal_fn = spec["make"](value)
        sharpes, returns, trades = [], [], []
        for pair in args.pairs:
            frame = load(pair, args.timeframe)
            if frame is None or len(frame) < 400:
                continue
            _, test = split(frame, 0.6)
            result = run_backtest(
                test,
                signal_fn(test),
                name=args.candidate,
                pair=pair,
                timeframe=args.timeframe,
                stoploss=-0.10,
                take_profit=0.15,
            )
            sharpes.append(result.sharpe)
            returns.append(result.total_return * 100)
            trades.append(result.n_trades)
        if not sharpes:
            continue
        print(
            f"{value:>8} {np.mean(sharpes):>8.2f} {np.median(sharpes):>8.2f} "
            f"{np.std(sharpes):>7.2f} {np.mean(returns):>9.2f} {np.mean(trades):>8.0f} "
            f"{(np.array(sharpes) > 0).mean() * 100:>6.0f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
