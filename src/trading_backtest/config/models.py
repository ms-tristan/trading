"""Typed configuration layer (pydantic v2) for the whole application.

Every layer receives a sub-configuration object; :class:`AppConfig` is the
aggregate root used by the CLI and by ``trading_backtest.config.loader``.
Environment variables use the ``TB_`` prefix and ``__`` as nested delimiter,
e.g. ``TB_DATA__TIMEFRAME=4h`` or ``TB_BACKTEST__INITIAL_BALANCE=2500``.
"""

from __future__ import annotations

import warnings
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_backtest.core.constants import (
    DEFAULT_FEE_RATE,
    DEFAULT_INITIAL_BALANCE,
    DEFAULT_SLIPPAGE,
    DEFAULT_STAKE_CURRENCY,
    DEFAULT_TIMEFRAME,
    SUPPORTED_TIMEFRAMES,
    UTC,
)

__all__ = [
    "AppConfig",
    "BacktestConfig",
    "BenchmarkConfig",
    "DataConfig",
    "ExchangeConfig",
    "ReportingConfig",
    "StrategyConfig",
    "ValidationConfig",
]

#: Types accepted in a strategy parameter grid.
ParamValue = float | int | bool | str

#: Formats the report can be rendered into.
ReportFormat = Literal["markdown", "json"]
_DEFAULT_REPORT_FORMATS: list[ReportFormat] = ["markdown", "json"]


class ExchangeConfig(BaseModel):
    """Exchange / market-microstructure settings."""

    model_config = {"extra": "forbid"}

    name: str = "binance"
    market: Literal["spot", "futures"] = "spot"
    quote_currency: str = DEFAULT_STAKE_CURRENCY
    fee_rate: float = Field(default=DEFAULT_FEE_RATE, ge=0)
    slippage: float = Field(default=DEFAULT_SLIPPAGE, ge=0)
    rate_limit_ms: int = Field(default=200, ge=0)


with warnings.catch_warnings():
    # ``validate`` is part of the public configuration contract, but pydantic
    # warns because ``BaseModel.validate`` exists as a deprecated helper.
    warnings.simplefilter("ignore", UserWarning)

    class DataConfig(BaseModel):
        """Market data location, format and quality gates."""

        model_config = {"extra": "forbid"}

        data_dir: Path = Path("data")
        cache_dir: Path = Path("data/cache")
        format: Literal["parquet", "csv"] = "parquet"
        timeframe: str = DEFAULT_TIMEFRAME
        start: datetime | None = None
        end: datetime | None = None
        allow_network: bool = True
        validate: bool = True  # type: ignore[assignment]  # shadows BaseModel.validate
        max_gap_factor: float = Field(default=3.0, ge=1)
        max_missing_ratio: float = Field(default=0.0, ge=0, le=1)

        @field_validator("timeframe")
        @classmethod
        def _check_timeframe(cls, value: str) -> str:
            if value not in SUPPORTED_TIMEFRAMES:
                supported = ", ".join(sorted(SUPPORTED_TIMEFRAMES))
                raise ValueError(f"unsupported timeframe: {value!r} (supported: {supported})")
            return value


class StrategyConfig(BaseModel):
    """Strategy selection and its default parameters."""

    model_config = {"extra": "forbid"}

    name: str = "basic"
    timeframe: str = DEFAULT_TIMEFRAME
    params: dict[str, ParamValue] = Field(default_factory=dict)


class BacktestConfig(BaseModel):
    """Execution settings of the backtest engine."""

    model_config = {"extra": "forbid"}

    initial_balance: float = Field(default=DEFAULT_INITIAL_BALANCE, gt=0)
    stake_amount: float | None = Field(default=None, gt=0)
    #: Values greater than 1 are accepted by the configuration, but the engine
    #: only ever holds a single position at a time.
    max_open_trades: int = Field(default=1, ge=1)
    fee_rate: float = Field(default=DEFAULT_FEE_RATE, ge=0)
    slippage: float = Field(default=DEFAULT_SLIPPAGE, ge=0)
    allow_short: bool = False
    compute_metrics: bool = True


class BenchmarkConfig(BaseModel):
    """Passive benchmark settings (buy & hold, cash, risk-free placement, random entry).

    ``risk_free_rate`` defaults to ``0.0`` on purpose: that value preserves the
    historical behaviour byte-for-byte, so no previously asserted Sharpe or
    Sortino ratio moves. ``0.05`` is the recommended realistic value for
    2023-2025 US T-bill runs and is set explicitly where such a run is
    configured. The ``10_000`` bound on ``n_random_simulations`` is a literal
    duplicated from the metrics layer on purpose (config and metrics are
    sibling layers, so the config layer never imports the metrics layer).
    """

    model_config = {"extra": "forbid"}

    enabled: bool = True
    variant: Literal["buy_and_hold", "cash", "risk_free", "random_entry", "none"] = "buy_and_hold"
    risk_free_rate: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Annualised risk-free rate as a fraction (0.05 = 5 %/yr), subtracted "
            "from the annualised mean return in sharpe_ratio/sortino_ratio and "
            "compounded by the risk_free benchmark variant. 0.0 preserves the "
            "historical behaviour; 0.05 is the realistic 2023-2025 US T-bill level."
        ),
    )
    n_random_simulations: int = Field(
        default=1000,
        ge=1,
        le=10_000,
        description=(
            "Number of random-entry simulations behind the random_entry benchmark distribution."
        ),
    )
    random_entry_seed: int = Field(
        default=42,
        ge=0,
        description=("Seed of the random_entry simulations (same seed = same distribution)."),
    )


class ValidationConfig(BaseModel):
    """Statistical validation layer settings (walk-forward, Monte Carlo, robustness)."""

    model_config = {"extra": "forbid"}

    n_windows: int = Field(default=5, ge=1)
    in_sample_ratio: float = Field(default=0.7, gt=0, lt=1)
    mode: Literal["rolling", "anchored"] = "rolling"
    purge_candles: int = Field(default=0, ge=0)
    n_monte_carlo: int = Field(default=1000, ge=1)
    monte_carlo_method: Literal["trade_resample", "bootstrap_equity"] = "trade_resample"
    random_seed: int = 42
    robustness_metric: str = "sharpe_ratio"
    robustness_max_combinations: int = Field(default=512, ge=1)
    robustness_grid: dict[str, list[ParamValue]] = Field(default_factory=dict)


class ReportingConfig(BaseModel):
    """Report rendering settings."""

    model_config = {"extra": "forbid"}

    output_dir: Path = Path("reports")
    basename: str = "report"
    formats: list[ReportFormat] = Field(default_factory=lambda: list(_DEFAULT_REPORT_FORMATS))
    title: str = "Backtest Report"
    include_trades: bool = True
    trade_limit: int = Field(default=50, ge=0)


class AppConfig(BaseSettings):
    """Aggregate configuration root.

    Field values follow this precedence: explicit keyword arguments, environment
    variables (``TB_`` prefixed, ``__`` delimited) and finally the defaults.
    """

    model_config = SettingsConfigDict(
        env_prefix="TB_",
        env_nested_delimiter="__",
        env_file=None,
        extra="forbid",
        validate_default=True,
    )

    project_name: str = "trading-backtest"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    benchmark: BenchmarkConfig = Field(default_factory=BenchmarkConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)

    @property
    def timezone(self) -> str:
        """Every timestamp handled by the package is UTC."""
        return UTC
