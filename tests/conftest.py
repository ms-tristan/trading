"""Shared pytest configuration and fixtures for the whole test-suite.

Owned by the core/data work-package: every other package may use these fixtures
but must not edit this file.

Design rules:

* everything is **offline** and deterministic (synthetic data only);
* tests marked ``network`` are skipped unless ``--run-network`` is given;
* this module never imports ``trading_backtest.strategy``, ``.validation``,
  ``.metrics`` or ``.reporting`` (dependency direction: core/config/data first).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_backtest.config import AppConfig
from trading_backtest.core.constants import OHLCV_INDEX_NAME, UTC
from trading_backtest.core.models import (
    BacktestResult,
    Direction,
    ExitReason,
    RunnerFn,
    TradeRecord,
)
from trading_backtest.data.synthetic import make_flat_ohlcv, make_ohlcv, make_trending_ohlcv

# ---------------------------------------------------------------------------
# command line / markers
# ---------------------------------------------------------------------------

NETWORK_SKIP_REASON = "network test skipped: use --run-network"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--run-network`` flag (network tests are opt-in)."""
    parser.addoption(
        "--run-network",
        action="store_true",
        default=False,
        help="run tests marked 'network' (they need real internet access)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``network``-marked tests unless ``--run-network`` is given."""
    if config.getoption("--run-network"):
        return
    skip_network = pytest.mark.skip(reason=NETWORK_SKIP_REASON)
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip_network)


# ---------------------------------------------------------------------------
# market data fixtures (synthetic, offline, deterministic)
# ---------------------------------------------------------------------------


@pytest.fixture
def ohlcv_frame() -> pd.DataFrame:
    """500 candles of deterministic random-walk OHLCV data."""
    return make_ohlcv(500)


@pytest.fixture
def trending_frame() -> pd.DataFrame:
    """600 candles of deterministic oscillating/trending OHLCV data."""
    return make_trending_ohlcv(600)


@pytest.fixture
def flat_frame() -> pd.DataFrame:
    """100 perfectly flat candles."""
    return make_flat_ohlcv(100)


@pytest.fixture
def synthetic_csv(tmp_path: Path) -> Path:
    """Write ``make_trending_ohlcv(600)`` as ``BTC_USDT-1h.csv`` and return its path.

    Used by the offline CLI tests (CSV data provider).
    """
    path = tmp_path / "BTC_USDT-1h.csv"
    make_trending_ohlcv(600).to_csv(path, index_label=OHLCV_INDEX_NAME)
    return path


@pytest.fixture
def equity_series() -> pd.Series:
    """A deterministic, tz-aware ``float64`` equity curve of 200 points."""
    index = pd.date_range(
        "2024-01-01T00:00:00Z", periods=200, freq="h", tz=UTC, name=OHLCV_INDEX_NAME
    )
    values = 10_000.0 + np.arange(200, dtype="float64") * 1.5
    return pd.Series(values, index=index, name="equity", dtype="float64")


# ---------------------------------------------------------------------------
# configuration fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """An empty OHLCV cache directory inside the test's tmp_path."""
    return tmp_path / "cache"


@pytest.fixture
def app_config(cache_dir: Path) -> AppConfig:
    """A deterministic, offline-first application configuration."""
    return AppConfig(
        data={
            "cache_dir": cache_dir,
            "allow_network": False,
            "validate": True,
        },
        backtest={"initial_balance": 10_000.0},
    )


# ---------------------------------------------------------------------------
# validation-layer helper: a deterministic runner, no engine required
# ---------------------------------------------------------------------------

_STUB_TRADES: tuple[tuple[float, float, Direction, ExitReason], ...] = (
    (0.10, 10.0, Direction.LONG, ExitReason.SIGNAL),
    (0.40, -5.0, Direction.SHORT, ExitReason.STOP_LOSS),
    (0.70, 25.0, Direction.LONG, ExitReason.TAKE_PROFIT),
)


def _params_id(params: dict[str, object] | None) -> str:
    if not params:
        return "default"
    return "|".join(f"{key}={params[key]}" for key in sorted(params))


@pytest.fixture
def runner_stub() -> RunnerFn:
    """A :data:`~trading_backtest.core.models.RunnerFn` built without any engine.

    Given any OHLCV frame it returns a deterministic
    :class:`~trading_backtest.core.models.BacktestResult` with three trades and a
    flat-ish equity curve (one point per input candle).  It only relies on
    ``trading_backtest.core``, so the validation layer (walk-forward, Monte
    Carlo, robustness) can be tested before the engine exists.
    """
    initial_balance = 10_000.0

    def _runner(data: pd.DataFrame, params: dict[str, object] | None = None) -> BacktestResult:
        index = pd.DatetimeIndex(data.index)
        count = len(index)
        values = np.full(count, initial_balance, dtype="float64")
        trades: list[TradeRecord] = []
        params_id = _params_id(params)
        if count >= 4:
            for fraction, pnl, direction, reason in _STUB_TRADES:
                position = min(max(int(count * fraction), 0), count - 2)
                exit_position = min(position + 1, count - 1)
                entry_time = index[position]
                exit_time = index[exit_position]
                entry_price = float(data["close"].iloc[position])
                pnl_pct = pnl / initial_balance
                exit_price = entry_price * (1.0 + pnl_pct)
                trades.append(
                    TradeRecord(
                        entry_time=entry_time,
                        exit_time=exit_time,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        size=1.0,
                        direction=direction,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        fees=0.1,
                        exit_reason=reason,
                        duration_minutes=float((exit_time - entry_time).total_seconds() / 60.0),
                        stop_price=entry_price * 0.98,
                        take_profit_price=entry_price * 1.02,
                        params_id=params_id,
                    )
                )
                values[exit_position:] += pnl
        equity = pd.Series(values, index=index, name="equity", dtype="float64")
        last = index[-1] if count else pd.Timestamp(0, unit="s", tz=UTC)
        first = index[0] if count else pd.Timestamp(0, unit="s", tz=UTC)
        return BacktestResult(
            strategy_name="stub",
            symbol="BTC/USDT",
            timeframe="1h",
            start=first,
            end=last,
            initial_balance=initial_balance,
            final_balance=float(values[-1]) if count else initial_balance,
            trades=trades,
            equity_curve=equity,
            params=dict(params or {}),
            metadata={"runner": "runner_stub", "params_id": params_id},
        )

    return _runner
