import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ProfileKpiRow } from "./ProfileKpiRow";
import { performance, profile } from "./fixtures";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

function renderRow(overrides: Partial<Parameters<typeof ProfileKpiRow>[0]> = {}) {
  return render(
    <ProfileKpiRow
      profile={profile({ id: "alpha", portfolio_value: 1040, initial_capital: 1000 })}
      performance={performance()}
      cash={40}
      positionsValue={1000}
      rank={2}
      uptimeSeconds={3670}
      {...overrides}
    />,
  );
}

describe("ProfileKpiRow", () => {
  it("shows the value, the capital and the windowed performance", () => {
    renderRow();

    expect(screen.getByText("Portfolio value")).toBeInTheDocument();
    expect(screen.getByText("1,040.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("initial capital 1,000.00 USDT")).toBeInTheDocument();

    expect(screen.getByText("Cash")).toBeInTheDocument();
    expect(screen.getByText("40.00 USDT")).toBeInTheDocument();

    expect(screen.getByText("Positions value")).toBeInTheDocument();
    expect(screen.getByText("1,000.00 USDT")).toBeInTheDocument();

    expect(screen.getByText("+40.00 USDT")).toBeInTheDocument();
    // Profit, profit %, realised P&L and unrealised P&L are all rising.
    expect(screen.getAllByText("(up)")).toHaveLength(4);
    expect(screen.getByText("+4.00%")).toBeInTheDocument();
    expect(screen.getByText("+25.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("+15.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("1 / 6")).toBeInTheDocument();
    expect(screen.getByText("50.00%")).toBeInTheDocument();
    expect(screen.getByText("1.40")).toBeInTheDocument();
    expect(screen.getByText("3.00%")).toBeInTheDocument();
  });

  it("renders the rank and the uptime", () => {
    renderRow();

    expect(screen.getByText("Rank")).toBeInTheDocument();
    expect(screen.getByText("#2")).toBeInTheDocument();
    expect(screen.getByText("Uptime")).toBeInTheDocument();
    expect(screen.getByText("1h 1m")).toBeInTheDocument();
  });

  it("renders an em dash for the figures the API does not publish", () => {
    renderRow({ cash: null, positionsValue: null, rank: null, uptimeSeconds: null });

    expect(screen.queryByText("40.00 USDT")).not.toBeInTheDocument();
    expect(screen.queryByText("#2")).not.toBeInTheDocument();
    expect(screen.queryByText("1h 1m")).not.toBeInTheDocument();
    expect(screen.getAllByText("\u2014").length).toBeGreaterThanOrEqual(4);
  });

  it("keeps a losing profile readable", () => {
    renderRow({
      profile: profile({ id: "alpha", portfolio_value: 900, initial_capital: 1000 }),
      performance: performance({
        portfolio_value: 900,
        profit_usdt: -100,
        profit_pct: -0.1,
        realised_profit_usdt: -60,
        unrealised_profit_usdt: -40,
        open_trades: 0,
        closed_trades: 4,
        win_rate: 0.25,
        profit_factor: 0,
        max_drawdown_pct: 0.12,
      }),
      cash: 900,
      positionsValue: 0,
    });

    expect(screen.getByText("-100.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("-10.00%")).toBeInTheDocument();
    expect(screen.getByText("-60.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("-40.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("0 / 4")).toBeInTheDocument();
    expect(screen.getByText("25.00%")).toBeInTheDocument();
    expect(screen.getByText("12.00%")).toBeInTheDocument();
    expect(screen.getAllByText("(down)")).toHaveLength(4);
  });

  it("names the window it aggregates", () => {
    renderRow({ performance: performance({ window: "7d" }) });

    expect(screen.getByText(/7d window/)).toBeInTheDocument();
  });
});
