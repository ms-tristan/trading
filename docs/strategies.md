# Strategy catalogue

This page is the catalogue of the **ten** strategies shipped with the platform,
and the evidence behind the ones that were **not** shipped. `momentum` — the
research-validated one — and the historical `basic` reference are specified in
§1–§8; the **eight strategies added by the catalogue delivery** are specified in
§9, and §10 is the recipe for adding an eleventh. Three of the eight implement a
family the repository's protocol evaluated and **rejected** (§8): they ship as
hypotheses under test, with the recorded verdict attached, and nothing on this
page may be read as a validation of them.

Three statuses are used throughout, and they are never interchangeable:

| status | meaning |
| --- | --- |
| **research-validated** | the repository's own protocol cleared the family: an untouched holdout, a fresh symbol universe and the accounting of §5. Only `momentum` carries it. |
| **historical reference** | shipped as the minimal, self-explanatory strategy the platform started with. It was never put through the protocol, and it is **never** called validated: that is `basic`. |
| **hypothesis under test** | a faithful implementation of a published rule set, shipped so the operator can run and compare it. Either the research ledger records a **negative** verdict for its family (quoted in the subsection) or the family was **never swept** at all. Neither case is a validation. |

It complements [`docs/architecture.md`](architecture.md) (layers, frozen
interfaces, the write-once / expose-twice recipe) and
[`docs/backtesting-methodology.md`](backtesting-methodology.md) (the validation
protocol and its acceptance thresholds, including the walk-forward efficiency
line of §4.3 used below).

---

## 1. What the strategies are

The registered catalogue holds ten strategies. Every one of them lives in its own
module under `trading_platform.strategy`, is registered by the
`@register_strategy` decorator, and is reachable by name through
`trading_platform.strategy.registry.strategy_names()` — which is what the
realtime catalog of `GET /api/catalog` exposes, so the dashboard pickers can
never offer a name the engine would reject.

| `strategy.name` | Class | Module | One-line rule | Status |
| --- | --- | --- | --- | --- |
| `basic` | `BasicStrategy` | `trading_platform.strategy.basic` | fast/slow EMA crossover filtered by an RSI band, ATR stop (parameters in [`docs/usage.md`](usage.md) §3.4) | historical reference — never called validated |
| `momentum` | `MomentumStrategy` | `trading_platform.strategy.momentum` | multi-horizon rate-of-change agreement, optional ATR tail stop | **research-validated** (§5) |
| `donchian` | `DonchianStrategy` | `trading_platform.strategy.donchian` | close takes out the **previous** completed 20-candle high/low channel | hypothesis under test — family **rejected** (§8, §9.1) |
| `keltner` | `KeltnerStrategy` | `trading_platform.strategy.keltner` | close outside `EMA(20) ± 2 × ATR(10)`, filtered by an `EMA(100)` trend | hypothesis under test — no verdict (§9.2) |
| `bollinger` | `BollingerStrategy` | `trading_platform.strategy.bollinger` | close outside the `SMA(20) ± 2σ` band, exit back at the mean | hypothesis under test — family **rejected** (§8, §9.3) |
| `rsi_reversion` | `RsiReversionStrategy` | `trading_platform.strategy.rsi_reversion` | `RSI(2) < 10` while the close sits above `EMA(200)`, exit above `RSI 70` | hypothesis under test — no verdict (§9.4) |
| `macd` | `MacdStrategy` | `trading_platform.strategy.macd` | `MACD(12/26/9)` line above its signal line | hypothesis under test — no verdict (§9.5) |
| `supertrend` | `SupertrendStrategy` | `trading_platform.strategy.supertrend` | `ATR(10) × 3` trailing band, long while the close holds it | hypothesis under test — no verdict (§9.6) |
| `faber` | `FaberStrategy` | `trading_platform.strategy.faber` | close above its own 200-candle simple moving average (long-only by construction) | hypothesis under test — family **rejected** (§8, §9.7) |
| `dual_thrust` | `DualThrustStrategy` | `trading_platform.strategy.dual_thrust` | close takes out `open ± 0.5 ×` the previous 5-candle range | hypothesis under test — no verdict (§9.8) |

`momentum` is the strategy that came out of the research protocol documented in
§5. In one line:

> go long (or short) while at least two of the three rate-of-change horizons —
> fast, mid, slow — agree on the direction, flat otherwise, with an optional
> wide ATR stop as tail insurance.

It lives in `trading_platform.strategy.momentum` as `MomentumStrategy`
(`name = "momentum"`), registered in
`trading_platform.strategy.registry.STRATEGIES`. It is **not** re-exported by
`trading_platform.strategy.__init__`: import it from its own module. The same
holds for the eight strategies of §9 — the only strategy class the public
namespace re-exports is the `basic` reference.

## 2. The `momentum` rules

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

## 3. The `momentum` candle-grid inference

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

### 3.1 The warm-up requirement: the candles a frame must hold

The conversion has a hard consequence, and it is part of the strategy contract
rather than an implementation detail: `momentum_score` is `NaN` until the
*longest* horizon is defined, and `NaN` never fires a signal (§2). A frame must
therefore hold

```
required = max(1, round(slow_days * candles_per_day)) + 1
```

candles before `momentum` can emit **any** signal on a given grid — the longest
lookback, plus the one candle the first rate of change needs. `MomentumStrategy`
declares that number through `Strategy.required_candles(candles_per_day)`
(`strategy/base.py`), a pure member of the frozen strategy contract whose base
implementation returns `0`, "no warm-up requirement": that default is what the
historical `basic` reference keeps, because its EMAs, RSI and ATR are already
expressed in candles, so declaring a floor would invent a constraint it never
had. With the default `momentum` parameters (7 / 14 / 28 days, so
`slow_days = 28`):

| timeframe | candles/day | candles required (`28 x candles/day + 1`) |
| --- | --- | --- |
| `1m` | 1440 | **40321** |
| `5m` | 288 | **8065** |
| `15m` | 96 | **2689** |
| `30m` | 48 | **1345** |
| `1h` | 24 | **673** |
| `4h` | 6 | **169** |
| `1d` | 1 | **29** |

The operator rule is one line — `warmup_candles >= slow_days * candles_per_day` —
and the exact requirement is that **plus one candle**: `required_candles()`
returns `max(fast, mid, slow) + 1`, the `+1` being the first close on which the
score is defined at all. A profile that asks the stream for fewer candles than
the grid requires — `warmup_candles = 200`, the platform default, on every grid
of the table except `4h` and `1d` — can never warm up: the score stays `NaN` on
every row and the profile emits zero signals for ever. That situation is no
longer silent: the platform refuses such a profile where it is created, reports
it through `realtime check` and ends it with an actionable error at start
([`docs/realtime.md`](realtime.md) §8) — but the arithmetic above is what an
operator has to satisfy.

**What a large window costs.** Feeding an intraday grid its requirement is a
configuration change, not a code change, and it is paid once.
`data.loader.OHLCVLoader.load` is **cache-first**: it reads the parquet cache of
`(exchange, symbol, timeframe)` and calls `download()` only when that cache does
not cover the requested window. `download()` paginates at
`_PAGE_LIMIT = 1000` candles per request, so a 42000-candle `1m` window is
**~42 downloads** per `(exchange, symbol, timeframe)` — 42 paginated requests —
**the first time only**; from then on the parquet cache serves the whole window
without a network call. The same arithmetic gives about 9 requests on `5m`, 3 on
`15m`, 2 on `30m` and a single request on `1h`, `4h` and `1d`.

**A grid that runs is not a grid that was validated.** The grids this page
actually **validated** are **4h and 1d** — the deployable ones, and also the two
whose default 200-candle warm-up already exceeds the requirement (169 and 29).
`1h` was measured and **degrades**: its holdout median return turns negative at
15 bp/side (§7, item 9). The `5m`, `15m`, `30m` and `1m` grids are merely
**possible**: they were **never validated at all**, nothing on this page measures
them, and the table above only says what it would take to make the strategy run
there. Feeding a grid enough candles makes the strategy
**compute** — it does not make it **profitable**, and §5 is the only evidence
this page accepts about that. What the warm-up requirement does show is that the
strategy itself is not the limitation: measured on a 200 000-row `1m` frame, the
score is defined on 159 680 bars, 68 713 entry signals are emitted and the
engine closes 30 trades. The `1m` incident was a **missing configuration
contract** — a profile the platform accepted and could never feed — not a
strategy that cannot work on `1m`.

## 4. The execution model every strategy inherits

The eight strategies of §9 are ordinary `Strategy` subclasses too: they inherit
**exactly** the model below, which is the whole reason a new rule set can be
compared with the others at all. Only the candle-grid inference of §3 is
`momentum`-specific — every period of the eight is expressed in candles, so on
them there is nothing to convert.

`momentum` inherits the whole execution model of
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
`entry_lookback_candles` at `0` (its default) for `momentum` profiles. The same
caveat applies verbatim to every state-based rule of §9 — `macd` and the breakout
strategies `donchian`, `keltner`, `supertrend` and `dual_thrust` included — see
§9.0.

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
11. **The warm-up is a configuration contract, and running a grid is not
    validating it.** `momentum` needs `max(fast, mid, slow) + 1` candles before
    it can emit any signal: 40321 on `1m`, 8065 on `5m`, 2689 on `15m`, 1345 on
    `30m`, 673 on `1h`, 169 on `4h` and 29 on `1d` with the default 7/14/28-day
    parameters (§3.1). The operator rule is
    `warmup_candles >= slow_days * candles_per_day`, plus one candle for the
    exact requirement. An under-provisioned profile does not fail loudly by
    itself — it runs and produces zero signals for ever — which is why the
    platform now refuses an impossible profile where it is created, reports it
    through `realtime check` and ends it in `ERROR` at start
    ([`docs/realtime.md`](realtime.md) §8), and why feeding an intraday grid is a
    configuration decision with a first-download cost (§3.1). **Satisfying that
    requirement makes the strategy run, not profit**: the deliverable grids are
    the validated `4h` and `1d`; `1h` was measured and degrades on the holdout
    (item 9); `5m`, `15m`, `30m` and `1m` were never validated at all. A green
    `1m` run is a run, not evidence, and must not be read as one.

## 8. What was tested and rejected

These candidates were evaluated under the same protocol and are **not**
delivered as validated strategies, because they failed it. Three of them are
nonetheless **shipped** by §9 as hypotheses under test — the delivered module is
a faithful implementation of the published rule, and the verdict below travels
with it: it is quoted in the strategy's own module docstring, in §9 and here.
The remaining rows have no shipped implementation at all.

| candidate | outcome | why it fails |
| --- | --- | --- |
| Donchian channel breakout — **shipped** as `donchian` (§9.1) | DEV Sharpe **0.797**; holdout basket **−8.2 %**, Sharpe **0.161** | the breakout entry is strictly worse than the state-based momentum entry: it pays for entries momentum gets for free |
| Bollinger mean reversion — **shipped** as `bollinger` (§9.3) | DEV Sharpe **0.706** (max drawdown −0.32); holdout basket **+14.4 %** vs **+37.5 %** buy & hold, Sharpe **0.404** | it also fails the parameter-plateau rule on DEV (**0/108** eligible cells) and does not recover out of sample |
| price-versus-long-MA filter (Faber-style) — **shipped** as `faber` (§9.7) | DEV Sharpe **0.910**; not carried | never profitable in **2 of the 3** DEV folds (**0/143** regime-consistency cells): a long-only filter cannot be positive in a bear fold |
| trend/mean-reversion regime switch | worse than either leg alone | the combination hypothesis is rejected |
| volatility-gated momentum | the gate removes exposure without improving risk-adjusted return | rejected |
| multi-timeframe gate (daily regime on intraday entries) | DEV Sharpe 1.00 | indistinguishable from plain momentum while adding a second timeframe to maintain |
| cross-sectional momentum / carry | not measured | not expressible in the single-instrument, one-position engine (carry needs spot+perp legs) |
| TimesFM point forecast (round 1) | statistically a random walk (DM p = 0.47) | the sign rule loses money before fees |

The other five delivered strategies — `keltner`, `rsi_reversion`, `macd`,
`supertrend` and `dual_thrust` — have **no row here on purpose**: their families
were never swept by the protocol, so there is neither a pass nor a failure to
record. §9.2, §9.4, §9.5, §9.6 and §9.8 state that explicitly, and the absence
of a verdict is not a softer kind of validation.

## 9. The eight strategies of the catalogue delivery

`donchian`, `keltner`, `bollinger`, `rsi_reversion`, `macd`, `supertrend`,
`faber` and `dual_thrust` were added so the operator can run **several published
rule sets side by side** on the paper ledger and compare them under one engine.
They are faithful implementations of well-known rules — that is the whole point —
and **not** validated alphas: §8 records the negative verdict of the three
families the protocol did sweep, and the other five were never swept at all.
Each subsection below repeats its own status, on purpose.

### 9.0 The conventions all eight share

These are the invariants that make the eight interchangeable with `momentum` in
the engine and in the realtime layer. They are part of the contract, not
implementation detail.

* **Every period is in candles**, never in days, so the declared warm-up of a
  strategy is the *same number on every grid* (1m, 1h, 4h, 1d). This is the one
  respect in which they differ from `momentum`, whose `_days` parameters are
  converted through the frame's own spacing (§3). `faber`'s 200-candle average is
  therefore a 200-*candle* rule: it reproduces the published 200-**day** rule on
  the `1d` grid and approximates a 10-month average on the `4h` grid.
* **One module per strategy** under `src/trading_platform/strategy/<name>.py`,
  with the class `<Camel>Strategy`, the frozen parameters model
  `<Camel>StrategyParams`, its short alias `<Camel>Params`, the class variables
  `name`, `ParamsModel` and `PARAM_SPACE`, a module-level `INDICATOR_COLUMNS`
  tuple naming every column `prepare()` adds, and an
  `__all__ = ["<Camel>Params", "<Camel>Strategy", "<Camel>StrategyParams"]`.
  Registration goes through the `@register_strategy` decorator of
  `trading_platform.strategy.registry`, exactly like `basic` and `momentum`.
* **`prepare(data)`** calls `require_ohlcv_frame(data, name="data")`, adds the
  indicator columns **to that copy**, never mutates its argument and preserves
  the index exactly.
* **`signals(data)`** validates that the input is a *prepared* frame and returns
  `ensure_signal_frame(...)`: `entry_long`, `exit_long`, `entry_short`,
  `exit_short` are `bool` with **no `NaN`**, and `stop_loss` is `float64` where
  `NaN` means "no stop".
* **`required_candles(candles_per_day=1.0)` is total** — it never raises, not
  even for a degenerate spacing — and it shares the *same* private `_warmup`
  helper as `prepare()`, so the declared warm-up can never drift from the
  lookbacks actually computed.
* **A short frame logs `strategy.warmup_incomplete`** (a structured `WARNING`
  carrying `strategy`, `rows` and `required_candles`) and keeps going. A frame
  below the declared warm-up is legitimate in walk-forward windows; it is never
  an exception, and the warning fires only once per call.
* **Exactly `0.0` on `atr_stop_multiplier` means "no stop"**: the whole
  `stop_loss` column is `NaN`. It is *not* a stop at the close, and it is *not*
  a zero-distance stop. This is the rule `momentum` already follows;
  `supertrend` has no such parameter (its trailing band *is* the stop) and
  `faber` defaults to `0.0`, so its stop column is `NaN` unless the multiplier
  is raised.
* **A `NaN` indicator never fires a signal.** Nothing is ever `fillna(0)`-ed:
  `0` is a legitimate value of every one of these indicators, so filling it would
  open and close positions on the warm-up candles of every run.
* **Long/short mirroring is exact.** When `allow_short` is `False`,
  `entry_short` and `exit_short` are all-`False` `bool` series; when it is
  `True` they are the mirror image of the long side (thresholds inverted about
  the same level, `RSI` mirrored through `100 − x`). `faber` is the exception by
  construction: it has **no** `allow_short` parameter at all (§9.7).
* **Entries are states, not crossover events**, exactly as for `momentum`: the
  entry column stays `True` on every candle the condition holds, and it does not
  fire once on the candle that crossed a threshold. It is worth spelling out for
  `macd` (above its signal line) and for the breakout strategies (`donchian`,
  `keltner`, `supertrend`, `dual_thrust`), where a "breakout" is the *condition*
  of standing outside the channel, not the single candle that left it. The live
  consequence is the one of §4: never pair them with an
  `entry_lookback_candles > 0` profile.

**The declared warm-up, and what the platform's default budget can feed.** The
default warm-up of a profile that overrides nothing is
`DEFAULT_WARMUP_CANDLES = 200` candles
(`trading_platform.realtime.warmup`), and `GET /api/catalog` reports the
comparison per strategy and timeframe under its `warmup` key
([`docs/realtime.md`](realtime.md) §5). With the **default parameters**, the
requirement below is the *same* number on the four grids, which is what
"periods in candles" buys:

| strategy | declared warm-up | 1m | 1h | 4h | 1d | feedable on the 200-candle default? |
| --- | --- | --- | --- | --- | --- | --- |
| `donchian` | `max(entry_period, exit_period) + 2` | 22 | 22 | 22 | 22 | **yes** |
| `keltner` | `max(trend_ema_period, ema_period, atr_period) + 1` | 101 | 101 | 101 | 101 | **yes** |
| `bollinger` | `max(period, atr_period) + 1` | 21 | 21 | 21 | 21 | **yes** |
| `rsi_reversion` | `max(trend_ema_period, atr_period, rsi_period) + 1` | 201 | 201 | 201 | 201 | **no** (201 > 200) |
| `macd` | `fast_period + slow_period + signal_period` | 47 | 47 | 47 | 47 | **yes** |
| `supertrend` | `atr_period + 2` | 12 | 12 | 12 | 12 | **yes** |
| `faber` | `max(sma_period, atr_period) + 1` | 201 | 201 | 201 | 201 | **no** (201 > 200) |
| `dual_thrust` | `max(range_period, atr_period) + 2` | 16 | 16 | 16 | 16 | **yes** |

So `faber` and `rsi_reversion` need an **explicit** `warmup_candles` override of
at least 201 (and the matching `history_candles`) on every grid, while the other
six run on the default budget. This is the reason the profile catalogue of
[`docs/usage.md`](usage.md) derives `warmup_candles` as
`max(200, required_candles())` instead of hard-coding a number. A grid that *can*
be fed is
still not a grid that was *validated*: §7, item 11 applies to every strategy on
this page.

### 9.1 `donchian` — Donchian channel breakout

**Status: hypothesis under test.** The repository's protocol swept this family
and **rejected** it: the breakout configuration B1 (`120/40`, 4h, long/short)
reached a DEV Sharpe of **0.797**, then a holdout basket of **−8.2 %** with a
Sharpe of **0.161** — "the breakout entry is strictly worse than the state-based
momentum entry: it pays for entries momentum gets for free" (§8,
`.scratch/research/FINDINGS2.md` §3). The module ships the published rule with
that verdict quoted in its own docstring; it is **not** a validated strategy.

**Canonical definition and reference implemented.** Richard Donchian's channel
breakout — the four-week rule — in the form the Turtle traders traded it: a long
entry when the close takes out the highest high of the last *N* candles, an exit
when it takes out the lowest low of the last *M* candles, with `N = 20` and
`M = 10` as the published System 1 pair. Equal-and-opposite levels carry the
short side. The channel is shifted by one candle, so the comparison is against
the **previous completed** channel and never against a window that already
contains the candle being decided on.

| Parameter | Default | Bounds | Meaning |
| --- | --- | --- | --- |
| `entry_period` | `20` | `>= 2` | candles of the entry channel (highest high) |
| `exit_period` | `10` | `>= 1` | candles of the exit channel (lowest low); must be `< entry_period` |
| `atr_period` | `14` | `>= 2` | period of the ATR used by the stop column |
| `atr_stop_multiplier` | `2.0` | `>= 0.0` | stop distance in ATR units; **`0.0` means "no stop"** |
| `allow_short` | `false` | boolean | enables the two short-side columns |

The validator rejects `exit_period >= entry_period`, because an exit channel at
least as long as the entry channel can never confirm a breakout.

**Indicator columns**: `donchian_upper` (rolling maximum of `high` over
`entry_period`), `donchian_lower` (rolling minimum of `low` over `exit_period`),
`atr`.

```
entry_long  = close > donchian_upper.shift(1)
exit_long   = close < donchian_lower.shift(1)
entry_short = close < donchian_lower.shift(1)    (all False unless allow_short)
exit_short  = close > donchian_upper.shift(1)    (all False unless allow_short)
stop_loss   = close - atr_stop_multiplier * atr  (all NaN when the multiplier is 0.0)
```

**Warm-up**: `max(entry_period, exit_period) + 2` = **22** candles with the
defaults — identical on every grid.

### 9.2 `keltner` — Keltner channel breakout with a trend filter

**Status: hypothesis under test. No verdict recorded in the repository research
ledger — this family was never swept by the protocol.** The research families
(`.scratch/research/strategies.py`) contain a plain volatility breakout
(`t6_vol_breakout`) and a volatility-gated momentum, but **no** Keltner channel
and no long-EMA trend filter on it; nothing on this page may be read as a
validation, and the absence of a verdict is not a soft pass.

**Canonical definition and reference implemented.** Chester Keltner's channel —
originally the typical price ± a moving average of the high−low range — in the
variant that became standard: a moving average of the close with bands placed at
a multiple of the ATR, plus Linda Raschke's long-term trend filter. This
implementation uses `EMA(20)` for the middle band, `ATR(10)` scaled by `2.0` for
the width, and `EMA(100)` as the trend filter: a long needs a close **above the
upper band and above the trend**, which keeps the breakout on the right side of
the regime.

| Parameter | Default | Bounds | Meaning |
| --- | --- | --- | --- |
| `ema_period` | `20` | `>= 2` | period of the middle-band EMA |
| `atr_period` | `10` | `>= 2` | period of the ATR that sets the band width |
| `atr_multiplier` | `2.0` | `> 0.0` | band width in ATR units (a width of zero is refused, not treated as "no band") |
| `trend_ema_period` | `100` | `>= 2` | period of the trend-filter EMA; must be `> ema_period` |
| `atr_stop_multiplier` | `3.0` | `>= 0.0` | stop distance in ATR units; **`0.0` means "no stop"** |
| `allow_short` | `false` | boolean | enables the two short-side columns |

**Indicator columns**: `keltner_mid`, `keltner_upper`, `keltner_lower`,
`keltner_trend`, `atr`.

```
entry_long  = (close > keltner_upper) & (close > keltner_trend)
exit_long   = close < keltner_mid
entry_short = (close < keltner_lower) & (close < keltner_trend)   (all False unless allow_short)
exit_short  = close > keltner_mid                                 (all False unless allow_short)
stop_loss   = close - atr_stop_multiplier * atr  (all NaN when the multiplier is 0.0)
```

**Warm-up**: `max(trend_ema_period, ema_period, atr_period) + 1` = **101**
candles with the defaults — identical on every grid.

### 9.3 `bollinger` — Bollinger band mean reversion

**Status: hypothesis under test.** The protocol swept this family and
**rejected** it: the reversion configuration V1 (`1h`, long-only) reached a DEV
Sharpe of **0.706** (max drawdown −0.32), then a holdout basket of **+14.4 %**
against **+37.5 %** for buy & hold (Sharpe 0.404) — it failed the DEV
parameter-plateau rule with **0/108** eligible cells and did not recover out of
sample (§8, `.scratch/research/FINDINGS2.md` §3).

**Canonical definition and reference implemented.** John Bollinger's bands: a
20-period simple moving average with bands at ± 2 standard deviations of the
same window. **The standard deviation is the population estimator (`ddof = 0`)** —
pandas' own rolling default, and the convention the band width is defined with;
the sample estimator (`ddof = 1`) would widen every band by
`sqrt(period / (period − 1))`. The choice is fixed in
`trading_platform.strategy.indicators.rolling_std` (whose `ddof` argument is
explicit for exactly that reason) and in the module docstring of `bollinger.py`.
Mean reversion buys the lower band touch and exits at the mean; the short side is
its mirror.

| Parameter | Default | Bounds | Meaning |
| --- | --- | --- | --- |
| `period` | `20` | `>= 2` | window of the moving average **and** of the standard deviation |
| `num_std` | `2.0` | `> 0.0` | half-width of the band in standard deviations |
| `atr_period` | `14` | `>= 2` | period of the ATR used by the stop column |
| `atr_stop_multiplier` | `3.0` | `>= 0.0` | stop distance in ATR units; **`0.0` means "no stop"** |
| `allow_short` | `false` | boolean | enables the two short-side columns (mean reversion is long-only on spot by default) |

**Indicator columns**: `bollinger_mid`, `bollinger_std` (the population
estimator), `bollinger_upper`, `bollinger_lower`, `atr`.

```
entry_long  = close < bollinger_lower
exit_long   = close >= bollinger_mid
entry_short = close > bollinger_upper   (all False unless allow_short)
exit_short  = close <= bollinger_mid    (all False unless allow_short)
stop_loss   = close - atr_stop_multiplier * atr  (all NaN when the multiplier is 0.0)
```

**Warm-up**: `max(period, atr_period) + 1` = **21** candles with the defaults —
identical on every grid.

### 9.4 `rsi_reversion` — short-term RSI(2) pullback in a long-term uptrend

**Status: hypothesis under test. No verdict recorded in the repository research
ledger — this family was never swept by the protocol.** `RSI(2)` appears once in
the research notes as a *feature* of an unrelated test
(`.scratch/research/FINDINGS.md`, section B), never as a swept strategy family,
and no pullback rule was carried. That is neither a pass nor a failure: it is an
untested hypothesis, and it is never to be dressed up as validation.

**Canonical definition and reference implemented.** Larry Connors' short-term
mean-reversion rule (*Short Term Trading Strategies That Work*): buy a
two-period RSI dip **only while the close is above a long-term moving average**,
and exit into strength. The published rule is long-only on the index or on a
liquid instrument; the implementation keeps the long-term average at 200 candles
and defaults `entry_rsi` to `10`, `exit_rsi` to `70`.

| Parameter | Default | Bounds | Meaning |
| --- | --- | --- | --- |
| `rsi_period` | `2` | `>= 2` | period of the RSI oscillator |
| `entry_rsi` | `10.0` | `0 < x < 100` | long entry when the RSI is **below** this level |
| `exit_rsi` | `70.0` | `0 < x <= 100` | long exit when the RSI is **above** this level; must be `> entry_rsi` |
| `trend_ema_period` | `200` | `>= 2` | period of the trend EMA the pullback must happen above |
| `atr_period` | `14` | `>= 2` | period of the ATR used by the stop column |
| `atr_stop_multiplier` | `3.0` | `>= 0.0` | stop distance in ATR units; **`0.0` means "no stop"** |
| `allow_short` | `false` | boolean | enables the two short-side columns |

**Indicator columns**: `rsi`, `trend_ema`, `atr`.

```
entry_long  = (rsi < entry_rsi) & (close > trend_ema)
exit_long   = rsi > exit_rsi
entry_short = (rsi > 100 - entry_rsi) & (close < trend_ema)   (all False unless allow_short)
exit_short  = rsi < 100 - exit_rsi                            (all False unless allow_short)
stop_loss   = close - atr_stop_multiplier * atr  (all NaN when the multiplier is 0.0)
```

The short side mirrors the RSI thresholds through `100 − x`, which is what
"overbought instead of oversold" means on the same oscillator.

**Warm-up**: `max(trend_ema_period, atr_period, rsi_period) + 1` = **201**
candles with the defaults — identical on every grid, and **one candle above the
platform's default 200-candle budget**: a `rsi_reversion` profile needs an
explicit `warmup_candles` override (see §9.0).

### 9.5 `macd` — MACD signal-line crossover

**Status: hypothesis under test. No verdict recorded in the repository research
ledger — this family was never swept by the protocol.** `MACD` is used in the
research notes as an input *feature* of a separate test, never as a swept
strategy family, and no MACD rule was carried by the protocol.

**Canonical definition and reference implemented.** Gerald Appel's moving average
convergence divergence in its standard form: the difference of the 12- and
26-period EMAs is the MACD line, its 9-period EMA is the signal line, and their
difference is the histogram.

**The entries are STATE-based, not crossover events** — exactly like `momentum`
(§2): `entry_long` is `True` on **every** candle whose MACD line sits above its
signal line, not only on the candle that crossed. The live consequence is the
one of §4 and §9.0: never pair `macd` with an `entry_lookback_candles > 0`
profile, which would replay an already-running state as fresh entries.

| Parameter | Default | Bounds | Meaning |
| --- | --- | --- | --- |
| `fast_period` | `12` | `>= 2` | period of the fast EMA |
| `slow_period` | `26` | `>= 3` | period of the slow EMA; must be `> fast_period` |
| `signal_period` | `9` | `>= 2` | period of the signal EMA, applied to the MACD line |
| `atr_period` | `14` | `>= 2` | period of the ATR used by the stop column |
| `atr_stop_multiplier` | `3.0` | `>= 0.0` | stop distance in ATR units; **`0.0` means "no stop"** |
| `allow_short` | `false` | boolean | enables the two short-side columns |

**Indicator columns**: `macd_line`, `macd_signal`, `macd_histogram`, `atr`.

```
macd_line      = ema(close, fast_period) - ema(close, slow_period)
macd_signal    = ema(macd_line, signal_period)
macd_histogram = macd_line - macd_signal
entry_long  = macd_line > macd_signal
exit_long   = macd_line < macd_signal
entry_short = macd_line < macd_signal   (all False unless allow_short)
exit_short  = macd_line > macd_signal   (all False unless allow_short)
stop_loss   = close - atr_stop_multiplier * atr  (all NaN when the multiplier is 0.0)
```

**Warm-up**: `fast_period + slow_period + signal_period` = **12 + 26 + 9 = 47**
candles with the defaults — identical on every grid. The arithmetic is the
conservative stacking of the three exponential lookbacks and is stated in the
module docstring: `ema()` seeds on the first value, so the signal EMA is defined
well before a full `cycle × multiplier` warm-up would be needed, and the declared
number is deliberately the safe, explainable upper bound rather than a tuning
knob.

### 9.6 `supertrend` — ATR Supertrend trailing flip

**Status: hypothesis under test. No verdict recorded in the repository research
ledger — this family was never swept by the protocol.** The research families
contain a plain volatility breakout and a volatility-gated momentum, but no
trailing-band flip rule; no Supertrend family was ever swept.

**Canonical definition and reference implemented.** The ATR Supertrend of the
technical-analysis literature (the rule popularised on TradingView and in the
`pandas-ta`/`TA-Lib`-adjacent ecosystem): a pair of bands placed at
`(high + low) / 2 ± multiplier × ATR` whose *final* values are recursive, and a
direction that flips when the close crosses the previous final band.

**The computation is sequential by nature** and is implemented as an explicit,
deterministic **O(n) loop over NumPy arrays** — never an expanding `apply` over
pandas rows. Each row depends on the previous row's final bands and direction:

```
basic_upper = (high + low) / 2 + multiplier * atr
basic_lower = (high + low) / 2 - multiplier * atr

final_upper[t] = basic_upper[t]  if basic_upper[t] < final_upper[t-1] or close[t-1] > final_upper[t-1]
                 final_upper[t-1] otherwise
final_lower[t] = basic_lower[t]  if basic_lower[t] > final_lower[t-1] or close[t-1] < final_lower[t-1]
                 final_lower[t-1] otherwise

direction[t] = -1 if direction[t-1] == +1 and close[t] <  final_lower[t]
               +1 if direction[t-1] == +1 and close[t] >= final_lower[t]
               +1 if direction[t-1] == -1 and close[t] >  final_upper[t]
               -1 if direction[t-1] == -1 and close[t] <= final_upper[t]

supertrend[t] = final_lower[t] if direction[t] == +1 else final_upper[t]
```

The recursion is seeded on the first row where the ATR is defined, with
`direction = +1`. The `supertrend_direction` column carries `+1.0` / `-1.0` so
the regime is readable in a report.

| Parameter | Default | Bounds | Meaning |
| --- | --- | --- | --- |
| `atr_period` | `10` | `>= 2` | period of the ATR that sets the band width |
| `multiplier` | `3.0` | `> 0.0` | band width in ATR units |
| `allow_short` | `false` | boolean | enables the two short-side columns |

There is deliberately **no** `atr_stop_multiplier`: the trailing band **is** the
stop, so the parameter that carries the "exactly `0.0` means no stop" rule
elsewhere has no meaning here.

**Indicator columns**: `supertrend`, `supertrend_direction`, `atr`.

```
entry_long  = close > supertrend     (the long regime, direction = +1)
exit_long   = close < supertrend     (the flip to direction = -1)
entry_short = close < supertrend     (all False unless allow_short)
exit_short  = close > supertrend     (all False unless allow_short)
stop_loss   = supertrend  when it sits below the close, NaN otherwise
```

**The stop choice, stated.** The trailing line is used as the stop only where it
is *below* the close — that is, on a long entry the band is already the
risk-defining level. Where it sits above the close (the short regime) the long
side has no stop and the column is `NaN`, which the engine reads as "no stop"
rather than as a stop at the close. A short entry mirrors the same rule.

**Warm-up**: `atr_period + 2` = **12** candles with the defaults — identical on
every grid.

### 9.7 `faber` — the long-term moving-average timing rule

**Status: hypothesis under test.** The protocol swept this family and
**rejected** it: the price-versus-long-MA filter reached a DEV Sharpe of
**0.910** and was not carried, because it was **never profitable in 2 of the 3
DEV folds** (**0/143** regime-consistency cells) — "a long-only filter cannot be
positive in a bear fold" (§8, `.scratch/research/FINDINGS2.md` §3).

**Canonical definition and reference implemented.** Mebane Faber's timing rule
(*A Quantitative Approach to Tactical Asset Allocation*, 2007; *Global Asset
Allocation*, 2013): hold the asset while its price is above its 10-month simple
moving average, move to cash otherwise. It is **long-only, one asset, in or out** —
there is no short side and no second instrument.

**The published rule is a 200-DAY average.** `sma_period` is expressed in
**candles**, so this implementation reproduces the published rule on the `1d`
grid, where 200 candles are 200 days, and *approximates* it on the `4h` grid,
where 200 candles are ~33 days. That is why the profile catalogue puts `faber` on
the `1d` grid — and only there does the module reproduce the published rule; its
second `faber` profile on `4h` is a deliberate second reading of the same rule at
a shorter horizon ([`docs/usage.md`](usage.md)). On `1m` and `1h` a 200-candle
average is a different economic horizon entirely.

| Parameter | Default | Bounds | Meaning |
| --- | --- | --- | --- |
| `sma_period` | `200` | `>= 2` | window of the moving average, **in candles** (200 candles ≈ the published 200-day rule on `1d`) |
| `atr_period` | `14` | `>= 2` | period of the ATR used by the stop column |
| `atr_stop_multiplier` | `0.0` | `>= 0.0` | stop distance in ATR units; the default `0.0` means **"no stop"**, so the whole `stop_loss` column is `NaN` |

**There is deliberately no `allow_short` parameter.** The published rule is
long-only by construction, and because `StrategyParams` forbids unknown keys, a
configuration carrying `allow_short` for `faber` is **rejected loudly** instead
of being silently ignored.

**Indicator columns**: `faber_sma`, `atr`.

```
entry_long  = close > faber_sma
exit_long   = close < faber_sma
entry_short = all False   (no allow_short parameter exists)
exit_short  = all False
stop_loss   = close - atr_stop_multiplier * atr  (all NaN with the default 0.0)
```

**Warm-up**: `max(sma_period, atr_period) + 1` = **201** candles with the
defaults — identical on every grid, and **one candle above the platform's default
200-candle budget**: a `faber` profile needs an explicit `warmup_candles`
override (see §9.0).

### 9.8 `dual_thrust` — the Dual Thrust range breakout

**Status: hypothesis under test. No verdict recorded in the repository research
ledger — this family was never swept by the protocol.** The research families
sweep Donchian and Bollinger breakouts, but no open-anchored range breakout;
nothing on this page may be read as a validation of it.

**Canonical definition and reference implemented.** Michael Chu's Dual Thrust (as
published among the classic intraday range-breakout systems): today's range is
derived from the last *N* candles, two trigger lines are placed around today's
**open** — `open + k1 × range` and `open − k2 × range` — and a break of either
line opens a position, with the opposite line as the exit. The range is
`max(HH − LC, HC − LL)` over the previous `N` candles, where `HH`/`LL` are the
highest high and lowest low and `HC`/`LC` the highest and lowest close.

**The range is computed on the PREVIOUS candles**: the raw expression is shifted
by one row, so the current candle never contributes to the range it is compared
against. Both lines are anchored on the **open** of the candle being decided on —
the one price that is known before the candle resolves.

| Parameter | Default | Bounds | Meaning |
| --- | --- | --- | --- |
| `range_period` | `5` | `>= 2` | candles of the range window (the shifted one) |
| `k1` | `0.5` | `> 0.0` | distance of the buy line above the open, in range units |
| `k2` | `0.5` | `> 0.0` | distance of the sell line below the open, in range units |
| `atr_period` | `14` | `>= 2` | period of the ATR used by the stop column |
| `atr_stop_multiplier` | `3.0` | `>= 0.0` | stop distance in ATR units; **`0.0` means "no stop"** |
| `allow_short` | `false` | boolean | enables the two short-side columns |

**Indicator columns**: `dual_thrust_range`, `dual_thrust_buy_line`,
`dual_thrust_sell_line`, `atr`.

```
dual_thrust_range      = max(rolling_max(high, range_period) - rolling_min(close, range_period),
                             rolling_max(close, range_period) - rolling_min(low, range_period)).shift(1)
dual_thrust_buy_line   = open + k1 * dual_thrust_range
dual_thrust_sell_line  = open - k2 * dual_thrust_range
entry_long  = close > dual_thrust_buy_line
exit_long   = close < dual_thrust_sell_line
entry_short = close < dual_thrust_sell_line   (all False unless allow_short)
exit_short  = close > dual_thrust_buy_line    (all False unless allow_short)
stop_loss   = close - atr_stop_multiplier * atr  (all NaN when the multiplier is 0.0)
```

**Warm-up**: `max(range_period, atr_period) + 2` = **16** candles with the
defaults — identical on every grid.

## 10. Adding a strategy: the recipe

A new house strategy touches a handful of files and **one** shared registry.
Nothing else needs to know about it: the catalog route of `GET /api/catalog`, the
Freqtrade adapter and the profile catalogue all read the registry at call time.
The checklist is the recipe the eight strategies of §9 followed, in order.

1. **Write the module** — `src/trading_platform/strategy/<name>.py`. It carries a
   module docstring stating the one-line rule, the canonical reference it
   implements, the **status** (validated, rejected, or untested — with the
   recorded verdict when there is one), the exact entry/exit rules and the
   declared warm-up. Inside: `INDICATOR_COLUMNS` (a
   `tuple[str, ...]` naming **every** column `prepare()` adds), the frozen
   parameters model `<Camel>StrategyParams(StrategyParams)` with its cross-field
   validator and its short alias `<Camel>Params = <Camel>StrategyParams`, the
   `@register_strategy`-decorated class `<Camel>Strategy(Strategy)` with
   `name`, `ParamsModel` and `PARAM_SPACE`, `prepare()`, `signals()` and
   `required_candles()`, one private `_warmup(params)` helper shared by
   `prepare()` and `required_candles()`, and
   `__all__ = ["<Camel>Params", "<Camel>Strategy", "<Camel>StrategyParams"]`.
   Start from `src/trading_platform/strategy/momentum.py` (the reference house
   strategy) and copy an existing §9 module when the new rule is close to one.
2. **Register it** — append **one import** at the **bottom** of
   `src/trading_platform/strategy/registry.py`. The position is not cosmetic:
   every strategy module starts with
   `from trading_platform.strategy.registry import register_strategy`, so
   importing it from the *top* of `registry.py` would hand it a partially
   initialised module and every `import trading_platform.strategy` would raise
   `ImportError`. A house strategy must never be added to the `STRATEGIES`
   literal at the top either: the decorator is the single registration path, and
   a second one raises "already registered".
3. **Test it** — write `tests/test_strategy_<name>.py` covering the twelve
   mandatory groups: the OHLCV contract rejection, the prepared-frame contract
   (index preserved, input not mutated, indicator columns present), the exact
   signal-frame contract through `ensure_signal_frame`, `required_candles()` on
   the `1m` / `1h` / `4h` / `1d` grids, an entry/exit truth table on a small
   hand-built frame, the `NaN`-never-fires rule, the `allow_short` mirroring in
   both settings, the exact-`0.0`-means-no-stop rule wherever the strategy has
   `atr_stop_multiplier`, and the parameters validator rejecting incoherent
   cross-field combinations. Indicators added to
   `src/trading_platform/strategy/indicators.py` are tested in
   `tests/test_strategy_indicators.py` — the single, shared indicator test file.
4. **Expose it to Freqtrade** with the two-line shim:
   `src/trading_platform/strategy/freqtrade_<name>.py` (the adapter class, built
   by `make_freqtrade_strategy`; it needs the optional `[freqtrade]` extra) plus
   `user_data/strategies/<Camel>Strategy.py`, a literal subclass carrying the
   class name Freqtrade resolves. Freqtrade keeps a file only if its **text**
   contains `class <Name>(` and keeps the class only if its `__module__` equals
   the file stem, so an alias assignment would silently resolve to
   `(None, None)`. Finally add the `!user_data/strategies/<Camel>Strategy.py`
   allow-list line to `.gitignore`, which ignores `user_data/*` by default.
5. **Add profile(s) to the catalogue** —
   `src/trading_platform/profiles/catalogue.py`. Nothing is hand-typed there:
   `warmup_candles = max(200, required_candles())` and
   `history_candles = warmup_candles + 10` are both derived from the strategy's
   own requirement on the target grid, and a test walks the whole catalogue to
   prove it: a row declares its identity and its money, nothing else. Adding a
   profile is a data edit, never a code change (see
   [`docs/usage.md`](usage.md)).
6. **Document it** — a subsection in §9 of this page (or a new one in the same
   shape), with the parameters table, the indicator columns, the rules, the
   declared warm-up and the status.

**How many files a new strategy touches.** Measured on the eight strategies of
§9, one new strategy is:

* **4 new files** — `src/trading_platform/strategy/<name>.py`,
  `tests/test_strategy_<name>.py`,
  `src/trading_platform/strategy/freqtrade_<name>.py`,
  `user_data/strategies/<Camel>Strategy.py`;
* **3 shared files edited** — `src/trading_platform/strategy/registry.py` (one
  import line), `.gitignore` (one allow-list line), `docs/strategies.md` (its
  section);
* **1 more shared file edited, only if profiles are added** —
  `src/trading_platform/profiles/catalogue.py`, and then only data.

The Freqtrade exposure of a batch of strategies adds no file of its own: the
adapter contract is pinned once for all of them by
`tests/test_strategy_freqtrade_catalogue.py`, which walks the registry and asserts
that every house strategy has a loadable shim. A per-strategy
`tests/test_strategy_freqtrade_<name>.py` is added only when its adapter carries
bespoke behaviour, as `basic`'s does.

The shared files are the ones a **second** strategy of the same delivery touches
too, which is why they are edited late and in one place each: `registry.py`
carries every import, `.gitignore` every allow-list line, and the catalogue every
profile. No shared *logic* file is touched at all — the engine, the warm-up
arithmetic of `trading_platform.realtime.warmup`, the adapter and the web routes
are untouched by a new strategy.
