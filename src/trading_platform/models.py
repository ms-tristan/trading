"""Domain models and shared value helpers of the trading platform.

This module is the **single source of truth** for every JSON shape the platform
persists or serves: the API layer only re-exports these models, and the state
store serialises exactly these fields.

Two rules are enforced here rather than at the edges:

* every timestamp is an ISO-8601 UTC string with second precision and a
  trailing ``Z`` (:func:`format_ts`) -- the canonical format of DB rows,
  snapshots and every API field;
* every float is finite. Freqtrade returns ``NaN`` and ``Infinity`` (for
  example ``profit_factor`` when a profile never lost a trade), and FastAPI
  would serialise those as bare ``NaN``/``Infinity`` tokens that a browser
  cannot parse. :func:`finite_float` is therefore mandatory on every value that
  comes from Freqtrade, and the models sanitise non-finite floats once more as
  a safety net.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "PROFILE_MODES",
    "PROFILE_SOURCES",
    "PROFILE_STATES",
    "STATE_BLOCKED",
    "STATE_ERROR",
    "STATE_RUNNING",
    "STATE_STOPPED",
    "STRATEGY_CATEGORIES",
    "SUPPORTED_TIMEFRAMES",
    "TS_FORMAT",
    "WINDOWS",
    "AccountPerformance",
    "AccountResponse",
    "CatalogueApplyResult",
    "DailyRow",
    "DashboardSettings",
    "EquityPoint",
    "Event",
    "EventsResponse",
    "HealthStatus",
    "KillSwitchResponse",
    "ProfileConfig",
    "ProfileDetail",
    "ProfileMetrics",
    "ProfileRecord",
    "ProfileResponse",
    "ProfileSnapshot",
    "ProfileSummary",
    "ProfileView",
    "ProfilesResponse",
    "StrategiesResponse",
    "StrategyMeta",
    "StrategyView",
    "finite_float",
    "format_ts",
    "normalise_drawdown_pct",
    "normalise_win_rate",
    "parse_ts",
    "utc_now",
    "window_start",
]

#: Timestamp layout shared by the state store, the snapshots and the API.
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

SUPPORTED_TIMEFRAMES: tuple[str, ...] = (
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
)

PROFILE_MODES: tuple[str, ...] = ("paper", "live")
PROFILE_STATES: tuple[str, ...] = ("running", "stopped", "error", "blocked")
PROFILE_SOURCES: tuple[str, ...] = ("catalogue", "operator")
STRATEGY_CATEGORIES: tuple[str, ...] = (
    "baseline",
    "trend",
    "mean-reversion",
    "breakout",
    "allocation",
)
WINDOWS: tuple[str, ...] = ("24h", "7d", "30d", "all")

STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"
STATE_BLOCKED = "blocked"

MODE_PAPER = "paper"
MODE_LIVE = "live"

_WINDOW_DAYS: dict[str, int] = {"24h": 1, "7d": 7, "30d": 30}


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def as_utc(ts: datetime) -> datetime:
    """Return ``ts`` as a timezone-aware UTC datetime (naive input means UTC)."""
    moment = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def format_ts(ts: datetime | None = None) -> str:
    """Format ``ts`` (default: now) in the canonical platform timestamp format.

    >>> format_ts(datetime(2026, 9, 27, 17, 31, 30, tzinfo=UTC))
    '2026-09-27T17:31:30Z'
    """
    moment = utc_now() if ts is None else ts
    return as_utc(moment).replace(microsecond=0).strftime(TS_FORMAT)


def parse_ts(value: str) -> datetime:
    """Parse a canonical (or Freqtrade-style) ISO-8601 timestamp into UTC.

    Raises :class:`ValueError` when ``value`` is not a usable timestamp.
    """
    text = str(value).strip()
    if not text:
        raise ValueError("timestamp must not be empty")
    candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    return as_utc(parsed)


def finite_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to a finite float, falling back to ``default``.

    ``None``, strings, ``NaN``, ``+Infinity`` and ``-Infinity`` all resolve to
    ``default``: a JSON document can never carry a non-finite number.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def normalise_win_rate(value: Any) -> float:
    """Return a win rate as a ratio inside ``[0, 1]``.

    Freqtrade reports a ratio, some endpoints report a percentage; anything
    above ``1`` is therefore divided by ``100``. Non-finite input becomes
    ``0.0``.
    """
    ratio = finite_float(value, 0.0)
    if ratio > 1.0:
        ratio /= 100.0
    return min(max(ratio, 0.0), 1.0)


def normalise_drawdown_pct(value: Any) -> float:
    """Return a drawdown as a non-negative percentage.

    Freqtrade reports drawdowns either as a negative ratio (``-0.12``) or as a
    percentage (``-12.0``); both become ``12.0``.
    """
    magnitude = abs(finite_float(value, 0.0))
    if magnitude <= 1.0:
        magnitude *= 100.0
    return magnitude


def window_start(window: str, now: datetime | None = None) -> datetime | None:
    """Return the inclusive start of ``window``, or ``None`` for ``"all"``."""
    key = str(window).strip().lower()
    if key == "all":
        return None
    days = _WINDOW_DAYS.get(key)
    if days is None:
        raise ValueError(f"unsupported window {window!r}; expected one of {', '.join(WINDOWS)}")
    return as_utc(utc_now() if now is None else now) - timedelta(days=days)


# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------
class _Model(BaseModel):
    """Base class of every platform shape.

    Unknown keys are ignored so that a document written by a newer revision
    never breaks an older reader, and non-finite floats are collapsed to
    ``0.0`` so that no model can ever serialise ``NaN`` or ``Infinity``.
    """

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def _sanitise_non_finite_floats(self) -> Self:
        for name in type(self).model_fields:
            value = getattr(self, name, None)
            if isinstance(value, float) and not math.isfinite(value):
                setattr(self, name, 0.0)
        return self


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
class ProfileConfig(_Model):
    """A profile as declared by the catalogue or by the operator."""

    id: str
    name: str = ""
    strategy: str
    timeframe: str = "1h"
    mode: Literal["paper", "live"] = "paper"
    exchange: str = "binance"
    pairs: list[str] = Field(default_factory=list)
    initial_capital: float = 1000.0
    max_open_trades: int = 2
    priority: int = 100
    enabled: bool = True

    @property
    def is_live(self) -> bool:
        """Whether this profile would trade real funds."""
        return self.mode == MODE_LIVE


class ProfileRecord(ProfileConfig):
    """A profile plus its runtime bookkeeping, as stored in the state DB.

    ``api_username``/``api_password`` are internal: they are never exposed by
    :class:`ProfileView` or by any API response.
    """

    source: str = "catalogue"
    state: str = STATE_STOPPED
    state_reason: str | None = None
    api_port: int = 0
    api_username: str | None = None
    api_password: str | None = None
    pid: int | None = None
    started_at: str | None = None
    last_error: str | None = None
    created_at: str = Field(default_factory=format_ts)
    updated_at: str = Field(default_factory=format_ts)


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------
class ProfileSnapshot(_Model):
    """One polled measurement of a profile; one row per profile and minute."""

    profile_id: str
    ts: str = Field(default_factory=format_ts)
    portfolio_value: float = 0.0
    cash: float = 0.0
    positions_value: float = 0.0
    profit_abs: float = 0.0
    profit_pct: float = 0.0
    realized_profit_abs: float = 0.0
    unrealized_profit_abs: float = 0.0
    open_trades: int = 0
    closed_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    healthy: bool = True


class ProfileMetrics(_Model):
    """The current numbers of a profile, independent of any view or envelope."""

    portfolio_value: float = 0.0
    cash: float = 0.0
    positions_value: float = 0.0
    profit_abs: float = 0.0
    realized_profit_abs: float = 0.0
    unrealized_profit_abs: float = 0.0
    open_trades: int = 0
    closed_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    best_pair: str | None = None
    starting_capital: float = 0.0


class EquityPoint(_Model):
    """One point of an equity curve."""

    t: str
    value: float = 0.0
    profit_pct: float | None = None


class DailyRow(_Model):
    """One day of a profile's realised performance."""

    date: str
    abs_profit: float = 0.0
    rel_profit: float = 0.0
    #: The balance the day started from, as the worker reported it; ``0.0`` when
    #: the API omitted it.
    starting_balance: float = 0.0
    trade_count: int = 0


class ProfileSummary(_Model):
    """A compact profile reference used inside aggregate payloads."""

    id: str
    name: str = ""
    profit_pct: float = 0.0
    portfolio_value: float = 0.0


class Event(_Model):
    """A line of the platform event log."""

    id: int | None = None
    ts: str = Field(default_factory=format_ts)
    profile_id: str | None = None
    level: str = "info"
    kind: str = "generic"
    message: str = ""


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
class StrategyMeta(_Model):
    """Catalogue metadata of one Freqtrade strategy file."""

    id: str
    class_name: str = ""
    file: str = ""
    title: str = ""
    category: str = "baseline"
    summary: str = ""
    description: str = ""
    indicators: list[str] = Field(default_factory=list)
    timeframes: list[str] = Field(default_factory=list)
    reference: str = ""
    risk_notes: str = ""


# ---------------------------------------------------------------------------
# Views (what the API serves)
# ---------------------------------------------------------------------------
class ProfileView(_Model):
    """A profile as the dashboard sees it: no credentials, no internal state."""

    id: str
    name: str = ""
    strategy: str = ""
    strategy_title: str = ""
    strategy_category: str = ""
    timeframe: str = ""
    mode: str = MODE_PAPER
    exchange: str = "binance"
    pairs: list[str] = Field(default_factory=list)
    initial_capital: float = 0.0
    max_open_trades: int = 0
    priority: int = 0
    state: str = STATE_STOPPED
    state_reason: str | None = None
    portfolio_value: float = 0.0
    cash: float = 0.0
    positions_value: float = 0.0
    profit_abs: float = 0.0
    profit_pct: float = 0.0
    realized_profit_abs: float = 0.0
    unrealized_profit_abs: float = 0.0
    open_trades: int = 0
    closed_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    best_pair: str | None = None
    uptime_seconds: float = 0.0
    last_updated: str = Field(default_factory=format_ts)
    rank: int = 0
    #: The last 60 equity points of the profile, oldest first, as published by
    #: ``GET /api/profiles`` (``PROFILE_SPARKLINE_POINTS``); ``[]`` when the
    #: profile has no snapshot yet. Each point is an :class:`EquityPoint`, so it
    #: carries ``t`` (ISO-8601 UTC), ``value`` and the optional ``profit_pct``.
    sparkline: list[EquityPoint] = Field(default_factory=list)
    #: The 1-based position of the profile among the **running** profiles,
    #: ordered by priority DESC then id ASC -- the order the scheduler uses.
    #: ``None`` when the profile is not running.
    slot: int | None = None
    #: The private Freqtrade REST port of the worker while it is running, and
    #: ``None`` otherwise: the port outlives a stop in the runtime columns, so
    #: the liveness test is what makes this field honest.
    worker_port: int | None = None


class StrategyView(_Model):
    """A strategy plus the aggregated performance of its profiles."""

    id: str
    class_name: str = ""
    title: str = ""
    category: str = "baseline"
    summary: str = ""
    description: str = ""
    indicators: list[str] = Field(default_factory=list)
    timeframes: list[str] = Field(default_factory=list)
    reference: str = ""
    risk_notes: str = ""
    profile_count: int = 0
    profiles_running: int = 0
    portfolio_value: float = 0.0
    profit_abs: float = 0.0
    profit_pct: float = 0.0
    best_profile_id: str | None = None
    win_rate: float = 0.0


class AccountPerformance(_Model):
    """The aggregated performance of a scope (``paper``, ``live`` or ``combined``)."""

    scope: str
    portfolio_value: float = 0.0
    initial_capital: float = 0.0
    profit_abs: float = 0.0
    profit_pct: float = 0.0
    realized_profit_abs: float = 0.0
    unrealized_profit_abs: float = 0.0
    open_positions: int = 0
    open_trades: int = 0
    closed_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    profiles_total: int = 0
    profiles_running: int = 0
    profiles_healthy: int = 0
    best_profile: ProfileSummary | None = None
    worst_profile: ProfileSummary | None = None
    equity_curve: list[EquityPoint] = Field(default_factory=list)
    generated_at: str = Field(default_factory=format_ts)


class HealthStatus(_Model):
    """The liveness payload of ``GET /api/health``."""

    status: Literal["ok", "degraded"]
    version: str = ""
    uptime_seconds: float = 0.0
    profiles_total: int = 0
    profiles_running: int = 0
    profiles_healthy: int = 0
    profiles_paper: int = 0
    profiles_running_paper: int = 0
    profiles_live: int = 0
    profiles_running_live: int = 0
    #: Retained for wire compatibility with the dashboard, which still parses it.
    #: The state ``queued`` no longer exists, so this counter is structurally
    #: always ``0`` and is never derived from any profile row.
    profiles_queued: int = 0
    kill_switch_engaged: bool = False
    generated_at: str = Field(default_factory=format_ts)


class DashboardSettings(_Model):
    """The operator-facing settings payload."""

    snapshot_interval_seconds: int = 60
    kill_switch_engaged: bool = False
    allow_live_trading: bool = False
    catalogue_profile_count: int = 0
    operator_profile_count: int = 0
    state_db_path: str = ""
    version: str = ""


class CatalogueApplyResult(_Model):
    """The outcome of applying the catalogue to the state store."""

    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    pruned: list[str] = Field(default_factory=list)
    refused_live: list[str] = Field(default_factory=list)


class ProfileDetail(_Model):
    """``GET /api/profiles/{id}``: one profile with its history and strategy."""

    profile: ProfileView
    strategy: StrategyView
    equity_curve: list[EquityPoint] = Field(default_factory=list)
    daily: list[DailyRow] = Field(default_factory=list)
    open_trades: list[dict[str, Any]] = Field(default_factory=list)
    recent_trades: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------
class ProfilesResponse(_Model):
    """``GET /api/profiles``."""

    profiles: list[ProfileView] = Field(default_factory=list)
    total: int = 0
    generated_at: str = Field(default_factory=format_ts)


class AccountResponse(_Model):
    """``GET /api/account``."""

    paper: AccountPerformance
    live: AccountPerformance
    combined: AccountPerformance
    generated_at: str = Field(default_factory=format_ts)


class StrategiesResponse(_Model):
    """``GET /api/strategies``."""

    strategies: list[StrategyView] = Field(default_factory=list)


class EventsResponse(_Model):
    """``GET /api/events``."""

    events: list[Event] = Field(default_factory=list)


class ProfileResponse(_Model):
    """``GET /api/profiles/{id}`` when only the profile row is needed."""

    profile: ProfileView


class KillSwitchResponse(_Model):
    """``POST /api/kill-switch``."""

    kill_switch_engaged: bool = False
