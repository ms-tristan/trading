"""Loading and signal-contract tests for the ten strategies shipped in user_data/strategies.

Every strategy file is resolved through freqtrade's own ``StrategyResolver`` with a
minimal in-memory configuration, then fed a deterministic OHLCV frame. The tests
never start a bot, never download candles and never touch an exchange: they only
pin the interface version, the risk attributes and the signal contract (0/1
columns, no NaN, at least one entry) of the shipped strategies.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from freqtrade.exchange.exchange_utils_timeframe import timeframe_to_minutes
from freqtrade.resolvers.strategy_resolver import StrategyResolver
from freqtrade.strategy import IStrategy

# Resolution is relative to the repository root, exactly like the platform launcher does.
STRATEGY_PATH = Path("user_data/strategies")

# The timeframes the platform is allowed to ship. freqtrade's own parser below is the
# authority; this set only keeps the catalogue honest.
SUPPORTED_TIMEFRAMES = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
)

# One case per shipped strategy: (class name, timeframe declared by the strategy).
# The declared timeframe is the primary one listed for the profile in config/strategies.json.
STRATEGY_CASES = [
    ("BasicStrategy", "1h"),
    ("MomentumStrategy", "15m"),
    ("RsiReversionStrategy", "15m"),
    ("BollingerStrategy", "5m"),
    ("MacdStrategy", "1h"),
    ("DonchianStrategy", "1h"),
    ("KeltnerStrategy", "15m"),
    ("SupertrendStrategy", "1h"),
    ("DualThrustStrategy", "15m"),
    ("FaberStrategy", "1d"),
    # Added by the 2026 research programme; the ten above are unchanged.
    ("KeltnerBreakoutV2Strategy", "4h"),
    ("TrendEnsembleV2Strategy", "1d"),
    ("VolTargetedTrendStrategy", "4h"),
    ("FaberAllInStrategy", "1d"),
    ("DonchianAllInStrategy", "1h"),
]

CANDLE_COUNT = 600
CANDLE_SEED = 42
DRIFT_PER_CANDLE = 0.0008
VOLATILITY = 0.010
CYCLE_AMPLITUDE = 0.003
CYCLE_PERIOD = 16.0
WICK_VOLATILITY = 0.004
PAIR = "BTC/USDT"


def build_candles(n_candles: int = CANDLE_COUNT, seed: int = CANDLE_SEED) -> pd.DataFrame:
    """Build a deterministic OHLCV frame with a mild upward drift and real volatility.

    The series is engineered rather than purely random: a fixed seed, a positive
    drift and a slow cycle on top of the noise. That combination guarantees that
    every shipped strategy -- trend following, breakout and mean reversion alike
    -- finds several setups, while staying perfectly reproducible.
    """
    rng = np.random.default_rng(seed)
    steps = np.arange(n_candles)
    returns = (
        DRIFT_PER_CANDLE
        + rng.normal(0.0, VOLATILITY, n_candles)
        + CYCLE_AMPLITUDE * np.sin(steps / CYCLE_PERIOD)
    )
    close = 100.0 * np.exp(np.cumsum(returns))

    open_price = np.empty(n_candles)
    open_price[0] = close[0]
    open_price[1:] = close[:-1]

    upper_wick = np.abs(rng.normal(0.0, WICK_VOLATILITY, n_candles))
    lower_wick = np.abs(rng.normal(0.0, WICK_VOLATILITY, n_candles))
    high = np.maximum(open_price, close) * np.exp(upper_wick)
    low = np.minimum(open_price, close) * np.exp(-lower_wick)

    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n_candles, freq="5min", tz="UTC"),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(50.0, 150.0, n_candles),
        }
    )


def load_strategy(strategy_name: str, timeframe: str, user_data_dir: Path) -> IStrategy:
    """Load one shipped strategy through freqtrade's own resolver.

    ``strategy_path`` and ``user_data_dir`` must be ``pathlib.Path`` instances:
    the resolver calls ``.joinpath()`` on them and a ``str`` raises ``AttributeError``.
    """
    config = {
        "strategy": strategy_name,
        "strategy_path": STRATEGY_PATH,
        "user_data_dir": Path(user_data_dir),
        "timeframe": timeframe,
        "stake_currency": "USDT",
        "stake_amount": 100.0,
        "dry_run": True,
        "trading_mode": "spot",
        "margin_mode": "",
        "max_open_trades": 1,
        "exchange": {
            "name": "binance",
            "key": "",
            "secret": "",
            "pair_whitelist": [PAIR],
        },
    }
    return StrategyResolver.load_strategy(config)


@pytest.fixture
def candles() -> pd.DataFrame:
    """Return a fresh deterministic OHLCV frame for every test case."""
    return build_candles()


@pytest.mark.parametrize(("strategy_name", "timeframe"), STRATEGY_CASES)
def test_strategy_loads_and_produces_valid_signals(
    strategy_name: str, timeframe: str, tmp_path: Path, candles: pd.DataFrame
) -> None:
    """A shipped strategy loads, declares the contract and emits 0/1 signals with no NaN."""
    strategy = load_strategy(strategy_name, timeframe, tmp_path)

    # --- static contract -------------------------------------------------
    assert type(strategy).__name__ == strategy_name
    assert strategy.INTERFACE_VERSION == 3
    assert strategy.can_short is False
    assert strategy.use_exit_signal is True
    assert strategy.process_only_new_candles is True
    assert isinstance(strategy.startup_candle_count, int)
    assert strategy.startup_candle_count > 0
    assert isinstance(strategy.minimal_roi, dict) and strategy.minimal_roi
    assert strategy.stoploss is not None and strategy.stoploss < 0

    # --- timeframe ------------------------------------------------------
    assert strategy.timeframe in SUPPORTED_TIMEFRAMES
    # freqtrade's own parser is the authority on a timeframe string.
    assert timeframe_to_minutes(strategy.timeframe) > 0

    # --- signals --------------------------------------------------------
    metadata = {"pair": PAIR}
    dataframe = strategy.populate_indicators(candles, metadata)
    dataframe = strategy.populate_entry_trend(dataframe, metadata)
    dataframe = strategy.populate_exit_trend(dataframe, metadata)

    assert set(dataframe["enter_long"].unique()) <= {0, 1}
    assert set(dataframe["exit_long"].unique()) <= {0, 1}
    assert not dataframe[["enter_long", "exit_long", "enter_tag", "exit_tag"]].isna().any().any()
    assert int(dataframe["enter_long"].sum()) >= 1
    assert int(dataframe["exit_long"].sum()) >= 1

    # Every fired signal carries its tag: an empty tag would reach the trade history.
    entered = dataframe["enter_long"] == 1
    exited = dataframe["exit_long"] == 1
    assert (dataframe.loc[entered, "enter_tag"] != "").all()
    assert (dataframe.loc[exited, "exit_tag"] != "").all()


@pytest.mark.parametrize(("strategy_name", "timeframe"), STRATEGY_CASES)
def test_strategy_files_are_discoverable(strategy_name: str, timeframe: str) -> None:
    """Every shipped strategy file exists, is named after its class and declares that class."""
    strategy_file = STRATEGY_PATH / f"{strategy_name}.py"

    assert strategy_file.is_file()
    source = strategy_file.read_text(encoding="utf-8")
    # freqtrade discovers a strategy class by looking for `class <Name>(` in the file.
    assert f"class {strategy_name}(" in source
    assert "import talib.abstract as ta" in source
