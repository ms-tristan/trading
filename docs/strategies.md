# Strategies

The platform ships **fifteen** trading strategies. Every one of them is an
ordinary [freqtrade](https://www.freqtrade.io) `IStrategy` (interface version 3,
spot, long only), living in its own file under `user_data/strategies/`. The
supervisor passes that directory to every worker as `--strategy-path`, and the
worker's generated configuration names the class, so a strategy is a *file* to
this platform — nothing in the platform's own code knows a strategy by heart.

> **Sections 1 to 12 document the ten original strategies**, which are unchanged
> and still run. The five added by the 2026 research programme — three improved
> versions of the worst performers and two full-size variants of the best
> long-run trend profiles — are documented in [§13](#13-the-research-programme-fifteen-strategies-since-2026),
> with the evidence behind them in `research/README.md`.

## Where a strategy is described

Two places, and both are read at run time:

| Place | Content | Read by |
| --- | --- | --- |
| `user_data/strategies/<Name>Strategy.py` | the **executable** rules: indicators, entry and exit conditions, stop loss, ROI table, startup candle count | the freqtrade worker |
| `config/strategies.json` | the **descriptive** catalogue: `id`, `class_name`, `file`, `title`, `category` (optional), `summary`, `description`, `indicators`, `timeframes`, `reference`, `risk_notes` | the catalogue loader, `GET /api/strategies`, the dashboard |

The metadata is what the operator reads; the file is what trades. Where a
metadata field and the code disagree (an indicator list that drifted, a summary
written before a rule changed), **the file is authoritative** — the sections
below always document the code, and quote the catalogue's own `summary`,
`timeframes` and `risk_notes` next to it.

`config/strategies.json` is optional for discovery: the loader scans
`user_data/strategies/*.py`, so a file with no metadata entry is still usable —
its title is derived from the class name and its description stays empty. See
[§12](#12-adding-a-strategy-the-two-file-recipe).

## Common properties of all fifteen

* exactly one class per file, whose name equals the file stem (`class
  BasicStrategy(IStrategy)` in `BasicStrategy.py`) — that is how freqtrade's
  resolver finds it;
* `INTERFACE_VERSION = 3`, `can_short = False` (the platform trades spot),
  `process_only_new_candles = True` (signals are evaluated on closed candles
  only) and `use_exit_signal = True`;
* an explicit `startup_candle_count`, `stoploss` and `minimal_roi`;
* every `enter_long` / `exit_long` assignment guarded by `volume > 0`, so an
  empty candle never produces a signal;
* indicators computed with `talib.abstract as ta` (plus plain pandas / NumPy
  where a rule is recursive);
* a module docstring naming the **public source of the idea** — the reference
  each rule set comes from.

| id | file | catalogue title | class timeframe | `stoploss` | `minimal_roi` |
| --- | --- | --- | --- | --- | --- |
| `basic` | `BasicStrategy.py` | EMA cross baseline | 5m | `-0.10` | `0: 0.08, 240: 0.04, 720: 0.0` |
| `momentum` | `MomentumStrategy.py` | Momentum breakout | 1h | `-0.10` | `0: 0.12, 480: 0.06, 1440: 0.0` |
| `rsi-reversion` | `RsiReversionStrategy.py` | RSI mean reversion | 15m | `-0.10` | `0: 0.06, 240: 0.03, 720: 0.0` |
| `bollinger` | `BollingerStrategy.py` | Bollinger band reversion | 15m | `-0.10` | `0: 0.05, 240: 0.025, 720: 0.0` |
| `macd` | `MacdStrategy.py` | MACD trend follow | 1h | `-0.10` | `0: 0.10, 480: 0.05, 1440: 0.0` |
| `donchian` | `DonchianStrategy.py` | Donchian channel breakout | 1h | `-0.08` | `0: 0.20, 1440: 0.10, 2880: 0.0` |
| `keltner` | `KeltnerStrategy.py` | Keltner channel breakout | 1h | `-0.10` | `0: 0.15, 720: 0.07, 2880: 0.0` |
| `supertrend` | `SupertrendStrategy.py` | Supertrend trailing stop | 15m | `-0.10` | `0: 0.10, 720: 0.05, 2880: 0.0` |
| `dual-thrust` | `DualThrustStrategy.py` | Dual Thrust range breakout | 5m | `-0.06` | `0: 0.02, 30: 0.01, 60: 0.0` |
| `faber` | `FaberStrategy.py` | Faber trend allocation | 1d | `-0.25` | `0: 1.0` |
| `keltner-breakout-v2` | `KeltnerBreakoutV2Strategy.py` | Keltner breakout v2 | 4h | `-0.10` | `0: 1.0` |
| `trend-ensemble-v2` | `TrendEnsembleV2Strategy.py` | Multi-horizon trend ensemble | 1d | `-0.15` | `0: 1.0` |
| `vol-targeted-trend` | `VolTargetedTrendStrategy.py` | Volatility-targeted trend | 4h | `-0.12` | `0: 1.0` |
| `faber-all-in` | `FaberAllInStrategy.py` | Faber trend allocation, full size | 1d | `-0.12` | `0: 1.0` |
| `donchian-all-in` | `DonchianAllInStrategy.py` | Donchian breakout, full size | 1h | `-0.15` | `0: 1.0` |

**The class `timeframe` is a default, not a constraint.** A profile declares its
own timeframe, the supervisor writes it into the generated configuration, and the
same class then runs on that grid — which is exactly how one strategy is compared
on two grids side by side. The catalogue's `timeframes` field lists the grids the
strategy is intended for, and the shipped profile catalogue runs it on these:

| id | catalogue `timeframes` | profiles in `config/profiles.json` |
| --- | --- | --- |
| `basic` | 1h, 4h | `basic-btc-1h`, `basic-eth-4h` |
| `momentum` | 15m, 1h, 4h | `momentum-btc-1h`, `momentum-sol-15m`, `momentum-eth-4h-live` |
| `rsi-reversion` | 15m, 1h | `rsi-reversion-btc-15m`, `rsi-reversion-ada-1h` |
| `bollinger` | 5m, 15m, 1h | `bollinger-eth-1h`, `bollinger-xrp-5m` |
| `macd` | 1h, 4h | `macd-btc-4h`, `macd-link-1h` |
| `donchian` | 1h, 4h | `donchian-eth-4h`, `donchian-doge-1h` |
| `keltner` | 15m, 1h | `keltner-sol-1h`, `keltner-bnb-15m` |
| `supertrend` | 1h, 4h | `supertrend-btc-1h`, `supertrend-avax-4h` |
| `dual-thrust` | 15m, 1h | `dual-thrust-eth-15m`, `dual-thrust-dot-1h` |
| `faber` | 1d | `faber-btc-1d`, `faber-btc-1d-live` |

Every strategy has at least two profiles, so two readings of the same rule set
are always comparable in the dashboard. Each profile starts from the same capital
(1000 USDT paper, 250 USDT live), so the comparison is meaningful.

> **What these strategies are, and what they are not.** They are faithful
> implementations of published rule sets, shipped so that an operator can run
> them and compare them on a paper ledger. They are **not** validated alphas: no
> strategy on this page carries an out-of-sample validation claim, and a green
> paper P&L is not evidence of an edge. `basic` is the reference baseline every
> other profile is compared against.

---

## 1. `basic` — moving-average crossover baseline

**File** `user_data/strategies/BasicStrategy.py` · **catalogue title** EMA cross
baseline · **reference** the classic dual moving-average crossover, published as
`SampleStrategy` in the freqtrade strategy documentation.

*Catalogue metadata.* Summary: "Minimal EMA crossover used as the platform
baseline." Timeframes: `1h`, `4h`. Risk notes: "Whipsaws in ranging markets; the
only exit is the opposite cross, so drawdowns can run."

**Implemented logic**

| | Rule |
| --- | --- |
| Indicators | `EMA(20)` (`ema_fast`), `EMA(50)` (`ema_slow`), `RSI(14)` (`rsi`) |
| Entry | `ema_fast` crosses **above** `ema_slow` while `rsi < 70` |
| Exit | `ema_fast` crosses **below** `ema_slow`, or `rsi > 78` |
| Parameters | periods 20 / 50 / 14, RSI entry ceiling 70, RSI exit floor 78, `startup_candle_count = 60` |
| Risk | `stoploss = -0.10`, `minimal_roi = {0: 0.08, 240: 0.04, 720: 0.0}` |

**Suitable timeframes.** The class default is 5m; the catalogue lists 1h and 4h,
which is where the platform runs it, and where the crossover is far less noisy.
On very short grids the rule trades often and pays the fee and the spread on
every whipsaw.

**Risk notes.** A crossover is a lagging signal: it enters after the move has
started and exits after it has turned, so it gives back part of every swing. It is
the *baseline*, not an edge — its value is that it makes the other nine
comparable to something simple and transparent.

---

## 2. `momentum` — rate of change inside a confirmed trend

**File** `user_data/strategies/MomentumStrategy.py` · **catalogue title**
Momentum breakout · **reference** Jegadeesh & Titman, *Returns to Buying Winners
and Selling Losers* (Journal of Finance, 1993), reduced to a single-asset
time-series filter.

*Catalogue metadata.* Summary: "Buys strength while price momentum stays positive
and above its trend filter." Timeframes: `15m`, `1h`, `4h`. Risk notes: "Momentum
crashes: it enters late in an extended move and gives back gains on a sharp
reversal."

**Implemented logic**

| | Rule |
| --- | --- |
| Indicators | `ROC(12)` (`roc`), `EMA(50)` (`ema_fast`), `EMA(200)` (`ema_slow`), `ADX(14)` (`adx`) |
| Entry | `roc > 0` **and** `ema_fast > ema_slow` **and** `adx > 20` |
| Exit | `ema_fast < ema_slow` (primary trend broken) or `roc < 0` (impulse lost) |
| Parameters | ROC period 12, EMAs 50 / 200, ADX period 14 with threshold 20, `startup_candle_count = 210` |
| Trailing stop | enabled: `trailing_stop_positive = 0.03` once `trailing_stop_positive_offset = 0.05` is reached (`trailing_only_offset_is_reached = True`) |
| Risk | `stoploss = -0.10`, `minimal_roi = {0: 0.12, 480: 0.06, 1440: 0.0}` |

**Suitable timeframes.** The catalogue lists 15m, 1h and 4h; the shipped profiles
use 1h, 15m and (live) 4h. Momentum needs trends to exist: on very short grids the
ADX > 20 filter rejects most of the time, and on very long grids the entry arrives
late in the move.

**Risk notes.** Trend following always loses in ranges — several small losses in a
row are the normal cost of waiting for the one large winner, which is why the
trailing stop (3 % behind the high, armed at +5 %) matters more than the target.
The 200-period EMA warm-up means a fresh worker produces no signal for its first
210 candles.

---

## 3. `rsi-reversion` — the RSI recovery out of oversold

**File** `user_data/strategies/RsiReversionStrategy.py` · **catalogue title** RSI
mean reversion · **reference** J. Welles Wilder, *New Concepts in Technical
Trading Systems* (1978), with the community-standard SMA(200) regime filter.

*Catalogue metadata.* Summary: "Fades oversold RSI readings inside a longer term
uptrend." Timeframes: `15m`, `1h`. Risk notes: "A persistent downtrend keeps RSI
oversold while price keeps falling; no short leg to hedge it."

**Implemented logic**

| | Rule |
| --- | --- |
| Indicators | `RSI(14)` (`rsi`), `SMA(200)` (`sma_slow`) |
| Entry | `rsi > 30` while the previous `rsi <= 30` **and** `close > sma_slow` |
| Exit | `rsi > 65` |
| Parameters | RSI period 14, oversold level 30, exit level 65, SMA period 200, `startup_candle_count = 210` |
| Risk | `stoploss = -0.10`, `minimal_roi = {0: 0.06, 240: 0.03, 720: 0.0}` |

**Suitable timeframes.** The catalogue lists 15m and 1h, which is what the
profiles use. The rule is a pullback buyer: it belongs on liquid majors, and it
loses its meaning on illiquid pairs, where a 30 RSI is a repricing rather than a
dip.

**Risk notes.** Buying weakness is buying a falling knife when the weakness is
informational: the SMA(200) filter is what separates "a dip inside an uptrend"
from "the start of a downtrend", and it delays the entry by design. The exit
(RSI > 65) is fast, so the win rate is high and the average win is small — a
handful of trends that never recover dominates the result.

---

## 4. `bollinger` — lower-band touch bought back into the trend

**File** `user_data/strategies/BollingerStrategy.py` · **catalogue title**
Bollinger band reversion · **reference** John Bollinger, *Bollinger on Bollinger
Bands* (2001).

*Catalogue metadata.* Summary: "Buys the lower Bollinger band touch and exits at
the middle band." Timeframes: `5m`, `15m`, `1h`. Risk notes: "Bands widen with
volatility, so a breakout trend can keep price outside the band for a long time."

**Implemented logic**

| | Rule |
| --- | --- |
| Indicators | `BBANDS(20, 2.0)` (`bb_lower`, `bb_middle`, `bb_upper`), `RSI(14)`, `SMA(200)` |
| Entry | `low <= bb_lower` **and** `rsi < 40` **and** `close > sma_slow` |
| Exit | `close >= bb_middle` (reversion complete) or `rsi > 65` |
| Parameters | band period 20, width 2.0 σ, RSI period 14 with entry ceiling 40, SMA period 200, `startup_candle_count = 210` |
| Risk | `stoploss = -0.10`, `minimal_roi = {0: 0.05, 240: 0.025, 720: 0.0}` |

**Suitable timeframes.** The catalogue lists 5m, 15m and 1h; the shipped profiles
use 5m and 1h. The 5m profile is the platform's shortest-horizon mean-reversion
reading and the most sensitive to fees and to the spread.

**Risk notes.** A band touch is a *volatility* statement, not a direction: the
RSI < 40 filter and the SMA(200) regime keep it from buying every expansion
lower. The target (the middle band) is close, so the strategy needs a high win
rate to pay for the cases where the band kept moving.

---

## 5. `macd` — histogram zero-line cross above the EMA(200)

**File** `user_data/strategies/MacdStrategy.py` · **catalogue title** MACD trend
follow · **reference** Gerald Appel's Moving Average Convergence Divergence
(1979), in its freqtrade documentation form.

*Catalogue metadata.* Summary: "Trades MACD crossovers filtered by the long EMA
regime." Timeframes: `1h`, `4h`. Risk notes: "MACD lags by construction: entries
arrive after a large part of the move and exits after the top."

**Implemented logic**

| | Rule |
| --- | --- |
| Indicators | `MACD(12, 26, 9)` (`macd`, `macdsignal`, `macdhist`), `EMA(200)` (`ema_slow`) |
| Entry | `macdhist` crosses **above** zero while `close > ema_slow` |
| Exit | `macdhist` crosses **below** zero |
| Parameters | 12 / 26 / 9, EMA period 200, `startup_candle_count = 210` |
| Risk | `stoploss = -0.10`, `minimal_roi = {0: 0.10, 480: 0.05, 1440: 0.0}` |

**Suitable timeframes.** The catalogue lists 1h and 4h, which is what the
profiles use. On 5m/15m the histogram crosses constantly and the strategy
degenerates into fee payment; the EMA(200) filter is what keeps it on the right
side of the regime.

**Risk notes.** MACD is built from moving averages, so it is doubly lagging: the
entry is late and the exit is later, and the strategy gives back a slice of every
move. The histogram cross is used rather than the slower signal-line cross to
limit that lag.

---

## 6. `donchian` — 20-candle channel breakout

**File** `user_data/strategies/DonchianStrategy.py` · **catalogue title** Donchian
channel breakout · **reference** Richard Donchian's channel breakout (the
four-week rule), popularised by the Turtle Traders (Dennis & Eckhardt, 1983).

*Catalogue metadata.* Summary: "Buys a new twenty period high and exits on the
opposite channel." Timeframes: `1h`, `4h`. Risk notes: "Breakouts fail often in
ranges, producing a string of small losses before a trend appears."

**Implemented logic**

| | Rule |
| --- | --- |
| Indicators | `donchian_high` = highest `high` of the previous 20 candles, `donchian_low` = lowest `low` of the previous 10, `ATR(14)` (`atr`), `chandelier_exit` = highest close of the previous 20 candles − `2 × ATR(14)` |
| Entry | `close > donchian_high` |
| Exit | `close < donchian_low` (channel exit) or `close < chandelier_exit` (volatility expansion against the trade) |
| Parameters | entry channel 20, exit channel 10, ATR period 14, chandelier multiplier 2.0, `startup_candle_count = 25` |
| Risk | `stoploss = -0.08`, `minimal_roi = {0: 0.20, 1440: 0.10, 2880: 0.0}` |

Both channels are shifted by one candle: the breakout is compared against the
**previous completed** channel, never against a window that already contains the
candle being decided on.

**Suitable timeframes.** The catalogue lists 1h and 4h, which is what the
profiles use. The shorter the grid, the more of the breakouts are noise; the
longer the grid, the fewer signals a paper ledger will collect.

**Risk notes.** Breakout systems live on a low win rate and a few very large
winners, and they are the family most exposed to false breakouts in ranging
markets — the price takes out the channel, fails, and the chandelier exit closes
the trade at a loss. The 20 % first target is deliberately far away so that a
genuine trend is not cut short.

---

## 7. `keltner` — volatility channel breakout with a trend filter

**File** `user_data/strategies/KeltnerStrategy.py` · **catalogue title** Keltner
channel breakout · **reference** Chester Keltner (1960), in the EMA/ATR
formulation popularised by Linda Raschke (*Street Smarts*, 1996).

*Catalogue metadata.* Summary: "Trades volatility adjusted breakouts of the
Keltner channel." Timeframes: `15m`, `1h`. Risk notes: "Low volatility compresses
the channel and produces frequent, low quality breakouts."

**Implemented logic**

| | Rule |
| --- | --- |
| Indicators | `keltner_middle = EMA(20)`, `keltner_upper = EMA(20) + 2 × ATR(10)`, `keltner_lower = EMA(20) − 2 × ATR(10)`, `EMA(50)`, `EMA(200)` |
| Entry | `close > keltner_upper` **and** `ema_fast > ema_slow` |
| Exit | `close < keltner_middle` |
| Parameters | EMA period 20, ATR period 10, band width 2.0 ATR, trend EMAs 50 / 200, `startup_candle_count = 210` |
| Risk | `stoploss = -0.10`, `minimal_roi = {0: 0.15, 720: 0.07, 2880: 0.0}` |

**Suitable timeframes.** The catalogue lists 15m and 1h, which is what the
profiles use. The bands are volatility-scaled, so the rule adapts to the grid,
but the EMA(50) > EMA(200) filter needs 200 candles of history before it can
accept anything.

**Risk notes.** The trend filter is what distinguishes this from a pure
volatility breakout, and it is also what makes the strategy late: by the time
both conditions hold, part of the move is gone. Exiting at the middle band caps
the win, so the strategy depends on a high proportion of winning breakouts.

---

## 8. `supertrend` — ATR band flip

**File** `user_data/strategies/SupertrendStrategy.py` · **catalogue title**
Supertrend trailing stop · **reference** the Supertrend indicator (Olivier Seban,
2008; TradingView Pine reference implementation).

*Catalogue metadata.* Summary: "Follows the Supertrend direction with an ATR
trailing stop." Timeframes: `1h`, `4h`. Risk notes: "The ATR multiplier sets the
trade frequency: too tight and it whipsaws, too wide and stops are expensive."

**Implemented logic**

| | Rule |
| --- | --- |
| Indicators | `ATR(10)` (`atr`), `hl2 = (high + low) / 2`, `supertrend_direction` (+1 / −1) |
| Entry | the direction **flips to +1** (previous candle −1) |
| Exit | the direction **flips to −1** |
| Parameters | ATR period 10, multiplier 3.0, `startup_candle_count = 30` |
| Risk | `stoploss = -0.10`, `minimal_roi = {0: 0.10, 720: 0.05, 2880: 0.0}` |

The direction is recursive: the final upper and lower bands depend on the
previous candle, so `supertrend_direction()` is an explicit sequential loop over
NumPy arrays (a per-row `apply` would be both slower and less readable). Unlike
most of the other nine, the entry here is a genuine **event** — the flip — not a
state that stays true.

**Suitable timeframes.** The class default is 15m; the catalogue lists 1h and 4h,
which is where the platform runs it, and where the band is less frequently
whipsawed. On a 5m grid the flip count explodes and the strategy becomes a fee
generator.

**Risk notes.** Every trailing-band system pays for its whipsaws: in a flat market
the flip alternates and each flip is a small realised loss. The multiplier
(3.0 ATR) is deliberately wide to reduce that count, at the price of giving back
more of each trend when it finally turns.

---

## 9. `dual-thrust` — range breakout on both sides of the open

**File** `user_data/strategies/DualThrustStrategy.py` · **catalogue title** Dual
Thrust range breakout · **reference** the Dual Thrust day-trading system
(Michael V. DiPietro, *Futures* magazine, 1994), widely re-published in the
Chinese futures literature.

*Catalogue metadata.* Summary: "Intraday range breakout built from the previous
range and two thrust factors." Timeframes: `15m`, `1h`. Risk notes: "Sensitive to
the session boundary used for the range; false breaks inside a range are the main
cost."

**Implemented logic**

| | Rule |
| --- | --- |
| Indicators | `dual_thrust_range = max(HH − LC, HC − LL)` over the **previous 4** candles, `buy_line = open + 0.5 × range`, `sell_line = open − 0.5 × range` |
| Entry | `close > buy_line` |
| Exit | `close < sell_line` |
| Parameters | range window 4, `K1 = 0.5` (buy), `K2 = 0.5` (sell), `startup_candle_count = 10` |
| Risk | `stoploss = -0.06`, `minimal_roi = {0: 0.02, 30: 0.01, 60: 0.0}` |

The range is measured on the previous candles only (every component is shifted by
one), and both trigger lines are anchored on the **open** of the candle being
decided on — the one price that is known before the candle resolves. The
`minimal_roi` is short-horizon on purpose: this is an intraday rule, and the
position is expected to work within the hour.

**Suitable timeframes.** The class default is 5m; the catalogue lists 15m and 1h,
which is where the platform runs it. It is the platform's highest-turnover
profile and therefore the most fee-sensitive one: on a 1h grid a single candle can
already be the whole move.

**Risk notes.** Intraday breakouts are the family most damaged by costs: with
`K1 = K2 = 0.5` the trigger sits close to the open, so the trade count is high and
the average win is small. The tight `stoploss = -0.06` keeps a failed breakout
cheap, but a flat, low-volatility session produces repeated small losses.

---

## 10. `faber` — the 200-period trend filter

**File** `user_data/strategies/FaberStrategy.py` · **catalogue title** Faber trend
allocation · **reference** Mebane T. Faber, *A Quantitative Approach to Tactical
Asset Allocation* (Journal of Wealth Management, 2007) — the 10-month
moving-average rule.

*Catalogue metadata.* Summary: "Holds the asset only while it trades above its
long moving average." Timeframes: `1d`. Risk notes: "Wide, slow signals: it gives
back a large part of a move before the average is crossed."

**Implemented logic**

| | Rule |
| --- | --- |
| Indicators | `SMA(200)` (`sma_slow`) |
| Entry | `close > sma_slow` |
| Exit | `close < sma_slow` |
| Parameters | SMA period 200, `startup_candle_count = 210` |
| Risk | `stoploss = -0.25`, `minimal_roi = {0: 1.0}` (unreachable: **only the signal closes the trade**) |

On the 1d grid, 200 candles are the published 10-month average; that is the grid
the catalogue declares and the only grid the profiles use. On a shorter grid the
same class is a different economic rule, not a faster reading of this one.

**Suitable timeframes.** 1d only — for the published rule, and per the catalogue.
On 1h or 15m the strategy is a slow moving-average filter with none of the
properties the reference describes.

**Risk notes.** This is an allocation rule, not a trading signal: it is in the
market or out of it, it holds for months, and it accepts deep drawdowns on the
way — hence `stoploss = -0.25`, wide on purpose so that ordinary daily noise does
not close a position the model intends to hold. It also gives back a large part of
a top before the close crosses back below the average, and the 200-candle warm-up
means a fresh worker needs 210 daily candles of history before it can trade at
all.

---

## 11. How a strategy is verified

`tests/` loads every strategy through freqtrade's **own** resolver
(`freqtrade.resolvers.strategy_resolver.StrategyResolver.load_strategy` with a
minimal config), runs `populate_indicators`, `populate_entry_trend` and
`populate_exit_trend` on a deterministically generated OHLCV frame (seeded NumPy,
~600 candles) and asserts that:

* the `enter_long` / `exit_long` columns contain only `0` and `1`;
* no `NaN` survives in the signal columns;
* at least one entry signal is produced over the series;
* the strategy's declared timeframe belongs to freqtrade's supported set.

The suite never spawns a `freqtrade trade` process and never reaches an exchange
(see `docs/testing-policy.md`). To check the strategies locally:

```bash
.venv/bin/python -m pytest tests/strategies -q --no-cov
```

---

## 12. Adding a strategy: the two-file recipe

1. **Drop the file** into `user_data/strategies/`, for example
   `user_data/strategies/MyIdeaStrategy.py`.
   * exactly one class, named after the file stem:
     `class MyIdeaStrategy(IStrategy)`;
   * `INTERFACE_VERSION = 3`, `can_short = False`,
     `process_only_new_candles = True`, `use_exit_signal = True`;
   * explicit `startup_candle_count`, `stoploss`, `minimal_roi`;
   * a `volume > 0` guard on every signal;
   * a module docstring naming the public source of the idea.
2. **Add one entry** to `config/strategies.json`, in the shape the other ten use
   (the `id` follows the kebab-case convention of the shipped catalogue):

```json
{
  "id": "my-idea",
  "class_name": "MyIdeaStrategy",
  "file": "MyIdeaStrategy.py",
  "title": "My idea",
  "category": "trend",
  "summary": "One line shown in the catalogue.",
  "description": "Two or three sentences in English.",
  "indicators": ["EMA(20)", "RSI(14)"],
  "timeframes": ["15m", "1h", "4h"],
  "reference": "Public name or URL of the idea",
  "risk_notes": "One line."
}
```

`category` is optional metadata (the shipped ten do not set it) and so is the
entry itself: the catalogue loader scans `user_data/strategies/*.py`, so the file
alone is enough — an entry-less strategy is discovered, its title is derived from
the class name and its description stays empty. The `id` is what
`config/profiles.json`'s `strategy` field references, so an entry is what makes a
strategy *selectable* by a declarative profile; `POST /api/profiles` validates the
id against the same discovered catalogue and answers `422` on an unknown one.

To run it:

```bash
# a profile referencing the new id, in config/profiles.json or through the API
python -m trading_platform realtime provision --api-url http://127.0.0.1:8080 --dry-run
python -m trading_platform realtime provision --api-url http://127.0.0.1:8080
```

Nothing else changes: no file of the platform's own code names a strategy.

---

## 13. The research programme: fifteen strategies since 2026

The catalogue holds **fifteen** strategies, not ten. The five added entries came
out of a research programme whose full write-up, data and experiments live in
`research/` (see `research/README.md`). The ten originals above are **untouched
and still running** — the additions are deliberately additive so that any
improvement can be measured against a live baseline rather than only in a
backtest.

### 13.1 What the diagnosis found

Replaying the shipped rules over two years of candles (`research/diagnose.py`)
attributed the losses to three defects rather than to bad ideas:

* **too much turnover for the volatility.** On BNB 15m a round trip costs about
  **1.0 ATR** — more than an average candle's whole range. `dual-thrust-eth-15m`
  paid 488% of its account in fees to earn a gross +388%;
* **exits that are already true on the entry candle.** Both losing mean-reversion
  rules exit on the channel midline, which after an entry is satisfied on the next
  candle, so the reversion they exist to capture never has room to happen. This is
  why `bollinger` shows a 54.7% win rate with a 0.53 profit factor;
* **entries that are states, not events.** `momentum` enters while `roc > 0`,
  which stays true for dozens of candles, so it re-enters right after every exit
  (494 entries on BTC 1h, median hold 5 bars).

Every losing profile has a negative per-trade t-statistic, so these are real
effects and not small-sample noise.

### 13.2 The added strategies

| id | file | replicates / replaces | entry | exit | timeframe |
| --- | --- | --- | --- | --- | --- |
| `keltner-breakout-v2` | `KeltnerBreakoutV2Strategy.py` | `keltner` | first close above `EMA(20) + 3 × ATR(10)`, in an uptrend, clearing the band by `0.3 × ATR` | Supertrend(10, 3) trail | 4h |
| `trend-ensemble-v2` | `TrendEnsembleV2Strategy.py` | `basic` | at least two of SMA(50/100/200) below price, and SMA(50) rising | that majority lost, or SMA(50) turns down | 1d |
| `vol-targeted-trend` | `VolTargetedTrendStrategy.py` | new | price above SMA(200) **and** realised volatility below its trailing median | price below SMA(200), or volatility above its 75th percentile | 4h |
| `faber-all-in` | `FaberAllInStrategy.py` | `faber`, resized | identical to `faber` | identical to `faber` | 1d |
| `donchian-all-in` | `DonchianAllInStrategy.py` | `donchian`, resized | identical to `donchian` | Donchian(10) exit or a 1.8-ATR chandelier | 1h |

All five follow the same contract as the ten originals (§Common properties):
interface version 3, `can_short = False`, `process_only_new_candles = True`, an
explicit `startup_candle_count`, a `volume > 0` guard on every signal, and a module
docstring naming the public source of the idea.

### 13.3 The evidence

Out-of-sample (the most recent 40% of history, 10 pairs), against buy-and-hold on
the same window:

| strategy | mean Sharpe | mean edge vs buy&hold | windows beating buy&hold | mean maxDD |
| --- | --- | --- | --- | --- |
| `keltner-breakout-v2` | **+0.23** | **+21.1%** | **86.7%** | -12.2% |
| `vol-targeted-trend` | -0.04 | +15.6% | 73.3% | -23.2% |
| `trend-ensemble-v2` | -0.21 | +8.9% | 56.7% | -34.9% |

`keltner-breakout-v2` on 4h is the strongest result of the programme: pooled
across ten pairs it makes **123 trades at a mean +3.09% log return each
(t = 3.41)**, it is positive in bull, bear **and** chop, and its three sequential
walk-forward folds are all positive. Its edge is also smooth in its main
parameter (band width), which is the difference between a real effect and a
fitted one.

### 13.4 The all-in profiles

`faber-all-in` and `donchian-all-in` exist to be run with `max_open_trades = 1`:
Freqtrade's `"unlimited"` staking then commits the **whole wallet** to the single
open position and compounds it, instead of splitting the wallet across two slots.

They were chosen on the **two-year backtest Sharpe**, not on the live leaderboard —
at a few days old the live ranking is not yet informative, and two of its "top
four" are among the worst profiles over two years. Only the risk envelope changes
relative to the shipped rule: the stop is tightened (`-0.25` → `-0.12` for Faber,
`-0.08` → `-0.15` plus an ATR chandelier for Donchian), because at full size the
shipped stops would risk a quarter of the account on one trade.

> **The all-in profiles are riskier by construction.** A stop sweep showed that
> these strategies' own signals, not their stops, drive the drawdown, so the
> tightened stop is a disaster brake and **not** a drawdown solution. Roughly
> double the half-size drawdown figures in §4 of `research/README.md`.

None of the fifteen strategies carries an out-of-sample validation claim, and a
green paper P&L is not evidence of an edge. See `research/README.md` §6 for the
threats to validity, which include survivorship bias in the research data and a
single-regime out-of-sample window.
