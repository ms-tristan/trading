Freqtrade writes its own state here: downloaded OHLCV, backtest results and strategy files.
The whole `user_data/` tree stays git-ignored (.gitignore), only this placeholder is tracked.
Point Freqtrade at this directory with `--userdir user_data` when running the bot in dry-run.
