import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ClosedTradesCard, OpenTradesCard } from "./ProfileTrades";
import { tradeRow } from "./fixtures";
import { readTradeList } from "./wire";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

const OPEN = readTradeList(
  [
    tradeRow({ trade_id: 12, profit_abs: 10, profit_pct: 1, current_rate: 60600 }),
    tradeRow({
      trade_id: 13,
      pair: "ETH/USDT",
      open_rate: 3000,
      current_rate: 2970,
      amount: 0.5,
      stake_amount: 1500,
      profit_abs: -15,
      profit_pct: -1,
      enter_tag: null,
    }),
  ],
  true,
);

const CLOSED = readTradeList(
  [
    tradeRow({
      trade_id: 41,
      is_open: false,
      close_date: "2026-09-27T11:00:00Z",
      close_rate: 61000,
      exit_reason: "roi",
      enter_tag: "ema_cross",
      profit_abs: 25,
      profit_pct: 2.5,
    }),
    tradeRow({
      trade_id: 40,
      pair: "ETH/USDT",
      is_open: false,
      close_date: "2026-09-27T10:00:00Z",
      close_rate: 2950,
      exit_reason: "stop_loss",
      enter_tag: null,
      profit_abs: -5,
      profit_pct: -0.5,
      profit_ratio: null,
    }),
  ],
  false,
);

describe("OpenTradesCard", () => {
  it("renders the freqtrade fields of an open trade", () => {
    render(<OpenTradesCard trades={OPEN} />);

    expect(screen.getByRole("heading", { level: 2, name: "Open trades" })).toBeInTheDocument();
    expect(screen.getByText("2 open positions")).toBeInTheDocument();

    const table = screen.getAllByRole("table")[0];
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);

    expect(rows[1]).toHaveTextContent("#12");
    expect(rows[1]).toHaveTextContent("BTC/USDT");
    expect(rows[1]).toHaveTextContent("2026-09-27 09:00:00 UTC");
    expect(rows[1]).toHaveTextContent("60,000");
    expect(rows[1]).toHaveTextContent("60,600");
    expect(rows[1]).toHaveTextContent("0.0166");
    expect(rows[1]).toHaveTextContent("1,000.00 USDT");
    expect(rows[1]).toHaveTextContent("+10.00 USDT");
    expect(rows[1]).toHaveTextContent("+1.00%");
    expect(rows[1]).toHaveTextContent("ema_cross");

    expect(rows[2]).toHaveTextContent("ETH/USDT");
    expect(rows[2]).toHaveTextContent("-15.00 USDT");
    expect(rows[2]).toHaveTextContent("-1.00%");
    // An absent enter tag is an em dash, never an empty cell.
    expect(rows[2]).toHaveTextContent("\u2014");
  });

  it("re-sorts the rows through the aria-sort header", () => {
    render(<OpenTradesCard trades={OPEN} />);

    const table = screen.getAllByRole("table")[0];
    const header = within(table).getByRole("button", { name: "Sort by Profit" });
    fireEvent.click(header);

    expect(header.closest("th")).toHaveAttribute("aria-sort", "ascending");
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("ETH/USDT");
    expect(rows[2]).toHaveTextContent("BTC/USDT");
  });

  it("says so when the profile holds no open trade", () => {
    render(<OpenTradesCard trades={[]} />);

    expect(screen.getByText("This profile holds no open trade.")).toBeInTheDocument();
    expect(screen.getByText("0 open positions")).toBeInTheDocument();
  });
});

describe("ClosedTradesCard", () => {
  it("renders the exit of every closed trade", () => {
    render(<ClosedTradesCard trades={CLOSED} />);

    expect(
      screen.getByRole("heading", { level: 2, name: "Recent closed trades" }),
    ).toBeInTheDocument();
    expect(screen.getByText("2 closed trades in the window")).toBeInTheDocument();

    const table = screen.getAllByRole("table")[0];
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("#41");
    expect(rows[1]).toHaveTextContent("2026-09-27 11:00:00 UTC");
    expect(rows[1]).toHaveTextContent("61,000");
    expect(rows[1]).toHaveTextContent("+25.00 USDT");
    expect(rows[1]).toHaveTextContent("+2.50%");
    expect(rows[1]).toHaveTextContent("roi");
    expect(rows[2]).toHaveTextContent("stop_loss");
    expect(rows[2]).toHaveTextContent("-0.50%");
  });

  it("says so when the window holds no closed trade", () => {
    render(<ClosedTradesCard trades={[]} />);

    expect(screen.getByText("No closed trade in the selected window.")).toBeInTheDocument();
    expect(screen.getByText("0 closed trades in the window")).toBeInTheDocument();
  });

  it("renders unknown numbers as an em dash", () => {
    const rows = readTradeList([{ trade_id: 7, pair: "BTC/USDT", is_open: false }], false);
    render(<ClosedTradesCard trades={rows} />);

    const table = screen.getAllByRole("table")[0];
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent("\u2014");
  });
});
