"""Freqtrade-compatible configuration helpers.

The backtesting engine never reads a Freqtrade file: it is driven by
:class:`~trading_backtest.config.AppConfig` only, so a backtest never needs the
``freqtrade`` extra.  This module exists for the *next* step: turning a
validated strategy into a bot configuration.

Two rules are enforced here:

* **no secret ever leaves this module** — every credential placeholder is an
  empty string, so the committed configurations are safe to share;
* a configuration is either valid or explains itself through a list of
  human-readable issues (see :func:`validate_freqtrade_config`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from trading_backtest.core.constants import SUPPORTED_TIMEFRAMES
from trading_backtest.core.errors import FreqtradeConfigError

__all__ = [
    "DEFAULT_DRY_RUN_WALLET",
    "DEFAULT_STRATEGY_NAME",
    "FREQTRADE_REQUIRED_KEYS",
    "base_freqtrade_config",
    "load_freqtrade_config",
    "validate_freqtrade_config",
    "write_freqtrade_config",
]

#: Default paper-trading wallet of the generated configurations.
DEFAULT_DRY_RUN_WALLET = 1000.0

#: Strategy class name Freqtrade resolves inside ``user_data/strategies``.
DEFAULT_STRATEGY_NAME = "BasicStrategy"

#: Scaling cap on the stake: "never risk more than 99 % of the wallet".
DEFAULT_TRADABLE_BALANCE_RATIO = 0.99

#: Keys a usable Freqtrade configuration must define.  ``strategy`` is left out
#: (it can be passed on the command line with ``--strategy``).
FREQTRADE_REQUIRED_KEYS: tuple[str, ...] = (
    "api_server",
    "dry_run",
    "dry_run_wallet",
    "entry_pricing",
    "exchange",
    "exit_pricing",
    "fiat_display_currency",
    "max_open_trades",
    "pairlists",
    "stake_amount",
    "stake_currency",
    "telegram",
    "timeframe",
    "tradable_balance_ratio",
    "unfilledtimeout",
)


def base_freqtrade_config(
    *,
    exchange: str = "binance",
    stake_currency: str = "USDT",
    stake_amount: float = 100.0,
    timeframe: str = "1h",
    pairs: Sequence[str] = ("BTC/USDT",),
    dry_run: bool = False,
    max_open_trades: int = 1,
    strategy: str = DEFAULT_STRATEGY_NAME,
) -> dict[str, Any]:
    """Return a minimal, secret-free Freqtrade configuration.

    Parameters
    ----------
    exchange:
        Exchange id understood by Freqtrade (``binance``, ``kraken``, ...).
    stake_currency:
        Currency the stake is denominated in (``USDT``, ``EUR``, ...).
    stake_amount:
        Amount engaged per trade, in ``stake_currency``.
    timeframe:
        Candle timeframe; must belong to
        :data:`~trading_backtest.core.constants.SUPPORTED_TIMEFRAMES`.
    pairs:
        Whitelisted pairs, copied into ``exchange.pair_whitelist``.
    dry_run:
        ``True`` for paper trading.  ``config/freqtrade_config.json`` is the
        live variant (``False``) and ``config/freqtrade_dryrun.json`` the
        paper one (``True``).
    max_open_trades:
        Maximum number of simultaneous positions.
    strategy:
        Strategy class name Freqtrade loads from ``user_data/strategies``.

    Returns
    -------
    dict
        A JSON-serialisable configuration containing every
        :data:`FREQTRADE_REQUIRED_KEYS` entry.  ``exchange.key``,
        ``exchange.secret``, ``telegram.token``, ``api_server.jwt_secret_key``
        and every other credential is an empty string: secrets are provided by
        the environment or by ``freqtrade``'s own ``--config`` merge, never by
        a committed file.
    """
    return {
        "max_open_trades": int(max_open_trades),
        "stake_currency": str(stake_currency),
        "stake_amount": float(stake_amount),
        "tradable_balance_ratio": DEFAULT_TRADABLE_BALANCE_RATIO,
        "fiat_display_currency": "USD",
        "dry_run": bool(dry_run),
        "dry_run_wallet": float(DEFAULT_DRY_RUN_WALLET),
        "timeframe": str(timeframe),
        "strategy": str(strategy),
        "exchange": {
            "name": str(exchange),
            "key": "",
            "secret": "",
            "pair_whitelist": [str(pair) for pair in pairs],
            "pair_blacklist": [],
        },
        "entry_pricing": {
            "price_side": "same",
            "use_order_book": True,
            "order_book_top": 1,
            "price_last_balance": 0.0,
            "check_depth_of_market": {"enabled": False, "bids_to_ask_delta": 1},
        },
        "exit_pricing": {
            "price_side": "same",
            "use_order_book": True,
            "order_book_top": 1,
        },
        "pairlists": [{"method": "StaticPairList"}],
        "unfilledtimeout": {
            "entry": 10,
            "exit": 10,
            "exit_timeout_count": 0,
            "unit": "minutes",
        },
        "telegram": {"enabled": False, "token": "", "chat_id": ""},
        "api_server": {
            "enabled": False,
            "listen_ip_address": "127.0.0.1",
            "listen_port": 8080,
            "verbosity": "error",
            "enable_openapi": False,
            "jwt_secret_key": "",
            "CORS_origins": [],
            "username": "",
            "password": "",
        },
    }


def _check_max_open_trades(value: Any) -> str | None:
    """Return an issue when ``value`` is not a positive integer, else ``None``."""
    # ``bool`` is an ``int`` subclass: ``True`` must not pass as "1 trade".
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return f"max_open_trades must be a positive integer, got {value!r}"
    return None


def _check_stake_currency(value: Any) -> str | None:
    """Return an issue when ``value`` is not a non-empty string, else ``None``."""
    if not isinstance(value, str) or not value.strip():
        return f"stake_currency must be a non-empty string, got {value!r}"
    return None


def _check_dry_run(value: Any) -> str | None:
    """Return an issue when ``value`` is not a boolean, else ``None``."""
    if not isinstance(value, bool):
        return f"dry_run must be a boolean, got {value!r}"
    return None


def _check_timeframe(value: Any) -> str | None:
    """Return an issue when ``value`` is not a supported timeframe, else ``None``."""
    if not isinstance(value, str) or value not in SUPPORTED_TIMEFRAMES:
        supported = ", ".join(sorted(SUPPORTED_TIMEFRAMES))
        return f"unsupported timeframe: {value!r} (supported: {supported})"
    return None


def _check_exchange(value: Any) -> list[str]:
    """Return every issue of the ``exchange`` section."""
    if not isinstance(value, Mapping):
        return [f"exchange must be a JSON object, got {type(value).__name__}"]
    issues: list[str] = []
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append(f"exchange.name must be a non-empty string, got {name!r}")
    whitelist = value.get("pair_whitelist")
    if isinstance(whitelist, str) or not isinstance(whitelist, Sequence) or not whitelist:
        issues.append("exchange.pair_whitelist must be a non-empty list of trading pairs")
    elif not all(isinstance(pair, str) and pair.strip() for pair in whitelist):
        issues.append("exchange.pair_whitelist entries must be non-empty strings")
    return issues


def validate_freqtrade_config(payload: Mapping[str, Any]) -> list[str]:
    """Return the human-readable issues of ``payload`` (empty list when valid).

    The checks are exactly the ones a Freqtrade bot cannot start without:

    * every required key of :data:`FREQTRADE_REQUIRED_KEYS` is present;
    * ``max_open_trades`` is a positive integer (``True`` is rejected);
    * ``stake_currency`` is a non-empty string;
    * ``dry_run`` is a boolean;
    * ``exchange.name`` is a non-empty string;
    * ``exchange.pair_whitelist`` is a non-empty list of pair strings;
    * ``timeframe`` belongs to
      :data:`~trading_backtest.core.constants.SUPPORTED_TIMEFRAMES`.

    Parameters
    ----------
    payload:
        Decoded configuration (typically the output of
        :func:`load_freqtrade_config`).

    Returns
    -------
    list[str]
        One entry per violated rule, in a stable order.  An empty list means
        the configuration is usable.
    """
    if not isinstance(payload, Mapping):
        return [f"freqtrade configuration must be a JSON object, got {type(payload).__name__}"]

    issues: list[str] = []
    for key in FREQTRADE_REQUIRED_KEYS:
        if key not in payload:
            issues.append(f"missing required key: {key}")

    checks = (
        ("max_open_trades", _check_max_open_trades),
        ("stake_currency", _check_stake_currency),
        ("dry_run", _check_dry_run),
        ("timeframe", _check_timeframe),
    )
    for key, check in checks:
        if key in payload:
            issue = check(payload[key])
            if issue is not None:
                issues.append(issue)

    if "exchange" in payload:
        issues.extend(_check_exchange(payload["exchange"]))
    return issues


def write_freqtrade_config(payload: Mapping[str, Any], path: str | Path) -> Path:
    """Write ``payload`` as pretty JSON to ``path`` and return the path.

    The document is sorted by key and indented with two spaces, so two runs
    producing the same configuration produce byte-identical files (the
    committed files in ``config/`` are generated by this function).

    Raises
    ------
    FreqtradeConfigError
        If the destination cannot be created or written, or if ``payload`` is
        not JSON-serialisable.
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
        target.write_text(text, encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        raise FreqtradeConfigError(f"cannot write freqtrade configuration {target}: {exc}") from exc
    return target


def load_freqtrade_config(path: str | Path) -> dict[str, Any]:
    """Read the JSON object stored in ``path``.

    Raises
    ------
    FreqtradeConfigError
        If the file is missing, unreadable, not valid JSON, or does not contain
        a JSON object.
    """
    target = Path(path)
    if not target.is_file():
        raise FreqtradeConfigError(f"freqtrade configuration not found: {target}")
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FreqtradeConfigError(f"cannot read freqtrade configuration {target}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FreqtradeConfigError(
            f"invalid JSON in freqtrade configuration {target}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise FreqtradeConfigError(
            f"freqtrade configuration {target} must contain a JSON object, "
            f"got {type(payload).__name__}"
        )
    return payload
