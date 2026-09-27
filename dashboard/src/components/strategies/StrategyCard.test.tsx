import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { profile, strategyView } from "@/components/profile/fixtures";

import { StrategyCard } from "./StrategyCard";
import { strategyCardView } from "./strategyMeta";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

const MOMENTUM = strategyCardView(
  strategyView({
    id: "momentum",
    title: "Momentum breakout",
    category: "trend",
    summary: "Buys strength while price momentum stays positive.",
    description: "The strategy measures the rate of change of the close.",
    risk_notes: "Momentum crashes on a sharp reversal.",
    indicators: ["Momentum(10)", "EMA(50)"],
    timeframes: ["15m", "1h"],
    pairs: ["BTC/USDT"],
    reference: "Momentum investing - https://www.investopedia.com/terms/m/momentum.asp",
    profiles_total: 3,
    profiles_running: 2,
    portfolio_value: 3000,
    profit_usdt: 120,
    profit_pct: 0.04,
    open_trades: 1,
    closed_trades: 12,
    win_rate: 0.5,
    profit_factor: 1.4,
    sparkline: [1000, 1040, 3000],
  }),
)!;

describe("StrategyCard", () => {
  it("describes the strategy, its indicators and its reference", () => {
    render(
      <StrategyCard
        view={MOMENTUM}
        bestProfile={profile({ id: "bravo", name: "Bravo", portfolio_value: 1300 })}
      />,
    );

    expect(
      screen.getByRole("heading", { level: 3, name: "Momentum breakout" }),
    ).toBeInTheDocument();
    expect(screen.getByText("trend")).toBeInTheDocument();
    expect(screen.getByText("momentum")).toBeInTheDocument();
    expect(screen.getByText("Buys strength while price momentum stays positive.")).toBeInTheDocument();
    expect(screen.getByText("The strategy measures the rate of change of the close.")).toBeInTheDocument();
    expect(screen.getByText("Momentum(10)")).toBeInTheDocument();
    expect(screen.getByText("EMA(50)")).toBeInTheDocument();
    expect(screen.getByText("15m, 1h")).toBeInTheDocument();
    expect(screen.getByText("BTC/USDT")).toBeInTheDocument();
    expect(screen.getByText(/Risk: Momentum crashes on a sharp reversal/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /investopedia/ })).toHaveAttribute(
      "href",
      "https://www.investopedia.com/terms/m/momentum.asp",
    );
  });

  it("aggregates the profiles that hold the strategy", () => {
    render(<StrategyCard view={MOMENTUM} bestProfile={null} />);

    expect(screen.getByText("Profiles")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("2 running")).toBeInTheDocument();
    expect(screen.getByText("3,000.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("+120.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("+4.00%")).toBeInTheDocument();
    expect(screen.getByText("50.00%")).toBeInTheDocument();
    expect(screen.getByText("profit factor 1.40")).toBeInTheDocument();
    expect(screen.getByText("1 open / 12 closed trades")).toBeInTheDocument();
    expect(screen.getByText("No profile of this strategy is ranked yet.")).toBeInTheDocument();
  });

  it("links to the filtered profile list and to the best profile", () => {
    render(
      <StrategyCard
        view={MOMENTUM}
        bestProfile={profile({ id: "bravo", name: "Bravo", portfolio_value: 1300 })}
      />,
    );

    expect(screen.getByRole("link", { name: "View the profiles of Momentum breakout" }))
      .toHaveAttribute("href", "/profiles?strategy=momentum");
    expect(screen.getByRole("link", { name: "Bravo" })).toHaveAttribute(
      "href",
      "/profiles/bravo",
    );
    expect(screen.getByText(/Best profile:/)).toBeInTheDocument();
  });

  it("omits the aggregate block of a strategy embedded in a profile payload", () => {
    const embedded = strategyCardView({
      id: "basic",
      title: "EMA cross baseline",
      description: "Enters on the EMA cross.",
      indicators: ["EMA(12)"],
    })!;

    render(<StrategyCard view={embedded} />);

    expect(screen.getByRole("heading", { level: 3, name: "EMA cross baseline" })).toBeInTheDocument();
    expect(screen.getByText("EMA(12)")).toBeInTheDocument();
    expect(screen.queryByText("Profiles")).not.toBeInTheDocument();
    expect(screen.queryByText("No profile of this strategy is ranked yet.")).not.toBeInTheDocument();
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
  });

  it("renders an unknown strategy without a link and without NaN", () => {
    const unknown = strategyCardView({ title: "Mystery", profiles_total: 1 })!;

    render(<StrategyCard view={unknown} bestProfile={null} />);

    expect(screen.getByRole("heading", { level: 3, name: "Mystery" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /View the profiles/ })).not.toBeInTheDocument();
    expect(screen.getAllByText("\u2014").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
  });
});
