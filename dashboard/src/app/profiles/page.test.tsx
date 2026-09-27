import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiProfile, apiProfiles, jsonResponse } from "@/components/profile/fixtures";
import { forgetLastKnown } from "@/lib/api";
import type { ProfileView } from "@/lib/types";

import ProfilesPage, { filterByStrategy, normaliseStrategyFilter } from "./page";

const RANKED = apiProfiles([
  apiProfile({
    id: "bravo",
    name: "Bravo",
    strategy: "momentum",
    strategy_title: "Momentum breakout",
    portfolio_value: 1300,
    profit_usdt: 300,
    profit_pct: 0.3,
  }),
  apiProfile({
    id: "alpha",
    name: "Alpha",
    strategy: "basic",
    strategy_title: "EMA cross baseline",
    portfolio_value: 1100,
    profit_usdt: 100,
    profit_pct: 0.1,
  }),
  apiProfile({
    id: "charlie",
    name: "Charlie",
    strategy: "MomentumStrategy",
    strategy_title: "Momentum breakout",
    portfolio_value: 950,
    state: "queued",
    state_reason: "waiting for an engine slot",
  }),
  apiProfile({
    id: "funded",
    name: "Funded BTC",
    strategy: "momentum",
    strategy_title: "Momentum breakout",
    mode: "live",
    portfolio_value: 5000,
  }),
]);

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input).split("?")[0];
    if (path === "/api/profiles") {
      return jsonResponse(RANKED);
    }
    return jsonResponse({ detail: `${path} not found` }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  forgetLastKnown();
});

afterEach(() => {
  vi.unstubAllGlobals();
  forgetLastKnown();
});

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

async function renderIndex(searchParams?: Record<string, string>) {
  const ui = await ProfilesPage(
    searchParams === undefined ? {} : { searchParams: Promise.resolve(searchParams) },
  );
  return render(ui);
}

describe("normaliseStrategyFilter", () => {
  it("trims the parameter and treats an empty one as no filter", () => {
    expect(normaliseStrategyFilter(" momentum ")).toBe("momentum");
    expect(normaliseStrategyFilter("")).toBeNull();
    expect(normaliseStrategyFilter("   ")).toBeNull();
    expect(normaliseStrategyFilter(undefined)).toBeNull();
  });

  it("takes the first value of a repeated parameter", () => {
    expect(normaliseStrategyFilter(["momentum", "basic"])).toBe("momentum");
    expect(normaliseStrategyFilter(["", "basic"])).toBeNull();
  });
});

describe("filterByStrategy", () => {
  // The helper reads one field of the view model; the paginated double above is
  // the wire payload, so these direct calls name the rows they filter.
  const rows = RANKED.profiles as unknown as ProfileView[];

  it("keeps every profile when there is no filter", () => {
    expect(filterByStrategy(rows, null)).toHaveLength(4);
  });

  it("matches the catalogue id and the freqtrade class name", () => {
    const filtered = filterByStrategy(rows, "momentum");

    expect(filtered.map((entry) => entry.id)).toEqual(["bravo", "charlie", "funded"]);
  });

  it("answers an empty list for a strategy nobody holds", () => {
    expect(filterByStrategy(rows, "donchian")).toEqual([]);
  });
});

describe("ProfilesPage", () => {
  it("ranks the profiles in the order the API returned them", async () => {
    await renderIndex();

    expect(screen.getByRole("heading", { level: 2, name: /Paper trading/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: /Real trading/ })).toBeInTheDocument();
    expect(screen.getByText(/4 profiles in the API ranking/)).toBeInTheDocument();

    const paperRows = within(screen.getAllByRole("table")[0]).getAllByRole("row");
    expect(paperRows).toHaveLength(4); // header + three paper profiles
    expect(paperRows[1]).toHaveTextContent("Bravo");
    expect(paperRows[2]).toHaveTextContent("Alpha");
    expect(paperRows[3]).toHaveTextContent("Charlie");
    expect(screen.getByRole("link", { name: "Bravo" })).toHaveAttribute("href", "/profiles/bravo");
  });

  it("shows the columns of the overview", async () => {
    await renderIndex();

    const table = screen.getAllByRole("table")[0];
    // The ranking columns of the overview are the sortable ones.
    for (const header of ["Profile", "Strategy", "Value", "Profit", "Profit %", "Win rate"]) {
      expect(within(table).getByRole("button", { name: `Sort by ${header}` })).toBeInTheDocument();
    }
    expect(within(table).getByText("Timeframe")).toBeInTheDocument();
    expect(within(table).getByText("Pair")).toBeInTheDocument();
    expect(within(table).getByText("State")).toBeInTheDocument();
    expect(within(table).getByText("Open / closed")).toBeInTheDocument();
    expect(within(table).getByText("Equity")).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "#" })).toBeInTheDocument();
  });

  it("narrows both sections to one strategy and links back to all of them", async () => {
    await renderIndex({ strategy: "momentum" });

    expect(screen.getByText(/Filtered on strategy/)).toBeInTheDocument();
    expect(screen.getByText("momentum")).toBeInTheDocument();
    expect(screen.getByText(/3 of 4 profiles/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Clear filter" })).toHaveAttribute(
      "href",
      "/profiles",
    );

    const paperRows = within(screen.getAllByRole("table")[0]).getAllByRole("row");
    expect(paperRows).toHaveLength(3); // header + Bravo + Charlie
    expect(paperRows[1]).toHaveTextContent("Bravo");
    expect(paperRows[2]).toHaveTextContent("Charlie");
    expect(paperRows[2]).toHaveTextContent("waiting for an engine slot");

    const realRows = within(screen.getAllByRole("table")[1]).getAllByRole("row");
    expect(realRows).toHaveLength(2); // header + Funded BTC
    expect(realRows[1]).toHaveTextContent("Funded BTC");
  });

  it("says so when a strategy is held by no profile", async () => {
    await renderIndex({ strategy: "donchian" });

    expect(screen.getAllByText("No paper profile holds the strategy donchian.")).toHaveLength(1);
    expect(screen.getAllByText("No live profile holds the strategy donchian.")).toHaveLength(1);
    expect(screen.getByText(/0 of 4 profiles/)).toBeInTheDocument();
  });

  it("renders the banner plus the last known ranking when the API goes down", async () => {
    await renderIndex();
    cleanup();

    fetchMock.mockRejectedValue(new Error("connect ECONNREFUSED"));
    await renderIndex();

    expect(screen.getByText("The profile ranking could not be refreshed")).toBeInTheDocument();
    expect(screen.getByText(/ECONNREFUSED/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Bravo" })).toBeInTheDocument();
  });

  it("renders an empty but well-formed page on a cold failure", async () => {
    fetchMock.mockRejectedValue(new Error("connect ECONNREFUSED"));

    await renderIndex();

    expect(screen.getByText("The profile ranking could not be refreshed")).toBeInTheDocument();
    expect(screen.getByText("No paper profile is configured yet.")).toBeInTheDocument();
    expect(screen.getByText("No live profile is configured yet.")).toBeInTheDocument();
    expect(screen.getByText(/0 profiles in the API ranking/)).toBeInTheDocument();
  });
});
