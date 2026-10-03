"""Validate the new strategy files through freqtrade's own resolver.

Mirrors what ``tests/strategies/test_strategy_loading.py`` does for the original
ten, so the new files are held to the platform's existing contract: one class per
file named after the file stem, interface version 3, long-only, an explicit
startup count, and 0/1 signal columns with no NaN on a deterministic frame.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from freqtrade.resolvers.strategy_resolver import StrategyResolver

STRATEGY_PATH = Path("user_data/strategies")
PAIR = "BTC/USDT"

#: Strategies added by this work: (class name, declared timeframe).
NEW_CASES = [
    ("KeltnerBreakoutV2Strategy", "4h"),
    ("TrendEnsembleV2Strategy", "1d"),
    ("VolTargetedTrendStrategy", "4h"),
    ("FaberAllInStrategy", "1d"),
    ("DonchianAllInStrategy", "1h"),
]


def build_candles(n_candles: int = 900, seed: int = 42) -> pd.DataFrame:
    """A deterministic frame: positive drift, real volatility, a slow cycle.

    Deliberately the same construction as the shipped test suite so a strategy
    that passes here would also pass there.
    """
    rng = np.random.default_rng(seed)
    steps = np.arange(n_candles)
    returns = 0.0008 + rng.normal(0.0, 0.010, n_candles) + 0.003 * np.sin(steps / 16.0)
    close = 100.0 * np.exp(np.cumsum(returns))
    open_price = np.empty(n_candles)
    open_price[0] = close[0]
    open_price[1:] = close[:-1]
    upper_wick = np.abs(rng.normal(0.0, 0.004, n_candles))
    lower_wick = np.abs(rng.normal(0.0, 0.004, n_candles))
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n_candles, freq="5min", tz="UTC"),
            "open": open_price,
            "high": np.maximum(open_price, close) * np.exp(upper_wick),
            "low": np.minimum(open_price, close) * np.exp(-lower_wick),
            "close": close,
            "volume": rng.uniform(50.0, 150.0, n_candles),
        }
    )


def load(strategy_name: str, timeframe: str, tmp_dir: Path):
    config = {
        "strategy": strategy_name,
        "strategy_path": STRATEGY_PATH,
        "user_data_dir": tmp_dir,
        "timeframe": timeframe,
        "stake_currency": "USDT",
        "stake_amount": 100.0,
        "dry_run": True,
        "trading_mode": "spot",
        "margin_mode": "",
        "max_open_trades": 1,
        "exchange": {"name": "binance", "key": "", "secret": "", "pair_whitelist": [PAIR]},
    }
    return StrategyResolver.load_strategy(config)


def main() -> int:
    import tempfile

    candles = build_candles()
    failures = 0
    for class_name, timeframe in NEW_CASES:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                strategy = load(class_name, timeframe, Path(tmp))
            except Exception as exc:
                print(f"FAIL {class_name}: could not load -- {type(exc).__name__}: {exc}")
                failures += 1
                continue

            frame = strategy.populate_indicators(candles.copy(), {"pair": PAIR})
            frame = strategy.populate_entry_trend(frame, {"pair": PAIR})
            frame = strategy.populate_exit_trend(frame, {"pair": PAIR})

            problems = []
            if type(strategy).__name__ != class_name:
                problems.append("class name mismatch")
            if strategy.INTERFACE_VERSION != 3:
                problems.append("interface version")
            if strategy.can_short is not False:
                problems.append("can_short must be False")
            if not strategy.use_exit_signal:
                problems.append("use_exit_signal must be True")
            if not strategy.process_only_new_candles:
                problems.append("process_only_new_candles must be True")
            startup = strategy.startup_candle_count
            if not isinstance(startup, int) or startup <= 0:
                problems.append("startup_candle_count")
            for column in ("enter_long", "exit_long"):
                values = set(np.unique(frame[column]))
                if not values <= {0, 1}:
                    problems.append(f"{column} not 0/1: {values}")
                if frame[column].isna().any():
                    problems.append(f"{column} has NaN")
            if int(frame["enter_long"].sum()) == 0:
                problems.append("no entry signal produced")

            entries = int(frame["enter_long"].sum())
            exits = int(frame["exit_long"].sum())
            if problems:
                failures += 1
                print(f"FAIL {class_name}: {', '.join(problems)}")
            else:
                print(f"ok   {class_name:34} entries={entries:>4} exits={exits:>4} tf={timeframe}")

    print(f"\n{len(NEW_CASES) - failures}/{len(NEW_CASES)} strategies valid")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
