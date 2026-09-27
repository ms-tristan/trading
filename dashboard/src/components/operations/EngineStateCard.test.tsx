import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { health, profile } from "@/components/profile/fixtures";

import {
  EMPTY_HEALTH,
  EngineStateCard,
  STATE_ORDER,
  countByState,
  formatCount,
} from "./EngineStateCard";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("countByState", () => {
  it("counts the profiles of every state", () => {
    const counts = countByState([
      profile({ id: "a", state: "running" }),
      profile({ id: "b", state: "running" }),
      profile({ id: "c", state: "queued" }),
      profile({ id: "d", state: "error" }),
    ]);

    expect(counts).toEqual({ running: 2, queued: 1, stopped: 0, error: 1, blocked: 0 });
  });

  it("ignores a state the vocabulary does not know", () => {
    const counts = countByState([profile({ id: "a", state: "archived" as never })]);

    expect(counts.running).toBe(0);
    expect(Number.isNaN(counts.running)).toBe(false);
  });

  it("orders the states as the shared vocabulary does", () => {
    expect(STATE_ORDER).toEqual(["running", "queued", "stopped", "error", "blocked"]);
  });
});

describe("formatCount", () => {
  it("renders a missing figure as an em dash", () => {
    expect(formatCount(4)).toBe("4");
    expect(formatCount(Number.NaN)).toBe("\u2014");
    expect(formatCount(Number.POSITIVE_INFINITY)).toBe("\u2014");
  });
});

describe("EngineStateCard", () => {
  it("reports the slots, the uptime, the gates and the state counts", () => {
    render(
      <EngineStateCard
        health={health()}
        profiles={[
          profile({ id: "a", state: "running" }),
          profile({ id: "b", state: "queued", state_reason: "waiting for an engine slot" }),
          profile({ id: "c", state: "queued" }),
          profile({ id: "d", state: "stopped" }),
        ]}
      />,
    );

    expect(screen.getByRole("heading", { level: 2, name: "Engine state" })).toBeInTheDocument();
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect(screen.getByText("2 of 4")).toBeInTheDocument();
    expect(screen.getByText("1h 1m")).toBeInTheDocument();
    expect(screen.getByText("2026.8")).toBeInTheDocument();
    expect(screen.getByText("Released")).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();

    const states = screen.getByRole("heading", { level: 3, name: "Profiles by state" })
      .parentElement;
    expect(states).not.toBeNull();
    expect(within(states as HTMLElement).getByText("Running")).toBeInTheDocument();
    expect(within(states as HTMLElement).getAllByText("2").length).toBeGreaterThanOrEqual(1);
    expect(within(states as HTMLElement).getAllByText("needs attention")).toHaveLength(1);
    expect(within(states as HTMLElement).getByText("Blocked")).toBeInTheDocument();
  });

  it("reports an engaged kill switch and an enabled live gate", () => {
    render(
      <EngineStateCard
        health={health({ kill_switch_engaged: true, live_trading_enabled: true, status: "degraded" })}
        profiles={[]}
      />,
    );

    expect(screen.getByText("Engaged")).toBeInTheDocument();
    expect(screen.getByText("no worker may start")).toBeInTheDocument();
    expect(screen.getByText("Enabled")).toBeInTheDocument();
    expect(screen.getByText("degraded")).toBeInTheDocument();
  });

  it("reads the engine-slot KPI from the wire pair, not from the fleet size", () => {
    render(
      <EngineStateCard
        health={health({ profiles_running: 3, engine_slots_used: 3, engine_slots_total: 8 })}
        profiles={[profile({ id: "a", state: "running" })]}
      />,
    );

    expect(screen.getByText("3 of 8")).toBeInTheDocument();
    expect(screen.queryByText("3 of 3")).not.toBeInTheDocument();
    expect(screen.queryByText("0 of 8")).not.toBeInTheDocument();
  });

  it("renders an honest unknown card when the API answered nothing", () => {
    render(<EngineStateCard health={EMPTY_HEALTH} profiles={[]} />);

    // The fallback keeps the wire names of the slot pair, and both counters stay
    // unknown so the KPI renders em dashes instead of zeroes.
    expect(Object.keys(EMPTY_HEALTH)).toContain("engine_slots_used");
    expect(Object.keys(EMPTY_HEALTH)).toContain("engine_slots_total");
    expect(Number.isNaN(EMPTY_HEALTH.engine_slots_used)).toBe(true);
    expect(Number.isNaN(EMPTY_HEALTH.engine_slots_total)).toBe(true);

    expect(screen.getByText("unknown")).toBeInTheDocument();
    expect(screen.getAllByText("\u2014").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("\u2014 of \u2014")).toBeInTheDocument();
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
  });
});
