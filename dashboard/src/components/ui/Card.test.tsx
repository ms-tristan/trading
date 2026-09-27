import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Card } from "./Card";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("Card", () => {
  it("renders its title, description and children", () => {
    render(
      <Card title="Paper trading" description="4 profiles">
        <p>body</p>
      </Card>,
    );

    expect(screen.getByRole("heading", { level: 3, name: "Paper trading" })).toBeInTheDocument();
    expect(screen.getByText("4 profiles")).toBeInTheDocument();
    expect(screen.getByText("body")).toBeInTheDocument();
  });

  it("renders the title at the requested heading level", () => {
    render(
      <Card title="Account" headingLevel={2}>
        <p>body</p>
      </Card>,
    );

    expect(screen.getByRole("heading", { level: 2, name: "Account" })).toBeInTheDocument();
  });

  it("renders its actions next to the title", () => {
    render(
      <Card title="Settings" actions={<button type="button">Reload</button>}>
        <p>body</p>
      </Card>,
    );

    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("renders a plain surface when it has no title, description or action", () => {
    render(
      <Card className="custom-class">
        <p>body</p>
      </Card>,
    );

    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
    expect(screen.getByText("body").closest("section")).toHaveClass("custom-class");
  });
});
