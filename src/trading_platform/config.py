"""Platform settings, environment overrides and the live-trading safety gate.

``config/platform.json`` holds the nine documented settings; the environment
overrides exactly two of them (the ones an operator changes per deployment).
The live-trading gate is deliberately painful to open: a live profile needs both
the acknowledgement string *and* the exchange credentials in the environment.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from .paths import resolve_config_dir

__all__ = [
    "ENV_ALLOW_LIVE_TRADING",
    "ENV_FREETRADE_BIN",
    "ENV_LIVE_EXCHANGE_KEY",
    "ENV_LIVE_EXCHANGE_SECRET",
    "ENV_LOG_LEVEL",
    "ENV_OPERATOR_TOKEN",
    "ENV_PROFILE_API_PORT_BASE",
    "ENV_SNAPSHOT_INTERVAL_SECONDS",
    "LIVE_TRADING_CONFIRMATION",
    "PLATFORM_CONFIG_FILENAME",
    "PlatformSettings",
    "allow_live_trading",
    "live_trading_gate",
    "operator_token",
]

ENV_SNAPSHOT_INTERVAL_SECONDS = "TB_SNAPSHOT_INTERVAL_SECONDS"
ENV_PROFILE_API_PORT_BASE = "TB_PROFILE_API_PORT_BASE"
ENV_OPERATOR_TOKEN = "TB_OPERATOR_TOKEN"
ENV_ALLOW_LIVE_TRADING = "TB_ALLOW_LIVE_TRADING"
ENV_LIVE_EXCHANGE_KEY = "TB_LIVE_EXCHANGE_KEY"
ENV_LIVE_EXCHANGE_SECRET = "TB_LIVE_EXCHANGE_SECRET"
ENV_LOG_LEVEL = "TB_LOG_LEVEL"
ENV_FREETRADE_BIN = "TB_FREETRADE_BIN"

#: Exact value ``TB_ALLOW_LIVE_TRADING`` must carry for live trading to be armed.
LIVE_TRADING_CONFIRMATION = "I_UNDERSTAND_THE_RISK"

PLATFORM_CONFIG_FILENAME = "platform.json"


def _environment(env: Mapping[str, str] | None) -> Mapping[str, str]:
    """Return the environment mapping to read from (``os.environ`` by default)."""
    return os.environ if env is None else env


def _env_int(
    env: Mapping[str, str],
    name: str,
    minimum: int,
    maximum: int | None = None,
) -> int | None:
    """Read a bounded integer from ``env``; ``None`` when absent or unusable."""
    raw = env.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    if value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _environment_overrides(env: Mapping[str, str] | None) -> dict[str, int]:
    """Return the two settings the environment is allowed to override."""
    source = _environment(env)
    candidates: tuple[tuple[str, int | None], ...] = (
        ("snapshot_interval_seconds", _env_int(source, ENV_SNAPSHOT_INTERVAL_SECONDS, minimum=1)),
        ("profile_api_port_base", _env_int(source, ENV_PROFILE_API_PORT_BASE, 1, maximum=65535)),
    )
    return {field: value for field, value in candidates if value is not None}


class PlatformSettings(BaseModel):
    """The platform-wide settings, loaded from ``config/platform.json``."""

    model_config = ConfigDict(extra="ignore")

    snapshot_interval_seconds: int = 60
    profile_api_port_base: int = 8101
    default_exchange: str = "binance"
    default_timeframe: str = "1h"
    default_stake_currency: str = "USDT"
    default_initial_capital: float = 1000.0
    default_max_open_trades: int = 2
    dashboard_refresh_seconds: int = 15
    equity_retention_days: int = 90

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> PlatformSettings:
        """Load the settings document, then apply the environment overrides.

        ``path`` defaults to ``platform.json`` inside the resolved config
        directory (``TB_CONFIG_DIR``, ``/app/config`` or ``<repo>/config``).
        A missing, unreadable or malformed document is not an error: the
        documented defaults are used instead. Only
        ``TB_SNAPSHOT_INTERVAL_SECONDS`` and ``TB_PROFILE_API_PORT_BASE`` are read
        from the environment, and an unusable value is ignored rather than fatal.
        """
        document_path = (
            resolve_config_dir(env) / PLATFORM_CONFIG_FILENAME if path is None else Path(path)
        )
        settings = cls()
        document = cls._read_document(document_path)
        if document is not None:
            try:
                settings = cls.model_validate(document)
            except ValidationError:
                settings = cls()
        return settings.with_overrides(**_environment_overrides(env))

    @staticmethod
    def _read_document(path: Path) -> dict[str, Any] | None:
        """Return the JSON object stored at ``path``, or ``None``."""
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            document = json.loads(raw)
        except ValueError:
            return None
        return document if isinstance(document, dict) else None

    def with_overrides(self, **changes: Any) -> PlatformSettings:
        """Return a validated copy of these settings with ``changes`` applied.

        Unknown keys are ignored, so a caller may forward a wider payload
        (for example the dashboard settings body) without filtering it first.
        """
        return type(self).model_validate({**self.model_dump(), **changes})


def live_trading_gate(mode: str, env: Mapping[str, str] | None = None) -> tuple[bool, str | None]:
    """Decide whether a profile may run in ``mode``.

    Returns ``(True, None)`` when the profile may start, otherwise
    ``(False, reason)`` with the operator-facing refusal reason.
    """
    # Fail safe: anything that normalises to "live" is treated as live, so a
    # caller can never open real-money trading by a casing accident.
    if str(mode).strip().lower() != "live":
        return (True, None)
    source = _environment(env)
    if source.get(ENV_ALLOW_LIVE_TRADING) != LIVE_TRADING_CONFIRMATION:
        expected = f"{ENV_ALLOW_LIVE_TRADING} must equal {LIVE_TRADING_CONFIRMATION}"
        return (False, f"live trading refused: {expected}")
    missing = [
        name
        for name in (ENV_LIVE_EXCHANGE_KEY, ENV_LIVE_EXCHANGE_SECRET)
        if not str(source.get(name) or "").strip()
    ]
    if missing:
        return (
            False,
            f"live trading refused: missing environment variable(s): {', '.join(missing)}",
        )
    return (True, None)


def operator_token(env: Mapping[str, str] | None = None) -> str:
    """Return the operator token used by mutating API routes ("" when unset)."""
    raw = _environment(env).get(ENV_OPERATOR_TOKEN)
    return "" if raw is None else str(raw)


def allow_live_trading(env: Mapping[str, str] | None = None) -> bool:
    """Whether the operator acknowledged live trading in the environment.

    This is the acknowledgement flag only; a profile still has to pass
    :func:`live_trading_gate` (which also requires the exchange credentials).
    """
    return _environment(env).get(ENV_ALLOW_LIVE_TRADING) == LIVE_TRADING_CONFIRMATION
