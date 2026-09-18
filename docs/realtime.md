# Temps réel multi-profils

Ce document décrit la plateforme temps réel livrée avec le squelette :

- **`trading_backtest.realtime`** (couche 6) — moteur : flux de marché, courtier,
  passerelle d'exécution, risque, persistance, orchestrateur, observabilité ;
- **`trading_backtest.web`** (couche 7) — transport HTTP **bibliothèque standard
  uniquement** et tableau de bord statique ;
- **`trading_backtest.cli`** (couche 8) — les trois commandes
  `realtime run`, `realtime serve` et `realtime check`.

Il complète [`docs/architecture.md`](architecture.md) (§3 pour les couches,
§4.10 et §4.11 pour l'inventaire des interfaces gelées, §6.3 pour l'arbre des
erreurs) et [`docs/usage.md`](usage.md) (exemples copiables).

---

## 1. Qu'est-ce qu'un profil ?

Un **profil** est une stratégie de trading autonome : `asset + stratégie +
timeframe + paper/live + limites de risque`. Chaque profil est configuré,
exécuté et surveillé **indépendamment** des autres ; N profils tournent dans le
même processus, sur le même état persistant, sans partager ni positions ni
compteurs.

Un profil est décrit par un objet JSON du fichier de profils (voir
`config/profiles.example.json`) et validé par `config.models.ProfileConfig`,
qui refuse toute clé inconnue (`extra="forbid"`) :

| Champ `ProfileConfig` | Rôle |
| --- | --- |
| `id` | identité du profil (`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`) : clé primaire de l'état et du journal |
| `symbol` | paire négociée, par exemple `BTC/USDT` |
| `timeframe` | `1m`, `5m`, `15m`, `30m`, `1h`, `4h` ou `1d` |
| `strategy` | nom du registre `strategy.registry.get_strategy` (jamais réimplémentée) |
| `params` | paramètres de la stratégie (mêmes conventions que `AppConfig`) |
| `mode` | `paper` (simulé) ou `live` (réel, sous condition, voir §3) |
| `initial_balance` | capital initial du profil |
| `stake_amount` | montant engagé par entrée (défaut : tout le solde disponible) |
| `exchange` | nom du lieu d'exécution |
| `enabled` | un profil désactivé est persisté mais jamais démarré |
| `warmup_candles` | nombre de bougies passées que la stratégie reçoit à chaque décision |
| `poll_interval_seconds` | cadence de sondage propre au profil |
| `risk` | bloc `RiskLimitsConfig` (§3) |

Le document complet porte trois clés racine : `profiles`, `realtime`
(`RealtimeConfig` : base d'état, répertoires, provider CSV ou cache, ancre
`start_at`, délais, reconnexions, benchmark) et `monitoring`
(`MonitoringConfig` : `host`, `port`, `refresh_seconds`,
`request_timeout_seconds`, `max_request_bytes`). Toute autre clé racine est
refusée.

## 2. Un seul chemin d'exécution pour paper et live

Il existe **une** `ExecutionGateway` et **un** `ProfileRunner`. Paper et live ne
diffèrent que par trois choses :

1. **le courtier injecté** — `PaperBroker` (lieu simulé : exécution au prix de
   référence ajusté du slippage, frais maison, remplissages partiels
   déterministes pilotés par une graine explicite) ou `CcxtBroker` (lieu réel,
   `import ccxt` **paresseux**, idempotent sur `clientOrderId`) ;
2. **la porte live** (`LiveTradingGate`) — un profil `live` n'est armé que si
   l'environnement porte exactement
   `TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK` ;
3. **la configuration**.

La passerelle ne contient **aucune** branche « si paper / si live » : elle route
vers le `Broker` injecté. Le courtier papier est donc exercé par exactement les
mêmes tests que le cycle de vie partagé.

### 2.1 Équivalence temps réel / backtest

Le contrat d'exécution est celui de `strategy.engine` : **la décision est
évaluée à la clôture de la bougie `t` et exécutée à l'ouverture de `t+1`**, le
slippage étant toujours défavorable au trader. En temps réel, la clôture de `t`
**est** l'instant de déclenchement : le flux n'émet que des bougies fermées
(`timestamp <= now - candle_delta(timeframe)`) et le prix de référence d'une
décision est la **clôture de `t`** — soit l'ouverture de `t+1` à la granularité
d'une bougie. Conséquence assumée : un profil live réagit **une frontière de
timeframe après le signal**, exactement comme le moteur de backtest, et les deux
moteurs ne produisent pas les mêmes chiffres (seuls les **signaux** sont le
contrat).

### 2.2 Réutilisation obligatoire

Le moteur temps réel **n'implémente aucune formule**. Il assemble :

- les stratégies du registre (`strategy.registry.get_strategy` + `Strategy.run`) ;
- les données (`data.loader` fournisseurs et `data.cache`) et
  `data.validation.ensure_ohlcv` pour **chaque** trame entrant dans le moteur ;
- les métriques et benchmarks (`metrics.compute_metrics`,
  `metrics.compute_benchmark`, `metrics.compare_benchmark`, `benchmark_alpha`,
  `validation.validate_benchmark`) via le read model `realtime.monitor` ;
- les modèles de domaine (`core.models.TradeRecord`, `BacktestResult`) ;
- la couche de configuration pydantic ;
- le pont Freqtrade (`strategy.freqtrade_adapter.make_freqtrade_strategy`),
  exposé par `realtime.strategies.freqtrade_strategy_for(profile)` (import
  paresseux, rend `None` quand l'extra est absent) : **la même définition de
  profil peut être remise à Freqtrade sans changement**.

## 3. Le modèle de sûreté

1. **Opt-in explicite du live.** Un profil `live` exige à la fois
   `mode: "live"` **et** `TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK` ;
   sinon `LiveTradingForbiddenError`.
2. **Secrets uniquement par l'environnement.** `TB_LIVE_API_KEY` /
   `TB_LIVE_API_SECRET` / `TB_LIVE_API_PASSWORD`, ou par profil
   `TB_PROFILE_<ID>_API_KEY` / `_API_SECRET`. Les modèles de configuration
   n'ont **aucun** champ de credential : un profil contenant `api_key` échoue
   bruyamment (`ConfigError`). `ExchangeCredentials.__repr__` et chaque
   journalisation structurée masquent les secrets.
3. **Limites par profil, évaluées avant l'appel au courtier**
   (`RiskLimitsConfig` → `RiskLimits`) : `max_position_notional`,
   `max_order_notional`, `max_open_positions`, `max_daily_loss`,
   `max_drawdown_pct`, `max_daily_trades`. L'ordre d'évaluation est gelé et le
   **premier** échec gagne ; chaque refus est journalisé avec sa raison, et le
   profil n'est pas watermarqué (le refus est une décision, pas un plantage).
4. **Kill switch global** (`KillSwitch`), sous trois formes : fichier
   (`realtime.kill_switch_file`), environnement, et API (`POST /api/kill-switch`).
   Il arrête tous les profils, **n'annule rien en silence**, et il est persisté
   dans l'état : un redémarrage ne le remet pas à zéro. Un kill switch forcé par
   un fichier ou l'environnement ne peut pas être libéré par l'API.
5. **Séparation paper/live infalsifiable.** Un profil papier ne peut **jamais**
   être routé vers un courtier réel : le mode fait partie de l'identité du
   profil et de chaque ordre persisté, et le courtier vérifie son propre mode.

## 4. Persistance, redémarrage et réconciliation

- L'état vit dans **un seul fichier SQLite** (`realtime.state_db`, défaut
  `data/realtime/state.db`, ignoré par git), en mode WAL, une connexion par
  thread appelant, **toutes les écritures en transaction**.
- Chaque écriture est **idempotente** : UPSERT sur clé naturelle, et l'identifiant
  de commande est **déterministe** — `new_client_order_id(profile_id, symbol,
  candle_timestamp, sequence)` — donc la même décision produit toujours le même
  `client_order_id`. Un redémarrage entre la soumission et le remplissage ne
  double **jamais** un ordre.
- La **dernière bougie traitée** est persistée par profil : un redémarrage ne
  rejoue pas une bougie et n'en saute pas non plus.
- Au démarrage, l'orchestrateur **réconcilie** l'état local contre le lieu
  d'exécution (`Broker.reconcile()`) et marque le profil `degraded` en cas
  d'écart. Les remplissages sont réconciliés à intervalle borné
  (`realtime.reconcile_interval_seconds`).
- Le magasin est **mono-écrivain** : un second orchestrateur sur le même fichier
  échoue avec `StateStoreError` (verrou `flock` posé sur `<base>.lock`). Ce
  verrou est **consultatif** : il protège deux `SqliteStateStore`, pas un
  processus tiers qui écrirait dans le fichier en contournant le magasin. Une
  version de schéma plus récente lève également `StateStoreError` au lieu
  d'écrire à l'aveugle.
- Un écart de réconciliation sur un lieu **en mémoire** est attendu après un
  redémarrage : le courtier papier ne connaît pas les ordres qu'il n'a pas vus,
  donc le profil repart en `degraded` — l'état local, lui, est intact et rien
  n'est re-soumis. C'est le comportement documenté de D7, pas une perte de
  données.
- Un lieu **simulé** n'a pas de mémoire : après un redémarrage, son cash repart
  du solde initial alors que la position est restaurée depuis le magasin.
  L'orchestrateur **réamorce donc le cash du courtier papier** avec le dernier
  point d'equity persisté (`PaperBroker.restore_cash`) avant la première bougie,
  sans quoi la position serait comptée deux fois et le tableau de bord
  afficherait une equity que la courbe persistée contredit. Un lieu **réel**
  n'est jamais réamorcé : il publie son propre solde
  (`CcxtBroker.fetch_balance`).

## 5. Reference de l'API web

Serveur `http.server.ThreadingHTTPServer`, JSON partout, horodatages ISO-8601
UTC, **aucun NaN ni Infinity** dans un payload. Les seuls actifs statiques
servis sont `app.js` et `styles.css` (liste blanche fixe : toute traversée de
chemin répond 404). Le tableau de bord **sonde** `GET /api/profiles` toutes les
2 secondes (`monitoring.refresh_seconds`) et dessine les courbes d'equity en
`canvas`/SVG en ligne, sans bibliothèque.

| Méthode et route | Réponse 200 | Erreurs |
| --- | --- | --- |
| `GET /` | tableau de bord HTML | 404 |
| `GET /static/{asset}` | `app.js` ou `styles.css` | 404 (actif inconnu ou traversée) |
| `GET /api/health` | `{status, version, uptime_seconds, profiles_total, profiles_running, kill_switch, checked_at}` | — |
| `GET /api/profiles` | `{profiles: [ProfileSnapshot…], generated_at}` | — |
| `GET /api/profiles/{id}` | `ProfileSnapshot` | 404 `{error}` |
| `GET /api/profiles/{id}/equity` | `{points: [{timestamp, equity, cash, position_value}…]}` | 404 |
| `GET /api/profiles/{id}/trades` | `{trades: [...], count}` | 404 |
| `GET /api/profiles/{id}/orders` | `{orders: [...]}` | 404 |
| `GET /api/profiles/{id}/positions` | `{positions: [...]}` | 404 |
| `GET /api/profiles/{id}/metrics` | `{metrics: {...}, benchmark: {...} \| null, generated_at}` | 404 |
| `POST /api/kill-switch` | corps `{engage: bool, reason: str}` → `{kill_switch, reason, changed_at}` | 400 corps malformé, 403 jeton absent/invalide, 403 serveur en lecture seule |

`realtime serve` ouvre la base d'état en lecture/écriture de schéma : si le
fichier n'existe pas encore, il est **créé** (schéma vide) et le tableau de bord
affiche une plateforme sans profil. Lancez d'abord `realtime run --once` pour
peupler l'état avant de le servir.

Codes transverses : `400` requête malformée, `404` route ou profil inconnu,
`405` méthode incorrecte (avec en-tête `Allow`), `500` avec `{error}` — **jamais**
de trace sur le réseau. Le jeton opérateur unique vient de `TB_OPERATOR_TOKEN`
(comparaison en temps constant) ; sans jeton configuré, les routes mutantes
refusent **tout** le monde. Un serveur démarré par `realtime serve` est
**lecture seule** : `POST /api/kill-switch` répond 403.

## 6. Les trois commandes

```bash
# pré-vol statique : ne passe AUCUN ordre et ne touche PAS au réseau
trading-backtest realtime check --profiles config/profiles.example.json --json

# moteur + tableau de bord
trading-backtest realtime run --profiles config/profiles.example.json
trading-backtest realtime run --profiles config/profiles.example.json --host 127.0.0.1 --port 8080

# un seul tick déterministe (ancre realtime.start_at), puis sortie 0
trading-backtest realtime run --profiles config/profiles.example.json --once --json

# surveillance seule, en lecture seule, sur l'état persisté
trading-backtest realtime serve --profiles config/profiles.example.json --port 8080
```

Payloads (clés exactes) :

- `realtime check` → `{command: "realtime-check", ok, config_path, state_db,
  state_db_writable, kill_switch, profiles: [{id, symbol, timeframe, strategy,
  mode, ok, issues, credentials_present, live_gate_allowed, risk}], issues}`.
  La clé **`issues` de premier niveau** porte les problèmes *de plateforme*
  (document illisible, répertoire d'état non inscriptible) ; les `issues` de
  chaque profil portent les problèmes *du profil*. Sortie `1` dès qu'un profil ne
  peut pas démarrer.
- `realtime run` / `run --once` → `{command: "realtime-run", ok, config_path,
  state_db, profiles: [ProfileSnapshot…], decisions: [TradeSignalDecision…],
  url}`. `url` vaut `null` avec `--once` et `http://host:port/` sinon ; `--once`
  ne démarre **aucun** serveur.
- `realtime serve` → `{command: "realtime-serve", ok, config_path, state_db,
  profiles: [ProfileSnapshot…], url}`.

En mode `--json`, l'URL de démarrage est annoncée sur **stderr** : stdout ne
contient qu'un seul objet JSON. `SIGINT` arrête proprement le serveur et le
moteur, puis sort avec le code `0`.

L'horloge : `realtime.start_at` arme un `ManualClock` (replay déterministe)
sinon `SystemClock` est utilisé. Le `ManualClock` injecté par la CLI **rend la
main à la boucle d'événements** après chaque `sleep` : le moteur ne se cadence
que par `Clock.sleep`, donc un manuel non coopératif affamerait la boucle, ses
minuteurs (`asyncio.wait_for`) ne se déclencheraient plus et `SIGINT` ne serait
jamais délivré. Contrepartie assumée : un `run` **ancré** rejoue l'historique
aussi vite que le CPU le permet (c'est du backfill), ce n'est pas un suivi du
temps réel.

## 7. Ce qui n'est PAS prouvé

- Le tableau de bord **sonde** en HTTP toutes les 2 secondes : il n'y a **ni
  WebSocket ni ASGI** (décision « bibliothèque standard uniquement »), donc pas
  de *push*, et la latence d'affichage est bornée par la cadence de sondage.
- L'authentification se limite à un **jeton opérateur unique** : c'est une
  surface de **surveillance pour réseau de confiance**, pas une interface
  exposable sur Internet.
- Les remplissages sont réconciliés sur un **intervalle de sondage borné** :
  entre deux sondages, l'état local peut retarder sur le lieu d'exécution. En
  mode papier, les remplissages partiels sont **simulés de façon déterministe**
  (graine explicite) et ne modélisent **pas** un carnet d'ordres réel.
- Les décisions sont prises sur des bougies **fermées** uniquement : un profil
  live réagit une frontière de timeframe après le signal, exactement comme le
  moteur de backtest.
- **Aucun** financement, emprunt, levier ou type d'ordre spécifique à un
  exchange n'est modélisé (les ordres sont des ordres au marché, éventuellement
  limites touchées).
- Le trading **live est implémenté mais il n'est PAS exercé contre un vrai
  exchange par la suite de tests** : les tests couvrent le courtier papier, la
  porte live, les limites de risque et les chemins d'erreur, jamais un ordre
  réel.
- Le magasin SQLite est **mono-écrivain** : un second orchestrateur sur le même
  fichier lève `StateStoreError` (comportement testé, pas une garantie de
  partage).
- `realtime check` atteste l'**absence** de credentials, pas leur validité : il
  n'ouvre aucune connexion, donc un couple clé/secret invalide ne sera détecté
  qu'au premier appel réel du courtier.
