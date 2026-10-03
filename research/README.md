# Strategy research programme

This directory holds the diagnosis, the experiments and the evidence behind the
five strategies and ten profiles added to the platform in 2026. It is **research
code, not platform code**: nothing under `src/` imports it, it is excluded from the
ruff/pytest scope, and deleting it would not change how the platform trades.

The work was commissioned to answer three questions:

1. why the worst-performing shipped strategies perform badly;
2. whether better versions of them can be built;
3. whether new strategies with a genuine literature-backed edge can be found.

---

## 1. Why the losing strategies lose

### 1.1 The measurement

`diagnose.py` replays every deployed profile over two years of candle history and
splits each result into the mechanisms an operator can act on: fee drag against the
gross result, the mix of exit reasons, and the holding period.

```bash
.venv/bin/python research/fetch_data.py --days 730      # cache OHLCV (once)
.venv/bin/python research/diagnose.py                   # the table below
```

Headline output (net = as realised, gross = before costs, cost = the charge):

| profile | net% | cost% | gross% | trades | win% | maxDD% | median bars |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dual-thrust-eth-15m | -100.0 | **488.0** | +388.0 | 3346 | 24.6 | -100.0 | 8 |
| momentum-sol-15m | -99.6 | 95.9 | -3.8 | 1672 | 19.4 | -99.7 | 5 |
| dual-thrust-dot-1h | -98.8 | 135.8 | +37.0 | 1040 | 33.1 | -99.0 | 6 |
| momentum-btc-1h | -80.3 | 32.4 | -47.9 | 494 | 19.8 | -81.6 | 5 |
| keltner-bnb-15m | -75.5 | 21.1 | -54.4 | 543 | 23.2 | -75.6 | 14 |
| donchian-eth-4h | +17.1 | 7.8 | +25.0 | 79 | 32.9 | -44.1 | 15 |
| faber-btc-1d | +30.0 | 0.5 | +30.4 | 5 | 40.0 | -20.0 | 7 |

### 1.2 The three defects

**Defect 1 — trading far too often for the volatility on offer.** On BNB 15m the
measured average candle range is 0.29% while a round trip costs 0.30%: **one round
trip costs about 1.0 ATR**. The signal would have to be right far more often than
any of these rules are just to break even. `dual-thrust-eth-15m` made 3346 trades
and paid 488% of the account in fees to produce a gross +388% — the fees, not the
idea, destroyed it. This is visible in the exit mix too: 93% of its exits are the
signal firing again, i.e. churn.

**Defect 2 — exits that fire immediately.** Both losing mean-reversion rules exit on
a condition that is **already true on the entry candle**. `bollinger` buys a
lower-band touch and exits when `close >= bb_middle` — but after a lower-band touch
the middle band is *above* the entry price, so the exit is satisfied on the very
next candle. Measured: 95 trades, **54.7% win rate**, profit factor 0.53, median
hold 13 bars. A high win rate with a losing profit factor is the signature of
"take the small win instantly, keep the rare large loss" — the trade is never given
room to mean-revert, which is the entire premise of the strategy. `keltner` has the
same shape (exit below the middle band, 100% of exits are that one line).

**Defect 3 — entries that are states, not events.** `momentum` enters when
`roc > 0`, which stays true for dozens of consecutive candles, so it re-enters
immediately after every exit. BTC 1h: 494 entries, median hold 5 bars, 100% of
exits are `impulse_lost`. The rule never distinguishes "momentum turned positive"
(the event the idea describes) from "momentum is positive" (a condition that is
simply true).

### 1.3 The significance test

`backtest.py` now reports the mean **log** return per trade and its t-statistic,
following the methodology correction in the literature review (§2 below): crypto
returns are so skewed (skew ≈ 14, kurtosis ≈ 466) that a positive arithmetic mean
can compound to a loss.

Every losing profile has a **negative** per-trade t-statistic, so the losses are
statistically real and not small-sample noise:

| profile | mean log return/trade | t-statistic |
| --- | --- | --- |
| dual-thrust DOT 1h | -0.429% | -6.12 |
| momentum BTC 1h | -0.329% | -5.23 |
| bollinger ETH 1h | -0.553% | -2.01 |
| supertrend BTC 1h | -0.366% | -1.95 |
| basic BTC 1h | -0.340% | -1.60 |

### 1.4 A caution about the live leaderboard

The live dashboard ranked `dual-thrust-dot-1h` **first** (+1.37%) and
`supertrend-btc-1h` fourth. Over two years of history those two profiles return
**-98.8%** (Sharpe -4.09) and **-54.3%** (Sharpe -1.20). Both had fewer than eight
closed trades on the live ledger. **The live leaderboard is not yet informative at
three days of age** — which is precisely why the new profiles are additive and the
originals are left running, so a live comparison accumulates.

---

## 2. The literature review

`literature/REVIEW.md` (~17,400 words) reviews the published evidence, with every
citation verified against Crossref/OpenAlex/RePEc. Its conclusions are
deliberately deflationary and three findings changed this work directly:

* **The harness was reporting a misleading statistic.** Crypto's skew means an
  arithmetic mean return can be significant while the compounded result is not
  (Jensen's inequality). The harness now reports mean log return, its t-statistic,
  turnover and Calmar alongside Sharpe.
* **The cost assumption was optimistic.** An industry measurement across 432 live
  round-trips at nine regulated providers puts retail round-trip costs at
  0.53–6.45%. This repository charged a flat 0.30%. `costs.py` re-derives the
  spread from the platform's own candles using the Corwin & Schultz (2012)
  high-low estimator — the proxy Brauneis et al. (2021, *JBF* 124, 106041)
  validate for crypto. Measured 1h effective spreads are **7.5 bp (BTC) to 19.9 bp
  (DOT)** one way, so the real hurdle is ~28–40 bp rather than 30 bp.
* **Several obvious "next ideas" are refuted.** Dip-buying is documented to invert
  on liquid majors (Zaremba et al. 2021); seasonality effects are an order of
  magnitude smaller than the fees needed to trade them (Mueller 2024; Baur et al.
  2019); cross-sectional momentum needs a short leg and a cross-section, so it is
  structurally impossible here.

> **A caveat on the cost tool.** The Corwin-Schultz estimator is sensitive to the
> sampling frequency: the same BTC market measures 8.5 bp on 1h candles, 21.0 bp
> when those candles are resampled to 4h and 45.3 bp at 1d. The market did not get
> six times more expensive — the estimator lost resolution. **Only the 1h figures
> are usable**, and even those are an order of magnitude, not a precise cost.

---

## 3. The redesigned strategies

Each redesign answers a specific defect from §1.2 rather than being a new idea.

| strategy | replaces | what changed | why |
| --- | --- | --- | --- |
| `keltner-breakout-v2` | `keltner` | exit is a Supertrend trail, not the midline; band widened 2.0 → 3.0 ATR; entry must be the *first* close above the band; expansion must clear the band by 0.3 ATR | a breakout cut at the midline can never pay for its false breaks |
| `trend-ensemble-v2` | `basic` | three horizons (50/100/200) vote instead of one crossover | a single crossover has a single failure point |
| `vol-targeted-trend` | — (new) | 200-period trend gated by a rolling volatility regime | avoids entries in the regime where trend rules whipsaw |

### 3.1 Results

Out-of-sample (last 40% of history, 10 pairs, 1h/4h/1d):

| candidate | mean Sharpe | mean edge vs buy&hold | beats B&H | mean maxDD |
| --- | --- | --- | --- | --- |
| **v2-keltner** | **+0.23** | **+21.1%** | **86.7%** | -12.2% |
| vol-targeted-trend | -0.04 | +15.6% | 73.3% | -23.2% |
| trend-ensemble | -0.21 | +8.9% | 56.7% | -34.9% |

`v2-keltner` on 4h is the standout and is the strategy this programme recommends:

* **pooled across 10 pairs: 123 trades, mean log return +3.09%/trade, t = 3.41** —
  significant by the literature's own bar;
* **walk-forward stable**: three sequential folds give t = 2.46, 1.43, 2.13, all
  positive — the edge is not one lucky period;
* **regime-robust**: positive in bull (+50.3%), bear (+16.5%) *and* chop (+38.5%)
  on 4h, at only ~8% exposure — rare, because most trend rules make their money
  only in a bull market;
* **parameter-robust**: Sharpe rises smoothly across band widths 1.5 → 3.5 (peak
  at 3.0), so it is a plateau and not a fitted spike.

Both shipped strategy files were verified **bit-for-bit equivalent** to the
research model that produced these numbers (0 signal mismatches on a reference
frame), so the backtest describes the code that actually ships.

---

## 4. The all-in variants

`faber-all-in` and `donchian-all-in` reproduce the entry and exit of the shipped
`faber` and `donchian` rules and change **only the risk envelope**, so the pairs
stay comparable in the dashboard. Run with `max_open_trades = 1`, Freqtrade's
`"unlimited"` staking commits the whole wallet to the single position and
compounds it.

The basis is the **two-year backtest Sharpe**, not the three-day live leaderboard
(§1.4):

| profile | live rank | 2-year Sharpe | 2-year return | 2-year maxDD |
| --- | --- | --- | --- | --- |
| faber-btc-1d | 3rd | **0.75** | +30.0% | -20.0% |
| faber-eth-1d | — | **0.61** | +34.1% | -38.5% |
| donchian-doge-1h | — | **0.43** | +19.7% | -63.4% |
| donchian-eth-4h | 2nd | **0.40** | +17.1% | -44.1% |

Two deliberate choices:

* **the stop is tightened, not widened.** The shipped `faber` uses `stoploss =
  -0.25` because a half-sized position can absorb twice the noise; at full size the
  same stop risks a quarter of the account on one trade. The all-in variants use
  -0.12 (Faber) and -0.15 (Donchian).
* **Donchian's exit became an ATR chandelier.** A fixed percentage cannot be right
  across assets whose volatility differs threefold (DOGE vs BTC); an ATR trail
  expresses risk relative to the asset, which is the correct primitive for a
  full-size position.

**The honest caveat.** A stop sweep showed that these strategies' own signals, not
their stops, drive the drawdown — tightening the stop inside the range that
actually binds *reduced* returns without reducing the max drawdown, because the
drawdown comes from holding through a slow decline. The tightened stop is a
**disaster brake for the all-in sizing**, not a drawdown solution. At full size the
-20% (Faber BTC) and -63% (Donchian DOGE) figures roughly double in account terms.

---

## 5. Reproducing all of it

```bash
.venv/bin/python research/fetch_data.py --days 730     # cache OHLCV to data/
.venv/bin/python research/diagnose.py                  # §1.1 defect attribution
.venv/bin/python research/sweep.py run                 # §3.1 out-of-sample table
.venv/bin/python research/regimes.py                   # per-regime behaviour
.venv/bin/python research/robustness.py --candidate v2-keltner --timeframe 4h
.venv/bin/python research/costs.py                     # measured cost hurdle
.venv/bin/python research/validate.py                  # resolver contract on the new files
```

| file | role |
| --- | --- |
| `backtest.py` | event-driven, cost-aware backtester (next-candle fills, log-return t-stat, Calmar, turnover) |
| `indicators.py` | shared indicator helpers (KAMA, Supertrend, percentiles) |
| `strategies_original.py` | faithful re-implementations of the ten shipped rules |
| `candidates.py` | the v2 redesigns and new research candidates |
| `run.py` / `sweep.py` | drivers, with a chronological train/test split |
| `diagnose.py` / `regimes.py` / `robustness.py` / `costs.py` / `validate.py` | the experiments above |
| `literature/REVIEW.md`, `literature/citations.json` | the evidence review and verified citations |
| `literature/sources/*.md` | the per-topic deep dives the review was built from |

## 6. Threats to validity

Stated plainly, because they bound how much weight these results carry:

1. **Survivorship bias.** `data/` holds nine pairs that are *currently listed*.
   Coins that went to zero are absent, which systematically flatters every
   long-only result here. Treat every number as an upper bound.
2. **The out-of-sample window is a bear/choppy market** (BTC -5.8%, ETH -15.1%).
   Beating buy-and-hold there is meaningful, but it is one regime, and a strategy
   that mainly avoids losses will look different in a strong bull market.
3. **`v2-keltner`'s trade count is low** (123 pooled trades, ~4-6 per pair per two
   years). t = 3.41 clears the bar, but a small sample is a small sample.
4. **The cost model is a floor**, not a full model: it omits market impact and
   assumes the taker fee. The measured spreads in §2 suggest the true hurdle is
   modestly higher than the 0.30% used throughout.
5. **Backtest results are not a prediction.** The platform's own documentation
   already states that the shipped strategies carry no out-of-sample validation
   claim; the same applies here, in full.
