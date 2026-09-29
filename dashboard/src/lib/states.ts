/**
 * Profile-state vocabulary shared by the badges, the ranking tables and the
 * overview sections.
 *
 * `state_label` is what the operator reads, `state_color_var` is the CSS custom
 * property (`globals.css`) the badge paints itself with.
 *
 * The vocabulary separates two different kinds of "not running" profile:
 *
 * - `needsDecision`: the profile failed or was refused (`error`, `blocked`), so
 *   an operator has to act on it;
 * - the states that are simply not running yet (`stopped`).
 *
 * `isAttentionState` answers "this row is not running", which is what the row
 * tint and the fleet counters rely on.
 */

import type { ProfileState } from "./types";

/** Human label of every state. */
export const STATE_LABELS: Record<ProfileState, string> = {
  running: "Running",
  stopped: "Stopped",
  error: "Error",
  blocked: "Blocked",
};

/** CSS custom property carrying the colour of every state. */
export const STATE_COLOR_VARS: Record<ProfileState, string> = {
  running: "--status-running",
  stopped: "--status-stopped",
  error: "--status-error",
  blocked: "--status-blocked",
};

/** States that are not `running` or `stopped`: a profile that failed or was refused. */
export const ATTENTION_STATES: readonly ProfileState[] = ["error", "blocked"];

/** States that need an OPERATOR DECISION: failed or refused. */
export const STATES_NEEDING_DECISION: readonly ProfileState[] = ["error", "blocked"];

/** Label of `state`, with a defensive fallback for an unknown wire value. */
export function stateLabel(state: ProfileState): string {
  return STATE_LABELS[state] ?? "Unknown";
}

/** CSS custom property of `state`, with a defensive fallback. */
export function stateColorVar(state: ProfileState): string {
  return STATE_COLOR_VARS[state] ?? "--status-stopped";
}

/** `true` for `error` and `blocked`, i.e. every state but `running` and `stopped`. */
export function isAttentionState(state: ProfileState): boolean {
  return ATTENTION_STATES.includes(state);
}

/**
 * `true` for `error` and `blocked`.
 *
 * A defensive check (`.includes`) so an unknown wire state answers `false`.
 */
export function needsDecision(state: ProfileState): boolean {
  return STATES_NEEDING_DECISION.includes(state);
}
