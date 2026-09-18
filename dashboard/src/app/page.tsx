import { ServerCrash } from 'lucide-react';

import { KillSwitchPanel } from '@/components/kill-switch/kill-switch-panel';
import { OverviewLive } from '@/components/overview/overview-live';
import { PlatformSummary } from '@/components/overview/platform-summary';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorBanner } from '@/components/ui/error-banner';
import { errorMessage, fetchHealth, fetchKillSwitch, fetchProfiles } from '@/lib/api';
import { resolvePollIntervalMs, serverApiBaseUrl } from '@/lib/config';
import type { HealthPayload, KillSwitchPayload, ProfilesPayload } from '@/lib/types';

/**
 * The overview is rendered per request: an outage of the monitoring server must
 * never be answered from a cached payload, and the "checked at" stamp must be
 * the stamp of the current render.
 */
export const dynamic = 'force-dynamic';

interface Overview {
  health: HealthPayload;
  profiles: ProfilesPayload;
  killSwitch: KillSwitchPayload;
}

/**
 * First paint rendered when the monitoring API cannot be reached.
 *
 * It is an ordinary page render, not an error screen: the operator gets the
 * documented failure message, the origin the dashboard tried to call (the
 * `API_ORIGIN` configuration) and what to check — and no polling is started.
 */
function ApiUnreachable({ message, baseUrl }: { message: string; baseUrl: string }) {
  return (
    <div className="flex flex-col gap-xl">
      <header>
        <h1 className="font-mono text-xl font-semibold text-foreground">Overview</h1>
        <p className="mt-xs text-sm text-muted-foreground">
          Every trading profile at a glance, refreshed by HTTP polling.
        </p>
      </header>
      <ErrorBanner message={message} />
      <EmptyState
        title="The monitoring API is unreachable"
        description={`No payload was returned by ${baseUrl}. Check that the Python monitoring server is running and that API_ORIGIN points at it.`}
        icon={<ServerCrash className="size-5" />}
      />
    </div>
  );
}

/**
 * Overview route — every profile at a glance.
 *
 * This is a Server Component: it fetches health, the profile list and the
 * kill-switch state with `cache: 'no-store'` (pinned by the API client) and
 * renders the whole first paint, so the browser issues no request until
 * hydration. A failure of any of the three calls renders the unreachable state
 * above instead of throwing: an API outage never becomes a Next.js error screen.
 *
 * On success the page renders, in order, the platform summary, the kill-switch
 * control (seeded with the server-rendered state) and the single live region of
 * the page, whose polling cadence is resolved here — on the server — from
 * `NEXT_PUBLIC_POLL_INTERVAL_MS`, the mirror of `monitoring.refresh_seconds`.
 */
export default async function OverviewPage() {
  const baseUrl = serverApiBaseUrl();

  let overview: Overview;
  try {
    const [health, profiles, killSwitch] = await Promise.all([
      fetchHealth({ baseUrl }),
      fetchProfiles({ baseUrl }),
      fetchKillSwitch({ baseUrl }),
    ]);
    overview = { health, profiles, killSwitch };
  } catch (error) {
    return <ApiUnreachable message={errorMessage(error)} baseUrl={baseUrl} />;
  }

  const { health, profiles, killSwitch } = overview;

  return (
    <div className="flex flex-col gap-2xl">
      <header>
        <h1 className="font-mono text-xl font-semibold text-foreground">Overview</h1>
        <p className="mt-xs text-sm text-muted-foreground">
          Every trading profile at a glance, refreshed by HTTP polling.
        </p>
      </header>

      <PlatformSummary health={health} killSwitch={killSwitch} profiles={profiles.profiles} />

      <KillSwitchPanel initialState={killSwitch} />

      <OverviewLive
        initialProfiles={profiles}
        initialHealth={health}
        initialKillSwitch={killSwitch}
        initialCheckedAt={health.checked_at ?? profiles.generated_at ?? new Date().toISOString()}
        pollIntervalMs={resolvePollIntervalMs()}
      />
    </div>
  );
}
