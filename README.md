# trading

Framework de backtesting modulaire, robuste et scalable pour bots de trading
algorithmique, basé sur **Freqtrade**.

## Objectif

Valider rigoureusement l'efficacité d'une stratégie de trading — pas seulement
produire un joli chiffre de rendement. Le projet impose un pipeline de
validation complet : exploration, walk-forward, out-of-sample strict, tests de
robustesse, puis paper trading avant tout capital réel.

## Statut

🚧 Projet en construction — squelette d'architecture en cours de mise en place.

## Démarrage rapide

```bash
# 1. installation de développement
python3.11 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"

# 2. jeu de données hors ligne déterministe (aucun accès réseau)
.venv/bin/python -c "from trading_backtest.data.synthetic import make_ohlcv; \
make_ohlcv(8760, start='2022-01-01T00:00:00Z', timeframe='1h', seed=42).to_csv('btc.csv')"

# 3. backtest sur ce CSV
.venv/bin/python -m trading_backtest.cli backtest \
    --config config/backtest_default.json --data-file btc.csv
```

La validation statistique enchaîne ensuite `walk-forward`, `robustness` puis
`monte-carlo` sur le même CSV. Le détail des commandes, de la configuration et
des rapports est dans [`docs/usage.md`](docs/usage.md).

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — architecture du projet, couches et interfaces
- [`docs/backtesting-methodology.md`](docs/backtesting-methodology.md) — méthodes de validation et seuils de décision
- [`docs/usage.md`](docs/usage.md) — installation, configuration, CLI, rapports, Docker
- [`docs/testing-policy.md`](docs/testing-policy.md) — politique de tests et de couverture

## Licence

Privé — tous droits réservés.
