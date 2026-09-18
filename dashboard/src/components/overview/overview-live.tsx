'use client';

import { useCallback } from 'react';

import { ShieldAlert } from 'lucide-react';

import { ProfileCard } from '@/components/overview/profile-card';
import { EmptyState } from '@/components/ui/empty-state';
import { LiveToolbar } from '@/components/ui/live-toolbar';
import { fetchHealth, fetchKillSwitch, fetchProfiles } from '@/lib/api';
import { EMPTY_PLACEHOLDER, formatInteger, formatTimestamp } from '@/lib/format';
import type { HealthPayload, KillSwitchPayload, ProfilesPayload } from '@/lib/types';
import { usePolling } from '@/lib/use-polling';

/** Everything one live cycle of the overview refreshes. */
export interface OverviewBundle {
  health: HealthPayload;
  profiles: ProfilesPayload;
  killSwitch: KillSwitchPayload;
}

/** Props of {@link OverviewLive}. */
export interface OverviewLiveProps {
  /** Profiles rendered by the Server Component (the first paint). */
  initialProfiles: ProfilesPayload;
  /** Platform health rendered by the Server Component. */
  initialHealth: HealthPayload;
  /** Kill-switch state rendered by the Server Component. */
  initialKillSwitch: KillSwitchPayload;
  /** ISO-8601 stamp of the server-rendered payload. */
  initialCheckedAt: string;
  /** Polling cadence, resolved on the server from `monitoring.refresh_seconds`. */
  pollIntervalMs: number;
}

const HEADING_ID = 'overview-live-heading';

/**
 * Live region of the overview: the only Client Component of the page.
 *
 * One polling cycle refreshes health, the profile list and the kill-switch state
 * together, so a card never mixes two different snapshots. The Server Component
 * already rendered the first payload — mounting therefore performs no request,
 * and the first refresh happens on the documented cadence (2 s by default).
 *
 * Robustness: a failed cycle is caught by `usePolling`, shows the non-blocking
 * banner of the toolbar and keeps the last known good bundle on screen; a card
 * is keyed by `profile_id`, so a refresh updates it in place instead of
 * remounting it. Accessibility: the toolbar carries the polite live region with
 * the "checked at" stamp and the paused / running state, and the Pause control
 * is a plain, keyboard-operable button.
 */
export function OverviewLive({
  initialProfiles,
  initialHealth,
  initialKillSwitch,
  initialCheckedAt,
  pollIntervalMs,
}: OverviewLiveProps) {
  // Same-origin on purpose: no base URL, so the request goes to the Next.js
  // rewrite (`/api/*` -> the Python monitoring server) and no CORS applies.
  const fetcher = useCallback(async (signal: AbortSignal): Promise<OverviewBundle> => {
    const [health, profiles, killSwitch] = await Promise.all([
      fetchHealth({ signal }),
      fetchProfiles({ signal }),
      fetchKillSwitch({ signal }),
    ]);
    return { health, profiles, killSwitch };
  }, []);

  const { data, checkedAt, error, isPaused, toggle, refreshNow } = usePolling<OverviewBundle>({
    fetcher,
    initialData: { health: initialHealth, profiles: initialProfiles, killSwitch: initialKillSwitch },
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
        error={error}
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
  );
}
