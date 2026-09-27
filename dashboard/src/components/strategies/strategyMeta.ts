/**
 * View model of the strategy catalogue.
 *
 * `@/lib/types` pins the aggregated figures of a `StrategyView`: how many
 * profiles hold the strategy, what they are worth and what they earned. The
 * catalogue metadata an operator reads next to those figures - the category, the
 * description, the indicators, the traded timeframes and the reference of the
 * strategy - comes from `config/strategies.json` through `GET /api/strategies`
 * and `GET /api/profiles/{id}`, but is not declared by that module yet. It is
 * therefore read defensively here, from the payload itself: a field the API does
 * not publish is simply not rendered.
 */

import type { ProfileDetail, ProfileView } from "@/lib/types";

import {
  readNumber,
  readNumberArray,
  readRecord,
  readStringArray,
  readText,
} from "@/components/profile/wire";

/** Everything a strategy card renders, normalised. */
export interface StrategyCardView {
  /** Catalogue id, e.g. `momentum`. */
  id: string;
  title: string;
  category: string | null;
  description: string | null;
  summary: string | null;
  riskNotes: string | null;
  indicators: string[];
  timeframes: string[];
  pairs: string[];
  reference: string | null;
  /** Unknown numbers are `NaN`: `@/lib/format` renders those as an em dash. */
  profilesTotal: number;
  profilesRunning: number;
  portfolioValue: number;
  profitUsdt: number;
  /** 0..1 ratio. */
  profitPct: number;
  openTrades: number;
  closedTrades: number;
  /** 0..1 ratio. */
  winRate: number;
  profitFactor: number;
  sparkline: number[];
}

/**
 * Read one strategy entry (`GET /api/strategies`, or the embedded `strategy` of
 * a profile detail) into the card view model.
 *
 * @returns `null` when the source is not an object at all.
 */
export function strategyCardView(source: unknown): StrategyCardView | null {
  const raw = readRecord(source);
  if (raw === null) {
    return null;
  }

  const timeframes = readStringArray(raw.timeframes);
  const singleTimeframe = readText(raw.timeframe);

  return {
    id: readText(raw.id) ?? readText(raw.class_name) ?? "",
    title: readText(raw.title) ?? readText(raw.class_name) ?? "",
    category: readText(raw.category),
    description: readText(raw.description),
    summary: readText(raw.summary),
    riskNotes: readText(raw.risk_notes),
    indicators: readStringArray(raw.indicators),
    timeframes: timeframes.length > 0 ? timeframes : singleTimeframe === null ? [] : [singleTimeframe],
    pairs: readStringArray(raw.pairs),
    reference: readText(raw.reference),
    profilesTotal: readNumber(raw.profiles_total) ?? Number.NaN,
    profilesRunning: readNumber(raw.profiles_running) ?? Number.NaN,
    portfolioValue: readNumber(raw.portfolio_value) ?? Number.NaN,
    profitUsdt: readNumber(raw.profit_usdt) ?? Number.NaN,
    profitPct: readNumber(raw.profit_pct) ?? Number.NaN,
    openTrades: readNumber(raw.open_trades) ?? Number.NaN,
    closedTrades: readNumber(raw.closed_trades) ?? Number.NaN,
    winRate: readNumber(raw.win_rate) ?? Number.NaN,
    profitFactor: readNumber(raw.profit_factor) ?? Number.NaN,
    sparkline: readNumberArray(raw.sparkline),
  };
}

/** Read the strategy block embedded in a profile detail, when it carries one. */
export function strategyFromDetail(detail: ProfileDetail): StrategyCardView | null {
  const raw = readRecord(detail);
  return strategyCardView(raw?.strategy);
}

/**
 * Fill the profile count of a strategy the API did not aggregate.
 *
 * The ranked profile list is the fallback, but only when it actually carries
 * rows: an empty list means "the API answered nothing", and counting it would
 * claim that the strategy has no profile at all.
 */
export function withDerivedProfileCount(
  view: StrategyCardView,
  profiles: ProfileView[],
): StrategyCardView {
  if (Number.isFinite(view.profilesTotal) || profiles.length === 0) {
    return view;
  }
  return { ...view, profilesTotal: countProfilesFor(view.id, profiles) };
}

/** Strip everything but letters and digits, so ids and class names compare. */
function normaliseIdentifier(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

/**
 * `true` when a profile holds `strategyId`.
 *
 * The API may publish the catalogue id (`momentum`, the value the cards link
 * with) or the freqtrade class name (`MomentumStrategy`) as
 * `ProfileView.strategy`; the suffix rule binds the two spellings so a filtered
 * page never silently loses its profiles. An empty side never matches.
 */
export function profileMatchesStrategy(strategyId: string, profileStrategy: string): boolean {
  const wanted = normaliseIdentifier(strategyId);
  const held = normaliseIdentifier(profileStrategy);
  if (wanted === "" || held === "") {
    return false;
  }
  return wanted === held || held === `${wanted}strategy` || wanted === `${held}strategy`;
}

/**
 * Best profile of a strategy: the first match of the ranked list.
 *
 * `GET /api/profiles` ranks by portfolio value, descending, so the first match
 * of that order is the best profile of the strategy.
 */
export function bestProfileFor(strategyId: string, profiles: ProfileView[]): ProfileView | null {
  return profiles.find((profile) => profileMatchesStrategy(strategyId, profile.strategy)) ?? null;
}

/** Number of profiles of the ranked list that hold a strategy. */
export function countProfilesFor(strategyId: string, profiles: ProfileView[]): number {
  return profiles.filter((profile) => profileMatchesStrategy(strategyId, profile.strategy)).length;
}

/** Split a catalogue reference into its label and its optional source URL. */
export function splitReference(reference: string): { label: string; url: string | null } {
  const text = reference.trim();
  const index = text.search(/https?:\/\//i);
  if (index === -1) {
    return { label: text, url: null };
  }

  const url = text.slice(index).trim();
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return { label: text, url: null };
    }
  } catch {
    // A reference that is not a usable URL stays plain text.
    return { label: text, url: null };
  }

  const label = text
    .slice(0, index)
    .replace(/[\s\-–—]+$/, "")
    .trim();
  return { label: label === "" ? url : label, url };
}
