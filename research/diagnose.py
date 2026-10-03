"""Diagnosis: attribute each losing strategy's loss to a concrete mechanism.

Run with::

    .venv/bin/python research/diagnose.py

For every deployed profile it backtests the shipped rules over the cached history
and splits the result into the three mechanisms an operator can act on:

* **fee drag** -- the sum of the costs charged, against the gross result. A
  strategy whose gross edge is roughly zero but whose fee bill is large is not a
  strategy with a bad signal; it is a strategy with too many round trips.
* **exit mix** -- how trades ended. A rule whose exits are dominated by one
  mechanical trigger (a middle band, a stop, a signal) tells us which line of the
  code is doing the work, and whether that line is the one the author intended.
* **holding period** -- the median bars held, which bounds how much a trade could
  possibly have moved given realistic per-candle volatility.

This is the evidence base for the v2 redesigns; it is deliberately separate from
the strategy code so the numbers can be regenerated at any time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import DEFAULT_FEE, DEFAULT_SLIPPAGE, buy_and_hold, run_backtest  # noqa: E402
from run import DEPLOYED, load  # noqa: E402
from strategies_original import ORIGINAL_RISK, ORIGINALS  # noqa: E402

#: Cost assumed per side, matching the platform's Binance spot taker fee plus spread.
COST_PER_SIDE = DEFAULT_FEE + DEFAULT_SLIPPAGE


def gross_vs_net(result) -> tuple[float, float, float]:
    """Return ``(net_return, total_cost_fraction, gross_return)`` for a result.

    The cost fraction is approximated from the number of round trips, each charged
    ``2 * COST_PER_SIDE`` on the notional actually traded. Because the strategies
    reinvest roughly the whole wallet, the cost drag on terminal equity is close to
    ``n_trades * 2 * COST_PER_SIDE`` while fully invested; it is scaled here by the
    exposure actually realised, which is the honest lower bound.
    """
    net = result.total_return
    charge = result.n_trades * 2 * COST_PER_SIDE * max(result.exposure, 0.05)
    return net, charge, net + charge


def diagnose(profile_id: str, strategy_id: str, pair: str, timeframe: str) -> dict | None:
    frame = load(pair, timeframe)
    if frame is None:
        return None
    stoploss, take_profit = ORIGINAL_RISK[strategy_id]
    result = run_backtest(
        frame,
        ORIGINALS[strategy_id](frame),
        name=strategy_id,
        pair=pair,
        timeframe=timeframe,
        stoploss=stoploss,
        take_profit=take_profit,
    )
    bench = buy_and_hold(frame, timeframe=timeframe, pair=pair)
    net, charge, gross = gross_vs_net(result)
    holds = [t.bars_held for t in result.trades] or [0]
    return {
        "profile": profile_id,
        "strategy": strategy_id,
        "pair": pair,
        "tf": timeframe,
        "net_pct": net * 100,
        "gross_pct": gross * 100,
        "cost_pct": charge * 100,
        "trades": result.n_trades,
        "win": result.win_rate * 100,
        "pf": result.profit_factor,
        "maxdd": result.max_drawdown * 100,
        "expo": result.exposure * 100,
        "median_bars": float(np.median(holds)),
        "avg_trade_pct": result.avg_trade * 100,
        "exits": result.exit_breakdown(),
        "bh_pct": bench.total_return * 100,
        "bh_sharpe": bench.sharpe,
        "sharpe": result.sharpe,
    }


def main() -> int:
    rows = []
    for profile_id, (strategy_id, pair, timeframe) in sorted(DEPLOYED.items()):
        row = diagnose(profile_id, strategy_id, pair, timeframe)
        if row:
            rows.append(row)

    print(
        f"{'profile':24} {'net%':>8} {'cost%':>7} {'gross%':>8} {'n':>5} {'win%':>6} "
        f"{'pf':>5} {'maxdd%':>7} {'expo%':>6} {'medBars':>7} {'B&H%':>8}"
    )
    print("-" * 110)
    for r in sorted(rows, key=lambda x: x["net_pct"]):
        print(
            f"{r['profile']:24} {r['net_pct']:>8.1f} {r['cost_pct']:>7.1f} "
            f"{r['gross_pct']:>8.1f} "
            f"{r['trades']:>5} {r['win']:>6.1f} {r['pf']:>5.2f} {r['maxdd']:>7.1f} "
            f"{r['expo']:>6.1f} {r['median_bars']:>7.0f} {r['bh_pct']:>8.1f}"
        )

    print("\nExit mix of the five worst profiles\n")
    for r in sorted(rows, key=lambda x: x["net_pct"])[:5]:
        total = sum(r["exits"].values()) or 1
        mix = ", ".join(f"{k}={v} ({v / total * 100:.0f}%)" for k, v in r["exits"].items())
        print(f"  {r['profile']:24} {mix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
