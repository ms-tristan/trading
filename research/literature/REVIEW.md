# Literature Review: Evidence-Backed Strategy Ideas for a Long-Only Spot Crypto Platform

**Scope constraints assumed throughout:**
- Python / freqtrade, **talib indicators, OHLCV candles only**
- **SPOT, LONG-ONLY**, one pair at a time, no shorting, no external data feeds
- Cost model: **0.1% taker fee + 0.05% slippage per side** (this is the platform's *actual*
  configured cost, verified in `research/backtest.py`: `DEFAULT_FEE = 0.001`,
  `DEFAULT_SLIPPAGE = 0.0005`) → **≈0.3% round trip**, slightly worse than the 0.2% briefed.

> 🚨 **COST CAVEAT — read §4.0c.** An industry measurement across 432 live round-trips and 9
> regulated providers finds retail round-trip costs of **0.53%–6.45%**, i.e. **~53 bp at best**
> — roughly **2.65× the 0.2% assumed**. If that applies to this platform, the verdicts below
> become substantially *stronger*, not weaker. **Re-derive your own cost before further work.**

**Why this matters:** every "edge" below must clear ~30 bps per round trip. That single number
kills more of the literature than any statistical critique does.

---

## 0. Executive summary — read this first

The honest top-line finding of this review is **deflationary**. Three things are true at once:

1. **The academic evidence for the classical strategies you already run is weak-to-absent**, and
   there are well-cited peer-reviewed papers that *fail to replicate* the very effects most of
   them are built on (TSMOM, volatility-managed portfolios, crypto momentum).
2. **The one effect with the strongest and most replicated evidence is time-series momentum /
   trend following** — but it is strongest at *multi-month* horizons, which on crypto means
   holding through 40–70% drawdowns, and its edge is largely a *crisis/diversification* effect
   that does **not** apply to a single-asset, long-only, always-in-or-out spot bot.
3. **The gap between academic "significance" and tradeable edge after 30 bps is enormous.** Most
   documented anomalies in crypto are documented as *cross-sectional long-short* weekly
   strategies (Q5−Q1), which are (a) not implementable long-only, and (b) have weekly turnover
   that would consume the entire spread in fees.

**And two findings that specifically kill the obvious "next ideas":**
- **"Buy the dip" is documented to INVERT on the coins you can actually trade.** Zaremba et al.
  (2021), across 3,600+ coins: daily reversal is an **illiquidity artefact**, and *"the handful of
  largest and most tradeable coins exhibit daily **momentum** rather than a reversal."* Your
  universe is that handful. A dip-buyer on liquid spot is betting against the documented sign of
  the effect in that universe (§4.0).
- **Volatility contraction predicts the SIZE of a move, not its DIRECTION** — and
  Christoffersen & Diebold (2006) prove sign-predictability is *not* expected at daily frequency.
  So a long-only "squeeze breakout" is really a bet on crypto's drift term, which will look
  profitable in any bull-heavy backtest and is not a signal (§7.0).

**Therefore my recommendation is not "find a better signal".** It is: **stop trying to find a
directional predictor and instead implement the two things the literature says are genuinely
robust — volatility-scaled exposure and a slow, low-turnover trend filter — and treat the
result as a risk-managed beta-capture vehicle, not an alpha strategy.** Details in §9.

**The two strongest crypto-specific results in this review both support that conclusion:**
- **Han, Kang & Ryu (2024)** — a *long-only* time-series momentum rule (buy when the 28-day return
  is in the top tercile, else hold cash) delivered **Sharpe 1.51 vs 0.84** for buy-and-hold with a
  **lower** max drawdown (61.8% vs 89.1%), after 15 bps costs, 2013–2023, survivorship-bias-free.
  Their explanation: *"holds a long position only when the market is bullish and defends well
  against market downturns."* They also find **the short leg loses money** — so long-only is not a
  limitation for you, it is the correct implementation.
- **Zarattini, Pagani & Barbon (2025)** — a 9-horizon Donchian ensemble with volatility-based
  sizing on a top-20 liquid-coin portfolio produced **net-of-fees Sharpe 1.57, MDD 11%, alpha
  10.8% vs Bitcoin.** *(Working paper; and it needs a 20-coin portfolio, which your one-pair
  constraint cannot replicate — see §5.4c.)*

**But note carefully:** in that same Zarattini paper the 9-horizon **ensemble's Sharpe (1.58) was
*below* the best single short lookback (5 days: 1.66)** — it won only on Sortino and drawdown.
**Multi-horizon ensembling buys you tail protection, not extra return.** That is the honest version
of the multi-horizon thesis.

### Ranking (expected robustness × implementability), full detail in §9

| Rank | Idea | Robustness of evidence | Implementable long-only? | Fee survival | Verdict |
|---|---|---|---|---|---|
| **1** | **Multi-horizon trend ensemble + vol-scaled sizing** (§1, §5) | **Moderate–strong** (best in review; survives 1880–2016, has replication critiques) | Yes | 1d/4h: **yes**; 1h: marginal | **BUILD** |
| **2** | **Volatility targeting as risk control** (§2) | **Strong for risk reduction, weak for Sharpe** | Yes | N/A (doesn't add trades) | **BUILD (as sizing layer)** |
| **3** | **Volatility-contraction breakout** (§7) | **Weak** — clustering is real, *direction* is not predicted | Yes | 1d only | **TEST, low expectation** |
| 4 | Cross-sectional momentum (§3) | **Moderate in academia** | **NO** (needs short leg / large universe) | No | **DO NOT BUILD** |
| 5 | Mean reversion / dip-buying (§4) | **Weak — and inverts against you on liquid coins** | Mechanically yes, but contrarian to the documented effect | No | **DO NOT BUILD as core** |
| 6 | Seasonality / calendar (§6) | **Weak — actively refuted** | Yes | **No** — effect ≈ fee | **DO NOT BUILD** |
| 7 | Overnight / funding effects (§8) | n/a | **NO** (requires funding data) | n/a | **OUT OF SCOPE** |

**Note the change from the brief's framing:** the two ideas most people reach for when the classic
crossovers fail — *cross-sectional momentum* and *buy-the-dip mean reversion* — are both ranked at
the bottom, for two independent and well-documented reasons (§3, §4.0). The ideas that survive are
the boring ones: **slow trend + volatility-scaled sizing.** That is the honest answer.

---

## 1. Time-Series Momentum (TSMOM)

### 1.1 The original paper — verified

> **Moskowitz, T. J., Ooi, Y. H., & Pedersen, L. H. (2012). "Time Series Momentum."**
> *Journal of Financial Economics*, 104(2), 228–250.
> DOI: [10.1016/j.jfineco.2011.11.003](https://doi.org/10.1016/j.jfineco.2011.11.003)
> AQR summary: https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum

**Verified abstract (verbatim, via OpenAlex + AQR page):**
> "We document significant 'time series momentum' in equity index, currency, commodity, and bond
> futures for each of the 58 liquid instruments we consider. We find persistence in returns for
> **one to 12 months** that partially reverses over longer horizons, consistent with sentiment
> theories of initial under-reaction and delayed over-reaction. A diversified portfolio of time
> series momentum strategies across all asset classes delivers substantial abnormal returns with
> little exposure to standard asset pricing factors and performs best during extreme markets."

**IMPORTANT — a correction to a widely repeated claim.** The famous "smile" curve and the specific
lookback table live in the paper body, and I could **not** retrieve the full text (Elsevier 403).
So I will **not** assert specific per-lookback t-statistics as verified. What *is* verified from
the abstract is the tested range: **1 to 12 months**, with partial reversal beyond.

**The sizing rule (the genuinely important and portable part) — now verified from the primary PDF.**
MOP's position sizing is the most useful export for your platform. From the NYU Stern copy of the
paper (https://pages.stern.nyu.edu/~lpederse/papers/TimeSeriesMomentum.pdf), **verified verbatim**:

```
# Eq. (1): ex-ante annualised variance, EWMA over 261 days
sigma^2_t = 261 * SUM_{i=0..inf} (1-delta) * delta^i * (r_{t-1-i} - rbar_t)^2
#   delta chosen so the centre of mass, delta/(1-delta) = 60 DAYS

# Position size scales inversely to ex-ante vol:
position_size_t = 40% / sigma_{t-1}              # 40% = ex-ante annualised vol target

# Eq. (5): the TSMOM return
r_TSMOM_t = sign(r_{t-12,t}) * (40% / sigma_t) * r_{t,t+1}
```
Two implementation details that matter: **volatility is estimated at t−1 and applied to t returns
(no look-ahead)**, and the **diversified** TSMOM factor has ~12% annualised vol (the 40% is
per-instrument, not portfolio-level).

**Lookbacks tested:** 1, 3, 6, 9, 12, 24, 36, 48 months × holdings 1, 3, 6, 9, 12, 24, 36, 48
months (Table 2 alphas, h=1 column): 1m t=4.34, 3m 5.35, 6m 5.03, 9m 6.06, **12m 6.61**, 24m 3.95,
36m 2.70, 48m 1.84. The effect is strong for lookback/holding ≤ 12 months, **decays beyond, and
turns NEGATIVE at 24–48m long holdings.** Best cell = **12m lookback / 1m holding** — hence the
headline "TSMOM = k=12, h=1".

**⚠️ Two widely-repeated claims that are WRONG (both corrected from the primary PDF):**
1. **The lookback pattern is monotone decay, not a "smile".** The famous "smile" is **Fig. 4**: TSMOM
   quarterly returns plotted against *contemporaneous S&P 500 returns* — a **market-state** smile
   (best when the market moves most extremely in *either* direction), **not** a lookback-horizon
   effect.
2. **The effect is not uniform across asset classes.** Currencies are the weak link — their t-stats
   go negative beyond a 9-month lookback.

Other verified specifics: all 58 contracts positive, **52/58 significant at 5%**; positive alpha vs
always-long in **90%** of cases; out-of-sample 1966–1985 significant with Sharpe ~1.1;
correlation(Sharpe, illiquidity) = −0.16.

### 1.2 The replication failure you must know about

> **Huang, D., Li, J., Wang, L., & Zhou, G. (2020). "Time series momentum: Is it there?"**
> *Journal of Financial Economics*, **135(3), 774–794.**
> DOI: [10.1016/j.jfineco.2019.08.004](https://doi.org/10.1016/j.jfineco.2019.08.004)
> Verified abstract: https://scholars.hkbu.edu.hk/en/publications/time-series-momentum-is-it-there-2/
> (129 citations; 82 Scopus citations)

**Verbatim abstract:**
> "Time series momentum (TSM) refers to the predictability of the past 12-month return on the next
> one-month return and is the focus of several recent influential studies. This paper shows that
> **asset-by-asset time series regressions reveal little evidence of TSM, both in- and
> out-of-sample.** While the t-statistic in a pooled regression appears large, it is not
> statistically reliable as it is less than the critical values of parametric and nonparametric
> bootstraps. From an investment perspective, **the TSM strategy is profitable, but its
> performance is virtually the same as that of a similar strategy that is based on historical
> sample mean and does not require predictability.** Overall, the evidence on TSM is weak,
> particularly for the large cross section of assets."

**Why this is the single most decision-relevant sentence in this entire review:**

> *"the TSM strategy is profitable, but its performance is virtually the same as that of a similar
> strategy that is based on historical sample mean and does not require predictability."*

Translation for your platform: **a 12-month-sign TSMOM rule on BTC is statistically
indistinguishable from "just always be long BTC".** On a long-only spot bot, that is a devastating
finding — because "always be long" is free, and your TSMOM rule costs you round trips every time
it flips out and back in. If the trend rule's entire value-add is that it is long during bull
markets and flat during bear markets, then you should test the null hypothesis *"is this better
than buy-and-hold?"* very hard before shipping it.

**Nuance, and why I still rank trend first:** Huang et al. are testing *whether the past return
has predictive power for the next month's return*. A **trend filter used as a risk overlay** is a
different claim — it doesn't need to predict returns, it needs to cut left-tail exposure. That
claim is better supported (§5). The mistake is to believe the trend rule is generating alpha.

### 1.3 Supporting evidence: volatility scaling is the load-bearing part

> **Kim, A. Y., Tse, Y., & Wald, J. K. (2016). "Time series momentum and volatility scaling."**
> *Journal of Financial Markets*, **30**, 103–124.
> DOI: [10.1016/j.finmar.2016.05.003](https://doi.org/10.1016/j.finmar.2016.05.003)
> (92 citations — verified via OpenAlex)

This paper's entire thesis, per its title and the citing literature, is that **the volatility
scaling — not the momentum signal — contributes the majority of TSMOM's profitability.** I could
not retrieve the abstract text directly (no abstract in OpenAlex, Elsevier 403), so I state this
as the paper's established argument rather than quoting a sentence I did not read. The
corroborating mechanism is well established and independently verified in §2.3 (Wang & Yan, 2021:
alpha "stems primarily from volatility timing").

**Implementation consequence:** if you build TSMOM, **build the vol-scaling block first and make
sure it is correct**, because that is where the money is. A TSMOM signal without vol scaling is
mostly a fee-generating coin flip.

### 1.4 Crypto-specific momentum evidence

> **Liu, Y., Tsyvinski, A., & Wu, X. (2022). "Common Risk Factors in Cryptocurrency."**
> *The Journal of Finance*, **77(2), 1133–1177.** DOI: [10.1111/jofi.13119](https://doi.org/10.1111/jofi.13119)
> NBER WP 25882 (May 2019): https://www.nber.org/papers/w25882 (verified abstract page)

**Verified abstract (NBER):** three factors — **cryptocurrency market, size, and momentum** —
capture cross-sectional expected crypto returns; nine crypto factors form successful long-short
strategies.

**Corrections from full-text reading of the NBER working paper (do not skip this):**

- The lookbacks actually tested are **1, 2, 3, 4, 8, 16, 50, 100 weeks** — *not* the
  "1/3/6/12 month" set.
- Only the **1-, 2-, 3-, and 4-week** horizons are statistically significant. **8, 16, 50 and
  100-week momentum are NOT significant.**
- Long-short (Q5−Q1) excess **weekly** returns: 1-week 2.7% (t=1.994), 2-week 3.3% (t=2.442),
  **3-week 4.1% (t=2.742)**, 4-week 2.5% (t=2.002).
- Their **footnote 7** states they use **three-week** momentum as the main measure because it
  generates the largest long-short spread. The `CMOM` factor is built on 3-week.
- **⚠️ The popular claim "1-week momentum is strongest in crypto" is NOT what this paper shows.**
  The accurate statement is: the live horizon is **1–4 weeks**, peaking at **3 weeks**.

**Critical caveats on this evidence:**
- t-statistics of **2.0–2.7** are *weak* evidence, especially with overlapping windows.
- Sample is **2014–2018**, 1,707 coins. That is a **short, single-regime sample** (essentially
  one bull market plus one crash). Nothing here is out-of-sample by modern standards.
- The result is a **long-short cross-sectional** result. It does **not** tell you a long-only spot
  bot can harvest it — see §3.

**Directly contradictory evidence (must be weighed):**

> **Grobys, K., & Sapkota, N. (2019). "Cryptocurrencies and momentum."**
> *Economics Letters*, **180**, 6–10. DOI: [10.1016/j.econlet.2019.03.028](https://doi.org/10.1016/j.econlet.2019.03.028)
> https://ideas.repec.org/a/eee/ecolet/v180y2019icp6-10.html

Verbatim: *"Contrary to earlier studies our findings **do not indicate any evidence of significant
momentum payoffs**, supporting the view that the cryptocurrency market is far more efficient than
suggested in earlier studies."* 143 coins, 2014–2018 — **overlapping sample with LTW, opposite
conclusion.** This is a genuine unresolved dispute in the literature.

**Further skeptical evidence (extreme claims — weight accordingly):**

> **Grobys, K., & Shahzad, S. J. H. (2025/2026). "Cryptocurrency Momentum: Is It an Illusion?"**
> *International Journal of Finance & Economics.* DOI: [10.1002/ijfe.70036](https://doi.org/10.1002/ijfe.70036)

Verbatim: realized variances of six crypto momentum strategies follow **power laws**; block-bootstrap
tests indicate the population mean and variance are *"statistically not defined"*, implying
*"in real life, we might not be able to realise these risk premiums"* and performance metrics
*"using variance as an input, are not informative."*

> **Grobys, K., et al. (2025). "Cryptocurrency momentum has (not) its moments."**
> *Financial Markets and Portfolio Management.* DOI: [10.1007/s11408-025-00474-9](https://doi.org/10.1007/s11408-025-00474-9)

Verbatim: *"cryptocurrency momentum is subject to severe crashes. Even a single cryptocurrency can
cause insignificant momentum portfolio returns... volatility management is a useful tool for
mitigating cryptocurrency momentum crashes."*

**Calibration note:** the Grobys papers are a *series* advancing an unusual, extremely strong
claim (that Sharpe ratios "do not exist" for these strategies). This is **not mainstream
consensus** and I would not build a platform on it. But the weaker, more defensible reading —
**crypto momentum has fat-tailed crash risk and the sample is short** — is well supported and
should temper any position sizing.

### 1.5 Crypto TSMOM — the recent picture

A 2025 post-ETF study exists and is worth reading but is **a non-peer-reviewed deposit**:

> "Time-Series Momentum in Cryptocurrency Markets: A Pre and Post Spot Bitcoin ETF Analysis"
> https://zenodo.org/records/19671502 — **Zenodo deposit, not peer-reviewed. Treat as indicative.**

A further very useful (but working-paper-stage) source repeatedly surfacing in searches:

> "Time Series and Cross-Sectional Momentum in the Cryptocurrency Market"
> Auckland Centre for Financial Research. https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf
> **⚠️ This URL is Cloudflare-blocked (HTTP 403) and I could not retrieve or verify its contents.**

One search-engine snippet attributed to it reads: *"Every long-short portfolio with a holding
period of less than a week yields a negative mean return."* **I could not verify this against the
source and you should not rely on it.** It is recorded here only as a lead.

### 1.6 Verdict on TSMOM

- **Directional TSMOM as an alpha source: weak.** Replication-failed asset-by-asset; on a
  long-only single asset it may not beat buy-and-hold.
- **Volatility scaling: strong and portable.** Build this.
- **Crypto momentum at 3-week horizon: real but fragile, long-short, and short-sample.**
- **Fees:** at 1h, a 3-week-lookback momentum rule rebalances too often to survive 30 bps/round
  trip unless it is heavily damped. At **1d** it is borderline-viable. At 4h it depends entirely
  on turnover discipline.

---

## 2. Volatility-Scaled Sizing / Volatility Targeting

### 2.1 The primary paper — with a crucial caveat the marketing omits

> **Harvey, C. R., Hoyle, E., Korgaonkar, R., Rattray, S., Sargaison, M., & Van Hemert, O. (2018).
> "The Impact of Volatility Targeting."** *The Journal of Portfolio Management*, **45(1)**, 14–33.
> DOI: [10.3905/jpm.2018.45.1.014](https://doi.org/10.3905/jpm.2018.45.1.014)

**Verified abstract (verbatim, via Semantic Scholar):**
> "Recent studies show that volatility-managed equity portfolios realize higher Sharpe ratios than
> portfolios with a constant notional exposure. The authors show that **this result only holds for
> risk assets, such as equity and credit**, and they link this finding to the so-called leverage
> effect for those assets. **In contrast, for bonds, currencies, and commodities, the impact of
> volatility targeting on the Sharpe ratio is negligible.** However, the impact of volatility
> targeting goes beyond the Sharpe ratio: it **reduces the likelihood of extreme returns across
> all asset classes.** Particularly relevant for investors, left-tail events tend to be less
> severe because they typically occur at times of elevated volatility, when a target-volatility
> portfolio has a relatively small notional exposure."

**Read that carefully — the headline is not what people quote.** The robust, asset-class-independent
benefit is **tail reduction**, not Sharpe improvement. The Sharpe improvement is conditional on
the asset being "risk-asset-like".

**Harvey et al.'s own verified numbers and caveats** (full PDF read from Harvey's Duke page,
https://people.duke.edu/~charvey/Research/Published_Papers/P135_The_impact_of.pdf):
- Equity Sharpe **0.40 → 0.48–0.51** when vol-scaled; regression of scaled on unscaled daily
  returns gives intercept **0.64 bp, t = 3.05** (Newey-West, 30 lags), R² = 0.73.
- Vol-of-vol falls from **4.6% to 1.8%**; costs are trivial (~0.066%/yr).
- **⚠️ Their own subsample break, verbatim:** *"The Sharpe ratio improves in all cases, **except
  during the 1957–1987 subsample period**."*
- For **bonds** the pre-1980 Sharpe actually *decreases*; for **FX and commodities the effect is
  negligible**.
- **⚠️ The authors concede the mechanism partly reduces to momentum:** vol scaling *"effectively
  introduces some momentum into strategies"* (via the leverage effect). **This means vol targeting
  and trend following are NOT independent edges — expect correlation between those two sleeves,
  not diversification.** This is an important point for your platform design.

**Is crypto "risk-asset-like"?** Plausibly yes — crypto behaves like a high-beta risk asset and
exhibits the leverage effect (the AQR mechanism: volatility rises when prices fall, so cutting
exposure into rising vol avoids the worst of the drawdown). This gives a *reasoned* case for vol
targeting in crypto, but it is **an extrapolation, not a crypto-specific verified result.**

**⚠️ And there is NO published crypto vol-targeting study.** I searched specifically for one and
found none. **This is a genuine open question** — which makes it a legitimate, testable hypothesis
for your platform rather than something you can cite as established.

**⚖️ The honest counterweight — the variance risk premium.** In fairness to the "volatility
predicts returns" camp, there IS a substantial literature claiming exactly that: the **variance risk
premium** (Bollerslev, Tauchen & Zhou 2009, *Review of Financial Studies* 22(11), 4463–4492,
DOI [10.1093/rfs/hhp008](https://doi.org/10.1093/rfs/hhp008)) finds that the variance risk premium
predicts stock returns. **I flag it and then set it aside, for a concrete reason: it is estimated at
monthly/quarterly horizons and requires OPTION-IMPLIED variance** — a volatility-derivatives feed,
**not computable from spot OHLCV**, and therefore outside your constraints.

This is worth knowing because it explains why "volatility predicts returns" claims circulate: in the
literature where that claim *is* well-supported, the predictor is an **implied** variance measure
and the horizon is **months**, not daily candles. **Do not port VRP conclusions into a 1h/4h/1d
OHLCV system** — the mismatch is in both the input (implied vs realised) and the horizon.

### 2.2 The baseline it must beat

> **Moreira, A., & Muir, T. (2017). "Volatility-Managed Portfolios."**
> *The Journal of Finance*, **72(4)**, 1611–1644. DOI: [10.1111/jofi.12513](https://doi.org/10.1111/jofi.12513)

The scaling rule (verified as the standard form in the vol-management literature, confirmed via
the FMA "Semivolatility-Managed Portfolios" paper which restates it as
`f_(σ),t = c_(σ) / σ̂²_(t−1) · f_t`):

```
f_managed,t = (c / RV̂_(t-1)) * f_t
```
i.e. scale the factor's exposure by the inverse of last period's realised variance, where `c` is a
constant chosen so the managed strategy has the same unconditional volatility as the unmanaged one.

### 2.3 The replication failure — and the *reason* it fails (most useful finding)

> **Cederburg, S., O'Doherty, M. S., Wang, F., & Yan, X. S. (2020). "On the performance of
> volatility-managed portfolios."** *Journal of Financial Economics*, **138(1)**, 95–117.
> DOI: [10.1016/j.jfineco.2020.04.015](https://doi.org/10.1016/j.jfineco.2020.04.015)
> (169 citations)

I could not retrieve Cederburg's own abstract (repository returned 403 / HTML). **But I verified
their central finding verbatim through the follow-up paper that cites it**, which is a legitimate
and traceable route:

> **Wang, F., & Yan, X. S. (2021). "Downside risk and the performance of volatility-managed
> portfolios."** *Journal of Banking & Finance*, **131**, 106198.
> DOI: [10.1016/j.jbankfin.2021.106198](https://doi.org/10.1016/j.jbankfin.2021.106198)
> Full text (verified): https://www.lehigh.edu/~xuy219/research/Downside.pdf

Verbatim from Wang & Yan's introduction, describing Cederburg et al. (2020):
> "Cederburg et al. (2020) show that **the trading strategies implied by the spanning regressions
> of Moreira and Muir (2017) are not implementable in real time and reasonable out-of-sample
> versions do not outperform simple investments in the original, unmanaged portfolios.**"

**And now the decomposition, which is the actionable insight.** Wang & Yan separate the alpha into
**volatility timing** (lagged vol predicts *future vol*) and **return timing** (lagged vol predicts
*future returns*). Verbatim:

> "Our decomposition results indicate that **the positive alphas of total volatility-managed
> portfolios stem primarily from volatility timing**... **The small, and sometimes even negative
> contribution from return timing suggests that total volatility is largely unrelated to future
> returns.**"
>
> "For total volatility-managed strategies, the return-timing component is positive among just
> **two of the nine equity factors and 42 of the 94 anomalies.**"

**This is the whole game, and it applies directly to your platform:**

- **Volatility clustering is one of the most robust stylised facts in finance.** Scaling by
  realised vol *will* deliver the volatility-timing component reliably. You get a **smoother
  equity curve and smaller tails. That is real and worth having.**
- **Volatility does NOT reliably predict returns.** So vol scaling **does not** reliably add alpha
  on a directional long-only strategy. Anyone promising a Sharpe uplift from vol targeting alone
  in crypto is over-claiming.

**Wang & Yan's fix, if you want more than risk control:** scale by **downside** volatility rather
than total volatility. They find ~95% of anomalies (89/94) show positive alphas under downside-vol
scaling vs ~two-thirds (62/94) under total-vol scaling, and for downside strategies the
improvement is *"almost entirely attributable to the return-timing component."* That is a genuine
return-timing edge. **For a long-only spot bot, "downside volatility" is cheaply computable from
OHLCV** — e.g. the realised semi-deviation of negative log returns, or ATR measured only on down
candles.

### 2.3b Two further adverse findings you should weigh

**(a) Transaction costs kill vol management outside the market factor.**

> **Barroso, P., & Detzel, A. (2021).** *Journal of Financial Economics*, **140(3), 744–767.**
> DOI: [10.1016/j.jfineco.2021.02.009](https://doi.org/10.1016/j.jfineco.2021.02.009)

Finding: even using **six** cost-mitigation strategies, volatility management of factors **other
than the market** generally produces **zero abnormal returns after costs** and **significantly
reduces Sharpe ratios.** Any gains are concentrated in stocks with the lowest limits to
arbitrage. **→ For your platform: vol-manage the *market exposure* (i.e. your single pair's
exposure), not a factor spread.**

**(b) The scaling constant is calibrated with full-sample information — a look-ahead trap.**

> **Liu, F., Tang, X., & Zhou, G. (2019).** *The Journal of Portfolio Management*, **46(1), 38–51.**
> DOI: [10.3905/jpm.2019.1.107](https://doi.org/10.3905/jpm.2019.1.107)

The constant `c` in the Moreira-Muir scaling rule is normally chosen so the managed portfolio
matches the unmanaged portfolio's *full-sample* volatility. That is **look-ahead bias.**
**Real-time calibration removes the out-of-sample benefit.**

**→ Implementation rule for §2.4:** choose `TARGET_VOL` from a **rolling/expanding window only**,
never from the full sample. This is the single easiest way to accidentally fool yourself with vol
targeting.

### 2.3c ⚖️ The strongest counter-rebuttal (in fairness)

> **DeMiguel, V., Martín-Utrera, A., & Uppal, R. (2024). "A Multifactor Perspective on
> Volatility-Managed Portfolios."** *The Journal of Finance*, **79(5)**.
> DOI: [10.1111/jofi.13395](https://doi.org/10.1111/jofi.13395)

They propose a **conditional multifactor** portfolio and show it *does* beat its unconditional
counterpart out-of-sample and net of costs. But their own out-of-sample numbers (Jan 1977–Dec 2020,
expanding window, 9 factors) are sobering:
- **OOS ignoring costs:** vol-managed Sharpe is **lower than in-sample for ALL NINE factors**, and
  significant at 10% for only 4/9.
- **OOS net of costs:** Sharpe goes **NEGATIVE for FIVE of nine** vol-managed factors.
- **Their conclusion:** for **single factors** the effect dies; for **multifactor portfolios** it
  survives.

**→ Direct implication:** the multifactor rescue is **unavailable to you** — you have one pair, not
nine factors. **The evidence that vol targeting survives out-of-sample and net of costs applies to
multifactor portfolios, and your platform is the single-factor case where it is documented to
fail.** This is precisely why §9 recommends vol targeting as a **risk layer**, not an alpha source.

### 2.4 Implementable formulas (OHLCV-only)

**(a) Realised volatility (close-to-close), freqtrade-friendly:**
```
logret_t   = ln(close_t / close_{t-1})
sigma_t    = stdev(logret_{t-N+1..t}) * sqrt(periods_per_year)
```
`periods_per_year` from `research/backtest.py`: 1d=365, 4h=2190, 1h=8760.

**(b) Wilder / talib ATR version (preferred — it uses the full candle and is what you already have):**
```
atr_t      = ta.ATR(high, low, close, timeperiod=N)          # N=14 or 20
sigma_t    = atr_t / close_t * sqrt(periods_per_year)        # ATR as fraction of price, annualised
```

**(c) Downside (semi-)volatility — the Wang & Yan refinement:**
```
neg_t      = min(logret_t, 0)
sigma_down = sqrt( mean( neg_{t-N+1..t}^2 ) ) * sqrt(periods_per_year)
```

**(d) The sizing rule (target-vol):**
```
exposure_t = clip( target_vol / sigma_t , 0 , max_leverage )   # max_leverage = 1.0 on SPOT
```
Because you are **spot and unlevered**, `target_vol` should be chosen so that `exposure` is
**below 1.0 most of the time** — i.e. vol targeting here becomes a *de-risking* rule that takes
you partially to cash in high-vol regimes. **Concretely: setting `target_vol` equal to the
*median* realised vol means you are fully invested ~half the time and scaled down the rest.**
This is the only honest way to use vol targeting long-only: it cannot lever you up.

**(e) Smoothing (important to avoid fee churn):** apply an EWMA to `exposure_t` and/or only change
the position when `|exposure_t − exposure_{t-1}| > threshold` (e.g. 0.10). Without this, vol
targeting *adds* turnover and can cost more than it saves at 1h.

### 2.6 A crypto-specific negative result: the equity low-vol anomaly does NOT import

> **Burggraf, T., & Rudolf, M. (2021).** *Finance Research Letters*, **40(C)**, 101683.
> DOI: [10.1016/j.frl.2020.101683](https://doi.org/10.1016/j.frl.2020.101683)

Finding: across **~1,000 coins, 2013–2019**, there is *"no evidence of a significant low volatility
premium"* in crypto — **in contrast to equities, bonds and commodities.**

**Why this matters:** a natural instinct when vol targeting under-delivers on Sharpe is to reach
for the well-documented **low-volatility anomaly** (buy low-vol assets, earn higher risk-adjusted
returns). **That anomaly is a cross-sectional equity/bond/commodity result and does not appear to
exist in crypto.** Do not import it.

**⚠️ Tension in the literature (flagging honestly):** Kaya & Mostowfi (2022), *Finance Research
Letters* 46, 102422, DOI [10.1016/j.frl.2021.102422](https://doi.org/10.1016/j.frl.2021.102422)
(open access at ZHAW) finds significant excess returns from low-vol **selection** on highly liquid
coins — **but at 6–12 MONTH horizons with a stop-loss overlay**, and as **cross-sectional
selection**, not short-term timing. So the two papers do not directly contradict each other: the
*short-term timing* version of low-vol is absent (Burggraf & Rudolf), while a *long-horizon
cross-sectional selection* version is claimed (Kaya & Mostowfi). **Neither is usable as a
long-only, one-pair, short-horizon timing rule.**

### 2.7 Verdict on volatility targeting

- **As a Sharpe-improver: over-claimed.** The asset-class-conditional result (risk assets only)
  plus the Cederburg out-of-sample failure plus the "mostly volatility timing" decomposition mean
  the alpha claim does not stand up.
- **As tail/drawdown control: genuinely robust** — Harvey et al. find it across *all* asset
  classes, and it does not require any predictive ability.
- **Best use here:** a **sizing layer** on top of whatever entry signal you choose. It is the one
  component in this review whose core mechanism (vol clustering) is not in serious dispute.
- **Fees:** neutral-to-positive if smoothed, **negative if applied naively at 1h** (it will
  re-scale every candle). Must be thresholded.

---

## 3. Cross-Sectional vs Time-Series Momentum in Crypto

**Answer up front: cross-sectional momentum (XSM) is documented in crypto at a reasonable
academic standard, but it is fundamentally NOT implementable on your platform, and the reason is
structural — not a matter of clever engineering.**

**Why XSM cannot work long-only-spot-one-pair:**

XSM ranks assets against each other and goes **long winners / short losers**. The return documented
in the literature (e.g. LTW's Q5−Q1 spread) is a **long-short spread**. Decomposing it:

- The **short leg** contributes a large share of a crypto XSM spread, because crypto losers can
  continue to fall dramatically (and, per Grobys et al., the strategy has severe crash risk
  precisely on that leg).
- **You cannot short.** So you only get the long leg.
- The **long leg alone is closer to high-beta crypto beta than to alpha.** Long-only
  "buy the top-ranked coins" in a market where the average coin rises is mostly beta capture.
- Your brief says **one pair at a time**, which eliminates the *cross-section* entirely. XSM
  requires *simultaneously* holding the winners and (ideally) shorting the losers. With one pair,
  "cross-sectional momentum" collapses into **time-series momentum on that pair** — which is §1.

**Evidence quality, stated fairly:**
- LTW (2022) is a top-tier (Journal of Finance) verification that *something* momentum-like exists
  cross-sectionally in crypto, and they build a `CMOM` factor on a 3-week formation period.
- But Grobys & Sapkota (2019), *same sample period*, find **no** significant momentum payoffs.
- And Grobys & Shahzad argue the strategy's moments are not even well-defined.

**One relevant caveat that works in your favour:** Grobys et al. (2025, FMPM) note that
*"cryptocurrency momentum appears to be a phenomenon associated with large-cap cryptocurrencies."*
If true, this means the effect concentrates in liquid coins — which is where your (presumably
BTC/ETH/major) universe lives, and where fees are lowest. That is an argument for a
**large-cap-only, long-only trend strategy**, not for XSM.

### Verdict
**DO NOT BUILD cross-sectional momentum.** It is not a matter of the evidence being weak — the
evidence is moderate — it is that the *implementable subset* (long leg, one pair) is not the part
that generated the documented returns. This idea is **structurally out of scope** for a
long-only, one-pair spot platform, and I would flag it as such rather than attempt a workaround.

---

## 4. Mean Reversion / Overreaction

### 4.0 🛑 THE DECISIVE FINDING — reversal is an illiquidity artefact and INVERTS on liquid coins

> **Zaremba, A., Bilgin, M. H., Long, H., Mercik, A., & Szczygielski, J. J. (2021). "Up or down?
> Short-term reversal, momentum, and liquidity effects in cryptocurrency markets."**
> *International Review of Financial Analysis*, **78(C).** DOI: [10.1016/j.irfa.2021.101908](https://doi.org/10.1016/j.irfa.2021.101908)
> Verified abstract: https://ideas.repec.org/a/eee/finana/v78y2021ics1057521921002349.html

**Verified abstract (verbatim):**
> "We demonstrate a new powerful predictive signal for cryptocurrency returns: the last day's
> return. Based on daily prices of **more than 3600 coins**, we document that the cryptocurrencies
> with low last day's return significantly outperform their counterparts with high last day's
> return. The effect is confirmed by a battery of cross-sectional tests and portfolio sorts, and is
> not subsumed by a broad range of other return predictors. We argue that **the daily reversals
> result from the illiquidity of the vast majority of traded cryptocurrencies.** In consequence,
> the pattern is cross-sectionally dependent on liquidity, and **the handful of largest and most
> tradeable coins exhibit daily momentum rather than a reversal.** Our findings help to reconcile
> earlier conflicting evidence on return persistence in cryptocurrency markets."

**Why this is the single most important finding for your platform:**

You are **spot long-only on liquid pairs** (BTC, ETH, and major alts — exactly the pairs in
`research/data/`). Zaremba et al. show that is **precisely the universe where short-term reversal
flips sign and becomes momentum.** The "buy the dip" effect that is real and powerful in this
data set lives in **micro-cap, illiquid coins that you cannot trade and should not trade.**

So a naive dip-buying core on liquid crypto is **contrarian to the only documented effect in that
universe.** This is the strongest available argument against making mean reversion your core
strategy, and it independently explains why your existing "Bollinger lower band + RSI<40" strategy
loses: it is betting on reversal in the one universe where reversal is documented to invert.

**Corroborating peer-reviewed evidence:**

> **Bianchi, D., Babiak, M., & Dickerson, A. (2022).** *Journal of Banking & Finance*, **142(C)**,
> 106547. DOI: [10.1016/j.jbankfin.2022.106547](https://doi.org/10.1016/j.jbankfin.2022.106547)

Replicates the liquidity interpretation: the reversal is compensation for **adverse selection /
inventory risk**, i.e. **a market-making premium** — not a directional alpha. (This is the same
mechanism Kitron & Wengrowicz identify via taker-flow conditioning, §4.0b — two independent
confirmations that this is liquidity provision, not mispricing to be arbitraged.)

> **Grobys, K., Sandretto, D., & Äijö, J. (2026). "On survivor cryptocurrency momentum."**
> *Finance Research Letters*, **92(C).** DOI: [10.1016/j.frl.2026.101339](https://doi.org/10.1016/j.frl.2026.101339)
> RePEc: https://ideas.repec.org/a/eee/finlet/v92y2026ics1544612326001339.html

Finding: crypto momentum *"is not evident when applied to survivor coins."* **This matters
specifically for your platform** — see §10's survivorship-bias warning. Your `research/data/`
contains only *surviving* pairs. This paper suggests the momentum effect itself may be weaker on
survivor-only samples, which is precisely the sample you will backtest on.

> **Fieberg, C., Liedtke, G., & Zaremba, A. (2024). "Cryptocurrency anomalies and economic
> constraints."** *International Review of Financial Analysis*, **94(C).**
> DOI: [10.1016/j.irfa.2024.103218](https://doi.org/10.1016/j.irfa.2024.103218)

Reported findings: size/volume anomalies *"originate from micro-cap coins of negligible economic
importance"*; momentum *"prevails in larger cryptocurrencies but incurs substantial trading costs
and extracts alphas largely from **short positions**."*

**Two independent kills in one sentence:** (a) the anomalies that survive in *your* universe
(large caps) are the momentum ones, not reversal; (b) even those extract their alpha from the
**short leg**, which you do not have. This is consistent with §3's conclusion on cross-sectional
momentum and reinforces the overall verdict.

### 4.0b ⚠️ A recent rigorous measurement: the reversal is REAL but too small to clear costs

> **Kitron, N. A., & Wengrowicz, J. M. (2026). "Short-horizon mean reversion in cryptocurrency
> markets: a matched cross-market measurement."** arXiv:2608.21888 [q-fin.TR].
> https://arxiv.org/abs/2608.21888 · Replication code: https://github.com/nadav2/short-horizon-reversion
> **⚠️ PREPRINT — not yet peer-reviewed.** But methodologically strong (matched out-of-sample
> protocol, permutation null, frozen holdout) and the most directly relevant measurement found.

**Verbatim abstract highlights:**
> "At 15-minute horizons, directional mean reversion is far stronger and more pervasive in
> cryptocurrency markets than in US equities: scored under one matched, strictly out-of-sample
> protocol, **90% of 183 Binance pairs carry significant directional reversal** against 2.7% of
> 187 US stocks and ETFs... The signal lives in signs, not magnitudes... On the originating tape,
> the reversal concentrates after moves driven by aggressive taker flow and grows with flow
> intensity... a conditioning consistent with **compensated liquidity provision**...
> **The gross edge peaks near 1.3 bp per trade against a 5 bp round-trip cost: large enough to
> detect, too small to clear benchmark spot capture costs.**"

**Why this is the most honest single data point in the review:**
- It **confirms** short-horizon reversal in crypto is real and pervasive (90% of 183 Binance
  pairs) — stronger than in equities. So the effect is *not* imaginary.
- It measures the **gross edge at ~1.3 bp per trade against a 5 bp round-trip cost** — i.e. the
  edge is **~4× too small to trade even at the cheapest possible cost assumption.**
- It attributes the effect to **compensated liquidity provision** (i.e. you are being paid to
  provide liquidity to taker flow), which is exactly why it is a *market-making* premium and not
  a directional alpha you can harvest with taker orders.
- ⚠️ **Note the horizon: 15-minute.** This does not transfer to 1h/4h/1d — and higher timeframes
  generally have *smaller* per-trade edges but lower turnover, not larger edges.

**This is the cleanest available illustration of the review's central theme:** a genuinely
significant, pervasive, out-of-sample-robust effect that is **still not tradeable at spot taker
costs.** Statistical significance and tradeable edge are different things.

### 4.0c 🚨 COST PREMISE CORRECTION — your real hurdle may be ~2.5–30× higher

The brief assumed **0.2% round trip**. The platform's own code assumes **~0.3%**
(0.1% fee + 0.05% slippage per side). **Both may be optimistic for retail spot.**

**A widely-cited industry measurement (⚠️ NOT peer-reviewed — sponsored by INTAS.tech /
Frankfurt School of Finance & Management, Co-Pierre Georg):** across **432 live round-trips**,
9 MiCAR-regulated providers, 6 coins, EUR 100/500 tickets: *"Average total costs per round-trip
range from approximately **0.53% to 6.45%**."*

That is **~53 bp at the cheapest provider** (≈2.65× your 0.2% assumption) and up to **645 bp** at
the worst (≈32×). **Caveats:** industry-sponsored, retail-ticket-sized, EU-regulated providers —
it may not describe your specific Binance VIP tier. But it is a serious warning that a flat 20 bp
assumption is likely too generous for retail-sized spot orders.

**Consequence for every ranking in this document:** if the true hurdle is ~53 bp rather than
~20 bp, then **the case against mean reversion, squeeze breakouts, and seasonality shifts from
"strong" to "overwhelming"**, and even the trend ensemble must be very low-turnover to survive.
**Re-derive your own cost before further strategy work.**

**How to measure your own spread from OHLCV alone (peer-reviewed method):**

> **Brauneis, A., Mestel, R., Riordan, R., & Theissen, E. (2021). "How to measure the liquidity of
> cryptocurrency markets?"** *Journal of Banking & Finance*, **124(C)**, 106041.
> DOI: [10.1016/j.jbankfin.2020.106041](https://doi.org/10.1016/j.jbankfin.2020.106041)
> Semantic Scholar: https://www.semanticscholar.org/paper/fb758c915db141bbe20f9fd94bebbb1648ad5bfa

This paper validates low-frequency liquidity proxies for crypto and identifies the **Corwin &
Schultz (2012)** and **Abdi & Ranaldo (2017)** high-low spread estimators as the best performers.
**Both compute an effective spread estimate from candles alone** — i.e. they are usable on your
existing `research/data/` parquet files with no external feed.

The **Corwin-Schultz** estimator works from consecutive two-day high/low pairs:

```
# Corwin & Schultz (2012) two-day high-low spread estimator
# beta = E[ (ln(H_t/L_t))^2 + (ln(H_{t+1}/L_{t+1}))^2 ]
# gamma = ( ln( max(H_t,H_{t+1}) / min(L_t,L_{t+1}) ) )^2
# alpha = ( sqrt(2*beta) - sqrt(beta) ) / (3 - 2*sqrt(2))  -  sqrt(gamma / (3 - 2*sqrt(2)))
# spread = 2*(exp(alpha)-1) / (1+exp(alpha))     # set negative values to 0
```
**Recommendation:** compute this per pair on your existing data and use the **measured** spread
instead of a flat constant in `research/backtest.py`. This is a concrete, self-contained task that
will sharpen every subsequent backtest.

### 4.1 The best crypto-specific evidence, and it is conditional

> **Wen, Z., Bouri, E., Xu, Y., & Zhao, Y. (2022). "Intraday return predictability in the
> cryptocurrency markets: Momentum, reversal, or both."**
> *The North American Journal of Economics and Finance*, **62**, 101733.
> DOI: [10.1016/j.najef.2022.101733](https://doi.org/10.1016/j.najef.2022.101733)
> Abstract mirror: https://m2.mtmt.hu/api/publication/33003705

**Findings:** BTC high-frequency data, 2013-03-03 → 2020-05-31. **Both** intraday momentum and
intraday reversal are present. Crucially, the predictability is **CONDITIONAL** — it *changes* in
the presence of **large intraday price jumps, FOMC announcements, liquidity levels, and COVID-19.**
Intraday reversal is described as *"unique to the cryptocurrency market"* and attributed to
**overreaction to non-fundamental information and overconfidence.** Replicated in ETH, LTC, XRP.

**Why this is important for you:** this is the *only* paper in this review that gives direct,
peer-reviewed support to the idea that **crypto mean reversion works better when conditioned on
regime and on the magnitude of the move** — exactly the hypothesis in your question 4. The
conditioning variables that matter most, and are OHLCV-derivable, are **jump magnitude** and
**liquidity**.

**But note the horizon:** this is *intraday high-frequency*. It does **not** transfer to 1d, and
transferring it to 1h requires the jump-magnitude conditioning to be real. Also note the
contradiction with your existing RSI strategy: the paper says intraday reversal is driven by
overreaction to **non-fundamental** news, i.e. precisely the kind of noise-driven move an RSI(14)
oversold signal fires on. That is a *reason* RSI-in-isolation churns: it cannot distinguish
overreaction (mean-reverts) from information arrival (continues).

### 4.2 The specific claim I could NOT verify — flagged honestly

Your question asks about "crypto RSI/mean-reversion papers with real out-of-sample results", and
about an LTW "1-week reversal" result. **I could not verify a specific LTW short-term reversal
number from a primary source.** The NBER abstract and the JoF landing page describe the
*three-factor (market, size, momentum)* result and do **not** advertise a 1-week reversal finding.
Wiley full text was inaccessible (403).

**Do not cite Liu/Tsyvinski/Wu (2022) for a specific 1-week reversal magnitude.** The paper is
real and top-tier; that *particular* claim is unverified.

### 4.3 The methodological kill-shot for all simple TA rules (including RSI)

> **Bajgrowicz, P., & Scaillet, O. (2012). "Technical trading revisited: False discoveries,
> persistence tests, and transaction costs."**
> *Journal of Financial Economics*, **106(3), 473–491.** DOI: [10.1016/j.jfineco.2012.06.001](https://doi.org/10.1016/j.jfineco.2012.06.001)
> Working paper: https://ideas.repec.org/p/chf/rpseri/rp0805.html

**Verbatim abstract:**
> "We revisit the apparent historical success of technical trading rules on daily prices of the
> DJIA from 1897 to 2008. We use the False Discovery Rate as a new approach to data snooping...
> **Persistence tests show that an investor would never have been able to select ex ante the
> future best-performing rules. Moreover, even the in-sample performance is completely offset by
> the introduction of transaction costs.** Overall, our results seriously call into question the
> economic value of technical trading rules."

**Two independent kill mechanisms, both of which apply to your platform:**
1. **No ex-ante rule selection ability** — the rule that backtests best is not the rule that
   performs best next. (This is exactly the trap your 10 classic strategies fell into.)
2. **Costs wipe out even in-sample gains.** Directly relevant at 30 bps/round trip.

### 4.4 The balanced survey

> **Park, C.-H., & Irwin, S. H. (2007). "What Do We Know About The Profitability Of Technical
> Analysis?"** *Journal of Economic Surveys*, **21(4), 786–826.**
> DOI: [10.1111/j.1467-6419.2007.00519.x](https://doi.org/10.1111/j.1467-6419.2007.00519.x)

**Verbatim:** *"Among a total of 95 modern studies, 56 studies find positive results regarding
technical trading strategies, 20 studies obtain negative results, and 19 studies indicate mixed
results."* **But:** *"most empirical studies are subject to various problems in their testing
procedures, e.g. data snooping, ex post selection of trading rules or search technologies, and
difficulties in estimation of risk and transaction costs."*

**Calibrated reading:** the naive count (56/95 positive) *looks* encouraging, but the authors
themselves say it is not conclusive. Cite this as "the TA literature looks positive but is
methodologically compromised", **not** as "TA works".

### 4.5 Crypto-specific technical trading — and the out-of-sample failure

> **Hudson, R., & Urquhart, A. (2021). "Technical trading and cryptocurrencies."**
> *Annals of Operations Research*, **297**, 191–220. DOI: [10.1007/s10479-019-03357-1](https://doi.org/10.1007/s10479-019-03357-1)
> https://centaur.reading.ac.uk/85715/

~15,000 TA rules, 5 classes, 2 BTC markets + 3 other coins. Reported: significant in-sample
predictability and profitability, breakeven transaction costs *"substantially higher than those
typically found in cryptocurrency markets"*, robust to data-snooping procedures.

**⚠️ The critical sentence, verbatim:**
> "**there is no predictability for Bitcoin in the out-of-sample period**, although predictability
> remains in other cryptocurrency markets."

**This is the most direct warning available for your exact situation.** A 15,000-rule sweep found
in-sample profits in BTC that **did not survive out-of-sample**. If your platform trades BTC, the
best available crypto-specific TA evidence says the in-sample edge is not there live.

*(Correction to the brief: the paper is **Hudson & Urquhart (2021)**, two authors — there is no
"McGroarty" co-author on this paper.)*

### 4.6 RSI specifically — weak, and the honest version is negative

> **Zatwarnicki, M., Zatwarnicki, K., & Stolarski, P. (2023). "Effectiveness of the Relative
> Strength Index Signals in Timing the Cryptocurrency Market."**
> *Sensors*, **23(3), 1664.** DOI: [10.3390/s23031664](https://doi.org/10.3390/s23031664)

Verbatim finding: *"the results show that the **RSI as a momentum indicator in the cryptocurrency
market involves high risk**. Using alternative RSI applications can allow traders to gain an
advantage..."*

**Reading:** naive RSI is found to be high-risk; only non-standard variants are claimed to help.
The positive claim rests on an MDPI venue and should be treated cautiously. **This is not evidence
that RSI(14) oversold recovery works.**

### 4.6b Overreaction is real in the data but NOT exploitable in either direction

> **Caporale, G. M., & Plastun, A. (2019). "Price overreactions in the cryptocurrency market."**
> *Journal of Economic Studies*, **46(5), 1137–1155.** DOI: [10.1108/JES-09-2018-0310](https://doi.org/10.1108/JES-09-2018-0310)
> https://ideas.repec.org/a/eme/jespps/jes-09-2018-0310.html

BTC, LTC, XRP, DASH. Parametric and non-parametric tests *"confirm the presence of price patterns
after overreactions: the next day price changes in both directions are bigger than after 'normal'
days."*

**But, verbatim:**
> "The results suggest that a strategy based on **counter-movements after overreactions is not
> profitable**, whilst one based on **inertia appears to be profitable but produces outcomes not
> statistically different from the random ones**. Therefore, the overreactions detected in the
> cryptocurrency market **do not give rise to exploitable profit opportunities (possibly because of
> transaction costs)** and cannot be seen as evidence against the EMH."

**This is a clean, direct answer to your question 4.** The overreaction is *statistically real*
(next-day moves are larger), but:
- the **contrarian trade loses money**, and
- the **momentum trade is indistinguishable from random**.

Note the authors' own hedge — *"possibly because of transaction costs"* — which is precisely the
constraint that binds your platform. **Crypto overreaction is a measurable statistical fact that
is not a tradeable edge.** That combination — real in-sample pattern, no exploitable P&L — is the
canonical signature of an effect that costs eat.

Also relevant: **Borgards, O., & Czudaj, R. L. (2020). "The prevalence of price overreactions in
the cryptocurrency market."** *Journal of International Financial Markets, Institutions & Money*,
**65(C).** DOI: [10.1016/j.intfin.2020.101195](https://doi.org/10.1016/j.intfin.2020.101195)

### 4.7 The most useful methodological paper for your platform

> **Zatwarnicki, M., Zatwarnicki, K., & Stolarski, P. (2025). "Timing Usage of Technical Analysis
> in the Cryptocurrency Market."** *Applied Sciences*, **15(23), 12802.**
> DOI: [10.3390/app152312802](https://doi.org/10.3390/app152312802)

Introduces a Rolling Strategy-Hold Ratio (RSHR) explicitly because *"many traders fail to test
their strategies adequately, limiting evaluations to selected time periods and **risking
overfitting**."* **Recommend adopting this discipline** regardless of which strategy you pick —
see §10.

### 4.7b The theory of conditional reversal — and why it is so hard to specify

> **Giner, J., & Zakamulin, V. (2023). "The (dis)advantages of regime-switching models for
> momentum and mean reversion."** *Economic Modelling*, **122**, 106237.
> DOI: [10.1016/j.econmod.2023.106237](https://doi.org/10.1016/j.econmod.2023.106237)
> https://ideas.repec.org/a/eee/ecmode/v122y2023ics0264999323000494.html

**Verified abstract (verbatim):**
> "A vast body of empirical literature documents the existence of short-term momentum and
> medium-term mean reversion... A Markov model, wherein the return process randomly switches
> between bull and bear states, can reproduce many stylized facts of financial asset returns,
> **excluding the mean reversion.** An important limitation of the Markov model is that the state
> termination probability does not depend on age. We develop a **semi-Markov** model wherein...
> the state termination probability **increases with age.** We demonstrate that this model induces
> short-term return momentum and subsequent reversal."

**The structural insight, which is genuinely valuable:** regime models reproduce **both** momentum
and reversal, and the reversal arises from **state aging (duration dependence)** — *not* from any
property of the asset itself. So the correct formulation of your conditional-reversion hypothesis
is **"state-dependent AND duration-dependent."**

**Three cautions:**
1. It is **pure theory** — no net-of-cost, out-of-sample trading test.
2. The reversion is **medium-term by construction**, not daily or 4h.
3. Calibrated on **equities**, not crypto. Only ~3 citations so far.

**Why this matters practically:** this makes the conditional-reversion hypothesis **very hard to
specify ex ante** and **highly vulnerable to look-ahead bias** — if you identify regimes using the
full sample, you will manufacture a beautiful backtest that cannot be traded. **Any regime
detection you implement must be strictly causal / expanding-window.**

> **Related, and closer to your question:** Zakamulin, V., & Giner, J. (2024). "Optimal
> trend-following rules in two-state regime-switching models." *Journal of Asset Management*,
> **25(4), 327–348.** DOI: [10.1057/s41260-024-00357-0](https://doi.org/10.1057/s41260-024-00357-0)
> — derives optimal trend-following rules *within* regime models. This is the closest academic
> work to "when should a trend/breakout system be trading", and is a promising follow-up.

> **OHLCV-specific and directly relevant:** Caporin, M., Ranaldo, A., & Santucci de Magistris, P.
> (2013). "On the predictability of stock prices: A case for high and low prices." *Journal of
> Banking & Finance*, **37(12), 5132–5146.**
> DOI: [10.1016/j.jbankfin.2013.08.007](https://doi.org/10.1016/j.jbankfin.2013.08.007)
> — predictability from candle **high/low** prices. Worth reading for an OHLCV-only platform.

### 4.8 Verdict on mean reversion

**DOWNGRADED to "do not build as a core" following the Zaremba et al. (2021) finding.**

- **Short-term reversal in crypto is an illiquidity artefact.** It is documented across 3,600+
  coins, but **the largest, most tradeable coins exhibit daily MOMENTUM instead.** Your universe
  is the one where the effect inverts. Building a dip-buyer on liquid spot is betting *against*
  the documented direction of the effect in that universe.
- **Overreaction is real but not exploitable** — the contrarian leg loses money outright
  (Caporale & Plastun, 2019), independently of costs.
- **Conditioning is the only defensible path**, and it is weakly supported: Wen et al. (2022) find
  the intraday effect is conditional on jump magnitude and liquidity — but that is *intraday*, not
  1h/4h/1d.
- **⚠️ Failure mode:** mean reversion is short volatility in disguise. It collects small premiums
  and pays out rarely and catastrophically. Given crypto's power-law tails (Grobys et al.), a
  naked dip-buyer on spot can suffer −70%+ drawdowns. **Every "buy the dip" that worked 2013–2021
  died in 2022.**
- **Fees:** reversal has the highest turnover of any idea here and is the *most* fee-sensitive.
  At 30 bps/round trip, naked reversal is not viable.

**Revised recommendation:** mean reversion should not be a core strategy. If used at all, it must
be (a) gated behind a confirmed uptrend so you only buy dips in bull regimes, and (b) sized
small. It ranks **below** the volatility-contraction idea and should be tested last.

---

## 5. Trend Following with Regime Filters / Multi-Horizon Ensembles

**This is the strongest-evidence area in the review, and therefore where I recommend you build.**

### 5.1 The century-scale evidence

> **Hurst, B., Ooi, Y. H., & Pedersen, L. H. (2017). "A Century of Evidence on Trend-Following
> Investing."** *The Journal of Portfolio Management*, **44(1)**, 15–29.
> DOI: [10.3905/jpm.2017.44.1.015](https://doi.org/10.3905/jpm.2017.44.1.015)
> SSRN: https://doi.org/10.2139/ssrn.2993026

**Verified abstract (verbatim):**
> "the authors study the performance of trend-following investing across global markets **since
> 1880**, extending the existing evidence by more than 100 years using a novel data set. They find
> that **in each decade since 1880, time-series momentum has delivered positive average returns
> with low correlations to traditional asset classes.** Further, time-series momentum has performed
> well in **8 out of 10 of the largest crisis periods** over the century... and has performed well
> across different macro environments, including recessions and booms, war and peace, high- and
> low-interest-rate regimes, and high- and low-inflation periods."

**Why this is the strongest evidence in the review:** 136 years, every decade positive, 8/10
crises. No other idea here comes close on sample length.

**⚠️ But the honest caveats:**
1. **It is AQR research on AQR's own strategy.** Not independent. (The *Journal of Portfolio
   Management* is peer-reviewed, but the conflict is real and worth naming.)
2. **It is a diversified multi-asset, long-short futures portfolio.** The "performs well in
   crises" property is a **diversification and long-volatility property**. A long-only spot crypto
   bot has **no short leg and no diversification** — it cannot harvest the crisis-alpha component.
   Trend following's best-documented property is precisely the one you cannot access.
3. Huang et al. (2020) still applies: the *predictive* claim is weak.

### 5.2 Multi-horizon ensembles — the mechanism and the evidence

**The argument for combining lookbacks** (well-supported in the managed-futures literature and
the Graham/CME "speed of trend" work):

- A **fast** trend rule has high turnover → costs and whipsaw.
- A **slow** trend rule reacts late → gives back a lot of the move.
- Their **errors are imperfectly correlated**, so an equal-weighted ensemble of speeds has a
  **better Sharpe than any individual speed**, and critically, **it is far less sensitive to the
  arbitrary choice of lookback parameter.**

That last point is the real prize and it directly addresses your problem: your 10 strategies are
each a *single* parameterisation, and Bajgrowicz & Scaillet's finding is that you cannot select
the ex-ante-best rule. **An ensemble removes the need to select.** You stop betting on EMA(20/50)
vs EMA(50/200) and hold both.

**Verified supporting source (multi-horizon, and it makes the parameter-sensitivity point):**

> "Evaluating time-series momentum against machine learning in commodity futures using
> **multi-horizon** and term-structure signals" — Bumann, B. L. (2025). Repository deposit.
> **⚠️ Not peer-reviewed.** Its verified abstract nonetheless *replicates MOP and finds the
> effect weakened after 2009*: *"confirming strong pre-2009 performance and weaker results
> thereafter."* **This decay-after-2009 finding is important and recurring** — several independent
> sources point to TSMOM being weaker post-2009.

> **Lim, B., Zohren, S., & Roberts, S. (2019). "Enhancing Time-Series Momentum Strategies Using
> Deep Neural Networks."** *The Journal of Financial Data Science*, **1(3)**, 24–42.
> DOI: [10.3905/jfds.2019.1.015](https://doi.org/10.3905/jfds.2019.1.015)

Verified: 88 futures contracts; Sharpe-optimised LSTM beat traditional methods >2× **in the
absence of transaction costs**, and *"continued outperforming when considering transaction costs
up to **2–3 bps**."*

**⚠️ This is an extremely valuable negative result for you:** the ML trend model's advantage
**evaporated above 2–3 bps** of cost. **Your cost is ~15 bps per side (30 bps round trip) — 5 to
10× higher.** Any high-turnover signal enhancement is therefore dead on arrival at your cost
level. **This is the single strongest quantitative argument in this review for slow timeframes
and low turnover.**

### 5.3 The regime filter: Faber's SMA rule, and its criticism

> **Faber, M. T. (2007). "A Quantitative Approach to Tactical Asset Allocation."**
> *The Journal of Wealth Management*, **9(4)**, 69–79. DOI: [10.3905/jwm.2007.674809](https://doi.org/10.3905/jwm.2007.674809)

The **10-month SMA rule** (= SMA(200) on daily data): hold the asset when `close > SMA(200)`,
move to cash otherwise. Applied monthly. This is essentially your existing "Faber SMA(200) trend
allocation" strategy.

**⚠️ The criticism you asked for — and the strongest version comes from Faber himself:**

> **Faber, M. T. (2018). "A Quantitative Approach to Tactical Asset Allocation Revisited 10 Years
> Later."** *The Journal of Portfolio Management*, **44(2)**, 156–167.
> DOI: [10.3905/jpm.2018.44.2.156](https://doi.org/10.3905/jpm.2018.44.2.156)

**Faber's own admission (verbatim, quoted from the 2018 revisit):** his method *"wound up
substantially outperforming the market in 2008–2009, but then **trailed the market over the
following eight years**."* He reframes the goal as reducing volatility and drawdowns rather than
beating the market.

**This is decisive calibration from the author:** the headline outperformance is **concentrated in
the 2000–2002 and 2007–2009 bear markets.** Out of those regimes, the rule trailed. Any backtest
of a slow SMA filter will look superb if your sample happens to contain two large bear markets —
and crypto samples are short, so this is a live risk.

Two further verified caveats from Faber (2007) itself:
- *"The timing system achieves these superior results while **underperforming the index in roughly
  half of all years since 1900**."* — i.e. **~50% of years are losing-relative years.** You must be
  able to hold a strategy that trails half the time.
- *"**Taxes, commissions, and slippage are excluded**."* — the 2007 results are **gross**. Max
  drawdown improved from 83.66% to 42.24%, and it exited before Oct 2000 and on 31 Dec 2007 — but
  those benefits are stated before costs.

**Peer-reviewed critique:** Marmi, Pacati, Reno & Risso (2013), "A quantitative approach to Faber's
tactical asset allocation," *International Journal of Computational Economics and Econometrics*
3(1/2), 91–101, DOI [10.1504/IJCEE.2013.056268](https://doi.org/10.1504/IJCEE.2013.056268).
Abstract (quoted; full text paywalled, so no p-values): *"...A very popular example is 'A
quantitative approach to tactical asset allocation' by M. Faber... Is this paper a counterexample
to market efficiency? **We reject this conclusion**, showing that a lot of caution should be used
in this field"* — they use bootstrapping experiments to argue the result is not robust evidence
against efficiency.

**My honest reading:** the SMA(200) filter is a **defensive overlay with a real but modest and
regime-dependent benefit**, not an alpha source. In crypto — where 80%+ drawdowns are common — a
slow trend filter has a *better* justification than in equities, because the left tail it protects
against is far larger. **That asymmetry is the strongest single argument in favour of trend
filtering on a long-only crypto spot book.** But it will lag in choppy markets, it trails in
roughly half of all years, and you must expect it to underperform buy-and-hold in bull markets.

### 5.4b 🎯 The single most important study for your platform: Han, Kang & Ryu (2024)

> **Han, C., Kang, B., & Ryu, J. (2024). "Time-Series and Cross-Sectional Momentum in the
> Cryptocurrency Market: A Comprehensive Analysis under Realistic Assumptions."**
> SSRN DOI: [10.2139/ssrn.4675565](https://doi.org/10.2139/ssrn.4675565)
> Presented at the New Zealand Finance Meeting, Dec 2024 (hosted by AUT/ACFR — the authors are at
> Sungkyunkwan University, Seoul; **AUT only hosts it**).
> **Accepted at the *Review of Asset Pricing Studies*; volume/issue/DOI not yet assigned.**
> Full text (116pp) retrieved and read via the `r.jina.ai` reader proxy (direct fetch is
> Cloudflare-blocked):
> https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf

**This is the closest thing in the literature to a study designed for your exact constraints.**
Sample 28 Dec 2013 – 28 Aug 2023, mktcap ≥ $1M **and** daily volume ≥ $1M, stablecoins excluded,
no survivorship bias; lookbacks/holdings 1–56 **days**; Binance-futures subsample.

#### ✅ Good news: time-series momentum is real, and it is a LONG-ONLY effect

- Rule: buy the market when the lookback return is in the **top tercile** of historical returns;
  cash otherwise (long-only variant).
- **Best cell (28-day lookback, 5-day holding): Sharpe 1.51 vs market 0.84**, cumulative return
  36,686% vs market 2,696%, **and lower max drawdown (61.8% vs 89.1%)** — *after* 15 bps costs.
  It holds a position only ~48% of the time.
- **The gain comes from reduced downside risk, not higher returns.** Verbatim: *"The strategy holds
  a long position only when the market is bullish and defends well against market downturns."*
  **This independently confirms recommendation #1 of this review on 10 years of crypto data.**
- **Robust across weighting schemes** (Sharpe 1.40–1.65) and across size/volume/overreaction groups
  (Sharpe ~1.67–1.76).
- **The short leg loses money.** Verbatim: *"All the portfolios but the (21, 7) portfolio make
  losses at the end of the sample period even without transaction costs. It appears that time-series
  momentum is almost non-existent when the market is bearish... Adding short positions only erodes
  the mean return without reducing the risk."*
  **→ Strong direct support for a LONG-ONLY implementation. Your constraint is an advantage here.**
- **Momentum lives in the LONG leg and in LARGE coins** — the opposite of equities. Minor coins
  mostly show **reversal**. Three independent papers now agree (§4.0).
- Momentum is **concentrated in bullish markets**: in a "bad" market state the coefficient is
  *always insignificant*.

#### 🛑 The methodology finding that should change how you validate everything

> **"Ten portfolios yield a positive mean return with a t-statistic greater than 2.0, but only three
> of them have a mean log return with a t-statistic greater than 2.0. Moreover, six portfolios with
> a positive mean return are either liquidated or earn a negative profit."**

The arithmetic mean return and the **compounded** return diverge sharply under fat tails (Jensen's
inequality). Their calibration implies return skewness of **14.4** and kurtosis of **466** — so a
portfolio can have a statistically significant *arithmetic* mean and still **compound to zero**.

**Actionable requirement:** your backtest harness must report **mean LOG return / CAGR and its
t-statistic**, not just the arithmetic mean and Sharpe. `research/backtest.py` already tracks the
compounded equity curve (good) — but any *significance* test on per-trade means must be repeated on
**log returns**, or it will declare strategies profitable that are not.

#### ⚠️ Other cautions from the same paper
- **Look-ahead bias is admitted:** they tested many (lookback, holding) pairs and picked the best.
  Verbatim: *"this practice introduces a look-ahead bias... our findings should be regarded as an
  optimistic view."*
- **Rebalancing day matters materially:** the same (28,7) strategy yields **Sharpe 1.40 rebalanced
  Mondays vs 1.09 on Sundays.** Verbatim: *"This result demonstrates how an empirical study can be
  distorted when it assumes rebalancing on a particular day of the week."*
  **→ Test rebalancing-day sensitivity; never choose a day because it backtests best.**
- **Max drawdown is still 61.8%** for the best strategy. This is not a smooth strategy.
- **Cost assumption:** 15 bps/trade from real Binance fees (10 bps spot / 4.5 bps futures) and
  slippage measured from 15.6M market-order records. They call 15 bps *"a reasonable estimate (or
  perhaps closer to the lower limit) of the actual transaction costs"* — and note it was measured
  on **futures** with small (~$56) orders.
- Sobering overall conclusion, verbatim: *"a momentum-based long-short strategy that can generate
  steady, market-neutral profits appears unattainable. The maximum Sharpe ratio we obtain from a
  momentum strategy is about 1.5."*

**Overall:** this paper **strongly supports recommendation #1** (long-only trend/TSMOM with
downside protection), **strongly supports long-only over long-short**, **supports the large-cap
focus**, and simultaneously **demolishes naive backtesting methodology**. It is the single most
valuable source in this review — and its limitations (self-admitted look-ahead bias, 61.8% MDD,
single 10-year sample) are exactly why it supports a *risk-reduction* framing rather than an alpha
claim.

### 5.4c 🌟 The best available crypto-specific trend backtest: Zarattini, Pagani & Barbon (2025)

> **Zarattini, C., Pagani, A., & Barbon, A. (2025). "Catching Crypto Trends: A Tactical Approach for
> Bitcoin and Altcoins."** *Swiss Finance Institute Research Paper Series* No. 25-80.
> RePEc: https://ideas.repec.org/p/chf/rpseri/rp2580.html · SSRN: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907
> Author copy: https://abarbon.com/papers/catching-crypto-trends
> **⚠️ Working paper (SFI Research Paper series), not yet peer-reviewed.** Abstract verified directly.

**Verified abstract (verbatim):**
> "This paper applies a well-established trend-following methodology... to Bitcoin, and then extends
> the analysis to a comprehensive, **survivorship bias-free** dataset covering all cryptocurrencies
> traded **since 2015**... We propose an **ensemble approach that aggregates multiple Donchian
> channel-based trend models, each calibrated with different lookback periods, into a single
> signal, as well as a volatility-based position sizing method.** This model, applied to a
> rotational portfolio of the **top 20 most liquid coins**, achieved notable **net-of-fees** returns,
> with a **Sharpe ratio above 1.5 and an annualized alpha of 10.8% versus Bitcoin**."

**This is the single strongest piece of crypto-specific evidence for recommendation #1.** It is
*exactly* the architecture recommended in §9 — a multi-horizon Donchian ensemble plus volatility-
based sizing — tested on survivorship-bias-free crypto data with **costs included**.

**Method (as reported):** 9-horizon Donchian ensemble with lookbacks **5, 10, 20, 30, 60, 90, 150,
250, 360 days**, plus volatility-based position sizing capped at 25% per position. Rotational
top-20 most-liquid coins, monthly rebalance, liquidity exits (median daily volume > $1M).

**Per-lookback results (BTC, ⚠️ Table 1 EXCLUDES transaction costs):**

| Lookback | CAGR | Vol | Sharpe | Sortino | MDD |
|---|---|---|---|---|---|
| **5d** | 36% | 19% | **1.66** | 1.87 | 25% |
| 10d | 32% | 18% | 1.55 | 1.64 | 27% |
| 20d | 34% | 18% | 1.60 | 1.60 | 26% |
| 30d | 34% | 19% | 1.61 | 1.61 | 24% |
| 60d | 28% | 19% | 1.30 | 1.25 | 19% |
| 90d | 27% | 20% | 1.20 | 1.15 | 24% |
| **150d** | 21% | 20% | **0.99 (worst)** | 0.97 | 29% |
| 250d | 25% | 20% | 1.13 | 1.15 | 33% |
| 360d | 29% | 20% | 1.28 | 1.27 | 34% |
| **Combo (9-horizon)** | 30% | **17%** | **1.58** | **2.03** | **19%** |

**Three crucial readings:**
1. **Short lookbacks (5–30 days) dominate** on Sharpe; **150d is the worst.** This is the *opposite*
   of equities/futures (where 12-month lookbacks win) and is consistent with crypto momentum being
   a faster phenomenon.
2. **⚠️ THE ENSEMBLE'S EDGE IS IN THE TAILS, NOT IN SHARPE.** The Combo's Sharpe (1.58) is **below**
   the best single short lookback (5d: 1.66) — but its **Sortino (2.03) and MDD (19%) are the best
   of any configuration.** **This is an important correction to the multi-horizon thesis of §5.2:
   do not expect an ensemble to dominate on Sharpe; expect it to dominate on drawdown and downside
   risk.** Anyone promising that multi-horizon "improves risk-adjusted returns" is over-claiming —
   it improves *downside*-adjusted returns, which is a different and more defensible claim.
3. **Passive BTC MDD was >80% vs the Combo's 19%.** This is the risk-reduction case in one number.

**⚠️ Costs — the detail that matters most:**
- The single-asset BTC Sharpes above **exclude costs**.
- The **diversified portfolio results DO include them**: 10 bps transaction cost and a **20%
  rebalance threshold** (i.e. they too needed turnover hysteresis).
- **Headline net-of-fees result:** *"A diversified strategy targeting the top 20 most actively
  traded cryptocurrencies would have produced net-of-fees Sharpe ratio 1.57, a maximum drawdown of
  just 11%, and a statistically significant alpha of 11% relative to Bitcoin."*
- Cost sensitivity tested at 0, 10, 25, 50 bps; Sharpe "remarkably stable" at ~1.5 for portfolios
  of 5–50 assets.

**⚠️ Honest limitations:**
- **Not peer-reviewed** (working paper).
- **The headline results require a 20-coin rotational portfolio.** Your platform is **one pair at
  a time**, so you **cannot** replicate the diversification that produces MDD 11% and net Sharpe
  1.57. **For a single pair, the relevant figures are the gross per-lookback ones (Sharpe
  ~1.2–1.66 before costs)**, which must then be discounted for your own costs and lack of
  diversification. **This is the most important caveat in this section.**
- Their 50 bps cost ceiling is still below the retail-cost range flagged in §4.0c.

**Verdict:** strong, crypto-specific, architecture-matched support for #1 — **but read it as
"multi-horizon trend with vol sizing works well on a diversified basket", not as "this works on one
pair".**

### 5.4d ⚖️ Optimal multi-horizon blending is NOT equal-weight

> **Benhamou, E., Ohana, J.-J., Etienne, G., Guez, B., Setrouk, A., & Jacquot, L. (2025).
> "Re-evaluating Short- and Long-Term Trend Factors in CTA Replication: A Bayesian Graphical
> Approach."** arXiv:2507.15876. https://arxiv.org/abs/2507.15876
> **⚠️ Preprint, not peer-reviewed.**

24 liquid futures, 2010–2025, **including realistic transaction and roll costs.** Closed-form
Sharpe-optimal blend of short- and long-term trend signals:

```
omega*_ST = (mu_ST - rho*mu_LT) / ((mu_ST + mu_LT)(1 - rho))
Var(P)    = 1 + 2(rho - 1) * omega_ST * omega_LT      # variance reduction from blending
```

**Applied to data, the Sharpe-optimal blend is ~17% short-term / ~83% long-term — NOT
equal-weight.** (Short bucket {10,20,40,60}d; long = 500d.)

**Actionable constraint:** if you build the ensemble in #1, **do not equal-weight the horizons
blindly.** Weight toward slower horizons and *test* the weights walk-forward (§10) rather than
assuming 1/K. **Note the tension with §5.4c**, where crypto *short* lookbacks performed best —
different asset classes likely have different optimal weights, so validate on your own data.

### 5.5 ⚖️ The one peer-reviewed paper that is genuinely pro-long-side (weigh it honestly)

> **Corbet, S., Eraslan, V., Lucey, B., & Sensoy, A. (2019). "The effectiveness of technical
> trading rules in cryptocurrency markets."** *Finance Research Letters*, **31(C), 32–37.**
> DOI: [10.1016/j.frl.2019.04.027](https://doi.org/10.1016/j.frl.2019.04.027)
> https://ideas.repec.org/a/eee/finlet/v31y2019icp32-37.html

**Verified abstract (verbatim):**
> "We analyse various technical trading rules in the form of the moving average-oscillator and
> trading range break-out strategies to specifically test resistance and support levels and their
> trading performance using high-frequency Bitcoin returns. Overall, our results provide
> **significant support for the moving average strategies.** In particular, variable-length moving
> average rule performs the best with **buy signals generating higher returns than sell signals.**"

**Why this is worth naming:** it is one of the very few papers whose positive result lands on the
**long side** — i.e. on the side you can actually trade. That is directly relevant to a long-only
platform and is a genuine counterweight to the negative findings.

**Why I do not treat it as decisive:**
- It is a **short letter** (6 pages) — limited scope for robustness testing.
- It does **not** appear to apply a formal data-snooping / FDR correction.
- It is **contradicted out-of-sample for Bitcoin** by Hudson & Urquhart (2021), who tested ~15,000
  rules and found *"no predictability for Bitcoin in the out-of-sample period."*
- *"Buy signals beat sell signals"* on a **rising asset** in a **bull-heavy sample** is close to
  tautological: on any asset that went up a lot, long signals beat short signals. This is the
  drift-term confound (§7.0, Christoffersen-Diebold result 1) appearing again.

**Calibrated reading:** this is weak-to-moderate positive evidence for moving-average rules on
crypto, and it is **not sufficient on its own** to justify a fast crossover system — especially
given the out-of-sample BTC refutation. It *is* sufficient to say the trend idea is not
universally refuted, which supports (but does not establish) the #1 recommendation.

### 5.6 Verdict on trend following / ensembles

- **Evidence: the strongest in this review**, especially at century scale.
- **Best structure: multi-horizon equal-weighted ensemble**, because it attenuates the parameter-
  selection problem that Bajgrowicz & Scaillet show is fatal.
- **Cost constraint is the binding one:** the ML result (edge gone above 2–3 bps) implies your
  strategy must be **low-turnover** — favour **1d** and slow lookbacks, and treat 1h with
  suspicion.
- **Failure mode:** choppy/range-bound markets. A trend ensemble in a sideways market is a
  fee-burning machine. Crypto spends a large fraction of time ranging.
- **Caveat:** post-2009 decay is reported by multiple sources.

---

## 6. Seasonality / Calendar Effects

**Verdict: DO NOT BUILD. The evidence is actively refuted, and the effect sizes are below your
fee floor.** This is the clearest negative finding in the review.

### 6.1 The refutation

> **Mueller, L. (2024). "Revisiting seasonality in cryptocurrencies."**
> *Finance Research Letters*, **64(C).** DOI: [10.1016/j.frl.2024.105429](https://doi.org/10.1016/j.frl.2024.105429)
> https://ideas.repec.org/a/eee/finlet/v64y2024ics1544612324004598.html

**Findings (verbatim from abstract):**
> "The evidence on seasonality in cryptocurrency returns **is not robust**. Although the positive
> Monday effect for Bitcoin is internally valid, **it does not persist in data after 2015.** We do
> not find robust evidence of return abnormalities but of **lower trading activity on weekends**.
> This finding is robust across 500 different coins. We also find that the Monday effect in the
> cross-section of coins is typically **negative**, but the confidence intervals remain wide."

**Key points:**
- The BTC Monday effect **is positive** (not negative — the "negative Monday" claim in the brief
  is contradicted for BTC) and **disappears after 2015**.
- The *only* robust finding is **lower weekend volume** — which is a *volume* fact, **not a
  directional return edge**. You cannot trade "volume is lower" on a long-only spot bot.
- The cross-sectional Monday sign is *opposite* to BTC's — i.e. the effect is **unstable across
  assets**, the classic signature of a data-mined artefact.

### 6.2 Independent confirmation of the non-persistence

> **Baur, D. G., Cahill, D., Godfrey, K., & Liu, Z. (2019). "Bitcoin time-of-day, day-of-week and
> month-of-year effects in returns and trading volume."**
> *Finance Research Letters*, **31(C)**, 78–92. DOI: [10.1016/j.frl.2019.04.023](https://doi.org/10.1016/j.frl.2019.04.023)
> https://ideas.repec.org/a/eee/finlet/v31y2019icp78-92.html

15 million+ observations, **seven** global continuously-traded BTC exchanges. Verbatim:
> "we find time-specific anomalies in returns **BUT NO PERSISTENT EFFECTS ACROSS TIME**. In
> contrast, we find **persistent differences in trading activity** across all exchanges with lower
> activity during local evening hours and on weekends."

**Two independent papers, 500 coins and 7 exchanges, agree: return anomalies do not persist; only
the volume/activity pattern does.** That is about as clean a refutation as this literature offers.

### 6.3 The "exploitable" claim, and why it fails

> **Caporale, G. M., & Plastun, A. (2019). "The day of the week effect in the cryptocurrency
> market."** *Finance Research Letters*, **31(C).** DOI: [10.1016/j.frl.2018.11.012](https://doi.org/10.1016/j.frl.2018.11.012)
> Open access: https://bura.brunel.ac.uk/handle/2438/17208

Verbatim: *"Most crypto currencies (LiteCoin, Ripple, Dash) are found not to exhibit this anomaly.
The only exception is BitCoin, for which returns on Mondays are significantly HIGHER than those on
the other days... the trading simulation analysis shows that there exist exploitable profit
opportunities; **however, most of these results are not significantly different from the random
ones** and therefore cannot be seen as conclusive evidence against market efficiency."*

**Note the self-refutation:** the same paper that claims "exploitable profit opportunities"
immediately concedes the results are **not distinguishable from random**. Combined with Mueller
(2024) showing the effect dies after 2015, this is a dead end.

### 6.4 Magnitude vs fees — the decisive arithmetic

Even *taking the pro-seasonality papers at face value*, the effect magnitudes reported are on the
order of **a few basis points to low tens of basis points per event**. To trade a day-of-week
effect you must:

1. Enter before the favourable window and exit after → **2 round trips per week minimum.**
2. At 30 bps/round trip → **60 bps/week ≈ 31% per year in fees alone.**
3. The documented effect is far smaller than that.

**The fee arithmetic alone makes calendar trading on this platform unviable**, independently of
whether the anomaly is real.

### Verdict
**DO NOT BUILD.** Weak, non-persistent, sign-unstable across assets, and — decisively —
**an order of magnitude smaller than the round-trip cost.** I want to be blunt here because
seasonality is where retail crypto systems most often overfit: the effect is easy to "find" in
backtest and structurally incapable of paying for itself.

---

## 7. Breakout with Volatility Contraction ("Squeeze")

**Verdict: volatility *clustering* is real and robust; volatility contraction predicting
*direction* is not supported. Test only at 1d with low expectations.**

### 7.0 🛑 THE FORMAL KILL — volatility predicts MAGNITUDE, not DIRECTION

> **Christoffersen, P. F., & Diebold, F. X. (2006). "Financial Asset Returns, Direction-of-Change
> Forecasting, and Volatility Dynamics."** *Management Science*, **52(8), 1273–1287.**
> DOI: [10.1287/mnsc.1060.0520](https://doi.org/10.1287/mnsc.1060.0520)
> NBER WP 10009: https://www.nber.org/papers/w10009
> https://ideas.repec.org/a/inm/ormnsc/v52y2006i8p1273-1287.html

This is the theoretical result that settles the squeeze question. Verified findings:
1. **"volatility dependence produces sign dependence, so long as expected returns are nonzero"** —
   i.e. volatility dynamics can generate *direction* predictability **only by interacting with a
   drift term.** The squeeze itself contributes nothing directional.
2. **"it is statistically possible to have sign dependence without conditional mean dependence."**
3. **"sign dependence is not likely to be found via analysis of sign autocorrelations, runs tests,
   or traditional market timing tests."**
4. **"sign dependence is NOT LIKELY TO BE FOUND IN VERY HIGH-FREQUENCY (e.g., daily) or very
   low-frequency (e.g., annual) returns; instead, it is more likely to be found at INTERMEDIATE
   return horizons."**

**Why this is decisive for your platform:**
- Volatility contraction → **bigger move**: robustly supported.
- Volatility contraction → **direction**: **no theoretical support at your timeframe.** Result (4)
  explicitly rules out the daily frequency, and your timeframes are 1h/4h/1d.
- Therefore **any long-only squeeze breakout system is really betting on the crypto drift term,
  not on the squeeze.** If crypto has positive drift over your sample, a "squeeze breakout" long
  will look profitable in backtest — but that profit is **beta, not signal**, and it will vanish
  (or invert) in a bear regime.

**This is exactly the trap that makes squeeze backtests look good and live trading fail.** The
correct conclusion is not "squeeze breakouts don't work"; it is *"the squeeze is not doing the work
you think it is"* — and per Result (1), the residual directional edge is proportional to the
expected return, which you cannot forecast from OHLCV.

#### 🔬 The mechanism, stated precisely — and why a squeeze is the *wrong* signal

Two further points from the same paper sharpen the kill-shot considerably:

**(a) The sign-forecast strength is driven by VOLATILITY-OF-VOLATILITY, not by the volatility
level.** Verbatim: *"because the optimal probability forecast is driven entirely by the volatility,
we have that..., is therefore **driven by the volatility of volatility**."*

**This is the decisive technical objection to the squeeze thesis.** A "squeeze" measures a low
volatility **level** — which is *not the same quantity* as low volatility-of-volatility. The theory
says the strength of any volatility-derived directional forecast depends on **vol-of-vol**. So a
squeeze is, at best, **measuring the wrong variable for the mechanism it claims to exploit.** You
could be in a deep squeeze and still have high vol-of-vol, in which case the theory predicts nothing
useful for direction.

**(b) Result (4) in plainer form** (same paper): *"one does not expect strong sign forecastability
for very high frequency returns such as daily, because expected daily returns are negligible."*
The edge scales with the expected return — and crypto's *daily* expected return is tiny relative to
its volatility. **This is why the daily-frequency squeeze signal is theoretically weak regardless
of how good the backtest looks.**

**(c) A related asymmetry worth knowing:** the well-known "leverage effect" runs
**direction → volatility**, *not* volatility → direction. It explains why vol spikes after a
*fall*, and is therefore a *consequence* of price moves, not a predictor of them. Citing the
leverage effect as support for a squeeze breakout inverts the causality.

**Net effect on the verdict below: this makes the squeeze idea weaker, not stronger.** The one
peer-reviewed supporter (§7.2b) still justifies *testing* it — but the theory now says you are
measuring the wrong variable for the claimed mechanism, which is a strong prior against it working
out of sample.

### 7.1 What is actually established

The **robust** fact is **volatility clustering** — high-vol periods follow high-vol periods, low
follow low. This is a foundational stylised fact, and it is directly relevant: **it is the same
mechanism that makes volatility targeting work (§2.3).** GARCH exists because this clustering is
one of the most reliable regularities in finance.

The **TTM Squeeze** (John Carter, *Mastering the Trade*) is a **practitioner indicator, not
peer-reviewed research.** It fires when Bollinger Bands contract inside Keltner Channels,
signalling low realised volatility. **I could not find any peer-reviewed academic validation of
the TTM Squeeze specifically.** Treat its claimed edge as unverified marketing until you test it
yourself.

**The decisive conceptual problem:** volatility contraction tells you a **larger move is
plausible** — it says nothing about **direction**. If you are long-only and the squeeze resolves
downward, you take the full loss. A squeeze strategy is fundamentally a **long-volatility**
position, and a long-only spot bot **cannot express long-volatility symmetrically** — you can only
be long or flat, so you eat the downside of every false breakout while capturing only part of the
upside.

This is confirmed by the vol-management literature from the other direction: Wang & Yan (2021,
§2.3) find that **lagged volatility is "largely unrelated to future returns"** — i.e. **volatility
predicts volatility, not returns.** That is a direct, peer-reviewed statement against the core
premise that volatility contraction predicts a *directional* breakout.

### 7.2 What this means for implementation

The **only** way volatility contraction is useful to you is as a **timing/filter overlay on a
directional signal that has its own justification** — never as a standalone entry. E.g.:
"when BB width is in its lowest decile, *and* the trend ensemble is long, take a larger position."
That uses contraction for its real content (bigger move coming → scale size up) while getting
direction from a separately-justified signal.

**Also note:** this is *equivalent in spirit* to vol targeting inverted — instead of sizing
*down* when vol is low (which §2.4(d) does), you size *up*. Both are legitimate; you must pick one
so they don't cancel.

### 7.2b ⚖️ The one peer-reviewed *supporter* of contraction-expansion (weigh it honestly)

> **Holmberg, U., Lönnbark, C., & Lundström, C. (2013). "Assessing the profitability of intraday
> opening range breakout strategies."** *Finance Research Letters*, **10(1), 27–33.**
> DOI: [10.1016/j.frl.2012.09.001](https://doi.org/10.1016/j.frl.2012.09.001)
> Working paper: Umeå Economic Studies No. 845 · https://ideas.repec.org/a/eee/finlet/v10y2013i1p27-33.html
> (Both fetched HTTP 200; 9 citations)

**This is materially better than I expected to find.** It is a real Elsevier journal, it is
peer-reviewed, and — notably — its **official keyword list literally includes
"Contraction–Expansion principle."** The paper does what the squeeze idea claims to do.

**Verified abstract (verbatim):**
> "we test the success rate of trades and profitability of the Open Range Breakout (ORB) strategy.
> An investor that trades on the ORB strategy seeks to **identify large intraday price movements**
> and trades only when the price moves beyond some predetermined threshold. We present an ORB
> strategy based on normally distributed returns to identify such days and find that our ORB
> trading strategy result in **significantly higher returns than zero** as well as an increased
> success rate in relation to a fair game. The characteristics of such an approach over
> conventional statistical tests is that it involves the **joint distribution of Low, High, Open
> and Close** over a given time horizon."

**Why this is directly relevant:** it uses **OHLC candle data** and a **volatility-threshold
filter** — structurally the same ingredients you have. It is the closest peer-reviewed analogue to
a squeeze breakout that exists.

**⚠️ Why I still do not treat it as establishing a tradeable edge — five serious limits:**
1. **One market: crude oil futures.** Not crypto, not equities. No basis to import the result.
2. **Intraday, single era (2013).** No out-of-sample extension, no post-publication replication
   reported.
3. **No transaction costs and no data-snooping correction in the abstract.** For an *intraday*
   breakout strategy this is the decisive omission — and Bajgrowicz & Scaillet (2012) is the
   standing demonstration that TA results die under costs + FDR. Intraday ORB trades often; fees
   bite hard.
4. It is a **magnitude/threshold claim as much as a directional one** — it "seeks to identify
   large intraday price movements", exactly consistent with Christoffersen & Diebold (§7.0).
5. **Only 9 citations in ~13 years.** Not heavily replicated.

**Verdict on this paper:** the strongest available support for a contraction-expansion breakout,
**and still weak.** It justifies *testing* a squeeze breakout rather than dismissing it. It does
**not** establish that such a system works, and nothing in it supports importing the result into
crypto.

### 7.2c ❌ A peer-reviewed test of the *closest* logic FAILS and decays

> **Fang, J., Jacobsen, B., & Qin, Y.-F. (2017). "Popularity versus Profitability: Evidence from
> Bollinger Bands."** *The Journal of Portfolio Management*, **43(4), 152–159.**
> DOI: [10.3905/jpm.2017.43.4.152](https://doi.org/10.3905/jpm.2017.43.4.152)
> Landing page: https://www.pm-research.com/content/iijpormgmt/43/4/152

Tests **Bollinger's own original breakout/trend-continuation claim** — that price closing beyond
the 2-SD band continues to outperform — and finds it **fails**: returns are *"mostly negative...
and such losses have worsened over time."*

**Why this matters a lot:** Bollinger Bands are the **outer layer of Carter's TTM Squeeze** (the
squeeze is BB contracting inside Keltner Channels, and the breakout is a BB-band break). A
peer-reviewed test of BB breakout-continuation **failing, with losses that "worsened over time"**,
is directly adverse to squeeze logic. And *"worsened over time"* is the classic **post-publication
decay** signature (see McLean & Pontiff, 2016, *Journal of Finance* 71(1), 5–32).

**⚠️ Verification caveat, stated plainly:** I confirmed **existence, authors, venue and DOI**, but
**could NOT retrieve the paper's own abstract** (pm-research redirects to an institutional login;
SSRN returned 403). The finding quoted above comes from a **third-party practitioner review**.
Treat the exact wording as **secondary-sourced and the magnitudes as UNVERIFIED.**

### 7.3 On Carter's TTM Squeeze specifically

**Confirmed practitioner material, NOT peer-reviewed.** The authoritative source is John Carter's
book *Mastering the Trade* (McGraw-Hill). A widely-circulated Barchart-hosted PDF
("The Squeeze by John Carter", `https://www.barchart.com/media/education/pdf/The%20Squeeze%20by%20John%20Carter.pdf`)
**could not be content-verified** — the fetch returned HTTP 202 with no body. Only the *existence*
of Carter's indicator is established; I did not verify that document's contents.

**I found NO peer-reviewed, data-snooping-corrected backtest of the TTM Squeeze or of a
Bollinger-band squeeze.** The "evidence" in circulation is vendor content, TradingView Pine
scripts, and SEO articles — **that is marketing, not evidence.** The nearest peer-reviewed tests
are the two in §7.2b and §7.2c (one weak supporter using ORB on oil futures; one direct opponent
testing Bollinger breakout-continuation, which failed and decayed).

### 7.3b Related literature found but not usable here

Search surfaced **Markov regime-switching and HMM** work for crypto/Bitcoin (e.g. Zenodo
"Cryptocurrency Market State Modeling via Spatial Clustering and Hidden Markov Formulations",
and Springer "Regime-Aware Adaptive Forecasting Framework for Bitcoin Prices"). These are mostly
**recent, low-citation, and non-peer-reviewed deposits**, and HMMs are **prone to look-ahead bias
and regime-label instability out-of-sample** (the fitted states change as new data arrives). I
would **not** recommend an HMM regime filter for a freqtrade bot — the parameters are not stable
enough to freeze and ship.

### 7.4 Verdict

**TEST at 1d, low expectation, as an overlay only — not a standalone entry.**
- **The balance of peer-reviewed evidence is net-negative but not empty.** One weak supporter
  (Holmberg et al. 2013: oil futures, intraday, no costs, 9 citations) against one direct
  opponent (Fang et al. 2017: Bollinger breakout-continuation fails *and decays over time*,
  though magnitudes unverified), plus Christoffersen & Diebold (2006) ruling out daily-frequency
  sign dependence from volatility altogether.
- **"Magnitude expansion after contraction" is solid; "directional breakout edge" is unproven and
  has failed in the closest peer-reviewed test.** Testing it is justified; assuming it works is not.
- The TTM Squeeze specifically has **no peer-reviewed validation**.
- **Any backtest profit from a long-only squeeze breakout should be assumed to be crypto beta
  until proven otherwise** — test it against buy-and-hold, and test it in 2018 and 2022
  sub-samples specifically. If it only "works" in bull markets, it is drift, not signal.

---

## 8. Overnight / Close-to-Open and Funding-Rate Effects

**Verdict: OUT OF SCOPE — and I want to be precise about why, rather than dismissive.**

- **Funding rates are only obtainable from the perpetual futures market.** They are not in
  OHLCV candles and would require an external data feed. Your brief explicitly excludes external
  data feeds. **Not implementable.**
- **Crypto trades 24/7 on spot**, so the classic equity "overnight vs intraday" decomposition
  (which exists because equities have a *close* and a *gap*) is **not well-defined** for a spot
  crypto pair. There is no overnight gap to harvest. The nearest analogue would be a "time-of-day"
  effect, which §6 shows is non-persistent (Baur et al. found the activity pattern persists but
  return anomalies do not).
- If you ever add perpetual futures and a funding-rate feed, the carry/funding basis trade
  becomes interesting — but that is a **different platform** with a **short leg**, and it is
  explicitly outside the current scope.

**No further research recommended here.** Nothing in scope.

---

## 9. Ranked Recommendations

### Ranking method
`score = (robustness of evidence) × (implementability under constraints) × (probability of
surviving 30 bps round trip)`.

---

### 🥇 #1 — Multi-horizon trend ensemble with volatility-scaled sizing

**Why #1:** This is the only idea in the review that combines (a) the longest and most replicated
evidence base (Hurst et al., 1880–2016, positive in every decade), (b) a *structural* reason to be
robust (ensembling removes the parameter-selection problem that Bajgrowicz & Scaillet identify as
the primary killer of TA rules), and (c) genuine implementability under your exact constraints
(talib only, OHLCV only, long-only, works at 4h/1d).

**And it now has direct crypto-specific confirmation.** Han, Kang & Ryu (2024) — the most rigorous
crypto momentum study available — find that a **long-only** time-series momentum rule (buy when
the 28-day return is in the top tercile of its own history, else hold cash) delivered **Sharpe 1.51
vs 0.84 for buy-and-hold, with a LOWER max drawdown (61.8% vs 89.1%)**, after 15 bps costs, over
2013–2023 with no survivorship bias. Their own explanation is the one that matters: *"The strategy
holds a long position only when the market is bullish and defends well against market downturns."*
They also find the **short leg loses money** and that momentum concentrates in **large** coins —
so **long-only is not a limitation here, it is the correct implementation.**

**Be honest about what it is:** a **risk-managed beta-capture overlay**, not an alpha generator.
Its job is to keep you out of the −80%/-90% crypto bear markets, not to beat buy-and-hold in every
bull run. It will underperform buy-and-hold in strong uptrends where it whipsaws. Note that even in
Han et al.'s best configuration the max drawdown is still 61.8%.

```python
# ---- Multi-horizon trend ensemble, long-only spot ----
# Timeframe: 1d (preferred) or 4h.  NOT 1h.

# 1. Signal from K horizons, each a simple "price above its average"
HORIZONS = [20, 50, 100, 200]        # ~1m, ~2.5m, ~5m, ~10m on daily bars
votes = 0
for h in HORIZONS:
    sma_h = SMA(close, h)
    votes += 1 if close > sma_h else 0
trend_score = votes / len(HORIZONS)   # in [0, 1]

# 2. Volatility scaling (uses ATR -> annualised vol; see 2.4)
sigma = ATR(high, low, close, 20) / close * sqrt(365)   # daily bars
raw_exposure = clip(TARGET_VOL / sigma, 0.0, 1.0)       # TARGET_VOL ~ median sigma

# 3. Combine: fractional exposure = conviction * risk budget
target_exposure = trend_score * raw_exposure

# 4. Hysteresis -- only trade on a meaningful change (fee discipline)
if abs(target_exposure - current_exposure) > 0.15:
    rebalance_to(target_exposure)

# 5. Entry/exit interpretation for freqtrade
enter_long  when trend_score >= 0.75          # e.g. 3 of 4 horizons bullish
exit_long   when trend_score <= 0.25          # e.g. 1 of 4
# Never fully exit on a single-horizon flip (that is the whipsaw trap).
```

**A simpler, crypto-validated variant worth testing as a baseline** (directly from Han et al.
2024's best cell — note it is on *daily* bars, so translate carefully to your timeframe):

```python
# Tercile time-series momentum, long-only
lookback = 28  # days (28 daily bars)
holding = 5  # days
entry_threshold = 0.50  # top 50% of historical lookback returns
# (they found the optimal trade-off between
#  profitability and opportunity at 30-50%,
#  while top-10% maximised per-trade Sharpe)

hist = rolling_rank_percentile(close / close.shift(lookback) - 1, window=365 * 3)
if hist >= (1 - entry_threshold):
    enter_long()  # hold ~48% of the time
else:
    exit_long()  # go to cash
# Rebalance every `holding` bars. TEST rebalancing-day sensitivity:
#   their same rule gave Sharpe 1.40 (Mon) vs 1.09 (Sun) -- do NOT pick the best day.
```

**Expected behaviour & failure modes:**
- *Works in:* sustained trends, both up and (by keeping you flat) down.
- *Fails in:* choppy/sideways markets — it will flip and burn fees. This is the main risk.
- *Bear-market caveat:* Han et al. find the coefficient is **always insignificant** in a "bad"
  market state — i.e. the momentum *signal* is weak in bear markets; what you get there is
  **avoidance**, not profit. Do not expect bear-market alpha, expect bear-market protection.
- *Fees:* at **1d**, rebalances are rare → survives easily. At **4h**, acceptable if hysteresis
  is enforced. At **1h**, the flip rate rises and fees will likely eat it.
- *Evidence against:* Huang et al. (2020) — the *predictive* claim is weak; do not expect alpha.
  Post-2009 decay is reported by multiple sources.
- *Validation requirement:* per Han et al., **report mean LOG return / CAGR and its t-stat, not
  just the arithmetic mean t-stat**, and **test rebalancing-day sensitivity**.

---

### 🥈 #2 — Volatility targeting as a risk-control sizing layer

**Why #2:** The mechanism (volatility clustering) is the **least disputed fact** in this entire
review, and it works across all asset classes for **tail reduction** (Harvey et al.). It requires
**zero predictive ability**, so it is immune to the replication problems that killed most other
ideas. It is also trivially implementable from ATR.

**Critical constraint:** implement it as a **de-risking overlay only**. On unlevered spot you
cannot scale *up*, so vol targeting means going partially to cash when vol spikes. Do **not**
expect a Sharpe uplift — Harvey et al. say Sharpe gains apply to risk assets only, and Cederburg
et al. show out-of-sample Sharpe gains generally fail. **Expect fewer catastrophic drawdowns.**

```python
sigma_ann = ATR(high, low, close, 20) / close * sqrt(PERIODS_PER_YEAR)
exposure = clip(TARGET_VOL / sigma_ann, 0.0, 1.0)

# Wang & Yan (2021) refinement: downside semi-vol has a genuine return-timing component
logret = ln(close / close.shift(1))
neg = min(logret, 0)
sigma_dn = sqrt(rolling_mean(neg**2, 20)) * sqrt(PERIODS_PER_YEAR)
# Prefer sigma_dn for the *return* component; use total sigma for the *risk* component.

exposure_smoothed = EWMA(exposure, span=10)  # avoid per-candle churn
if abs(exposure_smoothed - current) > 0.10:
    rebalance_to(exposure_smoothed)
```
**Fees:** neutral if smoothed/thresholded, **negative if applied raw at 1h** (re-scales every bar).

---

### 🥉 #3 — Volatility-contraction breakout as a *filter* (not a standalone entry)

**Why #3 and not higher:** the honest evidence is that volatility contraction predicts **magnitude
but not direction** — and Wang & Yan's verified decomposition states directly that lagged
volatility is *"largely unrelated to future returns."* A long-only bot cannot express
long-volatility symmetrically, so it eats every false downside break. Academic backing for the
directional claim is **absent**; the TTM Squeeze is practitioner folklore.

**Where it earns its place:** as a **sizing boost conditioned on a trend signal** — when
contraction is extreme *and* the trend ensemble is already long, increase conviction. Never as a
bare "BB width at lows → buy".

```python
bb_width = (BB_upper(20, 2) - BB_lower(20, 2)) / BB_middle(20, 2)
width_pct = rolling_percentile_rank(bb_width, 200)  # 0..1
in_squeeze = width_pct < 0.20  # lowest quintile

# ONLY valid combined with direction from #1:
if trend_score >= 0.75 and in_squeeze:
    target_exposure = min(1.0, target_exposure * 1.5)  # scale up, don't initiate
```
**Test at 1d only.** Expect no standalone edge; measure whether it improves #1's Sharpe.

---

### Ideas I recommend AGAINST, and why

| Idea | Reason to reject |
|---|---|
| **Cross-sectional momentum** | Structurally impossible long-only/one-pair. The documented returns come from the short leg and the cross-section — neither of which you can access. |
| **Seasonality / calendar** | Actively refuted by two independent papers (Mueller 2024, 500 coins; Baur et al. 2019, 7 exchanges). Effect magnitude is an order of magnitude *below* your 30 bps round trip. |
| **Overnight / funding** | Requires external data and/or a short leg. Out of scope. |
| **Any high-turnover signal enhancement** | Lim et al. (2019): an ML trend model's advantage vanished above **2–3 bps** of cost. You pay **~15 bps/side**. |
| **Any dip-buying / mean reversion as a core** | Zaremba et al. (2021): reversal is an **illiquidity artefact**; the largest, most tradeable coins — *your universe* — exhibit daily **momentum instead**. Caporale & Plastun (2019): the contrarian trade loses money outright. |
| **Standalone RSI signals** | Zatwarnicki et al. (2023): naive RSI is *"high risk"* in crypto. Hudson & Urquhart (2021): **no out-of-sample predictability for Bitcoin** across 15,000 rules. |
| **Standalone squeeze breakout** | Christoffersen & Diebold (2006): sign-predictability is **not expected at daily frequency**; the squeeze predicts magnitude only. Any backtest profit is crypto drift/beta, not signal. |
| **HMM / regime-switching filters** | Look-ahead-prone, unstable out-of-sample, and the available crypto papers are recent, low-citation, non-peer-reviewed. |

---

## 10. Methodological discipline — read before building anything

The most important lesson from this literature is not *which* strategy to pick. It is that
**the primary reason these strategies fail is not a bad signal — it is the selection process.**

Bajgrowicz & Scaillet (2012): *"an investor would never have been able to select ex ante the
future best-performing rules."* Your 10 classic strategies were presumably selected for
sounding reasonable, and per Park & Irwin (2007), most TA studies suffer from *"data snooping, ex
post selection of trading rules or search technologies, and difficulties in estimation of risk and
transaction costs."*

**Concrete requirements for anything you test next:**

1. **Walk-forward, not a single train/test split.** Re-fit and re-evaluate on a rolling basis.
2. **Report the number of variants you tried.** If you tested 50 parameter sets, the best one's
   t-statistic is meaningless. This is the FDR problem.
3. **Always benchmark against buy-and-hold** on the same pair and period. Per Huang et al., a
   trend strategy's performance may be *"virtually the same"* as a naive alternative — so this is
   the single most informative comparison you can run, and it is free.
4. **Charge full realistic costs**: 0.1% fee + 0.05% slippage per side (your `research/backtest.py`
   already does this correctly — keep it).
5. **Report turnover alongside Sharpe.** A strategy with a great Sharpe and 50 round trips/year
   needs >15% annual alpha just to break even on fees.
6. **Test on the timeframe you will actually run.** Don't validate at 1h and deploy at 1d.
7. **Beware the survivorship bias in your data.** Your `research/data/` holds 9 *currently listed*
   pairs. The coins that went to zero are absent. This **systematically flatters every long-only
   strategy you test.** LTW specifically controlled for this by including defunct coins; you cannot.
   **At minimum, treat every long-only backtest result as an upper bound.**
8. **Adopt a hold-ratio stability diagnostic** (Zatwarnicki et al., 2025, RSHR) — check that the
   strategy's *behaviour* is stable across sub-periods, not just that aggregate returns are
   positive.

---

## 11. Evidence-quality ledger (calibration)

| Claim | Strength | Basis |
|---|---|---|
| Volatility clustering is real and persistent | **Very strong** | Foundational; corroborated by Wang & Yan decomposition |
| Trend following has positive returns over 1880–2016 | **Strong** | Hurst et al. (2017), JPM — but AQR-authored, multi-asset, long-short |
| Vol targeting reduces tail risk across asset classes | **Strong** | Harvey et al. (2018) — verified abstract |
| Vol targeting improves Sharpe | **Weak/conditional** | Risk assets only (Harvey et al.); breaks in their own 1957–87 subsample; out-of-sample failure (Cederburg et al. 2020, 72/103); **negative net-of-cost Sharpe for 5/9 factors (DeMiguel et al. 2024)** |
| Vol targeting in **crypto** specifically | **NO PUBLISHED EVIDENCE** | Searched specifically; none found. Genuine open question |
| Multi-horizon ensembles beat single lookbacks | **Moderate — but NOT on Sharpe** | Zarattini et al. (2025): 9-horizon combo Sharpe 1.58 vs single 5d **1.66**; combo wins only on Sortino (2.03) and MDD (19%) |
| **Crypto multi-horizon Donchian + vol sizing** | **Strong (working paper)** | **Zarattini/Pagani/Barbon (2025)**: net-of-fees Sharpe 1.57, MDD 11%, alpha 11% vs BTC — **but on a top-20 portfolio, not one pair** |
| **Long-only crypto TSMOM beats buy-and-hold on Sharpe AND drawdown** | **Strong (best peer-reviewed crypto evidence)** | **Han/Kang/Ryu (2024)** — Sharpe 1.51 vs 0.84, MDD 61.8% vs 89.1%, after 15 bps costs, 2013–2023, no survivorship bias. Caveats: accepted-not-yet-published; self-admitted look-ahead bias; single 10yr sample |
| **The short leg of crypto momentum LOSES money** | **Strong** | Han/Kang/Ryu (2024) verbatim; corroborated by Fieberg et al. (2024) |
| **Arithmetic mean t-stat ≥ 2 is NOT sufficient for profit** | **Strong** | Han/Kang/Ryu (2024): 10 portfolios t>2 on mean return, only 3 on mean LOG return |
| TSMOM predicts returns asset-by-asset | **Weak / replication-failed** | Huang et al. (2020), JFE |
| Crypto momentum (3-week) exists | **Weak–moderate, disputed** | LTW (2022) t≈2.0–2.7, 2014–2018 vs Grobys & Sapkota (2019) null, same sample |
| Crypto TA rules are profitable | **Weak / fails OOS on BTC** | Hudson & Urquhart (2021); Bajgrowicz & Scaillet (2012) |
| Crypto TA rules — counterpoint (long side) | **Weak–moderate, pro-long** | Corbet et al. (2019) FRL, 31(C), 32–37 — but contradicted OOS for BTC |
| Crypto short-term reversal exists | **Strong** — 3,600+ coins | Zaremba et al. (2021) |
| Crypto short-term reversal works on **liquid** coins | **REFUTED — it inverts to momentum** | Zaremba et al. (2021), verbatim |
| Crypto overreaction is tradeable | **Refuted** | Caporale & Plastun (2019) — contrarian loses, momentum ≈ random |
| Volatility predicts **magnitude** | **Strong** | Christoffersen & Diebold (2006) |
| Volatility predicts **direction** | **Not supported at daily freq** | Christoffersen & Diebold (2006), result (4) |
| **A "squeeze" measures the right variable for direction** | **NO — measures vol LEVEL, but the mechanism needs vol-OF-VOL** | Christoffersen & Diebold (2006), verbatim |
| Volatility predicts returns (counterweight) | **Real but unusable here** | Bollerslev/Tauchen/Zhou (2009) RFS 22(11) — month/quarter horizon, needs OPTION-IMPLIED variance, not computable from spot OHLCV |
| Crypto momentum has severe crash risk | **Moderate–strong** | Grobys et al. (2025), FMPM 39(4), 443–476 |
| Crypto seasonality is tradeable | **Refuted** | Mueller (2024); Baur et al. (2019) |
| Intraday crypto reversal exists, conditioned on jumps | **Moderate** | Wen et al. (2022) — but intraday, not 1h/4h/1d |

**Sources I could NOT verify and have therefore NOT relied on** (recorded for transparency):
- Kim, Tse & Wald (2016) abstract text — citation verified via OpenAlex (JFM 30, 103–124, 92
  citations); abstract body not retrievable. Their argument is stated as such, not quoted.
- Cederburg et al. (2020) abstract — repository returned 403; their finding is quoted **verbatim
  from Wang & Yan (2021)**, a traceable secondary source, rather than asserted from memory.
- LTW (2022) 1-week reversal magnitude — **could not be verified; do not cite.**
- Fang, Jacobsen & Qin (2017) — existence/authors/venue/DOI confirmed, but the *finding* is
  second-hand from a practitioner review; magnitudes unverified.
- Svogun & Bazán-Palomino (2022), "Technical analysis in cryptocurrency markets: Do transaction
  costs and bubbles matter?", *Journal of International Financial Markets, Institutions & Money*
  79, 101601, DOI 10.1016/j.intfin.2022.101601 — **bibliographically verified but NO abstract or
  full text retrievable** (closed access). **Highest-priority paper to pull via institutional
  access** — it is exactly on-point (TA + transaction costs + crypto).
- Kitron & Wengrowicz (2026) — **preprint, not peer-reviewed**; used for its measured edge/cost
  numbers with that caveat stated inline.
- Retail-cost range (0.53%–6.45%) — industry-sponsored, **not peer-reviewed**; flagged inline.

**Verified in full from primary sources during this review** (i.e. read, not just cited):
Moskowitz/Ooi/Pedersen (2012) PDF; Liu/Tsyvinski/Wu NBER WP 25882; Wang & Yan (2021) PDF;
Han/Kang/Ryu (2024) 116pp full text; plus verbatim abstracts for ~20 further papers via
RePEc/OpenAlex/Semantic Scholar/journal pages.

---

## 12. Final recommendation

**Build #1 and #2 as a single combined system:** a slow, multi-horizon trend ensemble on **1d**
(preferred) or **4h**, sized by a thresholded volatility-target overlay, benchmarked obsessively
against buy-and-hold.

**Do not expect alpha.** Expect a **materially smaller maximum drawdown** than buy-and-hold in
exchange for **modest underperformance in strong bull markets**. On a long-only spot crypto book,
given that crypto's left tail is the dominant risk, **that is a legitimate and honest objective** —
and it is the only objective in this review that the literature actually supports.

**The single most valuable thing you can do next is not to add an 11th strategy.** It is to
re-run your existing 10 with (a) a buy-and-hold benchmark, (b) turnover reported, and (c) a
walk-forward split — because per Bajgrowicz & Scaillet, the reason they lose may be the selection
process rather than the signals, and adding more signals to a broken selection process will simply
produce a faster way to pay fees.

**Three concrete hypotheses worth testing, in priority order, all of which the literature
supports as *plausible* and none of which it supports as *proven*:**

1. **Does a 4-horizon SMA ensemble on 1d beat a single SMA(200) filter on drawdown-adjusted
   terms?** (Robustness of the *method*, per §5.2.) This is a low-risk, high-information test —
   it costs nothing but a backtest and directly tests whether ensembling solves your
   parameter-selection problem.
2. **Does ATR-based vol scaling reduce max drawdown without reducing CAGR by more than it saves?**
   (§2.) Frame the success metric as **max drawdown and Calmar**, *not* Sharpe — the literature
   says Sharpe is the wrong expectation.
3. **Does the volatility-contraction overlay add anything to (1)?** (§7.) Test last, expect
   nothing, and if it "works", check whether it works in the 2018 and 2022 sub-samples
   specifically — if it only works in bull markets, it is beta, not signal.

**Do not build an 11th entry signal.** The evidence in this review says the entry signal is not
where your edge is — and quite possibly not where an edge exists at all on this cost model and
this asset universe. The defensible edge available to a long-only spot crypto platform is
**risk management**, and that is a genuinely worthwhile thing to be good at.
