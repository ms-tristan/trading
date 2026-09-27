import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Sparkline, buildSparklinePath } from "./Sparkline";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("buildSparklinePath", () => {
  it("maps a series into the box, oldest value on the left", () => {
    expect(buildSparklinePath([1, 2, 3], 100, 20)).toBe(
      "M2.00 18.00 L50.00 10.00 L98.00 2.00",
    );
  });

  it("draws a flat series on the middle line", () => {
    expect(buildSparklinePath([5, 5], 100, 20)).toBe("M2.00 10.00 L98.00 10.00");
  });

  it("answers an empty path for a series of fewer than two points", () => {
    expect(buildSparklinePath([], 100, 20)).toBe("");
    expect(buildSparklinePath([7], 100, 20)).toBe("");
  });

  it("ignores non-finite points", () => {
    expect(buildSparklinePath([1, Number.NaN, 3], 100, 20)).toBe("M2.00 18.00 L98.00 2.00");
  });
});

describe("Sparkline", () => {
  it("hides the SVG from assistive technology and summarises it in visible text", () => {
    const { container } = render(<Sparkline values={[100, 110]} label="Combined equity" />);

    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText(/\+10\.00%/)).toBeInTheDocument();
    expect(screen.getByText(/Combined equity:/)).toBeInTheDocument();
  });

  it("drops the chart but keeps the wording when there are fewer than two points", () => {
    const { container } = render(<Sparkline values={[100]} label="Combined equity" />);

    expect(container.querySelector("svg")).toBeNull();
    expect(screen.getByText("no data")).toBeInTheDocument();
  });

  it("shows a fall instead of a rise for a declining series", () => {
    render(<Sparkline values={[200, 150]} label="Combined equity" />);

    expect(screen.getByText(/\u25BC -25\.00%/)).toBeInTheDocument();
  });

  it("handles a series starting at zero without dividing by it", () => {
    render(<Sparkline values={[0, 10]} label="Combined equity" />);

    expect(screen.getByText(/\u25B2 0\.00%/)).toBeInTheDocument();
  });

  it("accepts a caller-provided summary", () => {
    render(<Sparkline values={[1, 2]} label="Combined equity" summary="no curve yet" />);

    expect(screen.getByText("no curve yet")).toBeInTheDocument();
  });
});
