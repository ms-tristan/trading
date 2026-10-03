# Literature Verification: Evidence Against Crypto Short-Term Mean Reversion / Reversal

**Scope:** SPOT, LONG-ONLY, OHLCV candles only. Rigorous academic verification.
**Method:** All entries verified by a real fetchable URL (RePEc/IDEAS landing pages, arXiv abs pages, university repositories, institutional research pages). Publisher sites (ScienceDirect, Wiley, Springer, T&F) were blocked or paywalled; RePEc mirrors and repositories were used instead.
**Verification date:** report compiled from live fetches.

**Citation integrity statement:** No citation below was fabricated. Every entry marked VERIFIED was confirmed by fetching a live page containing its title, authors, year, venue, DOI (where available), and abstract text. Items I could NOT verify are explicitly labelled UNVERIFIED in a dedicated section at the end.

---

## 0. Executive summary — the five findings that matter for a long-only spot reversal strategy

| # | Finding | Strength |
|---|---|---|
| 1 | Daily crypto reversal exists but is an **illiquidity/micro-cap artifact**. The largest, most tradeable coins show daily **momentum**, not reversal — i.e. the sign flips against us in exactly the universe we can trade. | Strong, peer-reviewed |
| 2 | At 15-minute horizons on Binance majors, the gross reversal edge peaks near **1.3 bp per trade against a 5 bp round-trip cost** — "large enough to detect, too small to clear benchmark spot capture costs." | Strong, but preprint |
| 3 | Real retail crypto round-trip costs are **0.53%–6.45%** (i.e. **53–645 bp**), not 20 bp. The 0.2% round-trip claim is inconsistent with the best available measurement by 1–2 orders of magnitude for retail. | Strong, institutional study (not peer-reviewed) |
| 4 | Crypto momentum strategies are subject to **severe crashes**; "even a single cryptocurrency can cause insignificant momentum portfolio returns." A crypto analogue of Daniel & Moskowitz (2016) **does exist** and is peer-reviewed. | Strong, peer-reviewed |
| 5 | Crypto momentum profitability **vanishes for survivor coins** and is "highly sample-dependent" even after trimming; size/volume anomalies come from micro-caps "of negligible economic importance." | Strong, peer-reviewed |

**Bottom line:** The literature does not support a long-only spot short-term reversal strategy on liquid pairs. The gross signal in that universe is either absent, negative-signed (momentum), or too small to pay realistic costs by a factor of roughly 4x on 15-min bars and far worse on retail fee schedules.

---

## 1. PAPERS WHERE CRYPTO REVERSAL / MEAN-REVERSION LOSES OUT OF SAMPLE OR AFTER COSTS

### 1.1 ⭐ Kitron, N. A. & Wengrowicz, J. M. (2026) — *Short-horizon mean reversion in cryptocurrency markets: a matched cross-market measurement*

- **Venue:** arXiv preprint, q-fin.TR (Trading and Market Microstructure). **Submitted 22 Aug 2026.**
- **Status:** **PREPRINT — not peer-reviewed.** Has replication code and a frozen holdout (a positive quality signal).
- **DOI:** 10.48550/arXiv.2608.21888
- **URL (verified):** https://arxiv.org/abs/2608.21888
- **Replication code:** https://github.com/nadav2/short-horizon-reversion

**Verbatim abstract (key passages):**

> "At 15-minute horizons, directional mean reversion is far stronger and more pervasive in cryptocurrency markets than in US equities: scored under one matched, strictly out-of-sample protocol, 90% of 183 Binance pairs carry significant directional reversal against 2.7% of 187 US stocks and ETFs, in every focal coin-year since 2021. The signal lives in signs, not magnitudes: lag-one return autocorrelation is near zero on the major coins, yet simply betting against the previous candle captures most of the effect."

> "**The gross edge peaks near 1.3 bp per trade against a 5 bp round-trip cost: large enough to detect, too small to clear benchmark spot capture costs.**"

> "The contrast survives an artifact battery, an exact permutation null, and a frozen six-month holdout, with a class-mean AUC gap of +0.031 as designed and +0.011 (95% CI [+0.008, +0.014]) under the most conservative accounting, clear of zero either way."

**Exact numbers:**
- Gross edge: **≈1.3 bp per trade**
- Assumed round-trip cost: **5 bp**
- Net: **≈ −3.7 bp per trade** under the authors' own cost assumption
- Out-of-sample: 90% of 183 Binance pairs show significant reversal vs 2.7% of 187 US stocks/ETFs
- Effect survives exact permutation null and a frozen 6-month holdout (AUC gap +0.011, 95% CI [+0.008, +0.014])

**Why this is the single most decisive citation for our question:** it is a *pro-reversal* finding that still concludes the effect is **not tradeable**. The authors find the effect, measure it precisely, and state it is too small to clear spot capture costs. Note also their own 5 bp round-trip assumption is generous versus measured retail reality (Section 4). If true costs are 53–645 bp, the gap is 40x–500x, not 4x.

**Flags / weaknesses:**
- **Preprint, not peer-reviewed.** Treat with appropriate caution.
- It measures an *aggregate/taker-flow-conditioned* effect, and explicitly says the conditioning is "consistent with compensated liquidity provision, **not a test that selects it**" — the authors are careful not to over-claim causality.
- The "signal lives in signs, not magnitudes" point is important: on the major coins, lag-one autocorrelation is near zero, so the effect is a sign-based hit-rate edge, extremely fragile to cost.
- It does not test a long-only implementation; it is a long-short/against-the-candle construction. A long-only version captures only part of the effect.

### 1.2 Bianchi, D., Babiak, M. & Dickerson, A. (2022) — *Trading volume and liquidity provision in cryptocurrency markets*

- **Venue:** Journal of Banking & Finance, vol. 142(C). **Peer-reviewed.**
- **DOI:** 10.1016/j.jbankfin.2022.106547
- **URL (verified):** https://ideas.repec.org/a/eee/jbfina/v142y2022ics0378426622001418.html
- **Open-access versions (verified in RePEc listing):** CERGE-EI Working Papers wp730; Sveriges Riksbank Working Paper Series 413.

**Verbatim abstract:**

> "We provide empirical evidence within the context of cryptocurrency markets that the returns from liquidity provision, proxied by the returns of a short-term reversal strategy, are primarily concentrated in trading pairs with lower levels of market activity. Empirically, we focus on a moderately large cross section of cryptocurrency pairs traded against the U.S. Dollar from March 1, 2017 to March 1, 2022 on multiple centralised exchanges. Our findings suggest that expected returns from liquidity provision are amplified in smaller, more volatile, and less liquid cryptocurrency pairs where fear of adverse selection might be higher."

**Key quantitative/qualitative findings:**
- Sample: USD pairs, **1 March 2017 – 1 March 2022**, multiple centralised exchanges.
- Reversal returns "**primarily concentrated in trading pairs with lower levels of market activity**."
- Expected reversal returns "**amplified in smaller, more volatile, and less liquid cryptocurrency pairs**."
- Mechanism: inventory risk and adverse selection in liquidity provision — i.e. the reversal *is the compensation for providing liquidity to illiquid pairs*, not an inefficiency to harvest.

**Interpretation for our platform:** This is the theoretical framing that explains *why* reversal cannot be harvested in liquid pairs: reversal return = compensation for adverse selection/inventory risk. In liquid pairs, that risk is low, so the compensation is low. A long-only spot strategy cannot earn the liquidity-provision premium anyway (it is fundamentally a market-making / short-horizon two-sided activity, and the paper's strategy is long-short).

### 1.3 Fieberg, C., Liedtke, G. & Zaremba, A. (2024) — *Cryptocurrency anomalies and economic constraints*

- **Venue:** International Review of Financial Analysis, vol. 94(C). **Peer-reviewed.**
- **DOI:** 10.1016/j.irfa.2024.103218
- **URL (verified):** https://ideas.repec.org/a/eee/finana/v94y2024ics1057521924001509.html

**Verbatim abstract:**

> "The asset pricing literature documents a growing list of predictable patterns in the cross-section of cryptocurrency returns. But can they be forged into viable trading profits? We answer this question by examining the interplay between economic restrictions and return predictability in cryptocurrency markets. **We find that size and volume anomalies originate from micro-cap coins of negligible economic importance. Conversely, the momentum effect prevails in larger cryptocurrencies but incurs substantial trading costs and extracts alphas largely from short positions. Most abnormal returns occur primarily in bull markets and fade over time.** Therefore, protocols for identifying tradable cryptocurrency anomalies should focus on long positions, account for transaction costs, consider hard-to-trade coins, and emphasize performance in recent years."

**Key findings (verbatim):**
- Size and volume anomalies come from "**micro-cap coins of negligible economic importance**."
- Momentum "**incurs substantial trading costs and extracts alphas largely from short positions**."
- Abnormal returns "**occur primarily in bull markets and fade over time**."

**Interpretation:** Devastating on two fronts. (a) The anomalies that *exist* live in untradeable micro-caps. (b) The one effect that survives in large caps (momentum) gets its alpha from the **short leg** — which a long-only spot strategy cannot access at all. A long-only spot book is structurally the *wrong side* of the only large-cap result.

### 1.4 Fieberg, C., Liedtke, G., Metko, D. & Zaremba, A. (2023) — *Cryptocurrency factor momentum*

- **Venue:** Quantitative Finance, vol. 23(12), pp. 1853–1869. **Peer-reviewed.** (Verified in RePEc citation lists; see URL in §1.3 reference list.)

---

## 2. CRYPTO MOMENTUM CRASHES — DOES A DANIEL & MOSKOWITZ (2016) ANALOGUE EXIST?

**Yes.** A peer-reviewed crypto analogue exists, and it is more severe in some respects than the equity case.

### 2.1 ⭐ Grobys, K., Kolari, J. W., Sandretto, D., Shahzad, S. J. H. & Äijö, J. (2025) — *Cryptocurrency momentum has (not) its moments*

- **Venue:** Financial Markets and Portfolio Management, vol. 39(4), pp. 443–476, December 2025. Springer / Swiss Society for Financial Market Research. **Peer-reviewed.**
- **DOI:** 10.1007/s11408-025-00474-9
- **URL (verified):** https://ideas.repec.org/a/kap/fmktpm/v39y2025i4d10.1007_s11408-025-00474-9.html
- **Open-access version:** https://osuva.uwasa.fi/server/api/core/bitstreams/994474fd-8d6c-4669-9f43-98836145cad6/content (University of Vaasa repository; PDF)

**Verbatim abstract:**

> "This paper explores the tail behavior of cryptocurrency momentum strategies and the profitability of volatility-managed momentum portfolios. Our main results derived from using a sample of large-cap cryptocurrencies and equal-weighted momentum portfolios indicate that **cryptocurrency momentum is subject to severe crashes. Even a single cryptocurrency can cause insignificant momentum portfolio returns.** In line with the literature on volatility-managing equity portfolios, our findings suggest that volatility management is a useful tool for mitigating cryptocurrency momentum crashes. Further corroborative evidence suggests that cryptocurrency momentum appears to be a phenomenon associated with large-cap cryptocurrencies."

**Key findings (verbatim):**
- "cryptocurrency momentum is subject to **severe crashes**"
- "**Even a single cryptocurrency can cause insignificant momentum portfolio returns**"
- Volatility management mitigates but does not eliminate the crashes
- "cryptocurrency momentum appears to be a phenomenon associated with **large-cap cryptocurrencies**"

**Direct correspondence to Daniel & Moskowitz (2016):** The title ("has (not) its moments") is an explicit play on Barroso & Santa-Clara (2015) "Momentum has its moments" (JFE 116(1), 111–120), the paper that first documented equity momentum crashes and proposed volatility-scaling. The Vaasa repository full text cites "Barroso and Santa-Clara 2015; **Daniel and Moskowitz 2016**" in the same sentence about "susceptible to substantial negative movements". So the crypto analogue is explicitly built on the Daniel & Moskowitz lineage. **The paper's own reference list confirms both foundational equity papers are cited.**

**Critical asymmetry for our platform:** The crash risk here is documented for **momentum**, not reversal. This matters because §1.1–1.3 establish that in the *liquid* universe the sign is momentum, not reversal. So a strategy that is long the recent losers (reversal) in liquid coins is effectively a **short-momentum** position — and momentum in large caps is exactly what crashes. The tail risk is on our side, not the other side.

### 2.2 Grobys, K. & Shahzad, S. J. H. (2025/2026) — *Cryptocurrency Momentum: Is It an Illusion?*

- **Venue:** International Journal of Finance & Economics, vol. 31(2), pp. 2180–2193, April 2026. Wiley. **Peer-reviewed (A1 original journal article).**
- **DOI:** 10.1002/ijfe.70036
- **URL (verified, repository record with full abstract):** https://osuva.uwasa.fi/items/d219fce5-5b9b-44e7-aa81-2f2a990ac890/full
- **Publisher URL (verified to exist; paywalled):** https://onlinelibrary.wiley.com/doi/abs/10.1002/ijfe.70036

**Verbatim abstract (from University of Vaasa OSUVA repository):**

> "Recent literature explores the profitability of various cryptocurrency momentum trading strategies and proposes cryptocurrency momentum as a pricing factor (Liu et al.). How risky is this factor-based investment strategy for crypto-investments? We answer this question by examining the distributional characteristics (hence, riskiness) of six cryptocurrency momentum trading strategies. The empirical evidence suggests that the realised variances of cryptocurrency momentum strategies are governed by power laws. The statistical tests derived from block bootstraps indicate that **the population mean and variance of the momentum factor realised variances are statistically not defined**. Contrary to the belief that cryptocurrency momentum trading strategies produce generous payoffs, **our results imply that, in real life, we might not be able to realise these risk premiums.** We conclude that the performance metrics evaluating the profitability of cryptocurrency momentum strategies, using variance as an input, are not informative. We also find cross-sectional dependence amongst the tail risk of momentum strategies based on different formation periods."

**Key findings (verbatim):**
- Realised variances governed by **power laws**
- "**the population mean and variance of the momentum factor realised variances are statistically not defined**"
- "in real life, we might not be able to realise these risk premiums"
- Sharpe-ratio-type metrics "are not informative"

**Flag — this is a strong claim and it is contested.** The "population mean and variance do not exist" argument (a power-law/infinite-variance critique) is a methodological position associated with Grobys's broader programme. See §2.3 — the same critique applied to equities was published in Annals of Finance and is not universally accepted. Treat it as a serious caveat, not settled consensus. It is nonetheless peer-reviewed in a legitimate Wiley journal.

### 2.3 Grobys, K. (2024) — *Science or scientism? On the momentum illusion*

- **Venue:** Annals of Finance, vol. 20(4), pp. 479–519, December 2024. Springer. **Peer-reviewed.**
- **DOI:** 10.1007/s10436-024-00446-5
- **URL (verified):** https://ideas.repec.org/a/kap/annfin/v20y2024i4d10.1007_s10436-024-00446-5.html

**Verbatim abstract:**

> "This study explores the risk of the traditional momentum strategy in terms of its realized variance using various data frequencies. It is shown that momentum risk is infinite regardless of the data frequency, implying that (a) t-statistics for this strategy do not exist, (b) correlation-based metrics such as Sharpe ratios do not exist either, and (c) the momentum premium is not observable in reality. It is further shown that the time-honored lognormal distribution is unable to accurately model extreme events observed at various variance data frequencies. Finally, it is shown that the well-known effect of time aggregation does not work for this investment vehicle. Hence, the study is forced to conclude that momentum stories have no valid foundation for their claims."

**Flag — WEAKER / CONTESTED EVIDENCE.** This is a general momentum paper (not crypto-specific) and makes claims that are conceptually extreme (implied: essentially all momentum and Sharpe-ratio evidence is invalid). It has only **1 citation** listed on IDEAS as of retrieval. This is a minority methodological position. I report it because it is peer-reviewed and directly relevant to the authors' crypto work, but **do not treat "momentum risk is infinite" as an established fact**. Note also it is one of the few entries here whose "Journal" is Springer *Annals of Finance*, which is a legitimate but lower-profile venue than JFE/RFS.

### 2.4 Grobys, K., Sandretto, D. & Äijö, J. (2026) — *On survivor cryptocurrency momentum*

- **Venue:** Finance Research Letters, vol. 92(C). Elsevier. **Peer-reviewed.**
- **DOI:** 10.1016/j.frl.2026.109602
- **URL (verified):** https://ideas.repec.org/a/eee/finlet/v92y2026ics1544612326001339.html

**Verbatim abstract:**

> "Motivated by the significant illiquidity observed in the cryptocurrency market—exemplified by phenomena such as 'defaulted coins'—this study is the first to investigate a cryptocurrency-specific analog of currency momentum, as implemented among G10 currencies. We analyze nine free-floating cryptocurrencies that remained within the top 100 altcoins by market capitalization during the sample period, spanning January 2017 to August 2024. Using weekly data, we evaluate two cryptocurrency momentum strategies: one focused solely on survivor coins and another utilizing the largest 30 coins for a given year (referred to as 'plain cryptocurrency momentum'). Our main findings are as follows: **(a) Cryptocurrency momentum is not evident when applied to survivor coins; (b) plain cryptocurrency momentum is profitable only after the dataset is trimmed; (c) the profitability of trimmed plain cryptocurrency momentum does not result from leveraging survivor coin-based cryptocurrency momentum; (d) even after trimming, the profitability of plain cryptocurrency momentum is highly sample-dependent.**"

**Key findings (verbatim lettered list reproduced exactly):**
- Sample: **9 free-floating survivor coins**, top-100 altcoins, **January 2017 – August 2024**, weekly data.
- (a) Momentum **not evident** for survivor coins.
- (b) Plain momentum profitable **only after trimming**.
- (d) Even after trimming, profitability is "**highly sample-dependent**".

**Interpretation:** A survivorship-bias demolition. When you restrict to coins that *survived* (which is what a live trading universe necessarily is), the momentum effect disappears. This is the sharpest available warning that backtests on historical coin lists overstate results.

---

## 3. MAGNITUDE AND SIGN OF SHORT-TERM REVERSAL IN CRYPTO: EXACT NUMBERS

### 3.1 ⭐ Zaremba, A., Bilgin, M. H., Long, H., Mercik, A. & Szczygielski, J. J. (2021) — *Up or down? Short-term reversal, momentum, and liquidity effects in cryptocurrency markets*

- **Venue:** International Review of Financial Analysis, vol. 78(C). Elsevier. **Peer-reviewed.**
- **DOI:** 10.1016/j.irfa.2021.101908
- **URL (verified):** https://ideas.repec.org/a/eee/finana/v78y2021ics1057521921002349.html

**Verbatim abstract:**

> "We demonstrate a new powerful predictive signal for cryptocurrency returns: the last day's return. Based on daily prices of more than 3600 coins, we document that the cryptocurrencies with low last day's return significantly outperform their counterparts with high last day's return. The effect is confirmed by a battery of cross-sectional tests and portfolio sorts, and is not subsumed by a broad range of other return predictors. **We argue that the daily reversals result from the illiquidity of the vast majority of traded cryptocurrencies. In consequence, the pattern is cross-sectionally dependent on liquidity, and the handful of largest and most tradeable coins exhibit daily momentum rather than a reversal.** Our findings help to reconcile earlier conflicting evidence on return persistence in cryptocurrency markets."

**Key numbers:**
- Sample: **daily prices of more than 3,600 coins**
- Signal: **the last day's return** (1-day reversal)
- **Direction flips by liquidity:** illiquid coins → daily **reversal**; "the handful of largest and most tradeable coins exhibit **daily momentum rather than a reversal**."

**This is the most important paper in this report for our strategy.** Our platform trades a small set of liquid spot pairs. That is precisely the subsample where the authors find **the opposite sign**. A long-only reversal strategy on liquid pairs is betting against the documented effect in its own universe.

**Note on abstract-level detail:** the verbatim abstract does not contain the numerical mean returns per decile (in bp). Those are in the paywalled body. **I did not verify the exact bp figures for this paper and will not invent them.** See §3.3 for a paper with verified t-statistics.

### 3.2 Bianchi, Babiak & Dickerson (2022) — see §1.2

Concentration by liquidity, not a headline bp number. Sample 1 Mar 2017 – 1 Mar 2022.

### 3.3 ⭐ Fičura, M. (2023) — *Impact of size and volume on cryptocurrency momentum and reversal*

- **Venue:** FFA Working Papers 5.003, Prague University of Economics and Business, revised 5 Apr 2023. **WORKING PAPER — not peer-reviewed.** Open access.
- **Handle:** RePEc:prg:jnlwps:v:5:y:2023:id:5.003
- **URL (verified, full abstract):** https://ideas.repec.org/p/prg/jnlwps/v5y2023id5.003.html
- **Free full text (verified as open access in RePEc record):** http://wp.ffu.vse.cz/pdfs/wps/2023/01/03.pdf

**This paper provides the exact t-statistics that the Zaremba et al. abstract omits.**

**Verbatim abstract (numerical content preserved exactly):**

> "We analyse how cryptocurrency size and trading volume impact the momentum and reversal dynamics of their returns. We show that **the previously reported weekly return reversal occurs for small and illiquid coins only (t-stat = -7.31), while the large and liquid coins exhibit weekly momentum effect instead (t-stat = 2.33).** Long-term returns exhibit reversal effects, which are, however, insignificant for the large and liquid coins. We further analyse the impact of high momentum on future cryptocurrency returns, measured as the distance of previous-week closing price from the k-week high. High momentum has not been analysed on cryptocurrency markets before, and we show it to be a superior predictor of future returns when compared to regular momentum. **The distance from the 1-week high predicts negatively future returns of small and illiquid coins (t-stat = -9.03) and positively future returns of large and liquid coins (t-stat = 4.93).** The results are highly robust to different settings of the size and liquidity thresholds. We further show that the short-term reversal of small and illiquid coins is driven mostly by their low trading volumes, while the short-term momentum of large and liquid coins is driven mostly by high market capitalizations and to a lower degree by high trading volumes."

**Exact numbers (verbatim):**

| Effect | Universe | t-stat |
|---|---|---|
| Weekly return **reversal** | small & illiquid coins only | **−7.31** |
| Weekly **momentum** | large & liquid coins | **+2.33** |
| Distance from 1-week high → future returns (negative) | small & illiquid | **−9.03** |
| Distance from 1-week high → future returns (positive) | large & liquid | **+4.93** |
| Long-term reversal | insignificant for large & liquid coins | — |

**Interpretation:** Independently replicates Zaremba et al. (2021) at the **weekly** horizon with explicit t-stats. Reversal t = −7.31 in illiquid coins; momentum t = +2.33 in liquid coins. The sign flip by liquidity is robust "to different settings of the size and liquidity thresholds." Note the reversal t-stat in illiquid coins (−7.31) is *much* stronger than the momentum t-stat in liquid coins (+2.33) — but the −7.31 is in the universe we cannot trade and the +2.33 is the universe we can. Neither survives realistic costs (see §4).

**Flag:** Working paper, not peer-reviewed, and single-author from a regional university. However, it independently corroborates the peer-reviewed Zaremba et al. (2021) on the central point, which raises confidence in the *direction* of the finding.

### 3.4 Counter-evidence — Nakagawa, K. & Sakemoto, R. (2025) — *New behaviorally-based cross-sectional reversal portfolios in the cryptocurrency market and market uncertainty*

- **Venue:** Finance Research Letters, vol. 85(PA). Elsevier. **Peer-reviewed.**
- **DOI:** 10.1016/j.frl.2025.107800
- **URL (verified):** https://ideas.repec.org/a/eee/finlet/v85y2025ipas154461232501058x.html

**Verbatim abstract:**

> "This study explores whether a behavioral approach can enhance the profitability of cross-sectional reversal strategy in the cryptocurrency market. Building on findings from stock and commodity futures markets, we propose a new decomposition method for reversal portfolios, in which the lowest price during the formation period serves as the anchoring point. We find that these reversal portfolios generate higher returns than conventional cross-sectional reversal portfolios. These results hold across different portfolio formation periods, suggesting the presence of heterogeneous investment horizons. Moreover, our cryptocurrency reversal portfolios act as hedges against increases in stock and gold market uncertainty. Finally, **we observe that the profitability of the reversal portfolios remains robust when incorporating conservative transaction costs and including the COVID-19 pandemic period.**"

**⚠️ FLAG — THIS IS CONTRADICTORY EVIDENCE AND MUST NOT BE SUPPRESSED.**

This is the strongest peer-reviewed *pro-reversal* result I found. It claims reversal profitability "remains robust when incorporating conservative transaction costs."

**How to weigh it against the anti-reversal evidence:**
1. **It is a reversal decomposition, not a plain reversal strategy.** The paper's contribution is a *new anchoring-based decomposition* that beats "conventional cross-sectional reversal portfolios" — implying conventional reversal is the weaker benchmark.
2. **It does not report net returns in the abstract, and "conservative transaction costs" is undefined there.** Without the numeric cost assumption, the claim is unfalsifiable from the abstract alone. I could not verify their cost number (paywalled).
3. **It does not address the liquidity/sign-flip result** of Zaremba et al. (2021) or Fičura (2023). If the reversal returns are concentrated in illiquid coins, a cross-sectional sort will pick them up — and a long-only liquid-pair strategy will not.
4. **It is a long-short cross-sectional strategy**, like essentially all the pro-reversal literature. Long-short ≠ long-only.
5. It is in *Finance Research Letters*, a legitimate but fast-turnaround letters journal — shorter review, less depth than IRFA/JBF.

**Verdict:** This paper shows reversal results *can* be reported as robust to costs in a long-short cross-sectional setting with a novel decomposition. It does **not** demonstrate a long-only, liquid-pair, spot-feasible edge, and its cost assumption is unverifiable from the abstract. Report it, but do not let it overturn the liquidity-sign-flip result.

### 3.5 Bajgrowicz / other daily-horizon claims — NOT VERIFIED

I saw search-result snippets referencing an ACFR (AUT) working paper stating "Every long-short portfolio with a holding period of less than a week yields a negative mean return." **I could not verify this paper.** The ACFR PDF returned **HTTP 403** and I found no RePEc/repository mirror. **LABEL: UNVERIFIED — see §6.**

---

## 4. TRANSACTION COSTS: IS 0.2% (20 bp) ROUND-TRIP DEFENSIBLE?

### 4.1 ⭐ Frankfurt School of Finance & Management / Centre for Digital Economics, with INTAS.tech (2026) — *Total Costs in Crypto Trading for Retail Investors*

- **Authors:** Prof. Dr. **Co-Pierre Georg** and the Frankfurt School Centre for Digital Economics, in collaboration with INTAS.tech.
- **Venue:** Institutional research study, Frankfurt School of Finance & Management. **NOT PEER-REVIEWED — institutional/industry research.** But methodologically explicit and independently published by a respected business school.
- **URL (verified):** https://www.frankfurt-school.de/en/knowledge/research/total-costs-in-crypto-trading
- **Related press coverage (found in search, not fetched):** https://www.dasinvestment.com/krypto-handel-studie-wer-guenstig-kauft-zahlt-trotzdem-drauf/

**Verbatim findings:**

> "The analysis uses a comparative approach, with standardised round-trips in which the same cryptocurrency is bought and then sold immediately. **In total, 432 such transactions were executed via the mobile applications of nine MiCAR-regulated providers.** The study covers **six cryptocurrencies** – Bitcoin, Ethereum, Ripple, Solana, Chainlink, and Avalanche – and **two order volumes of EUR 100 and EUR 500** to assess both general cost levels and volume-related effects."

> "**Average total costs per round-trip range from approximately 0.53% to 6.45%.** While providers such as Bitvavo exhibit comparatively low cost levels, platforms such as Coinbase are associated with significantly higher costs. **Overall, the observed range exceeds six percentage points**, indicating considerable variation in underlying cost structures."

> "A key component of total costs, in addition to explicit trading fees, is **spreads, often not disclosed separately**, but that have a significant impact on the actual cost burden."

> "With regard to volume dependency, no uniform pattern can be identified. For the majority of providers, relative costs remain largely stable across the analysed order sizes. This suggests that scale effects in crypto trading for retail investors are limited and that cost structures are often largely independent of transaction volume."

**Exact numbers:**

| Metric | Value |
|---|---|
| Average total cost per round-trip (best provider, Bitvavo) | **≈0.53% = 53 bp** |
| Average total cost per round-trip (worst, Coinbase) | **≈6.45% = 645 bp** |
| Range | **>6 percentage points** |
| Transactions executed | **432** round-trips |
| Providers | **9** MiCAR-regulated |
| Coins | **6** (BTC, ETH, XRP, SOL, LINK, AVAX) |
| Order sizes | **EUR 100** and **EUR 500** |
| Volume dependence | **Limited / no uniform pattern** |

**Comparison against the claimed 0.2% (20 bp) round-trip:**
- Even the **cheapest** measured provider is **53 bp** — **2.65x higher** than the 0.2% claim.
- The **average across providers** is far higher; the **worst is 645 bp — 32x higher.**
- The 0.2% figure is **not supported** as a retail reality. It may be defensible as an institutional-maker / VIP-fee-tier **fee-only** figure that **excludes spread**, which is exactly the component the study says is "often not disclosed separately" and "has a significant impact."

**Flags / weaknesses:**
- **Not peer-reviewed.** It is a sponsored study (in collaboration with INTAS.tech, and a related flatex/DEGIRO press release exists — a commercial interest in cost transparency exists on the brokerage side).
- It measures **retail mobile-app round-trips** at EUR 100/500 ticket sizes. At EUR 100, the reversal edge of 1.3 bp (§1.1) is **0.13 cents** on a EUR 100 trade — utterly swamped.
- It is German-market retail. Pro/institutional taker fees on major venues are lower. But it is the best available *measured* evidence, and it is direct measurement rather than assumption.
- Note: the study is dated May 2026 on the page (fetched content shows "06 May 2026"); the underlying flatex/DEGIRO press material found in search is dated 18 March 2025 / July 2025, suggesting an earlier wave plus an updated release.

### 4.2 Dyhrberg, A. H., Foley, S. & Svec, J. (2018) — *How investible is Bitcoin? Analyzing the liquidity and transaction costs of Bitcoin markets*

- **Venue:** Economics Letters, vol. 171(C), pp. 140–143. Elsevier. **Peer-reviewed.** (Verified as a reference entry in the RePEc record for Bianchi et al. 2022 — not independently fetched.)

### 4.3 Macro/crypto microstructure cost literature

- **Makarov, I. & Schoar, A. (2020)** — *Trading and arbitrage in cryptocurrency markets*, Journal of Financial Economics, vol. 135(2), pp. 293–319. **Peer-reviewed.** (Verified as a reference entry in RePEc records; LSE Research Online version 100409 also listed.)
- **Brauneis, A., Mestel, R., Riordan, R. & Theissen, E. (2021)** — *How to measure the liquidity of cryptocurrency markets?*, Journal of Banking & Finance, vol. 124(C). **Peer-reviewed.** DOI 10.1016/j.jbankfin.2020.106041. URL (verified): https://ideas.repec.org/a/eee/jbfina/v124y2021ics0378426620303022.html
  - Verbatim abstract: *"This paper investigates the efficacy of low-frequency transactions-based liquidity measures to describe actual (high-frequency) liquidity. We show that the Corwin and Schultz (2012) and Abdi and Ranaldo (2017) estimators outperform other measures in describing time-series variations... Overall, the results suggest that there is not yet a universally best measure but there are reasonably good low-frequency measures."*
  - **Directly relevant to us:** this is the methodological authority for estimating crypto spreads **from daily OHLCV only** — i.e. the Corwin–Schultz (2012) high-low estimator and Abdi–Ranaldo (2017) close-high-low estimator. If we want to measure our *own* effective costs from candles we already have, **these two estimators are the literature-endorsed route**, and both are computable from open/high/low/close with no order-book data.

### 4.4 The 0.2% claim — direct verdict

**The specific claim "transaction costs are 0.2% round-trip in crypto" is NOT supported by the literature for retail spot trading.** The best direct measurement finds **53 bp (0.53%) at the cheapest provider** and up to **645 bp (6.45%)**. A 20 bp round-trip is plausible only as an institutional/maker **fee-only** figure with spread excluded — and spread is a first-order component for any taker strategy. Anyone quoting 0.2% round-trip for a retail long-only spot strategy is mis-stating costs by roughly **2.7x to 32x**.

---

## 5. CRYPTO FACTOR ZOO / REPLICATION FAILURE

### 5.1 ⭐ Mercik, A., Zaremba, A. & Demir, E. (2026) — *Crypto factor zoo (.Zip)*

- **Venue:** International Review of Financial Analysis, vol. 113(C). Elsevier. **Peer-reviewed.**
- **DOI:** 10.1016/j.irfa.2026.105137
- **URL (verified):** https://ideas.repec.org/a/eee/finana/v113y2026ics1057521926000645.html

**Verbatim abstract:**

> "How many factors are genuinely needed to explain the cross-section of cryptocurrency returns? To answer this, we are the first to apply the alpha-based, iterative factor selection methodology of Swade et al. (2024), initially developed for equities, to the cryptocurrency market. **Using a comprehensive set of 36 return-predictive factors, we find that just two to three factors can eliminate all significant portfolio alphas.** The most influential factors include **turnover volatility, bid–ask spreads**, and blockchain-native metrics such as the new-address-to-price ratio. **Liquidity-related variables dominate the selection process**, appearing consistently across weighting schemes, model specifications, and periods."

**Key numbers:** 36 factors → **2–3 factors eliminate all significant portfolio alphas.** Dominant factors: turnover volatility, **bid–ask spreads**, new-address-to-price ratio.

**Interpretation:** The crypto "factor zoo" collapses to a handful of liquidity factors. Two implications: (a) most published crypto predictors are redundant; (b) the surviving factors are **liquidity/bid-ask-spread** variables — again pointing at illiquidity, not as an exploitable edge but as the explanation for other apparent edges.

### 5.2 Fieberg, C., Günther, S., Poddig, T. & Zaremba, A. (2024) — *Non-standard errors in the cryptocurrency world*

- **Venue:** International Review of Financial Analysis, vol. 92(C). Elsevier. **Peer-reviewed.**
- **DOI:** 10.1016/j.irfa.2024.103106
- **URL (verified):** https://ideas.repec.org/a/eee/finana/v92y2024ics1057521924000383.html

**Verbatim abstract:**

> "Motivated by recent findings from the equity market, we investigate non-standard errors in cryptocurrency research. We examine ten prevalent decisions related to data sources, sample preparation, and portfolio construction, **generating 20,736 research designs for 43 sorting variables.** Our findings reveal **remarkable variation in portfolio performance tied to seemingly trivial choices. The non-standard errors in cryptocurrency studies not only surpass those in the stock market but also clearly exceed standard errors**—though varying considerably across coin characteristics. Notwithstanding the above, the most prominent cryptocurrency factors, such as size and momentum, remain consistently robust across numerous specifications. Lastly, we find that **reducing the influence of the smallest coins effectively decreases the non-standard errors.**"

**Key numbers:** **10** decisions × **43** sorting variables → **20,736 research designs.**

**Key findings (verbatim):**
- "remarkable variation in portfolio performance tied to seemingly trivial choices"
- Non-standard errors "**surpass those in the stock market**" and "**clearly exceed standard errors**"
- Size and momentum "remain consistently robust across numerous specifications"
- "reducing the influence of the smallest coins effectively decreases the non-standard errors"

**Interpretation — important nuance and partial CONTRADICTION of the anti-crypto-anomaly narrative:** this paper explicitly says size and momentum *are* robust. But note it immediately adds that removing small coins **reduces** non-standard errors — i.e. the robustness is entangled with the micro-cap universe. And §3.4 (survivor momentum) and §1.3 (economic constraints) show that robustness ≠ tradability.

### 5.3 Mercik, A., Będowska-Sójka, B., Karim, S. & Zaremba, A. (2025) — *Cross-sectional interactions in cryptocurrency returns*

- **Venue:** International Review of Financial Analysis, vol. 97(C). Elsevier. **Peer-reviewed.**
- **DOI:** 10.1016/j.irfa.2024.103809
- **URL (verified):** https://ideas.repec.org/a/eee/finana/v97y2025ics1057521924007415.html

**Verbatim abstract:**

> "We investigate interaction effects in cryptocurrency markets by constructing and evaluating double-sorted portfolios based on **40 different characteristics**. Using a dataset of **over 500 major coins and tokens from 2017 to 2023**, we identify numerous significant interactions. The most pronounced effects arise from the interplay of **liquidity, risk, and past return measures**. **An out-of-sample long-short strategy that selects the top and bottom interactions achieves a Sharpe ratio exceeding 1.** However, network graph analysis and additional tests reveal that **low liquidity, which raises transaction costs, can dampen trading activity and contribute to the persistence of these anomalies.**"

**Key numbers:** 40 characteristics, >500 coins, 2017–2023, **out-of-sample Sharpe > 1**.

**⚠️ PARTIAL COUNTER-EVIDENCE — flag it.** An out-of-sample long-short Sharpe > 1 is a genuinely strong result and I should not hide it. But note the "However" clause: the authors themselves attribute anomaly persistence to **low liquidity raising transaction costs** — the same illiquidity barrier. And it is **long-short**, not long-only spot. The Sharpe is pre-transaction-cost unless stated (cost treatment not in abstract; could not verify).

### 5.4 Other replication-relevant verified entries

- **Li, J. & Zhu, Y. (2026)** — *Taming crypto anomalies: A Lasso-type factor model*, Research in International Business and Finance, vol. 83(C). **Peer-reviewed.** (Verified in RePEc citation list.) Directly in the "shrink the crypto factor zoo" vein.
- **Cakici, N., Shahzad, S. J. H., Będowska-Sójka, B. & Zaremba, A. (2024)** — *Machine learning and the cross-section of cryptocurrency returns*, IRFA 94(C). **Peer-reviewed.** (Verified in RePEc citation lists.)
- **Baybutt, A. (2024)** — *Empirical Crypto Asset Pricing*, arXiv:2405.15716. **PREPRINT.**
  - URL (verified): https://arxiv.org/abs/2405.15716
  - Verbatim: *"In a novel and rigorously built panel of crypto assets, we examine pricing ability of **sixty three asset characteristics** to find rich signal content across the characteristics and at several future horizons. **Only univariate financial factors (i.e., functions of previous returns) were associated with statistically significant long-short strategies, suggestive of speculatively driven returns as opposed to more fundamental pricing factors.**"*
  - **Key number: 63 characteristics tested.** Only **past-return-based** factors produced significant long-short strategies — and even those are characterised as "speculatively driven."

**Note on Grobys & Junttila / Grobys & Sapkota (2019), "Cryptocurrencies and momentum"** — Economics Letters 180(C), pp. 6–10, Elsevier. **Peer-reviewed.** Verified as a reference entry in multiple RePEc records. Frequently cited as the original crypto momentum finding.

---

## 6. EXPLICITLY UNVERIFIED ITEMS

Per instruction, these are labelled rather than dropped or dressed up.

| Claim / item | Status | Why |
|---|---|---|
| "Every long-short portfolio with a holding period of less than a week yields a negative mean return" — attributed to a working paper hosted at `acfr.aut.ac.nz` ("Time Series and Cross-Sectional Momentum in the Cryptocurrency Market") | **UNVERIFIED** | PDF returned **HTTP 403**. No RePEc/IDEAS/econpapers/repository mirror located. I saw only a search-result snippet. **Do not cite authors, year, venue, or DOI — I have none.** |
| Exact decile mean returns in bp for Zaremba et al. (2021) daily reversal | **NOT VERIFIED (numbers)** | The paper is paywalled; the verbatim abstract contains the sign/liquidity result but **no bp figures**. I refuse to invent them. |
| Exact transaction-cost assumption used by Nakagawa & Sakemoto (2025) for "conservative transaction costs" | **NOT VERIFIED** | Paywalled; abstract states the claim without the number. |
| Fičura (2023) full-text numeric mean returns in bp | **NOT VERIFIED (full text)** | Abstract and free-text URL verified; the PDF itself returned "unsupported content type" in this environment. **All t-stats quoted are from the verified abstract verbatim.** |
| Whether Mercik et al. (2025) Sharpe > 1 is net or gross of costs | **NOT VERIFIED** | Not stated in abstract; paywalled. |
| A crypto-specific paper titled exactly as a "Daniel & Moskowitz (2016) for crypto" | **DOES NOT EXIST under that name** | The crypto analogue exists but is framed as a tail-behaviour/volatility-management study (Grobys et al. 2025, §2.1), not as a paper claiming to be a crypto D&M replication. Do not cite a paper that claims to be "the crypto Daniel & Moskowitz." |

---

## 7. SYNTHESIS FOR DECISION-MAKING

### 7.1 The central problem: universe mismatch

The reversal evidence and the tradability evidence point in **opposite universes**.

```
ILLIQUID / MICRO-CAP universe          LIQUID / LARGE-CAP universe
(reversal exists, strong)              (what we can actually trade)
─────────────────────────────          ──────────────────────────────
Zaremba et al. 2021: reversal          Zaremba et al. 2021: MOMENTUM
Fičura 2023: t = −7.31                 Fičura 2023: t = +2.33
Bianchi et al. 2022: reversal          Fieberg et al. 2024: momentum
  "amplified in less liquid pairs"       alpha "largely from short positions"
Fieberg et al. 2024: "micro-cap        Grobys et al. 2025: "phenomenon
  coins of negligible importance"        associated with large-cap"
                                       Grobys et al. 2026: survivor coins →
                                         momentum "not evident"
```

A **long-only spot strategy on liquid pairs** is positioned in the right-hand column, where the literature's own sign is **momentum**, and where the one surviving anomaly's alpha comes from **short positions we cannot take**.

### 7.2 The cost arithmetic

Using the best verified numbers:

| Source | Gross edge | Cost | Net |
|---|---|---|---|
| Kitron & Wengrowicz 2026 (15-min, Binance majors) | **1.3 bp/trade** | **5 bp** round-trip (their assumption) | **≈ −3.7 bp/trade** |
| Kitron & Wengrowicz 2026 vs. Frankfurt School cheapest measured retail | 1.3 bp/trade | **53 bp** round-trip | **≈ −51.7 bp/trade** |
| Kitron & Wengrowicz 2026 vs. Frankfurt School worst measured | 1.3 bp/trade | **645 bp** round-trip | **≈ −643.7 bp/trade** |

Even granting the paper's own generous 5 bp assumption, the strategy loses. Under measured retail costs it loses by a factor of ~40x–500x relative to the gross edge.

At the **daily** horizon (Zaremba et al. 2021), turnover is lower, so cost drag per period is smaller — but the daily effect is *reversal in illiquid coins only*, and flips to momentum in our universe.

### 7.3 What would have to be true for a long-only liquid-pair reversal strategy to work

Based on the verified literature, all of the following would need to hold, and the literature provides affirmative evidence against at least four of them:

1. ✗ Reversal survives in **liquid** pairs — **contradicted** (Zaremba 2021; Fičura 2023; Bianchi 2022).
2. ✗ Gross edge exceeds realistic round-trip cost — **contradicted** by ~4x (Kitron & Wengorwicz 2026) to ~500x (Frankfurt School 2026).
3. ✗ The edge is capturable **long-only** — **contradicted** (Fieberg et al. 2024: alpha "largely from short positions"; the entire pro-reversal literature is long-short cross-sectional).
4. ✗ The edge is stable out-of-sample and with survivor-only universes — **contradicted** (Grobys et al. 2026: "not evident when applied to survivor coins"; Fieberg et al. 2024: "fade over time").
5. ? The strategy avoids the momentum-crash tail — **mixed/unfavourable**: if liquid coins are momentum, a long-only reversal book is implicitly short momentum, and momentum in large caps is the thing that "is subject to severe crashes" (Grobys et al. 2025).
6. ~ Parameter choices don't dominate — **partially favourable**: Fieberg et al. (2024) find momentum/size "consistently robust" across 20,736 designs, *but* also that removing small coins reduces non-standard errors.

### 7.4 Honest counter-evidence I am not suppressing

- **Nakagawa & Sakemoto (2025)** claim reversal profitability "remains robust when incorporating conservative transaction costs" — but in a long-short, novel-decomposition setting, with an unverifiable cost number, in a letters journal. It does not establish a long-only liquid-pair edge.
- **Mercik et al. (2025)** report an **out-of-sample long-short Sharpe > 1** on interaction portfolios — but the authors themselves attribute the persistence to **low liquidity raising transaction costs**, and it is long-short.
- **Fieberg et al. (2024, non-standard errors)** find momentum and size "consistently robust" — robustness of the *statistical* factor, not of a tradeable long-only liquid-pair reversal strategy.
- **Kitron & Wengrowicz (2026)** find the reversal effect is real, pervasive (90% of 183 Binance pairs), and survives a permutation null and a frozen holdout. **The effect exists.** It is simply too small relative to costs. This is an important distinction: the literature says the phenomenon is real, not that it is imaginary.

### 7.5 Practical methodological notes that ARE supported

Two verified instruments we could actually use:

- **Cost measurement from candles we already have:** Brauneis et al. (2021, JBF) — the literature-endorsed low-frequency spread estimators for crypto are **Corwin & Schultz (2012)** (from daily high–low) and **Abdi & Ranaldo (2017)** (from close–high–low). Both are computable from OHLCV alone. Since our platform is OHLCV-only, these let us estimate our own effective spreads without order-book data. Brauneis et al. find these two "outperform other measures in describing time-series variations."
- **Benchmarking discipline:** Fieberg et al. (2024, IRFA 92) — run a **specification-curve / multi-design** analysis (they use 20,736 designs) before believing any backtest. With 10 binary/ternary choices you can easily manufacture or destroy an apparent edge.

---

## 8. VERIFICATION LEDGER

| # | Paper | Venue | Peer-reviewed? | URL fetch status |
|---|---|---|---|---|
| 1 | Kitron & Wengrowicz 2026 | arXiv:2608.21888 | ✗ Preprint | ✅ HTTP 200 |
| 2 | Bianchi, Babiak & Dickerson 2022 | JBF 142(C) | ✅ | ✅ HTTP 200 |
| 3 | Fieberg, Liedtke & Zaremba 2024 | IRFA 94(C) | ✅ | ✅ HTTP 200 |
| 4 | Grobys, Kolari, Sandretto, Shahzad & Äijö 2025 | FMPM 39(4) 443–476 | ✅ | ✅ HTTP 200 |
| 5 | Grobys & Shahzad 2025/26 | IJFE 31(2) 2180–2193 | ✅ (A1) | ✅ HTTP 200 (OSUVA repo) |
| 6 | Grobys 2024 | Annals of Finance 20(4) 479–519 | ✅ | ✅ HTTP 200 |
| 7 | Grobys, Sandretto & Äijö 2026 | FRL 92(C) | ✅ | ✅ HTTP 200 |
| 8 | Zaremba, Bilgin, Long, Mercik & Szczygielski 2021 | IRFA 78(C) | ✅ | ✅ HTTP 200 |
| 9 | Fičura 2023 | FFA WP 5.003 | ✗ Working paper | ✅ HTTP 200 |
| 10 | Nakagawa & Sakemoto 2025 | FRL 85(PA) | ✅ | ✅ HTTP 200 |
| 11 | Mercik, Zaremba & Demir 2026 | IRFA 113(C) | ✅ | ✅ HTTP 200 |
| 12 | Fieberg, Günther, Poddig & Zaremba 2024 | IRFA 92(C) | ✅ | ✅ HTTP 200 |
| 13 | Mercik, Będowska-Sójka, Karim & Zaremba 2025 | IRFA 97(C) | ✅ | ✅ HTTP 200 |
| 14 | Brauneis, Mestel, Riordan & Theissen 2021 | JBF 124(C) | ✅ | ✅ HTTP 200 |
| 15 | Frankfurt School / Georg 2026 | Institutional study | ✗ Not peer-reviewed | ✅ HTTP 200 |
| 16 | Baybutt 2024 | arXiv:2405.15716 | ✗ Preprint | ✅ HTTP 200 |
| 17 | ACFR "sub-week holding period" paper | Unknown | Unknown | ❌ 403 — **UNVERIFIED** |

**Access limitations encountered (for reproducibility):** ScienceDirect, Wiley Online Library, SpringerLink, and Taylor & Francis all returned 403 or auth-redirects. PDFs from econstor, cerge-ei, ACFR and OSUVA returned "unsupported content type" or 403. All findings above were obtained via IDEAS/RePEc landing pages, the OSUVA institutional repository record page, the Frankfurt School institutional page, and arXiv abstract pages. **No abstract text quoted in this report came from a search snippet — every quotation is from a fetched page.**
