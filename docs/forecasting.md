# Forecasting layer — offline TimesFM-backed predictions

This page documents `trading_platform.forecast`, the layer that pre-computes
probabilistic price paths **offline**, stores them in a versioned parquet
artifact, and feeds them to the `timesfm` strategy as a deterministic external
input. It complements:

- [`architecture.md`](architecture.md) — layers, frozen interfaces, dependency rule;
- [`usage.md`](usage.md) — installation, configuration, CLI, reports, Docker;
- [`backtesting-methodology.md`](backtesting-methodology.md) — validation protocol and thresholds;
- [`testing-policy.md`](testing-policy.md) — test and coverage protocol (the single source of truth).

> **Honesty first.** A positive backtest PnL is **not evidence of an edge**
> (§9). The deliverable ships its own skill measurement precisely because the
> zero-shot skill of a time-series foundation model on 1h crypto is
> **unverified**.

---

## 1. Overview and the dependency direction

```
core  ->  forecast  ->  strategy  ->  cli
```

`trading_platform.forecast` sits **between** `core` (layer 1) and
`trading_platform.strategy` (layer 3); `docs/architecture.md` §3 numbers it
**layer 2.5**. The dependency rule is one-directional and mechanical:

- it may import the standard library, `numpy`, `pandas`, `pyarrow` and
  `trading_platform.core` — **nothing else from the project**;
- `torch`, `timesfm` and `jax` are **optional** and are imported **lazily inside
  functions**, exactly like `freqtrade` and `ccxt` elsewhere in this repository:
  importing the package, running the CLI and running the whole test suite stay
  green with only the `.[dev]` extra installed;
- the strategy layer imports `forecast` **downwards**; `forecast` never imports
  `strategy`, never imports `config`, and never imports the CLI.

**The strategy never predicts.** `Strategy.prepare()` and `Strategy.signals()`
are pure, deterministic and I/O-free (see [`testing-policy.md`](testing-policy.md)),
so TimesFM never runs inside them. Instead:

1. the **CLI** runs the model **offline**, once per forecast origin, over a
   candle file (`trading forecast-build`);
2. it writes a **versioned parquet artifact** plus a JSON sidecar;
3. the strategy consumes that artifact as a **deterministic external input**:
   same artifact in, same signals out — no model, no network, no clock.

Two prediction targets are stored and consumed:

| Target | Column family | Used for |
| --- | --- | --- |
| Normalised log-price **path** (direction) | `forecast_alpha_*`, `forecast_slope`, `forecast_path_eff`, `forecast_mfe`, `forecast_mae` | entry direction and size |
| Predicted **dispersion** (volatility regime) | `forecast_iqr_term`, `forecast_iqr_per_bar`, `vol_ratio`, `forecast_reliability` | regime filter, risk, exit |

The model always sees a **deterministically de-seasonalised** price series
(calendar effects baked into the target) so that the stored path is a
de-seasonalised path with no future-covariate leakage.

---

## 2. Artifact format

The artifact is a single **parquet** file written by `trading forecast-build`:

| Element | Type | Content |
| --- | --- | --- |
| index | `DatetimeIndex`, named `origin` | the **last context candle** of each forecast; strictly increasing, past-only |
| `horizon` | `int16` | number of stored forecast steps `H` |
| `stride` | `int16` | candles between two stored origins (`reforecast_every`) |
| `n_quantiles` | `int16` | number of stored quantile levels (9 for the ten-channel TimesFM output: q10…q90) |
| `quantile_levels` | `list<float32>` | the stored levels, e.g. `[0.1, 0.2, …, 0.9]` |
| `median` | `list<float32>`, length `horizon` | the predicted median path, **levels relative to the origin close** (level `0.0` at the origin) |
| `quantiles` | `list<float32>`, length `n_quantiles * horizon` | **row-major**: quantile path 0 (all `H` steps), then path 1, … then path `n_quantiles − 1` |

Row `origin` means: *"using only candles `<= origin`, the next `H` levels are
predicted to be `median[0..H-1]` (in normalised units relative to the origin
close), with the deciles stored in `quantiles`"*. A round-trip parquet test
pins this layout.

Next to the parquet file lives a sidecar metadata file named
`<artifact>.meta.json` (for `forecast.parquet`, the sidecar is
`forecast.parquet.meta.json`). It carries:

| Field | Meaning |
| --- | --- |
| `schema_version` | artifact schema version (integer, bumped on any layout change) |
| `symbol` | instrument the artifact was built for |
| `timeframe` | candle timeframe |
| `backend` | backend name (`naive`, `seasonal`, `timesfm`, …) |
| `model_id` | model identifier (empty for the offline backends) |
| `context_length` | number of candles fed to the backend per origin |
| `stride` | same value as the `stride` column |
| `horizon` | same value as the `horizon` column |
| `quantile_levels` | the stored levels |
| `features` | the feature/column list the artifact was built from |
| `created_at` | creation timestamp (ISO-8601 UTC) |
| `package_version` | `trading_platform.__version__` of the writer |
| `data_span` | `[first_candle, last_candle]` of the source frame |
| `license_note` | licence note of the backend (see §7) |

`ForecastStore.load(path)` refuses a missing, truncated, corrupt or
schema-incompatible artifact with `ForecastArtifactError`; it never guesses and
never silently degrades to an empty forecast.

---

## 3. Backend contract

```python
class ForecastBackend(Protocol):
    name: str

    def is_available(self) -> bool: ...
    def predict(
        self, requests: Sequence[ForecastRequest], *, horizon: int
    ) -> list[ForecastTrajectory]: ...
```

`ForecastRequest` is a frozen dataclass `(origin: pd.Timestamp,
context: tuple[float, ...])`; `ForecastTrajectory` is a frozen dataclass
`(origin, timeframe, horizon, quantile_levels, quantiles)` where `quantiles` is a
`float32` array shaped `(len(quantile_levels), horizon)` and `median` is the
`0.5` row (helper methods expose the path shape, e.g. `alpha(k)`).

The registry mirrors `trading_platform.strategy.registry`:

| Symbol | Role |
| --- | --- |
| `register_backend` | class decorator that registers a backend under `name` |
| `available_backends` | names known to the process, sorted |
| `installed_backends` | names whose optional dependency is actually importable (`is_available()`) |
| `get_backend(name, **options)` | instantiates a registered backend; raises `ForecastError` on an unknown name or a missing extra |

Three backends ship:

| Backend | Needs | What it does |
| --- | --- | --- |
| `naive` | numpy/pandas only | **random walk**: the median holds the last context value flat; the dispersion comes from the trailing absolute differences, scaled by `sqrt(step)`. It is the honest baseline. |
| `seasonal` | numpy/pandas only | **seasonal-naive, drift-corrected**: a calendar-aware deterministic baseline on the de-seasonalised series (same weekday/hour bucket plus the trailing drift). It is the second honest baseline. |
| `timesfm` | the `timesfm` extra | TimesFM **2.5** (`google/timesfm-2.5-200m-pytorch`, Apache-2.0), zero-shot univariate forecasting with the continuous quantile head. |

The `naive` and `seasonal` backends are **fully offline**: they make the whole
pipeline, the CLI and the tests runnable without any ML dependency, and they are
the baselines a TimesFM artifact must beat before it deserves any trust.

`timesfm` backend options: model id, device, `torch_compile`, `max_context`,
`max_horizon`, the `ForecastConfig` flags (`normalize_inputs=True`,
`use_continuous_quantile_head=True`, `fix_quantile_crossing=True`,
`infer_is_positive=False` because the target is a signed de-seasonalised
log-price, `force_flip_invariance=False` — it exactly doubles latency), and an
optional, explicitly opt-in `xreg` mode (calendar-only past **and** future
covariates; never the default). Requests are grouped by equal context length and
sent as **one batched `model.forecast(inputs=[...])` call per group**; the caller
list is always a fresh copy (§8). On Apple silicon the backend resolves to
**CPU**: the 2.5 code path never selects MPS.

---

## 4. Install

The whole test suite, the CLI, `forecast-build` with the offline backends and
every documented offline recipe need **only** the dev extra:

```bash
python3.11 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
```

The real model lives behind its own optional extras — never installed by CI,
never required by the suite:

```bash
# TimesFM 2.5 (Apache-2.0 weights) — the default backend
.venv/bin/python -m pip install -e ".[timesfm]"

# optional XReg covariate mode (pulls jax + scikit-learn)
.venv/bin/python -m pip install -e ".[timesfm-xreg]"
```

Exact error hints raised by the code:

| Situation | Error | Hint in the message |
| --- | --- | --- |
| `--backend timesfm` without the extra | `ForecastError` (never a bare `ImportError`) | `pip install 'trading-platform[timesfm]'` |
| `--backend timesfm --xreg` without `timesfm[xreg]` | `ForecastError` | `pip install 'trading-platform[timesfm-xreg]'` |
| missing / corrupt / incompatible artifact | `ForecastArtifactError` | the offending path and the reason |

`is_available()` **returns `False`, it never raises**, when its extra is
missing — which is why `trading forecast-info`/`available_backends` stay usable
on a machine without torch.

---

## 5. Usage

```bash
# 1. build the artifact offline (naive = random-walk baseline, no ML dependency)
trading forecast-build --config config/backtest_default.json --data-file data/BTC_USDT-1h.csv --out data/forecast/forecast.parquet --backend naive

# 2. measure the artifact's forecast skill against the random walk
trading forecast-skill --artifact data/forecast/forecast.parquet \
    --data-file data/BTC_USDT-1h.csv

# 3. inspect the artifact metadata (schema, backend, span, licence note)
trading forecast-info --artifact data/forecast/forecast.parquet

# the same build through make (DATA_FILE / FORECAST_ARTIFACT / FORECAST_BACKEND override)
make forecast-build
```

Exact options:

| Command | Options |
| --- | --- |
| `trading forecast-build` | `--config/-c` (required), `--data-file`, `--out`, `--backend naive\|seasonal\|timesfm`, `--context N`, `--horizon H`, `--reforecast-every S` |
| `trading forecast-skill` | `--artifact`, `--data-file` |
| `trading forecast-info` | `--artifact` |

All three accept `--json` (a single JSON object on stdout, like every other
command). `--context` is the number of candles fed to the backend per origin,
`--horizon` the number of stored forecast steps, `--reforecast-every` the stride
between two stored origins. Each origin uses **only candles `<= origin`**.

`--context` / `--horizon` size the *artifact*; they are **not** forwarded to the
model-side limits of the TimesFM backend (`max_context=1024`,
`max_horizon=128`, applied through `model.compile(timesfm.ForecastConfig(...))`,
which enforces `max_context + max_horizon <= 16384`). A longer context therefore
needs the backend option rather than the flag: pass `max_context` through
`ForecastBuildConfig.backend_options` (an unknown option raises a `TypeError`, it
is never silently ignored) and keep `--context <= max_context`.

### Configuration

The `forecast` section of `AppConfig` (validated, `extra="forbid"`) tells the
strategy where the artifact is:

| Key | Default | Meaning |
| --- | --- | --- |
| `forecast.artifact` | `null` | path of the parquet artifact to consume. `null` means *no forecast data*: the `timesfm` strategy then emits **no signal at all** (it never guesses, and it never raises). |

A minimal configuration for the strategy is therefore:

```json
{
  "strategy": {"name": "timesfm"},
  "forecast": {"artifact": "data/forecast/forecast.parquet"}
}
```

### Strategy parameters

`TimesfmForecastStrategy` (`name = "timesfm"`, module
`trading_platform.strategy.timesfm_forecast`) is parameterised by
`TimesFMForecastParams` (pydantic, `extra="forbid"`, cross-field coherence
validated) and exposes the same `PARAM_SPACE` grid for the robustness sweep. The
groups are:

| Group | Parameters |
| --- | --- |
| Artifact wiring | artifact path / feature wiring, `context_length`, `horizon`, `reforecast_every` (stride), `min_lead` (candle cushion: decisions only where the active path still has room), forecast-age staleness bound |
| Entry gates | minimum predicted edge (`min_alpha`), edge versus ATR (`alpha_vs_atr`), reliability (`min_reliability`), decile agreement (`min_agreement`), path efficiency (`min_path_efficiency`), volatility regime (`max_vol_ratio`) and ATR-percentile window (`min_atr_percentile` / `max_atr_percentile`), optional RSI context filter (`rsi_min` / `rsi_max`), cooldown after a losing exit (`cooldown`) |
| Exit thresholds | `exit_alpha` (edge decay), `exit_confirm` (consecutive confirming bars), `target_capture`, `min_progress`, `exit_vol_ratio`, path-degradation thresholds |
| Risk | ATR period and `atr_stop_multiplier` (the static protective stop carried in the `stop_loss` column), `max_hold`, `allow_short` |

The authoritative default of every field is the one declared in
`TimesFMForecastParams`; read it with
`python -c "from trading_platform.strategy.timesfm_forecast import TimesFMForecastParams as P; print(P())"`
or with `trading config show`.

---

## 6. Reproduce an end-to-end offline backtest

No ML dependency, no network, no cache — the whole loop runs on the synthetic
generator:

```python
from pathlib import Path

from trading_platform.config import load_config
from trading_platform.data.synthetic import make_ohlcv
from trading_platform.forecast.artifact import ForecastBuildConfig, build_forecast_artifact
from trading_platform.strategy.engine import run_backtest_on_config

# 1. a deterministic candle frame (offline, seeded)
frame = make_ohlcv(2000, start="2022-01-01T00:00:00Z", timeframe="1h", seed=42)

# 2. build the artifact offline with the random-walk baseline
build_forecast_artifact(
    frame,
    ForecastBuildConfig(backend="naive", context_length=256, horizon=24, reforecast_every=8),
    Path("data/forecast/forecast.parquet"),
)

# 3. point an AppConfig JSON at the artifact, then backtest the strategy
Path("forecast_config.json").write_text(
    '{"strategy": {"name": "timesfm"}, "forecast": {"artifact": "data/forecast/forecast.parquet"}}',
    encoding="utf-8",
)
result = run_backtest_on_config(load_config("forecast_config.json"), frame)
print(result.n_trades, result.final_balance)
```

> **The `naive` baseline deliberately opens no position.** A random walk has no
> thesis: its median path is flat, so `forecast_agreement` never clears and the
> reliability gate never fires, leaving `n_trades == 0` and
> `final_balance == initial_balance`. `test_end_to_end_backtest_with_the_naive_baseline`
> pins exactly that — it is the honest baseline, not a broken pipeline. Swap in
> the calendar-aware `seasonal` baseline to watch the same artifact contract
> actually trade:
>
> ```python
> build_forecast_artifact(
>     frame,
>     ForecastBuildConfig(backend="seasonal", context_length=256, horizon=24, reforecast_every=8),
>     Path("data/forecast/forecast.parquet"),
> )
> result = run_backtest_on_config(load_config("forecast_config.json"), frame)
> print(result.n_trades, result.final_balance)  # 28 trades, 6078.79 on the seed-42 frame
> ```
>
> Those 28 trades **lose about 39 %** of the account on this synthetic frame.
> That is the point of the honesty rule in section 9: this loop proves that the
> artifact, the gates, the exits and the engine wiring all work end to end — it
> proves nothing about profitability, and on this frame the result is not even
> profitable.

`run_backtest_on_config` resolves the `forecast` section through the
feature-injection seam (`trading_platform.strategy.features`) and injects the
loaded `ForecastStore` into the strategy instance — so `backtest`,
`walk-forward`, `robustness` and `monte-carlo` all work unchanged on the new
strategy:

```bash
make forecast-build                       # naive backend by default (no trade: see the note above)
make forecast-build FORECAST_BACKEND=seasonal   # same path, a baseline that does trade
trading backtest  --config forecast_config.json --data-file btc.csv --no-network
trading walk-forward --config forecast_config.json --data-file btc.csv --no-network
```

> **Scope limit — the realtime engine does not inject features yet.** The
> artifact seam is wired into the backtest and validation paths only
> (`strategy.engine.run_backtest_on_config` / `make_runner`). Because `timesfm`
> is a normal registry entry, it appears in the realtime catalog
> (`GET /api/catalog`) and a realtime profile may name it; such a profile then
> instantiates the strategy **without** a bundle, so every forecast diagnostic
> stays `NaN` and the profile emits **no signal at all** — it never raises and it
> never guesses. Feed a realtime or Freqtrade deployment with `basic` until the
> realtime engine grows its own feature resolution.

---

## 7. Licence table

| Artefact | Licence | Commercial use |
| --- | --- | --- |
| TimesFM **source code** (`timesfm` on PyPI) | Apache-2.0 | yes |
| TimesFM **weights 1.0 / 2.0 / 2.5** — `google/timesfm-2.5-200m-pytorch` is the **default** here | Apache-2.0 | yes |
| TimesFM **weights 3.0** (Aug 2026, `google/timesfm-3.0-pytorch`) | **TimesFM Non-Commercial License v1.0** — non-commercial, non-production, no redistribution; fine-tuned derivatives included | **no** |

This repository is **proprietary**, which is exactly why TimesFM **2.5** is the
default backend and 3.0 is never selected implicitly: 3.0 is technically better
(native multivariate forecasting, native past+future covariates) but its weights
forbid commercial or production use. Wiring 3.0 is an explicit, opt-in,
documented choice — never a default, and never a silent fallback.

---

## 8. Measured facts and library traps

Everything in this section was **measured on this machine** (Apple M4, 16 GB
RAM, CPU-only, `timesfm==3.0.2` / `torch==2.14.0` serving the 2.5 checkpoint) or
read directly from the library source, not repeated from a vendor claim:

- **CPU only on Apple silicon.** The 2.5 PyTorch code path selects `cuda` or
  `cpu`; **MPS is never selected** and moving the module to MPS manually is
  unsupported (it fails on the device transfer of the float64 padding row). The
  backend therefore resolves to CPU and says so.
- **Cost.** Context 1024 / batch 32 costs about **741 ms**, i.e. roughly **23 ms
  per series**; context 512 / batch 32 costs 388 ms. `horizon` is nearly free
  (1, 24, 72 and 128 steps all cost about the same at a fixed `max_horizon`),
  while **`max_horizon` and `context` drive the cost** (128 → 256 → 512 costs
  762 → 957 → 1249 ms).
- **`force_flip_invariance=True` exactly doubles latency** (741 ms → 381 ms when
  disabled) because the decode runs a second time on the negated input. It
  defaults to `False` here.
- **`forecast()` mutates the caller's input list in place**: when the batch size
  is not a multiple of `global_batch_size` the library appends dummy rows to
  *your* list, so the next call would return the padded batch shape. The backend
  therefore always passes a **fresh copy** of the request list.
- **Quantile channel 0 is the MEAN, not a quantile.** Indices 1…9 are
  q10…q90 (monotone); the backend drops channel 0 and takes the median from the
  point forecast (channel 5).
- **Trailing NaNs are not handled** by the library (leading NaNs are stripped,
  internal NaNs interpolated): the caller drops them before batching.
- **`forecast_naive` is not on the public wrapper** (it lives on `model.model`)
  and is documented upstream as debugging-only; the backend never uses it.
- **Constraints:** `compile()` enforces `max_context + max_horizon <= 16384`,
  and the continuous quantile head caps `max_horizon` at **1024**.
- **Covariates never enter the transformer in 2.5.** The tokenizer input is
  patch ⊕ mask only, with no covariate and no calendar channel.
  `forecast_with_covariates()` requires `return_backcast=True`, the
  `timesfm[xreg]` extra (jax + scikit-learn) and dynamic covariates spanning the
  context **and** the horizon; it is a per-series **ridge regression (XReg)**
  wrapped *around* the model, not a channel the network attends to. Only
  **deterministic calendar covariates** can ever be supplied for the future —
  RSI or momentum cannot be turned into future covariates without fabricating
  the future, which is look-ahead leakage and is **rejected** by design.

---

## 9. Honesty: what this proves, and what it does not

**What the suite proves.** Determinism (same artifact in, same signals out);
**causality** (an artifact built from a truncated candle frame is byte-identical
on the shared origins — no look-ahead); the artifact schema (parquet round-trip);
the wiring (`backtest`, `walk-forward`, `robustness`, `monte-carlo` run on the
new strategy); and the **auditability** of every entry gate and every exit rule
(each rule is individually tested, and `exit_code` records which one fired).

**What the suite does NOT prove.** It does **not** prove that TimesFM has any
forecast skill on 1h crypto: zero-shot skill on this asset class is
**unverified**, and **a positive backtest PnL is not evidence of an edge** — a
backtest over one window can be luck, regime, or a base-rate artifact. The
strategy may well be **no better than the baselines**, and the honest default is
to assume it until `trading forecast-skill` says otherwise.

The 2025-26 empirical literature on foundation models for financial series —
which is why this layer measures its own skill instead of claiming one:

| Study | Finding |
| --- | --- |
| [arXiv:2606.27100](https://arxiv.org/abs/2606.27100) — *Pretrained TSFMs for Financial Return Forecasting* | TimesFM-2.5 skill versus a random walk is **negative** (AAPL −0.0319, JPM −0.0466) with one-sided Diebold–Mariano **p = 0.9999**: the random walk is better. |
| [arXiv:2511.18578](https://arxiv.org/abs/2511.18578) — *Re(Visiting) Time Series Foundation Models in Finance* | On **18.1M** daily excess returns, off-the-shelf TSFMs **lose to tree ensembles**; fine-tuning narrows but does not close the gap. |
| [arXiv:2607.12248](https://arxiv.org/abs/2607.12248) — *When Directional Accuracy Lies* | The reported ~80 % directional accuracy of a LoRA-adapted TimesFM-2.5 is a **base-rate artifact** (an always-up rule already scores ~0.70); no directional skill over the base rate at any horizon. |
| [arXiv:2607.05291](https://arxiv.org/abs/2607.05291) — *Forecasting Realized Volatility with TSFMs* | TimesFM-2.5 **loses to Log-HAR at every horizon** on realised volatility (QLIKE ratios 1.086 / 1.201 / 1.331). |

### Measured on this machine (2026-09-19, real BTC/USDT)

> **Read this section before quoting any number from this page.** An earlier
> revision of it reported a `0.650` directional accuracy and a positive skill
> score. Those figures are **against the de-seasonalised target**, and it was
> later measured that they do **not** carry over to the price a strategy is
> actually paid on. Both targets are now reported side by side; see
> [§9.1](#91-the-target-you-score-on-decides-whether-you-see-an-edge).

The two offline baselines and the real TimesFM 2.5 checkpoint were run side by
side on **2 000 real hourly Binance BTC/USDT candles** (2023-01-01 →
2023-03-25, a `+66 %` buy-and-hold window), context `1024`, horizon `24`,
stride `24`: **41 non-overlapping origins**, **984 `(origin, step)` pairs**. The
data is *not* committed (`.gitignore` ignores `/data/`); reproduce it with the
repository's own downloader, then score each backend:

```bash
make data-download                                   # fills data/cache/binance/BTC_USDT/1h.parquet
.venv/bin/python -m trading_platform forecast-build --config config/backtest_default.json \
    --data-file <candles.csv> --out forecast.parquet --backend timesfm \
    --context 1024 --horizon 24 --reforecast-every 24 \
    --model-id google/timesfm-2.5-200m-pytorch --symbol BTC/USDT
.venv/bin/python -m trading_platform forecast-skill --artifact forecast.parquet --data-file <candles.csv>
```

| Backend | `rmse_skill_score` | `mae_skill_score` | `directional_accuracy_horizon` | wall clock |
| --- | --- | --- | --- | --- |
| `timesfm` (2.5-200m, 41 origins batched, CPU) | **+0.0068** | **+0.0122** | 0.650 | 3.7 s (model load included) |
| `naive` (random walk) | 0.0000 | 0.0000 | n/a (its median path is flat) | < 0.1 s |
| `seasonal` (de-seasonalised drift) | −0.2734 | −0.2878 | 0.525 | < 0.1 s |

### The two-year walk-forward (688 origins, TimesFM 3.0)

The 41-origin figures above are a smoke test. The real measurement uses the
**whole two-year file** — 17 543 hourly candles, 2023-01-01 → 2025-01-01 — as six
overlapping quarterly folds, context `1024`, horizon `24`, stride `24`, through
the backend with `--model-id google/timesfm-3.0-pytorch`.

| fold | period | n | `rmse_skill_score` | `directional_accuracy_horizon` | `coverage_error_mean` |
| --- | --- | --- | --- | --- | --- |
| 0 | 2023-01 → 05 | 80 | +0.0043 | 0.620 | 0.012 |
| 1 | 2023-03 → 09 | 122 | −0.0069 | 0.579 | 0.036 |
| 2 | 2023-07 → 2024-01 | 122 | +0.0137 | 0.620 | 0.007 |
| 3 | 2023-11 → 2024-05 | 122 | +0.0161 | 0.612 | 0.012 |
| 4 | 2024-03 → 09 | 122 | **+0.0446** | 0.612 | 0.011 |
| 5 | 2024-07 → 12 | 122 | −0.0006 | 0.636 | 0.014 |
| **pooled** | | **688** | **+0.0123** | **0.613** | 0.016 |

Four folds of six beat the random walk on point error, and the quantile
calibration is consistently tight (a 0.016 mean coverage error against a nominal
0.10). But **the point-error improvement is not statistically significant**: with
16 512 `(origin, step)` pairs the mean squared error is `0.00025146` against
`0.00025461` for the random walk — a `+1.24 %` reduction — a Diebold–Mariano
statistic of **−0.716 (p = 0.474)** and a block-bootstrap 95 % CI of
**[−0.0134, +0.0252]**, which contains zero.

The honest reading is the one the literature predicts: **the point forecast
behaves like a random walk**, with excellent probabilistic calibration and no
reliable edge in the magnitude of the move.

**What that actually means — and it is not a green light.** On the 41 discrete
`24 h` endpoints (the decision-relevant horizon of this strategy) the skill over
the random walk **vanishes**: mean squared error `0.001489` (TimesFM) versus
`0.001480` (random walk), a Diebold–Mariano statistic of **+0.055 → two-sided
p = 0.956**. The 65 % directional accuracy is *not* a base-rate artifact on this
sample (the up-rate of those 41 windows is only `0.475`, so an always-long rule
scores `0.525`) and the forecast/realised correlation is weakly positive
(`+0.113`) — but `n = 41` inside a single `+66 %` trend window is **not**
evidence of a tradeable edge. The honest reading is the one the literature
predicts: **the point forecast behaves like a random walk shrunk mildly toward
zero**.

Two consequences are built into the design and were confirmed by this run:

- the default gates (`min_reliability = 0.5`, `min_path_efficiency = 0.2`,
  `min_agreement = 0.6`) are **materially stricter** than what this checkpoint
  produces on 1h crypto, so the honest default behaviour is *few or no trades*
  instead of a forced position;
- the `seasonal` baseline **loses** to the random walk by a wide margin on real
  data, so "the strategy made money on synthetic candles" is not evidence of
  anything — which is precisely why this section exists.

**How to check it yourself.** Run `trading forecast-skill --artifact … --data-file …`
and read the numbers **in this order**:

1. `real_rmse_skill_score` and `real_directional_accuracy_horizon` — the two keys
   measured against the **price actually traded**. These are the ones that speak
   to profitability; ignore the others until these say something.
2. `rmse_skill_score <= 0` — the artifact is **no better than the random walk**;
3. `mase >= 1` — same conclusion, on the scaled absolute-error scale;
4. `real_directional_accuracy_horizon` near `0.5` — the sign of the predicted move
   carries **no information about the price**;
5. decile `coverage` far from the nominal level — the quantiles are not
   calibrated, so the dispersion gates and the risk sizing are meaningless.

If any of these holds, the artifact carries **no usable skill**: the correct
decision is to keep the `basic` strategy (or the naive baseline) and to treat
any positive PnL of the `timesfm` strategy as noise. The strategy is designed so
that this is visible, not hidden.

### 9.0 What this model is actually good at: risk, not direction

The one thing TimesFM does demonstrably well here is estimate **volatility**. On
the same 688 origins, the predicted interquartile range of the path
(`q80 − q20` at the horizon) ranks the *realised* volatility of the horizon that
follows:

| predictor | Spearman vs future realised vol | p |
| --- | --- | --- |
| **TimesFM predicted IQR (`forecast_iqr_term`)** | **+0.443** | 1.9e−34 |
| trailing realised vol (free, no model) | +0.427 | 7.7e−32 |

The spread is monotone across quintiles — from `0.00323` in the lowest to
`0.00614` in the highest, a factor of **1.9×** — and the model is not merely
echoing recent volatility: the **partial** Spearman, controlling for trailing
volatility, is still **+0.163**. The forecast carries information about future
risk that the free baseline does not.

**This is the intended use of the layer, and it is not a directional edge.**
Two things must be said plainly:

- volatility *targeting* with this signal **reduces** PnL on a trending asset
  (−0.27 Sharpe against a matched flat allocation), because cutting exposure when
  volatility rises removes the best periods; but
- it is a **much better risk input than trailing volatility** (+0.36 Sharpe over
  that control, and a 76 % against 113 % maximum drawdown).

So the honest framing of the delivered strategy is: **a risk-management tool
built on a model whose direction is a random walk and whose dispersion is
genuinely informative.** It is not alpha, and nothing in the backtests supports
treating it as such.

### 9.1 The target you score on decides whether you see an edge

This is the most important lesson of the whole layer, and it was a real defect.

The skill report evaluates the forecast against the **de-seasonalised** target
`z[t+k] − z[t]`, because `z` is what the model is asked to predict. But a trader
is paid on the **real** price move `log(close[t+k]) − log(close[t])`. The
de-seasonalisation removes a component whose standard deviation is **0.97 %**,
while the real 24 h move has a standard deviation of **2.44 %** — and that removed
component is *not* predicted by the model. The consequence, measured on 688
origins:

| scored against | `rmse_skill_score` | directional accuracy | correlation with the forecast |
| --- | --- | --- | --- |
| de-seasonalised target (what the model predicts) | +0.0062 | **0.593** (p < 0.0001) | +0.2285 |
| **real price move** (what a strategy earns) | **−0.0378** | **0.519** | **−0.0227** |

An earlier revision of this page quoted the `0.593`. It is a true statement about
`z` and a **false** statement about PnL: the sign accuracy against the real price
is `0.519`, the correlation is `−0.023` (noise), and trading `sign(forecast)` with
a 24 h hold loses money on every variant tested, **before** fees:

| selection | n | accuracy | gross/trade | net/trade (0.1 %/side) |
| --- | --- | --- | --- | --- |
| all signals | 688 | 0.519 | −0.058 % | **−0.258 %** |
| top-2 \|forecast\| quintiles | 275 | 0.502 | −0.144 % | **−0.344 %** |
| top \|forecast\| quintile | 138 | 0.536 | −0.014 % | **−0.214 %** |

Buy & hold over the same two years: **+174 %**.

**What was done about it.** The report now carries `real_rmse`,
`real_baseline_rmse`, `real_rmse_skill_score` and
`real_directional_accuracy_horizon`, measured on the same `(origin, step)` pairs
but against the real log price. `trading forecast-skill` therefore cannot again
show a confident, highly significant `0.593` for a model that loses money: the
two numbers sit side by side and the divergence is visible in one screen.

**The general rule.** Before trusting any skill metric, ask *which series it is
scored on*. A transform that makes the modelling problem easier can also make the
metric meaningless, and the two are indistinguishable until you score against the
thing you actually trade.

---

## 10. Diagnostic columns, exit codes, and the test protocol

`prepare()` adds the house indicators (`rsi`, `atr`, `ema`) **and** the forecast
diagnostics below. Each of them is a plain column, unit-testable in isolation:

| Column | Meaning | NaN rule |
| --- | --- | --- |
| `forecast_alpha_<k>` | normalised expected move over the look-ahead `k` candles (the median path at offset `k`) | NaN when no stored trajectory covers the bar |
| `forecast_slope` | per-candle drift of the predicted path (terminal median divided by `horizon`) | NaN without a trajectory |
| `forecast_mfe` | predicted maximum favourable excursion (highest point of the median path; the short side mirrors it) | NaN without a trajectory |
| `forecast_mae` | predicted maximum adverse excursion (lowest point of the median path; the short side mirrors it) | NaN without a trajectory |
| `forecast_path_eff` | net move divided by the path length (straightness: a noisy path scores low) | `0.0` when the path length is 0, NaN without a trajectory |
| `forecast_iqr_term` | terminal dispersion: the stored interquartile range, or `0.526 × (q90 − q10)` when only deciles are stored | NaN without a trajectory, or when the artifact stores no dispersion level at all |
| `forecast_iqr_per_bar` | per-bar dispersion (terminal dispersion scaled by `1/sqrt(horizon)`) | NaN without a trajectory, or when the artifact stores no dispersion level at all |
| `forecast_reliability` | predicted edge divided by per-bar dispersion (a forecast Sharpe) | `0.0` when the dispersion is 0, NaN when it is unknown, never `inf` |
| `forecast_agreement` | fraction of stored deciles on the same side of zero at the horizon | `0.0` when the median move is 0 or no other level is stored, NaN without a trajectory |
| `vol_ratio` | forecast dispersion divided by trailing realised volatility (regime detector) | NaN when the trailing volatility or the forecast dispersion is unknown |
| `forecast_age` | candles since the active origin (`t − a(t)`, bounded by the stride) | NaN outside the covered window |
| `atr_pct` | `ATR / close` (the noise scale the edge is compared to) | NaN during the ATR warm-up |
| `exit_code` | integer code of the exit that fired on the bar (see below) | `0` when no exit fired (never NaN) |

A NaN in a gate column is a **false** gate: the strategy emits **no signal**
(never an exception, never a guess). A missing artifact, an unknown origin, a
stale origin or a decision outside the covered window all produce no signal.

The frozen `EXIT_CODES` mapping, in priority order:

| `exit_code` | Reason | Meaning |
| --- | --- | --- |
| 1 | `FORECAST_FLIP` | the predicted edge reverses beyond `exit_alpha` against the position |
| 2 | `EDGE_DECAY` | the edge falls under `exit_alpha` for `exit_confirm` consecutive bars |
| 3 | `TARGET_REACHED` | the realised move captured `target_capture` of the excursion predicted at entry, and the forecast no longer supports more |
| 4 | `PATH_DEGRADED` | the predicted path collapses (`path_eff` / `reliability`): the thesis is gone, not the price |
| 5 | `TIME_STOP` | `max_hold` candles without reaching `min_progress` of the expected move |
| 6 | `VOL_REGIME` | `vol_ratio` spikes above the exit threshold: the risk regime changed |
| 7 | `STALE_FORECAST` | the forecast disappeared (no origin, stale origin beyond the bound, or uncovered window) |
| 8 | `ATR_STOP` | the static ATR protective stop carried in the `stop_loss` column (reserved for the engine-side stop) |

Exits are evaluated **in priority order at every close, first match wins**:
`FORECAST_FLIP` → `EDGE_DECAY` → `TARGET_REACHED` → `PATH_DEGRADED` →
`TIME_STOP` → `VOL_REGIME`, with the static ATR stop in the `stop_loss` column
and the stale-forecast guard. **Every open position reaches an exit within
`min(max_hold, horizon − min_lead)` candles**, and shorts mirror longs exactly
(the engine reads `stop_loss` as the mirror of the long formula).

Tests and the coverage gate follow [`testing-policy.md`](testing-policy.md) —
scoped commands while developing, the full command with the 85 % gate before a
push. The TimesFM backend itself is tested against a **fake stub module**
injected in `sys.modules`, so the batching, the input-list-copy trap, the
quantile index mapping and the lazy-import error path are pinned **without
torch**; no test touches the network.
