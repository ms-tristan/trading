import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { ProfileView } from "@/lib/types";

import { ModeSection } from "./ModeSection";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

function profile(overrides: Partial<ProfileView> & { id: string }): ProfileView {
  return {
    name: overrides.id,
    strategy: "BasicStrategy",
    strategy_title: "Basic momentum",
    timeframe: "5m",
    pairs: ["BTC/USDT"],
    mode: "paper",
    state: "running",
    state_reason: null,
    portfolio_value: 0,
    initial_capital: 1000,
    profit_usdt: 0,
    profit_pct: 0,
    open_trades: 0,
    closed_trades: 0,
    win_rate: 0,
    profit_factor: 0,
    max_drawdown_pct: 0,
    engine_slot: null,
    api_port: null,
    sparkline: [],
    updated_at: "2026-09-27T00:00:00Z",
    ...overrides,
  };
}

/** Already ranked by portfolio value, descending: exactly what the API sends. */
const RANKED: ProfileView[] = [
  profile({
    id: "bravo",
    name: "Bravo",
    strategy_title: "Zeta breakout",
    portfolio_value: 1300,
    profit_usdt: 300,
    profit_pct: 0.3,
    sparkline: [1000, 1300],
  }),
  profile({
    id: "alpha",
    name: "Alpha",
    portfolio_value: 1100,
    profit_usdt: 100,
    profit_pct: 0.1,
    open_trades: 2,
    closed_trades: 5,
    win_rate: 0.6,
  }),
  profile({
    id: "charlie",
    name: "Charlie",
    portfolio_value: 950,
    profit_usdt: -50,
    profit_pct: -0.05,
    state: "queued",
    state_reason: "waiting for an engine slot",
  }),
];

function bodyRows(): HTMLElement[] {
  const groups = screen.getAllByRole("rowgroup");
  return within(groups[1]).getAllByRole("row");
}

/** The aggregate line `SectionHeader` renders just under the title. */
function aggregateLine(): HTMLElement {
  return screen.getByText(/running/);
}

/**
 * Text nodes of a rendered subtree, each trimmed.
 *
 * Assertions on wording walk the text nodes instead of `container.textContent`:
 * an ancestor's `textContent` concatenates every descendant, so it can make a
 * word that only exists in one place look like it also exists in another.
 */
function textNodes(node: Node): string[] {
  const texts: string[] = [];
  for (const child of Array.from(node.childNodes)) {
    if (child.nodeType === Node.TEXT_NODE) {
      const text = child.textContent?.trim();
      if (text) {
        texts.push(text);
      }
    } else {
      texts.push(...textNodes(child));
    }
  }
  return texts;
}

describe("ModeSection", () => {
  it("heads the section with its title, count and aggregate line", () => {
    render(<ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />);

    const heading = screen.getByRole("heading", { level: 2, name: /Paper trading/ });
    expect(heading).toHaveAttribute("id", "paper-trading");
    expect(screen.getByText("3 profiles")).toBeInTheDocument();
    expect(aggregateLine()).toHaveTextContent(
      /2 running - 1 profile is waiting for a slot - 3,350\.00 USDT - \+350\.00 USDT \(\+11\.67%\)/,
    );
  });

  it("describes one waiting profile in the singular", () => {
    render(<ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />);

    expect(aggregateLine()).toHaveTextContent("1 profile is waiting for a slot");
  });

  it("describes several waiting profiles in the plural", () => {
    render(
      <ModeSection
        id="paper-trading"
        title="Paper trading"
        profiles={[RANKED[2], profile({ id: "delta", name: "Delta", state: "queued" })]}
      />,
    );

    expect(aggregateLine()).toHaveTextContent("2 profiles are waiting for a slot");
  });

  it("reserves the 'needing attention' wording for error and blocked profiles", () => {
    render(
      <ModeSection
        id="paper-trading"
        title="Paper trading"
        profiles={[profile({ id: "errored", name: "Errored", state: "error" })]}
      />,
    );

    const line = aggregateLine();
    expect(line).toHaveTextContent("0 running");
    expect(line).toHaveTextContent("1 needing attention");
    expect(line).not.toHaveTextContent("waiting for a slot");
  });

  it("renders an explanatory summary as visible text between the header and the table", () => {
    render(
      <ModeSection
        id="paper-trading"
        title="Paper trading"
        profiles={RANKED}
        summary="The engine runs at most 2 profiles at once."
      />,
    );

    const sentence = screen.getByText("The engine runs at most 2 profiles at once.");
    expect(sentence.tagName).toBe("P");

    // The summary is visible text above the table, in document order.
    const rows = bodyRows();
    expect(sentence.compareDocumentPosition(rows[0]) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });

  it("renders no summary at all when none is given", () => {
    const { container } = render(
      <ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />,
    );

    expect(container.querySelectorAll("section p")).toHaveLength(1);
  });

  it("omits a blank summary instead of rendering an empty paragraph", () => {
    const { container } = render(
      <ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} summary="   " />,
    );

    expect(container.querySelectorAll("section p")).toHaveLength(1);
  });

  it("lists the profiles in the API order and numbers them", () => {
    render(<ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />);

    const rows = bodyRows();
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("Bravo");
    expect(rows[1]).toHaveTextContent("Alpha");
    expect(rows[2]).toHaveTextContent("Charlie");
    expect(within(rows[0]).getByText("1")).toBeInTheDocument();
    expect(within(rows[2]).getByText("3")).toBeInTheDocument();
  });

  it("shows strategy, timeframe, pair, value, profit, trades and win rate of a row", () => {
    render(<ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />);
    const row = bodyRows()[0];

    expect(within(row).getByText("Zeta breakout")).toBeInTheDocument();
    expect(within(row).getByText("5m")).toBeInTheDocument();
    expect(within(row).getByText("BTC/USDT")).toBeInTheDocument();
    expect(within(row).getByText("1,300.00 USDT")).toBeInTheDocument();
    expect(within(row).getByText(/\+300\.00 USDT/)).toBeInTheDocument();
    expect(within(row).getByText("0 / 0")).toBeInTheDocument();
    expect(within(row).getByText("0.00%")).toBeInTheDocument();
  });

  it("draws the per-row equity sparkline and skips it when there is no series", () => {
    render(<ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />);
    const rows = bodyRows();

    // row 0: the profit % cell and the sparkline summary carry the same wording
    expect(within(rows[0]).getByText("+30.00%")).toBeInTheDocument();
    expect(within(rows[0]).getByText("\u25B2 +30.00%")).toBeInTheDocument();

    // The chart is announced: a named image with the same title as its label.
    const chart = rows[0].querySelector("svg");
    expect(chart).not.toBeNull();
    expect(chart).not.toHaveAttribute("aria-hidden", "true");
    expect(chart).toHaveAttribute("role", "img");
    expect(chart).toHaveAttribute("aria-label", "Bravo portfolio value");
    expect(chart?.querySelector("title")).toHaveTextContent("Bravo portfolio value");
    expect(within(rows[0]).getByRole("img", { name: "Bravo portfolio value" })).toBeInTheDocument();

    // row 2 has no series at all: wording only, never an empty chart
    expect(within(rows[2]).getByText("no data")).toBeInTheDocument();
    expect(rows[2].querySelector("svg")).toBeNull();
  });

  it("links every row to its profile page", () => {
    render(<ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />);

    expect(screen.getByRole("link", { name: "Bravo" })).toHaveAttribute(
      "href",
      "/profiles/bravo",
    );
    expect(screen.getByRole("link", { name: "Charlie" })).toHaveAttribute(
      "href",
      "/profiles/charlie",
    );
  });

  it("keeps a queued row in the ranking, visually distinct and with its reason", () => {
    render(<ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />);
    const queued = bodyRows()[2];

    expect(queued).toHaveClass("bg-muted/50");
    expect(within(queued).getByText("Queued")).toBeInTheDocument();
    expect(within(queued).getByText("waiting for an engine slot")).toBeInTheDocument();
  });

  it("explains a queued row as a wait, never as something needing attention", () => {
    render(<ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />);
    const queued = bodyRows()[2];

    expect(queued).toHaveTextContent("waiting for an engine slot");
    expect(within(queued).getByText("waiting for an engine slot")).toBeInTheDocument();

    // No rendered text node of the section ever says "needing attention" for a
    // queued profile. The line break of a text node is normalised away first,
    // so the phrase cannot hide behind it.
    const rendered = textNodes(document.body)
      .join(" ")
      .replace(/\s+/g, " ");
    expect(rendered).not.toContain("needing attention");
  });

  it("marks the trading mode of every row next to its state badge", () => {
    render(<ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />);

    for (const row of bodyRows()) {
      expect(within(row).getByText("paper")).toBeInTheDocument();
    }
  });

  it("renders the section notice between the header and the table", () => {
    render(
      <ModeSection
        id="real-trading"
        title="Real trading"
        profiles={[]}
        notice={<p>Live trading is not enabled</p>}
      />,
    );

    expect(screen.getByText("Live trading is not enabled")).toBeInTheDocument();
  });

  it("renders the empty message when the section has no profile", () => {
    render(
      <ModeSection
        id="real-trading"
        title="Real trading"
        profiles={[]}
        emptyMessage="No live profile is configured yet."
      />,
    );

    expect(screen.getByText("No live profile is configured yet.")).toBeInTheDocument();
    expect(screen.getByText("0 profiles")).toBeInTheDocument();
  });

  it("re-sorts on demand without losing the portfolio-value rank", () => {
    render(<ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />);

    fireEvent.click(screen.getByRole("button", { name: "Sort by Profile" }));

    const rows = bodyRows();
    expect(rows[0]).toHaveTextContent("Alpha");
    expect(within(rows[0]).getByText("2")).toBeInTheDocument();
    expect(rows[2]).toHaveTextContent("Charlie");
  });

  it("exposes the contract sort keys on the ranking columns", () => {
    render(<ModeSection id="paper-trading" title="Paper trading" profiles={RANKED} />);

    for (const header of ["Profile", "Strategy", "Value", "Profit"]) {
      expect(screen.getByRole("button", { name: `Sort by ${header}` })).toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: "Sort by Timeframe" })).not.toBeInTheDocument();
  });
});
