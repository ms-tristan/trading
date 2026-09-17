"""Market-data access: providers, caching loader and window slicing.

The loader is the only place allowed to read/write the OHLCV cache and the only
place that can trigger a download.  Every network call goes through a
:class:`MarketDataProvider`, which makes the whole layer testable offline by
injecting :class:`CsvDataProvider` or a fake provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

import pandas as pd

from trading_backtest.core.constants import (
    OHLCV_INDEX_NAME,
    UTC,
    candle_delta,
    timeframe_minutes,
)
from trading_backtest.core.errors import (
    ConfigError,
    DataDownloadError,
    InsufficientDataError,
)
from trading_backtest.data.cache import CacheFormat, OHLCVCache
from trading_backtest.data.validation import ensure_ohlcv, validate_ohlcv

__all__ = [
    "CcxtDataProvider",
    "CsvDataProvider",
    "MarketDataProvider",
    "OHLCVLoader",
]

#: How many candles are requested per exchange call.
_PAGE_LIMIT = 1000


def _as_utc_timestamp(value: Any, *, field_name: str) -> pd.Timestamp:
    """Parse ``value`` as a tz-aware timestamp converted to UTC."""
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid {field_name}: {value!r} is not a valid timestamp") from exc
    if timestamp is None or pd.isna(timestamp):
        raise ConfigError(f"invalid {field_name}: {value!r} is not a valid timestamp")
    if timestamp.tz is None:
        raise ConfigError(
            f"{field_name} must be timezone-aware (UTC), got the naive datetime {value!r}"
        )
    return timestamp.tz_convert(UTC)


@runtime_checkable
class MarketDataProvider(Protocol):
    """Anything able to deliver OHLCV candles for a ``[since, until]`` window."""

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: pd.Timestamp,
        until: pd.Timestamp,
    ) -> pd.DataFrame:  # pragma: no cover - protocol definition
        ...


class CsvDataProvider:
    """Offline provider reading one CSV file per symbol/timeframe.

    Files are named ``<SYMBOL>-<timeframe><suffix>`` (``/`` replaced by ``_``,
    upper-cased), e.g. ``BTC_USDT-1h.csv``.  This provider never touches the
    network.
    """

    def __init__(self, directory: Path, *, suffix: str = ".csv") -> None:
        self.directory = Path(directory)
        self.suffix = suffix

    def path_for(self, symbol: str, timeframe: str) -> Path:
        """Return the CSV path backing ``symbol``/``timeframe``."""
        return self.directory / f"{str(symbol).replace('/', '_').upper()}-{timeframe}{self.suffix}"

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: pd.Timestamp,
        until: pd.Timestamp,
    ) -> pd.DataFrame:
        """Read the CSV file and return the ``[since, until]`` slice."""
        path = self.path_for(symbol, timeframe)
        if not path.is_file():
            raise InsufficientDataError(
                f"no CSV data for {symbol} {timeframe}: {path} does not exist"
            )
        frame = pd.read_csv(path, index_col=OHLCV_INDEX_NAME, parse_dates=True)
        frame = ensure_ohlcv(frame, name=str(path))
        start = pd.Timestamp(since)
        start = start.tz_localize(UTC) if start.tz is None else start.tz_convert(UTC)
        end = pd.Timestamp(until)
        end = end.tz_localize(UTC) if end.tz is None else end.tz_convert(UTC)
        return frame.loc[(frame.index >= start) & (frame.index <= end)]


class CcxtDataProvider:
    """Live provider backed by ``ccxt`` (imported lazily, only when constructed).

    The dependency is optional: constructing this provider without ``ccxt``
    installed raises :class:`~trading_backtest.core.errors.DataDownloadError`
    with the pip extra to install.
    """

    def __init__(
        self,
        exchange: str = "binance",
        *,
        market: str = "spot",
        rate_limit_ms: int = 200,
    ) -> None:
        try:
            import ccxt  # optional dependency, imported on demand
        except ImportError as exc:
            raise DataDownloadError(
                "ccxt is not installed: pip install 'trading-backtest[exchange]'"
            ) from exc

        exchange_class = getattr(ccxt, str(exchange), None)
        if exchange_class is None:
            raise DataDownloadError(
                f"unsupported ccxt exchange: {exchange!r} (see ccxt.exchanges for the supported ids)"
            )
        self.exchange_id = str(exchange)
        self.market = market
        self.rate_limit_ms = rate_limit_ms
        try:
            self.client = exchange_class({"enableRateLimit": True, "timeout": 30_000})
        except Exception as exc:  # pragma: no cover - exchange constructor failure
            raise DataDownloadError(f"cannot initialise ccxt exchange {exchange!r}: {exc}") from exc
        options = getattr(self.client, "options", None)
        if market == "futures" and isinstance(options, dict):
            options["defaultType"] = "future"

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: pd.Timestamp,
        until: pd.Timestamp,
    ) -> pd.DataFrame:
        """Download the ``[since, until]`` window, paginating over the exchange limit."""
        start = _as_utc_timestamp(since, field_name="since")
        end = _as_utc_timestamp(until, field_name="until")
        step_ms = timeframe_minutes(timeframe) * 60_000
        since_ms = int(start.timestamp() * 1000)
        until_ms = int(end.timestamp() * 1000)

        rows: list[list[Any]] = []
        cursor = since_ms
        while cursor < until_ms:
            try:
                batch = self.client.fetch_ohlcv(
                    symbol, timeframe=timeframe, since=cursor, limit=_PAGE_LIMIT
                )
            except Exception as exc:
                raise DataDownloadError(
                    f"failed to download {symbol} {timeframe} from {self.exchange_id}: {exc}"
                ) from exc
            if not batch:
                break
            rows.extend(batch)
            last_ms = int(batch[-1][0])
            next_cursor = max(last_ms + step_ms, cursor + step_ms)
            if next_cursor <= cursor:  # pragma: no cover - defensive: no progress
                break
            cursor = next_cursor
            if len(batch) < _PAGE_LIMIT:
                break

        if not rows:
            raise InsufficientDataError(
                f"no data returned by {self.exchange_id} for {symbol} {timeframe} "
                f"between {start.isoformat()} and {end.isoformat()}"
            )
        frame = pd.DataFrame(
            rows, columns=[OHLCV_INDEX_NAME, "open", "high", "low", "close", "volume"]
        )
        frame[OHLCV_INDEX_NAME] = pd.to_datetime(frame[OHLCV_INDEX_NAME], unit="ms", utc=True)
        frame = frame.set_index(OHLCV_INDEX_NAME).astype("float64")
        frame = frame.loc[(frame.index >= start) & (frame.index <= end)]
        return ensure_ohlcv(frame, name=f"{self.exchange_id} {symbol} {timeframe}")


class OHLCVLoader:
    """Cache-first OHLCV loader.

    ``load`` serves the requested window from the local cache; when the cache does
    not cover it, the missing data is downloaded through ``provider`` (a
    :class:`CcxtDataProvider` by default) and merged back into the cache.  With
    ``allow_network=False`` the provider is *never* called and a cache miss
    raises :class:`~trading_backtest.core.errors.InsufficientDataError`.
    """

    def __init__(
        self,
        exchange: str,
        cache_dir: Path,
        *,
        fmt: str = "parquet",
        allow_network: bool = True,
        validate: bool = True,
        max_gap_factor: float = 3.0,
        max_missing_ratio: float = 0.0,
        provider: MarketDataProvider | None = None,
        cache: OHLCVCache | None = None,
    ) -> None:
        self.exchange = str(exchange)
        self.cache = (
            cache
            if cache is not None
            else OHLCVCache(Path(cache_dir), fmt=cast("CacheFormat", fmt))
        )
        self.allow_network = allow_network
        self.validate = validate
        self.max_gap_factor = max_gap_factor
        self.max_missing_ratio = max_missing_ratio
        self._provider = provider

    # -- helpers --------------------------------------------------------------
    @property
    def provider(self) -> MarketDataProvider:
        """The download provider, built lazily so that ``ccxt`` stays optional."""
        if self._provider is None:
            self._provider = CcxtDataProvider(self.exchange)
        return self._provider

    @staticmethod
    def _covers(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> bool:
        if frame is None or frame.empty:
            return False
        return bool(frame.index[0] <= start and frame.index[-1] >= end)

    def _window(
        self,
        symbol: str,
        timeframe: str,
        frame: pd.DataFrame,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        window = frame.loc[(frame.index >= start) & (frame.index <= end)]
        return ensure_ohlcv(window, name=f"{symbol} {timeframe}")

    # -- public api -----------------------------------------------------------
    def load(
        self,
        symbol: str,
        timeframe: str,
        since: Any,
        until: Any,
    ) -> pd.DataFrame:
        """Return the candles of ``[since, until]`` (inclusive), cache first."""
        start = _as_utc_timestamp(since, field_name="since")
        end = _as_utc_timestamp(until, field_name="until")
        if start >= end:
            raise ConfigError(
                f"since ({start.isoformat()}) must be strictly before until ({end.isoformat()})"
            )
        candle_delta(timeframe)  # validates the timeframe (ConfigError otherwise)

        cached = self.cache.read(self.exchange, symbol, timeframe)
        frame = cached
        if cached is None or not self._covers(cached, start, end):
            if not self.allow_network:
                available = (
                    "no cached data"
                    if cached is None or cached.empty
                    else f"cached range {cached.index[0].isoformat()} .. {cached.index[-1].isoformat()}"
                )
                raise InsufficientDataError(
                    f"cache for {symbol} {timeframe} does not cover "
                    f"{start.isoformat()} .. {end.isoformat()} ({available}) and network access "
                    "is disabled"
                )
            downloaded = self.download(symbol, timeframe, start, end)
            merged = OHLCVCache.merge(cached, downloaded)
            self.cache.write(self.exchange, symbol, timeframe, merged)
            frame = merged

        if frame is None or frame.empty:  # pragma: no cover - a covered cache is never empty
            raise InsufficientDataError(f"no candle available for {symbol} {timeframe}")

        window = self._window(symbol, timeframe, frame, start, end)
        if window.empty:
            raise InsufficientDataError(
                f"no candle for {symbol} {timeframe} inside "
                f"{start.isoformat()} .. {end.isoformat()}"
            )
        if self.validate:
            validate_ohlcv(
                window,
                timeframe=timeframe,
                max_gap_factor=self.max_gap_factor,
                max_missing_ratio=self.max_missing_ratio,
                raise_on_error=True,
            )
        return window

    def download(self, symbol: str, timeframe: str, since: Any, until: Any) -> pd.DataFrame:
        """Download the window through the provider, without touching the cache."""
        start = _as_utc_timestamp(since, field_name="since")
        end = _as_utc_timestamp(until, field_name="until")
        if start >= end:
            raise ConfigError(
                f"since ({start.isoformat()}) must be strictly before until ({end.isoformat()})"
            )
        frame = self.provider.fetch_ohlcv(symbol, timeframe, start, end)
        frame = ensure_ohlcv(frame, name=f"{symbol} {timeframe}")
        if frame.empty:
            raise InsufficientDataError(
                f"provider returned no candle for {symbol} {timeframe} "
                f"between {start.isoformat()} and {end.isoformat()}"
            )
        return frame

    def load_cached(
        self,
        symbol: str,
        timeframe: str,
        since: Any | None = None,
        until: Any | None = None,
    ) -> pd.DataFrame:
        """Return cached candles only (optionally windowed), never downloading."""
        frame = self.cache.read(self.exchange, symbol, timeframe)
        if frame is None or frame.empty:
            raise InsufficientDataError(
                f"no cached data for {symbol} {timeframe} "
                f"({self.cache.path_for(self.exchange, symbol, timeframe)})"
            )
        if since is not None:
            frame = frame.loc[frame.index >= _as_utc_timestamp(since, field_name="since")]
        if until is not None:
            frame = frame.loc[frame.index <= _as_utc_timestamp(until, field_name="until")]
        if frame.empty:
            raise InsufficientDataError(f"no cached candle for {symbol} {timeframe} in that window")
        return frame

    def available_range(self, symbol: str, timeframe: str) -> tuple[pd.Timestamp, pd.Timestamp]:
        """Return the ``(first, last)`` cached timestamps for that symbol/timeframe."""
        frame = self.cache.read(self.exchange, symbol, timeframe)
        if frame is None or frame.empty:
            raise InsufficientDataError(f"no cached data for {symbol} {timeframe}")
        return pd.Timestamp(frame.index[0]), pd.Timestamp(frame.index[-1])
