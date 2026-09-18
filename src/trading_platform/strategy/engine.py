"""Deterministic, single-position backtest engine.

Execution model (frozen — the validation layer, the metrics layer and the
documentation all depend on it):

* signals are evaluated **at the close of candle ``t``** and filled **at the
  open of candle ``t + 1``**;
* a buy fills at ``open * (1 + slippage)``, a sell fills at ``open * (1 - slippage)``;
* fees are ``fee_rate * size * (entry_price + exit_price)``; they are recorded on
  the :class:`~trading_platform.core.models.TradeRecord` and subtracted from its
  ``pnl``;
* sizing: ``stake_amount is None`` -> ``size = equity / (entry_price * (1 + fee_rate))``,
  otherwise ``size = stake_amount / entry_price``;
* **one position at a time**: an entry signal raised while a position is open is
  ignored;
* the stop is read from the ``stop_loss`` column of the **signal candle** and
  stays **static** for the whole trade; it is checked intrabar from the entry
  candle onwards against ``low`` (long) / ``high`` (short) and fills *at the stop
  price* with :attr:`~trading_platform.core.models.ExitReason.STOP_LOSS`.  For a
  short entry the column is read as the mirror of the long formula: the distance
  ``close(t) - stop_loss(t)`` is applied **above** the fill price;
* otherwise a signal exit fills at the open of the following candle
  (:attr:`~trading_platform.core.models.ExitReason.SIGNAL`);
* a position still open on the last candle is closed at the last close
  (:attr:`~trading_platform.core.models.ExitReason.END_OF_DATA`).
  ``TAKE_PROFIT`` and ``MAX_DURATION`` exist in the enum but are never produced
  by this engine;
* ``equity_curve`` holds one mark-to-market (close) point per candle, starting at
  ``initial_balance``, indexed like the input frame and named ``"equity"``;
  ``final_balance == float(equity_curve.iloc[-1])``.

The engine uses no randomness and no dict ordering: two runs over the same inputs
produce the exact same result.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from trading_platform.config import AppConfig
from trading_platform.core.constants import (
    DEFAULT_FEE_RATE,
    DEFAULT_INITIAL_BALANCE,
    DEFAULT_SLIPPAGE,
    DEFAULT_TIMEFRAME,
)
from trading_platform.core.errors import InsufficientDataError, StrategyError
from trading_platform.core.models import (
    BacktestResult,
    Direction,
    ExitReason,
    RunnerFn,
    TradeRecord,
)
from trading_platform.strategy.base import Strategy, ensure_signal_frame, require_ohlcv_frame
from trading_platform.strategy.registry import get_strategy

__all__ = [
    "DEFAULT_SYMBOL",
    "ENGINE_VERSION",
    "make_runner",
    "run_backtest",
    "run_backtest_on_config",
]

#: Version of the execution model, stored in ``BacktestResult.metadata``.
ENGINE_VERSION = "1"

#: Symbol used when neither the caller nor the configuration provides one.
DEFAULT_SYMBOL = "UNKNOWN/USDT"


@dataclass(frozen=True)
class _PendingEntry:
    """An entry order scheduled by the close of the previous candle."""

    direction: Direction
    raw_stop: float


@dataclass
class _Position:
    """The (single) open position of the engine."""

    direction: Direction
    entry_index: int
    entry_price: float
    size: float
    stop_price: float | None


def _resolve_allow_short(strategy: Strategy, allow_short: bool | None) -> bool:
    """Resolve the short switch: explicit argument first, strategy parameter else."""
    if allow_short is not None:
        return bool(allow_short)
    return bool(getattr(strategy.params, "allow_short", False))


def _entry_fill(open_price: float, direction: Direction, slippage: float) -> float:
    """Fill price of an entry at ``open_price`` (slippage always works against us)."""
    if direction is Direction.LONG:
        return float(open_price * (1.0 + slippage))
    return float(open_price * (1.0 - slippage))


def _exit_fill(open_price: float, direction: Direction, slippage: float) -> float:
    """Fill price of an exit at ``open_price`` (slippage always works against us)."""
    if direction is Direction.LONG:
        return float(open_price * (1.0 - slippage))
    return float(open_price * (1.0 + slippage))


def _resolve_stop(
    raw_stop: float,
    direction: Direction,
    signal_close: float,
    entry_price: float,
) -> float | None:
    """Turn the ``stop_loss`` column value of the signal candle into a static price.

    For a long the column *is* the stop price.  For a short the column is read as
    the mirror of the long formula: ``distance = signal_close - raw_stop`` is
    applied above the fill price (``entry_price + distance``), so a short is
    stopped out at a loss.  ``NaN`` means "no stop".
    """
    if not np.isfinite(raw_stop):
        return None
    if direction is Direction.LONG:
        return float(raw_stop)
    return float(entry_price + (signal_close - raw_stop))


def _make_trade(
    position: _Position,
    *,
    index: pd.DatetimeIndex,
    exit_index: int,
    exit_price: float,
    reason: ExitReason,
    fee_rate: float,
    params_id: str,
) -> TradeRecord:
    """Build the immutable record of the round-trip closed at ``exit_index``."""
    if position.direction is Direction.LONG:
        gross = (exit_price - position.entry_price) * position.size
    else:
        gross = (position.entry_price - exit_price) * position.size
    fees = fee_rate * position.size * (position.entry_price + exit_price)
    pnl = gross - fees
    entry_time = pd.Timestamp(index[position.entry_index])
    exit_time = pd.Timestamp(index[exit_index])
    duration_minutes = float(pd.Timedelta(exit_time - entry_time) / pd.Timedelta(minutes=1))
    return TradeRecord(
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=float(position.entry_price),
        exit_price=float(exit_price),
        size=float(position.size),
        direction=position.direction,
        pnl=float(pnl),
        pnl_pct=float(pnl / (position.size * position.entry_price)),
        fees=float(fees),
        exit_reason=reason,
        duration_minutes=duration_minutes,
        stop_price=None if position.stop_price is None else float(position.stop_price),
        take_profit_price=None,
        params_id=str(params_id),
    )


def run_backtest(
    strategy: Strategy,
    data: pd.DataFrame,
    *,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    fee_rate: float = DEFAULT_FEE_RATE,
    slippage: float = DEFAULT_SLIPPAGE,
    stake_amount: float | None = None,
    symbol: str = DEFAULT_SYMBOL,
    timeframe: str = DEFAULT_TIMEFRAME,
    allow_short: bool | None = None,
    params_id: str = "",
) -> BacktestResult:
    """Backtest ``strategy`` over ``data`` and return the full result.

    Parameters
    ----------
    strategy:
        Any :class:`~trading_platform.strategy.base.Strategy`; ``strategy.run``
        produces the prepared frame and the signals.
    data:
        OHLCV frame satisfying the contract (see
        :func:`~trading_platform.strategy.base.require_ohlcv_frame`).  It is never
        mutated.
    initial_balance:
        Starting equity (also the first point of the equity curve).
    fee_rate:
        Taker fee rate applied to both legs.
    slippage:
        Relative slippage applied to every fill.
    stake_amount:
        Fixed stake per trade; ``None`` means "full equity".
    symbol, timeframe:
        Descriptive metadata of the run.
    allow_short:
        ``None`` lets the strategy decide (its ``allow_short`` parameter when it
        has one); ``False`` ignores every short entry signal.
    params_id:
        Free-form label stored on every trade (used by the robustness layer).

    Raises
    ------
    StrategyError
        If ``data`` does not satisfy the OHLCV contract, if the strategy returns a
        frame that is not a valid signal frame, or if ``stake_amount`` is not
        positive.
    InsufficientDataError
        If ``data`` holds fewer than two candles.
    """
    frame = require_ohlcv_frame(data, name="data")
    row_count = len(frame)
    if row_count < 2:
        raise InsufficientDataError(
            f"at least 2 candles are required to run a backtest, got {row_count}"
        )
    if stake_amount is not None and float(stake_amount) <= 0:
        raise StrategyError(f"stake_amount must be > 0, got {stake_amount!r}")

    short_enabled = _resolve_allow_short(strategy, allow_short)
    _prepared, raw_signals = strategy.run(frame)
    signals = ensure_signal_frame(raw_signals, frame.index)

    index = pd.DatetimeIndex(frame.index)
    opens = frame["open"].to_numpy(dtype="float64")
    highs = frame["high"].to_numpy(dtype="float64")
    lows = frame["low"].to_numpy(dtype="float64")
    closes = frame["close"].to_numpy(dtype="float64")
    entry_long = signals["entry_long"].to_numpy(dtype=bool)
    exit_long = signals["exit_long"].to_numpy(dtype=bool)
    if short_enabled:
        entry_short = signals["entry_short"].to_numpy(dtype=bool)
        exit_short = signals["exit_short"].to_numpy(dtype=bool)
    else:
        entry_short = np.zeros(row_count, dtype=bool)
        exit_short = np.zeros(row_count, dtype=bool)
    stop_column = signals["stop_loss"].to_numpy(dtype="float64")

    fee = float(fee_rate)
    slip = float(slippage)
    balance = float(initial_balance)
    last = row_count - 1
    equity_values = np.empty(row_count, dtype="float64")
    trades: list[TradeRecord] = []
    position: _Position | None = None
    pending_entry: _PendingEntry | None = None
    pending_exit = False

    for i in range(row_count):
        # 1. orders scheduled by the previous candle fill at this candle's open.
        if pending_entry is not None:
            direction = pending_entry.direction
            fill = _entry_fill(opens[i], direction, slip)
            if fill <= 0:
                raise StrategyError(f"cannot enter a trade at a non-positive price: {fill!r}")
            if stake_amount is None:
                size = balance / (fill * (1.0 + fee))
            else:
                size = float(stake_amount) / fill
            position = _Position(
                direction=direction,
                entry_index=i,
                entry_price=fill,
                size=float(size),
                stop_price=_resolve_stop(
                    pending_entry.raw_stop, direction, float(closes[i - 1]), fill
                ),
            )
            pending_entry = None
        if pending_exit:
            pending_exit = False
            if position is not None:
                exit_price = _exit_fill(opens[i], position.direction, slip)
                trade = _make_trade(
                    position,
                    index=index,
                    exit_index=i,
                    exit_price=exit_price,
                    reason=ExitReason.SIGNAL,
                    fee_rate=fee,
                    params_id=params_id,
                )
                balance += trade.pnl
                trades.append(trade)
                position = None

        # 2. the static stop is checked intrabar, from the entry candle onwards.
        if position is not None and position.stop_price is not None:
            stop = position.stop_price
            stopped = lows[i] <= stop if position.direction is Direction.LONG else highs[i] >= stop
            if stopped:
                trade = _make_trade(
                    position,
                    index=index,
                    exit_index=i,
                    exit_price=stop,
                    reason=ExitReason.STOP_LOSS,
                    fee_rate=fee,
                    params_id=params_id,
                )
                balance += trade.pnl
                trades.append(trade)
                position = None

        # 3. a position still open on the last candle is closed at the last close.
        if position is not None and i == last:
            trade = _make_trade(
                position,
                index=index,
                exit_index=i,
                exit_price=float(closes[i]),
                reason=ExitReason.END_OF_DATA,
                fee_rate=fee,
                params_id=params_id,
            )
            balance += trade.pnl
            trades.append(trade)
            position = None

        # 4. mark to market at this candle's close.
        if position is None:
            equity_values[i] = balance
        elif position.direction is Direction.LONG:
            equity_values[i] = balance + position.size * (float(closes[i]) - position.entry_price)
        else:
            equity_values[i] = balance + position.size * (position.entry_price - float(closes[i]))

        # 5. signals are evaluated at this candle's close, filled on the next one.
        if i < last:
            if position is None:
                if entry_long[i]:
                    pending_entry = _PendingEntry(Direction.LONG, float(stop_column[i]))
                elif entry_short[i]:
                    pending_entry = _PendingEntry(Direction.SHORT, float(stop_column[i]))
            elif (position.direction is Direction.LONG and exit_long[i]) or (
                position.direction is Direction.SHORT and exit_short[i]
            ):
                pending_exit = True

    equity_curve = pd.Series(equity_values, index=index, name="equity", dtype="float64")
    return BacktestResult(
        strategy_name=str(strategy.name),
        symbol=str(symbol),
        timeframe=str(timeframe),
        start=pd.Timestamp(index[0]),
        end=pd.Timestamp(index[-1]),
        initial_balance=float(initial_balance),
        final_balance=float(equity_curve.iloc[-1]),
        trades=trades,
        equity_curve=equity_curve,
        params=strategy.params.model_dump(),
        metadata={
            "fee_rate": fee,
            "slippage": slip,
            "stake_amount": None if stake_amount is None else float(stake_amount),
            "allow_short": bool(short_enabled),
            "symbol": str(symbol),
            "timeframe": str(timeframe),
            "engine_version": ENGINE_VERSION,
        },
    )


def _params_id(params: Mapping[str, Any]) -> str:
    """Deterministic identifier of a parameter set (``""`` when there is none)."""
    if not params:
        return ""
    return "|".join(f"{key}={params[key]}" for key in sorted(params))


def run_backtest_on_config(
    cfg: AppConfig,
    data: pd.DataFrame,
    *,
    params: Mapping[str, Any] | None = None,
    symbol: str | None = None,
) -> BacktestResult:
    """Backtest the strategy configured in ``cfg`` over ``data``.

    The strategy is ``cfg.strategy.name``, configured with
    ``cfg.strategy.params`` overridden by ``params``; execution settings come from
    ``cfg.backtest.*`` (balance, stake, short switch), ``cfg.exchange.fee_rate`` /
    ``cfg.exchange.slippage`` and ``cfg.data.timeframe``.

    Shorts are enabled when either ``cfg.backtest.allow_short`` or the strategy
    parameter ``allow_short`` asks for them.
    """
    merged: dict[str, Any] = dict(cfg.strategy.params)
    if params:
        merged.update(dict(params))
    strategy = get_strategy(cfg.strategy.name, merged)
    return run_backtest(
        strategy,
        data,
        initial_balance=cfg.backtest.initial_balance,
        fee_rate=cfg.exchange.fee_rate,
        slippage=cfg.exchange.slippage,
        stake_amount=cfg.backtest.stake_amount,
        symbol=symbol if symbol is not None else DEFAULT_SYMBOL,
        timeframe=cfg.data.timeframe,
        allow_short=cfg.backtest.allow_short or bool(merged.get("allow_short", False)),
        params_id=_params_id(merged),
    )


def make_runner(cfg: AppConfig, *, symbol: str | None = None) -> RunnerFn:
    """Return the :data:`~trading_platform.core.models.RunnerFn` bound to ``cfg``.

    The returned callable only depends on the frame it is given (plus the frozen
    configuration) — the validation layer runs it over arbitrary sub-windows:

    ``runner(data, params=None) -> BacktestResult``
    """

    def _runner(data: pd.DataFrame, params: Mapping[str, Any] | None = None) -> BacktestResult:
        return run_backtest_on_config(cfg, data, params=params, symbol=symbol)

    return _runner
