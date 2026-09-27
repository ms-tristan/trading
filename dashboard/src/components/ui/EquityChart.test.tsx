import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { EquityChart, type EquitySeries } from "./EquityChart";

const RISING: EquitySeries = {
  id: "account",
  label: "Account",
  points: [
    { timestamp: "2026-09-26T00:00:00Z", value: 1000 },
    { timestamp: "2026-09-26T12:00:00Z", value: 1050 },
    { timestamp: "2026-09-27T00:00:00Z", value: 1100 },
  ],
};

const FALLING: EquitySeries = {
  id: "benchmark",
  label: "Benchmark",
  points: [
    { timestamp: "2026-09-26T00:00:00Z", value: 1000 },
    { timestamp: "2026-09-27T00:00:00Z", value: 900 },
  ],
};

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("EquityChart", () => {
  it("draws one path per series and names the chart", () => {
    const { container } = render(<EquityChart series={[RISING, FALLING]} />);

    expect(screen.getByRole("img", { name: /Equity curve \(2 series, 3 points\)/ })).toBeInTheDocument();
    expect(container.querySelectorAll("[data-series]")).toHaveLength(2);
    expect(container.querySelector('[data-series="account"] path[stroke]')).not.toBeNull();
  });

  it("distinguishes a falling series by a dashed stroke, not only by colour", () => {
    const { container } = render(<EquityChart series={[RISING, FALLING]} />);

    const rising = container.querySelector('[data-series="account"] path[stroke]:not([stroke-dasharray])');
    const falling = container.querySelector('[data-series="benchmark"] path[stroke-dasharray]');

    expect(rising).not.toBeNull();
    expect(falling).not.toBeNull();
  });

  it("repeats every series in the legend with its arrow glyph", () => {
    const { container } = render(<EquityChart series={[RISING, FALLING]} />);
    const legend = within(container.querySelector("figcaption") as HTMLElement);

    expect(legend.getByText("Account")).toBeInTheDocument();
    expect(legend.getByText("Benchmark")).toBeInTheDocument();
    expect(legend.getByText("+10.00%")).toBeInTheDocument();
    expect(legend.getByText("-10.00%")).toBeInTheDocument();
    expect(legend.getByText("\u25B2")).toBeInTheDocument();
    expect(legend.getByText("\u25BC")).toBeInTheDocument();
  });

  it("pairs the chart with a real table summarising every series", () => {
    render(<EquityChart series={[RISING, FALLING]} />);

    const table = screen.getByRole("table", { name: "Equity curve: series summary" });
    expect(table).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: /Account/ })).toBeInTheDocument();
    expect(screen.getByText("1,100.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("900.00 USDT")).toBeInTheDocument();
  });

  it("renders the time range of the first series", () => {
    render(<EquityChart series={[RISING, FALLING]} />);

    expect(screen.getByText(/2026-09-26 00:00:00 UTC to 2026-09-27 00:00:00 UTC/)).toBeInTheDocument();
  });

  it("says so when there is not enough data to draw", () => {
    const { container } = render(
      <EquityChart series={[{ id: "account", label: "Account", points: [RISING.points[0]] }]} />,
    );

    expect(screen.getByText("Not enough data to draw the equity curve.")).toBeInTheDocument();
    expect(container.querySelector("svg")).toBeNull();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("ignores non-finite points", () => {
    render(
      <EquityChart
        series={[
          {
            id: "account",
            label: "Account",
            points: [
              { timestamp: "2026-09-26T00:00:00Z", value: Number.NaN },
              { timestamp: "2026-09-27T00:00:00Z", value: 1100 },
            ],
          },
        ]}
      />,
    );

    expect(screen.getByText("Not enough data to draw the equity curve.")).toBeInTheDocument();
  });

  it("summarises a series without any usable point", () => {
    render(<EquityChart series={[{ id: "empty", label: "Empty", points: [] }]} />);

    expect(screen.getByText("Not enough data to draw the equity curve.")).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: /Empty/ })).toBeInTheDocument();
    expect(screen.getAllByText("\u2014").length).toBeGreaterThan(0);
  });
});
