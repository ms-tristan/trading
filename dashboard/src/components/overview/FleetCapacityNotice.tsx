import { cn } from "@/lib/cn";

/**
 * Why a profile waits for a worker.
 *
 * The fleet cap is an engine setting (`max_running_profiles` of
 * `GET /api/settings`) and the slots actually in use are published by
 * `GET /api/health`: the sentence below is derived from those two reads and
 * never from a number written in this file. A profile that waits for a slot is
 * normal operation - it starts on its own as soon as a worker exits - so the
 * notice states a fact instead of raising an alarm.
 */

/** Memory a single Freqtrade worker costs, in MiB: the reason the cap exists. */
const WORKER_MEMORY_MIB = 390;

export interface FleetCapacityNoticeProps {
  /**
   * `max_running_profiles` of `GET /api/settings`; `undefined` when the settings
   * could not be read, in which case the section count is derived from health.
   */
  maxRunningProfiles?: number;
  /** Slots in use and total, from `GET /api/health`; `NaN` when it could not be read. */
  engineSlotsUsed: number;
  engineSlotsTotal: number;
  /** Profiles of the mode section that are waiting for a slot. */
  waitingProfiles: number;
  /** Delay the supervisor keeps between two worker starts (`GET /api/settings`). */
  staggerSeconds?: number;
  className?: string;
}

/** Guards a settings value that may be absent or non-finite. */
export function positiveOrNull(value: number | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

/**
 * One short, always-visible sentence stating why profiles wait for a worker,
 * derived from the REAL settings: never a hard-coded 6 or 10.
 *
 * The first clause names the cap (falling back to the slot total of the health
 * payload, then to an honest "could not be read"), the second states the memory
 * cost that justifies it, and the third says that a wait is not a failure. The
 * stagger clause is appended only when the supervisor really publishes a delay:
 * it answers "why is nothing starting yet?", which is the question an operator
 * asks during a cold start.
 */
export function fleetCapacitySentence(props: FleetCapacityNoticeProps): string {
  const capacity =
    positiveOrNull(props.maxRunningProfiles) ?? positiveOrNull(props.engineSlotsTotal);
  const used = positiveOrNull(props.engineSlotsUsed);

  const opening =
    capacity !== null
      ? used !== null
        ? `The engine runs at most ${capacity} profiles at a time (${used} of ${capacity} slots in use).`
        : `The engine runs at most ${capacity} profiles at a time.`
      : `The engine runs a limited number of profiles at a time (the current cap could not be read).`;

  const reason = `The limit is deliberate: one Freqtrade worker costs about ${WORKER_MEMORY_MIB} MiB of memory.`;
  const reassurance = "A queued profile is not an error - it starts automatically as soon as a slot frees.";

  const stagger = positiveOrNull(props.staggerSeconds);
  const staggering =
    stagger !== null
      ? ` Workers start one at a time, ${stagger} seconds apart, so a cold start never overloads the host.`
      : "";

  return `${opening} ${reason} ${reassurance}${staggering}`;
}

/**
 * The fleet-capacity line of the overview.
 *
 * It is ordinary text, always rendered - never a tooltip and never a hover-only
 * affordance - and it reuses the shell of `LiveTradingNotice` so the two notices
 * of the page read as the same product. The waiting count is shown as soon as
 * one profile waits; it never changes the sentence itself, which describes the
 * engine and not the section.
 */
export function FleetCapacityNotice(props: FleetCapacityNoticeProps) {
  const { waitingProfiles, className } = props;
  const waiting = positiveOrNull(waitingProfiles) ?? 0;

  return (
    <div
      role="note"
      className={cn(
        "mb-2 min-w-0 rounded-lg border border-border bg-muted/40 p-3 text-base",
        className,
      )}
    >
      <p className="font-semibold">Fleet capacity</p>
      <p className="text-muted-foreground">{fleetCapacitySentence(props)}</p>
      <p className="mt-1 text-sm text-muted-foreground">
        {waiting > 0
          ? `${waiting} ${waiting === 1 ? "profile is" : "profiles are"} waiting for a slot in this section.`
          : "No profile of this section is waiting for a slot."}
      </p>
    </div>
  );
}
