import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { AccountPerformance, EquityPoint } from "@/lib/types";

import { AccountBand } from "./AccountBand";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

function performance(overrides: Partial<AccountPerformance> = {}): AccountPerformance {
  return {
    window: "24h",
    portfolio_value: 26500,
    initial_capital: 25000,
    profit_usdt: 1500,
    profit_pct: 0.06,
    realised_profit_usdt: 1200,
    unrealised_profit_usdt: 300,
    open_trades: 3,
    closed_trades: 42,
    win_rate: 0.55,
    profit_factor: 1.85,
    max_drawdown_pct: 0.08,
    profiles_total: 12,
    profiles_running: 8,
    ...overrides,
  };
}

const EQUITY: EquityPoint[] = [
  { timestamp: "2026-09-26T00:00:00Z", portfolio_value: 25000, profit_usdt: 0, profit_pct: 0 },
  { timestamp: "2026-09-27T00:00:00Z", portfolio_value: 26500, profit_usdt: 1500, profit_pct: 0.06 },
];

describe("AccountBand", () => {
  it("leads with the combined portfolio value and the signed total profit", () => {
    render(<AccountBand performance={performance()} equity={EQUITY} window="24h" />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Account performance" }),
    ).toBeInTheDocument();
    expect(screen.getByText("26,500.00 USDT")).toBeInTheDocument();
    expect(screen.getByText(/\+1,500\.00 USDT \(\+6\.00%\)/)).toBeInTheDocument();
    expect(screen.getByText(/initial capital 25,000\.00 USDT over 24h/)).toBeInTheDocument();
  });

  it("renders the platform KPIs, with no engine-slot capacity", () => {
    render(<AccountBand performance={performance()} equity={EQUITY} window="24h" />);

    expect(screen.getByText("Realised P&L")).toBeInTheDocument();
    expect(screen.getByText("+1,200.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("Unrealised P&L")).toBeInTheDocument();
    expect(screen.getByText("+300.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("Open positions")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("Closed trades")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("Win rate")).toBeInTheDocument();
    expect(screen.getByText("55.00%")).toBeInTheDocument();
    expect(screen.getByText("Profit factor")).toBeInTheDocument();
    expect(screen.getByText("1.85")).toBeInTheDocument();
    expect(screen.getByText("Max drawdown")).toBeInTheDocument();
    expect(screen.getByText("8.00%")).toBeInTheDocument();
    expect(screen.getByText("Profiles running")).toBeInTheDocument();
    expect(screen.getByText("8 of 12")).toBeInTheDocument();
    expect(screen.queryByText("Engine slots")).not.toBeInTheDocument();
  });

  it("marks the selected aggregation window with aria-current", () => {
    render(<AccountBand performance={performance()} equity={EQUITY} window="7d" />);

    const nav = screen.getByRole("navigation", { name: "Aggregation window" });
    const links = within(nav).getAllByRole("link");

    expect(links.map((link) => link.textContent)).toEqual(["24h", "7d", "30d", "all"]);
    expect(within(nav).getByRole("link", { name: "7d" })).toHaveAttribute("aria-current", "true");
    expect(within(nav).getByRole("link", { name: "24h" })).not.toHaveAttribute("aria-current");
    expect(within(nav).getByRole("link", { name: "7d" })).toHaveAttribute("href", "/?window=7d");
  });

  it("summarises the combined equity curve next to the sparkline", () => {
    render(<AccountBand performance={performance()} equity={EQUITY} window="24h" />);

    expect(screen.getByText(/\u25B2 \+6\.00%/)).toBeInTheDocument();
    expect(screen.getByText("2 equity points")).toBeInTheDocument();
  });

  it("says so when the equity curve is empty", () => {
    render(<AccountBand performance={performance()} equity={[]} window="24h" />);

    expect(screen.getByText("no curve yet")).toBeInTheDocument();
    expect(screen.getByText("0 equity points")).toBeInTheDocument();
  });

  it("renders a flat profit without an up or down arrow", () => {
    render(
      <AccountBand
        performance={performance({ profit_usdt: 0, profit_pct: 0 })}
        equity={EQUITY}
        window="all"
      />,
    );

    expect(screen.getByText(/0\.00 USDT \(0\.00%\)/)).toBeInTheDocument();
    expect(screen.getAllByText("(flat)").length).toBeGreaterThan(1);
  });
});
