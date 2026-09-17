"""Contract tests for the deterministic synthetic OHLCV generators."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_backtest.config import AppConfig
from trading_backtest.core.errors import ConfigError
from trading_backtest.core.models import BacktestResult, RunnerFn
from trading_backtest.data.synthetic import make_flat_ohlcv, make_ohlcv, make_trending_ohlcv

TIMEFRAMES = ("1m", "15m", "1h", "4h", "1d")


def _crossovers(close: pd.Series) -> int:
    fast = close.ewm(span=9, adjust=False).mean()
    slow = close.ewm(span=21, adjust=False).mean()
    sign = np.sign(np.round((fast - slow).to_numpy(), 10))
    sign = sign[sign != 0]
    if sign.size == 0:
        return 0
    return int((np.diff(sign) != 0).sum())


def _assert_ohlcv_invariants(frame: pd.DataFrame) -> None:
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.name == "timestamp"
    assert str(frame.index.tz) == "UTC"
    assert frame.index.is_monotonic_increasing
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert all(dtype == np.dtype("float64") for dtype in frame.dtypes)
    assert frame.isna().to_numpy().sum() == 0
    assert (frame[["open", "high", "low", "close"]] > 0).to_numpy().all()
    assert (frame["volume"] > 0).to_numpy().all()
    assert (frame["high"] >= frame[["open", "close"]].max(axis=1)).all()
    assert (frame["low"] <= frame[["open", "close"]].min(axis=1)).all()


# ---------------------------------------------------------------------------
# make_ohlcv
# ---------------------------------------------------------------------------


def test_make_ohlcv_is_deterministic() -> None:
    pd.testing.assert_frame_equal(make_ohlcv(200, seed=3), make_ohlcv(200, seed=3))


def test_make_ohlcv_different_seeds_differ() -> None:
    first = make_ohlcv(200, seed=1)
    second = make_ohlcv(200, seed=2)
    assert not first["close"].equals(second["close"])


def test_make_ohlcv_index_contract(ohlcv_frame: pd.DataFrame) -> None:
    assert len(ohlcv_frame) == 500
    assert ohlcv_frame.index.name == "timestamp"
    assert str(ohlcv_frame.index.tz) == "UTC"
    assert ohlcv_frame.index.freq == pd.Timedelta(hours=1)
    assert ohlcv_frame.index[0] == pd.Timestamp("2023-01-01T00:00:00Z")
    assert ohlcv_frame.index[-1] == pd.Timestamp("2023-01-21T19:00:00Z")


def test_make_ohlcv_invariants(ohlcv_frame: pd.DataFrame) -> None:
    _assert_ohlcv_invariants(ohlcv_frame)
    assert ohlcv_frame["open"].iloc[0] == pytest.approx(100.0)
    assert ohlcv_frame["close"].iloc[:-1].tolist() == pytest.approx(
        ohlcv_frame["open"].iloc[1:].tolist()
    )


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_make_ohlcv_uses_the_requested_timeframe(timeframe: str) -> None:
    frame = make_ohlcv(5, timeframe=timeframe)
    assert frame.index[1] - frame.index[0] == pd.Timedelta(
        minutes={"1m": 1, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}[timeframe]
    )


def test_make_ohlcv_honours_start_and_parameters() -> None:
    frame = make_ohlcv(
        10, start="2024-06-01T12:00:00Z", initial_price=50.0, drift=0.001, volatility=0.0
    )
    assert frame.index[0] == pd.Timestamp("2024-06-01T12:00:00Z")
    assert frame["open"].iloc[0] == pytest.approx(50.0)
    assert frame["close"].iloc[-1] > frame["close"].iloc[0]  # positive drift

    flat = make_ohlcv(10, drift=0.0, volatility=0.0)
    assert flat["close"].nunique() == 1
    assert flat["close"].iloc[0] == pytest.approx(100.0)


def test_make_ohlcv_reaches_up_and_down() -> None:
    frame = make_ohlcv(500, seed=42, drift=0.0005)
    assert frame["close"].max() > frame["close"].iloc[0]
    assert frame["close"].min() < frame["close"].max()


def test_make_ohlcv_zero_candles() -> None:
    frame = make_ohlcv(0)
    assert len(frame) == 0
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert str(frame.index.tz) == "UTC"
    assert frame.index.name == "timestamp"
    assert all(dtype == np.dtype("float64") for dtype in frame.dtypes)


def test_make_ohlcv_single_candle() -> None:
    frame = make_ohlcv(1)
    assert len(frame) == 1
    assert frame["open"].iloc[0] == pytest.approx(100.0)
    _assert_ohlcv_invariants(frame)


def test_make_ohlcv_negative_n_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        make_ohlcv(-1)


def test_make_ohlcv_returns_a_fresh_frame() -> None:
    first = make_ohlcv(20)
    first.iloc[0, 0] = -1.0
    second = make_ohlcv(20)
    assert second.iloc[0, 0] > 0


# ---------------------------------------------------------------------------
# make_trending_ohlcv
# ---------------------------------------------------------------------------


def test_make_trending_ohlcv_is_deterministic() -> None:
    pd.testing.assert_frame_equal(make_trending_ohlcv(300), make_trending_ohlcv(300))
    assert not make_trending_ohlcv(300, seed=1)["close"].equals(
        make_trending_ohlcv(300, seed=2)["close"]
    )


@pytest.mark.parametrize("n", [400, 401, 450, 500, 600, 700, 800, 1000])
def test_make_trending_ohlcv_guarantees_ema_crossovers(n: int) -> None:
    frame = make_trending_ohlcv(n)
    close = frame["close"]
    assert close.isna().sum() == 0
    assert (close > 0).all()
    assert _crossovers(close) >= 2


def test_trending_frame_fixture_has_enough_crossovers(trending_frame: pd.DataFrame) -> None:
    assert len(trending_frame) == 600
    assert _crossovers(trending_frame["close"]) >= 2
    _assert_ohlcv_invariants(trending_frame)


def test_make_trending_ohlcv_oscillates_and_trends() -> None:
    frame = make_trending_ohlcv(600)
    assert frame["close"].max() - frame["close"].min() > 10.0
    assert frame["close"].iloc[-1] > frame["close"].iloc[0]
    assert frame["close"].min() >= 90.0


def test_make_trending_ohlcv_edge_cases() -> None:
    empty = make_trending_ohlcv(0)
    assert len(empty) == 0
    assert list(empty.columns) == ["open", "high", "low", "close", "volume"]
    single = make_trending_ohlcv(1)
    assert len(single) == 1
    _assert_ohlcv_invariants(single)


def test_make_trending_ohlcv_returns_a_fresh_frame() -> None:
    first = make_trending_ohlcv(50)
    first.iloc[0, 0] = -1.0
    assert make_trending_ohlcv(50).iloc[0, 0] > 0


# ---------------------------------------------------------------------------
# make_flat_ohlcv
# ---------------------------------------------------------------------------


def test_make_flat_ohlcv_has_zero_range_and_zero_volatility(flat_frame: pd.DataFrame) -> None:
    assert len(flat_frame) == 100
    _assert_ohlcv_invariants(flat_frame)
    assert flat_frame["close"].nunique() == 1
    assert flat_frame["close"].iloc[0] == pytest.approx(100.0)
    assert (flat_frame["high"] == flat_frame["low"]).all()
    assert (flat_frame["high"] - flat_frame["low"]).sum() == 0.0
    assert flat_frame["close"].pct_change().dropna().abs().sum() == 0.0
    assert (flat_frame["volume"] == 1.0).all()


def test_make_flat_ohlcv_custom_price_and_edges() -> None:
    frame = make_flat_ohlcv(5, price=42.5)
    assert frame["close"].iloc[0] == pytest.approx(42.5)
    assert frame["high"].iloc[0] == pytest.approx(42.5)
    assert len(make_flat_ohlcv(0)) == 0
    assert len(make_flat_ohlcv(1)) == 1


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def test_every_conftest_fixture_resolves(
    ohlcv_frame: pd.DataFrame,
    trending_frame: pd.DataFrame,
    flat_frame: pd.DataFrame,
    equity_series: pd.Series,
    cache_dir: Path,
    app_config: AppConfig,
    synthetic_csv: Path,
    runner_stub: RunnerFn,
) -> None:
    assert len(ohlcv_frame) == 500
    assert len(trending_frame) == 600
    assert len(flat_frame) == 100

    assert equity_series.name == "equity"
    assert equity_series.dtype == np.dtype("float64")
    assert len(equity_series) == 200
    assert str(equity_series.index.tz) == "UTC"
    assert equity_series.index.name == "timestamp"
    assert equity_series.is_monotonic_increasing

    assert cache_dir.name == "cache"
    assert not cache_dir.exists()

    assert app_config.data.cache_dir == cache_dir
    assert app_config.data.allow_network is False
    assert app_config.data.validate is True
    assert app_config.backtest.initial_balance == 10_000.0

    assert synthetic_csv.is_file()
    assert synthetic_csv.name == "BTC_USDT-1h.csv"
    written = pd.read_csv(synthetic_csv, index_col="timestamp", parse_dates=True)
    assert len(written) == 600

    result = runner_stub(trending_frame, None)
    assert isinstance(result, BacktestResult)
    assert result.n_trades == 3
    assert len(result.equity_curve) == len(trending_frame)
    assert all(trade.entry_time >= trending_frame.index[0] for trade in result.trades)
