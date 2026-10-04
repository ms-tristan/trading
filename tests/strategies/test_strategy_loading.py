"""Loading and signal-contract tests for the fifteen strategies shipped in user_data/strategies.

Every strategy file is resolved through freqtrade's own ``StrategyResolver`` with a
minimal in-memory configuration, then fed a deterministic OHLCV frame. The tests
never start a bot, never download candles and never touch an exchange: they only
pin the interface version, the risk attributes and the signal contract (0/1
columns, no NaN, at least one entry) of the shipped strategies.

The six unfiltered trend and breakout profiles also mix in the shared BTC 200-day
regime gate, so they are fed a daily ``BTC/USDT`` frame instead of an exchange:
the gate fails closed without one, which is what keeps a broken data feed from
opening a trade.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

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

#: The declared timeframe of each shipped strategy, keyed by class name.
TIMEFRAMES: dict[str, str] = dict(STRATEGY_CASES)

CANDLE_COUNT = 600
CANDLE_SEED = 42
DRIFT_PER_CANDLE = 0.0008
VOLATILITY = 0.010
CYCLE_AMPLITUDE = 0.003
CYCLE_PERIOD = 16.0
WICK_VOLATILITY = 0.004
PAIR = "BTC/USDT"

#: The six unfiltered trend and breakout profiles that mix in the BTC regime gate.
#: Kept in one place so the gated and the ungated assertions below can never drift.
GATED_STRATEGIES = frozenset(
    {
        "BasicStrategy",
        "MacdStrategy",
        "DonchianStrategy",
        "SupertrendStrategy",
        "DualThrustStrategy",
        "MomentumStrategy",
    }
)

#: The support module and class the six gated strategies import, and the pair and
#: timeframe the gate reads.
REGIME_GATE_MODULE = "market_regime"
REGIME_GATE_CLASS = "BtcRegimeGateMixin"
BTC_REGIME_TIMEFRAME = "1d"

#: The exact ROI ladder every shipped strategy must declare.
#:
#: The six trend and breakout profiles disable the ladder with a single unreachable
#: rung, so only the exit signal or the stoploss closes the trade and the right tail
#: of a trend move is no longer truncated. The six research strategies keep their
#: own ladders untouched. The three mean-reversion profiles keep a ladder -- mean
#: reversion needs a target -- but their terminal rung carries the previous tier's
#: value instead of decaying to zero.
EXPECTED_MINIMAL_ROI: dict[str, dict[int, float]] = {
    "BasicStrategy": {0: 1.0},
    "MacdStrategy": {0: 1.0},
    "DonchianStrategy": {0: 1.0},
    "SupertrendStrategy": {0: 1.0},
    "DualThrustStrategy": {0: 1.0},
    "MomentumStrategy": {0: 1.0},
    "BollingerStrategy": {0: 0.05, 240: 0.025, 720: 0.025},
    "KeltnerStrategy": {0: 0.15, 720: 0.07, 2880: 0.07},
    "RsiReversionStrategy": {0: 0.06, 240: 0.03, 720: 0.03},
    "FaberStrategy": {0: 1.0},
    "KeltnerBreakoutV2Strategy": {0: 1.0},
    "TrendEnsembleV2Strategy": {0: 1.0},
    "VolTargetedTrendStrategy": {0: 1.0},
    "FaberAllInStrategy": {0: 1.0},
    "DonchianAllInStrategy": {0: 1.0},
}

#: Number of daily BTC candles served to the gate, and the first of them.
BTC_DAILY_CANDLES = 400
BTC_DAILY_START = pd.Timestamp("2023-01-01", tz="UTC")


def import_market_regime() -> ModuleType:
    """Import ``user_data/strategies/market_regime.py`` the way freqtrade's resolver does.

    ``StrategyResolver`` injects the strategy directory into ``sys.path`` while it
    executes a strategy module, which is how ``from market_regime import
    BtcRegimeGateMixin`` resolves inside the six gated strategies. Importing the
    module here under that same name guarantees both sides see one class object.
    """
    module = sys.modules.get(REGIME_GATE_MODULE)
    if module is not None:
        return module
    directory = str(STRATEGY_PATH.resolve())
    sys.path.insert(0, directory)
    try:
        return importlib.import_module(REGIME_GATE_MODULE)
    finally:
        sys.path.remove(directory)


BtcRegimeGateMixin = import_market_regime().BtcRegimeGateMixin


class DataProviderDouble:
    """Minimal stand-in for freqtrade's ``DataProvider``, serving one cached frame."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def get_pair_dataframe(
        self, pair: str, timeframe: str | None = None, candle_type: str = ""
    ) -> pd.DataFrame:
        return self.frame


def build_btc_daily_candles(rising: bool = True) -> pd.DataFrame:
    """Return a 400-day BTC/USDT frame whose 200-day trend is unambiguously up or down.

    The frame starts before and ends after the deterministic OHLCV series below, so
    every signal candle is aligned on a daily candle the gate can answer for. A
    strictly monotonic close keeps the answer independent of the exact dates: every
    day past the warm-up is risk-on when the series rises and risk-off when it falls.
    """
    if rising:
        daily_close = 100.0 * np.exp(np.linspace(0.0, 1.0, BTC_DAILY_CANDLES))
    else:
        daily_close = 100.0 * np.exp(np.linspace(1.0, 0.0, BTC_DAILY_CANDLES))
    return pd.DataFrame(
        {
            "date": pd.date_range(BTC_DAILY_START, periods=BTC_DAILY_CANDLES, freq="1D", tz="UTC"),
            "open": daily_close,
            "high": daily_close,
            "low": daily_close,
            "close": daily_close,
            "volume": 1.0,
        }
    )


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
    # The six gated strategies read BTC/USDT on the daily timeframe and fail closed
    # without a provider, so a rising daily frame is attached before the signals run.
    strategy.dp = DataProviderDouble(build_btc_daily_candles())

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


@pytest.mark.parametrize(("strategy_name", "timeframe"), STRATEGY_CASES)
def test_only_the_six_unfiltered_strategies_mix_in_the_btc_regime_gate(
    strategy_name: str, timeframe: str, tmp_path: Path
) -> None:
    """Exactly the six unfiltered profiles carry the gate; the other nine are untouched."""
    strategy = load_strategy(strategy_name, timeframe, tmp_path)
    source = (STRATEGY_PATH / f"{strategy_name}.py").read_text(encoding="utf-8")

    if strategy_name in GATED_STRATEGIES:
        assert BtcRegimeGateMixin in type(strategy).__bases__
        assert isinstance(strategy, BtcRegimeGateMixin)
        assert BtcRegimeGateMixin.__module__ == REGIME_GATE_MODULE
        assert f"from {REGIME_GATE_MODULE} import {REGIME_GATE_CLASS}" in source
        assert f"class {strategy_name}({REGIME_GATE_CLASS}, IStrategy):" in source
        # The daily pair must be declared, or DRY_RUN/LIVE never caches it.
        assert (PAIR, BTC_REGIME_TIMEFRAME) in strategy.informative_pairs()
    else:
        assert not isinstance(strategy, BtcRegimeGateMixin)
        assert REGIME_GATE_MODULE not in source
        assert f"class {strategy_name}(IStrategy):" in source
        assert strategy.informative_pairs() == []


@pytest.mark.parametrize(("strategy_name", "timeframe"), STRATEGY_CASES)
def test_shipped_strategies_declare_the_expected_roi_ladder(
    strategy_name: str, timeframe: str, tmp_path: Path
) -> None:
    """The ROI ladders are pinned exactly, including the six disabled ones."""
    strategy = load_strategy(strategy_name, timeframe, tmp_path)

    # freqtrade normalises the rung keys to integers when it loads a strategy.
    assert strategy.minimal_roi == EXPECTED_MINIMAL_ROI[strategy_name]


@pytest.mark.parametrize("strategy_name", sorted(GATED_STRATEGIES))
def test_a_falling_btc_series_blocks_every_entry_of_the_gated_strategies(
    strategy_name: str, tmp_path: Path, candles: pd.DataFrame
) -> None:
    """Below its 200-day average BTC turns the six gated profiles off entirely."""
    strategy = load_strategy(strategy_name, TIMEFRAMES[strategy_name], tmp_path)
    metadata = {"pair": PAIR}
    dataframe = strategy.populate_indicators(candles, metadata)

    strategy.dp = DataProviderDouble(build_btc_daily_candles(rising=False))
    risk_off = strategy.populate_entry_trend(dataframe.copy(), metadata)
    assert int(risk_off["enter_long"].sum()) == 0
    assert (risk_off["enter_tag"] == "").all()

    # The very same candles do enter while BTC is risk-on: the gate is what blocks them.
    strategy.dp = DataProviderDouble(build_btc_daily_candles(rising=True))
    risk_on = strategy.populate_entry_trend(dataframe.copy(), metadata)
    assert int(risk_on["enter_long"].sum()) >= 1


@pytest.mark.parametrize("strategy_name", ["RsiReversionStrategy", "BollingerStrategy"])
def test_an_ungated_strategy_keeps_entering_while_btc_falls(
    strategy_name: str, tmp_path: Path, candles: pd.DataFrame
) -> None:
    """The gate is a property of the six profiles only: the others ignore the regime."""
    strategy = load_strategy(strategy_name, TIMEFRAMES[strategy_name], tmp_path)
    strategy.dp = DataProviderDouble(build_btc_daily_candles(rising=False))

    metadata = {"pair": PAIR}
    dataframe = strategy.populate_indicators(candles, metadata)
    dataframe = strategy.populate_entry_trend(dataframe, metadata)

    assert int(dataframe["enter_long"].sum()) >= 1
