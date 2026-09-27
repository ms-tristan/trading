import { cn } from "@/lib/cn";
import { isAttentionState, stateColorVar, stateLabel } from "@/lib/states";
import type { ProfileState } from "@/lib/types";

export interface StateBadgeProps {
  state: ProfileState;
  /** `state_reason` of the profile; rendered as visible text and as `title`. */
  reason?: string | null;
  className?: string;
}

/**
 * Lifecycle badge of one profile.
 *
 * The colour comes from the `--status-*` custom property of the state and is
 * carried by the outline and the marker, never by the label text: three of the
 * five status colours sit below 4.5:1 on `--card`, so the wording is always
 * rendered in the card foreground and works on its own. `state_reason` is
 * visible text as well as the `title` attribute.
 */
export function StateBadge({ state, reason, className }: StateBadgeProps) {
  const label = stateLabel(state);
  const colorVar = stateColorVar(state);
  const explanation = reason && reason.trim() !== "" ? reason : label;

  return (
    <span
      className={cn("inline-flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1", className)}
      data-state={state}
      data-attention={isAttentionState(state) ? "true" : "false"}
    >
      <span
        className="inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-sm font-medium text-card-foreground transition-smooth"
        style={{ borderColor: `var(${colorVar})` }}
        title={explanation}
      >
        <span aria-hidden="true" style={{ color: `var(${colorVar})` }}>
          {"\u25CF"}
        </span>
        <span>{label}</span>
      </span>
      <span className="min-w-0 text-sm text-muted-foreground" title={explanation}>
        {reason && reason.trim() !== "" ? reason : null}
      </span>
    </span>
  );
}
