# tests/fixtures

Small, committed, **fully offline** OHLCV fixtures. No market data is ever
downloaded by the test-suite (`docs/testing-policy.md` §1).

| File | Rows | Content |
| --- | --- | --- |
| `BTC_USDT-1h.csv` | 200 | 200 hourly candles starting `2023-01-01T00:00:00Z`, columns `timestamp,open,high,low,close,volume`, ISO-8601 UTC timestamps |

## Provenance

`BTC_USDT-1h.csv` is generated, not downloaded:

```bash
.venv/bin/python -c "
from trading_platform.data.synthetic import make_trending_ohlcv
f = make_trending_ohlcv(n=200, period=45, amplitude=8.0, seed=7, start='2023-01-01T00:00:00Z')
o = f.copy(); o.insert(0, 'timestamp', o.index.strftime('%Y-%m-%dT%H:%M:%SZ'))
o.to_csv('tests/fixtures/BTC_USDT-1h.csv', index=False, float_format='%.6f')"
```

The data is perfectly regular (no gap, no duplicate, no `NaN`) so it passes the
`data.max_missing_ratio = 0.0` quality gate of `config/backtest_default.json`,
and it produces a handful of trades with the `basic` strategy — which is what
makes the CLI end-to-end tests meaningful (`tests/test_cli.py`).
