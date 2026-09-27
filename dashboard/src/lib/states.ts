/**
 * Profile-state vocabulary shared by the badges, the ranking tables and the
 * overview sections.
 *
 * `state_label` is what the operator reads, `state_color_var` is the CSS custom
 * property (`globals.css`) the badge paints itself with, and the attention flag
 * marks the three states that need an operator decision.
 */

import type { ProfileState } from "./types";

/** Human label of every state. */
export const STATE_LABELS: Record<ProfileState, string> = {
  running: "Running",
  queued: "Queued",
  stopped: "Stopped",
  error: "Error",
  blocked: "Blocked",
};

/** CSS custom property carrying the colour of every state. */
export const STATE_COLOR_VARS: Record<ProfileState, string> = {
  running: "--status-running",
  queued: "--status-queued",
  stopped: "--status-stopped",
  error: "--status-error",
  blocked: "--status-blocked",
};

/** States that require an operator decision: waiting, failed or refused. */
export const ATTENTION_STATES: readonly ProfileState[] = ["queued", "error", "blocked"];

/** Label of `state`, with a defensive fallback for an unknown wire value. */
export function stateLabel(state: ProfileState): string {
  return STATE_LABELS[state] ?? "Unknown";
}

/** CSS custom property of `state`, with a defensive fallback. */
export function stateColorVar(state: ProfileState): string {
  return STATE_COLOR_VARS[state] ?? "--status-stopped";
}

/** `true` for `queued`, `error` and `blocked`. */
export function isAttentionState(state: ProfileState): boolean {
  return ATTENTION_STATES.includes(state);
}
