import { ProfileConfigCard } from "@/components/profile/ProfileConfigCard";
import { ProfileControls } from "@/components/profile/ProfileControls";
import {
  ProfileEquitySection,
  normaliseProfileWindow,
} from "@/components/profile/ProfileEquitySection";
import { ProfileHeader } from "@/components/profile/ProfileHeader";
import { ProfileKpiRow } from "@/components/profile/ProfileKpiRow";
import { ClosedTradesCard, OpenTradesCard } from "@/components/profile/ProfileTrades";
import {
  EMPTY_PROFILE_CONFIG,
  EMPTY_PROFILE_DETAIL,
  EMPTY_PROFILE_VIEW,
  profileRank,
  readProfileExtras,
  resolveCapital,
} from "@/components/profile/wire";
import { EMPTY_HEALTH } from "@/components/operations/EngineStateCard";
import { StrategyCard } from "@/components/strategies/StrategyCard";
import { strategyFromDetail } from "@/components/strategies/strategyMeta";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { fetchHealth, fetchProfileDetail, fetchProfiles } from "@/lib/api";
import {
  EMPTY_ACCOUNT,
  EMPTY_PROFILES,
} from "@/lib/types";

/** Live data on every request: a profile page is never prerendered. */
export const dynamic = "force-dynamic";

export interface ProfileDetailPageProps {
  params: Promise<{ id: string }>;
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}

/**
 * Profile page.
 *
 * Top to bottom: the identity header with the lifecycle controls, the KPI row,
 * the equity curve with its window selector, the daily bars, the open trades, the
 * recent closed trades, the resolved configuration and the strategy card.
 *
 * Every read goes through the mapped fetchers of `@/lib/api`, so a failing API
 * `ErrorBanner` **above** the data it did manage to load: the last known payload
 * when there is one, an empty but well-formed page otherwise - a page of this
 * dashboard is never blank and never throws. An unknown id is the `404` of the
 * API: its message is the banner, and the header keeps showing the requested id.
 *
 * The `rank` and the `uptime` of the KPI row are not published by the profile
 * endpoint: the rank is the position of the profile in the ranked list of
 * `GET /api/profiles`, and the uptime is the process uptime of `GET /api/health`
 * (a per-profile `uptime_seconds` wins when the payload carries one).
 *
 * The engine slot and the freqtrade REST port the header prints are part of the
 * profile row (`slot`, `worker_port` on the wire): they are read from the mapped
 * `ProfileView`, so a profile that holds no worker simply omits the line.
 */
export default async function ProfileDetailPage({
  params,
  searchParams,
}: ProfileDetailPageProps) {
  const { id } = await params;
  const query = searchParams !== undefined ? await searchParams : {};
  const apiWindow = normaliseProfileWindow(query?.window);

  const [detail, profiles, health] = await Promise.all([
    fetchProfileDetail(id, apiWindow, EMPTY_PROFILE_DETAIL),
    fetchProfiles(EMPTY_PROFILES),
    fetchHealth(EMPTY_HEALTH),
  ]);

  // The documented blocks are also read through a default: a 200 that does not
  // carry one of them renders an empty page, it never throws.
  const payload = detail.data;
  const profileView = payload.profile ?? EMPTY_PROFILE_VIEW;
  const performanceBlock = payload.performance ?? EMPTY_ACCOUNT.performance;
  const configBlock = payload.config ?? EMPTY_PROFILE_CONFIG;
  const equity = payload.equity_curve ?? [];

  const extras = readProfileExtras(payload);
  const capital = resolveCapital(detail.data, extras);
  const rankedProfiles = profiles.data.profiles ?? [];
  const rank = extras.rank ?? profileRank(rankedProfiles, id);
  const healthUptime =
    health.data.generated_at !== "" && Number.isFinite(health.data.uptime_seconds)
      ? health.data.uptime_seconds
      : null;
  const uptimeSeconds = extras.uptimeSeconds ?? healthUptime;
  const strategy = strategyFromDetail(payload);
  const notFound = detail.error?.status === 404;

  return (
    <div className="grid grid-cols-12 gap-2">
      <ProfileHeader
        className="col-span-12"
        profile={profileView}
        fallbackName={id}
        actions={<ProfileControls profileId={id} state={profileView.state} />}
      />

      <ErrorBanner
        className="col-span-12"
        error={detail.error}
        title={notFound ? "This profile does not exist" : "The profile could not be refreshed"}
      />
      <ErrorBanner
        className="col-span-12"
        error={profiles.error}
        title="The profile ranking could not be refreshed"
      />
      <ErrorBanner
        className="col-span-12"
        error={health.error}
        title="The engine state could not be refreshed"
      />

      <ProfileKpiRow
        className="col-span-12"
        profile={profileView}
        performance={performanceBlock}
        cash={capital.cash}
        positionsValue={capital.positionsValue}
        rank={rank}
        uptimeSeconds={uptimeSeconds}
      />

      <ProfileEquitySection
        className="col-span-12"
        profileId={id}
        window={apiWindow}
        equity={equity}
        daily={extras.dailyBars}
      />

      <OpenTradesCard className="col-span-12" trades={extras.openTrades} />
      <ClosedTradesCard className="col-span-12" trades={extras.recentTrades} />

      <ProfileConfigCard
        className="col-span-12 lg:col-span-6"
        config={configBlock}
        profile={profileView}
        exchange={extras.exchange}
        priority={extras.priority}
      />

      {strategy !== null ? (
        <StrategyCard className="col-span-12 lg:col-span-6" view={strategy} />
      ) : null}
    </div>
  );
}
