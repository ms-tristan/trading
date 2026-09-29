import { AccountBand } from "@/components/overview/AccountBand";
import { LiveTradingNotice } from "@/components/overview/LiveTradingNotice";
import { ModeSection } from "@/components/overview/ModeSection";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { fetchAccount, fetchProfiles, fetchSettings } from "@/lib/api";
import { splitByMode } from "@/lib/ranking";
import {
  DEFAULT_SETTINGS,
  EMPTY_ACCOUNT,
  EMPTY_PROFILES,
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
 *
 * The fleet runs every profile of a section in one process: there is no cap and
 * no queue, so the page states no capacity and no queue. The real-trading section
 * carries the live-gate notice, which spells out why a live profile is blocked.
 */
export default async function OverviewPage({ searchParams }: OverviewPageProps) {
  const params = searchParams !== undefined ? await searchParams : {};
  const apiWindow = normaliseWindow(params?.window);

  const [account, profiles, settings] = await Promise.all([
    fetchAccount(apiWindow, EMPTY_ACCOUNT),
    fetchProfiles(EMPTY_PROFILES),
    fetchSettings(DEFAULT_SETTINGS),
  ]);

  const { paper, live } = splitByMode(profiles.data.profiles);

  return (
    <div className="grid grid-cols-12 gap-2">
      <AccountBand
        className="col-span-12"
        performance={account.data.performance}
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
