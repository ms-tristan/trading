import { describe, expect, it } from "vitest";

import {
  ATTENTION_STATES,
  STATES_NEEDING_DECISION,
  STATE_COLOR_VARS,
  STATE_LABELS,
  isAttentionState,
  needsDecision,
  stateColorVar,
  stateLabel,
} from "./states";
import type { ProfileState } from "./types";

const STATES: ProfileState[] = ["running", "stopped", "error", "blocked"];

describe("state vocabulary", () => {
  it("labels the four documented states", () => {
    expect(STATE_LABELS).toEqual({
      running: "Running",
      stopped: "Stopped",
      error: "Error",
      blocked: "Blocked",
    });
  });

  it("maps every state to its --status-* custom property", () => {
    expect(STATE_COLOR_VARS).toEqual({
      running: "--status-running",
      stopped: "--status-stopped",
      error: "--status-error",
      blocked: "--status-blocked",
    });
  });

  it("no longer knows a queued state", () => {
    expect(Object.keys(STATE_LABELS)).not.toContain("queued");
    expect(Object.keys(STATE_COLOR_VARS)).not.toContain("queued");
    expect(Object.keys(STATE_LABELS)).toEqual(STATES);
  });

  it("marks error and blocked as attention states", () => {
    expect(ATTENTION_STATES).toEqual(["error", "blocked"]);
    expect(STATES.filter(isAttentionState)).toEqual(["error", "blocked"]);
  });

  it("separates the states needing a decision from the states that are idle", () => {
    expect(STATES_NEEDING_DECISION).toEqual(["error", "blocked"]);
    expect(STATES.filter(needsDecision)).toEqual(["error", "blocked"]);
  });

  it("flags error and blocked as decisions and nothing else", () => {
    expect(needsDecision("error")).toBe(true);
    expect(needsDecision("blocked")).toBe(true);
    expect(needsDecision("running")).toBe(false);
    expect(needsDecision("stopped")).toBe(false);
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
  });
});
