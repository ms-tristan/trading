import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { toProfileDetail } from "@/lib/api-wire";

import { PROFILE_WINDOWS, ProfileEquitySection, normaliseProfileWindow } from "./ProfileEquitySection";
import { dailyBar, equityPoint } from "./fixtures";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

const EQUITY = [
  equityPoint({ timestamp: "2026-09-26T12:00:00Z", portfolio_value: 1000 }),
  equityPoint({ timestamp: "2026-09-27T12:00:00Z", portfolio_value: 1040 }),
];

const DAILY = [dailyBar({ date: "2026-09-26", profit_usdt: 15, trades: 2 })];

describe("normaliseProfileWindow", () => {
  it("accepts the four documented windows", () => {
    expect(normaliseProfileWindow("24h")).toBe("24h");
    expect(normaliseProfileWindow("7d")).toBe("7d");
    expect(normaliseProfileWindow("30d")).toBe("30d");
    expect(normaliseProfileWindow("all")).toBe("all");
  });

  it("falls back to 24h for anything else", () => {
    expect(normaliseProfileWindow(undefined)).toBe("24h");
    expect(normaliseProfileWindow("1y")).toBe("24h");
    expect(normaliseProfileWindow("")).toBe("24h");
  });

  it("takes the first value of a repeated parameter", () => {
    expect(normaliseProfileWindow(["30d", "7d"])).toBe("30d");
    expect(normaliseProfileWindow([])).toBe("24h");
  });
});

describe("ProfileEquitySection", () => {
  it("offers every window as a link of the same route", () => {
    render(
      <ProfileEquitySection profileId="alpha" window="7d" equity={EQUITY} daily={DAILY} />,
    );

    for (const entry of PROFILE_WINDOWS) {
      expect(screen.getByRole("link", { name: entry })).toHaveAttribute(
        "href",
        `/profiles/alpha?window=${entry}`,
      );
    }
    expect(screen.getByRole("link", { name: "7d" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("link", { name: "24h" })).not.toHaveAttribute("aria-current");
  });

  it("plots the portfolio value of the selected window", () => {
    render(
      <ProfileEquitySection profileId="alpha" window="7d" equity={EQUITY} daily={DAILY} />,
    );

    expect(
      screen.getByRole("img", { name: /Portfolio value of alpha over 7d/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Portfolio value over 7d/)).toBeInTheDocument();
  });

  it("keeps the numbers readable without the chart", () => {
    render(
      <ProfileEquitySection profileId="alpha" window="24h" equity={EQUITY} daily={DAILY} />,
    );

    // The equity chart is doubled by its own summary table.
    expect(screen.getByRole("img", { name: /Portfolio value of alpha over 24h/ })).toBeInTheDocument();
    expect(screen.getAllByText("1,000.00 USDT").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("1,040.00 USDT").length).toBeGreaterThanOrEqual(1);

    // The daily bars are doubled by the day table.
    expect(screen.getByText("2026-09-26")).toBeInTheDocument();
    expect(screen.getByText("+15.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("renders the daily card without bars when the window has none", () => {
    render(
      <ProfileEquitySection profileId="alpha" window="24h" equity={[]} daily={[]} />,
    );

    expect(screen.getByText("No daily profit published yet.")).toBeInTheDocument();
    expect(screen.getByText("Not enough data to draw the equity curve.")).toBeInTheDocument();
  });

  it("lists the days of a wire payload: `abs_profit` and `trade_count` reach the table", () => {
    // The state database serves `abs_profit`/`trade_count`; the section renders
    // the mapped bars, so the table is pinned on the real field names.
    const payload = toProfileDetail(
      {
        profile: { id: "alpha", name: "Alpha", initial_capital: 1000 },
        daily: [
          {
            date: "2026-09-25",
            abs_profit: -5,
            rel_profit: -0.005,
            starting_balance: 1000,
            trade_count: 1,
          },
          {
            date: "2026-09-26",
            abs_profit: 15,
            rel_profit: 0.015,
            starting_balance: 1000,
            trade_count: 2,
          },
        ],
      },
      "24h",
    );

    render(
      <ProfileEquitySection
        profileId="alpha"
        window="24h"
        equity={payload.equity_curve}
        daily={payload.daily_profit}
      />,
    );

    const card = screen.getByRole("heading", { level: 2, name: "Daily profit" }).closest("section");
    expect(card).not.toBeNull();
    const rows = within(within(card as HTMLElement).getByRole("table")).getAllByRole("row");

    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("2026-09-25");
    expect(rows[2]).toHaveTextContent("2026-09-26");
    expect(within(rows[1]).getAllByRole("cell")[0]).toHaveTextContent("-5.00 USDT");
    expect(within(rows[2]).getAllByRole("cell")[1]).toHaveTextContent("2");
  });
});
