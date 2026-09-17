# Méthodologie de backtesting

Un backtest rentable n'est pas une preuve : c'est une **hypothèse**. Ce
document décrit le protocole de validation que le squelette implémente, les
hypothèses d'exécution du moteur, les seuils de décision, et — surtout — les
limites honnêtes de l'outil.

Voir aussi :

- [`docs/architecture.md`](architecture.md) — couches et interfaces gelées ;
- [`docs/usage.md`](usage.md) — comment lancer chaque commande ;
- [`docs/testing-policy.md`](testing-policy.md) — politique de tests.

---

## 1. Principe directeur : seul l'out-of-sample compte

Un résultat in-sample mesure la capacité d'une stratégie *et de son
optimiseur* à mémoriser un passé connu. Un résultat out-of-sample mesure la
capacité de la stratégie à affronter un futur qu'elle n'a jamais vu.

Règle appliquée dans tout le projet :

> **Toute sélection — paramètres, variante, seuil — se fait in-sample.
> L'out-of-sample est lu une fois, à la fin, et sert uniquement à juger.**

Toute performance citée dans un rapport est une performance OOS. Un chiffre IS
n'est affiché que comme référence de comparaison (c'est le rôle du ratio
d'efficacité du walk-forward, §4.3).

---

## 2. Contrôles qualité des données (avant toute chose)

Un backtest sur des données douteuses est un backtest faux, souvent flatteur.
`trading_backtest.data` exécute ces contrôles à la frontière et refuse une frame
non conforme plutôt que de deviner — sauf le dernier, qui n'est pas implémenté :

| Contrôle | Règle | Pourquoi |
| --- | --- | --- |
| Index temporel | `DatetimeIndex` tz-aware **UTC**, nommé `timestamp` | un index naïf ou mal converti décale tous les signaux |
| Tri | horodatages strictement croissants | un désordre produit des entrées dans le futur |
| Doublons | aucun horodatage répété | un doublon double le poids d'une bougie et fausse les indicateurs |
| Valeurs manquantes | aucun `NaN` sur `open/high/low/close/volume` | les indicateurs se propagent silencieusement à partir d'un `NaN` |
| Trou de données | trou supérieur à `max_gap_factor` (3 bougies par défaut) signalé | une bougie absente crée un saut de prix artificiel |
| Volume | volume strictement positif | un volume nul ou négatif signale une donnée inversée |
| Cohérence OHLC | `low <= min(open, close)` et `high >= max(open, close)` | détecte les données corrompues ou mal agrégées — **non implémenté** : à contrôler à la main, aucun code du projet ne teste cette règle |

Les deux fonctions de `trading_backtest.data.validation` ne font pas la même
chose : `ensure_ohlcv` **normalise** chaque frame (tri croissant, doublons
supprimés avec `keep="last"`, index naïf localisé en UTC, index renommé
`timestamp`, colonnes OHLCV castées en `float64`) et ne refuse que les entrées
structurellement invalides (colonnes absentes, index non temporel, colonnes non
numériques) ; `validate_ohlcv` **contrôle** et rapporte colonnes manquantes,
`NaN`, prix non positifs, volume non positif, index naïf, index non trié,
doublons, trous au-delà de `max_gap_factor` et `missing_ratio` au-delà de
`max_missing_ratio`.

En cas d'échec du contrôle (et avec `data.validate: true`, le défaut), **aucun
résultat n'est produit** : mieux vaut un run rouge qu'un rapport trompeur.

---

## 3. Découpage in-sample / out-of-sample

### 3.1 Split simple avec purge

`split_is_oos(data, *, in_sample_ratio=0.7, purge_candles=0)` découpe la série
positionnellement — **jamais aléatoirement** : in-sample = les
`floor(n × in_sample_ratio)` premières bougies moins les `purge_candles`
dernières (retirées de la **fin** de l'IS), out-of-sample = tout ce qui suit
`floor(n × in_sample_ratio)`. Les bougies purgées n'appartiennent donc à aucune
des deux moitiés, et chaque côté doit conserver au moins
`MIN_ROWS_PER_SLICE = 2` lignes (sinon `InsufficientDataError`).

`purge_candles` vaut **0 par défaut** dans la configuration (`ValidationConfig`) :
pour un usage sérieux, passez-le à `>= 1` (`TB_VALIDATION__PURGE_CANDLES=1` ou
`"purge_candles": 1` dans le JSON).

La purge existe parce que le moteur décide **à la clôture de la bougie `t`** et
exécute **à l'ouverture de la bougie `t+1`** : sans purge, le dernier signal
in-sample serait exécuté sur la première bougie out-of-sample, et l'information
la plus précieuse (la frontière) fuiterait du jeu de test vers le jeu
d'entraînement. Avec `purge_candles=1`, la dernière bougie IS est simplement jetée.

### 3.2 Fenêtres walk-forward

`make_windows(data, *, n_windows=5, in_sample_ratio=0.7, mode="rolling", purge_candles=0)`
découpe la série en `n_windows` blocs **de même taille**
(`block = n // n_windows` bougies ; la queue non divisible est ignorée) et
produit une liste de fenêtres `Window(index, is_start, is_end, oos_start, oos_end)` :

- **glissante** (`mode="rolling"`, défaut) : l'in-sample est la première
  `floor(block × in_sample_ratio)` partie du bloc, l'out-of-sample le reste du
  bloc. La fenêtre IS avance donc d'un bloc à chaque itération, à taille
  constante. Elle teste la **stabilité dans le temps** ;
- **ancrée** (`mode="anchored"`) : le début de la fenêtre IS est fixé à la
  première bougie de l'historique, la fenêtre s'allonge. Elle teste la
  **robustesse à l'accumulation de données** — utile pour détecter qu'une
  stratégie « vit » de ses premières observations.

Dans les deux modes, les `purge_candles` bougies de purge sont retirées de la
**fin** de l'in-sample (purge de frontière, §3.1) : pour un bloc commençant à
l'indice `b`, l'out-of-sample commence toujours à
`b + floor(block × in_sample_ratio)`, et les fenêtres OOS se succèdent sans
recouvrement — c'est la lecture la plus honnête.

Une série trop courte pour former `n_windows` blocs d'au moins une bougie IS et
une bougie OOS est refusée (`InsufficientDataError`) : on ne fabrique jamais une
fenêtre partielle.

---

## 4. Lecture du walk-forward

### 4.1 Ce qui est mesuré

Pour chaque fenêtre, le **même** jeu de paramètres (celui de la configuration)
est appliqué à l'IS puis à l'OOS : `walk_forward` appelle
`runner(slice, None)`, il **n'optimise rien**. Ce que l'on mesure est donc la
stabilité temporelle de la stratégie, pas la qualité d'un optimiseur : on
obtient une séquence de performances OOS, plus la métrique agrégée de l'OOS
recousu comparée à celle des fenêtres IS (le WFE, §4.3).

> Conséquence : la « sélection in-sample » du §1 est ici **manuelle** — c'est
> `parameter_sweep` (§5) qui balaye la grille, et c'est vous qui reportez le
> réglage retenu dans `strategy.params` avant de relancer le walk-forward.

### 4.2 Ce qui n'est **pas** permis

- Sommer les rendements OOS comme s'il s'agissait d'une courbe d'equity
  continue : les fenêtres ne couvrent qu'une fraction du calendrier.
- Réoptimiser après avoir vu l'OOS.
- Choisir la fenêtre la plus favorable : c'est du data snooping.

### 4.3 Ratio d'efficacité du walk-forward (WFE)

Le code agrège d'abord les runs : l'out-of-sample recousu d'un côté
(`aggregate_oos_metric`), les runs in-sample de l'autre
(`aggregate_is_metric`), puis rapporte les deux **métriques** — pas les
rendements :

```
efficiency (WFE) = aggregate_oos_metric / aggregate_is_metric
```

La métrique est celle de `metric` (`sharpe_ratio` par défaut, ou n'importe quel
nom de `METRIC_NAMES`). Un dénominateur non fini ou nul donne `0.0`.

Lecture :

| WFE | Interprétation |
| --- | --- |
| ≥ 0.5 | dégradation acceptable : la stratégie survit hors échantillon |
| 0.3 – 0.5 | dégradation forte : suspect, à documenter avant toute décision |
| < 0.3 | la performance IS était du bruit surappris — rejet |
| > 1.0 | à examiner de près : souvent un signe de chance sur quelques fenêtres |

### 4.4 Règle de consistance à 60 %

> **Au moins 60 % des fenêtres OOS doivent être positives.**

`is_consistent` vaut `consistency_ratio >= CONSISTENCY_THRESHOLD` avec
`CONSISTENCY_THRESHOLD = 0.6` et `consistency_ratio` = part des fenêtres dont
`oos_metric` est **strictement** positif. Le détail est exposé par
`WalkForwardResult` (`efficiency`, `is_consistent`, `consistency_ratio`,
`mean_is_metric`, `mean_oos_metric`, `n_oos_trades`, `stitched_oos_equity`).

Une moyenne portée par 1 fenêtre sur 9 n'est pas une stratégie, c'est un
accident. La consistance est le premier filtre, le WFE le second.

### 4.5 Exemple numérique complet

Données : `BTC/USDT`, timeframe `1h`, du **2022-01-01 00:00 UTC** au
**2022-12-31 23:00 UTC** — soit **8 760 bougies** (365 jours × 24).

Paramètres de fenêtrage : `is_size = 2160` (90 jours), `oos_size = 720`
(30 jours), `step = 720` (30 jours), mode glissant.

> L'exemple décrit la **géométrie** des fenêtres en bougies pour rendre le
> calcul lisible. Dans la configuration livrée, la même chose se déclare avec
> `validation.n_windows`, `validation.in_sample_ratio`, `validation.mode` et
> `validation.purge_candles` : `n=5` fenêtres glissantes sur 70 % in-sample, par
> défaut. `make_windows` procède par **blocs de taille égale**
> (`block = n // n_windows`, §3.2), pas par un pas de 720 bougies : la géométrie
> ci-dessous est donc une illustration, pas la sortie de `make_windows`.

> De même, le WFE ci-dessous est calculé sur la **moyenne des rendements** des
> fenêtres, pour que l'arithmétique se suive à la main. Le code, lui, divise la
> métrique du run OOS **recousu** (`aggregate_oos_metric`) par celle des runs IS
> agrégés (`aggregate_is_metric`), §4.3.

Nombre de fenêtres : la fenêtre `i` occupe les indices
`[i·720, i·720 + 2160 + 720)` ; il faut `i·720 + 2880 <= 8760`, donc
`i = 0…8`, soit **9 fenêtres**. La fenêtre 9 se termine à l'indice 8640
(2022-12-27) : les 120 dernières bougies ne forment pas un OOS complet et sont
ignorées.

| # | IS (début → fin) | Indices IS | OOS (début → fin) | Rendement IS | Rendement OOS |
| --- | --- | --- | --- | --- | --- |
| 1 | 2022-01-01 → 2022-03-31 | 0 – 2159 | 2022-04-01 → 2022-04-30 | +18.0 % | **+15.5 %** |
| 2 | 2022-01-31 → 2022-04-30 | 720 – 2879 | 2022-05-01 → 2022-05-30 | +21.0 % | **−4.2 %** |
| 3 | 2022-03-02 → 2022-05-31 | 1440 – 3599 | 2022-06-01 → 2022-06-30 | +24.5 % | **+18.0 %** |
| 4 | 2022-04-01 → 2022-06-30 | 2160 – 4319 | 2022-07-01 → 2022-07-30 | +20.0 % | **+12.5 %** |
| 5 | 2022-05-01 → 2022-07-30 | 2880 – 5039 | 2022-07-31 → 2022-08-29 | +15.0 % | **−2.8 %** |
| 6 | 2022-05-31 → 2022-08-29 | 3600 – 5759 | 2022-08-30 → 2022-09-28 | +12.0 % | **+11.0 %** |
| 7 | 2022-06-30 → 2022-09-28 | 4320 – 6479 | 2022-09-29 → 2022-10-28 | +9.5 % | **+13.2 %** |
| 8 | 2022-07-30 → 2022-10-28 | 5040 – 7199 | 2022-10-29 → 2022-11-27 | +7.0 % | **−4.1 %** |
| 9 | 2022-08-29 → 2022-11-26 | 5760 – 7919 | 2022-11-27 → 2022-12-26 | +5.5 % | **+13.8 %** |

Calcul :

```
moyenne IS  = (18.0 + 21.0 + 24.5 + 20.0 + 15.0 + 12.0 + 9.5 + 7.0 + 5.5) / 9
            = 132.5 / 9 = 14.72 %

moyenne OOS = (15.5 − 4.2 + 18.0 + 12.5 − 2.8 + 11.0 + 13.2 − 4.1 + 13.8) / 9
            = 72.9 / 9 = 8.10 %

WFE         = 8.10 / 14.72 = 0.55        → ≥ 0.5 : accepté

consistance = fenêtres OOS positives / total = 6 / 9 = 66.7 %   → ≥ 60 % : acceptée
```

**Verdict** : la stratégie passe l'étape walk-forward, avec une dégradation
hors échantillon de 45 % par rapport à l'in-sample. Note importante : ces 9
fenêtres OOS couvrent 270 jours (9 × 30) et non 365 — sommer les rendements OOS
comme une courbe continue serait une erreur de lecture.

---

## 5. Robustesse paramétrique

`parameter_sweep(runner, data, grid, *, base_params=None, metric="sharpe_ratio", max_combinations=512, initial_balance=10000.0)`
évalue **toute** la grille (`validation.robustness_grid`, produit cartésien des
valeurs, plafonné par `validation.robustness_max_combinations`, défaut `512` —
une grille plus grande est refusée, pas tronquée) et agrège trois indicateurs.
La métrique cible est `validation.robustness_metric` (défaut `sharpe_ratio`) :

| Indicateur | Définition | Ce qu'il détecte |
| --- | --- | --- |
| `stability` | `metric_mean / metric_std` sur la grille (`0.0` si l'écart-type est nul) | un optimum isolé sur une falaise de performance |
| `positive_ratio` | fraction des points de grille dont la métrique est **strictement positive** | une stratégie qui ne marche que sur un réglage |
| `robust_ratio` | fraction des points dont la métrique vaut au moins `0.5 × base_metric` (le point de référence `base_params` ; si `base_metric <= 0`, le seuil est `base_metric` lui-même) | la largeur du plateau de performance |

Règle de décision :

```
is_robust  =  (positive_ratio >= 0.7)  and  (robust_ratio >= 0.5)
```

Un `positive_ratio` de 0.7 signifie que **70 % de la grille** a une métrique
positive : c'est un plateau, pas un pic. Un `robust_ratio` de 0.5 signifie qu'au
moins une combinaison sur deux reste à mi-chemin du point de référence.

Anti-pattern que cela met en évidence : un point à +180 % entouré de voisins à
−30 % n'est **pas** un bon réglage, c'est un artefact d'échantillonnage. Le
choix doit se porter sur le **centre du plateau**, pas sur le maximum.

---

## 6. Monte Carlo sur les trades

`monte_carlo(result, *, n_simulations=..., method=..., random_seed=..., initial_balance=None)`
répond à une question différente : *le résultat observé dépend-il de l'ordre et
du tirage des trades ?* Les paramètres viennent de la configuration :
`validation.n_monte_carlo` (défaut `1000`), `validation.monte_carlo_method`
(défaut `"trade_resample"`) et `validation.random_seed` (défaut `42`) ; la CLI
les expose aussi par `--simulations`, `--method` et `--seed`. Aucun backtest
n'est relancé : les simulations rejouent les trades (ou les rendements) du
`BacktestResult` fourni.

### 6.1 Deux méthodes

| Méthode | Principe | Ce qu'elle teste |
| --- | --- | --- |
| `trade_resample` | on rééchantillonne **avec remise** la séquence des trades observés et on rejoue l'equity | la sensibilité à l'ordre et à la composition du flux de trades |
| `bootstrap_equity` | on rééchantillonne avec remise les **rendements par bougie** et on reconstruit l'equity | la sensibilité de la courbe d'equity, sans dépendre d'une liste de trades |

### 6.2 Statistiques produites

- **VaR 95 %** — quantile 5 % de la distribution des rendements simulés
  (`var_95`) : « dans 95 % des cas, le rendement ne descend pas sous X ». Elle
  vaut le plus souvent une valeur négative (une perte), mais reste positive
  quand même les tirages défavorables gagnent.
- **CVaR 95 %** — perte moyenne conditionnelle au-delà de ce quantile
  (`cvar_95`) : la sévérité des scénarios extrêmes, plus informative que la VaR
  seule.
- **Probabilité de profit** (`prob_profit`) — fraction des simulations terminant
  **au-dessus** du capital initial.
- **Distribution complète des rendements finaux** — `mean_return`,
  `median_return`, `std_return`, les percentiles `p05`, `p25`, `p50`, `p75`,
  `p95`, et les bornes en capital `worst_case_balance` / `best_case_balance`,
  pour situer le résultat observé dans son propre intervalle de confiance.

### 6.3 Deux hypothèses explicites

1. **Plancher d'equity à 0.** Une simulation qui atteint 0 est en **ruine** :
   l'equity est bornée à 0, jamais négative. Sans ce plancher, un tirage
   défavorable produirait une equity négative mathématiquement absurde (en
   pratique, la position est liquidée bien avant) et polluerait la queue de
   distribution. L'appauvrissement complet n'a pas de compteur dédié : il se lit
   dans `worst_case_balance`, `max_drawdowns` et la queue basse de `returns`.
2. **Les trades sont supposés échangeables.** Le rééchantillonnage ignore la
   corrélation entre trades et les régimes de marché : il sous-estime donc la
   vraie queue de risque. Un Monte Carlo large **ne remplace pas** un
   walk-forward ; il le complète.

Tout est piloté par une graine explicite (`random_seed`, défaut `42`) : mêmes
données + même graine = mêmes résultats.

Un `BacktestResult` sans aucun trade ne lève pas : les `n_simulations` tirages
rendent alors `0.0` avec une equity plate — le Monte Carlo ne peut rien dire
d'un run vide.

---

## 7. Hypothèses d'exécution du moteur (contrat)

Ces hypothèses sont **explicites** dans le code et doivent l'être dans la
lecture des résultats.

1. **Décision à la clôture de la bougie `t`** (`close[t]`), sur des indicateurs
   calculés uniquement avec les données `<= t`.
2. **Exécution à l'ouverture de la bougie `t+1`** (`open[t+1]`), jamais à
   `close[t]`. Aucun ordre n'est exécuté sur la bougie qui l'a produit : c'est
   la protection anti-*look-ahead* structurelle du moteur.
3. **Frais et slippage** : les frais valent
   `fee_rate × taille × (prix_entrée + prix_sortie)` et sont **déduits de
   `pnl`** ; le slippage joue contre nous sur les deux jambes (majoration à
   l'entrée, minoration à la sortie) — **sauf** une sortie sur stop, qui est
   remplie exactement au prix du stop.
4. **Stop-loss statique** : le moteur ne calcule pas le stop, il lit la colonne
   `stop_loss` de la **bougie de signal** (`close − atr_stop_multiplier × ATR`
   pour `basic`), la convertit en prix au remplissage, puis ne la bouge plus
   jusqu'à la sortie. Le stop est testé intrabar (`low` pour un long, `high`
   pour un short) **dès la bougie d'entrée**, et un `NaN` dans la colonne
   signifie « pas de stop ». Pas de trailing stop, pas de stop dynamique.
5. **Une seule position à la fois**, taille fixe, sans levier, sans
   augmentation ni réduction de position.
6. **Fins de période** : une position encore ouverte à la dernière bougie est
   close sur place et marquée `end_of_data` dans `exit_reason` — elle compte
   dans les métriques.

### 7.1 Exemple numérique d'une exécution

- Compte : 10 000 USDT (`backtest.initial_balance` par défaut).
- Frais : 10 bps (`exchange.fee_rate` / `backtest.fee_rate = 0.001` par défaut).
- Slippage : 5 bps dans cet exemple — le défaut du projet est `0.0`, car le
  slippage est une hypothèse à activer explicitement plutôt qu'un réglage
  implicite.
- `stake_amount = 1000`, `ATR = 900` sur la bougie de signal,
  `atr_stop_multiplier = 2.5`.

| Étape | Bougie | Valeur |
| --- | --- | --- |
| Signal (croisement EMA + RSI valide) | `t` = 2022-04-01 12:00 UTC, clôture | `close[t] = 45 000` |
| Colonne `stop_loss` du signal | `t` | `45 000 − 2.5 × 900 = 42 750` |
| Exécution de l'entrée | `t+1` = 2022-04-01 13:00 UTC, ouverture | `open[t+1] = 45 120` |
| Prix d'entrée avec slippage | — | `45 120 × 1.0005 = 45 142.56` |
| Taille | — | `size = 1000 / 45 142.56 = 0.022152` |
| Stop touché | bougie où `low <= 42 750` | `low = 42 700` |
| Prix de sortie (au stop, sans slippage) | — | `42 750.00` |
| P&L brut | — | `0.022152 × (42 750 − 45 142.56) = −53.00 USDT` |
| Frais (aller-retour) | — | `0.001 × 0.022152 × (45 142.56 + 42 750) = −1.95 USDT` |
| P&L net (`TradeRecord.pnl`) | — | `−53.00 − 1.95 = −54.95 USDT` |

Lecture :

- `pnl = −54.95 USDT` (`TradeRecord.pnl`, net de frais) ;
- `pnl_pct = −5.49 %` — rapporté au capital **engagé** (1 000 USDT) ;
- rendement du **compte** = `−54.95 / 10 000 = −0.55 %`.

Ne jamais confondre les deux : un `pnl_pct` flatteur avec un `stake_amount`
faible se dilue dans le rendement du compte.

---

## 8. Pièges de surapprentissage et parades

| Piège | Comment il se manifeste | Parade dans le squelette |
| --- | --- | --- |
| *Look-ahead bias* | indicateur calculé avec une donnée future, fill sur la bougie du signal | signal à `close[t]`, fill à `open[t+1]`, purge à la frontière IS/OOS |
| Fuite à la frontière du split | le dernier signal IS s'exécute dans l'OOS | `purge_candles` de frontière dans `split_is_oos` |
| Sélection sur l'OOS | on réoptimise après avoir vu le résultat de test | l'OOS n'est lu qu'une fois, en fin de pipeline |
| *Data snooping* / multiple testing | on essaie 500 variantes et on publie la meilleure | grille bornée et déclarée, `positive_ratio` / `robust_ratio` |
| Optimum sur une falaise | un point excellent isolé parmi des voisins médiocres | stabilité de la grille, choix du centre du plateau |
| Coûts ignorés | courbe parfaite sans frais ni slippage | frais et slippage appliqués sur les deux jambes |
| Échantillon trop court | 6 trades « suffisent » à conclure | lecture de `n_trades` et de la consistance avant toute conclusion |
| Période unique favorable | un seul split chanceux | walk-forward glissant sur plusieurs fenêtres |
| Somme de rendements OOS | courbe d'equity fictive | fenêtres OOS traitées indépendamment |
| Sur-confiance au Monte Carlo | queue de risque sous-estimée | hypothèse d'échangeabilité documentée (§6.3) |

---

## 9. Limites honnêtes du squelette

Ce squelette est un **cadre**, pas un moteur de production. Ce qu'il ne modélise
pas, et qui doit être assumé dans toute conclusion :

1. **Une seule position simultanée** — pas de portefeuille multi-actifs, pas de
   corrélation entre actifs.
2. **Stop statique pris à l'entrée** — le risque par trade est figé ; un stop
   dynamique changerait la distribution des pertes.
3. **Sans levier** — la ruine est bornée, les rendements sont donc plafonnés par
   le capital disponible.
4. **Sans coût de funding ni de borrowing** — un backtest perpétuel ou short
   réel serait plus cher que ce que le moteur calcule.
5. **Sans impact de marché ni contrainte de liquidité** — la taille de position
   n'influence jamais le prix d'exécution.
6. **Données synthétiques dans les tests** — garantit le déterminisme et
   l'absence de réseau, mais ne prouve **rien** sur un actif réel.
7. **Pas d'hyperopt, pas de recherche bayésienne** — le balayage paramétrique
   est une grille bornée, volontairement modeste pour limiter le data snooping.

Conséquence méthodologique :

> Un backtest vert est une **condition nécessaire**, jamais suffisante. L'étape
> suivante est le paper trading (dry-run) sur flux réel, puis une exposition
> réduite, puis seulement une montée en taille progressive.

---

## 10. Ordre d'exécution recommandé

```
1. data download            → données propres, contrôlées
2. backtest                 → une mesure de référence IS et OOS
3. robustness               → la performance est-elle un plateau ou un pic ?
4. walk-forward             → la performance survit-elle au temps ?
5. monte-carlo              → quelle est la queue de risque ?
6. rapport                  → décision documentée, ou rejet documenté
```

Chaque étape peut invalider la précédente. L'objectif du squelette n'est pas de
produire un chiffre rassurant : c'est de rendre **difficile** de se raconter une
histoire fausse.
