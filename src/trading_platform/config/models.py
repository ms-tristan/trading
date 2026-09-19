"""Typed configuration layer (pydantic v2) for the whole application.

Every layer receives a sub-configuration object; :class:`AppConfig` is the
aggregate root used by the CLI and by ``trading_platform.config.loader``.
Environment variables use the ``TB_`` prefix and ``__`` as nested delimiter,
e.g. ``TB_DATA__TIMEFRAME=4h`` or ``TB_BACKTEST__INITIAL_BALANCE=2500``.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_platform.core.constants import (
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
    "ForecastConfig",
    "MonitoringConfig",
    "ProfileConfig",
    "RealtimeConfig",
    "ReportingConfig",
    "RiskLimitsConfig",
    "StrategyConfig",
    "ValidationConfig",
    "resolve_platform_initial_balance",
]

#: Types accepted in a strategy parameter grid.
ParamValue = float | int | bool | str

#: Formats the report can be rendered into.
ReportFormat = Literal["markdown", "json"]
_DEFAULT_REPORT_FORMATS: list[ReportFormat] = ["markdown", "json"]

#: Accepted profile identifiers: 1-64 characters, no dot, slash or space.
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: Upper bound of ``ProfileConfig.entry_lookback_candles``.
#:
#: The value is deliberate: it equals the default ``warmup_candles`` and stays
#: below the smallest ``history_candles`` (300) able to feed a full warmup, so
#: the configured maximum is always satisfiable by a well-configured profile
#: while an absurd value is refused loudly by the field bounds.  This constant
#: is the single source of truth shared by the live runner guard and the tests;
#: it is intentionally absent from ``__all__`` so the public surface is stable.
MAX_ENTRY_LOOKBACK_CANDLES: int = 200


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


class ForecastConfig(BaseModel):
    """Offline forecast artifact consumed by the ``timesfm`` strategy.

    The artifact is built *outside* the strategy (``trading forecast-build``) and
    read once per run through
    :func:`trading_platform.strategy.features.resolve_features`, so a strategy
    never performs I/O of its own and the whole decision path stays deterministic
    and testable without any machine-learning dependency installed.

    A missing path is a valid configuration: the strategy then produces **no
    signal at all** (rather than guessing or raising).
    """

    model_config = {"extra": "forbid"}

    artifact: Path | None = None


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


class RiskLimitsConfig(BaseModel):
    """Per-profile risk limits enforced *before* any order reaches the venue.

    Every limit is optional: ``None`` means "not enforced".  ``0`` is a valid,
    meaningful value for the counting limits (``max_open_positions=0`` forbids
    opening a position at all, ``max_daily_trades=0`` forbids trading).
    """

    model_config = {"extra": "forbid"}

    max_position_notional: float | None = Field(default=None, gt=0)
    max_order_notional: float | None = Field(default=None, gt=0)
    max_open_positions: int = Field(default=1, ge=0)
    max_daily_loss: float | None = Field(default=None, gt=0)
    max_drawdown_pct: float | None = Field(default=None, gt=0, le=1)
    max_daily_trades: int | None = Field(default=None, ge=0)


class ProfileConfig(BaseModel):
    """One independently runnable trading profile (asset + strategy + mode).

    The model carries **no credential field on purpose**: ``extra="forbid"``
    turns a profile file that contains ``api_key``/``api_secret``/``password``
    into a loud :class:`~trading_platform.core.errors.ConfigError` instead of a
    silently ignored key.  Secrets are resolved from the environment only (see
    ``trading_platform.realtime.credentials``).

    ``allocation`` is the profile's share of the one shared platform wallet: the
    figure its per-profile risk limits are measured against and the figure its
    reporting is attributed to.  It is *optional and additive*: when it is absent
    (``None``) the profile's own ``initial_balance`` **is** its allocation, so
    every configuration written before the shared wallet existed keeps its exact
    previous behaviour.

    ``entry_lookback_candles`` is the live-only catch-up window: ``0`` (the
    default) keeps the historical behaviour where only the last row of the
    signals frame decides, while ``N > 0`` lets the live engine act on a
    crossover that occurred within the last ``N`` candles.  The backtest is
    unaffected: it already reads every row.  The field is declared last so the
    field order of every pre-existing configuration never moves.

    ``forecast`` is the *optional* path of the offline forecast artifact a
    forecast-driven strategy consumes, built by ``trading forecast-build``.  It
    is additive in exactly the same way: ``None`` (the default) means the
    profile declares no forecast at all, which is the historical behaviour of
    every ``basic`` profile.  A profile that *does* declare one is validated at
    startup -- the artifact is loaded once and its coverage must reach the
    profile's decision horizon -- so a stale or mismatched artifact refuses to
    start instead of silently producing no signal.
    """

    model_config = {"extra": "forbid"}

    id: str
    symbol: str
    timeframe: str = DEFAULT_TIMEFRAME
    strategy: str = "basic"
    params: dict[str, ParamValue] = Field(default_factory=dict)
    mode: Literal["paper", "live"] = "paper"
    initial_balance: float = Field(default=DEFAULT_INITIAL_BALANCE, gt=0)
    allocation: float | None = Field(default=None, gt=0)
    stake_amount: float | None = Field(default=None, gt=0)
    exchange: str = "binance"
    enabled: bool = True
    warmup_candles: int = Field(default=200, ge=1)
    poll_interval_seconds: float = Field(default=5.0, gt=0)
    risk: RiskLimitsConfig = Field(default_factory=RiskLimitsConfig)
    entry_lookback_candles: int = Field(default=0, ge=0, le=MAX_ENTRY_LOOKBACK_CANDLES)
    forecast: Path | None = Field(
        default=None,
        description=(
            "Path to the offline forecast artifact consumed by a forecast-driven "
            "strategy (built by `trading forecast-build`); None means the profile "
            "declares no forecast."
        ),
    )

    @property
    def effective_allocation(self) -> float:
        """Return this profile's share of the shared wallet.

        ``allocation`` when it is configured, otherwise ``initial_balance``: the
        fallback is what keeps a configuration written before the shared wallet
        existed strictly equivalent to the new model.
        """
        return float(self.initial_balance if self.allocation is None else self.allocation)

    @property
    def forecast_artifact(self) -> Path | None:
        """Return the forecast artifact this profile declares, or ``None``.

        A forward-compatible accessor: the field is read through ``getattr`` so
        the property keeps answering ``None`` for a profile whose ``forecast``
        ever becomes a richer ``dict | model`` union -- a caller that only wants
        the path never has to know which shape was declared.
        """
        declared = getattr(self, "forecast", None)
        return declared if declared is None or isinstance(declared, Path) else None

    def strategy_params(self) -> dict[str, Any]:
        """Return the strategy parameters of this profile.

        A copy of ``params``, plus the ``artifact`` key the forecast-driven
        strategy reads when the profile declares a forecast artifact.  The
        profile's own ``params`` mapping is never mutated.
        """
        resolved: dict[str, Any] = dict(self.params)
        artifact = self.forecast_artifact
        if artifact is not None:
            resolved["artifact"] = str(artifact)
        return resolved

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _PROFILE_ID.match(value):
            raise ValueError(
                f"invalid profile id: {value!r} (expected ^[A-Za-z0-9][A-Za-z0-9_-]{{0,63}}$)"
            )
        return value

    @field_validator("symbol")
    @classmethod
    def _check_symbol(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("symbol must not be empty")
        return stripped

    @field_validator("timeframe")
    @classmethod
    def _check_timeframe(cls, value: str) -> str:
        if value not in SUPPORTED_TIMEFRAMES:
            supported = ", ".join(sorted(SUPPORTED_TIMEFRAMES))
            raise ValueError(f"unsupported timeframe: {value!r} (supported: {supported})")
        return value


class RealtimeConfig(BaseModel):
    """Engine settings shared by every profile of a realtime run.

    The three ``platform_*`` fields are the platform-wide wallet and risk caps:
    ``platform_initial_balance`` seeds the one shared USDT wallet when the store
    remembers nothing, while ``platform_max_total_notional`` and
    ``platform_max_daily_loss`` cap the exposure and the daily loss aggregated
    over *every* profile.  All three are optional and additive -- an absent cap
    means "not enforced", and an absent initial balance means the wallet starts
    at the sum of the profiles' effective allocations (see
    :func:`resolve_platform_initial_balance`).
    """

    model_config = {"extra": "forbid"}

    state_db: Path = Path("data/realtime/state.db")
    logs_dir: Path = Path("data/realtime/logs")
    data_dir: Path = Path("data")
    cache_dir: Path = Path("data/cache")
    format: Literal["parquet", "csv"] = "parquet"
    allow_network: bool = True
    csv_dir: Path | None = None
    start_at: datetime | None = None
    history_candles: int = Field(default=300, ge=1)
    poll_interval_seconds: float = Field(default=5.0, gt=0)
    stream_poll_timeout_seconds: float = Field(default=10.0, gt=0)
    max_stream_reconnects: int = Field(default=5, ge=0)
    reconnect_backoff_seconds: float = Field(default=1.0, gt=0)
    reconcile_interval_seconds: float = Field(default=60.0, gt=0)
    risk_free_rate: float = Field(default=0.0, ge=0)
    benchmark_variant: Literal["buy_and_hold", "cash", "risk_free", "random_entry", "none"] = (
        "buy_and_hold"
    )
    kill_switch_file: Path | None = None
    platform_initial_balance: float | None = Field(default=None, gt=0)
    platform_max_total_notional: float | None = Field(default=None, gt=0)
    platform_max_daily_loss: float | None = Field(default=None, gt=0)


def resolve_platform_initial_balance(
    realtime: RealtimeConfig, profiles: Sequence[ProfileConfig]
) -> float:
    """Return the cash the one shared platform wallet starts with.

    ``realtime.platform_initial_balance`` when it is configured, otherwise the sum
    of the profiles' :attr:`ProfileConfig.effective_allocation` -- which is the
    documented backward-compatible fallback: a configuration that predates the
    shared wallet funds it with exactly the balances the profiles declared, so the
    platform's total cash is unchanged.  An empty ``profiles`` sequence therefore
    resolves to ``0.0``.

    Parameters
    ----------
    realtime:
        The realtime section of the configuration document.
    profiles:
        Every profile of the run (an empty sequence is accepted).

    Returns
    -------
    float
        The starting cash of the shared wallet, in USDT.
    """
    if realtime.platform_initial_balance is not None:
        return float(realtime.platform_initial_balance)
    return float(sum(profile.effective_allocation for profile in profiles))


class MonitoringConfig(BaseModel):
    """HTTP monitoring surface settings (standard library server only).

    There is **no operator token field on purpose**: the token is a credential
    and is resolved from the environment (``TB_OPERATOR_TOKEN``) by
    ``trading_platform.web.server``.
    """

    model_config = {"extra": "forbid"}

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=0, le=65535)
    refresh_seconds: float = Field(default=2.0, gt=0)
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    max_request_bytes: int = Field(default=65536, ge=1)


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

    project_name: str = "trading-platform"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    benchmark: BenchmarkConfig = Field(default_factory=BenchmarkConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    realtime: RealtimeConfig = Field(default_factory=RealtimeConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    #: Declared last on purpose: the field order of every pre-existing
    #: configuration therefore never moves.
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)

    @property
    def timezone(self) -> str:
        """Every timestamp handled by the package is UTC."""
        return UTC
