"""A small event-driven backtester for long-only spot strategies.

The platform runs freqtrade, and freqtrade owns the real execution semantics. This
harness exists for *research*: it evaluates a signal function over cached OHLCV
and reports the numbers a strategy author iterates on (net return, Sharpe, max
drawdown, trade count, win rate, profit factor, time in market) under an explicit
and deliberately conservative cost model.

Keeping it independent of freqtrade is the point: a strategy that only looks good
inside one framework's assumptions is not evidence. The rules encoded here are
the ones that actually destroy naive backtests:

* **next-candle execution.** A signal computed on the close of candle ``i`` is
  filled at the **open of candle ``i+1``** -- never at the close that produced it.
  This alone removes most look-ahead optimism.
* **costs on both legs.** ``fee`` is charged on entry and on exit, plus
  ``slippage`` on each side, because the platform's generated config uses
  order-book pricing and a 0.1% taker fee.
* **single position, optional pyrading.** The platform's profiles hold one
  position per pair in practice; the harness models one position at a time by
  default.
* **intrabar stop/target ordering pessimism.** When a candle's range contains both
  the stop and the target, the **stop is assumed to hit first**. This is the
  conservative choice and it is what stops a backtest from inventing returns out
  of a wide bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import pandas as pd

#: One-way cost applied on top of the fee (spread + market impact).
DEFAULT_SLIPPAGE = 0.0005
#: Binance spot taker fee, matching the platform's real cost.
DEFAULT_FEE = 0.001
#: Candles per year, per timeframe -- used to annualise Sharpe.
PERIODS_PER_YEAR = {
    "5m": 105_120,
    "15m": 35_040,
    "30m": 17_520,
    "1h": 8_760,
    "2h": 4_380,
    "4h": 2_190,
    "6h": 1_460,
    "12h": 730,
    "1d": 365,
}


class SignalFn(Protocol):
    """A strategy: takes an OHLCV frame, returns entry/exit boolean columns."""

    def __call__(self, frame: pd.DataFrame) -> pd.DataFrame: ...


@dataclass
class Trade:
    """One completed round trip."""

    entry_index: int
    exit_index: int
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    return_pct: float
    exit_reason: str
    bars_held: int


@dataclass
class BacktestResult:
    """Everything a research loop wants to compare two rule sets on."""

    name: str
    timeframe: str
    pair: str
    trades: list[Trade] = field(default_factory=list)
    equity: pd.Series = field(default_factory=pd.Series)
    exposure: float = 0.0
    bars: int = 0

    # ---- headline metrics -------------------------------------------------
    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def total_return(self) -> float:
        if self.equity.empty:
            return 0.0
        return float(self.equity.iloc[-1] / self.equity.iloc[0] - 1.0)

    @property
    def cagr(self) -> float:
        if self.equity.empty or len(self.equity) < 2:
            return 0.0
        years = (self.equity.index[-1] - self.equity.index[0]).total_seconds() / (365.25 * 86_400)
        if years <= 0:
            return 0.0
        growth = self.equity.iloc[-1] / self.equity.iloc[0]
        if growth <= 0:
            return -1.0
        return float(growth ** (1 / years) - 1.0)

    @property
    def max_drawdown(self) -> float:
        """Worst peak-to-trough decline of the equity curve, as a negative ratio."""
        if self.equity.empty:
            return 0.0
        running_max = self.equity.cummax()
        drawdown = self.equity / running_max - 1.0
        return float(drawdown.min())

    @property
    def sharpe(self) -> float:
        """Annualised Sharpe of per-candle equity returns (risk-free = 0)."""
        if self.equity.empty or len(self.equity) < 3:
            return 0.0
        returns = self.equity.pct_change().dropna()
        std = float(returns.std())
        if std == 0 or not np.isfinite(std):
            return 0.0
        periods = PERIODS_PER_YEAR.get(self.timeframe, 8_760)
        return float(returns.mean() / std * np.sqrt(periods))

    @property
    def sortino(self) -> float:
        if self.equity.empty or len(self.equity) < 3:
            return 0.0
        returns = self.equity.pct_change().dropna()
        downside = returns[returns < 0]
        if downside.empty:
            return 0.0
        std = float(downside.std())
        if std == 0 or not np.isfinite(std):
            return 0.0
        periods = PERIODS_PER_YEAR.get(self.timeframe, 8_760)
        return float(returns.mean() / std * np.sqrt(periods))

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.return_pct > 0)
        return wins / len(self.trades)

    @property
    def profit_factor(self) -> float:
        gains = sum(t.return_pct for t in self.trades if t.return_pct > 0)
        losses = -sum(t.return_pct for t in self.trades if t.return_pct < 0)
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def avg_trade(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([t.return_pct for t in self.trades]))

    @property
    def avg_bars_held(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([t.bars_held for t in self.trades]))

    @property
    def expectancy_per_bar(self) -> float:
        """Return per candle held -- the fair way to compare timeframes."""
        if not self.trades or self.bars == 0:
            return 0.0
        return self.total_return / self.bars

    # ---- log-return view -------------------------------------------------
    # Crypto returns are extremely skewed (the literature review cites skew ~14,
    # kurtosis ~466 for this asset class). Under that distribution an arithmetic
    # mean return can be significantly positive while the compounded result is
    # zero or worse -- Jensen's inequality punishes variance in the compounded
    # series. Reporting only an arithmetic mean therefore overstates a strategy,
    # so the compounded (log) view is computed alongside it and used in the
    # verdicts.
    @property
    def log_return_per_trade(self) -> float:
        """Mean per-trade log return, the compounded view."""
        if not self.trades:
            return 0.0
        values = [np.log1p(t.return_pct) for t in self.trades if t.return_pct > -1.0]
        if not values:
            return -1.0
        return float(np.mean(values))

    @property
    def log_t_stat(self) -> float:
        """t-statistic of the mean per-trade log return.

        This is the honest significance test for a strategy whose trades are the
        unit of observation. It answers "is the average trade distinguishable from
        zero", which a Sharpe ratio computed on per-candle equity does not.
        """
        values = [np.log1p(t.return_pct) for t in self.trades if t.return_pct > -1.0]
        if len(values) < 3:
            return 0.0
        array = np.asarray(values, dtype=float)
        std = float(array.std(ddof=1))
        if std == 0 or not np.isfinite(std):
            return 0.0
        return float(array.mean() / (std / np.sqrt(len(array))))

    @property
    def turnover_per_year(self) -> float:
        """Round trips per year.

        Turnover belongs next to every Sharpe: two strategies with the same Sharpe
        but 5 and 500 trades per year are not comparable, because only one of them
        is exposed to the cost model being wrong.
        """
        if self.equity.empty or len(self.equity) < 2 or not self.trades:
            return 0.0
        years = (self.equity.index[-1] - self.equity.index[0]).total_seconds() / (365.25 * 86_400)
        if years <= 0:
            return 0.0
        return self.n_trades / years

    @property
    def calmar(self) -> float:
        """CAGR divided by max drawdown -- the metric the trend literature favours.

        The review's recommendation is explicit that success for a slow trend
        filter should be framed as drawdown and Calmar, not Sharpe, because Sharpe
        penalises the upside volatility that a trend rule deliberately accepts.
        """
        drawdown = abs(self.max_drawdown)
        if drawdown == 0:
            return 0.0
        return self.cagr / drawdown

    def exit_breakdown(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for trade in self.trades:
            out[trade.exit_reason] = out.get(trade.exit_reason, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def summary(self) -> dict[str, object]:
        return {
            "name": self.name,
            "pair": self.pair,
            "tf": self.timeframe,
            "return": round(self.total_return * 100, 2),
            "cagr": round(self.cagr * 100, 2),
            "sharpe": round(self.sharpe, 2),
            "sortino": round(self.sortino, 2),
            "calmar": round(self.calmar, 2),
            "maxdd": round(self.max_drawdown * 100, 2),
            "trades": self.n_trades,
            "win": round(self.win_rate * 100, 1),
            "pf": round(self.profit_factor, 2),
            "avg_tr": round(self.avg_trade * 100, 2),
            "log_tr": round(self.log_return_per_trade * 100, 3),
            "t": round(self.log_t_stat, 2),
            "turn": round(self.turnover_per_year, 1),
            "bars": round(self.avg_bars_held, 1),
            "expo": round(self.exposure * 100, 1),
        }


def run_backtest(
    frame: pd.DataFrame,
    signals: pd.DataFrame,
    *,
    name: str,
    pair: str,
    timeframe: str,
    fee: float = DEFAULT_FEE,
    slippage: float = DEFAULT_SLIPPAGE,
    stoploss: float | None = None,
    take_profit: float | None = None,
    max_bars: int | None = None,
) -> BacktestResult:
    """Simulate ``signals`` over ``frame`` and return the result.

    ``signals`` must carry boolean ``enter_long`` / ``exit_long`` columns aligned
    to ``frame``'s index. Entry fills at the next candle's open.
    """
    if len(frame) != len(signals):
        raise ValueError("signals and frame must have the same length")

    opens = frame["open"].to_numpy(dtype=float)
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    index = frame.index

    enter = signals["enter_long"].fillna(False).to_numpy(dtype=bool)
    exit_ = signals["exit_long"].fillna(False).to_numpy(dtype=bool)
    # An exit signal that would have to be read on the entry candle describes the
    # same bar; drop it so a trade cannot open and close on one decision.
    enter = enter & ~exit_

    n = len(frame)
    equity = np.ones(n, dtype=float)
    trades: list[Trade] = []

    # Equity is tracked as *cash + mark-to-market value of the open position*, so
    # the return of a long hold is applied once, not compounded every candle.
    cash = 1.0
    units = 0.0  # units of the base asset held
    in_position = False
    entry_price = 0.0
    entry_idx = 0
    bars_held = 0
    next_entry_ok = False
    next_exit = False

    def _exit_at(price: float, i: int, reason: str) -> None:
        """Close the open position at ``price``, banking the proceeds into cash."""
        nonlocal in_position, units, cash
        proceeds = units * price * (1 - fee)
        cash += proceeds
        units = 0.0
        trades.append(_close_trade(entry_idx, i, index, entry_price, price, fee, bars_held, reason))
        in_position = False

    for i in range(1, n):
        # --- execute decisions taken on the previous candle, at this open ---
        if in_position and next_exit:
            _exit_at(opens[i] * (1 - slippage), i, "signal")
        elif (not in_position) and next_entry_ok:
            price = opens[i] * (1 + slippage)
            notional = cash / (1 + fee)  # fee is charged on top of the stake
            units = notional / price
            cash -= notional * (1 + fee)
            entry_price = price
            entry_idx = i
            bars_held = 0
            in_position = True

        # --- intrabar protective orders -----------------------------------
        if in_position:
            bars_held += 1
            hit_stop = stoploss is not None and lows[i] <= entry_price * (1 + stoploss)
            hit_tp = take_profit is not None and highs[i] >= entry_price * (1 + take_profit)
            if hit_stop:
                # Pessimistic: the stop is assumed to fill before the target.
                _exit_at(entry_price * (1 + stoploss), i, "stop")
            elif hit_tp:
                _exit_at(entry_price * (1 + take_profit), i, "target")
            elif max_bars is not None and bars_held >= max_bars:
                _exit_at(closes[i] * (1 - slippage), i, "timeout")

        # Mark to market on this candle's close.
        equity[i] = cash + units * closes[i]

        # --- decisions for the next candle ---------------------------------
        next_entry_ok = bool(enter[i]) and not in_position
        next_exit = bool(exit_[i]) and in_position

    if in_position:
        _exit_at(closes[n - 1] * (1 - slippage), n - 1, "open_at_end")

    result = BacktestResult(
        name=name,
        timeframe=timeframe,
        pair=pair,
        trades=trades,
        equity=pd.Series(equity, index=index),
        bars=n,
    )
    held = sum(t.bars_held for t in trades)
    result.exposure = held / n if n else 0.0
    return result


def _close_trade(
    entry_idx: int,
    exit_idx: int,
    index: pd.Index,
    entry_price: float,
    exit_price: float,
    fee: float,
    bars_held: int,
    reason: str,
) -> Trade:
    """Build a :class:`Trade`, charging the fee on both legs."""
    gross = exit_price / entry_price
    net = gross * (1 - fee) * (1 - fee) - 1.0
    return Trade(
        entry_index=entry_idx,
        exit_index=exit_idx,
        entry_time=index[entry_idx],
        exit_time=index[exit_idx],
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=float(net),
        exit_reason=reason,
        bars_held=bars_held,
    )


def buy_and_hold(frame: pd.DataFrame, *, timeframe: str, pair: str) -> BacktestResult:
    """The benchmark every strategy must beat to be worth its risk."""
    opens = frame["open"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    n = len(frame)
    entry = opens[1] * (1 + DEFAULT_SLIPPAGE)
    units = (1.0 - DEFAULT_FEE) / entry
    equity = units * closes
    equity[0] = 1.0
    final = float(units * closes[-1] * (1 - DEFAULT_FEE))
    trade = Trade(
        entry_index=1,
        exit_index=n - 1,
        entry_time=frame.index[1],
        exit_time=frame.index[-1],
        entry_price=entry,
        exit_price=float(closes[-1]),
        return_pct=final - 1.0,
        exit_reason="buy_and_hold",
        bars_held=n - 1,
    )
    result = BacktestResult(
        name="buy_and_hold",
        timeframe=timeframe,
        pair=pair,
        trades=[trade],
        equity=pd.Series(equity, index=frame.index),
        bars=n,
    )
    result.exposure = 1.0
    return result


def format_table(results: list[BacktestResult]) -> str:
    """Render a list of results as a fixed-width comparison table.

    The layout leads with the compounded view and the significance of the average
    trade, because crypto's skew makes an arithmetic mean alone misleading.
    """
    header = (
        f"{'strategy':28} {'pair':10} {'tf':>3} {'ret%':>8} {'cagr%':>7} {'sharpe':>7} "
        f"{'calmar':>7} {'maxdd%':>7} {'n':>5} {'win%':>6} {'log_tr%':>8} {'t':>6} {'turn':>6}"
    )
    lines = [header, "-" * len(header)]
    for r in sorted(results, key=lambda x: -x.sharpe):
        s = r.summary()
        lines.append(
            f"{r.name[:28]:28} {r.pair[:10]:10} {r.timeframe:>3} {s['return']:>8.2f} "
            f"{s['cagr']:>7.2f} {s['sharpe']:>7.2f} {s['calmar']:>7.2f} {s['maxdd']:>7.2f} "
            f"{s['trades']:>5} {s['win']:>6.1f} {s['log_tr']:>8.3f} "
            f"{s['t']:>6.2f} {s['turn']:>6.0f}"
        )
    return "\n".join(lines)
