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
| `all` | `freqtrade` + `exchange` + `dev` | poste de travail complet | tout installer |

```bash
# téléchargement de données réelles
.venv/bin/python -m pip install -e ".[exchange]"

# bot Freqtrade (live / dry-run)
.venv/bin/python -m pip install -e ".[freqtrade]"
```

> **Le moteur de backtest n'a besoin ni de `freqtrade` ni de `ccxt`.** La suite
> de tests doit rester verte avec le seul extra `dev` :
> voir [`docs/testing-policy.md`](testing-policy.md).

---

## 2. Démarrage rapide

```bash
# 0. installation (voir §1)
python3.11 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"

# 1. générer un jeu de données déterministe, hors ligne, sans cache ni exchange
.venv/bin/python -c "from trading_backtest.data.synthetic import make_ohlcv; \
make_ohlcv(8760, start='2022-01-01T00:00:00Z', timeframe='1h', seed=42).to_csv('btc.csv')"

# 2. backtest complet sur ce CSV
.venv/bin/python -m trading_backtest.cli backtest \
    --config config/backtest_default.json --data-file btc.csv

# 3. walk-forward (le juge de paix)
.venv/bin/python -m trading_backtest.cli walk-forward \
    --config config/backtest_default.json --data-file btc.csv
```

Une fois le paquet installé, la commande console `trading-backtest …` est
équivalente à `.venv/bin/python -m trading_backtest.cli …`. Les deux formes sont
utilisées indifféremment dans ce document.

---

## 3. Anatomie de `config/backtest_default.json`

Le fichier de configuration du moteur est un JSON unique, validé par
`trading_backtest.config.AppConfig` (pydantic). **Toute clé inconnue est une
erreur** (`extra="forbid"`) ; toute clé absente prend sa valeur par défaut. Les
valeurs ci-dessous sont celles du modèle `AppConfig` et du fichier livré
`config/backtest_default.json`.

Une clé peut aussi être surchargée par variable d'environnement, préfixe `TB_`
et délimiteur `__` :

```bash
TB_DATA__TIMEFRAME=4h TB_BACKTEST__INITIAL_BALANCE=2500 \
    python -m trading_backtest.cli backtest --config config/backtest_default.json
```

### 3.1 Racine

| Clé | Défaut | Sens |
| --- | --- | --- |
| `project_name` | `"trading-backtest"` | nom affiché dans les rapports et les logs |
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
  "project_name": "trading-backtest",
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
python -m trading_backtest.cli backtest \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --benchmark-variant risk_free --risk-free-rate 0.05

# test de compétence : entrées aléatoires reproductibles (graine du config)
python -m trading_backtest.cli backtest \
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
.venv/bin/python -c "from trading_backtest.data.synthetic import make_ohlcv; \
make_ohlcv(8760, start='2022-01-01T00:00:00Z', timeframe='1h', seed=42).to_csv('btc.csv')"
```

### 4.1 `backtest` — un run unique

```bash
# CSV local : aucun réseau, aucun cache
python -m trading_backtest.cli backtest \
    --config config/backtest_default.json \
    --data-file btc.csv --no-network

# symbole, timeframe et sous-fenêtre explicites
python -m trading_backtest.cli backtest \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --symbol BTC/USDT --timeframe 1h \
    --start 2023-01-02T00:00:00Z --end 2023-01-06T00:00:00Z

# sans --data-file : cache d'abord, puis téléchargement (extra exchange requis)
python -m trading_backtest.cli backtest \
    --config config/backtest_default.json \
    --start 2023-01-01T00:00:00Z --end 2024-01-01T00:00:00Z

# sortie machine : un objet JSON sur stdout, et un rapport markdown seulement
python -m trading_backtest.cli backtest \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --formats markdown --output-dir reports/runs --json

# changer de timeframe ne demande aucune option : la config suffit
TB_DATA__TIMEFRAME=4h TB_STRATEGY__TIMEFRAME=4h \
    python -m trading_backtest.cli backtest \
    --config config/backtest_default.json --data-file btc.csv

# comparer explicitement le run au buy & hold de la même fenêtre
python -m trading_backtest.cli backtest \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --benchmark

# désactiver le benchmark : plus de section « Benchmark », plus de run.benchmark
python -m trading_backtest.cli backtest \
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
python -m trading_backtest.cli walk-forward \
    --config config/backtest_default.json \
    --data-file btc.csv --no-network

# 8 fenêtres ancrées, 75 % in-sample, scorées sur le rendement total
python -m trading_backtest.cli walk-forward \
    --config config/backtest_default.json --data-file btc.csv --no-network \
    --windows 8 --is-ratio 0.75 --mode anchored --metric total_return

# les mêmes réglages par variables d'environnement
TB_VALIDATION__MODE=anchored TB_VALIDATION__N_WINDOWS=8 \
    python -m trading_backtest.cli walk-forward \
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
python -m trading_backtest.cli robustness \
    --config config/backtest_default.json \
    --data-file btc.csv --no-network

# métrique cible et garde-fou de grille explicites
python -m trading_backtest.cli robustness \
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
python -m trading_backtest.cli monte-carlo \
    --config config/backtest_default.json \
    --data-file btc.csv

# variante : bootstrap de la courbe d'equity, 500 tirages, graine fixée
python -m trading_backtest.cli monte-carlo \
    --config config/backtest_default.json --data-file btc.csv \
    --simulations 500 --method bootstrap_equity --seed 7

# les mêmes réglages par variables d'environnement
TB_VALIDATION__MONTE_CARLO_METHOD=bootstrap_equity \
TB_VALIDATION__N_MONTE_CARLO=5000 \
TB_VALIDATION__RANDOM_SEED=7 \
    python -m trading_backtest.cli monte-carlo \
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
python -m trading_backtest.cli data download \
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
python -m trading_backtest.cli config show --config config/backtest_default.json

# le même contenu en JSON
python -m trading_backtest.cli config show \
    --config config/backtest_default.json --json

# validation seule (code de sortie non nul si le fichier est invalide)
python -m trading_backtest.cli config validate --config config/backtest_default.json
```

`config validate` est le premier réflexe en cas de doute : il vérifie les clés
inconnues, les types, les timeframes supportés et la cohérence
`start < end`. Le type du fichier est détecté automatiquement :
`config validate --config config/freqtrade_dryrun.json` valide la configuration
Freqtrade et rend `{"kind": "freqtrade", "valid": true, "issues": []}` (un
`AppConfig` rend `{"kind": "appconfig", ...}`).

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
| `make backtest` | `python -m trading_backtest backtest --config $(CONFIG)` | backtest unique (`CONFIG=…` pour changer de fichier) |
| `make walk-forward` | `python -m trading_backtest walk-forward --config $(CONFIG)` | walk-forward |
| `make robustness` | `python -m trading_backtest robustness --config $(CONFIG)` | balayage paramétrique |
| `make monte-carlo` | `python -m trading_backtest monte-carlo --config $(CONFIG)` | Monte Carlo |
| `make data-download` | `python -m trading_backtest data download …` | remplissage du cache (seule cible qui utilise le réseau) |
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

---

## 8. Docker

L'image fournit un environnement reproductible (Python 3.11 + les dépendances
déclarées dans `requirements*.txt`), utile pour rejouer un backtest à
l'identique sur une autre machine.

```bash
# construction (la dernière étape du Dockerfile est `test` : la suite tourne
# pendant le build ; `docker build --target base` ne construit que le runtime)
docker build -t trading-backtest:latest .

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
l'image est `python -m trading_backtest`, la commande se passe donc directement
en arguments.

```bash
docker run --rm -v "$PWD/reports:/app/reports" trading-backtest:latest \
    backtest \
    --config config/backtest_default.json --data-file data/btc.csv
```

---

## 9. Configs Freqtrade et config de backtest

Le dépôt contient **deux familles de configuration**, qui ne servent pas à la
même chose :

| Fichier | Consommateur | Rôle |
| --- | --- | --- |
| `config/backtest_default.json` | `trading_backtest.config.AppConfig` | configuration du **moteur de backtest** : données, stratégie, exécution, validation, rapports |
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
| `ConfigError` au démarrage | clé inconnue ou mal typée dans le JSON | `python -m trading_backtest.cli config validate --config …` |
| `DataValidationError` | colonne obligatoire absente, `NaN`, prix ou volume non positif, index non temporel, trous au-delà de `max_gap_factor` | corriger la source ; voir le contrat OHLCV dans [`docs/architecture.md`](architecture.md#5-contrat-de-données-ohlcv) (le tri, les doublons et un index naïf sont corrigés automatiquement par `ensure_ohlcv`) |
| `InsufficientDataError` | historique trop court pour la fenêtre demandée | réduire `validation.n_windows` / `validation.in_sample_ratio` ou allonger `data.start` |
| `DataDownloadError` | extra `exchange` absent ou API indisponible | `pip install -e ".[exchange]"` ou utiliser `--data-file` |
| `StrategyError` | nom de stratégie inconnu | vérifier `strategy.name` et les noms passés à `register_strategy` |
| Aucun trade | paramètres trop stricts (RSI, seuils) | élargir `rsi_min`/`rsi_max`, réduire `ema_fast`/`ema_slow` |
| Tests rouges sur la couverture | seuil de 85 % non atteint | voir [`docs/testing-policy.md`](testing-policy.md) §5 |
