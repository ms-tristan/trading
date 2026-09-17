"""Contract tests for the market-data providers and the cache-first loader.

Every test is offline: downloads are exercised through a fake provider or a fake
``ccxt`` module injected into ``sys.modules``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_backtest.core.errors import (
    ConfigError,
    DataDownloadError,
    DataValidationError,
    InsufficientDataError,
)
from trading_backtest.data.cache import OHLCVCache
from trading_backtest.data.loader import (
    CcxtDataProvider,
    CsvDataProvider,
    MarketDataProvider,
    OHLCVLoader,
)
from trading_backtest.data.synthetic import make_ohlcv

SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"


class FakeProvider:
    """In-memory provider recording every call (proves *when* a download happens)."""

    def __init__(self, frame: pd.DataFrame | None = None, error: Exception | None = None) -> None:
        self.frame = frame
        self.error = error
        self.calls: list[tuple[str, str, pd.Timestamp, pd.Timestamp]] = []

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: pd.Timestamp, until: pd.Timestamp
    ) -> pd.DataFrame:
        self.calls.append((symbol, timeframe, since, until))
        if self.error is not None:
            raise self.error
        assert self.frame is not None
        return self.frame.loc[(self.frame.index >= since) & (self.frame.index <= until)]


@pytest.fixture
def frame() -> pd.DataFrame:
    return make_ohlcv(300)


def _loader(cache_dir: Path, provider: FakeProvider | None = None, **kwargs: Any) -> OHLCVLoader:
    return OHLCVLoader("binance", cache_dir, provider=provider, **kwargs)


# ---------------------------------------------------------------------------
# cache-first behaviour, offline
# ---------------------------------------------------------------------------


def test_load_returns_only_the_requested_window(cache_dir: Path, frame: pd.DataFrame) -> None:
    loader = _loader(cache_dir, allow_network=False)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, frame)
    window = loader.load(SYMBOL, TIMEFRAME, frame.index[50], frame.index[120])
    assert len(window) == 71
    assert window.index[0] == frame.index[50]
    assert window.index[-1] == frame.index[120]
    assert window.index.is_monotonic_increasing
    assert window.index.name == "timestamp"
    assert str(window.index.tz) == "UTC"


def test_load_never_downloads_when_the_cache_covers_the_window(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    provider = FakeProvider(error=AssertionError("provider must not be called"))
    loader = _loader(cache_dir, provider, allow_network=True)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, frame)
    window = loader.load(SYMBOL, TIMEFRAME, frame.index[0], frame.index[100])
    assert provider.calls == []
    assert len(window) == 101


def test_partial_cache_without_network_raises_and_never_downloads(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    provider = FakeProvider(error=AssertionError("provider must not be called"))
    loader = _loader(cache_dir, provider, allow_network=False)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, frame.iloc[:50])
    with pytest.raises(InsufficientDataError) as excinfo:
        loader.load(SYMBOL, TIMEFRAME, frame.index[10], frame.index[200])
    assert provider.calls == []
    assert "network access is disabled" in str(excinfo.value)


def test_empty_cache_without_network_raises(cache_dir: Path) -> None:
    loader = _loader(cache_dir, allow_network=False)
    with pytest.raises(InsufficientDataError):
        loader.load(
            SYMBOL,
            TIMEFRAME,
            pd.Timestamp("2023-01-01T00:00:00Z"),
            pd.Timestamp("2023-01-02T00:00:00Z"),
        )


def test_allow_network_true_but_a_covered_window_still_avoids_the_custom_provider(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    provider = FakeProvider(error=AssertionError("provider must not be called"))
    loader = _loader(cache_dir, provider)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, frame)
    loader.load(SYMBOL, TIMEFRAME, frame.index[5], frame.index[6])
    assert provider.calls == []


# ---------------------------------------------------------------------------
# download + merge
# ---------------------------------------------------------------------------


def test_download_merges_into_the_cache_and_returns_the_window(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    provider = FakeProvider(frame.loc[frame.index[100] : frame.index[299]])
    loader = _loader(cache_dir, provider)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, frame.iloc[:150])

    window = loader.load(SYMBOL, TIMEFRAME, frame.index[10], frame.index[250])
    assert len(provider.calls) == 1
    symbol, timeframe, since, until = provider.calls[0]
    assert (symbol, timeframe) == (SYMBOL, TIMEFRAME)
    assert since == frame.index[10] and until == frame.index[250]
    assert len(window) == 241
    assert window.index[0] == frame.index[10]
    assert window.index[-1] == frame.index[250]

    assert loader.cache.exists("binance", SYMBOL, TIMEFRAME)
    first, last = loader.available_range(SYMBOL, TIMEFRAME)
    assert first == frame.index[0]
    assert last == frame.index[250]
    merged = loader.cache.read("binance", SYMBOL, TIMEFRAME)
    assert merged is not None and not merged.index.has_duplicates
    assert len(merged) == 251


def test_load_from_an_empty_cache_downloads_everything(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    provider = FakeProvider(frame)
    loader = _loader(cache_dir, provider)
    window = loader.load(SYMBOL, TIMEFRAME, frame.index[0], frame.index[9])
    assert len(window) == 10
    assert len(provider.calls) == 1


def test_download_returns_the_provider_frame_without_caching(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    provider = FakeProvider(frame)
    loader = _loader(cache_dir, provider)
    downloaded = loader.download(SYMBOL, TIMEFRAME, frame.index[0], frame.index[50])
    assert len(downloaded) == 51
    assert loader.cache.exists("binance", SYMBOL, TIMEFRAME) is False


# ---------------------------------------------------------------------------
# argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "since",
    [pd.Timestamp("2023-01-01T00:00:00"), "2023-01-01T00:00:00"],
)
def test_naive_since_raises_config_error(cache_dir: Path, frame: pd.DataFrame, since: Any) -> None:
    loader = _loader(cache_dir, allow_network=False)
    with pytest.raises(ConfigError) as excinfo:
        loader.load(SYMBOL, TIMEFRAME, since, frame.index[10])
    assert "timezone-aware" in str(excinfo.value)


def test_naive_until_raises_config_error(cache_dir: Path, frame: pd.DataFrame) -> None:
    loader = _loader(cache_dir, allow_network=False)
    with pytest.raises(ConfigError):
        loader.load(SYMBOL, TIMEFRAME, frame.index[0], pd.Timestamp("2023-01-02T00:00:00"))


def test_since_not_strictly_before_until_raises_config_error(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    loader = _loader(cache_dir, allow_network=False)
    with pytest.raises(ConfigError):
        loader.load(SYMBOL, TIMEFRAME, frame.index[10], frame.index[10])
    with pytest.raises(ConfigError):
        loader.load(SYMBOL, TIMEFRAME, frame.index[10], frame.index[5])


def test_unknown_timeframe_raises_config_error(cache_dir: Path, frame: pd.DataFrame) -> None:
    loader = _loader(cache_dir, allow_network=False)
    with pytest.raises(ConfigError):
        loader.load(SYMBOL, "2h", frame.index[0], frame.index[10])


def test_nat_timestamp_raises_config_error(cache_dir: Path, frame: pd.DataFrame) -> None:
    loader = _loader(cache_dir, allow_network=False)
    with pytest.raises(ConfigError):
        loader.load(SYMBOL, TIMEFRAME, pd.NaT, frame.index[10])


def test_invalid_timestamp_raises_config_error(cache_dir: Path) -> None:
    loader = _loader(cache_dir, allow_network=False)
    with pytest.raises(ConfigError):
        loader.load(SYMBOL, TIMEFRAME, "not-a-date", pd.Timestamp("2023-01-02T00:00:00Z"))


def test_download_rejects_an_inverted_window(cache_dir: Path, frame: pd.DataFrame) -> None:
    loader = _loader(cache_dir, FakeProvider(frame))
    with pytest.raises(ConfigError):
        loader.download(SYMBOL, TIMEFRAME, frame.index[10], frame.index[5])


def test_download_raises_when_the_provider_returns_nothing(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    loader = _loader(cache_dir, FakeProvider(frame.iloc[:0]))
    with pytest.raises(InsufficientDataError) as excinfo:
        loader.download(SYMBOL, TIMEFRAME, frame.index[0], frame.index[10])
    assert "provider returned no candle" in str(excinfo.value)


def test_an_empty_cache_entry_does_not_cover_the_window(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    loader = _loader(cache_dir, allow_network=False)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, frame.iloc[:0])
    assert loader.cache.exists("binance", SYMBOL, TIMEFRAME) is True
    with pytest.raises(InsufficientDataError):
        loader.load(SYMBOL, TIMEFRAME, frame.index[0], frame.index[10])


def test_a_download_only_outside_the_window_leaves_nothing_to_return(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    class OutOfWindowProvider:
        def fetch_ohlcv(
            self, symbol: str, timeframe: str, since: object, until: object
        ) -> pd.DataFrame:
            return frame

    loader = _loader(cache_dir, OutOfWindowProvider())  # type: ignore[arg-type]
    future = frame.index[-1] + pd.Timedelta(days=30)
    with pytest.raises(InsufficientDataError) as excinfo:
        loader.load(SYMBOL, TIMEFRAME, future, future + pd.Timedelta(hours=10))
    assert "inside" in str(excinfo.value)


# ---------------------------------------------------------------------------
# load_cached / available_range
# ---------------------------------------------------------------------------


def test_load_cached_returns_the_whole_cache_or_a_window(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    loader = _loader(cache_dir, allow_network=False)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, frame)
    assert len(loader.load_cached(SYMBOL, TIMEFRAME)) == 300
    assert len(loader.load_cached(SYMBOL, TIMEFRAME, since=frame.index[10])) == 290
    assert len(loader.load_cached(SYMBOL, TIMEFRAME, until=frame.index[10])) == 11
    assert len(loader.load_cached(SYMBOL, TIMEFRAME, frame.index[10], frame.index[20])) == 11
    with pytest.raises(ConfigError):
        loader.load_cached(SYMBOL, TIMEFRAME, since=pd.Timestamp("2023-01-01"))


def test_load_cached_raises_when_the_window_is_empty(cache_dir: Path, frame: pd.DataFrame) -> None:
    loader = _loader(cache_dir, allow_network=False)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, frame)
    with pytest.raises(InsufficientDataError) as excinfo:
        loader.load_cached(SYMBOL, TIMEFRAME, since=frame.index[-1] + pd.Timedelta(days=5))
    assert "in that window" in str(excinfo.value)


def test_load_cached_on_an_empty_cache_raises(cache_dir: Path) -> None:
    loader = _loader(cache_dir, allow_network=False)
    with pytest.raises(InsufficientDataError):
        loader.load_cached(SYMBOL, TIMEFRAME)


def test_available_range_on_an_empty_cache_raises(cache_dir: Path) -> None:
    loader = _loader(cache_dir, allow_network=False)
    with pytest.raises(InsufficientDataError):
        loader.available_range(SYMBOL, TIMEFRAME)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_validate_true_propagates_a_validation_error(cache_dir: Path, frame: pd.DataFrame) -> None:
    corrupted = frame.copy()
    corrupted.iloc[60, corrupted.columns.get_loc("close")] = np.nan
    loader = _loader(cache_dir, allow_network=False, validate=True)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, corrupted)
    with pytest.raises(DataValidationError) as excinfo:
        loader.load(SYMBOL, TIMEFRAME, frame.index[10], frame.index[100])
    assert any("nan" in issue.lower() for issue in excinfo.value.issues)


def test_validate_false_returns_the_corrupted_window(cache_dir: Path, frame: pd.DataFrame) -> None:
    corrupted = frame.copy()
    corrupted.iloc[60, corrupted.columns.get_loc("close")] = np.nan
    loader = _loader(cache_dir, allow_network=False, validate=False)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, corrupted)
    window = loader.load(SYMBOL, TIMEFRAME, frame.index[10], frame.index[100])
    assert window["close"].isna().sum() == 1


def test_max_missing_ratio_is_forwarded_to_the_validator(
    cache_dir: Path, frame: pd.DataFrame
) -> None:
    holed = frame.drop(frame.index[[50, 51, 52]])
    loader = _loader(cache_dir, allow_network=False, max_gap_factor=10.0, max_missing_ratio=0.5)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, holed)
    assert len(loader.load(SYMBOL, TIMEFRAME, frame.index[10], frame.index[100])) == 88


# ---------------------------------------------------------------------------
# CsvDataProvider
# ---------------------------------------------------------------------------


def test_csv_provider_reads_a_written_csv(tmp_path: Path, frame: pd.DataFrame) -> None:
    path = tmp_path / "BTC_USDT-1h.csv"
    frame.to_csv(path, index_label="timestamp")
    provider = CsvDataProvider(tmp_path)
    assert provider.path_for(SYMBOL, TIMEFRAME) == path
    assert (
        CsvDataProvider(tmp_path, suffix=".txt").path_for(SYMBOL, TIMEFRAME).name
        == "BTC_USDT-1h.txt"
    )

    window = provider.fetch_ohlcv(SYMBOL, TIMEFRAME, frame.index[10], frame.index[20])
    assert len(window) == 11
    assert window.index[0] == frame.index[10]
    assert window.index.name == "timestamp"
    assert str(window.index.tz) == "UTC"
    assert all(dtype == np.dtype("float64") for dtype in window.dtypes)


def test_csv_provider_raises_when_the_file_is_missing(tmp_path: Path) -> None:
    provider = CsvDataProvider(tmp_path)
    with pytest.raises(InsufficientDataError) as excinfo:
        provider.fetch_ohlcv(
            SYMBOL,
            TIMEFRAME,
            pd.Timestamp("2023-01-01T00:00:00Z"),
            pd.Timestamp("2023-01-02T00:00:00Z"),
        )
    assert "does not exist" in str(excinfo.value)


def test_csv_provider_drives_the_loader_without_any_network(tmp_path: Path) -> None:
    frame = make_ohlcv(120)
    frame.to_csv(tmp_path / "BTC_USDT-1h.csv", index_label="timestamp")
    loader = OHLCVLoader(
        "binance", tmp_path / "cache", provider=CsvDataProvider(tmp_path), allow_network=True
    )
    window = loader.load(SYMBOL, TIMEFRAME, frame.index[0], frame.index[119])
    assert len(window) == 120
    assert loader.cache.exists("binance", SYMBOL, TIMEFRAME) is True


def test_providers_satisfy_the_runtime_protocol(tmp_path: Path) -> None:
    assert isinstance(CsvDataProvider(tmp_path), MarketDataProvider)
    assert isinstance(FakeProvider(make_ohlcv(5)), MarketDataProvider)
    assert not isinstance(object(), MarketDataProvider)


# ---------------------------------------------------------------------------
# CcxtDataProvider -- both branches pinned, always offline
# ---------------------------------------------------------------------------


def test_ccxt_provider_without_ccxt_raises_data_download_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "ccxt", None)
    with pytest.raises(DataDownloadError) as excinfo:
        CcxtDataProvider("binance")
    assert "ccxt is not installed" in str(excinfo.value)
    assert "trading-backtest[exchange]" in str(excinfo.value)


def test_ccxt_provider_construction_when_ccxt_is_installed() -> None:
    pytest.importorskip("ccxt")
    provider = CcxtDataProvider("binance", market="spot", rate_limit_ms=250)
    assert provider.exchange_id == "binance"
    assert provider.market == "spot"
    assert provider.rate_limit_ms == 250


def test_ccxt_provider_rejects_an_unknown_exchange() -> None:
    pytest.importorskip("ccxt")
    with pytest.raises(DataDownloadError) as excinfo:
        CcxtDataProvider("not-an-exchange")
    assert "unsupported ccxt exchange" in str(excinfo.value)


class _FakeCcxtClient:
    """Minimal ccxt-like client returning full pages of candles."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.options: dict[str, Any] = dict(config)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str | None = None,
        since: int | None = None,
        limit: int | None = None,
    ) -> list[list[float]]:
        assert since is not None and limit is not None
        return [
            [since + index * 3_600_000, 100.0, 101.0, 99.0, 100.5, 10.0] for index in range(limit)
        ]


class _FailingCcxtClient(_FakeCcxtClient):
    def fetch_ohlcv(self, *args: Any, **kwargs: Any) -> list[list[float]]:
        raise RuntimeError("boom")


class _EmptyCcxtClient(_FakeCcxtClient):
    def fetch_ohlcv(self, *args: Any, **kwargs: Any) -> list[list[float]]:
        return []


@pytest.fixture
def fake_ccxt(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    module = SimpleNamespace(binance=_FakeCcxtClient)
    monkeypatch.setitem(sys.modules, "ccxt", module)
    return module


def test_ccxt_provider_paginates(fake_ccxt: SimpleNamespace) -> None:
    provider = CcxtDataProvider("binance")
    start = pd.Timestamp("2023-01-01T00:00:00Z")
    until = start + pd.Timedelta(hours=1500)
    frame = provider.fetch_ohlcv(SYMBOL, TIMEFRAME, start, until)
    assert len(frame) == 1501
    assert frame.index[0] == start
    assert frame.index[-1] == until
    assert all(dtype == np.dtype("float64") for dtype in frame.dtypes)
    assert frame.index.name == "timestamp"


def test_ccxt_provider_stops_on_a_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    class ShortPageClient(_FakeCcxtClient):
        def fetch_ohlcv(
            self,
            symbol: str,
            timeframe: str | None = None,
            since: int | None = None,
            limit: int | None = None,
        ) -> list[list[float]]:
            assert since is not None and limit is not None
            calls.append(since)
            return super().fetch_ohlcv(
                symbol, timeframe=timeframe, since=since, limit=min(limit, 5)
            )

    monkeypatch.setitem(sys.modules, "ccxt", SimpleNamespace(binance=ShortPageClient))
    provider = CcxtDataProvider("binance")
    start = pd.Timestamp("2023-01-01T00:00:00Z")
    frame = provider.fetch_ohlcv(SYMBOL, TIMEFRAME, start, start + pd.Timedelta(hours=100))
    assert len(calls) == 1
    assert len(frame) == 5


def test_ccxt_provider_wraps_exchange_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ccxt", SimpleNamespace(binance=_FailingCcxtClient))
    provider = CcxtDataProvider("binance")
    with pytest.raises(DataDownloadError) as excinfo:
        provider.fetch_ohlcv(
            SYMBOL,
            TIMEFRAME,
            pd.Timestamp("2023-01-01T00:00:00Z"),
            pd.Timestamp("2023-01-02T00:00:00Z"),
        )
    assert "failed to download" in str(excinfo.value)
    assert "boom" in str(excinfo.value)


def test_ccxt_provider_raises_when_no_candle_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "ccxt", SimpleNamespace(binance=_EmptyCcxtClient))
    provider = CcxtDataProvider("binance")
    with pytest.raises(InsufficientDataError):
        provider.fetch_ohlcv(
            SYMBOL,
            TIMEFRAME,
            pd.Timestamp("2023-01-01T00:00:00Z"),
            pd.Timestamp("2023-01-02T00:00:00Z"),
        )


def test_ccxt_provider_sets_the_futures_market(fake_ccxt: SimpleNamespace) -> None:
    provider = CcxtDataProvider("binance", market="futures")
    assert provider.client.options["defaultType"] == "future"


def test_loader_builds_a_ccxt_provider_lazily(
    cache_dir: Path, frame: pd.DataFrame, fake_ccxt: SimpleNamespace
) -> None:
    loader = OHLCVLoader("binance", cache_dir, allow_network=True)
    loader.cache.write("binance", SYMBOL, TIMEFRAME, frame)
    start, until = frame.index[0], frame.index[10]
    window = loader.load(SYMBOL, TIMEFRAME, start, until)
    assert len(window) == 11
    assert isinstance(loader.provider, CcxtDataProvider)


def test_cache_instance_can_be_injected(cache_dir: Path, frame: pd.DataFrame) -> None:
    cache = OHLCVCache(cache_dir, fmt="csv")
    loader = OHLCVLoader("binance", cache_dir, fmt="csv", allow_network=False, cache=cache)
    assert loader.cache is cache
    loader.cache.write("binance", SYMBOL, TIMEFRAME, frame)
    assert (cache_dir / "binance" / "BTC_USDT" / "1h.csv").is_file()
    with pytest.raises(ConfigError):
        OHLCVLoader("binance", cache_dir, fmt="nope", allow_network=False)
