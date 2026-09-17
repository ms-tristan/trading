"""Contract tests for the on-disk OHLCV cache."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_backtest.core.errors import ConfigError
from trading_backtest.data.cache import OHLCVCache
from trading_backtest.data.synthetic import make_ohlcv


@pytest.fixture(params=["parquet", "csv"])
def cache(request: pytest.FixtureRequest, cache_dir: Path) -> OHLCVCache:
    return OHLCVCache(cache_dir / request.param, fmt=request.param)


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------


def test_path_for_naming(cache_dir: Path) -> None:
    cache = OHLCVCache(cache_dir, fmt="parquet")
    path = cache.path_for("Binance", "btc/usdt", "1h")
    assert path == cache_dir / "binance" / "BTC_USDT" / "1h.parquet"
    assert cache.path_for("binance", "ETH/USDT", "4h").name == "4h.parquet"
    assert cache.extension == "parquet"
    csv_cache = OHLCVCache(cache_dir, fmt="csv")
    assert csv_cache.path_for("binance", "BTC/USDT", "1d").name == "1d.csv"
    assert csv_cache.extension == "csv"


def test_unsupported_format_raises_config_error(cache_dir: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        OHLCVCache(cache_dir, fmt="feather")  # type: ignore[arg-type]
    assert "feather" in str(excinfo.value)


def test_exists_is_false_before_any_write(cache: OHLCVCache) -> None:
    assert cache.exists("binance", "BTC/USDT", "1h") is False
    assert cache.read("binance", "BTC/USDT", "1h") is None


# ---------------------------------------------------------------------------
# write / read round-trip (both formats)
# ---------------------------------------------------------------------------


def test_write_then_read_round_trip(cache: OHLCVCache, ohlcv_frame: pd.DataFrame) -> None:
    path = cache.write("binance", "BTC/USDT", "1h", ohlcv_frame)
    assert path.is_file()
    assert path.parent.is_dir()
    assert cache.exists("binance", "BTC/USDT", "1h") is True

    restored = cache.read("binance", "BTC/USDT", "1h")
    assert restored is not None
    pd.testing.assert_frame_equal(restored, ohlcv_frame, check_freq=False)


def test_round_trip_preserves_index_name_timezone_order_and_dtypes(
    cache: OHLCVCache, ohlcv_frame: pd.DataFrame
) -> None:
    cache.write("binance", "BTC/USDT", "1h", ohlcv_frame)
    restored = cache.read("binance", "BTC/USDT", "1h")
    assert restored is not None
    assert restored.index.name == "timestamp"
    assert isinstance(restored.index, pd.DatetimeIndex)
    assert str(restored.index.tz) == "UTC"
    assert restored.index.is_monotonic_increasing
    assert restored.index.tolist() == ohlcv_frame.index.tolist()
    assert list(restored.columns) == list(ohlcv_frame.columns)
    assert all(dtype == np.dtype("float64") for dtype in restored.dtypes)
    assert restored["close"].tolist() == pytest.approx(ohlcv_frame["close"].tolist())


def test_write_coerces_to_the_ohlcv_contract(cache: OHLCVCache) -> None:
    frame = make_ohlcv(5)
    frame[["open", "close"]] = frame[["open", "close"]].astype("int64")
    cache.write("binance", "BTC/USDT", "1h", frame)
    restored = cache.read("binance", "BTC/USDT", "1h")
    assert restored is not None
    assert all(dtype == np.dtype("float64") for dtype in restored.dtypes)


def test_write_creates_missing_parents(cache_dir: Path, ohlcv_frame: pd.DataFrame) -> None:
    cache = OHLCVCache(cache_dir / "deep" / "nested", fmt="parquet")
    path = cache.write("binance", "BTC/USDT", "1h", ohlcv_frame.iloc[:10])
    assert path.is_file()


def test_csv_cache_writes_the_timestamp_column(cache_dir: Path, ohlcv_frame: pd.DataFrame) -> None:
    cache = OHLCVCache(cache_dir, fmt="csv")
    path = cache.write("binance", "BTC/USDT", "1h", ohlcv_frame.iloc[:5])
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert header == "timestamp,open,high,low,close,volume"


def test_write_does_not_mutate_the_input(cache: OHLCVCache, ohlcv_frame: pd.DataFrame) -> None:
    before = ohlcv_frame.copy(deep=True)
    cache.write("binance", "BTC/USDT", "1h", ohlcv_frame)
    pd.testing.assert_frame_equal(ohlcv_frame, before)


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def test_merge_dedupes_keeping_the_newest_and_sorts(cache: OHLCVCache) -> None:
    frame = make_ohlcv(60)
    older = frame.iloc[:40].copy()
    newer = frame.iloc[20:60].copy()
    newer = newer.assign(close=frame["close"].iloc[20:60] + 5.0)
    merged = cache.merge(older, newer)
    assert len(merged) == 60
    assert merged.index.is_monotonic_increasing
    assert not merged.index.has_duplicates
    assert merged["close"].loc[newer.index[0]] == pytest.approx(newer["close"].iloc[0])
    assert merged["close"].iloc[-1] == pytest.approx(newer["close"].iloc[-1])


def test_merge_is_available_as_a_static_call(cache: OHLCVCache) -> None:
    frame = make_ohlcv(10)
    assert len(OHLCVCache.merge(None, frame)) == 10
    assert len(OHLCVCache.merge(frame, frame.iloc[:0])) == 10
    assert len(OHLCVCache.merge(frame.iloc[:5], frame.iloc[5:])) == 10


def test_merge_returns_the_contract_conformant_frame(cache: OHLCVCache) -> None:
    merged = cache.merge(make_ohlcv(5), make_ohlcv(5, start="2023-02-01T00:00:00Z"))
    assert merged.index.name == "timestamp"
    assert str(merged.index.tz) == "UTC"
    assert merged.index.is_monotonic_increasing
    assert all(dtype == np.dtype("float64") for dtype in merged.dtypes)


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_returns_the_deleted_count_and_is_idempotent(
    cache: OHLCVCache, ohlcv_frame: pd.DataFrame
) -> None:
    cache.write("binance", "BTC/USDT", "1h", ohlcv_frame.iloc[:10])
    cache.write("binance", "ETH/USDT", "1h", ohlcv_frame.iloc[:10])
    assert cache.clear() == 2
    assert cache.clear() == 0
    assert cache.read("binance", "BTC/USDT", "1h") is None


def test_clear_keeps_directories_that_still_hold_files(cache_dir: Path) -> None:
    cache = OHLCVCache(cache_dir)
    cache.write("binance", "BTC/USDT", "1h", make_ohlcv(10))
    keep = cache_dir / "binance" / "BTC_USDT" / "notes.txt"
    keep.write_text("keep me", encoding="utf-8")
    assert cache.clear() == 1
    assert keep.is_file()
    assert cache.read("binance", "BTC/USDT", "1h") is None


def test_clear_can_be_restricted_to_one_exchange(cache_dir: Path) -> None:
    frame = make_ohlcv(10)
    cache = OHLCVCache(cache_dir)
    cache.write("binance", "BTC/USDT", "1h", frame)
    cache.write("kraken", "BTC/USDT", "1h", frame)
    assert cache.clear("KRAKEN") == 1
    assert cache.exists("binance", "BTC/USDT", "1h") is True
    assert cache.exists("kraken", "BTC/USDT", "1h") is False
    assert cache.clear("nowhere") == 0
