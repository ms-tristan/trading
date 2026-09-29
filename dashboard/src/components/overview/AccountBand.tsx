import Link from "next/link";

import { KpiStat } from "@/components/ui/KpiStat";
import { Sparkline } from "@/components/ui/Sparkline";
import { cn } from "@/lib/cn";
import {
  directionClass,
  directionGlyph,
  directionLabel,
  formatRatioPercent,
  formatSignedRatioPercent,
  formatSignedUsdt,
  formatUsdt,
} from "@/lib/format";
import type { AccountPerformance, ApiWindow, EquityPoint } from "@/lib/types";

import { directionOf, formatProfitFactor } from "./aggregate";

export interface AccountBandProps {
  performance: AccountPerformance;
  /** Combined equity curve; only its portfolio value is plotted. */
  equity: EquityPoint[];
  /** Selected aggregation window. */
  window: ApiWindow;
  className?: string;
}

const WINDOWS: readonly ApiWindow[] = ["24h", "7d", "30d", "all"];

/**
 * Full-width account performance band leading the overview.
 *
 * Order of information: the combined portfolio value (large), the total profit
 * in USDT and percent with its direction arrow, the compact KPI row, then the
 * combined equity sparkline. The window switcher is a plain link list: it works
 * without JavaScript and every current entry is marked with `aria-current`.
 */
export function AccountBand({ performance, equity, window: apiWindow, className }: AccountBandProps) {
  const values = equity
    .map((point) => point.portfolio_value)
    .filter((value) => Number.isFinite(value));

  return (
    <section
      aria-labelledby="account-performance-heading"
      className={cn(
        "min-w-0 rounded-lg border border-border bg-card p-3 text-card-foreground",
        className,
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h1
            id="account-performance-heading"
            className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
          >
            Account performance
          </h1>
          <p className="mt-1 text-3xl font-semibold tabular-nums">
            {formatUsdt(performance.portfolio_value)}
          </p>
          <p className={cn("mt-1 text-base font-medium tabular-nums", directionClass(performance.profit_usdt))}>
            <span aria-hidden="true">{directionGlyph(performance.profit_usdt)}</span>{" "}
            {formatSignedUsdt(performance.profit_usdt)} (
            {formatSignedRatioPercent(performance.profit_pct)})
            <span className="sr-only"> {directionLabel(performance.profit_usdt)}</span>
          </p>
          <p className="mt-0.5 text-sm text-muted-foreground tabular-nums">
            initial capital {formatUsdt(performance.initial_capital)} over {apiWindow}
          </p>
        </div>

        <nav aria-label="Aggregation window" className="flex flex-wrap gap-1">
          {WINDOWS.map((entry) => (
            <Link
              key={entry}
              href={`/?window=${entry}`}
              aria-current={entry === apiWindow ? "true" : undefined}
              className={cn(
                "rounded-md border border-border px-2 py-1 text-sm transition-smooth",
                entry === apiWindow ? "bg-secondary text-foreground" : "text-muted-foreground",
              )}
            >
              {entry}
            </Link>
          ))}
        </nav>
      </div>

      <div className="mt-3 grid grid-cols-12 gap-2">
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-2"
          label="Realised P&L"
          value={formatSignedUsdt(performance.realised_profit_usdt)}
          direction={directionOf(performance.realised_profit_usdt)}
        />
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-2"
          label="Unrealised P&L"
          value={formatSignedUsdt(performance.unrealised_profit_usdt)}
          direction={directionOf(performance.unrealised_profit_usdt)}
        />
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-2"
          label="Open positions"
          value={String(performance.open_trades)}
        />
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-2"
          label="Closed trades"
          value={String(performance.closed_trades)}
        />
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-2"
          label="Win rate"
          value={formatRatioPercent(performance.win_rate)}
        />
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-2"
          label="Profit factor"
          value={formatProfitFactor(performance.profit_factor)}
        />
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-2"
          label="Max drawdown"
          value={formatRatioPercent(performance.max_drawdown_pct)}
        />
        <KpiStat
          className="col-span-6 md:col-span-4 lg:col-span-2"
          label="Profiles running"
          value={`${performance.profiles_running} of ${performance.profiles_total}`}
        />
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2">
        <Sparkline
          values={values}
          label={`Combined equity over ${apiWindow}`}
          width={320}
          height={48}
          summary={
            values.length >= 2
              ? `${directionGlyph(values[values.length - 1] - values[0])} ${formatSignedRatioPercent(
                  values[0] !== 0 ? (values[values.length - 1] - values[0]) / Math.abs(values[0]) : 0,
                )}`
              : "no curve yet"
          }
        />
        <p className="text-sm text-muted-foreground tabular-nums">
          {values.length} equity {values.length === 1 ? "point" : "points"}
        </p>
      </div>
    </section>
  );
}
