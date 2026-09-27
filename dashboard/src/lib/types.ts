/**
 * View model of the dashboard.
 *
 * These are the shapes the components render. They are produced by `@/lib/api`
 * from the JSON the platform API serves: `@/lib/api-wire` transcribes the wire
 * payloads (`snake_case`, documented in `docs/architecture.md` section 9) and
 * maps them field by field, so a rename on the Python side is a change to that
 * one module and never a change to a component.
 *
 * Ratio convention
 * ----------------
 * Every field whose name ends in `_pct` (`profit_pct`, `max_drawdown_pct`) and
 * `win_rate` carry a **0..1 ratio**, not an already-multiplied percentage: the
 * documented aggregation is
 * `profit_pct = (portfolio_value - initial_capital) / initial_capital`.
 * Render them with `formatRatioPercent` from `@/lib/format`.
 */

/** Lifecycle state of one profile, as published by the engine supervisor. */
export type ProfileState = "running" | "queued" | "stopped" | "error" | "blocked";

/** Whether a profile trades on a simulated (paper) or a funded (live) venue. */
export type ProfileMode = "paper" | "live";

/** Aggregation window accepted by the account and profile endpoints. */
export type ApiWindow = "24h" | "7d" | "30d" | "all";

/** Ranking keys shared by the tables and by `sortProfiles`. */
export type SortKey = "value" | "profit" | "name" | "strategy";

/** Action accepted by `POST /api/profiles/{id}/actions/{action}`. */
export type ProfileAction = "start" | "stop" | "restart";

/** `GET /api/health` */
export interface HealthStatus {
  status: string;
  version: string;
  uptime_seconds: number;
  running_profiles: number;
  max_running_profiles: number;
  live_trading_enabled: boolean;
  kill_switch_engaged: boolean;
  generated_at: string;
}

/**
 * One profile as `GET /api/profiles` publishes it.
 *
 * The endpoint returns the profiles **already ranked by portfolio value,
 * descending**; the dashboard renders that order and never re-sorts it on
 * arrival (only an explicit operator click on a sortable header does).
 */
export interface ProfileView {
  id: string;
  name: string;
  /** Strategy identifier, e.g. `MomentumStrategy`. */
  strategy: string;
  /** Human title of the strategy, e.g. `Momentum breakout`. */
  strategy_title: string;
  timeframe: string;
  /** Traded pairs (`pair_whitelist`); the first entry is the primary pair. */
  pairs: string[];
  mode: ProfileMode;
  state: ProfileState;
  /** Why the profile is in this state; `null` when there is nothing to report. */
  state_reason: string | null;
  portfolio_value: number;
  initial_capital: number;
  profit_usdt: number;
  /** 0..1 ratio (see the module docstring). */
  profit_pct: number;
  open_trades: number;
  closed_trades: number;
  /** 0..1 ratio. */
  win_rate: number;
  profit_factor: number;
  /** 0..1 ratio. */
  max_drawdown_pct: number;
  /** Engine slot (1..fleet cap) when the profile holds one, else `null`. */
  engine_slot: number | null;
  /** Loopback port of the profile's freqtrade REST API, when it holds one. */
  api_port: number | null;
  /** Tiny portfolio-value series, oldest first. */
  sparkline: number[];
  updated_at: string;
  /** Position of the profile in the ranking the API returned, 1-based. */
  rank?: number;
  /** Uptime of the worker of this profile, in seconds. */
  uptime_seconds?: number;
  /** Free stake-currency balance of the profile wallet. */
  cash?: number;
  /** Mark-to-market value of the open positions of the profile. */
  positions_value?: number;
  /** Exchange the profile trades on. */
  exchange?: string;
  /** Concurrency allowed for this profile. */
  max_open_trades?: number;
  /** Scheduling rank: higher runs first when the fleet is full. */
  priority?: number;
  /** Catalogue category of the strategy of this profile. */
  strategy_category?: string;
  /** Best pair of the profile, when the worker reported one. */
  best_pair?: string | null;
}

/** `GET /api/profiles` */
export interface ProfilesResponse {
  generated_at: string;
  profiles: ProfileView[];
}

/** One point of an equity curve. */
export interface EquityPoint {
  timestamp: string;
  portfolio_value: number;
  profit_usdt: number;
  /** 0..1 ratio. */
  profit_pct: number;
}

/** Combined account performance of one window. */
export interface AccountPerformance {
  window: ApiWindow;
  portfolio_value: number;
  initial_capital: number;
  profit_usdt: number;
  /** 0..1 ratio. */
  profit_pct: number;
  realised_profit_usdt: number;
  unrealised_profit_usdt: number;
  open_trades: number;
  closed_trades: number;
  /** 0..1 ratio. */
  win_rate: number;
  profit_factor: number;
  /** 0..1 ratio. */
  max_drawdown_pct: number;
  profiles_total: number;
  profiles_running: number;
  engine_slots_used: number;
  engine_slots_total: number;
  /** Free balance of one profile wallet (per-profile block only). */
  cash?: number;
  /** Value of the open positions of one profile (per-profile block only). */
  positions_value?: number;
}

/** `GET /api/account?window=...` */
export interface AccountResponse {
  window: ApiWindow;
  generated_at: string;
  performance: AccountPerformance;
  equity_curve: EquityPoint[];
}

/** One profitable/unprofitable day, used by the daily bars chart. */
export interface DailyBarPoint {
  /** Calendar day, `YYYY-MM-DD` (UTC). */
  date: string;
  profit_usdt: number;
  trades: number;
}

/** One entry of the strategy catalogue, with its aggregated performance. */
export interface StrategyView {
  id: string;
  title: string;
  timeframe: string;
  pairs: string[];
  description: string;
  profiles_total: number;
  profiles_running: number;
  portfolio_value: number;
  profit_usdt: number;
  /** 0..1 ratio. */
  profit_pct: number;
  open_trades: number;
  closed_trades: number;
  /** 0..1 ratio. */
  win_rate: number;
  profit_factor: number;
  sparkline: number[];
  /** Freqtrade class name of the strategy, when the catalogue declares one. */
  class_name?: string;
  /** Catalogue category, e.g. `trend`, `breakout`. */
  category?: string;
  /** One-line summary of the idea. */
  summary?: string;
  /** Indicators the strategy computes. */
  indicators?: string[];
  /** Every timeframe the strategy may run on. */
  timeframes?: string[];
  /** Public source of the idea (paper, book or reference implementation). */
  reference?: string;
  /** What the strategy risks, in the operator's words. */
  risk_notes?: string;
  /** Best profile of the strategy, by portfolio value. */
  best_profile_id?: string;
}

/** `GET /api/strategies` */
export interface StrategiesResponse {
  generated_at: string;
  strategies: StrategyView[];
}

/** Configuration of one profile, as returned inside a profile detail. */
export interface ProfileConfigView {
  strategy: string;
  timeframe: string;
  pairs: string[];
  mode: ProfileMode;
  initial_capital: number;
  max_open_trades: number;
  stake_amount: number;
  dry_run: boolean;
  /** Whether the engine may start this profile at all. */
  startable: boolean;
  /** Exchange the profile trades on. */
  exchange?: string;
  /** Scheduling rank of the profile. */
  priority?: number;
}

/** `GET /api/profiles/{id}?window=...` */
export interface ProfileDetail {
  generated_at: string;
  window: ApiWindow;
  profile: ProfileView;
  performance: AccountPerformance;
  equity_curve: EquityPoint[];
  daily_profit: DailyBarPoint[];
  events: EventItem[];
  config: ProfileConfigView;
  /**
   * Catalogue entry of the strategy of this profile. Partial on purpose: the
   * catalogue metadata is optional, so an entry may declare only some fields.
   */
  strategy?: Partial<StrategyView>;
  /** Raw freqtrade rows of the open trades, as the worker published them. */
  open_trades?: Record<string, unknown>[];
  /** Raw freqtrade rows of the most recent closed trades. */
  recent_trades?: Record<string, unknown>[];
}

/** One structured engine/platform event. */
export interface EventItem {
  id: string;
  timestamp: string;
  level: "info" | "warning" | "error";
  /** Machine kind, e.g. `profile_started`, `live_refused`. */
  kind: string;
  profile_id: string | null;
  message: string;
}

/** `GET /api/events?limit=...` */
export interface EventsResponse {
  generated_at: string;
  events: EventItem[];
}

/**
 * `GET /api/settings`
 *
 * The two fields the dashboard actually reads are required; the remaining
 * engine settings are optional so a partially deployed API can still answer.
 */
export interface DashboardSettings {
  /** Polling interval of the dashboard, in seconds. */
  refresh_interval_seconds: number;
  /** Platform-wide gate: when false, no live profile can start. */
  allow_live_trading: boolean;
  max_running_profiles?: number;
  snapshot_interval_seconds?: number;
  engine_api_port_base?: number;
  kill_switch_engaged?: boolean;
  updated_at?: string;
}

/** Body of `POST /api/profiles`. */
export interface ProfileCreateRequest {
  name: string;
  strategy: string;
  timeframe: string;
  pairs: string[];
  mode: ProfileMode;
  initial_capital: number;
  max_open_trades?: number;
  stake_amount?: number;
}

/** Body of `PATCH /api/profiles/{id}`. */
export type ProfileUpdateRequest = Partial<ProfileCreateRequest>;

/** Body of `POST /api/catalogue/apply`. */
export interface CatalogueApplyRequest {
  /** Delete the profiles the catalogue does not declare. */
  prune?: boolean;
}

/** `POST /api/catalogue/apply` */
export interface CatalogueApplyResponse {
  generated_at: string;
  created: string[];
  updated: string[];
  unchanged: string[];
  pruned: string[];
  refused: string[];
}

/** Body of `POST /api/settings`. */
export interface SettingsUpdateRequest {
  refresh_interval_seconds?: number;
  allow_live_trading?: boolean;
  max_running_profiles?: number;
  snapshot_interval_seconds?: number;
}

/** `POST /api/kill-switch` */
export interface KillSwitchResponse {
  engaged: boolean;
  stopped_profiles: string[];
  generated_at: string;
}

/** `POST /api/profiles/{id}/actions/{action}` */
export interface ProfileActionResponse {
  profile: ProfileView;
  message: string;
  generated_at: string;
}

/** Default polling interval of the dashboard, in seconds. */
export const DEFAULT_REFRESH_INTERVAL_SECONDS = 15;

/**
 * Number of journal rows the operations page reads from `GET /api/events`.
 *
 * It lives here, and not next to `EventsTable`, because that component is a
 * client component: a server page importing a runtime value from a
 * `"use client"` module gets a client reference instead of the value.
 */
export const EVENTS_LIMIT = 50;

/**
 * Fallback payloads handed to `fetchWithFallback`.
 *
 * They are API-shaped on purpose: when the Python API is unreachable the
 * dashboard renders an empty but well-formed page plus an `ErrorBanner` instead
 * of a blank screen.
 */
export const EMPTY_PROFILES: ProfilesResponse = { generated_at: "", profiles: [] };

export const EMPTY_ACCOUNT: AccountResponse = {
  window: "24h",
  generated_at: "",
  performance: {
    window: "24h",
    portfolio_value: 0,
    initial_capital: 0,
    profit_usdt: 0,
    profit_pct: 0,
    realised_profit_usdt: 0,
    unrealised_profit_usdt: 0,
    open_trades: 0,
    closed_trades: 0,
    win_rate: 0,
    profit_factor: 0,
    max_drawdown_pct: 0,
    profiles_total: 0,
    profiles_running: 0,
    engine_slots_used: 0,
    engine_slots_total: 0,
  },
  equity_curve: [],
};

export const DEFAULT_SETTINGS: DashboardSettings = {
  refresh_interval_seconds: DEFAULT_REFRESH_INTERVAL_SECONDS,
  allow_live_trading: false,
};

export const EMPTY_STRATEGIES: StrategiesResponse = { generated_at: "", strategies: [] };

export const EMPTY_EVENTS: EventsResponse = { generated_at: "", events: [] };
