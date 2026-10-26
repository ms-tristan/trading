"""Crash-window analysis: what each guard actually does during the drawdowns.

The averages in ``hold_guard_study.py`` answer "is the rule good on average". They
do **not** answer the question the whole idea came from: *when the market crashes,
does the guard get out and does stepping aside save real money?*

A two-year mean hides this. A rule that is flat through the crash and merely
average the rest of the time produces the same mean as a rule that is average
throughout -- but only the first one is crash protection.

This module therefore:

1. finds the actual crash episodes in the cached history (peak-to-trough declines
   of the **benchmark**, dated), rather than assuming which dates matter;
2. replays every candidate over each episode and reports its return *inside that
   window* next to buy-and-hold's;
3. reports the **capture ratio** (how much of the crash the guard ate) and the
   recovery behaviour after the trough.

Usage::

    .venv/bin/python research/crash_windows.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import BacktestResult, buy_and_hold, run_backtest  # noqa: E402
from hold_guard_study import _catalogue  # noqa: E402
from hold_plus_guard import load_btc_daily  # noqa: E402
from run import load  # noqa: E402

#: Pairs examined for crash episodes. Majors only: they define "the market".
CRASH_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

#: A drawdown of at least this depth (as a positive fraction) counts as a crash.
CRASH_DEPTH = 0.20


@dataclass
class Episode:
    """One peak-to-trough drawdown of the benchmark."""

    pair: str
    peak: pd.Timestamp
    trough: pd.Timestamp
    depth: float

    @property
    def label(self) -> str:
        return f"{self.pair} {self.peak.date()}→{self.trough.date()} ({self.depth * 100:.0f}%)"


def find_episodes(frame: pd.DataFrame, pair: str, min_depth: float = CRASH_DEPTH) -> list[Episode]:
    """Locate every benchmark drawdown deeper than ``min_depth``.

    The trough of a drawdown is only knowable in hindsight, which is exactly why
    this is a *diagnostic* and not a trading rule: it identifies the windows worth
    measuring, it does not claim anyone could have called them live.
    """
    close = frame["close"]
    running_max = close.cummax()
    drawdown = close / running_max - 1.0

    episodes: list[Episode] = []
    peak_idx = close.index[0]
    peak_val = close.iloc[0]
    trough_idx = close.index[0]
    trough_val = close.iloc[0]
    in_dd = False

    for ts, value, dd in zip(close.index, close.to_numpy(), drawdown.to_numpy()):
        if value >= peak_val:
            # A new high closes any open episode.
            if in_dd:
                depth = 1.0 - trough_val / peak_val
                if depth >= min_depth:
                    episodes.append(Episode(pair, peak_idx, trough_idx, depth))
                in_dd = False
            peak_idx, peak_val = ts, value
            trough_idx, trough_val = ts, value
        else:
            if not in_dd:
                in_dd = True
                trough_idx, trough_val = ts, value
            if value < trough_val:
                trough_idx, trough_val = ts, value

    if in_dd:
        depth = 1.0 - trough_val / peak_val
        if depth >= min_depth:
            episodes.append(Episode(pair, peak_idx, trough_idx, depth))
    return episodes


def evaluate_episode(
    frame: pd.DataFrame,
    episode: Episode,
    catalogue: dict,
    timeframe: str,
    btc_daily: pd.DataFrame | None,
) -> list[dict[str, object]]:
    """Return each candidate's performance inside one crash window."""
    window = frame.loc[episode.peak : episode.trough]
    if len(window) < 20:
        return []
    bh = buy_and_hold(window, timeframe=timeframe, pair=episode.pair)

    out: list[dict[str, object]] = []
    for name, (signal_fn, stoploss, take_profit) in catalogue.items():
        try:
            signals = (
                signal_fn(window, btc=btc_daily)
                if signal_fn.__name__ == "sig_btc_regime_gate"
                else signal_fn(window)
            )
            result: BacktestResult = run_backtest(
                window,
                signals,
                name=name,
                pair=episode.pair,
                timeframe=timeframe,
                stoploss=stoploss,
                take_profit=take_profit,
            )
        except Exception:
            continue
        out.append(
            {
                "episode": episode.label,
                "candidate": name,
                "ret": result.total_return * 100,
                "maxdd": result.max_drawdown * 100,
                "expo": result.exposure * 100,
                "trades": result.n_trades,
                "bh": bh.total_return * 100,
                # Capture ratio: how much of the benchmark's move the guard took.
                # During a crash, lower is better -- <1 means it dodged some of it.
                "capture": (result.total_return / bh.total_return) if bh.total_return else float("nan"),
            }
        )
    return out


def main() -> int:
    catalogue = _catalogue()
    btc_daily = load_btc_daily()
    all_rows: list[dict[str, object]] = []
    episodes_found: list[Episode] = []

    for pair in CRASH_PAIRS:
        frame = load(pair, "1d")
        if frame is None:
            continue
        episodes = find_episodes(frame, pair)
        episodes_found.extend(episodes)
        for ep in episodes:
            all_rows.extend(evaluate_episode(frame, ep, catalogue, "1d", btc_daily))

    if not all_rows:
        print("no crash episodes found at the configured depth")
        return 1

    df = pd.DataFrame(all_rows)
    df.to_csv(Path(__file__).resolve().parent / "crash_windows_results.csv", index=False)

    print(f"=== crash episodes of >= {CRASH_DEPTH * 100:.0f}% in the cached history ===")
    for ep in sorted(episodes_found, key=lambda e: e.peak):
        print(f"  {ep.label}")

    print("\n=== behaviour INSIDE the crash windows (mean across episodes) ===")
    print("capture = strategy return / buy&hold return; <1 means it cushioned the fall")
    summary = (
        df.groupby("candidate")
        .agg(
            ret=("ret", "mean"),
            bh=("bh", "mean"),
            capture=("capture", "mean"),
            maxdd=("maxdd", "mean"),
            expo=("expo", "mean"),
            trades=("trades", "mean"),
        )
        .sort_values("ret", ascending=False)
    )
    print(
        f"{'candidate':30} {'ret%':>8} {'B&H%':>8} {'capture':>8} {'maxdd%':>8} "
        f"{'expo%':>7} {'n':>5}"
    )
    for name, row in summary.iterrows():
        cap = row["capture"]
        cap_s = "n/a" if pd.isna(cap) else f"{cap:>8.2f}"
        print(
            f"{name:30} {row['ret']:>8.2f} {row['bh']:>8.2f} {cap_s} "
            f"{row['maxdd']:>8.2f} {row['expo']:>7.1f} {row['trades']:>5.1f}"
        )

    print("\n=== per episode ===")
    piv = df.pivot_table(index="episode", columns="candidate", values="ret")
    bh_series = df.groupby("episode")["bh"].first()
    print(pd.concat([bh_series.rename("BUY&HOLD"), piv], axis=1).to_string(float_format=lambda x: f"{x:.1f}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
