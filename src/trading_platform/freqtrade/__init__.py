"""Freqtrade interoperability layer.

Public API::

    from trading_platform.freqtrade import (
        base_freqtrade_config, validate_freqtrade_config,
        write_freqtrade_config, load_freqtrade_config,
    )

The layer only depends on :mod:`trading_platform.core` (constants and errors):
it never imports ``freqtrade``, which stays an optional extra.  See
``config/freqtrade_config.json`` (live) and ``config/freqtrade_dryrun.json``
(paper trading).
"""

from __future__ import annotations

from trading_platform.freqtrade.config import (
    DEFAULT_DRY_RUN_WALLET,
    DEFAULT_STRATEGY_NAME,
    FREQTRADE_REQUIRED_KEYS,
    base_freqtrade_config,
    load_freqtrade_config,
    validate_freqtrade_config,
    write_freqtrade_config,
)

__all__ = [
    "DEFAULT_DRY_RUN_WALLET",
    "DEFAULT_STRATEGY_NAME",
    "FREQTRADE_REQUIRED_KEYS",
    "base_freqtrade_config",
    "load_freqtrade_config",
    "validate_freqtrade_config",
    "write_freqtrade_config",
]
