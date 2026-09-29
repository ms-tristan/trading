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
      profile({ id: "c", state: "stopped" }),
      profile({ id: "d", state: "error" }),
    ]);

    expect(counts).toEqual({ running: 2, stopped: 1, error: 1, blocked: 0 });
  });

  it("ignores a state the vocabulary does not know", () => {
    const counts = countByState([profile({ id: "a", state: "archived" as never })]);

    expect(counts.running).toBe(0);
    expect(Number.isNaN(counts.running)).toBe(false);
  });

  it("orders the states as the shared vocabulary does", () => {
    expect(STATE_ORDER).toEqual(["running", "stopped", "error", "blocked"]);
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
  it("reports the uptime, the gates and the state counts", () => {
    render(
      <EngineStateCard
        health={health()}
        profiles={[
          profile({ id: "a", state: "running" }),
          profile({ id: "b", state: "stopped", state_reason: "not started" }),
          profile({ id: "c", state: "stopped" }),
          profile({ id: "d", state: "stopped" }),
          profile({ id: "e", state: "error", state_reason: "worker exited with code 1" }),
        ]}
      />,
    );

    expect(screen.getByRole("heading", { level: 2, name: "Engine state" })).toBeInTheDocument();
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect(screen.getByText("1h 1m")).toBeInTheDocument();
    expect(screen.getByText("2026.8")).toBeInTheDocument();
    expect(screen.getByText("Released")).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();

    const states = screen.getByRole("heading", { level: 3, name: "Profiles by state" })
      .parentElement;
    expect(states).not.toBeNull();
    expect(within(states as HTMLElement).getByText("Running")).toBeInTheDocument();
    expect(within(states as HTMLElement).getAllByText("3").length).toBeGreaterThanOrEqual(1);
    // The one error profile is the only entry that needs a decision.
    expect(within(states as HTMLElement).getByText("needs attention")).toBeInTheDocument();
    expect(within(states as HTMLElement).queryByText("needing attention")).not.toBeInTheDocument();
    expect(within(states as HTMLElement).getByText("Blocked")).toBeInTheDocument();
  });

  it("lists exactly the four lifecycle states and never a waiting label", () => {
    render(
      <EngineStateCard
        health={health()}
        profiles={[
          profile({ id: "a", state: "stopped" }),
          profile({ id: "b", state: "stopped" }),
          profile({ id: "c", state: "error", state_reason: "worker exited with code 1" }),
        ]}
      />,
    );

    const states = screen.getByRole("heading", { level: 3, name: "Profiles by state" })
      .parentElement;
    expect(states).not.toBeNull();
    const list = states as HTMLElement;
    expect(within(list).getAllByText("needs attention")).toHaveLength(1);
    expect(within(list).queryByText(/waiting/)).not.toBeInTheDocument();
    expect(within(list).queryByText("Queued")).not.toBeInTheDocument();
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
  });

  it("states no capacity of the fleet", () => {
    render(<EngineStateCard health={health()} profiles={[]} />);

    expect(screen.queryByText("Engine slots")).not.toBeInTheDocument();
    expect(screen.queryByText("profiles holding a worker")).not.toBeInTheDocument();
    expect(screen.queryByText(/max_running_profiles/)).not.toBeInTheDocument();
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

  it("renders an honest unknown card when the API answered nothing", () => {
    render(<EngineStateCard health={EMPTY_HEALTH} profiles={[]} />);

    // The fallback keeps the wire names of the counters the card renders, and
    // the fleet size stays unknown so the page shows no invented number.
    expect(Object.keys(EMPTY_HEALTH)).toContain("profiles_running");
    expect(Object.keys(EMPTY_HEALTH)).not.toContain("engine_slots_used");
    expect(Number.isNaN(EMPTY_HEALTH.profiles_running)).toBe(true);

    expect(screen.getByText("unknown")).toBeInTheDocument();
    expect(screen.getAllByText("\u2014").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
  });
});
