import type { JSX } from 'react';

import { notFound } from 'next/navigation';

import { ProfileLive } from '@/components/profile/profile-live';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorBanner } from '@/components/ui/error-banner';
import {
  ApiError,
  errorMessage,
  fetchEquity,
  fetchHealth,
  fetchKillSwitch,
  fetchMetrics,
  fetchOrders,
  fetchPositions,
  fetchProfile,
  fetchTrades,
} from '@/lib/api';
import { resolveDetailPollIntervalMs, resolvePollIntervalMs, serverApiBaseUrl } from '@/lib/config';
import type { ProfileDetailBundle, ProfileLiveBundle } from '@/lib/types';

/**
 * Per-profile detail route.
 *
 * A Server Component performs the first render: it awaits the route params,
 * fetches every payload once with `cache: 'no-store'` (the Python monitoring
 * server is the source of truth, nothing may be cached between requests) and
 * hands the bundles to {@link ProfileLive}. The render tree of this route is
 * therefore the profile header (identity and platform fields), the kill-switch
 * panel seeded with its server-rendered state, the equity chart section, and the
 * positions, trades, orders, metrics and benchmark panels — all of them server
 * rendered for the first paint, because a Client Component is still rendered on
 * the server in the App Router.
 *
 * The live section is a Client Component on purpose: the fast loop refreshes the
 * profile snapshot, the platform health and the kill-switch state, so the header
 * and the emergency control must re-render with it. Keeping a second, static
 * copy of them in this file would render the same identity block twice and would
 * freeze the equity tile while the curve below it kept moving.
 *
 * Failure handling:
 *
 * * an `ApiError` with `kind: 'http'` and `status: 404` means the profile does
 *   not exist: the documented JSON 404 of the API becomes {@link notFound}, and
 *   the root `not-found.tsx` renders;
 * * any other failure (network, malformed payload, 5xx) renders a non-blocking
 *   banner plus an empty state and **starts no polling at all** — a page that
 *   cannot read the API must not hammer it every two seconds.
 */

/** Next 16 hands the route params to the page as a promise. */
export interface ProfileDetailPageProps {
  params: Promise<{ id: string }>;
}

/** Never prerender: the page reads live state from the monitoring server. */
export const dynamic = 'force-dynamic';

/** Whether a failure is the documented "this profile does not exist" answer. */
function isNotFoundFailure(failure: unknown): boolean {
  return failure instanceof ApiError && failure.kind === 'http' && failure.status === 404;
}

/** Outage view: no data, no polling, no crash. */
function OutageView({ profileId, message }: { profileId: string; message: string }) {
  return (
    <div className="flex flex-col gap-xl">
      <h1 className="font-mono text-xl font-semibold text-foreground">
        Profile <span className="font-mono">{profileId}</span>
      </h1>
      <ErrorBanner message={message} />
      <EmptyState
        title="Profile unavailable"
        description="The monitoring API did not answer, so no live view was started. Reload the page once the server is reachable."
      />
    </div>
  );
}

/** Read the profile and the two bundles, then render the live section. */
export default async function ProfileDetailPage({
  params,
}: ProfileDetailPageProps): Promise<JSX.Element> {
  const { id } = await params;
  const baseUrl = serverApiBaseUrl();

  const profileResult = await fetchProfile(id, { baseUrl }).then(
    (value) => ({ ok: true as const, value }),
    (failure: unknown) => ({ ok: false as const, failure }),
  );

  if (!profileResult.ok) {
    if (isNotFoundFailure(profileResult.failure)) {
      notFound();
    }
    return <OutageView profileId={id} message={errorMessage(profileResult.failure)} />;
  }

  const bundlesResult = await Promise.all([
    fetchEquity(id, { baseUrl }),
    fetchPositions(id, { baseUrl }),
    fetchTrades(id, { baseUrl }),
    fetchOrders(id, { baseUrl }),
    fetchMetrics(id, { baseUrl }),
    fetchHealth({ baseUrl }),
    fetchKillSwitch({ baseUrl }),
  ]).then(
    (value) => ({ ok: true as const, value }),
    (failure: unknown) => ({ ok: false as const, failure }),
  );

  if (!bundlesResult.ok) {
    return <OutageView profileId={id} message={errorMessage(bundlesResult.failure)} />;
  }

  const [equity, positions, trades, orders, metrics, health, killSwitch] = bundlesResult.value;
  const detail: ProfileDetailBundle = { equity, positions, trades, orders, metrics };
  const live: ProfileLiveBundle = { profile: profileResult.value, health, killSwitch };

  const pollIntervalMs = resolvePollIntervalMs();
  const detailIntervalMs = resolveDetailPollIntervalMs(pollIntervalMs);

  return (
    <ProfileLive
      profileId={id}
      initialLive={live}
      initialDetail={detail}
      initialCheckedAt={health.checked_at ?? new Date().toISOString()}
      pollIntervalMs={pollIntervalMs}
      detailIntervalMs={detailIntervalMs}
    />
  );
}
