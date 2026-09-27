'use client';

import { useCallback, useState } from 'react';

import Link from 'next/link';

import { ArrowDownWideNarrow, Banknote, PlusCircle, ShieldAlert } from 'lucide-react';

import { AccountPerformance } from '@/components/overview/account-performance';
import { ModeToggle } from '@/components/overview/mode-toggle';
import { ProfileCard } from '@/components/overview/profile-card';
import { OrphanWarning } from '@/components/overview/orphan-warning';
import { WalletPanel } from '@/components/overview/wallet-panel';
import { BUTTON_SIZE_CLASSES } from '@/components/ui/button';
import { EmptyState } from '@/components/ui/empty-state';
import { LiveToolbar } from '@/components/ui/live-toolbar';
import { fetchHealth, fetchKillSwitch, fetchOrphans, fetchProfiles } from '@/lib/api';
import { failureReport } from '@/lib/api-failure';
import { cn } from '@/lib/cn';
import { EMPTY_PLACEHOLDER, formatInteger, formatTimestamp } from '@/lib/format';
import { resolveModeWallet } from '@/lib/performance';
import { sortProfilesByPortfolioValue } from '@/lib/ranking';
import type {
  HealthPayload,
  KillSwitchPayload,
  OrphanReport,
  ProfilesPayload,
  RunMode,
} from '@/lib/types';
import { usePolling } from '@/lib/use-polling';

/** Everything one live cycle of the overview refreshes. */
export interface OverviewBundle {
  health: HealthPayload;
  profiles: ProfilesPayload;
  killSwitch: KillSwitchPayload;
  /**
   * Report of the startup safety sweep, `null` when the platform was never
   * swept or when the orphan route alone could not be read.
   */
  orphans: OrphanReport | null;
}

/** Props of {@link OverviewLive}. */
export interface OverviewLiveProps {
  /** Profiles rendered by the Server Component (the first paint). */
  initialProfiles: ProfilesPayload;
  /** Platform health rendered by the Server Component. */
  initialHealth: HealthPayload;
  /** Kill-switch state rendered by the Server Component. */
  initialKillSwitch: KillSwitchPayload;
  /** Orphan sweep report rendered by the Server Component, `null` when none. */
  initialOrphans: OrphanReport | null;
  /** ISO-8601 stamp of the server-rendered payload. */
  initialCheckedAt: string;
  /** Polling cadence, resolved on the server from `monitoring.refresh_seconds`. */
  pollIntervalMs: number;
}

const HEADING_ID = 'overview-live-heading';

/** The mode the page starts on, in words, for the headings that name it. */
const MODE_WORDS: Record<RunMode, string> = {
  paper: 'paper trading',
  live: 'real trading',
};

/** How many profiles each mode holds, for the counts of the toggle. */
function countByMode(profiles: ProfilesPayload): Record<RunMode, number> {
  return {
    paper: profiles.profiles.filter((profile) => profile.mode === 'paper').length,
    live: profiles.profiles.filter((profile) => profile.mode === 'live').length,
  };
}

/**
 * The note that states the ordering of the list.
 *
 * The cards are sorted by portfolio value (highest first) so the ranking is
 * visible without sorting by hand — and a reordering an operator did not ask for
 * must never be silent, so the section says what the order is. The icon matches
 * the direction of the sort and is decorative: the sentence carries the meaning.
 */
const ORDER_NOTE = 'Ordered by portfolio value, highest first';

/**
 * What a real-trading profile needs before the platform will run it.
 *
 * The credentials are read from the **engine environment**, never from the
 * dashboard: the monitoring server cannot invent a venue account, and a live
 * profile it cannot authenticate is quarantined as a boot failure rather than
 * started half-configured. The wording names the exact variables so the operator
 * knows what to set, and the variables are split per line so each one stays
 * greppable.
 */
const LIVE_CREDENTIALS_NOTE =
  'A real-trading profile needs venue API credentials in the engine environment: either the per-profile TB_PROFILE_<ID>_API_KEY / TB_PROFILE_<ID>_API_SECRET pair or the shared TB_LIVE_API_KEY / TB_LIVE_API_SECRET pair, plus TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK. The platform refuses to run a live profile it cannot authenticate — such a profile is quarantined as a boot failure, never traded blind.';

/**
 * Classes of the profile creation action.
 *
 * The action is an anchor, not a button: it navigates to the creation route, it
 * does not act on the current payload. It is a *peer* of the two live controls
 * it sits between, so it borrows their exact box metrics from
 * {@link BUTTON_SIZE_CLASSES} (`size="sm"`) rather than restating them: measured
 * in a browser, a version that restated the padding with `text-sm` stood 30px
 * tall next to its 26px neighbours and the cluster read as misaligned. It keeps
 * the visible focus ring and the pressed feedback of every other control.
 */
const NEW_PROFILE_ACTION_CLASSES = cn(
  'inline-flex cursor-pointer select-none items-center rounded-button border border-border font-medium text-foreground',
  BUTTON_SIZE_CLASSES.sm,
  'motion-safe:transition-colors motion-safe:duration-200 hover:text-accent active:border-accent active:bg-muted-pressed active:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
);

/**
 * Live region of the overview: the only Client Component of the page.
 *
 * One polling cycle refreshes health, the profile list, the shared platform
 * wallet and the kill-switch state together, so a card never mixes two different
 * snapshots. The Server Component already rendered the first payload — mounting
 * therefore performs no request, and the first refresh happens on the documented
 * cadence (2 s by default).
 *
 * The region **leads with the global performance of the account**
 * ({@link AccountPerformance}): what the shared ledger of the selected mode is
 * worth, what it has returned, which profiles run and which one leads. It is
 * part of this region — and not a page-level block — precisely so that it
 * refreshes on the cycle that is already running and follows the selected mode
 * without a request of its own; it starts no second poll and fetches nothing.
 *
 * The wallet panel comes next, seeded with the wallet of the server payload: it
 * is the one instance of the panel on the page, so the overview never shows the
 * shared wallet twice. It is immediately followed by the orphan sweep warning,
 * whose own absence renders nothing.
 *
 * The profile list is a **ranking**: the profiles of the selected mode are
 * ordered by portfolio value, highest first, with `lib/ranking.ts` — the same
 * pure comparator the Server Component uses for the first paint, so the server
 * markup and the hydrated client agree and the list never reorders on
 * hydration. The order is never silent: the heading area states it and every
 * card shows the ordinal position it holds.
 *
 * **This component owns the selected run mode.** The mode is a filter over the
 * one payload the cycle refreshes, so the account performance block, the profile
 * list, the wallet panel and the counts of the toggle all read the *same*
 * object: switching modes re-reads it and issues **no request**. The default is
 * `paper`, which is what the Server Component renders before this component
 * mounts. Switching is therefore never a navigation, never a URL parameter and
 * never a second fetch.
 *
 * Robustness: a failed cycle is caught by `usePolling`, shows the non-blocking
 * banner of the toolbar — in the shared operator-facing vocabulary, never as a
 * raw proxy status — and keeps the last known good bundle on screen (the shared
 * wallet and the performance figures included); a card is keyed by `profile_id`,
 * so a refresh updates it in place instead of remounting it. Accessibility: the
 * toolbar carries the polite live region with the "checked at" stamp and the
 * paused / running state, and the Pause control is a plain, keyboard-operable
 * button. The creation action of the section ("New profile") is injected into
 * that same control cluster, so the action sits with the list it creates into
 * and stays the last tab stop of the cluster.
 */
export function OverviewLive({
  initialProfiles,
  initialHealth,
  initialKillSwitch,
  initialOrphans,
  initialCheckedAt,
  pollIntervalMs,
}: OverviewLiveProps) {
  // Same-origin on purpose: no base URL, so the request goes to the Next.js
  // rewrite (`/api/*` -> the Python monitoring server) and no CORS applies.
  //
  // The orphan sweep report is the one call of the cycle that is allowed to
  // fail on its own: a dashboard that cannot read `/api/orphans` must never
  // lose the profile list over it, so its failure is normalised to `null`
  // (no warning banner) while the rest of the bundle still rejects as before.
  const fetcher = useCallback(async (signal: AbortSignal): Promise<OverviewBundle> => {
    const [health, profiles, killSwitch, orphans] = await Promise.all([
      fetchHealth({ signal }),
      fetchProfiles({ signal }),
      fetchKillSwitch({ signal }),
      fetchOrphans({ signal }).catch(() => null),
    ]);
    return { health, profiles, killSwitch, orphans };
  }, []);

  const { data, checkedAt, failure, isPaused, toggle, refreshNow } = usePolling<OverviewBundle>({
    fetcher,
    initialData: {
      health: initialHealth,
      profiles: initialProfiles,
      killSwitch: initialKillSwitch,
      orphans: initialOrphans,
    },
    initialCheckedAt,
    intervalMs: pollIntervalMs,
  });

  const handleRefresh = useCallback((): void => {
    void refreshNow();
  }, [refreshNow]);

  // The selected mode is page state held here, on the client, and NOT in the URL.
  // The two modes are two views of one payload the polling cycle already
  // refreshes, so a route or a search param would add a navigation — and a
  // request — for a pure presentation switch. The user asked for a toggle, not
  // two pages.
  //
  // `paper` is the default because the Server Component renders the paper view
  // before this component mounts: the first paint and the client's initial state
  // are then the same view, so hydration neither flashes nor mismatches.
  const [mode, setMode] = useState<RunMode>('paper');

  const allProfiles = data.profiles.profiles;
  // The whole page follows the toggle, read out of the SAME payload the cycle
  // refreshes: the profile list keeps only the profiles of the selected mode,
  // ordered by portfolio value (highest first) so the list reads as a ranking.
  // The sort is the shared pure helper — the Server Component ordered the first
  // paint with it too, so hydration cannot reorder anything.
  const profiles = sortProfilesByPortfolioValue(
    allProfiles.filter((profile) => profile.mode === mode),
  );
  const counts = countByMode(data.profiles);
  // The ledger of the mode, resolved by the shared helper the account
  // performance block uses too: the mode-keyed `wallets` object is the source of
  // truth as soon as the server emits it — including when one of its entries is
  // an explicit `null` — and only a payload *without* that object falls back to
  // `wallet`, for **paper only**. One implementation, so the wallet panel and the
  // performance hero can never disagree about which ledger is on screen.
  const wallet = resolveModeWallet(data.profiles, mode);
  const isEngaged = data.killSwitch.kill_switch;
  const reason = data.killSwitch.reason.trim();

  return (
    <>
      {/* The global performance of the account leads the live region: the first
          question an operator opens this page with is "how is the account
          doing", not "what is in the wallet". It reads the same payload the
          cycle refreshes and follows the same selected mode — no request, no
          second poll. The wallet panel below keeps every cash, position and
          exposure detail; the only figure the two blocks share is the portfolio
          value. */}
      <AccountPerformance profiles={data.profiles} mode={mode} />

      {/* The shared wallet of the server payload, refreshed by every cycle and
          kept as the last known good value when a cycle fails. `?? null`
          tolerates a payload without the wallet key at all. */}
      <WalletPanel wallet={wallet} mode={mode} />

      {/* The safety sweep warning is the first block after the wallet, before
          the kill-switch notice and the profile grid: a position that was
          flattened — or worse, that could NOT be flattened — at the venue must
          never be pushed below the fold by the list it belongs to. */}
      <OrphanWarning report={data.orphans} />

      <section aria-labelledby={HEADING_ID} className="flex flex-col gap-lg">
        <header className="flex flex-wrap items-end justify-between gap-md">
          <div className="min-w-0">
            {/* The heading names the mode, so the list is never anonymous: an
                empty list must say *which* side is empty. */}
            <h2 id={HEADING_ID} className="font-mono text-base font-semibold text-foreground">
              {`Profiles — ${MODE_WORDS[mode]}`}
            </h2>
            <p className="mt-xs text-sm text-muted-foreground">
              One card per configured profile, refreshed by HTTP polling.
            </p>
            {/* The ordering is visible, never silent: the list is a ranking and
                it says so, right above the list itself. */}
            <p className="mt-xs flex flex-wrap items-center gap-xs text-xs text-muted-foreground">
              <ArrowDownWideNarrow aria-hidden="true" className="size-3.5 shrink-0" />
              <span>{ORDER_NOTE}</span>
            </p>
          </div>
        </header>

        {/* The mode filter, owned by this live region: switching it issues no
            request, it re-reads the payload the cycle already refreshed. */}
        <ModeToggle value={mode} onChange={setMode} counts={counts} />

        <LiveToolbar
          checkedAt={checkedAt}
          isPaused={isPaused}
          onToggle={toggle}
          onRefresh={handleRefresh}
          // The failure is never printed raw: the mapping turns a 502/503/504, a
          // connection failure or a timeout into the same operator-facing headline
          // and keeps the underlying cause as the detail line.
          error={failure === null ? null : failureReport(failure).headline}
          errorDetail={failure === null ? null : failureReport(failure).detail}
          actions={
            <Link href="/profiles/new" className={NEW_PROFILE_ACTION_CLASSES}>
              <PlusCircle aria-hidden="true" className="size-3.5 text-accent" />
              <span>New profile</span>
            </Link>
          }
        />

        {isEngaged ? (
          <p
            role="status"
            aria-live="polite"
            className="flex flex-wrap items-center gap-sm rounded-card border border-loss/40 bg-loss/10 px-lg py-md text-sm text-foreground"
          >
            <ShieldAlert aria-hidden="true" className="size-4 shrink-0 text-loss" />
            <span className="font-medium">Kill switch engaged — every profile is halted.</span>
            <span className="min-w-0 break-words text-muted-foreground">
              Reason: {reason === '' ? EMPTY_PLACEHOLDER : reason} · changed at{' '}
              {formatTimestamp(data.killSwitch.changed_at)}
            </span>
          </p>
        ) : null}

        {profiles.length === 0 ? (
          allProfiles.length > 0 ? (
            // The platform DOES hold profiles — just none in the selected mode.
            // Saying "no profile configured" here would be a lie the operator
            // would act on, so the two empty cases never share a wording. The
            // real-trading side gets its own, actionable sentence: the reason a
            // live profile is missing is almost always a missing credential, and
            // "switch to the other side" would send the operator looking in the
            // wrong place.
            mode === 'live' ? (
              <EmptyState
                title="No real-trading profile configured"
                description={LIVE_CREDENTIALS_NOTE}
                icon={<Banknote className="size-5" />}
              />
            ) : (
              <EmptyState
                title="No profile for this mode"
                description={`The platform holds ${formatInteger(
                  allProfiles.length,
                )} profile(s), none of them in ${MODE_WORDS[mode]}. Switch the trading mode above to see the other side.`}
              />
            )
          ) : data.health.profiles_total > 0 ? (
            <EmptyState
              title="No profile reported yet"
              description={`Configured profiles: ${formatInteger(
                data.health.profiles_total,
              )}. None has published a snapshot yet — reload once a profile has started.`}
            />
          ) : (
            <EmptyState
              title="No profile configured"
              description="The monitoring server has no profile configured. Add one to its configuration, then reload this page."
            />
          )
        ) : (
          <div className="grid gap-xl lg:grid-cols-2 2xl:grid-cols-3">
            {profiles.map((profile, index) => (
              <ProfileCard
                key={profile.profile_id}
                profile={profile}
                // The position in the ranking this list IS: the same order the
                // Server Component rendered for the first paint.
                rank={index + 1}
                rankTotal={profiles.length}
              />
            ))}
          </div>
        )}
      </section>
    </>
  );
}
