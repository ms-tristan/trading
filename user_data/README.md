# `user_data/` — strategies shipped with the platform

This directory contains the freqtrade strategies of the platform. Everything
here is plain freqtrade: no wrapper, no import path magic, no generated file.

```
user_data/
├── README.md                 # this file
└── strategies/
    ├── BasicStrategy.py      # EMA(20) / EMA(50) crossover baseline
    ├── MomentumStrategy.py   # ROC(12) momentum inside an EMA(50) > EMA(200) trend
    ├── RsiReversionStrategy.py  # RSI(14) recovery out of oversold above SMA(200)
    ├── BollingerStrategy.py  # lower Bollinger(20, 2.0) band touch
    ├── MacdStrategy.py       # MACD(12, 26, 9) histogram zero-line cross
    ├── DonchianStrategy.py   # 20-candle channel breakout, ATR(14) chandelier exit
    ├── KeltnerStrategy.py    # EMA(20) +/- 2 * ATR(10) breakout
    ├── SupertrendStrategy.py # Supertrend(10, 3.0) direction flip
    ├── DualThrustStrategy.py # 4-candle range breakout on both sides of the open
    └── FaberStrategy.py      # 200-period trend filter (Mebane Faber)
```

## Adding a strategy: the two-file recipe

Adding a profile to the platform never touches Python outside this directory and
never touches the realtime service. Two files are involved — one you create, one
you edit:

1. **Drop `<Name>Strategy.py` into `user_data/strategies/`.**
   The file stem and the class name must be identical: `MyStrategy.py` defines
   exactly one strategy class, `class MyStrategy(IStrategy)`. That is how
   freqtrade resolves a profile (`--strategy MyStrategy` looks for
   `MyStrategy.py` and for the class `MyStrategy(` inside it), and how the
   dashboard and the profile catalogue refer to it.

2. **Add one entry for it to `config/strategies.json`.**
   That catalogue is the single source of truth for the display metadata of a
   profile: it is keyed by the strategy class name and carries the title, the
   description and the optional per-profile overrides the dashboard shows and
   the launcher applies. Open the file, copy an existing entry, adapt it — the
   field names and their meaning are documented there and in the platform
   documentation.

   **The catalogue entry is optional.** A strategy file with no entry is still
   discovered by the launcher and by the dashboard: the profile then simply
   shows the title derived from the class name (`MyStrategy` is displayed as
   `My Strategy`) with no custom description. The catalogue entry only makes the
   profile nicer to read and lets it carry overrides; it never enables the
   strategy, so a missing entry can never make a strategy silently disappear.

Nothing else is needed: no registration call, no import, no restart of the
realtime image, no change to `config/strategies.json` schema.

## Timeframes

A strategy class declares a default `timeframe` — for the ten shipped
strategies it is the primary timeframe listed for the profile in
`config/strategies.json`. A profile is not bound to that default: a profile runs
the strategy on any timeframe its catalogue entry lists, and the timeframe of
the profile wins, because freqtrade applies the configuration timeframe over the
class attribute when it loads the strategy. The dashboard and the launcher read
the allowed list from the catalogue, so the only place where a new timeframe has
to be declared is `config/strategies.json`.

## Required class contract

Every strategy file in `user_data/strategies/` must satisfy the following
contract. It is enforced by `tests/strategies/test_strategy_loading.py`, which
loads each file through freqtrade's own `StrategyResolver`.

* **Module docstring** naming the public source of the idea (the paper, the book
  or the reference implementation the logic comes from).
* **`import talib.abstract as ta`** — indicators come from TA-Lib, never from
  `qtpylib` or hand-rolled rolling windows.
* **Exactly one class, named exactly like the file stem.**
* **`INTERFACE_VERSION = 3`**, **`can_short = False`**,
  **`process_only_new_candles = True`**, **`use_exit_signal = True`**.
* **Explicit `startup_candle_count`**, at least the longest indicator period
  used by the strategy plus one candle.
* **Explicit `stoploss` and explicit `minimal_roi`** — never rely on a default.
* **Every entry and exit assignment guarded by `(dataframe["volume"] > 0)`**.

### Mandatory signal-column pattern

A signal column is never assigned from a bare mask: an unguarded
`dataframe.loc[mask, "enter_long"] = 1` leaves `NaN` in every row the mask does
not match, and freqtrade then refuses the dataframe. Always initialise the
column and the tag first, then assign through the volume-guarded mask:

```python
def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
    condition = dataframe["close"] > dataframe["sma_slow"]

    dataframe["enter_long"] = 0
    dataframe["enter_tag"] = ""
    dataframe.loc[condition & (dataframe["volume"] > 0), "enter_long"] = 1
    dataframe.loc[condition & (dataframe["volume"] > 0), "enter_tag"] = "above_sma"
    return dataframe
```

`populate_exit_trend` follows exactly the same pattern with `exit_long` and
`exit_tag`. The result is a column that only ever contains `0` and `1`, never a
`NaN`, which is what the loading test asserts for all ten shipped strategies.

## Running the strategies

```bash
# Load every shipped strategy and check its signal contract
.venv/bin/python -m pytest tests/strategies -q --no-cov

# Lint the strategy files
.venv/bin/ruff check user_data/strategies tests/strategies
.venv/bin/ruff format --check user_data/strategies tests/strategies
```
