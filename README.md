# trading

Plateforme de trading modulaire, robuste et scalable, basée sur **Freqtrade** :
un moteur de backtesting (walk-forward, out-of-sample, robustesse, Monte Carlo)
**et** un moteur temps réel multi-profils (paper/live, persistance, arrêt
d'urgence, tableau de bord de surveillance).

## Objectif

Valider rigoureusement l'efficacité d'une stratégie de trading — pas seulement
produire un joli chiffre de rendement. Le projet impose un pipeline de
validation complet : exploration, walk-forward, out-of-sample strict, tests de
robustesse, puis paper trading avant tout capital réel.

La stratégie s'écrit **une seule fois**, dans la couche maison
(`src/trading_platform/strategy/`) : un **adaptateur** la traduit vers le
contrat `freqtrade.strategy.IStrategy`, si bien que **la même définition pilote
à la fois le backtest maison et Freqtrade** (dry-run puis live) — sans dupliquer
indicateurs ni règles d'entrée/sortie. Les deux backtests ne produisent pas les
mêmes chiffres (les signaux sont partagés, pas le moteur d'exécution) : c'est
documenté noir sur blanc dans
[`docs/architecture.md`](docs/architecture.md#49-ladaptateur-freqtrade-écrire-une-stratégie-une-fois-lexposer-deux-fois).

## Statut

🚧 Projet en construction — squelette d'architecture en cours de mise en place.

## Démarrage rapide

```bash
# 1. installation de développement
python3.11 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"

# 2. jeu de données hors ligne déterministe (aucun accès réseau)
.venv/bin/python -c "from trading_platform.data.synthetic import make_ohlcv; \
make_ohlcv(8760, start='2022-01-01T00:00:00Z', timeframe='1h', seed=42).to_csv('btc.csv')"

# 3. backtest sur ce CSV
.venv/bin/python -m trading_platform.cli backtest \
    --config config/backtest_default.json --data-file btc.csv
```

La validation statistique enchaîne ensuite `walk-forward`, `robustness` puis
`monte-carlo` sur le même CSV. Le détail des commandes, de la configuration et
des rapports est dans [`docs/usage.md`](docs/usage.md).

Une fois la stratégie validée, **la même définition** est exposée à Freqtrade
(dry-run puis live) via l'adaptateur, sans réécrire la moindre règle :
`make_freqtrade_strategy("basic")` plus le shim
`user_data/strategies/BasicStrategy.py`. Voir
[`docs/architecture.md`](docs/architecture.md#49-ladaptateur-freqtrade-écrire-une-stratégie-une-fois-lexposer-deux-fois)
et la section « Exposer une stratégie à Freqtrade / dry-run » de
[`docs/usage.md`](docs/usage.md).

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — architecture du projet, couches et interfaces
- [`docs/backtesting-methodology.md`](docs/backtesting-methodology.md) — méthodes de validation et seuils de décision
- [`docs/usage.md`](docs/usage.md) — installation, configuration, CLI, rapports, Docker
- [`docs/realtime.md`](docs/realtime.md) — temps réel multi-profils : moteur, paper/live, persistance, dashboard
- [`docs/testing-policy.md`](docs/testing-policy.md) — politique de tests et de couverture
- [`deploy/README.md`](deploy/README.md) — déploiement local du tableau de bord temps réel (Docker, nginx, TLS, fail2ban)

## Licence

Privé — tous droits réservés.
