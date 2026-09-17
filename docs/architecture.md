# Architecture

Ce document décrit l'architecture du squelette de backtesting : l'arborescence
livrée, les couches et leurs règles d'import, l'inventaire **gelé** des
interfaces publiques, le contrat de données OHLCV, les modèles de domaine et les
deux « coutures » (*seams*) qui rendent le projet testable hors ligne.

Il complète :

- [`docs/backtesting-methodology.md`](backtesting-methodology.md) — protocole de validation ;
- [`docs/usage.md`](usage.md) — installation, CLI, rapports, Docker ;
- [`docs/testing-policy.md`](testing-policy.md) — politique de tests et seuil de couverture.

---

## 1. Objectifs d'architecture

1. **Modulaire** — chaque responsabilité (données, stratégie, exécution,
   validation, métriques, restitution, orchestration) vit dans un paquet isolé
   — l'exécution est le sous-module `trading_backtest.strategy.engine` — et ne
   dépend que des couches inférieures.
2. **Robuste** — le domaine est modélisé par des objets typés (dataclasses
   gelées pour `core`, modèles pydantic pour `config`), les entrées sont
   validées à la frontière (config, OHLCV), et aucune erreur silencieuse n'est
   tolérée sur les données de marché.
3. **Scalable** — l'exécution est injectable (`RunnerFn`), les résultats sont
   sérialisables (`to_dict()`), ce qui permet de paralléliser des balayages
   paramétriques ou des walk-forwards sans réécrire le moteur.
4. **Testable hors ligne** — aucune dépendance réseau ou optionnelle
   (`freqtrade`, `ccxt`) n'est requise par la suite de tests. Voir
   [`docs/testing-policy.md`](testing-policy.md).

---

## 2. Arborescence livrée

```
Trading/
├── .github/
│   └── workflows/
│       └── ci.yml                    # CI unique : lint + type-check + tests + coverage
├── config/
│   ├── backtest_default.json         # configuration de référence du moteur (AppConfig)
│   ├── freqtrade_config.json         # config Freqtrade « bot » (live/paper)
│   └── freqtrade_dryrun.json         # variante dry-run de la config Freqtrade
├── docs/
│   ├── architecture.md               # ce document
│   ├── backtesting-methodology.md    # protocole de validation statistique
│   ├── usage.md                      # guide d'usage (install, CLI, rapports, Docker)
│   └── testing-policy.md             # politique de tests et de couverture (gelé)
├── src/trading_backtest/
│   ├── __init__.py                   # exports publics et version du paquet
│   ├── __main__.py                   # point d'entrée `python -m trading_backtest`
│   ├── core/
│   │   ├── constants.py              # constantes partagées (timeframes, colonnes OHLCV, UTC)
│   │   ├── models.py                 # modèles de domaine : TradeRecord, BacktestResult, RunnerFn
│   │   └── errors.py                 # hiérarchie d'exceptions du projet
│   ├── config/
│   │   ├── models.py                 # configuration typée (pydantic) : AppConfig et sous-modèles
│   │   └── loader.py                 # chargement et validation d'un fichier JSON de configuration
│   ├── data/
│   │   ├── cache.py                  # OHLCVCache : cache disque des bougies
│   │   ├── loader.py                 # OHLCVLoader + providers (ccxt paresseux, CSV hors ligne)
│   │   ├── validation.py             # contrôles qualité OHLCV (tri, doublons, NaN, trous)
│   │   └── synthetic.py              # générateur de données synthétiques déterministes (tests)
│   ├── strategy/
│   │   ├── indicators.py             # EMA, RSI, ATR — calculs purs, sans dépendance externe
│   │   ├── base.py                   # Strategy : contrat prepare / signals / run
│   │   ├── basic.py                  # BasicStrategy : croisement EMA + filtre RSI + stop ATR
│   │   ├── registry.py               # register_strategy / get_strategy (résolution par nom)
│   │   └── engine.py                 # run_backtest / make_runner : boucle d'exécution + seam RunnerFn
│   ├── validation/
│   │   ├── split.py                  # split_is_oos / make_windows : découpage IS/OOS + purge
│   │   ├── walk_forward.py           # walk_forward : fenêtres glissantes ou ancrées
│   │   ├── robustness.py             # parameter_sweep : balayage paramétrique et ratios de stabilité
│   │   └── monte_carlo.py            # monte_carlo : rééchantillonnage des trades / bootstrap d'equity
│   ├── metrics/
│   │   ├── performance.py            # METRIC_NAMES / MetricSet / compute_metrics / metric_value
│   │   └── drawdown.py               # drawdown_series / max_drawdown / drawdown_duration / drawdown_table
│   ├── reporting/
│   │   ├── builder.py                # Report / ReportBuilder / build_report : agrégation d'un rapport
│   │   ├── markdown.py               # render_markdown / render_summary_table : rendu markdown
│   │   └── writer.py                 # write_report / read_report : écriture markdown + JSON
│   ├── freqtrade/
│   │   └── config.py                 # validation des configs Freqtrade (dry-run et live)
│   └── cli.py                        # CLI Typer : backtest, walk-forward, robustness, monte-carlo, data, config
├── tests/                            # suite pytest hors ligne (voir docs/testing-policy.md)
├── user_data/
│   └── README.md                     # état écrit par Freqtrade (OHLCV, backtests, stratégies) — git-ignoré
├── Dockerfile                        # image reproductible (Python 3.11)
├── Makefile                          # tâches de développement
├── pyproject.toml                    # packaging + config ruff / mypy / pytest / coverage (gelé)
├── requirements.txt                  # dépendances runtime (miroir de [project].dependencies)
├── requirements-dev.txt              # dépendances de développement et de CI
├── requirements-freqtrade.txt        # dépendances de l'extra `freqtrade` (jamais importé par le moteur)
└── README.md                         # porte d'entrée du dépôt
```

Chaque module a **une** responsabilité. Les fichiers d'un même paquet ne
s'importent jamais « en travers » : un sous-module de `data` ne va pas chercher
un sous-module de `validation`.

Les découpages internes (`split.py` / `walk_forward.py` / …) sont des détails
d'implémentation : ce qui est **gelé**, ce sont les chemins de paquets
(`trading_backtest.<paquet>`) et les symboles listés au §4.

---

## 3. Couches et règle de dépendance

```
        ┌──────────────────────────────────────────┐
  6     │                  cli                     │   orchestration, entrées/sorties
        └──────────────────────────────────────────┘
                            │
        ┌──────────────────────────────────────────┐
  5     │              validation                  │   IS/OOS, walk-forward, robustesse, Monte Carlo
        └──────────────────────────────────────────┘
                            │
        ┌──────────────────────────────────────────┐
  4     │               reporting                  │   restitution : markdown + JSON
        └──────────────────────────────────────────┘
                            │
        ┌────────────────────────┬─────────────────┐
  3     │ strategy (+ engine.py) │     metrics     │   décision, boucle d'exécution, mesure
        └────────────────────────┴─────────────────┘
                            │
        ┌───────────┬───────────┬──────────────────┐
  2     │  config   │   data    │    freqtrade     │   configuration, marché, configs Freqtrade
        └───────────┴───────────┴──────────────────┘
                            │
        ┌──────────────────────────────────────────┐
  1     │                  core                    │   modèles de domaine et erreurs
        └──────────────────────────────────────────┘
```

**Règle de dépendance (non négociable) : les imports ne pointent QUE vers le
bas de cette liste.**

| Couche | Paquet | Peut importer |
| --- | --- | --- |
| 1 | `trading_backtest.core` | la bibliothèque standard, `pandas` |
| 2 | `trading_backtest.config`, `trading_backtest.data`, `trading_backtest.freqtrade` | couche 1 |
| 3 | `trading_backtest.strategy` (dont `strategy.engine`), `trading_backtest.metrics` | couches 1–2 (`strategy.engine` importe `config` ; `metrics` n'importe que `core`) |
| 4 | `trading_backtest.reporting` | couches 1–3 (`core` et `metrics`) |
| 5 | `trading_backtest.validation` | couches 1–4 (`core`, `data.validation`, `metrics` importé paresseusement) |
| 6 | `trading_backtest.cli` | couches 1–5 (plus `freqtrade` pour valider une config Freqtrade) |

Conséquences pratiques :

- `core` n'importe **rien** du projet : c'est le vocabulaire commun.
- Une stratégie ne connaît ni le cache disque ni la CLI : elle reçoit un
  `DataFrame` OHLCV et rend des signaux.
- `validation` orchestre un exécuteur *injecté* (`RunnerFn`) et `metrics`, jamais
  l'inverse : elle n'importe **jamais** `strategy.engine` — c'est l'appelant
  (la CLI) qui construit le runner avec `make_runner(cfg)`. Le moteur, lui,
  ignore ce qu'est un walk-forward.
- L'ordre « `metrics` **sous** `validation` » est structurel : `validation`
  note ses fenêtres avec `metrics.metric_value`, importé paresseusement dans le
  corps des fonctions.
- Seule la CLI a le droit de parler à l'utilisateur (sortie terminal, fichiers
  de rapport) et de lire des arguments.

---

## 4. Inventaire des interfaces gelées

Ces symboles et signatures constituent le contrat entre paquets. Toute
évolution doit être répercutée ici **dans la même modification**.

### 4.1 Configuration — `trading_backtest.config`

| Symbole | Signature clé | Rôle |
| --- | --- | --- |
| `AppConfig` | racine `pydantic-settings` ; champs `project_name`, `log_level`, `exchange`, `data`, `strategy`, `backtest`, `validation`, `reporting`, plus la propriété `timezone` | configuration complète d'un run, validée à l'instanciation |
| `load_config` | `load_config(path: str \| Path \| None = None, overrides: Mapping[str, Any] \| None = None) -> AppConfig` | lit un JSON (**`None` = défauts seuls**), fusionne les surcharges (imbriquées ou pointées `"data.timeframe"`), puis valide |
| `default_config` | `() -> AppConfig` | configuration intégrée, sans fichier |
| `dump_config` | `dump_config(cfg: AppConfig, path: str \| Path) -> Path` | écrit la configuration effective en JSON (`model_dump(mode="json")`), réinjectable dans `load_config` |
| `override_params` | `override_params(cfg: AppConfig, params: Mapping[str, Any]) -> AppConfig` | copie profonde dont `strategy.params` est mis à jour (sans muter l'entrée) — utilisé par le balayage paramétrique |
| `AppConfig.model_dump` | `() -> dict[str, Any]` | payload JSON-able (fourni par pydantic) |

Sous-modèles exposés (tous en `extra="forbid"`) : `ExchangeConfig`,
`DataConfig`, `StrategyConfig`, `BacktestConfig`, `ValidationConfig`,
`ReportingConfig`. Les valeurs par défaut sont celles de
`config/backtest_default.json` — détaillées dans
[`docs/usage.md`](usage.md#3-anatomie-de-configbacktest_defaultjson).

Les variables d'environnement `TB_` (délimiteur `__`) surchargent le fichier :
`TB_DATA__TIMEFRAME=4h`, `TB_BACKTEST__INITIAL_BALANCE=2500`.

### 4.2 Données — `trading_backtest.data`

| Symbole | Signature clé | Rôle |
| --- | --- | --- |
| `OHLCVLoader` | `OHLCVLoader(exchange: str, cache_dir: Path, *, fmt="parquet", allow_network=True, validate=True, max_gap_factor=3.0, max_missing_ratio=0.0, provider=None, cache=None)` | chargeur « cache d'abord », seul endroit autorisé à lire/écrire le cache et à déclencher un téléchargement |
| `OHLCVLoader.load` | `load(symbol: str, timeframe: str, since, until) -> pd.DataFrame` | sert la fenêtre demandée depuis le cache ; télécharge le manquant via le provider |
| `OHLCVLoader.load_cached` | `load_cached(symbol, timeframe, since=None, until=None) -> pd.DataFrame` | **jamais** de réseau : lève `InsufficientDataError` si le cache ne couvre pas |
| `OHLCVLoader.download` | `download(symbol, timeframe, since, until) -> pd.DataFrame` | télécharge sans passer par le cache |
| `OHLCVLoader.available_range` | `available_range(symbol, timeframe) -> tuple[pd.Timestamp, pd.Timestamp]` | première et dernière bougie en cache |
| `MarketDataProvider` | `Protocol` : `fetch_ohlcv(symbol, timeframe, since, until) -> pd.DataFrame` | couture d'accès au marché |
| `CsvDataProvider` | `CsvDataProvider(directory: Path, *, suffix=".csv")` | provider **hors ligne** : un CSV par symbole/timeframe (`BTC_USDT-1h.csv`) |
| `CcxtDataProvider` | `CcxtDataProvider(exchange="binance", *, market="spot", rate_limit_ms=200)` | provider réel, `ccxt` importé **paresseusement** (erreur explicite s'il manque) |
| `OHLCVCache` | `OHLCVCache(cache_dir: Path, fmt: Literal["parquet", "csv"] = "parquet")` | cache disque, un fichier par `(exchange, symbol, timeframe)` |
| `OHLCVCache.path_for` | `path_for(exchange: str, symbol: str, timeframe: str) -> Path` | chemin du fichier de cache (`<cache_dir>/<exchange>/<SYMBOL>/<tf>.<ext>`) |
| `OHLCVCache.read` | `read(exchange, symbol, timeframe) -> pd.DataFrame \| None` | rend `None` en cas d'absence, jamais d'exception |
| `OHLCVCache.write` | `write(exchange, symbol, timeframe, df: pd.DataFrame) -> Path` | écrit la frame et rend le chemin |
| `ensure_ohlcv` | `ensure_ohlcv(data: pd.DataFrame, *, name: str = "data") -> pd.DataFrame` | **normalisant** : rend une copie conforme au §5 (tri croissant, doublons supprimés avec `keep="last"`, index naïf localisé en UTC et index tz-aware converti en UTC, index nommé `timestamp`, colonnes OHLCV castées en `float64`). `DataValidationError` n'est levée que sur les violations **structurelles** (pas un `DataFrame`, colonne requise absente, index non `DatetimeIndex`, colonne non convertible en `float64`) |
| `validate_ohlcv` | `validate_ohlcv(data: pd.DataFrame, *, timeframe: str = "1h", max_gap_factor: float = 3.0, max_missing_ratio: float = 0.0, raise_on_error: bool = False) -> DataQualityReport` | **statistique** : rapporte colonnes manquantes, `NaN`, prix non positifs, volume non positif, index naïf, index non trié, doublons, trous au-delà de `max_gap_factor` et `missing_ratio` au-delà de `max_missing_ratio` ; lève `DataValidationError` seulement si `raise_on_error=True` (c'est le mode utilisé par la CLI quand `data.validate` vaut `true`) |
| `make_ohlcv` / `make_flat_ohlcv` / `make_trending_ohlcv` | `(n, …) -> pd.DataFrame` | générateurs synthétiques déterministes (tests et démos hors ligne) |

Les dépendances d'exchange (`ccxt`) sont **optionnelles** : elles ne sont
importées que dans le constructeur de `CcxtDataProvider`, jamais au chargement
du paquet. Avec `allow_network=False`, le provider n'est **jamais** appelé et un
défaut de cache lève `InsufficientDataError` — c'est ce qui garantit qu'un run
de test ne peut pas sortir sur le réseau.

### 4.3 Exécution — `trading_backtest.strategy.engine`

| Symbole | Signature clé | Rôle |
| --- | --- | --- |
| `RunnerFn` | `Callable[[pandas.DataFrame, Mapping[str, Any] \| None], BacktestResult]` | type de la fonction d'exécution injectable : `runner(data, params) -> BacktestResult`, où `params=None` signifie « paramètres par défaut de la stratégie » |
| `make_runner` | `make_runner(cfg: AppConfig, *, symbol: str \| None = None) -> RunnerFn` | construit l'exécuteur par défaut à partir de la configuration (`symbol=None` laisse `UNKNOWN/USDT`) |
| `run_backtest` | `run_backtest(strategy, data, *, initial_balance=10000.0, fee_rate=0.001, slippage=0.0, stake_amount=None, symbol="UNKNOWN/USDT", timeframe="1h", allow_short=None, params_id="") -> BacktestResult` | exécute une **instance** de stratégie sur une frame (`strategy` et `data` sont positionnels) |
| `run_backtest_on_config` | `run_backtest_on_config(cfg: AppConfig, data, *, params=None, symbol=None) -> BacktestResult` | exécute la stratégie décrite par la configuration : `params` surcharge `cfg.strategy.params`, le reste vient de `cfg.backtest.*` / `cfg.exchange.*` / `cfg.data.timeframe` |

### 4.4 Stratégies — `trading_backtest.strategy`

| Symbole | Signature clé | Rôle |
| --- | --- | --- |
| `Strategy` | classe de base abstraite ; `name: ClassVar[str]`, `ParamsModel: ClassVar[type[StrategyParams]]`, `PARAM_SPACE: ClassVar[dict[str, list[float \| int]]]`, propriété `params` | contrat d'une stratégie |
| `Strategy.prepare` | `prepare(data: pd.DataFrame) -> pd.DataFrame` | ajoute les colonnes d'indicateurs (pur, sans mutation de l'entrée) |
| `Strategy.signals` | `signals(data: pd.DataFrame) -> pd.DataFrame` | ajoute les colonnes de signal (`entry_long`, `exit_long`, `entry_short`, `exit_short`, `stop_loss` ; le squelette est *long only* par défaut, les colonnes *short* restent déclarées) |
| `Strategy.run` | `run(data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]` | rend `(prepared, signals)` — le point d'entrée du moteur |
| `BasicStrategy` | `name = "basic"` ; paramètres `ema_fast=9`, `ema_slow=21`, `rsi_period=14`, `rsi_min=30.0`, `rsi_max=70.0`, `atr_period=14`, `atr_stop_multiplier=2.0`, `allow_short=False` | croisement EMA + filtre RSI + stop ATR (`stop_loss` = `close − atr_stop_multiplier × ATR` sur la bougie de signal) |
| `register_strategy` | `register_strategy(cls: type[Strategy]) -> type[Strategy]` | décorateur de classe qui enregistre la stratégie dans `STRATEGIES` |
| `get_strategy` | `get_strategy(name: str, params: Mapping[str, Any] \| None = None) -> Strategy` | instancie une stratégie enregistrée |
| `strategy_names` / `strategy_param_space` | `strategy_names() -> list[str]`, `strategy_param_space(name: str) -> dict[str, list[float \| int]]` | noms disponibles et grille de paramètres d'une stratégie (base du balayage de robustesse) |

### 4.5 Validation — `trading_backtest.validation`

| Symbole | Signature clé | Rôle |
| --- | --- | --- |
| `split_is_oos` | `split_is_oos(data: pd.DataFrame, *, in_sample_ratio: float = 0.7, purge_candles: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]` | découpe positionnelle in-sample / out-of-sample : IS = les `floor(n × ratio)` premières lignes moins `purge_candles` lignes retirées **à la fin**, OOS = les lignes restantes à partir de `floor(n × ratio)` ; chaque côté doit garder `MIN_ROWS_PER_SLICE = 2` lignes |
| `make_windows` | `make_windows(data: pd.DataFrame, *, n_windows: int = 5, in_sample_ratio: float = 0.7, mode: Literal["rolling", "anchored"] = "rolling", purge_candles: int = 0) -> list[Window]` | découpe la série en `n_windows` blocs de `n // n_windows` lignes (la queue non divisible est ignorée) et rend les `Window(index, is_start, is_end, oos_start, oos_end)` |
| `walk_forward` | `walk_forward(runner: RunnerFn, data: pd.DataFrame, *, metric: str = "sharpe_ratio", metric_fn=None, n_windows: int = 5, in_sample_ratio: float = 0.7, mode: str = "rolling", purge_candles: int = 0, initial_balance: float = 10000.0) -> WalkForwardResult` | boucle IS → OOS **sans optimisation** (`runner(slice, None)`) ; agrège l'OOS recousu, `efficiency = aggregate_oos_metric / aggregate_is_metric` et `is_consistent` (`CONSISTENCY_THRESHOLD = 0.6`) |
| `parameter_sweep` | `parameter_sweep(runner: RunnerFn, data: pd.DataFrame, grid: Mapping[str, Sequence[float \| int]], *, base_params=None, metric: str = "sharpe_ratio", metric_fn=None, max_combinations: int = 512, initial_balance: float = 10000.0) -> RobustnessResult` | balayage paramétrique : `stability`, `positive_ratio`, `robust_ratio`, `worst_case_return`, `best_params`, `is_robust` |
| `monte_carlo` | `monte_carlo(result: BacktestResult, *, n_simulations: int = 1000, method: Literal["trade_resample", "bootstrap_equity"] = "trade_resample", random_seed: int = 42, initial_balance: float \| None = None) -> MonteCarloResult` | `trade_resample` ou `bootstrap_equity`, VaR/CVaR 95 %, `prob_profit`, percentiles `p05…p95` |

Les valeurs par défaut ci-dessus sont celles de `ValidationConfig`
(`n_windows`, `in_sample_ratio`, `mode`, `purge_candles`, `n_monte_carlo`,
`monte_carlo_method`, `random_seed`, `robustness_grid`,
`robustness_max_combinations`, `robustness_metric`) : la CLI ne fait que
recopier la configuration et n'invente aucun paramètre supplémentaire.

Seuils gelés : `is_robust = (positive_ratio >= 0.7) and (robust_ratio >= 0.5)`
(`POSITIVE_RATIO_THRESHOLD` / `ROBUST_RATIO_THRESHOLD`), `CONSISTENCY_THRESHOLD
= 0.6`, `MIN_ROWS_PER_SLICE = 2`, `DEFAULT_MAX_COMBINATIONS = 512`.

Tous les tirages aléatoires passent un `random_seed` explicite : deux exécutions
identiques donnent des résultats identiques.

### 4.6 Métriques — `trading_backtest.metrics`

| Symbole | Signature clé | Rôle |
| --- | --- | --- |
| `MetricSet` | `@dataclass(frozen=True)` avec un unique champ `values: dict[str, float]`, plus le protocole de mapping (`__getitem__` qui lève `MetricsError` si le nom est inconnu, `get`, `keys`, `__len__`, `__iter__`, `__contains__`) | conteneur unique des métriques d'un run |
| `compute_metrics` | `compute_metrics(result: BacktestResult, *, timeframe: str = "1h", risk_free_rate: float = 0.0) -> MetricSet` | dérive les 23 métriques des trades et de la courbe d'equity |
| `metric_value` | `metric_value(result: BacktestResult, name: str, *, timeframe: str = "1h", risk_free_rate: float = 0.0) -> float` | une seule métrique, par nom — seam utilisé par `validation` |
| `METRIC_NAMES` | tuple de 23 noms, ordre gelé : `total_return`, `cagr`, `sharpe_ratio`, `sortino_ratio`, `max_drawdown`, `max_drawdown_duration`, `calmar_ratio`, `volatility`, `win_rate`, `profit_factor`, `expectancy`, `avg_trade_pnl`, `avg_win`, `avg_loss`, `largest_win`, `largest_loss`, `n_trades`, `exposure`, `best_trade_pct`, `worst_trade_pct`, `recovery_factor`, `total_fees`, `final_balance` | noms canoniques ; `TRADE_METRIC_NAMES` liste le sous-ensemble qui n'a de sens qu'avec au moins un trade |
| `MetricSet.to_dict` / `as_dict` | `to_dict() -> {"values": {...}}` (clés triées), `as_dict() -> dict[str, float]` | payload JSON-able |
| `drawdown_series`, `max_drawdown`, `drawdown_duration`, `drawdown_table` | module `trading_backtest.metrics.drawdown` : `drawdown_series(equity) -> pd.Series`, `max_drawdown(equity) -> float`, `drawdown_duration(equity) -> int`, `drawdown_table(equity, *, top: int = 5) -> list[dict]` | statistiques de drawdown réutilisables hors d'un `BacktestResult` |

### 4.7 Restitution — `trading_backtest.reporting`

| Symbole | Signature clé | Rôle |
| --- | --- | --- |
| `Report` | `@dataclass(frozen=True)` : `title`, `generated_at`, `summary`, `sections: list[ReportSection]`, `metadata` ; `to_dict()`, `to_markdown()`, `to_json()`, `section_titles`, `get_section(title)` | rapport complet, rendu markdown **ou** JSON |
| `ReportBuilder` | `ReportBuilder(*, title: str = "Backtest Report", config_echo=None, generated_at=None)`, puis `add_result` / `add_metrics` / `add_validation(name, payload)` / `add_trades(trades, *, limit=50)` / `add_equity(equity, *, max_points=200)` / `add_section(title, body, *, level=2)` / `add_note(text)` → `build() -> Report` | construction fluide d'un rapport |
| `build_report` | `build_report(*, result: BacktestResult, metrics=None, extras=None, title="Backtest Report", config_echo=None, generated_at=None, include_trades=True, trade_limit=50, include_equity=True) -> Report` | **tout mot-clé**, rend un objet `Report` (les métriques sont calculées paresseusement si `metrics` est omis) |
| `write_report` | `write_report(report: Report, output_dir: Path, *, formats: Sequence[str] = ("markdown", "json"), basename: str = "report") -> list[Path]` | écrit les formats demandés et rend leurs chemins ; `SUPPORTED_FORMATS = {"markdown": ".md", "json": ".json"}` |
| `read_report` | `read_report(path: str \| Path) -> dict[str, Any]` | relit un rapport JSON et lève `ReportingError` s'il est absent ou invalide |

### 4.8 CLI — `trading_backtest.cli`

| Commande | Options (toutes les commandes acceptent aussi `--json`) | Rôle |
| --- | --- | --- |
| `trading-backtest backtest` | `--config/-c` (obligatoire), `--symbol`, `--timeframe`, `--start`, `--end`, `--data-file`, `--output-dir`, `--formats`, `--no-network` | backtest unique |
| `trading-backtest walk-forward` | options de `backtest` + `--windows`, `--is-ratio`, `--mode`, `--metric` | walk-forward sur les fenêtres de `validation.make_windows` |
| `trading-backtest robustness` | `--config`, `--symbol`, `--timeframe`, `--data-file`, `--metric`, `--max-combinations`, `--output-dir`, `--formats`, `--no-network` (pas de `--start`/`--end`) | balayage paramétrique du `validation.robustness_grid` (grille vide ⇒ `strategy.PARAM_SPACE` de la stratégie) |
| `trading-backtest monte-carlo` | `--config`, `--symbol`, `--timeframe`, `--data-file`, `--simulations`, `--method`, `--seed`, `--output-dir`, `--formats` (pas de `--no-network`) | Monte Carlo sur les trades d'un backtest |
| `trading-backtest data download` | `--config`, `--symbol`, `--timeframe`, `--start`, `--end` (tous obligatoires) | téléchargement OHLCV vers le cache — seule commande qui peut sortir sur le réseau |
| `trading-backtest config show` / `config validate` | `--config/-c` | affiche la configuration effective / valide un fichier `AppConfig` **ou** Freqtrade (type détecté automatiquement) |

Le point d'entrée console est `trading-backtest = trading_backtest.cli:main`
(déclaré dans `pyproject.toml`) ; `python -m trading_backtest …`
(`__main__.py`) est strictement équivalent. Détail des options :
[`docs/usage.md`](usage.md#4-exemples-cli).

---

## 5. Contrat de données OHLCV

Toute fonction qui consomme des bougies (stratégies, moteur, validation,
métriques) suppose **exactement** le format suivant. Deux fonctions le
surveillent à la frontière, avec des rôles différents :

- `ensure_ohlcv` **normalise** : il rend toujours une copie conforme au contrat
  (tri croissant, doublons supprimés avec `keep="last"`, index naïf localisé en
  UTC et index tz-aware converti en UTC, index renommé `timestamp`, colonnes
  OHLCV castées en `float64`) et ne lève `DataValidationError` que sur les
  violations **structurelles** : pas un `DataFrame`, colonne requise absente,
  index qui n'est pas un `DatetimeIndex`, colonne non convertible en `float64` ;
- `validate_ohlcv` **contrôle** : il rapporte (et, avec `raise_on_error=True`,
  refuse) les `NaN`, prix non positifs, volume non positif, index naïf, index
  non trié, doublons, trous au-delà de `max_gap_factor` et `missing_ratio`
  au-delà de `max_missing_ratio`. C'est le mode utilisé par la CLI quand
  `data.validate` vaut `true` (défaut).

| Élément | Contrainte | Qui la fait respecter |
| --- | --- | --- |
| Index | `pandas.DatetimeIndex` **tz-aware UTC**, nommé `timestamp` | `ensure_ohlcv` (localise un index naïf en UTC, convertit un index tz-aware en UTC, renomme l'index) ; `validate_ohlcv` signale un index naïf |
| Colonnes | `open`, `high`, `low`, `close`, `volume` — toutes `float64` | `ensure_ohlcv` (cast `float64`) ; `validate_ohlcv` signale les colonnes absentes |
| Ordre | croissant (`DatetimeIndex.is_monotonic_increasing`) | `ensure_ohlcv` trie ; `validate_ohlcv` signale un index non trié |
| Doublons | aucun horodatage dupliqué | `ensure_ohlcv` déduplique (`keep="last"`) ; `validate_ohlcv` les compte |
| Valeurs manquantes | aucun `NaN` dans les colonnes OHLCV | `validate_ohlcv` uniquement (`ensure_ohlcv` ne rejette pas un `NaN`) |
| Continuité | les trous supérieurs à `max_gap_factor` et `missing_ratio > max_missing_ratio` sont signalés | `validate_ohlcv` |
| Cohérence | `low <= min(open, close)` et `high >= max(open, close)` sur chaque bougie | **non vérifié mécaniquement** : ni `ensure_ohlcv` ni `validate_ohlcv` ne testent cette règle — c'est un contrôle manuel (voir [`docs/backtesting-methodology.md`](backtesting-methodology.md#2-contrôles-qualité-des-données-avant-toute-chose)) |
| Mutabilité | un consommateur ne modifie jamais le `DataFrame` reçu en argument | convention respectée par `ensure_ohlcv` et les stratégies (copie systématique) |

Exemple minimal valide :

```python
import pandas as pd

index = pd.date_range("2022-01-01", periods=500, freq="1h", tz="UTC", name="timestamp")
# colonnes : open, high, low, close, volume en float64
```

Les tests n'utilisent que des données synthétiques
(`trading_backtest.data.synthetic`) ou des CSV de `tests/fixtures/` : **aucun
téléchargement réel** dans la suite de tests.

---

## 6. Modèles de domaine — `trading_backtest.core`

Deux modèles structurent tout le projet. Ce sont des **dataclasses gelées**
(`frozen=True`) ou quasi immuables, sans dépendance autre que `pandas`, et
sérialisables via `to_dict()` / reconstruites via `from_dict()`.

### 6.1 `TradeRecord`

Un aller-retour complet (une position, de l'entrée à la sortie).
`@dataclass(frozen=True)`.

| Champ | Type | Sens |
| --- | --- | --- |
| `entry_time` | `pandas.Timestamp` (UTC) | horodatage de la bougie d'exécution d'entrée |
| `exit_time` | `pandas.Timestamp` (UTC) | horodatage de la bougie de sortie |
| `entry_price` | `float` | prix d'entrée (open de la bougie suivant le signal) |
| `exit_price` | `float` | prix de sortie |
| `size` | `float` | taille de la position |
| `direction` | `Direction` | `long` ou `short` (le squelette est *long only* par défaut) |
| `pnl` | `float` | P&L absolu après frais |
| `pnl_pct` | `float` | P&L relatif au capital engagé |
| `fees` | `float` | frais payés sur les deux jambes |
| `exit_reason` | `ExitReason` | `stop_loss`, `take_profit`, `signal`, `end_of_data`, `max_duration` |
| `duration_minutes` | `float` | durée de détention |
| `stop_price` | `float \| None` | niveau de stop (statique, fixé à l'entrée) |
| `take_profit_price` | `float \| None` | niveau de take-profit, si défini |
| `params_id` | `str` | identifiant des paramètres utilisés (utile en balayage) |

`to_dict()` convertit les timestamps par `isoformat()` et les enums par leur
`.value` ; `from_dict()` est l'inverse exact.

### 6.2 `BacktestResult`

`@dataclass(eq=False)` — l'égalité est implémentée explicitement pour comparer
les courbes d'equity élément par élément.

| Champ | Type | Sens |
| --- | --- | --- |
| `strategy_name` | `str` | nom de la stratégie résolue (`basic`…) |
| `symbol` | `str` | symbole tradé (`BTC/USDT`) |
| `timeframe` | `str` | timeframe des bougies |
| `start` / `end` | `pandas.Timestamp` (UTC) | bornes effectives de la période testée |
| `initial_balance` | `float` | capital de départ |
| `final_balance` | `float` | capital final |
| `trades` | `list[TradeRecord]` | trades clos, ordonnés |
| `equity_curve` | `pandas.Series` | equity (`float64`, nommée `equity`) indexée `timestamp` en UTC — normalisée à la construction |
| `params` | `dict[str, Any]` | paramètres effectifs de la stratégie |
| `metadata` | `dict[str, Any]` | informations libres du run |
| `n_trades` | propriété `int` | nombre de trades |
| `is_empty` | propriété `bool` | `True` si aucun trade n'a été produit |

**Les métriques ne sont pas un champ du résultat** : elles sont dérivées à la
demande par `compute_metrics(result) -> MetricSet`. C'est ce qui permet à
`validation` de manipuler un `BacktestResult` sans dépendre de `metrics`.

Les résultats de validation (`WalkForwardResult`, `RobustnessResult`,
`MonteCarloResult`) suivent le même principe : un modèle typé **plus** un
`to_dict()` JSON-able.

### 6.3 Erreurs — `trading_backtest.core.errors`

Toute erreur levée par le projet dérive de `TradingBacktestError`, ce qui donne
à la CLI une surface d'erreur unique. Le message porte une description courte,
et les erreurs de validation exposent leurs problèmes détaillés dans
`issues: tuple[str, ...]`.

```
TradingBacktestError
├── ConfigError
│   └── FreqtradeConfigError
├── DataError
│   ├── DataValidationError      # violation du contrat OHLCV (§5)
│   ├── InsufficientDataError    # historique trop court pour la fenêtre demandée
│   └── DataDownloadError        # échec de téléchargement exchange
├── StrategyError
├── ValidationLayerError
│   ├── WalkForwardError
│   ├── RobustnessError
│   └── MonteCarloError
├── MetricsError
└── ReportingError
```

Aucun module ne lève d'exception « nue » : les erreurs de bas niveau de pandas
ou de pydantic sont converties à la frontière de la couche concernée.

---

## 7. Les deux coutures (*seams*)

L'architecture repose sur deux points d'injection volontaires. Ce ne sont pas
des détails : ce sont eux qui rendent le projet développable en parallèle et
testable hors ligne.

### 7.1 `RunnerFn` — l'exécution est injectée

`walk_forward(runner, data, …)` et `parameter_sweep(runner, data, grid, …)`
acceptent n'importe quel appelable conforme à `RunnerFn` ; l'exécuteur par
défaut est fourni par `make_runner(cfg)`. Le moteur lui-même
(`run_backtest`, `run_backtest_on_config`) n'a **pas** de paramètre `runner` :
il *est* le runner.

**Pourquoi :**

- **Implémentation concurrente** — la couche `validation` peut être écrite et
  testée contre un `RunnerFn` factice avant que le moteur réel ne soit complet.
- **Tests hors ligne et déterministes** — un runner factice rend un
  `BacktestResult` synthétique en quelques microsecondes : pas de données
  réelles, pas de hasard, pas de réseau.
- **Extensibilité** — brancher un exécuteur vectorisé, un exécuteur
  `freqtrade.backtesting` ou un exécuteur distribué ne demande aucune
  modification de `validation` ni de `cli`.

Le **même principe** s'applique à l'accès au marché :
`OHLCVLoader(provider=...)` accepte n'importe quel `MarketDataProvider`, et
`CsvDataProvider` fournit l'implémentation hors ligne. C'est ce qui permet de
documenter et de tester `data` sans réseau.

### 7.2 `to_dict()` — les payloads sont JSON-ables

Chaque modèle exposé (`TradeRecord`, `BacktestResult`, `MetricSet`, résultats de
validation, rapport) sait se sérialiser en structures Python natives ; la
configuration s'appuie sur `AppConfig.model_dump()` (pydantic).

**Pourquoi :**

- **Frontière stable** — `reporting` et `cli` ne dépendent pas de la forme
  interne des modèles : ils manipulent des dictionnaires.
- **Parallélisation** — un payload JSON-able traverse sans friction un process
  (`multiprocessing`), un fichier ou un futur worker distant : c'est la
  condition d'un balayage paramétrique scalable.
- **Tests** — un test peut comparer deux dictionnaires, snapshoter un rapport
  ou vérifier qu'aucun `NaN`/`Timestamp` non sérialisable ne fuit.

Corollaire : **aucun objet du domaine ne rend un `DataFrame` brut dans un
payload public**. Les séries (equity, drawdown) sont converties en listes ou en
paires `(timestamp, valeur)` sérialisables.

---

## 8. Points d'extension

| Besoin | Point d'extension |
| --- | --- |
| Nouvelle stratégie | hériter de `Strategy` (en implémentant `prepare` et `signals`, et en déclarant `ParamsModel` / `PARAM_SPACE`) et décorer la classe avec `@register_strategy`, puis référencer `cls.name` dans `strategy.name` de la configuration |
| Nouvel indicateur | fonction pure ajoutée à `trading_backtest.strategy.indicators`, sans effet de bord |
| Nouvelle métrique | entrée ajoutée au dictionnaire `values` de `compute_metrics` + nom ajouté à `METRIC_NAMES` (l'ordre de `METRIC_NAMES` est l'ordre de calcul ; le tableau du rapport markdown trie les clés par ordre alphabétique) |
| Nouvel exécuteur | fonction conforme à `RunnerFn`, passée aux fonctions de validation (`walk_forward`, `parameter_sweep`) à la place de `make_runner(cfg)` |
| Nouveau mode de validation | nouveau module dans `trading_backtest.validation`, exposé par la CLI, sans toucher au moteur |
| Nouvel exchange | implémentation dans `trading_backtest.data`, l'import `ccxt` restant paresseux |
| Nouveau format de rapport | branche supplémentaire dans `write_report`, activée par `reporting.formats` |

---

## 9. Ce que l'architecture ne fait pas (encore)

Le squelette est volontairement minimal **et honnête** sur ses limites : une
seule position à la fois, stop statique fixé à l'entrée, sans levier, sans coût
de funding ni de borrowing, et des données synthétiques dans les tests. Le
détail des hypothèses d'exécution et de leurs conséquences sur
l'interprétation des résultats est dans
[`docs/backtesting-methodology.md`](backtesting-methodology.md).

Ajouter une brique (levier, multi-positions, coûts de financement) se fait
**dans la couche concernée** (respectivement `strategy.engine`, `strategy.engine`,
`metrics`), jamais en travers des couches.
