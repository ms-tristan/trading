import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiProfile, apiProfiles, apiStrategy, jsonResponse } from "@/components/profile/fixtures";
import { forgetLastKnown } from "@/lib/api";

import StrategiesPage, { sumCatalogue } from "./page";

const CATALOGUE = {
  strategies: [
    apiStrategy({
      id: "momentum",
      title: "Momentum breakout",
      category: "trend",
      timeframe: "15m",
      timeframes: ["15m", "1h"],
      description: "Buys strength while price momentum stays positive.",
      indicators: ["Momentum(10)", "EMA(50)"],
      reference: "Momentum investing - https://www.investopedia.com/terms/m/momentum.asp",
      profiles_total: 2,
      profiles_running: 1,
      portfolio_value: 2300,
      profit_usdt: 250,
      profit_pct: 0.12,
      win_rate: 0.5,
    }),
    apiStrategy({
      id: "basic",
      title: "EMA cross baseline",
      category: "baseline",
      description: "Enters on the EMA cross.",
      indicators: ["EMA(12)"],
      profiles_total: 1,
      profiles_running: 1,
      portfolio_value: 1100,
      profit_usdt: 100,
      profit_pct: 0.1,
      win_rate: 0.5,
    }),
    // The API omitted the aggregate: the ranked profile list fills the count.
    apiStrategy({
      id: "donchian",
      title: "Donchian channel breakout",
      category: "breakout",
      description: "Turtle style breakout.",
      indicators: ["Donchian channel(20)"],
    }),
  ],
};

/** Ranked by portfolio value, descending, with the two spellings of an id. */
const RANKED = apiProfiles([
  apiProfile({
    id: "bravo",
    name: "Bravo",
    strategy: "momentum",
    strategy_title: "Momentum breakout",
    portfolio_value: 1300,
    profit_usdt: 300,
  }),
  apiProfile({
    id: "alpha",
    name: "Alpha",
    strategy: "basic",
    strategy_title: "EMA cross baseline",
    portfolio_value: 1100,
    profit_usdt: 100,
  }),
  apiProfile({
    id: "charlie",
    name: "Charlie",
    strategy: "MomentumStrategy",
    strategy_title: "Momentum breakout",
    portfolio_value: 1000,
    profit_usdt: -50,
  }),
  apiProfile({
    id: "delta",
    name: "Delta",
    strategy: "donchian",
    strategy_title: "Donchian channel breakout",
    portfolio_value: 900,
  }),
]);

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input).split("?")[0];
    if (path === "/api/strategies") {
      return jsonResponse(CATALOGUE);
    }
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

async function renderCatalogue() {
  return render(await StrategiesPage());
}

describe("sumCatalogue", () => {
  it("sums the finite values and ignores the unknown ones", () => {
    const views = [
      { portfolioValue: 100, profitUsdt: 10 },
      { portfolioValue: Number.NaN, profitUsdt: 5 },
      { portfolioValue: 50, profitUsdt: Number.NaN },
    ] as Parameters<typeof sumCatalogue>[0];

    expect(sumCatalogue(views, (view) => view.portfolioValue)).toBe(150);
    expect(sumCatalogue(views, (view) => view.profitUsdt)).toBe(15);
    expect(sumCatalogue([], (view) => view.portfolioValue)).toBe(0);
  });
});

describe("StrategiesPage", () => {
  it("renders one card per strategy with its catalogue metadata", async () => {
    await renderCatalogue();

    expect(
      screen.getByRole("heading", { level: 3, name: "Momentum breakout" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 3, name: "EMA cross baseline" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 3, name: "Donchian channel breakout" }),
    ).toBeInTheDocument();

    expect(screen.getByText("trend")).toBeInTheDocument();
    expect(screen.getByText("Momentum(10)")).toBeInTheDocument();
    expect(screen.getByText("EMA(12)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /investopedia/ })).toBeInTheDocument();
    expect(screen.getByText("Buys strength while price momentum stays positive.")).toBeInTheDocument();
  });

  it("aggregates the whole catalogue in its header", async () => {
    await renderCatalogue();

    expect(screen.getByRole("heading", { level: 2, name: /Strategy catalogue/ })).toBeInTheDocument();
    expect(screen.getByText(/3 strategies - 3,400\.00 USDT held by their profiles - \+350\.00 USDT/))
      .toBeInTheDocument();
  });

  it("links every card to the filtered profile index", async () => {
    await renderCatalogue();

    expect(screen.getByRole("link", { name: "View the profiles of Momentum breakout" }))
      .toHaveAttribute("href", "/profiles?strategy=momentum");
    expect(screen.getByRole("link", { name: "View the profiles of EMA cross baseline" }))
      .toHaveAttribute("href", "/profiles?strategy=basic");
    expect(screen.getByRole("link", { name: "View the profiles of Donchian channel breakout" }))
      .toHaveAttribute("href", "/profiles?strategy=donchian");
  });

  it("shows the best profile of every strategy from the ranked list", async () => {
    await renderCatalogue();

    // The first match of the ranking, not the richest of the two.
    expect(screen.getByRole("link", { name: "Bravo" })).toHaveAttribute("href", "/profiles/bravo");
    expect(screen.getByRole("link", { name: "Alpha" })).toHaveAttribute("href", "/profiles/alpha");
    expect(screen.getByRole("link", { name: "Delta" })).toHaveAttribute("href", "/profiles/delta");
    expect(screen.getAllByText(/Best profile:/)).toHaveLength(3);
  });

  it("derives a missing profile count from the ranked list", async () => {
    await renderCatalogue();

    // `donchian` carries no aggregate: the one profile that holds it is counted.
    const card = screen
      .getByRole("heading", { level: 3, name: "Donchian channel breakout" })
      .closest("section");
    expect(card).not.toBeNull();
    expect(card).toHaveTextContent("1");
    expect(card).toHaveTextContent("Profiles");
  });

  it("renders the banner plus the last known catalogue when the API goes down", async () => {
    await renderCatalogue();
    cleanup();

    fetchMock.mockRejectedValue(new Error("connect ECONNREFUSED"));
    await renderCatalogue();

    expect(screen.getByText("The strategy catalogue could not be refreshed")).toBeInTheDocument();
    expect(screen.getByText("The best profile of each strategy could not be refreshed"))
      .toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 3, name: "Momentum breakout" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View the profiles of Momentum breakout" }))
      .toBeInTheDocument();
  });

  it("renders an empty but well-formed page on a cold failure", async () => {
    fetchMock.mockRejectedValue(new Error("connect ECONNREFUSED"));

    await renderCatalogue();

    expect(screen.getByText("The API publishes no strategy yet.")).toBeInTheDocument();
    expect(screen.getByText(/0 strategies/)).toBeInTheDocument();
  });

  it("renders the empty state of an empty catalogue", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input).split("?")[0];
      if (path === "/api/strategies") {
        return jsonResponse({ generated_at: "", strategies: [] });
      }
      return jsonResponse(RANKED);
    });

    await renderCatalogue();

    expect(screen.getByText("The API publishes no strategy yet.")).toBeInTheDocument();
  });
});
