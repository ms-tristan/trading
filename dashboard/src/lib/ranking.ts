/**
 * Ranking helpers of the overview.
 *
 * The API already returns the profiles ranked by portfolio value, descending.
 * `splitByMode` therefore preserves that order untouched, and `sortProfiles`
 * only runs when an operator explicitly asks for another ranking.
 */

import { needsDecision } from "./states";
import type { ProfileMode, ProfileState, ProfileView, SortKey } from "./types";

export { isAttentionState } from "./states";
export { needsDecision } from "./states";

/** Split the ranked profile list by trading mode, preserving the API order. */
export function splitByMode(profiles: ProfileView[]): {
  paper: ProfileView[];
  live: ProfileView[];
} {
  const paper: ProfileView[] = [];
  const live: ProfileView[] = [];
  for (const profile of profiles) {
    if (profile.mode === "live") {
      live.push(profile);
    } else {
      paper.push(profile);
    }
  }
  return { paper, live };
}

/** Numeric comparator that never returns `NaN` for non-finite inputs. */
function compareNumbers(left: number, right: number): number {
  const a = Number.isFinite(left) ? left : 0;
  const b = Number.isFinite(right) ? right : 0;
  return a - b;
}

/** Sort value of one profile for the requested key. */
function sortValue(profile: ProfileView, key: SortKey): number | string {
  switch (key) {
    case "value":
      return profile.portfolio_value;
    case "profit":
      return profile.profit_usdt;
    case "name":
      return profile.name;
    case "strategy":
      return profile.strategy_title;
  }
}

/**
 * Return a **new** array of `profiles` ordered by `key`.
 *
 * `value` and `profit` rank descending (the best first), `name` and `strategy`
 * order ascending; the input array is never mutated and ties keep the API order.
 */
export function sortProfiles(profiles: ProfileView[], key: SortKey): ProfileView[] {
  const decorated = profiles.map((profile, index) => ({ profile, index }));
  decorated.sort((left, right) => {
    const a = sortValue(left.profile, key);
    const b = sortValue(right.profile, key);
    let comparison: number;
    if (typeof a === "number" && typeof b === "number") {
      comparison = compareNumbers(a, b);
      comparison = key === "value" || key === "profit" ? -comparison : comparison;
    } else {
      comparison = String(a).localeCompare(String(b));
    }
    return comparison !== 0 ? comparison : left.index - right.index;
  });
  return decorated.map((entry) => entry.profile);
}

/**
 * `true` when at least one profile of the list needs an operator decision.
 *
 * Every profile that is not running a worker either needs a decision (`error`,
 * `blocked`) or is simply stopped, which is not a decision.
 */
export function hasAttentionProfile(states: ProfileState[]): boolean {
  return states.some(needsDecision);
}

/** Sorting keys offered by the tables, in display order. */
export const SORT_KEYS: readonly SortKey[] = ["value", "profit", "name", "strategy"];

/** Modes in display order: paper trading first, then real trading. */
export const MODE_ORDER: readonly ProfileMode[] = ["paper", "live"];
