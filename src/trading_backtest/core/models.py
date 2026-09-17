"""Frozen cross-package domain models.

Everything the other layers exchange travels through this module: trade records,
backtest results and the :data:`RunnerFn` seam used by the validation layer.

The models are deliberately free of heavy side effects: importing this module
only requires ``pandas``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pandas as pd

from trading_backtest.core.constants import OHLCV_INDEX_NAME, UTC

__all__ = [
    "BacktestResult",
    "Direction",
    "ExitReason",
    "RunnerFn",
    "TradeRecord",
]


class Direction(str, Enum):  # noqa: UP042 - the frozen contract mandates (str, Enum)
    """Direction of a trade."""

    LONG = "long"
    SHORT = "short"


class ExitReason(str, Enum):  # noqa: UP042 - the frozen contract mandates (str, Enum)
    """Why a position was closed."""

    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    SIGNAL = "signal"
    END_OF_DATA = "end_of_data"
    MAX_DURATION = "max_duration"


@dataclass(frozen=True)
class TradeRecord:
    """Immutable record of a single round-trip trade.

    ``to_dict()`` / ``from_dict()`` round-trip losslessly: timestamps are
    serialised with :meth:`pandas.Timestamp.isoformat` and enums with their
    ``.value``.
    """

    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    size: float
    direction: Direction
    pnl: float
    pnl_pct: float
    fees: float
    exit_reason: ExitReason
    duration_minutes: float
    stop_price: float | None = None
    take_profit_price: float | None = None
    params_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per dataclass field."""
        return {
            "entry_time": pd.Timestamp(self.entry_time).isoformat(),
            "exit_time": pd.Timestamp(self.exit_time).isoformat(),
            "entry_price": float(self.entry_price),
            "exit_price": float(self.exit_price),
            "size": float(self.size),
            "direction": Direction(self.direction).value,
            "pnl": float(self.pnl),
            "pnl_pct": float(self.pnl_pct),
            "fees": float(self.fees),
            "exit_reason": ExitReason(self.exit_reason).value,
            "duration_minutes": float(self.duration_minutes),
            "stop_price": None if self.stop_price is None else float(self.stop_price),
            "take_profit_price": (
                None if self.take_profit_price is None else float(self.take_profit_price)
            ),
            "params_id": str(self.params_id),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TradeRecord:
        """Rebuild a :class:`TradeRecord` from :meth:`to_dict` output."""
        stop_price = payload.get("stop_price")
        take_profit_price = payload.get("take_profit_price")
        return cls(
            entry_time=pd.Timestamp(payload["entry_time"]),
            exit_time=pd.Timestamp(payload["exit_time"]),
            entry_price=float(payload["entry_price"]),
            exit_price=float(payload["exit_price"]),
            size=float(payload["size"]),
            direction=Direction(payload["direction"]),
            pnl=float(payload["pnl"]),
            pnl_pct=float(payload["pnl_pct"]),
            fees=float(payload["fees"]),
            exit_reason=ExitReason(payload["exit_reason"]),
            duration_minutes=float(payload["duration_minutes"]),
            stop_price=None if stop_price is None else float(stop_price),
            take_profit_price=None if take_profit_price is None else float(take_profit_price),
            params_id=str(payload.get("params_id") or ""),
        )


def _normalise_equity(series: Any) -> Any:
    """Return a contract-conformant equity curve without mutating the input."""
    if not isinstance(series, pd.Series):  # pragma: no cover - defensive guard
        return series
    out = series.astype("float64")
    index = pd.DatetimeIndex(out.index)
    index = index.tz_localize(UTC) if index.tz is None else index.tz_convert(UTC)
    # Canonical (microsecond) resolution: keeps the isoformat round-trip of
    # to_dict/from_dict lossless whatever the resolution of the input index.
    index = index.as_unit("us")
    index.name = OHLCV_INDEX_NAME
    out.index = index
    if out.index.has_duplicates or not out.index.is_monotonic_increasing:
        out = out[~out.index.duplicated(keep="last")].sort_index()
    out.name = "equity"
    return out


@dataclass(eq=False)
class BacktestResult:
    """Full outcome of a single backtest run.

    ``equity_curve`` is normalised on construction to ``float64`` values indexed
    by an ascending, tz-aware UTC ``DatetimeIndex`` named ``"timestamp"`` and is
    named ``"equity"``.  ``__eq__`` is implemented explicitly (instead of the
    ``dataclass`` default) so that comparing two results compares their equity
    curves element-wise instead of raising ``ValueError``.
    """

    strategy_name: str
    symbol: str
    timeframe: str
    start: pd.Timestamp
    end: pd.Timestamp
    initial_balance: float
    final_balance: float
    trades: list[TradeRecord]
    equity_curve: pd.Series
    params: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.equity_curve = _normalise_equity(self.equity_curve)

    @property
    def n_trades(self) -> int:
        """Number of trades recorded by the run."""
        return len(self.trades)

    @property
    def is_empty(self) -> bool:
        """``True`` when the run produced no trade at all."""
        return not self.trades

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping (equity curve as timestamps/values)."""
        index = pd.DatetimeIndex(self.equity_curve.index)
        values = self.equity_curve.to_numpy(dtype="float64")
        return {
            "strategy_name": str(self.strategy_name),
            "symbol": str(self.symbol),
            "timeframe": str(self.timeframe),
            "start": pd.Timestamp(self.start).isoformat(),
            "end": pd.Timestamp(self.end).isoformat(),
            "initial_balance": float(self.initial_balance),
            "final_balance": float(self.final_balance),
            "trades": [trade.to_dict() for trade in self.trades],
            "equity_curve": {
                "timestamps": [timestamp.isoformat() for timestamp in index],
                "values": [float(value) for value in values],
            },
            "params": dict(self.params),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BacktestResult:
        """Exact inverse of :meth:`to_dict`."""
        curve = payload["equity_curve"]
        raw_timestamps = list(curve["timestamps"])
        values = [float(value) for value in curve["values"]]
        if raw_timestamps:
            index = pd.DatetimeIndex(
                [pd.Timestamp(timestamp) for timestamp in raw_timestamps],
                name=OHLCV_INDEX_NAME,
            )
        else:
            index = pd.DatetimeIndex([], tz=UTC, name=OHLCV_INDEX_NAME)
        equity_curve = pd.Series(values, index=index, name="equity", dtype="float64")
        return cls(
            strategy_name=str(payload["strategy_name"]),
            symbol=str(payload["symbol"]),
            timeframe=str(payload["timeframe"]),
            start=pd.Timestamp(payload["start"]),
            end=pd.Timestamp(payload["end"]),
            initial_balance=float(payload["initial_balance"]),
            final_balance=float(payload["final_balance"]),
            trades=[TradeRecord.from_dict(trade) for trade in payload["trades"]],
            equity_curve=equity_curve,
            params=dict(payload.get("params") or {}),
            metadata=dict(payload.get("metadata") or {}),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BacktestResult):
            return NotImplemented
        scalars = (
            self.strategy_name == other.strategy_name
            and self.symbol == other.symbol
            and self.timeframe == other.timeframe
            and pd.Timestamp(self.start) == pd.Timestamp(other.start)
            and pd.Timestamp(self.end) == pd.Timestamp(other.end)
            and float(self.initial_balance) == float(other.initial_balance)
            and float(self.final_balance) == float(other.final_balance)
            and self.trades == other.trades
            and self.params == other.params
            and self.metadata == other.metadata
        )
        if not scalars:
            return False
        try:
            pd.testing.assert_series_equal(
                self.equity_curve,
                other.equity_curve,
                check_exact=True,
                check_names=True,
                check_freq=False,
            )
        except AssertionError:
            return False
        return True


#: Signature of a backtest runner: ``runner(data, params_or_None) -> BacktestResult``.
#:
#: It is the seam used by the validation layer (walk-forward, robustness): a
#: runner receives the OHLCV frame it must backtest and the strategy parameters
#: to apply (``None`` means "use the strategy defaults") and returns a complete
#: :class:`BacktestResult` computed over that frame only.
RunnerFn = Callable[[pd.DataFrame, Mapping[str, Any] | None], BacktestResult]
