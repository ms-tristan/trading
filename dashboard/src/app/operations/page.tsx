import {
  EMPTY_HEALTH,
  EngineStateCard,
  formatCount,
} from "@/components/operations/EngineStateCard";
import { EventsTable } from "@/components/operations/EventsTable";
import { OperationsConsole } from "@/components/operations/OperationsConsole";
import { SlotUsageSection } from "@/components/operations/SlotUsageSection";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { SectionHeader } from "@/components/ui/SectionHeader";
import { fetchEvents, fetchHealth, fetchProfiles, fetchSettings } from "@/lib/api";
import {
  DEFAULT_SETTINGS,
  EMPTY_EVENTS,
  EMPTY_PROFILES,
  EVENTS_LIMIT,
  type HealthStatus,
} from "@/lib/types";

/** Live data on every request: the console is never prerendered. */
export const dynamic = "force-dynamic";

/**
 * Operations page.
 *
 * Top to bottom: the engine state of `GET /api/health` (slots, uptime, gates and
 * the profiles counted per lifecycle state), the ranked slot usage - who holds a
 * worker, who waits for one and why - the two mutating controls (the settings
 * editor and the kill switch, both behind one operator token) and the 50 newest
 * rows of the engine journal.
 *
 * All four reads go through the mapped fetchers of `@/lib/api`: a failing API
 * above the last known payload, and every mutating control surfaces the answer of
 * the API - a `401` or a `403` included - through the same banner.
 */
export default async function OperationsPage() {
  const [health, profiles, settings, events] = await Promise.all([
    fetchHealth(EMPTY_HEALTH),
    fetchProfiles(EMPTY_PROFILES),
    fetchSettings(DEFAULT_SETTINGS),
    fetchEvents(EVENTS_LIMIT, EMPTY_EVENTS),
  ]);

  const killSwitchEngaged =
    health.data.kill_switch_engaged || settings.data.kill_switch_engaged === true;
  const version = health.data.version === "" ? null : health.data.version;
  const subtitle = [
    `engine ${health.data.status}`,
    version === null ? null : `version ${version}`,
    `${formatCount(health.data.engine_slots_used)} of ${formatCount(
      health.data.engine_slots_total,
    )} engine slots used`,
    `${profiles.data.profiles.length} ${
      profiles.data.profiles.length === 1 ? "profile" : "profiles"
    } known`,
  ]
    .filter((part): part is string => part !== null)
    .join(" - ");

  // The live-trading gate is published by `GET /api/settings`, not by
  // `GET /api/health`: the card shows the gate the settings endpoint reports.
  const engineState: HealthStatus = {
    ...health.data,
    live_trading_enabled: settings.data.allow_live_trading,
  };

  return (
    <div className="grid grid-cols-12 gap-2">
      <SectionHeader className="col-span-12" title="Operations" subtitle={subtitle} />

      <ErrorBanner
        className="col-span-12"
        error={health.error}
        title="The engine state could not be refreshed"
      />
      <ErrorBanner
        className="col-span-12"
        error={profiles.error}
        title="The slot usage could not be refreshed"
      />
      <ErrorBanner
        className="col-span-12"
        error={settings.error}
        title="The platform settings could not be refreshed"
      />
      <ErrorBanner
        className="col-span-12"
        error={events.error}
        title="The event log could not be refreshed"
      />

      <EngineStateCard
        className="col-span-12"
        health={engineState}
        profiles={profiles.data.profiles}
      />

      <SlotUsageSection className="col-span-12" profiles={profiles.data.profiles} />

      <OperationsConsole
        className="col-span-12"
        settings={settings.data}
        killSwitchEngaged={killSwitchEngaged}
      />

      <EventsTable className="col-span-12" events={events.data.events} />
    </div>
  );
}
