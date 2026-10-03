# Academic Literature Review — Quantitative Crypto Trading (Spot, Long-Only, OHLCV)

**Scope:** short-term reversal / mean reversion · seasonality & calendar effects · volatility-contraction
("squeeze") breakouts.
**Constraint context:** SPOT LONG-ONLY, OHLCV candles only, **0.2% round-trip fee** as the viability hurdle.
⚠️ **Later research (see §2A.3) indicates the 0.2% hurdle is itself far too optimistic for retail spot — measured
retail round-trip costs run 0.53%–6.45%. The true hurdle is likely 2.7×–32× higher, which makes every result below
worse, not better.**
**Method:** every citation below was retrieved from a live URL. Publisher sites (ScienceDirect, Wiley, SSRN,
Taylor & Francis, MDPI direct) returned HTTP 403 in this environment; findings were therefore verified through
RePEc/IDEAS abstract pages, NBER, and institutional open-access repositories, which reproduce the publishers'
author abstracts verbatim. Items that could **not** be verified are explicitly labelled **UNVERIFIED**.

---

## 0. Executive summary — the three headline results

**1. Short-term reversal in crypto is an ILLIQUIDITY artifact, and the sign FLIPS on liquid coins.**
The one large, well-powered study (3,600+ coins) finds daily reversal, then shows it is driven by illiquidity and
that *the handful of largest, most tradeable coins exhibit daily MOMENTUM, not reversal*. For a spot long-only book
restricted to liquid pairs, the reversal effect is absent and the documented effect has the opposite sign.
→ **Mean-reversion "buy the dip" on liquid spot is contrarian to the published evidence in that exact universe.**

**2. Every crypto calendar anomaly with a plausible magnitude fails the stability test.** The peer-reviewed
consensus: return anomalies exist in-sample but do **not** persist across time; the *only* robust calendar fact is
lower weekend/evening **trading volume/activity**, which is not a directional edge. The Monday effect is positive for
BTC (Caporale & Plastun) yet negative in the cross-section and dead after 2015 (Mueller). Sign instability alone is
disqualifying.

**3. Volatility contraction predicts MAGNITUDE, not DIRECTION — and the canonical paper says the sign effect should
not even exist at daily frequency.** This is the cleanest, most decisive finding in the whole review. Christoffersen
& Diebold (Management Science 2006) prove volatility dependence produces *sign* dependence only insofar as expected
returns are nonzero, and state sign dependence is "not likely to be found in very high-frequency (e.g., daily) ...
returns." "Squeeze → bigger move" is solid. "Squeeze → up" is not supported at our timeframe.
*Nuance:* the squeeze hypothesis does have **one** weak peer-reviewed supporter — Holmberg, Lönnbark & Lundström
(2013), whose official keyword is literally **"Contraction–Expansion principle"** (§3.2) — and **one** peer-reviewed
opponent, Fang, Jacobsen & Qin (2017), who find Bollinger breakout-continuation fails with losses that "worsened over
time" (§3.3). Honest position: **magnitude expansion after contraction is solid; the directional breakout edge is
unproven and has failed the closest peer-reviewed test.** Testing it is justified; assuming it works is not.

**4. The 0.2% cost hurdle is probably wrong by a large factor.** Measured retail spot round-trip costs are
**0.53%–6.45%** (Frankfurt School, 9 MiCAR-regulated providers, 432 round-trips). The nearest thing to a
quantified crypto reversal edge is **~1.3 bp gross per trade**. The gap is two to three orders of magnitude.
**Resolve this premise before building anything** (§2A.3, §2.7).

A single theme unifies all three: **the effects that are statistically real in crypto are (a) in the micro-cap /
illiquid tail we cannot trade, or (b) magnitude rather than direction, or (c) destroyed by transaction costs and
data-snooping correction.** The evidence base is remarkably hostile to the three candidate edges.

---

## 1. SHORT-TERM REVERSAL / MEAN REVERSION

### 1.1 THE decisive paper: reversal is illiquidity, and it inverts on liquid coins

**Zaremba, A.; Bilgin, M.H.; Long, H.; Mercik, A.; Szczygielski, J.J. (2021), "Up or down? Short-term reversal,
momentum, and liquidity effects in cryptocurrency markets", *International Review of Financial Analysis* 78(C).
DOI: 10.1016/j.irfa.2021.101908.**
Verified: https://ideas.repec.org/a/eee/finana/v78y2021ics1057521921002349.html

Verbatim findings:
> "Based on daily prices of **more than 3600 coins**, we document that the cryptocurrencies with low last day's
> return significantly outperform their counterparts with high last day's return. The effect is confirmed by a
> battery of cross-sectional tests and portfolio sorts, and is not subsumed by a broad range of other return
> predictors. We argue that the daily reversals result from the **illiquidity of the vast majority of traded
> cryptocurrencies**. In consequence, the pattern is cross-sectionally dependent on liquidity, and **the handful of
> largest and most tradeable coins exhibit daily momentum rather than a reversal**."

**Why this is decisive for us:** the effect's sign is a *function of liquidity*. Our universe (liquid spot pairs) is
precisely the subsample where it inverts. Published: IRFA (peer-reviewed, Elsevier).

Supporting, same research group:

- **Fieberg, C.; Liedtke, G.; Zaremba, A. (2024), "Cryptocurrency anomalies and economic constraints", *IRFA* 94(C).
  DOI: 10.1016/j.irfa.2024.103218.** https://ideas.repec.org/a/eee/finana/v94y2024ics1057521924001509.html
  > "size and volume anomalies originate from **micro-cap coins of negligible economic importance**. Conversely, the
  > momentum effect prevails in larger cryptocurrencies but **incurs substantial trading costs and extracts alphas
  > largely from short positions**. Most abnormal returns occur primarily in **bull markets and fade over time**."
  Two independent disqualifiers: the tradable-coin effect lives in the *short* leg (unavailable to us), and
  abnormal returns decay over time (non-stationarity).

- **Grobys, K.; Kolari, J.W.; Sandretto, D.; Shahzad, S.J.H.; Äijö, J. (2025), "Cryptocurrency momentum has (not) its
  moments", *Financial Markets and Portfolio Management* 39(4), 443–476. DOI: 10.1007/s11408-025-00474-9.**
  https://ideas.repec.org/a/kap/fmktpm/v39y2025i4d10.1007_s11408-025-00474-9.html
  > "cryptocurrency momentum is subject to **severe crashes**. **Even a single cryptocurrency can cause insignificant
  > momentum portfolio returns.** ... volatility management is a useful tool for mitigating cryptocurrency momentum
  > crashes."
  This is the crypto analogue of Barroso & Santa-Clara / Daniel & Moskowitz momentum crashes. Concentration risk is
  extreme — a single coin can invalidate the whole portfolio. Directly relevant to a long-only book with few names.

### 1.2 Overreaction is statistically real but NOT exploitable

**Caporale, G.M. & Plastun, A. (2019), "Price overreactions in the cryptocurrency market", *Journal of Economic
Studies* 46(5), 1137–1155. DOI: 10.1108/JES-09-2018-0310.**
Verified: https://ideas.repec.org/a/eme/jespps/jes-09-2018-0310.html

Covers BTC, LTC, XRP, DASH. Verbatim:
> "A number of parametric (t-test, ANOVA, regression analysis with dummy variables) and non-parametric
> (Mann–Whitney U-test) tests **confirm the presence of price patterns after overreactions: the next day price
> changes in both directions are bigger than after 'normal' days**. ... **a strategy based on counter-movements after
> overreactions is not profitable**, whilst one based on inertia appears to be profitable but produces outcomes
> **not statistically different from the random ones**. Therefore, the overreactions detected in the cryptocurrency
> market **do not give rise to exploitable profit opportunities (possibly because of transaction costs)** and cannot
> be seen as evidence against the EMH."

**This is the sharpest statement of the "real but untradeable" problem.** Note the authors' own hedge "possibly
because of transaction costs" — but the direct finding is that the *contrarian* (buy-the-dip) leg has negative P&L.
Related: Borgards & Czudaj (2020), "The prevalence of price overreactions in the cryptocurrency market",
*J. Int. Financial Markets, Institutions & Money* 65(C), DOI: 10.1016/j.intfin.2020.101195.

### 1.3 Conditional / regime-dependent predictability — CONFIRMED, and it is the key nuance

**Wen, Z.; Bouri, E.; Xu, Y.; Zhao, Y. (2022), "Intraday return predictability in the cryptocurrency markets:
Momentum, reversal, or both", *North American Journal of Economics and Finance* vol. 62, paper 101733.
DOI: 10.1016/j.najef.2022.101733.**
Verified (full abstract): https://m2.mtmt.hu/api/publication/33003705

> "This paper reports evidence of intraday return predictability, consisting of **both intraday momentum and
> reversal**, in the cryptocurrency market. Using high-frequency price data on Bitcoin from **March 3, 2013, to
> May 31, 2020**, it shows that **the patterns of intraday return predictability change in the presence of large
> intraday price jumps, FOMC announcement release, liquidity levels, and the outbreak of the COVID-19**. Intraday
> return predictability is also found in other actively traded cryptocurrencies such as Ethereum, Litecoin, and
> Ripple. ... Evidence of intraday momentum can be explained in light of the theory of late-informed investors,
> whereas evidence of intraday reversal, which is **unique to the cryptocurrency market**, can be related to
> investors' **overreaction to non-fundamental information and overconfidence bias**."

⚠️ **Citation correction:** this paper is in the **North American Journal of Economics and Finance**, *not* Finance
Research Letters. The requested title is otherwise correct.

**This directly supports the "conditional mean reversion" hypothesis you asked about** — predictability is
state-dependent (jumps, FOMC, liquidity, COVID). **But note the double edge:** the same paper finds momentum *and*
reversal in the same data. A state-dependent effect whose sign flips by regime is extremely hard to harvest long-only,
and the paper gives no net-of-cost, out-of-sample trading result.

### 1.4 Liu, Tsyvinski & Wu (2022) — VERIFIED as a paper, reversal number UNVERIFIED

**Liu, Y.; Tsyvinski, A.; Wu, X. (2022), "Common Risk Factors in Cryptocurrency", *Journal of Finance* 77(2),
1133–1177. DOI: 10.1111/jofi.13119.** NBER WP 25882 (May 2019), DOI: 10.3386/w25882.
Verified: https://www.nber.org/papers/w25882

Abstract (verbatim, NBER): *"We find that three factors – cryptocurrency market, size, and momentum – capture the
cross-sectional expected cryptocurrency returns. ... Nine cryptocurrency factors form successful long-short
strategies that generate sizable and statistically significant excess returns. We show that all of these strategies
are accounted for by the cryptocurrency three-factor model."*

🚩 **EXPLICIT FLAG — do not cite an exact figure.** I could **not** verify a specific "1-week reversal" magnitude
from a primary source. The NBER abstract and the JoF record describe the three-factor model (market, size,
**momentum**) — they do **not** advertise a short-term reversal result at a 1-week horizon, and full text was
inaccessible (Wiley 403). **The paper is real and top-tier; the specific 1-week reversal number is UNVERIFIED.**
Do not attribute a precise reversal figure to LTW 2022 in any deliverable.

### 1.5 Calibrated criticism: why most TA rules fail after data-snooping correction

**Bajgrowicz, P. & Scaillet, O. (2012), "Technical trading revisited: False discoveries, persistence tests, and
transaction costs", *Journal of Financial Economics* 106(3), 473–491. DOI: 10.1016/j.jfineco.2012.06.001.**
Verified: https://ideas.repec.org/a/eee/jfinec/v106y2012i3p473-491.html
(WP version: Swiss Finance Institute 08-05, https://ideas.repec.org/p/chf/rpseri/rp0805.html)

DJIA daily 1897–2011. Verbatim:
> "We use the false discovery rate (FDR) as a new approach to data snooping. The advantage of the FDR over existing
> methods is that it **selects more outperforming rules**, which allows diversifying against model uncertainty.
> **Persistence tests show that, even with the more powerful FDR technique, an investor would never have been able to
> select ex ante the future best-performing rules.** Moreover, **even in-sample, the performance is completely offset
> by the introduction of low transaction costs.** Overall, our results seriously call into question the economic
> value of technical trading rules that has been reported for early periods."

**Two independent kill mechanisms, and the FDR choice makes the test generous to TA** — the authors adopted the
*more permissive* data-snooping method and the rules *still* failed. This is the strongest available calibration
against any TA-style rule, including RSI and squeeze breakouts.

**Park, C-H. & Irwin, S.H. (2007), "What Do We Know About The Profitability Of Technical Analysis?",
*Journal of Economic Surveys* 21(4), 786–826. DOI: 10.1111/j.1467-6419.2007.00519.x.**
Verified: https://ideas.repec.org/a/bla/jecsur/v21y2007i4p786-826.html
> "Among a total of **95 modern studies, 56 studies find positive results** regarding technical trading strategies,
> **20 studies obtain negative results, and 19 studies indicate mixed results**. Despite the positive evidence ...
> most empirical studies are subject to various problems in their testing procedures, e.g. **data snooping, ex post
> selection of trading rules or search technologies, and difficulties in estimation of risk and transaction costs**."

⚖️ **Important calibration:** the survey is actually **56/95 POSITIVE** on technical analysis. It is *not* evidence
that TA is dead — it is evidence that the literature leans positive but is **methodologically compromised**, and the
authors explicitly decline to call it conclusive. Cite it precisely.

### 1.6 Crypto-specific TA evidence: in-sample yes, Bitcoin out-of-sample NO

**Hudson, R. & Urquhart, A. (2021), "Technical trading and cryptocurrencies", *Annals of Operations Research* 297,
191–220. DOI: 10.1007/s10479-019-03357-1.**
Verified (open access, full abstract): https://centaur.reading.ac.uk/85715/

> "We employ almost **15,000 technical trading rules** from the main five classes ... and find **significant
> predictability and profitability for each class** ... We find that the **breakeven transaction costs are
> substantially higher than those typically found in cryptocurrency markets**. To safeguard against data-snooping,
> we implement a number of multiple hypothesis procedures which **confirms our findings** ... [and] the technical
> trading rules offer substantially higher risk-adjusted returns than the simple buy-and-hold strategy ...
> **However there is no predictability for Bitcoin in the out-of-sample period**, although predictability remains in
> other cryptocurrency markets."

**The single most important crypto TA result for us.** In-sample: 15,000 rules, survives multiple-hypothesis
correction. Out-of-sample: **BTC fails.** The most liquid, most-traded crypto asset is exactly where the edge
disappears — the mirror image of the Zaremba liquidity-flip finding. Two independent papers, same conclusion:
*edges concentrate where liquidity is worst.*

⚠️ **Citation correction:** the requested "Hudson, McGroarty & Urquhart 2020" is actually **Hudson & Urquhart
(2021, online 2019)** — **two authors, no McGroarty.**

**Corbet, S.; Eraslan, V.; Lucey, B.; Sensoy, A. (2019), "The effectiveness of technical trading rules in
cryptocurrency markets", *Finance Research Letters* 31(C), 32–37. DOI: 10.1016/j.frl.2019.04.027.**
Verified: https://ideas.repec.org/a/eee/finlet/v31y2019icp32-37.html
> "moving average-oscillator and trading range break-out strategies to specifically test resistance and support
> levels ... using high-frequency Bitcoin returns. Overall, our results provide **significant support for the moving
> average strategies**. In particular, variable-length moving average rule performs the best with **buy signals
> generating higher returns than sell signals**."

⚖️ This is one of the few papers with a **long-side** positive result — worth noting. But: short letter format, no
formal data-snooping correction reported, and **directly contradicted out-of-sample for BTC by Hudson & Urquhart
(2021)**. Treat as weak positive evidence, heavily discounted.

### 1.7 RSI specifically — peer-reviewed, and skeptical

**Zatwarnicki, M.; Zatwarnicki, K.; Stolarski, P. (2023), "Effectiveness of the Relative Strength Index Signals in
Timing the Cryptocurrency Market", *Sensors* 23(3), 1664. DOI: 10.3390/s23031664.**
Verified (abstract + metadata; indexed Scopus **and** Web of Science): https://ouci.dntb.gov.ua/en/works/4yEoKNg4/
(MDPI direct returned 403.)

> "A model corresponding to an actual cryptocurrency exchange was used to backtest the strategies. The results show
> that **the RSI as a momentum indicator in the cryptocurrency market involves high risk.** Using **alternative RSI
> applications** can allow traders to gain an advantage ... Comparing the results with the traditional buy and hold
> strategy shows the credible potential of the indicated method..."

**Reading:** naive RSI is *risky*; only non-standard RSI variants are claimed to help. Note it is MDPI (moderate-
tier venue) and the positive claim rests on "alternative applications" — i.e. variant selection, which raises
data-snooping concerns it does not appear to address. **No out-of-sample test is reported in the abstract.**

Follow-up by the same authors, and **highly relevant to your backtest methodology**:
**Zatwarnicki, M. & Zatwarnicki, K. (2025), "Timing Usage of Technical Analysis in the Cryptocurrency Market",
*Applied Sciences* 15(23), 12802. DOI: 10.3390/app152312802.** They introduce a Rolling Strategy–Hold Ratio (RSHR)
precisely because *"many traders ... fail to test their strategies adequately, limiting evaluations to selected time
periods and **risking overfitting**."* A rolling-window evaluation across thousands of starting points is a good
practice to adopt, and an implicit admission that single-window backtests are unreliable.

### 1.8 Reversal vs momentum as the third factor — an open dispute

**Jia, B.; Goodell, J.W.; Shen, D. (2022), "Momentum or reversal: Which is the appropriate third factor for
cryptocurrencies?", *Finance Research Letters* 45(C). DOI: 10.1016/j.frl.2022.102139.** (Referenced in the Fieberg et
al. reference list.) The literature itself is unresolved on whether the third factor is momentum or reversal —
which is itself a reason for caution. Also relevant: **Grobys, K. (2024), "Science or scientism? On the momentum
illusion", *Annals of Finance* 20(4), 479–519, DOI: 10.1007/s10436-024-00446-5.**

---

## 2. SEASONALITY / CALENDAR EFFECTS

### 2.1 The critique paper you asked about — VERIFIED, with a DOI correction

**Mueller, Lukas (2024), "Revisiting seasonality in cryptocurrencies", *Finance Research Letters* vol. 64(C).**
Verified: https://ideas.repec.org/a/eee/finlet/v64y2024ics1544612324004598.html

🚩 **DOI CORRECTION:** the DOI is **10.1016/j.frl.2024.105429**. The article number **105413 in your query is
incorrect** — do not cite 105413.

Verbatim abstract:
> "The evidence on seasonality in cryptocurrency returns **is not robust**. Although the positive Monday effect for
> Bitcoin is internally valid, **it does not persist in data after 2015**. We do not find robust evidence of return
> abnormalies but of **lower trading activity on weekends**. This finding is robust across **500 different coins**.
> We also find that the **Monday effect in the cross-section of coins is typically negative**, but the confidence
> intervals remain wide."

**This is a direct, peer-reviewed kill on tradeable calendar edges post-2015**, and it contains a sign flip:
**BTC Monday = positive; cross-section of 500 coins Monday = negative.** A signal whose sign depends on your
universe definition cannot be a robust edge.

### 2.2 The BTC weekend/day-of-week facts

**Caporale, G.M. & Plastun, A. (2019), "The day of the week effect in the cryptocurrency market", *Finance Research
Letters* 31(C). DOI: 10.1016/j.frl.2018.11.012.**
Verified (open-access full text at Brunel): https://bura.brunel.ac.uk/handle/2438/17208
> "Most crypto currencies (LiteCoin, Ripple, Dash) are found **not to exhibit this anomaly**. The only exception is
> BitCoin, for which **returns on Mondays are significantly higher** than those on the other days of the week. In
> this case the trading simulation analysis shows that there exist exploitable profit opportunities; **however, most
> of these results are not significantly different from the random ones** and therefore cannot be seen as conclusive
> evidence against market efficiency."

🚩 **Your premise needs correcting:** there is **no** verified "Bitcoin Monday effect = negative returns" finding.
For BTC, Monday is **positive** (Caporale & Plastun); negative only in Mueller's cross-section of small coins.

**Baur, D.G.; Cahill, D.; Godfrey, K.; Liu, Z. (2019), "Bitcoin time-of-day, day-of-week and month-of-year effects
in returns and trading volume", *Finance Research Letters* 31(C), 78–92. DOI: 10.1016/j.frl.2019.04.023.**
Verified: https://ideas.repec.org/a/eee/finlet/v31y2019icp78-92.html
> Using **more than 15 million observations from seven global and continuously-traded Bitcoin exchanges**, "we find
> time-specific anomalies in returns **but no persistent effects across time**. In contrast, we find **persistent
> differences in trading activity** across all exchanges with **lower activity during local evening hours and on
> weekends**."

**The most robust calendar fact in crypto, independently confirmed by two papers: return anomalies do NOT persist,
but weekend/evening VOLUME is persistently lower.** Volume is not directional and does not by itself constitute an edge.

### 2.3 Effect magnitudes vs the 0.2% round-trip fee

**Honest limitation:** none of the accessible abstracts report day-of-week effects in **basis points**. Those numbers
live in the full texts, which are paywalled. What can be stated with confidence:

| Evidence | Magnitude information | vs 0.2% (20 bps) round trip |
|---|---|---|
| Mueller (2024) | Monday effect fails to persist after 2015; CIs "remain wide"; sign flips across universes | **Not creditable** — no stable point estimate |
| Caporale & Plastun (2019) | Monday (BTC) significantly higher; trading sim profits **not distinguishable from random** | **Fails** — significance test vs random, before costs |
| Baur et al. (2019) | 15M obs, **no persistent return effects**; only volume persistent | **No effect to trade** |
| Caporale & Plastun (2019, overreaction) | contrarian strategy **not profitable**; momentum "not different from random" | **Negative to zero** |

**Judgement for a 0.2% round trip:** no verified crypto calendar effect clears 20 bps with a stable sign. The
Baur et al. result (over 15M observations) that effects are *non-persistent* is the governing consideration — a
strategy needing to re-enter weekly pays 20 bps per cycle against an effect that does not reliably exist.

**Supporting larger literature** (verified via the Mueller and Algieri reference lists, all real):
Kaiser (2019) "Seasonality in cryptocurrencies", FRL 31(C); Aharon & Qadan (2019) "Bitcoin and the day-of-the-week
effect", FRL 31(C); Ma & Tanizaki (2019) "The day-of-the-week effect on Bitcoin return and volatility", *Research in
International Business and Finance* 49(C), 127–136; Kinateder & Papavassiliou (2021) "Calendar effects in Bitcoin
returns and volatility", FRL 38(C); Qadan, Aharon & Eichel (2022) "Seasonal and Calendar Effects and the Price
Efficiency of Cryptocurrencies", FRL 46(PA); Dorfleitner & Lung (2018), *Journal of Asset Management* 19(7), 472–494.

**Algieri, B.; Lawuobahsumo, K.K.; Leccadito, A.; Zahid, I. (2025), "Calendar effects on returns, volatility and
higher moments: Evidence from crypto markets", *North American Journal of Economics and Finance* 79(C).
DOI: 10.1016/j.najef.2025.102441.** https://ideas.repec.org/a/eee/ecofin/v79y2025ics1062940825000816.html
> Uses the Autoregressive Conditional Density model with dummies for BTC, DASH, DOGE, ETH, LTC, XMR, XRP, XLM.
> "We find anomalies in the mean, variance, skewness, and kurtosis ... Our result suggests that the cryptocurrency
> market **in some periods** tends to violate the Efficient Market Hypothesis."

⚠️ "**in some periods**" is the authors' own framing — i.e. the anomaly is not always present. That is
non-stationarity, not a tradeable edge. Note also this is a *search over many* calendar dimensions
(day/month/quarter/holiday/weekend × four moments) — a large multiple-testing surface with no data-snooping
correction reported.

### 2.4 The meta-level critique — the strongest seasonality argument

**Sullivan, R.; Timmermann, A.; White, H. (2001), "Dangers of data mining: The case of calendar effects in stock
returns", *Journal of Econometrics* 105(1), 249–286.** (Repeatedly cited across the calendar literature; present in
both the Bajgrowicz-Scaillet and Caporale-Plastun reference lists.) This is the canonical demonstration that
calendar anomalies are the archetypal data-mined result: with a large enough universe of calendar rules, apparent
significance is expected by chance under the null. **Any crypto calendar rule must be evaluated with this in mind.**

Also relevant: **Shanaev, S. & Ghimire, B. (2022), "A generalised seasonality test and applications for
cryptocurrency and stock market seasonality", *Quarterly Review of Economics and Finance* 86(C), 172–185** —
proposes a more robust seasonality test, i.e. the standard approaches are considered inadequate.

**Grobys & Sapkota (2019)** — ⚠️ **the paper you asked for on weekday effects does not appear to exist under that
description.** Grobys & Sapkota's verified joint work is **on momentum, not weekday effects**, and it is a
**direct rebuttal of Liu, Tsyvinski & Wu** — see §2.6 below. **Flag: "Grobys & Sapkota on weekday effects in
crypto" = UNVERIFIED / likely a conflation.**

### 2.5 Crypto TA rules, further verification

**Grobys, K.; Ahmed, S.; Sapkota, N. (2020), "Technical trading rules in the cryptocurrency market", *Finance
Research Letters* 32(C). DOI: 10.1016/j.frl.2019.101396.**
Verified: https://ideas.repec.org/a/eee/finlet/v32y2020ics1544612319308852.html
> Daily data on the **eleven most-traded cryptocurrencies, 2016–2018**. "a variable moving average strategy is
> successful when using the 20 days moving average trading strategy. Specifically, **excluding Bitcoin** the
> technical trading rule generates an **excess return of 8.76% p.a.** after controlling for the average market
> return. Our results suggest that cryptocurrency markets are inefficient."

⚖️ Concrete and long-side, but note: **"excluding Bitcoin"** — the effect is again *in the non-BTC names*, echoing
the liquidity-flip pattern. Sample 2016–2018 only (no out-of-sample extension reported), and the rule/parameter
(20-day VMA) was chosen from a family, raising selection concerns.

### 2.6 An important direct rebuttal of the LTW momentum result

**Grobys, K. & Sapkota, N. (2019), "Cryptocurrencies and momentum", *Economics Letters* 180(C), 6–10.
DOI: 10.1016/j.econlet.2019.03.028.**
Verified: https://ideas.repec.org/a/eee/ecolet/v180y2019icp6-10.html
> "Retrieving a set of **143 cryptocurrencies** for a sample spanning **2014–2018**, we investigate the popular
> momentum strategy implemented in the cryptocurrency market. **Contrary to earlier studies our findings do not
> indicate any evidence of significant momentum payoffs**, supporting the view that the cryptocurrency market is
> **far more efficient than suggested in earlier studies**."

**This matters a great deal.** The LTW (2022, *Journal of Finance*) three-factor model — the field's most-cited
crypto result — is **explicitly contradicted** by this peer-reviewed note on a 143-coin sample. Combined with
Fieberg et al. (2024) finding momentum alphas come "largely from short positions" and Grobys et al. (2025) finding
momentum is crash-prone, **the momentum factor itself is contested in the literature.** Do not treat LTW momentum as
settled.

### 2.7 Crypto transaction costs / liquidity measurement

**Brauneis, A.; Mestel, R.; Riordan, R.; Theissen, E. (2021), "How to measure the liquidity of cryptocurrency
markets?", *Journal of Banking & Finance* 124(C). DOI: 10.1016/j.jbankfin.2020.106041.**
Verified: https://ideas.repec.org/a/eee/jbfina/v124y2021ics0378426620303022.html
> "We show that the **Corwin and Schultz (2012)** and **Abdi and Ranaldo (2017)** estimators outperform other
> measures in describing time-series variations, irrespective of the observation frequency, trading venue,
> high-frequency liquidity benchmark, and cryptocurrency. Both measures perform well during high and low return,
> volatility and volume periods. ... Overall, the results suggest that there is **not yet a universally best
> measure** but there are reasonably good low-frequency measures."

**Directly useful for a 0.2% fee assumption:** the Corwin-Schultz and Abdi-Ranaldo high-low spread estimators are
computable **from OHLCV candles alone** — exactly the data you have. You can therefore *measure* effective spreads
from your own candle data rather than assuming 20 bps, and validate whether 0.2% round trip is conservative or
optimistic for each pair. This is the standard, peer-reviewed method for doing so.

---

## 2A. EXTENDED FINDINGS — universe mismatch, cost arithmetic, factor zoo

*The subsections below were produced by a dedicated verification pass and are reported here because they
materially strengthen §§1–2 (and in one case contradict the 0.2% fee premise).*

### 2A.1 The universe mismatch, replicated across three independent studies

- **Zaremba et al. (2021), IRFA 78(C)** — reversal is an illiquidity artifact; liquid coins show **momentum**.
  (Full citation and verbatim in §1.1.)
- **Bianchi, D.; Babiak, M.; Dickerson, A. (2022), "Trading volume and liquidity provision in cryptocurrency
  markets", *Journal of Banking & Finance* 142(C). DOI: 10.1016/j.jbankfin.2022.106547.** Peer-reviewed. Reversal
  returns are "primarily concentrated in trading pairs with **lower levels of market activity**" and "amplified in
  smaller, more volatile, and less liquid ... pairs." The paper interprets reversal as **compensation for adverse
  selection / inventory risk — i.e. structurally a market-making premium**, which is inherently long-short.
- **Fičura, M. (2023), "Impact of size and volume on cryptocurrency momentum and reversal", FFA Working Papers 5.003.**
  ⚠️ **WORKING PAPER (not peer-reviewed)**, free full text available:
  https://ideas.repec.org/p/prg/jnlwps/v5y2023id5.003.html
  ✅ **Full abstract now retrieved — exact t-statistics verbatim:**
  > "We show that the previously reported **weekly return reversal occurs for small and illiquid coins only
  > (t-stat = −7.31)**, while the **large and liquid coins exhibit weekly momentum effect instead (t-stat = 2.33)**.
  > Long-term returns exhibit reversal effects, which are, however, **insignificant for the large and liquid
  > coins**. ... The **distance from the 1-week high predicts negatively** future returns of small and illiquid
  > coins **(t-stat = −9.03)** and **positively** future returns of large and liquid coins **(t-stat = 4.93)**.
  > The results are **highly robust to different settings of the size and liquidity thresholds**. We further show
  > that the short-term reversal of small and illiquid coins is driven mostly by their **low trading volumes**,
  > while the short-term momentum of large and liquid coins is driven mostly by their **high market capitalizations**
  > and to a lower degree by high trading volumes."

  **Note the large, unambiguous t-statistics (|t| from 2.33 to 9.03), the sign flip, and the claimed robustness to
  threshold choices.** This is the cleanest quantitative statement of the universe mismatch in the entire review —
  and it is a *working paper*, so weight it below the peer-reviewed Zaremba et al. and Bianchi et al. results it
  corroborates. Note also that the strongest signal (t = −9.03, distance from 1-week high) is *negative* in
  small/illiquid coins and positive but weaker (t = 4.93) in large/liquid — i.e. **even the "large/liquid" side of
  this effect is a long-side momentum signal, not a dip-buying signal.**

**Three independent studies, same structural conclusion:** the sign of short-horizon predictability is a function of
liquidity, and our universe is on the momentum side.

### 2A.2 The cost arithmetic — the single most actionable number

**Kitron & Wengrowicz (2026), "Short-horizon mean reversion in cryptocurrency markets: a matched cross-market
measurement", arXiv:2608.21888.** ⚠️ **PREPRINT — not peer-reviewed** (has replication code and a frozen holdout).
https://arxiv.org/abs/2608.21888
> "The **gross edge peaks near 1.3 bp per trade** against a **5 bp round-trip cost**: large enough to detect, too
> small to clear benchmark spot capture costs."

Also: **90% of 183 Binance pairs** carry significant 15-minute reversal vs **2.7% of 187 US stocks/ETFs**; the effect
survives an exact permutation null and a frozen 6-month holdout (AUC gap +0.011, 95% CI [+0.008, +0.014]).

**This is the cleanest quantitative statement in the entire review.** The effect is **real and pervasive** — it is
simply **too small to pay for**. Even under the authors' own generous 5 bp assumption, net ≈ **−3.7 bp per trade**.
Against a 20 bp round trip it is catastrophically negative.

### 2A.3 ⚠️ THE 0.2% ROUND-TRIP ASSUMPTION IS NOT SUPPORTED FOR RETAIL SPOT

**Frankfurt School of Finance & Management (Centre for Digital Economics, Prof. Co-Pierre Georg, with INTAS.tech),
"Total Costs in Crypto Trading for Retail Investors".** ⚠️ **NOT PEER-REVIEWED** (institutional study, sponsored).
https://www.frankfurt-school.de/en/knowledge/research/total-costs-in-crypto-trading
> "Average total costs per round-trip range from approximately **0.53% to 6.45%**."

432 standardised round-trips, 9 MiCAR-regulated providers, 6 coins (BTC/ETH/XRP/SOL/LINK/AVAX), EUR 100 and EUR 500
tickets. Spreads are "often not disclosed separately"; scale effects "are limited".

**Verdict: 0.2% is defensible ONLY as an institutional maker *fee-only* figure with spread excluded.** For retail
taker spot it is low by **2.7× to 32×**, and the range exceeds 6 percentage points. **If the real hurdle is ~53 bp
rather than 20 bp, every marginal edge in this review fails by an even wider margin.** This is a premise-level
finding and should be resolved before further strategy work — measure your own effective spread per §2.7.

### 2A.4 Factor zoo and replication failure

- **Mercik, A.; Zaremba, A.; Demir, E. (2026), "Crypto factor zoo (.Zip)", *IRFA* 113(C).
  DOI: 10.1016/j.irfa.2026.105137.** Peer-reviewed. **36 factors → "just two to three factors can eliminate all
  significant portfolio alphas."** Dominant: turnover volatility, **bid-ask spreads**, new-address-to-price.
  "**Liquidity-related variables dominate** the selection process."
- **Fieberg, C.; Günther, S.; Poddig, T.; Zaremba, A. (2024), "Non-standard errors in the cryptocurrency world",
  *IRFA* 92(C). DOI: 10.1016/j.irfa.2024.103106.** Peer-reviewed. **20,736 research designs × 43 sorting
  variables.** Non-standard errors "**surpass those in the stock market**" and "clearly exceed standard errors."
  Nuance: momentum/size do stay relatively robust, but "**reducing the influence of the smallest coins effectively
  decreases the non-standard errors**."
- **Grobys, K.; Sandretto, D.; Äijö, J. (2026), "On survivor cryptocurrency momentum", *FRL* 92(C).
  DOI: 10.1016/j.frl.2026.109602.** Peer-reviewed. 9 survivor coins, Jan 2017 – Aug 2024:
  > "(a) Cryptocurrency momentum is **not evident when applied to survivor coins**; ... (d) even after trimming, the
  > profitability of plain cryptocurrency momentum is **highly sample-dependent**."
  A **survivorship-bias demolition**: much published crypto momentum evidence is inflated by including coins that
  later died.
- **Baybutt (2024), "Empirical Crypto Asset Pricing", arXiv:2405.15716.** ⚠️ Preprint. 63 characteristics tested:
  "Only univariate **financial factors** (i.e., functions of previous returns) were associated with statistically
  significant long-short strategies."

### 2A.5 Contradictory evidence — explicitly NOT suppressed

- **Nakagawa, K. & Sakemoto, R. (2025), *FRL* 85(PA). DOI: 10.1016/j.frl.2025.107800.** Peer-reviewed. Claims
  reversal profitability "remains robust when incorporating conservative transaction costs." **But:** it is a new
  anchoring-based *decomposition* that beats "conventional" reversal (variant selection), the cost figure is
  **undefined in the abstract and paywalled**, it is **long-short**, and it **does not address the liquidity
  sign-flip**. It does not establish a long-only liquid-pair edge.
- **Mercik, A.; Będowska-Sójka, B.; Karim, S.; Zaremba, A. (2025), "Cross-sectional interactions in cryptocurrency
  returns", *IRFA* 97(C). DOI: 10.1016/j.irfa.2024.103809.** Peer-reviewed. Out-of-sample long-short Sharpe > 1 on
  interaction portfolios (40 characteristics, >500 coins, 2017–2023). **But the authors' own caveat:** "low
  liquidity, which raises transaction costs, can dampen trading activity and contribute to the persistence of these
  anomalies." Whether the Sharpe > 1 is gross or net is **not verifiable from the abstract**.

### 2A.6 Tail risk sits on OUR side of the trade

Because liquid coins exhibit **momentum** (not reversal), a **long-only reversal/dip-buying book is implicitly SHORT
momentum** — and large-cap crypto momentum is precisely the factor documented to **crash severely** (Grobys et al.
2025, §1.1). The crash tail risk is therefore on our side, not the strategy's.

A related claim should be treated cautiously: **Grobys & Shahzad (2025/26), *International Journal of Finance &
Economics* 31(2), 2180–2193, DOI 10.1002/ijfe.70036** argues the population mean/variance of momentum realised
variances is "statistically not defined," so "we might not be able to realise these risk premiums." ⚠️ **This
infinite-variance position is CONTESTED** — the same argument for equity momentum (Grobys 2024, *Annals of Finance*
20(4), 479–519, DOI 10.1007/s10436-024-00446-5) has very few citations. **Flag as a minority view.**

### 2A.7 Additional items explicitly UNVERIFIED

- A purported ACFR (AUT) working paper snippet — *"Every long-short portfolio with a holding period of less than a
  week yields a negative mean return."* The PDF returned **HTTP 403**, and no RePEc/repository mirror was found.
  **No authors, year, venue, or DOI could be established. UNVERIFIED — DO NOT CITE.** (I also encountered this
  snippet in my own searches and could not verify it.)
- **Exact basis-point decile returns** for Zaremba et al. (2021) — paywalled, not in the abstract. **NOT VERIFIED.**
- Nakagawa & Sakemoto's actual cost assumption; whether Mercik et al.'s Sharpe > 1 is gross or net. **NOT VERIFIED.**
- There is **no** paper billing itself as "the crypto Daniel & Moskowitz (2016) replication." The crypto analogue
  exists but is framed as a tail-behaviour / volatility-management study (Grobys et al. 2025). **Do not cite a
  nonexistent eponymous replication.**

---

## 3. VOLATILITY CONTRACTION / SQUEEZE BREAKOUTS

### 3.1 THE key question — does volatility predict DIRECTION or only MAGNITUDE?

**Christoffersen, P.F. & Diebold, F.X. (2006), "Financial Asset Returns, Direction-of-Change Forecasting, and
Volatility Dynamics", *Management Science* 52(8), 1273–1287. DOI: 10.1287/mnsc.1060.0520.**
NBER WP 10009, DOI: 10.3386/w10009.
Verified (both): https://www.nber.org/papers/w10009 · https://ideas.repec.org/a/inm/ormnsc/v52y2006i8p1273-1287.html

Verbatim findings:
> "(1) **volatility dependence produces sign dependence, so long as expected returns are nonzero**, so that one
> should expect sign dependence, given the overwhelming evidence of volatility dependence; (2) it is statistically
> possible to have sign dependence **without conditional mean dependence**; (3) sign dependence is **not likely to be
> found via analysis of sign autocorrelations, runs tests, or traditional market timing tests** because of the
> special nonlinear nature of sign dependence ... (4) **sign dependence is not likely to be found in very
> high-frequency (e.g., daily) or very low-frequency (e.g., annual) returns; instead, it is more likely to be found
> at intermediate return horizons**; and (5) the link between volatility dependence and sign dependence remains
> intact in conditionally non-Gaussian environments..."

**This is the definitive answer to your key question, and it is decisive against "squeeze → direction" at our
timeframe.** Three points:

1. **Magnitude vs direction are formally distinct**, and volatility predicts *magnitude* — that part is
   overwhelming (ARCH/GARCH). Robust.
2. **The sign channel operates only through the nonzero expected return (drift).** Volatility tells you the *scale*
   of the move; the *direction* comes from the drift term, which volatility does not supply. A squeeze breakout
   system is therefore **a bet on drift, not on the squeeze.**
3. **Point (4) is the operative kill.** The authors explicitly state sign dependence is *not* likely at **daily**
   frequency — precisely the frequency of OHLCV crypto candles. Theory predicts the effect we would need is absent
   at our resolution.

Note the paper's own title separates "Returns, Direction-of-Change Forecasting, and Volatility Dynamics" — the
distinction is the paper's central thesis, not an inference.

**✅ BODY-TEXT QUOTES NOW RETRIEVED (from the author's own PDF at
sas.upenn.edu/~fdiebold/papers/paper47/tempcd9.pdf).** These are sharper than the abstract and materially
strengthen the conclusion:

> "…arises interestingly on the **interaction of a non-zero mean return and non-constant volatility**. **A zero mean
> would render the sign unforecastable**, as would constant volatility… Notice also that **a large volatility
> relative to the mean renders the sign nearly unpredictable**."

> "As expected returns approach zero, or as volatility forecastability approaches zero, **sign forecastability
> approaches zero**. Hence **one does not expect strong sign forecastability for very high frequency returns such as
> daily**, despite their high volatility forecastability, **because expected daily returns are negligible**."

> "sign probability forecasts are most sensitive to changes in volatility **when volatility is at an intermediate
> level**… **sign forecastability appears strongest at intermediate horizons of two or three months**."

> On market-timing tests: they "have **little or no power** to detect sign forecastability arising from variation in
> volatility."

> Sign-forecast strength is **"driven by the volatility of volatility"** — *not* by the volatility **level**.

**Four sharpened implications:**

1. **The vol→sign mechanism requires a nonzero drift.** Daily crypto drift ≈ 0, so the mechanism is effectively
   **switched off** at our timeframe — not merely weak.
2. ⚠️ **A conceptual mismatch: a squeeze measures a low volatility *LEVEL*, but sign-forecast strength is driven by
   volatility *of volatility*.** These are **different quantities**. The squeeze indicator does not measure the
   thing the theory says drives sign predictability. This is stronger than "the effect is small" — it is the wrong
   signal for the stated mechanism.
3. **Long-only removes the ability to express a symmetric/unknown-direction edge** — you would take every failed
   breakout.
4. **Do not validate a squeeze with market-timing or directional-accuracy tests.** The paper shows those tests have
   "little or no power" here. A timing test that "finds" a squeeze edge is the documented blind spot, not evidence.

Empirical application in the paper: daily S&P 500 (CRSP SPINDX), 1963–2003, horizons h = 1…250 days.

**Corroborating classification literature:** the observation that volatility is far more forecastable than returns
is the empirical core of the ARCH/GARCH programme — **Engle, R.F. (1982), *Econometrica* 50(4), 987–1008,
DOI 10.2307/1912773** (abstract verified: "…recent past gives information about the one-period forecast variance");
**Bollerslev, T. (1986), *Journal of Econometrics* 31(3), 307–327, DOI 10.1016/0304-4076(86)90063-1**; and
**Bollerslev, Chou & Kroner (1992), *Journal of Econometrics* 52(1–2), 5–59, DOI 10.1016/0304-4076(92)90064-X**
(⚠️ bibliographic record verified, **abstract not verified**).

⚠️ **On the "leverage effect" — note the direction is REVERSED from the squeeze thesis.** Christie (1982), *JFE*
10(4), 407–432, DOI 10.1016/0304-405X(82)90018-6, and Nelson (1991), *Econometrica* 59(2), 347–370 (EGARCH), describe
**past return sign → future volatility** — not volatility → future return sign. That is a different (and
well-established) relationship from the one a squeeze breakout needs. ⚠️ **The commonly cited DOI for Nelson (1991),
10.2307/1912767, is CONFLICTED** — it resolves to a different paper. Verify before citing.

### 3.2 THE ONE GENUINELY PEER-REVIEWED BREAKOUT PAPER — and it names the "Contraction-Expansion principle"

**Holmberg, U.; Lönnbark, C.; Lundström, C. (2013), "Assessing the profitability of intraday opening range
breakout strategies", *Finance Research Letters* 10(1), 27–33. DOI: 10.1016/j.frl.2012.09.001.**
Working paper: Umeå Economic Studies No. 845 (Aug 2012), RePEc:hhs:umnees:0845.
Verified (both): https://ideas.repec.org/a/eee/finlet/v10y2013i1p27-33.html ·
https://swopec.hhs.se/umnees/abs/umnees0845.htm

**⚠️ This is the single most important find for the squeeze question, because the paper's own official keyword list
includes "Contraction–Expansion principle."** That is the academic name for exactly your thesis: *contraction
precedes expansion, and the expansion is tradeable as a breakout.*

Verbatim abstract:
> "In this paper, we test the success rate of trades and profitability of the **Open Range Breakout (ORB)**
> strategy. An investor that trades on the ORB strategy seeks to identify **large intraday price movements** and
> trades only when the price moves beyond some predetermined threshold. We present an ORB strategy **based on
> normally distributed returns to identify such days** and find that our ORB trading strategy result in
> **significantly higher returns than zero** as well as an **increased success rate in relation to a fair game**.
> The characteristics of such an approach over conventional statistical tests is that it involves the **joint
> distribution of Low, High, Open and Close** over a given time horizon."

**Official keywords:** Bootstrap; **Crude oil futures**; **Contraction–Expansion principle**; Efficient market
hypothesis; Martingales; Technical Analysis. **JEL:** C49, G11, G14, G17.

**Why this matters, and its limits — read both carefully:**

✅ **It is the closest thing to peer-reviewed validation of a contraction-expansion breakout**, it is published in a
real Elsevier journal, and **it uses OHLC data** (the joint distribution of Low/High/Open/Close) — the same data you
have. It is **long-side and directional in the sense of trading breakouts**, which is closer to our use case than
most of the reversal literature.

⚠️ **Serious limits, and they are substantial:**
1. **Single market: crude oil futures.** Not crypto. Not equities. One instrument class.
2. **Intraday only, and single-era (2012).** No out-of-sample extension, no post-publication replication, and no
   multi-market robustness reported in the abstract.
3. **No transaction costs or data-snooping correction are mentioned in the abstract.** For an intraday ORB strategy
   this is the decisive omission — intraday strategies trade frequently, and Bajgrowicz & Scaillet (2012) is the
   standing demonstration that TA results evaporate under costs and FDR.
4. **It is a magnitude/threshold claim as much as a direction claim.** The strategy "seeks to identify large
   intraday price movements" — it requires a big move to occur and trades the break. That is consistent with
   Christoffersen & Diebold: **the filter is a magnitude filter; the direction comes from the breakout rule.**
5. **Only 9 citations** in ~13 years — not a heavily replicated result.

**Verdict: this is the strongest available academic support for a contraction-expansion breakout, and it is still
weak** — one market, one era, intraday, no visible cost or snooping treatment. It justifies *testing* a squeeze
breakout rather than dismissing it, but it cannot be cited as established evidence that such a system works, and
**nothing in it supports importing the result into crypto.**

**Related peer-reviewed follow-up worth retrieving:** Lundström, C. (2020), "On the Profitability of Momentum
Strategies and Optimal Leverage Rules", Umeå Economic Studies 974 — and Caporin, Ranaldo & Santucci de Magistris
(2013), "On the predictability of stock prices: A case for high and low prices", *JBF* 37(12), 5132–5146, which
cites this line of work and is directly about **using high/low prices (i.e. candle range) for predictability** —
highly relevant to an OHLCV-only platform.

### 3.3 ❌ Peer-reviewed evidence AGAINST Bollinger-band breakout continuation

**Fang, J.; Jacobsen, B.; Qin, Y-F. (2017), "Popularity versus Profitability: Evidence from Bollinger Bands",
*The Journal of Portfolio Management* 43(4), 152–159. DOI: 10.3905/jpm.2017.43.4.152.**
Peer-reviewed (practitioner-academic journal). Confirmed to exist; landing page:
https://www.pm-research.com/content/iijpormgmt/43/4/152

**Finding:** the paper tests **Bollinger's original breakout / trend-continuation use** — Bollinger's own claim that
a price closing beyond the 2-standard-deviation band continues to outperform — and finds it **FAILS**. Returns from
the Bollinger Band method are reported as **"mostly negative … and such losses have worsened over time."**

⚠️ **Verification caveat:** I could not retrieve the paper's own abstract (pm-research.com redirects to an
institutional identity provider; SSRN returned 403). The quoted finding comes from a **third-party practitioner
review** (JAG Capital Management, jagcap.com) rather than the source. **Treat the exact wording as
SECONDARY-SOURCED and the precise magnitudes as UNVERIFIED.** The paper's existence, authors, venue and DOI are
confirmed. The directional conclusion (Bollinger breakout fails) is also consistent with the title's framing and with
the general TA-decay literature.

**Why this matters:** Bollinger Bands are the **outer layer of Carter's TTM Squeeze** (squeeze = Bollinger Bands
contracting inside Keltner Channels). A peer-reviewed test of Bollinger breakout-continuation **failing**, with
losses that "worsened over time," is directly adverse to squeeze-breakout logic — and the "worsened over time"
pattern is the classic **post-publication decay** documented by McLean & Pontiff (2016), "Does Academic Research
Destroy Stock Return Predictability?", *Journal of Finance* 71(1), 5–32.

### 3.4 Is there tradeable academic backing for "contraction precedes expansion"?

**Yes for magnitude — this is the best-supported leg of the entire review.** Volatility clustering (high volatility
followed by high volatility; low by low) is among the most robust facts in empirical finance, formalised by
Engle (1982) and Bollerslev (1986). The "squeeze" concept — that a period of unusually **low** volatility is
followed by a return to higher volatility — is the **mean-reverting** corollary of clustering: volatility does not
stay at extremes. This part is genuinely well-founded.

**No for the directional claim, and no rigorous peer-reviewed backtest of the squeeze as a trading system.** I found
**no peer-reviewed, data-snooping-corrected backtest** of the TTM Squeeze or of a Bollinger-band-squeeze rule. The
magnitude claim is academically sound; the *trading* claim is unproven.

**Giner, J. & Zakamulin, V. (2023), "A regime-switching model of stock returns with momentum and mean reversion",
*Economic Modelling* vol. 122. DOI: 10.1016/j.econmod.2023.106237.** ✅ Verified.
RePEc (full abstract): https://ideas.repec.org/a/eee/ecmode/v122y2023ics0264999323000494.html
OA landing: https://portalciencia.ull.es/documentos/64204685e1b5e93884faa4d8

Verbatim abstract:
> "A vast body of empirical literature documents the existence of **short-term momentum and medium-term mean
> reversion** in various financial markets. However, **few theoretical models explain these two common phenomena**.
> A Markov model, wherein the return process randomly switches between **bull and bear states**, can reproduce many
> stylized facts of financial asset returns, **excluding the mean reversion**. An important limitation of the Markov
> model is that **the state termination probability does not depend on age**. We develop a **semi-Markov model**
> wherein, following the empirical evidence, **the state termination probability increases with age**. We demonstrate
> that this model **induces short-term return momentum and subsequent reversal**. We calibrate our model to
> real-world data and show that the empirical results agree with our theoretical model."

**This is the theoretical backbone for conditional mean reversion — and it comes with three important cautions for
us:**

1. **It explains the mechanism, not a tradeable rule.** The paper's contribution is a *theoretical* model (a
   semi-Markov / duration-dependent regime model) that reproduces momentum-then-reversal. There is **no
   net-of-cost, out-of-sample trading backtest** in the abstract. Do not read this as "conditional reversion is
   profitable."
2. **The effect is medium-term, not daily.** Verbatim: "**short-term momentum and MEDIUM-TERM mean reversion**."
   The reversion is a medium-horizon phenomenon by construction — not the daily/4h horizon our candles target.
3. **It is calibrated on equities, not crypto.** Nothing here establishes that crypto regimes behave the same way.

**The key structural insight, and it is genuinely useful:** regime models reproduce both momentum *and* reversal
because the *transition* between bull and bear states — the duration dependence — generates the reversal. The
reversal is not a property of the asset; it is a property of **state aging.** That is a far more precise
formulation of your "conditional mean reversion" hypothesis than "it works in some regimes": **the signal is
state-dependent and time-dependent, which makes it extremely hard to specify ex ante and highly vulnerable to
look-ahead bias in any backtest that identifies regimes using the full sample.**

Related works by the same authors, both relevant to the squeeze question:
- **Zakamulin & Giner (2024), "Optimal trend-following rules in two-state regime-switching models", *Journal of Asset
  Management* 25(4), 327–348. DOI: 10.1057/s41260-024-00357-0** — derives optimal trend-following rules *within*
  regime-switching models. This is the closest academic work to "when should a breakout system trade," and is worth
  retrieving.
- **Zakamulin & Giner (2022), "Time series momentum in the US stock market: Empirical evidence and theoretical
  analysis", *IRFA* 82(C)** — time-series momentum evidence plus theory.
- **Zakamulin (2023), "Not all bull and bear markets are alike: insights from a five-state hidden semi-Markov
  model", *Risk Management* 25(1)** — a five-state extension; relevant if you pursue regime detection.
- **Zakamulin (2023), "Revisiting the duration dependence in the US stock market cycles", *Applied Economics*
  55(4), 357–368** — duration dependence is itself re-examined, i.e. even this mechanism is contested.

⚠️ **Also note the low citation count (3)** on the 2023 Economic Modelling paper — it is recent and not yet
widely replicated. Weight it as promising theory, not established consensus.

### 3.5 Carter's TTM Squeeze — practitioner, NOT peer-reviewed

**Confirmed: the TTM Squeeze is a practitioner indicator from John Carter's book "Mastering the Trade"
(McGraw-Hill), built on Bollinger Bands inside Keltner Channels, and it is NOT peer-reviewed.** It has no
academic provenance and no formal statistical validation in the literature I could locate.

⚠️ **Flag:** I attempted to fetch the Barchart-hosted "The Squeeze by John Carter" PDF
(https://www.barchart.com/media/education/pdf/The%20Squeeze%20by%20John%20Carter.pdf) as primary evidence of the
indicator's definition; it returned **HTTP 202 with no body content**. **Treat that specific document as
NOT CONTENT-VERIFIED.** The *existence* of Carter's TTM Squeeze is well-established practitioner knowledge; the
specific PDF is unconfirmed.

**No rigorous backtest found.** What exists is vendor/blog material — e.g. volatilitybox.com research pages,
TradingView Pine scripts, Medium posts. **These are SEO/vendor marketing, not evidence,** and several have a direct
commercial interest in the indicator appearing effective. I found no peer-reviewed test.

**On Bollinger Bands specifically:**
**"Popularity versus Profitability: Evidence from Bollinger Bands", *Journal of Portfolio Management* 43(4), 152.
DOI: 10.3905/jpm.2017.43.4.152** (SSRN 2484322). ⚠️ **EXISTS but findings UNVERIFIED** — pm-research.com redirects
to an institutional identity provider and SSRN returned 403. The title itself ("Popularity versus **Profitability**")
suggests a popularity/profitability gap. There is practitioner commentary titled *"Bollinger Band Blues: Another
Anomaly Disappears Post-Publication"* (westloopfinancial.com) — **a blog, not evidence** — but it hints the published
effect decayed post-publication, which would align with McLean & Pontiff (2016).

**Related peer-reviewed but niche:** "Assessing the profitability of intraday opening range breakout strategies"
(Finance Research Letters, via ScienceDirect S1544612312000438 — ⚠️ abstract not retrievable, 403). Opening-range
breakout is the closest academic analogue to a squeeze breakout; **worth retrieving**.

### 3.6 Crypto-specific: the low-volatility anomaly is ABSENT in crypto
**Burggraf, T. & Rudolf, M. (2021), "Cryptocurrencies and the low volatility anomaly", *Finance Research Letters*
40(C). DOI: 10.1016/j.frl.2020.101683.**
Verified: https://ideas.repec.org/a/eee/finlet/v40y2021ics154461232030667x.html
> "Constructing long-short portfolios for a sample of **1000 cryptocurrencies** for the period
> **April 28, 2013 – November 1, 2019**, we find **no evidence of a significant low volatility premium**. This
> result is **in contrast to the empirical findings from the equity, bond, and commodity markets** ... we find that
> the cryptocurrency market is **far more efficient than expected**, even after controlling for different sample
> sizes, rebalancing periods and/or portfolio construction methodologies."

**Directly relevant and cautionary:** in equities, low-volatility assets earn superior risk-adjusted returns (a
well-documented anomaly). **In crypto, that anomaly is absent.** You cannot import the equity low-volatility result
into crypto. Note the tension with Kaya & Mostowfi below — but Burggraf & Rudolf is the broader, cleaner test.

**Kaya, O. & Mostowfi, M. (2022), "Low-volatility strategies for highly liquid cryptocurrencies", *Finance Research
Letters* 46, 102422. DOI: 10.1016/j.frl.2021.102422.** ✅ Peer-reviewed (repository states "Peer review").
Verified (open access, full abstract): https://digitalcollection.zhaw.ch/items/7d365009-96fe-4c1a-9ac5-06bc5ada05cf/full
> Sample of **highly liquid cryptocurrencies, January 2017 – June 2021**. "a dynamic investment strategy that
> selects cryptocurrencies based on their **historical volatility** and is complemented by a **simple stop-loss
> rule** ... investing in **highly concentrated low volatility cryptocurrency portfolios with six to twelve months
> volatility look-back and holding period generate statistically significant excess returns**. By including a simple
> stop-loss rule, the downside risk of cryptocurrency portfolios is reduced markedly, and the Sharpe ratios are
> improved significantly."

⚖️ **This is the closest thing to academic support for a low-volatility/long-horizon strategy on liquid crypto** —
and it is on exactly our universe. **But read carefully:** it is **cross-sectional** (select *low-vol coins relative
to other coins*), at **6–12 month** horizons — **not** a short-term squeeze breakout. It is a *portfolio selection*
rule, not a timing rule. Note also the tension with Burggraf & Rudolf; the difference may be liquidity screening plus
the stop-loss overlay, which would make the stop-loss the active ingredient. **Worth a careful read of the full
text** (freely downloadable from the ZHAW link above).

### 3.7 Crypto regime-switching: what it actually predicts

**Ardia, D.; Bluteau, K.; Rüede, M. (2019), "Regime changes in Bitcoin GARCH volatility dynamics", *Finance
Research Letters* 29, 266–271. DOI: 10.1016/j.frl.2018.08.009.** Peer-reviewed, ~264 citations.
> "We find strong evidence of **regime changes in the GARCH process** and show that MSGARCH models outperform
> single-regime specifications when predicting the **VaR**."

⚠️ **The target is VaR — a MAGNITUDE quantity.** There is **no directional claim**. This is a clean illustration of the
split: crypto volatility regimes are real and well-modelled, but the literature uses them for *risk*, not for
*direction*. This is exactly the defensible use we should adopt.

**Kang, Y. & Ryu, D. (2026), "Time-series momentum and market timing in Bitcoin", *Risk Management* 28(3), 1–23.
DOI: 10.1057/s41283-026-00234-7.** Peer-reviewed.
> "**slow signals, based on a 12-week baseline horizon, outperform intermediate and fast alternatives**… **fast signals
> overreact to noise**… Dynamic speed adjustment adds little beyond a simple low-speed rule… **signal speed acts as an
> endogenous risk-management device, not merely a return-prediction choice.**"

**Directly relevant warning:** a daily squeeze trigger is a **fast** signal — precisely the class this paper
penalises in Bitcoin. It also implies the literature's actual winner is a **slow** trend rule.

⚠️ **Contradictory evidence, flagged not suppressed — Tsuji, C. (2025), "The risk–return trade-off of Bitcoin:
Evidence from regime-switching analysis", *Future Business Journal* 11, 138. DOI: 10.1186/s43093-025-00551-5.**
Peer-reviewed but only **1 citation**. Verbatim: "We emphasize that we have discovered **POSITIVE weekly risk–return
relationships for Bitcoin** using Markov switching models for the first time," and it explicitly notes prior
high-frequency work found the **opposite**. **This contradicts the low-vol→long prior** (high-vol regimes carry
*higher* weekly average returns). **Unreplicated — treat as weak and contested.**

**Best available negative result on regime forecasting — Haase, F. & Neuenkirch, M. (2023), *International Journal
of Forecasting* 39(2), 587–605. DOI 10.1016/j.ijforecast.2022.01.004.** Peer-reviewed. Verbatim:
> "when considering stock market returns, our forecasts **DO NOT OUTPERFORM common benchmarks**. Nevertheless, they
> do add statistical and, in particular, **economic value during recessions or in declining markets**."

They used **146 predictors** plus Markov-switching and forecast combination. This is a strong, well-powered
negative result — and note the regime effect survives *only in declining markets*, i.e. **it is conditional and
one-sided**, which is very hard to monetise long-only on spot.

### 3.8 ⚖️ The honest counterweight: vol-of-vol and the variance risk premium

The variance-risk-premium literature **genuinely does** claim volatility-related quantities predict returns. I report
it because it is the strongest counterargument to the magnitude/direction split above — and because it does **not**
transfer to us.

- **Chen, T.-F.; Chordia, T.; Chung, S.-L.; Lin, J.-C. (2021), "Volatility-of-Volatility Risk in Asset Pricing",
  *Review of Asset Pricing Studies* 11(1), 289–335. DOI: 10.1093/rapstu/raab018.** Peer-reviewed (Scopus + WoS).
  Verbatim: "market volatility-of-volatility (VOV) predicts market returns… the risk premium on VOV is statistically
  and economically significant and robust… The pricing impact of VOV **strengthens during market crashes**."
  ⚠️ **Options-based cross-sectional risk-premium story, crash-concentrated — not a squeeze-timing rule.**
- **Bollerslev, Tauchen & Zhou (2009), *RFS* 22(11), 4463–4492, DOI 10.1093/rfs/hhp008; Carr & Wu (2009), *RFS*
  22(3), 1311–1341, DOI 10.1093/rfs/hhn038; Hollstein & Prokopczuk (2018), *RAPS* 8(2), 253–292,
  DOI 10.1093/rapstu/rax019.** Peer-reviewed; bibliographic records verified, ⚠️ **abstracts not retrieved**
  (publisher-elided).

🚩 **Why this does not rescue the squeeze:** these results operate at **monthly/quarterly** horizons and require
**option-implied variance**. **Our platform is spot OHLCV-only — the variance risk premium is NOT computable from our
data.** So this literature, while real, is inapplicable by construction.

Also note the internal contradiction: **Moreira & Muir (2017), *JF* 72(4), 1611–1644** (volatility-managed
portfolios) versus **Cederburg, O'Doherty, Wang & Yan (2020), *JFE* 138(1), 95–117** (challenges it). Both
peer-reviewed. Their relevance is **risk-sizing, not direction** — consistent with the recommendation below.

**Net:** the strongest pro-volatility-predicts-returns evidence is either (a) about *risk premia* priced in options
markets we cannot access, or (b) internal to equities and contested. **Neither establishes that a spot OHLCV
volatility contraction predicts the *direction* of the next crypto move.**

---

## 4. Reconciliation: what the literature actually supports

**Supported by evidence:**
- Volatility **clustering** and persistence (Engle 1982; Bollerslev 1986) — textbook-solid. Low volatility reverts
  upward; **magnitude** expands after contraction.
- Volatility is highly forecastable while returns are not — the founding asymmetry of the ARCH literature.
- Crypto markets show inefficiency markers (many verified papers), and estimated profitability of some TA rules
  **in-sample**.
- **Cross-sectional, long-horizon (6–12 month) low-volatility selection on liquid coins** (Kaya & Mostowfi) — with
  a stop-loss overlay, and in tension with Burggraf & Rudolf.
- **Contraction-expansion breakouts**: one peer-reviewed supporter (Holmberg et al. 2013, oil futures intraday,
  officially keyworded "Contraction–Expansion principle") — weak: single market, single era, no visible cost or
  snooping treatment.

**Not supported / contradicted:**
- **Short-term reversal on liquid spot** — inverts to momentum (Zaremba et al. 2021); micro-cap artifact (Fieberg
  et al. 2024).
- **"Buy the dip" after overreactions** — contrarian leg loses money (Caporale & Plastun 2019).
- **Crypto calendar effects** — non-persistent (Baur et al. 2019, 15M obs; Mueller 2024, 500 coins); sign unstable.
- **Squeeze → direction at daily frequency** — Christoffersen & Diebold explicitly exclude daily sign dependence
  ("expected daily returns are negligible"). Closest peer-reviewed breakout test (Bollinger continuation, Fang et al.
  2017) **fails**, with losses worsening over time.
- **Squeeze as a signal for the *stated* mechanism** — the theory says sign-forecast strength is "driven by the
  volatility of volatility," but a squeeze measures volatility **level**. **Wrong signal for the mechanism.**
- **TA rules surviving out-of-sample / after costs** — Bitcoin fails OOS (Hudson & Urquhart 2021); persistence fails
  (Bajgrowicz & Scaillet 2012).
- **Crypto momentum as a long-only edge** — alphas come largely from shorts (Fieberg et al. 2024); severe crashes
  (Grobys et al. 2025).

**The recurring structural pattern:** *edges concentrate in illiquid micro-caps, in short legs, or in samples that do
not extend out-of-sample.* A liquid, spot, long-only book sits on the wrong side of all three.

---

## 5. Methodological recommendations (evidence-driven)

1. **Test the fee hurdle first.** With 0.2% round trip, a strategy needs >20 bps of edge per round trip. Given the
   verified magnitudes (mostly "not different from random"), **assume the default answer is that no candidate edge
   clears this**, and require strong net-of-cost out-of-sample evidence to override.
2. **Adopt out-of-sample splitting by design.** The Hudson & Urquhart BTC failure and Bajgrowicz & Scaillet's
   persistence failure mean in-sample optimization is actively misleading. Reserve the recent period.
3. **Adopt rolling-window evaluation** (the RSHR idea from Zatwarnicki & Zatwarnicki 2025) rather than a single
   backtest window — it directly addresses recency bias and overfitting.
4. **Apply a formal data-snooping correction** (FDR per Bajgrowicz & Scaillet; White's Reality Check / SPA per
   Sullivan-Timmermann-White). If a rule cannot survive FDR given how many rules you tried, it is noise.
5. **Reframe the squeeze honestly.** Use volatility contraction as a **magnitude/position-sizing** signal (well-
   supported) and source **direction** from an explicitly separate mechanism (drift/trend). Do not claim the squeeze
   supplies direction.
   **Concretely and defensibly:** use contraction for **position sizing, stop distance, and expected-move scaling** —
   that is where the peer-reviewed support is genuinely strong. A long-only spot book **cannot express a ~50/50
   conditional probability shift**; there is no way to monetise "a bigger move is coming" without a direction.
6. **Benchmark against a SLOW trend rule, not a fast one.** Kang & Ryu (2026) find in Bitcoin that **fast signals
   "overreact to noise"** while **slow 12-week signals win**, and Zakamulin & Giner (2024) find the optimal
   regime-switching rule approximates a **MACD / SMA-crossover** at monthly frequency. A daily squeeze trigger is a
   **fast** signal — the penalised class. Any squeeze test should be benchmarked against a slow trend rule, which is
   the literature's actual winner.
7. **Never validate a squeeze with a market-timing or directional-accuracy test.** Christoffersen & Diebold show such
   tests have **"little or no power"** for volatility-driven sign dependence. A timing test producing a squeeze edge
   is the documented blind spot, not evidence. Use net-of-cost P&L with a pre-registered protocol instead.
8. **Pre-register the test.** Given Bajgrowicz & Scaillet (persistence failure, costs) and Hudson & Urquhart (BTC
   fails out-of-sample), plus the number of squeeze parameterisations available, an unregistered test is a
   data-mining exercise by construction.
9. **Check sign stability across universes and subperiods** before trusting any effect (the Monday sign flip and the
   liquidity reversal→momentum flip both fail this test).
10. **Beware concentration.** Grobys et al. (2025): a single coin can make crypto momentum insignificant. With a
    small long-only book, this is a first-order risk.
11. **Measure your actual spread instead of assuming 20 bps.** Brauneis, Mestel, Riordan & Theissen (2021) validate the
    **Corwin-Schultz** and **Abdi-Ranaldo** high-low spread estimators as the best low-frequency liquidity proxies in
    crypto — and they need only OHLCV data. Compute per-pair effective spreads from your own candles, then re-run the
    fee hurdle per asset rather than applying a flat 0.2%.
12. **Treat the momentum factor as contested.** Grobys & Sapkota (2019) find *no significant momentum payoffs* in 143
    coins (2014–2018), directly contradicting LTW (2022). Any strategy leaning on the LTW three-factor model is
    resting on a disputed result.

---

## 6. Explicitly flagged weaknesses and unverified items

| Item | Status |
|---|---|
| LTW (2022) exact "1-week reversal" number | **UNVERIFIED** — could not access full text; abstracts don't state it |
| Grobys & Sapkota on weekday effects in crypto | **UNVERIFIED / likely conflation** — their verified joint paper is on momentum (Economics Letters 180), and finds NO significant momentum |
| "Revisiting seasonality" article number **105413** | **WRONG** — actual DOI 10.1016/j.frl.2024.105429 |
| "Hudson, McGroarty & Urquhart 2020" | **WRONG attribution** — actual: Hudson & Urquhart (2021), 2 authors |
| Wen et al. (2022) journal | **WRONG** — NAJEF, not Finance Research Letters |
| Barchart "The Squeeze by John Carter" PDF | **NOT CONTENT-VERIFIED** (HTTP 202, empty body) |
| Bollinger Bands JPM 2017 findings | Existence/DOI **VERIFIED**; exact wording **SECONDARY-SOURCED** (third-party review, not the paper); magnitudes **UNVERIFIED** |
| Opening-range-breakout FRL paper | ✅ **VERIFIED** (both RePEc and S-WoPEc); "Contraction–Expansion principle" confirmed as an official keyword |
| Giner & Zakamulin (2023) findings | ✅ **VERIFIED** (full abstract retrieved) — theory only, no net-of-cost trading test |
| Opening-range-breakout FRL paper findings | **UNVERIFIED** (403) |
| Fičura (2023) | ✅ **VERIFIED** — full abstract with exact t-stats (−7.31 / 2.33 / −9.03 / 4.93) |
| Day-of-week effects in **basis points** | **NOT AVAILABLE** in accessible abstracts — paywalled |
| Any peer-reviewed TTM Squeeze backtest | **DOES NOT APPEAR TO EXIST** — absence is itself a finding |
| Abstracts **publisher-elided** (bibliographic records verified, abstract text NOT retrieved) | Bollerslev-Chou-Kroner 1992; Andersen et al. 2003; Baker-Bradley-Wurgler 2011; Bollerslev-Tauchen-Zhou 2009; Carr-Wu 2009; Hollstein-Prokopczuk 2018; Moreira-Muir 2017; Cederburg et al. 2020 |
| Nelson (1991) DOI | **CONFLICTED** — 10.2307/1912767 resolves to a different paper |
| SSRN 5288827 "Quantifying Volatility Compression and Expansion" | **UNVERIFIED** — unrefereed, SSRN 403s; do not cite |
| Carter "Mastering the Trade" ISBN | **UNVERIFIED** (book existence confirmed) |
| Giner & Zakamulin: any tradeable claim | Paper is **THEORETICAL** — no OOS test, no costs, no directional claim. Calling it proof of a tradeable edge is an **overreach** |
| Yang (2025) "Cryptocurrency market risk-managed momentum strategies", FRL 85(PA) | Noted, not retrieved |
| "Revisiting seasonality" volume/article number 64 | ✅ Verified (FRL vol 64) |

---

## 7. Verified citation index

All URLs below were fetched successfully during this review.

**Short-term reversal / momentum**
1. Zaremba, Bilgin, Long, Mercik, Szczygielski (2021), IRFA 78(C), DOI 10.1016/j.irfa.2021.101908 —
   https://ideas.repec.org/a/eee/finana/v78y2021ics1057521921002349.html
2. Fieberg, Liedtke, Zaremba (2024), IRFA 94(C), DOI 10.1016/j.irfa.2024.103218 —
   https://ideas.repec.org/a/eee/finana/v94y2024ics1057521924001509.html
3. Grobys, Kolari, Sandretto, Shahzad, Äijö (2025), FMPM 39(4) 443–476, DOI 10.1007/s11408-025-00474-9 —
   https://ideas.repec.org/a/kap/fmktpm/v39y2025i4d10.1007_s11408-025-00474-9.html
4. Caporale & Plastun (2019), J. Economic Studies 46(5) 1137–1155, DOI 10.1108/JES-09-2018-0310 —
   https://ideas.repec.org/a/eme/jespps/jes-09-2018-0310.html
5. Wen, Bouri, Xu, Zhao (2022), NAJEF 62, 101733, DOI 10.1016/j.najef.2022.101733 —
   https://m2.mtmt.hu/api/publication/33003705
6. Liu, Tsyvinski, Wu (2022), JoF 77(2) 1133–1177, DOI 10.1111/jofi.13119 / NBER WP 25882 —
   https://www.nber.org/papers/w25882
6b. Bianchi, Babiak, Dickerson (2022), JBF 142(C), DOI 10.1016/j.jbankfin.2022.106547 (reversal = market-making premium)
6c. Fičura (2023), FFA WP 5.003 (**working paper**) — https://ideas.repec.org/p/prg/jnlwps/v5y2023id5.003.html
6d. Kitron & Wengrowicz (2026), arXiv:2608.21888 (**preprint**) — https://arxiv.org/abs/2608.21888
6e. Mercik, Zaremba, Demir (2026), IRFA 113(C), DOI 10.1016/j.irfa.2026.105137 (36 factors → 2–3)
6f. Fieberg, Günther, Poddig, Zaremba (2024), IRFA 92(C), DOI 10.1016/j.irfa.2024.103106 (non-standard errors)
6g. Grobys, Sandretto, Äijö (2026), FRL 92(C), DOI 10.1016/j.frl.2026.109602 (survivorship demolition)
6h. Baybutt (2024), arXiv:2405.15716 (**preprint**)
6i. Nakagawa & Sakemoto (2025), FRL 85(PA), DOI 10.1016/j.frl.2025.107800 (**contradictory evidence**)
6j. Mercik, Będowska-Sójka, Karim, Zaremba (2025), IRFA 97(C), DOI 10.1016/j.irfa.2024.103809
    (**contradictory evidence**)
6k. Frankfurt School of Finance & Management, "Total Costs in Crypto Trading for Retail Investors"
    (**NOT peer-reviewed**; 0.53%–6.45% per round trip) —
    https://www.frankfurt-school.de/en/knowledge/research/total-costs-in-crypto-trading

**Technical analysis & data snooping**
7. Bajgrowicz & Scaillet (2012), JFE 106(3) 473–491, DOI 10.1016/j.jfineco.2012.06.001 —
   https://ideas.repec.org/a/eee/jfinec/v106y2012i3p473-491.html
8. Park & Irwin (2007), J. Economic Surveys 21(4) 786–826, DOI 10.1111/j.1467-6419.2007.00519.x —
   https://ideas.repec.org/a/bla/jecsur/v21y2007i4p786-826.html
9. Hudson & Urquhart (2021), Annals of Operations Research 297, 191–220, DOI 10.1007/s10479-019-03357-1 —
   https://centaur.reading.ac.uk/85715/
10. Corbet, Eraslan, Lucey, Sensoy (2019), FRL 31(C) 32–37, DOI 10.1016/j.frl.2019.04.027 —
    https://ideas.repec.org/a/eee/finlet/v31y2019icp32-37.html
11. Grobys, Ahmed, Sapkota (2020), FRL 32(C), DOI 10.1016/j.frl.2019.101396 —
    https://ideas.repec.org/a/eee/finlet/v32y2020ics1544612319308852.html
12. Zatwarnicki, Zatwarnicki, Stolarski (2023), Sensors 23(3) 1664, DOI 10.3390/s23031664 —
    https://ouci.dntb.gov.ua/en/works/4yEoKNg4/
13. Zatwarnicki & Zatwarnicki (2025), Applied Sciences 15(23) 12802, DOI 10.3390/app152312802 (same URL)

**Seasonality**
14. Mueller (2024), FRL 64(C), DOI 10.1016/j.frl.2024.105429 —
    https://ideas.repec.org/a/eee/finlet/v64y2024ics1544612324004598.html
15. Caporale & Plastun (2019), FRL 31(C), DOI 10.1016/j.frl.2018.11.012 —
    https://bura.brunel.ac.uk/handle/2438/17208
16. Baur, Cahill, Godfrey, Liu (2019), FRL 31(C) 78–92, DOI 10.1016/j.frl.2019.04.023 —
    https://ideas.repec.org/a/eee/finlet/v31y2019icp78-92.html
17. Algieri, Lawuobahsumo, Leccadito, Zahid (2025), NAJEF 79(C), DOI 10.1016/j.najef.2025.102441 —
    https://ideas.repec.org/a/eee/ecofin/v79y2025ics1062940825000816.html
18. Sullivan, Timmermann, White (2001), J. Econometrics 105(1) 249–286 (calendar data-mining critique)
18b. Grobys & Sapkota (2019), Economics Letters 180(C) 6–10, DOI 10.1016/j.econlet.2019.03.028 —
    https://ideas.repec.org/a/eee/ecolet/v180y2019icp6-10.html
18c. Brauneis, Mestel, Riordan, Theissen (2021), J. Banking & Finance 124(C),
    DOI 10.1016/j.jbankfin.2020.106041 —
    https://ideas.repec.org/a/eee/jbfina/v124y2021ics0378426620303022.html

**Volatility / regimes**
19. Christoffersen & Diebold (2006), Management Science 52(8) 1273–1287, DOI 10.1287/mnsc.1060.0520 —
    https://www.nber.org/papers/w10009
19b. **Holmberg, Lönnbark & Lundström (2013), FRL 10(1) 27–33, DOI 10.1016/j.frl.2012.09.001 — THE
    "Contraction–Expansion principle" paper** — https://ideas.repec.org/a/eee/finlet/v10y2013i1p27-33.html
    and https://swopec.hhs.se/umnees/abs/umnees0845.htm
19c. Fang, Jacobsen & Qin (2017), JPM 43(4) 152–159, DOI 10.3905/jpm.2017.43.4.152 —
    https://www.pm-research.com/content/iijpormgmt/43/4/152
19d. Giner & Zakamulin (2023), Economic Modelling 122, DOI 10.1016/j.econmod.2023.106237 —
    https://ideas.repec.org/a/eee/ecmode/v122y2023ics0264999323000494.html
19e. McLean & Pontiff (2016), "Does Academic Research Destroy Stock Return Predictability?", JoF 71(1) 5–32
    (post-publication decay)
19f. Caporin, Ranaldo & Santucci de Magistris (2013), "On the predictability of stock prices: A case for high
    and low prices", JBF 37(12) 5132–5146 (OHLC range-based predictability)
19g. Ardia, Bluteau & Rüede (2019), "Regime changes in Bitcoin GARCH volatility dynamics", FRL 29, 266–271,
    DOI 10.1016/j.frl.2018.08.009 (VaR = magnitude, not direction)
19h. Kang & Ryu (2026), "Time-series momentum and market timing in Bitcoin", Risk Management 28(3), 1–23,
    DOI 10.1057/s41283-026-00234-7 (slow signals beat fast in BTC)
19i. Haase & Neuenkirch (2023), Int. J. Forecasting 39(2) 587–605, DOI 10.1016/j.ijforecast.2022.01.004
    (146 predictors; forecasts do NOT beat benchmarks except in declining markets)
19j. Tsuji (2025), Future Business Journal 11, 138, DOI 10.1186/s43093-025-00551-5 (**contradictory; 1 citation**)
19k. Chen, Chordia, Chung & Lin (2021), RAPS 11(1) 289–335, DOI 10.1093/rapstu/raab018 (VOV risk premium;
    **options-based, not computable from OHLCV**)
19l. Bollerslev, Tauchen & Zhou (2009), RFS 22(11) 4463–4492, DOI 10.1093/rfs/hhp008; Carr & Wu (2009),
    RFS 22(3) 1311–1341, DOI 10.1093/rfs/hhn038; Hollstein & Prokopczuk (2018), RAPS 8(2) 253–292,
    DOI 10.1093/rapstu/rax019 (**bibliographic records verified; abstracts NOT retrieved**)
19m. Engle (1982), Econometrica 50(4) 987–1008, DOI 10.2307/1912773; Bollerslev (1986), J. Econometrics 31(3)
    307–327, DOI 10.1016/0304-4076(86)90063-1
19n. Christie (1982), JFE 10(4) 407–432, DOI 10.1016/0304-405X(82)90018-6; Nelson (1991), Econometrica 59(2)
    347–370 (⚠️ **DOI 10.2307/1912767 is CONFLICTED — verify before citing**)
19o. Moreira & Muir (2017), JF 72(4) 1611–1644 vs Cederburg, O'Doherty, Wang & Yan (2020), JFE 138(1) 95–117
    (**contradicting peer-reviewed pair** on volatility-managed portfolios)
20. Burggraf & Rudolf (2021), FRL 40(C), DOI 10.1016/j.frl.2020.101683 —
    https://ideas.repec.org/a/eee/finlet/v40y2021ics154461232030667x.html
21. Kaya & Mostowfi (2022), FRL 46, 102422, DOI 10.1016/j.frl.2021.102422 (open access) —
    https://digitalcollection.zhaw.ch/items/7d365009-96fe-4c1a-9ac5-06bc5ada05cf/full
22. Giner & Zakamulin (2023), Economic Modelling 122, DOI 10.1016/j.econmod.2023.106237 (open access) —
    https://portalciencia.ull.es/documentos/64204685e1b5e93884faa4d8
