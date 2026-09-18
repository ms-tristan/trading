import { EmptyState } from '@/components/ui/empty-state';
import { cn } from '@/lib/cn';
import { METRIC_NAMES, formatMetric, humaniseMetricName } from '@/lib/format';

/** Props of {@link MetricsPanel}. */
export interface MetricsPanelProps {
  /** Metric payload of one profile, exactly as `GET .../metrics` returns it. */
  metrics: Record<string, number | null>;
  className?: string;
}

/**
 * The frozen metric set, flattened into display order.
 *
 * `METRIC_NAMES` is the source of truth (a name may belong to several format
 * families, the first occurrence wins), so the panel never invents an order of
 * its own.
 */
export const KNOWN_METRIC_ORDER: readonly string[] = (() => {
  const seen = new Set<string>();
  const order: string[] = [];
  for (const names of Object.values(METRIC_NAMES)) {
    for (const name of names) {
      if (!seen.has(name)) {
        seen.add(name);
        order.push(name);
      }
    }
  }
  return order;
})();

/**
 * Sort metric names: the frozen order first, unknown names alphabetically after
 * it. Every key of the payload survives.
 */
export function orderMetricNames(names: readonly string[]): string[] {
  const rank = new Map(KNOWN_METRIC_ORDER.map((name, index) => [name, index]));
  return [...names].sort((left, right) => {
    const leftRank = rank.get(left);
    const rightRank = rank.get(right);
    if (leftRank !== undefined && rightRank !== undefined) {
      return leftRank - rightRank;
    }
    if (leftRank !== undefined) {
      return -1;
    }
    if (rightRank !== undefined) {
      return 1;
    }
    return left.localeCompare(right);
  });
}

/**
 * Metrics of one profile.
 *
 * Every key of the payload is rendered — never a silent subset — so a metric the
 * backend adds shows up without a frontend change. A `null` value is an em dash,
 * not a zero.
 */
export function MetricsPanel({ metrics, className }: MetricsPanelProps) {
  const names = orderMetricNames(Object.keys(metrics));

  if (names.length === 0) {
    return <EmptyState title="No metric yet" className={className} />;
  }

  return (
    <dl
      aria-label="Metrics"
      className={cn('grid gap-x-2xl gap-y-0 sm:grid-cols-2 lg:grid-cols-3', className)}
    >
      {names.map((name) => (
        <div
          key={name}
          className="flex items-baseline justify-between gap-md border-b border-border/60 py-sm"
        >
          <dt className="text-xs uppercase tracking-wide text-muted-foreground">
            {humaniseMetricName(name)}
          </dt>
          <dd className="font-mono text-sm tabular-nums text-foreground">
            {formatMetric(name, metrics[name] ?? null)}
          </dd>
        </div>
      ))}
    </dl>
  );
}
