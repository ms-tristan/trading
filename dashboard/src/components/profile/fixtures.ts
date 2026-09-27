/**
 * API-shaped fixtures of the dashboard pages of this package.
 *
 * Two families live here. The view builders (`profile`, `health`, `detail`, ...)
 * produce the mapped view model the components render. The `api*` builders below
 * produce **the JSON the monitoring API really serves**, wire field names
 * included (`profit_abs`, `sparkline: [{t, value}]`, `slot`, `worker_port`,
 * `daily.abs_profit`, `daily.trade_count`, `engine_slots_used`): the page tests
 * mock `fetch` with them, so a page is pinned against the payload the Python API
 * answers and not against the model it is mapped onto.
 *
 * Colocated tests import them so that a page or a component can be pinned without
 * a network call, and no test has to spell out a full payload again.
 */

import type {
  AccountPerformance,
  DailyBarPoint,
  EquityPoint,
  EventItem,
  HealthStatus,
  ProfileConfigView,
  ProfileDetail,
  ProfileView,
  StrategyView,
} from "@/lib/types";

/** One profile, ranked fields included. */
export function profile(
  overrides: Partial<ProfileView> & Record<string, unknown> & { id: string },
): ProfileView {
  return {
    name: overrides.id,
    strategy: "basic",
    strategy_title: "EMA cross baseline",
    timeframe: "5m",
    pairs: ["BTC/USDT"],
    mode: "paper",
    state: "running",
    state_reason: null,
    portfolio_value: 1000,
    initial_capital: 1000,
    profit_usdt: 0,
    profit_pct: 0,
    open_trades: 0,
    closed_trades: 0,
    win_rate: 0,
    profit_factor: 0,
    max_drawdown_pct: 0,
    engine_slot: null,
    api_port: null,
    sparkline: [],
    updated_at: "2026-09-27T12:00:00Z",
    ...overrides,
  };
}

/** One performance block of a profile or of the whole account. */
export function performance(
  overrides: Partial<AccountPerformance> & Record<string, unknown> = {},
): AccountPerformance {
  return {
    window: "24h",
    portfolio_value: 1040,
    initial_capital: 1000,
    profit_usdt: 40,
    profit_pct: 0.04,
    realised_profit_usdt: 25,
    unrealised_profit_usdt: 15,
    open_trades: 1,
    closed_trades: 6,
    win_rate: 0.5,
    profit_factor: 1.4,
    max_drawdown_pct: 0.03,
    profiles_total: 3,
    profiles_running: 2,
    engine_slots_used: 2,
    engine_slots_total: 4,
    ...overrides,
  };
}

/** The resolved configuration of one profile. */
export function config(
  overrides: Partial<ProfileConfigView> & Record<string, unknown> = {},
): ProfileConfigView {
  return {
    strategy: "basic",
    timeframe: "5m",
    pairs: ["BTC/USDT"],
    mode: "paper",
    initial_capital: 1000,
    max_open_trades: 2,
    stake_amount: 100,
    dry_run: true,
    startable: true,
    ...overrides,
  };
}

/** One equity point. */
export function equityPoint(overrides: Partial<EquityPoint> & { timestamp: string }): EquityPoint {
  return {
    portfolio_value: 1000,
    profit_usdt: 0,
    profit_pct: 0,
    ...overrides,
  };
}

/** One daily profit bar. */
export function dailyBar(overrides: Partial<DailyBarPoint> & { date: string }): DailyBarPoint {
  return {
    profit_usdt: 0,
    trades: 0,
    ...overrides,
  };
}

/**
 * One profile detail.
 *
 * The extra keys are the wire fields the contract of this package renders but
 * `@/lib/types` does not declare: the freqtrade trade rows, the embedded
 * strategy card and the catalogue fields of the configuration.
 */
export function detail(overrides: Partial<ProfileDetail> & Record<string, unknown> = {}): ProfileDetail {
  const base: Record<string, unknown> = {
    generated_at: "2026-09-27T12:00:00Z",
    window: "24h",
    profile: profile({ id: "alpha", name: "Alpha" }),
    performance: performance(),
    equity_curve: [
      equityPoint({ timestamp: "2026-09-26T12:00:00Z", portfolio_value: 1000 }),
      equityPoint({ timestamp: "2026-09-27T12:00:00Z", portfolio_value: 1040 }),
    ],
    daily_profit: [dailyBar({ date: "2026-09-26", profit_usdt: 15, trades: 2 })],
    events: [],
    config: config(),
  };
  return { ...base, ...overrides } as unknown as ProfileDetail;
}

/** One raw freqtrade trade row, as the API relays it. */
export function tradeRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    trade_id: 12,
    pair: "BTC/USDT",
    is_open: true,
    open_date: "2026-09-27T09:00:00Z",
    open_rate: 60000,
    current_rate: 60600,
    close_rate: null,
    amount: 0.0166,
    stake_amount: 1000,
    profit_abs: 10,
    profit_pct: 1,
    exit_reason: null,
    enter_tag: "ema_cross",
    ...overrides,
  };
}

/**
 * One engine health answer, as the view model of `GET /api/health`.
 *
 * The counters mirror the wire names: a caller that wants a different slot pair
 * overrides `engine_slots_used`/`engine_slots_total`, exactly like the payload.
 */
export function health(
  overrides: Partial<HealthStatus> & Record<string, unknown> = {},
): HealthStatus {
  return {
    status: "ok",
    version: "2026.8",
    uptime_seconds: 3670,
    profiles_running: 2,
    engine_slots_used: 2,
    engine_slots_total: 4,
    live_trading_enabled: false,
    kill_switch_engaged: false,
    generated_at: "2026-09-27T12:00:00Z",
    ...overrides,
  };
}

/** One engine event. */
export function eventItem(
  overrides: Partial<EventItem> & Record<string, unknown> & { id: string },
): EventItem {
  return {
    timestamp: "2026-09-27T11:59:00Z",
    level: "info",
    kind: "profile_started",
    profile_id: null,
    message: "Alpha started.",
    ...overrides,
  };
}

/** One entry of the strategy catalogue. */
export function strategyView(
  overrides: Partial<StrategyView> & Record<string, unknown> & { id: string },
): StrategyView {
  return {
    title: overrides.id,
    timeframe: "5m",
    pairs: ["BTC/USDT"],
    description: "A strategy of the catalogue.",
    profiles_total: 2,
    profiles_running: 1,
    portfolio_value: 2000,
    profit_usdt: 80,
    profit_pct: 0.04,
    open_trades: 1,
    closed_trades: 10,
    win_rate: 0.5,
    profit_factor: 1.4,
    sparkline: [1000, 1040],
    ...overrides,
  };
}

/** Minimal `Response` double of the mocked `fetch` of the page tests. */
export function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

// ---------------------------------------------------------------------------
// Wire builders - the payloads the Python API really serves
// ---------------------------------------------------------------------------
//
// The page tests mock `fetch`, so their bodies must be the wire JSON of the
// platform API and not the view model above: `@/lib/api-wire` maps the former
// onto the latter, and a page test that fed it a view model would assert against
// a payload the server never sends. Each builder accepts the same overrides as
// its view-model twin, so a test keeps speaking in `profit_usdt` terms while the
// body carries `profit_abs`.

/**
 * Timestamps of a fixture sparkline: one minute apart, oldest first, ending on
 * the fixture "now" of this module (`2026-09-27T12:00:00Z`).
 */
function sparklineTimestamps(count: number): string[] {
  const end = Date.UTC(2026, 8, 27, 12, 0, 0);
  return Array.from({ length: count }, (_, index) =>
    new Date(end - (count - 1 - index) * 60_000).toISOString().replace(/\.\d{3}Z$/, "Z"),
  );
}

/** One row of `GET /api/profiles`. */
export function apiProfile(
  overrides: Partial<ProfileView> & Record<string, unknown> & { id: string },
): Record<string, unknown> {
  const view = profile(overrides);
  const row: Record<string, unknown> = {
    id: view.id,
    name: view.name,
    strategy: view.strategy,
    strategy_title: view.strategy_title,
    timeframe: view.timeframe,
    mode: view.mode,
    state: view.state,
    state_reason: view.state_reason,
    exchange: view.exchange ?? "binance",
    pairs: view.pairs,
    initial_capital: view.initial_capital,
    portfolio_value: view.portfolio_value,
    cash: view.cash ?? view.portfolio_value,
    positions_value: view.positions_value ?? 0,
    profit_abs: view.profit_usdt,
    profit_pct: view.profit_pct,
    // The view model of one profile does not split realised and unrealised
    // profit; a test that needs the split passes the wire keys directly.
    realized_profit_abs: overrides.realized_profit_abs ?? 0,
    unrealized_profit_abs: overrides.unrealized_profit_abs ?? 0,
    open_trades: view.open_trades,
    closed_trades: view.closed_trades,
    win_rate: view.win_rate,
    profit_factor: view.profit_factor,
    max_drawdown_pct: view.max_drawdown_pct,
    max_open_trades: view.max_open_trades ?? 2,
    priority: view.priority ?? 100,
    best_pair: view.best_pair ?? null,
    last_updated: view.updated_at,
  };
  // The three fields the API publishes about the worker of a profile are only
  // sent when the caller declares them: a fixture that declares no series and no
  // slot reproduces a stopped profile, whose payload carries none of them.
  if (overrides.sparkline !== undefined) {
    const timestamps = sparklineTimestamps(view.sparkline.length);
    const first = view.sparkline[0];
    row.sparkline = view.sparkline.map((value, index) => ({
      t: timestamps[index],
      value,
      profit_pct: first !== undefined && first !== 0 ? (value - first) / first : null,
    }));
  }
  if (overrides.engine_slot !== undefined) {
    row.slot = view.engine_slot;
  }
  if (overrides.api_port !== undefined) {
    row.worker_port = view.api_port;
  }
  // `rank` and `uptime_seconds` are only published when the caller declares
  // them: the profile page derives the rank from the ranked list and the uptime
  // from `/api/health` when the profile row carries none.
  if (overrides.rank !== undefined) {
    row.rank = view.rank;
  }
  if (overrides.uptime_seconds !== undefined) {
    row.uptime_seconds = view.uptime_seconds;
  }
  return row;
}

/** `GET /api/profiles` */
export interface ApiProfilesFixture {
  generated_at: string;
  total: number;
  profiles: Record<string, unknown>[];
}

export function apiProfiles(
  rows: Record<string, unknown>[],
  generatedAt = "2026-09-27T12:00:00Z",
): ApiProfilesFixture {
  return { generated_at: generatedAt, total: rows.length, profiles: rows };
}

/** `GET /api/health` */
export function apiHealth(overrides: ApiHealthOverrides = {}): Record<string, unknown> {
  const profilesRunning = overrides.profiles_running ?? 2;
  return {
    status: overrides.status ?? "ok",
    version: overrides.version ?? "2026.8",
    uptime_seconds: overrides.uptime_seconds ?? 3670,
    profiles_total: overrides.profiles_total ?? 3,
    profiles_running: profilesRunning,
    profiles_healthy: overrides.profiles_healthy ?? profilesRunning,
    profiles_paper: overrides.profiles_paper ?? profilesRunning,
    profiles_running_paper: overrides.profiles_running_paper ?? profilesRunning,
    profiles_live: overrides.profiles_live ?? 0,
    profiles_running_live: overrides.profiles_running_live ?? 0,
    profiles_queued: overrides.profiles_queued ?? 0,
    engine_slots_used: overrides.engine_slots_used ?? profilesRunning,
    engine_slots_total: overrides.engine_slots_total ?? 4,
    kill_switch_engaged: overrides.kill_switch_engaged ?? false,
    generated_at: overrides.generated_at ?? "2026-09-27T12:00:00Z",
  };
}

/**
 * Overrides of {@link apiHealth}.
 *
 * They are the real keys of `GET /api/health`: a test that renames one back to
 * the view model (`running_profiles`, `max_running_profiles`) does not compile,
 * which is what keeps the doubles of the page tests on the wire.
 */
export interface ApiHealthOverrides {
  status?: string;
  version?: string;
  uptime_seconds?: number;
  profiles_total?: number;
  profiles_running?: number;
  profiles_healthy?: number;
  profiles_paper?: number;
  profiles_running_paper?: number;
  profiles_live?: number;
  profiles_running_live?: number;
  profiles_queued?: number;
  engine_slots_used?: number;
  engine_slots_total?: number;
  kill_switch_engaged?: boolean;
  generated_at?: string;
}

/** `GET /api/settings` */
export function apiSettings(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    max_running_profiles: overrides.max_running_profiles ?? 4,
    snapshot_interval_seconds: overrides.snapshot_interval_seconds ?? 60,
    kill_switch_engaged: overrides.kill_switch_engaged ?? false,
    allow_live_trading: overrides.allow_live_trading ?? false,
    catalogue_profile_count: overrides.catalogue_profile_count ?? 3,
    operator_profile_count: overrides.operator_profile_count ?? 0,
    state_db_path: overrides.state_db_path ?? "data/realtime/state.db",
    version: overrides.version ?? "2026.8",
  };
}

/** One row of `GET /api/events`. */
export function apiEvent(
  overrides: Partial<EventItem> & Record<string, unknown> & { id: string },
): Record<string, unknown> {
  const view = eventItem(overrides);
  return {
    id: Number(view.id) || 1,
    ts: view.timestamp,
    profile_id: view.profile_id,
    level: view.level,
    kind: view.kind,
    message: view.message,
  };
}

/** `GET /api/events` */
export function apiEvents(
  rows: Record<string, unknown>[],
  generatedAt = "2026-09-27T12:00:00Z",
): Record<string, unknown> {
  return { generated_at: generatedAt, events: rows };
}

/**
 * One entry of `GET /api/strategies`.
 *
 * Only the aggregate keys the caller declares reach the wire, so a test can
 * reproduce a catalogue row whose aggregate the API did not compute - the case
 * the strategies page fills from the ranked profile list.
 */
export function apiStrategy(
  overrides: Partial<StrategyView> & Record<string, unknown> & { id: string },
): Record<string, unknown> {
  const view = strategyView(overrides);
  const row: Record<string, unknown> = {
    id: view.id,
    class_name: view.class_name ?? `${view.id}Strategy`,
    title: view.title,
    category: view.category ?? "trend",
    summary: view.summary ?? "A strategy of the catalogue.",
    description: view.description,
    indicators: view.indicators ?? ["EMA(20)", "EMA(50)"],
    timeframes: view.timeframes ?? [view.timeframe],
    reference: view.reference ?? "Freqtrade strategy customization guide",
    risk_notes: view.risk_notes ?? "Whipsaws in ranging markets.",
  };
  if (overrides.profiles_total !== undefined) {
    row.profile_count = view.profiles_total;
  }
  if (overrides.profiles_running !== undefined) {
    row.profiles_running = view.profiles_running;
  }
  if (overrides.portfolio_value !== undefined) {
    row.portfolio_value = view.portfolio_value;
  }
  if (overrides.profit_usdt !== undefined) {
    row.profit_abs = view.profit_usdt;
  }
  if (overrides.profit_pct !== undefined) {
    row.profit_pct = view.profit_pct;
  }
  if (overrides.win_rate !== undefined) {
    row.win_rate = view.win_rate;
  }
  if (overrides.best_profile_id !== undefined) {
    row.best_profile_id = view.best_profile_id;
  }
  return row;
}

/**
 * `GET /api/profiles/{id}`.
 *
 * The daily series carries the real state-database keys (`abs_profit`,
 * `rel_profit`, `starting_balance`, `trade_count`); the trade rows are the
 * freqtrade payloads the caller passes, relayed unchanged.
 */
export function apiDetail(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const view = detail(overrides as Partial<ProfileDetail>);
  const raw = overrides;
  const startingBalance = view.profile.initial_capital;
  return {
    profile: apiProfile(view.profile as Partial<ProfileView> & { id: string }),
    strategy:
      raw.strategy === undefined
        ? undefined
        : apiStrategy(raw.strategy as Partial<StrategyView> & { id: string }),
    equity_curve: view.equity_curve.map((point) => ({
      t: point.timestamp,
      value: point.portfolio_value,
      profit_pct: point.profit_pct,
    })),
    daily: view.daily_profit.map((bar) => ({
      date: bar.date,
      abs_profit: bar.profit_usdt,
      rel_profit: startingBalance !== 0 ? bar.profit_usdt / startingBalance : 0,
      starting_balance: startingBalance,
      trade_count: bar.trades,
    })),
    open_trades: raw.open_trades ?? [],
    recent_trades: raw.recent_trades ?? [],
  };
}
