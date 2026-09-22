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
configuration. Concretely, the basket curve is built the way an account holding
those profiles actually behaves: each symbol's equity curve is normalised by its
own first value and the **levels** are averaged, so the weights **drift** with
performance and the basket is *not* rebalanced back to equal weight every candle.
Holdout basket, fees 10 bp/side, long/short 4h, fast/mid/slow =
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

**An independent cross-sectional test agrees — and tempers the headline.** Every
number above was read on the development panel — the symbols that selected the
parameters. After the parameters were frozen, a **second symbol universe** was
downloaded from the same source (Binance spot monthly klines): **67 further
USDT pairs, disjoint from the 74 development symbols**, whose data was never
inspected before the finalists were frozen. The same frozen parameters, the same
protocol, the same fees (10 bp/side) and the same windows were re-run there, so
**no instrument is shared with the universe that selected the parameters**.

Equal-weight basket over the **HOLDOUT** (2023-01-01 → 2026-08-31):

| symbol universe | momentum 4h long/short | equal-weight buy & hold | share of symbols beating buy & hold |
| --- | --- | --- | --- |
| the 71 development-panel symbols | +155.7 %, Sharpe 0.772, maxDD −39.1 % | +37.5 %, Sharpe 0.486, maxDD −78.1 % | 64.8 % |
| the 67 fresh symbols | **+8.4 %, Sharpe 0.294, maxDD −47.4 %** | **−71.3 %, Sharpe 0.203, maxDD −96.1 %** | **71.6 %** |

Supporting per-symbol medians on the fresh universe (holdout, long/short 4h):
median Sharpe **0.250**, median return **−40.8 %**, median buy & hold **−79.7 %**,
**34.3 %** of symbols positive, **71.6 %** beating buy & hold, median max
drawdown **−83.6 %**, **114 trades**.

**The edge over buy & hold replicates.** On a universe the parameters had never
seen, the strategy still beat buy & hold by **+79.7 percentage points** (+8.4 %
against −71.3 %) and did so on **71.6 %** of the symbols — a *higher* hit rate
than the 64.8 % of the universe that selected it. In a window where the
equal-weight buy & hold of the fresh universe lost **−71.3 %** (the median fresh
symbol lost **−79.7 %** on buy & hold, with a **−96.1 %** basket drawdown), the
long/short variant finished **positive** while buy & hold lost nearly
everything.

**The absolute return does not replicate.** +8.4 % over 3.66 years is not
+155.7 %. The universes differ in composition — the development panel is
weighted towards large caps (BTC, ETH, SOL, BNB), while the fresh universe is
almost entirely small alts — and the second half of the holdout was a brutal alt
bear market. **The strategy is a defensive, alpha-over-buy-and-hold strategy
whose absolute return is regime-dependent, not a money printer.** A deployment
that cannot short gives up the bear-market half of the edge: on the fresh
universe the long-only variant returned **−15.2 %** (Sharpe 0.112) and the daily
variant **−25.2 %** (Sharpe 0.144).

**The fresh universe's own DEV window was strongly positive.** Before
2023-01-01, on the 46 fresh symbols that existed then, the same frozen
parameters returned **+534.1 %** (Sharpe 0.840) for the long/short 4h basket
against a buy & hold of **−15.9 %**, and **+747.7 %** for the daily variant. The
two universes therefore bracket a wide range of outcomes for the *same*
parameters: strongly positive on one window, merely defensive on the next.

**The practical consequence is the composition of the basket.** Run the strategy
as an equal-weight basket that **includes the majors** — the large caps the
development panel is weighted towards — not on a basket of small alts only. The
67 fresh symbols are a stress test of the least favourable case, not the
deployment target: they show the edge over buy & hold surviving out of universe,
and they show that the absolute return depends on which symbols the basket
holds.

**Statistical significance: the honest verdict.** Everything above is measured
on a development window that was *searched*, and a search of this size has to be
paid for. A formal multiple-testing and backtest-overfitting audit was therefore
run on the whole programme — trial count, deflated Sharpe ratio (Bailey &
López de Prado 2014, implemented from the definition), probability of backtest
overfitting (CSCV), Hansen's Superior Predictive Ability test, and a zero-skill
simulation on the same assets and the same windows. **Its verdict is
unfavourable to the development-window result**, and the honesty stance of
[`docs/testing-policy.md`](testing-policy.md) and §8 of
[`docs/backtesting-methodology.md`](backtesting-methodology.md) require it to be
recorded here rather than left out. The audit lives in the research scratch area
(`.scratch/research/STATS_AUDIT.md`, raw numbers in
`.scratch/research/results/stats_audit.json`), not in the shipped tree.

**How many trials were run.** The programme evaluated **3 414 (family,
configuration, timeframe) cells = 1 138 parameter sets** — **228 738 individual
development-window backtests**. Correlation clustering puts the effective number
of *independent* trials at roughly **900–2 700** (and 1 138 genuinely distinct
parameter sets); 3 414 is the hard upper bound.

**A pure-noise search of the same size produced a better-looking winner.**
Zero-skill strategies with random block exposure were simulated on the same
assets and the same windows, and their best-of-3 414 was compared with what the
programme actually found (development-window median-symbol Sharpe):

| | DEV median-symbol Sharpe |
| --- | --- |
| the delivered configuration (M1, 4h long/short) | **0.997** |
| the best cell of the whole sweep, rejected by the pre-registered plateau rule | 1.236 |
| null, zero-edge long/short rule: median of the best of 3 414 | **3.04** |
| null, long/flat rule keeping the market drift: median of the best of 3 414 | **1.39** |

**0 of 300** noise replications produced a best-of-3 414 below M1's figure: a
search of this size would have had to be unlucky to select the configuration
this programme selected.

**Deflated Sharpe Ratio.** M1 on the development window gives **0.034** on the
portfolio basis (N = 1 117) and **0.0002** on the median-symbol basis
(N = 3 414). The DSR's own null distribution at that (N, T) is centred on
**0.478** with a 5th percentile of **0.312**, so the observed value sits *below
the 5th percentile of what pure noise produces*. The other finalists are no
better — only the daily variant M3 reaches 0.383/0.510, a coin flip. On
sensitivity: using N = 1 558 instead of 3 414 gives 0.0232/0.0006, and using the
block-count effective sample size raises the portfolio-basis figure to 0.31.
**No convention reaches 0.5.**

**Minimum Backtest Length and the break-even trial count.** The trial count
implies a MinBTL of **16.4 years** at the observed development-window Sharpe;
the window is **5.37 years** (median symbol 2.69 years). The DSR would only
reach 0.95 if the programme had run **6 independent trials** on the portfolio
basis (0–25 depending on the finalist and the basis). It ran three orders of
magnitude more.

**Hansen's Superior Predictive Ability test.** Against buy & hold, over the full
4h search space on the development window: **p = 0.574** (White's Reality Check
0.653) — the best of 1 117 configurations does not beat buy & hold on the
development window. On the holdout, over the 7 frozen finalists: **p = 0.589**.

**The holdout is positive but not significant.** M1's own one-sided t-statistic
on the holdout is **t = 1.48 (p = 0.069)** before any multiple-testing
correction, and reaching t = 1.64 at that Sharpe would need **12.1 years**; the
holdout is 3.67 years.

**The one favourable diagnostic, and its limit.** The combinatorial
purged/blocked cross-validation PBO for the momentum family is **0.13–0.17**
(0.06–0.09 pooled over all 15 families), i.e. the *ranking* of parameters inside
the family is stable. But those blocks are still inside the development window
and in the same regime — precisely the stability the real holdout did not
confirm (portfolio Sharpe **1.82 → 0.76**) — and PBO says nothing about the
*level* of performance.

**The verdict.** The development-window result is not merely statistically
insignificant: **it is below what a search of this size would produce from noise
alone**. The out-of-sample record is positive — it survived a 3.67-year holdout
and replicated on a disjoint symbol universe — but **it is not statistically
distinguishable from luck at these sample sizes and this trial count**. The
strategy is therefore delivered as a **candidate** with a documented, plausible
but unproven edge, to be **paper-traded before any capital is committed** —
never read as a validated alpha.

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

**What the shipped CLI reports on real data.** The commands above, run on real
Binance spot 4h OHLCV exported to CSV with `config/backtest_momentum.json` and
`--risk-free-rate 0.05` (fees 10 bp/side, the configuration default), give one
trajectory per symbol:

| symbol | window | total return | Sharpe | max drawdown | trades | alpha vs buy & hold | walk-forward efficiency | is_consistent |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTC/USDT | 2017-08-17 → 2026-08-31 (19 789 candles) | +4 851 % | 0.98 | −49.1 % | 306 | +3 169 pp | 0.80 | yes |
| ETH/USDT | same window | +12 932 % | 1.05 | −66.0 % | 305 | — | 0.53 | yes |
| SOL/USDT | 2020-08-11 → 2026-08-31 | +12 369 % | 1.26 | −61.6 % | 195 | — | −0.15 | **no** |

On BTC/USDT the full gate set of the platform, computed by the shipped code, is:
`strategy_beats_benchmark` **true** (alpha +31.69, beta 0.046, correlation
0.058 — the strategy is nearly uncorrelated with holding the asset),
`is_consistent` **true**, walk-forward `efficiency` **0.802**, `is_robust`
**true** (36/36 parameter combinations positive, `positive_ratio` 1.0,
`robust_ratio` 1.0, stability 13.63), `strategy_beats_random` **true**
(`p_value` 0.0, `percentile` 100.0 from 1 000 random-entry simulations with the
same trade count and exposure), and the trade-order Monte Carlo (2 000
resamples) gives `prob_profit` 0.9825 with `VaR 95 %` +8.60.

Honest reading: the same parameters pass every gate on BTC and ETH, but **SOL
fails the walk-forward consistency gate** (`is_consistent` false, efficiency
−0.15) — a single symbol is a single draw, which is exactly why §5 argues for
the basket. These figures come from the same engine as everything else on this
page and are reproducible with the commands already listed in this section,
adding `--risk-free-rate 0.05` to match the configuration's benchmark setting.

## 7. Limitations

1. **The multiple-testing budget is spent.** 3 414 DEV (family, configuration,
   timeframe) cells were evaluated against a safe budget of roughly 45–55
   independent configurations for this sample length. The defence is *not* a
   corrected p-value: it is a pre-registered protocol, an untouched holdout and
   a fresh symbol universe. The finalists were frozen before the holdout was
   read, and the holdout degraded by ~30 % instead of collapsing — which is what
   an overfitted configuration would not produce. Treat the holdout figures as
   the only ones with a clean interpretation. The multiple-testing audit of §5
   makes the discount explicit: the delivered configuration's development-window
   performance must be discounted accordingly, because a search of this size
   produces a better-looking winner from noise alone.
2. **The result depends materially on the symbol universe.** Two disjoint
   universes were run with the same frozen parameters (§5), and they bracket a
   wide range of outcomes: the 71 development-panel symbols returned **+155.7 %**
   over the holdout against a buy & hold of **+37.5 %**, while the 67 fresh
   symbols returned **+8.4 %** against a buy & hold of **−71.3 %**. The edge
   over buy & hold replicates on both (**+79.7 pp**, and **71.6 %** of the fresh
   symbols beat it); the absolute return does not, because it follows the
   composition of the basket (large caps versus small alts) and the regime of the
   window.
3. **Survivorship is reduced, not eliminated.** Delisted symbols are in the
   panel, but the 74 symbols are those that had a Binance USDT pair at all.
4. **One venue, one quote currency.** Binance spot, USDT. No funding, no borrow,
   no leverage, no market impact, no partial fills.
5. **The panel is alt-heavy.** The median panel symbol *lost* 53 % over the
   holdout, so the +37.5 % buy & hold it is compared against is far weaker than
   BTC's. On a single large cap the edge over buy & hold is smaller.
6. **Fees changed during the sample** (Binance ran a near-zero-fee promotion from
   mid-2022 to March 2023). A flat 10 bp/side is applied throughout: conservative
   for that window, correct elsewhere.
7. **The basket is drifting-weight, not rebalanced.** Each symbol's curve is
   normalised by its first value and the levels are averaged, so a symbol that
   performs well grows to a larger share of the basket. A deployment that
   re-weights back to equal capital per profile will differ; the difference is
   second-order but not zero.
8. **No paper-trading period was run.** A green backtest is a necessary
   condition, never a sufficient one — dry-run first.
9. **The 1h grid degrades most on the holdout** (its median return turns negative
   at 15 bp), so **4h and 1d are the deployable grids**, even though 1h looks
   fine on DEV.
10. **The data carries one known splice.** `LUNAUSDT` mixes the pre-collapse
    token and the relaunched one under the same ticker: its close moves from
    `5.0e-05` to `8.6` in a single 4h candle on 2022-05-31, a `+1.7e5×` artefact
    that destroys any *rebalanced* buy & hold benchmark built from raw closes.
    It does not move the numbers on this page: the drifting-weight basket above
    is insensitive to it (the symbol's weight had already collapsed), and the
    `4×ATR` stop caps what a short can lose on that candle. Re-measured with the
    symbol excluded, the DEV basket changes by at most **0.4 index points out of
    ~15** (`+1525.5 %` → `+1485.8 %` for the long/short variant) and the holdout
    basket by less than **1 point** (`+155.7 %` → `+154.8 %`). Anyone rebuilding
    the panel should still mask that candle.

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
