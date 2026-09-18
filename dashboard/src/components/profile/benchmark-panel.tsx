import { EmptyState } from '@/components/ui/empty-state';
import { cn } from '@/lib/cn';
import { formatInteger, formatMetric, formatMoney, humaniseMetricName } from '@/lib/format';
import type { BenchmarkPayload } from '@/lib/types';

import { orderMetricNames } from './metrics-panel';

/** Props of {@link BenchmarkPanel}. */
export interface BenchmarkPanelProps {
  /** Benchmark block of the metrics payload; `null` when none is configured. */
  benchmark: BenchmarkPayload | null;
  className?: string;
}

/** Labelled row of the benchmark summary. */
function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-md border-b border-border/60 py-sm">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="font-mono text-sm tabular-nums text-foreground">{value}</dd>
    </div>
  );
}

/**
 * Benchmark comparison of one profile.
 *
 * The comparison is optional in the API (`benchmark: null`), and an absent
 * benchmark is a legitimate state, not an error: it renders an empty state.
 */
export function BenchmarkPanel({ benchmark, className }: BenchmarkPanelProps) {
  if (benchmark === null) {
    return (
      <EmptyState
        title="No benchmark configured"
        description="Set a benchmark variant for this profile to compare it against a passive strategy."
        className={className}
      />
    );
  }

  const names = orderMetricNames(Object.keys(benchmark.metrics));

  return (
    <div className={cn('flex flex-col gap-lg', className)}>
      <dl aria-label="Benchmark summary" className="grid gap-x-2xl sm:grid-cols-2">
        <Row label="Variant" value={benchmark.variant} />
        <Row label="Timeframe" value={benchmark.timeframe} />
        <Row label="Periods" value={formatInteger(benchmark.n_periods)} />
        <Row label="Initial balance" value={formatMoney(benchmark.initial_balance)} />
        <Row label="Final balance" value={formatMoney(benchmark.final_balance)} />
      </dl>

      {names.length === 0 ? (
        <p className="text-sm text-muted-foreground">The benchmark carries no metric.</p>
      ) : (
        <dl
          aria-label="Benchmark metrics"
          className="grid gap-x-2xl gap-y-0 sm:grid-cols-2 lg:grid-cols-3"
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
                {formatMetric(name, benchmark.metrics[name] ?? null)}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
