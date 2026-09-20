'use client';

import { useCallback } from 'react';

import Link from 'next/link';

import { PlusCircle, ShieldAlert } from 'lucide-react';

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
import type {
  HealthPayload,
  KillSwitchPayload,
  OrphanReport,
  ProfilesPayload,
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
 * The wallet panel is the first block of the live region, seeded with the wallet
 * of the server payload: it is the one instance of the panel on the page, so the
 * overview never shows the shared wallet twice. It is immediately followed by
 * the orphan sweep warning, whose own absence renders nothing.
 *
 * Robustness: a failed cycle is caught by `usePolling`, shows the non-blocking
 * banner of the toolbar — in the shared operator-facing vocabulary, never as a
 * raw proxy status — and keeps the last known good bundle on screen (the shared
 * wallet included); a card is keyed by `profile_id`, so a refresh updates it in
 * place instead of remounting it. Accessibility: the toolbar carries the polite
 * live region with the "checked at" stamp and the paused / running state, and
 * the Pause control is a plain, keyboard-operable button. The creation action of
 * the section ("New profile") is injected into that same control cluster, so the
 * action sits with the list it creates into and stays the last tab stop of the
 * cluster.
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

  const profiles = data.profiles.profiles;
  const isEngaged = data.killSwitch.kill_switch;
  const reason = data.killSwitch.reason.trim();

  return (
    <>
      {/* The shared wallet of the server payload, refreshed by every cycle and
          kept as the last known good value when a cycle fails. `?? null`
          tolerates a payload without the wallet key at all. */}
      <WalletPanel wallet={data.profiles.wallet ?? null} />

      {/* The safety sweep warning is the first block after the wallet, before
          the kill-switch notice and the profile grid: a position that was
          flattened — or worse, that could NOT be flattened — at the venue must
          never be pushed below the fold by the list it belongs to. */}
      <OrphanWarning report={data.orphans} />

      <section aria-labelledby={HEADING_ID} className="flex flex-col gap-lg">
        <header className="flex flex-wrap items-end justify-between gap-md">
          <div className="min-w-0">
            <h2 id={HEADING_ID} className="font-mono text-base font-semibold text-foreground">
              Profiles
            </h2>
            <p className="mt-xs text-sm text-muted-foreground">
              One card per configured profile, refreshed by HTTP polling.
            </p>
          </div>
        </header>

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
          data.health.profiles_total > 0 ? (
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
            {profiles.map((profile) => (
              <ProfileCard key={profile.profile_id} profile={profile} />
            ))}
          </div>
        )}
      </section>
    </>
  );
}
