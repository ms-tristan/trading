/**
 * Translation layer between the platform API wire format and the dashboard view
 * model.
 *
 * The Python API (`src/trading_platform/api/`) is the source of truth: it
 * serialises `snake_case` and publishes the payloads documented in
 * `docs/architecture.md` section 9. This module transcribes those payloads
 * (`Api*` types below) and maps each of them onto the view model of
 * `./types.ts`, which is what the components render. Keeping the mapping in one
 * place means a wire change is a change to this file only, and the components
 * never guess a field name.
 *
 * Two rules shape every mapper:
 *
 * * **tolerant reads.** A missing, `null` or mistyped field degrades to the
 *   documented default (`0`, `""`, `[]`, `null`) instead of throwing, so a
 *   partially deployed API still renders an empty but well-formed page. No
 *   mapper can emit `NaN` or `Infinity`: every number goes through
 *   {@link asNumber}, and every *optional* measurement (the `null` the API
 *   publishes for an undefined `profit_factor`) goes through
 *   {@link asOptionalNumber}, which answers `null` and never `0`.
 * * **no invented measurement.** When the API publishes no counterpart for a
 *   view-model field, the mapper leaves it empty and says so in a comment: the
 *   strategy `pairs`/`open_trades` scalar and the dashboard refresh interval are
 *   the remaining cases.
 *
 * Field-for-field correspondence (wire -> view model):
 *
 * | Wire | View model |
 * | --- | --- |
 * | `health.profiles_running` | `HealthStatus.profiles_running` |
 * | `profile.profit_abs` | `ProfileView.profit_usdt` |
 * | `profile.last_updated` | `ProfileView.updated_at` |
 * | `profile.slot` / `.worker_port` | `ProfileView.engine_slot` / `.api_port` |
 * | `profile.sparkline[].value` | `ProfileView.sparkline` |
 * | `equity_point.t` / `.value` | `EquityPoint.timestamp` / `.portfolio_value` |
 * | `account.combined` | `AccountResponse.performance` |
 * | `account.combined.equity_curve` | `AccountResponse.equity_curve` |
 * | `profile_detail.profile` / `.strategy` | `ProfileDetail.profile` / `.strategy` |
 * | `profile_detail.daily.abs_profit` | `DailyBarPoint.profit_usdt` |
 * | `profile_detail.daily.trade_count` | `DailyBarPoint.trades` |
 * | `strategy.profile_count` | `StrategyView.profiles_total` |
 * | `event.ts` / `event.id` | `EventItem.timestamp` / `EventItem.id` (string) |
 * | `catalogue.skipped` / `.refused_live` | `CatalogueApplyResponse.unchanged` / `.refused` |
 * | `kill_switch.kill_switch_engaged` | `KillSwitchResponse.engaged` |
 */

import {
  DEFAULT_REFRESH_INTERVAL_SECONDS,
  type AccountPerformance,
  type AccountResponse,
  type ApiWindow,
  type CatalogueApplyResponse,
  type DailyBarPoint,
  type DashboardSettings,
  type EquityPoint,
  type EventItem,
  type EventsResponse,
  type HealthStatus,
  type KillSwitchResponse,
  type ProfileActionResponse,
  type ProfileConfigView,
  type ProfileDetail,
  type ProfileMode,
  type ProfileState,
  type ProfileView,
  type ProfilesResponse,
  type StrategiesResponse,
  type StrategyView,
} from "./types";

// ---------------------------------------------------------------------------
// Raw wire shapes (transcription of the payloads the API serves)
// ---------------------------------------------------------------------------

/** `GET /api/health` */
export interface ApiHealth {
  status?: unknown;
  version?: unknown;
  uptime_seconds?: unknown;
  profiles_total?: unknown;
  profiles_running?: unknown;
  profiles_healthy?: unknown;
  profiles_paper?: unknown;
  profiles_live?: unknown;
  /**
   * Retained for compatibility with an engine that still publishes it, and
   * always `0` on the current engine.
   */
  profiles_queued?: unknown;
  kill_switch_engaged?: unknown;
  generated_at?: unknown;
}

/** One row of `GET /api/profiles` (the platform's `ProfileView`). */
export interface ApiProfile {
  id?: unknown;
  name?: unknown;
  strategy?: unknown;
  strategy_title?: unknown;
  strategy_category?: unknown;
  timeframe?: unknown;
  mode?: unknown;
  state?: unknown;
  state_reason?: unknown;
  exchange?: unknown;
  pairs?: unknown;
  initial_capital?: unknown;
  portfolio_value?: unknown;
  cash?: unknown;
  positions_value?: unknown;
  profit_abs?: unknown;
  profit_pct?: unknown;
  realized_profit_abs?: unknown;
  unrealized_profit_abs?: unknown;
  open_trades?: unknown;
  closed_trades?: unknown;
  win_rate?: unknown;
  profit_factor?: unknown;
  max_drawdown_pct?: unknown;
  max_open_trades?: unknown;
  priority?: unknown;
  rank?: unknown;
  uptime_seconds?: unknown;
  best_pair?: unknown;
  /** Portfolio-value series of the profile: `[{t, value, profit_pct}]`, oldest first. */
  sparkline?: unknown;
  /** 1-based engine slot among the running profiles, `null` when not running. */
  slot?: unknown;
  /** Private freqtrade REST port of the running worker, `null` otherwise. */
  worker_port?: unknown;
  last_updated?: unknown;
}

/** `GET /api/profiles` */
export interface ApiProfiles {
  generated_at?: unknown;
  total?: unknown;
  profiles?: unknown;
}

/** One point of a published equity curve. */
export interface ApiEquityPoint {
  t?: unknown;
  value?: unknown;
  profit_pct?: unknown;
}

/** One scope of `GET /api/account` (`paper`, `live` or `combined`). */
export interface ApiAggregate {
  scope?: unknown;
  portfolio_value?: unknown;
  initial_capital?: unknown;
  profit_abs?: unknown;
  profit_pct?: unknown;
  realized_profit_abs?: unknown;
  unrealized_profit_abs?: unknown;
  open_positions?: unknown;
  open_trades?: unknown;
  closed_trades?: unknown;
  win_rate?: unknown;
  profit_factor?: unknown;
  max_drawdown_pct?: unknown;
  sharpe?: unknown;
  profiles_total?: unknown;
  profiles_running?: unknown;
  profiles_healthy?: unknown;
  equity_curve?: unknown;
  generated_at?: unknown;
}

/** `GET /api/account` */
export interface ApiAccount {
  paper?: unknown;
  live?: unknown;
  combined?: unknown;
  generated_at?: unknown;
}

/** One row of `GET /api/strategies` (the platform's `StrategyView`). */
export interface ApiStrategy {
  id?: unknown;
  class_name?: unknown;
  title?: unknown;
  category?: unknown;
  summary?: unknown;
  description?: unknown;
  indicators?: unknown;
  timeframes?: unknown;
  reference?: unknown;
  risk_notes?: unknown;
  profile_count?: unknown;
  profiles_running?: unknown;
  portfolio_value?: unknown;
  profit_abs?: unknown;
  profit_pct?: unknown;
  best_profile_id?: unknown;
  win_rate?: unknown;
}

/** `GET /api/strategies` */
export interface ApiStrategies {
  strategies?: unknown;
}

/** `GET /api/profiles/{id}` */
export interface ApiProfileDetail {
  profile?: unknown;
  strategy?: unknown;
  equity_curve?: unknown;
  daily?: unknown;
  open_trades?: unknown;
  recent_trades?: unknown;
}

/** One row of `GET /api/events` */
export interface ApiEvent {
  id?: unknown;
  ts?: unknown;
  profile_id?: unknown;
  level?: unknown;
  kind?: unknown;
  message?: unknown;
}

/** `GET /api/events` */
export interface ApiEvents {
  events?: unknown;
}

/** `GET /api/settings` */
export interface ApiSettings {
  snapshot_interval_seconds?: unknown;
  kill_switch_engaged?: unknown;
  allow_live_trading?: unknown;
  catalogue_profile_count?: unknown;
  operator_profile_count?: unknown;
  state_db_path?: unknown;
  version?: unknown;
}

/** Envelope of every mutating route that answers one profile. */
export interface ApiProfileEnvelope {
  profile?: unknown;
}

/** `POST /api/catalogue/apply` */
export interface ApiCatalogueApply {
  created?: unknown;
  updated?: unknown;
  skipped?: unknown;
  pruned?: unknown;
  refused_live?: unknown;
}

/** `POST /api/kill-switch` */
export interface ApiKillSwitch {
  kill_switch_engaged?: unknown;
}

// ---------------------------------------------------------------------------
// Defensive readers
// ---------------------------------------------------------------------------

/** A decoded JSON object. */
type JsonObject = Record<string, unknown>;

/** Read `value` as a JSON object, or `{}` when it is anything else. */
export function asRecord(value: unknown): JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

/** Read `value` as a JSON array, or `[]` when it is anything else. */
export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/** Read a finite number, or `fallback` (never `NaN`, never `Infinity`). */
export function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/**
 * Read an optional finite number: the number itself, or `null` when the field is
 * absent, `null` or not a finite number.
 *
 * This is the reader of the measurements the API may publish as `null` because
 * they are undefined - `profit_factor` is `null` while no losing trade has
 * closed, because `Infinity` has no JSON encoding. `null` must stay `null` and
 * never become `0`, which would claim the opposite measurement (all losses); the
 * components render it as an em dash.
 */
export function asOptionalNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Read a string, or `fallback`. */
export function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/** Read an optional string: `null` when the field is absent or not a string. */
export function asOptionalString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** Read a boolean, or `fallback`. */
export function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

/** Read a JSON array of objects, dropping every entry that is not an object. */
export function asRecordArray(value: unknown): Record<string, unknown>[] {
  return asArray(value).filter(
    (entry): entry is Record<string, unknown> =>
      typeof entry === "object" && entry !== null && !Array.isArray(entry),
  );
}

/** Read an array of strings, dropping every non-string entry. */
export function asStringArray(value: unknown): string[] {
  return asArray(value).filter((entry): entry is string => typeof entry === "string");
}

/** Narrow a wire mode onto the two modes the platform runs. */
export function asMode(value: unknown): ProfileMode {
  return value === "live" ? "live" : "paper";
}

/**
 * Narrow a wire state onto the four documented lifecycle states.
 *
 * The literal `"queued"` is tolerated on purpose: an older engine still
 * publishes profiles that wait for a worker slot, and a dashboard must degrade
 * such a row to a readable state instead of crashing the page. Nothing waits for
 * a slot any more, so the row is reported as `stopped`.
 */
export function asState(value: unknown): ProfileState {
  if (value === "queued") {
    return "stopped";
  }
  return value === "running" || value === "stopped" || value === "error" || value === "blocked"
    ? value
    : "stopped";
}

/** Narrow a wire event level onto the three documented levels. */
export function asLevel(value: unknown): EventItem["level"] {
  return value === "warning" || value === "error" ? value : "info";
}

// ---------------------------------------------------------------------------
// Mappers
// ---------------------------------------------------------------------------

/**
 * One profile of the ranking.
 *
 * `sparkline` is read from the `{t, value, profit_pct}` points of the wire and
 * reduced to their values (oldest first): the table renders the series, never the
 * timestamps. A point the payload malformed is dropped and the order of the
 * remaining ones is preserved.
 *
 * `slot` and `worker_port` describe the worker of a profile: `slot` is the
 * 1-based position of a running profile among the running ones (priority
 * descending, then id ascending) and `worker_port` its private freqtrade REST
 * port. A profile that holds no worker answers `null` for both, which is exactly
 * the `null` the view model keeps.
 */
export function toProfileView(raw: unknown): ProfileView {
  const row = asRecord(raw);
  const view: ProfileView = {
    id: asString(row.id),
    name: asString(row.name),
    strategy: asString(row.strategy),
    strategy_title: asString(row.strategy_title, asString(row.strategy)),
    timeframe: asString(row.timeframe),
    pairs: asStringArray(row.pairs),
    mode: asMode(row.mode),
    state: asState(row.state),
    state_reason: asOptionalString(row.state_reason),
    portfolio_value: asNumber(row.portfolio_value),
    initial_capital: asNumber(row.initial_capital),
    profit_usdt: asNumber(row.profit_abs),
    profit_pct: asNumber(row.profit_pct),
    open_trades: asNumber(row.open_trades),
    closed_trades: asNumber(row.closed_trades),
    win_rate: asNumber(row.win_rate),
    // `null` when no losing trade has closed yet: an undefined factor, not `0`.
    profit_factor: asOptionalNumber(row.profit_factor),
    max_drawdown_pct: asNumber(row.max_drawdown_pct),
    engine_slot: typeof row.slot === "number" ? row.slot : null,
    api_port: typeof row.worker_port === "number" ? row.worker_port : null,
    sparkline: asRecordArray(row.sparkline)
      .map((point) => asNumber(point.value, Number.NaN))
      .filter((value) => Number.isFinite(value)),
    updated_at: asString(row.last_updated),
  };
  // The wire publishes more than the table of the overview renders; the extra
  // fields are carried through so the profile page can read them.
  const rank = row.rank;
  if (typeof rank === "number") {
    view.rank = rank;
  }
  const uptime = row.uptime_seconds;
  if (typeof uptime === "number") {
    view.uptime_seconds = uptime;
  }
  const cash = row.cash;
  if (typeof cash === "number") {
    view.cash = cash;
  }
  const positionsValue = row.positions_value;
  if (typeof positionsValue === "number") {
    view.positions_value = positionsValue;
  }
  const maxOpenTrades = row.max_open_trades;
  if (typeof maxOpenTrades === "number") {
    view.max_open_trades = maxOpenTrades;
  }
  const priority = row.priority;
  if (typeof priority === "number") {
    view.priority = priority;
  }
  if (typeof row.exchange === "string") {
    view.exchange = row.exchange;
  }
  if (typeof row.strategy_category === "string") {
    view.strategy_category = row.strategy_category;
  }
  if (typeof row.best_pair === "string" || row.best_pair === null) {
    view.best_pair = row.best_pair as string | null;
  }
  return view;
}

/** `GET /api/profiles` */
export function toProfilesResponse(raw: unknown): ProfilesResponse {
  const body = asRecord(raw);
  return {
    generated_at: asString(body.generated_at),
    profiles: asArray(body.profiles).map(toProfileView),
  };
}

/**
 * One equity point.
 *
 * The wire carries `t`, `value` and the ratio `profit_pct`; the absolute profit
 * is derived from the initial capital the curve belongs to, because
 * `portfolio_value - initial_capital` is exactly the documented definition.
 */
export function toEquityPoint(raw: unknown, initialCapital: number): EquityPoint {
  const point = asRecord(raw);
  const portfolioValue = asNumber(point.value);
  return {
    timestamp: asString(point.t),
    portfolio_value: portfolioValue,
    profit_usdt: portfolioValue - initialCapital,
    profit_pct: asNumber(point.profit_pct),
  };
}

/** A published equity curve. */
export function toEquityCurve(raw: unknown, initialCapital: number): EquityPoint[] {
  return asArray(raw).map((point) => toEquityPoint(point, initialCapital));
}

/**
 * The dashboard performance block of one account scope.
 *
 * `/api/account` publishes the profile counters and nothing else about the
 * fleet: every field of this block is mapped one for one.
 */
export function toAccountPerformance(raw: unknown, window: ApiWindow): AccountPerformance {
  const scope = asRecord(raw);
  const profilesRunning = asNumber(scope.profiles_running);
  return {
    window,
    portfolio_value: asNumber(scope.portfolio_value),
    initial_capital: asNumber(scope.initial_capital),
    profit_usdt: asNumber(scope.profit_abs),
    profit_pct: asNumber(scope.profit_pct),
    realised_profit_usdt: asNumber(scope.realized_profit_abs),
    unrealised_profit_usdt: asNumber(scope.unrealized_profit_abs),
    open_trades: asNumber(scope.open_trades),
    closed_trades: asNumber(scope.closed_trades),
    win_rate: asNumber(scope.win_rate),
    // `null` when the scope measured none (no losing trade yet): not `0`.
    profit_factor: asOptionalNumber(scope.profit_factor),
    max_drawdown_pct: asNumber(scope.max_drawdown_pct),
    profiles_total: asNumber(scope.profiles_total),
    profiles_running: profilesRunning,
  };
}

/** `GET /api/account` -- the combined scope drives the overview band. */
export function toAccountResponse(raw: unknown, window: ApiWindow): AccountResponse {
  const body = asRecord(raw);
  const combined = asRecord(body.combined);
  const initialCapital = asNumber(combined.initial_capital);
  return {
    window,
    generated_at: asString(body.generated_at, asString(combined.generated_at)),
    performance: toAccountPerformance(combined, window),
    equity_curve: toEquityCurve(combined.equity_curve, initialCapital),
  };
}

/**
 * The dashboard settings.
 *
 * `refresh_interval_seconds` is a *dashboard* preference and the API does not
 * publish one: it keeps the documented default. The engine settings the API
 * does publish are mapped one for one.
 */
export function toDashboardSettings(raw: unknown): DashboardSettings {
  const body = asRecord(raw);
  const snapshotInterval = body.snapshot_interval_seconds;
  const portBase = body.engine_api_port_base;
  return {
    refresh_interval_seconds: DEFAULT_REFRESH_INTERVAL_SECONDS,
    allow_live_trading: asBoolean(body.allow_live_trading),
    snapshot_interval_seconds: typeof snapshotInterval === "number" ? snapshotInterval : undefined,
    engine_api_port_base: typeof portBase === "number" ? portBase : undefined,
    kill_switch_engaged: asBoolean(body.kill_switch_engaged),
    updated_at: asOptionalString(body.updated_at) ?? undefined,
  };
}

/** One row of `GET /api/strategies`. */
export function toStrategyView(raw: unknown): StrategyView {
  const row = asRecord(raw);
  const timeframes = asStringArray(row.timeframes);
  const view: StrategyView = {
    id: asString(row.id),
    title: asString(row.title, asString(row.id)),
    // The catalogue publishes a list of timeframes, never one scalar.
    timeframe: timeframes.length > 0 ? timeframes[0] : "",
    // The catalogue is pair agnostic: the pairs belong to the profiles.
    pairs: [],
    description: asString(row.description, asString(row.summary)),
    // An omitted aggregate stays *unknown* (`NaN`, the convention of
    // `@/lib/components/strategies/strategyMeta`), so the strategies page can
    // still derive the count from the ranked profile list instead of reading a
    // zero the API never published.
    profiles_total: typeof row.profile_count === "number" ? row.profile_count : Number.NaN,
    profiles_running: asNumber(row.profiles_running),
    portfolio_value: asNumber(row.portfolio_value),
    profit_usdt: asNumber(row.profit_abs),
    profit_pct: asNumber(row.profit_pct),
    open_trades: 0,
    closed_trades: 0,
    win_rate: asNumber(row.win_rate),
    // The API publishes no strategy-level factor: an omitted measurement is
    // *undefined*, so it stays `null` (an em dash) instead of a `0` that would
    // claim every trade lost.
    profit_factor: null,
    sparkline: [],
  };
  // Catalogue metadata: carried through so a strategy card renders the same
  // block from `GET /api/strategies` and from `GET /api/profiles/{id}`.
  if (typeof row.class_name === "string") {
    view.class_name = row.class_name;
  }
  if (typeof row.category === "string") {
    view.category = row.category;
  }
  if (typeof row.summary === "string") {
    view.summary = row.summary;
  }
  if (row.indicators !== undefined) {
    view.indicators = asStringArray(row.indicators);
  }
  if (row.timeframes !== undefined) {
    view.timeframes = timeframes;
  }
  if (typeof row.reference === "string") {
    view.reference = row.reference;
  }
  if (typeof row.risk_notes === "string") {
    view.risk_notes = row.risk_notes;
  }
  if (typeof row.best_profile_id === "string") {
    view.best_profile_id = row.best_profile_id;
  }
  return view;
}

/** `GET /api/strategies` */
export function toStrategiesResponse(raw: unknown): StrategiesResponse {
  const body = asRecord(raw);
  return { generated_at: "", strategies: asArray(body.strategies).map(toStrategyView) };
}

/** One journal row. */
export function toEventItem(raw: unknown): EventItem {
  const row = asRecord(raw);
  const id = row.id;
  return {
    id: typeof id === "number" || typeof id === "string" ? String(id) : "",
    timestamp: asString(row.ts),
    level: asLevel(row.level),
    kind: asString(row.kind),
    profile_id: asOptionalString(row.profile_id),
    message: asString(row.message),
  };
}

/** `GET /api/events` */
export function toEventsResponse(raw: unknown): EventsResponse {
  const body = asRecord(raw);
  return { generated_at: "", events: asArray(body.events).map(toEventItem) };
}

/**
 * One daily bar of a profile detail (`GET /api/profiles/{id}`).
 *
 * The wire serves `abs_profit` and `trade_count`; the older `profit_abs`/`trades`
 * pair is still accepted so a partially deployed API keeps rendering its bars.
 */
export function toDailyBar(raw: unknown): DailyBarPoint {
  const row = asRecord(raw);
  return {
    date: asString(row.date, asString(row.ts)),
    profit_usdt: asNumber(row.abs_profit, asNumber(row.profit_abs, asNumber(row.profit_usdt))),
    trades: asNumber(row.trade_count, asNumber(row.trades, asNumber(row.count))),
  };
}

/**
 * The resolved configuration block of a profile.
 *
 * The API publishes no `stake_amount` (the platform sizes every trade from the
 * worker configuration), so it stays `0` and the card renders a zero the
 * operator can compare against `initial_capital`.
 */
export function toProfileConfigView(raw: unknown, profile: ProfileView): ProfileConfigView {
  const row = asRecord(raw);
  const pairs = asStringArray(row.pairs);
  return {
    strategy: asString(row.strategy, profile.strategy),
    timeframe: asString(row.timeframe, profile.timeframe),
    pairs: pairs.length > 0 ? pairs : profile.pairs,
    mode: asMode(row.mode === undefined ? profile.mode : row.mode),
    initial_capital: asNumber(row.initial_capital, profile.initial_capital),
    max_open_trades: asNumber(row.max_open_trades, profile.max_open_trades ?? 0),
    stake_amount: 0,
    dry_run: asMode(row.mode === undefined ? profile.mode : row.mode) === "paper",
    startable: profile.state !== "blocked",
    exchange: typeof row.exchange === "string" ? row.exchange : profile.exchange,
    priority: typeof row.priority === "number" ? row.priority : profile.priority,
  };
}

/**
 * The performance block of one profile detail.
 *
 * `/api/profiles/{id}` publishes the profile, its strategy and its equity curve,
 * but no aggregate block: the block is the profile's own figures, which is what
 * the profile page renders. `profiles_total` is 1 by construction.
 */
export function toProfilePerformance(profile: ProfileView): AccountPerformance {
  const running = profile.state === "running" ? 1 : 0;
  return {
    window: "24h",
    portfolio_value: profile.portfolio_value,
    initial_capital: profile.initial_capital,
    profit_usdt: profile.profit_usdt,
    profit_pct: profile.profit_pct,
    realised_profit_usdt: 0,
    unrealised_profit_usdt: 0,
    open_trades: profile.open_trades,
    closed_trades: profile.closed_trades,
    win_rate: profile.win_rate,
    profit_factor: profile.profit_factor,
    max_drawdown_pct: profile.max_drawdown_pct,
    profiles_total: 1,
    profiles_running: running,
    cash: profile.cash,
    positions_value: profile.positions_value,
  };
}

/** `GET /api/profiles/{id}` */
export function toProfileDetail(raw: unknown, window: ApiWindow): ProfileDetail {
  const body = asRecord(raw);
  const profile = toProfileView(body.profile);
  const performance = toProfilePerformance(profile);
  return {
    generated_at: profile.updated_at,
    window,
    profile,
    performance: { ...performance, window },
    equity_curve: toEquityCurve(body.equity_curve, profile.initial_capital),
    daily_profit: asArray(body.daily).map(toDailyBar),
    events: [],
    config: toProfileConfigView(body.profile, profile),
    strategy: body.strategy === undefined ? undefined : toStrategyView(body.strategy),
    // The trade rows are relayed verbatim: they are raw freqtrade payloads that
    // the profile page normalises field by field.
    open_trades: asRecordArray(body.open_trades),
    recent_trades: asRecordArray(body.recent_trades),
  };
}

/** The envelope of `POST`/`PATCH /api/profiles...`, which answers one profile. */
export function toProfileDetailEnvelope(raw: unknown, window: ApiWindow = "24h"): ProfileDetail {
  const body = asRecord(raw);
  const profile = toProfileView(body.profile);
  return {
    generated_at: profile.updated_at,
    window,
    profile,
    performance: { ...toProfilePerformance(profile), window },
    equity_curve: [],
    daily_profit: [],
    events: [],
    config: toProfileConfigView(body.profile, profile),
  };
}

/** `POST /api/profiles/{id}/actions...` -- the refreshed view of the profile. */
export function toProfileActionResponse(raw: unknown): ProfileActionResponse {
  const body = asRecord(raw);
  const profile = toProfileView(body.profile);
  return { profile, message: "", generated_at: profile.updated_at };
}

/** `POST /api/catalogue/apply` */
export function toCatalogueApplyResponse(raw: unknown): CatalogueApplyResponse {
  const body = asRecord(raw);
  return {
    generated_at: "",
    created: asStringArray(body.created),
    updated: asStringArray(body.updated),
    unchanged: asStringArray(body.skipped),
    pruned: asStringArray(body.pruned),
    refused: asStringArray(body.refused_live),
  };
}

/** `POST /api/kill-switch` -- only the flag is published. */
export function toKillSwitchResponse(raw: unknown): KillSwitchResponse {
  const body = asRecord(raw);
  return {
    engaged: asBoolean(body.kill_switch_engaged),
    stopped_profiles: [],
    generated_at: "",
  };
}

/**
 * `GET /api/health`.
 *
 * Every counter is read from the wire name it mirrors (`profiles_running`); a
 * missing one degrades to `0`, so no page can render `NaN` from this mapper.
 *
 * `live_trading_enabled` is not part of the health payload: the live gate is
 * published by `GET /api/settings`, and the operations page merges the two. The
 * health mapper reports the fail-safe default (`false`) so a page that only
 * reads `/api/health` never claims live trading is armed.
 */
export function toHealthStatus(raw: unknown): HealthStatus {
  const body = asRecord(raw);
  return {
    status: asString(body.status, "unknown"),
    version: asString(body.version),
    uptime_seconds: asNumber(body.uptime_seconds),
    profiles_running: asNumber(body.profiles_running),
    live_trading_enabled: false,
    kill_switch_engaged: asBoolean(body.kill_switch_engaged),
    generated_at: asString(body.generated_at),
  };
}
