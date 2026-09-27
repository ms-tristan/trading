import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiDetail,
  apiHealth,
  apiProfile,
  apiProfiles,
  jsonResponse,
  tradeRow,
} from "@/components/profile/fixtures";
import { forgetLastKnown } from "@/lib/api";

import ProfileDetailPage from "./page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn() }),
}));

/**
 * One detail payload, as `GET /api/profiles/{id}` serves it: the state-database
 * daily series, an open trade, a closed trade and the three worker fields of the
 * profile (`sparkline`, `slot`, `worker_port`).
 */
const DETAIL = apiDetail({
  profile: {
    id: "alpha",
    name: "Alpha BTC 5m",
    strategy: "basic",
    strategy_title: "EMA cross baseline",
    timeframe: "5m",
    pairs: ["BTC/USDT"],
    state: "running",
    portfolio_value: 1040,
    initial_capital: 1000,
    profit_usdt: 40,
    profit_pct: 0.04,
    open_trades: 1,
    closed_trades: 6,
    updated_at: "2026-09-27T12:00:00Z",
    rank: 2,
    cash: 40,
    positions_value: 1000,
    engine_slot: 1,
    api_port: 8081,
    sparkline: [1000, 1040],
  },
  equity_curve: [
    { timestamp: "2026-09-26T12:00:00Z", portfolio_value: 1000, profit_usdt: 0, profit_pct: 0 },
    { timestamp: "2026-09-27T12:00:00Z", portfolio_value: 1040, profit_usdt: 40, profit_pct: 0.04 },
  ],
  daily_profit: [
    { date: "2026-09-25", profit_usdt: -5, trades: 1 },
    { date: "2026-09-26", profit_usdt: 15, trades: 2 },
  ],
  open_trades: [tradeRow({ trade_id: 12 })],
  recent_trades: [
    tradeRow({
      trade_id: 41,
      is_open: false,
      close_date: "2026-09-27T11:00:00Z",
      close_rate: 61000,
      exit_reason: "roi",
      profit_abs: 25,
      profit_pct: 2.5,
    }),
  ],
  strategy: {
    id: "basic",
    title: "EMA cross baseline",
    category: "baseline",
    description: "Enters on the EMA cross and exits on the opposite cross.",
    indicators: ["EMA(12)", "EMA(26)"],
    timeframes: ["1h", "4h"],
    pairs: ["BTC/USDT"],
    reference:
      "Freqtrade strategy customization guide - https://www.freqtrade.io/en/stable/strategy-customization/",
  },
});

/** Ranked by portfolio value, descending: the ranking the rank column reads. */
const PROFILES = apiProfiles([
  apiProfile({ id: "bravo", name: "Bravo", portfolio_value: 1300 }),
  apiProfile({ id: "alpha", name: "Alpha BTC 5m", portfolio_value: 1040 }),
]);

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input).split("?")[0];
    if (path === "/api/health") {
      return jsonResponse(apiHealth());
    }
    if (path === "/api/profiles/alpha") {
      return jsonResponse(DETAIL);
    }
    if (path === "/api/profiles") {
      return jsonResponse(PROFILES);
    }
    if (path.startsWith("/api/profiles/")) {
      return jsonResponse({ detail: `profile ${path} not found` }, 404);
    }
    return jsonResponse({ detail: `${path} not found` }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  // the last-known-value cache is module state: it must not leak between tests
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

async function renderDetail(id = "alpha", searchParams: Record<string, string> = {}) {
  const ui = await ProfileDetailPage({
    params: Promise.resolve({ id }),
    searchParams: Promise.resolve(searchParams),
  });
  return render(ui);
}

describe("ProfileDetailPage", () => {
  it("leads with the identity of the profile and its controls", async () => {
    await renderDetail();

    expect(
      screen.getByRole("heading", { level: 1, name: "Alpha BTC 5m" }),
    ).toBeInTheDocument();
    // The badge of the header and the state row of the configuration card.
    expect(screen.getAllByText("Running").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart" })).toBeInTheDocument();
    // The profile row publishes the engine slot of the worker and its private
    // REST port: the identity header prints both.
    const header = screen.getByRole("heading", { level: 1, name: "Alpha BTC 5m" }).closest(
      "section",
    );
    expect(header).not.toBeNull();
    expect(header).toHaveTextContent("engine slot #1");
    expect(header).toHaveTextContent("freqtrade API port 8081");
  });

  it("renders the engine slot and the REST port of the profile only once", async () => {
    await renderDetail();

    // The header line and the "API port" row of the resolved configuration.
    expect(screen.getAllByText(/8081/)).toHaveLength(2);
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
  });

  it("ranks the profile through the ranked list and reads the engine uptime", async () => {
    await renderDetail();

    expect(screen.getByText("#2")).toBeInTheDocument();
    expect(screen.getByText("1h 1m")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/profiles", expect.anything());
    expect(fetchMock).toHaveBeenCalledWith("/api/health", expect.anything());
  });

  it("renders the KPI row, the equity curve, the daily bars and both trade tables", async () => {
    await renderDetail();

    // The KPI row and the summary table of the equity chart both show the value.
    expect(screen.getAllByText("1,040.00 USDT").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("40.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("+40.00 USDT")).toBeInTheDocument();
    // The profit KPI and the change column of the equity summary table.
    expect(screen.getAllByText("+4.00%").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("1 / 6")).toBeInTheDocument();

    expect(
      screen.getByRole("img", { name: /Portfolio value of alpha over 24h/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("2026-09-26")).toBeInTheDocument();

    expect(screen.getByRole("heading", { level: 2, name: "Open trades" })).toBeInTheDocument();
    expect(screen.getByText("#12")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "Recent closed trades" }),
    ).toBeInTheDocument();
    expect(screen.getByText("#41")).toBeInTheDocument();
    expect(screen.getByText("roi")).toBeInTheDocument();
  });

  it("lists every day of the daily series, profit and trade count included", async () => {
    await renderDetail();

    // The days used to be published under `profit_abs`/`trades`; the state
    // database serves `abs_profit`/`trade_count`, and the table lists both days.
    const card = screen.getByRole("heading", { level: 2, name: "Daily profit" }).closest(
      "section",
    );
    expect(card).not.toBeNull();
    const rows = within(within(card as HTMLElement).getByRole("table")).getAllByRole("row");

    expect(rows).toHaveLength(3); // header + two days
    expect(rows[1]).toHaveTextContent("2026-09-25");
    expect(rows[2]).toHaveTextContent("2026-09-26");
    // The two numeric cells of every day: profit and trade count.
    const losingDay = within(rows[1]).getAllByRole("cell");
    expect(losingDay[0]).toHaveTextContent("-5.00 USDT");
    expect(losingDay[1]).toHaveTextContent("1");
    const winningDay = within(rows[2]).getAllByRole("cell");
    expect(winningDay[0]).toHaveTextContent("+15.00 USDT");
    expect(winningDay[1]).toHaveTextContent("2");
  });

  it("says exactly what is missing when both trade lists are empty", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input).split("?")[0];
      if (path === "/api/health") {
        return jsonResponse(apiHealth());
      }
      if (path === "/api/profiles/alpha") {
        return jsonResponse(apiDetail({ profile: { id: "alpha", name: "Alpha" } }));
      }
      return jsonResponse({ generated_at: "", profiles: [] });
    });

    await renderDetail();

    expect(screen.getByText("No open position")).toBeInTheDocument();
    expect(screen.getByText("No closed trade yet")).toBeInTheDocument();
    expect(screen.queryByText("This profile holds no open trade.")).not.toBeInTheDocument();
  });

  it("renders the resolved configuration and the embedded strategy", async () => {
    await renderDetail();

    expect(screen.getByText("binance")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();

    expect(
      screen.getByRole("heading", { level: 3, name: "EMA cross baseline" }),
    ).toBeInTheDocument();
    expect(screen.getByText("EMA(12)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /freqtrade\.io/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View the profiles of EMA cross baseline" }))
      .toHaveAttribute("href", "/profiles?strategy=basic");
  });

  it("drives the window through the URL, never through a full reload", async () => {
    await renderDetail("alpha", { window: "7d" });

    expect(fetchMock).toHaveBeenCalledWith("/api/profiles/alpha?window=7d", expect.anything());
    expect(screen.getByRole("link", { name: "7d" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("link", { name: "7d" })).toHaveAttribute(
      "href",
      "/profiles/alpha?window=7d",
    );
    expect(
      screen.getByRole("img", { name: /Portfolio value of alpha over 7d/ }),
    ).toBeInTheDocument();
  });

  it("ignores an unknown window and falls back to 24h", async () => {
    await renderDetail("alpha", { window: "1y" });

    expect(fetchMock).toHaveBeenCalledWith("/api/profiles/alpha?window=24h", expect.anything());
    expect(screen.getByRole("link", { name: "24h" })).toHaveAttribute("aria-current", "true");
  });

  it("renders the banner plus the last known data when the API goes down", async () => {
    await renderDetail();
    cleanup();

    fetchMock.mockRejectedValue(new Error("connect ECONNREFUSED"));
    await renderDetail();

    expect(screen.getByText("The profile could not be refreshed")).toBeInTheDocument();
    expect(screen.getAllByText(/ECONNREFUSED/).length).toBeGreaterThanOrEqual(1);

    // The page keeps the payload it already had instead of going blank.
    expect(screen.getByRole("heading", { level: 1, name: "Alpha BTC 5m" })).toBeInTheDocument();
    expect(screen.getAllByText("1,040.00 USDT").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
  });

  it("renders the 404 of the API through the banner, without crashing", async () => {
    await renderDetail("ghost");

    expect(screen.getByText("This profile does not exist")).toBeInTheDocument();
    expect(screen.getByText(/HTTP 404/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "ghost" })).toBeInTheDocument();
    expect(screen.getByText("No open position")).toBeInTheDocument();
    expect(screen.getByText("No closed trade yet")).toBeInTheDocument();
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
  });

  it("renders an empty page when a 200 carries none of the documented blocks", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input).split("?")[0];
      if (path === "/api/health") {
        return jsonResponse({});
      }
      if (path === "/api/profiles/alpha") {
        // A partial answer: no profile, no performance, no configuration.
        return jsonResponse({ generated_at: "2026-09-27T12:00:00Z" });
      }
      return jsonResponse({ generated_at: "2026-09-27T12:00:00Z" });
    });

    await renderDetail();

    expect(screen.getByRole("heading", { level: 1, name: "alpha" })).toBeInTheDocument();
    expect(screen.getByText("No open position")).toBeInTheDocument();
    expect(screen.getByText("No closed trade yet")).toBeInTheDocument();
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
    expect(screen.queryByText("The profile could not be refreshed")).not.toBeInTheDocument();
  });

  it("keeps the controls disabled when nothing can be started", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/profiles/alpha")) {
        return jsonResponse(
          apiDetail({ profile: { id: "alpha", name: "Alpha", state: "stopped" } }),
        );
      }
      if (url.startsWith("/api/health")) {
        return jsonResponse(apiHealth());
      }
      return jsonResponse({ generated_at: "", profiles: [] });
    });

    await renderDetail();

    expect(screen.getByRole("button", { name: "Start" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
  });

  it("renders the sections of the profile page only", async () => {
    await renderDetail();

    const sections = screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent);
    expect(sections).toEqual([
      "Key metrics",
      "Equity curve",
      "Daily profit",
      "Open trades",
      "Recent closed trades",
      "Resolved configuration",
    ]);
    expect(within(screen.getAllByRole("table")[0]).getAllByRole("row").length).toBeGreaterThan(1);
  });
});
