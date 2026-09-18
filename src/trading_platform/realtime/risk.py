"""Risk, kill switch and live-trading gate of the realtime layer.

Three safety mechanisms live here, in the order they are consulted before any
order can reach a venue:

1. :class:`KillSwitch` -- a *global* halt, forced by a file, by the environment
   (``TB_KILL_SWITCH``) or by an explicit API call, and persisted in a
   :class:`MetaStore` so that it survives a restart.
2. :class:`RiskManager` -- the per-profile limits enforced *before* the broker is
   called.  It **returns** a :class:`RiskDecision` and never raises: mapping a
   rejection to :class:`~trading_platform.core.errors.RiskLimitExceededError` (or
   :class:`~trading_platform.core.errors.KillSwitchActiveError`) is the single
   responsibility of the execution gateway, which keeps exactly one
   exception-mapping policy in the codebase.
3. :class:`LiveTradingGate` -- the explicit opt-in that arms a ``live`` profile.

Both the clock and the meta store are injected, so every behaviour here is
testable offline and deterministically.  :class:`MetaStore` is a structural
protocol: it is satisfied by ``SqliteStateStore`` (layer 6) as well as by a
trivial in-memory fake in a test, which is why this module never imports the
persistence module.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from trading_platform.config.models import ProfileConfig, RiskLimitsConfig
from trading_platform.core.errors import KillSwitchActiveError, LiveTradingForbiddenError
from trading_platform.realtime.clock import Clock
from trading_platform.realtime.models import OrderRequest

__all__ = [
    "ENV_KILL_SWITCH",
    "KILL_SWITCH_CHANGED_META_KEY",
    "KILL_SWITCH_META_KEY",
    "KILL_SWITCH_REASON_META_KEY",
    "KillSwitch",
    "KillSwitchState",
    "LiveTradingGate",
    "MetaStore",
    "RiskDecision",
    "RiskLimits",
    "RiskManager",
]

#: Logger name is a literal on purpose: ``realtime.observability`` upgrades this
#: stdlib logger to one-JSON-object-per-line at runtime by installing its
#: formatter on the ``trading_platform`` logger tree, so this module stays
#: structured without importing that module (which belongs to another package).
_LOGGER = logging.getLogger("trading_platform.realtime.risk")

#: Meta keys the kill switch persists through :class:`MetaStore`.
KILL_SWITCH_META_KEY = "kill_switch_engaged"
KILL_SWITCH_REASON_META_KEY = "kill_switch_reason"
KILL_SWITCH_CHANGED_META_KEY = "kill_switch_changed_at"

#: Environment variable that forces the kill switch from outside the process.
ENV_KILL_SWITCH = "TB_KILL_SWITCH"

#: Values of ``TB_KILL_SWITCH`` that force the switch (compared case-insensitively).
TRUTHY = {"1", "true", "yes", "on"}

#: The reason reported when the flag file exists but is empty.
_FILE_REASON = "kill switch file present"

#: Drawdown precision of the rejection reason (four decimals keeps the compared
#: values readable without turning them into an unparseable percentage).
_DRAWDOWN_PRECISION = 4


# ---------------------------------------------------------------------------
# meta store seam
# ---------------------------------------------------------------------------


@runtime_checkable
class MetaStore(Protocol):
    """Minimal key/value persistence of the kill switch.

    ``SqliteStateStore`` satisfies it structurally through its
    ``get_meta``/``set_meta`` methods, so the kill switch never hard-depends on
    the persistence module and a test can inject a two-line fake.
    """

    def get_meta(self, key: str) -> str | None:
        """Return the stored value of ``key``, or ``None`` when it is unset."""
        ...

    def set_meta(self, key: str, value: str) -> None:
        """Store ``value`` under ``key`` (upsert)."""
        ...


# ---------------------------------------------------------------------------
# per-profile limits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskLimits:
    """The per-profile limits, all optional (``None`` means "not enforced").

    ``0`` is a meaningful value for the counting limits: ``max_open_positions=0``
    forbids opening any position and ``max_daily_trades=0`` forbids trading.
    """

    max_position_notional: float | None = None
    max_order_notional: float | None = None
    max_open_positions: int = 1
    max_daily_loss: float | None = None
    max_drawdown_pct: float | None = None
    max_daily_trades: int | None = None

    @classmethod
    def from_config(cls, cfg: RiskLimitsConfig) -> RiskLimits:
        """Build the limits from their validated configuration model."""
        return cls(
            max_position_notional=cfg.max_position_notional,
            max_order_notional=cfg.max_order_notional,
            max_open_positions=cfg.max_open_positions,
            max_daily_loss=cfg.max_daily_loss,
            max_drawdown_pct=cfg.max_drawdown_pct,
            max_daily_trades=cfg.max_daily_trades,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping (``None`` stays ``None``)."""
        return {
            "max_position_notional": self.max_position_notional,
            "max_order_notional": self.max_order_notional,
            "max_open_positions": int(self.max_open_positions),
            "max_daily_loss": self.max_daily_loss,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_daily_trades": self.max_daily_trades,
        }


@dataclass(frozen=True)
class RiskDecision:
    """The verdict of :meth:`RiskManager.check_order`.

    ``limit`` names the limit that rejected the order (empty when allowed) and is
    the single source of truth the gateway uses to pick the exception it raises.
    """

    allowed: bool
    reason: str = ""
    limit: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {"allowed": bool(self.allowed), "reason": str(self.reason), "limit": str(self.limit)}

    @classmethod
    def allow(cls) -> RiskDecision:
        """Return the nominal verdict (no limit name, no reason)."""
        return cls(allowed=True)

    @classmethod
    def reject(cls, limit: str, reason: str) -> RiskDecision:
        """Return a rejection naming the limit that blocked the order."""
        return cls(allowed=False, reason=str(reason), limit=str(limit))


class RiskManager:
    """Enforce the per-profile limits before any order reaches the broker.

    The evaluation order is frozen and the **first** failure wins, so a rejection
    reason is reproducible: ``kill_switch``, ``max_order_notional``,
    ``max_position_notional``, ``max_open_positions``, ``max_daily_loss``,
    ``max_drawdown_pct``, ``max_daily_trades``.

    Exits are never blocked by a *position* cap: when ``closes_position`` is
    ``True`` the position-notional and open-position limits are skipped, while the
    order-notional, daily-loss, drawdown and daily-trade limits still apply (a
    runaway exit is still an order, and a blown risk budget must stop trading).
    """

    def __init__(
        self, limits: RiskLimits, *, clock: Clock, kill_switch: KillSwitch | None = None
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._kill_switch = kill_switch

    @property
    def limits(self) -> RiskLimits:
        """Return the immutable limits this manager enforces."""
        return self._limits

    def check_order(
        self,
        request: OrderRequest,
        *,
        reference_price: float,
        equity: float,
        open_positions: int,
        position_notional: float,
        daily_pnl: float,
        daily_trades: int,
        peak_equity: float,
        closes_position: bool = False,
    ) -> RiskDecision:
        """Evaluate every limit against one order request, first failure wins.

        Parameters
        ----------
        request:
            The order about to be routed; the notional is
            ``request.quantity * reference_price``.
        reference_price:
            Price the notional is computed at (the candle open the order would
            fill at).
        equity:
            Current account equity of the profile.
        open_positions:
            Number of positions currently open for the profile.
        position_notional:
            Current notional held on ``request.symbol``.
        daily_pnl:
            Realised + unrealised profit and loss of the current UTC day.
        daily_trades:
            Number of orders already submitted during the current UTC day.
        peak_equity:
            Highest equity reached so far (the drawdown denominator).
        closes_position:
            ``True`` when the order reduces or closes an existing position.

        Returns
        -------
        RiskDecision
            :meth:`RiskDecision.allow` when every limit passes, otherwise a
            rejection naming the blocking limit.
        """
        decision = self._evaluate(
            request,
            reference_price=reference_price,
            equity=equity,
            open_positions=open_positions,
            position_notional=position_notional,
            daily_pnl=daily_pnl,
            daily_trades=daily_trades,
            peak_equity=peak_equity,
            closes_position=closes_position,
        )
        if not decision.allowed:
            _LOGGER.warning("risk limit %s: %s", decision.limit, decision.reason)
        return decision

    def to_dict(self) -> dict[str, Any]:
        """Return the limits plus the current kill-switch verdict."""
        payload = self._limits.to_dict()
        payload["kill_switch"] = self._kill_switch is not None and self._kill_switch.engaged()
        return payload

    # -- internals ---------------------------------------------------------

    def _evaluate(
        self,
        request: OrderRequest,
        *,
        reference_price: float,
        equity: float,
        open_positions: int,
        position_notional: float,
        daily_pnl: float,
        daily_trades: int,
        peak_equity: float,
        closes_position: bool,
    ) -> RiskDecision:
        """Return the first rejection of the frozen evaluation order, or allow."""
        limits = self._limits
        notional = float(request.quantity) * float(reference_price)

        kill_switch = self._kill_switch
        if kill_switch is not None and kill_switch.engaged():
            state = kill_switch.state()
            return RiskDecision.reject("kill_switch", f"global kill switch engaged: {state.reason}")

        if limits.max_order_notional is not None and notional > limits.max_order_notional:
            return RiskDecision.reject(
                "max_order_notional",
                f"order notional {notional:.2f} exceeds "
                f"max_order_notional {limits.max_order_notional:.2f}",
            )

        if not closes_position:
            projected = float(position_notional) + notional
            if (
                limits.max_position_notional is not None
                and projected > limits.max_position_notional
            ):
                return RiskDecision.reject(
                    "max_position_notional",
                    f"position notional {projected:.2f} exceeds "
                    f"max_position_notional {limits.max_position_notional:.2f}",
                )

            projected_positions = int(open_positions) + 1
            if projected_positions > limits.max_open_positions:
                return RiskDecision.reject(
                    "max_open_positions",
                    f"open positions {projected_positions} exceeds "
                    f"max_open_positions {limits.max_open_positions}",
                )

        if limits.max_daily_loss is not None and -float(daily_pnl) > limits.max_daily_loss:
            return RiskDecision.reject(
                "max_daily_loss",
                f"daily loss {abs(float(daily_pnl)):.2f} exceeds "
                f"max_daily_loss {limits.max_daily_loss:.2f}",
            )

        if limits.max_drawdown_pct is not None and float(peak_equity) > 0:
            drawdown = (float(peak_equity) - float(equity)) / float(peak_equity)
            if drawdown > limits.max_drawdown_pct:
                return RiskDecision.reject(
                    "max_drawdown_pct",
                    f"drawdown {drawdown:.{_DRAWDOWN_PRECISION}f} exceeds "
                    f"max_drawdown_pct {limits.max_drawdown_pct:.{_DRAWDOWN_PRECISION}f}",
                )

        if limits.max_daily_trades is not None and int(daily_trades) >= limits.max_daily_trades:
            return RiskDecision.reject(
                "max_daily_trades",
                f"daily trades {int(daily_trades)} reaches "
                f"max_daily_trades {limits.max_daily_trades}",
            )

        return RiskDecision.allow()


# ---------------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KillSwitchState:
    """The effective state of the global kill switch.

    ``source`` names the winning force: ``'file'`` (a flag file exists),
    ``'env'`` (``TB_KILL_SWITCH`` is truthy), ``'store'`` (the persisted meta
    row), ``'api'``/whatever label an in-process :meth:`KillSwitch.engage` was
    called with, or ``'none'`` when nothing forces the switch.
    """

    engaged: bool
    reason: str = ""
    changed_at: pd.Timestamp | None = None
    source: str = "none"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "engaged": bool(self.engaged),
            "reason": str(self.reason),
            "changed_at": None
            if self.changed_at is None
            else pd.Timestamp(self.changed_at).isoformat(),
            "source": str(self.source),
        }


class KillSwitch:
    """The global halt, forced by a file, the environment, the store or the API.

    Precedence is frozen and the winning source is reported by :meth:`state`:

    ``file`` > ``env`` > ``store`` > in-process ``engage`` call

    The file and the environment are *forcing*: :meth:`release` refuses to clear
    them (they live outside the process) and raises
    :class:`~trading_platform.core.errors.KillSwitchActiveError` instead.  Only
    the store rows -- and the in-process override -- are cleared, and the store is
    what makes an engagement survive a restart.
    """

    def __init__(
        self,
        store: MetaStore | None = None,
        *,
        clock: Clock,
        flag_path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._flag_path = None if flag_path is None else Path(flag_path)
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._local: KillSwitchState | None = None

    def engage(self, reason: str, *, source: str = "api") -> KillSwitchState:
        """Engage the switch and persist it (when a store is injected).

        The returned state is the *effective* one: when a flag file or the
        environment already forces the switch, that force wins and is reported
        with its own reason.
        """
        self._local = KillSwitchState(
            engaged=True,
            reason=str(reason),
            changed_at=pd.Timestamp(self._clock.now()),
            source=str(source),
        )
        if self._store is not None:
            self._store.set_meta(KILL_SWITCH_META_KEY, "1")
            self._store.set_meta(KILL_SWITCH_REASON_META_KEY, str(reason))
            self._store.set_meta(KILL_SWITCH_CHANGED_META_KEY, self._clock.now().isoformat())
        return self.state()

    def release(self, *, source: str = "api") -> KillSwitchState:
        """Clear the persisted engagement.

        Raises
        ------
        KillSwitchActiveError
            When the flag file or the environment still forces the switch; the
            message names the winning source.
        """
        forced = self._forced()
        if forced is not None:
            raise KillSwitchActiveError(
                f"kill switch is forced by the {forced.source} and cannot be released here"
            )
        if self._store is not None:
            self._store.set_meta(KILL_SWITCH_META_KEY, "0")
            self._store.set_meta(KILL_SWITCH_REASON_META_KEY, "")
            self._store.set_meta(KILL_SWITCH_CHANGED_META_KEY, self._clock.now().isoformat())
        self._local = None
        return self.state()

    def engaged(self) -> bool:
        """Return ``True`` while any source forces the switch."""
        return self.state().engaged

    def state(self) -> KillSwitchState:
        """Return the effective state, naming the winning source."""
        forced = self._forced()
        if forced is not None:
            return forced
        stored = self._from_store()
        if stored is not None:
            return stored
        if self._local is not None:
            return self._local
        return KillSwitchState(engaged=False, reason="", changed_at=None, source="none")

    def require_inactive(self) -> None:
        """Raise :class:`KillSwitchActiveError` when the switch is engaged.

        Raises
        ------
        KillSwitchActiveError
            Carrying the reason of the winning source.
        """
        state = self.state()
        if state.engaged:
            raise KillSwitchActiveError(state.reason)

    # -- internals ---------------------------------------------------------

    def _forced(self) -> KillSwitchState | None:
        """Return the state forced by the flag file or the environment, or ``None``."""
        path = self._flag_path
        if path is not None:
            try:
                exists = path.exists()
            except OSError:  # pragma: no cover - unreadable path is a filesystem failure
                exists = False
            if exists:
                reason = self._read_flag_reason(path)
                return KillSwitchState(engaged=True, reason=reason, changed_at=None, source="file")

        value = self._environ.get(ENV_KILL_SWITCH)
        if value is not None and value.strip().lower() in TRUTHY:
            return KillSwitchState(
                engaged=True,
                reason=f"{ENV_KILL_SWITCH} is set",
                changed_at=None,
                source="env",
            )
        return None

    @staticmethod
    def _read_flag_reason(path: Path) -> str:
        """Return the stripped content of the flag file, or the default reason."""
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover - the file vanished between exists() and read
            return _FILE_REASON
        return content or _FILE_REASON

    def _from_store(self) -> KillSwitchState | None:
        """Return the persisted state, or ``None`` when it is unset or released."""
        store = self._store
        if store is None:
            return None
        raw = store.get_meta(KILL_SWITCH_META_KEY)
        if raw is None or raw.strip() != "1":
            return None
        reason = store.get_meta(KILL_SWITCH_REASON_META_KEY) or ""
        changed_at = _parse_timestamp(store.get_meta(KILL_SWITCH_CHANGED_META_KEY))
        return KillSwitchState(engaged=True, reason=reason, changed_at=changed_at, source="store")


def _parse_timestamp(raw: str | None) -> pd.Timestamp | None:
    """Decode a persisted ISO-8601 timestamp, tolerating a corrupted row."""
    if raw is None or not raw.strip():
        return None
    try:
        return pd.Timestamp(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# live trading gate
# ---------------------------------------------------------------------------


class LiveTradingGate:
    """The explicit opt-in that arms a ``live`` profile.

    A profile whose ``mode`` is not ``live`` always passes: paper trading needs no
    arming.  A ``live`` profile is only allowed when the environment carries
    exactly ``TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK``.
    """

    ENV_VAR = "TB_ALLOW_LIVE_TRADING"
    REQUIRED_VALUE = "I_UNDERSTAND_THE_RISK"

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ

    def allowed(self, profile: ProfileConfig) -> bool:
        """Return ``True`` when this profile may trade in its configured mode."""
        if profile.mode != "live":
            return True
        return self._environ.get(self.ENV_VAR) == self.REQUIRED_VALUE

    def check(self, profile: ProfileConfig) -> None:
        """Raise for a ``live`` profile that is not armed, no-op otherwise.

        Raises
        ------
        LiveTradingForbiddenError
            When ``profile.mode == 'live'`` and the environment does not carry the
            exact required value.
        """
        if self.allowed(profile):
            return
        raise LiveTradingForbiddenError(
            f"live trading is forbidden for profile {profile.id!r}: "
            f"set {self.ENV_VAR}={self.REQUIRED_VALUE} to arm it"
        )
