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

The document also pins the order handling of every worker: ``order_types``
makes the stoploss a **market** order and raises ``stoploss_on_exchange`` for a
live profile, so a supervisor stop, error, kill or profile delete never leaves a
position naked on the exchange. :data:`PROTECTIONS` publishes the risk controls
of the fleet (``StoplossGuard``, ``MaxDrawdown``, ``CooldownPeriod``) in the
shape Freqtrade documents for a strategy class. It is deliberately *not* written
into the generated document as a top-level ``protections`` key: Freqtrade 2026.8
-- the version of the deployed base image -- aborts the boot of a worker whose
configuration carries that key::

    freqtrade.configuration.deprecated_settings.process_temporary_deprecated_settings
    -> ConfigurationError: DEPRECATED: Setting 'protections' in the configuration
       is deprecated.

A worker that cannot boot protects nothing, so the constant is exported for the
strategy side instead of being emitted here.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
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
    "MAX_API_PORT",
    "PROTECTIONS",
    "TIMEFRAME_THROTTLE_SECONDS",
    "TRADES_DB_FILENAME",
    "allocate_api_port",
    "build_freqtrade_argv",
    "build_freqtrade_config",
    "port_is_available",
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

#: Highest port number a profile REST API may be allocated: the TCP ceiling.
#: The port scan wraps around at this value instead of running past it.
MAX_API_PORT = 65535

#: Loopback address every worker REST API binds to.
_API_HOST = "127.0.0.1"

#: Seconds between two on-exchange stoploss updates of a live worker.
STOPLOSS_ON_EXCHANGE_INTERVAL_SECONDS = 60

#: Fleet risk controls, in the order they are evaluated, in the shape Freqtrade
#: documents for the ``protections`` attribute of a strategy class.
#:
#: * ``StoplossGuard`` stops trading after four stoploss exits within 60 candles
#:   of the profile timeframe, globally (``only_per_pair`` is ``False``);
#: * ``MaxDrawdown`` stops trading for 120 candles once the last 20 trades drew
#:   down more than 20 percent;
#: * ``CooldownPeriod`` waits five candles after any exit before re-entering.
#:
#: The constant is the shared definition of the fleet risk controls; it is not
#: written into the generated configuration because Freqtrade 2026.8 refuses to
#: boot a worker configured that way (see the module docstring).
PROTECTIONS: tuple[dict[str, Any], ...] = (
    {
        "method": "StoplossGuard",
        "lookback_period_candles": 60,
        "trade_limit": 4,
        "stop_duration_candles": 60,
        "only_per_pair": False,
    },
    {
        "method": "MaxDrawdown",
        "lookback_period_candles": 200,
        "trade_limit": 20,
        "max_allowed_drawdown": 0.2,
        "stop_duration_candles": 120,
    },
    {
        "method": "CooldownPeriod",
        "stop_duration_candles": 5,
    },
)


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


def _order_types(*, paper: bool) -> dict[str, Any]:
    """Return the ``order_types`` block of a generated configuration.

    The mapping always names the four keys Freqtrade requires (``entry``,
    ``exit``, ``stoploss``, ``stoploss_on_exchange``) plus the strategy default
    refresh interval, so the strategy resolver never raises "Order-types mapping
    is incomplete". Entry and exit stay **limit** orders, which keeps
    ``_validate_price_config`` satisfied because the generated ``entry_pricing``
    and ``exit_pricing`` sides are ``"same"``.

    Only the two safety-relevant values differ:

    * ``stoploss`` is a **market** order for paper and live alike: a limit
      stoploss can sit unfilled while the market runs through it, which is the
      opposite of what a stop is for;
    * ``stoploss_on_exchange`` is ``True`` for a live profile, so the exchange
      keeps the stop (no strategy sets ``stoploss_on_exchange`` and Freqtrade
      defaults it to ``False``). Every supervisor stop, error, kill or profile
      delete would otherwise leave the position naked. A paper profile keeps
      ``False``: nothing is really placed, and its behaviour must not change.
    """
    return {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": not paper,
        "stoploss_on_exchange_interval": STOPLOSS_ON_EXCHANGE_INTERVAL_SECONDS,
    }


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
    key Freqtrade 2026.8 requires: ``max_open_trades``, ``stake_currency``,
    ``stake_amount``, ``tradable_balance_ratio``, ``fiat_display_currency``,
    ``dry_run`` (``dry_run_wallet`` for a paper profile), ``trading_mode``,
    ``margin_mode``, ``timeframe``, ``strategy``, ``unfilledtimeout``,
    ``order_types``, ``entry_pricing``, ``exit_pricing``, ``exchange``,
    ``pairlists``, ``api_server``, ``initial_state``, ``internals`` and
    ``bot_name``. The paper/live differences are:

    * ``dry_run`` is ``True`` for a paper profile, ``False`` for a live one;
    * ``dry_run_wallet`` is present for a paper profile only (a live profile
      must never see a simulated wallet);
    * ``stake_amount`` is ``"unlimited"`` for a paper profile and the initial
      capital divided by the open-trade slots for a live one;
    * ``order_types["stoploss_on_exchange"]`` is ``True`` for a live profile
      and ``False`` for a paper one (see :func:`_order_types`).

    ``protections`` is **not** emitted: Freqtrade 2026.8 aborts the boot of a
    worker whose configuration carries that key (see the module docstring).
    :data:`PROTECTIONS` is the exportable definition of the fleet risk controls
    for the strategy side.

    ``exchange_key``/``exchange_secret`` are written as given: the caller passes
    the credentials of a live profile and leaves them empty for paper trading.
    Fresh values are generated for ``jwt_secret_key`` and ``ws_token`` on every
    call, so two profiles never share an API secret.

    ``fiat_display_currency`` is deliberately empty: Freqtrade answers an empty
    conversion currency as "no fiat conversion requested" and never calls the
    CoinGecko price API. A worker set to ``"USD"`` refreshes that conversion on
    every API request, and the anonymous CoinGecko rate limit shared by the
    whole fleet is what made ``GET /api/v1/balance`` take 12 to 20 s.
    """
    paper = _is_paper(profile)
    config: dict[str, Any] = {
        "max_open_trades": profile.max_open_trades,
        "stake_currency": settings.default_stake_currency,
        "stake_amount": _stake_amount(profile, paper=paper),
        "tradable_balance_ratio": 0.99,
        "fiat_display_currency": "",
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
        "order_types": _order_types(paper=paper),
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


def allocate_api_port(
    index: int,
    settings: PlatformSettings,
    *,
    reserved: Collection[int] = (),
    is_available: Callable[[int], bool] | None = None,
) -> int:
    """Return an API port for the profile at position ``index``.

    ``base + index`` (``base`` is :attr:`PlatformSettings.profile_api_port_base`)
    is the *preferred* port -- a profile keeps the deterministic port it is
    documented to use -- but it is only returned when nothing else owns it. The
    scan then walks up to :data:`MAX_API_PORT` and wraps around from ``base`` to
    the port just below the preferred one, returning the first candidate that is
    neither ``reserved`` nor refused by ``is_available``.

    ``reserved`` names the ports of the other profiles (a port already stored in
    the state database or about to be written for a sibling), and
    ``is_available`` is the liveness probe (normally :func:`port_is_available`):
    the fleet map has drifted before, and a worker that cannot bind its REST
    port hangs before its trading loop and is walked to a permanent error. Both
    default to "nothing to avoid", so the plain call still returns ``base +
    index`` exactly.

    When every candidate is taken the preferred port is returned as the
    documented last resort: a duplicated port is a diagnosable worker error,
    while an exception here would abort the whole scheduling pass.

    >>> from trading_platform.config import PlatformSettings
    >>> allocate_api_port(3, PlatformSettings(profile_api_port_base=8101))
    8104
    >>> allocate_api_port(3, PlatformSettings(profile_api_port_base=8101), reserved={8104})
    8105
    """
    preferred = _preferred_api_port(index, settings)
    taken = {int(port) for port in reserved}
    for candidate in _api_port_candidates(preferred, settings):
        if candidate in taken:
            continue
        if is_available is not None and not is_available(candidate):
            continue
        return candidate
    return preferred


def _preferred_api_port(index: int, settings: PlatformSettings) -> int:
    """Return the deterministic (preferred) port of the profile at ``index``."""
    return int(settings.profile_api_port_base) + int(index)


def _api_port_candidates(preferred: int, settings: PlatformSettings) -> list[int]:
    """Return the candidate ports of ``preferred`` in allocation order.

    The order is the preferred port, every port above it up to
    :data:`MAX_API_PORT`, then the wrap-around range from the configured base to
    the port just below the preferred one. Only real TCP ports (``1`` to
    :data:`MAX_API_PORT`) are candidates, so a base close to the ceiling cannot
    produce a port the operating system would refuse.
    """
    base = max(1, int(settings.profile_api_port_base))
    above = range(preferred, MAX_API_PORT + 1)
    wrapped = range(base, min(preferred, MAX_API_PORT + 1))
    return [port for port in (*above, *wrapped) if 1 <= port <= MAX_API_PORT]


def port_is_available(port: int) -> bool:
    """Return whether ``port`` can be bound on the loopback interface.

    The probe binds a TCP socket to ``127.0.0.1:<port>`` without listening and
    closes it again. A port already held -- by a stray worker, by another
    service, or by a supervisor that never reaped its child -- refuses the bind,
    which is exactly the drift :func:`allocate_api_port` has to see.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((_API_HOST, int(port)))
    except OSError:
        return False
    return True


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
