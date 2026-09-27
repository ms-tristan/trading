import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SectionHeader } from "./SectionHeader";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("SectionHeader", () => {
  it("renders the title and the profile count", () => {
    render(<SectionHeader id="paper-trading" title="Paper trading" count={4} />);

    const heading = screen.getByRole("heading", { level: 2, name: /Paper trading/ });
    expect(heading).toHaveAttribute("id", "paper-trading");
    expect(screen.getByText("4 profiles")).toBeInTheDocument();
  });

  it("uses the singular for a single profile", () => {
    render(<SectionHeader title="Real trading" count={1} />);
    expect(screen.getByText("1 profile")).toBeInTheDocument();
  });

  it("renders the aggregate line and the actions", () => {
    render(
      <SectionHeader
        title="Paper trading"
        subtitle="2 profiles - 1 running"
        actions={<button type="button">Reload</button>}
      />,
    );

    expect(screen.getByText("2 profiles - 1 running")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("omits the count, the subtitle and the actions when they are absent", () => {
    render(<SectionHeader title="Strategies" />);

    expect(screen.getByRole("heading", { level: 2, name: "Strategies" })).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
