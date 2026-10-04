"""Aggregation helpers: the single place where raw rows become dashboard views.

Nothing else in the platform sums, ranks or averages profile data. Three
conventions are implemented here and reused everywhere:

* ``profit_pct`` is always ``(portfolio_value - initial_capital) /
  initial_capital`` -- a ratio, never Freqtrade's own percentage;
* ranking is computed once, over every profile of both modes, before any
  filtering or sorting, so a profile keeps the same rank in every widget;
* every value of a ``*_pct`` field is passed through **unchanged**: the store
  keeps a drawdown as the 0..1 ratio Freqtrade published, and this module never
  rescales it (the dashboard is what multiplies by ``100`` for display).
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from .models import (
    STATE_RUNNING,
    AccountPerformance,
    DailyRow,
    EquityPoint,
    HealthStatus,
    ProfileRecord,
    ProfileSnapshot,
    ProfileSummary,
    ProfileView,
    StrategyMeta,
    StrategyView,
    finite_float,
    format_ts,
    normalise_win_rate,
    utc_now,
    window_start,
)

__all__ = [
    "aggregate_account",
    "aggregate_strategy",
    "build_health",
    "build_profile_view",
    "compute_profit_pct",
    "daily_rows",
    "equity_curve_from_snapshots",
    "filter_views",
    "latest_snapshot_map",
    "rank_views",
    "sort_views",
]


# ---------------------------------------------------------------------------
# Small coercions
# ---------------------------------------------------------------------------
def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a counter to ``int`` (Freqtrade sometimes reports them as floats)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _best_pair(snapshot: ProfileSnapshot) -> str | None:
    """Return the snapshot's best pair when it carries one, otherwise ``None``.

    ``best_pair`` is not part of the snapshot contract, so this stays ``None``
    unless the poller stores the optional field.
    """
    candidate = getattr(snapshot, "best_pair", None)
    if isinstance(candidate, str) and candidate:
        return candidate
    return None


# ---------------------------------------------------------------------------
# Core formulas
# ---------------------------------------------------------------------------
def compute_profit_pct(portfolio_value: float, initial_capital: float) -> float:
    """Return the platform profit ratio, ``0.0`` without a usable capital.

    A non-finite portfolio value is treated as unchanged capital: a broken read
    must never be reported as a total loss.
    """
    capital = finite_float(initial_capital, 0.0)
    if capital <= 0:
        return 0.0
    value = finite_float(portfolio_value, capital)
    return finite_float((value - capital) / capital, 0.0)


def equity_curve_from_snapshots(
    snapshots: Iterable[ProfileSnapshot],
    initial_capital: float,
) -> list[EquityPoint]:
    """Build an ascending equity curve out of one profile's snapshots."""
    capital = finite_float(initial_capital, 0.0)
    points: list[EquityPoint] = []
    for snapshot in sorted(snapshots, key=lambda item: item.ts):
        value = finite_float(snapshot.portfolio_value, capital)
        points.append(
            EquityPoint(t=snapshot.ts, value=value, profit_pct=compute_profit_pct(value, capital))
        )
    return points


def latest_snapshot_map(
    snapshots: Iterable[ProfileSnapshot] | Mapping[str, Sequence[ProfileSnapshot]],
) -> dict[str, ProfileSnapshot]:
    """Return the most recent snapshot of every profile.

    Accepts either a flat iterable of snapshots or the
    ``{profile_id: [snapshots]}`` mapping the poller writes, so callers can
    forward whichever they already have.
    """
    latest: dict[str, ProfileSnapshot] = {}
    for snapshot in _iter_snapshots(snapshots):
        current = latest.get(snapshot.profile_id)
        if current is None or snapshot.ts >= current.ts:
            latest[snapshot.profile_id] = snapshot
    return latest


def _iter_snapshots(snapshots: Any) -> Iterator[ProfileSnapshot]:
    if isinstance(snapshots, Mapping):
        for group in snapshots.values():
            if isinstance(group, ProfileSnapshot):
                yield group
            else:
                yield from group
    else:
        yield from snapshots


def daily_rows(payload: Mapping[str, Any] | None) -> list[DailyRow]:
    """Map Freqtrade's ``/daily`` body onto the platform's daily rows."""
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get("data")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    daily: list[DailyRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        date = row.get("date")
        if date is None:
            continue
        abs_profit = finite_float(row.get("abs_profit"), 0.0)
        if "rel_profit" in row:
            rel_profit = finite_float(row.get("rel_profit"), 0.0)
        else:
            starting_balance = finite_float(row.get("starting_balance"), 0.0)
            rel_profit = abs_profit / starting_balance if starting_balance > 0 else 0.0
        daily.append(
            DailyRow(
                date=str(date),
                abs_profit=abs_profit,
                rel_profit=finite_float(rel_profit, 0.0),
                trade_count=_as_int(row.get("trade_count")),
            )
        )
    daily.sort(key=lambda item: item.date)
    return daily


# ---------------------------------------------------------------------------
# Profile views
# ---------------------------------------------------------------------------
def build_profile_view(
    record: ProfileRecord,
    snapshot: ProfileSnapshot | None,
    strategy: StrategyMeta | None,
    *,
    rank: int = 0,
    uptime_seconds: float = 0.0,
    now: datetime | None = None,
) -> ProfileView:
    """Build the API view of one profile.

    Without a snapshot (the profile never ran, or it is queued) the profile is
    shown at its initial capital with a zero profit: inventing a measurement
    would corrupt every aggregate, and showing ``0.0`` would silently remove the
    profile's capital from the account total. Such a profile has no measurable
    profit factor either, so it reports ``None`` rather than the ``0.0`` that
    means "every trade lost".
    """
    initial_capital = finite_float(record.initial_capital, 0.0)
    if snapshot is None:
        portfolio_value = initial_capital
        cash = initial_capital
        positions_value = 0.0
        profit_abs = 0.0
        realized_profit_abs = 0.0
        unrealized_profit_abs = 0.0
        open_trades = 0
        closed_trades = 0
        win_rate = 0.0
        profit_factor: float | None = None
        max_drawdown_pct = 0.0
        best_pair = None
        last_updated = format_ts(now)
    else:
        portfolio_value = finite_float(snapshot.portfolio_value, initial_capital)
        cash = finite_float(snapshot.cash, 0.0)
        positions_value = finite_float(snapshot.positions_value, 0.0)
        profit_abs = finite_float(snapshot.profit_abs, 0.0)
        realized_profit_abs = finite_float(snapshot.realized_profit_abs, 0.0)
        unrealized_profit_abs = finite_float(snapshot.unrealized_profit_abs, 0.0)
        open_trades = _as_int(snapshot.open_trades)
        closed_trades = _as_int(snapshot.closed_trades)
        win_rate = normalise_win_rate(snapshot.win_rate)
        # ``None`` stays ``None``: "no losing trade yet" is not "all losses", and
        # a profile without a defined factor must not dilute the fleet one.
        profit_factor = (
            None if snapshot.profit_factor is None else finite_float(snapshot.profit_factor, 0.0)
        )
        # The stored value is already the 0..1 ratio the API publishes, so it is
        # passed through untouched (no magnitude heuristic, no rescaling).
        max_drawdown_pct = finite_float(snapshot.max_drawdown_pct, 0.0)
        best_pair = _best_pair(snapshot)
        last_updated = snapshot.ts or format_ts(now)

    return ProfileView(
        id=record.id,
        name=record.name,
        strategy=record.strategy,
        strategy_title=strategy.title if strategy is not None else "",
        strategy_category=strategy.category if strategy is not None else "",
        timeframe=record.timeframe,
        mode=record.mode,
        exchange=record.exchange,
        pairs=list(record.pairs),
        initial_capital=initial_capital,
        max_open_trades=_as_int(record.max_open_trades),
        priority=_as_int(record.priority),
        state=record.state,
        state_reason=record.state_reason,
        portfolio_value=portfolio_value,
        cash=cash,
        positions_value=positions_value,
        profit_abs=profit_abs,
        profit_pct=compute_profit_pct(portfolio_value, initial_capital),
        realized_profit_abs=realized_profit_abs,
        unrealized_profit_abs=unrealized_profit_abs,
        open_trades=open_trades,
        closed_trades=closed_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        max_drawdown_pct=max_drawdown_pct,
        best_pair=best_pair,
        uptime_seconds=max(0.0, finite_float(uptime_seconds, 0.0)),
        last_updated=last_updated,
        rank=_as_int(rank),
    )


def rank_views(views: Sequence[ProfileView]) -> list[ProfileView]:
    """Rank every view by portfolio value (DESC, id ASC) and return them sorted.

    The rank is assigned on the views themselves, over both modes, so filtering
    or re-sorting afterwards never changes it. Ties break on the profile id.
    """
    ranked = sorted(views, key=lambda view: (-finite_float(view.portfolio_value, 0.0), view.id))
    for position, view in enumerate(ranked, start=1):
        view.rank = position
    return ranked


def filter_views(
    views: Sequence[ProfileView],
    *,
    mode: str | None = None,
    state: str | None = None,
) -> list[ProfileView]:
    """Filter views by mode and/or state, preserving the incoming order."""
    selected = list(views)
    if mode is not None:
        wanted_mode = str(mode).strip().lower()
        selected = [view for view in selected if view.mode == wanted_mode]
    if state is not None:
        wanted_state = str(state).strip().lower()
        selected = [view for view in selected if view.state == wanted_state]
    return selected


def sort_views(views: Sequence[ProfileView], sort: str = "value") -> list[ProfileView]:
    """Sort views: ``value``, ``profit``, ``name`` or ``strategy``.

    An unknown key falls back to ``value`` so a query string can never break a
    response. Every order is deterministic (the profile id breaks ties).
    """
    key = str(sort).strip().lower()
    items = list(views)
    if key == "profit":
        return sorted(items, key=lambda view: (-finite_float(view.profit_pct, 0.0), view.id))
    if key == "name":
        return sorted(items, key=lambda view: (view.name.casefold(), view.id))
    if key == "strategy":
        return sorted(
            items,
            key=lambda view: (view.strategy, -finite_float(view.portfolio_value, 0.0), view.id),
        )
    return sorted(items, key=lambda view: (-finite_float(view.portfolio_value, 0.0), view.id))


# ---------------------------------------------------------------------------
# Aggregate views
# ---------------------------------------------------------------------------
def _aggregate_win_rate(views: Sequence[ProfileView]) -> float:
    """Pool per-profile win rates, weighted by their closed trade counts."""
    rates = [normalise_win_rate(view.win_rate) for view in views]
    if not rates:
        return 0.0
    weights = [max(0, _as_int(view.closed_trades)) for view in views]
    total = sum(weights)
    if total <= 0:
        return finite_float(sum(rates) / len(rates), 0.0)
    pooled = sum(rate * weight for rate, weight in zip(rates, weights, strict=True))
    return min(max(finite_float(pooled / total, 0.0), 0.0), 1.0)


def _aggregate_profit_factor(views: Sequence[ProfileView]) -> float | None:
    """Pool the per-profile profit factors that are actually defined.

    A view without a factor (``None``: the profile has not lost a trade yet, or
    it was never measured) is **excluded** instead of counted as ``0.0``, so a
    flawless profile no longer drags the fleet factor down. The remaining
    factors are weighted by their closed trade counts; when every one of them
    has no closed trade, the plain mean of the defined factors is the honest
    answer. ``None`` is returned when the scope defines no factor at all.
    """
    defined = [view for view in views if view.profit_factor is not None]
    if not defined:
        return None
    factors = [max(finite_float(view.profit_factor, 0.0), 0.0) for view in defined]
    weights = [max(0, _as_int(view.closed_trades)) for view in defined]
    total = sum(weights)
    if total <= 0:
        return finite_float(sum(factors) / len(factors), 0.0)
    pooled = sum(factor * weight for factor, weight in zip(factors, weights, strict=True))
    return max(finite_float(pooled / total, 0.0), 0.0)


def _sharpe(curve: Sequence[EquityPoint]) -> float:
    """Unannualised Sharpe ratio of the point-to-point returns of ``curve``."""
    values = [finite_float(point.value, 0.0) for point in curve]
    returns = [
        (current - previous) / previous
        for previous, current in zip(values, values[1:], strict=False)
        if previous > 0
    ]
    if len(returns) < 2:
        return 0.0
    spread = statistics.pstdev(returns)
    if spread <= 0:
        return 0.0
    return finite_float(statistics.fmean(returns) / spread, 0.0)


def _combined_equity_curve(
    views: Sequence[ProfileView],
    snapshots_by_profile: Mapping[str, Sequence[ProfileSnapshot]],
    start: datetime | None,
    initial_capital: float,
) -> list[EquityPoint]:
    """Sum the fleet equity per snapshot timestamp, carrying missed minutes forward.

    The timeline is the sorted union of every snapshot timestamp at or after the
    window boundary. Every profile contributes at **every** point of that
    timeline:

    * its last known portfolio value at or before the point, so a minute whose
      read failed no longer drops the profile's whole equity and fakes a cliff
      (measured: -33 % / 1,000 USDT on a single failed minute, corrupting the
      reported Sharpe);
    * its ``initial_capital`` until its first snapshot inside the timeline, and
      when it has no snapshot at all, so a profile never vanishes from the
      fleet total;
    * the value of its most recent snapshot **strictly before** the window
      boundary at the first point of the window, so a window opens at the state
      the profile really was in rather than at its starting capital.

    An empty timeline still answers ``[]``.
    """
    boundary = None if start is None else format_ts(start)
    # The timestamps of the window, and the value each profile measured there.
    window: dict[str, dict[str, float]] = {}
    timeline: set[str] = set()
    for view in views:
        fallback = finite_float(view.initial_capital, 0.0)
        values: dict[str, float] = {}
        for snapshot in snapshots_by_profile.get(view.id, ()):
            if boundary is not None and snapshot.ts < boundary:
                continue
            values[snapshot.ts] = finite_float(snapshot.portfolio_value, fallback)
            timeline.add(snapshot.ts)
        window[view.id] = values
    if not timeline:
        return []

    # The value every profile starts the window from: its most recent snapshot
    # strictly *before* the boundary when it has one -- so the window opens at
    # the state the profile really was in -- and its initial capital otherwise,
    # which is also the answer for a profile with no snapshot at all.
    carried: dict[str, float] = {}
    for view in views:
        fallback = finite_float(view.initial_capital, 0.0)
        latest: ProfileSnapshot | None = None
        if boundary is not None:
            for snapshot in snapshots_by_profile.get(view.id, ()):
                if snapshot.ts >= boundary:
                    continue
                if latest is None or snapshot.ts > latest.ts:
                    latest = snapshot
        carried[view.id] = (
            fallback if latest is None else finite_float(latest.portfolio_value, fallback)
        )

    # One pass over the sorted timeline: a snapshot measured at a point replaces
    # its profile's last known value from that point on, and every profile is
    # summed at every point -- which is the forward fill of the minutes it
    # missed.
    points: list[EquityPoint] = []
    for ts in sorted(timeline):
        for view in views:
            value = window[view.id].get(ts)
            if value is not None:
                carried[view.id] = value
        total = sum(carried.get(view.id, 0.0) for view in views)
        points.append(
            EquityPoint(t=ts, value=total, profit_pct=compute_profit_pct(total, initial_capital))
        )
    return points


def _healthy_count(
    views: Sequence[ProfileView],
    snapshots_by_profile: Mapping[str, Sequence[ProfileSnapshot]],
) -> int:
    """Count the profiles whose latest snapshot is healthy.

    A profile without any snapshot is not counted: the platform has no evidence
    that it is healthy.
    """
    latest = latest_snapshot_map(snapshots_by_profile)
    healthy = 0
    for view in views:
        snapshot = latest.get(view.id)
        if snapshot is not None and snapshot.healthy:
            healthy += 1
    return healthy


def _profile_summary(view: ProfileView | None) -> ProfileSummary | None:
    if view is None:
        return None
    return ProfileSummary(
        id=view.id,
        name=view.name,
        profit_pct=finite_float(view.profit_pct, 0.0),
        portfolio_value=finite_float(view.portfolio_value, 0.0),
    )


def aggregate_account(
    views: Sequence[ProfileView],
    snapshots_by_profile: Mapping[str, Sequence[ProfileSnapshot]],
    *,
    scope: str,
    window: str = "all",
    now: datetime | None = None,
) -> AccountPerformance:
    """Aggregate a set of views (``paper``, ``live`` or ``combined``).

    ``window`` restricts the equity curve to the snapshots inside it; the
    headline numbers always describe the current state of the views, because no
    windowed per-profile metric is stored. Max drawdown is the worst profile
    drawdown -- already a 0..1 ratio in the store, and passed through unchanged
    so the API never rescales it -- which is the risk figure an operator reacts
    to.
    """
    moment = utc_now() if now is None else now
    selected = list(views)
    initial_capital = sum(finite_float(view.initial_capital, 0.0) for view in selected)
    portfolio_value = sum(finite_float(view.portfolio_value, 0.0) for view in selected)
    open_trades = sum(_as_int(view.open_trades) for view in selected)
    curve = _combined_equity_curve(
        selected,
        snapshots_by_profile,
        window_start(window, moment),
        initial_capital,
    )
    best = min(
        selected, key=lambda view: (-finite_float(view.profit_pct, 0.0), view.id), default=None
    )
    worst = min(
        selected, key=lambda view: (finite_float(view.profit_pct, 0.0), view.id), default=None
    )
    return AccountPerformance(
        scope=scope,
        portfolio_value=portfolio_value,
        initial_capital=initial_capital,
        profit_abs=sum(finite_float(view.profit_abs, 0.0) for view in selected),
        profit_pct=compute_profit_pct(portfolio_value, initial_capital),
        realized_profit_abs=sum(finite_float(view.realized_profit_abs, 0.0) for view in selected),
        unrealized_profit_abs=sum(
            finite_float(view.unrealized_profit_abs, 0.0) for view in selected
        ),
        open_positions=open_trades,
        open_trades=open_trades,
        closed_trades=sum(_as_int(view.closed_trades) for view in selected),
        win_rate=_aggregate_win_rate(selected),
        profit_factor=_aggregate_profit_factor(selected),
        # The stored value is the 0..1 ratio Freqtrade published: the worst
        # profile is the LARGEST ratio, and it is served unchanged.
        max_drawdown_pct=max(
            (finite_float(view.max_drawdown_pct, 0.0) for view in selected),
            default=0.0,
        ),
        sharpe=_sharpe(curve),
        profiles_total=len(selected),
        profiles_running=sum(1 for view in selected if view.state == STATE_RUNNING),
        profiles_healthy=_healthy_count(selected, snapshots_by_profile),
        best_profile=_profile_summary(best),
        worst_profile=_profile_summary(worst),
        equity_curve=curve,
        generated_at=format_ts(moment),
    )


def aggregate_strategy(meta: StrategyMeta, views: Sequence[ProfileView]) -> StrategyView:
    """Aggregate every profile that runs one strategy."""
    selected = list(views)
    capital = sum(finite_float(view.initial_capital, 0.0) for view in selected)
    portfolio_value = sum(finite_float(view.portfolio_value, 0.0) for view in selected)
    best = min(
        selected, key=lambda view: (-finite_float(view.profit_pct, 0.0), view.id), default=None
    )
    return StrategyView(
        id=meta.id,
        class_name=meta.class_name,
        title=meta.title,
        category=meta.category,
        summary=meta.summary,
        description=meta.description,
        indicators=list(meta.indicators),
        timeframes=list(meta.timeframes),
        reference=meta.reference,
        risk_notes=meta.risk_notes,
        profile_count=len(selected),
        profiles_running=sum(1 for view in selected if view.state == STATE_RUNNING),
        portfolio_value=portfolio_value,
        profit_abs=sum(finite_float(view.profit_abs, 0.0) for view in selected),
        profit_pct=compute_profit_pct(portfolio_value, capital),
        best_profile_id=best.id if best is not None else None,
        win_rate=_aggregate_win_rate(selected),
    )


def build_health(
    *,
    version: str,
    uptime_seconds: float,
    views: Sequence[ProfileView],
    profiles_healthy: int,
    kill_switch_engaged: bool,
    now: datetime | None = None,
) -> HealthStatus:
    """Build the health payload; the platform is ``ok`` as soon as one profile runs."""
    selected = list(views)
    running = [view for view in selected if view.state == STATE_RUNNING]
    paper = [view for view in selected if view.mode == "paper"]
    live = [view for view in selected if view.mode == "live"]
    return HealthStatus(
        status="ok" if running else "degraded",
        version=version,
        uptime_seconds=max(0.0, finite_float(uptime_seconds, 0.0)),
        profiles_total=len(selected),
        profiles_running=len(running),
        profiles_healthy=max(0, _as_int(profiles_healthy)),
        profiles_paper=len(paper),
        profiles_running_paper=sum(1 for view in paper if view.state == STATE_RUNNING),
        profiles_live=len(live),
        profiles_running_live=sum(1 for view in live if view.state == STATE_RUNNING),
        # With no fleet cap there is no way to hold a profile back, so no profile
        # can ever be queued: the field stays on the wire pinned to the constant.
        profiles_queued=0,
        kill_switch_engaged=bool(kill_switch_engaged),
        generated_at=format_ts(now),
    )
