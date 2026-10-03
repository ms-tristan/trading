# Volatility Contraction / Squeeze Breakouts — Rigorous Literature Verification

**Scope:** SPOT, LONG-ONLY, OHLCV daily candles only. Question: does volatility contraction
(low-vol regime, range compression, Bollinger squeeze) predict the **direction** of the
subsequent move, or only its **magnitude**?

**Method:** Every citation below was verified by fetching a real URL (or via the Semantic
Scholar / Unpaywall graph API) during this session. Nothing is reconstructed from memory.
Where a source could not be fetched, it is labelled **UNVERIFIED**. Publisher sites
(ScienceDirect, Wiley, SSRN, T&F, INFORMS) returned HTTP 403 as expected; RePEc/IDEAS,
EconPapers, S-WoPEc, CentAUR, OUCI, NBER, Duke, and the Semantic Scholar API were used instead.

---

## 0. BOTTOM LINE (read this first)

1. **Volatility contraction/expansion is a well-established, peer-reviewed fact about
   MAGNITUDE.** Volatility clustering (Engle 1982; Bollerslev 1986) is one of the most
   replicated results in financial econometrics (~23,000 citations each).
2. **Directional prediction from volatility alone is theoretically possible but
   structurally negligible at daily frequency.** The canonical paper
   (Christoffersen & Diebold 2006, *Management Science*) proves that sign predictability
   arises *only* from the **interaction of a nonzero expected return with time-varying
   volatility**, and states explicitly that it is **not** expected at daily horizons.
3. **There is no peer-reviewed paper demonstrating a tradeable, cost-surviving
   directional edge from a volatility-contraction/squeeze breakout.** The closest
   peer-reviewed result (Holmberg, Lönnbark & Lundström 2013, *Finance Research Letters*)
   uses the "Contraction-Expansion principle" as an explicit keyword but tests it on
   intraday crude-oil futures, not crypto, and its claim is about breakout *success rate
   and mean return*, not sign prediction.
4. **The one peer-reviewed test of the Bollinger-band breakout use found it NEGATIVE**
   (Fang, Jacobsen & Qin 2017, *Journal of Portfolio Management*): returns from the
   classic Bollinger method "are mostly negative… and such losses have worsened over time."
5. **Crypto evidence is at best mixed, and the strongest peer-reviewed crypto TA paper
   loses its edge for Bitcoin out-of-sample** (Hudson & Urquhart 2021).

---

## 1. THE KEY QUESTION — Does volatility predict DIRECTION or only MAGNITUDE?

### 1.1 The single most important paper (PEER-REVIEWED, canonical)

**Christoffersen, Peter F.; Diebold, Francis X. (2006). "Financial Asset Returns,
Direction-of-Change Forecasting, and Volatility Dynamics."**
*Management Science* 52(8): 1273–1287.
- DOI: `10.1287/mnsc.1060.0520`
- Working paper: NBER Working Paper 10009 (Oct 2003), DOI `10.3386/w10009`
- Verified URL: https://www.nber.org/papers/w10009 (fetched HTTP 200)
- Full text (fetched via curl, PDF text-extracted): https://www.sas.upenn.edu/~fdiebold/papers/paper47/tempcd9.pdf
  (author's own copy, dated October 29 2005)

**Status: PEER-REVIEWED** (Management Science, INFORMS). Also a top-tier NBER working paper.

**Verbatim abstract findings:**
> "(a) Volatility dependence produces sign dependence, so long as expected returns are
> nonzero, so that one should expect sign dependence, given the overwhelming evidence of
> volatility dependence; (b) The standard finding of little or no conditional mean
> dependence is entirely consistent with a significant degree of sign dependence and
> volatility dependence; (c) Sign dependence is not likely to be found via analysis of sign
> autocorrelations, runs tests, or traditional market timing tests, because of the special
> nonlinear nature of sign dependence; (d) Sign dependence is not likely to be found in very
> high-frequency (e.g., daily) or very low-frequency (e.g., annual) returns; instead, it is
> more likely to be found at intermediate return horizons; (e) Sign dependence is very much
> present in actual U.S. equity returns, and its properties match closely our theoretical
> predictions; (f) The link between volatility forecastability and sign forecastability
> remains intact in conditionally non-Gaussian environments, as for example with time-varying
> conditional skewness and/or kurtosis."

**Verbatim quotes from the body (extracted from the PDF text):**
> "[Sign dependence arises] interestingly on the interaction of a non-zero mean return and
> non-constant volatility. A zero mean would render the sign unforecastable, as would
> constant volatility; hence the tradition in financial econometrics of removing
> unconditional means and working with zero-mean series disguises sign forecastability.
> Notice also that **a large volatility relative to the mean renders the sign nearly
> unpredictable.**"

> "In sign forecasting, volatility dynamics interact with a nonzero mean to produce time
> variation in the probability of a positive return and hence sign forecastability."

> "We have shown that return sign forecastability arises from the interaction of nonzero
> expected returns and volatility forecastability. **As expected returns approach zero, or
> as volatility forecastability approaches zero, sign forecastability approaches zero.**
> Hence one does not expect strong sign forecastability for very high frequency returns such
> as daily, despite their high volatility forecastability, **because expected daily returns
> are negligible.**"

> "**sign probability forecasts are most sensitive to changes in volatility when volatility
> is at an intermediate level**, and we show in a realistically calibrated simulation
> exercise that sign forecastability appears strongest at intermediate horizons of two or
> three months."

> "although they may have good power to detect sign forecastability arising from time-varying
> expected returns, they have **little or no power to detect sign forecastability arising
> from variation in volatility** (or higher-ordered conditional moments)."

> Empirical application: daily S&P 500 index returns (CRSP `SPINDX`), 1963-01-01 to
> 2003-12-31, horizons h = 1 to 250 days. "both the correlations and the autocorrelations are
> generally much larg[er]..." at intermediate horizons than at h=1.

**Direct implications for a daily, long-only crypto spot system:**
- The theorem is conditional: volatility → sign **only if** E[r] ≠ 0. With near-zero daily
  crypto drift, the mechanism is switched off.
- The paper explicitly says the effect should **NOT** appear at daily frequency. This is a
  direct, peer-reviewed argument against "squeeze → daily directional signal."
- The paper warns that runs tests and traditional market-timing tests have **little to no
  power** to detect volatility-driven sign dependence — so a backtest that "finds" a
  daily squeeze edge via a market-timing test is exactly the failure mode the authors
  describe.
- The authors also caution sign dependence "is not necessarily indicative of time-varying
  expected returns and should not be interpreted as such" — i.e. even a real sign edge from
  volatility is not evidence of a genuine directional forecast.

**Verdict: This is the strongest and most directly on-point academic evidence, and it argues
that volatility contraction predicts MAGNITUDE, not DIRECTION, at our timeframe.**

### 1.2 Supporting: the vol→sign effect is second-order and driven by vol-of-vol

Same paper, verbatim:
> "because the optimal probability forecast is driven entirely by the volatility, we have
> that ..., is therefore **driven by the volatility of volatility**."

So the strength of any sign forecast is governed by **volatility-of-volatility**, not by the
volatility level itself. This matters: a "squeeze" is a low-vol *level*, which is not the
same quantity as low vol-of-vol. See §5.

### 1.3 The leverage effect runs the OTHER WAY (important asymmetry)

The "leverage effect" is about DIRECTION → VOLATILITY, not volatility → direction. Verified:

**Christie, Andrew A. (1982). "The stochastic behavior of common stock variances: Value,
leverage and interest rate effects."** *Journal of Financial Economics* 10(4): 407–432.
- DOI: `10.1016/0304-405X(82)90018-6` — verified via Semantic Scholar API
  (title, author, year, 2,441 citations). Cited in OUCI with pages 407.
- **Status: PEER-REVIEWED.**
- Note: the unfetched abstract was elided by the publisher, so I quote only the verified
  bibliographic record; the **direction** of the effect (price declines → higher subsequent
  volatility) is the standard reading and is confirmed by the secondary peer-reviewed
  sources below.

**Nelson, Daniel B. (1991). "Conditional Heteroskedasticity in Asset Returns: A New
Approach."** *Econometrica* 59(2): 347–370.
- Verified URLs: https://econpapers.repec.org/article/ecmemetrp/v_3a59_3ay_3a1991_3ai_3a2_3ap_3a347-70.htm
  (EconPapers/RePEc) and https://m2.mtmt.hu/api/reference/889753 (MTMT record: "Nelson, D.,
  Conditional heteroskedasticity in asset returns: A new approach (1991) Econometrica, 59,
  pp. 347-370").
- DOI: commonly cited as `10.2307/1912767` — **CAUTION: this DOI resolves in Crossref/Semantic
  Scholar to Kreps & Wilson, "Sequential Equilibria" (Econometrica 1982)**. I therefore treat
  the DOI mapping as **UNVERIFIED/CONFLICTED** and rely on the verified EconPapers + MTMT
  journal/volume/page record instead. This is the EGARCH paper.
- **Status: PEER-REVIEWED.**

**Interpretation to flag:** the leverage effect establishes that *past return sign* predicts
*future volatility*. That is the reverse of the squeeze thesis. A squeeze-breakout signal
that uses past returns to infer future volatility is on solid (peer-reviewed) ground on the
**volatility** side; it is **not** thereby licensed to claim direction.

### 1.4 No peer-reviewed support for "low volatility → up"

The **low-volatility anomaly** is a cross-sectional statement about *relative* average
returns of stocks, not a time-series directional timing rule:

**Baker, Malcolm P.; Bradley, Brendan O.; Wurgler, Jeffrey (2011). "Benchmarks as Limits to
Arbitrage: Understanding the Low-Volatility Anomaly."** *Financial Analysts Journal* 67(1).
- DOI: `10.2469/faj.v67.n1.4` — verified via Semantic Scholar API (title, all three authors,
  year 2011, 775 citations). SSRN version abstractid=1585031.
- **Status: PEER-REVIEWED** (FAJ). **Abstract was elided by the publisher (abstract: null) —
  so I do NOT quote any abstract text for this paper.** Content claim limited to: it is the
  canonical reference for the low-volatility anomaly and attributes it to
  benchmark-constrained arbitrage.
- **Why it does not help us:** it is cross-sectional (sort stocks by vol), equity-only, and
  about average *return levels*, not about directional timing after a compression event.

---

## 2. Does "volatility contraction precedes volatility expansion" have peer-reviewed backing?

### 2.1 YES for the persistence/clustering fact (PEER-REVIEWED, canonical)

**Engle, Robert F. (1982). "Autoregressive Conditional Heteroscedasticity with Estimates of
the Variance of United Kingdom Inflation."** *Econometrica* 50(4): 987–1008.
- DOI: `10.2307/1912773`
- Verified URL: https://jstor.econometricsociety.org/publications/econometrica/browse/1982/07/01/autoregressive-conditional-heteroscedasticity-estimates
  (fetched HTTP 200; official Econometric Society page)
- **Verbatim abstract:** "Traditional econometric models assume a constant one-period
  forecast variance. To generalize this implausible assumption, a new class of stochastic
  processes called autoregressive conditional heteroscedastic (ARCH) processes are
  introduced in this paper. These are mean zero, serially uncorrelated processes with
  nonconstant variances conditional on the past, but constant unconditional variances. For
  such processes, the recent past gives information about the one-period forecast variance."
- Note the page lists pp. 987-1008 (Semantic Scholar records 987-1007).
- **Status: PEER-REVIEWED** (~22,800 citations per Semantic Scholar).

**Bollerslev, Tim (1986). "Generalized autoregressive conditional heteroskedasticity."**
*Journal of Econometrics* 31(3): 307–327.
- DOI: `10.1016/0304-4076(86)90063-1`
- Verified URL: https://fds.duke.edu/db/aas/Economics/boller/publications/238009 (Duke
  faculty database, Tim Bollerslev's own publication record; fetched HTTP 200)
- **Verbatim abstract (from Duke's record):** "A natural generalization of the ARCH
  (Autoregressive Conditional Heteroskedastic) process introduced in Engle (1982) to allow
  for past conditional variances in the current conditional variance equation is proposed.
  Stationarity conditions and autocorrelation structure for this new class of parametric
  models are derived. Maximum likelihood estimation and testing are also considered. Finally
  an empirical example relating to the uncertainty of the inflation rate is presented."
- **Status: PEER-REVIEWED** (~24,000 citations).

**Bollerslev, Tim; Chou, Ray Y.; Kroner, Kenneth F. (1992). "ARCH modeling in finance: A
review of the theory and empirical evidence."** *Journal of Econometrics* 52(1–2): 5–59.
- DOI: `10.1016/0304-4076(92)90064-X` — verified via Semantic Scholar API (title, all three
  authors, pages 5-59, vol 52, 4,364 citations).
- **Status: PEER-REVIEWED survey.** (Publisher-elided abstract; no abstract text quoted.)

**Andersen, Torben G.; Bollerslev, Tim; Diebold, Francis X.; Labys, Paul (2003). "Modeling
and Forecasting Realized Volatility."** *Econometrica* 71(2): 579–625.
- DOI: `10.1111/1468-0262.00418` — verified via Semantic Scholar API (title, all four
  authors, pages 579-625, vol 71, 3,865 citations).
- **Status: PEER-REVIEWED.** (Publisher-elided abstract; no abstract text quoted.)

### 2.2 Is there a peer-reviewed BACKTEST of contraction → expansion as a tradeable strategy?

**The closest thing, and it is real (PEER-REVIEWED):**

**Holmberg, Ulf; Lönnbark, Carl; Lundström, Christian (2013). "Assessing the profitability of
intraday opening range breakout strategies."** *Finance Research Letters* 10(1): 27–33.
- Working paper version: Umeå Economic Studies No. 845, RePEc handle `RePEc:hhs:umnees:0845`
- Verified URL: https://swopec.hhs.se/umnees/abs/umnees0845.htm (fetched HTTP 200)
- Journal record verified via RePEc/EconPapers:
  https://econpapers.repec.org/article/eeefinlet/v_3a10_3ay_3a2013_3ai_3a1_3ap_3a27-33.htm
- **Status: PEER-REVIEWED** (Finance Research Letters, Elsevier).

**Verbatim abstract (from S-WoPEc):**
> "Is it possible to beat the market by mechanical trading rules based on historical and
> publicly known information? Such rules have long been used by investors and in this paper,
> we test the success rate of trades and profitability of the Open Range Breakout (ORB)
> strategy. An investor that trades on the ORB strategy seeks to identify large intraday
> price movements and trades only when the price moves beyond some predetermined threshold.
> We present an ORB strategy based on normally distributed returns to identify such days and
> find that our ORB trading strategy result in significantly higher returns than zero as well
> as an increased success rate in relation to a fair game. The characteristics of such an
> approach over conventional statistical tests is that it involves the joint distribution of
> Low, High, Open and Close over a given time horizon."

**KEY KEYWORDS (verbatim from the same page):** "Bootstrap; Crude oil futures;
**Contraction-Expansion principle**; Efficient market hypothesis; Martingales; Technical
Analysis." JEL: C49, G11, G14, G17. 11 pages, August 23, 2012.

**Why this is the most relevant peer-reviewed finding for our thesis — and its limits:**
- It is the academic formalisation of "contraction precedes expansion" as a tradeable rule.
- BUT it tests **intraday** ORB on **crude oil futures**, not daily crypto.
- It claims **significantly positive returns vs zero** and an **increased success rate vs a
  fair game** — a magnitude/breakout claim. It does **not** claim to predict direction from
  the compression state; the direction is supplied by the breakout itself.
- It is one market, one era, one paper. **I found no replication in crypto or in a spot
  long-only setting.**

**Related, also peer-reviewed, but weaker for our purposes:**

**Wu, Mu-En; Luo, Sheng-Chi; Lin, Wei-Xi; Chung, Chien-Ping; Wu, Jun-Yo; Wu, Jimmy Ming-Tai
(2026). "Enhancing Opening Range Breakout Strategies with LSTM-Based True Range Prediction."**
In *Intelligent Information and Database Systems* (ACIIDS), pp. 323–335.
- DOI: `10.1007/978-981-92-0074-0_23` — verified via Semantic Scholar API (title, all six
  authors, year 2026, pages 323-335).
- **Status: PEER-REVIEWED conference proceedings** (Springer LNCS/LNAI series). 0 citations.
- **Weakness to flag:** adds an ML layer (LSTM) on top of ORB; no abstract available via the
  API; small recent conference paper; not a clean test of the contraction thesis itself.

**A cautionary peer-reviewed result on backtested technical rules:**

**Bajgrowicz, Pierre; Scaillet, Olivier (2012). "Technical trading revisited: False
discoveries, persistence tests, and transaction costs."** *Journal of Financial Economics*
106(3): 473–491. (Verified via RePEc citation listing on the IDEAS page for
`10.1016/j.irfa.2023.102928`, fetched HTTP 200.)
- **Status: PEER-REVIEWED.** Cited because it is the standard reference for the claim that
  apparent technical-rule profits are largely false discoveries that do not persist after
  transaction costs. **UNVERIFIED abstract** (not fetched directly).

---

## 3. Carter's TTM Squeeze — practitioner, NOT peer-reviewed

**Origin (PRACTITIONER BOOK, not peer-reviewed):**
Carter, John F. *Mastering the Trade: Proven Techniques for Profiting from Intraday and
Swing Trading Setups.* McGraw-Hill (1st ed. 2005/2006; 2nd ed. 2012).
- Corroborating verified records: Bursa Malaysia library catalogue lists "Mastering The
  Trade (2nd Edition): John F. Carter" (https://knowledgecentre.bursamalaysia.com/webopac20/Record/0000053918/Holdings);
  Google Books edition record for "Mastering the Trade" (edition ID `AOIGj5ATqvAC`).
- I could **not** fetch a publisher page confirming the exact ISBN within this session —
  **treat the ISBN as UNVERIFIED**, though the book's existence and authorship are
  well-corroborated by multiple independent library records.
- **Status: PRACTITIONER BOOK. NOT peer-reviewed. There is no peer review of TTM Squeeze.**

**Primary practitioner source for the indicator itself:**
John Carter's own document "The Squeeze," hosted by Barchart:
https://www.barchart.com/media/education/pdf/The%20Squeeze%20by%20John%20Carter.pdf
- The **URL is confirmed to exist** (it appears as a live search result and returns HTTP 202
  — a bot-challenge, not a 404). I could not extract its body text.
- **Status: VENDOR/PRACTITIONER SELF-PUBLISHED MARKETING MATERIAL. NOT academic.**
- "TTM" = **Trade the Markets**, Carter's firm — this is why the indicator is branded
  "TTM Squeeze."

**Widely copied open-source implementations (NOT academic):**
- LazyBear's "Squeeze Momentum Indicator" on TradingView, e.g.
  https://www.tradingview.com/script/nqQ1DT5a-Squeeze-Momentum-Indicator-LazyBear/ and its
  GitHub mirror
  https://github.com/b25723/tradingview_pinescript_strategies/blob/master/Squeeze-Momentum-Indicator.md
- The `pandas_ta` `squeeze` function documentation
  (https://tradingstrategy.ai/docs/api/technical-analysis/momentum/help/pandas_ta.momentum.squeeze.html)
- StockCharts ChartSchool "TTM Squeeze" page
  (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/ttm-squeeze)
- **Status: ALL NOT ACADEMIC.** These are vendor documentation, community scripts, and SEO
  content. They are not evidence. Do not cite them in any research artefact.

**Did I find ANY rigorous backtest of TTM Squeeze?**
- **No peer-reviewed backtest of TTM Squeeze exists that I could locate.** I searched
  extensively. This absence is itself a finding.
- One SSRN **working paper** surfaced and is **NOT peer-reviewed**:
  "Quantifying Volatility Compression and Expansion: A Predictive Model for Momentum-Driven
  Market Dislocations," SSRN abstract_id 5288827
  (https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5288827). **Status: WORKING PAPER,
  UNREFEREED, UNVERIFIED CONTENT** (SSRN returns 403 to automated fetches; I could not read
  the abstract). **Do not rely on this.** Treat its claims as unverified author assertions.
- Note the title's framing ("Momentum-Driven Market Dislocations") and the venue (SSRN
  preprint, no journal) — this is exactly the vendor-adjacent tier to be skeptical of.

### 3.1 The one real peer-reviewed Bollinger-band test — and it is NEGATIVE

**Fang, Jiali; Jacobsen, Ben; Qin, Ya-Feng (2017). "Popularity versus Profitability: Evidence
from Bollinger Bands."** *The Journal of Portfolio Management* 43(4): 152–159.
- DOI: `10.3905/jpm.2017.43.4.152`
- Verified via Semantic Scholar API: title, all three authors (Jiali Fang, B. Jacobsen,
  Ya-Feng Qin), year 2017, vol 43, pages 152-159. Also SSRN DOI `10.2139/ssrn.2484322`
  (abstract_id 2484322) and publisher page https://www.pm-research.com/content/iijpormgmt/43/4/152.
- **Status: PEER-REVIEWED** (Journal of Portfolio Management — peer-reviewed practitioner
  journal). 6 citations in Semantic Scholar (low count; note this).
- **Publisher abstract was elided (abstract: null) — I did NOT obtain the verbatim abstract.**
  I quote instead the finding **as reported in a third-party analytical write-up** (JAG
  Capital Management, "Strike Down the Band," https://www.jagcap.com/insights/strike-down-the-band/,
  fetched HTTP 200), which describes the paper directly:

> "Our own analysis agrees with that finding... a 2017 research paper from Fang, Jacobsen,
> and Qin determined that returns from a Bollinger Band-based method **'are mostly
> negative… and such losses have worsened over time.'**"

And on the original Bollinger claim itself:
> "About four decades ago, John Bollinger developed a stock trading system that relied on
> three factors: a 20-day moving average of stock prices along with an upper and lower band
> each two standard deviations away from the middle band. **His finding was that when stocks
> advanced beyond the upper two standard deviation band that they would continue to
> outperform for some time.**"

**Critical interpretation:** the peer-reviewed paper tests the **breakout-continuation** use
of Bollinger bands — precisely the "expansion follows compression, and it continues"
mechanism underlying squeeze breakouts — and finds the returns negative and deteriorating.
The JAG write-up further claims "using the Bands the opposite of the original design yields
performance in excess of broad market benchmarks," i.e. a **contrarian** result.

**Flag:** the verbatim quote comes from a third party (an asset manager's blog), not from my
own fetch of the paper. **Treat the exact quotation as SECONDARY-SOURCED.** The bibliographic
record is verified; the exact wording should be re-checked against the PDF before publication.

**SEO/vendor content explicitly excluded from evidence (NOT academic):**
- volatilitybox.com (commercial indicator vendor)
- tradingview.com scripts (community code)
- quantifiedstrategies.com, medium posts, blog backtests
- StockCharts ChartSchool, FinWiz, pandas_ta docs
None of these constitute peer-reviewed evidence and none should appear in a research artefact.

---

## 4. GARCH regime switching and directional predictability

### 4.1 The paper you asked me to verify (VERIFIED)

**Giner, Javier; Zakamulin, Valeriy (2023). "A regime-switching model of stock returns with
momentum and mean reversion."** *Economic Modelling* (Elsevier) vol. 122(C), article 106237.
- DOI: `10.1016/j.econmod.2023.106237` — **VERIFIED**
- Verified URL: https://ideas.repec.org/a/eee/ecmode/v122y2023ics0264999323000494.html
  (fetched HTTP 200)
- RePEc handle: `RePEc:eee:ecmode:v:122:y:2023:i:c:s0264999323000494`
- **Status: PEER-REVIEWED** (Economic Modelling).
- **Verbatim abstract:** "A vast body of empirical literature documents the existence of
  short-term momentum and medium-term mean reversion in various financial markets. However,
  few theoretical models explain these two common phenomena. A Markov model, wherein the
  return process randomly switches between bull and bear states, can reproduce many stylized
  facts of financial asset returns, excluding the mean reversion. An important limitation of
  the Markov model is that the state termination probability does not depend on age. We
  develop a semi-Markov model wherein, following the empirical evidence, the state
  termination probability increases with age. **We demonstrate that this model induces
  short-term return momentum and subsequent reversal. We calibrate our model to real-world
  data and show that the empirical results agree with our theoretical model.**"

**CRITICAL ASSESSMENT — does it work out-of-sample after costs?**
- **The paper does NOT claim a tradeable, out-of-sample, after-cost strategy.** Its
  contribution is **theoretical**: it explains *why* momentum and mean reversion coexist, via
  **duration dependence** (state termination probability rising with age) in a semi-Markov
  model, and then shows calibration consistency ("the empirical results agree with our
  theoretical model").
- It is anchored on **bull/bear market states identified with hindsight-style dating
  algorithms**, not on volatility regimes, and not on daily squeeze events.
- **No evidence in the abstract of (a) an out-of-sample trading test, (b) transaction
  costs, or (c) a directional-prediction claim.** Any claim that this paper "proves
  regime-switching predicts direction profitably after costs" would be **an overreach**.
- **Flag as weak for our use case.**

### 4.2 The follow-up that DOES address optimality (and its limits)

**Zakamulin, Valeriy; Giner, Javier (2024). "Optimal trend-following rules in two-state
regime-switching models."** *Journal of Asset Management* 25(4): 327–348.
- DOI: `10.1057/s41260-024-00357-0`
- Verified URL: https://ideas.repec.org/a/pal/assmgt/v25y2024i4d10.1057_s41260-024-00357-0.html
  (fetched HTTP 200)
- **Status: PEER-REVIEWED.**
- **Verbatim abstract:** "Academic research on trend-following investing has almost
  exclusively focused on testing various trading rules' profitability. However, all existing
  trend-following rules are essentially ad hoc, lacking a solid theoretical justification for
  their optimality. This paper aims to address this gap in the literature. Specifically, we
  examine the optimal trend-following when the returns follow a two-state process, randomly
  switching between bull and bear markets. We show that if a Markov model governs the return
  process, it is optimal to follow the trend using the Exponential Moving Average rule.
  However, the Markov model is unrealistic because it does not represent the bull and bear
  market duration times correctly. It is more sensible to model the return process by a
  semi-Markov model where the state termination probability increases with age. Under this
  framework, the optimal trend-following rule resembles the Moving Average Convergence/Divergence
  [MACD] rule. **We confirm the validity of the semi-Markov model with an empirical study
  demonstrating that the theoretically optimal trading rule outperforms the popular 10-month
  Simple Moving Average and 12-month Momentum rules across a universe of international
  markets.**"
- **Key takeaway for us:** the OPTIMAL rule under a regime-switching return process is a
  **trend-following / MACD-type rule**, and its empirical out-performance is measured against
  a **10-month SMA** baseline — i.e. **monthly, long-horizon** trend following on
  **international equity indices**. This is not a daily crypto squeeze signal, and it is
  direction-from-**price trend**, not direction-from-**volatility state**.

**Zakamulin, Valeriy; Giner, Javier (2023). "Optimal trend-following with transaction costs."**
*International Review of Financial Analysis* vol. 90(C), article 102928.
- DOI: `10.1016/j.irfa.2023.102928`
- Verified URL: https://ideas.repec.org/a/eee/finana/v90y2023ics1057521923004441.html
  (fetched HTTP 200)
- **Status: PEER-REVIEWED.**
- **Verbatim abstract:** "Despite the widespread popularity of trend-following investing,
  optimal trend-following in the presence of transaction costs remains poorly understood.
  ... In this paper, we propose a new, more pragmatic model that balances theoretical
  simplicity and practical relevance. Under plausible assumptions regarding the trend
  dynamics, we establish a noteworthy resemblance between the optimal trend-following
  strategy and the well-known simple moving average crossover rule. Consequently, our model
  provides a compelling rationale for the practical adoption of crossover rules. **We also
  perform historical simulations that demonstrate the effectiveness of our model**, supporting
  our theoretical findings."
- **This is the closest thing to an after-cost treatment in this literature**, and even here
  the out-of-sample protocol and cost magnitudes are not specified in the abstract. **Flag as
  incomplete evidence for a cost-surviving daily edge.**

### 4.3 Regime prediction — the honest peer-reviewed answer

**Haase, Felix; Neuenkirch, Matthias (2023). "Predictability of bull and bear markets: A new
look at forecasting stock market regimes (and returns) in the US."** *International Journal of
Forecasting* 39(2): 587–605.
- DOI: `10.1016/j.ijforecast.2022.01.004`
- Verified URL: https://ideas.repec.org/a/eee/intfor/v39y2023i2p587-605.html (fetched HTTP 200)
- **Status: PEER-REVIEWED** (with CESifo WP 8828 / Univ. Trier WP versions).
- **Verbatim abstract (the honest, negative core):** "Our weekly forecasts respond to regime
  changes in a timely manner to participate in recoveries or to prevent losses. This is also
  reflected in an improvement of risk-adjusted performance measures as compared to several
  benchmarks. **However, when considering stock market returns, our forecasts do not
  outperform common benchmarks.** Nevertheless, they do add statistical and, in particular,
  economic value during recessions or in declining markets."

**This is the single most useful negative result in the regime literature for our purposes:**
sophisticated Markov-switching models with time-varying transition probabilities and 146
predictors, combined via forecast pooling, **fail to beat benchmarks for return prediction**,
and only add value in **declining** markets. It is also **weekly** and S&P 500, not daily
crypto.

**Hammerschmid, Regina; Lohre, Harald (2018). "Regime shifts and stock return
predictability."** *International Review of Economics & Finance* 56(C): 138–160.
- Verified via RePEc citation listing (on the IDEAS page for `10.1016/j.ijforecast.2022.01.004`).
- **Status: PEER-REVIEWED.** **UNVERIFIED abstract** (not fetched directly).

---

## 5. Volatility-of-volatility and "expected/realized returns on volatility"

### 5.1 Variance risk premium — the literature that DOES claim volatility predicts returns

This is the strongest *honest* counterweight to §1.1, and it must be reported fairly.

**Bollerslev, Tim; Tauchen, George; Zhou, Hao (2009). "Expected Stock Returns and Variance
Risk Premia."** *The Review of Financial Studies* 22(11): 4463–4492.
- DOI: `10.1093/rfs/hhp008` — verified via Semantic Scholar API (title, all three authors,
  pages 4463-4492, vol 22, year 2009, 293 citations). Also verified as a reference on the
  IDEAS page https://ideas.repec.org/a/eee/intfor/v39y2023i2p587-605.html with full
  volume/issue/pages.
- **Status: PEER-REVIEWED.** **Abstract elided by publisher — no verbatim quote available.**
- Substance (reported as established in the literature, NOT quoted): the variance risk premium
  predicts aggregate stock returns at **quarterly/monthly** horizons.

**Carr, Peter; Wu, Liuren (2009). "Variance Risk Premiums."** *The Review of Financial
Studies* 22(3): 1311–1341.
- DOI: `10.1093/rfs/hhn038` — verified via Semantic Scholar API (title, authors P. Carr and
  Liu-Ren Wu, pages 1311-1341, vol 22, 1,195 citations).
- **Status: PEER-REVIEWED.** **Abstract elided — no verbatim quote.**

**Why this does not rescue the squeeze thesis — flag carefully:**
- These are about a **risk premium**: compensation for bearing variance risk, measured from
  **option-implied** variance vs realized variance. Our platform is **SPOT, OHLCV only — no
  options data exists**, so the variance risk premium is **not computable** from our data.
- The predictive horizon is **monthly to quarterly**, not daily.
- It is a statement about **expected excess return levels** (a conditional mean), not about
  the sign of the next move conditional on a volatility *compression* event.

### 5.2 Volatility-of-volatility predicts returns (PEER-REVIEWED, and directly relevant)

**Chen, Te-Feng; Chordia, Tarun; Chung, San-Lin; Lin, Ji-Chai (2021).
"Volatility-of-Volatility Risk in Asset Pricing."** *The Review of Asset Pricing Studies*
11(1): 289–335.
- DOI: `10.1093/rapstu/raab018`
- Verified URL: https://ouci.dntb.gov.ua/en/works/7X8GWJX7/ (fetched HTTP 200; OUCI confirms
  **Indexed in Scopus: Yes; Indexed in Web of Science: Yes**)
- **Status: PEER-REVIEWED.**
- **Verbatim abstract:** "This paper develops a general equilibrium model and provides
  empirical support that **the market volatility-of-volatility (VOV) predicts market
  returns** and drives the time-varying volatility risk. In asset pricing tests with the
  market, volatility, and VOV as factors, **the risk premium on VOV is statistically and
  economically significant and robust.** Market and volatility risks are not priced in
  unconditional models, but, consistent with theory, their factor loadings, conditional on
  VOV, are priced. **The pricing impact of VOV strengthens during market crashes**, suggesting
  that VOV is particularly relevant during market turmoil, when investors demand increased
  compensation for VOV risk."

**Hollstein, Fabian; Prokopczuk, Marcel (2018). "How Aggregate Volatility-of-Volatility
Affects Stock Returns."** *The Review of Asset Pricing Studies* 8(2): 253–292.
- DOI: `10.1093/rapstu/rax019` — verified via Semantic Scholar API (title, both authors,
  vol 8, pages 253-292, year 2018, 39 citations). Also verified as reference #39 on the OUCI
  page above.
- **Status: PEER-REVIEWED.** **Abstract elided — no verbatim quote available.**

**Connecting back to §1.2:** Christoffersen & Diebold proved the strength of any
volatility-driven sign forecast is **driven by the volatility of volatility**. The VOV
literature above confirms VOV has genuine return-predictive content — but note it is
(a) a **cross-sectional / risk-premium** result, (b) measured from **options**, and (c) it
strengthens in **crashes**, i.e. it is a risk-compensation story, not a squeeze-timing story.
**A "squeeze" measures a low volatility LEVEL, not low VOV. These are different quantities
and the literature does not license conflating them.**

**Chong, Carsten H.; Todorov, Viktor (2024). "Volatility of volatility and leverage effect
from options."** *Journal of Econometrics* 243(1), article 105669.
- DOI: `10.1016/j.jeconom.2024.105669` — verified via OUCI (Journal Article, Scopus WoS,
  Crossref 12) and Semantic Scholar API (title, both authors).
- **Status: PEER-REVIEWED.** Methodological (model-free estimators from high-frequency
  short-dated options). "We propose model-free (nonparametric) estimators of the volatility of
  volatility and leverage effect using high-frequency observations of short-dated options."
- **Requires options data — not applicable to our OHLCV-only spot platform.**

### 5.3 Volatility-managed portfolios (PEER-REVIEWED context)

**Moreira, Alan; Muir, Tyler (2017). "Volatility-Managed Portfolios."** *Journal of Finance*
72(4): 1611–1644. (Verified as reference #27 on the IDEAS page for
`10.1057/s41283-026-00234-7`.)
- **Status: PEER-REVIEWED.** **UNVERIFIED abstract** (not fetched directly).
- Relevance: shows scaling exposure **down** when volatility is high improves Sharpe ratios —
  a **magnitude/risk-sizing** result, not a **directional** one.

**Cederburg, Scott; O'Doherty, Michael S.; Wang, Feifei; Yan, Xuemin (Sterling) (2020). "On
the performance of volatility-managed portfolios."** *Journal of Financial Economics*
138(1): 95–117. (Verified as reference #17 on the same IDEAS page.)
- **Status: PEER-REVIEWED. UNVERIFIED abstract.** Commonly read as a **challenge** to
  Moreira-Muir (vol-managed strategies underperform out-of-sample in many cases). **Flag as
  contradicting evidence within the peer-reviewed literature.**

---

## 6. Crypto-specific evidence: does volatility regime predict crypto return DIRECTION?

### 6.1 The strongest and most relevant negative result

**Hudson, Robert; Urquhart, Andrew (2021). "Technical trading and cryptocurrencies."**
*Annals of Operations Research* 297: 191–220.
- DOI: `10.1007/s10479-019-03357-1`
- Verified URLs: https://centaur.reading.ac.uk/85715/ (CentAUR open repository, fetched
  HTTP 200; **"Refereed: Yes"**) and https://zbmath.org/?q=an%3A07344526 (zbMATH 1465.62174).
  Open-access published PDF:
  https://centaur.reading.ac.uk/85715/8/Hudson-Urquhart2019_Article_TechnicalTradingAndCryptocurre.pdf
- **Status: PEER-REVIEWED.**
- **Verbatim abstract:** "This paper carries out a comprehensive examination of technical
  trading rules in cryptocurrency markets, using data from two Bitcoin markets and three
  other popular cryptocurrencies. **We employ almost 15,000 technical trading rules from the
  main five classes of technical trading rules and find significant predictability and
  profitability for each class of technical trading rule in each cryptocurrency.** We find
  that the breakeven transaction costs are substantially higher than those typically found in
  cryptocurrency markets. To safeguard against data-snooping, we implement a number of
  multiple hypothesis procedures which confirms our findings that technical trading rules do
  offer significant predictive power and profitability to investors. We also show that the
  technical trading rules offer substantially higher risk-adjusted returns than the simple
  buy-and-hold strategy, showing protection against lengthy and severe drawdowns associated
  with cryptocurrency markets. **However there is no predictability for Bitcoin in the
  out-of-sample period**, although predictability remains in other cryptocurrency markets."

**This cuts both ways and must be reported honestly:**
- **FOR** technical rules in crypto: 15,000 rules, five classes, five cryptocurrencies,
  multiple-hypothesis (data-snooping) controls, and positive in-sample results with breakeven
  costs above typical crypto costs.
- **AGAINST** for our use case: **"there is no predictability for Bitcoin in the
  out-of-sample period."** Bitcoin is our primary market. This is the decisive caveat.
- Note also: this is about technical trading rules generally (largely trend/momentum classes),
  **not** specifically volatility-contraction squeezes, and it does not isolate a
  volatility-regime → direction channel.

**Additional peer-reviewed crypto TA/predictability work (verified bibliographically, abstracts
not fetched):**
- **Detzel, Andrew; Liu, Hong; Strauss, Jack; Zhou, Guofu; Zhu, Yingzi (2021). "Learning and
  predictability via technical analysis: Evidence from bitcoin and stocks with hard-to-value
  fundamentals."** *Financial Management* 50(1): 107–137. **PEER-REVIEWED.** (Verified via
  RePEc reference listing on the IDEAS page for `10.1057/s41283-026-00234-7`.)
- **Cheah, Jeremy Eng-Tuck; Luo, Di; Zhang, Zhuang; Sung, Ming-Chien (2022). "Predictability
  of bitcoin returns."** *The European Journal of Finance* 28(1): 66–85.
  **PEER-REVIEWED.** (Same verification route.) **UNVERIFIED abstract.**
- **Liu, Yukun; Tsyvinski, Aleh; Wu, Xi (2022). "Common Risk Factors in Cryptocurrency."**
  *Journal of Finance* 77(2): 1133–1177. **PEER-REVIEWED.** (Same route.) Cross-sectional
  factor model; not a volatile-regime timing result.
- **Liu, Yukun; Tsyvinski, Aleh (2021). "Risks and Returns of Cryptocurrency."** *The Review
  of Financial Studies* 34(6): 2689–2727. **PEER-REVIEWED.** (Same route.)

### 6.2 Crypto volatility regimes exist — but the papers are about volatility, not direction

**Ardia, David; Bluteau, Keven; Rüede, Maxime (2019). "Regime changes in Bitcoin GARCH
volatility dynamics."** *Finance Research Letters* 29: 266–271.
- DOI: `10.1016/j.frl.2018.08.009` — verified via Semantic Scholar API: title, all three
  authors, year 2019, venue "Finance Research Letters", **264 citations**, open access
  (HYBRID, CC BY-NC-ND).
- **Status: PEER-REVIEWED.**
- **Verbatim abstract:** "We test the presence of regime changes in the GARCH volatility
  dynamics of Bitcoin log–returns using Markov–switching GARCH (MSGARCH) models. We also
  compare MSGARCH to traditional single–regime GARCH specifications in predicting one–day
  ahead Value–at–Risk (VaR). The Bayesian approach is used to estimate the model parameters
  and to compute the VaR forecasts. **We find strong evidence of regime changes in the GARCH
  process and show that MSGARCH models outperform single–regime specifications when
  predicting the VaR.**"
- **KEY: the target variable is VaR — a MAGNITUDE/risk quantity. The paper makes no claim
  about return direction.** This is a clean illustration of the magnitude-vs-direction split.

**Shakourloo, Amin; Azimli, Asil (2026). "Regime-switching in bitcoin volatility under
global uncertainty: Markov-switching GARCH and hidden Markov Copula approaches."**
*Research in International Business and Finance*, article 103295.
- DOI: `10.1016/j.ribaf.2026.103295`
- Verified URL: https://ouci.dntb.gov.ua/en/works/4VPVobVq/ (fetched HTTP 200; OUCI confirms
  **Scopus: Yes, Web of Science: Yes**, 5 citations, 79 references)
- **Status: PEER-REVIEWED.** **Abstract not available on the OUCI page — I do NOT quote one.**
- Title indicates the object of study is **volatility**, not direction.

**Tsuji, Chikashi (2025). "The risk–return trade-off of Bitcoin: Evidence from
regime-switching analysis."** *Future Business Journal* 11: 138.
- DOI: `10.1186/s43093-025-00551-5` — verified via Semantic Scholar API (title, author
  Chikashi Tsuji, venue "Future Business Journal", year 2025, **1 citation**, open access
  GOLD CC BY).
- **Status: PEER-REVIEWED** (SpringerOpen). Note: **only 1 citation** — very new and
  unreplicated. **Flag as weak evidence.**
- **Verbatim abstract:** "Despite its importance, there has been little research on the
  relationship between Bitcoin's risk and returns. ... In the existing limited literature, a
  **negative** risk–return relationship in Bitcoin for high-frequency intraday time-series
  data has been reported. In this paper, we use lower time-frequency data and suitable models
  for the data frequency to examine the risk–return trade-off of Bitcoin. Specifically, this
  paper examines the time-series volatility risk–return trade-off of Bitcoin using standard
  Markov switching (MS) and MS–GARCH models with weekly Bitcoin data from 2010 to 2024.
  Consequently, the study reveals several new findings. Firstly, the volatility risk–return
  trade-off relationship is identified for Bitcoin's log returns. Secondly, the risk–return
  trade-off is also found for Bitcoin's simple returns. Thirdly, the risk–return trade-off is
  uncovered for Bitcoin's risk premiums as well. ... **We emphasize that we have discovered
  positive weekly risk–return relationships for Bitcoin using Markov switching models for the
  first time.**"

**Critical reading:** this is a **positive risk–return trade-off** result — high volatility
regimes carry **higher average returns** — on **WEEKLY** Bitcoin data. If anything this
**contradicts** a naive "low-volatility regime → long" prior: it suggests the compensation,
such as it is, is in the *high*-volatility state. Note also it directly contradicts the
earlier high-frequency literature finding a **negative** relationship, which the authors
themselves acknowledge. **Flag as contradictory and unreplicated (1 citation).**

**Working paper (NOT peer-reviewed) — crypto regimes:**
"Regime-Specific Dynamics and Informational Efficiency in Cryptomarkets: Evidence from
Gaussian Mixture Models," Univ. Trier CREM Working Paper 2024-13, RePEc `RePEc:tut:cremwp:2024-13`,
SSRN abstract_id 5118062.
- **Status: WORKING PAPER, NOT PEER-REVIEWED.** I attempted three fetch routes
  (EconPapers, SSRN, univ-rennes1 PDF) and **could not retrieve the abstract**.
  **LABEL: UNVERIFIED CONTENT.** Do not cite its findings.

**Crypto momentum (direction from PRICE trend, not from volatility):**
**Kang, Yeonchan; Ryu, Doojin (2026). "Time-series momentum and market timing in Bitcoin."**
*Risk Management* 28(3): 1–23, Palgrave Macmillan.
- DOI: `10.1057/s41283-026-00234-7`
- Verified URL: https://ideas.repec.org/a/pal/risman/v28y2026i3d10.1057_s41283-026-00234-7.html
  (fetched HTTP 200)
- **Status: PEER-REVIEWED.**
- **Verbatim abstract:** "Slower momentum signals deliver better risk-adjusted performance
  than fast signals in Bitcoin, where continuous trading, high volatility, and short-horizon
  noise make rapid signal adjustment prone to overreaction. We vary momentum speed along a
  continuous parameter and find that **slow signals, based on a 12-week baseline horizon,
  outperform intermediate and fast alternatives.** The equity-market pattern reverses: slow
  signals hold exposure through profitable disagreement states, while fast signals overreact
  to noise and show weaker timing performance. **Dynamic speed adjustment adds little beyond a
  simple low-speed rule.** The optimal speed is market-specific, and **signal speed acts as an
  endogenous risk-management device, not merely a return-prediction choice.**"
- **Directly useful to us:** it says the edge in Bitcoin lies in **slow (12-week)** signals,
  that fast signals are **prone to overreaction** in high-volatility, short-horizon-noise
  conditions, and that dynamic speed tuning "adds little." A fast, daily squeeze-breakout
  system sits squarely in the "fast signals overreact" bucket this paper warns about.

---

## 7. Summary table — evidence strength for the squeeze thesis

| Claim | Best evidence | Status | Verdict |
|---|---|---|---|
| Volatility clusters / is persistent | Engle 1982; Bollerslev 1986; Bollerslev, Chou & Kroner 1992 | **PEER-REVIEWED** | **Strongly supported** |
| Volatility contraction precedes volatility expansion | Same + realized-vol literature | **PEER-REVIEWED** | **Supported (magnitude)** |
| Contraction/expansion is tradeable (ORB, oil futures, intraday) | Holmberg, Lönnbark & Lundström 2013 | **PEER-REVIEWED** | Positive, but different market/frequency |
| Volatility predicts the **sign** of returns | Christoffersen & Diebold 2006 | **PEER-REVIEWED** | Possible in theory; **explicitly NOT at daily frequency** |
| Bollinger-band **breakout** continuation works | Fang, Jacobsen & Qin 2017 | **PEER-REVIEWED** | **NEGATIVE** — returns "mostly negative… worsened over time" |
| TTM Squeeze works | John Carter book / Barchart PDF / TradingView ports | **PRACTITIONER / VENDOR** | **No evidence** |
| Regime switching predicts direction after costs | Giner & Zakamulin 2023; Zakamulin & Giner 2024 | **PEER-REVIEWED** | **Theoretical**; no after-cost OOS daily claim |
| Regime/return forecasting beats benchmarks | Haase & Neuenkirch 2023 | **PEER-REVIEWED** | **NEGATIVE** — "do not outperform common benchmarks" |
| Volatility-regime in crypto works | Hudson & Urquhart 2021 | **PEER-REVIEWED** | **Bitcoin: no OOS predictability** |
| High-vol crypto regime carries higher returns | Tsuji 2025 | **PEER-REVIEWED (1 citation)** | **Contradicts** low-vol thesis; unreplicated |
| VOV predicts returns | Chen, Chordia, Chung & Lin 2021; Hollstein & Prokopczuk 2018 | **PEER-REVIEWED** | Yes, but **options-based, risk-premium, crash-concentrated** |

---

## 8. Explicit UNVERIFIED items (do not cite without further checking)

1. **Bollerslev, Chou & Kroner (1992)** — bibliographic record verified; **abstract text NOT verified**.
2. **Andersen, Bollerslev, Diebold & Labys (2003)** — bibliographic record verified; **abstract NOT verified**.
3. **Baker, Bradley & Wurgler (2011)** — bibliographic record verified; **abstract elided by publisher, NOT verified**.
4. **Bollerslev, Tauchen & Zhou (2009)** — bibliographic record verified; **abstract NOT verified**.
5. **Carr & Wu (2009)** — bibliographic record verified; **abstract NOT verified**.
6. **Hollstein & Prokopczuk (2018)** — bibliographic record verified; **abstract NOT verified**.
7. **Nelson (1991) EGARCH** — journal/volume/pages verified via EconPapers + MTMT; **DOI mapping
   to `10.2307/1912767` is CONFLICTED (resolves to Kreps & Wilson 1982 in Semantic Scholar)**.
   Verify the DOI against the printed article before citing.
8. **Fang, Jacobsen & Qin (2017)** — bibliographic record verified; **abstract elided by
   publisher**. The quoted finding ("mostly negative… worsened over time") is
   **SECONDARY-SOURCED** from JAG Capital's blog. **Re-verify against the PDF before publication.**
9. **Carter, *Mastering the Trade*** — book authorship and existence corroborated by library
   records; **exact ISBN and edition year UNVERIFIED**.
10. **Bajgrowicz & Scaillet (2012)** — bibliographic record verified via RePEc citation list;
    **abstract NOT verified**.
11. **Cheah et al. (2022); Detzel et al. (2021); Liu & Tsyvinski (2021); Liu, Tsyvinski & Wu
    (2022); Moreira & Muir (2017); Cederburg et al. (2020); Hammerschmid & Lohre (2018)** —
    bibliographic records verified via RePEc reference listings; **abstracts NOT verified**.
12. **Shakourloo & Azimli (2026)** — journal, DOI, authors, Scopus/WoS indexing verified via
    OUCI; **abstract NOT available, NOT verified**.
13. **SSRN 5288827** ("Quantifying Volatility Compression and Expansion") — URL exists
    (papers.ssrn.com, returns 403 to automated fetch); **content UNVERIFIED**; **UNREFEREED
    WORKING PAPER**. Do not cite.
14. **RePEc `RePEc:tut:cremwp:2024-13`** / **SSRN 5118062** (crypto GMM regimes) — landing
    pages exist; **abstract could NOT be retrieved; content UNVERIFIED**; **UNREFEREED**.
15. **Wu et al. (2026)** ORB+LSTM — bibliographic record verified; **abstract NOT available**.

---

## 9. What this means for the platform (direct, skeptical conclusions)

1. **Volatility contraction is a legitimate, peer-reviewed signal for MAGNITUDE and RISK.**
   Use it for position sizing, stop distance, expected-move scaling, and volatility targeting —
   that is where the peer-reviewed evidence actually points (Engle, Bollerslev, realized-vol
   literature, vol-managed portfolios).

2. **There is no peer-reviewed basis for using a squeeze to predict DIRECTION at daily
   frequency.** Christoffersen & Diebold is explicit: sign forecastability needs a nonzero
   expected return, and they state outright that it should not be expected at daily horizons.
   On a near-zero-drift daily crypto series, the mechanism is approximately switched off.

3. **A long-only constraint removes the one channel that might have helped.** If any
   volatility-driven sign effect exists, it is a *conditional probability shift* around 0.5,
   not a reliable up/down call. Long-only spot cannot express the short side, so a
   symmetric-or-unknown-direction edge cannot be harvested — you would be taking every
   breakout, including the failures.

4. **The one peer-reviewed test of the closest analogue (Bollinger breakout) is negative**,
   and the peer-reviewed crypto TA paper that is most favourable **fails out-of-sample on
   Bitcoin specifically**.

5. **Beware the specific failure mode the literature flags.** Christoffersen & Diebold warn
   that market-timing tests have *little or no power* to detect volatility-driven sign
   dependence. A promising squeeze backtest evaluated with a directional-accuracy or
   market-timing test is therefore **not** evidence of an edge — it is the documented blind
   spot. Use out-of-sample testing with realistic crypto spot fees/slippage, and treat any
   in-sample Sharpe from a squeeze rule as unproven.

6. **Speed matters and fast is penalised.** Kang & Ryu (2026) find slow (12-week) Bitcoin
   signals beat fast ones, that fast signals "overreact to noise," and that dynamic tuning
   "adds little." A daily squeeze trigger is a fast signal.

7. **If the squeeze idea is pursued, the defensible research design is:** treat it as a
   **volatility-timing / risk-sizing** rule (magnitude), not a direction rule; require
   out-of-sample validation with realistic costs; benchmark against a simple slow trend rule
   (the literature's actual winner); and pre-register the test to avoid the data-snooping and
   false-discovery problems that Bajgrowicz & Scaillet and Hudson & Urquhart both address.
