import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DailyBars } from "./DailyBars";
import type { DailyBarPoint } from "@/lib/types";

const BARS: DailyBarPoint[] = [
  { date: "2026-09-25", profit_usdt: 120.5, trades: 4 },
  { date: "2026-09-26", profit_usdt: -80.25, trades: 2 },
  { date: "2026-09-27", profit_usdt: 0, trades: 1 },
];

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("DailyBars", () => {
  it("draws one bar per day and names the chart", () => {
    const { container } = render(<DailyBars bars={BARS} />);

    expect(screen.getByRole("img", { name: /Daily profit: 3 bars/ })).toBeInTheDocument();
    expect(container.querySelectorAll("rect")).toHaveLength(3);
  });

  it("colours a profitable day with the bull token and a losing day with the bear token", () => {
    const { container } = render(<DailyBars bars={BARS} />);
    const bars = Array.from(container.querySelectorAll("rect"));

    expect(bars[0]).toHaveAttribute("fill", "var(--chart-bull)");
    expect(bars[1]).toHaveAttribute("fill", "var(--chart-bear)");
    expect(bars[2]).toHaveAttribute("fill", "var(--chart-bull)");
  });

  it("repeats every bar in a real table with its glyph", () => {
    render(<DailyBars bars={BARS} />);

    expect(screen.getByRole("table", { name: "Daily profit: daily values" })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: /2026-09-25/ })).toBeInTheDocument();
    expect(screen.getByText("+120.50 USDT")).toBeInTheDocument();
    expect(screen.getByText("-80.25 USDT")).toBeInTheDocument();
    expect(screen.getByText("0.00 USDT")).toBeInTheDocument();
  });

  it("summarises the period", () => {
    render(<DailyBars bars={BARS} />);

    expect(screen.getByText(/3 days - total \+40\.25 USDT/)).toBeInTheDocument();
  });

  it("uses the singular for a single day", () => {
    render(<DailyBars bars={[BARS[0]]} />);

    expect(screen.getByText(/1 day - total \+120\.50 USDT/)).toBeInTheDocument();
  });

  it("says so when no day is published", () => {
    const { container } = render(<DailyBars bars={[]} />);

    expect(screen.getByText("No daily profit published yet.")).toBeInTheDocument();
    expect(container.querySelector("svg")).toBeNull();
    expect(screen.getByText(/0 days - total 0\.00 USDT/)).toBeInTheDocument();
  });

  it("ignores a non-finite day", () => {
    const { container } = render(
      <DailyBars bars={[{ date: "2026-09-27", profit_usdt: Number.NaN, trades: 0 }]} />,
    );

    expect(container.querySelectorAll("rect")).toHaveLength(0);
    expect(screen.getByText("No daily profit published yet.")).toBeInTheDocument();
  });
});
