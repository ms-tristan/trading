"""Exception hierarchy shared by every layer of the package.

All errors raised by ``trading_backtest`` derive from :class:`TradingBacktestError`
so that the CLI can render a single, uniform error surface.  Errors that carry a
list of human readable problems (typically validation issues) expose them through
the :attr:`TradingBacktestError.issues` attribute.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "ConfigError",
    "DataDownloadError",
    "DataError",
    "DataValidationError",
    "FreqtradeConfigError",
    "InsufficientDataError",
    "MetricsError",
    "MonteCarloError",
    "ReportingError",
    "RobustnessError",
    "StrategyError",
    "TradingBacktestError",
    "ValidationLayerError",
    "WalkForwardError",
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
