import Link from "next/link";

import { formatCount } from "@/components/operations/EngineStateCard";
import { directionOf, formatProfitFactor } from "@/components/overview/aggregate";
import { Card } from "@/components/ui/Card";
import { KpiStat } from "@/components/ui/KpiStat";
import { Sparkline } from "@/components/ui/Sparkline";
import { cn } from "@/lib/cn";
import {
  formatRatioPercent,
  formatSignedRatioPercent,
  formatSignedUsdt,
  formatUsdt,
} from "@/lib/format";
import type { ProfileView } from "@/lib/types";

import { splitReference, type StrategyCardView } from "./strategyMeta";

export interface StrategyCardProps {
  view: StrategyCardView;
  /** Best profile of the strategy: the first match of the API ranking. */
  bestProfile?: ProfileView | null;
  className?: string;
}

/**
 * One entry of the strategy catalogue.
 *
 * Reading order: the title and its category, the one-line summary, the full
 * description, the aggregated figures of the profiles that hold it, the traded
 * pairs and timeframes, the indicators it computes, its reference and its risk
 * note, and finally the two ways to leave the card - the best profile of the
 * strategy and the filtered profile list. A strategy the profile detail embeds
 * carries no aggregate, and the card then simply omits that block instead of
 * showing four dashes.
 */
export function StrategyCard({ view, bestProfile = null, className }: StrategyCardProps) {
  const reference = view.reference === null ? null : splitReference(view.reference);
  const hasAggregates =
    Number.isFinite(view.profilesTotal) ||
    Number.isFinite(view.portfolioValue) ||
    Number.isFinite(view.profitUsdt);
  const hasTrades = Number.isFinite(view.openTrades) || Number.isFinite(view.closedTrades);

  return (
    <Card
      className={cn("min-w-0", className)}
      title={view.title === "" ? "Unnamed strategy" : view.title}
      description={view.summary ?? undefined}
      actions={
        view.category === null ? undefined : (
          <span className="rounded-full border border-border px-2 py-0.5 text-sm text-muted-foreground">
            {view.category}
          </span>
        )
      }
    >
      {view.id !== "" ? <p className="font-mono text-sm text-muted-foreground">{view.id}</p> : null}

      {view.description !== null ? (
        <p className="mt-2 text-sm text-muted-foreground">{view.description}</p>
      ) : null}

      {hasAggregates ? (
        <div className="mt-3 grid grid-cols-12 gap-2">
          <KpiStat
            className="col-span-6"
            label="Profiles"
            value={formatCount(view.profilesTotal)}
            hint={`${formatCount(view.profilesRunning)} running`}
          />
          <KpiStat
            className="col-span-6"
            label="Portfolio value"
            value={formatUsdt(view.portfolioValue)}
          />
          <KpiStat
            className="col-span-6"
            label="P&L"
            value={formatSignedUsdt(view.profitUsdt)}
            direction={directionOf(view.profitUsdt)}
            hint={formatSignedRatioPercent(view.profitPct)}
          />
          <KpiStat
            className="col-span-6"
            label="Win rate"
            value={formatRatioPercent(view.winRate)}
            hint={`profit factor ${formatProfitFactor(view.profitFactor)}`}
          />
        </div>
      ) : null}

      {hasTrades ? (
        <p className="mt-2 text-sm text-muted-foreground tabular-nums">
          {formatCount(view.openTrades)} open / {formatCount(view.closedTrades)} closed trades
        </p>
      ) : null}

      <dl className="mt-3 grid grid-cols-1 gap-x-4 gap-y-1 text-sm">
        {view.timeframes.length > 0 ? (
          <div className="flex min-w-0 flex-wrap gap-x-2">
            <dt className="text-muted-foreground">Timeframes</dt>
            <dd className="min-w-0 tabular-nums">{view.timeframes.join(", ")}</dd>
          </div>
        ) : null}
        {view.pairs.length > 0 ? (
          <div className="flex min-w-0 flex-wrap gap-x-2">
            <dt className="text-muted-foreground">Pairs</dt>
            <dd className="min-w-0 font-mono">{view.pairs.join(", ")}</dd>
          </div>
        ) : null}
        {view.indicators.length > 0 ? (
          <div className="flex min-w-0 flex-wrap gap-x-2">
            <dt className="text-muted-foreground">Indicators</dt>
            <dd className="min-w-0">
              <span className="flex min-w-0 flex-wrap gap-1">
                {view.indicators.map((indicator) => (
                  <span
                    key={indicator}
                    className="rounded-sm border border-border px-1 font-mono text-sm"
                  >
                    {indicator}
                  </span>
                ))}
              </span>
            </dd>
          </div>
        ) : null}
      </dl>

      {reference !== null ? (
        <p className="mt-2 min-w-0 text-sm text-muted-foreground">
          Reference: {reference.label}
          {reference.url === null ? null : (
            <>
              {" "}
              <a
                href={reference.url}
                target="_blank"
                rel="noreferrer noopener"
                className="break-all underline underline-offset-2"
              >
                {reference.url}
              </a>
            </>
          )}
        </p>
      ) : null}

      {view.riskNotes !== null ? (
        <p className="mt-2 text-sm text-muted-foreground">Risk: {view.riskNotes}</p>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2">
        <Sparkline
          values={view.sparkline}
          label={`${view.title} portfolio value`}
          width={120}
          height={28}
        />
        {view.id !== "" ? (
          <Link
            href={`/profiles?strategy=${encodeURIComponent(view.id)}`}
            aria-label={`View the profiles of ${view.title}`}
            className="rounded-md border border-border px-2 py-1 text-sm transition-smooth hover:border-ring"
          >
            View profiles
          </Link>
        ) : null}
      </div>

      {bestProfile === null ? (
        hasAggregates ? (
          <p className="mt-2 text-sm text-muted-foreground">
            No profile of this strategy is ranked yet.
          </p>
        ) : null
      ) : (
        <p className="mt-2 text-sm text-muted-foreground">
          Best profile:{" "}
          <Link
            href={`/profiles/${encodeURIComponent(bestProfile.id)}`}
            className="text-foreground underline-offset-2 hover:underline"
          >
            {bestProfile.name}
          </Link>{" "}
          <span className="tabular-nums">{formatUsdt(bestProfile.portfolio_value)}</span>
        </p>
      )}
    </Card>
  );
}
