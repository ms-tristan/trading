"""The aggregated HTTP API: one router, every route of the dashboard contract.

The routes are thin on purpose. Every aggregate is computed by
:mod:`trading_platform.metrics`, every row comes from the state store, and every
process action goes through :class:`~trading_platform.engine.supervisor.Supervisor`
-- this module only validates the request, picks the right aggregate and maps a
domain failure onto an HTTP status.

Two rules shape the surface:

* **reading routes are public, mutating routes are not.** A mutation carries
  ``dependencies=[Depends(require_operator_token)]``; a read has no dependency
  at all, because the dashboard is served same-origin through its ``/api/*``
  rewrite and CORS is deliberately never enabled.
* **an API answer never invents a measurement.** A profile that never ran is
  reported at its initial capital and a profile whose worker is dead is reported
  as ``degraded`` by ``GET /api/health``, even when the stored state still says
  ``running``: the deploy pipeline asserts on exactly that number.

``GET /api/profiles`` ranks once, over both modes, and filters afterwards, so a
profile keeps the same ``rank`` in every widget of the dashboard.

One compatibility alias exists beyond the documented routes:
``POST /api/profiles/{id}/actions/{action}`` accepts the action in the path (the
form the dashboard client calls) and is left out of the OpenAPI schema, whose
published form is the documented body-based route.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from .. import __version__
from ..config import PlatformSettings, allow_live_trading, live_trading_gate
from ..engine.supervisor import (
    EVENT_CATALOGUE_APPLIED,
    EVENT_PROFILE_CREATED,
    EVENT_PROFILE_DELETED,
    EVENT_PROFILE_UPDATED,
    Supervisor,
)
from ..metrics import (
    aggregate_account,
    aggregate_strategy,
    build_health,
    build_profile_view,
    equity_curve_from_snapshots,
    filter_views,
    rank_views,
    sort_views,
)
from ..models import (
    SUPPORTED_TIMEFRAMES,
    AccountResponse,
    CatalogueApplyResult,
    DailyRow,
    DashboardSettings,
    EventsResponse,
    HealthStatus,
    KillSwitchResponse,
    ProfileConfig,
    ProfileDetail,
    ProfileRecord,
    ProfileResponse,
    ProfilesResponse,
    ProfileView,
    StrategiesResponse,
    StrategyView,
    format_ts,
    utc_now,
    window_start,
)
from ..profiles.catalogue import (
    StrategyCatalogue,
    load_profile_catalogue,
    load_strategy_catalogue,
)
from ..profiles.store import StateStore
from .schemas import (
    CatalogueApplyRequest,
    KillSwitchRequest,
    ProfileActionRequest,
    ProfileCreateRequest,
    ProfileUpdateRequest,
    SettingsUpdateRequest,
)
from .security import require_operator_token

__all__ = [
    "API_PREFIX",
    "DAILY_DETAIL_LIMIT",
    "PROFILE_SPARKLINE_POINTS",
    "RECENT_TRADES_LIMIT",
    "router",
]

logger = logging.getLogger(__name__)

#: Mount point of the whole API. ``app.py`` re-exports this constant.
API_PREFIX = "/api"

#: Every supported window, mirroring :data:`trading_platform.models.WINDOWS`.
Window = Literal["24h", "7d", "30d", "all"]

#: A profile action accepted by the API.
ProfileAction = Literal["start", "stop", "restart"]

#: Declarative fields a catalogue apply may refresh on an existing row.
#:
#: A change to a config-affecting field -- ``strategy``, ``timeframe``,
#: ``mode``, ``exchange``, ``pairs``, ``initial_capital``,
#: ``max_open_trades`` -- is written into the file the worker was started from,
#: so the apply route restarts a running worker after the write: the process
#: matches the dashboard instead of quietly trading the values it was spawned
#: with. ``name``, ``priority`` and ``enabled`` never restart anything.
CATALOGUE_MUTABLE_FIELDS: tuple[str, ...] = (
    "name",
    "strategy",
    "timeframe",
    "mode",
    "exchange",
    "pairs",
    "initial_capital",
    "max_open_trades",
    "priority",
    "enabled",
)

#: Number of equity points ``ProfileView.sparkline`` publishes: the 60 most
#: recent snapshots of the profile, oldest first.
PROFILE_SPARKLINE_POINTS = 60

#: Number of days ``GET /api/profiles/{id}`` publishes in ``daily``, the most
#: recent ones, chronological (most recent last).
DAILY_DETAIL_LIMIT = 30

#: Number of closed trades ``GET /api/profiles/{id}`` publishes in
#: ``recent_trades``, the most recent ones, newest first.
RECENT_TRADES_LIMIT = 20

#: The documented mutating dependency, spelled once.
OPERATOR_ONLY = [Depends(require_operator_token)]

router = APIRouter(prefix=API_PREFIX)


# ---------------------------------------------------------------------------
# Accessors: the app state is the only place a route reads its collaborators
# ---------------------------------------------------------------------------
def _supervisor(request: Request) -> Supervisor:
    """Return the engine of this application."""
    return request.app.state.supervisor


def _settings(request: Request) -> PlatformSettings:
    """Return the effective platform settings of this application."""
    return request.app.state.settings


def _store(request: Request) -> StateStore:
    """Return the state store the engine reads and writes."""
    return _supervisor(request).store


def _strategy_catalogue(request: Request) -> StrategyCatalogue:
    """Return the strategy catalogue, honouring an application-level override.

    ``create_app`` installs the catalogue discovered from ``config`` and
    ``user_data/strategies``. A test may set ``app.state.strategy_catalogue``
    before the first request and the whole surface then works against that
    fixed catalogue, without touching the checkout.
    """
    override = getattr(request.app.state, "strategy_catalogue", None)
    if override is not None:
        return override
    return load_strategy_catalogue()


def _profile_catalogue(request: Request) -> list[ProfileConfig]:
    """Return the declarative profile catalogue (``config/profiles.json``)."""
    override = getattr(request.app.state, "profile_catalogue", None)
    if override is not None:
        return list(override)
    return load_profile_catalogue()


def _uptime_seconds(request: Request) -> float:
    """Return how long this API process has been up."""
    started_at = getattr(request.app.state, "started_at", None)
    if not isinstance(started_at, datetime):
        return 0.0
    return max(0.0, (utc_now() - started_at).total_seconds())


def _views(request: Request) -> list[ProfileView]:
    """Return every profile as a ranked view, over both modes.

    The ranking happens here, once, before any filter: ``GET /api/profiles``
    then only filters and re-sorts, so the ``rank`` of a profile never depends on
    the query string that asked for it.

    The monitoring read model is attached here as well, so every route that
    serves a :class:`ProfileView` (``/api/profiles``, ``/api/account``,
    ``/api/profiles/{id}``, ``/api/strategies``) publishes the same three extra
    fields:

    * ``sparkline`` -- the last :data:`PROFILE_SPARKLINE_POINTS` equity points of
      the profile, oldest first, ``[]`` without a snapshot. Every element is the
      documented :class:`~trading_platform.models.EquityPoint`, so it carries
      ``t`` (ISO-8601 UTC), ``value`` and the optional ``profit_pct``;
    * ``slot`` -- the 1-based position of the profile among the running ones.
      ``store.list_profiles()`` already answers priority DESC then id ASC, which
      is the scheduler order, and ``supervisor.is_running`` is the same liveness
      test ``GET /api/health`` uses: the slot of a profile whose worker is dead
      is ``None`` even when the stored state still says ``running``;
    * ``worker_port`` -- the private REST port of the worker, published only
      while the worker is alive: a stopped profile keeps its last port in the
      runtime columns, and the liveness test is what makes the field honest.
    """
    store = _store(request)
    supervisor = _supervisor(request)
    catalogue = _strategy_catalogue(request)
    snapshots = store.latest_snapshots()
    records = store.list_profiles()
    alive = [record.id for record in records if supervisor.is_running(record.id)]
    views = [
        build_profile_view(record, snapshots.get(record.id), catalogue.get(record.strategy))
        for record in records
    ]
    rank_views(views)
    records_by_id = {record.id: record for record in records}
    for view in views:
        record = records_by_id[view.id]
        view.sparkline = equity_curve_from_snapshots(
            store.list_snapshots(view.id, limit=PROFILE_SPARKLINE_POINTS),
            record.initial_capital,
        )
        view.slot = alive.index(view.id) + 1 if view.id in alive else None
        view.worker_port = record.api_port if view.id in alive and record.api_port else None
    return views


def _view_of(request: Request, profile_id: str) -> ProfileView:
    """Return the view of one stored profile (the caller checked it exists)."""
    store = _store(request)
    record = store.get_profile(profile_id)
    if record is None:  # pragma: no cover - the caller just read the same row
        raise _profile_not_found()
    return build_profile_view(
        record,
        store.latest_snapshots().get(profile_id),
        _strategy_catalogue(request).get(record.strategy),
    )


def _profile_not_found() -> HTTPException:
    """Return the ``404`` of an unknown profile id."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="profile not found")


def _live_allowed(mode: str) -> bool:
    """Whether a profile in ``mode`` may run, according to the live-trading gate."""
    allowed, _reason = live_trading_gate(mode)
    return allowed


def _slugify(value: str) -> str:
    """Build a profile id out of a profile name: lowercase, dash separated."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower())
    return cleaned.strip("-")[:64].strip("-")


# ---------------------------------------------------------------------------
# Reading routes (public: no operator token)
# ---------------------------------------------------------------------------
@router.get("/health", response_model=HealthStatus, summary="Liveness of the engine")
def get_health(request: Request) -> HealthStatus:
    """Return the health payload the container healthcheck and the deploy assert on.

    ``status`` is ``ok`` as soon as one worker is alive and ``degraded``
    otherwise; ``profiles_running`` is a plain integer count of the profiles
    whose worker process is alive *right now*, not of the rows whose stored state
    says ``running``. A crashed worker that has not been reaped yet is therefore
    reported as degraded, which is exactly what the deployment smoke test needs
    to distinguish a booted engine from a serving-but-idle API.
    """
    supervisor = _supervisor(request)
    store = supervisor.store
    records = store.list_profiles()
    snapshots = store.latest_snapshots()
    views = [build_profile_view(record, snapshots.get(record.id), None) for record in records]
    alive = [record for record in records if supervisor.is_running(record.id)]
    health = build_health(
        version=__version__,
        uptime_seconds=_uptime_seconds(request),
        views=views,
        profiles_healthy=supervisor.healthy_count(),
        kill_switch_engaged=supervisor.kill_switch_engaged(),
    )
    return health.model_copy(
        update={
            "status": "ok" if alive else "degraded",
            "profiles_running": len(alive),
            "profiles_running_paper": sum(1 for record in alive if record.mode == "paper"),
            "profiles_running_live": sum(1 for record in alive if record.mode == "live"),
        }
    )


@router.get("/account", response_model=AccountResponse, summary="Aggregated performance")
def get_account(
    request: Request,
    window: Window = Query(default="24h"),
) -> AccountResponse:
    """Return the paper, live and combined performance of the whole fleet.

    ``window`` only restricts the equity curve: no windowed per-profile metric is
    stored, so the headline numbers always describe the current state. An unknown
    window is refused with ``422`` by the query validation.
    """
    views = _views(request)
    series = _store(request).snapshot_series(since=window_start(window))
    return AccountResponse(
        paper=aggregate_account(
            [view for view in views if view.mode == "paper"],
            series,
            scope="paper",
            window=window,
        ),
        live=aggregate_account(
            [view for view in views if view.mode == "live"],
            series,
            scope="live",
            window=window,
        ),
        combined=aggregate_account(views, series, scope="combined", window=window),
        generated_at=format_ts(),
    )


@router.get("/profiles", response_model=ProfilesResponse, summary="The ranked fleet")
def list_profiles(
    request: Request,
    mode: str | None = Query(default=None),
    state: str | None = Query(default=None),
    sort: str = Query(default="value"),
    limit: int | None = Query(default=None, ge=0),
) -> ProfilesResponse:
    """Return the profiles, ranked by portfolio value descending.

    ``rank`` is assigned over both modes before the filters run, so
    ``?mode=paper`` keeps the ranks of the full fleet. ``mode``, ``state`` and
    ``sort`` are passed to the lenient metrics helpers: an unknown value selects
    nothing (or falls back to ``value``) instead of failing, so a stale bookmark
    never breaks a page. ``total`` is the number of profiles actually returned.
    """
    ordered = sort_views(filter_views(_views(request), mode=mode, state=state), sort)
    if limit is not None:
        ordered = ordered[:limit]
    return ProfilesResponse(profiles=ordered, total=len(ordered), generated_at=format_ts())


@router.get("/profiles/{profile_id}", response_model=ProfileDetail, summary="One profile")
def get_profile(
    request: Request,
    profile_id: str,
    window: Window = Query(default="24h"),
) -> ProfileDetail:
    """Return one profile with its equity curve, its strategy and its trades.

    The three lists are served from the **state database**, never from an HTTP
    call inside the request path: the poller persists what the worker published,
    so this route answers for a stopped profile as well -- which is exactly what
    the dashboard needs.

    * ``daily`` -- the last :data:`DAILY_DETAIL_LIMIT` days, chronological with
      the most recent day last, each row carrying exactly ``date``,
      ``abs_profit``, ``rel_profit``, ``starting_balance`` and ``trade_count``;
    * ``open_trades`` -- every open row, newest first, with exactly ``trade_id``,
      ``pair``, ``open_date``, ``amount``, ``open_rate``, ``current_rate``,
      ``stake_amount``, ``profit_abs`` and ``profit_pct``;
    * ``recent_trades`` -- the :data:`RECENT_TRADES_LIMIT` most recent closed
      rows, newest first, with exactly ``trade_id``, ``pair``, ``open_date``,
      ``close_date``, ``amount``, ``open_rate``, ``close_rate``,
      ``stake_amount``, ``profit_abs``, ``profit_pct`` and ``exit_reason``.
    """
    store = _store(request)
    record = store.get_profile(profile_id)
    if record is None:
        raise _profile_not_found()
    views = _views(request)
    views_by_id = {view.id: view for view in views}
    catalogue = _strategy_catalogue(request)
    meta = catalogue.get(record.strategy)
    strategy = (
        aggregate_strategy(meta, [view for view in views if view.strategy == meta.id])
        if meta is not None
        else StrategyView(id=record.strategy, title=record.strategy)
    )
    snapshots = store.list_snapshots(profile_id, since=window_start(window))
    trades = store.trade_records(profile_id)
    return ProfileDetail(
        profile=views_by_id[profile_id],
        strategy=strategy,
        equity_curve=equity_curve_from_snapshots(snapshots, record.initial_capital),
        daily=[
            DailyRow(
                date=row.date,
                abs_profit=row.abs_profit,
                rel_profit=row.rel_profit,
                starting_balance=row.starting_balance,
                trade_count=row.trade_count,
            )
            # The store answers newest first; a chart wants the other way round.
            for row in reversed(store.daily_records(profile_id, DAILY_DETAIL_LIMIT))
        ],
        open_trades=[
            {
                "trade_id": trade.trade_id,
                "pair": trade.pair,
                "open_date": trade.open_date,
                "amount": trade.amount,
                "open_rate": trade.open_rate,
                # The frozen ``profile_trades`` DDL carries no live rate, so the
                # documented "current_rate falls back to open_rate when the
                # worker did not publish one" always applies here. This is not a
                # bug: the row keeps the entry price until the trade closes.
                "current_rate": trade.open_rate,
                "stake_amount": trade.stake_amount,
                "profit_abs": trade.profit_abs,
                "profit_pct": trade.profit_pct,
            }
            for trade in trades
            if trade.is_open
        ],
        recent_trades=[
            {
                "trade_id": trade.trade_id,
                "pair": trade.pair,
                "open_date": trade.open_date,
                "close_date": trade.close_date,
                "amount": trade.amount,
                "open_rate": trade.open_rate,
                "close_rate": trade.close_rate,
                "stake_amount": trade.stake_amount,
                "profit_abs": trade.profit_abs,
                # The percentage wp-1's normalisation produced (profit_ratio *
                # 100), which is the scale the dashboard's trade reader assumes.
                "profit_pct": trade.profit_pct,
                "exit_reason": trade.exit_reason,
            }
            for trade in trades
            if not trade.is_open
        ][:RECENT_TRADES_LIMIT],
    )


@router.get("/strategies", response_model=StrategiesResponse, summary="The strategy catalogue")
def list_strategies(request: Request) -> StrategiesResponse:
    """Return every discovered strategy plus the performance of its profiles."""
    views = _views(request)
    return StrategiesResponse(
        strategies=[
            aggregate_strategy(meta, [view for view in views if view.strategy == meta.id])
            for meta in _strategy_catalogue(request).all()
        ]
    )


@router.get("/events", response_model=EventsResponse, summary="The engine journal")
def list_events(
    request: Request,
    limit: int = Query(default=50, ge=0),
    since: str | None = Query(default=None),
) -> EventsResponse:
    """Return the newest journal lines first, optionally from ``since`` (inclusive)."""
    return EventsResponse(
        events=_store(request).list_events(limit=limit, since=since or None),
    )


@router.get("/settings", response_model=DashboardSettings, summary="Operator settings")
def get_settings(request: Request) -> DashboardSettings:
    """Return the settings the engine acts on, plus the state of the kill switch."""
    return _dashboard_settings(request)


# ---------------------------------------------------------------------------
# Mutating routes (every one of them needs the operator token)
# ---------------------------------------------------------------------------
@router.post(
    "/profiles",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=OPERATOR_ONLY,
    summary="Create an operator-owned profile",
)
def create_profile(request: Request, payload: ProfileCreateRequest) -> ProfileResponse:
    """Create a profile owned by the operator.

    The id defaults to a slug of the name. An unknown strategy and an unsupported
    timeframe are refused with ``422`` before anything is written, and an id that
    already exists is refused with ``409``: creating a profile never overwrites
    one, catalogue-owned or operator-owned.
    """
    store = _store(request)
    settings = _settings(request)
    if _strategy_catalogue(request).get(payload.strategy) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown strategy: {payload.strategy}",
        )
    if payload.timeframe not in SUPPORTED_TIMEFRAMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unsupported timeframe: {payload.timeframe}",
        )
    profile_id = (payload.id or "").strip() or _slugify(payload.name)
    if not profile_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="profile id must not be empty",
        )
    if store.get_profile(profile_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="profile already exists",
        )
    profile = ProfileConfig(
        id=profile_id,
        name=payload.name,
        strategy=payload.strategy,
        timeframe=payload.timeframe,
        mode=payload.mode,
        exchange=payload.exchange or settings.default_exchange,
        pairs=list(payload.pairs),
        initial_capital=payload.initial_capital,
        max_open_trades=(
            settings.default_max_open_trades
            if payload.max_open_trades is None
            else payload.max_open_trades
        ),
        priority=0 if payload.priority is None else payload.priority,
    )
    store.upsert_profile(profile, source="operator")
    store.record_event(
        "info",
        EVENT_PROFILE_CREATED,
        f"profile created by the operator: {profile_id}",
        profile_id=profile_id,
    )
    _supervisor(request).schedule()
    return ProfileResponse(profile=_view_of(request, profile_id))


@router.patch(
    "/profiles/{profile_id}",
    response_model=ProfileResponse,
    dependencies=OPERATOR_ONLY,
    summary="Edit a profile",
)
def update_profile(
    request: Request,
    profile_id: str,
    payload: ProfileUpdateRequest,
) -> ProfileResponse:
    """Write the declarative fields carried by the body and leave the rest alone.

    A field that is absent from the body is not written; a field sent as ``null``
    is ignored as well, so this route never clears a value by accident. The
    scheduler runs immediately, which is what stops a profile disabled here.

    ``initial_capital``, ``max_open_trades`` and ``pairs`` are written into the
    configuration file the worker was started from, so editing one of them on a
    **running** profile restarts its worker through
    :meth:`~trading_platform.engine.supervisor.Supervisor.apply_config_change`
    (journaled as ``restart``): the process reloads the file instead of trading
    the values the dashboard no longer shows. ``name``, ``priority`` and
    ``enabled`` only change the stored row, so they leave the worker alone; a
    stopped profile is never started by an edit. The response shape is
    unchanged -- it is the refreshed view of the profile.
    """
    store = _store(request)
    if store.get_profile(profile_id) is None:
        raise _profile_not_found()
    supervisor = _supervisor(request)
    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    if fields:
        store.update_profile_fields(profile_id, fields)
        supervisor.apply_config_change(profile_id, fields)
        store.record_event(
            "info",
            EVENT_PROFILE_UPDATED,
            f"profile updated by the operator: {', '.join(sorted(fields))}",
            profile_id=profile_id,
        )
    supervisor.schedule()
    return ProfileResponse(profile=_view_of(request, profile_id))


@router.delete(
    "/profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=OPERATOR_ONLY,
    summary="Delete a profile",
)
def delete_profile(
    request: Request,
    profile_id: str,
    force: bool = Query(default=False),
) -> None:
    """Stop and delete one profile.

    A catalogue-owned profile is protected: deleting it would only be undone by
    the next catalogue apply, so it takes an explicit ``?force=true``. An
    operator-owned profile is always deletable. Every profile id is refused with
    ``404`` until the row exists. Nothing is returned.
    """
    supervisor = _supervisor(request)
    store = supervisor.store
    record = store.get_profile(profile_id)
    if record is None:
        raise _profile_not_found()
    if record.source == "catalogue" and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="catalogue profile cannot be deleted without force=true",
        )
    supervisor.stop_profile(profile_id)
    store.delete_profile(profile_id)
    store.record_event("info", EVENT_PROFILE_DELETED, f"profile deleted: {profile_id}")
    supervisor.schedule()


@router.post(
    "/profiles/{profile_id}/actions",
    response_model=ProfileResponse,
    dependencies=OPERATOR_ONLY,
    summary="Start, stop or restart a profile",
)
def post_profile_action(
    request: Request,
    profile_id: str,
    payload: ProfileActionRequest,
) -> ProfileResponse:
    """Apply one operator action to one profile and return its refreshed view."""
    return _apply_action(request, profile_id, payload.action)


@router.post(
    "/profiles/{profile_id}/actions/{action}",
    response_model=ProfileResponse,
    dependencies=OPERATOR_ONLY,
    include_in_schema=False,
)
def post_profile_action_in_path(
    request: Request,
    profile_id: str,
    action: ProfileAction,
) -> ProfileResponse:
    """Compatibility alias of the action route, with the action in the path.

    The dashboard client posts ``/api/profiles/{id}/actions/{action}``; the
    documented route carries the action in the body. Both are served, so neither
    caller has to change. The alias is hidden from the OpenAPI schema.
    """
    return _apply_action(request, profile_id, action)


@router.post(
    "/catalogue/apply",
    response_model=CatalogueApplyResult,
    dependencies=OPERATOR_ONLY,
    summary="Apply config/profiles.json",
)
def apply_catalogue(
    request: Request,
    payload: Annotated[CatalogueApplyRequest | None, Body()] = None,
) -> CatalogueApplyResult:
    """Upsert the declarative catalogue into the state store, idempotently.

    The rules are the ones the runbook documents:

    * a profile the operator owns (``source == "operator"``) is **never** touched
      -- it ends up in ``skipped``;
    * a catalogue profile is created when missing (``created``), refreshed when a
      declarative field changed (``updated``) and left alone otherwise
      (``skipped``), which makes a second apply a no-op. A refresh that changes
      a config-affecting field (``strategy``, ``timeframe``, ``mode``,
      ``exchange``, ``pairs``, ``initial_capital``, ``max_open_trades``)
      restarts the worker of a **running** profile, so the process picks the new
      configuration up instead of keeping the one it was spawned with; a
      ``name``/``priority``/``enabled`` change never restarts anything and a
      stopped profile is never started by an apply;
    * a live entry whose gate is unmet is never created: its id is reported in
      ``refused_live``. An existing live row is still refreshed, because the live
      gate governs starting a worker, not owning a row -- the scheduler blocks it
      from running;
    * nothing is deleted unless ``prune`` is true, and then only catalogue-owned
      rows the document no longer declares (``pruned``).
    """
    supervisor = _supervisor(request)
    store = supervisor.store
    result = CatalogueApplyResult()
    declared = _profile_catalogue(request)
    known = {profile.id for profile in declared}
    for profile in declared:
        existing = store.get_profile(profile.id)
        if existing is None:
            if not _live_allowed(profile.mode):
                result.refused_live.append(profile.id)
                continue
            store.upsert_profile(profile, source="catalogue")
            result.created.append(profile.id)
            continue
        if existing.source == "operator":
            result.skipped.append(profile.id)
            continue
        changes = _catalogue_changes(existing, profile)
        if changes:
            store.update_profile_fields(profile.id, changes)
            supervisor.apply_config_change(profile.id, changes)
            result.updated.append(profile.id)
        else:
            result.skipped.append(profile.id)
    if payload is not None and payload.prune:
        for record in store.list_profiles():
            if record.source != "catalogue" or record.id in known:
                continue
            supervisor.stop_profile(record.id)
            store.delete_profile(record.id)
            result.pruned.append(record.id)
        result.pruned.sort()
    store.record_event(
        "info",
        EVENT_CATALOGUE_APPLIED,
        f"catalogue applied: {len(result.created)} created, {len(result.updated)} updated, "
        f"{len(result.skipped)} skipped, {len(result.pruned)} pruned, "
        f"{len(result.refused_live)} refused (live)",
    )
    supervisor.schedule()
    return result


@router.post(
    "/kill-switch",
    response_model=KillSwitchResponse,
    dependencies=OPERATOR_ONLY,
    summary="Engage or release the kill switch",
)
def post_kill_switch(request: Request, payload: KillSwitchRequest) -> KillSwitchResponse:
    """Engage the kill switch (stop everything) or release it (resume scheduling)."""
    supervisor = _supervisor(request)
    if payload.engaged:
        supervisor.engage_kill_switch()
    else:
        supervisor.release_kill_switch()
    return KillSwitchResponse(kill_switch_engaged=bool(supervisor.kill_switch_engaged()))


@router.post(
    "/settings",
    response_model=DashboardSettings,
    dependencies=OPERATOR_ONLY,
    summary="Change the run-time settings",
)
def post_settings(request: Request, payload: SettingsUpdateRequest) -> DashboardSettings:
    """Persist the settings sent by the operator and reschedule the fleet at once.

    The field of the body is optional: left out it is not written by the
    supervisor, so a partial update never clears a setting the operator did not
    mention.
    """
    supervisor = _supervisor(request)
    settings = supervisor.apply_settings(
        snapshot_interval_seconds=payload.snapshot_interval_seconds,
    )
    request.app.state.settings = settings
    return _dashboard_settings(request)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _apply_action(request: Request, profile_id: str, action: str) -> ProfileResponse:
    """Run one profile action through the supervisor and return the new view."""
    supervisor = _supervisor(request)
    if supervisor.store.get_profile(profile_id) is None:
        raise _profile_not_found()
    logger.info("operator action %s on profile %s", action, profile_id)
    if action == "start":
        supervisor.start_profile(profile_id)
    elif action == "stop":
        supervisor.stop_profile(profile_id)
    else:
        supervisor.restart_profile(profile_id)
    return ProfileResponse(profile=_view_of(request, profile_id))


def _dashboard_settings(request: Request) -> DashboardSettings:
    """Build the operator settings payload shared by both settings routes."""
    supervisor = _supervisor(request)
    settings = _settings(request)
    counts = supervisor.store.count_by_source()
    return DashboardSettings(
        snapshot_interval_seconds=int(settings.snapshot_interval_seconds),
        kill_switch_engaged=bool(supervisor.kill_switch_engaged()),
        allow_live_trading=allow_live_trading(),
        catalogue_profile_count=int(counts.get("catalogue", 0)),
        operator_profile_count=int(counts.get("operator", 0)),
        state_db_path=str(supervisor.store.path),
        version=__version__,
    )


def _catalogue_changes(record: ProfileRecord, profile: ProfileConfig) -> dict[str, Any]:
    """Return the declarative fields of ``profile`` that differ from the stored row.

    Only the fields a catalogue apply owns are compared, and every value is
    normalised the way the store writes it (``pairs`` as a list, the numbers as
    ``float``/``int``), so an apply that changes nothing reports no update.
    """
    changes: dict[str, Any] = {}
    for name in CATALOGUE_MUTABLE_FIELDS:
        current = getattr(record, name)
        wanted = getattr(profile, name)
        if name == "pairs":
            same = list(current) == list(wanted)
        elif name == "initial_capital":
            same = float(current) == float(wanted)
        elif name in {"max_open_trades", "priority"}:
            same = int(current) == int(wanted)
        elif name == "enabled":
            same = bool(current) == bool(wanted)
        else:
            same = current == wanted
        if not same:
            changes[name] = wanted
    return changes
