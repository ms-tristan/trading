# Strategy catalogue

This page is the catalogue of the strategies shipped with the platform, and the
evidence behind the ones that were **not** shipped. It currently documents the
research-validated `momentum` strategy next to the historical `basic` reference.

It complements [`docs/architecture.md`](architecture.md) (layers, frozen
interfaces, the write-once / expose-twice recipe) and
[`docs/backtesting-methodology.md`](backtesting-methodology.md) (the validation
protocol and its acceptance thresholds, including the walk-forward efficiency
line of §4.3 used below).

---

## 1. What the strategy is

`momentum` is the multi-horizon, day-scaled momentum strategy that came out of
the research protocol documented in §5. In one line:

> go long (or short) while at least two of the three rate-of-change horizons —
> fast, mid, slow — agree on the direction, flat otherwise, with an optional
> wide ATR stop as tail insurance.

It lives in `trading_platform.strategy.momentum` as `MomentumStrategy`
(`name = "momentum"`), registered in
`trading_platform.strategy.registry.STRATEGIES`. It is **not** re-exported by
`trading_platform.strategy.__init__`: import it from its own module.

| `strategy.name` | Class | Module | One-line rule |
| --- | --- | --- | --- |
| `basic` | `BasicStrategy` | `trading_platform.strategy.basic` | fast/slow EMA crossover filtered by an RSI band, ATR stop (the historical reference; parameters in [`docs/usage.md`](usage.md) §3.4) |
| `momentum` | `MomentumStrategy` | `trading_platform.strategy.momentum` | multi-horizon rate-of-change agreement, optional ATR tail stop — this page |

## 2. The rules

The parameters (frozen pydantic model `MomentumStrategyParams`, also published
under its short spelling `MomentumParams`, unknown keys rejected) are:

| Parameter | Default | Bounds | Meaning |
| --- | --- | --- | --- |
| `fast_days` | `7` | `>= 1` | short momentum lookback, in **days** |
| `mid_days` | `14` | `>= 2` | mid momentum lookback, in days; must be `> fast_days` |
| `slow_days` | `28` | `>= 3` | long momentum lookback, in days; must be `> mid_days` |
| `enter_score` | `0.6` | `0 < x <= 1` | score at or above which a long entry (and, mirrored, a short exit) is allowed |
| `exit_score` | `0.0` | `-1 <= x <= 1` | score at or below which a long is closed; must be `< enter_score` |
| `atr_period` | `14` | `>= 2` | period of the ATR used by the stop column |
| `atr_stop_multiplier` | `4.0` | `>= 0.0` | distance of the stop in ATR units; **`0.0` means "no stop"** |
| `allow_short` | `false` | boolean | enables (or not) the two short-side columns |

The cross-field invariants `fast_days < mid_days < slow_days` and
`exit_score < enter_score` are enforced by a model validator, so an incoherent
configuration fails at construction with a `StrategyError` naming the offending
fields.

**The score.** Each horizon contributes the *sign* of its rate of change, and
the score is the mean of the three signs:

```
momentum_fast  = roc(close, fast_days  converted to candles)
momentum_mid   = roc(close, mid_days   converted to candles)
momentum_slow  = roc(close, slow_days  converted to candles)
momentum_score = (sign(momentum_fast) + sign(momentum_mid) + sign(momentum_slow)) / 3
```

The score therefore lives in `{-1, -2/3, -1/3, 0, 1/3, 2/3, 1}`: it counts how
many horizons agree on a direction. With the default `enter_score = 0.6`,
`score >= 0.6` is exactly **"at least two of the three horizons are positive and
none is negative"** — the equivalence the research protocol selected the
strategy on.

**The signals.**

```
entry_long  = momentum_score >= enter_score
exit_long   = momentum_score <= exit_score
entry_short = momentum_score <= -enter_score    (all False unless allow_short)
exit_short  = momentum_score >= -exit_score     (all False unless allow_short)
stop_loss   = close - atr_stop_multiplier * atr (all NaN when the multiplier is 0.0)
```

Two properties matter when reading those lines:

* the entries are **state, not crossover events**: `entry_long` stays `True` on
  every candle whose score holds — it does **not** only fire on the candle that
  crossed the threshold (this has a live consequence, see §4);
* `NaN` never fires a signal. While any of the three horizons is still
  undefined the score is `NaN` — it is deliberately **not** filled with `0`,
  because `0` is a legitimate neutral score and a `fillna(0)` would open (and
  close) positions on the warm-up candles of every run.

`momentum_score` is not a "nice-to-have" diagnostic: it is what the strategy is
defined by, so the candle counts below are part of the contract.

## 3. The candle-grid inference

Lookbacks are expressed in **days**, never in candles, and converted from the
frame's own spacing:

```
candles_per_day = max(1, round(1440 / median spacing of the DatetimeIndex in minutes))
horizon         = max(1, round(days * candles_per_day))
```

so a 1h grid yields `candles_per_day = 24` (7/14/28 days → 168/336/672 candles),
a 4h grid yields `6` (→ 42/84/168) and a 1d grid yields `1` (→ 7/14/28). A
single-row frame, a degenerate (zero or negative) spacing, or a grid coarser
than one candle a day all safely fall back to `1`. The value actually used is
exposed as the constant `candles_per_day` column of the prepared frame, so a
report can never silently disagree with the parameters.

That conversion is not cosmetic: the same *economic* horizon wins on all three
candle grids (§5), which is a cross-timeframe consistency check the data cannot
fake. It is also why the parameter names carry the `_days` suffix: `7` means
"one week" on every timeframe, not "seven candles".

## 4. The execution model it inherits

`momentum` is an ordinary `Strategy`; it inherits the whole execution model of
[`trading_platform.strategy.engine`](architecture.md), which is:

* the decision is taken at the **close of candle `t`** and the fill happens at
  the **open of candle `t+1`** — no look-ahead, no same-candle fill;
* fees are charged on **both legs** (`exchange.fee_rate`, 10 bp/side by default)
  and optional slippage is applied to every fill;
* the equity is compounded on the **full equity** by default
  (`backtest.stake_amount = null`), and the engine holds **one position at a
  time**: a `entry_short` while long is not a reversal, it is ignored until the
  long is closed;
* the stop price is read from the **signal candle** and applied statically;
  `stop_loss` is the **long** price, and for a short entry the engine mirrors
  the same distance about the entry candle's close;
* every comparison is against the prepared frame, so an undefined (`NaN`) score
  or ATR means "no signal" / "no stop", never `0`.

**Live caveat.** Because `entry_long` is a state and not a cross event, never
pair `momentum` with a profile whose `entry_lookback_candles > 0`. That
catch-up window exists for crossover strategies (it makes a missed cross
actionable for N candles); read on a state-based strategy it would treat the
last N candles of an already-running trend as fresh entry events. Leave
`entry_lookback_candles` at `0` (its default) for `momentum` profiles.

## 5. The validation evidence

Everything below was measured by the research harness, which is a NumPy clone of
the **shipped** engine (`trading_platform.strategy.engine`), proven bit-identical
by a 60/60 differential test. These numbers do **not** come from a single-symbol
CLI run of the platform, and §6 explains why they cannot.

**Data.** Binance spot monthly klines, **74 USDT pairs** (56 survivors + 18
delisted/collapsed symbols, included to blunt survivorship bias), 1h native,
resampled to 4h and 1d — **3.87 M candles**, 2017-08-17 → 2026-08-31. Fees are
**10 bp per side** throughout.

**Protocol.** `DEV` = everything before 2023-01-01 (5.37 years, 67 symbols);
`HOLDOUT` = 2023-01-01 → 2026-08-31 (3.66 years, 71 symbols), read **once**,
after the parameters were frozen.

**The horizon is a plateau, not a peak.** Median Sharpe of long-only
multi-horizon momentum by lookback triple on DEV, no stop:

| lookbacks (days) | 1h | 4h | 1d |
| --- | --- | --- | --- |
| 5 / 10 / 15 | 0.72 | 0.76 | 0.81 |
| **7 / 14 / 28** | 0.99 | 1.03 | **1.06** |
| **10 / 20 / 30** | 0.94 | 1.05 | **1.10** |
| 30 / 60 / 90 | 0.79 | 0.80 | 0.72 |

A broad plateau from roughly one to eight weeks, bracketed by worse performance
on both sides — and the same economic horizon wins on all three candle grids.

**The basket is the deployment unit.** The strategy is meant to be run as an
**equal-weight basket of many symbols**, one profile per symbol, which is what
the realtime layer does. Running it on a single symbol is not the tested
configuration. Holdout basket, fees 10 bp/side, long/short 4h, fast/mid/slow =
7/14/28 days, `enter_score 0.6`, `exit_score 0.0`, `atr_stop_multiplier 4.0`:

| | DEV (5.37 y, 67 sym) | HOLDOUT (3.66 y, 71 sym) |
| --- | --- | --- |
| momentum 4h, long/short | Sharpe 1.111 | **+155.7 % (CAGR 29.2 %), Sharpe 0.772, maxDD −39.1 %** |
| momentum 4h, long-only | Sharpe 1.148 | **+140.0 % (CAGR 27.0 %), Sharpe 0.837, maxDD −34.9 %** |
| equal-weight buy & hold | Sharpe 0.863 | +37.5 %, Sharpe 0.486, maxDD −78.1 % |
| alpha of the long/short variant | — | **+118 pp** |
| alpha of the long-only variant | — | **+103 pp** |

Walk-forward efficiency DEV → HOLDOUT is therefore **0.69** (`0.772 / 1.111`)
and **0.73** (`0.837 / 1.148`), both above the repository's documented **0.5**
"acceptable degradation" line of
[`docs/backtesting-methodology.md`](backtesting-methodology.md) §4.3. The
degradation is real — the Sharpe roughly halves — and expected: DEV contains two
full cycles and a −91 % basket drawdown, the holdout is another regime.

**Per-symbol holdout medians** (not the basket), long/short 4h: Sharpe **0.47**,
return **+25.6 %**, max drawdown **−71.3 %**, and **64.8 %** of the symbols beat
buy & hold. Long-only: Sharpe **0.38**, return **+25.3 %**, **74.6 %** beating
buy & hold. The average pairwise correlation of the per-symbol return streams is
**0.30** — that is the whole diversification argument: the same strategy that
draws down −71 % on one symbol draws down −39 % (long/short) or −35 %
(long-only) as a basket.

**Costs do not decide it, but they decide the timeframe.** DEV median Sharpe,
long/short 4h: **0.977** at 4 bp/side, **0.970** at 10 bp, **0.950** at 10 bp +
5 bp slippage, **0.940** at 15 bp + 5 bp slippage. Turnover is **10–25 round
trips per year**, i.e. a fee drag of a few percent a year.

**The stop is free insurance.** An ATR stop of 0x, 4x or 8x changes the median
DEV Sharpe by **less than 0.05**: the momentum exit already *is* the stop, and a
wide stop only removes tail risk without costing performance. That is why the
default is `atr_stop_multiplier = 4.0` and why `0.0` (no stop) is a legitimate
setting, not a degenerate one.

## 6. Reproduce it

The strategy, its parameters and the platform plumbing are exercised offline by
`tests/test_strategy_momentum.py`. The research numbers of §5 are reproduced
with the study scripts, not with the single-symbol CLI; what the CLI can
reproduce is the plumbing and a one-symbol trajectory:

```bash
# 1. the two shipped configurations are valid
.venv/bin/python -m trading_platform.cli config validate --config config/backtest_momentum.json
.venv/bin/python -m trading_platform.cli config validate --config config/backtest_momentum_spot.json

# 2. fill the cache with the real 4h history of BTC/USDT (the only network step)
.venv/bin/python -m trading_platform.cli data download \
    --config config/backtest_momentum.json \
    --symbol BTC/USDT --timeframe 4h \
    --start 2017-08-17 --end 2026-08-31

# 3. the three validation commands of the platform, on one symbol
.venv/bin/python -m trading_platform.cli backtest      --config config/backtest_momentum.json
.venv/bin/python -m trading_platform.cli walk-forward  --config config/backtest_momentum.json
.venv/bin/python -m trading_platform.cli robustness    --config config/backtest_momentum.json

# 4. the spot-only variant (no short side)
.venv/bin/python -m trading_platform.cli backtest      --config config/backtest_momentum_spot.json
```

Honest note: step 3 runs **one symbol**. The basket table of §5 needs one
profile per symbol — the realtime layer (N profiles on the shared wallet) or the
research scripts — so a single-symbol CLI backtest is **not** expected to
reproduce the basket figures, and a different result there is not a
contradiction.

## 7. Limitations

1. **The multiple-testing budget is spent.** 3 414 DEV (family, configuration,
   timeframe) cells were evaluated against a safe budget of roughly 45–55
   independent configurations for this sample length. The defence is *not* a
   corrected p-value: it is a pre-registered protocol, an untouched holdout and
   a fresh symbol universe. The finalists were frozen before the holdout was
   read, and the holdout degraded by ~30 % instead of collapsing — which is what
   an overfitted configuration would not produce. Treat the holdout figures as
   the only ones with a clean interpretation.
2. **Survivorship is reduced, not eliminated.** Delisted symbols are in the
   panel, but the 74 symbols are those that had a Binance USDT pair at all.
3. **One venue, one quote currency.** Binance spot, USDT. No funding, no borrow,
   no leverage, no market impact, no partial fills.
4. **The panel is alt-heavy.** The median panel symbol *lost* 53 % over the
   holdout, so the +37.5 % buy & hold it is compared against is far weaker than
   BTC's. On a single large cap the edge over buy & hold is smaller.
5. **Fees changed during the sample** (Binance ran a near-zero-fee promotion from
   mid-2022 to March 2023). A flat 10 bp/side is applied throughout: conservative
   for that window, correct elsewhere.
6. **The analysis rebalances every candle.** A real deployment rebalances by
   re-running the profiles; the difference is second-order, but it is not zero.
7. **No paper-trading period was run.** A green backtest is a necessary
   condition, never a sufficient one — dry-run first.
8. **The 1h grid degrades most on the holdout** (its median return turns negative
   at 15 bp), so **4h and 1d are the deployable grids**, even though 1h looks
   fine on DEV.

## 8. What was tested and rejected

These candidates were evaluated under the same protocol and are **not**
delivered, because they failed it:

| candidate | outcome | why it fails |
| --- | --- | --- |
| Donchian channel breakout | holdout basket **−8.2 %** | the breakout entry is strictly worse than the state-based momentum entry: it pays for entries momentum gets for free |
| Bollinger mean reversion | holdout basket **+14.4 %** vs **+37.5 %** buy & hold | it also fails the parameter-plateau rule on DEV, and does not recover out of sample |
| price-versus-long-MA filter (Faber-style) | never profitable in **2 of the 3** DEV folds | a long-only filter cannot be positive in a bear fold; it fails the regime-consistency rule |
| trend/mean-reversion regime switch | worse than either leg alone | the combination hypothesis is rejected |
| volatility-gated momentum | the gate removes exposure without improving risk-adjusted return | rejected |
| TimesFM point forecast (round 1) | statistically a random walk (DM p = 0.47) | the sign rule loses money before fees |
