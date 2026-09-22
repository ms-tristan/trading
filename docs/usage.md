# Guide d'usage

Installation, anatomie de la configuration, exécution de chaque commande CLI,
lecture des rapports, tâches `make` et usage de Docker.

Documents liés :

- [`docs/architecture.md`](architecture.md) — couches, interfaces gelées, contrat OHLCV ;
- [`docs/backtesting-methodology.md`](backtesting-methodology.md) — protocole de validation et seuils ;
- [`docs/testing-policy.md`](testing-policy.md) — politique de tests, commande complète, seuil de couverture.

---

## 1. Installation

Prérequis : **Python 3.11** (cible de référence du projet), `make` et — pour
Docker — un moteur de conteneurs.

### 1.1 Installation de développement (recommandée)

```bash
git clone <url-du-depot> Trading
cd Trading
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
```

Avec `uv` :

```bash
UV_CACHE_DIR=.uv-cache uv venv --python 3.11 .venv
UV_CACHE_DIR=.uv-cache uv pip install --python .venv/bin/python -e ".[dev]"
```

L'extra `dev` suffit pour **tout** : lint, type-check, tests, backtest sur CSV
local et rapport. Aucun accès réseau n'est nécessaire.

### 1.2 Extras optionnels

| Extra | Contenu | Quand l'installer | Impact |
| --- | --- | --- | --- |
| *(base)* | `pandas`, `numpy`, `pydantic`, `pydantic-settings`, `typer`, `rich`, `pyarrow` | toujours | moteur, config, CLI, cache parquet |
| `dev` | `pytest`, `pytest-cov`, `coverage`, `ruff`, `mypy`, `pandas-stubs`, `types-requests` | développement et CI | outillage qualité |
| `exchange` | `ccxt`, `requests` | uniquement pour télécharger des données réelles (`data download`) | ajoute un client exchange |
| `freqtrade` | `freqtrade` | uniquement pour l'exécution live/dry-run du bot | dépendance lourde (ccxt, TA-Lib) |
| `timesfm` | `timesfm`, `torch` | only to build a forecast artifact with the real model (`forecast-build --backend timesfm`) | very heavy; **never** required by the test suite nor by the `naive`/`seasonal` backends |
| `timesfm-xreg` | `timesfm`, `torch`, `jax`, `scikit-learn` | only for the calendar-only XReg covariate mode, an explicit opt-in | the heaviest one; never the default |
| `all` | `freqtrade` + `exchange` + `dev` | poste de travail complet | tout installer |

```bash
# téléchargement de données réelles
.venv/bin/python -m pip install -e ".[exchange]"

# bot Freqtrade (live / dry-run)
.venv/bin/python -m pip install -e ".[freqtrade]"

# TimesFM 2.5 — the Apache-2.0 checkpoint, the default forecast backend
.venv/bin/python -m pip install -e ".[timesfm]"

# optional XReg covariate mode (jax + scikit-learn), never the default
.venv/bin/python -m pip install -e ".[timesfm-xreg]"
```

> **Le moteur de backtest n'a besoin ni de `freqtrade` ni de `ccxt`.** La suite
> de tests doit rester verte avec le seul extra `dev` :
> voir [`docs/testing-policy.md`](testing-policy.md).
>
> The forecasting layer follows the same rule: the `naive` and `seasonal`
> backends are pure numpy/pandas, so `forecast-build`, `forecast-skill`, the
> tests and the offline backtest all run **without** `torch`
> (see `docs/forecasting.md` (page retired: the forecast subsystem was removed)).

---

## 2. Démarrage rapide

```bash
# 0. installation (voir §1)
python3.11 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"

# 1. générer un jeu de données déterministe, hors ligne, sans cache ni exchange
.venv/bin/python -c "from trading_platform.data.synthetic import make_ohlcv; \
make_ohlcv(8760, start='2022-01-01T00:00:00Z', timeframe='1h', seed=42).to_csv('btc.csv')"

# 2. backtest complet sur ce CSV
.venv/bin/python -m trading_platform.cli backtest \
    --config config/backtest_default.json --data-file btc.csv

# 3. walk-forward (le juge de paix)
.venv/bin/python -m trading_platform.cli walk-forward \
    --config config/backtest_default.json --data-file btc.csv
```

Une fois le paquet installé, la commande console `trading …` est
équivalente à `.venv/bin/python -m trading_platform.cli …`. Les deux formes sont
utilisées indifféremment dans ce document.

---

## 3. Anatomie de `config/backtest_default.json`

Le fichier de configuration du moteur est un JSON unique, validé par
`trading_platform.config.AppConfig` (pydantic). **Toute clé inconnue est une
erreur** (`extra="forbid"`) ; toute clé absente prend sa valeur par défaut. Les
valeurs ci-dessous sont celles du modèle `AppConfig` et du fichier livré
`config/backtest_default.json`.

Une clé peut aussi être surchargée par variable d'environnement, préfixe `TB_`
et délimiteur `__` :

```bash
TB_DATA__TIMEFRAME=4h TB_BACKTEST__INITIAL_BALANCE=2500 \
    python -m trading_platform.cli backtest --config config/backtest_default.json
```

### 3.1 Racine

| Clé | Défaut | Sens |
| --- | --- | --- |
| `project_name` | `"trading-platform"` | nom affiché dans les rapports et les logs |
| `log_level` | `"INFO"` | `DEBUG`, `INFO`, `WARNING` ou `ERROR` |

### 3.2 `exchange` — marché et microstructure

| Clé | Défaut | Sens |
| --- | --- | --- |
| `name` | `"binance"` | identifiant d'exchange utilisé par `data download` |
| `market` | `"spot"` | `spot` ou `futures` |
| `quote_currency` | `"USDT"` | devise de cotation / du capital |
| `fee_rate` | `0.001` | frais par jambe (10 bps) |
| `slippage` | `0.0` | slippage par jambe (0 bps par défaut) |
| `rate_limit_ms` | `200` | pause entre deux appels API lors d'un téléchargement |

### 3.3 `data` — source et qualité des données

| Clé | Défaut | Sens |
| --- | --- | --- |
| `data_dir` | `"data"` | répertoire racine des données |
| `cache_dir` | `"data/cache"` | répertoire du cache disque (`OHLCVCache`) |
| `format` | `"parquet"` | format de cache : `parquet` ou `csv` |
| `timeframe` | `"1h"` | granularité (`1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`) |
| `start` | `null` | début de l'historique demandé (UTC) ; `null` = pas de borne |
| `end` | `null` | fin de l'historique demandé (UTC) ; `null` = pas de borne |
| `allow_network` | `true` | lu par `OHLCVLoader` : `false` y interdit tout accès réseau ; la CLI exige **les deux** feux verts (`allow_network: true` *et* absence de `--no-network`) pour télécharger |
| `validate` | `true` | fait échouer le run quand `validate_ohlcv` détecte un problème (NaN, trous, doublons…) ; `ensure_ohlcv` normalise de toute façon chaque frame chargée |
| `max_gap_factor` | `3.0` | tolérance de trou, en multiples du timeframe |
| `max_missing_ratio` | `0.0` | proportion de bougies manquantes tolérée |

### 3.4 `strategy` — stratégie et paramètres

| Clé | Défaut | Sens |
| --- | --- | --- |
| `name` | `"basic"` | nom de la stratégie résolue par `get_strategy` |
| `timeframe` | `"1h"` | timeframe de référence de la stratégie |
| `params` | `{}` | surcharges de paramètres ; vide = défauts de la stratégie |

Les paramètres de `basic` (valeurs par défaut définies **dans la stratégie**,
surchargeables par `strategy.params`) sont :

| Paramètre | Défaut | Sens |
| --- | --- | --- |
| `ema_fast` | `9` | période de l'EMA rapide |
| `ema_slow` | `21` | période de l'EMA lente (doit être > `ema_fast`) |
| `rsi_period` | `14` | période du RSI |
| `rsi_min` / `rsi_max` | `30.0` / `70.0` | bornes du filtre RSI (`rsi_min < rsi_max`) |
| `atr_period` | `14` | période de l'ATR |
| `atr_stop_multiplier` | `2.0` | distance du stop-loss en multiples d'ATR (colonne `stop_loss` = `close − atr_stop_multiplier × ATR`) |
| `allow_short` | `false` | autorise les signaux *short* de la stratégie |

Logique de `basic` : entrée *long* quand l'EMA rapide croise au-dessus de l'EMA
lente **et** que le RSI est strictement dans `]rsi_min, rsi_max[` ; sortie sur
croisement inverse ou sur stop ATR. `strategy.params` est aussi la base du
balayage paramétrique (`validation.robustness_grid`), et une grille vide laisse
la CLI utiliser `BasicStrategy.PARAM_SPACE`.

### 3.5 `backtest` — exécution

| Clé | Défaut | Sens |
| --- | --- | --- |
| `initial_balance` | `10000.0` | capital de départ |
| `stake_amount` | `null` | montant engagé par trade ; `null` = capital disponible |
| `max_open_trades` | `1` | positions simultanées (le moteur n'en tient qu'une) |
| `fee_rate` | `0.001` | frais par jambe (10 bps) |
| `slippage` | `0.0` | slippage par jambe (0 bps par défaut) |
| `allow_short` | `false` | autorise les ventes à découvert (désactivé par défaut) |
| `compute_metrics` | `true` | calcule les métriques à la fin du run |

### 3.6 `validation` — protocole statistique

| Clé | Défaut | Sens |
| --- | --- | --- |
| `n_windows` | `5` | nombre de fenêtres walk-forward |
| `in_sample_ratio` | `0.7` | part in-sample du split simple |
| `mode` | `"rolling"` | `rolling` (fenêtre glissante) ou `anchored` (ancrée) |
| `purge_candles` | `0` | bougies retirées à la frontière IS/OOS (0 = aucune ; **≥ 1 recommandé**) |
| `n_monte_carlo` | `1000` | nombre de simulations Monte Carlo |
| `monte_carlo_method` | `"trade_resample"` | `trade_resample` ou `bootstrap_equity` |
| `random_seed` | `42` | graine explicite (déterminisme) |
| `robustness_metric` | `"sharpe_ratio"` | métrique cible du balayage paramétrique |
| `robustness_max_combinations` | `512` | garde-fou : nombre maximal de combinaisons de la grille |
| `robustness_grid` | `{}` | grille à balayer, ex. `{"ema_fast": [8, 12, 16]}` |

### 3.7 `reporting` — restitution

| Clé | Défaut | Sens |
| --- | --- | --- |
| `output_dir` | `"reports"` | répertoire d'écriture des rapports |
| `basename` | `"report"` | nom de base des fichiers produits |
| `formats` | `["markdown", "json"]` | formats produits par `write_report` |
| `title` | `"Backtest Report"` | titre affiché dans le rapport markdown |
| `include_trades` | `true` | inclut le tableau des trades |
| `trade_limit` | `50` | nombre maximal de trades listés |

### 3.8 Exemple complet

```json
{
  "project_name": "trading-platform",
  "log_level": "INFO",
  "exchange": {
    "name": "binance",
    "market": "spot",
    "quote_currency": "USDT",
    "fee_rate": 0.001,
    "slippage": 0.0,
    "rate_limit_ms": 200
  },
  "data": {
    "data_dir": "data",
    "cache_dir": "data/cache",
    "format": "parquet",
    "timeframe": "1h",
    "start": null,
    "end": null,
    "allow_network": true,
    "validate": true,
    "max_gap_factor": 3.0,
    "max_missing_ratio": 0.0
  },
  "strategy": {
    "name": "basic",
    "timeframe": "1h",
    "params": {}
  },
  "backtest": {
    "initial_balance": 10000.0,
    "stake_amount": null,
    "max_open_trades": 1,
    "fee_rate": 0.001,
    "slippage": 0.0,
    "allow_short": false,
    "compute_metrics": true
  },
  "benchmark": {
    "enabled": true,
    "variant": "buy_and_hold",
    "risk_free_rate": 0.0,
    "n_random_simulations": 1000,
    "random_entry_seed": 42
  },
  "validation": {
    "n_windows": 5,
    "in_sample_ratio": 0.7,
    "mode": "rolling",
    "purge_candles": 0,
    "n_monte_carlo": 1000,
    "monte_carlo_method": "trade_resample",
    "random_seed": 42,
    "robustness_metric": "sharpe_ratio",
    "robustness_max_combinations": 512,
    "robustness_grid": {}
  },
  "reporting": {
    "output_dir": "reports",
    "basename": "report",
    "formats": ["markdown", "json"],
    "title": "Backtest Report",
    "include_trades": true,
    "trade_limit": 50
  }
}
```

Toute clé absente de ce fichier reprend la valeur par défaut du tableau
correspondant : un fichier minimal `{"data": {"timeframe": "4h"}}` est valide.

### 3.9 `benchmark` — la référence à battre

| Clé | Défaut | Sens |
| --- | --- | --- |
| `enabled` | `true` | calcule et expose le benchmark (même fenêtre, même capital, mêmes frais que le run) ; `false` supprime tout le calcul |
| `variant` | `"buy_and_hold"` | variante comparée : `buy_and_hold`, `cash`, `risk_free`, `random_entry` ou `none` |
| `risk_free_rate` | `0.0` | **taux sans risque annuel, en fraction** (`0.05` = 5 %/an), `>= 0`. Sert à la fois au calcul des `sharpe_ratio`/`sortino_ratio` (stratégie **et** benchmark) et à la variante `risk_free`. Défaut `0.0` = comportement historique ; `0.05` est la valeur recommandée pour un run 2023-2025 (T-bills US) |
| `n_random_simulations` | `1000` | nombre de simulations d'entrées aléatoires derrière la distribution `random_entry` (bornes `1 … 10000`) |
| `random_entry_seed` | `42` | graine des simulations `random_entry` : même graine ⇒ même distribution, même percentile |

Sens des cinq valeurs de `variant` :

- `buy_and_hold` (**défaut**) — achat de l'actif sur la clôture de la première
  bougie de la fenêtre, conservation jusqu'à la dernière, avec le modèle de frais
  et de slippage du moteur : c'est la référence à battre ;
- `cash` — capital laissé en cash : rendement `0.0`, aucune exposition. C'est la
  référence « ne rien faire du tout », et l'un des deux cas où la volatilité du
  benchmark est nulle (beta et corrélation deviennent alors « non calculables ») ;
- `risk_free` — **cash rémunéré** : le capital est placé au taux
  `risk_free_rate` composé bougie par bougie sur la même fenêtre
  (`equity[i] = balance × (1 + risk_free_rate / periods_per_year(timeframe)) ** i`).
  C'est le plancher économique honnête : ce que le capital rapportait sans aucun
  risque. Différence avec `cash` : `cash` **ignore** le taux (courbe plate,
  `total_return = 0.0`), `risk_free` l'encaisse ; à `risk_free_rate = 0.0` les
  deux sont **bit-identiques**. Aucune des deux ne paie de frais ni de slippage :
  il n'y a aucune transaction à exécuter ;
- `random_entry` — **une distribution, pas une courbe** : `n_random_simulations`
  stratégies à entrées aléatoires (même nombre de trades, durée de détention
  dérivée de l'exposition réalisée, tout-en-un, frais et slippage sur les deux
  jambes, graine `random_entry_seed`). Le rapport situe la stratégie réelle dans
  cette distribution (`percentile`, p-value empirique `p_value`,
  `strategy_beats_random` avec `MIN_P_VALUE = 0.05`) ; comme la référence n'est
  pas une courbe, il n'y a **pas** de `gap` métrique par métrique ni de beta pour
  cette variante. C'est le test « compétence ou chance » de la méthodologie
  (§11.8 de [`docs/backtesting-methodology.md`](backtesting-methodology.md)) ;
- `none` — **désactive le benchmark même quand `enabled` vaut `true`** : rien
  n'est quantifié, aucune section `Benchmark` n'est écrite dans le rapport et
  `run.benchmark` est absent du payload JSON. `enabled: false` et
  `variant: "none"` ont donc le même effet pratique ; le second sert à garder la
  variante choisie sous la main.

Les clés se surchargent par l'environnement — `TB_BENCHMARK__ENABLED`,
`TB_BENCHMARK__VARIANT`, `TB_BENCHMARK__RISK_FREE_RATE`,
`TB_BENCHMARK__N_RANDOM_SIMULATIONS`, `TB_BENCHMARK__RANDOM_ENTRY_SEED` — ou,
pour un run ponctuel, par les drapeaux `--benchmark/--no-benchmark`,
`--benchmark-variant` et `--risk-free-rate` de la CLI (§4). Méthodologie
complète, lecture de l'alpha, taux sans risque et biais haussier :
[`docs/backtesting-methodology.md`](backtesting-methodology.md).

### 3.10 `forecast` — the pre-computed forecast artifact

The `forecast` section wires the offline prediction layer into the strategy. It
is **optional**: with no `forecast.artifact`, the `timesfm` strategy produces no
signal at all (it never guesses and never raises), and the `basic` strategy is
unaffected.

| Key | Default | Meaning |
| --- | --- | --- |
| `artifact` | `null` | path of the parquet artifact produced by `trading forecast-build` (plus its `<artifact>.meta.json` sidecar). `null` = no forecast data available |

```json
{
  "strategy": {"name": "timesfm"},
  "forecast": {"artifact": "data/forecast/forecast.parquet"}
}
```

A **realtime profile** declares the same artifact through its own `forecast` key
(§11.3), and there it is *required* by a `timesfm` profile: the profile is
refused at startup, with an actionable message, instead of running without ever
trading ([`docs/realtime.md`](realtime.md) §3.1).

Every other forecast setting lives in the strategy parameters
(`strategy.params`, §4.7). The artifact itself, its schema, the backends, the
licence table and the measured library traps are documented in
`docs/forecasting.md` (page retired: the forecast subsystem was removed).

---

## 4. Exemples CLI

Toutes les commandes acceptent `--help` et `--json` (un unique objet JSON sur
stdout, au lieu du résumé humain). Six options structurent tous les exemples :

- `--config CHEMIN` (`-c`) — le fichier JSON de configuration (**obligatoire**
  pour toutes les commandes) ;
- `--data-file CHEMIN` — un CSV local, qui **court-circuite tout accès réseau**
  et tout cache : c'est le mode à utiliser pour vérifier une installation ou
  reproduire un résultat ;
- `--no-network` — interdit tout téléchargement : un défaut de cache devient une
  erreur dure (`InsufficientDataError`). `monte-carlo` ne l'expose pas.
- `--benchmark/--no-benchmark` — force (`--benchmark`) ou coupe
  (`--no-benchmark`) le benchmark **pour ce run**, sans modifier le fichier de
  configuration.
- `--benchmark-variant VARIANTE` — choisit la référence du run :
  `buy_and_hold` (défaut), `cash`, `risk_free`, `random_entry` ou `none` ;
  surcharge `benchmark.variant` pour ce run seulement.
- `--risk-free-rate FRACTION` — fixe le taux sans risque **annuel** du run, en
  fraction (`0.05` = 5 %/an) ; il alimente les `sharpe_ratio`/`sortino_ratio` de
  la stratégie et du benchmark, ainsi que la variante `risk_free` (§3.9).

Ces trois derniers drapeaux sont acceptés par `backtest`, `walk-forward`,
`robustness` et `monte-carlo` ; omis, ils laissent la main à `benchmark.enabled`,
`benchmark.variant` et `benchmark.risk_free_rate` de la configuration (§3.9), et
`--no-benchmark` retire à la fois la section `Benchmark` du rapport et
`run.benchmark` du payload JSON. Une ligne suffit pour chacun des nouveaux :

```bash
--risk-free-rate 0.05            # taux sans risque annuel (T-bills US 2023-2025)
--benchmark-variant risk_free    # référence : placement sans risque
```

Deux exemples d'un run ponctuel, sans toucher au fichier de configuration :

```bash
# benchmark au taux sans risque, taux réaliste 2023-2025 (T-bills US)
python -m trading_platform.cli backtest \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --benchmark-variant risk_free --risk-free-rate 0.05

# test de compétence : entrées aléatoires reproductibles (graine du config)
python -m trading_platform.cli backtest \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --benchmark-variant random_entry --risk-free-rate 0.05
```

Les autres réglages ont chacun une option dédiée plutôt qu'une surcharge `TB_` :
`--symbol`, `--timeframe`, `--start`, `--end`, `--output-dir`, `--formats`
(formats de rapport séparés par des virgules, p. ex. `markdown`), puis, selon la
commande, `--windows`, `--is-ratio`, `--mode`, `--metric`,
`--max-combinations`, `--simulations`, `--method`, `--seed`. Le fichier JSON et
les variables `TB_` restent la référence pour tout ce qui n'est pas exposé — et
les deux se combinent.

Sans `--data-file`, la fenêtre doit être fournie : `data.start` / `data.end`
dans le JSON, ou `--start` / `--end` — sinon la commande échoue avec un
`ConfigError` (« data.start and data.end are required when no --data-file is
given »). `robustness` n'a pas d'option `--start`/`--end` et `monte-carlo` non
plus : ces deux commandes travaillent sur tout le fichier fourni.

Le CSV doit respecter le contrat OHLCV (voir
[`docs/architecture.md`](architecture.md#5-contrat-de-données-ohlcv)) : colonne
`timestamp` en UTC, colonnes `open`, `high`, `low`, `close`, `volume`. Pour en
fabriquer un **hors ligne et déterministe** :

```bash
.venv/bin/python -c "from trading_platform.data.synthetic import make_ohlcv; \
make_ohlcv(8760, start='2022-01-01T00:00:00Z', timeframe='1h', seed=42).to_csv('btc.csv')"
```

### 4.1 `backtest` — un run unique

```bash
# CSV local : aucun réseau, aucun cache
python -m trading_platform.cli backtest \
    --config config/backtest_default.json \
    --data-file btc.csv --no-network

# symbole, timeframe et sous-fenêtre explicites
python -m trading_platform.cli backtest \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --symbol BTC/USDT --timeframe 1h \
    --start 2023-01-02T00:00:00Z --end 2023-01-06T00:00:00Z

# sans --data-file : cache d'abord, puis téléchargement (extra exchange requis)
python -m trading_platform.cli backtest \
    --config config/backtest_default.json \
    --start 2023-01-01T00:00:00Z --end 2024-01-01T00:00:00Z

# sortie machine : un objet JSON sur stdout, et un rapport markdown seulement
python -m trading_platform.cli backtest \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --formats markdown --output-dir reports/runs --json

# changer de timeframe ne demande aucune option : la config suffit
TB_DATA__TIMEFRAME=4h TB_STRATEGY__TIMEFRAME=4h \
    python -m trading_platform.cli backtest \
    --config config/backtest_default.json --data-file btc.csv

# comparer explicitement le run au buy & hold de la même fenêtre
python -m trading_platform.cli backtest \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --benchmark

# désactiver le benchmark : plus de section « Benchmark », plus de run.benchmark
python -m trading_platform.cli backtest \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --no-benchmark
```

Affiche le tableau des métriques, puis `strategy_name`, `initial_balance`,
`final_balance`, `n_trades`, la qualité des données (`data: <n> rows (ok)`) et
les chemins des rapports écrits. `--json` remplace ce résumé par un objet dont
les clés sont `command`, `ok`, `symbol`, `timeframe`, `config_path`, `metrics`,
`run`, `reports` et `data_quality`.

### 4.2 `walk-forward` — la validation temporelle

```bash
python -m trading_platform.cli walk-forward \
    --config config/backtest_default.json \
    --data-file btc.csv --no-network

# 8 fenêtres ancrées, 75 % in-sample, scorées sur le rendement total
python -m trading_platform.cli walk-forward \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --windows 8 --is-ratio 0.75 --mode anchored --metric total_return

# les mêmes réglages par variables d'environnement
TB_VALIDATION__MODE=anchored TB_VALIDATION__N_WINDOWS=8 \
    python -m trading_platform.cli walk-forward \
    --config config/backtest_default.json --data-file btc.csv
```

`--mode` accepte `rolling` ou `anchored`, `--is-ratio` doit être strictement
compris entre 0 et 1, `--metric` est n'importe quel nom de `METRIC_NAMES`
(défaut : `validation.robustness_metric`).

Affiche `mode`, `n_windows`, `aggregate_is_metric`, `aggregate_oos_metric`,
`efficiency` (le WFE) et `is_consistent`. Grille de lecture :
[`docs/backtesting-methodology.md`](backtesting-methodology.md#4-lecture-du-walk-forward).

### 4.3 `robustness` — balayage paramétrique

```bash
python -m trading_platform.cli robustness \
    --config config/backtest_default.json \
    --data-file btc.csv --no-network

# métrique cible et garde-fou de grille explicites
python -m trading_platform.cli robustness \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --metric total_return --max-combinations 81
```

Balaye `validation.robustness_grid` — renseignez-la dans le JSON (la valeur par
défaut est une grille vide), par exemple :

```json
"validation": { "robustness_grid": { "ema_fast": [5, 9, 13], "atr_stop_multiplier": [1.5, 2.0, 3.0] } }
```

Une grille vide n'est **pas** une erreur : la CLI retombe alors sur
`strategy.PARAM_SPACE` de la stratégie (`basic` : `ema_fast`, `ema_slow`,
`rsi_max`, `atr_stop_multiplier`), tronquée à `robustness_max_combinations`.

Produit `stability`, `positive_ratio`, `robust_ratio`, `worst_case_return`,
`best_params` et le verdict `is_robust` (`positive_ratio >= 0.7` **et**
`robust_ratio >= 0.5`). Le résumé humain affiche `metric_name`, `n_points`,
`metric_mean`, `stability`, `positive_ratio`, `robust_ratio` et `is_robust` ;
le détail de la grille (`points`) est dans `--json` et dans le rapport.

### 4.4 `monte-carlo` — distribution des résultats

```bash
python -m trading_platform.cli monte-carlo \
    --config config/backtest_default.json \
    --data-file btc.csv

# variante : bootstrap de la courbe d'equity, 500 tirages, graine fixée
python -m trading_platform.cli monte-carlo \
    --config config/backtest_default.json --data-file btc.csv \
    --simulations 500 --method bootstrap_equity --seed 7

# les mêmes réglages par variables d'environnement
TB_VALIDATION__MONTE_CARLO_METHOD=bootstrap_equity \
TB_VALIDATION__N_MONTE_CARLO=5000 \
TB_VALIDATION__RANDOM_SEED=7 \
    python -m trading_platform.cli monte-carlo \
    --config config/backtest_default.json --data-file btc.csv
```

Les trois options recouvrent `validation.n_monte_carlo`,
`validation.monte_carlo_method` et `validation.random_seed`. Le résumé humain
affiche `method`, `n_simulations`, `mean_return`, `median_return`,
`prob_profit`, `var_95` et `cvar_95` ; `--json` (et le rapport) ajoutent les
percentiles `p05`…`p95`, `std_return`, `worst_case_balance` et
`best_case_balance` — et, tronquées à 1 000 valeurs, les listes
`final_balances`, `returns` et `max_drawdowns`.

Cette commande n'expose pas `--no-network` : sans `--data-file`, elle passe par
le cache puis, si nécessaire, par le réseau.

### 4.5 `data download` — alimenter le cache

Cette commande est la **seule** qui a besoin du réseau, donc de l'extra
`exchange`. Les quatre options de fenêtre sont **obligatoires** :

```bash
python -m trading_platform.cli data download \
    --config config/backtest_default.json \
    --symbol BTC/USDT --timeframe 1h \
    --start 2023-01-01T00:00:00Z --end 2024-01-01T00:00:00Z
```

Affiche `rows`, `start`, `end` et `cache_path`, par exemple
`data/cache/binance/BTC_USDT/1h.parquet` (le chemin est
`<data.cache_dir>/<exchange>/<SYMBOL>/<timeframe>.<ext>`).

Le cache écrit est ensuite réutilisé automatiquement par `backtest` lorsqu'aucun
`--data-file` n'est fourni. Pour travailler **entièrement hors ligne**, fabriquez
le CSV avec `data.synthetic` (voir l'introduction du §4) et passez-le via
`--data-file` : c'est le mode utilisé par la suite de tests
([`docs/testing-policy.md`](testing-policy.md)).

### 4.6 `config show` / `config validate`

```bash
# configuration effective, valeurs par défaut appliquées
python -m trading_platform.cli config show --config config/backtest_default.json

# le même contenu en JSON
python -m trading_platform.cli config show \
    --config config/backtest_default.json --json

# validation seule (code de sortie non nul si le fichier est invalide)
python -m trading_platform.cli config validate --config config/backtest_default.json
```

`config validate` est le premier réflexe en cas de doute : il vérifie les clés
inconnues, les types, les timeframes supportés et la cohérence
`start < end`. Le type du fichier est détecté automatiquement :
`config validate --config config/freqtrade_dryrun.json` valide la configuration
Freqtrade et rend `{"kind": "freqtrade", "valid": true, "issues": []}` (un
`AppConfig` rend `{"kind": "appconfig", ...}`).

### 4.7 `forecast-build` / `forecast-bootstrap` / `forecast-skill` / `forecast-info`

These four commands build, measure and inspect the **offline** forecast artifact
consumed by the `timesfm` strategy. None of them runs inside
`prepare()`/`signals()`: prediction is pre-computed here, once, and the strategy
reads the result as a deterministic external input
(`docs/forecasting.md` (page retired: the forecast subsystem was removed)).

```bash
# build the artifact offline: random-walk baseline, no ML dependency, no network
python -m trading_platform.cli forecast-build \
    --config config/backtest_default.json \
    --data-file data/BTC_USDT-1h.csv \
    --out data/forecast/forecast.parquet \
    --backend naive

# same command with the real model (needs the timesfm extra)
python -m trading_platform.cli forecast-build \
    --config config/backtest_default.json --data-file data/BTC_USDT-1h.csv \
    --out data/forecast/forecast_timesfm.parquet --backend timesfm \
    --context 1024 --horizon 24 --reforecast-every 8

# any symbol, any supported timeframe: both are recorded in the artifact metadata
python -m trading_platform.cli forecast-build \
    --config config/backtest_default.json --data-file data/ETH_USDT-4h.csv \
    --out data/forecast/eth-4h.parquet --backend seasonal \
    --symbol ETH/USDT --timeframe 4h

# build the artifact a PROFILE declares, from its own candle file (offline only)
python -m trading_platform.cli forecast-bootstrap \
    --state-db data/realtime/state.db --backend seasonal

# measure the artifact's forecast skill against the random walk
python -m trading_platform.cli forecast-skill \
    --artifact data/forecast/forecast.parquet --data-file data/BTC_USDT-1h.csv

# inspect the metadata (backend, model, span, schema version, licence note)
python -m trading_platform.cli forecast-info \
    --artifact data/forecast/forecast.parquet --json

# ... and ask whether it is STILL USABLE right now, against the profile it feeds
python -m trading_platform.cli forecast-info \
    --artifact data/forecast/btc-timesfm-paper-1h-seasonal.parquet \
    --state-db data/realtime/state.db --profile btc-timesfm-paper
```

| Command | Options | Output |
| --- | --- | --- |
| `trading forecast-build` | `--config/-c` (required), `--data-file`, `--out`, `--backend naive\|seasonal\|timesfm`, `--context N`, `--horizon H`, `--reforecast-every S`, `--seasonal-period N`, `--seasonal-window N`, `--model-id`, `--symbol`, `--timeframe` | writes the parquet artifact and its `<artifact>.meta.json` sidecar; prints the path, the number of origins and the covered window |
| `trading forecast-bootstrap` | `--state-db` (required), `--profile` (default: the first profile of the database), `--config/-c`, `--backend naive\|seasonal` (default `seasonal`) | builds what the profile declares from the candle file its `symbol`/`timeframe` imply, writes it under `data/forecast/<profile-id>-<timeframe>-<backend>.parquet`, and prints the same `coverage` block as `forecast-info` |
| `trading forecast-skill` | `--artifact`, `--data-file` | RMSE / MAE / MASE against the random-walk baseline, decile coverage and directional accuracy at the horizon, on the modelled target **and** on the real price |
| `trading forecast-info` | `--artifact`, `--now ISO-8601`, `--state-db`, `--profile`, `--timeframe`, `--horizon` | metadata of the artifact (no candle file needed) plus `run['coverage']`: `first_origin`, `last_origin`, `usable_until`, `usable`, `seasonal_period`, `checked_at` |

Each origin uses **only candles `<= origin`**, `--context` is the number of
candles fed to the backend, `--horizon` the number of stored steps and
`--reforecast-every` the stride between two stored origins. All four commands
accept `--json`. **Do not read a positive PnL as an edge**: read
`forecast-skill` first — `rmse_skill_score <= 0`, `mase >= 1` or a directional
accuracy near `0.5` mean the artifact carries no usable skill
(`docs/forecasting.md` (page retired: the forecast subsystem was removed) §9).

**The seasonal period follows the timeframe.** `--seasonal-period` defaults to
the number of candles of one day at `--timeframe` — `1440` on `1m`, `288` on
`5m`, `96` on `15m`, `48` on `30m`, `24` on `1h`, `6` on `4h`, `1` on `1d` (and
longer) — instead of the historical hard-coded `24`, which was only correct for
hourly candles. An explicit `--seasonal-period N` overrides the derivation, and
the resolved value is recorded in the artifact metadata.

**Is this artifact still usable right now?** `forecast-info` answers it through
`run['coverage']`. Without `--state-db` the command only **reports** and never
fails on staleness. Pointed at a real profile of the state database
(`--state-db <db> [--profile <id>]`) it becomes a pre-flight of that profile:
it reuses the realtime startup guard verbatim, so a symbol, timeframe or coverage
failure raises the **same** message the engine raises at startup, and `coverage`
gains `profile_id`, `symbol_ok` and `timeframe_ok`. `--now` makes the answer
deterministic — useful in a script or a test — and `--timeframe` / `--horizon`
override what the coverage is measured on. The guard itself is documented in
[`docs/realtime.md`](realtime.md) §3.1.

---

## 5. Lire un rapport

`write_report` écrit un fichier par format demandé dans `reporting.output_dir`
(par défaut `reports/`), sous `reporting.basename` :

```
reports/
├── report.md      # rapport lisible (formats: "markdown")
└── report.json    # payload machine (formats: "json")
```

`reporting.basename` permet de nommer chaque run différemment (par exemple
`basic_BTCUSDT_1h`) et d'archiver plusieurs campagnes côte à côte.

### 5.1 Rapport markdown

Le document est un objet `Report` rendu par `Report.to_markdown()` :

1. **Titre et date de génération** — `reporting.title` et
   `generated_at` (UTC).
2. **`## Summary`** — un tableau `| Metric | Value |` qui mélange l'identité du
   run (`symbol`, `timeframe`, `strategy_name`, `start`, `end`,
   `initial_balance`, `final_balance`, `n_trades`) et les 23 métriques ; les
   lignes sont triées **par ordre alphabétique** (pas dans l'ordre de
   `METRIC_NAMES`).
3. **`## Metadata`** — l'écho de la configuration effective du run
   (`AppConfig.model_dump(mode="json")`), c'est-à-dire les valeurs *réellement*
   utilisées, pas seulement celles du fichier.
4. **Une section par payload de validation** — `data_quality` toujours,
   `walk_forward` / `robustness` / `monte_carlo` lorsqu'ils ont été exécutés,
   avec leurs verdicts.
5. **`## Trades`** — tableau des trades clos (`direction`, `entry_time`,
   `exit_time`, `entry_price`, `exit_price`, `size`, `pnl`, `pnl_pct`,
   `exit_reason`, `duration_minutes`), limité à `reporting.trade_limit` lignes.
6. **`## Equity curve`** — `timestamps` et `values`, sous-échantillonnés à
   200 points.
7. **`## Benchmark`** — présent dès que le benchmark est actif (§3.9) : la
   comparaison stratégie vs référence, avec l'écart métrique par métrique.
8. **La section `random_entry`** — uniquement quand
   `benchmark.variant: "random_entry"` : la distribution des entrées aléatoires
   (moyenne, médiane, écart-type, percentiles) et la position de la stratégie
   réelle dedans — `percentile`, `p_value` et le verdict
   `strategy_beats_random`.

La section **`Benchmark`** est un tableau à **trois lignes** pour les variantes à
courbe : `strategy` (les métriques du run), la ligne du benchmark — dont le
**libellé suit la variante choisie**, `buy_and_hold` (défaut), `cash` ou
`risk_free` — et `gap = strategy − benchmark` pour **chaque** métrique. Le `gap`
le plus important est `alpha`, l'écart des `total_return` : il est exprimé en
**fraction**, donc `0.12` signifie 12 points de pourcentage d'avance sur le fait
de ne rien faire. Avec `random_entry` il n'y a **pas de courbe** à comparer ligne
à ligne : la référence est une distribution, et la section rapporte la
distribution plus la position de la stratégie dedans (`percentile`, `p_value`,
`strategy_beats_random`). L'équivalent machine se trouve sous `run.benchmark`
(dans la sortie `--json` et dans `report.json`) avec `strategy_beats_benchmark`,
`alpha`, `beta` et `correlation` — ces deux derniers valant `null` (mention
« non calculable ») quand les rendements ne sont pas appariables en nombre
suffisant ou que la variance du benchmark est nulle —, complétés pour
`random_entry` par la distribution, son exposition simulée, le `percentile` et la
`p_value`. `--no-benchmark` (§4) supprime à la fois cette section et cette clé.

### 5.2 Payload JSON

`report.json` est le `Report.to_dict()` du même document, pas
`BacktestResult.to_dict()` : ses clés sont `title`, `generated_at`, `summary`,
`sections` et `metadata`. `summary` porte l'identité du run et les métriques
(donc les 23 métriques), `sections` porte les payloads de validation, les trades
et l'equity, `metadata` porte l'écho de la configuration effective. Le run brut
(`BacktestResult.to_dict()` et les payloads de validation non tronqués) est
disponible dans la sortie `--json` de la CLI, sous la clé `run`. Toutes les
valeurs sont JSON-natives (les timestamps sont des chaînes ISO-8601 UTC, les
séries des listes).

### 5.3 Ce qu'il faut regarder en premier

| À vérifier | Où | Seuil d'alerte |
| --- | --- | --- |
| Nombre de trades (`n_trades`) | métriques | < 30 trades : aucune conclusion statistique |
| Max drawdown (`max_drawdown`) | métriques | au-delà de la tolérance de risque fixée |
| Consistance walk-forward | section walk-forward | < 60 % de fenêtres OOS positives |
| WFE | section walk-forward | < 0.5 |
| VaR / CVaR 95 % | section Monte Carlo | queue plus profonde que le drawdown observé |
| Probabilité de profit | section Monte Carlo | < 60 % |
| Verdict vs benchmark (`strategy_beats_benchmark`) | section Benchmark | `false`, c'est-à-dire `alpha <= 0` : la stratégie ne bat pas le fait de ne rien faire |
| Verdict de compétence (`strategy_beats_random`, `p_value`) | section `random_entry` | `p_value >= 0.05` (percentile < 95) : la performance s'explique par la chance |
| Taux sans risque utilisé (`benchmark.risk_free_rate`) | section Metadata | `0.0` sur une fenêtre où le sans-risque rapportait 5 %/an : Sharpe et Sortino sont **optimistes** (voir §3.9 et la méthodologie, §11.7) |

Détail des seuils et de leur justification :
[`docs/backtesting-methodology.md`](backtesting-methodology.md).

---

## 6. Tests et couverture

La politique de tests et le seuil de couverture sont définis **une seule fois**
dans [`docs/testing-policy.md`](testing-policy.md) : commandes scopées, règle
des 85 %, règles de déterminisme et d'absence de réseau. Ce document ne la
duplique pas — il pointe vers elle.

Les deux commandes à connaître :

```bash
# la commande complète (celle que lance la CI) : tests + gate de couverture à 85 %
make test-cov

# un fichier de test isolé pendant le développement
.venv/bin/python -m pytest tests/test_docs.py -q
```

La CI ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)) exécute
`ruff check .`, `ruff format --check .`, `mypy src` puis la commande complète —
exactement les mêmes commandes que `make check` en local.

---

## 7. Tâches `make`

| Cible | Commande sous-jacente | Rôle |
| --- | --- | --- |
| `make help` | rappel des cibles (`make` seul l'affiche aussi) | aide (cible par défaut) |
| `make venv` | `python3.11 -m venv .venv` | création de l'environnement virtuel |
| `make install` | `pip install -e .` | installation runtime |
| `make install-dev` | `pip install -e ".[dev]"` | installation de développement |
| `make lint` | `ruff check .` + `ruff format --check .` | contrôle de style |
| `make format` | `ruff format .` + `ruff check --fix .` | formatage automatique puis corrections |
| `make type-check` | `mypy src` | vérification de types |
| `make test` | `pytest tests -q` | tests seuls, sans gate de couverture |
| `make test-cov` | la commande complète de `docs/testing-policy.md` §3 | tests + gate de couverture |
| `make cov` | la même commande avec `--cov-report=html` | tests + rapport HTML `htmlcov/` |
| `make check` | `lint` + `type-check` + `test-cov` | porte complète avant push |
| `make backtest` | `python -m trading_platform backtest --config $(CONFIG)` | backtest unique (`CONFIG=…` pour changer de fichier) |
| `make walk-forward` | `python -m trading_platform walk-forward --config $(CONFIG)` | walk-forward |
| `make robustness` | `python -m trading_platform robustness --config $(CONFIG)` | balayage paramétrique |
| `make monte-carlo` | `python -m trading_platform monte-carlo --config $(CONFIG)` | Monte Carlo |
| `make data-download` | `python -m trading_platform data download …` | remplissage du cache (seule cible qui utilise le réseau) |
| `make forecast-build` | `python -m trading_platform forecast-build --config $(CONFIG) --data-file $(DATA_FILE) --out $(FORECAST_ARTIFACT) --backend $(FORECAST_BACKEND)` | build the offline forecast artifact (`DATA_FILE`, `FORECAST_ARTIFACT`, `FORECAST_BACKEND` override the defaults) |
| `make forecast-bootstrap` | same command with `--out $(FORECAST_DIR)/bootstrap-naive.parquet --backend naive` | build the naive-baseline artifact used to bootstrap a forecast profile |
| `make forecast-bootstrap-seasonal` | same command with `--out $(FORECAST_DIR)/bootstrap-seasonal.parquet --backend seasonal` | build the seasonal-baseline artifact used to bootstrap a forecast profile |
| `make forecast-profile` | `python -m trading_platform forecast-bootstrap --state-db $${STATE_DB:-data/realtime/state.db} --config $(CONFIG) --backend $${BACKEND:-seasonal}` | build the artifact the profile **declares**, from its own candle file (`BACKEND=…`, `STATE_DB=…` override the defaults) |
| `make forecast-info` | `python -m trading_platform forecast-info --artifact $(FORECAST_ARTIFACT) --state-db $${STATE_DB:-data/realtime/state.db} --json` | report whether `$(FORECAST_ARTIFACT)` is still usable; `STATE_DB=…` checks it against a profile through the same startup guard |
| `make realtime` | `realtime run --state-db $${STATE_DB:-data/realtime/state.db}` | moteur temps réel + API JSON (§11) ; dashboard : `make dashboard-dev` (§11.6) |
| `make realtime-forecast` | `realtime run --state-db $${STATE_DB:-data/realtime/state.db}` | run the realtime engine on the forecast profile, which therefore really trades (§11.7) |
| `make forecast-flow` | `data-download forecast-bootstrap forecast-info realtime-forecast` | the documented three-command operational flow, end to end: download the candles, build the artifact, verify it, trade |
| `make docker-build` | `docker build` | construction de l'image |
| `make docker-test` | `docker build --target test` puis `docker run … pytest tests --cov-fail-under=85` | suite complète dans le conteneur |
| `make clean` | suppression des caches et artefacts | nettoyage |

Quatre cibles sont **garanties par le contrat du projet** et ne peuvent pas
disparaître : `make lint`, `make type-check`, `make test-cov` (celles que
[`docs/testing-policy.md`](testing-policy.md) §3 impose avant tout push) et
`make docker-test`. Le `Makefile` livré reste la référence pour le reste :
`make help` (ou `make -qp`) liste les cibles réellement disponibles.

Les cibles de pipeline (`backtest`, `walk-forward`, `robustness`, `monte-carlo`,
`data-download`) n'ajoutent aucune option : elles appellent la CLI avec
`--config $(CONFIG)` (`config/backtest_default.json` par défaut). Pour un run
hors ligne, appelez directement la CLI avec `--data-file … --no-network` (§4).

`make forecast-build` is the only pipeline target with extra variables: it calls
`forecast-build` with `DATA_FILE` (`data/BTC_USDT-1h.csv`), `FORECAST_ARTIFACT`
(`data/forecast/forecast.parquet`) and `FORECAST_BACKEND` (`naive`), all
overridable on the command line:

```bash
make forecast-build
FORECAST_BACKEND=timesfm FORECAST_ARTIFACT=data/forecast/tfm.parquet make forecast-build
```

The forecast **profile** targets carry their own variables —
`FORECAST_DIR` (`data/forecast`), `STATE_DB` (`data/realtime/state.db`, the
database whose profiles `forecast-info` checks against) and `SYMBOL`/`TIMEFRAME`
(`BTC/USDT`, `1h`, used by `data-download`) — and they are the operational flow
of §11.7:

```bash
make data-download SYMBOL=BTC/USDT TIMEFRAME=1h           # candles (only network step)
make forecast-profile STATE_DB=… BACKEND=seasonal         # the artifact the profile declares
make forecast-info STATE_DB=…                             # is it still usable right now?
make realtime-forecast                                    # the engine, on the forecast profile
make forecast-flow                                        # the four, in order
```

---

## 8. Docker

L'image fournit un environnement reproductible (Python 3.11 + les dépendances
déclarées dans `requirements*.txt`), utile pour rejouer un backtest à
l'identique sur une autre machine.

```bash
# construction (la dernière étape du Dockerfile est `test` : la suite tourne
# pendant le build ; `docker build --target base` ne construit que le runtime)
docker build -t trading-platform:latest .

# suite complète + gate de couverture dans le conteneur
make docker-test
```

**Choix assumé : Docker n'est pas exécuté dans la CI.** Le workflow
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) garde **un seul job**
`quality` (lint + type-check + tests + coverage) sur `ubuntu-latest` : c'est le
chemin de feedback le plus court, et il couvre 100 % des garanties du projet.
La reproductibilité conteneurisée est vérifiée localement avec
`make docker-test`, à la demande.

Pour rejouer un backtest dans le conteneur avec vos données : l'`ENTRYPOINT` de
l'image est `python -m trading_platform`, la commande se passe donc directement
en arguments.

```bash
docker run --rm -v "$PWD/reports:/app/reports" trading-platform:latest \
    backtest \
    --config config/backtest_default.json --data-file data/btc.csv
```

---

## 9. Configs Freqtrade et config de backtest

Le dépôt contient **deux familles de configuration**, qui ne servent pas à la
même chose :

| Fichier | Consommateur | Rôle |
| --- | --- | --- |
| `config/backtest_default.json` | `trading_platform.config.AppConfig` | configuration du **moteur de backtest** : données, stratégie, exécution, validation, rapports |
| `config/freqtrade_config.json` | Freqtrade | configuration d'un **bot** (exchange, `stake_currency`, `dry_run=false`, paires en liste blanche…) |
| `config/freqtrade_dryrun.json` | Freqtrade | même chose avec `dry_run=true` : paper trading sur flux réel |

Points clés :

- **Le moteur de backtest est indépendant** : il lit `AppConfig` et n'ouvre
  jamais un fichier Freqtrade. Un run de backtest ne nécessite donc pas
  l'installation de `freqtrade`.
- Les configs Freqtrade servent à **exécuter** la stratégie après validation
  (dry-run d'abord, puis live). Elles ne pilotent ni le walk-forward, ni la
  robustesse, ni le Monte Carlo.
- La cohérence entre les deux familles est **manuelle et volontaire** : mêmes
  paires, même timeframe, même `stake_amount`. Le squelette ne synchronise pas
  les fichiers automatiquement — une divergence est une erreur de configuration,
  pas un bug.

### 9.1 Exposer une stratégie à Freqtrade / dry-run

La stratégie s'écrit **une seule fois** dans `trading_platform.strategy` (voir
[`docs/architecture.md`](architecture.md#49-ladaptateur-freqtrade-écrire-une-stratégie-une-fois-lexposer-deux-fois)
pour le contrat de traduction). L'exposition à Freqtrade se fait ensuite sans
écrire de code métier.

**1. Installer l'extra `freqtrade`** (facultatif : le moteur de backtest n'en a
jamais besoin) :

```bash
.venv/bin/python -m pip install -e ".[freqtrade]"
```

**2. Convention de nom.** Le nom maison se traduit en nom Freqtrade :

| Nom maison (`strategy.name`) | Classe adaptateur | Nom Freqtrade (config `strategy:`) |
| --- | --- | --- |
| `basic` | `BasicFreqtradeStrategy` | `BasicStrategy` |

`config/freqtrade_config.json` (live) et `config/freqtrade_dryrun.json` (paper)
déclarent tous les deux `"strategy": "BasicStrategy"` : c'est le nom attendu par
Freqtrade, et il doit correspondre au nom de classe du fichier de shim.

**3. Le shim livré.** `user_data/strategies/BasicStrategy.py` est le **seul**
fichier suivi de `user_data/` : il ne contient aucune logique, seulement le
`make_freqtrade_strategy("basic")` et une déclaration de classe d'une ligne pour
donner à Freqtrade le nom qu'il attend. Le reste de `user_data/` est l'état
écrit par Freqtrade et reste git-ignoré.

**4. Lancer le bot.** Freqtrade cherche les stratégies dans
`user_data/strategies/` : le shim y est déjà, rien à copier.

```bash
# paper trading (dry-run sur flux réel) — commencer TOUJOURS par là
.venv/bin/python -m freqtrade trade \
    --config config/freqtrade_dryrun.json \
    --strategy BasicStrategy --userdir user_data

# live (capital réel) — après validation, taille réduite puis progressive
.venv/bin/python -m freqtrade trade \
    --config config/freqtrade_config.json \
    --strategy BasicStrategy --userdir user_data
```

Si le shim vit ailleurs (dépôt de stratégies séparé, `user_data/` non versionné
sur la machine cible), pointez le répertoire explicitement avec
`--strategy-path` :

```bash
.venv/bin/python -m freqtrade trade \
    --config config/freqtrade_dryrun.json \
    --strategy BasicStrategy \
    --strategy-path user_data/strategies --userdir user_data
```

Le backtest Freqtrade et le backtest maison **ne donnent pas les mêmes
chiffres** : les signaux sont partagés, pas l'exécution. L'écart est documenté
et attendu — voir le §4.9.4 de
[`docs/architecture.md`](architecture.md#494-lécart-de-modèle-dexécution-écrit-noir-sur-blanc).

Parcours recommandé :

```
backtest  →  robustness  →  walk-forward  →  monte-carlo
          →  freqtrade dry-run (config/freqtrade_dryrun.json)
          →  live (config/freqtrade_config.json), taille réduite puis progressive
```

---

## 10. Dépannage

| Symptôme | Cause probable | Action |
| --- | --- | --- |
| `ConfigError` au démarrage | clé inconnue ou mal typée dans le JSON | `python -m trading_platform.cli config validate --config …` |
| `DataValidationError` | colonne obligatoire absente, `NaN`, prix ou volume non positif, index non temporel, trous au-delà de `max_gap_factor` | corriger la source ; voir le contrat OHLCV dans [`docs/architecture.md`](architecture.md#5-contrat-de-données-ohlcv) (le tri, les doublons et un index naïf sont corrigés automatiquement par `ensure_ohlcv`) |
| `InsufficientDataError` | historique trop court pour la fenêtre demandée | réduire `validation.n_windows` / `validation.in_sample_ratio` ou allonger `data.start` |
| `DataDownloadError` | extra `exchange` absent ou API indisponible | `pip install -e ".[exchange]"` ou utiliser `--data-file` |
| `StrategyError` | nom de stratégie inconnu | vérifier `strategy.name` et les noms passés à `register_strategy` |
| Aucun trade | paramètres trop stricts (RSI, seuils) | élargir `rsi_min`/`rsi_max`, réduire `ema_fast`/`ema_slow` |
| Tests rouges sur la couverture | seuil de 85 % non atteint | voir [`docs/testing-policy.md`](testing-policy.md) §5 |

---

## 11. Temps réel multi-profils (`realtime`)

Le groupe `realtime` exécute **N profils concurrents** (`asset + stratégie +
timeframe + paper/live + limites de risque`) dans un seul processus, persiste
leur état dans un fichier SQLite et l'expose par une **API JSON** HTTP.
Le contrat complet (interfaces, API, limites assumées) est dans
[`docs/realtime.md`](realtime.md) ; cette section donne des exemples copiables.

### 11.1 Les trois commandes

```bash
# pré-vol statique : ne passe AUCUN ordre, ne touche PAS au réseau
python -m trading_platform realtime check --state-db data/realtime/state.db

# un seul tick déterministe (ancre realtime.start_at), puis sortie 0
python -m trading_platform realtime run --state-db data/realtime/state.db --once --json

# moteur + API JSON (port 0 = port éphémère choisi par l'OS)
python -m trading_platform realtime run --state-db data/realtime/state.db \
    --host 127.0.0.1 --port 8080

# surveillance seule, LECTURE SEULE, sur l'état déjà persisté
python -m trading_platform realtime serve --state-db data/realtime/state.db --port 8080
```

The profile set and the engine settings live in that SQLite state database, which
is the single source of truth: `--state-db` is the path of the database and the
only thing a command must know before the store exists. `--profiles` / `-p` is
kept as an **alias** of `--state-db` for scripts written against the previous
name. A legacy JSON profiles document is no longer read by anything, and the host
starts with an empty profile set — create the profiles from the dashboard
(`POST /api/profiles`).

| Commande | Options | Rôle |
| --- | --- | --- |
| `realtime run` | `--state-db/-p` (facultatif : `TB_REALTIME_STATE_DB`, sinon `data/realtime/state.db`), `--logs-dir`, `--host`, `--port`, `--once`, `--json` | moteur **et** serveur de surveillance ; `--once` exécute **un** tick déterministe, écrit l'état et sort (aucun serveur) |
| `realtime serve` | `--state-db/-p` (facultatif : `TB_REALTIME_STATE_DB`, sinon `data/realtime/state.db`), `--logs-dir`, `--host`, `--port`, `--json` | surveillance **lecture seule** sur l'état persisté, sans moteur : `POST /api/kill-switch` répond **403** |
| `realtime check` | `--state-db/-p` (facultatif : `TB_REALTIME_STATE_DB`, sinon `data/realtime/state.db`), `--logs-dir`, `--json` | pré-vol statique : validité de la base d'état et des profils qu'elle contient, **présence** des credentials (jamais leur valeur), porte live, limites de risque, inscriptibilité de la base d'état ; sortie `1` dès qu'un **un** profil ne peut pas démarrer ; ne crée **pas** la base d'état |

`SIGINT` arrête proprement le serveur et le moteur, puis la commande sort avec le
code `0`. En mode `--json`, l'URL de démarrage est annoncée sur **stderr** :
stdout ne contient qu'**un seul** objet JSON.

### 11.2 Payloads JSON (clés exactes)

`realtime check` :

```json
{
  "command": "realtime-check",
  "ok": true,
  "state_db": "data/realtime/state.db",
  "state_db_writable": true,
  "kill_switch": false,
  "profiles": [
    {
      "id": "btc-paper",
      "symbol": "BTC/USDT",
      "timeframe": "1h",
      "strategy": "basic",
      "mode": "paper",
      "ok": true,
      "issues": [],
      "credentials_present": false,
      "live_gate_allowed": true,
      "risk": {
        "max_position_notional": 5000.0,
        "max_order_notional": 1000.0,
        "max_open_positions": 1,
        "max_daily_loss": 500.0,
        "max_drawdown_pct": 0.25,
        "max_daily_trades": 10
      }
    }
  ],
  "issues": []
}
```

La clé **`issues` de premier niveau** porte les problèmes *de plateforme*
(base d'état inutilisable, répertoire d'état non inscriptible) ; les `issues` de
chaque profil portent les problèmes *du profil* (porte live non armée,
credentials absents, profil désactivé…).

`realtime run` et `realtime run --once` :

```json
{
  "command": "realtime-run",
  "ok": true,
  "state_db": "data/realtime/state.db",
  "profiles": [ "« ProfileSnapshot.to_dict() » pour chaque profil" ],
  "decisions": [ "« TradeSignalDecision.to_dict() » — vide en mode serveur, vide aussi si le tick n'a rien de neuf à traiter" ],
  "url": "http://127.0.0.1:8080/"
}
```

`url` vaut `null` avec `--once` (aucun serveur n'est démarré) et
`http://host:port/` sinon. `realtime serve` renvoie le même objet avec
`"command": "realtime-serve"` et **sans** clé `decisions`.

The configuration is a set of rows, not a file: the **profile set is the
`profiles` table** of the SQLite state database and the engine and monitoring
settings are the `meta` key `platform_settings` of the same database
([`docs/realtime.md`](realtime.md) §1.1). Neither carries a credential: the
secrets come only from the environment (§11.4).

Chaque entrée de `profiles` est un `ProfileConfig` (`extra="forbid"` : toute clé
inconnue, dont `api_key`, est refusée bruyamment) :

| Champ | Type | Rôle |
| --- | --- | --- |
| `id` | `str` | identité du profil, `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` : clé primaire de l'état |
| `symbol` | `str` | paire négociée, par exemple `BTC/USDT` |
| `timeframe` | `str` | `1m`, `5m`, `15m`, `30m`, `1h`, `4h` ou `1d` |
| `strategy` | `str` | nom du registre `strategy.registry.get_strategy` |
| `params` | `object` | paramètres de la stratégie (mêmes conventions que `AppConfig`) |
| `mode` | `"paper"` \| `"live"` | simulation ou lieu réel (§11.4) |
| `initial_balance` | `float > 0` | capital initial |
| `stake_amount` | `float > 0` \| `null` | montant engagé par entrée |
| `exchange` | `str` | nom du lieu d'exécution |
| `enabled` | `bool` | un profil désactivé est persisté mais jamais démarré |
| `warmup_candles` | `int >= 1` | bougies passées fournies à la stratégie à chaque décision |
| `poll_interval_seconds` | `float > 0` | cadence de sondage propre au profil |
| `risk` | `object` | bloc `RiskLimitsConfig` ci-dessous |
| `entry_lookback_candles` | `int` dans `[0, 200]` (défaut `0`) | fenêtre de rattrapage **live uniquement** : la décision d'entrée peut porter sur un croisement survenu dans les `N` dernières bougies ; `0` conserve le comportement historique (seule la dernière ligne décide) ; le backtest l'ignore, il lit déjà chaque ligne |
| `forecast` | `str \| null` (default `null`) | path of the offline forecast artifact consumed by a forecast-driven strategy (`trading forecast-build`); the **last field** of `ProfileConfig`, so the order of the existing fields does not move. `null` = no artifact, the historical behaviour of every `basic` profile. A `timesfm` profile requires it: at startup, an artifact that is missing, corrupt, stale or built for another symbol/timeframe **refuses to start** with an actionable message, instead of running without ever trading (§11.7) |

The `forecast` key is documented in depth in
[`docs/realtime.md`](realtime.md) §1 and §3.1 and in
`docs/forecasting.md` (page retired: the forecast subsystem was removed); `"forecast": null` is the default every
`basic` profile gets, and a `timesfm` profile declares its artifact path.

`risk` (`RiskLimitsConfig`) — toutes les limites sont optionnelles, `null`
signifie « non appliquée », et `0` est une valeur **valide** pour les limites de
comptage :

| Champ | Type | Rôle |
| --- | --- | --- |
| `max_position_notional` | `float > 0` \| `null` | notionnel maximal d'une position |
| `max_order_notional` | `float > 0` \| `null` | notionnel maximal d'un ordre |
| `max_open_positions` | `int >= 0` (défaut `1`) | nombre maximal de positions ouvertes (`0` interdit toute ouverture) |
| `max_daily_loss` | `float > 0` \| `null` | perte journalière maximale |
| `max_drawdown_pct` | `float` dans `]0, 1]` \| `null` | drawdown maximal (`0.25` = 25 %) |
| `max_daily_trades` | `int >= 0` \| `null` | nombre maximal de trades par jour |

`realtime` (`RealtimeConfig`) :

| Champ | Défaut | Rôle |
| --- | --- | --- |
| `state_db` | `data/realtime/state.db` | base SQLite de l'état (git-ignorée) |
| `logs_dir` | `data/realtime/logs` | journaux JSON structurés |
| `data_dir` | `data` | racine des données |
| `cache_dir` | `data/cache` | cache OHLCV utilisé par le provider réseau |
| `format` | `parquet` | format du cache (`parquet` ou `csv`) |
| `allow_network` | `true` | `false` interdit tout téléchargement |
| `csv_dir` | `null` | répertoire de CSV locaux : **court-circuite le réseau**, c'est le mode des tests et du replay |
| `start_at` | `null` | ancre de replay : arme un `ManualClock` (tick déterministe) |
| `history_candles` | `300` | taille de la fenêtre de sondage |
| `poll_interval_seconds` | `5.0` | cadence de sondage par défaut |
| `stream_poll_timeout_seconds` | `10.0` | borne explicite de **chaque** attente |
| `max_stream_reconnects` | `5` | reconnexions consécutives tolérées avant `MarketStreamError` |
| `reconnect_backoff_seconds` | `1.0` | base de l'attente exponentielle bornée |
| `reconcile_interval_seconds` | `60.0` | intervalle de réconciliation avec le lieu |
| `risk_free_rate` | `0.0` | taux sans risque annuel du benchmark du read model |
| `benchmark_variant` | `buy_and_hold` | variante de benchmark (`none` la désactive) |
| `kill_switch_file` | `null` | fichier drapeau du kill switch global |

Une entrée de `profiles` accepte aussi `entry_lookback_candles` (`int` dans
`[0, 200]`, défaut `0`) : la fenêtre de rattrapage **live uniquement** — `0`
conserve le comportement historique (seule la dernière bougie décide), `N > 0`
autorise l'entrée sur un croisement survenu dans les `N` dernières bougies. Le
backtest ignore ce champ, il lit déjà chaque ligne :

```json
{
  "profiles": [
    {
      "id": "btc-paper",
      "symbol": "BTC/USDT",
      "timeframe": "1h",
      "strategy": "basic",
      "mode": "paper",
      "initial_balance": 10000.0,
      "warmup_candles": 200,
      "poll_interval_seconds": 5.0,
      "risk": {},
      "entry_lookback_candles": 3
    }
  ]
}
```

`monitoring` (`MonitoringConfig`) :

| Champ | Défaut | Rôle |
| --- | --- | --- |
| `host` | `127.0.0.1` | interface d'écoute |
| `port` | `8080` | port (`0` = port éphémère choisi par l'OS) |
| `refresh_seconds` | `2.0` | cadence de sondage du tableau de bord |
| `request_timeout_seconds` | `10.0` | délai maximal d'une requête |
| `max_request_bytes` | `65536` | taille maximale d'un corps de requête |

Aucun de ces modèles ne porte de champ de credential, et `extra="forbid"`
s'applique partout : un fichier qui contient `api_key`, `api_secret`,
`password` ou `token` échoue avec un `ConfigError` explicite.

### 11.4 Le modèle de sûreté paper/live

```bash
# 1. les credentials viennent UNIQUEMENT de l'environnement (jamais du dépôt)
export TB_LIVE_API_KEY="…"           # portée globale
export TB_LIVE_API_SECRET="…"
export TB_LIVE_API_PASSWORD="…"      # seulement si le lieu en exige un
# … ou par profil (les variables de profil gagnent sur les globales) :
export TB_PROFILE_BTC_LIVE_API_KEY="…"
export TB_PROFILE_BTC_LIVE_API_SECRET="…"

# 2. armer explicitement le live : VALEUR EXACTE, sinon LiveTradingForbiddenError
export TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK

# 3. jeton de l'unique opérateur autorisé à muter l'état (POST /api/kill-switch)
export TB_OPERATOR_TOKEN="…"
```

Sans `TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK`, tout profil `mode: "live"`
est refusé (`LiveTradingForbiddenError`) et `realtime check` le signale avec
`live_gate_allowed: false`. Un profil `paper` n'a jamais besoin d'être armé, et
il ne peut **jamais** être routé vers un courtier réel : le mode fait partie de
l'identité du profil et de chaque ordre persisté.

Le **kill switch global** existe sous trois formes, et il est persisté dans
l'état (il survit donc à un redémarrage) :

1. **fichier** — créer le fichier `realtime.kill_switch_file`
   (`data/realtime/KILL_SWITCH`) ; il est *forçant* : l'API ne peut pas le lever ;
2. **environnement** — la variable d'environnement correspondante (également
   forçante) ;
3. **API** — `curl -X POST -H 'X-Operator-Token: …' -d '{"engage": true,
   "reason": "incident"}' http://127.0.0.1:8080/api/kill-switch`.

Le kill switch arrête tous les profils, **n'annule rien en silence**, et chaque
refus est journalisé avec sa raison.

### 11.5 Base d'état, redémarrage et réconciliation

L'état vit dans **un seul** fichier SQLite (`realtime.state_db`). Toutes les
écritures sont **idempotentes** (UPSERT sur clé naturelle) et l'identifiant de
commande est **déterministe** : `profile_id + symbol + horodatage de la bougie +
séquence`. Un redémarrage entre la soumission et le remplissage ne double donc
**jamais** un ordre, et la **dernière bougie traitée** est persistée par profil :
un redémarrage ne rejoue pas une bougie et n'en saute pas.

Au démarrage, l'orchestrateur **réconcilie** l'état local contre le lieu
d'exécution (`Broker.reconcile()`) et marque le profil `degraded` en cas
d'écart. Le magasin est **mono-écrivain** : lancer deux orchestrateurs sur le
même fichier échoue avec `StateStoreError` (verrou de fichier), et une base
écrite par une version de schéma plus récente échoue de la même façon.

```bash
# un tick, puis inspection directe de l'état persisté
python -m trading_platform realtime run --state-db data/realtime/state.db --once
sqlite3 data/realtime/state.db "select profile_id, timestamp, equity from equity;"
sqlite3 data/realtime/state.db "select key, value from meta where key like 'last_candle%';"
```

### 11.6 API web

Le tableau de bord sonde `GET /api/profiles` toutes les 2 secondes ; toutes les
réponses sont du JSON (horodatages ISO-8601 UTC, aucun `NaN`).

The Python server serves **no HTML page and no static asset**: every path outside
the table below — `GET /` and `GET /static/{asset}` included — answers the
documented JSON 404 `{"error": "not found: <path>"}`.

| Méthode et route | Réponse |
| --- | --- |
| `GET /api/health` | `status`, `version`, `uptime_seconds`, `profiles_total`, `profiles_running`, `kill_switch`, `checked_at` |
| `GET /api/profiles` | `{profiles: [...], generated_at}` |
| `GET /api/profiles/{id}` | le snapshot du profil |
| `GET /api/profiles/{id}/equity` | `{points: [{timestamp, equity, cash, position_value}…]}` |
| `GET /api/profiles/{id}/trades` | `{trades: [...], count}` |
| `GET /api/profiles/{id}/orders` | `{orders: [...]}` |
| `GET /api/profiles/{id}/positions` | `{positions: [...]}` |
| `GET /api/profiles/{id}/metrics` | `{metrics: {...}, benchmark: {...} \| null, generated_at}` |
| `GET /api/kill-switch` | `{kill_switch, reason, changed_at}` (the emergency-stop state the dashboard polls) |
| `POST /api/kill-switch` | `{engage: bool, reason: str}` → `{kill_switch, reason, changed_at}` ; exige `X-Operator-Token` |

`400` requête malformée, `403` jeton absent/invalide **ou** serveur en lecture
seule (`realtime serve`), `404` route ou profil inconnu, `405` méthode
incorrecte, `500` `{error}` — jamais de trace sur le réseau.

The monitoring dashboard is a standalone **Next.js** application in `dashboard/`
(App Router, React 19, Tailwind CSS v4, TypeScript strict): it runs as a real Node
server — not a static export — and it polls the JSON API above on the cadence of
`monitoring.refresh_seconds` (2 s by default). Install and start it with
`make dashboard-install` and `make dashboard-dev`, then open
`http://127.0.0.1:3000`. The browser only ever talks to that origin: the rewrite
declared in `dashboard/next.config.ts` proxies `/api/:path*` to this Python server
(`API_ORIGIN`, `http://127.0.0.1:8080` by default), so there is no CORS, no
absolute URL in the browser, and the `X-Operator-Token` header flows through
untouched.

```bash
# vérification rapide d'une instance
curl -s http://127.0.0.1:8080/api/health
curl -s http://127.0.0.1:8080/api/profiles/btc-paper/metrics
```

### 11.7 Cible `make`

```bash
make realtime                                   # moteur + API JSON
STATE_DB=data/realtime/state.db make realtime
```

La cible `realtime` appelle `realtime run --state-db
${STATE_DB:-data/realtime/state.db}` et laisse `make check` intact.

### 11.7.1 Operating a forecast profile: three commands

A `timesfm` profile is inert without an artifact, and the startup guard refuses
it rather than letting it run silently ([`docs/realtime.md`](realtime.md) §3.1).
The operational path is **download → build → declare → start → confirm**, and the
first three steps are wired as `make` targets:

```bash
# 1. the candles of the symbol/timeframe the profile declares (ONLY network step)
make data-download SYMBOL=BTC/USDT TIMEFRAME=1h

# 2. the artifact the profile declares, built offline and deterministically
make forecast-profile STATE_DB=data/realtime/state.db BACKEND=seasonal
#    ... the equivalent CLI call, which is what the target runs:
python -m trading_platform forecast-bootstrap \
    --state-db data/realtime/state.db --backend seasonal

# 2b. is it still usable right now? (the very same guard the engine runs)
make forecast-info STATE_DB=data/realtime/state.db
python -m trading_platform forecast-info \
    --artifact data/forecast/btc-timesfm-paper-1h-seasonal.parquet \
    --state-db data/realtime/state.db --profile btc-timesfm-paper

# 3. start the engine: the profile now really trades
make realtime-forecast       # == realtime run --state-db $(STATE_DB)

# the four, in order
make forecast-flow
```

| Target | Underlying command | Role |
| --- | --- | --- |
| `make forecast-bootstrap` | `forecast-build … --backend naive --out $(FORECAST_DIR)/bootstrap-naive.parquet` | the *naive* baseline artifact |
| `make forecast-bootstrap-seasonal` | `forecast-build … --backend seasonal --out $(FORECAST_DIR)/bootstrap-seasonal.parquet` | the *seasonal* baseline artifact |
| `make forecast-profile` | `forecast-bootstrap --state-db $${STATE_DB:-data/realtime/state.db} --backend $${BACKEND:-seasonal}` | the artifact the **profile declares**, from its own candle file |
| `make forecast-info` | `forecast-info --artifact $(FORECAST_ARTIFACT) --state-db $${STATE_DB:-data/realtime/state.db} --json` | is the artifact still usable? `STATE_DB=…` evaluates it against a profile |
| `make realtime-forecast` | `realtime run --state-db $${STATE_DB:-data/realtime/state.db}` | the engine, on the profiles of the state database |
| `make forecast-flow` | `data-download forecast-bootstrap forecast-info realtime-forecast` | the whole chain, in order |

**Declare the profile.** The forecast profile is a normal `ProfileConfig`: the only
thing that distinguishes it is the `strategy` name (`timesfm`), its `params` and
the `forecast` key:

```json
{
  "id": "btc-timesfm-paper",
  "symbol": "BTC/USDT",
  "timeframe": "1h",
  "strategy": "timesfm",
  "mode": "paper",
  "forecast": "data/forecast/btc-timesfm-paper-1h-seasonal.parquet"
}
```

`symbol`, `timeframe` and the `forecast` path must agree with the artifact's own
metadata: a mismatch is refused at startup rather than trading another
instrument's forecast.

**Confirm it trades.** "Started" is not "trading". Read the observable
surface, not the log line:

```bash
# one deterministic tick, then exit 0 (no server)
python -m trading_platform.cli realtime run \
    --state-db data/realtime/state.db --once --json

# or, with the engine running, the per-profile snapshot of the JSON API
curl -s http://127.0.0.1:8080/api/profiles
```

A profile that trades shows up in three places of the `ProfileSnapshot` (§11.6):
`n_trades` becomes greater than `0`, `open_positions` reports the position held,
and `last_block_reason` stays `null` as long as the risk layer refuses nothing.
A profile whose `n_trades` stays at `0` while `status` is `running` **is not
trading**: read its `forecast_*` columns and its `exit_code`, and remember that
the entry gates are deliberately strict — a *wired* artifact is not an
*informative* one (§12.4, and `docs/forecasting.md` (page retired: the forecast subsystem was removed) §9).

The dashboard has its own targets, next to the Python ones:

```bash
make dashboard-install          # npm ci in dashboard/ (the lockfile is committed)
make dashboard-dev              # Next.js dev server on http://127.0.0.1:3000
make dashboard-lint             # eslint
make dashboard-typecheck        # tsc --noEmit
make dashboard-test             # vitest run
make dashboard-test-coverage    # vitest run --coverage (docs/testing-policy.md §9)
make dashboard-build            # next build (standalone output)
make dashboard-check            # lint + typecheck + test-coverage + build
make check-all                  # make check (Python) + make dashboard-check
```

### 11.8 Cycle de vie des profils et historique de bougies

Le tableau de bord Next.js de `dashboard/` expose désormais la création, la mise
en pause, la reprise et la suppression d'un profil, ainsi qu'un graphique en
chandeliers japonais sur la page d'un profil (avec les entrées/sorties, le prix
moyen de la position ouverte et son stop).

```bash
# le catalogue des sélecteurs : paires négociables, stratégies, timeframes, modes
curl -s http://127.0.0.1:8080/api/catalog

# l'état de pause de chaque profil, et ce que ce serveur accepte de muter
curl -s http://127.0.0.1:8080/api/control

# les bougies persistées d'un profil (500 par défaut, 1000 au maximum)
curl -s "http://127.0.0.1:8080/api/profiles/btc-paper/candles?limit=100"

# pause, reprise et suppression : le jeton opérateur est obligatoire
curl -s -X POST -H "X-Operator-Token: $TB_OPERATOR_TOKEN" \
  http://127.0.0.1:8080/api/profiles/btc-paper/pause
curl -s -X POST -H "X-Operator-Token: $TB_OPERATOR_TOKEN" \
  http://127.0.0.1:8080/api/profiles/btc-paper/resume
curl -s -X DELETE -H "X-Operator-Token: $TB_OPERATOR_TOKEN" \
  http://127.0.0.1:8080/api/profiles/btc-paper

# création : le profil est écrit dans le fichier de profils, puis démarré
curl -s -X POST -H "X-Operator-Token: $TB_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"profile_id": "sol-paper", "symbol": "SOL/USDT", "timeframe": "15m",
       "strategy": "basic", "mode": "paper", "initial_balance": 2500}' \
  http://127.0.0.1:8080/api/profiles
```

Mettre un profil en pause arrête l'**ouverture** de nouvelles positions mais
garde la position ouverte pilotée : le stop et les sorties restent évalués, donc
un profil en pause reste `running` et continue de publier son equity et ses
bougies. La suppression aplatit d'abord toute la position au marché, puis retire
le profil du moteur **et** du fichier de profils (réécrit de façon atomique) :
si l'aplatissement échoue, la suppression échoue et rien n'est modifié. La
création valide le corps contre le catalogue (`400` sur une stratégie ou un
timeframe inconnus, `409` sur un identifiant déjà pris). Ces quatre routes
exigent `X-Operator-Token` et répondent `403` sur un serveur `realtime serve`,
qui ne mute jamais l'état.

---

## 12. Forecast layer — offline TimesFM artifacts

`trading_platform.forecast` (layer 2.5) pre-computes probabilistic price paths
**offline** and the `timesfm` strategy consumes them as a deterministic external
input. The strategy never runs the model inside `prepare()`/`signals()`: the
whole prediction step happens in `trading forecast-build` (§4.7), which writes a
versioned parquet artifact plus a `<artifact>.meta.json` sidecar. The complete
reference — artifact schema, backend contract, licence table (TimesFM 2.5
Apache-2.0 is the default; TimesFM 3.0 weights are
non-commercial and opt-in only), measured hardware numbers, library traps and
the honesty section — is `docs/forecasting.md` (page retired: the forecast subsystem was removed).

### 12.1 Install

```bash
.venv/bin/python -m pip install -e ".[dev]"           # suite + naive/seasonal backends
.venv/bin/python -m pip install -e ".[timesfm]"       # TimesFM 2.5 (Apache-2.0 weights)
.venv/bin/python -m pip install -e ".[timesfm-xreg]"  # optional calendar-only XReg mode
```

The heavy extras are never installed by CI and never required by the test suite:
the four commands of §4.7, the offline backends and every test run with the
`dev` extra alone.

**Realtime use.** The same artifact feeds a realtime profile through its
`forecast` key, and the engine resolves it exactly as the backtest does — so a
`timesfm` profile really trades, and a stale or mismatched artifact is refused at
startup instead of running inert. The profile field, the startup guard and the
three-command operational flow are documented in
[`docs/realtime.md`](realtime.md) §1, §3.1 and §11.7.1 of this page.

### 12.2 End-to-end offline backtest

No ML dependency, no network, no cache — the recipe uses only the synthetic
generator, the `naive` backend and the engine:

```python
from pathlib import Path

from trading_platform.config import load_config
from trading_platform.data.synthetic import make_ohlcv
from trading_platform.forecast import ForecastBuildConfig, build_forecast_artifact
from trading_platform.strategy.engine import run_backtest_on_config

frame = make_ohlcv(2000, start="2022-01-01T00:00:00Z", timeframe="1h", seed=42)

build_forecast_artifact(
    frame,
    Path("data/forecast/forecast.parquet"),
    ForecastBuildConfig(backend="naive", context_length=256, horizon=24, reforecast_every=8),
)

Path("forecast_config.json").write_text(
    '{"strategy": {"name": "timesfm"}, "forecast": {"artifact": "data/forecast/forecast.parquet"}}',
    encoding="utf-8",
)
result = run_backtest_on_config(load_config("forecast_config.json"), frame)
print(result.n_trades, result.final_balance)
```

`run_backtest_on_config` resolves `forecast.artifact` through the
feature-injection seam (`trading_platform.strategy.features`) and injects the
loaded `ForecastStore` into the strategy, so `backtest`, `walk-forward`,
`robustness` and `monte-carlo` all run unchanged on `timesfm`:

```bash
make forecast-build
python -m trading_platform.cli backtest \
    --config forecast_config.json --data-file btc.csv --no-network
python -m trading_platform.cli walk-forward \
    --config forecast_config.json --data-file btc.csv --no-network
```

### 12.3 Strategy parameters and diagnostics

`strategy.name` selects `timesfm` (registry name) and `strategy.params`
overrides `TimesFMForecastParams`: artifact wiring, `context_length`, `horizon`,
`reforecast_every`, `min_lead`, the forecast-age bound, the entry gates
(predicted edge, edge vs ATR, reliability, decile agreement, path efficiency,
volatility regime, ATR-percentile window, optional RSI context filter, cooldown),
the exit thresholds (`exit_alpha`, `exit_confirm`, `target_capture`,
`min_progress`, `exit_vol_ratio`), the ATR stop (`atr_period`,
`atr_stop_multiplier`), `max_hold` and `allow_short`. `PARAM_SPACE` exposes the
same fields to the robustness sweep.

`prepare()` adds the house indicators (`rsi`, `atr`, `ema`) **and** the
`forecast_*` diagnostic columns (`forecast_alpha_<k>`, `forecast_slope`,
`forecast_mfe`, `forecast_mae`, `forecast_path_eff`, `forecast_iqr_term`,
`forecast_iqr_per_bar`, `forecast_reliability`, `forecast_agreement`,
`vol_ratio`, `forecast_age`, `atr_pct`), plus `exit_code` — the integer reason
of the exit that fired (1 `FORECAST_FLIP`, 2 `EDGE_DECAY`, 3 `TARGET_REACHED`,
4 `PATH_DEGRADED`, 5 `TIME_STOP`, 6 `VOL_REGIME`, 7 `STALE_FORECAST`,
8 `ATR_STOP`). Every column, its NaN rule and the priority order of the exits
are tabulated in `docs/forecasting.md` (page retired: the forecast subsystem was removed) §10.

### 12.4 Honesty

The artifact can be measured, and it must be measured: run
`trading forecast-skill` before believing any backtest. The 2025-26 literature
on foundation models for financial series finds **no reliable directional edge**
on returns, and a positive backtest PnL is **not evidence of an edge**. The
exact numbers, the arXiv references and how to read the skill metrics are in
`docs/forecasting.md` (page retired: the forecast subsystem was removed) §9.

**Making the strategy usable changes nothing about that.** The realtime
integration (§12.1) is engineering: it makes an existing, already-researched
strategy selectable, wired, generic and safely operable. It adds no directional
edge and it tuned no threshold against data. A `timesfm` profile that now starts
and trades is therefore *observable*, not *validated* — the model predicts
**risk**, not direction, and it remains exactly as unprofitable as §9 says until
`forecast-skill` says otherwise. Tests and the coverage gate follow
[`docs/testing-policy.md`](testing-policy.md) — this document never duplicates
it.
