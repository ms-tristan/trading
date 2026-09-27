"""Generated Freqtrade configuration and the Freqtrade process contract.

Every profile runs as its own Freqtrade child process, and this module owns the
two artifacts that make that possible:

* :func:`build_freqtrade_config` renders the JSON document a profile is started
  with. The key set is complete on purpose -- Freqtrade validates its
  configuration at startup and refuses to boot with an incomplete document --
  and the generated file never relies on Freqtrade's own defaults for a value
  the platform cares about;
* :func:`build_freqtrade_argv` renders the ``freqtrade trade`` command line,
  while the ``profile_*`` helpers derive the per-profile runtime layout
  (runtime directory, database URL, log file, data directory).

Two keys are deliberately never emitted: ``telegram`` (the platform has no
Telegram bot) and ``user_data_dir`` (the ``--userdir`` command line flag owns
that location, and a value in the document would fight the flag).

The generated document carries the REST API password of the profile and, for a
live profile, the exchange key and secret: :func:`write_freqtrade_config`
therefore persists it with mode ``0o600``.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..config import PlatformSettings
from ..models import ProfileRecord

__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_FREQTRADE_BINARY",
    "DEFAULT_THROTTLE_SECONDS",
    "FREQTRADE_BIN_ENV_VAR",
    "FREQTRADE_MODE_LIVE",
    "FREQTRADE_MODE_PAPER",
    "GENERATED_CONFIG_MODE",
    "TIMEFRAME_THROTTLE_SECONDS",
    "TRADES_DB_FILENAME",
    "allocate_api_port",
    "build_freqtrade_argv",
    "build_freqtrade_config",
    "profile_config_path",
    "profile_data_dir",
    "profile_db_url",
    "profile_log_path",
    "profile_runtime_dir",
    "resolve_freqtrade_binary",
    "throttle_for_timeframe",
    "write_freqtrade_config",
]

#: Seconds Freqtrade waits between two process iterations, per timeframe.
TIMEFRAME_THROTTLE_SECONDS: dict[str, int] = {"5m": 5, "15m": 15, "1h": 30, "4h": 60, "1d": 60}

#: Throttle used for a timeframe the table above does not know.
DEFAULT_THROTTLE_SECONDS = 30

#: File mode of a generated configuration: it holds credentials.
GENERATED_CONFIG_MODE = 0o600

#: Console script used when the environment does not name another executable.
DEFAULT_FREQTRADE_BINARY = "freqtrade"

#: Environment variable naming the Freqtrade executable. The variable name is
#: part of the deployment contract (it is documented in the README and set by
#: the compose stack); the engine reads it directly so that it never depends on
#: the exact spelling of the constant exported by ``trading_platform.config``.
FREQTRADE_BIN_ENV_VAR = "TB_FREQTRADE_BIN"

#: Value of :attr:`ProfileRecord.mode` that trades simulated funds.
FREQTRADE_MODE_PAPER = "paper"

#: Value of :attr:`ProfileRecord.mode` that trades real funds.
FREQTRADE_MODE_LIVE = "live"

#: Configuration filename inside a profile runtime directory.
CONFIG_FILENAME = "config.json"

#: SQLite database filename inside a profile runtime directory.
TRADES_DB_FILENAME = "tradesv3.sqlite"

#: Live stake amount divider for a profile without open-trade slot.
_MINIMUM_TRADE_SLOTS = 1


def throttle_for_timeframe(timeframe: str) -> int:
    """Return the process throttle in seconds for ``timeframe``.

    The lookup is exact first and case-insensitive second, so ``"1H"`` resolves
    like ``"1h"``. An unknown timeframe falls back to
    :data:`DEFAULT_THROTTLE_SECONDS` rather than failing: a slow poll is always
    better than a profile that cannot start.

    >>> throttle_for_timeframe("5m")
    5
    >>> throttle_for_timeframe("3m")
    30
    """
    key = str(timeframe).strip()
    seconds = TIMEFRAME_THROTTLE_SECONDS.get(key)
    if seconds is None:
        seconds = TIMEFRAME_THROTTLE_SECONDS.get(key.lower())
    return DEFAULT_THROTTLE_SECONDS if seconds is None else seconds


def _is_paper(profile: ProfileRecord) -> bool:
    """Whether ``profile`` trades simulated funds (anything but ``"live"``)."""
    return str(profile.mode).strip().lower() != FREQTRADE_MODE_LIVE


def _stake_amount(profile: ProfileRecord, *, paper: bool) -> str | float:
    """Return the ``stake_amount`` of ``profile``.

    A paper profile lets Freqtrade size every trade itself (``"unlimited"``),
    while a live profile stakes an equal share of the initial capital across its
    open-trade slots.
    """
    if paper:
        return "unlimited"
    slots = max(_MINIMUM_TRADE_SLOTS, int(profile.max_open_trades))
    return round(float(profile.initial_capital) / slots, 8)


def build_freqtrade_config(
    profile: ProfileRecord,
    settings: PlatformSettings,
    *,
    strategy_class_name: str,
    api_port: int,
    api_username: str,
    api_password: str,
    exchange_key: str = "",
    exchange_secret: str = "",
) -> dict[str, Any]:
    """Render the complete Freqtrade configuration of ``profile``.

    The returned document is a plain JSON-serialisable mapping and carries every
    key Freqtrade 2026.8 requires. The paper/live differences are:

    * ``dry_run`` is ``True`` for a paper profile, ``False`` for a live one;
    * ``dry_run_wallet`` is present for a paper profile only (a live profile
      must never see a simulated wallet);
    * ``stake_amount`` is ``"unlimited"`` for a paper profile and the initial
      capital divided by the open-trade slots for a live one.

    ``exchange_key``/``exchange_secret`` are written as given: the caller passes
    the credentials of a live profile and leaves them empty for paper trading.
    Fresh values are generated for ``jwt_secret_key`` and ``ws_token`` on every
    call, so two profiles never share an API secret.
    """
    paper = _is_paper(profile)
    config: dict[str, Any] = {
        "max_open_trades": profile.max_open_trades,
        "stake_currency": settings.default_stake_currency,
        "stake_amount": _stake_amount(profile, paper=paper),
        "tradable_balance_ratio": 0.99,
        "fiat_display_currency": "USD",
        "dry_run": paper,
        "trading_mode": "spot",
        "margin_mode": "",
        "timeframe": profile.timeframe,
        "strategy": strategy_class_name,
        "unfilledtimeout": {
            "entry": 10,
            "exit": 10,
            "exit_timeout_count": 0,
            "unit": "minutes",
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
        "exchange": {
            "name": profile.exchange,
            "key": exchange_key,
            "secret": exchange_secret,
            "ccxt_config": {"enableRateLimit": True, "timeout": 30000},
            "ccxt_async_config": {"enableRateLimit": True, "timeout": 30000},
            "pair_whitelist": list(profile.pairs),
            "pair_blacklist": [],
        },
        "pairlists": [{"method": "StaticPairList"}],
        "api_server": {
            "enabled": True,
            "listen_ip_address": "127.0.0.1",
            "listen_port": int(api_port),
            "verbosity": "error",
            "enable_openapi": False,
            "jwt_secret_key": secrets.token_hex(32),
            "ws_token": secrets.token_urlsafe(32),
            "CORS_origins": [],
            "username": api_username,
            "password": api_password,
        },
        "initial_state": "running",
        "internals": {"process_throttle_secs": throttle_for_timeframe(profile.timeframe)},
        "bot_name": profile.id,
    }
    if paper:
        # A live profile must not carry a simulated wallet.
        config["dry_run_wallet"] = profile.initial_capital
    return config


def write_freqtrade_config(config: Mapping[str, Any], target: Path) -> Path:
    """Write ``config`` to ``target`` as JSON with mode ``0o600``.

    Missing parent directories are created. The mode is applied with
    :func:`os.chmod` *after* the write, so the file never becomes readable by
    another user, whatever the ambient umask allows. Returns ``target``.
    """
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(config), indent=2, sort_keys=False) + "\n"
    path.write_text(payload, encoding="utf-8")
    os.chmod(path, GENERATED_CONFIG_MODE)
    return path


def profile_runtime_dir(state_dir: Path, profile_id: str) -> Path:
    """Return the runtime directory of ``profile_id`` (``profiles/<id>``)."""
    return Path(state_dir) / "profiles" / str(profile_id)


def profile_config_path(state_dir: Path, profile_id: str) -> Path:
    """Return the generated configuration path of ``profile_id``."""
    return profile_runtime_dir(state_dir, profile_id) / CONFIG_FILENAME


def profile_db_url(state_dir: Path, profile_id: str) -> str:
    """Return the SQLAlchemy URL of the trade database of ``profile_id``."""
    database = profile_runtime_dir(state_dir, profile_id) / TRADES_DB_FILENAME
    return f"sqlite:///{database}"


def profile_log_path(state_dir: Path, profile_id: str) -> Path:
    """Return the log file of ``profile_id`` (``logs/<id>.log``)."""
    return Path(state_dir) / "logs" / f"{profile_id}.log"


def profile_data_dir(state_dir: Path, profile_id: str) -> Path:
    """Return the Freqtrade user-data directory of ``profile_id``."""
    return profile_runtime_dir(state_dir, profile_id) / "data"


def allocate_api_port(index: int, settings: PlatformSettings) -> int:
    """Return the REST API port of the profile at position ``index``."""
    return settings.profile_api_port_base + index


def resolve_freqtrade_binary(env: Mapping[str, str] | None = None) -> list[str]:
    """Return the command prefix that starts Freqtrade.

    ``TB_FREQTRADE_BIN`` names the executable (``freqtrade`` by default). When
    that executable is not on ``PATH`` -- the usual case for a virtualenv that
    was never activated -- the interpreter running the platform is used with
    ``-m freqtrade`` instead, which always resolves to the same installation.
    """
    source = os.environ if env is None else env
    raw = source.get(FREQTRADE_BIN_ENV_VAR)
    text = "" if raw is None else str(raw).strip()
    binary = text or DEFAULT_FREQTRADE_BINARY
    if shutil.which(binary) is None:
        return [sys.executable, "-m", "freqtrade"]
    return [binary]


def build_freqtrade_argv(
    profile: ProfileRecord,
    *,
    config_path: Path,
    user_data_dir: Path,
    db_url: str,
    logfile: Path,
    strategy_path: Path | None,
    binary: Sequence[str] | None = None,
) -> list[str]:
    """Return the ``freqtrade trade`` command line of ``profile``.

    ``profile`` is accepted so that a caller renders the whole command from one
    place; the rendered command line itself is derived from the explicit
    arguments, which keeps the call site independent of profile bookkeeping.
    ``strategy_path`` is optional: a profile whose strategy ships inside its own
    user-data directory needs no extra search path.
    """
    executable = list(binary) if binary is not None else resolve_freqtrade_binary()
    argv = [
        *executable,
        "trade",
        "--config",
        str(config_path),
        "--userdir",
        str(user_data_dir),
        "--db-url",
        db_url,
        "--logfile",
        str(logfile),
    ]
    if strategy_path is not None:
        argv += ["--strategy-path", str(strategy_path)]
    return argv
