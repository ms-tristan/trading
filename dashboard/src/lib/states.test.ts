import { describe, expect, it } from "vitest";

import {
  ATTENTION_STATES,
  STATES_NEEDING_DECISION,
  STATE_COLOR_VARS,
  STATE_LABELS,
  WAITING_STATES,
  isAttentionState,
  isWaitingForSlot,
  needsDecision,
  stateColorVar,
  stateLabel,
} from "./states";
import type { ProfileState } from "./types";

const STATES: ProfileState[] = ["running", "queued", "stopped", "error", "blocked"];

describe("state vocabulary", () => {
  it("labels the five documented states", () => {
    expect(STATE_LABELS).toEqual({
      running: "Running",
      queued: "Queued",
      stopped: "Stopped",
      error: "Error",
      blocked: "Blocked",
    });
  });

  it("maps every state to its --status-* custom property", () => {
    expect(STATE_COLOR_VARS).toEqual({
      running: "--status-running",
      queued: "--status-queued",
      stopped: "--status-stopped",
      error: "--status-error",
      blocked: "--status-blocked",
    });
  });

  it("marks queued, error and blocked as attention states", () => {
    expect(ATTENTION_STATES).toEqual(["queued", "error", "blocked"]);
    expect(STATES.filter(isAttentionState)).toEqual(["queued", "error", "blocked"]);
  });

  it("separates the states needing a decision from the states waiting for a slot", () => {
    expect(STATES_NEEDING_DECISION).toEqual(["error", "blocked"]);
    expect(WAITING_STATES).toEqual(["queued"]);
    expect(STATES.filter(needsDecision)).toEqual(["error", "blocked"]);
    expect(STATES.filter(isWaitingForSlot)).toEqual(["queued"]);
  });

  it("never flags a queued profile as needing a decision", () => {
    expect(needsDecision("queued")).toBe(false);
    expect(isWaitingForSlot("queued")).toBe(true);
    expect(needsDecision("error")).toBe(true);
    expect(needsDecision("blocked")).toBe(true);
    expect(isWaitingForSlot("error")).toBe(false);
    expect(isWaitingForSlot("blocked")).toBe(false);
    expect(needsDecision("running")).toBe(false);
    expect(isWaitingForSlot("running")).toBe(false);
  });

  it("exposes label and colour of every state", () => {
    for (const state of STATES) {
      expect(stateLabel(state)).toBe(STATE_LABELS[state]);
      expect(stateColorVar(state)).toBe(STATE_COLOR_VARS[state]);
    }
  });

  it("falls back to a readable label and a colour for an unknown state", () => {
    const unknown = "legacy" as ProfileState;
    expect(stateLabel(unknown)).toBe("Unknown");
    expect(stateColorVar(unknown)).toBe("--status-stopped");
    expect(isAttentionState(unknown)).toBe(false);
    expect(needsDecision(unknown)).toBe(false);
    expect(isWaitingForSlot(unknown)).toBe(false);
  });
});
