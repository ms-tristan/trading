import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiHealth,
  apiProfile,
  apiProfiles,
  apiSettings,
  jsonResponse,
} from "@/components/profile/fixtures";
import { forgetLastKnown } from "@/lib/api";

import OverviewPage, { normaliseWindow } from "./page";

/**
 * Wire payloads of the mocked `fetch`.
 *
 * The page consumes `@/lib/api`, which maps the JSON the Python API serves onto
 * the view model: the doubles below are therefore the payloads of the API
 * (`profit_abs`, `t`/`value`, `engine_slots_total`), never the view model.
 */
const PAPER_ROWS = [
  apiProfile({ id: "bravo", name: "Bravo", portfolio_value: 1300, profit_usdt: 300, profit_pct: 0.3 }),
  apiProfile({ id: "alpha", name: "Alpha", portfolio_value: 1100, profit_usdt: 100, profit_pct: 0.1 }),
];

const LIVE_ROW = apiProfile({
  id: "funded",
  name: "Funded BTC",
  mode: "live",
  state: "blocked",
  state_reason: "live trading is not enabled",
  portfolio_value: 5000,
});

const RANKED = apiProfiles([...PAPER_ROWS, LIVE_ROW]);

const ACCOUNT = {
  generated_at: "2026-09-27T12:00:00Z",
  paper: {
    scope: "paper",
    portfolio_value: 2400,
    initial_capital: 2000,
    profit_abs: 400,
    profit_pct: 0.2,
    realized_profit_abs: 300,
    unrealized_profit_abs: 100,
    open_trades: 2,
    closed_trades: 12,
    win_rate: 0.5,
    profit_factor: 1.5,
    max_drawdown_pct: 0.04,
    profiles_total: 2,
    profiles_running: 2,
  },
  live: { scope: "live", portfolio_value: 5000, initial_capital: 5000, profiles_total: 1 },
  combined: {
    scope: "combined",
    portfolio_value: 7400,
    initial_capital: 7000,
    profit_abs: 400,
    profit_pct: 0.0571,
    realized_profit_abs: 300,
    unrealized_profit_abs: 100,
    open_trades: 2,
    closed_trades: 12,
    win_rate: 0.5,
    profit_factor: 1.5,
    max_drawdown_pct: 0.04,
    profiles_total: 3,
    profiles_running: 2,
    equity_curve: [
      { t: "2026-09-26T00:00:00Z", value: 7000, profit_pct: 0 },
      { t: "2026-09-27T00:00:00Z", value: 7400, profit_pct: 0.0571 },
    ],
  },
};

const HEALTH = apiHealth({ running_profiles: 2, max_running_profiles: 4 });

const SETTINGS = apiSettings({ allow_live_trading: false });

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/health")) {
      return jsonResponse(HEALTH);
    }
    if (url.startsWith("/api/settings")) {
      return jsonResponse(SETTINGS);
    }
    if (url.startsWith("/api/profiles")) {
      return jsonResponse(RANKED);
    }
    if (url.startsWith("/api/account")) {
      return jsonResponse(ACCOUNT);
    }
    return jsonResponse({ detail: "not found" }, 404);
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

async function renderOverview(searchParams?: Record<string, string>) {
  const ui = await OverviewPage(
    searchParams === undefined ? {} : { searchParams: Promise.resolve(searchParams) },
  );
  return render(ui);
}

/** Rows of the n-th table, header row included. */
function tableRows(index: number): HTMLElement[] {
  return within(screen.getAllByRole("table")[index]).getAllByRole("row");
}

describe("normaliseWindow", () => {
  it("accepts the four documented windows", () => {
    expect(normaliseWindow("24h")).toBe("24h");
    expect(normaliseWindow("7d")).toBe("7d");
    expect(normaliseWindow("30d")).toBe("30d");
    expect(normaliseWindow("all")).toBe("all");
  });

  it("falls back to 24h for anything else", () => {
    expect(normaliseWindow(undefined)).toBe("24h");
    expect(normaliseWindow("1y")).toBe("24h");
    expect(normaliseWindow("")).toBe("24h");
  });

  it("takes the first value of a repeated parameter", () => {
    expect(normaliseWindow(["30d", "7d"])).toBe("30d");
    expect(normaliseWindow([])).toBe("24h");
  });
});

describe("OverviewPage", () => {
  it("leads with the account band, then paper trading, then real trading", async () => {
    await renderOverview();

    const band = screen.getByRole("heading", { level: 1, name: "Account performance" });
    const paper = screen.getByRole("heading", { level: 2, name: /Paper trading/ });
    const real = screen.getByRole("heading", { level: 2, name: /Real trading/ });

    expect(band.compareDocumentPosition(paper) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(paper.compareDocumentPosition(real) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows the combined account performance at the top", async () => {
    await renderOverview();

    expect(screen.getByText("7,400.00 USDT")).toBeInTheDocument();
    expect(screen.getByText(/\+400\.00 USDT \(\+5\.71%\)/)).toBeInTheDocument();
    expect(screen.getByText("Profiles running")).toBeInTheDocument();
    expect(screen.getByText("2 of 3")).toBeInTheDocument();
    expect(screen.getByText("Engine slots")).toBeInTheDocument();
    expect(screen.getByText("2 of 4")).toBeInTheDocument();
  });

  it("ranks the paper profiles in the order the API returned them", async () => {
    await renderOverview();

    const rows = tableRows(0);
    expect(rows).toHaveLength(3); // header + two profiles
    expect(rows[1]).toHaveTextContent("Bravo");
    expect(rows[2]).toHaveTextContent("Alpha");
    expect(screen.getByRole("link", { name: "Bravo" })).toHaveAttribute("href", "/profiles/bravo");
  });

  it("keeps live profiles in the real-trading section only", async () => {
    await renderOverview();

    const realRows = tableRows(1);
    expect(realRows).toHaveLength(2);
    expect(realRows[1]).toHaveTextContent("Funded BTC");
    expect(screen.getByText("Refused profile")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("Live trading is not enabled");
  });

  it("fetches the requested window and marks it as current", async () => {
    await renderOverview({ window: "7d" });

    expect(fetchMock).toHaveBeenCalledWith("/api/account?window=7d", expect.anything());
    expect(screen.getByRole("link", { name: "7d" })).toHaveAttribute("aria-current", "true");
  });

  it("defaults to the 24h window", async () => {
    await renderOverview();

    expect(fetchMock).toHaveBeenCalledWith("/api/account?window=24h", expect.anything());
    expect(screen.getByRole("link", { name: "24h" })).toHaveAttribute("aria-current", "true");
  });

  it("renders the aggregate line of both sections", async () => {
    await renderOverview();

    expect(screen.getByText(/2 profiles - 2 running/)).toBeInTheDocument();
    expect(screen.getByText(/1 profile - 0 running/)).toBeInTheDocument();
  });

  it("reports a failing API without losing the page", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));

    await renderOverview();

    const banners = screen.getAllByRole("status");
    expect(banners).toHaveLength(3);
    expect(banners[0]).toHaveTextContent("/api/account?window=24h");
    expect(screen.getByRole("heading", { level: 2, name: /Paper trading/ })).toBeInTheDocument();
    expect(screen.getByText("Live trading status unknown")).toBeInTheDocument();
  });

  it("renders the empty messages when no profile exists", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/profiles")) {
        return jsonResponse({ generated_at: "", profiles: [] });
      }
      if (url.startsWith("/api/settings")) {
        return jsonResponse(SETTINGS);
      }
      return jsonResponse(ACCOUNT);
    });

    await renderOverview();

    expect(screen.getByText("No paper profile is configured yet.")).toBeInTheDocument();
    expect(screen.getByText("No live profile is configured yet.")).toBeInTheDocument();
  });
});
