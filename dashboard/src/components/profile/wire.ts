/**
 * Defensive readers of the profile payload.
 *
 * `@/lib/types` pins the contract of `GET /api/profiles/{id}`: the profile, its
 * performance, its equity curve, its daily profit, its events and its resolved
 * configuration. Three groups of fields the pages of this package render are
 * part of the API answer but are not declared by that module:
 *
 * * the freqtrade trade rows (`open_trades`, `recent_trades`);
 * * the embedded strategy card (`strategy`);
 * * the catalogue and capital fields of the payload (`exchange`, `priority`,
 *   `cash`, `positions_value`, `rank`, `uptime_seconds`).
 *
 * They are read here, field by field, from the payload itself: a missing or
 * malformed value becomes `null` and the page renders an em dash, so a partial
 * API answer never throws and never puts `NaN` on the screen.
 *
 * Number conventions of a trade row: `profit_abs` is expressed in USDT,
 * `profit_pct` is **already a percentage** (freqtrade multiplies the ratio by
 * 100 in its REST payload) and `profit_ratio` is a 0..1 ratio. Both percentage
 * fields are normalised into `profitPercent` (percentage points), which the
 * tables render with `formatSignedPercent`.
 */

import { EMPTY_ACCOUNT } from "@/lib/types";
import type {
  DailyBarPoint,
  ProfileConfigView,
  ProfileDetail,
  ProfileView,
} from "@/lib/types";

/** Read one payload object without trusting its declared type. */
export function readRecord(source: unknown): Record<string, unknown> | null {
  return typeof source === "object" && source !== null && !Array.isArray(source)
    ? (source as Record<string, unknown>)
    : null;
}

/** Non-empty trimmed string, or `null`. Numbers are accepted as text. */
export function readText(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed === "" ? null : trimmed;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  return null;
}

/** Finite number, or `null`: `null`, a blank string and `NaN` never leak through. */
export function readNumber(value: unknown): number | null {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/** Array of non-empty strings; a non-array or a malformed entry is dropped. */
export function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const entries: string[] = [];
  for (const entry of value) {
    const text = readText(entry);
    if (text !== null) {
      entries.push(text);
    }
  }
  return entries;
}

/** Array of finite numbers; a non-array or a malformed entry is dropped. */
export function readNumberArray(value: unknown): number[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const entries: number[] = [];
  for (const entry of value) {
    const parsed = readNumber(entry);
    if (parsed !== null) {
      entries.push(parsed);
    }
  }
  return entries;
}

/** One freqtrade trade row, normalised for the two trade tables. */
export interface TradeRow {
  /** `trade_id` as text; an empty string when the row carries none. */
  tradeId: string;
  pair: string;
  openedAt: string | null;
  closedAt: string | null;
  openRate: number | null;
  currentRate: number | null;
  closeRate: number | null;
  amount: number | null;
  stakeAmount: number | null;
  profitAbs: number | null;
  /** Percentage points, whatever basis the payload published. */
  profitPercent: number | null;
  exitReason: string | null;
  enterTag: string | null;
  isOpen: boolean;
}

/**
 * Read one trade row.
 *
 * @param defaultOpen `is_open` of a row that does not carry the flag: `true` for
 *   the open-trade list, `false` for the recent closed-trade list.
 */
export function readTrade(source: unknown, defaultOpen: boolean): TradeRow | null {
  const raw = readRecord(source);
  if (raw === null) {
    return null;
  }

  const profitPct = readNumber(raw.profit_pct);
  const profitRatio = readNumber(raw.profit_ratio);
  const profitAbs = readNumber(raw.profit_abs);
  const stakeAmount = readNumber(raw.stake_amount);

  let profitPercent: number | null = null;
  if (profitPct !== null) {
    profitPercent = profitPct;
  } else if (profitRatio !== null) {
    profitPercent = profitRatio * 100;
  } else if (profitAbs !== null && stakeAmount !== null && stakeAmount !== 0) {
    // Last resort of a row that publishes neither percentage: derive it from the
    // absolute profit and the stake it was made on.
    profitPercent = (profitAbs / stakeAmount) * 100;
  }

  const isOpen = raw.is_open;

  return {
    tradeId: readText(raw.trade_id) ?? "",
    pair: readText(raw.pair) ?? "",
    openedAt: readText(raw.open_date) ?? readText(raw.open_timestamp),
    closedAt: readText(raw.close_date) ?? readText(raw.close_timestamp),
    openRate: readNumber(raw.open_rate),
    currentRate: readNumber(raw.current_rate),
    closeRate: readNumber(raw.close_rate),
    amount: readNumber(raw.amount),
    stakeAmount,
    profitAbs,
    profitPercent,
    exitReason: readText(raw.exit_reason),
    enterTag: readText(raw.enter_tag),
    isOpen: typeof isOpen === "boolean" ? isOpen : defaultOpen,
  };
}

/** Read a list of trade rows; a non-array answers an empty list. */
export function readTradeList(source: unknown, defaultOpen: boolean): TradeRow[] {
  if (!Array.isArray(source)) {
    return [];
  }
  const rows: TradeRow[] = [];
  for (const entry of source) {
    const row = readTrade(entry, defaultOpen);
    if (row !== null) {
      rows.push(row);
    }
  }
  return rows;
}

/** Read one daily bar; a row without a date is dropped. */
function readDailyBar(source: unknown): DailyBarPoint | null {
  const raw = readRecord(source);
  if (raw === null) {
    return null;
  }
  const date = readText(raw.date) ?? readText(raw.day);
  if (date === null) {
    return null;
  }
  return {
    date,
    profit_usdt: readNumber(raw.profit_usdt) ?? Number.NaN,
    trades: readNumber(raw.trades) ?? 0,
  };
}

/** Read the daily profit series (`daily_profit`, or its `daily` alias). */
export function readDailyBars(source: unknown): DailyBarPoint[] {
  if (!Array.isArray(source)) {
    return [];
  }
  const bars: DailyBarPoint[] = [];
  for (const entry of source) {
    const bar = readDailyBar(entry);
    if (bar !== null) {
      bars.push(bar);
    }
  }
  return bars;
}

/** The part of the profile payload that `@/lib/types` does not declare. */
export interface ProfileExtras {
  dailyBars: DailyBarPoint[];
  openTrades: TradeRow[];
  recentTrades: TradeRow[];
  /** Raw embedded strategy block, or `null` when the payload carries none. */
  strategy: Record<string, unknown> | null;
  exchange: string | null;
  priority: number | null;
  cash: number | null;
  positionsValue: number | null;
  rank: number | null;
  uptimeSeconds: number | null;
}

/**
 * Read every undeclared field of a profile detail in one pass.
 *
 * `daily_profit` is the declared daily series and `daily` its accepted alias;
 * `recent_trades` is the declared closed-trade list and `closed_trades` its
 * alias. The profile-level `open_trades` of the payload is a **list**, whereas
 * `performance.open_trades` is a **count**: the two never collide here.
 */
export function readProfileExtras(detail: ProfileDetail): ProfileExtras {
  const payload = readRecord(detail) ?? {};
  const performance = readRecord(payload.performance) ?? {};
  const profile = readRecord(payload.profile) ?? {};
  const config = readRecord(payload.config) ?? {};

  return {
    dailyBars: readDailyBars(payload.daily_profit ?? payload.daily),
    openTrades: readTradeList(payload.open_trades, true),
    recentTrades: readTradeList(payload.recent_trades ?? payload.closed_trades, false),
    strategy: readRecord(payload.strategy),
    exchange: readText(config.exchange),
    priority: readNumber(config.priority),
    cash: readNumber(performance.cash),
    positionsValue: readNumber(performance.positions_value),
    rank: readNumber(profile.rank),
    uptimeSeconds:
      readNumber(profile.uptime_seconds) ?? readNumber(performance.uptime_seconds),
  };
}

/** Cash and open-position value of one profile. */
export interface ProfileCapital {
  cash: number | null;
  positionsValue: number | null;
}

/**
 * Resolve the cash and the open-position value of a profile.
 *
 * The payload wins when it publishes them; otherwise the open trade rows are
 * summed and the cash is derived (`portfolio value - open positions`). When the
 * performance says a profile holds open trades but publishes no rows to sum from,
 * both stay unknown and the KPI renders an em dash instead of a made-up zero.
 */
export function resolveCapital(detail: ProfileDetail, extras: ProfileExtras): ProfileCapital {
  const payload = readRecord(detail) ?? {};
  const performance = readRecord(payload.performance) ?? {};
  const openTradeCount = readNumber(performance.open_trades) ?? 0;
  const portfolioValue = readNumber(performance.portfolio_value);

  let positionsValue = extras.positionsValue;
  if (positionsValue === null && extras.openTrades.length > 0) {
    positionsValue = extras.openTrades.reduce(
      (total, trade) => total + (trade.stakeAmount ?? 0),
      0,
    );
  }
  if (positionsValue === null && openTradeCount === 0) {
    positionsValue = 0;
  }

  const cash =
    extras.cash ??
    (positionsValue !== null && portfolioValue !== null ? portfolioValue - positionsValue : null);

  return { cash, positionsValue };
}

/** Position of a profile in the ranked list the API returned, 1-based. */
export function profileRank(profiles: ProfileView[], id: string): number | null {
  const index = profiles.findIndex((entry) => entry.id === id);
  return index === -1 ? null : index + 1;
}

/**
 * Rates, amounts and quantities of a trade row.
 *
 * `@/lib/format` covers money, percentages, durations and timestamps; a raw
 * exchange rate or an asset amount is none of those, so it is grouped here
 * without a currency unit. An unknown value renders as an em dash.
 */
export function formatTradeNumber(value: number | null, decimals = 8): string {
  if (value === null || !Number.isFinite(value)) {
    return "\u2014";
  }
  return value.toLocaleString("en-US", { maximumFractionDigits: decimals });
}

/** Empty but API-shaped profile view. */
export const EMPTY_PROFILE_VIEW: ProfileView = {
  id: "",
  name: "",
  strategy: "",
  strategy_title: "",
  timeframe: "",
  pairs: [],
  mode: "paper",
  state: "stopped",
  state_reason: null,
  portfolio_value: 0,
  initial_capital: 0,
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
  updated_at: "",
};

/** Empty but API-shaped resolved configuration. */
export const EMPTY_PROFILE_CONFIG: ProfileConfigView = {
  strategy: "",
  timeframe: "",
  pairs: [],
  mode: "paper",
  initial_capital: 0,
  max_open_trades: 0,
  stake_amount: 0,
  dry_run: true,
  startable: false,
};

/**
 * Fallback of `GET /api/profiles/{id}` handed to `fetchWithFallback`.
 *
 * Its `window` is the documented default; the pages always render the window the
 * URL asks for, so a cold failure never changes the selected window.
 */
export const EMPTY_PROFILE_DETAIL: ProfileDetail = {
  generated_at: "",
  window: "24h",
  profile: EMPTY_PROFILE_VIEW,
  performance: EMPTY_ACCOUNT.performance,
  equity_curve: [],
  daily_profit: [],
  events: [],
  config: EMPTY_PROFILE_CONFIG,
};
