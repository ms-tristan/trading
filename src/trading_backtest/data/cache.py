"""On-disk OHLCV cache (parquet or csv), one file per exchange/symbol/timeframe."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Literal

import pandas as pd

from trading_backtest.core.constants import OHLCV_INDEX_NAME
from trading_backtest.core.errors import ConfigError
from trading_backtest.data.validation import ensure_ohlcv

__all__ = ["OHLCVCache"]

CacheFormat = Literal["parquet", "csv"]

_EXTENSIONS: dict[str, str] = {"parquet": "parquet", "csv": "csv"}


class OHLCVCache:
    """Read/write a local OHLCV cache laid out as ``<cache_dir>/<exchange>/<SYMBOL>/<tf>.<ext>``."""

    def __init__(self, cache_dir: Path, fmt: CacheFormat = "parquet") -> None:
        if fmt not in _EXTENSIONS:
            raise ConfigError(
                f"unsupported cache format: {fmt!r} (expected one of {sorted(_EXTENSIONS)})"
            )
        self.cache_dir = Path(cache_dir)
        self.fmt: str = fmt

    # -- layout ---------------------------------------------------------------
    @property
    def extension(self) -> str:
        """File extension used by this cache."""
        return _EXTENSIONS[self.fmt]

    def path_for(self, exchange: str, symbol: str, timeframe: str) -> Path:
        """Return the cache file path of one ``(exchange, symbol, timeframe)`` triple."""
        folder = str(symbol).replace("/", "_").upper()
        return self.cache_dir / str(exchange).lower() / folder / f"{timeframe}.{self.extension}"

    def exists(self, exchange: str, symbol: str, timeframe: str) -> bool:
        """``True`` when a cache file is present for that triple."""
        return self.path_for(exchange, symbol, timeframe).is_file()

    # -- io -------------------------------------------------------------------
    def read(self, exchange: str, symbol: str, timeframe: str) -> pd.DataFrame | None:
        """Read a cached frame, or ``None`` when the entry does not exist.

        The returned frame is contract-conformant: ``float64`` values, ascending
        tz-aware UTC ``DatetimeIndex`` named ``"timestamp"``.
        """
        path = self.path_for(exchange, symbol, timeframe)
        if not path.is_file():
            return None
        if self.fmt == "parquet":
            frame = pd.read_parquet(path)
        else:
            frame = pd.read_csv(path, index_col=OHLCV_INDEX_NAME, parse_dates=True)
        return ensure_ohlcv(frame, name=str(path))

    def write(self, exchange: str, symbol: str, timeframe: str, data: pd.DataFrame) -> Path:
        """Write ``data`` into the cache (parents are created) and return the path."""
        frame = ensure_ohlcv(data, name=f"{symbol} {timeframe}")
        path = self.path_for(exchange, symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.fmt == "parquet":
            frame.to_parquet(path, index=True)
        else:
            frame.to_csv(path, index_label=OHLCV_INDEX_NAME)
        return path

    @staticmethod
    def merge(existing: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
        """Concatenate two frames, keeping the newest row for shared timestamps.

        The result is sorted by timestamp and has no duplicated index entry.

        Declared as a ``@staticmethod`` so that both ``cache.merge(a, b)`` and
        ``OHLCVCache.merge(a, b)`` work.
        """
        if existing is None or len(existing) == 0:
            return ensure_ohlcv(new, name="new")
        if len(new) == 0:
            return ensure_ohlcv(existing, name="existing")
        combined = pd.concat(
            [ensure_ohlcv(existing, name="existing"), ensure_ohlcv(new, name="new")]
        )
        combined = combined[~combined.index.duplicated(keep="last")]
        return combined.sort_index()

    def clear(self, exchange: str | None = None) -> int:
        """Delete cached files and return how many were removed.

        With ``exchange`` given only that exchange folder is cleared, otherwise
        the whole cache directory is.  The operation is idempotent.
        """
        root = self.cache_dir if exchange is None else self.cache_dir / str(exchange).lower()
        if not root.is_dir():
            return 0
        deleted = 0
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix[1:] in set(_EXTENSIONS.values()):
                path.unlink()
                deleted += 1
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                # A non-empty directory (a foreign file lives in it) is kept alive on purpose.
                with contextlib.suppress(OSError):
                    path.rmdir()
        return deleted
