# Architecture

Ce document décrit l'architecture du squelette de backtesting : l'arborescence
livrée, les couches et leurs règles d'import, l'inventaire **gelé** des
interfaces publiques, le contrat de données OHLCV, les modèles de domaine et les
trois « coutures » (*seams*) qui rendent le projet testable hors ligne.

Il complète :

- [`docs/backtesting-methodology.md`](backtesting-methodology.md) — protocole de validation ;
- [`docs/usage.md`](usage.md) — installation, CLI, rapports, Docker ;
- [`docs/testing-policy.md`](testing-policy.md) — politique de tests et seuil de couverture.

---

## 1. Objectifs d'architecture

1. **Modulaire** — chaque responsabilité (données, stratégie, exécution,
   validation, métriques, restitution, orchestration) vit dans un paquet isolé
   — l'exécution est le sous-module `trading_platform.strategy.engine` — et ne
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
│   ├── freqtrade_dryrun.json         # variante dry-run de la config Freqtrade
│   └── profiles.example.json         # profils temps réel (profiles + realtime + monitoring)
├── docs/
│   ├── architecture.md               # ce document
│   ├── backtesting-methodology.md    # protocole de validation statistique
│   ├── realtime.md                   # temps réel multi-profils : profils, sûreté, API, limites
│   ├── usage.md                      # guide d'usage (install, CLI, rapports, Docker)
│   └── testing-policy.md             # politique de tests et de couverture (gelé)
├── src/trading_platform/
│   ├── __init__.py                   # exports publics et version du paquet
│   ├── __main__.py                   # point d'entrée `python -m trading_platform`
│   ├── core/
│   │   ├── constants.py              # constantes partagées (timeframes, colonnes OHLCV, UTC)
│   │   ├── models.py                 # modèles de domaine : TradeRecord, BacktestResult, RunnerFn
│   │   └── errors.py                 # hiérarchie d'exceptions du projet (dont la branche RealtimeError)
│   ├── config/
│   │   ├── models.py                 # configuration typée (pydantic) : AppConfig et sous-modèles
│   │   └── loader.py                 # chargement et validation d'un fichier JSON de configuration
│   ├── data/
│   │   ├── cache.py                  # OHLCVCache : cache disque des bougies
│   │   ├── loader.py                 # OHLCVLoader + providers (ccxt paresseux, CSV hors ligne)
│   │   ├── validation.py             # contrôles qualité OHLCV (tri, doublons, NaN, trous)
│   │   └── synthetic.py              # générateur de données synthétiques déterministes (tests)
│   ├── forecast/                     # layer 2.5: offline forecasting (backends, parquet artifact, skill report)
│   │   ├── series.py                 # de-seasonalisation + seasonal period per timeframe (candles_per_day, resolve_seasonal_period)
│   │   ├── artifact.py               # ForecastBuildConfig / build_forecast_artifact / ForecastStore + sidecar metadata
│   │   ├── bootstrap.py              # ensure_forecast_backends: the offline backends are importable, or one loud error
│   │   └── bootstrap_profile.py      # bootstrap_profile_forecast: build the artifact a realtime profile declares
│   ├── strategy/
│   │   ├── indicators.py             # EMA, RSI, ATR — calculs purs, sans dépendance externe
│   │   ├── base.py                   # Strategy : contrat prepare / signals / run
│   │   ├── basic.py                  # BasicStrategy : croisement EMA + filtre RSI + stop ATR
│   │   ├── timesfm_forecast.py       # TimesfmForecastStrategy : entries/exits driven by the forecast artifact
│   │   ├── features.py               # feature-injection seam: resolves the forecast artifact into a ForecastStore
│   │   ├── registry.py               # register_strategy / get_strategy (résolution par nom)
│   │   ├── engine.py                 # run_backtest / make_runner : boucle d'exécution + seam RunnerFn
│   │   ├── freqtrade_parameters.py   # traduction ParamsModel / PARAM_SPACE -> IntParameter, DecimalParameter, CategoricalParameter
│   │   ├── freqtrade_stoploss.py     # stop-loss par bougie -> stop Freqtrade : table d'entrée + ratio par trade
│   │   ├── freqtrade_adapter.py      # make_freqtrade_strategy : fabrique une IStrategy générique depuis une Strategy du registre
│   │   └── freqtrade_basic.py        # BasicFreqtradeStrategy : la classe concrète de `basic`, chargeable par nom
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
│   ├── realtime/
│   │   ├── __init__.py               # exports publics de la couche 6 (classes moteur paresseuses)
│   │   ├── models.py                 # vocabulaire gelé : dataclasses gelées + to_dict() de chaque payload
│   │   ├── clock.py                  # couture temps : Clock, SystemClock, ManualClock
│   │   ├── stream.py                 # couture marché : MarketStream + Replay/Polling/CcxtPro/Composite
│   │   ├── store.py                  # couture état : StateStore + SqliteStateStore (schéma et migration)
│   │   ├── broker.py                 # couture lieu : Broker + PaperBroker + CcxtBroker
│   │   ├── credentials.py            # ExchangeCredentials : résolution par environnement et masquage
│   │   ├── risk.py                   # RiskLimits / RiskManager / KillSwitch / LiveTradingGate
│   │   ├── gateway.py                # ExecutionGateway : l'UNIQUE cycle de vie d'un ordre (paper = live)
│   │   ├── strategies.py             # résolution de stratégie + pont IStrategy Freqtrade (paresseux)
│   │   ├── features.py               # profile forecast bundle + startup guard (coverage, symbol, timeframe)
│   │   ├── runner.py                 # ProfileRunner : un profil, warm-up, boucle par bougie, santé
│   │   ├── orchestrator.py           # RealtimeOrchestrator : N profils, supervision, câblage, kill switch
│   │   ├── monitor.py                # read model : ProfileReport, equity, métriques, benchmark, santé
│   │   └── observability.py          # journalisation JSON structurée, filtre de masquage, compteurs
│   ├── web/                          # API-only layer 7: no static asset is served
│   │   ├── __init__.py               # exports publics de la couche 7 (bibliothèque standard seule)
│   │   ├── routes.py                 # routage pur requête -> réponse + couture SnapshotProvider
│   │   └── server.py                 # ThreadingHTTPServer, create_server, serve, start_in_thread
│   └── cli.py                        # CLI Typer (couche 8) : backtest, walk-forward, robustness,
│                                     # monte-carlo, forecast-build / forecast-bootstrap /
│                                     # forecast-skill / forecast-info,
│                                     # data, config et `realtime run|serve|check`
├── tests/                            # suite pytest hors ligne (voir docs/testing-policy.md)
├── dashboard/                        # standalone Next.js dashboard (App Router, Tailwind v4, 2 s polling)
├── user_data/
│   ├── README.md                     # état écrit par Freqtrade (OHLCV, backtests, stratégies) — git-ignoré
│   └── strategies/
│       └── BasicStrategy.py          # shim suivi : la SEULE exception à user_data/ git-ignoré (§4.9)
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
(`trading_platform.<paquet>`) et les symboles listés au §4.

**`user_data/` reste l'état de Freqtrade** — OHLCV téléchargé, résultats de
backtest, bases SQLite, stratégies engendrées — et demeure **git-ignoré** : la
seule exception suivie est `user_data/strategies/BasicStrategy.py`, le shim
d'exposition décrit au §4.9. Ce fichier n'embarque **aucune** logique métier :
un import et une déclaration de classe d'une ligne. Tout le reste de
`user_data/` est produit par Freqtrade à l'exécution et n'est jamais versionné.

---

## 3. Couches et règle de dépendance

```
        ┌──────────────────────────────────────────┐
  8     │                  cli                     │   orchestration, entrées/sorties
        └──────────────────────────────────────────┘
                            │
        ┌────────────────────┬─────────────────────┐
  7     │        web         │   HTTP JSON API     │   transport — bibliothèque standard seule
        └────────────────────┴─────────────────────┘
                            │
        ┌──────────────────────────────────────────┐
  6     │               realtime                   │   moteur : flux, courtier, passerelle, risque, état
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
        ┌──────────────────────────────────────────┐
  2.5   │                forecast                  │   offline forecasting: backends, artifact, skill report
        └──────────────────────────────────────────┘
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
| 1 | `trading_platform.core` | la bibliothèque standard, `pandas` |
| 2 | `trading_platform.config`, `trading_platform.data`, `trading_platform.freqtrade` | couche 1 |
| 2.5 | `trading_platform.forecast` | layer 1 **only** — standard library + `numpy` / `pandas` / `pyarrow`; `torch`, `timesfm` and `jax` stay optional and are imported lazily inside functions |
| 3 | `trading_platform.strategy` (dont `strategy.engine` et `strategy.freqtrade_*`), `trading_platform.metrics` | couches 1–2.5 (`strategy.engine` importe `config` ; `strategy.timesfm_forecast` importe `forecast` ; `strategy.freqtrade_*` importe `freqtrade` ; `metrics` n'importe que `core`) |
| 4 | `trading_platform.reporting` | couches 1–3 (`core` et `metrics`) |
| 5 | `trading_platform.validation` | couches 1–4 (`core`, `data.validation`, `metrics` importé paresseusement) |
| 6 | `trading_platform.realtime` | couches 1–5 (moteur temps réel : flux, courtier, passerelle, risque, état, read model ; `ccxt` reste paresseux) ; it also imports the `strategy.features` seam to inject a profile's forecast artifact (`realtime/features.py`), and the forecast startup guard lives in the same layer |
| 7 | `trading_platform.web` | layers 1-6, standard library only (HTTP JSON API; no HTML, no static asset - it only knows the SnapshotProvider seam) |
| 8 | `trading_platform.cli` | couches 1–7 (plus `freqtrade` pour valider une config Freqtrade) |

> **`trading_platform.cli` a déménagé de la couche 6 à la couche 8.** La CLI
> importe désormais `realtime` (couche 6) et `web` (couche 7) dans le corps de ses
> commandes : la garder en couche 6 aurait été un import **ascendant interdit**.
> Les couches 1 à 5 sont inchangées, seul le haut de la pile a grandi. La règle
> est **mécaniquement testée** : `tests/test_cli_realtime.py` prouve dans un
> sous-processus qu'importer `trading_platform.cli` ne met **ni**
> `trading_platform.realtime` **ni** `trading_platform.web` dans `sys.modules`.

Conséquences pratiques :

- `core` n'importe **rien** du projet : c'est le vocabulaire commun.
- `forecast` is the **offline prediction** layer (2.5): it may import `core` and
  the scientific stack only, and it is the *only* place where a forecasting
  backend (including TimesFM) can be instantiated. It never imports `strategy`,
  `config` or the CLI, so the direction `core -> forecast -> strategy -> cli`
  stays strictly downward. `torch`, `timesfm` and `jax` are imported inside
  functions, which keeps the whole suite green with the `.[dev]` extra alone.
- Une stratégie ne connaît ni le cache disque ni la CLI : elle reçoit un
  `DataFrame` OHLCV et rend des signaux. The `timesfm` strategy additionally
  receives **pre-computed** forecast trajectories through the feature-injection
  seam (`trading_platform.strategy.features`): it never runs the model itself,
  never reads the artifact inside `signals()`, and never touches the network.
- `validation` orchestre un exécuteur *injecté* (`RunnerFn`) et `metrics`, jamais
  l'inverse : elle n'importe **jamais** `strategy.engine` — c'est l'appelant
  (la CLI) qui construit le runner avec `make_runner(cfg)`. Le moteur, lui,
  ignore ce qu'est un walk-forward.
- `realtime` **réutilise** les couches basses au lieu de les réimplémenter :
  stratégies du registre, `data.loader` / `data.validation`, `metrics`,
  `validation`, `config` et le pont Freqtrade. Il n'écrit **aucune** formule. It
  likewise reuses the **feature-injection seam** (`strategy.features`,
  §4.10.2): a profile's forecast artifact is loaded once, at startup, by
  `strategy.features.resolve_features` — never by a second loader, never per
  candle.
- `web` n'importe **jamais** l'orchestrateur : il ne connaît de la plateforme que
  la couture `SnapshotProvider`, ce qui lui permet de servir un état persisté
  alors qu'aucun moteur ne tourne (`realtime serve`).
- L'ordre « `metrics` **sous** `validation` » est structurel : `validation`
  note ses fenêtres avec `metrics.metric_value`, importé paresseusement dans le
  corps des fonctions.
- Seule la CLI a le droit de parler à l'utilisateur (sortie terminal, fichiers
  de rapport) et de lire des arguments.

### 3.1 Décision de couches — l'adaptateur Freqtrade ne crée **aucune** arête

L'adaptateur Freqtrade (§4.9) est la brique 1 de la trajectoire vers le temps
réel multi-profils, et la brique 2 est arrivée : `trading_platform.realtime`
(couche 6) et `trading_platform.web` (couche 7) existent désormais. Ce qui change
ici, et pourquoi :

- le bloc de l'adaptateur lui-même reste **en couche 3** : les quatre modules
  `trading_platform.strategy.freqtrade_parameters`, `freqtrade_stoploss`,
  `freqtrade_adapter` et `freqtrade_basic` ne bougent pas, et
  `trading_platform.strategy` -> `trading_platform.freqtrade` reste un import
  **descendant** (couche 3 -> couche 2), parfaitement légal ;
- `trading_platform.freqtrade` **conserve sa règle de liaison actuelle** : ce
  paquet n'importe que `trading_platform.core` et **n'importe jamais
  `freqtrade`** (c'est son docstring). Déplacer l'adaptateur *dans*
  `trading_platform.freqtrade` serait un import **ascendant** (couche 2 ->
  couche 3) et falsifierait ce docstring : **c'est refusé** ;
- **les seules arêtes ajoutées** sont `realtime` -> `strategy` / `data` /
  `metrics` (couche 6 -> couches 3 et 2, donc **descendantes**) et
  `web` -> `realtime` (couche 7 -> couche 6) ; `cli` passe de la couche 6 à la
  couche 8, ce qui rend son import de `realtime` et de `web` **descendant** lui
  aussi. Aucune arête montante n'apparaît ;
- l'import du paquet externe `import freqtrade` est **paresseux et gardé** : il
  est exécuté *dans le corps des fonctions* (`freqtrade_available`,
  `make_freqtrade_strategy`, `validate_freqtrade_adapter_class`,
  `realtime.strategies.freqtrade_strategy_for`), jamais au chargement d'un
  paquet. Conséquence directe : avec le seul extra `.[dev]` — donc **sans**
  Freqtrade installé — les paquets restent importables,
  `freqtrade_available()` rend `False`, et la suite de tests complète reste verte
  ([`docs/testing-policy.md`](testing-policy.md)). `ccxt` suit exactement la même
  règle : il n'est importé que dans le corps de `CcxtBroker` et de
  `CcxtProMarketStream`.

Autrement dit : la logique métier (indicateurs, règles d'entrée/sortie) reste
écrite **une seule fois** dans `trading_platform.strategy`, et l'adaptateur comme
le moteur temps réel se contentent de **traduire** entre les deux contrats. Ils
ne dupliquent rien.

---

## 4. Inventaire des interfaces gelées

Ces symboles et signatures constituent le contrat entre paquets. Toute
évolution doit être répercutée ici **dans la même modification**.

### 4.1 Configuration — `trading_platform.config`

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

### 4.2 Données — `trading_platform.data`

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

### 4.3 Exécution — `trading_platform.strategy.engine`

| Symbole | Signature clé | Rôle |
| --- | --- | --- |
| `RunnerFn` | `Callable[[pandas.DataFrame, Mapping[str, Any] \| None], BacktestResult]` | type de la fonction d'exécution injectable : `runner(data, params) -> BacktestResult`, où `params=None` signifie « paramètres par défaut de la stratégie » |
| `make_runner` | `make_runner(cfg: AppConfig, *, symbol: str \| None = None) -> RunnerFn` | construit l'exécuteur par défaut à partir de la configuration (`symbol=None` laisse `UNKNOWN/USDT`) |
| `run_backtest` | `run_backtest(strategy, data, *, initial_balance=10000.0, fee_rate=0.001, slippage=0.0, stake_amount=None, symbol="UNKNOWN/USDT", timeframe="1h", allow_short=None, params_id="") -> BacktestResult` | exécute une **instance** de stratégie sur une frame (`strategy` et `data` sont positionnels) |
| `run_backtest_on_config` | `run_backtest_on_config(cfg: AppConfig, data, *, params=None, symbol=None, features=None) -> BacktestResult` | exécute la stratégie décrite par la configuration : `params` surcharge `cfg.strategy.params`, le reste vient de `cfg.backtest.*` / `cfg.exchange.*` / `cfg.data.timeframe`. `features` is the **keyword-only** injection seam for external features (`None` = default resolution from the configuration, i.e. the artifact of `cfg.forecast.artifact` through `trading_platform.strategy.features`) |

### 4.4 Stratégies — `trading_platform.strategy`

| Symbole | Signature clé | Rôle |
| --- | --- | --- |
| `Strategy` | classe de base abstraite ; `name: ClassVar[str]`, `ParamsModel: ClassVar[type[StrategyParams]]`, `PARAM_SPACE: ClassVar[dict[str, list[float \| int]]]`, propriété `params` | contrat d'une stratégie |
| `Strategy.prepare` | `prepare(data: pd.DataFrame) -> pd.DataFrame` | ajoute les colonnes d'indicateurs (pur, sans mutation de l'entrée) |
| `Strategy.signals` | `signals(data: pd.DataFrame) -> pd.DataFrame` | ajoute les colonnes de signal (`entry_long`, `exit_long`, `entry_short`, `exit_short`, `stop_loss` ; le squelette est *long only* par défaut, les colonnes *short* restent déclarées) |
| `Strategy.run` | `run(data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]` | rend `(prepared, signals)` — le point d'entrée du moteur |
| `BasicStrategy` | `name = "basic"` ; paramètres `ema_fast=9`, `ema_slow=21`, `rsi_period=14`, `rsi_min=30.0`, `rsi_max=70.0`, `atr_period=14`, `atr_stop_multiplier=2.0`, `allow_short=False` | croisement EMA + filtre RSI + stop ATR (`stop_loss` = `close − atr_stop_multiplier × ATR` sur la bougie de signal) |
| `TimesfmForecastStrategy` | `name = "timesfm"`; `TimesFMForecastParams` (pydantic) + `PARAM_SPACE` | strategy driven by the forecast artifact: AND-composed entries, a priority-ordered exit decision tree, the `forecast_*` diagnostic columns and `exit_code` (§4.14) |
| `register_strategy` | `register_strategy(cls: type[Strategy]) -> type[Strategy]` | décorateur de classe qui enregistre la stratégie dans `STRATEGIES` |
| `get_strategy` | `get_strategy(name: str, params: Mapping[str, Any] \| None = None) -> Strategy` | instancie une stratégie enregistrée |
| `strategy_names` / `strategy_param_space` | `strategy_names() -> list[str]`, `strategy_param_space(name: str) -> dict[str, list[float \| int]]` | noms disponibles et grille de paramètres d'une stratégie (base du balayage de robustesse) |
| `make_freqtrade_strategy` | `make_freqtrade_strategy(house_name: str, …) -> type[IStrategy]` | **fabrique générique** : rend une classe `IStrategy` Freqtrade pour **n'importe quelle** stratégie du registre (§4.9) |
| `validate_freqtrade_adapter_class` | `validate_freqtrade_adapter_class(cls) -> None` | vérifie que la classe produite est une `IStrategy` valide (version d'interface, les trois `populate_*`, attributs requis) et lève `StrategyError` sinon ; rend `None` quand tout est conforme |
| `freqtrade_available` | `freqtrade_available() -> bool` | `True` si le paquet externe `freqtrade` est importable — import **paresseux**, jamais au chargement du paquet |
| `BasicFreqtradeStrategy` | `make_freqtrade_strategy("basic")` ; `BASIC_FREQTRADE_STRATEGY_NAME = "BasicStrategy"` | la classe concrète chargeable par nom qui expose la stratégie maison `basic` à Freqtrade |

### 4.4.1 Inventaire de l'adaptateur Freqtrade — `trading_platform.strategy.freqtrade_*`

Ces symboles sont **gelés** comme les précédents. Ils appartiennent tous à la
couche 3 et ne sont utilisés que par le chemin d'exposition à Freqtrade (§4.9).

| Symbole | Module | Rôle |
| --- | --- | --- |
| `FREQTRADE_INTERFACE_VERSION` | `freqtrade_adapter` | `3` — la valeur d'`INTERFACE_VERSION` déclarée par la classe produite (contrat vérifié contre Freqtrade 2026.8) |
| `FREQTRADE_ORDER_COLUMNS` | `freqtrade_adapter` | tuple des colonnes de signal **Freqtrade** : `enter_long`, `exit_long`, `enter_short`, `exit_short` |
| `SIGNAL_TO_FREQTRADE_COLUMNS` | `freqtrade_adapter` | table de renommage maison -> Freqtrade : `entry_long -> enter_long`, `exit_long -> exit_long`, `entry_short -> enter_short`, `exit_short -> exit_short` |
| `house_frame` | `freqtrade_adapter` | traduit une frame Freqtrade (index positionnel `RangeIndex` + colonne `date`) vers le contrat OHLCV maison (`DatetimeIndex` UTC nommé `timestamp`) |
| `freqtrade_indicator_frame` / `freqtrade_entry_frame` / `freqtrade_exit_frame` | `freqtrade_adapter` | traduisent la frame maison vers la frame rendue par chacun des trois `populate_*` : mêmes lignes, même ordre, index positionnel préservé, colonnes de signal renommées |
| `FreqtradeParamSpec` | `freqtrade_parameters` | description d'**un** paramètre traduit : nom, type (`IntParameter` / `DecimalParameter` / `CategoricalParameter`), bornes, défaut, espace, décimales |
| `freqtrade_param_specs` | `freqtrade_parameters` | dérive la liste des `FreqtradeParamSpec` d'une stratégie maison (depuis `ParamsModel` et `PARAM_SPACE`) |
| `freqtrade_parameter_attributes` | `freqtrade_parameters` | rend le dictionnaire d'attributs de classe (`{"ema_fast": IntParameter(...), …}`) injecté dans l'espace de noms de la classe engendrée |
| `startup_candle_count_for` | `freqtrade_parameters` | dérive `startup_candle_count` des paramètres de la stratégie (périodes d'indicateurs) au lieu de le laisser à `0` |
| `DEFAULT_FREQTRADE_STOPLOSS` | `freqtrade_stoploss` | `-0.99` — l'attribut global `stoploss` de la classe engendrée : une borne dure, jamais le stop réellement utilisé |
| `entry_stop_map` | `freqtrade_stoploss` | table (mémoire de process) des stops **par bougie de signal** construite pendant la traduction de la frame de signaux |
| `signal_candle_for` | `freqtrade_stoploss` | retrouve la bougie de signal d'un trade (`trade.open_date_utc` décalée d'une bougie) |
| `lookup_entry_stop` | `freqtrade_stoploss` | lit le stop associé à cette bougie dans `entry_stop_map` (trois clés essayées : bougie de signal, bougie d'exécution, horodatage brut) et rend `None` si rien ne correspond — Freqtrade retombe alors sur son attribut `stoploss` |
| `stoploss_ratio_from_absolute` | `freqtrade_stoploss` | convertit un **prix** de stop absolu (colonne `stop_loss`) en **ratio** relatif au prix courant : délègue à l'helper natif `freqtrade.strategy.stoploss_from_absolute(stop_rate, current_rate, is_short=…)` (seule unité acceptée par `custom_stoploss`) |
| `freqtrade_strategy_namespace` | `freqtrade_adapter` | construit l'espace de noms injecté dans `type(...)` : attributs d'instance gelés (`timeframe`, `can_short`, `minimal_roi`, `process_only_new_candles`, `use_custom_stoploss`, `INTERFACE_VERSION`…) et les trois `populate_*` |
| `BasicFreqtradeStrategy` | `freqtrade_basic` | la classe concrète de `basic` : `make_freqtrade_strategy("basic")`, chargeable par nom par Freqtrade |
| `BASIC_FREQTRADE_STRATEGY_NAME` | `freqtrade_basic` | `"BasicStrategy"` — le nom visible côté Freqtrade, celui de `config/freqtrade_config.json` et `config/freqtrade_dryrun.json` |

**Note de nommage.** La conversion « stop absolu -> ratio » existe en deux
exemplaires : l'**helper natif de Freqtrade** `stoploss_from_absolute`
(`freqtrade.strategy.stoploss_from_absolute(stop_rate, current_rate,
is_short=False, leverage=1.0)`, vérifié en 2026.8) et le **wrapper maison**
`stoploss_ratio_from_absolute`, qui n'est qu'une délégation vérifiée à
l'helper natif — avec l'erreur explicite `StrategyError` quand l'extra
`freqtrade` est absent. Le token `stoploss_from_absolute` employé dans cette
documentation désigne donc bien l'API **amont**, pas un symbole maison
supplémentaire.

### 4.5 Validation — `trading_platform.validation`

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

### 4.6 Métriques — `trading_platform.metrics`

| Symbole | Signature clé | Rôle |
| --- | --- | --- |
| `MetricSet` | `@dataclass(frozen=True)` avec un unique champ `values: dict[str, float]`, plus le protocole de mapping (`__getitem__` qui lève `MetricsError` si le nom est inconnu, `get`, `keys`, `__len__`, `__iter__`, `__contains__`) | conteneur unique des métriques d'un run |
| `compute_metrics` | `compute_metrics(result: BacktestResult, *, timeframe: str = "1h", risk_free_rate: float = 0.0) -> MetricSet` | dérive les 23 métriques des trades et de la courbe d'equity |
| `metric_value` | `metric_value(result: BacktestResult, name: str, *, timeframe: str = "1h", risk_free_rate: float = 0.0) -> float` | une seule métrique, par nom — seam utilisé par `validation` |
| `METRIC_NAMES` | tuple de 23 noms, ordre gelé : `total_return`, `cagr`, `sharpe_ratio`, `sortino_ratio`, `max_drawdown`, `max_drawdown_duration`, `calmar_ratio`, `volatility`, `win_rate`, `profit_factor`, `expectancy`, `avg_trade_pnl`, `avg_win`, `avg_loss`, `largest_win`, `largest_loss`, `n_trades`, `exposure`, `best_trade_pct`, `worst_trade_pct`, `recovery_factor`, `total_fees`, `final_balance` | noms canoniques ; `TRADE_METRIC_NAMES` liste le sous-ensemble qui n'a de sens qu'avec au moins un trade |
| `MetricSet.to_dict` / `as_dict` | `to_dict() -> {"values": {...}}` (clés triées), `as_dict() -> dict[str, float]` | payload JSON-able |
| `drawdown_series`, `max_drawdown`, `drawdown_duration`, `drawdown_table` | module `trading_platform.metrics.drawdown` : `drawdown_series(equity) -> pd.Series`, `max_drawdown(equity) -> float`, `drawdown_duration(equity) -> int`, `drawdown_table(equity, *, top: int = 5) -> list[dict]` | statistiques de drawdown réutilisables hors d'un `BacktestResult` |

### 4.7 Restitution — `trading_platform.reporting`

| Symbole | Signature clé | Rôle |
| --- | --- | --- |
| `Report` | `@dataclass(frozen=True)` : `title`, `generated_at`, `summary`, `sections: list[ReportSection]`, `metadata` ; `to_dict()`, `to_markdown()`, `to_json()`, `section_titles`, `get_section(title)` | rapport complet, rendu markdown **ou** JSON |
| `ReportBuilder` | `ReportBuilder(*, title: str = "Backtest Report", config_echo=None, generated_at=None)`, puis `add_result` / `add_metrics` / `add_validation(name, payload)` / `add_trades(trades, *, limit=50)` / `add_equity(equity, *, max_points=200)` / `add_section(title, body, *, level=2)` / `add_note(text)` → `build() -> Report` | construction fluide d'un rapport |
| `build_report` | `build_report(*, result: BacktestResult, metrics=None, extras=None, title="Backtest Report", config_echo=None, generated_at=None, include_trades=True, trade_limit=50, include_equity=True) -> Report` | **tout mot-clé**, rend un objet `Report` (les métriques sont calculées paresseusement si `metrics` est omis) |
| `write_report` | `write_report(report: Report, output_dir: Path, *, formats: Sequence[str] = ("markdown", "json"), basename: str = "report") -> list[Path]` | écrit les formats demandés et rend leurs chemins ; `SUPPORTED_FORMATS = {"markdown": ".md", "json": ".json"}` |
| `read_report` | `read_report(path: str \| Path) -> dict[str, Any]` | relit un rapport JSON et lève `ReportingError` s'il est absent ou invalide |

### 4.8 CLI — `trading_platform.cli`

| Commande | Options (toutes les commandes acceptent aussi `--json`) | Rôle |
| --- | --- | --- |
| `trading backtest` | `--config/-c` (obligatoire), `--symbol`, `--timeframe`, `--start`, `--end`, `--data-file`, `--output-dir`, `--formats`, `--no-network` | backtest unique |
| `trading walk-forward` | options de `backtest` + `--windows`, `--is-ratio`, `--mode`, `--metric` | walk-forward sur les fenêtres de `validation.make_windows` |
| `trading robustness` | `--config`, `--symbol`, `--timeframe`, `--data-file`, `--metric`, `--max-combinations`, `--output-dir`, `--formats`, `--no-network` (pas de `--start`/`--end`) | balayage paramétrique du `validation.robustness_grid` (grille vide ⇒ `strategy.PARAM_SPACE` de la stratégie) |
| `trading monte-carlo` | `--config`, `--symbol`, `--timeframe`, `--data-file`, `--simulations`, `--method`, `--seed`, `--output-dir`, `--formats` (pas de `--no-network`) | Monte Carlo sur les trades d'un backtest |
| `trading forecast-build` | `--config/-c`, `--data-file`, `--out`, `--backend naive\|seasonal\|timesfm`, `--context`, `--horizon`, `--reforecast-every` | builds the forecast **parquet artifact** (plus its `*.meta.json` sidecar) from a candle file, offline and origin by origin |
| `trading forecast-skill` | `--artifact`, `--data-file` | offline skill report: RMSE/MAE/MASE against the random walk, decile coverage, directional accuracy at the horizon |
| `trading forecast-info` | `--artifact` | artifact metadata (schema, backend, model, span, licence) |
| `trading data download` | `--config`, `--symbol`, `--timeframe`, `--start`, `--end` (tous obligatoires) | téléchargement OHLCV vers le cache — seule commande qui peut sortir sur le réseau |
| `trading config show` / `config validate` | `--config/-c` | affiche la configuration effective / valide un fichier `AppConfig` **ou** Freqtrade (type détecté automatiquement) |
| `trading realtime run` | `--profiles/-p` (obligatoire), `--host`, `--port` (0 = port éphémère), `--once`, `--json` | démarre le moteur **et** le serveur de surveillance ; `--once` exécute **un** tick déterministe, écrit l'état et sort (aucun serveur) |
| `trading realtime serve` | `--profiles/-p` (obligatoire), `--host`, `--port`, `--json` | surveillance **lecture seule** sur l'état persisté, sans moteur ; les routes mutantes répondent 403 |
| `trading realtime check` | `--profiles/-p` (obligatoire), `--json` | pré-vol statique : validité de la config, présence des credentials, porte live, limites de risque, inscriptibilité de la base d'état ; ne passe **aucun** ordre et ne touche **pas** au réseau ; sortie `1` dès qu'un profil ne peut pas démarrer |

Les trois commandes `realtime` ont leurs propres ensembles de clés JSON
(`realtime-check`, puis `realtime-run`/`realtime-serve`) : elles ne passent pas
par `PAYLOAD_KEYS`. Ces payloads sont documentés mot pour mot dans
[`docs/realtime.md`](realtime.md#6-the-three-commands) et
[`docs/usage.md`](usage.md).

Le point d'entrée console est `trading = trading_platform.cli:main` (`trading-backtest` reste déclaré comme **alias** de la même fonction)
(déclaré dans `pyproject.toml`) ; `python -m trading_platform …`
(`__main__.py`) est strictement équivalent. Détail des options :
[`docs/usage.md`](usage.md#4-exemples-cli).

### 4.9 L'adaptateur Freqtrade — écrire une stratégie **une fois**, l'exposer deux fois

L'adaptateur est la brique 1 de la trajectoire vers le temps réel multi-profils
(un profil = actif + stratégie + timeframe + paper/live). Son principe tient en
une phrase : **les indicateurs et les règles d'entrée/sortie restent écrits dans
`trading_platform.strategy`**, et l'adaptateur ne fait que **traduire** entre
deux contrats — celui du moteur maison et celui de
`freqtrade.strategy.IStrategy`. Aucune règle métier n'est recopiée.

Les faits ci-dessous ont été **vérifiés contre l'interface réelle** de Freqtrade
2026.8 installé dans `.venv` (`INTERFACE_VERSION = 3`), jamais contre une
supposition.

#### 4.9.1 Le contrat de traduction

Freqtrade 2026.8 impose exactement trois méthodes de peuplement :

```
populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame
populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame
populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame
```

Les attributs de classe lus par Freqtrade sont `INTERFACE_VERSION`, `timeframe`,
`stoploss`, `can_short`, `minimal_roi`, `startup_candle_count`,
`use_custom_stoploss` et `process_only_new_candles`. La traduction faite par
l'adaptateur :

| Contrat maison | Contrat Freqtrade 2026.8 | Traitement |
| --- | --- | --- |
| colonne `entry_long` | colonne `enter_long` | **renommage** — `entry_long` n'existe pas côté Freqtrade, c'est le piège n°1 de ce contrat (`SIGNAL_TO_FREQTRADE_COLUMNS`) |
| colonne `exit_long` | colonne `exit_long` | nom inchangé |
| colonne `entry_short` | colonne `enter_short` | **renommage** (même piège, côté short) |
| colonne `exit_short` | colonne `exit_short` | nom inchangé |
| colonne `stop_loss` (par bougie) | attribut `stoploss` global | `stoploss = DEFAULT_FREQTRADE_STOPLOSS` (`-0.99`) : borne dure, **jamais** le stop utilisé ; le stop réel est capturé dans `entry_stop_map` et rejoué par `use_custom_stoploss = True` + `custom_stoploss()` (§4.9.4). La colonne `stop_loss` reste malgré tout dans la frame Freqtrade, **pour audit seulement** — Freqtrade l'ignore (elle ne fait pas partie de ses `HEADERS`) |
| `ParamsModel` / `StrategyParams` (pydantic) | `IntParameter` / `DecimalParameter` / `CategoricalParameter` | traduction automatique quand elle est **exacte** (§4.9.3) |
| `Strategy.prepare` + `Strategy.signals` | les trois `populate_*` | `prepare` alimente `populate_indicators` ; `signals` alimente `populate_entry_trend` et `populate_exit_trend` |
| — | `INTERFACE_VERSION` | `FREQTRADE_INTERFACE_VERSION = 3` |
| — | `minimal_roi` | `{"0": 100.0}` : le ROI table de Freqtrade est **désactivé** — le moteur maison ne produit jamais de take-profit, en activer un rendrait les deux exécutions encore moins comparables |
| — | `use_custom_stoploss` | `True` — c'est le seul mécanisme qui permette de rejouer un stop **par trade** |
| — | `can_short` | `True` si la stratégie maison déclare `allow_short`, `False` sinon |
| — | `process_only_new_candles` | `True` (défaut Freqtrade) |
| — | `startup_candle_count` | dérivé des paramètres par `startup_candle_count_for` : le plus grand champ `int` du modèle (donc `21`, soit `ema_slow`, pour `basic`), `0` si le modèle n'en déclare aucun |

Deux détails de forme, vérifiés sur les données réelles du cache
(`data/cache/binance/BTC_USDT/1h.parquet`) :

- la frame reçue par un `populate_*` a un **`RangeIndex` positionnel** et porte
  l'horodatage dans une **colonne `date`** (tz-aware UTC), pas dans son index —
  `house_frame` la convertit vers le contrat OHLCV du §5
  (`DatetimeIndex` UTC nommé `timestamp`), et les trois `freqtrade_*_frame`
  font le chemin inverse ;
- le traitement est **positionnel et non destructif** : l'adaptateur n'ajoute ni
  ne supprime aucune ligne, ne réordonne rien et ne consomme pas la frame reçue
  (aucune mutation de l'argument). Le nombre de lignes rendues par chaque
  `populate_*` est exactement celui reçu.

#### 4.9.2 Écrire une stratégie **une seule fois**

Le mode d'emploi tient en quatre étapes, et la quatrième est la seule qui parle
de Freqtrade :

1. **écrire la stratégie maison** — hériter de `Strategy`, implémenter
   `prepare` (indicateurs, avec `strategy.indicators`) et `signals` (colonnes
   `entry_long` / `exit_long` / `entry_short` / `exit_short` / `stop_loss`),
   déclarer un `ParamsModel` pydantic et un `PARAM_SPACE` ;
2. **l'enregistrer** avec `@register_strategy` — c'est ce qui la rend résolvable
   par `get_strategy(name)` **et** par `make_freqtrade_strategy(name)` ;
3. **la valider avec le moteur maison** : `backtest`, `walk-forward`,
   `robustness`, `monte-carlo` — c'est là que la stratégie se gagne ou se perd ;
4. **l'exposer à Freqtrade** — une seule ligne de code, et **aucun** code par
   stratégie :

   ```python
   BasicFreqtradeStrategy = make_freqtrade_strategy("basic")
   ```

   puis, dans `user_data/strategies/<ClassName>.py`, la déclaration d'une ligne
   qui donne à Freqtrade le nom qu'il attend :

   ```python
   class BasicStrategy(BasicFreqtradeStrategy):
       pass
   ```

Le **résolveur** de shim est volontairement strict, pour qu'un fichier périmé ne
puisse pas passer silencieusement : le texte du fichier doit contenir
littéralement `class <Name>(` et le `__module__` de la classe chargée doit être
égal au *stem* du fichier. Concrètement, `user_data/strategies/BasicStrategy.py`
doit définir `class BasicStrategy(` et la classe doit avoir
`__module__ == "BasicStrategy"`.

Cette règle n'ajoute **aucune** logique dans `user_data/` : le shim ne contient
ni indicateur, ni règle d'entrée, ni paramètre. Toute la logique reste dans la
couche maison ; supprimer le shim ne change rien au backtest maison.

#### 4.9.3 Traduction des paramètres pydantic

La traduction est **déterministe** (ordre de déclaration de
`ParamsModel.model_fields`) et se fait en deux temps : `freqtrade_param_specs`
produit des descriptions **inertes** (`FreqtradeParamSpec`, testables hors
ligne, sans Freqtrade), puis `freqtrade_parameter_attributes` produit les
**vrais** objets Freqtrade (importés *dans* la fonction).

| Annotation pydantic | Paramètre Freqtrade produit |
| --- | --- |
| `bool` | `BooleanParameter(default=..., space="buy")` |
| `int` | `IntParameter(low, high, default=..., space="buy")` |
| `float` | `DecimalParameter(low, high, default=..., decimals=3, space="buy")` |
| grille `PARAM_SPACE` à ≥ 2 candidats **passée explicitement**, et dont le défaut est l'un des candidats | `CategoricalParameter(categories=..., default=...)` |
| toute autre annotation (`str`, `Enum`, `datetime`, `Sequence`) | **non mappée** : absente du résultat, le défaut maison s'applique à l'exécution |

Les bornes sont lues par *duck-typing* des métadonnées pydantic v2
(`ge` / `gt` / `lt` / `le`) :

- `int` — `low = ge`, sinon `gt + 1`, sinon `0` ; `high = le`, sinon `lt − 1`,
  sinon `max(low + 1, int(default × 2.0))` ;
- `float` — `low = ge`, sinon `gt + 10⁻³`, sinon `0.0` ; `high = le`, sinon
  `lt − 10⁻³`, sinon `round(default × 2.0, 3)` ; `decimals = 3`.

**Garde obligatoire** : Freqtrade ne vérifie **pas** que le défaut tombe dans
`[low, high]` (`IntParameter(1, 18, default=20)` est accepté silencieusement en
2026.8), donc chaque spec numérique est **clampée** jusqu'à ce que
`low <= default <= high` et `low < high` tiennent — une plage de largeur nulle
ferait échantillonner un unique point à l'hyperopt.

Limites de cette traduction, écrites ici plutôt que découvertes plus tard :

1. les bornes **exclusives** `gt` / `lt` sont approximées par un décalage de
   `10**-decimals` (Freqtrade n'a que des bornes inclusives) : l'ensemble
   admissible est **rétréci**, jamais élargi ;
2. `extra="forbid"` et les invariants inter-champs (`ema_slow > ema_fast`,
   `rsi_min < rsi_max`) n'ont **aucun** équivalent Freqtrade, dont les
   paramètres sont des objets indépendants champ par champ : ils restent
   appliqués par le `ParamsModel` maison, que l'adaptateur **revalide** au
   moment de l'exécution ;
3. les paramètres Freqtrade sont des **attributs de classe** : un fichier de
   paramètres Freqtrade (`--strategy-params` / `buy_params`) peut les
   surcharger et **contourner** le modèle maison — d'où la revalidation du
   point 2 ;
4. la grille de `PARAM_SPACE` n'est **jamais devinée** : elle est passée
   explicitement (`grid=…`) ; sans elle, chaque champ garde son type numérique.

**Mapping manuel.** `freqtrade_param_specs(..., overrides={…})` est
l'échappatoire documentée : après la passe automatique, une entrée **remplace**
une spec par nom (à la même position) ou en **ajoute** une (en fin de liste) —
c'est le chemin pour exposer un champ non mappé du point précédent, par exemple
en `CategoricalParameter`. Une spec surchargée passe par la **même** garde
numérique : une surcharge mal formée est réparée, jamais émise telle quelle.

#### 4.9.4 L'écart de modèle d'exécution — écrit noir sur blanc

**Les deux backtests ne donneront pas les mêmes chiffres.** C'est **attendu**, ce
n'est pas un bug, et les **signaux** — eux — sont bien identiques.

Le point commun, vérifié dans le code de Freqtrade 2026.8 : la convention
« décision à la clôture de `t`, exécution à l'ouverture de `t+1` » est la même
des deux côtés. Le docstring gelé de `strategy/engine.py` la formule ainsi :

> signals are evaluated **at the close of candle ``t``** and filled **at the
> open of candle ``t + 1``**

Côté Freqtrade, `Backtesting._get_ohlcv_as_lists` **décale les colonnes de
signal d'une bougie** (`.shift(1)`) puis supprime la première ligne, et la
boucle d'entrée entre au prix `row[OPEN_IDX]` — c'est-à-dire à l'**ouverture**
de la bougie qui suit le signal. Le moteur maison fait exactement la même
chose.

Tout le reste diffère :

| Point | Moteur maison (`strategy.engine`) | Freqtrade 2026.8 |
| --- | --- | --- |
| Signal -> fill | clôture de `t` -> ouverture de `t+1` (achat `open × (1 + slippage)`, vente `open × (1 − slippage)`) | **même convention** (signaux décalés d'une bougie, entrée à `row[OPEN_IDX]`) |
| Type d'ordre | prix d'ouverture, plus slippage paramétrique | `entry` / `exit` / `stoploss` en **limit** par défaut, `stoploss_on_exchange = False` : les prix d'exécution peuvent différer du prix théorique |
| Frais | `fee_rate × size × (entry_price + exit_price)`, les deux jambes | frais de l'exchange, maker/taker, résolus par la config et la précision de la paire |
| Take-profit | **jamais** produit (`TAKE_PROFIT` existe dans l'enum mais n'est jamais émis) | **désactivé** par l'adaptateur via `minimal_roi = {"0": 100.0}` ; Freqtrade sait faire, on lui dit de ne pas le faire |
| Stop-loss | colonne `stop_loss` de la **bougie de signal**, **statique** pour tout le trade, testée intrabar depuis la bougie d'entrée contre `low` (long) / `high` (short), remplie **au prix du stop** | stop **absolu capturé** sur la bougie de signal puis reconverti en ratio à **chaque** appel de `custom_stoploss()` (rien n'est mis en cache : le stop reste absolu et ne dérive donc pas en trailing stop) ; borné par `self.stoploss = -0.99`, et Freqtrade ne l'applique que s'il **resserre** le stop live — le stop exécuté peut être plus serré, jamais plus large, que celui du moteur maison |
| Nombre de positions | **une seule** à la fois : un signal d'entrée pendant une position ouverte est ignoré | `max_open_trades` de la config (`1`) **et** une seule position par paire : la contrainte se rejoint, mais par deux mécanismes différents (verrous de paire, protections) |
| Ordres non remplis | sans objet : le fill est supposé au prix d'ouverture | `unfilledtimeout` et expiration d'ordre : un ordre limit non rempli peut être annulé, retardé ou re-tarifé |
| Analyse des bougies | à chaque bougie | `process_only_new_candles` : l'analyse peut être sautée sur une bougie déjà vue |
| Startup | aucun échauffement implicite, la stratégie gère ses `NaN` | `startup_candle_count` **rogne** les premières bougies : les fenêtres ne portent pas sur le même nombre de bougies |
| Précision et coûts d'exchange | prix bruts du CSV / parquet | précision, pas de cotation, minimum notionnel, funding en futures |
| Sortie | ordre de priorité : stop -> signal -> fin de données (`END_OF_DATA` à la dernière clôture) | ordre de priorité **propre à Freqtrade** : stop-loss, puis ROI, puis signal de sortie, puis sortie forcée |
| Sortie de fin de série | position ouverte sur la dernière bougie **fermée à la dernière clôture** | la position reste ouverte et est marquée « open trade » dans le rapport de backtest |

**Conclusion, à retenir telle quelle : les SIGNAUX partagés sont le contrat ; le
PnL, la liste des trades et les métriques ne sont PAS comparables entre les deux
backtests, et une divergence n'est pas un bug.** Un écart entre le backtest
maison et le backtest Freqtrade est le **prix normal** de deux moteurs
d'exécution différents ; ce qu'il faut vérifier, c'est que les **colonnes de
signal** coïncident bougie par bougie (c'est ce que fait le test d'intégration),
pas que les rendements s'égalent.

Le détail des hypothèses d'exécution du moteur maison — et de leur lecture — est
dans [`docs/backtesting-methodology.md`](backtesting-methodology.md).

#### 4.9.5 Limites connues de l'adaptateur

- **La table des stops vit en mémoire de process.** `entry_stop_map` associe une
  bougie de signal à son stop absolu ; elle est reconstruite à chaque cycle
  d'analyse mais **jamais persistée**, et ne connaît **aucun élagage** : environ
  un flottant par bougie et par paire pour toute la durée de vie du process —
  une limite réelle sur un run live en 1 minute qui tourne des mois. Un trade
  dont la bougie de signal sort de la fenêtre chargée (`lookup_entry_stop` rend
  alors `None`) retombe sur le stop global `-0.99`.
- **Aucun take-profit et aucune durée maximale** ne sont traduits : le moteur
  maison ne les produit pas, l'adaptateur ne les invente pas.
- **Le ROI Freqtrade est désactivé** (`minimal_roi = {"0": 100.0}`) : activer un
  ROI côté Freqtrade ferait diverger les deux exécutions sans qu'aucune règle
  maison ne le justifie.
- **La traduction des paramètres a ses trous** (§4.9.3) : bornes exclusives
  approximées, invariants inter-champs non traduits, champs d'annotation non
  scalaire non mappés, et surcharge possible par un fichier de paramètres
  Freqtrade — d'où la revalidation du modèle maison à l'exécution.
- **Le shim n'est pas un mécanisme de synchronisation** : si `BasicStrategy.py`
  et la stratégie maison divergent (mauvais nom de classe, fichier périmé), le
  résolveur échoue — c'est voulu — mais rien ne réécrit le fichier
  automatiquement.
- **Un seul profil à la fois.** L'adaptateur expose **une** stratégie pour **un**
  couple actif/timeframe : il n'y a pas encore d'orchestration multi-profils, ni
  de sélection de profil par le bot. C'est précisément la brique suivante.

---

### 4.10 Le temps réel — `trading_platform.realtime`

Le moteur exécute **N profils concurrents** (`asset + stratégie + timeframe +
paper/live + limites de risque`) dans une seule boucle d'événements `asyncio`.
Il ne réimplémente **rien** : stratégies du registre, données, métriques,
validation, configuration et pont Freqtrade restent les implémentations des
couches basses.

#### 4.10.1 Les quatre coutures injectables

Toute la testabilité hors ligne de la couche tient à ces `typing.Protocol` : un
test (ou un autre paquet) fournit une implémentation locale, et le moteur ne
change pas.

```python
class Clock(Protocol):  # realtime/clock.py
    def now(self) -> datetime: ...  # UTC, tz-aware
    def monotonic(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class MarketStream(Protocol):  # realtime/stream.py
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None: ...
    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame: ...
    @property
    def connected(self) -> bool: ...
    @property
    def last_error(self) -> str | None: ...
    @property
    def reconnect_count(self) -> int: ...


class Broker(Protocol):  # realtime/broker.py
    name: str

    def submit(self, request: OrderRequest, *, reference_price: float) -> BrokerAck: ...
    def cancel(self, client_order_id: str) -> bool: ...
    def poll(self) -> list[BrokerEvent]: ...
    def open_orders(self) -> list[Order]: ...
    def fetch_balance(self) -> float | None: ...
    def reconcile(self) -> ReconciliationReport: ...


class StateStore(Protocol):  # realtime/store.py
    def initialize(self) -> None: ...
    def close(self) -> None: ...
    def save_profile(self, spec: ProfileConfig) -> None: ...
    def load_profiles(self) -> list[ProfileConfig]: ...
    def upsert_order(self, order: Order) -> None: ...
    def get_order(self, client_order_id: str) -> Order | None: ...
    def list_orders(self, profile_id: str, *, limit: int = 100) -> list[Order]: ...
    def append_fill(self, fill: Fill) -> bool: ...  # idempotent
    def upsert_position(self, position: Position) -> None: ...
    def delete_position(self, profile_id: str, symbol: str) -> None: ...
    def get_position(self, profile_id: str, symbol: str) -> Position | None: ...
    def list_positions(self, profile_id: str) -> list[Position]: ...
    def append_equity(self, point: EquityPoint) -> bool: ...  # idempotent
    def equity_curve(self, profile_id: str) -> list[EquityPoint]: ...
    def append_trade(self, trade: TradeRecord) -> bool: ...  # idempotent
    def list_trades(self, profile_id: str) -> list[TradeRecord]: ...
    def save_status(self, profile_id: str, status: ProfileStatus, detail: str = "") -> None: ...
    def load_status(self, profile_id: str) -> ProfileState: ...
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def last_processed_candle(self, profile_id: str) -> pd.Timestamp | None: ...
    def profile_state(self, profile_id: str) -> ProfileState: ...


class MetaStore(Protocol):  # realtime/risk.py
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...
```

Implémentations livrées : `SystemClock` / `ManualClock` ; `ReplayMarketStream`
(déterministe, hors ligne), `PollingMarketStream` (réutilise
`data.loader.MarketDataProvider.fetch_ohlcv` via un provider injecté et un
`Clock` injecté), `CcxtProMarketStream` (`import ccxt.pro` **dans le corps de la
méthode**), `CompositeMarketStream` (multiplexe plusieurs symboles) ;
`SqliteStateStore` (`sqlite3` standard, WAL, une connexion par thread appelant,
écritures en transaction, UPSERT sur clé naturelle, ligne `schema_version` avec
contrôle de migration) ; `PaperBroker` et `CcxtBroker`.

**Règle unique des flux vivants.** `next_candle` rend la bougie fermée la **plus
récente** strictement postérieure à la dernière émise, et `history` la fenêtre
qui se termine **maintenant** : les deux règles n'en forment qu'une, parce que le
`ProfileRunner` échauffe la stratégie sur `history()` puis ajoute la bougie émise
à cette fenêtre. Un flux vivant qui émettrait la plus ancienne bougie de sa
fenêtre de rétrospection donnerait à la stratégie une trame tronquée
(`warmup_incomplete`, aucun trade) puis, la trame ayant grossi, lui ferait
décider sur une bougie vieille de plusieurs jours — remplie au prix du jour. Les
bougies sautées sont signalées (`market_data.candles_skipped`) ; le rejeu
déterministe d'une fenêtre passée reste le rôle de `ReplayMarketStream`
(`realtime run --once`, tests).

#### 4.10.2 Les quatre autres coutures

```python
class SnapshotProvider(Protocol):  # web/routes.py (réexporté par web/server.py)
    def snapshot(self) -> PlatformSnapshot: ...
    def health(self) -> dict[str, Any]: ...
    def profile_snapshot(self, profile_id: str) -> ProfileSnapshot | None: ...
    def engage_kill_switch(self, reason: str) -> Any: ...
    def release_kill_switch(self) -> Any: ...
    def kill_switch_state(self) -> Any: ...
```

C'est la **seule** chose que la couche 7 sait de la plateforme : elle n'importe
jamais `RealtimeOrchestrator`. `RiskManager`, `KillSwitch` et `LiveTradingGate`
(realtime/risk.py) complètent la surface de sûreté : le kill switch s'appuie sur
la couture `MetaStore` (`get_meta`/`set_meta`) et ne dépend donc pas durement de
`SqliteStateStore`.

#### 4.10.3 Vocabulaire gelé — `realtime/models.py`

Toutes les dataclasses gelées du domaine temps réel vivent dans ce module, et
chacune expose `to_dict()` (payload JSON-able, pas de NaN ni d'Infinity,
horodatages ISO-8601 UTC) :

`CandleEvent`, `OrderRequest`, `Order`, `OrderState`, `Fill`, `BrokerAck`,
`BrokerEvent`, `ReconciliationReport`, `Position`, `EquityPoint`, `ProfileState`,
`ProfileSnapshot`, `PlatformSnapshot`, `ProfileHealth`, `ProfileStatus`,
`RunMode`, `TradeSignalDecision`, `EngineCounters`, plus la fabrique
`new_client_order_id(...)`.

Les énumérations gélées sont `RunMode` (`paper`/`live`), `ProfileStatus`
(`starting|running|degraded|halted|stopped|error`), `OrderSide`, `OrderType`,
`OrderState`, `BrokerEventType` et `SignalAction`. `SignalAction` et `Direction`
sont partagés avec le moteur de backtest.

#### 4.10.4 Équivalence d'exécution et cycle de vie d'un ordre

- La décision est évaluée à la **clôture de la bougie `t`** et exécutée à
  l'**ouverture de `t+1`**, slippage toujours défavorable (convention de
  `strategy.engine`, voir §4.9.4). En temps réel, la clôture de `t` **est**
  l'instant de déclenchement : le prix de référence d'une décision est la
  **clôture de `t`**, et seules les bougies fermées sont émises.
- **Il n'y a qu'un seul chemin d'exécution** : une `ExecutionGateway` et un
  `ProfileRunner`. Paper et live ne diffèrent que par (a) le `Broker` injecté par
  la fabrique de l'orchestrateur, (b) la porte `LiveTradingGate` quand
  `mode == "live"` et (c) la configuration. La passerelle ne contient **aucune**
  branche « si paper / si live » : elle route vers le courtier injecté, et le
  mode fait partie de l'identité du profil et de chaque ordre persisté.
- Le risque est évalué **avant** l'appel au courtier ; tout refus est journalisé
  avec sa raison et n'écrit **pas** de watermark de bougie.
- Le kill switch global est **persisté** : il survit à un redémarrage, et rien
  n'est annulé en silence.

#### 4.10.5 Persistance, redémarrage, réconciliation

L'état vit dans **un seul** fichier SQLite (`realtime.state_db`, défaut
`data/realtime/state.db`, ignoré par git) : chaque écriture est idempotente
(UPSERT sur clé naturelle) et le `client_order_id` est **déterministe** —
dérivé de `profile_id + symbol + horodatage de la bougie + séquence` — donc la
même décision produit toujours la même identité et un redémarrage entre
soumission et remplissage ne double jamais un ordre.

La dernière bougie traitée est persistée par profil : un redémarrage ne rejoue
pas une bougie et n'en saute pas non plus. Au démarrage, l'orchestrateur
réconcilie l'état local contre le lieu d'exécution (`Broker.reconcile()`) et
marque le profil `degraded` en cas d'écart. Le magasin est **mono-écrivain** :
un second orchestrateur sur le même fichier lève `StateStoreError`, et une
version de schéma plus récente lève la même erreur au lieu d'écrire à l'aveugle.

Un lieu **simulé** n'a pas de mémoire : après un redémarrage son cash repart du
solde initial alors que la position, elle, est restaurée depuis le magasin.
L'orchestrateur **réamorce donc le cash du courtier papier** avec le dernier
point d'equity persisté (`PaperBroker.restore_cash`) avant la première bougie,
sinon la position serait comptée **deux fois** (`cash + quantité × mark`). Un
lieu réel n'est jamais réamorcé : il publie son propre solde.

#### 4.10.6 Observabilité

`realtime/observability.py` émet des **journaux JSON structurés** (un objet par
ligne : `ts`, `level`, `event`, `profile_id`, contexte) via un formateur standard
et un **filtre de masquage** ; des **compteurs** en mémoire (ordres soumis /
remplis / rejetés, bougies traitées, reconnexions, refus de risque) sont exposés
par le read model ; chaque profil porte son état explicite (`status`,
`last_candle_at`, `lag_seconds`, `last_error`, `reconnect_count`).

#### 4.10.7 Profile feature injection — `realtime/features.py`

The realtime engine attaches to each profile the **same** feature bundle as the
backtest, through the **same** seam (`trading_platform.strategy.features`:
`resolve_features` / `attach_features`). There is therefore **no** second
artifact-loading mechanism: the module only adapts a profile to the shape
expected by the project's single loader, then checks the coverage.

| Symbol | Key signature | Role |
| --- | --- | --- |
| `resolve_profile_features` | `resolve_profile_features(profile: ProfileConfig, *, required: bool) -> FeatureBundle` | the **only** construction path of a profile's bundle; loads the artifact **once** (at startup, never per candle) via `strategy.features.resolve_features`, then runs the guard when a path is declared |
| `check_profile_forecast` | `check_profile_forecast(profile, store, *, now: pd.Timestamp \| None = None) -> ForecastCoverage` | the **mandatory** startup guard: empty bundle, symbol, timeframe, then coverage; raises `ForecastArtifactError` with an actionable message |
| `ForecastCoverage` | `@dataclass(frozen=True)`: `path`, `symbol`, `timeframe`, `seasonal_period`, `horizon`, `stride`, `first_origin`, `last_origin` | the frozen answer to "is this artifact still usable right now?", rendered by `trading forecast-info` |
| `strategy_needs_forecast` | `strategy_needs_forecast(profile: ProfileConfig) -> bool` | does the profile's strategy declare an `artifact` parameter? (the strategy's contract, never a hardcoded name) |

`realtime/strategies.py::resolve_strategy` is the **only** construction path of a
realtime strategy, and it is the one that calls
`attach_features(strategy, resolve_profile_features(profile, required=True))`.
All its callers go through `ProfileRunner._prepare` (`start`, `run`, `run_once`,
the orchestrator, hence `realtime run`, `realtime run --once` and
`realtime check`): an artifact that is missing, corrupt, built for another
symbol/timeframe or too stale to cover the profile's decision horizon fails
**before the first candle**, instead of starting and never trading.

The `ProfileConfig.forecast` field (`Path | None`, `null` by default, the **last**
field of the model so the order of the existing fields does not move) carries the
declared path; `ProfileConfig.forecast_artifact` returns it and
`ProfileConfig.strategy_params()` merges `params` with the `artifact` key without
ever mutating the profile's configuration. A profile that declares nothing —
every `basic` profile, including the two shipped examples — keeps its behaviour
**unchanged**: empty bundle, no loading, no guard. The operational semantics and
the verbatim refusal message are in
[`docs/realtime.md`](realtime.md) §3.1, and the layer itself in
[`docs/forecasting.md`](forecasting.md).

### 4.11 La couche web — `trading_platform.web`

Couche **7**, **bibliothèque standard uniquement** : `http.server.ThreadingHTTPServer`
`+ json + sqlite3 + asyncio + logging + threading`. Aucun WebSocket, aucun ASGI,
aucune dépendance tierce, aucun CDN et aucune étape de build.
The HTML dashboard (`index.html` + `app.js` + `styles.css`) was **removed**:
layer 7 serves a **pure JSON API**, and the monitoring UI is now the standalone
**Next.js** application in `dashboard/`.

- `routes.py` est un **routage pur** (`Router.handle(method, path, ...)` →
  `HttpResponse`) : aucune socket, aucun thread, donc testable directement. Il
  porte la couture `SnapshotProvider` (§4.10.2) et le contrat JSON complet.
- `server.py` porte le transport : `MonitoringServer` (port `0` accepté, port
  relu depuis `server.server_address`), `create_server(...)`, `serve(...)` et
  `start_in_thread(...)`.
- The HTTP surface is **exactly** the `/api` routes: `GET /api/health`,
  `GET /api/profiles`, `GET /api/profiles/{id}`, `.../equity`, `.../trades`,
  `.../orders`, `.../positions`, `.../metrics`, `GET /api/kill-switch` and
  `POST /api/kill-switch`. Every other path — `GET /` and `GET /static/{asset}`
  included — answers the documented JSON 404 `{"error": "not found: <path>"}`:
  the layer keeps **no** static directory and **no** static allow-list.
- Les erreurs sont des payloads : `400` malformé, `404` route ou profil inconnu,
  `405` méthode incorrecte (avec `Allow`), `403` jeton opérateur absent/invalide
  ou serveur en lecture seule, `500` `{error}` — **jamais** de trace sur le
  réseau.
- `realtime serve` construit la couche 7 avec `read_only=True` : seules les
  routes `GET` répondent, la mutation est refusée (403).

Le détail route par route, les payloads et la cadence de sondage sont documentés
dans [`docs/realtime.md`](realtime.md#5-web-api-reference).

### 4.12 Cycle de vie des profils, catalogue et historique de bougies

Le tableau de bord pilote quatre mutations, toutes protégées par le **même**
jeton opérateur (`X-Operator-Token`) que le *kill switch* et toutes refusées avec
le `403` documenté par un serveur en lecture seule (`realtime serve`, qui
n'attache aucun contrôleur).

- `realtime/store.py` ajoute une table **`candles`** bornée (une ligne par profil
  et par horodatage, OHLCV plus l'indicateur *closed*) : le moteur y écrit chaque
  bougie qu'il traite et le magasin ne conserve que les **1000** lignes les plus
  récentes de chaque profil, élaguées dans la même transaction que l'ajout. La
  migration est **additive** : une base déployée en version 1 gagne la table vide
  sans perdre une seule ligne, et `SCHEMA_VERSION` vaut désormais `2`.
- `realtime/catalog.py` (`MarketCatalog`) est la **seule** source des quatre
  vocabulaires des sélecteurs : paires *spot* négociables de l'exchange pour la
  devise de cotation configurée (lues par la couture exchange/`ccxt`, mises en
  cache avec une durée de vie, et repli sur une table statique), noms de
  stratégies du registre, timeframes triés du plus court au plus long, modes
  `paper`/`live`. Il ne lève jamais : une venue injoignable dégrade la réponse au
  lieu de produire une erreur.
- `realtime/control.py` (`RuntimeProfileController`) est la **couture injectée**
  entre le serveur HTTP, qui répond depuis un thread, et l'orchestrateur, qui est
  asynchrone : chaque commande est renvoyée sur la boucle du moteur
  (`asyncio.run_coroutine_threadsafe`, la boucle étant liée depuis la coroutine du
  moteur), ce qui rend une mutation concurrente d'une bougie en cours de
  traitement sûre, et ce qui garde la couche web importable **sans** moteur.
- La couche 7 expose trois routes de lecture de plus
  (`GET /api/profiles/{id}/candles`, `GET /api/catalog`, `GET /api/control`) et
  quatre mutations (`POST /api/profiles`, `POST /api/profiles/{id}/pause`,
  `POST /api/profiles/{id}/resume`, `DELETE /api/profiles/{id}`). **Pause** ferme
  la porte d'entrée sans jamais laisser une position sans surveillance (le stop et
  les sorties restent évalués) ; **delete** aplatit au marché, par la passerelle
  d'exécution, **avant** de retirer quoi que ce soit, et échoue sans rien modifier
  si l'aplatissement échoue. Le détail (payloads, codes d'erreur, sémantique) est
  dans `docs/realtime.md` §5 et §8.

### 4.13 The forecasting layer — `trading_platform.forecast`

Layer **2.5**: it sits between `core` and `strategy` (`core -> forecast ->
strategy -> cli`) and may import `core` plus the standard library, `numpy`,
`pandas` and `pyarrow` — nothing else. `torch`, `timesfm` and `jax` are optional
and imported **lazily inside functions**, so importing the package, running the
CLI and running the whole suite stay green with the `.[dev]` extra alone. The
TimesFM model therefore **never** runs inside `Strategy.prepare()` /
`Strategy.signals()`: prediction is pre-computed **offline** by the CLI into a
versioned parquet artifact that the strategy consumes as a deterministic
external input. The full reference — artifact schema, backend contract, licence
table, measured traps and the honesty section — is
[`docs/forecasting.md`](forecasting.md).

#### 4.13.1 Types and errors

| Symbol | Key signature | Role |
| --- | --- | --- |
| `ForecastRequest` | `@dataclass(frozen=True)`: `origin: pd.Timestamp`, `context: tuple[float, ...]` | one forecast request: the origin and the **past** context window (never the future) |
| `ForecastTrajectory` | `@dataclass(frozen=True)`: `origin`, `timeframe`, `horizon`, `quantile_levels: tuple[float, ...]`, `quantiles: np.ndarray` `(len(quantile_levels), horizon)` `float32`; `median` property (the `0.5` row) and path-shape helpers (`alpha(k)`) | one probabilistic trajectory per origin |
| `ForecastError` | `core.errors`, listed in `core.errors.__all__` | forecasting-layer failure (unknown backend, missing optional extra) |
| `ForecastArtifactError` | `core.errors`, listed in `core.errors.__all__` | artifact missing, corrupt, truncated or of an incompatible schema |

#### 4.13.2 Backends and registry

| Symbol | Key signature | Role |
| --- | --- | --- |
| `ForecastBackend` | `Protocol`: `name: str`, `is_available() -> bool`, `predict(requests, *, horizon) -> list[ForecastTrajectory]` | the backend contract; `is_available()` returns `False` (never raises) when the extra is missing |
| `register_backend` | class decorator `register_backend(cls) -> type[ForecastBackend]` | registers a backend under its `name` (mirrors `strategy.registry`) |
| `available_backends` / `installed_backends` | `() -> list[str]` | names known to the process / names that can actually run (`is_available()`) |
| `get_backend` | `get_backend(name: str, **options) -> ForecastBackend` | builds a registered backend; `ForecastError` when the name is unknown or the extra is absent |
| `ensure_forecast_backends` | `ensure_forecast_backends() -> None` (`forecast/bootstrap.py`) | imports both **offline** backends or raises one actionable `ForecastError`; called before any artifact work, so an installation defect is never reported as a `ModuleNotFoundError` |
| `bootstrap_profile_forecast` | `bootstrap_profile_forecast(profiles_path, profile_id, *, config, backend="seasonal") -> tuple[Path, ArtifactMetadata]` (`forecast/bootstrap_profile.py`) | builds the artifact a **profile** declares, from the candle file its `symbol`/`timeframe` imply, with an offline backend only; cross-checks the declarations against the file name so an artifact is never stamped with a symbol/timeframe its numbers do not describe |
| `candles_per_day` / `resolve_seasonal_period` | `candles_per_day(timeframe) -> int`, `resolve_seasonal_period(timeframe, override=None) -> int` (`forecast/series.py`) | the seasonal period per timeframe (`1440` on `1m` … `1` on `1d` and longer) and the explicit override; a hard-coded `24` was only right for hourly candles |
| `naive` | backend `name = "naive"`, numpy/pandas only | random walk: flat median held at the last context point, dispersion from trailing absolute differences scaled by `sqrt(step)` |
| `seasonal` | backend `name = "seasonal"`, numpy/pandas only | seasonal baseline drift-corrected in the de-seasonalised space; deterministic and calendar aware |
| `timesfm` | backend `name = "timesfm"`, `[timesfm]` extra | TimesFM 2.5 (`google/timesfm-2.5-200m-pytorch`, Apache-2.0), lazy import, requests batched by context length, CPU on Apple silicon (MPS is not supported by the 2.5 path) |

#### 4.13.3 Artifact, store and skill report

| Symbol | Key signature | Role |
| --- | --- | --- |
| `ForecastStore` | `ForecastStore.load(path) -> ForecastStore`, then `metadata`, `origins() -> pd.DatetimeIndex`, `origin_for(timestamp)`, `trajectory(origin)` (lazy per-origin cache) | read-only view of the artifact; an explicit `ForecastArtifactError` on a missing / corrupt / incompatible file, never a silent degradation |
| `build_forecast_artifact` | `build_forecast_artifact(frame, out, config) -> Path` | builds the artifact origin by origin on the `reforecast_every` stride, each origin using **only** the candles `<= origin`, then writes the JSON metadata sidecar |
| `ForecastBuildConfig` | build settings: `backend`, `context_length`, `horizon`, `reforecast_every`, `symbol`, `timeframe`, `seasonal_period` (`None` = derive it from the timeframe), backend options | describes **what** is built (and feeds the sidecar) |
| skill report | offline helper used by `trading forecast-skill` | RMSE / MAE / MASE against the random walk, decile coverage, directional accuracy at the horizon |

The artifact is a **parquet** file: a `DatetimeIndex` named `origin` (the last
context candle), the `horizon` / `stride` / `n_quantiles` columns (`int16`) and
the `quantile_levels` / `median` / `quantiles` columns (`list<float32>`, with
`quantiles` in **row-major** order, length `n_quantiles × horizon`). The sidecar
`<artifact>.meta.json` carries the schema version, the symbol, the timeframe, the
backend, the model id, the context length, the stride, the horizon, the quantile
levels, the feature list, the creation timestamp, the package version, the data
span and the licence note. The exact schema and the commands are in
[`docs/forecasting.md`](forecasting.md) §2 and §5.

#### 4.13.4 CLI commands

`trading forecast-build` (options `--config/-c`, `--data-file`, `--out`,
`--backend naive|seasonal|timesfm`, `--context`, `--horizon`,
`--reforecast-every`, `--seasonal-period`, `--seasonal-window`, `--model-id`,
`--symbol`, `--timeframe`), `trading forecast-bootstrap` (`--profiles`,
`--profile`, `--config/-c`, `--backend naive|seasonal`),
`trading forecast-skill` (`--artifact`, `--data-file`) and
`trading forecast-info` (`--artifact`, `--now`, `--profiles`, `--profile`,
`--timeframe`, `--horizon`) — all of them accept `--json`, like the rest of the
CLI. `forecast-info` is the operability seam of the startup guard: pointed at a
profile it raises the **very same** refusal the realtime engine raises at
startup. The `make forecast-build`, `make forecast-bootstrap`,
`make forecast-profile`, `make forecast-info` and `make forecast-flow` targets
call them.

### 4.14 The `timesfm` strategy — `trading_platform.strategy.timesfm_forecast`

| Element | Frozen content |
| --- | --- |
| Registered name | `timesfm` (`TimesfmForecastStrategy.name`), resolved by `get_strategy("timesfm")` and therefore by `strategy.name` in the configuration |
| Parameters | `TimesFMForecastParams` (pydantic, `extra="forbid"`, cross-field validator) + `PARAM_SPACE` for the robustness sweep: artifact wiring, `context_length`, `horizon`, `reforecast_every`, `min_lead`, the forecast-age bound, the entry gates, the exit thresholds, the ATR stop, cooldown / maximum holding time, `allow_short` |
| `prepare` | adds the house indicators (`rsi`, `atr`, `ema`) **and** the diagnostic columns: `forecast_alpha_<k>`, `forecast_slope`, `forecast_mfe`, `forecast_mae`, `forecast_path_eff`, `forecast_iqr_term`, `forecast_iqr_per_bar`, `forecast_reliability`, `forecast_agreement`, `vol_ratio`, `forecast_age`, `atr_pct`, `exit_code` |
| `signals` | **AND-composed** entries (sign and size of the predicted edge, edge vs ATR, reliability, decile agreement, path efficiency, volatility regime, ATR-percentile window, optional RSI filter, freshness guard, cooldown) and a **priority-ordered exit tree**: `FORECAST_FLIP`, `EDGE_DECAY`, `TARGET_REACHED`, `PATH_DEGRADED`, `TIME_STOP`, `VOL_REGIME`, plus the static ATR stop carried by the `stop_loss` column and the stale-forecast guard |
| Missing data | missing artifact, unknown origin, stale origin or a decision outside the covered window ⇒ **no signal at all**: never an exception, never a guess |
| Auditability | `exit_code` (an integer) names the exit reason that fired, in priority order (`docs/forecasting.md` §10) |
| Short / long | *shorts* mirror *longs* exactly (the engine reads `stop_loss` as the mirror of the long formula) |

The `basic` strategy stays **unchanged**: `timesfm` is an additional strategy in
the registry, and the artifact is injected into it through the
`trading_platform.strategy.features` seam, used by
`run_backtest_on_config` / `make_runner` **and** by
`realtime.strategies.resolve_strategy` (§4.10.7). Every caller of a strategy
therefore injects the same features through the same seam: the strategy itself
never loads the artifact, never reads the clock and never touches the network.

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
(`trading_platform.data.synthetic`) ou des CSV de `tests/fixtures/` : **aucun
téléchargement réel** dans la suite de tests.

---

## 6. Modèles de domaine — `trading_platform.core`

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

### 6.3 Erreurs — `trading_platform.core.errors`

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
├── ReportingError
└── RealtimeError
    ├── ProfileError             # configuration de profil invalide
    ├── MarketStreamError        # flux de marché en échec / reconnexions épuisées
    ├── StateStoreError          # échec de persistance, version de schéma incompatible
    ├── BrokerError              # défaillance du lieu d'exécution
    │   ├── BrokerUnavailableError   # extra optionnel ccxt/freqtrade manquant
    │   └── OrderRejectedError
    ├── GatewayError             # cycle de vie d'un ordre / réconciliation
    ├── LiveTradingForbiddenError    # porte live non satisfaite
    ├── RiskLimitExceededError   # une limite de risque du profil a bloqué l'ordre
    ├── KillSwitchActiveError    # kill switch global engagé
    └── MonitoringError          # échec du read model de la couche web
```

La branche `RealtimeError` couvre **tout** le temps réel : le moteur (couche 6)
et le transport de surveillance (couche 7) dérivent de la même racine, si bien
qu'un `except RealtimeError` attrape l'ensemble de la surface live. Elle est
déclarée dans `core.errors`, qui reste importable **sans aucun import du
projet** (règle de couche 1).

Aucun module ne lève d'exception « nue » : les erreurs de bas niveau de pandas
ou de pydantic sont converties à la frontière de la couche concernée.

---

## 7. The three seams (*coutures*)

The architecture rests on three deliberate injection points. They are not
details: they are what makes the project developable in parallel and testable
offline. The third one (7.3, *feature* injection) was added by the realtime
`timesfm` strategy delivery.

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

### 7.3 `strategy.features` — external features are injected

`trading_platform.strategy.features` (`resolve_features` / `attach_features` /
`FeatureBundle`) is the seam that brings a strategy what it cannot produce on its
own — the offline forecast artifact — **without** introducing input/output into
the pure `prepare()` / `signals()` contract. It is used by **every** execution
path: `run_backtest_on_config` / `make_runner` for the backtest and the validation
layers, and `realtime.strategies.resolve_strategy` for the realtime engine
(§4.10.7).

**Why:**

- **A single loader** — the parquet is read only through this seam: neither the
  engine, nor the orchestrator, nor the strategy knows the artifact format, and a
  second loading mechanism cannot diverge from the first.
- **The strategy stays pure** — the forecast enters by injection, so
  `prepare()` / `signals()` stay deterministic, I/O-free and clock-free, and
  testable without a model or a network.
- **The same artifact everywhere** — a realtime profile and a backtest that
  declare the same artifact receive identical features, which makes the parity of
  the two engines verifiable.

---

## 8. Points d'extension

| Besoin | Point d'extension |
| --- | --- |
| Nouvelle stratégie | hériter de `Strategy` (en implémentant `prepare` et `signals`, et en déclarant `ParamsModel` / `PARAM_SPACE`) et décorer la classe avec `@register_strategy`, puis référencer `cls.name` dans `strategy.name` de la configuration |
| Nouvel indicateur | fonction pure ajoutée à `trading_platform.strategy.indicators`, sans effet de bord |
| Nouvelle métrique | entrée ajoutée au dictionnaire `values` de `compute_metrics` + nom ajouté à `METRIC_NAMES` (l'ordre de `METRIC_NAMES` est l'ordre de calcul ; le tableau du rapport markdown trie les clés par ordre alphabétique) |
| New forecast backend | implement the `ForecastBackend` `Protocol` (`name`, `is_available`, `predict`) and decorate the class with `@register_backend` in `trading_platform.forecast`: the CLI (`--backend <name>`) and the artifact builder resolve it by name, without touching the strategy |
| Nouvel exécuteur | fonction conforme à `RunnerFn`, passée aux fonctions de validation (`walk_forward`, `parameter_sweep`) à la place de `make_runner(cfg)` |
| Nouveau mode de validation | nouveau module dans `trading_platform.validation`, exposé par la CLI, sans toucher au moteur |
| Nouvel exchange | implémentation dans `trading_platform.data`, l'import `ccxt` restant paresseux |
| Nouveau format de rapport | branche supplémentaire dans `write_report`, activée par `reporting.formats` |
| Exposer une stratégie à Freqtrade | **aucun code par stratégie** : `make_freqtrade_strategy(<nom enregistré>)` rend la classe `IStrategy` générique (§4.9.2), puis une déclaration de classe d'**une ligne** dans `user_data/strategies/<ClassName>.py`. Résolveur de shim : le texte du fichier doit contenir `class <Name>(` et `__module__` doit valoir le *stem* du fichier |

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

Limites **restantes** de la brique Freqtrade (§4.9) — elles sont assumées et
écrites ici pour ne pas être découvertes en production :

- **la table des stops vit en mémoire de process** (`entry_stop_map`) et n'est
  pas persistée : un redémarrage du bot perd le stop par trade des positions
  déjà ouvertes, qui retombe sur le stop global ;
- **les configurations sont spécifiques au mode d'exécution** :
  `config/freqtrade_dryrun.json` (paper) et `config/freqtrade_config.json`
  (live) sont deux fichiers distincts, à garder cohérents **à la main** avec
  `config/backtest_default.json` — le squelette ne synchronise rien ;
- **aucune orchestration multi-profils** n'existe encore : l'adaptateur expose
  *une* stratégie pour *un* couple actif/timeframe. Un profil = actif +
  stratégie + timeframe + paper/live reste à construire ; l'adaptateur en est la
  **brique 1**, pas la brique finale ;
- **les deux backtests ne produisent pas les mêmes chiffres** (§4.9.4), et aucun
  mécanisme ne cherche à les réconcilier : seuls les **signaux** sont le contrat.

### 9.1 Limites de la brique temps réel (couches 6 et 7)

La brique multi-profils existe désormais (§4.10, §4.11) et elle est **honnête**
sur ce qui n'est **PAS prouvé** : la liste ci-dessous dit exactement ce que la
suite de tests ne démontre pas. Elle est reprise mot pour mot dans
[`docs/realtime.md`](realtime.md#7-what-is-not-proven) :

- **le tableau de bord sonde en HTTP toutes les 2 s** (`monitoring.refresh_seconds`)
  et il n'y a **ni WebSocket ni ASGI** : c'est le prix de la décision
  « bibliothèque standard uniquement » (couche 7 sans aucune dépendance tierce),
  donc **pas de *push*** et une latence d'affichage bornée par la cadence de
  sondage ;
- **l'authentification se limite à un jeton opérateur unique**
  (`TB_OPERATOR_TOKEN`, comparaison en temps constant, et refus de toute mutation
  quand aucun jeton n'est configuré) : c'est une surface de **surveillance pour
  réseau de confiance**, pas une interface exposable sur Internet ;
- **les remplissages sont réconciliés sur un intervalle de sondage borné** : entre
  deux sondages, l'état local peut **retarder** sur le lieu d'exécution, et en
  mode papier les remplissages partiels sont **simulés de façon déterministe**
  (graine explicite) au lieu d'être modélisés depuis un **carnet d'ordres** réel ;
- **les décisions ne portent que sur des bougies fermées** : un profil live
  réagit **une frontière de timeframe après le signal**, exactement comme le
  moteur de backtest — ce n'est pas un bug, c'est la même convention d'exécution ;
- **aucun financement (*funding*), emprunt (*borrowing*), levier ni type d'ordre
  spécifique à un exchange** n'est modélisé ;
- **le trading live est implémenté mais il n'est PAS exercé contre un vrai
  exchange par la suite de tests** : les tests couvrent le courtier papier, la
  porte `TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK`, les limites de risque et
  tous les chemins d'erreur — jamais un ordre réel ;
- **le magasin SQLite est mono-écrivain** (*single-writer*, un seul processus) :
  un second orchestrateur sur le même fichier lève `StateStoreError`, ce
  n'est pas un magasin partagé ;
- un `run` **ancré** sur `realtime.start_at` rejoue l'historique aussi vite que le
  CPU le permet (mode *backfill*) : c'est déterministe et interruptible, mais ce
  n'est pas un suivi du temps réel.
