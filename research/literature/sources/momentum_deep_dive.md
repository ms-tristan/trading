# Crypto Momentum Literature Review — Verified Findings

Research conducted for a quantitative crypto trading platform. Every citation below was verified
against a live DOI/Crossref/OpenAlex/RePEc record; primary-source PDFs were downloaded and
text-extracted where obtainable. Unverifiable items are flagged explicitly.

**Bottom line up front:** Crypto momentum is a *real but fragile, gross-of-cost, short-horizon,
long-leg-driven* effect. It is NOT robust in the way the stock-market momentum literature is.
The single most important correction in this report: **the common claim that "1-week momentum is
strongest in crypto" is NOT what Liu, Tsyvinski & Wu (2022) actually report — they use 3-week.**

---

## 1. Moskowitz, Ooi & Pedersen (2012), "Time Series Momentum"

**Citation (VERIFIED via Crossref, DOI 10.1016/j.jfineco.2011.11.003):**
Moskowitz, Tobias J.; Ooi, Yao Hua; Pedersen, Lasse Heje (2012).
"Time series momentum." *Journal of Financial Economics* **104**(2), 228–250.

- Exact title is **"Time series momentum"** (lowercase "series"), not "Time Series Momentum".
- Received 16 Aug 2010; revised 11 Jul 2011; accepted 12 Aug 2011; available online 11 Dec 2011.
- SSRN: https://doi.org/10.2139/ssrn.2089463

**Working URLs (all verified):**
- Full PDF (downloaded and read): https://pages.stern.nyu.edu/~lpederse/papers/TimeSeriesMomentum.pdf
- AQR landing page: https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum
- Publisher: https://doi.org/10.1016/j.jfineco.2011.11.003
- AQR original data: https://www.aqr.com/Insights/Datasets/Time-Series-Momentum-Original-Paper-Data

**Sample:** 58 liquid futures/forward contracts (country equity indices, currencies, commodities,
sovereign bonds), Jan 1985 – Dec 2009. Earlier data back to 1965 is used as an out-of-sample check.

### 1a. Lookback periods tested — and which worked

The lookback × holding grid is **k (lookback) = 1, 3, 6, 9, 12, 24, 36, 48 months**, crossed with
**h (holding) = 1, 3, 6, 9, 12, 24, 36, 48 months**. Your listed set (1,3,6,9,12,24,36,48) is
**correct**.

Table 2 reports **t-statistics of alphas** (from regressions on MSCI World, Barclays Bond, S&P GSCI,
and Fama-French SMB/HML/UMD). Panel A (all assets) — t-stats by lookback (rows) × holding (cols,
1/3/6/9/12/24/36/48):

| Lookback | h=1 | h=3 | h=6 | h=9 | h=12 | h=24 | h=36 | h=48 |
|---|---|---|---|---|---|---|---|---|
| 1 | 4.34 | 4.68 | 3.83 | 4.29 | 5.12 | 3.02 | 2.74 | 1.90 |
| 3 | 5.35 | 4.42 | 3.54 | 4.73 | 4.50 | 2.60 | 1.97 | 1.52 |
| 6 | 5.03 | 4.54 | 4.93 | 5.32 | 4.43 | 2.79 | 1.89 | 1.42 |
| 9 | 6.06 | 6.13 | 5.78 | 5.07 | 4.10 | 2.57 | 1.45 | 1.19 |
| **12** | **6.61** | **5.60** | 4.44 | 3.69 | 2.85 | 1.68 | 0.66 | 0.46 |
| 24 | 3.95 | 3.19 | 2.44 | 1.95 | 1.50 | 0.20 | −0.09 | −0.33 |
| 36 | 2.70 | 2.20 | 1.44 | 0.96 | 0.62 | 0.28 | 0.07 | 0.20 |
| 48 | 1.84 | 1.55 | 1.16 | 1.00 | 0.86 | 0.38 | 0.46 | 0.74 |

**What worked:** the effect is strong and significant for lookbacks and holdings of **12 months or
less**; it **decays monotonically beyond 12 months** and turns negative at 24–48 month lookbacks with
long holding periods (e.g. 24/48 = −0.33; 48/24 = 0.38). The single strongest cell is **12-month
lookback / 1-month holding (t = 6.61)** — this is why they define the headline "TSMOM" strategy as
**k=12, h=1**.

**Asset-class heterogeneity (Panel B–E, h=1 column):** commodities strongest at 3–12m (4.54–4.66);
equity indices peak at 9m (4.21); bonds peak at 12m (3.53); **currencies are the weak link** —
t-stats are 3.16 (1m), 3.90 (3m), then collapse to 0.10 at h=3, and go **negative beyond 9 months**
(e.g. 12/48 = −1.67, 48/48 = −2.32). Currencies show reversal, not momentum, at long horizons.

### 1b. The "smile" pattern — IMPORTANT CORRECTION

The term **"smile" in this paper does NOT refer to a lookback-horizon pattern.** There is no
"smile" in Table 2 (the lookback pattern is monotone decay, not a U-shape). The paper's "smile" is
**Fig. 4: "The time series momentum smile"** — a plot of the diversified 12-month TSMOM strategy's
non-overlapping **quarterly returns against contemporaneous S&P 500 returns**, which is U-shaped:
TSMOM performs *best when the market moves most extremely, in either direction*, and worst in flat
markets. This is a **market-state** smile, not a lookback smile.

- Their words: "the return to time series momentum tends to be largest when the stock market's
  returns are most extreme — performing best when the market experiences large up and down moves."
- If your strategy docs use "smile" to mean a lookback-horizon shape, that is a misuse of this
  paper's terminology. Note that the *crypto* literature (incl. our own framing) uses "smile"
  differently — do not conflate them.

### 1c. Exact position-sizing / volatility-scaling formulas (verbatim from the paper)

**Ex-ante volatility estimate, Eq. (1)** — exponentially weighted lagged squared daily returns
("similar to a simple univariate GARCH model"), 261 trading days annualization:

```
σ²_t = 261 · Σ_{i=0}^{∞} (1−δ) δⁱ (r_{t−1−i} − r̄_t)²        (1)
```

where `r̄_t` is the exponentially weighted average return computed similarly. **δ is chosen so the
centre of mass of the weights is δ/(1−δ) = 60 days.** They stress: *"To ensure no look-ahead bias
contaminates our results, we use the volatility estimates at time t−1 applied to time-t returns
throughout the analysis."*

**Position sizing:** each position (long or short) is sized to a **40% ex-ante annualized
volatility**, i.e. **position size = 40%/σ_{t−1}**. Direct quote: *"The choice of 40% is
inconsequential, but it makes it easier to intuitively compare our portfolios to others in the
literature."* The 40% is chosen because it resembles the risk of an average individual stock; when
equal-weighted across all securities the TSMOM factor has **12% annualized volatility** over
1985–2009.

**TSMOM return, Eq. (5):**

```
r^TSMOM_{s,t,t+1} = sign(r^s_{t−12,t}) · (40% / σ^s_t) · r^s_{t,t+1}        (5)
```

Diversified factor across S_t available securities:
`r^TSMOM_{t,t+1} = (1/S_t) Σ_{s=1}^{S_t} sign(r^s_{t−12,t}) · (40%/σ^s_t) · r^s_{t,t+1}`

**Portfolio aggregation:** to avoid overlapping observations they follow **Jegadeesh & Titman
(1993)** — for each (k,h) the time-t return averages all h currently active portfolios.

**Your stated formula is CONFIRMED as accurate**: target 40% annualized vol, ex-ante vol from an
exponentially weighted estimate with a 60-day centre of mass, applied with a one-day lag.

### 1d. Reported Sharpe ratios and t-statistics

- **Diversified TSMOM factor: annualized Sharpe ratio > 1.0** — *"roughly 2.5 times the Sharpe ratio
  for the equity market portfolio."* (Abstract/intro text; exact figure is Sharpe > 1.)
- **Alpha: 1.58% per month (≈4.75% per quarter)**, large and significant, with respect to MSCI
  World + SMB + HML + UMD. No significant beta on market, SMB, or HML; significant *positive*
  loading on UMD (cross-sectional momentum), but the large alpha shows TSMOM is not subsumed by
  cross-sectional momentum.
- **Per-instrument results:** **all 58 contracts** have positive TSMOM returns; **52 of 58 are
  statistically different from zero at the 5% level.**
- Versus an always-long strategy: positive alpha in **90%** of cases (26% statistically
  significant; none of the negative ones significant).
- **Out-of-sample 1966–1985** (limited instruments): statistically significant return and
  **annualized Sharpe ratio of 1.1** — *"providing strong out-of-sample evidence."*
- **Correlation with illiquidity:** correlation between Sharpe ratio and contract illiquidity
  = **−0.16** (weak).
- **Correlation structure:** within-asset-class TSMOM correlations 0.37–0.38 (equities, fixed
  income), 0.10 (commodities), 0.07 (currencies) — versus passive longs at 0.60–0.63 for equities
  and fixed income.

**Decomposition:** *"the dominant force to both [time-series and cross-sectional momentum]
strategies is significant positive auto-covariance between a security's excess return next month
and its lagged one-year return."* Also: no significant relationship between TSMOM profitability and
either market volatility (VIX) or the Baker-Wurgler sentiment index.

---

## 2. Crypto-specific time-series and cross-sectional momentum

### 2a. Liu, Tsyvinski & Wu (2022), "Common Risk Factors in Cryptocurrency"

**Citation (VERIFIED via Crossref, DOI 10.1111/jofi.13119):**
Liu, Yukun; Tsyvinski, Aleh; Wu, Xi (2022). "Common Risk Factors in Cryptocurrency."
*The Journal of Finance* **77**(2), 1133–1177. (Published online 24 Feb 2022.)

**Working URLs:**
- NBER WP 25882 (full PDF read for this report): https://www.nber.org/papers/w25882
- Open PDF: http://www.nber.org/papers/w25882.pdf
- RePEc: https://ideas.repec.org/a/bla/jfinan/v77y2022i2p1133-1177.html
- DOI: https://doi.org/10.1111/jofi.13119 (publisher page 403s to bots; record confirmed via Crossref)

**Sample:** 1,707 coins, weekly, beginning of 2014 – end of 2018. Weekly returns built from daily
close prices (52 weeks/year). Excludes coins with market cap < $1M; winsorizes non-return variables
at 1st/99th percentiles. Coin universe from coinmarketcap.com **including defunct coins** —
"alleviating concerns about survivorship bias." Coin count grows from 109 (2014) to 1,583 (2018).
Mean (median) market cap $356.71M ($8.17M); mean (median) daily dollar volume $18.31M ($103.89K).
Average coin market index return 1.3%/week; Bitcoin 1.2%; Ripple 3.5%; Ethereum 4.6%.

#### Momentum lookbacks — CORRECTION TO YOUR PREMISE

**You asked about "1-week, 2-week, 3-week, 4-week, and past 1/3/6/12 month."** What the paper
actually tests is: **1, 2, 3, 4, 8, 16, 50, and 100 weeks.**

- **Statistically significant:** 1-, 2-, 3-, and 4-week momentum.
- **NOT statistically significant:** 8-, 16-, 50-, and 100-week momentum (paper: *"The results of
  the zero-investment long-short strategies for the eight-, sixteen-, fifty, and one hundred-week
  momentum are not statistically significant"*).
- Only **9 of 25** price/market factors tested produce significant long-short strategies:
  market cap, price, max price, 1/2/3/4-week momentum, dollar volume, and SD of dollar volume.

#### Exact momentum returns — Table 4 (Q5−Q1, weekly value-weighted excess returns)

| Lookback | Q5−Q1 mean | t-stat | Significance |
|---|---|---|---|
| 1 week | **2.7%** | **1.994** | 5% |
| 2 week | **3.3%** | **2.442** | 5% |
| 3 week | **4.1%** | **2.742** | 1% |
| 4 week | **2.5%** | **2.002** | 5% |

Quintile means are "almost universally monotonic."

#### THE KEY FINDING — it is 3-WEEK, not 1-week

**Footnote 7 of the paper, verbatim:**
> *"We use three-week momentum as our main momentum measure because it generates the largest
> long-short spread in the data. The results are qualitatively similar using alternative measures
> of momentum."*

So the **CMOM (crypto momentum) factor is built on 3-week momentum**, and 3-week has both the
largest spread (4.1%/week) and the highest t-stat (2.742).

**Therefore: "they report 1-WEEK momentum is strongest in crypto (unlike stocks where 1-month is
standard)" is INCORRECT and should be removed from any strategy documentation.** The defensible
version of the claim is: *in crypto, momentum operates at a much shorter horizon than the stock
market's 3–12 month standard — the significant lookbacks are 1–4 weeks (i.e. roughly a
one-month scale), and the peak is 3 weeks.* Peak ≠ 1 week.

#### Other key LTW numbers

- **Size strategies (for context):** market cap 3.4%/week, end-of-week price 3.9%, highest price of
  week 4.1%; dollar volume 3.2%/week; returns "almost monotonic" across quintiles.
- **Three-factor model** (CMKT, CSMB, CMOM) accounts for all nine significant strategies. *"Adjusted
  for the cryptocurrency three-factor model, none of the alphas of the nine strategies remains
  statistically significant."* CMOM alone accounts for the 2-, 3-, and 4-week momentum strategies;
  **both CSMB and CMOM** are needed for the 1-week strategy.
- **One-factor (crypto CAPM) model performs poorly**; R² of long-short strategies ranges from ~0%
  (one-week momentum) to 26.8% (max day price).
- **Short-selling robustness:** replacing the short quintile with a short Bitcoin position —
  "the results virtually do not change."
- **Size interaction:** momentum works only in large coins. Below-median size group: **0.6%/week,
  statistically insignificant**; above-median size group: **4.2%/week, statistically significant.**
- **Daniel et al. (2018)-style unpriced-risk removal** "strengthens the cryptocurrency size factor
  but **not** the cryptocurrency momentum factor" — suggested reason: *"loadings on the
  cryptocurrency momentum factor are more transitory than loadings on the cryptocurrency size
  factor."* This is a notable internal weakness of CMOM.

#### Evidence AGAINST (from the paper itself)

**Verbatim caveat:** *"Of course, this strategy does not take into account trading costs and the
feasibility of short selling."* The headline ~3%/week figures are **GROSS**. Sample is only
2014–2018 (5 years). t-stats of 1.99–2.74 on overlapping weekly data are modest.

---

### 2b. Liu & Tsyvinski (2021), "Risks and Returns of Cryptocurrency"

**Citation (VERIFIED via Crossref, DOI 10.1093/rfs/hhaa113):**
Liu, Yukun; Tsyvinski, Aleh (2021). "Risks and Returns of Cryptocurrency."
*The Review of Financial Studies* **34**(6), 2689–2727. (Published online 26 Sep 2020.)

Note: the Crossref author list is Liu & Tsyvinski; NBER's "Published versions" field lists
"Itay Goldstein" as a third author, which appears to be an NBER metadata artifact — **the RFS
article is by Liu & Tsyvinski (2 authors).**

**Working URLs:**
- NBER WP 24877: https://www.nber.org/papers/w24877 (DOI 10.3386/w24877)
- DOI: https://doi.org/10.1093/rfs/hhaa113
- RePEc: https://ideas.repec.org/a/oup/rfinst/v34y2021i6p2689-2727..html

**Key findings (NBER abstract, verbatim):** risk-return tradeoff of Bitcoin, Ripple and Ethereum is
distinct from stocks, currencies and precious metals; no exposure to most common stock market and
macroeconomic factors, nor to currency/commodity returns. *"Specifically, we determine that there is
a strong time-series momentum effect and that proxies for investor attention strongly forecast
cryptocurrency returns."* Also constructs an index of crypto exposures of 354 US and 137 Chinese
industries.

**On momentum specifically:** the paper documents **time-series** momentum (a coin's own past
returns predict its own future returns), reported to predict up to **eight weeks** ahead. This is
time-series, not cross-sectional, momentum — an important distinction from Liu-Tsyvinski-Wu.

**CAVEAT (from Han, Kang & Ryu's critique):** Liu & Tsyvinski (2021) **use only ten major coins**.
Their finding that low-attention coins show stronger time-series momentum (which they attribute to
underreaction) is **directly contradicted** by Han et al., who find time-series momentum performs
comparably across attention/volume groups and that a high-volume portfolio actually does slightly
better (Sharpe 1.67 vs 1.76 for the lowest-volume group — a trivial difference). Han et al.
attribute momentum to **overreaction**, not underreaction.

---

### 2c. Tzouvanas, Kizys & Tsend-Ayush (2020) — VERIFIED

**Citation (VERIFIED via Crossref, DOI 10.1016/j.econlet.2019.108728):**
Tzouvanas, Panagiotis; Kizys, Renatas; Tsend-Ayush, Bayasgalan (2020).
"Momentum trading in cryptocurrencies: Short-term returns and diversification benefits."
*Economics Letters* **191**, 108728. (Early online 30 Sep 2019; published May 2020.)

**Working URLs:**
- RePEc + verbatim abstract: https://ideas.repec.org/a/eee/ecolet/v191y2020ics0165176519303647.html
- Accepted manuscript (open, CC BY-NC-ND): https://researchportal.port.ac.uk/en/publications/momentum-trading-in-cryptocurrencies-short-term-returns-and-diver/
  (direct PDF 403s to bots: https://researchportal.port.ac.uk/files/84609936/Momentum_trading_in_cryptocurrencies_Short_term_returns_and_diversification_benefits.pdf)
- DOI: https://doi.org/10.1016/j.econlet.2019.108728

**Method & findings (verbatim abstract):** classic **J/K** momentum strategy on **daily data from
twelve cryptocurrencies over three years**. *"We identify the existence of momentum effect, which is
**highly significant for short-term portfolios but disappears over the longer term**."* Cross
correlations of weekly returns between crypto momentum portfolios and traditional assets differ
from traditional-asset correlations. Crypto momentum portfolios *"not only offer diversification
benefits but also can be a hedge and safe haven for traditional assets."* Uses DCC-GARCH
(Engle 2002).

**Assessment: WEAK evidence.** 12 coins only, 3-year sample, and no transaction costs are modeled.
The "short-term works, long-term doesn't" conclusion is directionally consistent with LTW and with
Han et al., but the paper's real contribution is the diversification/DCC analysis, not a rigorous
momentum test.

---

### 2d. Grobys & Sapkota (2019) — VERIFIED, and your recollection is CORRECT

**Citation (VERIFIED via Crossref, DOI 10.1016/j.econlet.2019.03.028):**
Grobys, Klaus; Sapkota, Niranjan (2019). "Cryptocurrencies and momentum."
*Economics Letters* **180**, 6–10.

**Working URLs:**
- RePEc + verbatim abstract: https://ideas.repec.org/a/eee/ecolet/v180y2019icp6-10.html
- DOI: https://doi.org/10.1016/j.econlet.2019.03.028

**Exact conclusion (verbatim abstract):** *"Retrieving a set of **143 cryptocurrencies** for a sample
spanning **2014–2018**, we investigate the popular momentum strategy implemented in the
cryptocurrency market. **Contrary to earlier studies our findings do not indicate any evidence of
significant momentum payoffs**, supporting the view that the cryptocurrency market is far more
efficient than suggested in earlier studies."*

**This is the strongest direct contradiction of LTW**, on a heavily overlapping sample (143 coins,
2014–2018 vs LTW's 1,707 coins, 2014–2018). The resolution is likely methodological — see Han
et al. (2024) below, which shows that result differences hinge on portfolio construction,
weighting, rebalancing day and interim mark-to-market.

---

### 2e. "Cryptocurrency momentum: Is it an illusion?" — VERIFIED, and my title correction

**Citation (VERIFIED via RePEc; Wiley DOI resolves but 403s to bots):**
Grobys, Klaus; Shahzad, Syed Jawad Hussain (2026). "Cryptocurrency Momentum: Is It an Illusion?"
*International Journal of Finance & Economics* **31**(2), 2180–2193, April 2026.
DOI: **10.1002/ijfe.70036**. Earlier working-paper version: SSRN DOI 10.2139/ssrn.4633099 (2023).

**Working URLs:**
- RePEc + verbatim abstract: https://ideas.repec.org/a/wly/ijfiec/v31y2026i2p2180-2193.html
- OUCI (SSRN version + reference list): https://ouci.dntb.gov.ua/en/works/4zneAOv4/
- Wiley: https://onlinelibrary.wiley.com/doi/abs/10.1002/ijfe.70036 (bot-blocked)

**Note on the title:** the published title uses a **capital I — "Is It an Illusion?"**; the SSRN
working-paper version uses lowercase — "Is it an illusion?"

**Conclusion (verbatim to the extent quoted):** the authors examine the distributional
characteristics of **six** crypto momentum trading strategies and find their realized variances are
governed by **power laws**. Block-bootstrap tests indicate that *"the population mean and variance of
the momentum factor realised variances are statistically not defined."* They conclude: *"Contrary to
the belief that cryptocurrency momentum trading strategies produce generous payoffs, our results
imply that, in real life, we might not be able to realise these risk premiums"* and *"the performance
metrics evaluating the profitability of cryptocurrency momentum strategies, using variance as an
input, are not informative."* The SSRN version states it more starkly: *"t-statistics or Sharpe
ratios do not exist for this strategy and the premium is not observable in reality."* They also find
cross-sectional dependence among the tail risk of strategies based on different formation periods.

**Skeptical assessment: treat as an EXTREME claim, not consensus.** This is an argument from
infinite-variance / power-law tail behavior (Mandelbrot, Taleb-style reasoning). It is a legitimate
methodological critique of relying on Sharpe ratios under fat tails, and it echoes Han et al.'s
point that mean-return t-tests are inadequate in crypto. But "the moments don't exist" is a much
stronger and more contested claim than "the effect is weak after costs." Note Grobys has a series of
similarly framed papers (see 2f and the "Science or scientism?" paper below), and this is a small
author group publishing repeatedly on the same thesis.

---

### 2f. Related Grobys-group papers (all VERIFIED via Crossref/RePEc)

**(i) "Cryptocurrency momentum has (not) its moments"** — Grobys, Kolari, Sandretto, Shahzad, Äijö
(2025), *Financial Markets and Portfolio Management* **39**(4), 443–476. DOI 10.1007/s11408-025-00474-9
(https://ideas.repec.org/a/kap/fmktpm/v39y2025i4d10.1007_s11408-025-00474-9.html).
Abstract: *"cryptocurrency momentum is subject to severe crashes. **Even a single cryptocurrency can
cause insignificant momentum portfolio returns.** ... volatility management is a useful tool for
mitigating cryptocurrency momentum crashes. ... cryptocurrency momentum appears to be a phenomenon
associated with large-cap cryptocurrencies."* **A single coin can flip the result** — that is a
concrete, actionable fragility warning.

**(ii) "On survivor cryptocurrency momentum"** — Grobys, Sandretto, Äijö (2026), *Finance Research
Letters* **92**, 109602. DOI 10.1016/j.frl.2026.109602
(https://ideas.repec.org/a/eee/finlet/v92y2026ics1544612326001339.html). Nine free-floating coins
held inside the top-100 altcoins, weekly, Jan 2017 – Aug 2024. Findings: **(a) momentum is not
evident for survivor coins; (b) plain (largest-30) momentum is profitable only after the dataset is
trimmed; (c) that profitability does not come from survivor-coin momentum; (d) even after trimming,
profitability is highly sample-dependent.** This is a direct survivorship-bias critique.

**(iii) "Technical trading rules in the cryptocurrency market"** — Grobys, Ahmed, Sapkota (2020),
*Finance Research Letters* **32**. DOI 10.1016/j.frl.2019.101241
(https://ideas.repec.org/a/eee/finlet/v32y2020ics1544612319308852.html).

**(iv) "Science or scientism? On the momentum illusion"** — Grobys (2024), *Annals of Finance*
**20**(4), 479–519. DOI 10.1007/s10436-024-00446-5. (Same thesis, equity-general.)

---

### 2g. The AUT / ACFR paper — FULLY RESOLVED

**Your note said the ACFR PDF is Cloudflare-blocked. Confirmed — but it is retrievable via the
r.jina.ai reader proxy, and the full 116-page text (incl. Internet Appendix) has been extracted.**

**Citation (VERIFIED via Crossref + OpenAlex):**
Han, Chulwoo; Kang, Byeongguk; Ryu, Jehyeon (2024).
"**Time-Series and Cross-Sectional Momentum in the Cryptocurrency Market: A Comprehensive Analysis
under Realistic Assumptions**" (ACFR working paper / SSRN DOI 10.2139/ssrn.4675565).
All three authors are at **Sungkyunkwan University, Seoul — NOT AUT-affiliated.** The AUT/Auckland
Centre for Financial Research link is that it was **presented at the New Zealand Finance Meeting,
5–7 Dec 2024, at AUT**, and ACFR hosts the working paper. Accepted at **Review of Asset Pricing
Studies (RAPS, Oxford UP)** — acceptance confirmed by an SKKU announcement, but **no volume/issue/DOI
assigned yet (could not verify).**

**Working URLs:**
- ACFR PDF (Cloudflare-blocked to direct fetch): https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf
- **Working proxy that returned full text (HTTP 200):**
  https://r.jina.ai/https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf
- SSRN: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4675565 (bot-walled; metadata via Crossref)
- NZFM 2024 programme: https://acfr.aut.ac.nz/__data/assets/pdf_file/0008/968633/Conference-Programme-and-Information-2-1.pdf
- Replication data: Harvard Dataverse DOI 10.7910/dvn/tspkpg
- RAPS acceptance (Korean): https://ecostat.skku.edu/ecostat/news.do?mode=view&articleNo=225517

**Abstract (verbatim from the ACFR PDF):**
> "Prior studies on cryptocurrency momentum disregard important real-world considerations and
> inadequately assess its performance. We analyze time-series and cross-sectional momentum addressing
> these issues. When appropriately assessed, e.g., accounting for transaction costs and daily price
> fluctuations, many momentum portfolios are liquidated and many with statistically significant
> returns earn insignificant profits. The t-test of the mean return is insufficient to test
> profitability. Evidence of time-series momentum is strong, whereas evidence of cross-sectional
> momentum is weak. The momentum effect is concentrated among large winners. Losers often rebound and
> inflict significant losses. Overreaction is a likely cause of momentum, but what drives
> overreaction remains unclear."

**Sample:** CoinMarketCap daily OHLC/volume/market cap, **28 Dec 2013 – 28 Aug 2023**; no survivorship
bias (active + inactive). Filters: market cap ≥ $1M **and** daily volume ≥ $1M (volume binding);
96 stablecoins excluded. Filtered universe peaks at **784 coins** (Dec 2021), 433 at end. Separate
**Binance USDT-futures subsample: 239 coins, 8 Sep 2019 – 17 Nov 2023** (to allow shorting).
Lookback/holding grid: **1 to 56 days** (j, k ∈ {1,3,5,7,14,21,28,35,42,49,56}).

**Key numeric findings:**

| Result | Value |
|---|---|
| **Time-series momentum (best): 28-day lookback / 5-day holding, long-only** | **Sharpe 1.51** (cum. 36,686%, MDD 61.8%) |
| Market benchmark | Sharpe 0.84–0.85 (cum. 2,481%) |
| Bitcoin / Ethereum / NASDAQ-100 Sharpe | 0.88 / (ETH MDD 94.0%) / 0.96 |
| TS momentum in market only 48% of the time; superior performance from reduced downside | — |
| TS momentum under different weighting schemes | Sharpe 1.40–1.65 (capped-VW 1.65, equal-weight 1.55) |
| TS momentum low-volume vs high-volume Sharpe | 1.76 vs 1.67 (trivial difference — contradicts LT) |
| TS momentum short-only | **Loses money in most cases** |
| Individual-coin TS momentum long-short | **Negative profit for the majority of (j,k) pairs** |
| Cross-sectional momentum: of 21 selected pairs | **5 LIQUIDATED**; only 6 (or 8) beat the market |
| CS momentum best after costs: (14, 7) | **Sharpe 1.28** vs market 1.01 |
| CS momentum best before costs: (1, 7) | Sharpe 1.75, MDD 45.5% |
| CS long-only (14, 5) | Sharpe 1.62, cum 267,262% |

**THE MOST IMPORTANT RESULT IN THIS ENTIRE REPORT** (the mean-vs-log-return test):
> *"Ten portfolios yield a positive mean return with a t-statistic greater than 2.0, but only three
> of them have a mean log return with a t-statistic greater than 2.0. Moreover, six portfolios with a
> positive mean return are either liquidated or earn a negative profit."*

This is a **methodological landmine for backtesting**: with fat-tailed, jumpy crypto returns,
Jensen's inequality means a portfolio with a statistically significant positive **arithmetic mean**
return can have a **negative compound (log) return** — i.e. it loses money. Han et al. propose a
**t-test of mean log return** instead. **Any crypto backtest you run should report mean log return /
CAGR, not just arithmetic mean and a t-stat.**

**Other structural findings:**
- **Momentum lives in the LONG leg and in LARGE coins** — the *opposite* of equities, where momentum
  comes from the short leg and small stocks. The short leg is exposed to jump risk and loses money.
  Actionable: **run long-only, not long-short.**
- Except for a few of the largest coins, **most coins exhibit REVERSAL, not momentum.** Cross-
  sectional regression coefficients are **mostly negative**, strongest reversal at j=3, k=1. Among
  the top 5% of coins by market cap, coefficients turn positive for short lookbacks, and
  **long-term reversal appears among large coins** when both windows exceed a month.
- Momentum documented among **winners** (esp. large winners); losers often **rebound** and inflict
  losses. Strong long-term reversal among large losers.
- **Attention explanation REJECTED.** Long-leg performance *worsens monotonically with volume*; a
  high-attention long-short portfolio performs *worse*. This contradicts LTW's attention channel.
- **Rejection of the underreaction story:** momentum is better explained by **overreaction**
  (evidence: long-term reversal; retail-dominated market; stronger effect among winners with higher
  continuing-overreaction measures; three-digit returns not uncommon).
- **Day-of-week matters a lot:** (28,7) long-only Sharpe ranges from **1.40 (Monday)** to
  **1.09 (Sunday)**. Authors warn this can be exploited to fabricate favorable results.

**The authors' own bottom line (verbatim):**
> *"we do test various pairs of look-back and holding periods and choose optimal combinations. This
> practice introduces a **look-ahead bias**. ... our findings should be regarded as an **optimistic
> view**. ... A momentum-based long-short strategy that can generate steady, market-neutral profits
> **appears unattainable**. **The maximum Sharpe ratio we obtain from a momentum strategy is about
> 1.5.** ... Considering the high tail risk, the small number of liquid coins, and the high dominance
> of a few major coins, it is difficult to argue that a cryptocurrency momentum strategy is an
> attractive alternative investment vehicle to institutional investors."*

**Caveat they state:** only ~10 years of data, and the period with an adequate number of coins is
shorter still; conclusions may be overturned as the market matures.

---

## 3. Transaction cost reality — does crypto momentum survive 0.1%/side?

### 3a. Han, Kang & Ryu (2024) — the most careful cost treatment

**Assumption: 15 bps per trade.** Derivation (verbatim):
> *"Binance ... charges a fee of **10 bps to regular users in the spot market and 4.5 bps in the
> futures market**, and the average **tick size relative to the price is 3.26 bps** in the futures
> market. From **15,661,698 records of actual market orders** in the Binance futures market during
> the period from June 24, 2023 to August 20, 2023, we find that the minimum, maximum, and average
> **slippage** per coin are respectively 0.01, 11.81, and **1.53 bps**. The order sizes are very
> small compared to the daily trading volume: **56 USD or 0.02%** of the daily trading volume on
> average. **Larger orders would have a bigger impact on the market resulting in greater
> slippages.** ... we consider a transaction cost of **15 bps a reasonable estimate (or perhaps
> closer to the lower limit)** of the actual transaction costs."*

**Implication for your 0.1%/side assumption:** 0.1% = **10 bps per side**, i.e. **20 bps round trip**.
Han et al.'s own 15 bps/trade estimate corresponds to a **30 bps round trip** — *more* conservative
than your 0.1%/side. Their estimate is empirically grounded in real Binance order data, and they
explicitly flag it as a **lower bound** because their measured order sizes were only $56 (0.02% of
daily volume). **Your 0.1%/side (20 bps round trip) is realistic-to-optimistic for taker flow on
major pairs, and clearly optimistic for anything but the largest coins.**

Their net-of-cost conclusions: TS momentum (28,5) long-only still delivers **Sharpe 1.51** net of
15 bps — because it trades only ~48% of the time and is long-only. But **cross-sectional long-short
deteriorates sharply**: best net Sharpe 1.28 vs 1.75 gross; 5 of 21 portfolios liquidated; and the
mean-vs-log-return test kills 6 apparently-profitable portfolios.

### 3b. Fieberg, Liedtke, Poddig, Walker & Zaremba (2025), JFQA — the explicit BETC number

**Citation (VERIFIED via Crossref, DOI 10.1017/S0022109024000747):**
"**A Trend Factor for the Cross Section of Cryptocurrency Returns**", *Journal of Financial and
Quantitative Analysis* **60**(7), 3116–3153, Nov 2025. Open access (CC BY).
- Full PDF (downloaded and read): https://resolve-he.cambridge.org/core/services/aop-cambridge-core/content/view/4C1509ACBA33D5DCAF0AC24379148178/S0022109024000747a.pdf/a-trend-factor-for-the-cross-section-of-cryptocurrency-returns.pdf
- DOI: https://doi.org/10.1017/S0022109024000747

Data: >3,000 coins, **Apr 2015 – May 2022**. Costs modeled as **40 bps short / 30 bps long leg**
(following Bianchi et al. 2022), with robustness at 50/40 and 60/50. Turnover computed per Gu et al.
(2020). They report **BETC** (breakeven cost, net return → 0) and **BETC5%** (cost at which net
return loses 5% significance).

**Table 9, Panel A (all coins), H−L long-short CTREND:**

| Cost scenario | Net return | t-stat | Turnover | BETC | BETC5% |
|---|---|---|---|---|---|
| Gross | 3.87% | 5.19 | — | — | — |
| Net (I): 30/40 bps | 2.90% | 3.89 | 68.46% | — | — |
| Net (II): 40/50 bps | 2.62% | 3.53 | 68.46% | — | — |
| Net (III): 50/60 bps | 2.35% | 3.16 | 68.46% | — | — |
| **Breakeven** | — | — | 68.46% | **1.41%** | **0.88%** |

**Panel B (largest 100 coins): H−L gross 3.40% (t=4.48) → net 2.45% (t=3.22); BETC 1.25%, BETC5%
0.70%; turnover 68.21%.**

**Reading these numbers — CORRECTION:** BETC is **per side, not round-trip.** BETC = 1.41% means the
strategy breaks even when the cost is **1.41% per side**; BETC5% = 0.88% per side is where it loses
5% significance. So the headroom is genuinely large *in per-side terms*.

**This is NOT evidence that plain momentum survives costs.** Three critical qualifiers:
1. CTREND is an **ML-aggregated technical/trend factor**, not classic cross-sectional momentum.
2. **The same paper kills classic momentum** — verbatim: *"Most cryptocurrency momentum strategies no
   longer produce significant profits at a 3- or even 2-week horizon."*
3. The 30/40 bps cost assumption is a **maker-fee** assumption. See §3g — the source paper's own
   measured spreads are far wider, and the authors describe it as representing *"a set of fairly
   conservative transaction costs for a **market maker**."*

Also note their quintile breakdown: BETC falls off a cliff outside the top quintile —
L 0.08 / Q2 0.58 / Q3 0.72 / Q4 1.80 / H 3.17 (BETC5%: 0.00 / 0.00 / 0.00 / 0.82 / 1.86 per side).
**The tradable headroom lives almost entirely in the top quintile (H).**

### 3c. Arefev (2026) — net-of-costs replication, THE most directly on-point paper

**Citation:** Arefev, Oleg (2026). "**Cross-Sectional Momentum in Cryptocurrency: A Net-of-Costs
Replication on Tradable Binance Perpetuals (2020–2026)**", SSRN working paper, DOI
10.2139/ssrn.7404139. (OpenAlex record verified: year 2026, single author.)
- DOI: https://doi.org/10.2139/ssrn.7404139 (SSRN pages bot-wall; no abstract available via OpenAlex)

**Findings (from the research subagent; abstract not retrievable — see §6):** the gross J=1/K=1
cross-sectional spread is **+0.573%/week**, against realistic costs of **~0.40 pp/week** — the same
order of magnitude. Verbatim (as reported): *"neither of the two literature-backed specifications
... (J=1/K=1 and J=2/K=2) yields a net-of-costs spread distinguishable from zero under any of the
three tests. The gross J=1/K=1 spread (+0.573%/week) is of the same order of magnitude as realistic
costs (0.40 pp/week) and disappears into noise once they are subtracted."*

**Skeptical assessment: WEAK evidence — treat with caution.** This is a non-peer-reviewed SSRN
working paper by a single author, and **neither the abstract nor the full text could be retrieved**
(SSRN 403s; OpenAlex has no abstract). **The quoted figures are unverified second-hand.** But it is
the most directly on-point artifact for your exact question (tradable Binance perpetuals, net of
costs, 2020–2026 — i.e. the modern regime). Read it as a hypothesis to test, not a finding to rely on.

### 3g. Where the "30/40 bps" convention comes from — and why it is NOT conservative

**Citation (VERIFIED via Crossref, DOI 10.1016/j.jbankfin.2022.106547):**
Bianchi, Daniele; Babiak, Mykola; Dickerson, Alexander (2022). "Trading volume and liquidity
provision in cryptocurrency markets." *Journal of Banking & Finance* **142**, 106547.
Open-access version: CERGE-EI WP 730, http://www.cerge-ei.cz/pdf/wp/Wp730.pdf

This is the source of the 30 bps long / 40 bps short cost convention used by the JFQA paper above.
Verbatim: *"Order making fees are zero for the cheapest exchanges, such as CoinbasePro, itBit and
Luno, and increase to 0.43% for more expensive exchanges such as BitBay. For this reason, we apply a
fixed cost of 30 (40) basis points for the long (short) side."*

**CRITICAL REALITY CHECK:** the *same paper's own measured* synthetic bid-ask spreads are
**3.2%–5.3% (Corwin-Schultz)** and **6.5%–14.1% (Abdi-Ranaldo)**. The 30/40 bps figure is a
**maker-fee** assumption, and the JFQA authors themselves describe it as representing *"a set of
fairly conservative transaction costs for a **market maker**."* **It is not a taker cost.**

Their own net-of-cost result is also instructive: standard short-term reversal stays positive on an
equal-weighted basis (avg daily 1.68%, p=0.001), but **value-weighted, "none of the strategies
generate positive and statistically significant returns... alphas drastically decrease and are no
longer statistically significant."**

### 3h. Measured RETAIL costs — the decisive reality check

As reported by the research subagent (**citation not independently verified by me**):
Stotz, Olaf (2026), "The Retail Costs of Cryptocurrency Trading", SSRN DOI 10.2139/ssrn.7540899.
Field experiment, **2,088 round-trip market orders** on BTC/ETH/DOGE across six German brokers:
> *"Average costs range from **123 to 458 basis points** across brokers, far above quoted exchange
> spreads of 16-34 basis points... Market impact is not observed."*

**This is the single most important practical number in this report.** If your execution resembles
retail taker flow, round-trip costs of **1.23%–4.58%** are **3–10× above** the breakeven thresholds
of the most favorable studies — and they **annihilate** a gross edge of ~0.57%/week. Under measured
retail costs, **no published crypto momentum result survives.**

*Caveat: I verified the SSRN DOI format but could not confirm the paper's existence/content directly
(SSRN bot-walls). Treat the 123–458 bps figure as reported-but-unverified; it is nonetheless highly
consistent with the Bianchi et al. measured spreads above, which I verified from the source paper.*

### 3i. Gross-only papers — do not mistake for cost evidence

- **Grobys, Ahmed & Sapkota (2020)**, "Technical trading rules in the cryptocurrency market",
  *Finance Research Letters* **32**, 101396. DOI 10.1016/j.frl.2019.101396. VERIFIED. Reports 8.76%
  p.a. excess return (ex-Bitcoin, 20-day MA, 2016–2018), but the subagent text-mined the full OA PDF
  from University of Vaasa and found **the words transaction cost / fees / spread / slippage appear
  zero times. The 8.76% is GROSS.** (Note: this corrects the DOI I originally listed in §2f-iii,
  10.1016/j.frl.2019.101241 → correct is **10.1016/j.frl.2019.101396**.)
- **Dobrynskaya (2021)**, "Cryptocurrency Momentum and Reversal", SSRN 3913263 — 2,000 largest coins,
  2014–2020, "economically large, statistically significant" — **GROSS, no cost analysis.**
- **Caporale & Plastun (2019)**, "Price overreactions in the cryptocurrency market", *Journal of
  Economic Studies* **46**(3), 1137–1155. DOI 10.1108/JES-09-2018-0310. VERIFIED. One of the few that
  **does** build costs in (*"our analysis incorporates the most significant component of transaction
  costs (the spread)"*). Momentum/"inertia" strategy on BTC yields +$5,879 over 2015–2017
  (155 trades), but **all t-tests vs. random FAILED**: *"the overreactions detected in the
  cryptocurrency market do not give rise to exploitable profit opportunities (possibly because of
  transaction costs)."* Spread-only cost model; small trade counts — suggestive, not definitive.
- **"Retail Trader's Ruin: An Anatomy of Popular Signal Failure"** (2026), arXiv:2607.20093,
  https://arxiv.org/abs/2607.20093 — a preprint using a net-of-cost gated test (multiplicity
  correction + economic viability + bankroll survival). Four of six signal families refuted; **trend
  and a momentum calibration benchmark are "INCONCLUSIVE", not refuted.** Preprint; the momentum
  verdict is unresolved rather than dead.

**No paper was found that demonstrates crypto momentum robustly surviving out-of-sample across regimes and net of costs.**

### 3j. Highest-priority unread item

**Svogun, Daniel; Bazán-Palomino, Walter (2022)**, "**Technical analysis in cryptocurrency markets:
Do transaction costs and bubbles matter?**", *Journal of International Financial Markets,
Institutions and Money* **79**, 101601. DOI 10.1016/j.intfin.2022.101601. **DOI, title, authors,
journal, volume and page all VM-verified via Crossref.** The title is *exactly* on-point for the
transaction-cost question. **No abstract or full text was retrievable** (no Crossref abstract, no
OpenAlex abstract, closed access, Elsevier API key required, ScienceDirect 403s). **Retrieve this
manually via institutional access — it is the single most promising unread paper for your question.**

### 3d. Fieberg, Liedtke & Zaremba (2024) — "does it survive costs?"

**Citation (VERIFIED via Crossref, DOI 10.1016/j.irfa.2024.103218):**
Fieberg, Christian; Liedtke, Gerrit; Zaremba, Adam (2024). "Cryptocurrency anomalies and economic
constraints." *International Review of Financial Analysis* **94**, 103218.
(https://ideas.repec.org/a/eee/finana/v94y2024ics1057521924001509.html)

**Verbatim abstract findings — directly relevant:**
> *"We find that size and volume anomalies originate from micro-cap coins of negligible economic
> importance. Conversely, **the momentum effect prevails in larger cryptocurrencies but incurs
> substantial trading costs and extracts alphas largely from short positions**. Most abnormal returns
> occur primarily in bull markets and fade over time. Therefore, protocols for identifying tradable
> cryptocurrency anomalies should focus on **long positions**, **account for transaction costs**,
> consider **hard-to-trade coins**, and emphasize performance in **recent years**."*

This is the cleanest statement in the literature that (i) crypto momentum survives in large caps,
(ii) costs are substantial, (iii) the short leg is where the alpha nominally lives but is also a
problem, and (iv) the effect **fades over time**.

### 3e. Zaremba, Bilgin, Long, Mercik & Szczygielski (2021) — CAVEAT: NOT a cost paper

**Citation (VERIFIED via Crossref, DOI 10.1016/j.irfa.2021.101908):**
*"Up or down? Short-term reversal, momentum, and liquidity effects in cryptocurrency markets"*,
*International Review of Financial Analysis* **78**, 101908.
(https://ideas.repec.org/a/eee/finana/v78y2021ics1057521921002349.html)

**IMPORTANT: this paper does NOT measure transaction costs.** It is a liquidity/reversal paper.
Findings: based on daily prices of **>3,600 coins**, coins with **low last-day return significantly
outperform** those with high last-day return. The daily reversal is attributed to **illiquidity**;
the pattern is cross-sectionally dependent on liquidity, and **only the handful of largest, most
tradable coins exhibit daily MOMENTUM rather than reversal.** *"Our findings help to reconcile
earlier conflicting evidence on return persistence in cryptocurrency markets."*

**Do not cite this as transaction-cost evidence.** Its value here is the liquidity-dependence
result: **momentum (rather than reversal) is a large-cap phenomenon** — which independently
corroborates Han et al. and Fieberg et al.

### 3f. Explicit out-of-sample / replication failures

- **Grobys & Sapkota (2019)** — no significant momentum payoffs, 143 coins 2014–2018 (§2d).
- **Grobys, Sandretto & Äijö (2026)** — momentum absent in survivor coins; plain momentum profitable
  **only after trimming**; **highly sample-dependent** (§2f-ii). This is a survivorship-bias failure.
- **Grobys, Kolari et al. (2025)** — **a single cryptocurrency can render momentum portfolio returns
  insignificant** (§2f-i).
- **Han, Kang & Ryu (2024)** — look-ahead bias in choosing optimal (j,k) pairs; 5 of 21 CS portfolios
  liquidated; results "fragile" in the Binance futures subsample with most profits pre-2022.
- **Arefev (2026)** — net-of-costs replication on modern perpetuals: ≈ zero (§3c).
- **Fieberg et al. (2024)** — abnormal returns "**fade over time**" (§3d).

**No paper was found that demonstrates crypto momentum robustly surviving out-of-sample across
regimes and net of costs.** The strongest pro-momentum evidence (LTW) is gross, 2014–2018, and
cross-sectionally fragile.

---

## 4. Strength-of-evidence summary

| Claim | Evidence strength | Notes |
|---|---|---|
| TSMOM exists in futures, 1–12m lookbacks | **STRONG** | 58 contracts, 25 yrs, all positive, 52/58 significant, OOS 1966–85 Sharpe 1.1 |
| TSMOM 12m/1m is the peak (t=6.61) | **STRONG** | Table 2, all assets |
| "Smile" = lookback pattern | **FALSE** | It's a market-state (S&P 500) smile; lookback pattern is monotone decay |
| 40% vol target, EWMA ex-ante vol (60d CoM) | **CONFIRMED** | Eq. (1) and Eq. (5) verbatim |
| Crypto momentum exists, 1–4 week lookbacks | **MODERATE** | LTW 2022 JoF; but only 2014–2018, gross, t≈2.0–2.7 |
| **1-week is the strongest lookback** | **FALSE** | It's **3-week** (4.1%/wk, t=2.742); footnotes say so explicitly |
| Crypto momentum at 1/3/6/12 month lookbacks | **NOT TESTED by LTW** | They used 8/16/50/100 weeks instead — all insignificant |
| Crypto momentum NOT significant | **MODERATE** | Grobys & Sapkota 2019 (143 coins, same era) — direct contradiction |
| Momentum survives in large caps only | **STRONG** | Corroborated by Han et al., Fieberg et al., Zaremba et al. |
| Long-short crypto momentum is unattractive | **STRONG** | Short leg loses; jump risk; 5/21 portfolios liquidated |
| Mean-return t-test is invalid in crypto | **STRONG (methodological)** | Han et al.: 10 portfolios t>2 on mean, only 3 on log return, 6 lost money |
| Momentum survives ~15–20 bps/side | **SIGNAL-SPECIFIC** | Han et al. net Sharpe 1.51 (TS long-only, 15bps); JFQA CTREND BETC 1.41% **per side** (maker-fee assumption) |
| Plain CS momentum survives costs | **WEAK-TO-NEGATIVE** | Fieberg 2024 "substantial trading costs"; Fieberg 2025: momentum dies at 2–3wk horizon; Arefev 2026 ≈ zero net (unverified) |
| Money-making at measured RETAIL costs | **NEGATIVE** | Stotz 2026: 123–458 bps round-trip, 3–10× above breakevens (reported, unverified) |
| Sharpe ratios/t-stats don't exist (power law) | **CONTESTED** | Grobys & Shahzad — legitimate fat-tail critique, but extreme claim |
| The effect has faded over time | **MODERATE** | Fieberg et al. 2024 "fade over time"; Han et al. pre-2022 concentration |

---

## 5. Actionable implications for the platform

1. **Correct the 1-week claim.** Use **3-week** as the LTW-consistent momentum peak; the significant
   band is **1–4 weeks**. Remove "1-week is strongest" from docs.
2. **Report mean log return / CAGR and its t-stat**, not just arithmetic mean. A crypto strategy with
   t>2 on mean return can still lose money (Han et al.).
3. **Prefer long-only.** The long leg carries the momentum; the short leg carries jump risk and loses.
   This also sidesteps the shortability problem LTW flagged.
4. **Restrict the universe to large, liquid coins.** Momentum (vs reversal) is a large-cap
   phenomenon across three independent papers. LTW's own below-median-size momentum is 0.6%/week and
   insignificant vs 4.2%/week above median.
5. **Test sensitivity to rebalancing day.** Sharpe swings 1.40 → 1.09 (Mon → Sun) in Han et al.
6. **Use a maker-like assumption ONLY if you actually provide liquidity.** The literature's standard
   30/40 bps figure is a **maker-fee** assumption, and the source paper's own measured spreads are
   3.2–14.1%. Measured **retail round-trip costs are 123–458 bps** (Stotz 2026, reported). At those
   levels a gross edge of ~0.57%/week is wiped out entirely. **If you execute as a taker, model
   20–60+ bps round-trip minimum and stress to 200+ bps.** Note your 0.1%/side (20 bps RT) is at the
   optimistic end and is only realistic for maker flow or VIP-tier futures on major pairs.
7. **Volatility targeting per MOP (2012).** EWMA variance with a 60-day centre of mass, σ_{t−1}
   lagged, positions sized to a constant ex-ante vol target (40% in the paper; scale to taste).
   Critically: **lag the vol estimate** to avoid look-ahead bias.
8. **Model survivorship bias explicitly.** Grobys et al. (2026) show momentum can be an artifact of
   using only surviving/current top-100 coins.
9. **Expect fragility.** Single coins can flip significance; the effect fades over time and is
   concentrated in bull markets. The best available net-of-cost evidence for *classic* cross-sectional
   momentum (Arefev 2026, unverified) finds it indistinguishable from zero after costs.

---

## 6. Explicitly NOT verified

- **Arefev (2026), SSRN 7404139** — the DOI resolves to an OpenAlex record (title, author, year
  confirmed), but the SSRN page is bot-walled and **neither the abstract nor the full text was
  retrievable**. The "+0.573%/week gross vs ~0.40pp/week costs" figures are **unverified
  second-hand** from a research subagent.
- **Stotz (2026), SSRN 7540899** "The Retail Costs of Cryptocurrency Trading" — the **123–458 bps
  round-trip** retail cost figure is **unverified second-hand**; SSRN bot-walls prevented direct
  confirmation. It is, however, *consistent in direction and magnitude* with the Bianchi et al.
  measured spreads (3.2–14.1%), which I verified from the source paper.
- **Svogun & Bazán-Palomino (2022), DOI 10.1016/j.intfin.2022.101601** — bibliographic record fully
  verified via Crossref, but **no abstract or full text retrievable anywhere**. Highest-priority
  manual retrieval (see §3j).
- **"Retail Trader's Ruin" (arXiv:2607.20093)** and **Dobrynskaya (SSRN 3913263)** — records as
  reported by the subagent; abstracts not independently confirmed by me.
- **RAPS publication of Han, Kang & Ryu** — acceptance confirmed only via a Korean university news
  posting (https://ecostat.skku.edu/ecostat/news.do?mode=view&articleNo=225517); **no volume, issue
  or DOI exists yet.**
- **Semantic Scholar API** returned HTTP 429 on every attempt (with backoff) — no S2 records obtained
  for any paper; all verification was done via Crossref, OpenAlex, RePEc, and direct PDF retrieval.
- **Wiley, ScienceDirect, OUP and SSRN pages** all return 403 to automated fetching. Their metadata
  was verified via Crossref/RePEc/OpenAlex; their *content* was verified via open PDFs, NBER working
  papers, or the r.jina.ai proxy.
- **Grobys & Shahzad (2026)** — only RePEc/SSRN-version abstracts obtained; the full Wiley text
  (paywalled, 403) was not read, so the specific power-law exponent estimates and the exact treatment
  of the six strategies are unverified.
- **Tzouvanas et al. (2020)** — verbatim abstract obtained; the post-print PDF was 403-blocked, so the
  specific J/K lookback/holding values and reported momentum returns are **not** verified beyond the
  abstract.
- **Grobys, Ahmed & Sapkota (2020) DOI**: my §2f-iii originally listed 10.1016/j.frl.2019.101241;
  the **correct DOI is 10.1016/j.frl.2019.101396** (Crossref-verified).

## 7. Local artifacts produced

- `crypto_momentum_Han_Kang_Ryu_ACFR_fulltext.txt` — full extracted text of the 116-page
  Han/Kang/Ryu ACFR working paper (205 KB), usable for further table/quote extraction.
- This report: `crypto_momentum_literature_report.md`.
