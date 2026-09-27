import { Card } from "@/components/ui/Card";
import { KpiStat } from "@/components/ui/KpiStat";
import { StateBadge } from "@/components/ui/StateBadge";
import { cn } from "@/lib/cn";
import { formatDuration, formatTimestamp } from "@/lib/format";
import { STATE_LABELS, isAttentionState } from "@/lib/states";
import type { HealthStatus, ProfileState, ProfileView } from "@/lib/types";

/**
 * Fallback of `GET /api/health`.
 *
 * It carries no figure at all (`NaN`, an empty version, no timestamp), so a cold
 * failure renders an honest "unknown" card instead of a wall of zeroes. Pages
 * that only need one health field - the profile page reads the process uptime -
 * hand it to `fetchWithFallback` as well.
 */
export const EMPTY_HEALTH: HealthStatus = {
  status: "unknown",
  version: "",
  uptime_seconds: Number.NaN,
  running_profiles: Number.NaN,
  max_running_profiles: Number.NaN,
  live_trading_enabled: false,
  kill_switch_engaged: false,
  generated_at: "",
};

/** Display order of the lifecycle states: the vocabulary of `@/lib/states`. */
export const STATE_ORDER = Object.keys(STATE_LABELS) as ProfileState[];

/** Number of profiles per lifecycle state; an unknown wire state is ignored. */
export function countByState(profiles: ProfileView[]): Record<ProfileState, number> {
  const counts: Record<ProfileState, number> = {
    running: 0,
    queued: 0,
    stopped: 0,
    error: 0,
    blocked: 0,
  };
  for (const profile of profiles) {
    const current = counts[profile.state];
    if (typeof current === "number") {
      counts[profile.state] = current + 1;
    }
  }
  return counts;
}

/** Border colour of the three answers `status` can carry. */
const STATUS_CLASS: Record<string, string> = {
  ok: "border-status-running",
  degraded: "border-status-queued",
};

/**
 * Render a counter: a missing figure renders as an em dash, never as `NaN`.
 *
 * Shared by the cards of this dashboard that print a number straight from the
 * payload, so no page ever shows `NaN` or an empty cell for it.
 */
export function formatCount(value: number): string {
  return Number.isFinite(value) ? String(value) : "\u2014";
}

export interface EngineStateCardProps {
  health: HealthStatus;
  /** The ranked profile list, counted per state. */
  profiles: ProfileView[];
  className?: string;
}

/**
 * State of the supervisor: slot usage, uptime, the platform gates and how many
 * profiles sit in each lifecycle state.
 *
 * The five states are always listed, including the empty ones: an operator
 * checks "is anything queued or in error?" at a glance, and a state that needs a
 * decision says so in words next to its badge, never through colour alone.
 */
export function EngineStateCard({ health, profiles, className }: EngineStateCardProps) {
  const counts = countByState(profiles);

  return (
    <Card
      className={className}
      headingLevel={2}
      title="Engine state"
      description="What the supervisor reports right now"
      actions={
        <span
          data-status={health.status}
          className={cn(
            "rounded-full border px-2 py-0.5 text-sm",
            STATUS_CLASS[health.status] ?? "border-border",
          )}
        >
          {health.status}
        </span>
      }
    >
      <div className="grid grid-cols-12 gap-2">
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-3"
          label="Engine slots"
          value={`${formatCount(health.running_profiles)} of ${formatCount(health.max_running_profiles)}`}
          hint="profiles holding a worker"
        />
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-3"
          label="Uptime"
          value={formatDuration(health.uptime_seconds)}
          hint="API process"
        />
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-3"
          label="Version"
          value={health.version === "" ? "\u2014" : health.version}
          hint={`generated ${formatTimestamp(health.generated_at)}`}
        />
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-3"
          label="Kill switch"
          value={health.kill_switch_engaged ? "Engaged" : "Released"}
          hint={
            health.kill_switch_engaged
              ? "no worker may start"
              : "workers may hold slots"
          }
        />
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-3"
          label="Live trading"
          value={health.live_trading_enabled ? "Enabled" : "Disabled"}
          hint="platform gate"
        />
      </div>

      <div className="mt-3 border-t border-border pt-2">
        <h3 className="text-base font-semibold">Profiles by state</h3>
        <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-2">
          {STATE_ORDER.map((state) => (
            <li key={state} className="flex min-w-0 items-center gap-2">
              <StateBadge state={state} />
              <span className="tabular-nums">{counts[state]}</span>
              {isAttentionState(state) && counts[state] > 0 ? (
                <span className="text-sm text-muted-foreground">needs attention</span>
              ) : null}
            </li>
          ))}
        </ul>
      </div>
    </Card>
  );
}
