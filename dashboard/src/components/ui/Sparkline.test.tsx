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
  it("announces the series with a title, an aria-label and visible text", () => {
    const { container } = render(<Sparkline values={[100, 110]} label="Combined equity" />);
    const svg = container.querySelector("svg");

    expect(svg).not.toBeNull();
    expect(svg).not.toHaveAttribute("aria-hidden", "true");
    expect(svg).toHaveAttribute("role", "img");
    expect(svg).toHaveAttribute("aria-label", "Combined equity");
    expect(container.querySelector("svg > title")).toHaveTextContent("Combined equity");
    expect(screen.getByRole("img", { name: "Combined equity" })).toBeInTheDocument();
    expect(screen.getByText(/\+10\.00%/)).toBeInTheDocument();
    expect(screen.getByText(/Combined equity:/)).toBeInTheDocument();
  });

  it("drops the chart but keeps the wording when there are fewer than two points", () => {
    const { container } = render(<Sparkline values={[100]} label="Combined equity" />);

    expect(container.querySelector("svg")).toBeNull();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText("no data")).toBeInTheDocument();
  });

  it("says no data only below two finite points", () => {
    const empty = render(<Sparkline values={[]} label="Combined equity" />);
    expect(screen.getByText("no data")).toBeInTheDocument();
    empty.unmount();

    const malformed = render(
      <Sparkline values={[Number.NaN, 100]} label="Combined equity" />,
    );
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText("no data")).toBeInTheDocument();
    malformed.unmount();

    render(<Sparkline values={[100, 100]} label="Combined equity" />);
    expect(screen.queryByText("no data")).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Combined equity" })).toBeInTheDocument();
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
