import { AccountBand } from "@/components/overview/AccountBand";
import { FleetCapacityNotice, fleetCapacitySentence } from "@/components/overview/FleetCapacityNotice";
import { LiveTradingNotice } from "@/components/overview/LiveTradingNotice";
import { ModeSection } from "@/components/overview/ModeSection";
import { EMPTY_HEALTH, formatCount } from "@/components/operations/EngineStateCard";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { fetchAccount, fetchHealth, fetchProfiles, fetchSettings } from "@/lib/api";
import { splitByMode } from "@/lib/ranking";
import {
  DEFAULT_SETTINGS,
  EMPTY_ACCOUNT,
  EMPTY_PROFILES,
  type AccountPerformance,
  type ApiWindow,
  type ProfileView,
} from "@/lib/types";

/** Live data on every request: the overview is never prerendered. */
export const dynamic = "force-dynamic";

const WINDOWS: readonly ApiWindow[] = ["24h", "7d", "30d", "all"];

/**
 * What the capacity line says when `GET /api/health` could not be read: the
 * overview still states the count of waiting profiles, but never a cap, a slot
 * pair or a `NaN` it does not know.
 */
export const CAPACITY_UNKNOWN = "Unknown at the moment";

/** Profiles of one section that are eligible but hold no engine slot yet. */
function waitingForSlot(profiles: ProfileView[]): number {
  return profiles.filter((profile) => profile.state === "queued").length;
}

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
 * Both sections also state, as plain always-visible text, why a profile waits for
 * a worker: the fleet cap of `GET /api/settings`, the slots in use of
 * `GET /api/health` and the count of waiting profiles of the section. The cap is
 * never written in this file - it is read from the API - and a failed health read
 * degrades the line to `CAPACITY_UNKNOWN` instead of printing a number it does
 * not have.
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
  // pipeline asserts on - and so is the cap this page states under each section
  // header, since `GET /api/settings` publishes `max_running_profiles` and the
  // page now reads both endpoints itself.
  const performance: AccountPerformance = {
    ...account.data.performance,
    engine_slots_used: health.data.engine_slots_used,
    engine_slots_total: health.data.engine_slots_total,
  };

  const capacityUnknown = health.error !== null;

  /**
   * The capacity line under a section header: the derived sentence plus the
   * figures of this section. A failed health read degrades the figures to
   * `CAPACITY_UNKNOWN` instead of printing a cap, a slot pair or a `NaN`.
   */
  function capacitySummary(section: ProfileView[]): string {
    const waiting = waitingForSlot(section);
    const waitingPart = `${waiting} of ${section.length} profiles in this section waiting for a slot.`;
    if (capacityUnknown) {
      return `Fleet capacity: ${CAPACITY_UNKNOWN}. ${waitingPart}`;
    }
    const cap =
      settings.data.max_running_profiles === undefined
        ? ""
        : ` (cap ${formatCount(settings.data.max_running_profiles)})`;
    return `${fleetCapacitySentence({
      maxRunningProfiles: settings.data.max_running_profiles,
      engineSlotsUsed: health.data.engine_slots_used,
      engineSlotsTotal: health.data.engine_slots_total,
      waitingProfiles: waiting,
      staggerSeconds: settings.data.worker_start_stagger_seconds,
    })} Fleet capacity: ${formatCount(health.data.engine_slots_used)} of ${formatCount(
      health.data.engine_slots_total,
    )} slots in use${cap}, ${waitingPart}`;
  }

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
      <ErrorBanner
        className="col-span-12"
        error={health.error}
        title="The fleet capacity could not be refreshed"
      />

      <ModeSection
        className="col-span-12"
        id="paper-trading"
        title="Paper trading"
        profiles={paper}
        emptyMessage="No paper profile is configured yet."
        summary={capacitySummary(paper)}
        notice={
          <FleetCapacityNotice
            maxRunningProfiles={settings.data.max_running_profiles}
            engineSlotsUsed={health.data.engine_slots_used}
            engineSlotsTotal={health.data.engine_slots_total}
            waitingProfiles={waitingForSlot(paper)}
            staggerSeconds={settings.data.worker_start_stagger_seconds}
          />
        }
      />

      <ModeSection
        className="col-span-12"
        id="real-trading"
        title="Real trading"
        profiles={live}
        emptyMessage="No live profile is configured yet."
        summary={capacitySummary(live)}
        notice={
          <>
            <LiveTradingNotice
              allowLiveTrading={settings.error ? null : settings.data.allow_live_trading}
              profiles={live}
            />
            <FleetCapacityNotice
              maxRunningProfiles={settings.data.max_running_profiles}
              engineSlotsUsed={health.data.engine_slots_used}
              engineSlotsTotal={health.data.engine_slots_total}
              waitingProfiles={waitingForSlot(live)}
              staggerSeconds={settings.data.worker_start_stagger_seconds}
            />
          </>
        }
      />
    </div>
  );
}
