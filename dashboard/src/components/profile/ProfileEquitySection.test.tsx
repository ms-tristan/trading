import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

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
});
