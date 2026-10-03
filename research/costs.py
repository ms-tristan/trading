"""Measure the real round-trip cost hurdle per pair, from candle data alone.

Why this exists. Every backtest in this repository charged a flat 0.3% round trip
(0.1% taker fee + 0.05% slippage per side). A literature review commissioned for
this work found an industry measurement across 432 live round-trips at nine
regulated providers putting retail round-trip costs at **0.53% to 6.45%**, i.e. at
best ~2.65x that assumption. If the platform's real hurdle is closer to 0.5% than
0.3%, then every marginal strategy in the catalogue is worse than it looks, and the
verdicts that depended on a 0.3% assumption have to be re-read.

Rather than import an outside number, this module **measures** the effective spread
from the platform's own candles using the Corwin & Schultz (2012) high-low
estimator, which Brauneis, Mestel, Riordan & Theissen (2021), *Journal of Banking &
Finance* 124, 106041 ("How to measure the liquidity of cryptocurrency markets?")
identify as the best-performing low-frequency proxy for crypto. It needs only
high/low pairs, so it runs on the parquet files already cached.

The estimator infers the bid-ask spread from the fact that a two-day high-low range
is wider than the sum of two one-day ranges by roughly the spread. It is noisy per
observation and is therefore always reported as a cross-sectional median.

This is a **cost floor**, not a full cost model: it measures the quoted spread, not
market impact, and not the taker fee. The true hurdle is spread + fee + impact.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run import load  # noqa: E402
from sweep import UNIVERSE  # noqa: E402

#: Constant from Corwin & Schultz (2012), 3 - 2*sqrt(2).
_CS_CONSTANT = 3.0 - 2.0 * np.sqrt(2.0)


def corwin_schultz_spread(frame: pd.DataFrame) -> pd.Series:
    """Return the per-observation Corwin-Schultz spread estimate, in return units.

    Following the published estimator:

    ``beta``  = E[ ln(H_t/L_t)^2 + ln(H_{t+1}/L_{t+1})^2 ]
    ``gamma`` = ln( max(H_t, H_{t+1}) / min(L_t, L_{t+1}) )^2
    ``alpha`` = (sqrt(2*beta) - sqrt(beta)) / (3 - 2*sqrt(2)) - sqrt(gamma / (3 - 2*sqrt(2)))
    ``spread`` = 2*(exp(alpha) - 1) / (1 + exp(alpha))

    Negative estimates are set to zero: they are sampling noise, and the published
    method treats them as "no spread detected" rather than as a negative cost.
    """
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)

    log_hl = np.log(high / low)
    beta = log_hl**2 + np.roll(log_hl, -1) ** 2

    next_high = np.roll(high, -1)
    next_low = np.roll(low, -1)
    gamma = np.log(np.maximum(high, next_high) / np.minimum(low, next_low)) ** 2

    # The last observation has no successor; roll wraps around, so drop it.
    beta[-1] = np.nan
    gamma[-1] = np.nan

    with np.errstate(invalid="ignore"):
        alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / _CS_CONSTANT - np.sqrt(gamma / _CS_CONSTANT)
        spread = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))

    spread = np.where(np.isfinite(spread), spread, np.nan)
    spread = np.where(spread < 0, 0.0, spread)
    return pd.Series(spread, index=frame.index, name="cs_spread")


def measure(pair: str, timeframe: str, *, recent_bars: int = 5000) -> dict | None:
    """Measure the effective spread for ``pair`` on ``timeframe``.

    **Only the finest timeframe is informative.** This estimator is built from
    consecutive high/low pairs, and its ``gamma`` term grows with the volatility
    captured by one observation. Resampling the *same* BTC market illustrates the
    artefact starkly (measured on this repository's data):

    ============  ===================
    sampling      estimated spread
    ============  ===================
    1h            8.5 bp
    4h (resampled) 21.0 bp
    1d (resampled) 45.3 bp
    ============  ===================

    The market did not become six times more expensive; the estimator lost
    resolution. Corwin & Schultz design it for **daily** observations of a liquid
    equity, and the bias runs the other way in crypto, where a single 1h candle
    already contains a lot of range. Consequently the numbers reported for 4h and
    1d here are **upper bounds of a biased statistic**, not cost estimates, and no
    decision should rest on them. The 1h figures are the usable ones, and even those
    should be read as an order of magnitude.

    The median is taken over the most recent ``recent_bars`` observations, because
    liquidity in crypto changes structurally over years and an all-history median
    would describe a market that no longer exists. The all-history median is
    returned alongside it so the drift is visible.
    """
    frame = load(pair, timeframe)
    if frame is None or len(frame) < 100:
        return None
    spread = corwin_schultz_spread(frame)
    recent = spread.tail(recent_bars).dropna()
    all_time = spread.dropna()
    if recent.empty:
        return None
    return {
        "pair": pair,
        "tf": timeframe,
        "spread_recent": float(recent.median()),
        "spread_all": float(all_time.median()),
        "observed_positive": float((recent > 0).mean()),
        "n": len(recent),
        "candles": len(frame),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure effective spread per pair.")
    parser.add_argument("--timeframes", nargs="*", default=["1h", "4h", "1d"])
    parser.add_argument("--pairs", nargs="*", default=UNIVERSE)
    parser.add_argument("--fee", type=float, default=0.001, help="one-way taker fee")
    args = parser.parse_args()

    rows = []
    for timeframe in args.timeframes:
        for pair in args.pairs:
            row = measure(pair, timeframe)
            if row:
                rows.append(row)

    if not rows:
        print("no data")
        return 1

    print("\n=== Measured effective spread (Corwin-Schultz), one way ===")
    print("   'spread' is the estimated bid-ask; 'hurdle' adds the taker fee on both legs,")
    print("   so it is the round-trip cost a strategy must beat to break even.\n")
    for timeframe in args.timeframes:
        subset = [r for r in rows if r["tf"] == timeframe]
        if not subset:
            continue
        print(f"--- {timeframe} ---")
        print(
            f"{'pair':12} {'spread_bp':>10} {'all_hist_bp':>12} {'hurdle_bp':>10} {'detected%':>10}"
        )
        for r in sorted(subset, key=lambda x: x["spread_recent"]):
            spread_bp = r["spread_recent"] * 10_000
            all_bp = r["spread_all"] * 10_000
            # Round trip = spread paid once (you cross it on entry and on exit it is
            # already embedded) plus the taker fee twice.
            hurdle_bp = spread_bp + 2 * args.fee * 10_000
            print(
                f"{r['pair']:12} {spread_bp:>10.1f} {all_bp:>12.1f} "
                f"{hurdle_bp:>10.1f} {r['observed_positive'] * 100:>10.1f}"
            )
        print()

    print("Reference: this repository charged a flat 0.3% (30 bp) round trip in backtests.")
    print("A taker fee alone (0.1% x 2) is 20 bp, so the flat model was optimistic wherever")
    print("the measured spread exceeds ~10 bp.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
