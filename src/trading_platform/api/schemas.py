"""Request bodies of the mutating routes, and the re-exported response models.

:mod:`trading_platform.models` is the single source of truth for every JSON
shape the platform serves: this module never redeclares a response, it imports
the response models and re-exports them so a router can import its whole HTTP
surface from one place. Only the *request* bodies are declared here, because
they are not part of the domain model -- they describe what an operator may
send, not what the platform stores.

Unknown keys of a request body are ignored (``extra="ignore"``), the same
convention the domain models use: a dashboard built against a newer revision
sends a field this one does not know yet and the call still succeeds, instead of
failing with a validation error the operator cannot act on.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Response models: re-exported, never redeclared (ruff sees the re-export
# through ``__all__``).
from ..models import (
    AccountResponse,
    CatalogueApplyResult,
    DashboardSettings,
    EventsResponse,
    HealthStatus,
    KillSwitchResponse,
    ProfileDetail,
    ProfileResponse,
    ProfilesResponse,
    ProfileView,
    StrategiesResponse,
    StrategyView,
)

__all__ = [
    "AccountResponse",
    "CatalogueApplyRequest",
    "CatalogueApplyResult",
    "DashboardSettings",
    "EventsResponse",
    "HealthStatus",
    "KillSwitchRequest",
    "KillSwitchResponse",
    "ProfileActionRequest",
    "ProfileCreateRequest",
    "ProfileDetail",
    "ProfileResponse",
    "ProfileUpdateRequest",
    "ProfileView",
    "ProfilesResponse",
    "SettingsUpdateRequest",
    "StrategiesResponse",
    "StrategyView",
]


class _RequestBody(BaseModel):
    """Base class of the request bodies: unknown keys are ignored."""

    model_config = ConfigDict(extra="ignore")


class ProfileCreateRequest(_RequestBody):
    """``POST /api/profiles``: create an operator-owned profile.

    ``id`` defaults to a slug built from ``name``, ``exchange`` to the platform
    default exchange, ``max_open_trades`` to the platform default and
    ``priority`` to ``0``. The profile is owned by the operator (``source``
    ``operator``), so a later catalogue apply never touches it.
    """

    id: str | None = None
    name: str
    strategy: str
    timeframe: str
    mode: Literal["paper", "live"]
    exchange: str | None = None
    pairs: list[str]
    initial_capital: float
    max_open_trades: int | None = None
    priority: int | None = None


class ProfileUpdateRequest(_RequestBody):
    """``PATCH /api/profiles/{id}``: edit the declarative fields of a profile.

    A field left out of the body is left untouched; ``strategy``, ``timeframe``
    and ``mode`` are deliberately not writable -- changing them would silently
    restart a worker under a different configuration, which is a delete plus a
    create.
    """

    name: str | None = None
    pairs: list[str] | None = None
    initial_capital: float | None = None
    max_open_trades: int | None = None
    priority: int | None = None
    enabled: bool | None = None


class ProfileActionRequest(_RequestBody):
    """``POST /api/profiles/{id}/actions``: start, stop or restart one profile."""

    action: Literal["start", "stop", "restart"]


class KillSwitchRequest(_RequestBody):
    """``POST /api/kill-switch``: engage (``true``) or release (``false``)."""

    engaged: bool


class SettingsUpdateRequest(_RequestBody):
    """``POST /api/settings``: change the run-time settings.

    Both fields are optional; the ones left out keep their current value. The
    change is persisted and reschedules the fleet at once.
    """

    max_running_profiles: int | None = None
    snapshot_interval_seconds: int | None = None


class CatalogueApplyRequest(_RequestBody):
    """``POST /api/catalogue/apply``: apply ``config/profiles.json``.

    ``prune`` additionally deletes the catalogue-owned profiles the document no
    longer declares; without it nothing is ever deleted.
    """

    prune: bool = Field(default=False)
