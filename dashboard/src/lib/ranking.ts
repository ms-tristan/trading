/**
 * Ranking of the profile cards of the overview.
 *
 * The overview leads with the global performance of the account, so the list
 * below it must read as a **ranking**: the profile that holds the most capital
 * comes first. The order is therefore never "whatever the API returned" — it is
 * a documented, visible ranking, stated in the section heading and shown as an
 * ordinal badge on every card (see `components/overview/profile-card.tsx`).
 *
 * **The measure is `ProfileSnapshot.equity`** — the *attributed equity* of the
 * profile, which is exactly `allocation + realized_pnl + unrealized_pnl`: the
 * profile's share of the one shared ledger, marked to market. It is the measure
 * because it is what "how much is this profile worth right now" means on this
 * API, and because `WalletSnapshot.total_portfolio_value` is documented as
 * exactly `sum(profile.equity)`, so the ranking sums to the hero number the
 * operator reads at the top of the page.
 *
 * The ordering is **one** pure function used by both renderers: the Server
 * Component passes the sorted list into the first paint and the client live
 * region re-sorts the payload of every polling cycle with the very same code.
 * The server markup and the hydrated client then agree, so the list never
 * reorders (flashes) on hydration.
 *
 * Determinism is a hard requirement, not a nicety: any divergence between the
 * server render and the client render is a hydration mismatch. That is why a
 * tie is broken by a plain `<`/`>` comparison of `profile_id` and **never** by
 * `localeCompare`: Node and the browser do not share an ICU collation, so a
 * locale-aware comparison can order two ids differently on the two sides.
 */

import { isFiniteNumber } from '@/lib/format';
import type { ProfileSnapshot } from '@/lib/types';

/**
 * The ranking measure of one profile: its attributed equity.
 *
 * An absent, `null` or non-finite equity (a profile that has not published a
 * figure yet) is ranked **last** rather than dropped: a card never disappears
 * from the overview because one field is missing. `Number.NEGATIVE_INFINITY` is
 * used instead of `0` on purpose — a real `0` equity is a real, lowest-but-valid
 * position in the ranking, and it must not be confused with "unknown".
 */
export function portfolioValueOf(profile: ProfileSnapshot): number {
  return isFiniteNumber(profile.equity) ? profile.equity : Number.NEGATIVE_INFINITY;
}

/**
 * Total order over the profiles of one mode: attributed equity descending, ties
 * broken by `profile_id` ascending.
 *
 * The comparator is total — every pair of profiles is ordered, and the order is
 * antisymmetric and transitive — so the sorted result is deterministic for any
 * input, whatever order the server emitted it in.
 */
function compareProfilesByPortfolioValue(a: ProfileSnapshot, b: ProfileSnapshot): number {
  const left = portfolioValueOf(a);
  const right = portfolioValueOf(b);
  if (left !== right) {
    return left > right ? -1 : 1;
  }
  // Equal measure (including "both unknown"): the id decides, so the order is
  // total. Plain string comparison on purpose — see the module docstring.
  const leftId = a.profile_id;
  const rightId = b.profile_id;
  if (leftId === rightId) {
    return 0;
  }
  return leftId < rightId ? -1 : 1;
}

/**
 * Order the profiles by portfolio value, highest first.
 *
 * Returns a **new** array: the input is never mutated and never sorted in place,
 * because it is the payload of the polling cycle and other blocks of the page
 * (the counts of the mode toggle, the wallet panel) read it as it arrived.
 */
export function sortProfilesByPortfolioValue(profiles: ProfileSnapshot[]): ProfileSnapshot[] {
  return [...profiles].sort(compareProfilesByPortfolioValue);
}
