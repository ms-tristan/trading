"""The reference "basic" strategy: EMA crossover + RSI filter + ATR stop.

Rules (they are frozen — the engine, the validation layer and the documentation
all rely on them):

``entry_long``
    bullish EMA crossover — ``ema_fast > ema_slow`` and
    ``ema_fast.shift(1) <= ema_slow.shift(1)`` — **and** the RSI inside
    ``]rsi_min, rsi_max[``.  The entry therefore fires on the crossover candle
    only, never on the first candle and never on a row where an indicator is not
    yet defined.

``exit_long``
    bearish EMA crossover — ``ema_fast < ema_slow`` and
    ``ema_fast.shift(1) >= ema_slow.shift(1)``.

``entry_short`` / ``exit_short``
    the exact mirror images (bearish / bullish crossover plus the same RSI
    filter).  They are all ``False`` unless ``allow_short`` is ``True``.

``stop_loss``
    ``close - atr_stop_multiplier * atr`` (``NaN`` while the ATR is not yet
    defined, i.e. "no stop").  The value is the *long* stop price; for a short
    entry the engine mirrors the same distance about the entry candle's close
    (see :mod:`trading_backtest.strategy.engine`).
"""

from __future__ import annotations

from typing import ClassVar, cast

import pandas as pd
from pydantic import Field, model_validator

from trading_backtest.core.constants import REQUIRED_OHLCV_COLUMNS
from trading_backtest.core.errors import StrategyError
from trading_backtest.strategy.base import (
    Strategy,
    StrategyParams,
    ensure_signal_frame,
    require_ohlcv_frame,
)
from trading_backtest.strategy.indicators import atr, ema, rsi

__all__ = ["BasicStrategy", "BasicStrategyParams"]

#: Indicator columns added by :meth:`BasicStrategy.prepare`.
INDICATOR_COLUMNS: tuple[str, ...] = ("ema_fast", "ema_slow", "rsi", "atr")


class BasicStrategyParams(StrategyParams):
    """Validated parameters of :class:`BasicStrategy`."""

    ema_fast: int = Field(default=9, ge=1)
    ema_slow: int = Field(default=21, ge=2)
    rsi_period: int = Field(default=14, ge=2)
    rsi_min: float = Field(default=30.0, ge=0, lt=100)
    rsi_max: float = Field(default=70.0, gt=0, le=100)
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiplier: float = Field(default=2.0, gt=0)
    allow_short: bool = False

    @model_validator(mode="after")
    def _check_coherence(self) -> BasicStrategyParams:
        """Enforce the two cross-field invariants of the strategy."""
        issues: list[str] = []
        if self.ema_slow <= self.ema_fast:
            issues.append(
                f"ema_slow ({self.ema_slow}) must be greater than ema_fast ({self.ema_fast})"
            )
        if self.rsi_min >= self.rsi_max:
            issues.append(f"rsi_min ({self.rsi_min}) must be lower than rsi_max ({self.rsi_max})")
        if issues:
            raise ValueError("; ".join(issues))
        return self


class BasicStrategy(Strategy):
    """EMA crossover strategy with an RSI filter and an ATR-based stop loss."""

    name: ClassVar[str] = "basic"
    ParamsModel: ClassVar[type[StrategyParams]] = BasicStrategyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {
        "ema_fast": [5, 9, 13],
        "ema_slow": [21, 34, 55],
        "rsi_max": [65.0, 70.0, 75.0],
        "atr_stop_multiplier": [1.5, 2.0, 3.0],
    }

    @property
    def _basic_params(self) -> BasicStrategyParams:
        """The parameters, typed as :class:`BasicStrategyParams`."""
        return cast(BasicStrategyParams, self.params)

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add ``ema_fast``, ``ema_slow``, ``rsi`` and ``atr`` to a copy of ``data``.

        The OHLCV columns of the input are copied unchanged and the index is
        preserved exactly; ``data`` itself is never mutated.

        Raises
        ------
        StrategyError
            If ``data`` does not satisfy the OHLCV contract.
        """
        params = self._basic_params
        frame = require_ohlcv_frame(data, name="data")
        close = frame["close"]
        frame["ema_fast"] = ema(close, params.ema_fast).to_numpy(dtype="float64")
        frame["ema_slow"] = ema(close, params.ema_slow).to_numpy(dtype="float64")
        frame["rsi"] = rsi(close, params.rsi_period).to_numpy(dtype="float64")
        frame["atr"] = atr(frame["high"], frame["low"], close, params.atr_period).to_numpy(
            dtype="float64"
        )
        return frame

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the signal frame of a prepared frame.

        The input must carry the OHLCV columns **and** the indicator columns
        produced by :meth:`prepare`.  The computation is pure and repeatable:
        the input frame is never mutated.

        Raises
        ------
        StrategyError
            If ``data`` is not a prepared frame.
        """
        if not isinstance(data, pd.DataFrame):
            raise StrategyError(f"data must be a pandas DataFrame, got {type(data).__name__}")
        required = (*REQUIRED_OHLCV_COLUMNS, *INDICATOR_COLUMNS)
        missing = [column for column in required if column not in data.columns]
        if missing:
            raise StrategyError(
                "signals() must be called on a frame returned by prepare()",
                [f"missing column(s): {', '.join(missing)}"],
            )

        params = self._basic_params
        fast = data["ema_fast"].astype("float64")
        slow = data["ema_slow"].astype("float64")
        oscillator = data["rsi"].astype("float64")
        true_range_average = data["atr"].astype("float64")

        bullish_cross = (fast > slow) & (fast.shift(1) <= slow.shift(1))
        bearish_cross = (fast < slow) & (fast.shift(1) >= slow.shift(1))
        rsi_filter = (oscillator > params.rsi_min) & (oscillator < params.rsi_max)

        entry_long = _as_bool(bullish_cross & rsi_filter)
        exit_long = _as_bool(bearish_cross)
        if params.allow_short:
            entry_short = _as_bool(bearish_cross & rsi_filter)
            exit_short = _as_bool(bullish_cross)
        else:
            entry_short = pd.Series(False, index=data.index, dtype=bool)
            exit_short = pd.Series(False, index=data.index, dtype=bool)

        # The very first candle can never be an entry: there is no previous
        # candle to cross from.
        if len(data):
            for column in (entry_long, exit_long, entry_short, exit_short):
                column.iloc[0] = False

        signals = pd.DataFrame(
            {
                "entry_long": entry_long.to_numpy(dtype=bool),
                "exit_long": exit_long.to_numpy(dtype=bool),
                "entry_short": entry_short.to_numpy(dtype=bool),
                "exit_short": exit_short.to_numpy(dtype=bool),
                "stop_loss": (
                    data["close"].astype("float64")
                    - params.atr_stop_multiplier * true_range_average
                ).to_numpy(dtype="float64"),
            },
            index=data.index,
        )
        return ensure_signal_frame(signals, data.index)


def _as_bool(values: pd.Series) -> pd.Series:
    """Return ``values`` as a ``bool`` Series where a missing value means ``False``."""
    return values.fillna(False).astype(bool)
