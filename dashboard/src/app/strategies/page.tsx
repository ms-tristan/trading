import {
  StrategyCard,
  type StrategyCardProps,
} from "@/components/strategies/StrategyCard";
import {
  bestProfileFor,
  strategyCardView,
  withDerivedProfileCount,
  type StrategyCardView,
} from "@/components/strategies/strategyMeta";
import { Card } from "@/components/ui/Card";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { SectionHeader } from "@/components/ui/SectionHeader";
import { fetchProfiles, fetchStrategies } from "@/lib/api";
import { formatSignedUsdt, formatUsdt } from "@/lib/format";
import {
  EMPTY_PROFILES,
  EMPTY_STRATEGIES,
} from "@/lib/types";

/** Live data on every request: the catalogue is never prerendered. */
export const dynamic = "force-dynamic";

/** Sum a numeric field of the catalogue, ignoring the unknown values. */
export function sumCatalogue(
  views: StrategyCardView[],
  pick: (view: StrategyCardView) => number,
): number {
  return views.reduce((total, view) => {
    const value = pick(view);
    return Number.isFinite(value) ? total + value : total;
  }, 0);
}

/**
 * Strategy catalogue.
 *
 * One card per strategy: what it does, which indicators it computes, where it
 * comes from and what the profiles holding it are worth. Two reads feed the page:
 * the catalogue itself (`GET /api/strategies`) and the ranked profile list
 * (`GET /api/profiles`), from which the **best profile** of each strategy is the
 * first match - the ranking is portfolio value, descending.
 *
 * Every card links to `/profiles?strategy=<id>`, the filtered profile index. A
 * failing read never blanks the page: the banner names what could not be loaded
 * and the cards render whatever the API did answer.
 */
export default async function StrategiesPage() {
  const [strategies, profiles] = await Promise.all([
    fetchStrategies(EMPTY_STRATEGIES),
    fetchProfiles(EMPTY_PROFILES),
  ]);

  const ranked = profiles.data.profiles;
  const views: StrategyCardView[] = [];
  for (const entry of strategies.data.strategies) {
    const view = strategyCardView(entry);
    if (view !== null) {
      views.push(withDerivedProfileCount(view, ranked));
    }
  }

  const cards: StrategyCardProps[] = views.map((view) => ({
    view,
    bestProfile: bestProfileFor(view.id, ranked),
  }));

  const totalValue = sumCatalogue(views, (view) => view.portfolioValue);
  const totalProfit = sumCatalogue(views, (view) => view.profitUsdt);

  return (
    <div className="grid grid-cols-12 gap-2">
      <SectionHeader
        className="col-span-12"
        title="Strategy catalogue"
        subtitle={`${views.length} ${
          views.length === 1 ? "strategy" : "strategies"
        } - ${formatUsdt(totalValue)} held by their profiles - ${formatSignedUsdt(totalProfit)}`}
      />

      <ErrorBanner
        className="col-span-12"
        error={strategies.error}
        title="The strategy catalogue could not be refreshed"
      />
      <ErrorBanner
        className="col-span-12"
        error={profiles.error}
        title="The best profile of each strategy could not be refreshed"
      />

      {cards.length === 0 ? (
        <Card className="col-span-12">
          <p className="text-sm text-muted-foreground">
            The API publishes no strategy yet.
          </p>
        </Card>
      ) : (
        cards.map((card, index) => (
          <StrategyCard
            key={card.view.id === "" ? `strategy-${index}` : card.view.id}
            className="col-span-12 md:col-span-6 xl:col-span-4"
            view={card.view}
            bestProfile={card.bestProfile ?? null}
          />
        ))
      )}
    </div>
  );
}
