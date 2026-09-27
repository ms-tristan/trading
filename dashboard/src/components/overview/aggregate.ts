/**
 * Aggregation helpers of the overview.
 *
 * The API publishes platform-wide performance; the per-mode aggregate lines of
 * the two overview sections are derived from the profile list it returns (the
 * order of which is never changed here).
 */

import type { KpiDirection } from "@/components/ui/KpiStat";
import {
  formatRatioPercent,
  formatSignedRatioPercent,
  formatSignedUsdt,
  formatUsdt,
} from "@/lib/format";
import { isAttentionState } from "@/lib/states";
import type { ProfileView } from "@/lib/types";

/** Aggregate of every profile of one section. */
export interface ModeSummary {
  profiles_total: number;
  profiles_running: number;
  profiles_attention: number;
  portfolio_value: number;
  initial_capital: number;
  profit_usdt: number;
  /** 0..1 ratio. */
  profit_pct: number;
  open_trades: number;
  closed_trades: number;
  /** 0..1 ratio, weighted by the number of closed trades. */
  win_rate: number;
}

/** One row of a ranked section: the API rank plus the profile itself. */
export interface RankedProfileRow {
  rank: number;
  profile: ProfileView;
}

/** KPI direction of a signed value. */
export function directionOf(value: number): KpiDirection {
  if (!Number.isFinite(value) || value === 0) {
    return "flat";
  }
  return value > 0 ? "up" : "down";
}

/** Profit factor with two decimals; an unbounded factor renders as `∞`. */
export function formatProfitFactor(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "\u2014";
  }
  return Number.isFinite(value) ? value.toFixed(2) : "\u221E";
}

/** Sum the profiles of one section into its aggregate line. */
export function summariseProfiles(profiles: ProfileView[]): ModeSummary {
  const summary: ModeSummary = {
    profiles_total: profiles.length,
    profiles_running: 0,
    profiles_attention: 0,
    portfolio_value: 0,
    initial_capital: 0,
    profit_usdt: 0,
    profit_pct: 0,
    open_trades: 0,
    closed_trades: 0,
    win_rate: 0,
  };

  let weightedWinRate = 0;
  let winRateWeight = 0;

  for (const profile of profiles) {
    summary.portfolio_value += Number.isFinite(profile.portfolio_value)
      ? profile.portfolio_value
      : 0;
    summary.initial_capital += Number.isFinite(profile.initial_capital)
      ? profile.initial_capital
      : 0;
    summary.profit_usdt += Number.isFinite(profile.profit_usdt) ? profile.profit_usdt : 0;
    summary.open_trades += profile.open_trades;
    summary.closed_trades += profile.closed_trades;

    if (profile.state === "running") {
      summary.profiles_running += 1;
    }
    if (isAttentionState(profile.state)) {
      summary.profiles_attention += 1;
    }
    if (profile.closed_trades > 0 && Number.isFinite(profile.win_rate)) {
      weightedWinRate += profile.win_rate * profile.closed_trades;
      winRateWeight += profile.closed_trades;
    }
  }

  summary.profit_pct =
    summary.initial_capital !== 0 ? summary.profit_usdt / summary.initial_capital : 0;
  summary.win_rate = winRateWeight > 0 ? weightedWinRate / winRateWeight : 0;
  return summary;
}

/** The aggregate line rendered under a section header. */
export function formatModeSummary(summary: ModeSummary): string {
  return [
    `${summary.profiles_total} ${summary.profiles_total === 1 ? "profile" : "profiles"}`,
    `${summary.profiles_running} running`,
    summary.portfolio_value > 0 ? formatUsdt(summary.portfolio_value) : null,
    `${formatSignedUsdt(summary.profit_usdt)} (${formatSignedRatioPercent(summary.profit_pct)})`,
    summary.profiles_attention > 0 ? `${summary.profiles_attention} needing attention` : null,
    `${formatRatioPercent(summary.win_rate)} win rate`,
  ]
    .filter((part): part is string => part !== null)
    .join(" - ");
}

/**
 * Attach the ranking position to every profile.
 *
 * The rank is the position in the API order, i.e. by portfolio value descending:
 * it stays the portfolio-value rank even when an operator re-sorts the table.
 */
export function rankProfiles(profiles: ProfileView[]): RankedProfileRow[] {
  return profiles.map((profile, index) => ({ rank: index + 1, profile }));
}
