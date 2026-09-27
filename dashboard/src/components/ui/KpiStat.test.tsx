import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { KpiStat } from "./KpiStat";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("KpiStat", () => {
  it("renders the label, the value and the hint", () => {
    render(<KpiStat label="Realised P&L" value="+120.00 USDT" hint="since inception" />);

    expect(screen.getByText("Realised P&L")).toBeInTheDocument();
    expect(screen.getByText("+120.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("since inception")).toBeInTheDocument();
  });

  it("marks a rise with a glyph, the profit colour and a spoken direction", () => {
    const { container } = render(<KpiStat label="Profit" value="+1.00 USDT" direction="up" />);

    expect(screen.getByText("\u25B2")).toBeInTheDocument();
    expect(screen.getByText("(up)")).toBeInTheDocument();
    expect(container.querySelector(".text-profit")).not.toBeNull();
  });

  it("marks a loss with a glyph, the loss colour and a spoken direction", () => {
    const { container } = render(<KpiStat label="Profit" value="-1.00 USDT" direction="down" />);

    expect(screen.getByText("\u25BC")).toBeInTheDocument();
    expect(screen.getByText("(down)")).toBeInTheDocument();
    expect(container.querySelector(".text-loss")).not.toBeNull();
  });

  it("renders no glyph for a flat value", () => {
    render(<KpiStat label="Open positions" value="0" />);

    expect(screen.queryByText("\u25B2")).not.toBeInTheDocument();
    expect(screen.queryByText("\u25BC")).not.toBeInTheDocument();
    expect(screen.getByText("(flat)")).toBeInTheDocument();
  });
});
