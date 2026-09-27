import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { apiProfile, apiProfiles, profile } from "@/components/profile/fixtures";
import { toProfilesResponse } from "@/lib/api-wire";

import { SlotUsageSection, slotUsageRows } from "./SlotUsageSection";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("slotUsageRows", () => {
  it("ranks the running profiles by slot, then the queued ones in API order", () => {
    const rows = slotUsageRows([
      profile({ id: "bravo", name: "Bravo", state: "running", engine_slot: 2 }),
      profile({ id: "queued-one", name: "Queued one", state: "queued", state_reason: "cap reached" }),
      profile({ id: "alpha", name: "Alpha", state: "running", engine_slot: 1 }),
      profile({ id: "stopped-one", name: "Stopped one", state: "stopped" }),
      profile({ id: "queued-two", name: "Queued two", state: "queued" }),
    ]);

    expect(rows.map((row) => row.profile.id)).toEqual([
      "alpha",
      "bravo",
      "queued-one",
      "queued-two",
    ]);
    expect(rows.map((row) => row.slot)).toEqual([1, 2, null, null]);
  });

  it("keeps a running profile without a slot at the end of the running group", () => {
    const rows = slotUsageRows([
      profile({ id: "no-slot", name: "No slot", state: "running", engine_slot: null }),
      profile({ id: "slotted", name: "Slotted", state: "running", engine_slot: 4 }),
    ]);

    expect(rows.map((row) => row.profile.id)).toEqual(["slotted", "no-slot"]);
  });
});

describe("SlotUsageSection", () => {
  /**
   * The rows are the wire payload of `GET /api/profiles` mapped through
   * `@/lib/api-wire`: the table is pinned on the field names the API serves
   * (`slot`, `worker_port`), not on a hand-written view model.
   */
  const PROFILES = toProfilesResponse(
    apiProfiles([
      apiProfile({
        id: "alpha",
        name: "Alpha",
        state: "running",
        engine_slot: 1,
        api_port: 8081,
        strategy_title: "EMA cross baseline",
        portfolio_value: 1040,
      }),
      apiProfile({
        id: "bravo",
        name: "Bravo",
        state: "running",
        strategy_title: "Momentum breakout",
        portfolio_value: 990,
      }),
      apiProfile({
        id: "charlie",
        name: "Charlie",
        state: "queued",
        state_reason: "waiting for an engine slot",
        strategy_title: "Momentum breakout",
        portfolio_value: 950,
      }),
    ]),
  ).profiles;

  it("lists who holds a slot and who waits for one, with the reason", () => {
    render(<SlotUsageSection profiles={PROFILES} />);

    expect(screen.getByRole("heading", { level: 2, name: /Engine slots/ })).toBeInTheDocument();
    expect(screen.getByText(/2 running, 1 queued/)).toBeInTheDocument();

    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(rows).toHaveLength(4);
    expect(rows[1]).toHaveTextContent("#1");
    expect(rows[1]).toHaveTextContent("Alpha");
    expect(rows[1]).toHaveTextContent("8081");
    expect(rows[2]).toHaveTextContent("Bravo");
    expect(rows[3]).toHaveTextContent("queued");
    expect(rows[3]).toHaveTextContent("Charlie");
    expect(rows[3]).toHaveTextContent("waiting for an engine slot");
    expect(screen.getByRole("link", { name: "Alpha" })).toHaveAttribute(
      "href",
      "/profiles/alpha",
    );
  });

  it("keeps the em dash for the port of a running profile that holds none", () => {
    render(<SlotUsageSection profiles={PROFILES} />);

    const rows = within(screen.getByRole("table")).getAllByRole("row");
    // Bravo runs but the engine published neither a slot nor a worker port.
    expect(rows[2]).toHaveTextContent("queued");
    expect(rows[2]).toHaveTextContent("\u2014");
    expect(rows[2]).not.toHaveTextContent("#");
  });

  it("keeps the port column em-dashed for a profile without a port", () => {
    const rows = toProfilesResponse(
      apiProfiles([
        apiProfile({ id: "alpha", name: "Alpha", state: "running", engine_slot: 1 }),
      ]),
    ).profiles;

    render(<SlotUsageSection profiles={rows} />);

    const table = within(screen.getByRole("table")).getAllByRole("row");
    expect(table[1]).toHaveTextContent("#1");
    expect(table[1]).toHaveTextContent("\u2014");
    expect(table[1]).not.toHaveTextContent("8081");
  });

  it("says so when no profile holds or waits for a slot", () => {
    render(<SlotUsageSection profiles={[profile({ id: "stopped-one", state: "stopped" })]} />);

    expect(screen.getByText("No profile holds or waits for an engine slot.")).toBeInTheDocument();
    expect(screen.getByText(/0 running, 0 queued/)).toBeInTheDocument();
  });
});
