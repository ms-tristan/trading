import { directionOf, formatProfitFactor } from "@/components/overview/aggregate";
import { KpiStat } from "@/components/ui/KpiStat";
import { SectionHeader } from "@/components/ui/SectionHeader";
import { cn } from "@/lib/cn";
import {
  formatDuration,
  formatRatioPercent,
  formatSignedRatioPercent,
  formatSignedUsdt,
  formatUsdt,
} from "@/lib/format";
import type { AccountPerformance, ProfileView } from "@/lib/types";

export interface ProfileKpiRowProps {
  /** The current state of the profile: value, capital and slot. */
  profile: ProfileView;
  /** The windowed performance block of the profile. */
  performance: AccountPerformance;
  /** Cash of the profile, `null` when the API publishes no way to know it. */
  cash: number | null;
  /** Value of the open positions, `null` when it cannot be derived. */
  positionsValue: number | null;
  /** Position of the profile in the ranked profile list, 1-based. */
  rank: number | null;
  /** Uptime in seconds, of the profile or of the engine process. */
  uptimeSeconds: number | null;
  className?: string;
}

const CELL = "col-span-6 md:col-span-4 lg:col-span-3";

/** `value` formatted as USDT, or an em dash when the API publishes none. */
function usdt(value: number | null): string {
  return value === null ? "\u2014" : formatUsdt(value);
}

/**
 * KPI row of one profile.
 *
 * The current value and the initial capital come from the profile record, the
 * profit, trade and risk figures from its windowed performance block. Cash and
 * the open-position value are resolved by `resolveCapital`: when the API
 * publishes no way to compute them the cell renders an em dash rather than a
 * made-up zero, and the hint says what the figure would mean.
 */
export function ProfileKpiRow({
  profile,
  performance,
  cash,
  positionsValue,
  rank,
  uptimeSeconds,
  className,
}: ProfileKpiRowProps) {
  return (
    <section aria-labelledby="profile-metrics-heading" className={cn("min-w-0", className)}>
      <SectionHeader
        id="profile-metrics-heading"
        title="Key metrics"
        subtitle={`${performance.window} window - value, profit and trade statistics of this profile`}
      />
      <div className="grid grid-cols-12 gap-2">
        <KpiStat
          className={CELL}
          label="Portfolio value"
          value={formatUsdt(profile.portfolio_value)}
          hint={`initial capital ${formatUsdt(profile.initial_capital)}`}
        />
        <KpiStat
          className={CELL}
          label="Cash"
          value={usdt(cash)}
          hint="portfolio value minus open positions"
        />
        <KpiStat
          className={CELL}
          label="Positions value"
          value={usdt(positionsValue)}
          hint={`${performance.open_trades} open ${
            performance.open_trades === 1 ? "trade" : "trades"
          }`}
        />
        <KpiStat
          className={CELL}
          label="Profit"
          value={formatSignedUsdt(performance.profit_usdt)}
          direction={directionOf(performance.profit_usdt)}
          hint="since the initial capital"
        />
        <KpiStat
          className={CELL}
          label="Profit %"
          value={formatSignedRatioPercent(performance.profit_pct)}
          direction={directionOf(performance.profit_pct)}
        />
        <KpiStat
          className={CELL}
          label="Realised P&L"
          value={formatSignedUsdt(performance.realised_profit_usdt)}
          direction={directionOf(performance.realised_profit_usdt)}
        />
        <KpiStat
          className={CELL}
          label="Unrealised P&L"
          value={formatSignedUsdt(performance.unrealised_profit_usdt)}
          direction={directionOf(performance.unrealised_profit_usdt)}
        />
        <KpiStat
          className={CELL}
          label="Open / closed trades"
          value={`${performance.open_trades} / ${performance.closed_trades}`}
        />
        <KpiStat
          className={CELL}
          label="Win rate"
          value={formatRatioPercent(performance.win_rate)}
          hint={`${performance.closed_trades} closed ${
            performance.closed_trades === 1 ? "trade" : "trades"
          }`}
        />
        <KpiStat
          className={CELL}
          label="Profit factor"
          value={formatProfitFactor(performance.profit_factor)}
        />
        <KpiStat
          className={CELL}
          label="Max drawdown"
          value={formatRatioPercent(performance.max_drawdown_pct)}
        />
        <KpiStat
          className={CELL}
          label="Rank"
          value={rank === null ? "\u2014" : `#${rank}`}
          hint="by portfolio value, descending"
        />
        <KpiStat
          className={CELL}
          label="Uptime"
          value={uptimeSeconds === null ? "\u2014" : formatDuration(uptimeSeconds)}
          hint={profile.state === "running" ? "worker holds an engine slot" : "engine process"}
        />
      </div>
    </section>
  );
}
