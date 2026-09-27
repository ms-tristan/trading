"use client";

import { Card } from "@/components/ui/Card";
import { DataTable, type DataTableColumn } from "@/components/ui/DataTable";
import { directionClass, directionGlyph, formatSignedPercent, formatSignedUsdt, formatTimestamp, formatUsdt } from "@/lib/format";

import { formatTradeNumber, type TradeRow } from "./wire";

/** Profit in USDT, with its direction glyph and colour; em dash when unknown. */
function ProfitCell({ value }: { value: number | null }) {
  if (value === null) {
    return <span className="text-muted-foreground">{"\u2014"}</span>;
  }
  return (
    <span className={directionClass(value)}>
      <span aria-hidden="true">{directionGlyph(value)}</span> {formatSignedUsdt(value)}
    </span>
  );
}

/** Profit in percent, with its direction colour; em dash when unknown. */
function ProfitPercentCell({ value }: { value: number | null }) {
  if (value === null) {
    return <span className="text-muted-foreground">{"\u2014"}</span>;
  }
  return <span className={directionClass(value)}>{formatSignedPercent(value)}</span>;
}

/** Plain text cell: an unparsable field renders as an em dash. */
function TextCell({ value }: { value: string | null }) {
  return value === null ? (
    <span className="text-muted-foreground">{"\u2014"}</span>
  ) : (
    <span>{value}</span>
  );
}

const OPEN_COLUMNS: DataTableColumn<TradeRow>[] = [
  {
    id: "trade_id",
    header: "Trade",
    headerClassName: "w-16",
    render: (row) =>
      row.tradeId === "" ? (
        <span className="text-muted-foreground">{"\u2014"}</span>
      ) : (
        <span className="tabular-nums text-muted-foreground">#{row.tradeId}</span>
      ),
  },
  {
    id: "pair",
    header: "Pair",
    render: (row) => <span className="font-mono text-sm">{row.pair}</span>,
  },
  {
    id: "opened",
    header: "Opened",
    render: (row) => <span className="tabular-nums">{formatTimestamp(row.openedAt ?? "")}</span>,
  },
  {
    id: "open_rate",
    header: "Open rate",
    align: "end",
    sortValue: (row) => row.openRate ?? Number.NaN,
    render: (row) => formatTradeNumber(row.openRate),
  },
  {
    id: "current_rate",
    header: "Current rate",
    align: "end",
    sortValue: (row) => row.currentRate ?? Number.NaN,
    render: (row) => formatTradeNumber(row.currentRate),
  },
  {
    id: "amount",
    header: "Amount",
    align: "end",
    sortValue: (row) => row.amount ?? Number.NaN,
    render: (row) => formatTradeNumber(row.amount),
  },
  {
    id: "stake_amount",
    header: "Stake",
    align: "end",
    sortValue: (row) => row.stakeAmount ?? Number.NaN,
    render: (row) => (row.stakeAmount === null ? "\u2014" : formatUsdt(row.stakeAmount)),
  },
  {
    id: "profit_abs",
    header: "Profit",
    align: "end",
    sortValue: (row) => row.profitAbs ?? Number.NaN,
    render: (row) => <ProfitCell value={row.profitAbs} />,
  },
  {
    id: "profit_pct",
    header: "Profit %",
    align: "end",
    sortValue: (row) => row.profitPercent ?? Number.NaN,
    render: (row) => <ProfitPercentCell value={row.profitPercent} />,
  },
  {
    id: "enter_tag",
    header: "Enter tag",
    render: (row) => <TextCell value={row.enterTag} />,
  },
];

const CLOSED_COLUMNS: DataTableColumn<TradeRow>[] = [
  ...OPEN_COLUMNS.slice(0, 3),
  {
    id: "closed",
    header: "Closed",
    render: (row) => <span className="tabular-nums">{formatTimestamp(row.closedAt ?? "")}</span>,
  },
  {
    id: "open_rate",
    header: "Open rate",
    align: "end",
    sortValue: (row) => row.openRate ?? Number.NaN,
    render: (row) => formatTradeNumber(row.openRate),
  },
  {
    id: "close_rate",
    header: "Close rate",
    align: "end",
    sortValue: (row) => row.closeRate ?? Number.NaN,
    render: (row) => formatTradeNumber(row.closeRate),
  },
  {
    id: "amount",
    header: "Amount",
    align: "end",
    sortValue: (row) => row.amount ?? Number.NaN,
    render: (row) => formatTradeNumber(row.amount),
  },
  {
    id: "stake_amount",
    header: "Stake",
    align: "end",
    sortValue: (row) => row.stakeAmount ?? Number.NaN,
    render: (row) => (row.stakeAmount === null ? "\u2014" : formatUsdt(row.stakeAmount)),
  },
  {
    id: "profit_abs",
    header: "Profit",
    align: "end",
    sortValue: (row) => row.profitAbs ?? Number.NaN,
    render: (row) => <ProfitCell value={row.profitAbs} />,
  },
  {
    id: "profit_pct",
    header: "Profit %",
    align: "end",
    sortValue: (row) => row.profitPercent ?? Number.NaN,
    render: (row) => <ProfitPercentCell value={row.profitPercent} />,
  },
  {
    id: "exit_reason",
    header: "Exit reason",
    render: (row) => <TextCell value={row.exitReason} />,
  },
  {
    id: "enter_tag",
    header: "Enter tag",
    render: (row) => <TextCell value={row.enterTag} />,
  },
];

export interface TradesCardProps {
  trades: TradeRow[];
  className?: string;
}

/**
 * Trades the profile holds right now.
 *
 * The rows are the freqtrade ones the API relays, in the order it published
 * them; an operator can re-sort any column through its `aria-sort` header. The
 * table scrolls inside its own container, so the page never scrolls sideways.
 */
export function OpenTradesCard({ trades, className }: TradesCardProps) {
  return (
    <Card
      className={className}
      headingLevel={2}
      title="Open trades"
      description={`${trades.length} open ${trades.length === 1 ? "position" : "positions"}`}
    >
      <DataTable
        columns={OPEN_COLUMNS}
        rows={trades}
        rowKey={(row) => `${row.tradeId}-${row.pair}-${row.openedAt ?? ""}`}
        caption="Open trades of this profile"
        emptyMessage="No open position"
      />
    </Card>
  );
}

/** Recently closed trades of the profile, newest first, as the API orders them. */
export function ClosedTradesCard({ trades, className }: TradesCardProps) {
  return (
    <Card
      className={className}
      headingLevel={2}
      title="Recent closed trades"
      description={`${trades.length} closed ${trades.length === 1 ? "trade" : "trades"} in the window`}
    >
      <DataTable
        columns={CLOSED_COLUMNS}
        rows={trades}
        rowKey={(row) => `${row.tradeId}-${row.pair}-${row.openedAt ?? ""}`}
        caption="Recent closed trades of this profile"
        emptyMessage="No closed trade yet"
        minWidthClassName="min-w-[1080px]"
      />
    </Card>
  );
}
