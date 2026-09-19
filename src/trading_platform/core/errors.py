"""Exception hierarchy shared by every layer of the package.

All errors raised by ``trading_platform`` derive from :class:`TradingBacktestError`
so that the CLI can render a single, uniform error surface.  Errors that carry a
list of human readable problems (typically validation issues) expose them through
the :attr:`TradingBacktestError.issues` attribute.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "BrokerError",
    "BrokerUnavailableError",
    "ConfigError",
    "DataDownloadError",
    "DataError",
    "DataValidationError",
    "FreqtradeConfigError",
    "GatewayError",
    "InsufficientDataError",
    "KillSwitchActiveError",
    "LiveTradingForbiddenError",
    "MarketStreamError",
    "MetricsError",
    "MonitoringError",
    "MonteCarloError",
    "OrderRejectedError",
    "ProfileError",
    "RealtimeError",
    "ReportingError",
    "RiskLimitExceededError",
    "RobustnessError",
    "StateStoreError",
    "StrategyError",
    "TradingBacktestError",
    "ValidationLayerError",
    "WalkForwardError",
    "WalletError",
]


class TradingBacktestError(Exception):
    """Base class of every error raised by this package.

    Parameters
    ----------
    message:
        Short human readable description of the failure.
    issues:
        Optional list of individual problems (one entry per violated rule).
        Stored as a tuple on :attr:`issues` and appended to ``str(error)``.
    """

    def __init__(self, message: str = "", issues: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.issues: tuple[str, ...] = tuple(issues)

    def __str__(self) -> str:
        if not self.issues:
            return self.message
        details = "; ".join(str(issue) for issue in self.issues)
        prefix = self.message or self.__class__.__name__
        return f"{prefix}: {details}"


class ConfigError(TradingBacktestError):
    """Invalid configuration (file, environment or programmatic override)."""


class DataError(TradingBacktestError):
    """Base class of every market-data related failure."""


class DataValidationError(DataError):
    """Market data violates the OHLCV contract."""


class InsufficientDataError(DataError):
    """Not enough data is available to satisfy the requested window."""


class DataDownloadError(DataError):
    """Downloading market data from an exchange failed."""


class StrategyError(TradingBacktestError):
    """A strategy could not be built or produced invalid signals."""


class ValidationLayerError(TradingBacktestError):
    """Base class of the statistical validation layer (walk-forward, robustness)."""


class WalkForwardError(ValidationLayerError):
    """Walk-forward analysis could not be performed."""


class RobustnessError(ValidationLayerError):
    """Parametric robustness analysis could not be performed."""


class MonteCarloError(ValidationLayerError):
    """Monte Carlo simulation could not be performed."""


class MetricsError(TradingBacktestError):
    """Performance metrics could not be computed."""


class ReportingError(TradingBacktestError):
    """The report could not be rendered or written."""


class FreqtradeConfigError(ConfigError):
    """A Freqtrade configuration file is invalid or incomplete."""


# ---------------------------------------------------------------------------
# Realtime branch (layer 6 / layer 7): streaming engine, venue adapters and
# monitoring transport.  Every class below derives from :class:`RealtimeError`
# so that ``except RealtimeError`` catches the whole live-trading surface.
# ---------------------------------------------------------------------------


class RealtimeError(TradingBacktestError):
    """Base class of every failure raised by the realtime engine and its web layer."""


class ProfileError(RealtimeError):
    """A profile definition is invalid or cannot be used to start a runner."""


class MarketStreamError(RealtimeError):
    """Reading the market data stream failed or exhausted its reconnect budget."""


class StateStoreError(RealtimeError):
    """The persistent state store failed, or its schema version is not supported."""


class WalletError(RealtimeError):
    """The shared platform wallet refused an operation, or an amount is unusable.

    The wallet is the single source of truth for the USDT cash of the platform, so
    a refusal here is never a silent one: an amount that is not a finite,
    non-negative number, an attempt to mutate the read-only live mirror, a debit
    the ledger cannot fund and a non-finite restored balance all raise this error
    with an explicit message.
    """


class BrokerError(RealtimeError):
    """Base class of every venue (broker) failure."""


class BrokerUnavailableError(BrokerError):
    """The optional exchange extra (ccxt / freqtrade) is not installed."""


class OrderRejectedError(BrokerError):
    """The venue refused an order."""


class GatewayError(RealtimeError):
    """The order lifecycle or the reconciliation against the venue failed."""


class LiveTradingForbiddenError(RealtimeError):
    """Live trading was requested without satisfying the live gate."""


class RiskLimitExceededError(RealtimeError):
    """A per-profile risk limit blocked an order before it reached the venue."""


class KillSwitchActiveError(RealtimeError):
    """The global kill switch is engaged, so no order may be submitted."""


class MonitoringError(RealtimeError):
    """The web layer could not build its read model."""
