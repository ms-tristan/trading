"""Market-data layer: quality control, on-disk cache, providers and generators.

Public API::

    from trading_backtest.data import (
        OHLCVCache, OHLCVLoader, CsvDataProvider, CcxtDataProvider,
        ensure_ohlcv, validate_ohlcv, make_ohlcv, make_trending_ohlcv,
    )
"""

from __future__ import annotations

from trading_backtest.data.cache import OHLCVCache
from trading_backtest.data.loader import (
    CcxtDataProvider,
    CsvDataProvider,
    MarketDataProvider,
    OHLCVLoader,
)
from trading_backtest.data.synthetic import (
    make_flat_ohlcv,
    make_ohlcv,
    make_trending_ohlcv,
)
from trading_backtest.data.validation import (
    DataQualityReport,
    ensure_ohlcv,
    validate_ohlcv,
)

__all__ = [
    "CcxtDataProvider",
    "CsvDataProvider",
    "DataQualityReport",
    "MarketDataProvider",
    "OHLCVCache",
    "OHLCVLoader",
    "ensure_ohlcv",
    "make_flat_ohlcv",
    "make_ohlcv",
    "make_trending_ohlcv",
    "validate_ohlcv",
]
