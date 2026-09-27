import { cleanup, act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AutoRefresh } from "./AutoRefresh";

const { refreshMock, routerMock } = vi.hoisted(() => {
  const refresh = vi.fn();
  return { refreshMock: refresh, routerMock: { refresh } };
});

vi.mock("next/navigation", () => ({
  useRouter: () => routerMock,
}));

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("AutoRefresh", () => {
  it("refreshes the route on every interval", () => {
    render(<AutoRefresh intervalSeconds={15} />);

    act(() => {
      vi.advanceTimersByTime(15000);
    });
    expect(refreshMock).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(15000);
    });
    expect(refreshMock).toHaveBeenCalledTimes(2);
  });

  it("defaults to 15 s", () => {
    render(<AutoRefresh />);

    expect(screen.getByText("Auto-refresh every 15 s")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(14999);
    });
    expect(refreshMock).not.toHaveBeenCalled();
  });

  it("never refreshes a hidden tab", () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    render(<AutoRefresh intervalSeconds={10} />);

    act(() => {
      vi.advanceTimersByTime(30000);
    });
    expect(refreshMock).not.toHaveBeenCalled();

    visibility.mockReturnValue("visible");
    act(() => {
      vi.advanceTimersByTime(10000);
    });
    expect(refreshMock).toHaveBeenCalledTimes(1);
  });

  it("clears its timer on unmount", () => {
    const { unmount } = render(<AutoRefresh intervalSeconds={5} />);

    unmount();
    act(() => {
      vi.advanceTimersByTime(60000);
    });
    expect(refreshMock).not.toHaveBeenCalled();
  });

  it("does not schedule anything when it is disabled", () => {
    render(<AutoRefresh intervalSeconds={10} enabled={false} />);

    expect(screen.getByText("Auto-refresh paused")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(60000);
    });
    expect(refreshMock).not.toHaveBeenCalled();
  });

  it("does not schedule anything for a non-positive interval", () => {
    render(<AutoRefresh intervalSeconds={0} />);

    act(() => {
      vi.advanceTimersByTime(60000);
    });
    expect(refreshMock).not.toHaveBeenCalled();
  });

  it("renders nothing when the label is turned off", () => {
    const { container } = render(<AutoRefresh intervalSeconds={10} showLabel={false} />);

    expect(container).toBeEmptyDOMElement();
  });
});
