import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";

import { ErrorBanner } from "./ErrorBanner";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("ErrorBanner", () => {
  it("renders nothing without an error", () => {
    const { container } = render(<ErrorBanner error={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("announces itself politely and never blocks the page", () => {
    render(<ErrorBanner error={new ApiError("Request failed", 503, "/api/account")} />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Request failed");
    expect(status).toHaveTextContent("/api/account");
    expect(status).toHaveTextContent("HTTP 503");
  });

  it("names what could not be loaded", () => {
    render(
      <ErrorBanner
        title="Account performance could not be refreshed"
        error={new ApiError("offline", 0, "/api/account")}
      />,
    );

    expect(screen.getByText("Account performance could not be refreshed")).toBeInTheDocument();
    expect(screen.queryByText(/HTTP/)).not.toBeInTheDocument();
  });

  it("renders a plain Error without a path", () => {
    render(<ErrorBanner error={new Error("something went wrong")} />);

    expect(screen.getByRole("status")).toHaveTextContent("something went wrong");
    expect(screen.queryByText(/path/)).not.toBeInTheDocument();
  });
});
