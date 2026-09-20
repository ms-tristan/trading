import { ServerCrash } from 'lucide-react';

import { KillSwitchPanel } from '@/components/kill-switch/kill-switch-panel';
import { OverviewLive } from '@/components/overview/overview-live';
import { PlatformSummary } from '@/components/overview/platform-summary';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorBanner } from '@/components/ui/error-banner';
import { fetchHealth, fetchKillSwitch, fetchOrphans, fetchProfiles } from '@/lib/api';
import { failureReport } from '@/lib/api-failure';
import { resolvePollIntervalMs, serverApiBaseUrl } from '@/lib/config';
import type {
  HealthPayload,
  KillSwitchPayload,
  OrphanReport,
  ProfilesPayload,
} from '@/lib/types';

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
  /** Report of the startup safety sweep, `null` when it could not be read. */
  orphans: OrphanReport | null;
}

/**
 * The page heading.
 *
 * It is deliberately **visually hidden**: the app shell already names the
 * surface ("Trading Platform — real-time monitor"), so a second visible title
 * plus a one-line subtitle was pure duplication at the top of every render.
 * It stays in the accessibility tree because a page without a level-1 heading
 * loses its screen-reader outline.
 */
function PageHeading() {
  return <h1 className="sr-only">Overview</h1>;
}

/**
 * First paint rendered when the monitoring API cannot be reached.
 *
 * It is an ordinary page render, not an error screen: the operator gets the
 * mapped failure headline, the origin the dashboard tried to call (the
 * `API_ORIGIN` configuration) and what to check — and no polling is started.
 * The raw cause (status, server text, requested path) stays visible under the
 * headline, never as the headline itself.
 */
function ApiUnreachable({ failure, baseUrl }: { failure: unknown; baseUrl: string }) {
  const report = failureReport(failure);
  return (
    <div className="flex flex-col gap-xl">
      <PageHeading />
      <ErrorBanner message={report.headline} detail={report.detail} />
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
 * This is a Server Component: it fetches health, the profile list, the
 * kill-switch state and the orphan sweep report with `cache: 'no-store'`
 * (pinned by the API client) and renders the whole first paint, so the browser
 * issues no request until hydration. A failure of any of the three core calls
 * renders the unreachable state above instead of throwing: an API outage never
 * becomes a Next.js error screen. The orphan call is the documented exception —
 * it is normalised to `null` on its own, so a server that does not expose that
 * route yet still renders the normal page, merely without the warning banner.
 *
 * On success the page renders, in order, the platform summary, the kill-switch
 * control (seeded with the server-rendered state) and the single live region of
 * the page, whose polling cadence is resolved here — on the server — from
 * `NEXT_PUBLIC_POLL_INTERVAL_MS`, the mirror of `monitoring.refresh_seconds`.
 *
 * The shared platform wallet is part of that live region: it is rendered by
 * {@link OverviewLive} from the `wallet` of the `profiles` payload this Server
 * Component already fetched, so the first paint carries the wallet values and
 * the same polling bundle refreshes them. Exactly one wallet panel is rendered
 * — a second, page-level copy would show the same ledger twice.
 */
export default async function OverviewPage() {
  const baseUrl = serverApiBaseUrl();

  let overview: Overview;
  try {
    const [health, profiles, killSwitch, orphans] = await Promise.all([
      fetchHealth({ baseUrl }),
      fetchProfiles({ baseUrl }),
      fetchKillSwitch({ baseUrl }),
      // The orphan route alone is allowed to fail: a server that does not expose
      // it yet, or an operator token the route refuses, must NOT send the whole
      // page to the unreachable state. The warning banner is simply absent.
      fetchOrphans({ baseUrl }).catch(() => null),
    ]);
    overview = { health, profiles, killSwitch, orphans };
  } catch (error) {
    return <ApiUnreachable failure={error} baseUrl={baseUrl} />;
  }

  const { health, profiles, killSwitch, orphans } = overview;

  return (
    <div className="flex flex-col gap-2xl">
      <PageHeading />

      <PlatformSummary health={health} killSwitch={killSwitch} profiles={profiles.profiles} />

      <KillSwitchPanel initialState={killSwitch} />

      <OverviewLive
        initialProfiles={profiles}
        initialHealth={health}
        initialKillSwitch={killSwitch}
        initialOrphans={orphans}
        initialCheckedAt={health.checked_at ?? profiles.generated_at ?? new Date().toISOString()}
        pollIntervalMs={resolvePollIntervalMs()}
      />
    </div>
  );
}
