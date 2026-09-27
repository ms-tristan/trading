/**
 * The ledger arithmetic of the global account performance block.
 *
 * Every figure the hero shows is computed **here**, as a pure function of the
 * payload the polling cycle already fetched (`GET /api/profiles`): the component
 * that renders the block stays presentational and never derives a percentage of
 * its own. That split is what makes the numbers unit-testable without a DOM, and
 * it is what guarantees the em dash rule: a value that cannot be derived is
 * `null` — never `NaN`, never a guess.
 *
 * No percentage is ever invented. The return in percent is derived from the
 * ledger's own `initial_balance` and its portfolio value and nothing else; when
 * the initial balance is missing or zero there is no ratio to compute, so the
 * component renders the em dash placeholder.
 */

import { isFiniteNumber } from '@/lib/format';
import type { ProfileSnapshot, ProfilesPayload, RunMode, WalletSnapshot } from '@/lib/types';

/** The performance of one ledger, as far as it can be derived from its payload. */
export interface LedgerPerformance {
  /** Portfolio value: `total_portfolio_value`, or the sum of its two parts. */
  portfolioValue: number | null;
  /** `portfolioValue - initial_balance`, in USDT; `null` when not derivable. */
  totalReturn: number | null;
  /** `totalReturn / initial_balance` (a fraction); `null` when not derivable. */
  totalReturnRatio: number | null;
}

/** One ranked profile: its id and its total return, as a fraction. */
export interface RankedProfile {
  /** `profile_id` of the profile, used as its displayed name. */
  profileId: string;
  /** `total_return` of the profile (a fraction, not a percentage). */
  totalReturn: number;
}

/** Everything the global account performance block renders, for one mode. */
export interface AccountPerformanceSummary {
  /** Performance of the ledger of the selected mode. */
  ledger: LedgerPerformance;
  /** Realized P&L of that ledger, `null` when the payload does not carry it. */
  realizedPnl: number | null;
  /** Unrealized P&L of that ledger, `null` when the payload does not carry it. */
  unrealizedPnl: number | null;
  /** Capital deployed in open positions across the mode, `null` when absent. */
  deployed: number | null;
  /** Profiles of the mode whose status is `running`. */
  profilesRunning: number;
  /** Profiles the mode holds, whatever their status. */
  profilesConfigured: number;
  /** Best performing profile of the mode by total return, `null` when none. */
  best: RankedProfile | null;
  /** Worst performing profile of the mode by total return, `null` when none. */
  worst: RankedProfile | null;
}

/** A finite value, or `null` when the payload holds nothing usable. */
function finiteOrNull(value: number | null | undefined): number | null {
  return isFiniteNumber(value) ? value : null;
}

/**
 * The ledger of one run mode out of the profiles payload.
 *
 * `wallets` is the mode-keyed view and is the source of truth as soon as the
 * server emits it — **including** when one of its two entries is an explicit
 * `null`, which means "that mode was never traded and holds no ledger row yet"
 * and must NOT fall back to the other mode's figures: showing paper money under
 * a real-trading heading is exactly the lie this rule exists to prevent.
 *
 * Only when the whole `wallets` object is `null` — an older server that does not
 * emit the key at all — does the payload fall back to `wallet`. That fallback is
 * for **paper only**, because `wallet` *is* the paper/default ledger by contract.
 *
 * This is the one implementation of that rule: `overview-live.tsx` resolves the
 * ledger it hands to the wallet panel with this very function, so the wallet
 * panel and the performance hero can never disagree about which ledger is on
 * screen.
 */
export function resolveModeWallet(
  profiles: ProfilesPayload,
  mode: RunMode,
): WalletSnapshot | null {
  const ledgers = profiles.wallets ?? null;
  if (ledgers === null) {
    return mode === 'paper' ? profiles.wallet ?? null : null;
  }
  return ledgers[mode] ?? null;
}

/**
 * The portfolio value of one ledger.
 *
 * `total_portfolio_value` is the documented total and is preferred. When the
 * server does not emit the three `total_*` fields yet, the sum of its two parts
 * is the same number; when even those are missing, the ledger's own `equity` is
 * the last available reading of the same quantity. Only a ledger that carries
 * none of the three yields `null`.
 */
function portfolioValueOf(wallet: WalletSnapshot): number | null {
  if (isFiniteNumber(wallet.total_portfolio_value)) {
    return wallet.total_portfolio_value;
  }
  if (isFiniteNumber(wallet.total_cash) && isFiniteNumber(wallet.positions_value)) {
    return wallet.total_cash + wallet.positions_value;
  }
  return finiteOrNull(wallet.equity);
}

/**
 * The performance of one ledger: its portfolio value, its return in USDT and its
 * return as a fraction.
 *
 * The three are computed together because they are one chain: no portfolio value
 * means no return, and no usable initial balance means no return either — an
 * absent or zero `initial_balance` cannot be divided by, and a return in percent
 * is never invented from a different field. The portfolio value itself survives
 * both: a ledger that reports a value but no initial balance still renders its
 * value (with em dashes for the returns).
 *
 * A real `0` portfolio value is a value: it renders `$0.00` and yields a return
 * of `-1` (−100%) against a non-zero initial balance, exactly as read.
 */
export function ledgerPerformance(wallet: WalletSnapshot | null | undefined): LedgerPerformance {
  const empty: LedgerPerformance = {
    portfolioValue: null,
    totalReturn: null,
    totalReturnRatio: null,
  };
  if (wallet === null || wallet === undefined) {
    return empty;
  }

  const portfolioValue = portfolioValueOf(wallet);
  if (portfolioValue === null) {
    return empty;
  }

  const initialBalance = finiteOrNull(wallet.initial_balance);
  if (initialBalance === null || initialBalance === 0) {
    return { portfolioValue, totalReturn: null, totalReturnRatio: null };
  }

  const totalReturn = portfolioValue - initialBalance;
  return { portfolioValue, totalReturn, totalReturnRatio: totalReturn / initialBalance };
}

/**
 * Rank the profiles of one mode by total return.
 *
 * Only profiles carrying a **finite** `total_return` qualify: a profile that has
 * not published a return yet is not "the worst one", it is unranked, and picking
 * it would put a name on a figure that does not exist.
 *
 * Ties are broken by `profile_id` ascending (a plain string comparison, never
 * `localeCompare`) so the picked profile is the same on the server and in the
 * browser for any payload.
 */
function rankByTotalReturn(
  profiles: ProfileSnapshot[],
  direction: 'best' | 'worst',
): RankedProfile | null {
  let selected: RankedProfile | null = null;
  for (const profile of profiles) {
    if (!isFiniteNumber(profile.total_return)) {
      continue;
    }
    const candidate: RankedProfile = {
      profileId: profile.profile_id,
      totalReturn: profile.total_return,
    };
    if (selected === null) {
      selected = candidate;
      continue;
    }
    const isBetter =
      direction === 'best'
        ? candidate.totalReturn > selected.totalReturn
        : candidate.totalReturn < selected.totalReturn;
    const isTie = candidate.totalReturn === selected.totalReturn;
    if (isBetter || (isTie && candidate.profileId < selected.profileId)) {
      selected = candidate;
    }
  }
  return selected;
}

/**
 * The global performance of the account, for one run mode.
 *
 * Everything is read out of the payload of `GET /api/profiles` the polling cycle
 * already refreshed: the mode's ledger (performance and P&L), the profiles of
 * that mode (how many run out of how many are configured) and the best and worst
 * of them by total return. Nothing is fetched, nothing is estimated, and a mode
 * that holds no ledger row yields all-`null` figures rather than the other mode's.
 */
export function accountPerformance(
  profiles: ProfilesPayload,
  mode: RunMode,
): AccountPerformanceSummary {
  const wallet = resolveModeWallet(profiles, mode);
  const ofMode = profiles.profiles.filter((profile) => profile.mode === mode);

  return {
    ledger: ledgerPerformance(wallet),
    realizedPnl: finiteOrNull(wallet?.realized_pnl),
    unrealizedPnl: finiteOrNull(wallet?.unrealized_pnl),
    deployed: finiteOrNull(wallet?.deployed),
    profilesRunning: ofMode.filter((profile) => profile.status === 'running').length,
    profilesConfigured: ofMode.length,
    best: rankByTotalReturn(ofMode, 'best'),
    worst: rankByTotalReturn(ofMode, 'worst'),
  };
}
