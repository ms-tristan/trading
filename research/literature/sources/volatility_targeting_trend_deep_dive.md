# Literature Review: Volatility Targeting, Trend Following & Crypto

Compiled for quantitative crypto trading platform research. Every citation below was verified
via DOI resolution (Crossref), a primary author/repository PDF, or both. Unverified items are
explicitly flagged in the final section. Retrieval caveats are noted inline.

---

## 1. VOLATILITY TARGETING

### 1.1 Harvey, Hoyle, Korgaonkar, Rattray, Sargaison & Van Hemert (2018) — FOR

**Citation (verified):** Harvey, C.R., Hoyle, E., Korgaonkar, R., Rattray, S., Sargaison, M.,
Van Hemert, O. (2018). "The Impact of Volatility Targeting." *The Journal of Portfolio
Management*, 45(1), Fall 2018, 14–33. DOI: [10.3905/jpm.2018.45.1.014](https://doi.org/10.3905/jpm.2018.45.1.014)

**Working URL (verified, full text PDF read directly):**
<https://people.duke.edu/~charvey/Research/Published_Papers/P135_The_impact_of.pdf>
(linked from Harvey's own CV at <https://people.duke.edu/~charvey/vitae.htm>, entry [P135])
SSRN abstract page: <https://ssrn.com/abstract=3175538> — note SSRN returns **HTTP 403 to
automated fetch**; the Duke PDF is the reliable route. Confirmed authorship and venue from the
PDF's own header: "2018, 45 (1) 14-33 JPM", "http://jpm.iijournals.com/content/45/1/14".

**Methodology (from the paper):** Leverage the portfolio up when volatility is low, scale down
when volatility is high — targeting a *constant level of volatility* rather than constant
notional exposure. Volatility is estimated with exponentially weighted moving average
(EWMA) weights, with the half-life varied across {10, 20, 40, 60, 90} days as a robustness
grid. Unscaled and scaled returns are both renormalized to the same 10% full-sample realized
volatility so that Sharpe ratios are directly comparable. Measured at a rolling one-month
(30 calendar day / 21 weekday) evaluation frequency. They also test a rolling three-month
(90-day) evaluation frequency and report "very similar results."

**Asset classes tested:** "more than 60 assets" — U.S. equities (CRSP, from July 1 1926),
U.S. bonds, 50 futures/forwards across global equity indices, fixed income, currencies (all
vs USD) and commodities, plus credit. Portfolio level: a 60/40 equity–bond balanced portfolio
and a risk-parity (equity–bond–credit–commodity) portfolio. Equity intraday tests use S&P 500
futures 5-minute bars from 1988.

**Sample period:** Equities from 1926 (daily data), performance stats reported 1927–2017.
Bonds 1926–2017 (proxy daily before 1963 using daily yield data; 1963+ actual). Treasury
futures 1988–2017.

**Key reported numbers:**
- **Equity Sharpe ratio improves from 0.40 unscaled to between 0.48 and 0.51 volatility-scaled**,
  and is "not very sensitive to the choice of volatility estimate" (half-life).
- **Statistical significance: intercept of 0.64 bp with a t-stat of 3.05** (Newey–West corrected,
  30 lags), from regressing volatility-scaled daily returns (20-day half-life) on unscaled
  returns. R² = 0.73. This is the headline significance test.
- Gross vs net Sharpe are identical at reported precision. Footnote 16 gives the cost detail:
  for a 10-day half-life, costs ≈ 2 × 1bp × 71% × 4.66 = 0.066%/yr for a 10% vol strategy;
  unrounded gross and net Sharpe are 0.4831 and 0.4766 — both round to 0.48.
- Vol of vol improves from **4.6% unscaled to 1.8% volatility-scaled** (20-day half-life).
- **Subsample robustness — an important caveat:** "The Sharpe ratio improves in all cases,
  **except during the 1957–1987 subsample period**." Vol of vol and mean shortfall still
  improve consistently in all subsamples.
- **Equity quintile sort:** mean return shows "no clear pattern" across previous-month
  volatility quintiles, while volatility is strongly persistent — i.e., expected returns do
  not compensate for predictable volatility changes.
- **Bonds:** Volatility scaling **decreases** the bond Sharpe ratio over part of the sample;
  "The underperformance of the volatility-scaled investment is solely due to the pre-1980
  period" (specifically high/volatile inflation 1964–1980). Over 1988–2017 (10-yr Treasury
  futures, daily and intraday) the Sharpe ratio and left tail are "similar"; only vol of vol
  clearly improves.
- **Futures/forwards/credit:** "The Sharpe ratio improves slightly for equity indexes and
  credit when using volatility scaling, but it is similar for other assets." For bonds,
  currencies and commodities the effect on realized Sharpe is **negligible**.

**Authors' own mechanistic admission (relevant skepticism, stated by the authors themselves):**
Risk assets exhibit a leverage effect, so "volatility scaling effectively introduces some
momentum into strategies" — positions get cut when volatility rises alongside negative
returns, the same direction as a time-series momentum strategy. They attribute part of the
gain to this mechanism, and note the strategy historically performed well (Hamill, Rattray &
Van Hemert 2016).

---

### 1.2 EVIDENCE AGAINST volatility targeting

#### (a) Moreira & Muir (2017) — the original claim (FOR)

**Citation (verified):** Moreira, A. & Muir, T. (2017). "Volatility-Managed Portfolios."
*The Journal of Finance*, 72(4), 1611–1644. DOI: [10.1111/jofi.12513](https://doi.org/10.1111/jofi.12513)
CrossRef confirms vol 72, issue 4, pp. 1611–1644, dated 2017-05-15.

**Working URL (verified, full text read):** NBER Working Paper 22208 (April 2016, revised
June 2016) — <https://www.nber.org/papers/w22208> ; PDF:
<https://www.nber.org/system/files/working_papers/w22208/w22208.pdf>
Also: <https://doi.org/10.3386/w22208>. (SSRN/publisher pages are paywalled/403.)

**Exact scaling formula (equation 1 in the paper):**

  f̃_{t+1} = (c / σ̂²_t(f)) · f_{t+1}

where f_{t+1} is the buy-and-hold portfolio excess return, σ̂²_t(f) is a proxy for the
portfolio's conditional variance, and the constant c controls average exposure. **c is chosen
so that the managed portfolio has the same unconditional standard deviation as the
buy-and-hold portfolio.** In the main results the conditional variance proxy is simply the
**previous month's realized variance**:

  σ̂²_t(f) = RV²_t(f) = (1/22) · Σ_{d=1}^{22} f²_{t+d−...}

(i.e. trailing ~22 daily observations of squared returns). Extensions tested: inverse
*volatility* instead of variance, expected rather than realized variance.

**Reported alphas (paper's own figures):**
- **Market portfolio: annualized alpha 4.9% (4.86% stated in the text), appraisal ratio 0.33,
  and a 25% increase in the buy-and-hold Sharpe ratio.** Scaled market beta is only 0.6.
- Appraisal ratios (α/σ_ε): **Market 0.34, HML 0.20, Profitability 0.41, Carry 0.44,
  ROE 0.80, Investment 0.32, Momentum ≈ 0.875** (α ≈ 12.5 on RMSE ≈ 50, annualized).
- Factors covered: market, value, momentum, profitability, ROE, investment (equities) plus
  the currency carry trade. Sample for the market figure: 1926–2015.
- Mechanism: variance is highly forecastable at short horizons while variance forecasts are
  only weakly related to future returns, so scaling down in high-variance months does not
  proportionally reduce returns.
- The authors explicitly argue the payoff is **not** option-like: "our strategy works by
  shifting when it takes market risk and not by loading on extreme market realizations as
  profitable option strategies typically do." Losses are concentrated in low-volatility
  periods (e.g. the 1960s) rather than in crashes.
- New Sharpe relation: SR_new = sqrt( SR²_old + (α/σ_ε)² ).

#### (b) Cederburg, O'Doherty, Wang & Yan (2020) — the major replication failure

**Citation (verified via Crossref):** Cederburg, S., O'Doherty, M.S., Wang, F., Yan, X. (2020).
"On the performance of volatility-managed portfolios." *Journal of Financial Economics*,
**138(1), 95–117**, October 2020. DOI: [10.1016/j.jfineco.2020.04.015](https://doi.org/10.1016/j.jfineco.2020.04.015)

**URL status — flagged honestly:** This is a **closed-access** paper. Unpaywall reports
`is_oa: False`; OpenAlex reports `oa_status: closed`, `oa_url: None`,
`any_repository_has_fulltext: False`. The publisher record is
<https://www.sciencedirect.com/science/article/abs/pii/S0304405X2030132X> (403 to automated
fetch). The University of Arizona repository copy at
`https://repository.arizona.edu/bitstream/handle/10150/648508/COWY%20Manuscript.pdf`
returns **HTTP 403 to non-browser clients** — I could not retrieve the full text. The URL that
appears in search results as a mirror (`http://gljc.sxu.edu.cn/docs/2022-01/b3783ddbf6e24abb9a4999880496224c.pdf`)
turned out to be a **Chinese-language lecture slide deck about the papers, not the paper** —
I did NOT rely on it as a source. **The numbers below are therefore drawn from two independent
peer-reviewed/academic secondary sources that quote Cederburg et al.'s results, not from the
paper's own text.** They should be treated as well-corroborated but second-hand.

**What they found (corroborated by two independent sources):**

From DeMiguel, Martín-Utrera & Uppal (2024), *Journal of Finance* (open access, full text read
— see §1.3): "Cederburg et al. (2020) show that the performance gains from volatility
management are not achievable out-of-sample **because of estimation error**."

From an 81-page University of Helsinki thesis (full text read):
<https://helda.helsinki.fi/server/api/core/bitstreams/d578606d-5309-4ca6-a8e6-46ca45cb1816/content>
- **Scope: 103 equity strategies** — the 9 factors studied by Moreira & Muir plus 94 additional
  anomaly portfolios (from Hou, Xue & Zhang 2015 and McLean & Pontiff 2016).
- **Sample: July 1926 – December 2016**; NYSE/Amex/Nasdaq common stocks, excluding financial
  firms, sub-first-NYSE-decile market cap, and share price < $5.
- **They DO confirm Moreira & Muir's spanning-regression result**: volatility-managed
  portfolios "often generate positive alphas in spanning regressions," and this "holds in a
  broad sample of 103 strategies." So the in-sample alpha is real, not a fluke.
- **But direct performance comparisons are much less supportive**: volatility-managed
  portfolios "do not systematically outperform unmanaged portfolios in direct comparisons,"
  and reasonable out-of-sample versions "generally earn **lower certainty-equivalent returns
  and Sharpe ratios** than simple investments in the original unmanaged portfolios."
- **Headline number: "In their base case, the real-time combination of managed and unmanaged
  portfolios earns a lower certainty-equivalent return in 72 of 103 cases."**
- **Attribution: "structural instability in the spanning regressions parameters"** — i.e., the
  in-sample alpha estimates do not persist. Their conclusion: positive in-sample alpha
  estimates should not automatically be read as evidence the managed strategy is a better
  real-world investment.

Corroborated independently by Xu (2024), *Critical Finance Review* forthcoming (full text read
— <https://cfr.ivo-welch.org/forthcoming/papers/xu2024improving.pdf>):
- "Liu et al. (2019) and Cederburg et al. (2020) argue that Moreira and Muir (2017)'s
  specification of variance scaling coefficient suffers from a **look-ahead bias**.
  **Calibrating this coefficient in real time relegates volatility-managed portfolios to
  underperformance.**"
- "Using a broad set of **103 trading anomalies**, Cederburg et al. (2020) show that the
  significant in-sample benefits of volatility management **hardly translate into out-of-sample
  gains**."
- Xu's own out-of-sample replication: "we find that the plain strategy **weakly** improves
  out-of-sample performance: **only the managed MOM and ROE factors witness a statistically
  significant Sharpe ratio increase**." The managed ROE factor has the largest increase of
  **0.38 (t = 2.89**, Ledoit-Wolf HAC Sharpe test). **SMB is "a notable failure"** — Sharpe
  ratio reduction of about **40% (0.09/0.22)**, Sortino of only 0.20.
- Xu also states that Cederburg et al.'s *in-sample* plain strategy "only achieves **four
  significant Sharpe ratio increases**" (citing Cederburg et al. 2020, Table 1).

#### (c) Transaction costs: Barroso & Detzel (2021)

**Citation (verified via Crossref):** Barroso, P. & Detzel, A. (2021). "Do limits to
arbitrage explain the benefits of volatility-managed portfolios?" *Journal of Financial
Economics*, **140(3), 744–767**, June 2021. DOI: [10.1016/j.jfineco.2021.02.009](https://doi.org/10.1016/j.jfineco.2021.02.009)
(SSRN working version: [10.2139/ssrn.3088828](https://doi.org/10.2139/ssrn.3088828))

**Findings (via DeMiguel et al. 2024 abstract and the Helsinki thesis):** "Barroso and Detzel
show they do not survive transaction costs." Specifically: "even using **six cost-mitigation
strategies**, volatility management of asset-pricing factors **other than the market**
generally produces **zero abnormal returns after transaction costs** and **significantly
reduces Sharpe ratios**." Gains from volatility management are concentrated in stocks with
the **lowest limits to arbitrage** (for the managed market portfolio).

#### (d) Liu, Tang & Zhou (2019) — look-ahead bias

**Citation (verified via Crossref):** Liu, F., Tang, X. & Zhou, G. (2019). "Volatility-Managed
Portfolio: *Does It Really Work?*" *The Journal of Portfolio Management*, **46(1), 38–51**.
DOI: [10.3905/jpm.2019.1.107](https://doi.org/10.3905/jpm.2019.1.107)
(SSRN: [10.2139/ssrn.3283395](https://doi.org/10.2139/ssrn.3283395))

**Findings:** The variance-scaling coefficient (the constant c that equates unconditional
variances) is set using full-sample information — a look-ahead bias. Calibrating it in real
time removes the out-of-sample benefit. Also cited by DeMiguel et al. (2024): "Liu, Tang, and
Zhou (2019) show that the out-of-sample Sharpe ratio of the managed factor is [lower]…the
scaling parameter c does not affect the out-of-sample performance."

#### (e) The strongest counter-rebuttal (important balance)

**Citation (verified, open access, full text read):** DeMiguel, V., Martín-Utrera, A., Uppal, R.
(2024). "A Multifactor Perspective on Volatility-Managed Portfolios." *The Journal of Finance*,
published October 2024. DOI: [10.1111/jofi.13395](https://doi.org/10.1111/jofi.13395)
**Open-access PDF:** <https://lbsresearch.london.edu/id/eprint/3716/1/The%20Journal%20of%20Finance%20-%202024%20-%20DeMIGUEL%20-%20A%20Multifactor%20Perspective%20on%20Volatility%E2%80%90Managed%20Portfolios.pdf>

Their abstract directly recaps the debate: "Moreira and Muir question the existence of a strong
risk-return trade-off… However, **Cederburg et al. show that these strategies fail
out-of-sample, and Barroso and Detzel show they do not survive transaction costs.** We propose
a conditional multifactor portfolio that outperforms its unconditional counterpart even
out-of-sample and net of costs."

Their own numbers (Tables, out-of-sample period January 1977 – December 2020, expanding-window
estimation using the first 120 months, 9 factors):
- **Panel C (out-of-sample, ignoring costs):** the out-of-sample Sharpe ratio of the
  volatility-managed individual-factor portfolios is **lower than the in-sample Sharpe ratio
  for all nine factors**. The optimal combination of unmanaged + managed factors delivers a
  Sharpe ratio that can be **smaller than even the unmanaged factor alone** — true for **MKT,
  SMB and CMA**. Out-of-sample gains are statistically significant at the 10% level for only
  **four of nine factors (UMD, ROE, IA, BAB)**.
- **Panel D (out-of-sample, net of costs, ignoring trading diversification):** "the Sharpe
  ratio for **five of the nine** volatility-managed individual-factor portfolios becomes
  **negative**." The Sharpe ratio of the optimal unmanaged+managed combination is lower than
  the unmanaged factor for **all factors except UMD and BAB, with neither being statistically
  significant.**
- Interpretation: estimation error and transaction costs do not explain away the gains from
  *multifactor* volatility management, so the breakdown of the risk-return trade-off is "more
  puzzling than previously thought."

**Bottom line on §1:** The effect is real *in-sample* and in *spanning regressions* (all sides
agree), but it does **not** reliably survive (i) real-time calibration of the scaling constant,
(ii) out-of-sample estimation error, or (iii) transaction costs at the single-factor level. It
survives most convincingly for the market factor and for momentum/ROE. For a crypto trading
platform, the realistic expectation is: **use vol targeting for risk/drawdown control, not as
a documented source of out-of-sample Sharpe improvement.**

---

## 2. VOLATILITY TARGETING IN CRYPTO SPECIFICALLY

There is **no** peer-reviewed replication of Harvey et al. or Moreira-Muir on crypto that I
could verify. What exists:

**(a) Zarattini, Pagani & Barbon (2025)** — the single most directly relevant study. It applies
trend following to crypto **with explicit volatility-based position sizing**.
**Citation (verified via RePEc/IDEAS):** "Catching Crypto Trends; A Tactical Approach for
Bitcoin and Altcoins," Swiss Finance Institute Research Paper Series **25-80** (2025).
Handle: RePEc:chf:rpseri:rp2580. Authors: Carlo Zarattini (Concretum Group), Alberto Pagani
(University of Parma), Andrea Barbon (University of St. Gallen / Swiss Finance Institute).
- RePEc record: <https://ideas.repec.org/p/chf/rpseri/rp2580.html>
- Author page (verified): <https://abarbon.com/papers/catching-crypto-trends>
- **Full-text PDF (verified, read directly):** <https://concretumgroup.com/wp-content/uploads/2026/02/Catching-Crypto-Trends.pdf>
- SSRN (per publisher link, 403 to automated fetch): <https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907>
- Last revision 2025-04-09.

Details in §4 below. The position-sizing rule is an explicit volatility target:
`w_t^n = min(0.25, .../σ_t)` — a volatility-scaled weight capped at 25%.

**(b) Man Group / Man AHL "Crypto: Too Hot to Handle?"** (21 March 2025) — a practitioner
white paper applying risk analysis to crypto, referenced in search results at
<https://www.man.com/documents/download/85f06-c1e79-1f994-529f6/Man_AHL_Analysis_Crypto._Too_Hot_to_Handle%3F_English_21-03-2025.pdf>
**I did not retrieve or read this document's contents, so I make no claims about its findings.**
Listed only as a lead to follow.

**Honest gap statement:** I could not find a verifiable academic paper that isolates the
*Sharpe-ratio improvement from volatility targeting specifically* (as opposed to trend
following or risk parity) on BTC/crypto with a stated sample and significance test. Treat
crypto vol targeting as **untested in the published literature** as of this review.

---

## 3. TREND FOLLOWING / MANAGED FUTURES LITERATURE

### 3.1 Hurst, Ooi & Pedersen (2017) — "A Century of Evidence…"

**Citation (verified):** Hurst, B., Ooi, Y.H., Pedersen, L.H. (2017). "A Century of Evidence
on Trend-Following Investing." *The Journal of Portfolio Management*, Fall 2017.
DOI: [10.2139/ssrn.2993026](https://doi.org/10.2139/ssrn.2993026) (SSRN version).

**Working URLs (verified, full text PDF downloaded and read):**
- AQR page: <https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing>
- **Direct PDF:** <https://www.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/AQR-JPM-Fall-2017.pdf>
  (dated October 31, 2017; confirmed 16 pages, JPM Fall 2017 running heads "JPM-Hurst.indd".)

**Sample period:** **January 1880 – December 2016** (137 years / "110 years" of the strategy's
history 1880–1990 era is referenced in the abstract wording; the strategy is simulated back to
1880 and run to end-2016).

**Methodology:** The basic strategy is **time series momentum** — long markets with recent
positive returns, short those with recent negative returns. **Critically, the signal is a
multi-horizon ensemble: they explicitly report performance for signals "based on the past
1-month, 3-month, and 12-month trend, respectively" (Exhibit 2), including lagged-by-one-month
variants.** Universe: **67 markets** across equity index futures, fixed income, commodity
futures and currency forwards.

**Findings:**
- **"The strategy has delivered positive average returns in each market, with an average Sharpe
  ratio of approximately 0.4"** across the 67 markets, 1880–2016 (gross of fees and costs;
  Exhibit 3 is explicitly the "annualized gross Sharpe ratio"). Positive returns in **each
  decade** of the 137-year sample.
- Out-of-sample relative to Moskowitz-Ooi-Pedersen (2012, data starting 1985): they report the
  pre-1985 subsample **1880–1984** for markets with ≥10 years of data, showing "remarkably
  consistent performance across markets and asset classes."
- **Crisis performance ("the smile"):** annual strategy returns plotted vs U.S. equity returns
  1880–2016 show trend following performs best in *extreme* up and down equity years. The
  strategy had **positive returns in 8 out of 10 of the largest peak-to-trough drawdowns of a
  60/40 portfolio**, and its 10 largest drawdowns 1880–2016 were approximately [figure in
  Exhibit 8]. Correlations to stocks and bonds were low over the full period and in each decade.
- The paper also reports performance "before and after simulated transaction costs, and gross
  and net of hypothetical 2-and-20 fees" (Exhibit 2 note).

### 3.2 Baltas & Kosowski (2013) — "Momentum Strategies in Futures Markets and Trend-following Funds"

**Citation (verified via Crossref):** Baltas, N. & Kosowski, R. "Momentum Strategies in Futures
Markets and Trend-following Funds." DOI: [10.2139/ssrn.1968996](https://doi.org/10.2139/ssrn.1968996)
Note: the Crossref record dates this SSRN entry to 2011. The version I read and cite in detail
below is the working-paper version titled **"Trend-following and Momentum Strategies in Futures
Markets," dated December 10, 2011** (the 2013-dated version circulates under the
"Momentum Strategies in Futures Markets and Trend-following Funds" title). Users should cite
both titles/versions carefully — I could **not** independently confirm a 2013 publication venue.

**Working URL (verified, full text read):**
<http://www.efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2012-Barcelona/papers/EFMA2012_0485_fullpaper.pdf>
Also an Oxford-Man Institute copy: <https://www.oxford-man.ox.ac.uk/wp-content/uploads/2020/04/BK_MOMF_beamer_2012_05_21.pdf>
Semantic Scholar landing: <https://www.semanticscholar.org/paper/98128dbb0752e2181699ea966ea90aa8af442d94>

**Sample:** 12 futures contracts, **intra-day quotes, November 1999 – October 2009** (10 years).

**Key findings — highly relevant to the multi-horizon question:**
- They compare momentum **trading signals**, including (i) the traditional `SIGN` signal (sign of
  past return) and (ii) `SMT`, a signal from **fitting a linear trend on the asset price path**
  (least-squares slope).
- **"The trend-related (SMT) signal offers the best out-of-sample momentum performance in terms
  of mean return, dollar growth and Sharpe ratio across all frequencies of portfolio
  rebalancing, all lookback and holding periods."**
- **Quantitatively: the time-series momentum strategy with a 6-month lookback and 1-month
  holding period generates 28.36% annualized mean return using the SMT signal vs 15.97% for the
  traditional SIGN signal; both strongly significant at the 1% level.** $1 grows to **$11.1
  (SMT) vs $4 (SIGN)**.
- **"The difference in the ex-post Sharpe ratio is not so pronounced (1.18 versus 1.00)"** —
  because the SMT signal has higher ex-post volatility due to sparse trading. Downside-risk
  Sharpe ratio (Ziemba 2005) is **1.83 (SMT) vs 1.38 (SIGN)**.
- **Turnover "more than halved"** using the SMT signal — a direct transaction-cost advantage.
- Weekly frequency (3-week lookback, 1-week holding): 19.99% annualized, $5.60 growth,
  DR-SR 0.95 (SMT) vs 10.56%, $2.49, 0.70 (SIGN).
- **Important negative result:** at the **daily** frequency momentum patterns are weak, and the
  3-day lookback / 1-day holding strategy shows **statistically significant and economically
  important REVERSAL effects** — it "loses on average 13.38% annualized" and $1 shrinks to
  $0.21 using the SMT signal. **Significant reversal at the very short-term horizon.**
- Volatility estimator choice matters: the **Yang-Zhang range estimator** is optimal for
  maximizing efficiency and minimizing bias and ex-post turnover.

### 3.3 Multi-horizon trend ensembles — evidence that combining lookbacks is more robust

**(a) Hurst, Ooi & Pedersen (2017)** (above) is itself core evidence: Exhibit 2 reports the
1-month, 3-month and 12-month signals separately, and the strategy aggregates them.

**(b) Benhamou, Ohana, Etienne, Guez, Setrouk & Jacquot (2025)** — "Re-evaluating Short- and
Long-Term Trend Factors in CTA Replication: A Bayesian Graphical Approach." arXiv:2507.15876.
**URL (verified, full text read):** <https://ar5iv.labs.arxiv.org/html/2507.15876>
(via <https://arxiv.org/abs/2507.15876>)
- Universe: 24 liquid futures (equity indices, government bonds, FX, commodities).
- **Sample: 2010–2025.** Costs included: "even after realistic transaction and roll costs."
- **Explicit closed-form result (Proposition 1)** for the Sharpe-optimal blend of a short-term
  and long-term trend factor with correlation ρ:
  `ω*_ST = (μ_ST − ρ·μ_LT) / ((μ_ST + μ_LT)(1 − ρ))`
  with the diversification variance reduction `Var(P) = 1 + 2(ρ−1)ω_ST·ω_LT`.
- **Applied to their data: ω*_ST ≈ 17%, ω*_LT ≈ 83%** — i.e. the Sharpe-optimal blend should
  lean roughly **5:1 toward the long-term** trend factor, which is a genuinely useful and
  non-obvious constraint on naive equal-weight multi-horizon ensembles.
- Short-term bucket = {10, 20, 40, 60}-day windows; long-term = 500-day. Composite factor is the
  **equal-weighted mean across horizons**: `F_trend = (1/|N|) Σ_{n∈N} T_{t,n}`.
- They document that "a short-term-trend + raw-beta sleeve outperforms classic multi-month
  breakouts on both Sharpe and drawdown efficiency over 2010–2025."
- Their literature review notes the debate is genuinely unresolved: Moskowitz et al. (2012) and
  Baltas-Kosowski (2013) "document sizable profits from 1–12-month trends, whereas Jegadeesh
  et al. (2022) highlight the fragility of ultra-short signals"; other work emphasizes the
  benefit of multi-horizon mixes (Baz et al. 2015; Baltas & Kosowski 2020).

**(c) Zarattini, Pagani & Barbon (2025)** — see §4; a 9-horizon Donchian ensemble on crypto.

**Skeptical note:** the "multi-horizon is more robust" claim is **asserted and separately
supported by each of these papers, but I did not find a study that directly A/B tests a single
crossover against a multi-horizon ensemble with the same data and cost model in a controlled
head-to-head significance test.** The Donchian results in §4 (Table 1) are the closest thing:
there, the 9-horizon Combo ensemble (Sharpe 1.58, Sortino 2.03, MDD 19%) had a *lower* Sharpe
than the best single short lookback (5d: Sharpe 1.66; 20d: 1.60) but a materially better
**Sortino (2.03 vs 1.87) and Max Drawdown (19% vs 25%)**. So the ensemble's documented edge is
in **tail/drawdown efficiency, not raw Sharpe**. This is an important nuance and cuts against
the common claim that ensembles strictly dominate on Sharpe.

### 3.4 Faber (2007) — "A Quantitative Approach to Tactical Asset Allocation"

**Citation (verified via Crossref):** Faber, M.T. (2007). "A Quantitative Approach to Tactical
Asset Allocation." *The Journal of Wealth Management*, **9(4), 69–79** (dated 2007-01-31).
DOI: [10.3905/jwm.2007.674809](https://doi.org/10.3905/jwm.2007.674809)
**Working URL (verified, full text PDF downloaded and read):**
<https://community.portfolio123.com/uploads/short-url/kbsA05k7XScAkgohfEGeGkwoOD3.pdf>
(the paper itself references its SSRN availability; SSRN abstract_id 962461 is the commonly
cited ID). 47 pages.

**The 10-month SMA rule (exact, from the paper):**
- "Buy when monthly price > 10-month SMA."
- "Sell and move to cash when monthly price < 10-month SMA."
The 10-month SMA is defined as the month-end prices of the last 10 months summed and divided
by 10. Tested on the S&P 500 back to **1900**, and in an asset-allocation framework from
**1973** (results begin 1973 to accommodate longer moving averages).

**Findings:**
- S&P 500 average return since 1900 was **11.20% vs 11.49% for the timing system**; **compounded
  returns 9.21% vs 10.45%.** Buy-and-hold loses **199 bp** to volatility; timing loses 104 bp.
- Reduced the largest drawdown from a catastrophic **83.66% to 42.24%** (1929-era bear market).
- In the 2000s episode: the timing model exited in **October 2000**, avoiding two of three
  consecutive down years, with a **16.52% drawdown vs 44.73%** for buy-and-hold. It exited again
  on **December 31, 2007**, avoiding the entire 2008 bear market.
- Invested roughly **70% of the time**, with **less than one round-trip trade per year**.
- **"The timing system achieves these superior results while underperforming the index in
  roughly half of all years since 1900."**
- **Stated exclusions (critical): "Taxes, commissions, and slippage are excluded."**
- Asset allocation 1973–2008: "equity-like returns with bond-like volatility and drawdown."

**CRITICISM of Faber (verified):**

**(i) Marmi, Pacati, Renò & Risso — peer-reviewed critique.**
**Citation (verified via two Crossref records):**
- Published version: Marmi, S., Pacati, C., Renò, R., Risso, W.A. (2013). "A quantitative
  approach to Faber's tactical asset allocation." *International Journal of Computational
  Economics and Econometrics*, **3(1/2), 91** (pp. 91–101).
  DOI: [10.1504/IJCEE.2013.056268](https://doi.org/10.1504/IJCEE.2013.056268)
- SSRN version: "A Quantitative Approach to Faber's Tactical Asset Allocation," 2012,
  DOI: [10.2139/ssrn.1476225](https://doi.org/10.2139/ssrn.1476225)
- Semantic Scholar: <https://www.semanticscholar.org/paper/cbb301ed85816338bcbe791e5f947c087c5f3da6>

**Their finding (from the published abstract, quoted):** "Routinely, practitioners and academics
alike propose the use of trading strategies with an alleged improvement on the risk-return
relation… A very popular example is 'A quantitative approach to tactical asset allocation' by
the fund manager M. Faber, a real hit in the SSRN online library. **Is this paper a counterexample
to market efficiency? We reject this conclusion, showing that a lot of caution should be used in
this field**, and we indicate a series of **bootstrapping experiments** which can be easily
implemented to evaluate the performance of trading strategies."
*Access note: I read the abstract and citation metadata via Crossref/Inderscience/Semantic
Scholar; the full text is paywalled and I did not read it, so I do not quote specific
bootstrapped p-values.*

**(ii) Faber's own out-of-sample admission — the strongest criticism, from the author.**
**Citation (verified via Crossref, full text read):** Faber, M. (2018). "A Quantitative Approach
to Tactical Asset Allocation Revisited 10 Years Later." *The Journal of Portfolio Management*,
Vol. 44, No. 2 (2018 Multi-Asset Special Issue), **156–167** (dated 2017-12-22).
DOI: [10.3905/jpm.2018.44.2.156](https://doi.org/10.3905/jpm.2018.44.2.156)
URL read: <https://www.pm-research.com/content/iijpracapp/6/1> (Practical Applications summary,
4 pages).
**Key admission, quoted:** "Assessed on an annual basis, his method wound up substantially
outperforming the market in **2008–2009, but then trailed the market over the following eight
years.**" He reaffirms the primary goal is "to sharply reduce volatility and the impact of
drawdowns" rather than to beat the market, and notes underperformance "over the short term,
especially in choppy markets." The paper also notes the original 2006 paper was downloaded
~200,000 times on SSRN.
**This is direct evidence that the 10-month SMA rule's headline outperformance is
concentrated in the 2000–2002 and 2007–2009 bear markets**, consistent with the critique the
user asked about. I did not find a paper that proves the *entire* result is an artifact of
2000–2008, but the concentration of the benefit in those episodes is documented by the author.

---

## 4. TREND FOLLOWING IN CRYPTO SPECIFICALLY

### 4.1 Zarattini, Pagani & Barbon (2025) — the primary study

**Citation + URLs: see §2(a).** Full-text PDF explicitly at
<https://concretumgroup.com/wp-content/uploads/2026/02/Catching-Crypto-Trends.pdf>
(Swiss Finance Institute Research Paper 25-80.)

**Methodology:**
- **Multi-horizon ensemble**: aggregates **nine Donchian Channel models** with lookbacks
  **5, 10, 20, 30, 60, 90, 150, 250, 360 days** into a single "Combo" signal (equal aggregation).
- **Volatility-based position sizing** (vol targeting), `w_t^n = min(0.25, …/σ_t)` capped at 25%.
- Trend signal: long when close breaks the upper Donchian boundary; exit on close below the
  midpoint; initial trailing stop = midpoint of the Donchian Channel.
- Universe: **survivorship-bias-free dataset of all cryptocurrencies traded since 2015**;
  portfolio = rotational **top 20 most liquid coins**, rebalanced monthly, selected on trailing
  30-day volume; liquidity exit rules (median daily volume > $1M; drop illiquid/compromised
  assets).
- **Sample: January 1, 2015 – March 19, 2025.**

**Which lookbacks work — explicit table (Table 1, BTC, transaction costs NOT included):**

| Model | CAGR | Vol | Sharpe | Sortino | MDD | MAR | Alpha | Beta |
|-------|------|-----|--------|---------|-----|-----|-------|------|
| 5d    | 36%  | 19% | **1.66** | 1.87 | 25% | 1.41 | 19% | 0.16 |
| 10d   | 32%  | 18% | 1.55 | 1.64 | 27% | 1.19 | 18% | 0.15 |
| 20d   | 34%  | 18% | 1.60 | 1.60 | 26% | 1.32 | 19% | 0.16 |
| 30d   | 34%  | 19% | 1.61 | 1.61 | 24% | 1.41 | 19% | 0.16 |
| 60d   | 28%  | 19% | 1.30 | 1.25 | 19% | 1.46 | 13% | 0.17 |
| 90d   | 27%  | 20% | 1.20 | 1.15 | 24% | 1.12 | 11% | 0.18 |
| 150d  | 21%  | 20% | **0.99** | 0.97 | 29% | 0.74 | 7%  | 0.19 |
| 250d  | 25%  | 20% | 1.13 | 1.15 | 33% | 0.76 | 9%  | 0.20 |
| 360d  | 29%  | 20% | 1.28 | 1.27 | 34% | 0.83 | 12% | 0.18 |
| **Combo** | **30%** | **17%** | **1.58** | **2.03** | **19%** | 0.88 | **14%** | 0.17 |

- **Short lookbacks (5–30 days) dominate on Sharpe (~1.55–1.66); 150-day is the worst (0.99).**
  Alpha statistically significant at 2.5% level for all models **except the 150-day and
  250-day**.
- **The 9-horizon Combo wins on Sortino (2.03) and Max Drawdown (19% vs >80% for passive BTC)**,
  with Sharpe 1.58 — slightly *below* the best single short lookbacks. Authors state the Combo
  "offered a solid overall profile."
- Passive Bitcoin Max Drawdown in this sample: **>80%**; the Combo's is **19%**, and the 5–30d
  models are "roughly one-third" of passive.

**Fees — explicitly addressed, and this is the important part:**
- For the BTC single-asset results, **transaction costs are NOT included** in Table 1.
- **The diversified crypto portfolio results DO include costs:** "a 10 basis point transaction
  cost and a 20% rebalance threshold."
- **Headline net-of-fees result: "A diversified strategy targeting the top 20 most actively
  traded cryptocurrencies would have produced net-of-fees Sharpe ratio 1.57, a maximum drawdown
  of just 11%, and a statistically significant alpha of 11% relative to Bitcoin."**
  (CAGR 443%, vol 18%, Sortino 1.97 — for the 20-asset portfolio.)
- **Cost sensitivity analysis (Figure 3): they test 0, 10, 25 and 50 bps** — "These values exceed
  the typical fee structure" — and a 20% rebalance threshold to damp turnover. They propose a
  portfolio technique to mitigate transaction costs.
- Breadth: Sharpe is "remarkably stable" around **1.5** for portfolios from 5 to 50 assets;
  performance improves with diversification **up to ~20 assets**, then marginal benefits
  diminish. Concentrated 2-asset portfolios show the highest Sharpe but are "not desirable in
  practice" (idiosyncratic risk).

### 4.2 Supporting academic crypto momentum evidence

**(a) Liu & Tsyvinski (2021)** — "Risks and Returns of Cryptocurrency." *The Review of Financial
Studies*, **34(6), 2689–2727**. DOI: [10.1093/rfs/hhaa113](https://doi.org/10.1093/rfs/hhaa113)
NBER WP 24877 (Aug 2018): <https://www.nber.org/papers/w24877> ;
DOI [10.3386/w24877](https://doi.org/10.3386/w24877)
**Verified finding (NBER abstract, quoted):** "we determine that there is a **strong time-series
momentum effect** and that proxies for investor attention strongly forecast cryptocurrency
returns." Assets: Bitcoin, Ripple, Ethereum. Crypto returns have **no exposure to most common
stock market and macroeconomic factors**, and no exposure to currency/commodity returns. This is
the canonical academic reference for crypto time-series momentum.

**(b) Liu, Tsyvinski & Wu (2019/2022)** — "Common Risk Factors in Cryptocurrency." NBER WP 25882,
DOI [10.3386/w25882](https://doi.org/10.3386/w25882). Published in *Journal of Finance* (2022).
Establishes market, size and momentum factors in crypto.

**(c) Borgards, O. (2021)** — "Dynamic time series momentum of cryptocurrencies." *The North
American Journal of Economics and Finance*, **57, 101428** (July 2021).
DOI: [10.1016/j.najef.2021.101428](https://doi.org/10.1016/j.najef.2021.101428)
**Verified metadata and venue only — I could NOT retrieve the abstract or full text** (Elsevier
paywall; Crossref record carries no abstract). **I therefore make no claims about its specific
findings or numbers.** Flagged as a lead worth obtaining.

**(d) Le, T. & Ruthbah, U. (n.d.)** — "Trend-following Strategies for Crypto Investors,"
Monash University working paper.
URL: <https://www.monash.edu/__data/assets/pdf_file/0011/3744821/Trend-following-Strategies-for-Crypto-Investors.pdf>
**I could NOT retrieve this document — Monash's server is behind Cloudflare and returned
HTTP 403 / "Just a moment..." to both curl and the fetch tool.** Authors identified only from
a search-result snippet ("Trinh Le and Ummul Ruthbah, August 23"). **No findings claimed.**

---

## 5. THINGS I COULD NOT VERIFY — EXPLICIT FLAGS

1. **Cederburg et al. (2020) full text** — closed access; every repository route 403'd. The
   numbers I report (103 strategies, 72/103 lower certainty-equivalent return, structural
   instability) come from **two independent secondary sources** that quote the paper (an
   open-access *Journal of Finance* paper and a University of Helsinki thesis). The paper's
   existence, exact title, journal, volume, issue, pages and DOI are **fully verified**. The
   *specific numeric claims* are second-hand.
2. **A search-result mirror of Cederburg et al.** at `gljc.sxu.edu.cn` is **NOT the paper** — it
   is a Chinese-language lecture deck *about* the papers. I did not use it as a source.
3. **Marmi et al. (2013) full text** — paywalled (Inderscience). I quote only the published
   abstract. Their bootstrapping p-values are unread by me.
4. **Baltas & Kosowski (2013) publication venue** — Crossref dates the SSRN record to 2011 and I
   read a December 2011 working-paper version. I could not confirm a 2013 journal publication.
   Cite the working paper, not a journal.
5. **Le & Ruthbah (Monash)** and **Man Group "Crypto: Too Hot to Handle?"** — not retrieved
   (Cloudflare 403 / not fetched). No claims made.
6. **Borgards (2021)** — abstract/full text not retrievable. Metadata verified only.
7. **Crypto vol targeting** — no verified peer-reviewed study isolating Sharpe improvement from
   volatility targeting on crypto. The Zarattini et al. work uses vol-based sizing but its
   headline results are for the *trend* signal, not vol targeting per se.
8. **SSRN** blocks automated retrieval (HTTP 403) for essentially all abstract pages. All SSRN
   DOIs and abstract IDs cited above were verified via Crossref/RePEc instead. The SSRN links
   are provided for human browsing and may work in a normal browser.
9. **Multi-horizon ensemble superiority** — no single controlled head-to-head significance test
   found. The crypto Table 1 data actually shows the ensemble having *lower* Sharpe than the best
   short single lookback while having better Sortino/MDD.

---

## 6. PRACTICAL SYNTHESIS FOR THE PLATFORM

**Volatility targeting:** Its documented value is **risk control** — vol of vol 4.6% → 1.8%,
shallower left tails, lower max drawdown (Harvey et al. 2018). Its documented **Sharpe**
improvement (equities 0.40 → 0.48–0.51, t-stat 3.05) is **in-sample and fragile**: it fails
real-time calibration (Liu, Tang & Zhou 2019), fails out-of-sample for 72/103 strategies
(Cederburg et al. 2020), becomes negative for five of nine factors net of costs (DeMiguel et al.
2024), and vanishes for bonds, FX and commodities even in-sample (Harvey et al. 2018). Harvey et
al. themselves note it "effectively introduces some momentum." **Do not count vol targeting as
alpha; do use it for drawdown and vol-of-vol control.**

**Trend following:** The multi-decade, multi-asset evidence is the strongest in this review
(Hurst-Ooi-Pedersen: positive in every decade 1880–2016, avg Sharpe ≈ 0.4 across 67 markets).
**Multi-horizon aggregation is standard practice in the reference implementations** (HOP use
1/3/12-month; the crypto study uses nine horizons), but the Sharpe-optimal blend is **not**
equal-weight — Benhamou et al. derive ω*_LT ≈ 83% vs ω*_ST ≈ 17%. Ensembles earn their keep in
**Sortino/drawdown**, not raw Sharpe.

**Crypto specifics:** Shorter lookbacks (5–30 days) dominate longer ones (150d was the worst at
Sharpe 0.99). The only verified net-of-fees crypto result is **Sharpe 1.57, MDD 11%, alpha 11%
vs BTC** for a top-20 liquid rotational portfolio at **10 bps costs with a 20% rebalance
threshold** (Zarattini et al. 2025) — treat the 1.5+ Sharpe claims for single-asset BTC models
as **gross**, since Table 1 explicitly excludes costs. The absence of a verified crypto
vol-targeting study is itself a finding: this is an open, testable question for the platform.
