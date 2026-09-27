import { AccountBand } from "@/components/overview/AccountBand";
import { LiveTradingNotice } from "@/components/overview/LiveTradingNotice";
import { ModeSection } from "@/components/overview/ModeSection";
import { EMPTY_HEALTH } from "@/components/operations/EngineStateCard";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { fetchAccount, fetchHealth, fetchProfiles, fetchSettings } from "@/lib/api";
import { splitByMode } from "@/lib/ranking";
import {
  DEFAULT_SETTINGS,
  EMPTY_ACCOUNT,
  EMPTY_PROFILES,
  type AccountPerformance,
  type ApiWindow,
} from "@/lib/types";

/** Live data on every request: the overview is never prerendered. */
export const dynamic = "force-dynamic";

const WINDOWS: readonly ApiWindow[] = ["24h", "7d", "30d", "all"];

export interface OverviewPageProps {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}

/** Validate the `?window=` parameter; anything unknown falls back to `24h`. */
export function normaliseWindow(value: string | string[] | undefined): ApiWindow {
  const candidate = Array.isArray(value) ? value[0] : value;
  return WINDOWS.find((entry) => entry === candidate) ?? "24h";
}

/**
 * Overview page.
 *
 * Top to bottom: the full-width account performance band, then two ranked
 * sections - "Paper trading" first, then "Real trading" - each with its own
 * header, its own aggregate line and its own ranking. The profiles keep the API
 * order (portfolio value, descending) and every row links to its profile page.
 */
export default async function OverviewPage({ searchParams }: OverviewPageProps) {
  const params = searchParams !== undefined ? await searchParams : {};
  const apiWindow = normaliseWindow(params?.window);

  const [account, profiles, settings, health] = await Promise.all([
    fetchAccount(apiWindow, EMPTY_ACCOUNT),
    fetchProfiles(EMPTY_PROFILES),
    fetchSettings(DEFAULT_SETTINGS),
    fetchHealth(EMPTY_HEALTH),
  ]);

  const { paper, live } = splitByMode(profiles.data.profiles);

  // `/api/account` publishes no slot capacity: the engine slots of the KPI row
  // are the ones `GET /api/health` reports, which is the endpoint the deploy
  // pipeline asserts on.
  const performance: AccountPerformance = {
    ...account.data.performance,
    engine_slots_used: health.data.running_profiles,
    engine_slots_total: health.data.max_running_profiles,
  };

  return (
    <div className="grid grid-cols-12 gap-2">
      <AccountBand
        className="col-span-12"
        performance={performance}
        equity={account.data.equity_curve}
        window={apiWindow}
      />
      <ErrorBanner
        className="col-span-12"
        error={account.error}
        title="Account performance could not be refreshed"
      />
      <ErrorBanner
        className="col-span-12"
        error={profiles.error}
        title="The profile ranking could not be refreshed"
      />
      <ErrorBanner
        className="col-span-12"
        error={settings.error}
        title="Platform settings could not be refreshed"
      />

      <ModeSection
        className="col-span-12"
        id="paper-trading"
        title="Paper trading"
        profiles={paper}
        emptyMessage="No paper profile is configured yet."
      />

      <ModeSection
        className="col-span-12"
        id="real-trading"
        title="Real trading"
        profiles={live}
        emptyMessage="No live profile is configured yet."
        notice={
          <LiveTradingNotice
            allowLiveTrading={settings.error ? null : settings.data.allow_live_trading}
            profiles={live}
          />
        }
      />
    </div>
  );
}
