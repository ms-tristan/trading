"""``python -m trading_backtest`` entry point.

Strictly equivalent to the ``trading-backtest`` console script declared in
``pyproject.toml``: both call :func:`trading_backtest.cli.main` and exit with the
code it returns (``0`` success, ``1`` domain error, ``2`` usage error).
"""

from __future__ import annotations

from trading_backtest.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
